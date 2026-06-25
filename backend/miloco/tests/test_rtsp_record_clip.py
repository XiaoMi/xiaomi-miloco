from __future__ import annotations

import asyncio

import numpy as np
import pytest
from miloco.rtsp.service import record_rtsp_clip


class _FakeRtspService:
    def __init__(
        self,
        frames: list[np.ndarray | None],
        *,
        fallback: np.ndarray | None = None,
    ):
        self.frames = frames
        self.fallback = fallback
        self.ensured: list[str] = []

    def ensure_reader(self, did: str) -> None:
        self.ensured.append(did)

    def latest_frame(self, did: str) -> np.ndarray | None:
        if self.frames:
            return self.frames.pop(0)
        return self.fallback


class _FakeRecorder:
    def __init__(self, duration_ms: int):
        self.duration_ms = duration_ms
        self.fed: list[tuple[tuple[int, ...], int]] = []
        self.cancelled = False

    async def feed_bgr(self, frame: np.ndarray, ts_ms: int) -> None:
        self.fed.append((frame.shape, ts_ms))

    async def wait(self, timeout: float) -> bytes:
        return b"mp4"

    def cancel(self) -> None:
        self.cancelled = True


def test_record_rtsp_clip_uses_rtsp_frames_and_returns_mp4():
    recorder = _FakeRecorder(duration_ms=1)
    service = _FakeRtspService(
        [np.zeros((4, 4, 3), dtype=np.uint8)],
        fallback=np.zeros((4, 4, 3), dtype=np.uint8),
    )

    out = asyncio.run(
        record_rtsp_clip(
            "rtsp:abc",
            duration_ms=1,
            service=service,
            recorder_factory=lambda duration_ms: recorder,
            poll_interval_s=0,
            timeout_s=1,
        )
    )

    assert out == b"mp4"
    assert service.ensured == ["rtsp:abc"]
    assert recorder.fed
    assert recorder.cancelled is True


def test_record_rtsp_clip_times_out_without_frames():
    recorder = _FakeRecorder(duration_ms=1)
    service = _FakeRtspService([None, None, None])

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            record_rtsp_clip(
                "rtsp:abc",
                duration_ms=1,
                service=service,
                recorder_factory=lambda duration_ms: recorder,
                poll_interval_s=0,
                timeout_s=0.001,
            )
        )

    assert service.ensured == ["rtsp:abc"]
    assert recorder.fed == []
    assert recorder.cancelled is True
