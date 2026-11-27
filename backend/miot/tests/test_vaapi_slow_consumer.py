import os
import threading
import time

import av
import pytest
from miot.types import MIoTCameraCodec, MIoTCameraFrameData, MIoTCameraFrameType
from miot.vaapi import FFmpegVAAPIDecoder

SAMPLE = os.environ.get("MIOT_TEST_VAAPI_SAMPLE")
CODEC = os.environ.get("MIOT_TEST_VAAPI_CODEC", "h264")
pytestmark = pytest.mark.skipif(not SAMPLE, reason="requires VAAPI sample/device")


def test_real_reader_drains_while_consumer_is_blocked(monkeypatch):
    codec, sample = CODEC, SAMPLE
    monkeypatch.setenv("LIBVA_DRIVER_NAME", "i965")
    with av.open(sample, format=codec) as source:
        packets = [bytes(p) for p in source.demux(video=0) if p.size][:90]
    data = [
        MIoTCameraFrameData(
            codec_id=MIoTCameraCodec.VIDEO_H264
            if codec == "h264"
            else MIoTCameraCodec.VIDEO_H265,
            length=len(p),
            timestamp=100000 + i * 67,
            sequence=i,
            frame_type=MIoTCameraFrameType.FRAME_P,
            channel=0,
            data=p,
            recv_unix_ms=200000 + i * 67,
        )
        for i, p in enumerate(packets)
    ]
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    errors = []
    seen = []

    def consume(frame, meta):
        if not entered.is_set():
            entered.set()
            assert release.wait(12)
        # Exercise ownership: read the frame after the reader advanced independently.
        assert frame.to_ndarray(format="bgr24").shape == (360, 640, 3)
        seen.append(meta.timestamp)

    worker = FFmpegVAAPIDecoder(
        data[0], ffmpeg="/usr/bin/ffmpeg", on_frame=consume, label="test-" + codec
    )

    def produce():
        start = time.monotonic()
        try:
            for i, item in enumerate(data):
                delay = start + i / 15 - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                worker.push(item)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    producer = threading.Thread(target=produce)
    producer.start()
    try:
        assert entered.wait(3)
        assert done.wait(8), "compressed producer blocked by slow frame consumer"
        assert not errors
        deadline = time.monotonic() + 2
        while (
            worker.metrics().get("output_frames", 0) < 85
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        m = worker.metrics()
        assert m["input_packets"] == 90
        assert m["output_frames"] >= 85
        assert m["delivery"]["pending"] <= 1
        assert m["delivery"]["replaced"] > 50
        assert worker._error is None
        release.set()
        deadline = time.monotonic() + 2
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(seen) >= 2
        assert seen[-1] >= data[-5].timestamp
    finally:
        release.set()
        worker.close()
        producer.join(3)
    assert not producer.is_alive()
    assert not worker._delivery.snapshot()["alive"]
    assert not worker._reader.is_alive()
    assert not worker._watcher.is_alive()
