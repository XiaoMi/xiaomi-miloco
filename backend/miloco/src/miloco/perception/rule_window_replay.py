"""Evaluate a recorded rule window without touching live rule state or actions."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

MULTI_SOURCE_WAIT_MS = 3_000


class SourceOutcome(StrEnum):
    MATCHED = "matched"
    NO_MATCH = "no_match"
    UNOBSERVED = "unobserved"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True, slots=True)
class RuleWindowKey:
    rule_id: str
    rule_revision: str
    window_grid_id: str
    window_sequence: int


@dataclass(frozen=True, slots=True)
class RuleWindowPlan:
    key: RuleWindowKey
    window_end_ms: int
    candidate_sources: frozenset[str]
    structural_single_source: bool = False

    def __post_init__(self) -> None:
        if self.structural_single_source and len(self.candidate_sources) != 1:
            raise ValueError(
                "a structural single-source rule requires exactly one candidate"
            )


@dataclass(frozen=True, slots=True)
class RecordedObservation:
    key: RuleWindowKey
    source_id: str
    outcome: SourceOutcome
    received_at_ms: int


@dataclass(frozen=True, slots=True)
class RuleWindowEvidence:
    observed_by_source: tuple[tuple[str, bool], ...]
    unobserved_sources: frozenset[str]
    out_of_scope_sources: frozenset[str]

    @property
    def matched(self) -> bool | None:
        """Raw window evidence only; not a rule transition or an action decision."""
        if self.unobserved_sources or not self.observed_by_source:
            return None
        return any(matched for _, matched in self.observed_by_source)


def replay_rule_window(
    plan: RuleWindowPlan,
    observations: Iterable[RecordedObservation],
    *,
    as_of_ms: int,
) -> RuleWindowEvidence | None:
    """Return closed evidence, or None while still waiting.

    Use the same monotonic clock for the window and receiver timestamps. Pass
    only observations from the relevant connection and scheduler generations.
    A structural single source relies on a terminal report from its device,
    including UNOBSERVED on timeout, rather than the multi-source deadline.
    The first terminal report per source wins; records with equal timestamps
    retain input order. OUT_OF_SCOPE requires exclusion by the dispatch plan;
    a failed or unavailable candidate is UNOBSERVED, not out of scope.
    """
    if as_of_ms < plan.window_end_ms:
        return None

    deadline_ms = None
    if not plan.structural_single_source:
        deadline_ms = plan.window_end_ms + MULTI_SOURCE_WAIT_MS
    terminal_by_source: dict[str, SourceOutcome] = {}
    for observation in sorted(
        observations, key=lambda observation: observation.received_at_ms
    ):
        if observation.received_at_ms > as_of_ms:
            break
        if deadline_ms is not None and observation.received_at_ms >= deadline_ms:
            break
        if (
            observation.key == plan.key
            and observation.source_id in plan.candidate_sources
            and observation.source_id not in terminal_by_source
        ):
            terminal_by_source[observation.source_id] = observation.outcome

    pending = plan.candidate_sources.difference(terminal_by_source)
    if pending and (deadline_ms is None or as_of_ms < deadline_ms):
        return None

    unobserved_sources = pending.union(
        source_id
        for source_id, outcome in terminal_by_source.items()
        if outcome is SourceOutcome.UNOBSERVED
    )
    return RuleWindowEvidence(
        observed_by_source=tuple(
            (source_id, outcome is SourceOutcome.MATCHED)
            for source_id, outcome in sorted(terminal_by_source.items())
            if outcome in (SourceOutcome.MATCHED, SourceOutcome.NO_MATCH)
        ),
        unobserved_sources=unobserved_sources,
        out_of_scope_sources=frozenset(
            source_id
            for source_id, outcome in terminal_by_source.items()
            if outcome is SourceOutcome.OUT_OF_SCOPE
        ),
    )
