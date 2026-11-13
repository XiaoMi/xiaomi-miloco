from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any


class ObservationMissingType:
    __slots__ = ()

    def __repr__(self) -> str:
        return "OBSERVATION_MISSING"


OBSERVATION_MISSING = ObservationMissingType()

if TYPE_CHECKING:
    from miloco.iot_observation.policy import ObservationPolicy


@dataclass(frozen=True, slots=True)
class ObservationAttribute:
    did: str
    path: str
    service_type_name: str
    type_name: str
    policy_version: str
    kind: str = "prop"

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.did, self.path, self.policy_version

    @classmethod
    def from_spec(
        cls,
        *,
        did: str,
        path: str,
        spec_entry: dict[str, Any],
        policy: ObservationPolicy,
    ) -> ObservationAttribute | None:
        if not policy.is_monitored(spec_entry):
            return None
        service_type_name = spec_entry.get("service_type_name")
        type_name = spec_entry.get("type_name")
        if not isinstance(service_type_name, str) or not isinstance(type_name, str):
            return None
        return cls(
            did=did,
            path=path,
            service_type_name=service_type_name,
            type_name=type_name,
            policy_version=policy.version + ":" + hashlib.sha256(json.dumps(
                {key: spec_entry.get(key) for key in (
                    "service_type_name", "type_name", "format", "writeable", "value_list", "unit",
                )}, sort_keys=True, ensure_ascii=True, allow_nan=False,
            ).encode()).hexdigest()[:24],
        )


@dataclass(frozen=True, slots=True)
class ObservationChange:
    attribute: ObservationAttribute
    old: Any
    new: Any
    source: str
    at_ms: int
    scope: int
    record_id: int = 0


@dataclass(frozen=True, slots=True)
class ObservationGap:
    scope: int
    reason: str
    at_ms: int
    path: str | None = None
