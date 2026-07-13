import numpy as np
from miloco.perception.engine.config import GateConfig
from miloco.perception.engine.gate.gate import run_gate
from miloco.perception.engine.types import GateTiming
from miloco.perception.types import (
    AudioFrame,
    AudioStream,
    DeviceSnapshot,
    PerceptionDevice,
    VideoFrame,
    VideoStream,
)


def _make_slice() -> DeviceSnapshot:
    device = PerceptionDevice(did="d1", name="d1", device_type="camera",
                              room_name="r1")
    video_frames = [
        VideoFrame(data=np.zeros((180, 320, 3), dtype=np.uint8), timestamp=i * 1000.0)
        for i in range(3)
    ]
    audio_frames = [AudioFrame(data=np.zeros(48000, dtype=np.int16), timestamp=0.0)]
    return DeviceSnapshot(
        device=device,
        start_timestamp=0.0,
        end_timestamp=3000.0,
        video=VideoStream(frames=video_frames, width=320, height=180),
        audio=AudioStream(frames=audio_frames, sample_rate=16000),
    )


async def test_run_gate_returns_packet_and_timing():
    cfg = GateConfig()
    result = await run_gate(_make_slice(), cfg, input_fps=1)
    assert isinstance(result, tuple) and len(result) == 5
    _packet, timing, _last_checked, _new_last_v, _new_last_a = result
    assert isinstance(timing, GateTiming)
    assert timing.video_ms >= 0
    assert timing.audio_ms >= 0
    assert isinstance(timing.video_pass, bool)
    assert isinstance(timing.audio_pass, bool)
    assert isinstance(timing.hold_pass, bool)
