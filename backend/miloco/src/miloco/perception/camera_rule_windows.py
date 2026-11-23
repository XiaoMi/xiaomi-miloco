"""Coordinate rule evidence from independently scheduled camera windows."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from miloco.perception.engine.api import rule_selects_device
from miloco.perception.rule_shadow_capture import rule_revision
from miloco.perception.rule_window_replay import (
    MULTI_SOURCE_WAIT_MS,
    RecordedObservation,
    RuleWindowEvidence,
    RuleWindowKey,
    RuleWindowPlan,
    SourceOutcome,
    replay_rule_window,
)

if TYPE_CHECKING:
    from miloco.perception.types import MatchedRule

CameraBounds = tuple[int, int]


@dataclass(frozen=True, slots=True)
class CameraRuleContext:
    event_id: str | None
    caption: str
    match: MatchedRule | None
    connection_current: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class CameraRuleDecision:
    plan: RuleWindowPlan
    rule: dict
    window_start_ms: int
    evidence: RuleWindowEvidence
    contexts: Mapping[str, CameraRuleContext]


@dataclass(slots=True)
class _PendingRule:
    plan: RuleWindowPlan
    observations: dict[str, RecordedObservation] = field(default_factory=dict)
    contexts: dict[str, CameraRuleContext] = field(default_factory=dict)


@dataclass(slots=True)
class _Window:
    rules: list[dict]
    pending: dict[str, _PendingRule]
    started_sources: set[str] = field(default_factory=set)


class CameraRuleWindows:
    """Freeze rules and sources once per window and emit each decision once."""

    def __init__(self, wake: Callable[[], None]) -> None:
        self.grid_generation = uuid.uuid4().hex
        self._wall_offset_ms = (time.time_ns() - time.monotonic_ns()) // 1_000_000
        self._wake = wake
        self._timer: asyncio.TimerHandle | None = None
        self._windows: dict[CameraBounds, _Window] = {}
        self._closed_through: dict[str, int] = {}

    async def rules_for(
        self,
        bounds: CameraBounds,
        did: str,
        sources: frozenset[str],
        load_rules: Callable[[], Awaitable[tuple[list[dict], list[str]]]],
    ) -> list[dict]:
        start, end = bounds
        size = end - start
        if size <= 0 or start % size != 0:
            return []
        window = self._windows.get(bounds)
        if window is None:
            rules, _ = await load_rules()
            window = self._windows.get(bounds)
        if window is None:
            pending: dict[str, _PendingRule] = {}
            applicable_rules = []
            for rule in rules:
                rule_id = rule["id"]
                if end <= self._closed_through.get(rule_id, -1):
                    continue
                selectors = rule.get("condition", {}).get("perceive_device_ids") or []
                candidates = {source for source in sources if rule_selects_device(selectors, source)}
                unmatched = {
                    selector for selector in selectors
                    if not any(rule_selects_device([selector], source) for source in candidates)
                }
                expected = frozenset(candidates | unmatched)
                if not candidates:
                    continue
                plan = RuleWindowPlan(
                    RuleWindowKey(rule_id, rule_revision(rule), f"{self.grid_generation}:{size}", start // size),
                    end,
                    expected,
                    structural_single_source=len(selectors) == len(expected) == 1,
                )
                pending[rule_id] = _PendingRule(plan)
                applicable_rules.append(rule)
            window = self._windows[bounds] = _Window(applicable_rules, pending)
        window.started_sources.add(did)
        self._reschedule(time.monotonic_ns() // 1_000_000)
        return [
            rule for rule in window.rules
            if rule["id"] in window.pending and did in window.pending[rule["id"]].plan.candidate_sources
        ]

    def observe(
        self,
        bounds: CameraBounds,
        observations: Iterable[RecordedObservation],
        contexts: Mapping[str, CameraRuleContext],
    ) -> None:
        window = self._windows.get(bounds)
        if window is None:
            return
        for observation in observations:
            rule_id = observation.key.rule_id
            pending = window.pending.get(rule_id)
            if pending is None or pending.plan.key != observation.key:
                continue
            if observation.source_id in pending.observations:
                continue
            pending.observations[observation.source_id] = observation
            if context := contexts.get(rule_id):
                pending.contexts[observation.source_id] = context
        self._wake()

    def finish(self, bounds: CameraBounds, did: str) -> None:
        window = self._windows.get(bounds)
        if window is None:
            return
        now_ms = time.monotonic_ns() // 1_000_000
        for pending in window.pending.values():
            if did in pending.plan.candidate_sources:
                pending.observations.setdefault(
                    did, RecordedObservation(pending.plan.key, did, SourceOutcome.UNOBSERVED, now_ms)
                )
        self._wake()

    def take_decisions(self) -> list[CameraRuleDecision]:
        now_ms = time.monotonic_ns() // 1_000_000
        decisions: list[CameraRuleDecision] = []
        waiting_tasks: set[str] = set()
        for bounds, window in sorted(self._windows.items()):
            _, end = bounds
            by_task: dict[str, list[dict]] = {}
            for rule in window.rules:
                if rule["id"] in window.pending:
                    by_task.setdefault(rule["task_id"], []).append(rule)
            for task_id, rules in by_task.items():
                if task_id in waiting_tasks:
                    continue
                evidence_by_rule = {
                    rule["id"]: self._evidence(window, window.pending[rule["id"]], now_ms)
                    for rule in rules
                }
                if any(evidence is None for evidence in evidence_by_rule.values()):
                    waiting_tasks.add(task_id)
                    continue
                guards_observed = all(
                    evidence is not None and evidence.matched is not None
                    for rule in rules
                    if rule.get("direction") == "guard"
                    for evidence in (evidence_by_rule[rule["id"]],)
                )
                for rule in sorted(rules, key=lambda item: (item.get("direction") != "guard", item["id"])):
                    rule_id = rule["id"]
                    pending = window.pending.pop(rule_id)
                    if end <= self._closed_through.get(rule_id, -1):
                        continue
                    self._closed_through[rule_id] = end
                    evidence = evidence_by_rule[rule_id]
                    if guards_observed and evidence is not None:
                        decisions.append(CameraRuleDecision(
                            pending.plan, rule, bounds[0], evidence, pending.contexts
                        ))
            if not window.pending and now_ms >= end + MULTI_SOURCE_WAIT_MS:
                del self._windows[bounds]
        self._reschedule(now_ms)
        return decisions

    @staticmethod
    def _evidence(window: _Window, pending: _PendingRule, now_ms: int) -> RuleWindowEvidence | None:
        plan = pending.plan
        observations = []
        for did, observation in pending.observations.items():
            context = pending.contexts.get(did)
            if context is not None and not context.connection_current():
                observation = RecordedObservation(
                    plan.key, did, SourceOutcome.UNOBSERVED, observation.received_at_ms,
                )
            observations.append(observation)
        if plan.structural_single_source and now_ms >= plan.window_end_ms + MULTI_SOURCE_WAIT_MS:
            did = next(iter(plan.candidate_sources))
            if did not in window.started_sources:
                observations.append(RecordedObservation(plan.key, did, SourceOutcome.UNOBSERVED, now_ms))
        return replay_rule_window(plan, observations, as_of_ms=now_ms)

    def _reschedule(self, now_ms: int) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        deadlines = [
            deadline
            for _, end in self._windows
            for deadline in (end, end + MULTI_SOURCE_WAIT_MS)
            if deadline > now_ms
        ]
        if deadlines:
            self._timer = asyncio.get_running_loop().call_later((min(deadlines) - now_ms) / 1000, self._wake)

    def discard(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._windows.clear()

    def unix_ms(self, monotonic_ms: int) -> int:
        return monotonic_ms + self._wall_offset_ms
