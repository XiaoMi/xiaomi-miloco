"""静默自愈重建后，watch 直播 / ``record_clip`` 的解码订阅必须被补注册。

``reconnect_camera`` 会 destroy 整个 native 实例，旧实例上的解码回调随之全部消失。
三个消费方里只有感知适配器有兜底（同一轮 sync 的 ``connect_device``），watch 直播
与录像没有——所以 ``_reconnect_stalled`` 要在重建成功后显式通知流管理器补注册。
钉两条：重建成功才补；重建失败不补（native 实例还是旧的那个，订阅仍然有效）。
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


async def test_reconnect_stalled_resubscribes_all_ws_consumers(monkeypatch):
    """视频（含录像）与音频两个 manager 都要被通知——音频是第四个消费方，最隐蔽。"""
    import miloco.miot.ws as ws_mod

    adapter, proxy = _adapter_with_connected("cam1")
    video = AsyncMock()
    audio = AsyncMock()
    monkeypatch.setattr(ws_mod.miot_video_stream_manager, "resubscribe_camera", video)
    monkeypatch.setattr(ws_mod.miot_audio_stream_manager, "resubscribe_camera", audio)

    await adapter._reconnect_stalled("cam1")

    proxy.reconnect_camera.assert_awaited_once_with("cam1")
    video.assert_awaited_once_with("cam1")
    audio.assert_awaited_once_with("cam1")


async def test_audio_resubscribe_runs_even_if_video_one_raises(monkeypatch):
    """两条各自独立 try：视频那条炸了不该把音频一起拖没。"""
    import miloco.miot.ws as ws_mod

    adapter, _ = _adapter_with_connected("cam1")
    audio = AsyncMock()
    monkeypatch.setattr(
        ws_mod.miot_video_stream_manager,
        "resubscribe_camera",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(ws_mod.miot_audio_stream_manager, "resubscribe_camera", audio)

    await adapter._reconnect_stalled("cam1")

    audio.assert_awaited_once_with("cam1")


async def test_resubscribe_is_skipped_when_rebuild_failed(monkeypatch):
    """重建本身失败时不补注册——native 实例还是旧的那个，订阅仍然有效。"""
    import miloco.miot.ws as ws_mod

    adapter, proxy = _adapter_with_connected("cam1")
    proxy.reconnect_camera = AsyncMock(side_effect=RuntimeError("boom"))
    video = AsyncMock()
    audio = AsyncMock()
    monkeypatch.setattr(ws_mod.miot_video_stream_manager, "resubscribe_camera", video)
    monkeypatch.setattr(ws_mod.miot_audio_stream_manager, "resubscribe_camera", audio)

    await adapter._reconnect_stalled("cam1")

    video.assert_not_awaited()
    audio.assert_not_awaited()
