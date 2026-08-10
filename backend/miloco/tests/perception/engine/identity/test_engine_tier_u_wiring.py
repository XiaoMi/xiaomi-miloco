"""IdentityEngine 接入 TierUPool 的端到端 wiring 单测。

验证关键路径:
- pool 注入后,unknown track 的 crop 自动 push 到池
- 已 confirmed 的 track 不进池(走 tier_c 路径)
- commit confirmed 时自动调 close_write_gate
- 未注入 pool 时所有路径安全 no-op,老行为不变
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from miloco.perception.engine.config import IdentityEngineConfig
from miloco.perception.engine.identity.dispatcher import (
    FusedDispatcher,
    OmniIdentityResult,
)
from miloco.perception.engine.identity.engine import IdentityEngine
from miloco.perception.engine.identity.library import IdentityLibrary
from miloco.perception.engine.identity.tier_u import TierUConfig, TierUPool

# =============================================================================
# Helpers
# =============================================================================


@pytest.fixture
def lib(tmp_path: Path) -> IdentityLibrary:
    return IdentityLibrary(tmp_path / "identity_lib")


@pytest.fixture
def frame() -> np.ndarray:
    return np.ones((480, 640, 3), dtype=np.uint8) * 100


def _make_tracking_result(track_id: int, x: int = 100, y: int = 100,
                          w: int = 100, h: int = 200) -> dict:
    return {
        "id": track_id,
        "class_id": 0,
        "bbox": (x, y, w, h),
        "xyxy": (x, y, x + w, y + h),
        "confidence": 0.9,
        "hits": 5,
        "age": 5,
        "time_since_update": 0,
    }


def _make_engine(lib: IdentityLibrary, pool: TierUPool | None = None) -> IdentityEngine:
    config = IdentityEngineConfig()
    config.stability.commit_threshold_high = 1  # 单测按高置信单票即 commit；生产默认已是 3
    dispatcher = FusedDispatcher(config=config.dispatch)
    return IdentityEngine(
        config=config,
        library=lib,
        dispatcher=dispatcher,
        scope_label="cam-test",   # 渲染用 (v2 重构后不再决定 cam_id)
        device_id="cam-test",     # cam_id 真值来源 — 与 _deep_sort_trackers key 同源
        engine_fps=1.0,
        tier_u_pool=pool,
    )


# =============================================================================
# Wiring: pool 注入后 unknown track 的 crop 自动 push
# =============================================================================


class TestPushUnknownToPool:
    @pytest.mark.asyncio
    async def test_unknown_track_pushes_to_pool(self, lib, frame):
        pool = TierUPool(config=TierUConfig(l1_capacity=30))
        engine = _make_engine(lib, pool=pool)

        # 第一窗口跑完,track=7 是新建的 → status="pending"(不 confirmed)
        await engine.process(
            tracking_results=[_make_tracking_result(7)],
            latest_frame=frame, frame_index=1, now_ts=1.0,
        )
        # pool 应该收到 1 张 crop(cam-test, 7)
        key = ("cam-test", 7)
        assert key in pool._entries
        assert len(pool._entries[key].crops_l1) == 1

    @pytest.mark.asyncio
    async def test_coasting_track_not_pushed_to_pool(self, lib, frame):
        """coasting（本帧未命中，框停在上一次真匹配位置）不进池——已不对应本帧真人，裁出的
        背景图会污染聚类，还能经 from-cluster 注册间接写进身份库。"""
        pool = TierUPool(config=TierUConfig(l1_capacity=30))
        engine = _make_engine(lib, pool=pool)

        tr = _make_tracking_result(7)
        tr["detected_this_frame"] = False
        await engine.process(
            tracking_results=[tr], latest_frame=frame, frame_index=1, now_ts=1.0,
        )
        assert ("cam-test", 7) not in pool._entries

    @pytest.mark.asyncio
    async def test_coasting_window_adds_no_crop_to_existing_entry(self, lib, frame):
        """生产时序：同一 track 先真检测（推图）后 coasting（不推）——池里 crop
        数不随 coasting 窗增长，track 本身照常存活。"""
        pool = TierUPool(config=TierUConfig(l1_capacity=30))
        engine = _make_engine(lib, pool=pool)
        key = ("cam-test", 7)

        await engine.process(
            tracking_results=[_make_tracking_result(7)],
            latest_frame=frame, frame_index=1, now_ts=1.0,
        )
        assert len(pool._entries[key].crops_l1) == 1

        tr = _make_tracking_result(7)
        tr["detected_this_frame"] = False
        await engine.process(
            tracking_results=[tr], latest_frame=frame, frame_index=2, now_ts=2.0,
        )
        assert len(pool._entries[key].crops_l1) == 1

    @pytest.mark.asyncio
    async def test_process_returns_bbox_norm_for_detected(self, lib, frame):
        """process() 返回 (face_id_map, bbox_norm)；本帧检测到的 track 带归一化 bbox。"""
        engine = _make_engine(lib)
        out, bbox_norm = await engine.process(
            tracking_results=[_make_tracking_result(7)],
            latest_frame=frame, frame_index=1, now_ts=1.0,
        )
        assert 7 in out
        assert 7 in bbox_norm
        x1, y1, x2, y2 = bbox_norm[7]
        assert all(0 <= v <= 1000 for v in (x1, y1, x2, y2))
        assert x1 < x2 and y1 < y2

    @pytest.mark.asyncio
    async def test_process_omits_bbox_for_coasting(self, lib, frame):
        """coasting（本帧未命中，框停在上一次真匹配位置）不注入过期位置 → bbox_norm 无该 track。"""
        engine = _make_engine(lib)
        tr = _make_tracking_result(7)
        tr["detected_this_frame"] = False
        out, bbox_norm = await engine.process(
            tracking_results=[tr], latest_frame=frame, frame_index=1, now_ts=1.0,
        )
        assert 7 in out
        assert 7 not in bbox_norm

    @pytest.mark.asyncio
    async def test_confirmed_recheck_marks_is_recheck_candidate(self, lib, frame):
        """confirmed track 到点重审被派发时 candidate.is_recheck=True（engine 内部标记，
        当前 prompt 不区分首次/重审），且不带任何身份字段（去先验）。"""
        from miloco.perception.engine.identity.state import TrackIdentityState

        engine = _make_engine(lib)  # engine_fps=1.0 → recheck interval≈30 帧
        st = TrackIdentityState(track_id=7)
        st.status = "confirmed"
        st.committed_person_id = "11111111-1111-4111-8111-111111111111"
        st.last_omni_call_frame = 0
        engine._states[7] = st
        # frame_index 远超 recheck 间隔 → 触发重审派发
        await engine.process(
            tracking_results=[_make_tracking_result(7)],
            latest_frame=frame, frame_index=1000, now_ts=1000.0,
        )
        pending = engine.take_fused_pending()
        assert pending is not None
        cand = next(c for c in pending.candidates if c.track_id == 7)
        assert cand.is_recheck is True
        # 去先验：candidate 不携带身份字段
        assert not hasattr(cand, "committed_name")
        assert not hasattr(cand, "status")

    @pytest.mark.asyncio
    async def test_no_pool_injection_skips_push(self, lib, frame):
        """未注入 pool 时,引擎流程不应该崩,也不应该试图调 pool。"""
        engine = _make_engine(lib, pool=None)
        await engine.process(
            tracking_results=[_make_tracking_result(7)],
            latest_frame=frame, frame_index=1, now_ts=1.0,
        )
        # 没崩就行;不需要其他断言

    @pytest.mark.asyncio
    async def test_confirmed_track_does_not_push(self, lib, frame):
        """已 confirmed 的 track 不进池(走 tier_c 路径)。"""
        pool = TierUPool(config=TierUConfig(l1_capacity=30))
        engine = _make_engine(lib, pool=pool)
        # 先 promote 一个 track 到 confirmed(直接改 state,模拟之前的 commit)
        from miloco.perception.engine.identity.state import TrackIdentityState
        st = TrackIdentityState(track_id=7)
        st.status = "confirmed"
        st.committed_person_id = "11111111-1111-4111-8111-111111111111"
        engine._states[7] = st

        await engine.process(
            tracking_results=[_make_tracking_result(7)],
            latest_frame=frame, frame_index=1, now_ts=1.0,
        )
        # confirmed track 不该有 crop 进池
        assert ("cam-test", 7) not in pool._entries


# =============================================================================
# Commit → close_write_gate
# =============================================================================


class TestCommitClosesGate:
    @pytest.mark.asyncio
    async def test_commit_closes_pool_gate(self, lib, frame):
        """omni result 触发 commit 时,IdentityEngine 应主动调 pool.close_write_gate。"""
        pool = MagicMock(spec=TierUPool)
        engine = _make_engine(lib, pool=pool)

        # 先建一个 track 在 _states 里,模拟之前窗口已 push 过
        from miloco.perception.engine.identity.state import TrackIdentityState
        st = TrackIdentityState(track_id=7)
        st.status = "pending"
        st.candidate_person_id = "11111111-1111-4111-8111-111111111111"
        st.stability_count = 0
        engine._states[7] = st

        # 取出 _on_result 闭包,模拟 omni 返回 high_conf 结果(1 次同答即 commit)
        on_result = engine._make_on_result(now_ts=100.0)
        await on_result(OmniIdentityResult(
            track_id=7,
            person_id="11111111-1111-4111-8111-111111111111",
            confidence=0.95,  # high → commit_threshold_high=1
            reason="test_commit",
            batch_size=2,  # batch>=2 跳过 tier_c 但 close_gate 不受影响
        ))

        # 断言 close_write_gate 被调
        pool.close_write_gate.assert_called_once_with("cam-test", 7)

    @pytest.mark.asyncio
    async def test_no_pool_does_not_crash_on_commit(self, lib):
        """未注入 pool 时,commit 路径不应试图调 close_write_gate。"""
        engine = _make_engine(lib, pool=None)
        from miloco.perception.engine.identity.state import TrackIdentityState
        st = TrackIdentityState(track_id=7)
        st.status = "pending"
        engine._states[7] = st

        on_result = engine._make_on_result(now_ts=100.0)
        # 不应抛异常
        await on_result(OmniIdentityResult(
            track_id=7,
            person_id="11111111-1111-4111-8111-111111111111",
            confidence=0.95,
            reason="test_commit",
            batch_size=2,
        ))


# =============================================================================
# Pool 内部周期管理:tick_ttl / gc_lru 每窗口都跑
# =============================================================================


class TestPoolPeriodicMaintenance:
    @pytest.mark.asyncio
    async def test_engine_calls_pool_lifecycle_each_window(self, lib, frame):
        """每个 process 窗口都应调 flush_if_due / tick_ttl / gc_lru。"""
        pool = MagicMock(spec=TierUPool)
        engine = _make_engine(lib, pool=pool)

        await engine.process(
            tracking_results=[_make_tracking_result(7)],
            latest_frame=frame, frame_index=1, now_ts=1.0,
        )

        assert pool.flush_if_due.called
        assert pool.tick_ttl.called
        assert pool.gc_lru_if_over_budget.called

    @pytest.mark.asyncio
    async def test_no_frame_skips_pool_lifecycle(self, lib):
        """latest_frame 为 None 时,跳过整个 pool 接入逻辑(crop 没法裁)。"""
        pool = MagicMock(spec=TierUPool)
        engine = _make_engine(lib, pool=pool)

        await engine.process(
            tracking_results=[_make_tracking_result(7)],
            latest_frame=None, frame_index=1, now_ts=1.0,
        )
        # 没 frame 时连 flush 都不该调(避免无 crop 的空窗口浪费)
        pool.push_crop.assert_not_called()
        pool.flush_if_due.assert_not_called()


# =============================================================================
# tier_c 入队锐度门: 过糊 crop 不入队
# =============================================================================


class TestSharpnessGate:
    def test_blurry_crop_skipped_with_log(self, lib, frame, caplog):
        """灰底(低锐度)crop 走入队路径时, 命中锐度门提前 return: 不入队 + 打"画面过糊"。"""
        import logging

        from miloco.perception.engine.identity.state import TrackIdentityState

        engine = _make_engine(lib)
        tid = 1
        # 铺齐到锐度门之前的所有门: 不在冷却/不在途、本帧有检测、本窗关联到 face、
        # 连续一致门达标(write_eligible_count >= 默认 6)、bbox 面积/长宽比合规。
        st = TrackIdentityState(track_id=tid)
        st.write_eligible_count = 6
        engine._states[tid] = st
        engine._detected_this_frame[tid] = True
        engine._face_match_this_window[tid] = object()
        engine._cur_frame_index = 0
        engine._latest_frame = frame                     # fixture: 全 100 灰底 → Laplacian 方差≈0
        engine._latest_bbox[tid] = (100, 100, 220, 340)  # area≈9%>5%, aspect=0.5∈[0.2,2.5]

        with caplog.at_level(logging.INFO):
            engine._enqueue_tier_c_candidate(person_id="p-x", track_id=tid, now_ts=0.0)

        # 锐度门在置 in_flight 之前就 return → 未入队; 且命中"画面过糊"分支
        assert st.in_flight_tier_c is False
        assert any("画面过糊" in r.message for r in caplog.records)


class TestOverlapGate:
    """tier_c 防遮挡门 (E6): 本窗人体框被他人框覆盖 ≥5% 时入队早退。"""

    def test_overlap_ratio_ioa(self):
        from miloco.perception.engine.identity.engine import _max_overlap_ratio

        box = (0, 0, 100, 100)  # area=10000
        assert _max_overlap_ratio(box, []) == 0.0
        assert _max_overlap_ratio(box, [(200, 200, 300, 300)]) == 0.0  # 无交集
        # 交集 [50,0,100,100] = 50×100 = 5000 → IoA = 0.5 (分母是当前框面积)
        assert _max_overlap_ratio(box, [(50, 0, 150, 100)]) == pytest.approx(0.5)
        # 取与多个他人框的最大值
        assert _max_overlap_ratio(
            box, [(90, 0, 150, 100), (50, 0, 150, 100)]
        ) == pytest.approx(0.5)
        # 退化框 (面积<=0) 返 0, 不除零
        assert _max_overlap_ratio((0, 0, 0, 100), [(0, 0, 100, 100)]) == 0.0

    def test_overlapped_crop_skipped_with_log(self, lib, frame, caplog):
        """本窗 track 被标记遮挡 → 在 E6 门提前 return: 不入队 + 打"与他人重叠"。"""
        import logging

        from miloco.perception.engine.identity.state import TrackIdentityState

        engine = _make_engine(lib)
        tid = 1
        # 铺齐到 E6 之前的门: 不在冷却/不在途、本帧有检测、本窗关联到 face。
        st = TrackIdentityState(track_id=tid)
        st.write_eligible_count = 6
        engine._states[tid] = st
        engine._detected_this_frame[tid] = True
        engine._face_match_this_window[tid] = object()
        engine._cur_frame_index = 0
        engine._latest_frame = frame
        engine._latest_bbox[tid] = (100, 100, 220, 340)
        engine._overlap_other_person[tid] = True  # 本窗被他人框遮挡

        with caplog.at_level(logging.INFO):
            engine._enqueue_tier_c_candidate(person_id="p-x", track_id=tid, now_ts=0.0)

        # E6 门在置 in_flight 之前就 return → 未入队; 且命中"与他人重叠"分支
        assert st.in_flight_tier_c is False
        assert any("与他人重叠" in r.message for r in caplog.records)
