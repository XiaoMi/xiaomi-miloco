"""camera adapter 时钟漂移自愈测试。

回归 2026-09-01 远端故障：macOS 系统休眠/App Nap 期间 ``time.monotonic()``
(mach_absolute_time)不累计、墙钟唤醒后已跳前 → mono 与 unix 的偏移(epoch_delta)
突变(实测 ~89s) → 窗口时间戳永久滞后 → stale 门控无限丢弃、感知空转。
修复:``_calibrate`` 检测到偏移突变(>5s)时重置 epoch_delta 并清空流缓冲,
从最新帧重新开始(与断流重连同语义:旧数据不处理)。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from miloco.perception.collect import camera_adapter as ca
from miloco.perception.collect.camera_adapter import (
    _CameraDeviceState,
    CameraDeviceAdapter,
)
from miloco.perception.collect.stream_buffer import MultiTrackSyncBuffer


def _state(epoch_delta: int | None = None) -> _CameraDeviceState:
    st = _CameraDeviceState(
        did="cam1",
        sync_buffer=MultiTrackSyncBuffer(
            track_names=["decoded_video", "decoded_audio"],
            window_ms=5000,
            max_windows=3,
            buffer_full_action="clear",
        ),
    )
    st.epoch_delta = epoch_delta
    return st


def test_no_drift_keeps_epoch_delta():
    """正常运行时(无休眠)偏移稳定,epoch_delta 保持不变。"""
    st = _state(epoch_delta=1000)
    buffer = st.sync_buffer
    buffer.clear = MagicMock()

    # mono/unix 同步推进,偏移不变
    ca._monotonic_ms = lambda: 1_000_000
    ca._unix_ms = lambda: 1_001_000
    wall, unix = CameraDeviceAdapter._calibrate(st, stream_ts=0)

    assert st.epoch_delta == 1000
    buffer.clear.assert_not_called()
    assert unix - wall == 1000


def test_drift_after_sleep_resets_and_clears(monkeypatch):
    """休眠恢复:mono 滞后 90s(不累计)、墙钟跳前 → 重置基准 + 清空缓冲。"""
    st = _state(epoch_delta=1000)
    buffer = st.sync_buffer
    buffer.clear = MagicMock()

    # 休眠前: mono=1_000_000, unix=1_001_000 (delta=1000)
    # 休眠 90s 后: mono 仍 ≈1_000_000(不累计), unix 已 =1_091_000
    ca._monotonic_ms = lambda: 1_000_000
    ca._unix_ms = lambda: 1_091_000

    wall, unix = CameraDeviceAdapter._calibrate(st, stream_ts=0)

    assert st.epoch_delta == 91_000  # 重新锁定: unix - mono
    buffer.clear.assert_called_once()  # 积压旧窗口被清空,从最新开始
    assert unix - wall == 91_000


def test_small_jitter_not_reset():
    """微小抖动(<5s,如 NTP 校时)不触发重置,避免误清流。"""
    st = _state(epoch_delta=1000)
    buffer = st.sync_buffer
    buffer.clear = MagicMock()

    ca._monotonic_ms = lambda: 1_000_000
    ca._unix_ms = lambda: 1_003_000  # 漂移 2s < 5s 阈值

    CameraDeviceAdapter._calibrate(st, stream_ts=0)

    assert st.epoch_delta == 1000
    buffer.clear.assert_not_called()


def test_first_frame_locks_baseline():
    """首帧(epoch_delta=None)正常锁定,不清缓冲。"""
    st = _state(epoch_delta=None)
    buffer = st.sync_buffer
    buffer.clear = MagicMock()

    ca._monotonic_ms = lambda: 5_000_000
    ca._unix_ms = lambda: 5_000_100

    CameraDeviceAdapter._calibrate(st, stream_ts=0)

    assert st.epoch_delta == 100
    buffer.clear.assert_not_called()
