"""Optional on-device tests. Run explicitly with MIOT_TEST_VAAPI_SAMPLE set."""

import os
import time
from unittest.mock import Mock

import av
import pytest
from miot.decoder import MIoTMediaDecoder
from miot.types import MIoTCameraCodec, MIoTCameraFrameData, MIoTCameraFrameType
from miot.vaapi import FFmpegVAAPIDecoder

SAMPLE = os.environ.get("MIOT_TEST_VAAPI_SAMPLE")
pytestmark = pytest.mark.skipif(not SAMPLE, reason="requires VAAPI sample/device")


def inputs():
    with av.open(SAMPLE, format="h264") as source:
        data = [bytes(p) for p in source.demux(video=0) if p.size][:90]
    return [
        MIoTCameraFrameData(
            codec_id=MIoTCameraCodec.VIDEO_H264,
            length=len(p),
            timestamp=100000 + i * 67,
            sequence=i,
            frame_type=MIoTCameraFrameType.FRAME_P,
            channel=0,
            data=p,
            recv_unix_ms=200000 + i * 67,
        )
        for i, p in enumerate(data)
    ]


def test_real_worker_preserves_sampled_timestamps_and_exits():
    data = inputs()
    seen = []
    worker = FFmpegVAAPIDecoder(
        data[0],
        ffmpeg="/usr/bin/ffmpeg",
        interval_ms=300,
        on_frame=lambda f, m: seen.append((f, m)),
    )
    try:
        start = time.monotonic()
        for i, item in enumerate(data):
            delay = start + i / 15 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            worker.push(item)
        deadline = time.monotonic() + 3
        while len(seen) < 18 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert [m.timestamp for f, m in seen] == [x.timestamp for x in data[::5]]
        assert [m.recv_unix_ms for f, m in seen] == [x.recv_unix_ms for x in data[::5]]
        assert all(f.width == 640 and f.height == 360 for f, m in seen)
        assert len(worker._pending) < 5
    finally:
        worker.close()
    assert worker._process.poll() is not None
    assert not worker._reader.is_alive()
    assert not worker._watcher.is_alive()
    worker.close()


def test_real_missing_executable_uses_original_software(monkeypatch):
    data = inputs()[:30]
    images = []
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/not/installed/ffmpeg")
    decoder = MIoTMediaDecoder(
        10**15,
        Mock(),
        video_frame_callback=lambda *args: images.append(args),
        enable_hw_accel=True,
        main_loop=Mock(),
    )
    for item in data:
        decoder._on_video_callback(item)
    assert decoder._hw_failed
    assert decoder._hw_decoder is None
    assert len(images) == 30
    assert images[-1][1] == data[-1].timestamp


def test_real_runtime_crash_resumes_software(monkeypatch):
    data = inputs()
    images = []
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    decoder = MIoTMediaDecoder(
        10**15,
        Mock(),
        video_frame_callback=lambda *args: images.append(args),
        enable_hw_accel=True,
        main_loop=Mock(),
        decoded_frame_interval=0,
    )
    start = time.monotonic()
    try:
        for i, item in enumerate(data[:30]):
            delay = start + i / 15 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            decoder._on_video_callback(item)
        hw = decoder._hw_decoder
        assert hw is not None
        hw._process.kill()
        hw._process.wait(2)
        time.sleep(0.3)
        for item in data[30:]:
            decoder._on_video_callback(item)
        assert decoder._hw_failed
        assert decoder._hw_decoder is None
        # After the kill the next IDR is admitted by software, followed by later frames.
        assert images[-1][1] == data[-1].timestamp
        assert len(images) >= 60
        assert not hw._reader.is_alive()
        assert not hw._watcher.is_alive()
    finally:
        if decoder._hw_decoder is not None:
            decoder._hw_decoder.close()
