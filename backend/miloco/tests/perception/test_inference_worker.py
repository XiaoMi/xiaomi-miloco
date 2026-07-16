"""Tests for InferenceWorker — persistent event loop lifecycle and generation isolation."""

from __future__ import annotations

import asyncio

import pytest
from miloco.perception.inference_worker import InferenceWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _return(val):
    return val


async def _raise(exc):
    raise exc


async def _probe_main_loop(main_loop: asyncio.AbstractEventLoop):
    """Assert we are running on a *different* loop than the main one."""
    current = asyncio.get_running_loop()
    assert current is not main_loop, "expected probe to run on worker loop"
    assert current is not None
    return current


# ---------------------------------------------------------------------------
# submit — basic correctness
# ---------------------------------------------------------------------------


async def test_submit_runs_on_worker_loop_and_returns():
    """submit() routes the coroutine to the worker thread's loop, not main."""
    w = InferenceWorker()
    w.start()
    main_loop = asyncio.get_running_loop()

    result = await w.submit(_return(42))
    assert result == 42

    worker_loop = await w.submit(_probe_main_loop(main_loop))
    assert worker_loop is w._loop

    w.shutdown(wait=True)


async def test_submit_propagates_exception():
    """submit() re-raises the coroutine's exception unchanged."""
    w = InferenceWorker()
    w.start()
    with pytest.raises(ValueError, match="boom"):
        await w.submit(_raise(ValueError("boom")))
    w.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Lifecycle — start / shutdown / restart
# ---------------------------------------------------------------------------


async def test_is_running_reflects_lifecycle():
    w = InferenceWorker()
    assert not w.is_running
    w.start()
    assert w.is_running
    w.shutdown(wait=True)
    assert not w.is_running


async def test_shutdown_flag_cleared_on_restart():
    """shutdown → start clears the flag so submit() works again."""
    w = InferenceWorker()
    w.start()
    assert w.is_running
    w.shutdown(wait=True)
    assert not w.is_running
    w.start()
    assert w.is_running
    assert await w.submit(_return("alive")) == "alive"
    w.shutdown(wait=True)


async def test_restart_uses_new_generation_loop():
    """A restarted worker gets a brand-new loop object."""
    w = InferenceWorker()
    w.start()
    loop1 = w._loop
    w.shutdown(wait=True)
    assert w._loop is None  # self._loop is loop → cleared

    w.start()
    loop2 = w._loop
    assert loop2 is not None
    assert loop2 is not loop1  # new generation, new loop
    assert await w.submit(_return("ok")) == "ok"
    w.shutdown(wait=True)


async def test_restart_without_joining_old_thread():
    """start() after shutdown(wait=False) creates a usable new generation
    even while the old thread is still draining."""
    w = InferenceWorker()
    w.start()
    loop1 = w._loop

    # Submit a coroutine that will be cancelled during shutdown —
    # the old generation drains it in its finally block.
    async def slow():
        await asyncio.sleep(10)

    # Fire-and-forget on the worker — shutdown won't wait for it.
    asyncio.ensure_future(w.submit(slow()))  # noqa: RUF006

    w.shutdown(wait=False)  # signal stop, don't join

    # Immediately restart — old thread may still be draining slow()
    w.start()
    assert w.is_running
    assert w._loop is not None
    assert w._loop is not loop1  # new object

    # New generation must be usable
    assert await w.submit(_return("fresh")) == "fresh"
    w.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


async def test_submit_after_shutdown_raises():
    w = InferenceWorker()
    w.start()
    w.shutdown(wait=True)
    with pytest.raises(RuntimeError, match="shutting down"):
        await w.submit(_return(1))


async def test_submit_before_start_raises():
    w = InferenceWorker()
    # never started → _loop is None, _shutdown_flag is True (init default)
    with pytest.raises(RuntimeError):
        await w.submit(_return(1))


async def test_coroutine_closed_on_guard_rejection():
    """When submit() raises via a guard, the passed coroutine is .close()d
    so Python does not warn about 'coroutine was never awaited' at GC time."""
    w = InferenceWorker()
    # Start then shut down so _shutdown_flag is True on next submit()
    w.start()
    w.shutdown(wait=True)

    async def never_run():
        pass  # pragma: no cover

    coro = never_run()
    with pytest.raises(RuntimeError, match="shutting down"):
        await w.submit(coro)

    # After close(), cr_frame is None — the coroutine is properly cleaned up
    assert coro.cr_frame is None


# ---------------------------------------------------------------------------
# shutdown wait
# ---------------------------------------------------------------------------


async def test_shutdown_wait_joins_thread():
    w = InferenceWorker()
    w.start()
    thread = w._thread
    assert thread is not None and thread.is_alive()
    w.shutdown(wait=True)
    # After wait=True join, the daemon thread has exited
    assert not thread.is_alive()
