"""Coordinate rule evidence from independently scheduled camera windows."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from miloco.config.settings import DEFAULT_RULE_WINDOW_WAIT_MS
from miloco.perception.engine.api import rule_selects_device
from miloco.perception.rule_shadow_capture import rule_revision
from miloco.perception.rule_window_replay import (
    RecordedObservation,
    RuleWindowEvidence,
    RuleWindowKey,
    RuleWindowPlan,
    SourceOutcome,
    replay_rule_window,
)
from miloco.rule.schema import RuleDirection, RuleMode

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
class CameraGuardEvidence:
    matching_contexts: tuple[CameraRuleContext, ...]

    def is_current(self) -> bool:
        return any(context.connection_current() for context in self.matching_contexts)


@dataclass(frozen=True, slots=True)
class CameraRuleDecision:
    plan: RuleWindowPlan
    rule: dict
    window_start_ms: int
    evidence: RuleWindowEvidence
    contexts: Mapping[str, CameraRuleContext]
    partial_enter_sources: tuple[str, ...] = ()
    guard_evidence: tuple[CameraGuardEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class _LateEnter:
    evidence: RuleWindowEvidence
    candidate_sources: frozenset[str]
    deadline_ms: int
    guard_evidence: tuple[CameraGuardEvidence, ...]


@dataclass(slots=True)
class _PendingRule:
    plan: RuleWindowPlan
    observations: dict[str, RecordedObservation] = field(default_factory=dict)
    contexts: dict[str, CameraRuleContext] = field(default_factory=dict)
    late_enter: _LateEnter | None = None


@dataclass(slots=True)
class _Window:
    rules: list[dict]
    pending: dict[str, _PendingRule]
    sources: frozenset[str]
    unavailable_guard_tasks: set[str]
    started_sources: dict[str, int] = field(default_factory=dict)
    finished_sources: set[str] = field(default_factory=set)
    processed_sources: dict[str, tuple[int, Callable[[], bool]]] = field(default_factory=dict)


def _allows_partial_enter(rule: dict) -> bool:
    if rule.get("duration_seconds"):
        return False
    direction = rule.get("direction")
    if direction is None:
        # Legacy event and state rules both have an entering edge.
        return rule.get("mode", RuleMode.EVENT) in (RuleMode.EVENT, RuleMode.STATE)
    return direction in (RuleDirection.ENTER, RuleDirection.SESSION)


def _partial_enter_sources(
    evidence: RuleWindowEvidence | None, contexts: Mapping[str, CameraRuleContext],
) -> tuple[str, ...]:
    if evidence is None or not evidence.unobserved_sources:
        return ()
    return tuple(
        did for did, hit in evidence.observed_by_source
        if hit and did in contexts and contexts[did].match is not None
    )


class CameraRuleWindows:
    """Freeze rules and sources once per window and emit each decision once."""

    def __init__(
        self, wake: Callable[[], None], *, wait_ms: int = DEFAULT_RULE_WINDOW_WAIT_MS,
        inference_budget_ms: int = 0,
    ) -> None:
        self.grid_generation = uuid.uuid4().hex
        self._wall_offset_ms = (time.time_ns() - time.monotonic_ns()) // 1_000_000
        self._wake = wake
        self._wait_ms = wait_ms
        self._inference_budget_ms = inference_budget_ms
        self._timer: asyncio.TimerHandle | None = None
        self._windows: dict[CameraBounds, _Window] = {}
        self._closed_through: dict[str, int] = {}
        # A newer unknown window can close without superseding an older usable result.
        self._rule_decided_through: dict[str, int] = {}
        self._task_blocks_enter_through: dict[str, int] = {}

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
        started_at_ms = time.monotonic_ns() // 1_000_000
        window = self._windows.get(bounds)
        if window is None:
            rules, _ = await load_rules()
            # Another camera may have opened this window while rules loaded.
            window = self._windows.get(bounds)
            if window is None:
                pending: dict[str, _PendingRule] = {}
                applicable_rules = []
                unavailable_guard_tasks = set()
                for rule in rules:
                    rule_id = rule["id"]
                    selectors = rule.get("condition", {}).get("perceive_device_ids") or []
                    # A camera that has produced frames still counts if this window is silent.
                    candidates = frozenset(source for source in sources if rule_selects_device(selectors, source))
                    if not candidates or end <= self._closed_through.get(rule_id, -1):
                        if rule.get("direction") == RuleDirection.GUARD:
                            unavailable_guard_tasks.add(rule["task_id"])
                        continue
                    plan = RuleWindowPlan(
                        RuleWindowKey(rule_id, rule_revision(rule), f"{self.grid_generation}:{size}", start // size),
                        end,
                        candidates,
                        structural_single_source=len(selectors) == len(candidates) == 1,
                        wait_ms=self._wait_ms,
                    )
                    pending[rule_id] = _PendingRule(plan)
                    applicable_rules.append(rule)
                window = self._windows[bounds] = _Window(applicable_rules, pending, sources, unavailable_guard_tasks)
        window.started_sources.setdefault(did, started_at_ms)
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

    def report_processed(
        self, bounds: CameraBounds, did: str, connection_current: Callable[[], bool],
    ) -> None:
        window = self._windows.get(bounds)
        if window is None or did not in window.sources:
            return
        received_at_ms = time.monotonic_ns() // 1_000_000
        window.processed_sources.setdefault(did, (received_at_ms, connection_current))
        self._wake()

    def finish(self, bounds: CameraBounds, did: str) -> None:
        window = self._windows.get(bounds)
        if window is None:
            return
        window.finished_sources.add(did)
        now_ms = time.monotonic_ns() // 1_000_000
        for pending in window.pending.values():
            if did in pending.plan.candidate_sources:
                pending.observations.setdefault(
                    did, RecordedObservation(pending.plan.key, did, SourceOutcome.UNOBSERVED, now_ms)
                )
        self._wake()

    def reject_late_enters(self, bounds: CameraBounds, task_id: str) -> None:
        window = self._windows.get(bounds)
        if window is None:
            return
        rejected = [
            rule["id"] for rule in window.rules if rule["task_id"] == task_id
            and (pending := window.pending.get(rule["id"])) is not None and pending.late_enter is not None
        ]
        for rule_id in rejected:
            self._close_rule(window, rule_id, bounds[1])
        if rejected:
            self._wake()

    def take_decisions(self) -> list[CameraRuleDecision]:
        now_ms = time.monotonic_ns() // 1_000_000
        decisions: list[CameraRuleDecision] = []
        waiting_tasks: set[str] = set()
        gate_waits: dict[str, list[tuple[int, _Window, list[dict]]]] = {}
        gate_exit_decisions: dict[str, list[int]] = {}
        superseded: set[int] = set()
        ordered_windows = sorted(self._windows.items())
        for bounds, window in ordered_windows:
            _, end = bounds
            by_task: dict[str, list[dict]] = {}
            for rule in window.rules:
                pending = window.pending.get(rule["id"])
                if pending is not None and pending.late_enter is None:
                    by_task.setdefault(rule["task_id"], []).append(rule)
            for task_id, rules in by_task.items():
                if task_id in waiting_tasks:
                    continue
                rules.sort(key=lambda rule: (rule.get("direction") != RuleDirection.GUARD, rule["id"]))
                results = [self._evidence(window, window.pending[rule["id"]], now_ms) for rule in rules]
                guards = [
                    (window.pending[rule["id"]], evidence)
                    for rule, (evidence, _) in zip(rules, results, strict=True)
                    if rule.get("direction") == RuleDirection.GUARD
                ]
                guard_results = [evidence for _, evidence in guards]
                guards_observed = all(evidence is not None and evidence.matched is not None for evidence in guard_results)
                matching_rules = {
                    rule["id"] for rule, (evidence, _) in zip(rules, results, strict=True)
                    if evidence is not None and evidence.matched is True
                }
                waits = [gate_dependent for evidence, gate_dependent in results if evidence is None]
                partial_entries: dict[str, tuple[str, ...]] = {}
                late_entries: dict[str, _LateEnter] = {}
                if task_id not in window.unavailable_guard_tasks and all(
                    evidence is not None and evidence.matched is True for evidence in guard_results
                ):
                    guard_evidence = tuple(
                        CameraGuardEvidence(tuple(
                            pending.contexts[did] for did, hit in evidence.observed_by_source
                            if hit and did in pending.contexts
                        ))
                        for pending, evidence in guards if evidence is not None
                    )
                    for rule, (evidence, _) in zip(rules, results, strict=True):
                        if not _allows_partial_enter(rule) or evidence is None or not evidence.unobserved_sources:
                            continue
                        rule_id = rule["id"]
                        pending = window.pending[rule_id]
                        if sources := _partial_enter_sources(evidence, pending.contexts):
                            partial_entries[rule_id] = sources
                        elif all(guard.is_current() for guard in guard_evidence):
                            late = self._start_late_enter(pending.plan, window, evidence, guard_evidence, now_ms)
                            if late is not None:
                                late_entries[rule_id] = late
                    matching_rules.update(partial_entries)
                has_match = any(
                    rule["id"] in matching_rules and rule.get("direction") != RuleDirection.GUARD
                    for rule in rules
                )
                if waits:
                    if all(waits) and not has_match:
                        gate_waits.setdefault(task_id, []).append((end, window, rules))
                    else:
                        waiting_tasks.add(task_id)
                    continue
                if has_match and guards_observed:
                    # Only a new hit for the same rule replaces a confirmed gate exit.
                    superseded.update(
                        index for index in gate_exit_decisions.pop(task_id, ())
                        if decisions[index].rule["id"] in matching_rules
                    )
                    for older_end, older_window, older_rules in gate_waits.pop(task_id, ()):
                        for rule in older_rules:
                            self._close_rule(older_window, rule["id"], older_end)
                elif task_id in gate_waits:
                    waiting_tasks.add(task_id)
                    continue

                for rule, (evidence, gate_dependent) in zip(rules, results, strict=True):
                    rule_id = rule["id"]
                    if rule_id in late_entries:
                        window.pending[rule_id].late_enter = late_entries[rule_id]
                        continue
                    pending = self._close_rule(window, rule_id, end)
                    if pending is not None and guards_observed and evidence is not None:
                        if not has_match and gate_dependent:
                            gate_exit_decisions.setdefault(task_id, []).append(len(decisions))
                        decisions.append(CameraRuleDecision(
                            pending.plan, rule, bounds[0], evidence, pending.contexts,
                            partial_enter_sources=partial_entries.get(rule_id, ()),
                        ))
                if has_match or not any(gate_dependent for _, gate_dependent in results):
                    gate_exit_decisions.pop(task_id, None)
        decisions = [decision for index, decision in enumerate(decisions) if index not in superseded]
        for decision in decisions:
            self._record_usable_decision(decision)
        decisions.extend(self._take_late_enters(ordered_windows, now_ms))
        self._windows = {
            bounds: window for bounds, window in self._windows.items()
            if window.pending or now_ms < bounds[1] + self._wait_ms
        }
        self._reschedule(now_ms)
        return decisions

    def _record_usable_decision(self, decision: CameraRuleDecision) -> None:
        matched = decision.evidence.matched
        end = decision.plan.window_end_ms
        if matched is not None or decision.partial_enter_sources:
            rule_id = decision.plan.key.rule_id
            self._rule_decided_through[rule_id] = max(end, self._rule_decided_through.get(rule_id, -1))
        direction = decision.rule.get("direction")
        if (direction == RuleDirection.EXIT and matched is True) or (direction == RuleDirection.GUARD and matched is False):
            # A newer exit or a failed guard makes a previous entering hit stale for the task.
            task_id = decision.rule["task_id"]
            self._task_blocks_enter_through[task_id] = max(end, self._task_blocks_enter_through.get(task_id, -1))

    def _start_late_enter(
        self, plan: RuleWindowPlan, window: _Window, evidence: RuleWindowEvidence,
        guard_evidence: tuple[CameraGuardEvidence, ...], now_ms: int,
    ) -> _LateEnter | None:
        deadline_ms = plan.window_end_ms + self._inference_budget_ms
        if plan.structural_single_source or not plan.deadline_ms <= now_ms < deadline_ms:
            return None
        sources = frozenset(
            did for did in evidence.unobserved_sources
            if window.started_sources.get(did, plan.deadline_ms) < plan.deadline_ms
        )
        return _LateEnter(evidence, sources, deadline_ms, guard_evidence) if sources else None

    def _take_late_enters(
        self, ordered_windows: list[tuple[CameraBounds, _Window]], now_ms: int,
    ) -> list[CameraRuleDecision]:
        decisions = []
        # The newest usable window wins when several results arrive in the same drain.
        for (start, end), window in reversed(ordered_windows):
            for rule in window.rules:
                rule_id = rule["id"]
                pending = window.pending.get(rule_id)
                if pending is None or (late := pending.late_enter) is None:
                    continue
                task_id = rule["task_id"]
                if (
                    now_ms >= late.deadline_ms
                    or end < self._rule_decided_through.get(rule_id, -1)
                    or end < self._task_blocks_enter_through.get(task_id, -1)
                    or not all(guard.is_current() for guard in late.guard_evidence)
                ):
                    self._close_rule(window, rule_id, end)
                    continue
                plan = pending.plan
                hits = tuple(
                    did for did in sorted(late.candidate_sources)
                    if (observation := pending.observations.get(did)) is not None
                    and observation.outcome is SourceOutcome.MATCHED
                    # A sample can be stamped before the cutoff and delivered in the next tick.
                    and observation.received_at_ms <= now_ms
                    and (context := pending.contexts.get(did)) is not None
                    and context.match is not None and context.connection_current()
                )
                if hits:
                    # Closure for dedup is tracked separately from newer usable evidence.
                    self._close_rule(window, rule_id, end)
                    decision = CameraRuleDecision(
                        plan, rule, start, late.evidence, pending.contexts,
                        partial_enter_sources=hits, guard_evidence=late.guard_evidence,
                    )
                    self._record_usable_decision(decision)
                    decisions.append(decision)
                elif not late.candidate_sources.difference(window.finished_sources, pending.observations):
                    self._close_rule(window, rule_id, end)
        return decisions

    def _close_rule(self, window: _Window, rule_id: str, end: int) -> _PendingRule | None:
        pending = window.pending.pop(rule_id)
        if end <= self._closed_through.get(rule_id, -1):
            return None
        self._closed_through[rule_id] = end
        return pending

    def _evidence(
        self, window: _Window, pending: _PendingRule, now_ms: int,
    ) -> tuple[RuleWindowEvidence | None, bool]:
        """Return evidence and whether a gate-only exit depends on a peer."""
        plan = pending.plan
        observations = []
        for did, observation in pending.observations.items():
            context = pending.contexts.get(did)
            if context is not None and not context.connection_current():
                observation = RecordedObservation(
                    plan.key, did, SourceOutcome.UNOBSERVED, observation.received_at_ms,
                )
            observations.append(observation)
        if plan.structural_single_source and now_ms >= plan.deadline_ms:
            did = next(iter(plan.candidate_sources))
            if did not in window.started_sources:
                observations.append(RecordedObservation(plan.key, did, SourceOutcome.UNOBSERVED, now_ms))
        evidence = replay_rule_window(plan, observations, as_of_ms=now_ms)
        if (
            evidence is None or evidence.matched is not None or evidence.unobserved_sources
            or not any(item.outcome is SourceOutcome.GATE_SKIPPED for item in observations)
        ):
            return evidence, False

        peers = window.sources.difference(plan.candidate_sources)
        started_peers = {
            did for did in peers if window.started_sources.get(did, plan.deadline_ms) < plan.deadline_ms
        }
        peer_deadline_ms = plan.deadline_ms + self._inference_budget_ms
        peer_processed = any(
            did in peers and received_at_ms < peer_deadline_ms and is_current()
            and (received_at_ms < plan.deadline_ms or did in started_peers)
            for did, (received_at_ms, is_current) in window.processed_sources.items()
        )
        if peer_processed:
            return replay_rule_window(plan, observations, as_of_ms=now_ms, peer_processed=True), True
        if (
            (now_ms < plan.deadline_ms and peers.difference(window.finished_sources))
            or (now_ms < peer_deadline_ms and started_peers.difference(window.finished_sources))
        ):
            # Only a gate-skipped rule may need the batch-equivalent peer signal.
            return None, True
        return evidence, False

    def _reschedule(self, now_ms: int) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        deadlines = [
            deadline
            for _, end in self._windows
            for deadline in (
                end, end + self._wait_ms, end + self._inference_budget_ms,
                end + self._wait_ms + self._inference_budget_ms,
            )
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
