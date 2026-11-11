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
CODEC = os.environ.get("MIOT_TEST_VAAPI_CODEC", "h264")
pytestmark = pytest.mark.skipif(not SAMPLE, reason="requires VAAPI sample/device")


def inputs():
    with av.open(SAMPLE, format=CODEC) as source:
        data = [bytes(p) for p in source.demux(video=0) if p.size][:90]
    return [
        MIoTCameraFrameData(
            codec_id=MIoTCameraCodec.VIDEO_H265
            if CODEC == "hevc"
            else MIoTCameraCodec.VIDEO_H264,
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


def test_real_device_recovers_once_then_falls_back(monkeypatch):
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
    workers = []
    start = time.monotonic()
    try:
        for i, item in enumerate(data):
            delay = start + i / 15 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if i in (22, 52):
                hw = decoder._hw_decoder
                assert hw is not None
                workers.append(hw)
                hw._process.kill()
                hw._process.wait(2)
                time.sleep(0.3)
            decoder._on_video_callback(item)
            if i == 45:
                assert not decoder._hw_failed
                assert decoder._hw_decoder is not None
                assert decoder._hw_decoder is not workers[0]
        assert decoder._hw_failed
        assert decoder._hw_decoder is None
        assert decoder._hw_recoveries == 1
        assert images[-1][1] == data[-1].timestamp
        assert all(w._process.poll() is not None for w in workers)
        assert all(
            not w._reader.is_alive() and not w._watcher.is_alive() for w in workers
        )
        assert all(not w._delivery.snapshot()["alive"] for w in workers)
    finally:
        if decoder._hw_decoder is not None:
            decoder._hw_decoder.close()


def test_real_rate_changes_keep_hardware_and_reap_previous_workers(monkeypatch):
    codec, sample = CODEC, SAMPLE
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setenv("LIBVA_DRIVER_NAME", "i965")
    with av.open(sample, format=codec) as source:
        packets = [bytes(p) for p in source.demux(video=0) if p.size][:90]
    seen = []
    decoder = MIoTMediaDecoder(
        10**15,
        Mock(),
        video_frame_callback=lambda *args: seen.append(args),
        enable_hw_accel=True,
        main_loop=Mock(),
        decoded_frame_interval=300,
    )
    workers = []
    start = time.monotonic()
    try:
        for i, payload in enumerate(packets):
            if i == 22:
                decoder.set_decoded_frame_interval(150, reason="test_faster")
            if i == 52:
                decoder.set_decoded_frame_interval(500, reason="test_slower")
            delay = start + i / 15 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            item = MIoTCameraFrameData(
                codec_id=MIoTCameraCodec.VIDEO_H264
                if codec == "h264"
                else MIoTCameraCodec.VIDEO_H265,
                length=len(payload),
                timestamp=100000 + i * 67,
                sequence=i,
                frame_type=MIoTCameraFrameType.FRAME_P,
                channel=0,
                data=payload,
                recv_unix_ms=200000 + i * 67,
            )
            decoder._on_video_callback(item)
            worker = decoder._hw_decoder
            assert worker is not None
            assert not decoder._hw_failed
            if not workers or worker is not workers[-1]:
                workers.append(worker)
        deadline = time.monotonic() + 3
        while not any(x[1] >= 104020 for x in seen) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(workers) == 3
        assert all(w._process.poll() is not None for w in workers[:-1])
        assert any(x[1] >= 104020 for x in seen)
        assert workers[-1]._process.poll() is None
        assert not decoder._hw_failed
    finally:
        if decoder._hw_decoder is not None:
            decoder._hw_decoder.close()
    assert all(not w._reader.is_alive() and not w._watcher.is_alive() for w in workers)


def test_real_timestamp_reset_keeps_hardware(monkeypatch):
    data = inputs()
    seen = []
    monkeypatch.setenv("MIOT_VAAPI_FFMPEG", "/usr/bin/ffmpeg")
    decoder = MIoTMediaDecoder(
        10**15,
        Mock(),
        video_frame_callback=lambda *args: seen.append(args),
        enable_hw_accel=True,
        main_loop=Mock(),
        decoded_frame_interval=0,
    )
    workers = []
    start = time.monotonic()
    try:
        for i, item in enumerate(data):
            if i >= 40:
                item = item.model_copy(update={"timestamp": item.timestamp - 10000})
            delay = start + i / 15 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            decoder._on_video_callback(item)
            worker = decoder._hw_decoder
            if worker is not None and worker not in workers:
                workers.append(worker)
        deadline = time.monotonic() + 3
        while (
            not any(x[1] == item.timestamp for x in seen)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert len(workers) == 2
        assert not decoder._hw_failed
        assert decoder._hw_recoveries == 0
        assert any(x[1] == item.timestamp for x in seen)
        assert workers[0]._process.poll() is not None
    finally:
        if decoder._hw_decoder is not None:
            decoder._hw_decoder.close()
    assert all(not worker._reader.is_alive() for worker in workers)
