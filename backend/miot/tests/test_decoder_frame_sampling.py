from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import miot.decoder as decoder_module
import numpy as np
from miot.decoder import MIoTMediaDecoder
from miot.types import MIoTCameraCodec


class _FakeLoop:
    def __init__(self) -> None:
        self.scheduled: list[tuple[object, tuple[object, ...]]] = []

    def call_soon_threadsafe(self, callback, *args) -> None:
        self.scheduled.append((callback, args))

    def create_task(self, coro):
        return coro


class _FakeFrame:
    def __init__(self) -> None:
        self.ndarray_calls = 0

    def to_ndarray(self, **kwargs):
        self.ndarray_calls += 1
        return np.zeros((8, 8, 3), dtype=np.uint8)


class _FakeCodec:
    def __init__(self, frame: _FakeFrame) -> None:
        self.frame = frame
        self.decode_calls = 0

    def decode(self, packet):
        self.decode_calls += 1
        return [self.frame]


def _make_decoder(
    interval_ms: int,
) -> tuple[MIoTMediaDecoder, _FakeLoop, _FakeFrame, _FakeCodec]:
    loop = _FakeLoop()
    decoder = MIoTMediaDecoder(
        frame_interval=1000,
        decoded_frame_interval=interval_ms,
        video_callback=Mock(return_value=None),
        video_frame_callback=Mock(return_value=None),
        main_loop=loop,
    )
    frame = _FakeFrame()
    codec = _FakeCodec(frame)
    decoder._video_decoder = codec
    decoder._last_jpeg_ts = 10**15
    return decoder, loop, frame, codec


def _frame_data(timestamp: int):
    return SimpleNamespace(
        codec_id=MIoTCameraCodec.VIDEO_H264,
        data=b"encoded",
        timestamp=timestamp,
        channel=0,
        recv_unix_ms=0,
    )


def test_decoded_callback_rate_limits_before_bgr_conversion(monkeypatch) -> None:
    decoder, loop, frame, codec = _make_decoder(interval_ms=500)
    ticks = iter((1000, 1200, 1500))
    monkeypatch.setattr(decoder_module, "Packet", lambda data: data)
    monkeypatch.setattr(decoder_module, "_monotonic_ms", lambda: next(ticks))

    decoder._on_video_callback(_frame_data(1))
    decoder._on_video_callback(_frame_data(2))
    decoder._on_video_callback(_frame_data(3))

    assert codec.decode_calls == 3
    assert frame.ndarray_calls == 2
    assert len(loop.scheduled) == 2


def test_zero_interval_preserves_every_decoded_callback(monkeypatch) -> None:
    decoder, loop, frame, codec = _make_decoder(interval_ms=0)
    ticks = iter((1000, 1001, 1002))
    monkeypatch.setattr(decoder_module, "Packet", lambda data: data)
    monkeypatch.setattr(decoder_module, "_monotonic_ms", lambda: next(ticks))

    decoder._on_video_callback(_frame_data(1))
    decoder._on_video_callback(_frame_data(2))
    decoder._on_video_callback(_frame_data(3))

    assert codec.decode_calls == 3
    assert frame.ndarray_calls == 3
    assert len(loop.scheduled) == 3


def test_switching_to_full_rate_emits_the_next_frame_immediately(monkeypatch) -> None:
    decoder, loop, frame, codec = _make_decoder(interval_ms=500)
    ticks = iter((1000, 1100, 1101))
    monkeypatch.setattr(decoder_module, "Packet", lambda data: data)
    monkeypatch.setattr(decoder_module, "_monotonic_ms", lambda: next(ticks))

    decoder._on_video_callback(_frame_data(1))
    decoder._on_video_callback(_frame_data(2))
    decoder.set_decoded_frame_interval(0)
    decoder._on_video_callback(_frame_data(3))

    assert codec.decode_calls == 3
    assert frame.ndarray_calls == 2
    assert len(loop.scheduled) == 2
