# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for omni_fps 运行时热更（免重建引擎 / 免模型重载 / 不丢 track）。

改 omni_fps 时，其经 ``adjust_fps_for_omni`` 顶起的 tracker fps 会烘进 3 处构造期
派生缓存。本组测试覆盖各层的 setter 与 PerceptionEngine 的编排：

- ``SortTracker.set_fps``：重算 ``_max_age_frames``，不清 ``_tracks``
- ``DeepSort.set_fps``：写穿到内部 ``_mot.config`` 的 max_age / human_max_lost_frames
- ``TrackingService.set_fps``：基类默认转调 tracker；Mock 无 tracker 时 no-op
- ``IdentityEngine.set_engine_fps``：重算 grace / cooldown / frames_per_window
- ``PerceptionEngine.apply_omni_fps``：更新 config + 刷 kwargs + 遍历 live tracker/engine，
  且 fps 从 base 重算（omni 2→3 回落，不从已调整值累积错算）
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from miloco.perception.engine.identity.engine import (
    _DEAD_TRACK_GRACE_SEC,
    IdentityEngine,
)

# ---- SortTracker.set_fps -----------------------------------------------------


def test_sorttracker_set_fps_recomputes_max_age_keeps_tracks():
    from miloco.perception.engine.identity.sort import SortConfig, SortTracker

    trk = SortTracker.__new__(SortTracker)
    trk.config = SortConfig(max_age_sec=1.0)
    trk.fps = 3
    trk._max_age_frames = 3
    sentinel = object()
    trk._tracks = [sentinel]

    trk.set_fps(4)

    assert trk.fps == 4
    assert trk._max_age_frames == 4  # round(1.0 * 4)
    assert trk._tracks == [sentinel]  # 活跃 track 不清


def test_sorttracker_set_fps_floors_to_one():
    from miloco.perception.engine.identity.sort import SortConfig, SortTracker

    trk = SortTracker.__new__(SortTracker)
    trk.config = SortConfig(max_age_sec=1.0)
    trk._tracks = []

    trk.set_fps(0)  # 极端值也至少 1 帧

    assert trk.fps == 1
    assert trk._max_age_frames == 1


# ---- DeepSort.set_fps（写穿 _mot.config） ------------------------------------


def test_deepsort_set_fps_writes_through_to_mot_config():
    from miloco.perception.engine.config import DeepSortConfigDC
    from miloco.perception.engine.identity.deep_sort import DeepSortTracker

    trk = DeepSortTracker.__new__(DeepSortTracker)
    trk.config = DeepSortConfigDC(max_age_sec=2.0)
    trk._mot = MagicMock()
    trk._mot.config = SimpleNamespace(max_age=6, human_max_lost_frames=6)

    trk.set_fps(4)

    assert trk.fps == 4
    assert trk._mot.config.max_age == 8  # round(2.0 * 4)
    assert trk._mot.config.human_max_lost_frames == 8


# ---- TrackingService.set_fps 基类默认 ----------------------------------------


def test_tracking_service_set_fps_delegates_to_tracker():
    from miloco.perception.engine.identity.tracking_service import RealTrackingService

    svc = RealTrackingService.__new__(RealTrackingService)
    svc._fps = 3
    svc._tracker = MagicMock()

    svc.set_fps(4)

    assert svc._fps == 4
    svc._tracker.set_fps.assert_called_once_with(4)


def test_mock_tracking_service_set_fps_is_noop():
    from miloco.perception.engine.identity.tracking_service import MockTrackingService

    svc = MockTrackingService()  # _tracker = None

    svc.set_fps(4)  # 不应抛错（tracker is None 时跳过）

    assert svc._fps == 4


# ---- IdentityEngine.set_engine_fps -------------------------------------------


def test_identity_engine_set_engine_fps_recomputes_frame_counts():
    from miloco.perception.engine.config import IdentityEngineConfig, StabilityConfigDC

    eng = IdentityEngine.__new__(IdentityEngine)
    # 用真实 IdentityEngineConfig / StabilityConfigDC（非 SimpleNamespace）：stability
    # 字段一旦改名，构造处即 TypeError，守住 setter 公式对真实 config 字段的依赖。
    eng.config = IdentityEngineConfig(
        stability=StabilityConfigDC(
            tier_c_cooldown_mult=2,
            write_eligible_min_count=6,
            recheck_interval_accumulating_sec=10,
        )
    )
    eng._period_sec = 4.0
    eng._engine_fps = 3.0
    eng._frames_per_window = 12.0
    eng._dead_track_grace_frames = 9
    eng._tier_c_cooldown_frames = 360

    eng.set_engine_fps(4)

    assert eng._engine_fps == 4.0
    assert eng._frames_per_window == 16.0  # 4 * 4
    assert eng._dead_track_grace_frames == round(_DEAD_TRACK_GRACE_SEC * 4)  # 12
    assert eng._tier_c_cooldown_frames == round(2 * 6 * 10 * 4)  # 480


# ---- PerceptionEngine.apply_omni_fps 编排 ------------------------------------


def _make_engine():
    from miloco.perception.engine.api import PerceptionEngine
    from miloco.perception.engine.config import PerceptionConfig

    eng = PerceptionEngine.__new__(PerceptionEngine)
    eng._config = PerceptionConfig()  # input.fps=3, omni_fps=1（默认）
    eng._base_fps = eng._config.input.fps  # 3
    eng._tracking_mode = "real"
    eng._tracking_service_kwargs = {"fps": 3}
    eng._tracking_services = {}
    eng._identity_engines = {}
    return eng


def test_apply_omni_fps_updates_config_and_pushes_to_live_instances():
    eng = _make_engine()
    svc = MagicMock()
    identity = MagicMock()
    eng._tracking_services = {"cam0": svc}
    eng._identity_engines = {"cam0": identity, "cam1": None}  # None 值应被跳过

    eng.apply_omni_fps(2)  # adjust_fps_for_omni(base=3, 2) = 4

    assert eng._config.input.omni_fps == 2
    assert eng._config.input.fps == 4
    assert eng._tracking_service_kwargs["fps"] == 4
    svc.set_fps.assert_called_once_with(4)
    identity.set_engine_fps.assert_called_once_with(4)


def test_apply_omni_fps_mock_mode_keeps_kwargs_empty():
    """mock 模式：kwargs 恒为空，热更不塞孤儿 fps key（与 __init__ mock 分支对称）。"""
    eng = _make_engine()
    eng._tracking_mode = "mock"
    eng._tracking_service_kwargs = {}

    eng.apply_omni_fps(2)

    assert eng._tracking_service_kwargs == {}  # 无消费者的 fps key 不该出现
    assert eng._config.input.fps == 4  # config/base 重算照常


def test_apply_omni_fps_recomputes_from_base_not_accumulate():
    """连续改 omni_fps 必须从 base fps 重算，不能拿已调整过的 fps 再调。

    base=3：omni 1→2 得 fps=4；再 omni 2→3 应回落 fps=3（adjust(3,3)=3），
    而非拿 4 再调 adjust(4,3)=6。这是「必须存 base_fps」的核心理由。
    """
    eng = _make_engine()

    eng.apply_omni_fps(2)
    assert eng._config.input.fps == 4

    eng.apply_omni_fps(3)
    assert eng._config.input.omni_fps == 3
    assert eng._config.input.fps == 3  # 从 base=3 重算，非 6
