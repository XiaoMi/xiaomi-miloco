"""no_person（非人误检抑制）单测。

覆盖三层：
  - 解析层：``parse_identity_assignments`` 把 omni name="no_person" 标成 no_person=True，
    区别于 unknown（有人但认不出）。
  - 状态机：``record_no_person`` 连续 2 票才落定、confirmed 成员不被翻；落定后
    ``needs_omni_call``→False、``get_face_id_value``→"none"。
  - 移动解除：``clear_no_person_on_motion`` 静止维持、明显移动回 pending。
"""

from __future__ import annotations

import json

from miloco.perception.engine.config import IdentityEngineConfig, StabilityConfigDC
from miloco.perception.engine.identity.engine import IdentityEngine, _bbox_iou
from miloco.perception.engine.identity.state import (
    TrackIdentityState,
    clear_no_person_on_motion,
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
    def test_needs_omni_call_false(self):
        state = TrackIdentityState(track_id=1, status="no_person")
        assert needs_omni_call(
            state, now_frame=100, now_ts=100.0, min_dispatch_interval_sec=5.0,
            config=StabilityConfigDC(), engine_fps=3,
        ) is False

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
