from __future__ import annotations

from types import SimpleNamespace

import pytest
from miloco.automation.service import (
    AutomationService,
    _coerce_number,
    _match_condition,
)
from miloco.middleware.exceptions import ResourceNotFoundException
from miloco.miot.schema import MiotEventMapping, MiotEventTrigger
from miloco.perception.types import CaptionEntry, MatchedRule
from miloco.rule.schema import RuleTriggerType


class _KVRepoStub:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._store.get(key, default)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value


class _RuleServiceStub:
    def __init__(self, rules: list[SimpleNamespace] | None = None) -> None:
        self.rules = rules or []
        self.created_rules = []

    async def create_rule(self, rule):
        rule.id = f"rule-auto-{len(self.created_rules) + 1}"
        self.created_rules.append(rule)
        self.rules.append(rule)
        return rule.id

    async def update_rule(self, rule):
        for idx, existing in enumerate(self.rules):
            if existing.id == rule.id:
                self.rules[idx] = rule
                return True
        raise ResourceNotFoundException(f"Rule '{rule.id}' not found")

    async def delete_rule(self, rule_id):
        self.rules = [rule for rule in self.rules if rule.id != rule_id]
        return True

    async def get_all_rules(self, enabled_only=False):
        if not enabled_only:
            return self.rules
        return [rule for rule in self.rules if getattr(rule, "enabled", True)]


@pytest.mark.parametrize(
    ("actual", "expected", "matched"),
    [
        ("1", {"op": "eq", "value": "1"}, True),
        ("1", {"op": "ne", "value": "0"}, True),
        ("12", {"op": "gt", "value": "10"}, True),
        ("3", {"op": "lt", "value": "5"}, True),
        ("9", {"op": "gte", "value": "9"}, True),
        ("9", {"op": "lte", "value": "9"}, True),
        ("abc", {"op": "gt", "value": "1"}, False),
        (None, {"op": "any", "value": "*"}, False),
        (None, {"op": "eq", "value": "None"}, False),
    ],
)
def test_match_condition_supports_string_and_numeric_operators(
    actual,
    expected,
    matched,
):
    assert _match_condition(actual, expected) is matched


@pytest.mark.parametrize(
    ("value", "number"),
    [
        ("12", 12.0),
        (" 7.5 ", 7.5),
        (8, 8.0),
        (True, None),
        ("abc", None),
    ],
)
def test_coerce_number(value, number):
    assert _coerce_number(value) == number


@pytest.mark.asyncio
async def test_handle_trigger_uses_external_gate_realtime_context():
    service = AutomationService(_KVRepoStub())
    service.create_mapping(
        MiotEventMapping(
            source_type="device",
            source_id="sensor-1",
            source_name_snapshot="门磁",
            camera_dids=["cam-1"],
            enabled=True,
            query_template="重点看门口",
            event_kinds=["device_prop"],
            property_filters={"prop.2.1": {"op": "eq", "value": "1"}},
            cooldown_seconds=0,
        )
    )

    captured: dict[str, object] = {}

    async def _external(sources, rules, extra_context_by_did=None):
        captured["sources"] = sources
        captured["rules"] = rules
        captured["extra_context_by_did"] = extra_context_by_did
        return (
            SimpleNamespace(
                caption=[SimpleNamespace(description="门口无人")],
                suggestions=[],
                matched_rules=[],
                device_rule_map={"cam-1": [rules[0]["id"]]},
                skipped=False,
            ),
            set(),
            set(),
            set(),
            SimpleNamespace(clips={}),
        )

    async def _handle_structured(**kwargs):
        captured["postprocess"] = kwargs
        return SimpleNamespace(snapshot_count=0, clip_kind=None)

    perception_service = SimpleNamespace(
        external_trigger_perceive=_external,
        handle_structured_perception_result=_handle_structured,
    )
    rule_service = _RuleServiceStub()
    meaningful_events_dao = SimpleNamespace(
        insert=lambda **_: None,
        update_snapshot_count=lambda *_: None,
    )

    trigger = MiotEventTrigger(
        source_type="device",
        source_id="sensor-1",
        source_name="门磁",
        event_name="device_prop",
        changed_properties={"prop.2.1": "1"},
        occurred_at=1234567890,
        raw={},
    )

    await service.handle_trigger(
        trigger=trigger,
        perception_service=perception_service,
        rule_service=rule_service,
        miot_service=None,
        meaningful_events_dao=meaningful_events_dao,
    )

    assert captured["sources"] == ["cam-1"]
    assert "米家触发上下文" in captured["extra_context_by_did"]["cam-1"]
    assert "属性变化" in captured["extra_context_by_did"]["cam-1"]
    assert captured["rules"][0]["id"].startswith("rule-auto-")
    assert captured["rules"][0]["condition"]["query"] == "重点看门口"
    assert captured["rules"][0]["condition"]["perceive_device_ids"] == ["cam-1"]
    assert captured["postprocess"]["text_prefix"].startswith("[米家设备触发]")
    assert captured["postprocess"]["pulse_reset_rule_ids"] == {"rule-auto-1"}


@pytest.mark.asyncio
async def test_handle_trigger_postprocesses_formal_mapping_rule_matches():
    service = AutomationService(_KVRepoStub())
    service.create_mapping(
        MiotEventMapping(
            source_type="device",
            source_id="sensor-1",
            source_name_snapshot="门磁",
            camera_dids=["cam-1"],
            enabled=True,
            query_template="重点看门口",
            event_kinds=["device_prop"],
            property_filters={"prop.2.1": {"op": "eq", "value": "1"}},
            cooldown_seconds=0,
        )
    )

    rule = SimpleNamespace(
        id="rule-1",
        name="门口有人",
        enabled=True,
        trigger_type=RuleTriggerType.MIOT_EVENT,
        condition=SimpleNamespace(
            source_ids=["sensor-1"],
            event_kinds=["device_prop"],
            property_filters={"prop.2.1": {"op": "eq", "value": "1"}},
            mapping_ids=[],
            use_global_mapping=True,
            query="画面中是否有人在门口",
        ),
    )
    captured: dict[str, object] = {}

    async def _external(_sources, rules, **__):
        captured["prompt_rule_ids"] = [rule["id"] for rule in rules]
        result = SimpleNamespace(
            caption=[CaptionEntry(description="门口有人")],
            suggestions=[],
            matched_rules=[
                MatchedRule(
                    rule_id="rule-1",
                    rule_name="门口有人",
                    reason="画面中有人在门口",
                    source_device_ids=["cam-1"],
                ),
                MatchedRule(
                    rule_id="rule-auto-1",
                    rule_name="[感知触发] 门磁",
                    reason="感知触发配置命中",
                    source_device_ids=["cam-1"],
                ),
            ],
            device_rule_map={"cam-1": [rule["id"] for rule in rules]},
            skipped=False,
        )
        return result, set(), set(), set(), SimpleNamespace(clips={})

    async def _handle_structured(**kwargs):
        captured["postprocess"] = kwargs
        return SimpleNamespace(snapshot_count=0, clip_kind=None)

    perception_service = SimpleNamespace(
        external_trigger_perceive=_external,
        handle_structured_perception_result=_handle_structured,
    )
    rule_service = _RuleServiceStub([rule])
    meaningful_events_dao = SimpleNamespace(
        insert=lambda **_: None,
        update_snapshot_count=lambda *_: None,
    )

    log = await service.handle_trigger(
        trigger=MiotEventTrigger(
            source_type="device",
            source_id="sensor-1",
            source_name="门磁",
            event_name="device_prop",
            changed_properties={"prop.2.1": "1"},
            occurred_at=1234567890,
            raw={},
        ),
        perception_service=perception_service,
        rule_service=rule_service,
        miot_service=None,
        meaningful_events_dao=meaningful_events_dao,
    )

    assert captured["prompt_rule_ids"] == ["rule-1", "rule-auto-1"]
    assert all(not rule_id.startswith("miot_mapping:") for rule_id in captured["prompt_rule_ids"])
    assert captured["postprocess"]["pulse_reset_rule_ids"] == {"rule-1", "rule-auto-1"}
    assert log.matched_rule_ids == ["rule-1", "rule-auto-1"]
    assert len(log.structured_matched_rules) == 2
