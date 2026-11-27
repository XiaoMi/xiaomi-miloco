from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

_JSON_SCALARS = (type(None), bool, int, float, str)


def _is_scalar(value: Any) -> bool:
    return (type(value) in _JSON_SCALARS
            and (type(value) is not float or math.isfinite(value))
            and (type(value) is not str or len(value.encode()) <= 1024))


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    version: str
    whitelist: frozenset[tuple[str, str, str]]

    def _key(self, entry: dict[str, Any]) -> tuple[str, str, str] | None:
        service_type_name = entry.get("service_type_name")
        kind = entry.get("kind", "prop")
        type_name = entry.get("type_name")
        if not all(isinstance(value, str) for value in (service_type_name, kind, type_name)):
            return None
        return service_type_name, kind, type_name

    def _enum_values(self, entry: dict[str, Any]) -> tuple[Any, ...] | None:
        raw_values = entry.get("value_list")
        if not isinstance(raw_values, list) or not raw_values:
            return None
        values: list[Any] = []
        for item in raw_values:
            if not isinstance(item, dict) or "value" not in item:
                return None
            value = item["value"]
            if not _is_scalar(value) or any(type(value) is type(other) and value == other for other in values):
                return None
            values.append(value)
        return tuple(values)

    def is_monitored(self, entry: dict[str, Any]) -> bool:
        if entry.get("kind", "prop") != "prop" or entry.get("writeable") is not True:
            return False
        key = self._key(entry)
        if key is None or key not in self.whitelist:
            return False
        if entry.get("format") == "bool":
            return True
        return self._enum_values(entry) is not None

    def accepts_value(self, entry: dict[str, Any], value: Any) -> bool:
        if not self.is_monitored(entry):
            return False
        if entry.get("format") == "bool":
            return type(value) is bool
        values = self._enum_values(entry)
        return values is not None and any(
            type(value) is type(candidate) and value == candidate for candidate in values
        )

    def validate_value(self, entry: dict[str, Any], value: Any) -> None:
        if not self.is_monitored(entry):
            raise ValueError("property is not monitored")
        if not self.accepts_value(entry, value):
            raise ValueError(f"value {value!r} is outside the monitored property domain")
