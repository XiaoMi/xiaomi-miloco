import asyncio
import time
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
from miloco.perception.types import MatchedRule

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


@pytest.mark.parametrize(
    "peer_outcome, expected",
    [
        (SourceOutcome.MATCHED, True),
        (SourceOutcome.NO_MATCH, False),
        (SourceOutcome.UNOBSERVED, None),
        (SourceOutcome.GATE_SKIPPED, None),
    ],
)
def test_gate_skip_contributes_only_if_a_peer_returns_usable_results(peer_outcome, expected):
    observations = [
        _record("camera-a", SourceOutcome.GATE_SKIPPED, 31_000),
        _record("camera-b", peer_outcome, 32_000),
    ]
    assert replay_rule_window(PLAN, observations, as_of_ms=31_000) is None
    evidence = replay_rule_window(PLAN, observations, as_of_ms=32_000)
    assert evidence is not None
    assert evidence.matched is expected
    if expected is not None:
        assert evidence.observed_by_source == (("camera-a", False), ("camera-b", expected))
    else:
        assert not evidence.observed_by_source


async def test_configured_window_wait_accepts_delayed_peer_without_delaying_single_source(monkeypatch):
    now_ms = 30_500
    monkeypatch.setattr(
        "miloco.perception.camera_rule_windows.time",
        SimpleNamespace(time_ns=lambda: 1_700_000_000_000_000_000, monotonic_ns=lambda: now_ms * 1_000_000),
    )
    coordinator = CameraRuleWindows(lambda: None, wait_ms=6_000)
    bounds = (27_000, 30_000)
    sources = frozenset({"camera-a", "camera-b"})
    single = {"id": "single", "task_id": "single-task", "condition": {"perceive_device_ids": ["camera-a"]}}
    multi = {"id": "multi", "task_id": "multi-task"}
    load_rules = AsyncMock(return_value=([single, multi], []))

    def observe(rule, source_id, outcome):
        key = RuleWindowKey(rule["id"], rule_revision(rule), f"{coordinator.grid_generation}:3000", 9)
        coordinator.observe(bounds, [RecordedObservation(key, source_id, outcome, now_ms)], {})

    try:
        assert await coordinator.rules_for(bounds, "camera-a", sources, load_rules) == [single, multi]
        assert await coordinator.rules_for(bounds, "camera-b", sources, load_rules) == [multi]
        observe(single, "camera-a", SourceOutcome.MATCHED)
        observe(multi, "camera-a", SourceOutcome.NO_MATCH)
        assert [decision.rule for decision in coordinator.take_decisions()] == [single]
        now_ms = 34_000
        assert coordinator.take_decisions() == []
        observe(multi, "camera-b", SourceOutcome.MATCHED)
        decision, = coordinator.take_decisions()
        assert decision.rule == multi and decision.evidence.matched is True
    finally:
        coordinator.discard()


@pytest.mark.parametrize("peer_has_video", [False, True])
async def test_only_sources_with_prior_video_are_waited_for(monkeypatch, peer_has_video):
    now_ms = 31_000
    monkeypatch.setattr(
        "miloco.perception.camera_rule_windows.time",
        SimpleNamespace(time_ns=lambda: 1_700_000_000_000_000_000, monotonic_ns=lambda: now_ms * 1_000_000),
    )
    coordinator = CameraRuleWindows(lambda: None)
    bounds = (27_000, 30_000)
    sources = frozenset({"camera-a", "camera-b"} if peer_has_video else {"camera-b"})
    rule = {"id": "rule", "task_id": "task", "condition": {"perceive_device_ids": ["camera-a", "camera-b"]}}
    load_rules = AsyncMock(return_value=([rule], []))
    key = RuleWindowKey("rule", rule_revision(rule), f"{coordinator.grid_generation}:3000", 9)

    try:
        assert await coordinator.rules_for(bounds, "camera-b", sources, load_rules) == [rule]
        if not peer_has_video:
            both = sources | {"camera-a"}
            assert await coordinator.rules_for(bounds, "camera-a", both, load_rules) == []
        hit = MatchedRule(rule_id="rule", reason="fixture", source_device_ids=["camera-b"])
        coordinator.observe(
            bounds, [RecordedObservation(key, "camera-b", SourceOutcome.MATCHED, now_ms)],
            {"rule": CameraRuleContext(None, "", hit, lambda: True)},
        )
        decisions = coordinator.take_decisions()
        if peer_has_video:
            assert decisions == []
            now_ms = 33_000
            decision, = coordinator.take_decisions()
            assert decision.evidence.matched is None
            assert decision.evidence.unobserved_sources == {"camera-a"}
            assert decision.partial_enter_sources == ("camera-b",)
        else:
            decision, = decisions
            assert decision.evidence.matched is True
            assert not decision.evidence.unobserved_sources
            assert not decision.partial_enter_sources
            now_ms = 34_000
            assert await coordinator.rules_for((30_000, 33_000), "camera-a", both, load_rules) == [rule]
        assert decision.evidence.observed_by_source == (("camera-b", True),)
    finally:
        coordinator.discard()


@pytest.mark.parametrize(
    ("overrides", "hit_time_ms", "connection_current", "allowed"),
    [
        ({"mode": "event"}, 32_999, True, True),
        ({"mode": "state"}, 32_999, True, True),
        ({"direction": "enter"}, 32_999, True, True),
        ({"direction": "session"}, 32_999, True, True),
        ({"direction": "exit"}, 32_999, True, False),
        ({"direction": "guard"}, 32_999, True, False),
        ({"direction": "milestone"}, 32_999, True, False),
        ({"direction": "enter", "duration_seconds": 12}, 32_999, True, False),
        ({"direction": "enter"}, 33_000, True, False),
        ({"direction": "enter"}, 32_999, False, False),
    ],
)
async def test_partial_entry_requires_an_instant_enter_rule_and_current_hit_before_deadline(
    monkeypatch, overrides, hit_time_ms, connection_current, allowed,
):
    now_ms = 31_000
    monkeypatch.setattr(
        "miloco.perception.camera_rule_windows.time",
        SimpleNamespace(time_ns=lambda: 1_700_000_000_000_000_000, monotonic_ns=lambda: now_ms * 1_000_000),
    )
    coordinator = CameraRuleWindows(lambda: None)
    bounds = (27_000, 30_000)
    sources = frozenset({"camera-a", "camera-b"})
    rule = {"id": "rule", "task_id": "task", **overrides}
    load_rules = AsyncMock(return_value=([rule], []))
    key = RuleWindowKey("rule", rule_revision(rule), f"{coordinator.grid_generation}:3000", 9)
    match = MatchedRule(rule_id="rule", reason="fixture", source_device_ids=["camera-a"])

    try:
        await coordinator.rules_for(bounds, "camera-a", sources, load_rules)
        now_ms = hit_time_ms
        coordinator.observe(
            bounds, [RecordedObservation(key, "camera-a", SourceOutcome.MATCHED, now_ms)],
            {"rule": CameraRuleContext(None, "", match, lambda: connection_current)},
        )
        now_ms = 33_000
        decisions = coordinator.take_decisions()
        if rule.get("direction") == "guard":
            assert not decisions
        else:
            decision, = decisions
            assert decision.evidence.matched is None
            assert decision.partial_enter_sources == (("camera-a",) if allowed else ())
        assert not coordinator.take_decisions()
    finally:
        coordinator.discard()


@pytest.mark.parametrize("peer_status", ["processed", "stale", "late", "missing"])
async def test_single_source_gate_skip_needs_current_peer_in_same_window(monkeypatch, peer_status):
    now_ms = 31_000
    monkeypatch.setattr(
        "miloco.perception.camera_rule_windows.time",
        SimpleNamespace(time_ns=lambda: 1_700_000_000_000_000_000, monotonic_ns=lambda: now_ms * 1_000_000),
    )
    coordinator = CameraRuleWindows(lambda: None)
    bounds = (27_000, 30_000)
    sources = frozenset({"camera-a", "camera-b"})
    rule = {"id": "rule", "task_id": "task", "condition": {"perceive_device_ids": ["camera-a"]}}
    load_rules = AsyncMock(return_value=([rule], []))
    key = RuleWindowKey("rule", rule_revision(rule), f"{coordinator.grid_generation}:3000", 9)

    try:
        assert await coordinator.rules_for(bounds, "camera-a", sources, load_rules) == [rule]
        assert await coordinator.rules_for(bounds, "camera-b", sources, load_rules) == []
        coordinator.observe(bounds, [RecordedObservation(key, "camera-a", SourceOutcome.GATE_SKIPPED, now_ms)], {})
        assert coordinator.take_decisions() == []

        now_ms = 33_001 if peer_status == "late" else 32_000
        if peer_status != "missing":
            coordinator.report_processed(bounds, "camera-b", lambda: peer_status != "stale")
        decisions = coordinator.take_decisions()
        if peer_status == "processed":
            decision, = decisions
            assert decision.evidence.observed_by_source == (("camera-a", False),)
            assert decision.evidence.matched is False
        else:
            if peer_status != "late":
                assert decisions == []
                now_ms = 33_000
                decisions = coordinator.take_decisions()
            decision, = decisions
            assert not decision.evidence.observed_by_source
            assert decision.evidence.matched is None
        assert coordinator.take_decisions() == []
    finally:
        coordinator.discard()


@pytest.mark.parametrize("peer_status", ["completed", "failed", "not_started", "stale", "at_cap"])
async def test_gate_exit_only_extends_for_an_inflight_peer(monkeypatch, peer_status):
    now_ms = 31_000
    monkeypatch.setattr(
        "miloco.perception.camera_rule_windows.time",
        SimpleNamespace(time_ns=lambda: 1_700_000_000_000_000_000, monotonic_ns=lambda: now_ms * 1_000_000),
    )
    coordinator = CameraRuleWindows(lambda: None, gate_peer_budget_ms=20_000)
    bounds = (27_000, 30_000)
    sources = frozenset({"camera-a", "camera-b"})
    rule = {"id": "rule", "task_id": "task", "condition": {"perceive_device_ids": ["camera-a"]}}
    load_rules = AsyncMock(return_value=([rule], []))
    key = RuleWindowKey("rule", rule_revision(rule), f"{coordinator.grid_generation}:3000", 9)

    try:
        await coordinator.rules_for(bounds, "camera-a", sources, load_rules)
        if peer_status != "not_started":
            await coordinator.rules_for(bounds, "camera-b", sources, load_rules)
        coordinator.observe(bounds, [RecordedObservation(key, "camera-a", SourceOutcome.GATE_SKIPPED, now_ms)], {})
        coordinator.finish(bounds, "camera-a")
        assert not coordinator.take_decisions()

        now_ms = 33_000
        if peer_status == "not_started":
            await coordinator.rules_for(bounds, "camera-b", sources, load_rules)
            coordinator.report_processed(bounds, "camera-b", lambda: True)
        else:
            assert not coordinator.take_decisions()
            now_ms = 53_000 if peer_status == "at_cap" else 35_000
            if peer_status != "failed":
                coordinator.report_processed(bounds, "camera-b", lambda: peer_status != "stale")
            if peer_status != "at_cap":
                coordinator.finish(bounds, "camera-b")

        decision, = coordinator.take_decisions()
        assert decision.evidence.matched is (False if peer_status == "completed" else None)
        assert not coordinator.take_decisions()
    finally:
        coordinator.discard()


@pytest.mark.parametrize("other_outcome", [None, SourceOutcome.NO_MATCH, SourceOutcome.MATCHED])
async def test_new_hit_only_replaces_the_same_rules_confirmed_gate_exit(monkeypatch, other_outcome):
    now_ms = 31_000
    monkeypatch.setattr(
        "miloco.perception.camera_rule_windows.time",
        SimpleNamespace(time_ns=lambda: 1_700_000_000_000_000_000, monotonic_ns=lambda: now_ms * 1_000_000),
    )
    coordinator = CameraRuleWindows(lambda: None, gate_peer_budget_ms=20_000)
    old, new = (27_000, 30_000), (30_000, 33_000)
    sources = frozenset({"camera-a", "camera-b", "camera-c"})
    entering = {"id": "entering", "task_id": "task", "condition": {"perceive_device_ids": ["camera-a"]}}
    other = {"id": "other", "task_id": "task", "condition": {"perceive_device_ids": ["camera-c"]}}
    load_rules = AsyncMock(return_value=([entering, other], []))

    def observe(bounds, rule, did, outcome):
        key = RuleWindowKey(
            rule["id"], rule_revision(rule), f"{coordinator.grid_generation}:3000", bounds[0] // 3000,
        )
        coordinator.observe(bounds, [RecordedObservation(key, did, outcome, now_ms)], {})
        coordinator.finish(bounds, did)

    try:
        for did in sources:
            await coordinator.rules_for(old, did, sources, load_rules)
        observe(old, entering, "camera-a", SourceOutcome.GATE_SKIPPED)
        observe(old, other, "camera-c", SourceOutcome.GATE_SKIPPED)
        assert not coordinator.take_decisions()

        now_ms = 34_000
        await coordinator.rules_for(new, "camera-a", sources, load_rules)
        observe(new, entering, "camera-a", SourceOutcome.MATCHED)
        if other_outcome is not None:
            await coordinator.rules_for(new, "camera-c", sources, load_rules)
            observe(new, other, "camera-c", other_outcome)

        now_ms = 36_000
        coordinator.report_processed(old, "camera-b", lambda: True)
        coordinator.finish(old, "camera-b")
        observed = [
            (decision.rule["id"], decision.plan.window_end_ms, decision.evidence.matched)
            for decision in coordinator.take_decisions()
            if decision.evidence.matched is not None
        ]
        expected = [("entering", new[1], True)]
        if other_outcome is not SourceOutcome.MATCHED:
            expected.insert(0, ("other", old[1], False))
        if other_outcome is not None:
            expected.append(("other", new[1], other_outcome is SourceOutcome.MATCHED))
        assert observed == expected
        assert not coordinator.take_decisions()
    finally:
        coordinator.discard()


async def test_concurrent_rule_load_keeps_first_window_snapshot():
    coordinator = CameraRuleWindows(lambda: None)
    end = (time.monotonic_ns() // 1_000_000 // 3000) * 3000
    bounds = (end - 3000, end)
    sources = frozenset({"camera-a", "camera-b"})
    first_load_started = asyncio.Event()
    finish_first_load = asyncio.Event()
    current_rule = {"id": "current", "task_id": "task"}
    stale_rule = {"id": "stale", "task_id": "task"}

    async def load_stale_rules():
        first_load_started.set()
        await finish_first_load.wait()
        return [stale_rule], []

    try:
        async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
            first = tasks.create_task(coordinator.rules_for(bounds, "camera-a", sources, load_stale_rules))
            await first_load_started.wait()
            second = await coordinator.rules_for(
                bounds, "camera-b", sources, AsyncMock(return_value=([current_rule], [])),
            )
            finish_first_load.set()
        assert first.result() == second == [current_rule]
    finally:
        coordinator.discard()


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
