import asyncio
from threading import Event
from unittest.mock import AsyncMock, Mock

import numpy as np
from miloco.perception.collect.camera_adapter import (
    CameraConnection,
    CameraDeviceAdapter,
)
from miloco.perception.collect.stream_buffer import MultiTrackSyncBuffer
from miloco.perception.inference_admission import InferenceAdmission
from miloco.perception.inference_worker import InferenceWorker
from miloco.perception.per_camera_scheduler import PerCameraScheduler
from miloco.perception.schema import DecodedVideoFrame, DeviceData
from miloco.perception.types import PerceptionDevice


class _ReadyCameras:
    def __init__(self):
        self.connections = {}
        self.windows = {}

    def put(self, did, start_ms, *, generation=1):
        self.connections[did] = CameraConnection(did, generation)
        self.windows[did] = DeviceData(
            meta=PerceptionDevice(
                did=did, name=did, device_type="camera", room_name=did
            ),
            window_start_ms=start_ms,
            window_end_ms=start_ms + 3000,
        )

    def ready_connections(self):
        return [self.connections[did] for did in self.windows]

    def is_current_connection(self, connection):
        return self.connections.get(connection.did) == connection

    def take_ready_window(self, connection):
        if self.is_current_connection(connection):
            return self.windows.pop(connection.did, None)
        return None


async def test_blocked_camera_does_not_delay_new_windows_on_another_camera():
    cameras = _ReadyCameras()
    admission = InferenceAdmission()
    outcomes = asyncio.Queue()
    started = []
    release = asyncio.Event()

    async def infer(window, _can_publish, _connection_current):
        key = (window.meta.did, window.window_start_ms)
        started.append(key)
        if key == ("slow", 0):
            await release.wait()
        return key

    scheduler = PerCameraScheduler(
        cameras, admission, infer, outcomes.put_nowait, capacity=2
    )
    cameras.put("slow", 0)
    cameras.put("fast", 0)
    scheduler.schedule_ready()
    try:
        first_fast = await asyncio.wait_for(outcomes.get(), 1)
        assert first_fast.value == ("fast", 0)

        cameras.put("slow", 3000)
        scheduler.schedule_ready()
        cameras.put("slow", 6000)
        cameras.put("fast", 3000)
        scheduler.schedule_ready()

        next_fast = await asyncio.wait_for(outcomes.get(), 1)
        assert next_fast.value == ("fast", 3000)
        assert started == [("slow", 0), ("fast", 0), ("fast", 3000)]
        assert cameras.windows["slow"].window_start_ms == 6000
    finally:
        release.set()

    old_slow = await asyncio.wait_for(outcomes.get(), 1)
    new_slow = await asyncio.wait_for(outcomes.get(), 1)
    assert old_slow.value == ("slow", 0)
    assert new_slow.value == ("slow", 6000)
    assert started[-2:] == [("fast", 3000), ("slow", 6000)]


async def test_timeout_suppresses_late_value_and_preserves_capacity_until_completion():
    cameras = _ReadyCameras()
    admission = InferenceAdmission()
    outcomes = asyncio.Queue()
    started = []
    release = asyncio.Event()

    async def infer(window, _can_publish, _connection_current):
        did = window.meta.did
        started.append(did)
        if did == "slow":
            await release.wait()
        return did

    scheduler = PerCameraScheduler(
        cameras, admission, infer, outcomes.put_nowait, capacity=1, timeout_s=0.1
    )
    cameras.put("slow", 0)
    cameras.put("next", 0)
    scheduler.schedule_ready()
    try:
        expired = await asyncio.wait_for(outcomes.get(), 1)
        assert expired.connection.did == "slow"
        assert expired.status == "timed_out"
        assert expired.value is None
        assert started == ["slow"]
        assert "next" in cameras.windows
        async with admission.drain(0) as drained:
            assert drained is False
    finally:
        release.set()

    resumed = await asyncio.wait_for(outcomes.get(), 1)
    assert resumed.status == "completed"
    assert resumed.value == "next"
    assert started == ["slow", "next"]
    assert outcomes.empty()


async def test_new_connection_waits_for_old_inference_and_drops_its_result():
    cameras = _ReadyCameras()
    admission = InferenceAdmission()
    outcomes = asyncio.Queue()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def infer(window, _can_publish, _connection_current):
        if window.window_start_ms == 0:
            entered.set()
            await release.wait()
        return window.window_start_ms

    scheduler = PerCameraScheduler(
        cameras, admission, infer, outcomes.put_nowait, capacity=2
    )
    cameras.put("camera", 0)
    scheduler.schedule_ready()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        cameras.put("camera", 3000, generation=2)
        scheduler.schedule_ready()
        assert cameras.windows["camera"].window_start_ms == 3000
    finally:
        release.set()

    obsolete = await asyncio.wait_for(outcomes.get(), 1)
    current = await asyncio.wait_for(outcomes.get(), 1)
    assert obsolete.connection.generation == 1
    assert obsolete.status == "stale_connection"
    assert obsolete.value is None
    assert current.connection.generation == 2
    assert current.status == "completed"
    assert current.value == 3000


async def test_ready_camera_advances_through_worker_while_another_native_call_is_busy():
    proxy = Mock(
        get_cached_camera=Mock(return_value=None),
        start_camera_decode_video_stream=AsyncMock(return_value=1),
        start_camera_decode_audio_stream=AsyncMock(return_value=2),
    )
    adapter = CameraDeviceAdapter(proxy)
    frame = DecodedVideoFrame(frame=np.zeros((2, 2, 3), dtype=np.uint8), stream_ts=0)
    for did in ("slow", "fast"):
        source = PerceptionDevice(
            did=did, name=did, device_type="camera", room_name=did
        )
        await adapter.connect_device(did, source=source)
        buffer = MultiTrackSyncBuffer(["decoded_video"], window_ms=1000)
        adapter._devices[did].sync_buffer = buffer
        for stamp in (1000, 2000, 3000):
            buffer.put("decoded_video", frame, stamp, stamp)

    worker = InferenceWorker(thread_name="test-camera-scheduler")
    admission = InferenceAdmission()
    outcomes = asyncio.Queue()
    loop = asyncio.get_running_loop()
    native_entered = asyncio.Event()
    release_native = Event()

    def block_in_native_call():
        loop.call_soon_threadsafe(native_entered.set)
        if not release_native.wait(timeout=10):
            raise TimeoutError("test did not release the native call")

    async def infer(window, _can_publish, _connection_current):
        async def on_worker():
            if window.meta.did == "slow":
                await asyncio.to_thread(block_in_native_call)
            return window.meta.did, window.window_start_ms

        return await worker.submit(on_worker())

    scheduler = PerCameraScheduler(
        adapter, admission, infer, outcomes.put_nowait, capacity=2
    )
    worker.start()
    try:
        scheduler.schedule_ready()
        await asyncio.wait_for(native_entered.wait(), 2)
        assert (await asyncio.wait_for(outcomes.get(), 2)).value == ("fast", 2000)

        adapter._devices["fast"].sync_buffer.put("decoded_video", frame, 4000, 4000)
        scheduler.schedule_ready()
        assert (await asyncio.wait_for(outcomes.get(), 2)).value == ("fast", 3000)
        async with admission.drain(0) as drained:
            assert drained is False
    finally:
        release_native.set()
        try:
            async with admission.drain(3) as drained:
                assert drained is True
        finally:
            worker.shutdown(wait=True)

    assert (await asyncio.wait_for(outcomes.get(), 2)).value == ("slow", 2000)
