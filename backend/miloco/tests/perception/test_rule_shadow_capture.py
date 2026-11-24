import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.observability.context import (
    DeviceContext,
    reset_device_context,
    set_device_context,
)
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni import omni
from miloco.perception.engine.omni.response_parser import (
    ParsedRuleHits,
    parse_omni_response,
    parse_shadow_rule_hits,
)
from miloco.perception.engine.types import OmniContext, RuleCondition
from miloco.perception.rule_shadow_capture import (
    RuleResponseSample,
    RuleShadowCapture,
    shadow_batch_payload,
)
from miloco.perception.rule_window_replay import (
    RecordedObservation,
    RuleWindowKey,
    RuleWindowPlan,
    SourceOutcome,
    replay_rule_window,
)
from miloco.perception.snapshot_context import OmniEventArtifacts, event_artifacts_scope

RULES = [
    {"id": "rule-a", "name": "Known rule", "condition": {"query": "not for telemetry"}}
]


def _response(content: str, *, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}]
    }


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        '{"matched_rules": null}',
        '{"matched_rules": [], "matched_rules": []}',
        '{"matched_rules": [false]}',
        '{"matched_rules": [{"rule_name": "Known rule", "reason": "seen"}]}',
        '{"matched_rules": [{"rule_name": "Known rule", "reason": "none", "hit": "false"}]}',
        '{"matched_rules": [{"rule_name": "Known rule", "hit": false, "reason": 42}]}',
        '{"matched_rules": [{"rule_name": "  ", "hit": true}]}',
        '{"matched_rules": [{"rule_name": "Unknown", "reason": "unknown", "hit": false}]}',
        '{"matched_rules": [{"rule_name": "Known rule", "reason": "seen", "hit": true}',
        '{"matched_rules": [{"rule_name": "Known rule", "reason": "seen", "hit": true},'
        ' {"rule_name": "Known rule", "reason": "none", "hit": false}]}',
    ],
)
def test_invalid_shadow_rule_array_is_not_an_empty_hit_set(content):
    assert parse_shadow_rule_hits(_response(content), {"Known rule": "rule-a"}) is None


def test_shadow_evidence_accepts_unique_rule_name_without_prefix_or_reason():
    raw = _response('{"matched_rules": [{"rule_name": "Known rule", "hit": true}]}')
    rule_names = {"[task-a] Known rule": "rule-a", "[task-b] Another rule": "rule-b"}
    assert parse_shadow_rule_hits(raw, rule_names) == ParsedRuleHits(frozenset({"rule-a"}), complete=True)
    assert [match.rule_id for match in parse_omni_response(raw, rule_names).matched_rules] == ["rule-a"]


def test_shadow_evidence_rejects_ambiguous_aliases_and_duplicate_rule_ids():
    raw = _response('{"matched_rules": [{"rule_name": "Known rule", "hit": true}]}')
    ambiguous_names = {"[task-a] Known rule": "rule-a", "[task-b] Known rule": "rule-b"}
    assert parse_shadow_rule_hits(raw, ambiguous_names) is None
    duplicate = _response(
        '{"matched_rules": [{"rule_name": "[task-a] Known rule", "hit": true},'
        ' {"rule_name": "Known rule", "hit": false}]}'
    )
    assert parse_shadow_rule_hits(duplicate, {"[task-a] Known rule": "rule-a"}) is None


def test_legacy_parser_stays_permissive_but_shadow_evidence_requires_explicit_boolean():
    raw = _response(
        '{"matched_rules": [{"rule_name": "Known rule", "reason": "seen"}]}'
    )
    rule_names = {"Known rule": "rule-a"}
    assert [
        match.rule_id for match in parse_omni_response(raw, rule_names).matched_rules
    ] == ["rule-a"]
    assert parse_shadow_rule_hits(raw, rule_names) is None
    assert (
        parse_shadow_rule_hits(_response('{"matched_rules": []}'), rule_names)
        == ParsedRuleHits(frozenset(), complete=True)
    )
    assert (
        parse_shadow_rule_hits(
            _response('{"matched_rules": []}', finish_reason="length"),
            rule_names,
        )
        is None
    )


def test_partial_evidence_keeps_unrelated_hits_and_drops_conflicting_aliases():
    names = {
        "Fall detected": "fall",
        "[a] Person present": "guard-a",
        "[b] Person present": "guard-b",
        "Movement detected": "movement",
    }
    raw = _response(json.dumps({"matched_rules": [
        {"rule_name": "Fall detected", "hit": True, "reason": None},
        {"rule_name": "Unknown output", "hit": False},
        {"rule_name": "[a] Person present", "hit": True},
        {"rule_name": "Person present", "hit": False},
        {"rule_name": "Movement detected", "hit": True},
        {"rule_name": "Movement detected", "hit": False},
    ]}))
    assert parse_shadow_rule_hits(raw, names) == ParsedRuleHits(frozenset({"fall"}), complete=False)


def _capture(
    source_id: str, captured_at_ms: int, hits: frozenset[str] | None
) -> RuleShadowCapture:
    capture = RuleShadowCapture()
    capture.prepare({source_id: ["rule-a"]}, RULES)
    capture.record(source_id, RuleResponseSample(captured_at_ms, hits))
    return capture


def _batch_observations(capture: RuleShadowCapture):
    return list(
        capture.observations(
            grid_generation="run",
            bounds_by_source={
                "camera-a": (27_000, 30_000),
                "camera-b": (27_000, 30_000),
            },
            batch_returned_at_ms=35_000,
        )
    )


def test_same_window_evidence_can_be_replayed_across_batches():
    first = _batch_observations(_capture("camera-a", 31_000, frozenset()))
    second = _batch_observations(_capture("camera-b", 32_500, frozenset({"rule-a"})))
    assert len(first) == len(second) == 1
    assert first[0].key == second[0].key
    assert first[0].key.window_sequence == 9
    plan = RuleWindowPlan(first[0].key, 30_000, frozenset({"camera-a", "camera-b"}))
    serialized = json.dumps([shadow_batch_payload(first), shadow_batch_payload(second)])
    assert "not for telemetry" not in serialized
    payloads = json.loads(serialized)
    assert all(payload["version"] == 1 for payload in payloads)
    observations = [
        RecordedObservation(
            RuleWindowKey(**row["key"]), row["source_id"], SourceOutcome(row["outcome"]), row["received_at_ms"],
        )
        for payload in payloads for row in payload["observations"]
    ]
    assert replay_rule_window(plan, observations, as_of_ms=32_499) is None
    decision = replay_rule_window(plan, observations, as_of_ms=32_500)
    assert decision is not None
    assert decision.observed_by_source == (("camera-a", False), ("camera-b", True))
    assert decision.matched is True


def test_absent_or_failed_sample_is_unobserved_and_uses_same_window_clock():
    capture = _capture("camera-a", 31_000, None)
    observations = _batch_observations(capture)
    assert len(observations) == 1
    assert observations[0].outcome is SourceOutcome.UNOBSERVED
    assert observations[0].received_at_ms == 31_000
    capture.prepare({"camera-b": ["rule-a"]}, RULES)
    pending = _batch_observations(capture)
    assert len(pending) == 1
    assert pending[0].key == observations[0].key
    assert pending[0].outcome is SourceOutcome.UNOBSERVED
    assert pending[0].received_at_ms == 35_000


def test_new_rule_revision_does_not_inherit_previous_response():
    capture = _capture("camera-a", 31_000, frozenset({"rule-a"}))
    (previous,) = _batch_observations(capture)
    capture.prepare({"camera-a": ["rule-a"]}, [{**RULES[0], "name": "New revision"}])
    (latest,) = _batch_observations(capture)
    assert latest.key.rule_revision != previous.key.rule_revision
    assert latest.outcome is SourceOutcome.UNOBSERVED


async def test_partial_model_response_does_not_write_unresolved_rules_as_misses(monkeypatch):
    raw = _response(
        '{"matched_rules":[{"rule_name":"Known rule","hit":true},{"rule_name":"Unknown output","hit":false}]}'
    )
    monkeypatch.setattr(omni, "call_omni", AsyncMock(return_value=raw))
    monkeypatch.setattr(omni, "build_batch_prompt", lambda *_args, **_kwargs: {"has_visual_rule_input": True})
    context = OmniContext(rule_conditions=[
        RuleCondition("rule-a", "Known rule", "query"),
        RuleCondition("rule-b", "Another rule", "query"),
    ])
    capture = RuleShadowCapture()
    capture.prepare({"camera-a": ["rule-a", "rule-b"]}, [*RULES, {"id": "rule-b", "name": "Another rule"}])
    token = set_device_context(DeviceContext("device-trace", "camera-a", "room"))
    try:
        with event_artifacts_scope(OmniEventArtifacts(rule_shadow=capture, scheduled_camera=True)):
            output = await omni.run_omni_batch([], context, OmniConfig())
    finally:
        reset_device_context(token)

    assert [match.rule_id for match in output.matched_rules] == ["rule-a"]
    assert {item.key.rule_id: item.outcome for item in _batch_observations(capture)} == {
        "rule-a": SourceOutcome.MATCHED,
        "rule-b": SourceOutcome.UNOBSERVED,
    }


@pytest.mark.parametrize("visual", [True, False])
async def test_fused_entry_records_shadow_evidence_without_changing_legacy_result(
    monkeypatch, visual
):
    raw = _response(
        '{"matched_rules": [{"rule_name": "Known rule", "reason": "seen", "hit": true}]}'
    )
    call = AsyncMock(return_value=raw)
    monkeypatch.setattr(omni, "_call_omni_messages", call)
    monkeypatch.setattr(
        omni,
        "build_fused_payload",
        lambda **_kwargs: {"messages": [], "has_visual_rule_input": visual},
    )
    engine = SimpleNamespace(
        take_fused_pending=lambda: None,
        library=SimpleNamespace(list_persons=lambda: []),
    )
    context = OmniContext(
        rule_conditions=[RuleCondition("rule-a", "Known rule", "query")]
    )
    capture = RuleShadowCapture()
    capture.prepare({"camera-a": ["rule-a"]}, RULES)
    token = set_device_context(DeviceContext("device-trace", "camera-a", "room"))
    try:
        with event_artifacts_scope(OmniEventArtifacts(rule_shadow=capture)):
            output = await omni.run_omni_fused([], context, OmniConfig(), engine)
    finally:
        reset_device_context(token)

    call.assert_awaited_once()
    assert [match.rule_id for match in output.matched_rules] == ["rule-a"]
    observations = _batch_observations(capture)
    assert len(observations) == 1
    expected = SourceOutcome.MATCHED if visual else SourceOutcome.UNOBSERVED
    assert observations[0].outcome is expected


@pytest.mark.parametrize(
    ("finish", "visual", "expected"),
    [
        ("stop", True, SourceOutcome.NO_MATCH),
        ("length", True, SourceOutcome.UNOBSERVED),
        ("stop", False, SourceOutcome.UNOBSERVED),
    ],
)
@pytest.mark.parametrize("scheduled", [False, True])
async def test_stream_capture_only_uses_complete_visual_rule_results(
    monkeypatch, finish, visual, expected, scheduled,
):
    async def stream(_payload, _config, *, usage_out, status_out):
        yield '{"matched_rules":[]}'
        status_out.update(completed=True, finish_reason=finish)

    monkeypatch.setattr(omni, "call_omni_stream", stream)
    monkeypatch.setattr(
        omni, "build_batch_stream_prompt", lambda *_args, **_kwargs: {"has_visual_rule_input": visual}
    )
    context = OmniContext(rule_conditions=[RuleCondition("rule-a", "Known rule", "query")])
    capture = RuleShadowCapture()
    capture.prepare({"camera-a": ["rule-a"]}, RULES)
    token = set_device_context(DeviceContext("device-trace", "camera-a", "room"))
    try:
        artifacts = OmniEventArtifacts(rule_shadow=capture, scheduled_camera=scheduled)
        with event_artifacts_scope(artifacts):
            await omni.run_omni_batch_stream([], context, OmniConfig())
    finally:
        reset_device_context(token)

    observation, = _batch_observations(capture)
    assert observation.outcome is expected


@pytest.mark.parametrize(
    ("scheduled", "capture_enabled", "visual", "expected"),
    [
        (True, True, True, SourceOutcome.MATCHED),
        (True, True, False, SourceOutcome.UNOBSERVED),
        (False, True, True, SourceOutcome.MATCHED),
        (False, False, True, SourceOutcome.UNOBSERVED),
    ],
)
async def test_nonstream_records_rule_evidence_when_capture_is_enabled(
    monkeypatch, scheduled, capture_enabled, visual, expected,
):
    raw = _response(
        '{"matched_rules":[{"rule_name":"Known rule","reason":"seen","hit":true}]}'
    )
    monkeypatch.setattr(omni, "call_omni", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        omni, "build_batch_prompt", lambda *_args, **_kwargs: {"has_visual_rule_input": visual}
    )
    context = OmniContext(rule_conditions=[RuleCondition("rule-a", "Known rule", "query")])
    capture = RuleShadowCapture()
    capture.prepare({"camera-a": ["rule-a"]}, RULES)
    token = set_device_context(DeviceContext("device-trace", "camera-a", "room"))
    try:
        artifacts = OmniEventArtifacts(
            rule_shadow=capture if capture_enabled else None, scheduled_camera=scheduled,
        )
        with event_artifacts_scope(artifacts):
            output = await omni.run_omni_batch([], context, OmniConfig())
    finally:
        reset_device_context(token)

    assert [match.rule_id for match in output.matched_rules] == ["rule-a"]
    observation, = _batch_observations(capture)
    assert observation.outcome is expected
