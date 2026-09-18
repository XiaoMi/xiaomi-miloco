from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.observability.perception_flow import (
    GraphStatus,
    PerceptionFlowCycleDiagnostics,
    PerceptionFlowSnapshotStore,
    PerDeviceFlowDiagnostics,
)
from miloco.perception.processor import PipelineProcessor
from miloco.perception.runner import PerceptionRunner


@pytest.mark.asyncio
async def test_successful_device_sync_retains_only_active_flow_devices():
    runner = PerceptionRunner.__new__(PerceptionRunner)
    runner._collector = MagicMock()
    runner._collector.sync_all_devices = AsyncMock()
    runner._collector.get_all_active_sources.return_value = {"camera-2": object()}
    runner._pipeline = MagicMock()

    await runner._sync_devices()

    runner._pipeline.retain_flow_devices.assert_called_once_with({"camera-2"})


@pytest.mark.asyncio
async def test_tick_removes_final_device_snapshot_without_another_cycle():
    runner = PerceptionRunner.__new__(PerceptionRunner)
    runner._collector = MagicMock()
    runner._collector.get_all_active_sources.return_value = {}
    runner._pipeline = MagicMock()

    await runner._tick()

    runner._pipeline.retain_flow_devices.assert_called_once_with(set())
    runner._pipeline.process_realtime.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_stop_still_clears_runtime_flow_snapshots():
    runner = PerceptionRunner.__new__(PerceptionRunner)
    runner._is_running = False
    runner._pipeline = MagicMock()

    await runner.stop()

    runner._pipeline.clear_flow_snapshots.assert_called_once_with()


def test_processor_clear_is_safe_for_partially_initialized_instance():
    from miloco.perception.processor import PipelineProcessor

    processor = PipelineProcessor.__new__(PipelineProcessor)

    processor.clear_flow_snapshots()
    processor.retain_flow_devices(set())


def test_store_is_process_local_and_clear_removes_all_runtime_state():
    store = PerceptionFlowSnapshotStore()
    store.retain_devices({"camera-1"})

    store.clear()

    snapshot = store.snapshot()
    assert snapshot.per_device == {}
    assert snapshot.active_device_ids == set()


def test_cycle_merge_does_not_readd_device_unregistered_during_processing():
    processor = PipelineProcessor.__new__(PipelineProcessor)
    processor._flow_store = PerceptionFlowSnapshotStore()
    processor._collector = MagicMock()
    processor._collector.get_all_active_sources.return_value = {}
    cycle = PerceptionFlowCycleDiagnostics(
        cycle_id="trace-1",
        observed_at=1_000,
        devices={
            "camera-1": PerDeviceFlowDiagnostics(
                device_id="camera-1",
                room_name="Living Room",
                trace_id="trace-1",
                device_trace_id="device-trace-1",
                observed_at=1_000,
                status=GraphStatus.OK,
            )
        },
    )
    batch = MagicMock()
    batch.devices = {"camera-1": MagicMock()}

    processor._merge_flow_snapshot(cycle, batch)

    assert processor.perception_flow_store.snapshot().per_device == {}
