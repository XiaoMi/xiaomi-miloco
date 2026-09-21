from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from miloco.observability.perception_flow import (
    GraphStatus,
    PerceptionFlowCycleDiagnostics,
)
from miloco.perception.engine.config import PerceptionConfig
from miloco.perception.engine.pipeline import (
    _mark_failed_omni_stage,
    run_batch_pipeline,
)
from miloco.perception.engine.types import GateTiming, OmniContext
from miloco.perception.processor import PipelineProcessor
from miloco.perception.types import (
    BatchedSnapshot,
    DeviceSnapshot,
    PerceptionDevice,
    RealtimePerceptionResult,
    VideoFrame,
    VideoStream,
)


def _snapshot(device_id: str) -> DeviceSnapshot:
    frames = [
        VideoFrame(
            data=np.zeros((48, 64, 3), dtype=np.uint8),
            timestamp=1_000 + index * 500,
        )
        for index in range(6)
    ]
    return DeviceSnapshot(
        device=PerceptionDevice(
            did=device_id,
            name=device_id,
            device_type="camera",
            room_name="Living Room",
        ),
        start_timestamp=1_000,
        end_timestamp=4_000,
        video=VideoStream(frames=frames, width=64, height=48),
    )


def _skipped_gate_result(last_checked=None):
    return (
        None,
        GateTiming(
            video_ms=1.0,
            audio_ms=2.0,
            video_pass=False,
            audio_pass=False,
        ),
        last_checked,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_batch_diagnostics_reuse_explicit_trace_ids_and_record_gate_skip():
    batch = BatchedSnapshot(snapshots=[_snapshot("camera-1")])
    config = PerceptionConfig()

    with patch(
        "miloco.perception.engine.pipeline.run_gate",
        new=AsyncMock(return_value=_skipped_gate_result()),
    ):
        result = await run_batch_pipeline(
            batch,
            {"camera-1": OmniContext()},
            config,
            trace_id="trace-1",
            device_trace_ids={"camera-1": "device-trace-1"},
            collect_flow_diagnostics=True,
        )

    diagnostic = result.flow_diagnostics.devices["camera-1"]
    assert result.flow_diagnostics.cycle_id == "trace-1"
    assert diagnostic.trace_id == "trace-1"
    assert diagnostic.device_trace_id == "device-trace-1"
    assert diagnostic.source_width == 64
    assert diagnostic.source_height == 48
    assert diagnostic.source_frame_count == 6
    assert diagnostic.status == GraphStatus.SKIPPED
    assert diagnostic.gate_status == GraphStatus.SKIPPED
    assert diagnostic.identity_status == GraphStatus.SKIPPED
    assert diagnostic.omni_status == GraphStatus.SKIPPED
    assert result.rooms["Living Room"].timing["_device_trace_id_camera-1"] == "device-trace-1"


@pytest.mark.asyncio
async def test_batch_diagnostics_collect_runtime_processing_dimensions():
    class FakeSession:
        def __init__(self, shape):
            self._shape = shape

        def get_inputs(self):
            return [SimpleNamespace(shape=self._shape)]

    detector = SimpleNamespace(session=FakeSession([1, 3, 640, 384]))
    reid = SimpleNamespace(session=FakeSession([1, 3, 256, 128]))
    tracking_service = SimpleNamespace(
        _detector=detector,
        _tracker=SimpleNamespace(human_reid=reid),
    )

    with patch(
        "miloco.perception.engine.pipeline.run_gate",
        new=AsyncMock(
            return_value=_skipped_gate_result(
                np.zeros((240, 320), dtype=np.uint8)
            )
        ),
    ):
        result = await run_batch_pipeline(
            BatchedSnapshot(snapshots=[_snapshot("camera-1")]),
            {"camera-1": OmniContext()},
            PerceptionConfig(),
            get_tracking_service=lambda *_args: tracking_service,
            trace_id="trace-dimensions",
            collect_flow_diagnostics=True,
        )

    diagnostic = result.flow_diagnostics.devices["camera-1"]
    assert diagnostic.gate_output_width == 320
    assert diagnostic.gate_output_height == 240
    assert diagnostic.detector_input_width == 384
    assert diagnostic.detector_input_height == 640
    assert diagnostic.reid_input_width == 128
    assert diagnostic.reid_input_height == 256


@pytest.mark.asyncio
async def test_gate_failure_returns_partial_diagnostics_without_aborting_batch():
    batch = BatchedSnapshot(
        snapshots=[_snapshot("camera-bad"), _snapshot("camera-skip")]
    )
    config = PerceptionConfig()

    async def run_gate(snapshot, *_args, **_kwargs):
        if snapshot.device.did == "camera-bad":
            raise RuntimeError("gate failed")
        return _skipped_gate_result()

    with patch("miloco.perception.engine.pipeline.run_gate", side_effect=run_gate):
        result = await run_batch_pipeline(
            batch,
            {
                "camera-bad": OmniContext(),
                "camera-skip": OmniContext(),
            },
            config,
            trace_id="trace-2",
            device_trace_ids={
                "camera-bad": "device-trace-bad",
                "camera-skip": "device-trace-skip",
            },
            collect_flow_diagnostics=True,
        )

    failed = result.flow_diagnostics.devices["camera-bad"]
    skipped = result.flow_diagnostics.devices["camera-skip"]
    assert failed.status == GraphStatus.ERROR
    assert failed.error_stage == "gate"
    assert failed.error_code == "RuntimeError"
    assert skipped.status == GraphStatus.SKIPPED


def test_runtime_flow_diagnostics_are_excluded_from_result_serialization():
    result = RealtimePerceptionResult(
        flow_diagnostics=PerceptionFlowCycleDiagnostics(
            cycle_id="trace-1",
            observed_at=1_000,
        )
    )

    assert result.flow_diagnostics is not None
    assert "flow_diagnostics" not in result.model_dump()
    assert "flow_diagnostics" not in result.model_dump_json()


def test_perf_disabled_processor_does_not_create_flow_store():
    settings = SimpleNamespace(perf=SimpleNamespace(enabled=False))
    proxy = MagicMock()

    with patch("miloco.perception.processor.get_settings", return_value=settings):
        processor = PipelineProcessor(MagicMock(), proxy, MagicMock())

    assert processor.perception_flow_store is None


def test_audio_only_encoding_failure_is_attributed_to_encode_stage():
    from miloco.observability.perception_flow import PerDeviceFlowDiagnostics
    from miloco.perception.flow_context import (
        flow_diagnostics_scope,
        record_audio_only_start,
    )

    diagnostics = PerDeviceFlowDiagnostics(
        device_id="camera-1",
        room_name="Living Room",
        trace_id="trace-1",
        device_trace_id="device-trace-1",
        observed_at=1,
    )
    with flow_diagnostics_scope(diagnostics):
        record_audio_only_start()

    _mark_failed_omni_stage(diagnostics)

    assert diagnostics.audio_only is True
    assert diagnostics.media_transform_status == GraphStatus.SKIPPED
    assert diagnostics.media_encode_status == GraphStatus.ERROR
    assert diagnostics.omni_request_status == GraphStatus.SKIPPED
