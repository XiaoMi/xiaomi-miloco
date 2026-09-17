from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from miloco.config.settings import CameraSettings
from miloco.miot.service import MiotService
from miot.camera import MIoTCameraInstance
from pydantic import ValidationError


@pytest.mark.asyncio
async def test_full_rate_registration_overrides_sampling_until_last_viewer_leaves():
    camera = MIoTCameraInstance.__new__(MIoTCameraInstance)
    camera._callbacks = {}
    camera._next_reg_id = 1
    camera._decoded_frame_interval = 333
    camera._full_rate_video_reg_ids = {}
    camera._decoders = [Mock()]
    camera._MIoTCameraInstance__update_raw_data_register_status_async = AsyncMock()
    callback = AsyncMock()

    first = await camera.register_decode_video_frame_async(
        callback, channel=0, multi_reg=True, full_rate=True
    )
    second = await camera.register_decode_video_frame_async(
        callback, channel=0, multi_reg=True, full_rate=True
    )

    camera._decoders[0].set_decoded_frame_interval.assert_called_once_with(0)

    await camera.unregister_decode_video_frame_async(channel=0, reg_id=first)
    camera._decoders[0].set_decoded_frame_interval.assert_called_once_with(0)

    await camera.unregister_decode_video_frame_async(channel=0, reg_id=second)
    assert camera._decoders[0].set_decoded_frame_interval.call_args_list[-1].args == (
        333,
    )


@pytest.mark.asyncio
async def test_live_service_marks_web_subscription_as_full_rate():
    proxy = SimpleNamespace(start_camera_decode_video_stream=AsyncMock(return_value=7))
    service = MiotService.__new__(MiotService)
    service._miot_proxy = proxy
    callback = AsyncMock()

    reg_id = await service.start_video_stream("camera-1", 0, callback)

    assert reg_id == 7
    proxy.start_camera_decode_video_stream.assert_awaited_once_with(
        "camera-1", 0, callback, full_rate=True
    )


def test_decoded_frame_interval_stays_below_silence_reconnect_threshold():
    with pytest.raises(ValidationError):
        CameraSettings(decoded_frame_interval=10_001)
