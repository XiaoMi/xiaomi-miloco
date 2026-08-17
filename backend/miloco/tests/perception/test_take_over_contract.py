"""接管机制（``take_over_device`` / ``release_device`` / ``disconnect_device(force=)``）
的契约用例，以及静默自愈后 watch/录像订阅的补注册接线。

接管机制是给 inject-video 等外部临时接管场景准备的，当前仓内没有生产调用方——但那些
契约只写在注释里，将来 inject 实现若与之不符（比如忘了先释放再恢复真流），代价是运行时
静默丢注入帧，且没有回归网。这里把三条核心语义钉住：

1. 接管中的 did 不被周期 sync 断开（否则 disconnect 会 clear 注入 buffer + 移除注入
   state，注入帧全部丢失）；
2. 接管中的 did 不被 sync 重连（``connect_device`` 会覆盖注入 state）；
3. ``force=True`` 才能真断——接管方自己要停真流时用；``release_device`` 后恢复常态。

另附一条接线用例：静默自愈重建 native 会话后，必须通知流管理器把 watch 直播 /
``record_clip`` 的解码订阅补注册到新实例上（感知侧有下轮 sync 兜底，那两个没有）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from miloco.perception.collect.camera_adapter import (
    CameraDeviceAdapter,
    _CameraDeviceState,
)


def _adapter_with_connected(did: str) -> tuple[CameraDeviceAdapter, MagicMock]:
    proxy = MagicMock()
    proxy.get_cached_camera.return_value = SimpleNamespace(did=did, online=True)
    proxy.stop_camera_decode_video_stream = AsyncMock(return_value=True)
    proxy.stop_camera_decode_audio_stream = AsyncMock(return_value=True)
    proxy.reconnect_camera = AsyncMock()
    adapter = CameraDeviceAdapter(miot_proxy=proxy)
    state = _CameraDeviceState(did=did)
    state.decoded_video_reg_id = 1
    state.decoded_audio_reg_id = 2
    adapter._devices[did] = state
    return adapter, proxy


async def test_taken_over_device_is_not_disconnected_by_sync():
    adapter, proxy = _adapter_with_connected("cam1")
    adapter.take_over_device("cam1")

    await adapter.disconnect_device("cam1")

    assert "cam1" in adapter._devices, "接管中的 did 不得被周期 sync 断开"
    proxy.stop_camera_decode_video_stream.assert_not_awaited()


async def test_taken_over_device_is_not_reconnected_by_sync():
    """接管期间 connect_device 必须跳过，否则新建的 state 会覆盖注入 state。"""
    adapter, proxy = _adapter_with_connected("cam1")
    adapter.take_over_device("cam1")
    injected_state = adapter._devices["cam1"]

    await adapter.connect_device("cam1", source=SimpleNamespace(did="cam1"))

    assert adapter._devices["cam1"] is injected_state
    proxy.start_camera_decode_video_stream.assert_not_called()


async def test_force_disconnect_bypasses_take_over():
    """接管方自己要停真流时用 force=True，这条路径必须真断。"""
    adapter, proxy = _adapter_with_connected("cam1")
    adapter.take_over_device("cam1")

    await adapter.disconnect_device("cam1", force=True)

    assert "cam1" not in adapter._devices
    proxy.stop_camera_decode_video_stream.assert_awaited_once()


async def test_release_restores_normal_disconnect():
    adapter, _ = _adapter_with_connected("cam1")
    adapter.take_over_device("cam1")
    adapter.release_device("cam1")

    await adapter.disconnect_device("cam1")

    assert "cam1" not in adapter._devices


async def test_release_is_idempotent_for_unknown_did():
    """释放没接管过的 did 不该抛——调用方在异常路径上会无条件释放。"""
    adapter, _ = _adapter_with_connected("cam1")
    adapter.release_device("never-taken-over")


async def test_reconnect_stalled_resubscribes_live_and_record_streams(monkeypatch):
    """重建后必须通知流管理器补注册：watch 直播与 record_clip 没有 sync 兜底。"""
    import miloco.miot.ws as ws_mod

    adapter, proxy = _adapter_with_connected("cam1")
    resubscribe = AsyncMock()
    monkeypatch.setattr(
        ws_mod.miot_video_stream_manager, "resubscribe_camera", resubscribe
    )

    await adapter._reconnect_stalled("cam1")

    proxy.reconnect_camera.assert_awaited_once_with("cam1")
    resubscribe.assert_awaited_once_with("cam1")


async def test_resubscribe_is_skipped_when_rebuild_failed(monkeypatch):
    """重建本身失败时不补注册——native 实例还是旧的那个，订阅仍然有效。"""
    import miloco.miot.ws as ws_mod

    adapter, proxy = _adapter_with_connected("cam1")
    proxy.reconnect_camera = AsyncMock(side_effect=RuntimeError("boom"))
    resubscribe = AsyncMock()
    monkeypatch.setattr(
        ws_mod.miot_video_stream_manager, "resubscribe_camera", resubscribe
    )

    await adapter._reconnect_stalled("cam1")

    resubscribe.assert_not_awaited()
