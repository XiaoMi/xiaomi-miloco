"""Retain inference owners while callers cancel or wait for teardown."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, TypeVar

OperationResult = TypeVar("OperationResult")


class InferenceAdmissionClosed(RuntimeError):
    """No new work may start while a drain is held."""


class InferenceDrainDeferred(RuntimeError):
    """The writer could not acquire exclusive access before the deadline."""


class InferenceAdmission:
    """Track operations on their owning loop, separately from their waiters.

    An operation must only return once it no longer uses engine resources.
    A caller that owns teardown can hold a drain; it must not stop the worker
    or close the engine if that drain reports outstanding operations.
    """

    def __init__(self) -> None:
        self._operations: set[asyncio.Task[Any]] = set()
        self._drain_ready: asyncio.Event | None = None
        self._drain_allows_sealed = False
        self._sealed = False

    @property
    def accepting(self) -> bool:
        return self._drain_ready is None and not self._sealed

    @property
    def idle(self) -> bool:
        return not self._operations

    @property
    def sealed(self) -> bool:
        return self._sealed

    def seal(self) -> None:
        """Keep admission closed for the rest of process shutdown."""
        self._sealed = True
        if self._drain_ready is not None and not self._drain_allows_sealed:
            self._drain_ready.set()

    def start(
        self, operation: Callable[[], Coroutine[Any, Any, OperationResult]]
    ) -> asyncio.Task[OperationResult]:
        """Start an owned operation. Observers may inspect or shield its task."""
        if not self.accepting:
            raise InferenceAdmissionClosed(
                "perception inference is unavailable during teardown"
            )
        owner = asyncio.create_task(operation())
        self._operations.add(owner)
        owner.add_done_callback(self._retire)
        return owner

    async def run(
        self, operation: Callable[[], Coroutine[Any, Any, OperationResult]]
    ) -> OperationResult:
        return await asyncio.shield(self.start(operation))

    def _retire(self, owner: asyncio.Task[Any]) -> None:
        self._operations.discard(owner)
        if not owner.cancelled():
            owner.exception()
        if not self._operations and self._drain_ready is not None:
            self._drain_ready.set()

    @asynccontextmanager
    async def drain(
        self, timeout_s: float | None, *, allow_sealed: bool = False,
    ) -> AsyncIterator[bool]:
        """Close admission through the writer's body; True permits teardown."""
        if self._sealed and not allow_sealed:
            raise InferenceAdmissionClosed("perception is shutting down")
        if self._drain_ready is not None:
            raise InferenceAdmissionClosed("a drain is already in progress")

        ready = self._drain_ready = asyncio.Event()
        self._drain_allows_sealed = allow_sealed
        try:
            if self._operations:
                try:
                    async with asyncio.timeout(timeout_s):
                        await ready.wait()
                except TimeoutError:
                    pass
            if self._sealed and not allow_sealed:
                raise InferenceAdmissionClosed("perception is shutting down")
            yield not self._operations
        finally:
            self._drain_ready = None
