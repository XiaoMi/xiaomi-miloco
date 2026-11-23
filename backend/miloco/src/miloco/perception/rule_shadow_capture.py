"""Record normalized batch evidence for offline rule-window replay."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any

from miloco.perception.rule_window_replay import (
    RecordedObservation,
    RuleWindowEvidence,
    RuleWindowKey,
    RuleWindowPlan,
    SourceOutcome,
    replay_rule_window,
)

RULE_SHADOW_EVENT = "perception_rule_shadow_batch"


def rule_revision(rule: dict) -> str:
    return sha256(json.dumps(rule, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RuleResponseSample:
    received_at_ms: int
    matched_rule_ids: frozenset[str] | None
    gate_skipped: bool = False


@dataclass(slots=True)
class RuleShadowCapture:
    _prepared: dict[str, dict[str, str]] = field(default_factory=dict)
    _responses: dict[str, RuleResponseSample] = field(default_factory=dict)

    def prepare(
        self, device_rule_map: Mapping[str, list[str]], rules: list[dict]
    ) -> None:
        prepared_rule_ids = {
            rule_id for rule_ids in device_rule_map.values() for rule_id in rule_ids
        }
        revisions = {
            rule["id"]: rule_revision(rule)
            for rule in rules
            if rule["id"] in prepared_rule_ids
        }
        self._prepared = {
            source_id: {rule_id: revisions[rule_id] for rule_id in rule_ids}
            for source_id, rule_ids in device_rule_map.items()
            if rule_ids
        }
        self._responses.clear()

    def record(self, source_id: str, sample: RuleResponseSample) -> None:
        if source_id in self._prepared:
            self._responses.setdefault(source_id, sample)

    def record_gate_skip(self, source_id: str, received_at_ms: int) -> None:
        self.record(source_id, RuleResponseSample(received_at_ms, None, gate_skipped=True))

    def observations(
        self,
        *,
        grid_generation: str,
        bounds_by_source: Mapping[str, tuple[int, int]],
        batch_returned_at_ms: int,
    ) -> Iterable[RecordedObservation]:
        """Use raw collection bounds, never the batch's Unix timestamp."""
        for source_id, rule_revisions in self._prepared.items():
            bounds = bounds_by_source.get(source_id)
            if bounds is None:
                continue
            start_ms, end_ms = bounds
            size_ms = end_ms - start_ms
            if size_ms <= 0 or start_ms % size_ms != 0:
                continue
            sample = self._responses.get(source_id)
            hits = sample.matched_rule_ids if sample is not None else None
            received_at_ms = (
                sample.received_at_ms if sample is not None else batch_returned_at_ms
            )
            for rule_id, revision in rule_revisions.items():
                outcome = SourceOutcome.UNOBSERVED
                if sample is not None and sample.gate_skipped:
                    outcome = SourceOutcome.GATE_SKIPPED
                elif hits is not None:
                    outcome = (
                        SourceOutcome.MATCHED
                        if rule_id in hits
                        else SourceOutcome.NO_MATCH
                    )
                yield RecordedObservation(
                    key=RuleWindowKey(
                        rule_id,
                        revision,
                        f"{grid_generation}:{size_ms}",
                        start_ms // size_ms,
                    ),
                    source_id=source_id,
                    outcome=outcome,
                    received_at_ms=received_at_ms,
                )


def shadow_batch_payload(observations: Iterable[RecordedObservation]) -> dict[str, Any]:
    return {
        "version": 1,
        "observations": [asdict(observation) for observation in observations],
    }


def replay_shadow_batches(
    plan: RuleWindowPlan, payloads: Iterable[dict], *, as_of_ms: int
) -> RuleWindowEvidence | None:
    """Replay persisted event payloads against an independently supplied plan.

    A batch only includes sources that were prepared in that batch. Its rows
    cannot determine the full candidate set of a logical window on their own.
    """

    def observations() -> Iterable[RecordedObservation]:
        for payload in payloads:
            if payload["version"] != 1:
                raise ValueError("unsupported rule-shadow payload version")
            for row in payload["observations"]:
                yield RecordedObservation(
                    key=RuleWindowKey(**row["key"]),
                    source_id=row["source_id"],
                    outcome=SourceOutcome(row["outcome"]),
                    received_at_ms=row["received_at_ms"],
                )

    return replay_rule_window(plan, observations(), as_of_ms=as_of_ms)
