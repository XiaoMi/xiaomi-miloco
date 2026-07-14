"""Tests for decode-latency instrumentation in CameraDeviceAdapter.

Decode is measured inside the MIoT decoder and flows through the adapter
as two host-local timestamps (``recv_unix_ms``, ``decoded_unix_ms``).
The adapter's job is to compute ``decode_latency_ms`` from those and
pack per-window frame-count-weighted aggregates onto the DeviceData.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from miloco.perception.collect.camera_adapter import (
    CameraDeviceAdapter,
    _CameraDeviceState,
)
from miloco.perception.collect.stream_buffer import StreamFragment
from miloco.perception.schema import DecodedAudioFrame, DecodedVideoFrame


def _make_state(
    epoch_delta: int | None = 1_700_000_000_000, did: str = "cam1"
) -> _CameraDeviceState:
    state = _CameraDeviceState(did=did)
    state.epoch_delta = epoch_delta
    return state


class _CachedCamera:
    def __init__(
        self, *, did: str = "cam1", name: str = "cam1", room_name: str = "r2"
    ):
        self._payload = {
            "did": did,
            "name": name,
            "online": True,
            "lan_online": True,
            "room_name": room_name,
        }

    def model_dump(self):
        return self._payload


class _Proxy:
    def __init__(self, camera: _CachedCamera | None):
        self._camera = camera

    def get_cached_camera(self, did: str):
        return self._camera


class _StreamProxy(_Proxy):
    is_authenticated = True

    async def start_camera_decode_video_stream(self, did, channel, callback):
        return 1

    async def start_camera_decode_audio_stream(self, did, channel, callback):
        return 2


class TestComputeDecodeLatency:
    def test_positive_decode_span(self):
        # decoded - recv = 35ms
        dec = CameraDeviceAdapter._compute_decode_latency(
            recv_unix_ms=1_700_000_000_580,
            decoded_unix_ms=1_700_000_000_615,
        )
        assert dec == 35.0

    def test_negative_decode_clamped_to_zero(self):
        # Clock skew — decoder stamp earlier than recv → clamp to 0.
        dec = CameraDeviceAdapter._compute_decode_latency(
            recv_unix_ms=1_700_000_000_600,
            decoded_unix_ms=1_700_000_000_590,
        )
        assert dec == 0.0

    def test_legacy_zero_stamps_return_zero(self):
        # Callbacks that pre-date instrumentation — no usable data.
        dec = CameraDeviceAdapter._compute_decode_latency(
            recv_unix_ms=0,
            decoded_unix_ms=0,
        )
        assert dec == 0.0


class TestCallbackIntegration:
    """Drive the async decoded-frame callbacks end-to-end."""

    def _make_adapter_with_device(self) -> tuple[CameraDeviceAdapter, _CameraDeviceState]:
        adapter = CameraDeviceAdapter(miot_proxy=object())  # type: ignore[arg-type]
        state = _make_state(epoch_delta=None)  # uncalibrated; first frame calibrates
        adapter._devices["cam1"] = state
        return adapter, state

    def test_video_frame_records_decode_latency(self, monkeypatch):
        adapter, state = self._make_adapter_with_device()
        monkeypatch.setattr(
            "miloco.perception.collect.camera_adapter._monotonic_ms",
            lambda: 5_000,
        )
        monkeypatch.setattr(
            "miloco.perception.collect.camera_adapter._unix_ms",
            lambda: 1_700_000_005_000,
        )

        cb = adapter._make_decoded_video_callback("cam1")
        # recv=...090, decoded=...130 → decode=40ms
        asyncio.run(
            cb(
                "cam1",
                np.zeros((2, 2, 3), dtype=np.uint8),
                4_800,
                0,
                1_700_000_005_090,
                1_700_000_005_130,
            )
        )

        ready = state.sync_buffer.peek_latest(duration_ms=10_000)
        assert ready is not None
        frag = ready["decoded_video"][0]
        assert isinstance(frag, StreamFragment)
        decoded = frag.data
        assert isinstance(decoded, DecodedVideoFrame)
        assert decoded.recv_unix_ms == 1_700_000_005_090
        assert decoded.decoded_unix_ms == 1_700_000_005_130
        assert decoded.decode_latency_ms == 40.0

    def test_audio_frame_records_decode_latency(self, monkeypatch):
        adapter, state = self._make_adapter_with_device()
        monkeypatch.setattr(
            "miloco.perception.collect.camera_adapter._monotonic_ms",
            lambda: 4_925,
        )

        cb = adapter._make_decoded_audio_callback("cam1")
        # recv=...870, decoded=...995 → decode=125ms
        asyncio.run(
            cb(
                "cam1",
                np.zeros(320, dtype=np.int16),
                4_800,
                0,
                1_700_000_004_870,
                1_700_000_004_995,
            )
        )

        ready = state.sync_buffer.peek_latest(duration_ms=10_000)
        assert ready is not None
        decoded: DecodedAudioFrame = ready["decoded_audio"][0].data
        assert decoded.recv_unix_ms == 1_700_000_004_870
        assert decoded.decoded_unix_ms == 1_700_000_004_995
        assert decoded.decode_latency_ms == 125.0

    def test_missing_device_is_noop(self, monkeypatch):
        adapter, _ = self._make_adapter_with_device()
        cb = adapter._make_decoded_video_callback("cam_unknown")
        # Should not raise and should produce nothing useful.
        asyncio.run(
            cb(
                "cam_unknown",
                np.zeros((2, 2, 3), dtype=np.uint8),
                1_000,
                0,
                1_700_000_000_100,
                1_700_000_000_120,
            )
        )


    def test_connect_device_uses_source_only_as_presence_marker(self):
        proxy = _StreamProxy(_CachedCamera(name="cache-name", room_name="cache-room"))
        adapter = CameraDeviceAdapter(miot_proxy=proxy)  # type: ignore[arg-type]

        asyncio.run(adapter.connect_device("cam1", source=object()))  # type: ignore[arg-type]

        state = adapter._devices["cam1"]
        source = adapter.get_connected_devices()["cam1"]
        assert state.did == "cam1"
        assert not hasattr(state, "source")
        assert source.name == "cache-name"
        assert source.room_name == "cache-room"


class TestBuildDeviceDataAggregation:
    """_build_device_data packs per-window decode aggregates.

    Each decoded frame carries ``decode_latency_ms``; the adapter packs
    three per-window aggregates — video / audio / combined — onto the
    DeviceData.
    """

    def _fragment(self, frame, stream_ts: int, wall_ms: int) -> StreamFragment:
        return StreamFragment(data=frame, stream_ts=stream_ts, wall_ms=wall_ms)

    def test_empty_tracks_return_none(self):
        adapter = CameraDeviceAdapter(miot_proxy=object())  # type: ignore[arg-type]
        state = _make_state()
        dd = adapter._build_device_data(
            state,
            tracks={"decoded_video": [], "decoded_audio": []},
            window_start_ms=0,
            window_end_ms=1000,
        )
        assert dd is None

    def test_current_source_uses_cached_camera_metadata(self):
        adapter = CameraDeviceAdapter(
            miot_proxy=_Proxy(_CachedCamera(name="cam-new", room_name="r-new"))
        )  # type: ignore[arg-type]
        state = _make_state()
        frame = DecodedVideoFrame(
            frame=np.zeros((2, 2, 3), dtype=np.uint8),
            stream_ts=100,
            wall_ms=100,
            unix_ms=100,
            decode_latency_ms=10.0,
        )
        tracks = {
            "decoded_video": [self._fragment(frame, frame.stream_ts, frame.wall_ms)],
            "decoded_audio": [],
        }

        dd = adapter._build_device_data(state, tracks, 100, 200)

        assert dd is not None
        assert dd.meta.name == "cam-new"
        assert dd.meta.room_name == "r-new"

    def test_get_connected_devices_reads_latest_cached_metadata(self):
        proxy = _Proxy(_CachedCamera(name="cam-old", room_name="r-old"))
        adapter = CameraDeviceAdapter(miot_proxy=proxy)  # type: ignore[arg-type]
        adapter._devices["cam1"] = _make_state()

        first = adapter.get_connected_devices()["cam1"]
        proxy._camera = _CachedCamera(name="cam-new", room_name="r-new")
        second = adapter.get_connected_devices()["cam1"]

        assert first.name == "cam-old"
        assert first.room_name == "r-old"
        assert second.name == "cam-new"
        assert second.room_name == "r-new"

    def test_current_source_falls_back_to_did_without_cache(self):
        adapter = CameraDeviceAdapter(miot_proxy=object())  # type: ignore[arg-type]
        adapter._devices["cam1"] = _make_state()

        source = adapter.get_connected_devices()["cam1"]

        assert source.did == "cam1"
        assert source.name == "cam1"
        assert source.room_name == "cam1"

    def test_video_only_average(self):
        adapter = CameraDeviceAdapter(miot_proxy=object())  # type: ignore[arg-type]
        state = _make_state()
        specs = [(100, 10.0), (200, 30.0), (300, 50.0)]
        frames = [
            DecodedVideoFrame(
                frame=np.zeros((2, 2, 3), dtype=np.uint8),
                stream_ts=t,
                wall_ms=t,
                unix_ms=t,
                decode_latency_ms=dec,
            )
            for t, dec in specs
        ]
        tracks = {
            "decoded_video": [self._fragment(f, f.stream_ts, f.wall_ms) for f in frames],
            "decoded_audio": [],
        }
        dd = adapter._build_device_data(state, tracks, 100, 400)
        assert dd is not None
        assert dd.decode_video_avg_ms == pytest.approx(30.0)
        assert dd.decode_audio_avg_ms == 0.0
        assert dd.decode_avg_ms == pytest.approx(30.0)

    def test_audio_only_average(self):
        adapter = CameraDeviceAdapter(miot_proxy=object())  # type: ignore[arg-type]
        state = _make_state()
        specs = [(100, 5.0), (200, 15.0)]
        frames = [
            DecodedAudioFrame(
                frame=np.zeros(320, dtype=np.int16),
                stream_ts=t,
                wall_ms=t,
                unix_ms=t,
                decode_latency_ms=dec,
            )
            for t, dec in specs
        ]
        tracks = {
            "decoded_video": [],
            "decoded_audio": [self._fragment(f, f.stream_ts, f.wall_ms) for f in frames],
        }
        dd = adapter._build_device_data(state, tracks, 100, 300)
        assert dd is not None
        assert dd.decode_video_avg_ms == 0.0
        assert dd.decode_audio_avg_ms == pytest.approx(10.0)
        assert dd.decode_avg_ms == pytest.approx(10.0)

    def test_mixed_is_frame_count_weighted(self):
        """Combined average weights every frame the same — not the two means."""
        adapter = CameraDeviceAdapter(miot_proxy=object())  # type: ignore[arg-type]
        state = _make_state()
        # video: 3 frames @ decode=30ms → sum 90
        vframes = [
            DecodedVideoFrame(
                frame=np.zeros((2, 2, 3), dtype=np.uint8),
                stream_ts=t,
                wall_ms=t,
                unix_ms=t,
                decode_latency_ms=30.0,
            )
            for t in (100, 200, 300)
        ]
        # audio: 1 frame @ decode=90ms → sum 90
        aframes = [
            DecodedAudioFrame(
                frame=np.zeros(320, dtype=np.int16),
                stream_ts=150,
                wall_ms=150,
                unix_ms=150,
                decode_latency_ms=90.0,
            )
        ]
        tracks = {
            "decoded_video": [self._fragment(f, f.stream_ts, f.wall_ms) for f in vframes],
            "decoded_audio": [self._fragment(f, f.stream_ts, f.wall_ms) for f in aframes],
        }
        dd = adapter._build_device_data(state, tracks, 100, 400)
        assert dd is not None
        assert dd.decode_video_avg_ms == pytest.approx(30.0)
        assert dd.decode_audio_avg_ms == pytest.approx(90.0)
        # Weighted combined: (90 + 90) / (3 + 1) = 45
        assert dd.decode_avg_ms == pytest.approx(45.0)
