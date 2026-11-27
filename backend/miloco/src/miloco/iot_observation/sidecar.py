from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from miloco.config.settings import IoTObservationSettings
from miloco.iot_observation.repository import ObservationRepository


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Episode(StrictModel):
    event_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(
        min_length=1, max_length=16
    )
    kind: Literal["support", "difference", "unknown"]


class Commonality(StrictModel):
    observation_id: str | None = Field(default=None, max_length=64)
    description: str = Field(min_length=1, max_length=240)
    attributes: list[
        Annotated[str, Field(max_length=16, pattern=r"^[0-9a-f]{16}$")]
    ] = Field(min_length=1, max_length=8)
    episodes: list[Episode] = Field(min_length=1, max_length=8)
    uncertainty: str = Field(default="", max_length=240)


class Submission(StrictModel):
    batch_id: str = Field(min_length=1, max_length=64)
    observations: list[Commonality] = Field(max_length=8)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _attribute(row):
    return {
        "did": row["did"],
        "path": row["path"],
        "service": row["service_type_name"],
        "property": row["type_name"],
        "version": row["policy_version"],
    }


def _attribute_id(row):
    return _digest(_attribute(row))[:16]


class ObservationSidecar:
    def __init__(
        self, repository: ObservationRepository, settings: IoTObservationSettings
    ):
        self.repository = repository
        self.settings = settings
        self.zone = ZoneInfo(settings.timezone)
        self.catalog = ("", {})

    @staticmethod
    def _events(conn, family, clause, parameters, limit=64, order="ASC"):
        return conn.execute(
            "SELECT c.*, a.service_type_name, a.type_name FROM observation_change c "
            "JOIN observation_attribute a USING(family_id,did,path,policy_version) "
            f"WHERE c.family_id=? AND c.replay=0 AND a.active=1 AND {clause} "
            f"ORDER BY c.id {order} LIMIT ?",
            (family, *parameters, limit),
        ).fetchall()

    def batch(self, family, *, scope, now_ms=None):
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self.repository.transaction() as conn:
            pending = conn.execute(
                "SELECT * FROM observe_batch WHERE family_id=?", (family,)
            ).fetchone()
            if (
                pending
                and pending["receipt"] is None
                and pending["scope"] == scope
                and 0 <= now_ms - pending["created_ms"] < 86400000
            ):
                packet = json.loads(pending["payload"])
                if self._packet_valid(conn, family, packet):
                    return packet
            cursor = self.repository.last_success_id(family, source="observe") or 0
            lost = self.repository.last_success_id(family, source="retained_floor") or 0
            rows = self._events(
                conn, family, "c.id>?", (cursor,), self.settings.batch_events + 1
            )
            if not rows:
                return {
                    "status": "empty",
                    "lost_before_id": lost if lost > cursor else None,
                }
            has_more = len(rows) > self.settings.batch_events
            rows = rows[: self.settings.batch_events]
            context = list(
                reversed(self._events(conn, family, "c.id<=?", (cursor,), 8, "DESC"))
            )
            packet = {
                "status": "ok",
                "batch_id": uuid.uuid4().hex,
                "events": [],
                "context": [],
                "attributes": {},
                "known": [],
                "has_more": has_more,
                "lost_before_id": lost if lost > cursor else None,
                "history": "retained_only",
                "actor": "unknown",
                "timezone": self.settings.timezone,
            }
            for label, selected in (("events", rows), ("context", context)):
                for row in selected:
                    key = _attribute_id(row)
                    packet["attributes"][key] = _attribute(row)
                    catalog_family, labels = self.catalog
                    identity = (row["did"], row["path"], row["policy_version"])
                    if catalog_family == family and identity in labels:
                        packet["attributes"][key]["current_labels"] = labels[identity]
                    packet[label].append(
                        {
                            "id": row["id"],
                            "attribute": key,
                            "at_ms": row["at_ms"],
                            "scope": row["scope"],
                            "at": datetime.fromtimestamp(
                                row["at_ms"] / 1000, self.zone
                            ).isoformat(),
                            "old": json.loads(row["old_json"]),
                            "new": json.loads(row["new_json"]),
                            "transport": row["source"],
                            "coverage": "healthy"
                            if self._covered(conn, family, [row], [row])
                            else "unknown",
                        }
                    )
            for known in conn.execute(
                "SELECT * FROM observe_commonality WHERE family_id=? ORDER BY updated_ms DESC LIMIT ?",
                (family, self.settings.max_observations),
            ):
                attributes = sorted(json.loads(known["attributes"]))
                if set(attributes).issubset(packet["attributes"]):
                    packet["known"].append(
                        {
                            "observation_id": known["id"],
                            "description": known["description"],
                            "attributes": attributes,
                            "uncertainty": known["uncertainty"],
                        }
                    )
                    if len(packet["known"]) == 8:
                        break
            while len(_json(packet).encode()) > self.settings.batch_bytes - 128:
                if packet["known"]:
                    packet["known"].pop()
                elif packet["context"]:
                    packet["context"].pop(0)
                elif len(packet["events"]) > 1:
                    packet["events"].pop()
                    packet["has_more"] = True
                else:
                    raise ValueError("event exceeds fact packet budget")
                used = {
                    event["attribute"] for event in packet["events"] + packet["context"]
                }
                packet["attributes"] = {
                    key: value
                    for key, value in packet["attributes"].items()
                    if key in used
                }
            conn.execute(
                "INSERT INTO observe_batch(family_id,batch_id,scope,created_ms,payload) VALUES(?,?,?,?,?) "
                "ON CONFLICT(family_id) DO UPDATE SET batch_id=excluded.batch_id,scope=excluded.scope,"
                "created_ms=excluded.created_ms,payload=excluded.payload,request_hash=NULL,receipt=NULL",
                (family, packet["batch_id"], scope, now_ms, _json(packet)),
            )
            return packet

    def _packet_valid(self, conn, family, packet):
        expected = {
            event["id"]: event["attribute"]
            for event in packet["events"] + packet["context"]
        }
        placeholders = ",".join("?" for _ in expected)
        rows = self._events(conn, family, f"c.id IN ({placeholders})", tuple(expected))
        return {row["id"]: _attribute_id(row) for row in rows} == expected

    @staticmethod
    def _covered(conn, family, rows, attributes):
        if len({row["scope"] for row in rows}) != 1:
            return False
        start = min(row["at_ms"] for row in rows)
        end = max(row["at_ms"] for row in rows) + 1
        scope = rows[0]["scope"]
        for attribute in attributes:
            identity = (
                family,
                attribute["did"],
                attribute["path"],
                attribute["policy_version"],
                scope,
            )
            if conn.execute(
                "SELECT 1 FROM observation_gap WHERE family_id=? AND scope=? AND replay=0 "
                "AND at_ms>=? AND at_ms<? AND (path IS NULL OR path=?) LIMIT 1",
                (family, scope, start, end, attribute["path"]),
            ).fetchone():
                return False
            coverage = conn.execute(
                "SELECT start_ms,end_ms,status FROM observation_coverage "
                "WHERE family_id=? AND did=? AND path=? AND policy_version=? AND scope=? "
                "AND replay=0 AND start_ms<? AND end_ms>? ORDER BY start_ms",
                (*identity, end, start),
            ).fetchall()
            through = start
            for interval in coverage:
                if interval["status"] != "healthy" or interval["start_ms"] > through:
                    return False
                through = max(through, interval["end_ms"])
            if through < end:
                return False
        return True

    def submit(self, family, request: Submission, *, scope, now_ms=None):
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        request_hash = _digest(request.model_dump())
        with self.repository.transaction() as conn:
            batch = conn.execute(
                "SELECT * FROM observe_batch WHERE family_id=? AND batch_id=?",
                (family, request.batch_id),
            ).fetchone()
            if (
                not batch
                or batch["scope"] != scope
                or not 0 <= now_ms - batch["created_ms"] < 86400000
            ):
                raise ValueError("unknown, expired or stale batch")
            if batch["receipt"]:
                if batch["request_hash"] != request_hash:
                    raise ValueError("batch already submitted with different content")
                return json.loads(batch["receipt"])
            packet = json.loads(batch["payload"])
            if not self._packet_valid(conn, family, packet):
                raise ValueError(
                    "evidence expired or attribute changed; read a new batch"
                )
            allowed = {event["id"] for event in packet["events"] + packet["context"]}
            result = []
            for commonality in request.observations:
                attributes = sorted(set(commonality.attributes))
                if not set(attributes).issubset(packet["attributes"]):
                    raise ValueError("unknown evidence attribute")
                fingerprint = _digest([commonality.description, attributes])
                if commonality.observation_id:
                    existing = conn.execute(
                        "SELECT * FROM observe_commonality WHERE family_id=? AND id=?",
                        (family, commonality.observation_id),
                    ).fetchone()
                    if not existing or existing["fingerprint"] != fingerprint:
                        raise ValueError("observation definition is immutable")
                else:
                    existing = conn.execute(
                        "SELECT * FROM observe_commonality WHERE family_id=? AND fingerprint=?",
                        (family, fingerprint),
                    ).fetchone()
                observation_id = existing["id"] if existing else uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO observe_commonality(id,family_id,fingerprint,description,attributes,uncertainty,updated_ms) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET uncertainty=excluded.uncertainty,updated_ms=excluded.updated_ms",
                    (
                        observation_id,
                        family,
                        fingerprint,
                        commonality.description,
                        _json({key: packet["attributes"][key] for key in attributes}),
                        commonality.uncertainty,
                        now_ms,
                    ),
                )
                for episode in commonality.episodes:
                    identifiers = sorted(set(episode.event_ids))
                    if not set(identifiers).issubset(allowed):
                        raise ValueError("evidence must reference this batch")
                    placeholders = ",".join("?" for _ in identifiers)
                    rows = self._events(
                        conn, family, f"c.id IN ({placeholders})", identifiers
                    )
                    start = min(row["at_ms"] for row in rows)
                    if max(row["at_ms"] for row in rows) - start > 86400000:
                        raise ValueError("evidence episode exceeds one day")
                    present = {_attribute_id(row) for row in rows}
                    if not present.issubset(attributes) or (
                        episode.kind == "support" and present != set(attributes)
                    ):
                        raise ValueError(
                            "evidence does not match observation attributes"
                        )
                    attribute_rows = []
                    for key in attributes:
                        entry = packet["attributes"][key]
                        attribute_rows.append(
                            {
                                "did": entry["did"],
                                "path": entry["path"],
                                "policy_version": entry["version"],
                            }
                        )
                    qualified = self._covered(conn, family, rows, attribute_rows)
                    conn.execute(
                        "INSERT INTO observe_evidence(family_id,observation_id,evidence_key,event_ids,at_ms,kind,qualified) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(observation_id,evidence_key) DO UPDATE SET "
                        "kind=CASE WHEN kind='difference' OR excluded.kind='difference' THEN 'difference' "
                        "WHEN kind<>excluded.kind THEN 'unknown' ELSE kind END, qualified=MIN(qualified,excluded.qualified)",
                        (
                            family,
                            observation_id,
                            _digest(identifiers),
                            _json(identifiers),
                            start,
                            episode.kind,
                            int(qualified),
                        ),
                    )
                result.append(observation_id)
            for table, capacity in (
                ("observe_commonality", self.settings.max_observations),
                ("observe_evidence", self.settings.max_evidence),
            ):
                if (
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    > capacity
                ):
                    raise ValueError("observation ledger capacity exceeded")
            for observation_id in set(result):
                qualification = self.state(family, observation_id)
                conn.execute(
                    "UPDATE observe_commonality SET qualification=? WHERE family_id=? AND id=?",
                    (_json(qualification), family, observation_id),
                )
            receipt = {"status": "accepted", "observation_ids": result}
            conn.execute(
                "UPDATE observe_batch SET request_hash=?,receipt=? WHERE family_id=?",
                (request_hash, _json(receipt), family),
            )
            conn.execute(
                "INSERT INTO observation_cursor(family_id,source,record_id) VALUES(?,'observe',?) "
                "ON CONFLICT(family_id,source) DO UPDATE SET record_id=MAX(record_id,excluded.record_id)",
                (family, packet["events"][-1]["id"]),
            )
            return receipt

    def state(self, family, observation_id):
        dates = {kind: set() for kind in ("support", "difference", "unknown")}
        with self.repository.transaction() as conn:
            commonality = conn.execute(
                "SELECT attributes FROM observe_commonality WHERE family_id=? AND id=?",
                (family, observation_id),
            ).fetchone()
            attributes = []
            if commonality:
                for definition in json.loads(commonality["attributes"]).values():
                    attributes.append(
                        {
                            "did": definition["did"],
                            "path": definition["path"],
                            "policy_version": definition["version"],
                        }
                    )
            for evidence in conn.execute(
                "SELECT * FROM observe_evidence WHERE family_id=? AND observation_id=?",
                (family, observation_id),
            ).fetchall():
                identifiers = json.loads(evidence["event_ids"])
                placeholders = ",".join("?" for _ in identifiers)
                rows = self._events(
                    conn, family, f"c.id IN ({placeholders})", identifiers
                )
                qualified = (
                    evidence["qualified"]
                    and len(rows) == len(identifiers)
                    and self._covered(conn, family, rows, attributes)
                )
                kind = evidence["kind"] if qualified else "unknown"
                dates[kind].add(
                    datetime.fromtimestamp(evidence["at_ms"] / 1000, self.zone).date()
                )
        effective = dates["support"] | dates["difference"]
        span = (max(effective) - min(effective)).days + 1 if effective else 0
        maturity = "collecting"
        if len(dates["support"]) >= self.settings.accumulated_days:
            maturity = "accumulated"
            if (
                len(effective) >= self.settings.review_days
                and span >= self.settings.review_days
            ):
                maturity = "reviewable"
        if dates["difference"]:
            maturity = "contested"
        return {
            "support_days": len(dates["support"]),
            "difference_days": len(dates["difference"]),
            "unknown_days": len(dates["unknown"]),
            "effective_days": len(effective),
            "span_days": span,
            "maturity": maturity,
            "action": "none",
        }
