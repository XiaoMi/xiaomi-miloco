from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from miloco.config.settings import CameraSettings
from miloco.miot.service import MiotService
from miot.camera import MIoTCameraInstance
from pydantic import ValidationError


def _camera(idle_interval: int) -> MIoTCameraInstance:
    camera = MIoTCameraInstance.__new__(MIoTCameraInstance)
    camera._callbacks = {}
    camera._next_reg_id = 1
    camera._decoded_frame_interval = idle_interval
    camera._live_video_intervals = {}
    camera._decoders = [Mock()]
    camera._MIoTCameraInstance__update_raw_data_register_status_async = AsyncMock()
    return camera


def _last_interval(camera: MIoTCameraInstance) -> int:
    return camera._decoders[0].set_decoded_frame_interval.call_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_full_rate_registration_overrides_sampling_until_last_viewer_leaves():
    camera = _camera(333)
    callback = AsyncMock()

    first = await camera.register_decode_video_frame_async(
        callback, channel=0, multi_reg=True, full_rate=True
    )
    second = await camera.register_decode_video_frame_async(
        callback, channel=0, multi_reg=True, full_rate=True
    )
    assert _last_interval(camera) == 0

    await camera.unregister_decode_video_frame_async(channel=0, reg_id=first)
    assert _last_interval(camera) == 0

    await camera.unregister_decode_video_frame_async(channel=0, reg_id=second)
    assert _last_interval(camera) == 333


@pytest.mark.asyncio
async def test_live_interval_follows_fastest_viewer_and_restores_idle_interval():
    camera = _camera(1000)
    callback = AsyncMock()

    slow = await camera.register_decode_video_frame_async(
        callback, channel=0, multi_reg=True, frame_interval_ms=200
    )
    assert _last_interval(camera) == 200
    fast = await camera.register_decode_video_frame_async(
        callback, channel=0, multi_reg=True, frame_interval_ms=100
    )
    assert _last_interval(camera) == 100

    await camera.unregister_decode_video_frame_async(channel=0, reg_id=fast)
    assert _last_interval(camera) == 200
    await camera.unregister_decode_video_frame_async(channel=0, reg_id=slow)
    assert _last_interval(camera) == 1000


@pytest.mark.asyncio
async def test_live_interval_never_slows_down_an_unthrottled_decoder():
    camera = _camera(0)
    await camera.register_decode_video_frame_async(
        AsyncMock(), channel=0, multi_reg=True, frame_interval_ms=200
    )
    assert _last_interval(camera) == 0


@pytest.mark.asyncio
async def test_plain_registration_does_not_touch_decoder_interval():
    camera = _camera(1000)
    await camera.register_decode_video_frame_async(AsyncMock(), channel=0, multi_reg=True)
    camera._decoders[0].set_decoded_frame_interval.assert_not_called()


@pytest.mark.asyncio
async def test_set_live_frame_interval_updates_existing_registration_only():
    camera = _camera(1000)
    reg = await camera.register_decode_video_frame_async(
        AsyncMock(), channel=0, multi_reg=True, frame_interval_ms=200
    )
    assert await camera.set_live_frame_interval_async(0, reg, 500) is True
    assert _last_interval(camera) == 500
    assert await camera.set_live_frame_interval_async(0, reg + 99, 50) is False
    assert _last_interval(camera) == 500


@pytest.mark.asyncio
async def test_live_service_passes_requested_interval_to_proxy():
    proxy = SimpleNamespace(start_camera_decode_video_stream=AsyncMock(return_value=7))
    service = MiotService.__new__(MiotService)
    service._miot_proxy = proxy
    callback = AsyncMock()

    reg_id = await service.start_video_stream("camera-1", 0, callback, frame_interval_ms=200)

    assert reg_id == 7
    proxy.start_camera_decode_video_stream.assert_awaited_once_with(
        "camera-1", 0, callback, frame_interval_ms=200
    )


@pytest.mark.asyncio
async def test_live_service_set_interval_reports_proxy_failure_as_false():
    proxy = SimpleNamespace(
        set_camera_decode_video_frame_interval=AsyncMock(side_effect=RuntimeError("x"))
    )
    service = MiotService.__new__(MiotService)
    service._miot_proxy = proxy
    assert await service.set_video_stream_interval("camera-1", 0, 7, 500) is False


def test_decoded_frame_interval_stays_below_silence_reconnect_threshold():
    with pytest.raises(ValidationError):
        CameraSettings(decoded_frame_interval=10_001)


def test_live_fps_settings_have_safe_defaults_and_bounds():
    cam = CameraSettings()
    assert cam.live_default_fps == 5
    assert cam.live_max_fps == 20
    with pytest.raises(ValidationError):
        CameraSettings(live_max_fps=0)
    with pytest.raises(ValidationError):
        CameraSettings(live_default_fps=61)
