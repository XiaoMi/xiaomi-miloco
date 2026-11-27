from __future__ import annotations

import json
from pathlib import Path

import pytest
from miloco.iot_observation.models import ObservationAttribute
from miloco.iot_observation.policy import ObservationPolicy


def test_packaged_catalog_policy_matches_cli_version_and_hash():
    from miloco.iot_observation.catalog_policy import (
        catalog_policy_hash,
        load_catalog_policy,
    )

    cli_resource = Path(__file__).resolve().parents[3] / "cli/src/miloco_cli/whitelist.json"
    assert load_catalog_policy().version == json.loads(cli_resource.read_text())["version"]
    assert catalog_policy_hash() == catalog_policy_hash(cli_resource)


@pytest.fixture
def policy() -> ObservationPolicy:
    return ObservationPolicy(
        version="catalog-2026-04-29",
        whitelist=frozenset(
            {
                ("light", "prop", "on"),
                ("light", "prop", "mode"),
            }
        ),
    )


def test_monitors_writable_boolean_property(policy: ObservationPolicy) -> None:
    entry = {
        "service_type_name": "light",
        "type_name": "on",
        "kind": "prop",
        "writeable": True,
        "format": "bool",
    }

    assert policy.is_monitored(entry)
    assert policy.accepts_value(entry, True)
    assert policy.accepts_value(entry, False)
    assert not policy.accepts_value(entry, 1)


def test_monitors_writable_finite_enum_only(policy: ObservationPolicy) -> None:
    entry = {
        "service_type_name": "light",
        "type_name": "mode",
        "kind": "prop",
        "writeable": True,
        "format": "uint8",
        "value_list": [
            {"name": "normal", "value": 0},
            {"name": "reading", "value": 1},
        ],
    }

    assert policy.is_monitored(entry)
    assert policy.accepts_value(entry, 0)
    assert policy.accepts_value(entry, 1)
    assert not policy.accepts_value(entry, 2)


def test_rejects_read_only_continuous_and_non_whitelisted_properties(
    policy: ObservationPolicy,
) -> None:
    read_only = {
        "service_type_name": "light",
        "type_name": "on",
        "kind": "prop",
        "writeable": False,
        "format": "bool",
    }
    continuous = {
        "service_type_name": "light",
        "type_name": "brightness",
        "kind": "prop",
        "writeable": True,
        "format": "uint8",
        "value_range": [0, 100, 1],
    }
    non_whitelisted = {
        "service_type_name": "fan",
        "type_name": "on",
        "kind": "prop",
        "writeable": True,
        "format": "bool",
    }

    assert not policy.is_monitored(read_only)
    assert not policy.is_monitored(continuous)
    assert not policy.is_monitored(non_whitelisted)


def test_attribute_identity_contains_policy_version(policy: ObservationPolicy) -> None:
    attribute = ObservationAttribute.from_spec(
        did="light-1",
        path="iot/device/light-1/prop/2.1",
        spec_entry={
            "service_type_name": "light",
            "type_name": "on",
            "kind": "prop",
            "writeable": True,
            "format": "bool",
        },
        policy=policy,
    )

    assert attribute is not None
    assert attribute.identity[:2] == ("light-1", "iot/device/light-1/prop/2.1")
    assert attribute.policy_version.startswith("catalog-2026-04-29:")


def test_policy_rejects_invalid_enum_definition() -> None:
    policy = ObservationPolicy(
        version="v1",
        whitelist=frozenset({("light", "prop", "mode")}),
    )
    entry = {
        "service_type_name": "light",
        "type_name": "mode",
        "kind": "prop",
        "writeable": True,
        "format": "uint8",
        "value_list": [{"name": "bad", "value": {"nested": True}}],
    }

    assert not policy.is_monitored(entry)
    with pytest.raises(ValueError, match="not monitored"):
        policy.validate_value(entry, 0)


def test_spec_semantic_change_creates_new_epoch(policy):
    spec = {"service_type_name": "light", "type_name": "mode", "writeable": True,
            "format": "uint8", "value_list": [{"value": 0, "description": "normal"}]}
    first = ObservationAttribute.from_spec(did="light", path="iot/device/light/prop/2.1", spec_entry=spec, policy=policy)
    spec["value_list"] = [{"value": 0, "description": "reading"}]
    second = ObservationAttribute.from_spec(did="light", path="iot/device/light/prop/2.1", spec_entry=spec, policy=policy)
    assert first.identity != second.identity


def test_nonfinite_enum_is_rejected(policy):
    entry = {"service_type_name": "light", "type_name": "mode", "writeable": True,
             "format": "float", "value_list": [{"value": float("nan")}]}
    assert not policy.is_monitored(entry)
