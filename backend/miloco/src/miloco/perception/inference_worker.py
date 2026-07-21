"""
InferenceWorker — persistent worker thread with a durable event loop.

Replaces the ``asyncio.run()`` inside ``run_in_executor`` pattern. Instead of
creating a fresh event loop (and its default ThreadPoolExecutor, 12 threads on
an 8-core machine) on every perception tick, this module owns ONE loop that
lives for the lifetime of the runner. Coroutines from the main event loop are
submitted via ``run_coroutine_threadsafe`` and awaited across the thread
boundary with ``asyncio.wrap_future``.

Eliminates the three root causes of thread leak:
1. ``asyncio.run()`` → new loop → new default executor (12 threads) per tick
2. ``_get_fused_http_client`` loop-mismatch forced httpx client rebuild
3. ``shutdown(wait=False)`` in runner.stop() left old worker threads unjoined
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class InferenceWorker:
    """Persistent event loop, one "generation" (thread + loop) at a time.

    Supports being stopped and started again on the *same* instance —
    ``runner.py`` holds one ``InferenceWorker`` for the whole app lifetime.
    The subtlety this class exists to get right: ``shutdown(wait=False)``
    only *requests* the loop to stop — an in-flight ONNX call on the worker
    thread cannot be preempted (asyncio cancellation is cooperative; a
    running ``concurrent.futures.Future`` can't be force-stopped), so the
    old thread may keep running for as long as that call takes to finish
    naturally. ``start()`` therefore never waits for a previous generation
    to fully drain before standing up the next one.

    Each ``start()`` spawns a thread with its own brand-new ``loop`` object
    (``asyncio.new_event_loop()`` — a fresh, unique object every call) as a
    local variable closed over by that thread's target function, *not*
    shared mutable ``self.`` state. ``self._loop`` always means "the current
    generation"; a retiring generation's own local ``loop`` variable is a
    different object, so its teardown (``run_until_complete`` etc.) runs
    entirely on its own copy without racing whatever the current generation
    is doing. The only coordination needed is in the teardown path, which
    checks ``self._loop is loop`` — "is the object I created still the one
    everyone else is using?" — before clearing the shared field, so a
    still-draining old generation can never clobber a newer one that has
    already taken over. The startup path needs no such check: ``start()``
    blocks until the new thread's loop is fully installed before returning,
    so two generations' startups can never interleave.
    """

    def __init__(self, thread_name: str = "perception-infer"):
        self._thread_name = thread_name
        # Always refer to the CURRENT generation; a retiring generation's
        # thread never writes here once superseded (see ``_run_loop``).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._shutdown_flag = True  # no generation running yet

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch a new generation and block until its loop is ready.

        Safe to call again after ``shutdown()`` — even if the previous
        generation's thread hasn't exited yet (it may still be draining a
        non-preemptible call). This never waits on the old generation.
        """
        self._shutdown_flag = False
        ready = threading.Event()
        thread = threading.Thread(
            target=self._run_loop,
            args=(ready,),
            name=self._thread_name,
            daemon=True,
        )
        self._thread = thread
        thread.start()
        # Block until this generation's loop is set up — subsequent submit()
        # calls can immediately use it. Local `ready`, not `self._ready`, so
        # an overlapping start() (shouldn't happen from the single asyncio
        # caller, but defensively) can't wait on the wrong event.
        ready.wait()

    def _run_loop(self, ready: threading.Event) -> None:
        """Entry point for one generation's worker thread.

        ``loop`` is a local variable for this thread's entire lifetime —
        the finally-block below always operates on *this* generation's own
        loop object, never on whatever ``self._loop`` happens to point to by
        the time this thread finishes (a newer generation may have already
        replaced it with a different loop object).
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        # Defer ready.set() to the first iteration of run_forever() so that
        # "ready" means "the loop is actually running", not just "the object
        # exists". This closes the window between set() and run_forever()
        # where submit() would see is_running()==False and raise.
        loop.call_soon(ready.set)
        try:
            loop.run_forever()
        finally:
            # Cancel all remaining tasks so they don't hold references.
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            # Gather with return_exceptions so cancellation errors don't
            # propagate and prevent the loop from closing cleanly. Tasks
            # blocked on a non-preemptible thread call (e.g. ONNX inference)
            # won't actually be cancelled until that call finishes — this
            # is exactly the "old generation drains on its own" wait, and it
            # happens here, on this thread, off to the side of whatever the
            # current generation is doing.
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            # Only clear the shared field if nobody has superseded us — i.e.
            # this loop object is still the one everyone else sees. If a
            # newer start() already replaced it with a different loop
            # object, leave it alone.
            if self._loop is loop:
                self._loop = None

    async def submit(self, coro: Coroutine[Any, Any, T]) -> T:
        """Submit a coroutine to the current generation's loop and await it.

        ``coro`` must be created on the calling (main) thread. Its closure
        may capture main-thread values (e.g. trace_id, artifacts). The
        coroutine itself runs on the worker thread where the persistent loop
        owns the correct ContextVar defaults and the shared httpx client.

        Returns the coroutine's result. Raises the coroutine's exception
        unchanged (including CancelledError) — the caller is responsible for
        handling errors.
        """
        if self._shutdown_flag:
            coro.close()
            raise RuntimeError("InferenceWorker is shutting down")
        loop = self._loop
        if loop is None:
            coro.close()
            raise RuntimeError("InferenceWorker loop is not running (call start() first)")
        if not loop.is_running():
            coro.close()
            raise RuntimeError("InferenceWorker loop has stopped")

        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return await asyncio.wrap_future(fut)
        except asyncio.CancelledError:
            # If the caller is cancelled, still try to cancel the worker-side
            # future so the worker loop doesn't keep running a dead coroutine.
            fut.cancel()
            raise

    def shutdown(self, wait: bool = False, timeout: float = 5.0) -> None:
        """Stop the current generation's loop and optionally join its thread.

        ``wait=True`` blocks until the thread exits (up to ``timeout``
        seconds) — this can take as long as whatever non-preemptible call is
        currently in flight on it. In the normal production path
        (runner.stop) we use ``wait=False`` to avoid blocking the main event
        loop; the daemon thread cleans itself up on its own once it drains.
        """
        if self._shutdown_flag:
            return
        self._shutdown_flag = True
        loop = self._loop
        thread = self._thread
        if loop is not None and loop.is_running():
            # Schedule loop.stop() from any thread — the next iteration of
            # run_forever() will return.
            loop.call_soon_threadsafe(loop.stop)
        if wait and thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        loop = self._loop
        return not self._shutdown_flag and loop is not None and loop.is_running()
