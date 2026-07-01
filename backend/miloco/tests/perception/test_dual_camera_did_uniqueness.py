"""Regression tests for dual-camera synthetic did uniqueness.

Ensures that dual-camera devices produce unique DeviceData.meta.did
for each channel, preventing key collision in downstream pipeline.
"""

from __future__ import annotations

import numpy as np
import pytest
from miloco.perception.collect.camera_adapter import (
    CameraDeviceAdapter,
    _CameraDeviceState,
    _ChannelState,
)
from miloco.perception.collect.stream_buffer import StreamFragment
from miloco.perception.schema import DecodedVideoFrame


class _CachedCamera:
    def __init__(self, *, did: str = "cam1", name: str = "cam1", room_name: str = "r1"):
        self._payload = {
            "did": did,
            "name": name,
            "online": True,
            "lan_online": True,
            "room_name": room_name,
            "channel_count": 2,
        }

    def model_dump(self):
        return self._payload


class _Proxy:
    def __init__(self, camera: _CachedCamera | None):
        self._camera = camera

    def get_cached_camera(self, did: str):
        return self._camera


def _make_fragment(frame, stream_ts: int, wall_ms: int) -> StreamFragment:
    return StreamFragment(data=frame, stream_ts=stream_ts, wall_ms=wall_ms)


def _make_video_frame(stream_ts: int, wall_ms: int) -> DecodedVideoFrame:
    return DecodedVideoFrame(
        frame=np.zeros((2, 2, 3), dtype=np.uint8),
        stream_ts=stream_ts,
        wall_ms=wall_ms,
        unix_ms=wall_ms,
        decode_latency_ms=10.0,
    )


class TestDualCameraDidUniqueness:
    """Verify that dual-camera channels produce unique synthetic dids."""

    def test_dual_camera_channels_have_unique_dids(self):
        """Two channels of the same camera must have different meta.did."""
        proxy = _Proxy(_CachedCamera(did="cam1", name="cam1", room_name="r1"))
        adapter = CameraDeviceAdapter(miot_proxy=proxy)

        # Create dual-camera state
        state = _CameraDeviceState(did="cam1", channel_count=2)
        adapter._devices["cam1"] = state

        # Build device data for channel 0
        ch0 = state.channels[0]
        ch0.epoch_delta = 1_700_000_000_000
        frame0 = _make_video_frame(100, 100)
        tracks0 = {"decoded_video": [_make_fragment(frame0, 100, 100)], "decoded_audio": []}
        dd0 = adapter._build_device_data(state, ch0, tracks0, 100, 200)

        # Build device data for channel 1
        ch1 = state.channels[1]
        ch1.epoch_delta = 1_700_000_000_000
        frame1 = _make_video_frame(200, 200)
        tracks1 = {"decoded_video": [_make_fragment(frame1, 200, 200)], "decoded_audio": []}
        dd1 = adapter._build_device_data(state, ch1, tracks1, 200, 300)

        # Verify both produced data
        assert dd0 is not None
        assert dd1 is not None

        # Verify unique dids
        assert dd0.meta.did != dd1.meta.did, \
            f"Dual-camera channels must have unique dids, got {dd0.meta.did} and {dd1.meta.did}"

        # Verify synthetic did format
        assert dd0.meta.did == "cam1:ch0"
        assert dd1.meta.did == "cam1:ch1"

    def test_single_camera_uses_original_did(self):
        """Single-camera device should use original did (no :ch suffix)."""
        proxy = _Proxy(_CachedCamera(did="cam1", name="cam1", room_name="r1"))
        adapter = CameraDeviceAdapter(miot_proxy=proxy)

        # Create single-camera state
        state = _CameraDeviceState(did="cam1", channel_count=1)
        adapter._devices["cam1"] = state

        # Build device data for channel 0
        ch0 = state.channels[0]
        ch0.epoch_delta = 1_700_000_000_000
        frame = _make_video_frame(100, 100)
        tracks = {"decoded_video": [_make_fragment(frame, 100, 100)], "decoded_audio": []}
        dd = adapter._build_device_data(state, ch0, tracks, 100, 200)

        # Verify original did (no :ch suffix)
        assert dd is not None
        assert dd.meta.did == "cam1"

    def test_dual_camera_get_connected_devices_unique_dids(self):
        """get_connected_devices should return unique dids for dual-camera channels."""
        proxy = _Proxy(_CachedCamera(did="cam1", name="cam1", room_name="r1"))
        adapter = CameraDeviceAdapter(miot_proxy=proxy)

        # Create dual-camera state
        state = _CameraDeviceState(did="cam1", channel_count=2)
        adapter._devices["cam1"] = state

        # Get connected devices
        connected = adapter.get_connected_devices()

        # Verify unique dids
        dids = list(connected.keys())
        assert len(dids) == len(set(dids)), \
            f"Connected devices must have unique dids, got {dids}"

        # Verify synthetic did format
        assert "cam1:ch0" in connected
        assert "cam1:ch1" in connected
