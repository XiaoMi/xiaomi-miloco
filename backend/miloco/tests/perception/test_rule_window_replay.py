from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.perception.camera_rule_windows import CameraRuleContext, CameraRuleWindows
from miloco.perception.rule_shadow_capture import rule_revision
from miloco.perception.rule_window_replay import (
    RecordedObservation,
    RuleWindowKey,
    RuleWindowPlan,
    SourceOutcome,
    replay_rule_window,
)

WINDOW_KEY = RuleWindowKey("rule", "revision-1", "grid", 9)
PLAN = RuleWindowPlan(WINDOW_KEY, 30_000, frozenset({"camera-a", "camera-b"}))


def _record(
    source_id: str, outcome: SourceOutcome, received_at_ms: int
) -> RecordedObservation:
    return RecordedObservation(WINDOW_KEY, source_id, outcome, received_at_ms)


@pytest.mark.parametrize("matching", [True, False])
def test_complete_window_closes_when_last_source_arrives(matching):
    newer = SourceOutcome.MATCHED if matching else SourceOutcome.NO_MATCH
    observations = [
        _record("camera-b", newer, 32_999),
        _record("camera-a", SourceOutcome.NO_MATCH, 31_400),
    ]

    assert replay_rule_window(PLAN, observations, as_of_ms=32_998) is None
    evidence = replay_rule_window(PLAN, observations, as_of_ms=32_999)
    assert evidence is not None
    assert evidence.observed_by_source == (("camera-a", False), ("camera-b", matching))
    assert evidence.unobserved_sources == frozenset()
    assert evidence.matched is matching


@pytest.mark.parametrize("late_arrival", [33_000, 33_800])
def test_deadline_closes_on_missing_source_and_ignores_late_match(late_arrival):
    observations = [
        _record("camera-a", SourceOutcome.NO_MATCH, 32_500),
        _record("camera-b", SourceOutcome.MATCHED, late_arrival),
    ]

    assert replay_rule_window(PLAN, observations, as_of_ms=32_999) is None
    at_deadline = replay_rule_window(PLAN, observations, as_of_ms=33_000)
    assert at_deadline is not None
    assert at_deadline.observed_by_source == (("camera-a", False),)
    assert isinstance(at_deadline.unobserved_sources, frozenset)
    assert at_deadline.unobserved_sources == {"camera-b"}
    assert at_deadline.matched is None
    assert replay_rule_window(PLAN, observations, as_of_ms=34_000) == at_deadline


def test_complete_single_source_can_report_after_multi_source_deadline():
    single_source = replace(
        PLAN,
        candidate_sources=frozenset({"camera-a"}),
        structural_single_source=True,
    )
    observations = [_record("camera-a", SourceOutcome.MATCHED, 37_000)]

    assert replay_rule_window(single_source, observations, as_of_ms=36_999) is None
    evidence = replay_rule_window(single_source, observations, as_of_ms=37_000)
    assert evidence is not None
    assert evidence.matched is True


def test_structural_single_source_requires_a_single_candidate():
    with pytest.raises(ValueError, match="exactly one candidate"):
        replace(PLAN, structural_single_source=True)


def test_single_candidate_is_not_automatically_a_structural_single_source():
    single_candidate = replace(PLAN, candidate_sources=frozenset({"camera-a"}))
    evidence = replay_rule_window(single_candidate, [], as_of_ms=33_000)
    assert evidence is not None
    assert evidence.unobserved_sources == {"camera-a"}
    assert evidence.matched is None


def test_single_source_device_timeout_ends_window_without_a_negative_result():
    single_source = replace(
        PLAN,
        candidate_sources=frozenset({"camera-a"}),
        structural_single_source=True,
    )
    observations = [_record("camera-a", SourceOutcome.UNOBSERVED, 45_000)]

    assert replay_rule_window(single_source, observations, as_of_ms=44_999) is None
    evidence = replay_rule_window(single_source, observations, as_of_ms=45_000)
    assert evidence is not None
    assert evidence.unobserved_sources == {"camera-a"}
    assert evidence.matched is None


@pytest.mark.parametrize(
    "outcome", [SourceOutcome.OUT_OF_SCOPE, SourceOutcome.UNOBSERVED]
)
def test_distinguishes_exclusion_from_missing_evidence(outcome):
    observations = [
        _record("camera-a", SourceOutcome.MATCHED, 31_000),
        _record("camera-b", outcome, 31_100),
    ]
    evidence = replay_rule_window(PLAN, observations, as_of_ms=31_100)
    assert evidence is not None
    assert evidence.observed_by_source == (("camera-a", True),)
    if outcome is SourceOutcome.OUT_OF_SCOPE:
        assert evidence.out_of_scope_sources == {"camera-b"}
        assert evidence.unobserved_sources == frozenset()
        assert evidence.matched is True
    else:
        assert evidence.out_of_scope_sources == frozenset()
        assert evidence.unobserved_sources == {"camera-b"}
        assert evidence.matched is None


def test_all_sources_out_of_scope_is_not_a_negative_result():
    observations = [
        _record("camera-a", SourceOutcome.OUT_OF_SCOPE, 31_000),
        _record("camera-b", SourceOutcome.OUT_OF_SCOPE, 31_100),
    ]
    evidence = replay_rule_window(PLAN, observations, as_of_ms=31_100)
    assert evidence is not None
    assert not evidence.observed_by_source
    assert not evidence.unobserved_sources
    assert evidence.matched is None


@pytest.mark.parametrize(
    "foreign_key",
    [
        replace(WINDOW_KEY, window_sequence=10),
        replace(WINDOW_KEY, window_grid_id="another-grid"),
        replace(WINDOW_KEY, rule_revision="earlier-revision"),
        replace(WINDOW_KEY, rule_id="unrelated-rule"),
    ],
)
def test_results_from_other_windows_and_rules_do_not_complete_this_one(foreign_key):
    observations = [
        _record("camera-a", SourceOutcome.NO_MATCH, 31_000),
        replace(_record("camera-b", SourceOutcome.MATCHED, 31_000), key=foreign_key),
        _record("unrelated-camera", SourceOutcome.MATCHED, 31_100),
    ]
    evidence = replay_rule_window(PLAN, observations, as_of_ms=33_000)
    assert evidence is not None
    assert evidence.observed_by_source == (("camera-a", False),)
    assert evidence.unobserved_sources == {"camera-b"}
    assert evidence.matched is None


def test_repeat_does_not_replace_first_terminal_observation():
    observations = [
        _record("camera-a", SourceOutcome.MATCHED, 31_200),
        _record("camera-a", SourceOutcome.NO_MATCH, 31_000),
        _record("camera-b", SourceOutcome.NO_MATCH, 31_100),
    ]
    before_repeat = replay_rule_window(PLAN, observations, as_of_ms=31_100)
    after_repeat = replay_rule_window(PLAN, observations, as_of_ms=31_200)
    assert before_repeat is not None
    assert before_repeat.matched is False
    assert after_repeat == before_repeat


@pytest.mark.parametrize("second_source_current", [True, False, None])
async def test_active_replay_closes_once_with_complete_current_sources(
    monkeypatch, second_source_current,
):
    now_ms = 30_100
    monkeypatch.setattr(
        "miloco.perception.camera_rule_windows.time",
        SimpleNamespace(time_ns=lambda: 1_700_000_000_000_000_000, monotonic_ns=lambda: now_ms * 1_000_000),
    )
    coordinator = CameraRuleWindows(lambda: None)
    bounds = (27_000, 30_000)
    sources = frozenset({"camera-a", "camera-b"})
    rule = {"id": "rule", "task_id": "task", "condition": {"perceive_device_ids": []}}
    load_rules = AsyncMock(return_value=([rule], []))
    key = RuleWindowKey("rule", rule_revision(rule), f"{coordinator.grid_generation}:3000", 9)

    try:
        assert await coordinator.rules_for(bounds, "camera-a", sources, load_rules) == [rule]
        assert await coordinator.rules_for(bounds, "camera-b", sources, load_rules) == [rule]
        load_rules.assert_awaited_once()
        coordinator.observe(
            bounds, [RecordedObservation(key, "camera-a", SourceOutcome.MATCHED, now_ms)],
            {"rule": CameraRuleContext("event-a", "", None, lambda: True)},
        )
        assert coordinator.take_decisions() == []
        if second_source_current is not None:
            coordinator.observe(
                bounds, [RecordedObservation(key, "camera-b", SourceOutcome.NO_MATCH, now_ms)],
                {"rule": CameraRuleContext("event-b", "", None, lambda: second_source_current)},
            )
        else:
            now_ms = 33_000
        decision, = coordinator.take_decisions()
        assert decision.evidence.matched is (True if second_source_current else None)
        coordinator.observe(
            bounds, [RecordedObservation(key, "camera-b", SourceOutcome.MATCHED, now_ms)],
            {"rule": CameraRuleContext("late", "", None, lambda: True)},
        )
        assert coordinator.take_decisions() == []
    finally:
        coordinator.discard()
