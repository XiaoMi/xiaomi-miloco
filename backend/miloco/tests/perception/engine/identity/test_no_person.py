"""no_person（非人误检抑制）单测。

覆盖：
  - 解析层：``parse_identity_assignments`` 把 omni name="no_person" 标成 no_person=True，
    区别于 unknown（有人但认不出）；且与 unknown 同口径放宽匹配（附注 / 空格 / 连字符变体）。
  - 状态机：``record_no_person`` 连续 2 票才落定、confirmed 成员不被翻；落定后
    ``get_face_id_value``→"none"。
  - 三重解除通道：① 多票；② ``clear_no_person_on_motion`` 明显移动回 pending；
    ③ ``needs_omni_call`` 慢周期重审 + ``clear_no_person_to_pending`` 判到人回 pending。
  - 编排层：``_make_on_result`` 的投票清零 / confirmed 早退不误伤 / 慢重审回 pending。
"""

from __future__ import annotations

import asyncio
import json

from miloco.perception.engine.config import IdentityEngineConfig, StabilityConfigDC
from miloco.perception.engine.identity.dispatcher import (
    FusedDispatcher,
    IdentityQueryItem,
    OmniIdentityResult,
)
from miloco.perception.engine.identity.engine import IdentityEngine, _bbox_iou
from miloco.perception.engine.identity.state import (
    TrackIdentityState,
    clear_no_person_on_motion,
    clear_no_person_to_pending,
    get_face_id_value,
    needs_omni_call,
    record_no_person,
)
from miloco.perception.engine.omni.response_parser import parse_identity_assignments


def _resp(identities: list[dict]) -> str:
    return json.dumps({"identities": identities})


class TestParseNoPerson:
    def test_no_person_flagged_and_not_a_person(self):
        out = parse_identity_assignments(
            _resp([{"track_id": 7, "name": "no_person", "confidence": 0.9, "reason": "空框"}]),
            prompt_track_ids={7},
        )
        assert len(out) == 1
        assert out[0]["no_person"] is True
        # no_person 不是身份 → person_id 归 "unknown"（下游靠 no_person 标志分流，不当成员）
        assert out[0]["person_id"] == "unknown"

    def test_unknown_is_not_no_person(self):
        out = parse_identity_assignments(
            _resp([{"track_id": 7, "name": "unknown", "confidence": 0.9}]),
            prompt_track_ids={7},
        )
        assert out[0]["no_person"] is False
        assert out[0]["person_id"] == "unknown"

    def test_named_member_is_not_no_person(self):
        out = parse_identity_assignments(
            _resp([{"track_id": 7, "name": "张三", "confidence": 0.95}]),
            name_to_pid={"张三": "pid-zhang"},
            prompt_track_ids={7},
        )
        assert out[0]["no_person"] is False
        assert out[0]["person_id"] == "pid-zhang"

    def test_no_person_variants_still_flagged(self):
        """omni 回显变体（附注 / 空格 / 连字符 / 大小写）仍判 no_person，不静默退化成 unknown。"""
        for name in ("no_person（3D打印机）", "No Person", "no-person", "NO_PERSON."):
            out = parse_identity_assignments(
                _resp([{"track_id": 7, "name": name, "confidence": 0.9}]),
                prompt_track_ids={7},
            )
            assert out[0]["no_person"] is True, name


class TestRecordNoPerson:
    def test_two_votes_to_commit(self):
        state = TrackIdentityState(track_id=1, status="pending")
        assert record_no_person(state, anchor_bbox=(10, 10, 50, 90), vote_threshold=2) is False
        assert state.status == "pending"
        assert state.no_person_vote_count == 1
        committed = record_no_person(state, anchor_bbox=(10, 10, 50, 90), vote_threshold=2)
        assert committed is True
        assert state.status == "no_person"
        assert state.no_person_anchor_bbox == (10, 10, 50, 90)

    def test_confirmed_member_not_flipped(self):
        state = TrackIdentityState(
            track_id=1, status="confirmed", committed_person_id="pid-x",
        )
        for _ in range(3):
            assert record_no_person(state, anchor_bbox=(0, 0, 1, 1), vote_threshold=2) is False
        assert state.status == "confirmed"
        assert state.committed_person_id == "pid-x"
        assert state.no_person_vote_count == 0

    def test_commit_clears_identity_fields(self):
        state = TrackIdentityState(
            track_id=1, status="unknown", unknown_index=3,
            candidate_person_id="pid-y", committed_person_id="pid-y",
        )
        record_no_person(state, anchor_bbox=(5, 5, 25, 65), vote_threshold=1)
        assert state.status == "no_person"
        assert state.unknown_index is None
        assert state.candidate_person_id is None
        assert state.committed_person_id is None


class TestNoPersonGates:
    def test_premarked_never_rechecks(self):
        """reject_region 预标（从未派过 omni，last_omni_call_frame==0）→ 永不时间重审，
        即便 now_frame 已很大（避免流跑久后一上来就 ≥ 周期而立即误派）。"""
        state = TrackIdentityState(track_id=1, status="no_person")  # last_omni_call_frame=0
        assert needs_omni_call(
            state, now_frame=10_000, now_ts=0.0, min_dispatch_interval_sec=5.0,
            config=StabilityConfigDC(), engine_fps=3, no_person_recheck_sec=10.0,
        ) is False

    def test_recheck_fires_after_period(self):
        """落定 no_person（曾派发，last_omni_call_frame>0）→ 距上次派发满周期才放行慢重审。"""
        state = TrackIdentityState(track_id=1, status="no_person", last_omni_call_frame=100)
        # no_person_recheck_sec=10s × fps=3 → interval=30 帧
        assert needs_omni_call(state, 100 + 29, 0.0, 5.0, StabilityConfigDC(), 3, 10.0) is False
        assert needs_omni_call(state, 100 + 30, 0.0, 5.0, StabilityConfigDC(), 3, 10.0) is True

    def test_inflight_blocks_recheck(self):
        """重审在途（inflight）→ 即便已过周期也不重复派。"""
        state = TrackIdentityState(
            track_id=1, status="no_person", last_omni_call_frame=100, inflight=True,
        )
        assert needs_omni_call(state, 100 + 999, 0.0, 5.0, StabilityConfigDC(), 3, 10.0) is False

    def test_face_id_value_none(self):
        state = TrackIdentityState(track_id=1, status="no_person")
        assert get_face_id_value(state, distinguish=False) == "none"


class TestClearNoPersonOnMotion:
    def test_static_keeps_no_person(self):
        state = TrackIdentityState(
            track_id=1, status="no_person", no_person_anchor_bbox=(100, 100, 200, 300),
        )
        # 抖动级位移（几像素）→ 不解除
        moved = clear_no_person_on_motion(
            state, (102, 101, 202, 301), displacement_ratio=0.15, min_abs_px=30.0,
        )
        assert moved is False
        assert state.status == "no_person"

    def test_clear_motion_back_to_pending(self):
        state = TrackIdentityState(
            track_id=1, status="no_person", no_person_anchor_bbox=(100, 100, 200, 300),
        )
        # 整框平移到远处（中心位移远超阈值）→ 解除回 pending 重新识别
        moved = clear_no_person_on_motion(
            state, (500, 500, 600, 700), displacement_ratio=0.15, min_abs_px=30.0,
        )
        assert moved is True
        assert state.status == "pending"
        assert state.no_person_vote_count == 0
        assert state.no_person_anchor_bbox is None


def _reject_engine(*, reject_enabled: bool = True, clear_iou: float = 0.3) -> IdentityEngine:
    """构造仅含 _apply_reject_regions 所需属性的轻量 engine（绕过 __init__）。"""
    eng = IdentityEngine.__new__(IdentityEngine)
    cfg = IdentityEngineConfig()
    cfg.no_person.enabled = True
    cfg.no_person.reject_region_enabled = reject_enabled
    cfg.no_person.reject_region_clear_iou = clear_iou
    eng.config = cfg
    eng.cam_id = "camA"
    eng._states = {}
    eng._latest_bbox = {}
    eng._detected_this_frame = {}
    eng._no_person_regions = []
    return eng


_FAR_FUTURE = 9_999_999_999.0  # 远未来 expiry，确保 TTL 不在测试中过期


class TestBboxIou:
    def test_identical(self):
        assert _bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0

    def test_disjoint(self):
        assert _bbox_iou((0, 0, 10, 10), (100, 100, 110, 110)) == 0.0

    def test_half_overlap(self):
        # inter = 5×10 = 50; union = 100 + 100 − 50 = 150
        assert abs(_bbox_iou((0, 0, 10, 10), (5, 0, 15, 10)) - 50 / 150) < 1e-9


class TestRejectRegion:
    def test_premark_new_track_in_region(self):
        eng = _reject_engine()
        eng._no_person_regions = [((100, 100, 200, 300), _FAR_FUTURE)]
        st = TrackIdentityState(track_id=5, status="pending")  # 全新: last_omni_call_frame=0, vote=0
        eng._states[5] = st
        eng._latest_bbox[5] = (100, 100, 200, 300)
        eng._detected_this_frame[5] = True
        eng._apply_reject_regions({5}, now_ts=1000.0)
        assert st.status == "no_person"
        assert st.no_person_anchor_bbox == (100, 100, 200, 300)

    def test_disabled_skips_premark(self):
        eng = _reject_engine(reject_enabled=False)
        eng._no_person_regions = [((100, 100, 200, 300), _FAR_FUTURE)]
        st = TrackIdentityState(track_id=5, status="pending")
        eng._states[5] = st
        eng._latest_bbox[5] = (100, 100, 200, 300)
        eng._detected_this_frame[5] = True
        eng._apply_reject_regions({5}, now_ts=1000.0)
        assert st.status == "pending"

    def test_already_dispatched_track_not_premarked(self):
        eng = _reject_engine()
        eng._no_person_regions = [((100, 100, 200, 300), _FAR_FUTURE)]
        st = TrackIdentityState(track_id=5, status="pending", last_omni_call_frame=42)
        eng._states[5] = st
        eng._latest_bbox[5] = (100, 100, 200, 300)
        eng._detected_this_frame[5] = True
        eng._apply_reject_regions({5}, now_ts=1000.0)
        assert st.status == "pending"  # 非全新 → 不预标

    def test_confirmed_track_disarms_region(self):
        eng = _reject_engine()
        eng._no_person_regions = [((100, 100, 200, 300), _FAR_FUTURE)]
        st = TrackIdentityState(track_id=9, status="confirmed", committed_person_id="pid")
        eng._states[9] = st
        eng._latest_bbox[9] = (100, 100, 200, 300)
        eng._detected_this_frame[9] = True
        eng._apply_reject_regions({9}, now_ts=1000.0)
        assert eng._no_person_regions == []  # 真人覆盖 → 区域解除

    def test_ttl_expiry_drops_region(self):
        eng = _reject_engine()
        eng._no_person_regions = [((100, 100, 200, 300), 500.0)]  # 已过期(< now 1000)
        eng._apply_reject_regions(set(), now_ts=1000.0)
        assert eng._no_person_regions == []


class TestClearNoPersonToPending:
    def test_recheck_saw_person_back_to_pending(self):
        """慢重审判到人 → 回 pending、清票 / 锚点、重置 pending 起始时间。"""
        state = TrackIdentityState(
            track_id=1, status="no_person",
            no_person_vote_count=2, no_person_anchor_bbox=(10, 10, 50, 90),
        )
        clear_no_person_to_pending(state, now_ts=1234.0)
        assert state.status == "pending"
        assert state.no_person_vote_count == 0
        assert state.no_person_anchor_bbox is None
        assert state.pending_started_ts == 1234.0


def _on_result_engine() -> IdentityEngine:
    """构造仅含 _make_on_result 所需属性的轻量 engine（绕过 __init__）。"""
    eng = IdentityEngine.__new__(IdentityEngine)
    cfg = IdentityEngineConfig()
    cfg.no_person.enabled = True
    eng.config = cfg
    eng.cam_id = "camA"
    eng._states = {}
    eng._latest_bbox = {}
    eng.tier_u_pool = None
    return eng


def _run_on_result(
    eng: IdentityEngine, result: OmniIdentityResult, *, now_ts: float = 1000.0,
) -> None:
    asyncio.run(eng._make_on_result(now_ts=now_ts)(result))


class TestOnResultOrchestration:
    """驱动 _make_on_result 闭包，回归"投票清零 / confirmed 早退 / 慢重审回 pending"的编排。"""

    def test_non_no_person_result_clears_vote(self):
        """投一票 no_person 后来一次"有人"结果 → 票清 0（连续性中断），仍 pending。"""
        eng = _on_result_engine()
        st = TrackIdentityState(track_id=1, status="pending", no_person_vote_count=1)
        eng._states[1] = st
        _run_on_result(eng, OmniIdentityResult(
            track_id=1, person_id=None, confidence=0.6, no_person=False,
        ))
        assert st.no_person_vote_count == 0
        assert st.status == "pending"

    def test_confirmed_member_no_person_early_return(self):
        """confirmed 成员连投 3 次 no_person → 仍 confirmed、committed 不变、写库连续计数清 0。"""
        eng = _on_result_engine()
        st = TrackIdentityState(
            track_id=1, status="confirmed", committed_person_id="pid-x",
            write_eligible_count=5,
        )
        eng._states[1] = st
        for _ in range(3):
            _run_on_result(eng, OmniIdentityResult(
                track_id=1, person_id=None, confidence=0.9, no_person=True,
            ))
        assert st.status == "confirmed"
        assert st.committed_person_id == "pid-x"
        assert st.write_eligible_count == 0      # 🔵-2：弃权口径打断 tier_c 写库连续段
        assert st.no_person_vote_count == 0      # confirmed 不计 no_person 票

    def test_committed_no_person_recovers_on_person(self):
        """落定 no_person 被慢重审"真判到人"（omni_answered=True 的 unknown）→ 回 pending
        （自愈通道 ③，端到端走 _on_result）。person_id=None 也算——真·未注册人正是要恢复的场景。"""
        eng = _on_result_engine()
        st = TrackIdentityState(
            track_id=1, status="no_person",
            no_person_vote_count=2, no_person_anchor_bbox=(10, 10, 50, 90),
            last_omni_call_frame=100,
        )
        eng._states[1] = st
        _run_on_result(eng, OmniIdentityResult(
            track_id=1, person_id=None, confidence=0.6, no_person=False,  # omni_answered 默认 True
        ))
        assert st.status == "pending"
        assert st.no_person_anchor_bbox is None
        assert st.no_person_vote_count == 0
        # 本窗这条真判定被当作 pending 第一票**立即消费**（刻意不 return）——锁定该有意行为
        assert st.stability_count == 1

    def test_committed_no_person_high_conf_member_single_window_recover(self):
        """落定 no_person 被慢重审"高置信认出成员"→ 本窗即 commit-to-confirmed（证第一票被消费、单窗恢复）。"""
        eng = _on_result_engine()
        st = TrackIdentityState(
            track_id=1, status="no_person",
            no_person_anchor_bbox=(10, 10, 50, 90), last_omni_call_frame=100,
        )
        eng._states[1] = st
        _run_on_result(eng, OmniIdentityResult(
            track_id=1, person_id="pid-x", confidence=0.95, no_person=False,
        ))
        assert st.status == "confirmed"
        assert st.committed_person_id == "pid-x"

    def test_committed_no_person_holds_on_omni_nonanswer(self):
        """慢重审窗 omni 漏报 / 整体失败（omni_answered=False，合成非答复）→ 维持 no_person、
        不被误当"判到人"解除；只清 inflight 让下周期可再重审。防"一次 omni 抖动掀翻抑制"。"""
        eng = _on_result_engine()
        st = TrackIdentityState(
            track_id=1, status="no_person",
            no_person_anchor_bbox=(10, 10, 50, 90), last_omni_call_frame=100, inflight=True,
        )
        eng._states[1] = st
        _run_on_result(eng, OmniIdentityResult(
            track_id=1, person_id=None, confidence=0.0,
            reason="fused_response_missing_track", omni_answered=False,
        ))
        assert st.status == "no_person"                    # 未被误解除
        assert st.no_person_anchor_bbox == (10, 10, 50, 90)
        assert st.inflight is False                        # 清 inflight，下周期可再慢重审


class TestDispatcherNonAnswerFlag:
    """锁定契约：omni 漏报 / 整体失败合成的结果 omni_answered=False；真实 assignment 为 True。
    no_person 慢重审恢复通道据此区分"判到人"与"没答/失败"，避免一次抖动掀翻抑制。"""

    @staticmethod
    def _drive(deliver) -> list[OmniIdentityResult]:
        captured: list[OmniIdentityResult] = []

        async def _cap(r: OmniIdentityResult) -> None:
            captured.append(r)

        async def _run() -> None:
            disp = FusedDispatcher()
            await disp.dispatch([IdentityQueryItem(track_id=1)], {}, _cap)
            await deliver(disp)

        asyncio.run(_run())
        return captured

    def test_missing_track_marked_not_answered(self):
        # response 里没有 track 1 → 漏报兜底
        out = self._drive(lambda d: d.deliver_response([]))
        assert len(out) == 1
        assert out[0].omni_answered is False
        assert out[0].no_person is False

    def test_failure_marked_not_answered(self):
        out = self._drive(lambda d: d.deliver_failure("boom"))
        assert len(out) == 1
        assert out[0].omni_answered is False

    def test_real_assignment_is_answered(self):
        out = self._drive(lambda d: d.deliver_response(
            [{"track_id": 1, "person_id": None, "confidence": 0.7, "reason": "x"}]
        ))
        assert len(out) == 1
        assert out[0].omni_answered is True
