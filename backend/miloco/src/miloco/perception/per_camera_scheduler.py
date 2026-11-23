"""Schedule ready camera windows independently of result publication."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from miloco.perception.collect.camera_adapter import (
        CameraConnection,
        CameraDeviceAdapter,
    )
    from miloco.perception.inference_admission import InferenceAdmission
    from miloco.perception.schema import DeviceData

logger = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")
CameraOutcomeStatus = Literal["completed", "failed", "timed_out", "stale_connection"]


@dataclass(frozen=True)
class CameraWindowOutcome(Generic[ResultT]):
    connection: CameraConnection
    window_start_ms: int
    window_end_ms: int
    status: CameraOutcomeStatus
    value: ResultT | None = None


@dataclass
class _RunningWindow:
    connection: CameraConnection
    window_start_ms: int
    window_end_ms: int
    deadline: float
    epoch: int
    deadline_handle: asyncio.TimerHandle | None = None
    reported: bool = False


class PerCameraScheduler(Generic[ResultT]):
    """Hold one slot per camera until inference finishes.

    The adapter keeps pending media and picks its newest ready window at admission.
    Inference should produce an observation and hold its resources for the whole call.
    The receiver consumes outcomes on this loop and must hand off slow work.
    """

    def __init__(
        self,
        adapter: CameraDeviceAdapter,
        admission: InferenceAdmission,
        infer: Callable[[DeviceData, Callable[[], bool], Callable[[], bool]], Awaitable[ResultT]],
        receive: Callable[[CameraWindowOutcome[ResultT]], None],
        *,
        capacity: int,
        timeout_s: float = 15.0,
    ) -> None:
        if capacity < 1 or timeout_s <= 0:
            raise ValueError("capacity and timeout must be positive")
        self._adapter = adapter
        self._admission = admission
        self._infer = infer
        self._receive = receive
        self._capacity = capacity
        self._timeout_s = timeout_s
        self._ready: dict[str, CameraConnection] = {}
        self._running: dict[str, _RunningWindow] = {}
        self._windows_through: dict[str, int] = {}
        self._epoch = 0

    def retire_results(self) -> None:
        self._epoch += 1
        self._ready.clear()

    def schedule_ready(self) -> None:
        """Sample readiness after the camera buffer signals or a timer fires."""
        if not self._admission.accepting:
            return
        ready = {source.did: source for source in self._adapter.ready_connections()}
        self._ready = {did: ready.pop(did) for did in self._ready if did in ready}
        self._ready.update(ready)
        self._start_ready()

    def _start_ready(self) -> None:
        while self._admission.accepting and len(self._running) < self._capacity:
            connection = next(
                (
                    source
                    for did, source in self._ready.items()
                    if did not in self._running
                ),
                None,
            )
            if connection is None:
                return
            del self._ready[connection.did]
            window = self._adapter.take_ready_window(connection)
            if window is None or window.window_end_ms <= self._windows_through.get(connection.did, -1):
                continue
            self._windows_through[connection.did] = window.window_end_ms
            self._start_window(connection, window)

    def _start_window(self, connection: CameraConnection, window: DeviceData) -> None:
        loop = asyncio.get_running_loop()
        job = _RunningWindow(
            connection=connection,
            window_start_ms=window.window_start_ms,
            window_end_ms=window.window_end_ms,
            deadline=loop.time() + self._timeout_s,
            epoch=self._epoch,
        )
        self._running[connection.did] = job
        job.deadline_handle = loop.call_at(job.deadline, self._report, job, "timed_out")
        task = self._admission.start(lambda: self._run_window(job, window))
        task.add_done_callback(lambda _: self._finish_window(job))

    async def _run_window(self, job: _RunningWindow, window: DeviceData) -> None:
        loop = asyncio.get_running_loop()

        def connection_current() -> bool:
            return job.epoch == self._epoch and self._adapter.is_current_connection(job.connection)

        def can_publish() -> bool:
            return loop.time() < job.deadline and connection_current()

        value = None
        status: CameraOutcomeStatus = "stale_connection"
        if can_publish():
            try:
                value = await self._infer(window, can_publish, connection_current)
                status = "completed"
            except Exception:
                logger.exception(
                    "Camera window inference failed for %s", job.connection.did
                )
                status = "failed"
        if loop.time() >= job.deadline:
            status, value = "timed_out", None
        self._report(job, status, value)

    def _finish_window(self, job: _RunningWindow) -> None:
        del self._running[job.connection.did]
        if job.deadline_handle is not None:
            job.deadline_handle.cancel()
        self._start_ready()

    def _report(
        self,
        job: _RunningWindow,
        status: CameraOutcomeStatus,
        value: ResultT | None = None,
    ) -> None:
        if job.reported or job.epoch != self._epoch:
            return
        job.reported = True
        if not self._adapter.is_current_connection(job.connection):
            status, value = "stale_connection", None
        try:
            self._receive(
                CameraWindowOutcome(
                    connection=job.connection,
                    window_start_ms=job.window_start_ms,
                    window_end_ms=job.window_end_ms,
                    status=status,
                    value=value,
                )
            )
        except Exception:
            logger.exception("Camera window receiver failed for %s", job.connection.did)
