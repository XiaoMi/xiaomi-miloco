import asyncio
from threading import Event
from unittest.mock import AsyncMock

import pytest
from miloco.perception.inference_admission import (
    InferenceAdmission,
    InferenceAdmissionClosed,
)


@pytest.mark.parametrize("waiter_stops", ["cancel", "timeout"])
async def test_stopped_waiter_does_not_mark_inference_complete(waiter_stops):
    admission = InferenceAdmission()
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def infer():
        started.set()
        await release.wait()
        completed.set()

    async def wait_for_inference():
        if waiter_stops == "timeout":
            async with asyncio.timeout(None) as timeout:
                deadlines.append(timeout)
                await admission.run(infer)
        else:
            await admission.run(infer)

    deadlines: list[asyncio.Timeout] = []
    waiter = asyncio.create_task(wait_for_inference())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        if waiter_stops == "timeout":
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            expected = TimeoutError
        else:
            waiter.cancel()
            expected = asyncio.CancelledError
        with pytest.raises(expected):
            await waiter

        assert not completed.is_set()
        blocked = AsyncMock()
        async with admission.drain(0) as drained:
            assert drained is False
            with pytest.raises(InferenceAdmissionClosed):
                await admission.run(blocked)
        blocked.assert_not_called()

        release.set()
        async with admission.drain(1) as drained:
            assert drained is True
            assert completed.is_set()
            with pytest.raises(InferenceAdmissionClosed):
                await admission.run(blocked)
        blocked.assert_not_called()
        assert await admission.run(AsyncMock(return_value="ready")) == "ready"
    finally:
        release.set()


async def test_cancelled_writer_reopens_admission_without_cancelling_owner():
    admission = InferenceAdmission()
    started = asyncio.Event()
    release = asyncio.Event()
    waiting = asyncio.Event()

    async def infer():
        started.set()
        await release.wait()
        return "complete"

    async def drain():
        waiting.set()
        async with admission.drain(1):
            pytest.fail("writer should be cancelled while waiting for inference")

    running = asyncio.create_task(admission.run(infer))
    writer = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        writer = asyncio.create_task(drain())
        await asyncio.wait_for(waiting.wait(), timeout=1)
        with pytest.raises(InferenceAdmissionClosed):
            await admission.run(AsyncMock())

        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert not running.done()
        assert await admission.run(AsyncMock(return_value="admitted")) == "admitted"
    finally:
        release.set()
        if writer is not None and not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        assert await running == "complete"


async def test_drain_waits_for_the_blocking_operation_after_waiter_cancels():
    admission = InferenceAdmission()
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = Event()

    def run_blocking():
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release the blocking operation")

    waiter = asyncio.create_task(admission.run(lambda: asyncio.to_thread(run_blocking)))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        async with admission.drain(0) as drained:
            assert drained is False
    finally:
        release.set()

    async with admission.drain(1) as drained:
        assert drained is True


async def test_process_shutdown_keeps_admission_sealed_after_a_deferred_drain():
    admission = InferenceAdmission()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def infer():
        entered.set()
        await release.wait()

    request = asyncio.create_task(admission.run(infer))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        admission.seal()
        async with admission.drain(0) as drained:
            assert drained is False
        with pytest.raises(InferenceAdmissionClosed):
            await admission.run(AsyncMock())
    finally:
        release.set()
        await asyncio.wait_for(request, 1)
