from __future__ import annotations

import hashlib
import json
from pathlib import Path

from miloco.iot_observation.policy import ObservationPolicy

CATALOG_POLICY_PATH = Path(__file__).with_name("catalog_whitelist.json")


class CatalogPolicyUnavailable(RuntimeError):
    pass


def load_catalog_policy(path: str | Path = CATALOG_POLICY_PATH) -> ObservationPolicy:
    policy_path = Path(path)
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
        version = data["version"]
        entries = data["entries"]
        if not isinstance(version, str) or not isinstance(entries, list) or not entries:
            raise ValueError("invalid catalog policy shape")
        whitelist = frozenset(
            (entry["service_type_name"], entry["kind"], entry["type_name"])
            for entry in entries
            if isinstance(entry, dict)
            and all(isinstance(entry.get(key), str) for key in ("service_type_name", "kind", "type_name"))
        )
        if not whitelist:
            raise ValueError("empty catalog policy")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise CatalogPolicyUnavailable(f"catalog policy unavailable: {policy_path}") from error
    return ObservationPolicy(version=version, whitelist=whitelist)


def catalog_policy_hash(path: str | Path = CATALOG_POLICY_PATH) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as error:
        raise CatalogPolicyUnavailable(f"catalog policy unavailable: {path}") from error
