from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from miloco.iot_observation.models import (
    OBSERVATION_MISSING,
    ObservationAttribute,
    ObservationChange,
    ObservationGap,
    ObservationMissingType,
)
from miloco.iot_observation.policy import ObservationPolicy
from miloco.iot_observation.repository import ObservationRepository

logger = logging.getLogger(__name__)

OBSERVATION_PATTERN = "iot/device/*/prop/*"
_QUEUE_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class _QueuedChange:
    change: Any
    scope: int
    generation: int
    family_id: str
    spec: dict[str, Any]
    loss_version: int


@dataclass(frozen=True, slots=True)
class _QueuedCoverage:
    intervals: list
    generation: int
    loss_version: int
    family_id: str
    scope: int
    continuity: int


class ObservationCollector:
    def __init__(
        self,
        state_store: Any,
        repository: ObservationRepository,
        policy: ObservationPolicy,
        *,
        family_id: str | Callable[[], str],
        scope_getter: Callable[[], int],
        aligned_getter: Callable[[], bool],
        spec_getter: Callable[[str], dict[str, Any] | None],
        baseline_getter: Callable[[], dict[str, Any]] | None = None,
        queue_limit: int = _QUEUE_LIMIT,
        health_token_getter: Callable[[], Any] | None = None,
    ) -> None:
        self.state_store = state_store
        self.repository = repository
        self.policy = policy
        self.family_id = family_id
        self.scope_getter = scope_getter
        self.aligned_getter = aligned_getter
        self.spec_getter = spec_getter
        self.baseline_getter = baseline_getter
        self.health_token_getter = health_token_getter
        self._health_token = None
        self.queue: asyncio.Queue[_QueuedChange] = asyncio.Queue(maxsize=queue_limit)
        self._unsubscribe: Callable[[], None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._running = False
        self._accepting = False
        self._known: dict[tuple[str, str, str], Any] = {}
        self._dropped = 0
        self._generation = 0
        self._loss_version = 0
        self._pending_gap: tuple[str, ObservationGap] | None = None
        self._continuity = 0

    @property
    def accepting(self):
        return self._accepting

    @property
    def continuity(self):
        return self._continuity, self._loss_version

    @property
    def known_identities(self):
        return frozenset(self._known)

    def check_health(self):
        if self.health_token_getter is None:
            return
        token = self.health_token_getter()
        if self._health_token is not None and token != self._health_token:
            self.mark_gap("upstream_or_distribution_loss")
        self._health_token = token

    def submit_coverage(self, intervals):
        if not self._running:
            return
        try:
            self.queue.put_nowait(_QueuedCoverage(
                intervals, self._generation, self._loss_version,
                self._family_id(), self.scope_getter(), self._continuity,
            ))
        except asyncio.QueueFull:
            self.mark_gap("coverage_queue_overflow")

    def _family_id(self) -> str:
        return self.family_id() if callable(self.family_id) else self.family_id

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._accepting = self.aligned_getter()
        self._subscribe()
        self._worker = asyncio.create_task(self._run())

    def _subscribe(self) -> None:
        generation = self._generation
        scope = self.scope_getter()

        def receive(change):
            if generation != self._generation or scope != self.scope_getter():
                return False
            return self.handle_change(change)

        self._unsubscribe = self.state_store.subscribe(OBSERVATION_PATTERN, receive)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._accepting = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._worker is not None:
            await self.queue.join()
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def mark_alignment_started(self) -> None:
        self._accepting = False
        self.mark_gap("alignment")

    def mark_alignment_completed(self) -> None:
        if self._running and self._unsubscribe is None:
            self._subscribe()
        self._accepting = True
        if self.baseline_getter is None:
            return
        for path, value in self.baseline_getter().items():
            self.handle_change(self._baseline_change(path, value))

    def switch_scope(self) -> None:
        self._accepting = False
        self._generation += 1
        self._known = {}
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                self.queue.task_done()

    def handle_change(self, change: Any) -> bool:
        if not self._running or not self._accepting or not self.aligned_getter():
            return False
        self.check_health()
        scope = self.scope_getter()
        spec = self.spec_getter(change.path)
        if not isinstance(spec, dict):
            return False
        return self._enqueue(_QueuedChange(change, scope, self._generation, self._family_id(), deepcopy(spec), self._loss_version))

    def mark_gap(self, reason: str, at_ms: int | None = None) -> None:
        self._loss_version += 1
        self._known = {}
        if self._pending_gap is None:
            self._pending_gap = (self._family_id(), ObservationGap(
                self.scope_getter(), reason, at_ms if at_ms is not None else int(time.time() * 1000),
            ))

    @property
    def dropped_count(self) -> int:
        return self._dropped

    def _enqueue(self, item: _QueuedChange) -> bool:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self._dropped += 1
            self.mark_gap("queue_overflow", item.change.at)
            return False
        return True

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            batch = [item]
            while len(batch) < 200:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await asyncio.to_thread(self._persist_batch, batch)
            except Exception:
                self.mark_gap("write_failed")
                logger.error("iot observation collector write failed", exc_info=True)
            finally:
                for _ in batch:
                    self.queue.task_done()

    def _persist_batch(self, batch) -> None:
        pending_gap = self._pending_gap
        with self.repository.transaction():
            if pending_gap is not None:
                self.repository.record_gap(*pending_gap)
            for item in batch:
                if isinstance(item, _QueuedCoverage):
                    if (item.generation == self._generation and item.loss_version == self._loss_version
                            and item.continuity == self._continuity):
                        for attribute, start, end in item.intervals:
                            self.repository.record_coverage(
                                item.family_id, attribute=attribute, scope=item.scope,
                                start_ms=start, end_ms=end, status="healthy",
                            )
                        self.repository.heartbeat(int(time.time() * 1000))
                else:
                    self._persist(item)
        if self._pending_gap is pending_gap:
            self._pending_gap = None

    def _persist(self, item: _QueuedChange) -> None:
        if item.generation != self._generation or item.loss_version != self._loss_version:
            return
        known = self._known
        change = item.change
        path = getattr(change, "path", None)
        if not isinstance(path, str):
            return
        spec = item.spec
        if not isinstance(spec, dict):
            return
        attribute = self._attribute(path, spec)
        if attribute is None:
            return
        old = self._normalize_missing(getattr(change, "old", OBSERVATION_MISSING))
        new = self._normalize_missing(getattr(change, "new", OBSERVATION_MISSING))
        source = str(getattr(change, "source", "unknown"))
        at_ms = int(getattr(change, "at", 0))
        if at_ms <= 0 or at_ms > int(time.time() * 1000) + 60000:
            self._continuity += 1
            self.repository.record_gap(item.family_id, ObservationGap(item.scope, "invalid_time", int(time.time() * 1000), path))
            known.pop(attribute.identity, None)
            return
        key = attribute.identity
        if isinstance(new, ObservationMissingType):
            self._continuity += 1
            self.repository.record_baseline(
                item.family_id,
                attribute,
                OBSERVATION_MISSING,
                scope=item.scope,
                source="delete",
                at_ms=at_ms,
            )
            known.pop(key, None)
            return
        if not self.policy.accepts_value(spec, new):
            self._continuity += 1
            self.repository.record_gap(
                item.family_id,
                ObservationGap(item.scope, "invalid_value", at_ms, path),
            )
            known.pop(key, None)
            return
        if source != "iot_push" or isinstance(old, ObservationMissingType) or key not in known:
            self._continuity += 1
            self.repository.record_baseline(
                item.family_id,
                attribute,
                new,
                scope=item.scope,
                source=source,
                at_ms=at_ms,
            )
            known[key] = (new, at_ms)
            return
        if not self.policy.accepts_value(spec, old) or not self._same(known[key][0], old) or at_ms < known[key][1]:
            self._continuity += 1
            self.repository.record_gap(
                item.family_id,
                ObservationGap(item.scope, "baseline_mismatch", at_ms, path),
            )
            self.repository.record_baseline(
                item.family_id,
                attribute,
                new,
                scope=item.scope,
                source="recovery",
                at_ms=at_ms,
            )
            known[key] = (new, at_ms)
            return
        if self._same(old, new):
            return
        recorded = self.repository.record_change(
            item.family_id,
            ObservationChange(attribute, old, new, source, at_ms, item.scope),
        )
        if recorded:
            known[key] = (new, at_ms)
        else:
            known.pop(key, None)

    def _attribute(self, path: str, spec: dict[str, Any]) -> ObservationAttribute | None:
        parts = path.split("/")
        if len(parts) != 5 or parts[0:2] != ["iot", "device"] or parts[3] != "prop":
            return None
        return ObservationAttribute.from_spec(
            did=parts[2], path=path, spec_entry=spec, policy=self.policy
        )

    def _baseline_change(self, path: str, value: Any) -> Any:
        class Baseline:
            pass

        change = Baseline()
        change.path = path
        change.old = OBSERVATION_MISSING
        change.new = value
        change.source = "alignment"
        change.at = int(time.time() * 1000)
        return change

    @staticmethod
    def _normalize_missing(value: Any) -> Any:
        if isinstance(value, ObservationMissingType) or type(value).__name__ == "MissingType":
            return OBSERVATION_MISSING
        return value

    @staticmethod
    def _same(left: Any, right: Any) -> bool:
        if type(left) is not type(right):
            return False
        if isinstance(left, tuple):
            return len(left) == len(right) and all(
                ObservationCollector._same(a, b) for a, b in zip(left, right)
            )
        return left == right
