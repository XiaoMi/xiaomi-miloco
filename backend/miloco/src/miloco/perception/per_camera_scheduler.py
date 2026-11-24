"""Run each camera's latest ready window independently."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from miloco.perception.engine.api import rule_selects_device
from miloco.utils.common import safe_log

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from miloco.perception.inference_admission import InferenceAdmission
    from miloco.perception.schema import (
        CameraConnection,
        CameraExitWait,
        CameraExitWaitFactory,
        CameraWindowSource,
        DeviceData,
    )

logger = logging.getLogger(__name__)
RealtimeRules = tuple[list[dict], list[str]]
_OR_EXIT_WAIT_S = 3.0


@dataclass
class _CameraJob:
    connection: CameraConnection
    rule_ids: frozenset[str]
    settled: asyncio.Future[None]
    started: bool = False

    def settle(self) -> None:
        if not self.settled.done():
            self.settled.set_result(None)


class PerCameraScheduler:
    def __init__(
        self,
        adapter: CameraWindowSource,
        admission: InferenceAdmission,
        infer: Callable[[DeviceData, Callable[[], bool], RealtimeRules, CameraExitWaitFactory], Awaitable[None]],
        wake: Callable[[], None],
        *,
        capacity: int,
        timeout_s: float,
    ) -> None:
        self._adapter = adapter
        self._admission = admission
        self._infer = infer
        self._wake = wake
        self._capacity = capacity
        self._timeout_s = timeout_s
        self._ready: dict[str, CameraConnection] = {}
        self._running: dict[str, _CameraJob] = {}
        self._windows_through: dict[str, int] = {}
        self._epoch = 0

    def retire_results(self) -> None:
        self._epoch += 1
        self._ready.clear()
        for job in self._running.values():
            job.settle()

    def schedule_ready(self, rules: RealtimeRules) -> None:
        if not self._admission.accepting:
            return
        for job in self._running.values():
            if not self._adapter.is_current_connection(job.connection):
                job.settle()
        ready = {source.did: source for source in self._adapter.ready_connections()}
        self._ready = {did: ready.pop(did) for did in self._ready if did in ready}
        self._ready.update(ready)
        for did, connection in tuple(self._ready.items()):
            if len(self._running) >= self._capacity:
                break
            if did in self._running:
                continue
            del self._ready[did]
            window = self._adapter.take_ready_window(connection)
            if window is None or window.window_end_ms <= self._windows_through.get(did, -1):
                continue
            self._windows_through[did] = window.window_end_ms
            self._start_window(connection, window, rules)

    def _start_window(
        self, connection: CameraConnection, window: DeviceData, rules: RealtimeRules,
    ) -> None:
        epoch = self._epoch
        deadline = time.monotonic() + self._timeout_s
        loop = asyncio.get_running_loop()
        job = _CameraJob(connection, frozenset(
            rule["id"] for rule in rules[0]
            if rule_selects_device(rule["condition"]["perceive_device_ids"], connection.did)
        ), loop.create_future())

        def can_publish() -> bool:
            return (
                epoch == self._epoch and time.monotonic() < deadline
                and self._adapter.is_current_connection(connection)
            )

        def timed_out() -> None:
            job.settle()
            if epoch == self._epoch:
                logger.warning("Camera window timed out | did=%s", safe_log(connection.did))

        def finish(_: asyncio.Task[None]) -> None:
            deadline_report.cancel()
            job.settle()
            del self._running[connection.did]
            self._wake()

        def wait_for_peers(rule_id: str) -> CameraExitWait | None:
            peers = [
                peer.settled for did, peer in self._running.items()
                if did != connection.did and peer.started and rule_id in peer.rule_ids
                and not peer.settled.done() and self._adapter.is_current_connection(peer.connection)
            ]
            if not peers:
                return None
            wait_until = loop.time() + _OR_EXIT_WAIT_S

            async def wait() -> bool:
                await asyncio.wait(peers, timeout=max(0, wait_until - loop.time()))
                return epoch == self._epoch

            return wait

        async def infer() -> None:
            job.started = True
            await self._infer(window, can_publish, rules, wait_for_peers)

        self._running[connection.did] = job
        deadline_report = loop.call_later(self._timeout_s, timed_out)
        # The slot belongs to the operation until the worker returns, even after the deadline.
        task = self._admission.start(infer)
        task.add_done_callback(finish)
