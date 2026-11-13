from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from miloco.iot_observation.models import (
    OBSERVATION_MISSING,
    ObservationAttribute,
    ObservationChange,
    ObservationGap,
)

SCHEMA_VERSION = 3


def _pack(value: Any) -> dict[str, Any]:
    if value is OBSERVATION_MISSING or type(value).__name__ == "MissingType":
        return {"type": "missing"}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        return {"type": "int", "value": value}
    if type(value) is float:
        return {"type": "float", "value": value}
    if type(value) is str:
        return {"type": "str", "value": value}
    if value is None:
        return {"type": "none", "value": None}
    if isinstance(value, tuple):
        return {"type": "tuple", "value": [_pack(item) for item in value]}
    raise TypeError(f"unsupported observation value: {type(value).__name__}")


def _unpack(raw: str) -> Any:
    item = json.loads(raw)
    value_type = item["type"]
    if value_type == "missing":
        return OBSERVATION_MISSING
    if value_type == "tuple":
        return tuple(_unpack(json.dumps(child)) for child in item["value"])
    return item.get("value")


def _same(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, tuple):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


class ObservationRepository:
    def __init__(
        self,
        path: str | Path,
        *,
        max_changes: int = 200_000,
        max_coverage: int = 100_000,
        max_attributes: int = 4_096,
        hard_bytes: int = 160 * 1024 * 1024,
        soft_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self.path = Path(path)
        self.max_changes = max_changes
        self.max_coverage = max_coverage
        self.max_attributes = max_attributes
        self.hard_bytes = hard_bytes
        self.soft_bytes = min(soft_bytes, hard_bytes * 4 // 5)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._depth = 0

    def open(self) -> ObservationRepository:
        with self._lock:
            if self._conn is not None:
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA wal_autocheckpoint=256")
            conn.execute("PRAGMA journal_size_limit=1048576")
            conn.execute(f"PRAGMA max_page_count={max(1, self.soft_bytes // 4096)}")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='observation_meta'").fetchone():
                version = conn.execute("SELECT value FROM observation_meta WHERE key='schema_version'").fetchone()
                if version is None or version[0] not in {"2", str(SCHEMA_VERSION)}:
                    conn.close()
                    raise RuntimeError("unsupported observation schema")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observation_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observation_attribute (
                    family_id TEXT NOT NULL,
                    did TEXT NOT NULL,
                    path TEXT NOT NULL,
                    service_type_name TEXT NOT NULL,
                    type_name TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_seen_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (family_id, did, path, policy_version)
                );
                CREATE TABLE IF NOT EXISTS observation_baseline (
                    family_id TEXT NOT NULL,
                    did TEXT NOT NULL,
                    path TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    scope INTEGER NOT NULL,
                    value_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    at_ms INTEGER NOT NULL,
                    PRIMARY KEY (family_id, did, path, policy_version)
                );
                CREATE TABLE IF NOT EXISTS observation_change (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    family_id TEXT NOT NULL,
                    did TEXT NOT NULL,
                    path TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    scope INTEGER NOT NULL,
                    old_json TEXT NOT NULL,
                    new_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    at_ms INTEGER NOT NULL,
                    replay INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS observation_change_lookup
                    ON observation_change (family_id, at_ms, id);
                CREATE INDEX IF NOT EXISTS observation_change_cursor
                    ON observation_change (family_id, replay, id);
                CREATE TABLE IF NOT EXISTS observation_coverage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    family_id TEXT NOT NULL,
                    did TEXT NOT NULL,
                    path TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    scope INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    replay INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS observation_coverage_lookup
                    ON observation_coverage (family_id, start_ms, end_ms);
                CREATE TABLE IF NOT EXISTS observation_gap (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    family_id TEXT NOT NULL,
                    scope INTEGER NOT NULL,
                    path TEXT,
                    reason TEXT NOT NULL,
                    at_ms INTEGER NOT NULL,
                    replay INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS observation_run (
                    run_id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    started_at_ms INTEGER NOT NULL,
                    finished_at_ms INTEGER,
                    status TEXT,
                    watermark_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS observation_cursor (
                    family_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    record_id INTEGER NOT NULL,
                    PRIMARY KEY (family_id, source)
                );
                CREATE TABLE IF NOT EXISTS observe_batch (
                    family_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    scope INTEGER NOT NULL,
                    created_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    request_hash TEXT,
                    receipt TEXT
                );
                CREATE TABLE IF NOT EXISTS observe_commonality (
                    id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    description TEXT NOT NULL,
                    attributes TEXT NOT NULL,
                    uncertainty TEXT NOT NULL,
                    qualification TEXT,
                    updated_ms INTEGER NOT NULL,
                    UNIQUE (family_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS observe_evidence (
                    family_id TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    evidence_key TEXT NOT NULL,
                    event_ids TEXT NOT NULL,
                    at_ms INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    qualified INTEGER NOT NULL,
                    PRIMARY KEY (observation_id, evidence_key)
                );
                CREATE INDEX IF NOT EXISTS observe_evidence_family
                    ON observe_evidence (family_id, observation_id, at_ms);
                """
            )
            conn.execute(
                "INSERT INTO observation_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
            self._conn = conn
        return self

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("observation repository is not open")
        return self._conn

    @contextmanager
    def deadline(self, seconds: float):
        end = time.monotonic() + seconds
        if not self._lock.acquire(timeout=max(0, seconds)):
            raise TimeoutError("observation database busy")
        try:
            conn = self._db()
            conn.set_progress_handler(lambda: int(time.monotonic() >= end), 1000)
            try:
                yield
            finally:
                conn.set_progress_handler(None, 0)
        finally:
            self._lock.release()

    @contextmanager
    def transaction(self):
        with self._lock:
            conn = self._db()
            outer = self._depth == 0
            if outer:
                if self._size_bytes() > self.soft_bytes:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if self._size_bytes() + 1024 * 1024 > self.hard_bytes:
                    raise RuntimeError("observation disk budget exhausted")
                conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield conn
                if outer:
                    conn.commit()
            except BaseException:
                if outer:
                    conn.rollback()
                raise
            finally:
                self._depth -= 1

    def _register_attribute(self, conn: sqlite3.Connection, family_id: str, attribute: ObservationAttribute) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO observation_attribute
            (family_id, did, path, service_type_name, type_name, policy_version)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                family_id,
                attribute.did,
                attribute.path,
                attribute.service_type_name,
                attribute.type_name,
                attribute.policy_version,
            ),
        )
        count = conn.execute("SELECT COUNT(*) FROM observation_attribute").fetchone()[0]
        if count > self.max_attributes:
            raise RuntimeError("observation metadata capacity exceeded")

    def record_baseline(
        self,
        family_id: str,
        attribute: ObservationAttribute,
        value: Any,
        *,
        scope: int,
        source: str,
        at_ms: int,
    ) -> None:
        with self.transaction() as conn:
            self._register_attribute(conn, family_id, attribute)
            conn.execute(
                "UPDATE observation_attribute SET last_seen_ms=MAX(last_seen_ms, ?) "
                "WHERE family_id=? AND did=? AND path=? AND policy_version=?",
                (at_ms, family_id, *attribute.identity),
            )
            conn.execute(
                """
                INSERT INTO observation_baseline
                (family_id, did, path, policy_version, scope, value_json, source, at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(family_id, did, path, policy_version) DO UPDATE SET
                    scope=excluded.scope, value_json=excluded.value_json,
                    source=excluded.source, at_ms=excluded.at_ms
                """,
                (
                    family_id,
                    attribute.did,
                    attribute.path,
                    attribute.policy_version,
                    scope,
                    json.dumps(_pack(value), separators=(",", ":")),
                    source,
                    at_ms,
                ),
            )

    def record_change(self, family_id: str, change: ObservationChange, *, replay: bool = False) -> bool:
        if _same(change.old, change.new):
            return False
        with self.transaction() as conn:
            self._register_attribute(conn, family_id, change.attribute)
            conn.execute(
                    """
                    INSERT INTO observation_change
                    (family_id, did, path, policy_version, scope, old_json, new_json, source, at_ms, replay)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        family_id,
                        change.attribute.did,
                        change.attribute.path,
                        change.attribute.policy_version,
                        change.scope,
                        json.dumps(_pack(change.old), separators=(",", ":")),
                        json.dumps(_pack(change.new), separators=(",", ":")),
                        change.source,
                        change.at_ms,
                        int(replay),
                    ),
                )
            self.record_baseline(family_id, change.attribute, change.new,
                                 scope=change.scope, source=change.source, at_ms=change.at_ms)
            self._trim_changes(conn)
            return True

    def record_gap(self, family_id: str, gap: ObservationGap, *, replay: bool = False) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO observation_gap(family_id, scope, path, reason, at_ms, replay) VALUES (?, ?, ?, ?, ?, ?)",
                (family_id, gap.scope, gap.path, gap.reason, gap.at_ms, int(replay)),
            )
            self._trim_table(conn, "observation_gap", 4096)

    def record_coverage(
        self,
        family_id: str,
        *,
        attribute: ObservationAttribute,
        scope: int,
        start_ms: int,
        end_ms: int,
        status: str,
        reason: str | None = None,
        replay: bool = False,
    ) -> None:
        if end_ms <= start_ms or status not in {"healthy", "unknown"}:
            raise ValueError("invalid coverage interval")
        with self.transaction() as conn:
            self._register_attribute(conn, family_id, attribute)
            previous = conn.execute(
                "SELECT id FROM observation_coverage WHERE family_id=? AND did=? AND path=? "
                "AND policy_version=? AND scope=? AND status=? AND replay=? AND end_ms=? ORDER BY id DESC LIMIT 1",
                (family_id, *attribute.identity, scope, status, int(replay), start_ms),
            ).fetchone()
            if previous:
                conn.execute("UPDATE observation_coverage SET end_ms=? WHERE id=?", (end_ms, previous[0]))
                return
            conn.execute(
                "INSERT INTO observation_coverage(family_id, did, path, policy_version, scope, start_ms, end_ms, status, reason, replay) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (family_id, *attribute.identity, scope, start_ms, end_ms, status, reason, int(replay)),
            )
            self._trim_table(conn, "observation_coverage", self.max_coverage)

    def begin_run(self, run_id: str, family_id: str, *, started_at_ms: int, source: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO observation_run(run_id, family_id, source, started_at_ms) VALUES (?, ?, ?, ?)",
                (run_id, family_id, source, started_at_ms),
            )
            conn.execute("DELETE FROM observation_run WHERE rowid <= (SELECT MAX(rowid)-1024 FROM observation_run)")

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at_ms: int,
        watermark_id: int | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE observation_run SET finished_at_ms=?, status=?, watermark_id=? WHERE run_id=?",
                (finished_at_ms, status, watermark_id, run_id),
            )
            if status in {"ok", "empty"} and watermark_id is not None:
                conn.execute(
                    "INSERT INTO observation_cursor(family_id, source, record_id) "
                    "SELECT family_id, source, ? FROM observation_run WHERE run_id=? "
                    "ON CONFLICT(family_id, source) DO UPDATE SET record_id=MAX(record_id, excluded.record_id)",
                    (watermark_id, run_id),
                )

    def last_success_id(self, family_id: str, *, source: str = "live") -> int | None:
        with self._lock:
            row = self._db().execute(
                "SELECT record_id FROM observation_cursor WHERE family_id=? AND source=?",
                (family_id, source),
            ).fetchone()
        return row[0] if row else None

    def recent_changes(
        self,
        family_id: str,
        *,
        since_ms: int,
        until_ms: int,
        replay: bool = False,
        after_id: int = 0,
        cutoff_id: int = 9223372036854775807,
    ) -> list[ObservationChange]:
        with self._lock:
            rows = self._db().execute(
                """
                SELECT did, path, service_type_name, type_name, policy_version,
                       old_json, new_json, source, at_ms, scope, c.id
                FROM observation_change AS c
                JOIN observation_attribute AS a USING (family_id, did, path, policy_version)
                WHERE c.family_id=? AND c.at_ms>=? AND c.at_ms<? AND c.replay=?
                    AND c.id>? AND c.id<=? AND a.active=1
                ORDER BY c.id
                """,
                (family_id, since_ms, until_ms, int(replay), after_id, cutoff_id),
            ).fetchall()
        return [
            ObservationChange(
                ObservationAttribute(
                    row["did"], row["path"], row["service_type_name"], row["type_name"], row["policy_version"]
                ),
                _unpack(row["old_json"]),
                _unpack(row["new_json"]),
                row["source"],
                row["at_ms"],
                row["scope"],
                row["id"],
            )
            for row in rows
        ]

    def analysis_snapshot(self, family_id: str, *, history_start_ms: int, now_ms: int, replay: bool = False):
        with self._lock:
            conn = self._db()
            cutoff = conn.execute("SELECT COALESCE(MAX(id),0) FROM observation_change WHERE family_id=? AND replay=?",
                                  (family_id, int(replay))).fetchone()[0]
            last = self.last_success_id(family_id, source="replay" if replay else "live")
            recent = self.recent_changes(
                family_id, since_ms=now_ms - 7200000 if last is None else 0,
                until_ms=9223372036854775807, replay=replay, after_id=last or 0, cutoff_id=cutoff,
            )
            history = self.recent_changes(family_id, since_ms=history_start_ms, until_ms=now_ms + 1,
                                          replay=replay, cutoff_id=cutoff)
            coverage = conn.execute(
                "SELECT * FROM observation_coverage WHERE family_id=? AND replay=? AND end_ms>? AND start_ms<? ORDER BY start_ms",
                (family_id, int(replay), history_start_ms, now_ms),
            ).fetchall()
            gaps = conn.execute(
                "SELECT * FROM observation_gap WHERE family_id=? AND replay=? AND at_ms>=? AND at_ms<=?",
                (family_id, int(replay), history_start_ms, now_ms),
            ).fetchall()
            return recent, history, coverage, gaps, cutoff

    def set_active_attributes(self, family_id: str, attributes: list[ObservationAttribute], *, at_ms: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE observation_attribute SET active=0 WHERE family_id=?", (family_id,))
            for attribute in attributes:
                self._register_attribute(conn, family_id, attribute)
                conn.execute("UPDATE observation_attribute SET active=1, last_seen_ms=? WHERE family_id=? AND did=? AND path=? AND policy_version=?",
                             (at_ms, family_id, *attribute.identity))

    def heartbeat(self, at_ms: int) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO observation_meta(key, value) VALUES('heartbeat', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(at_ms),))

    def forget_account(self, account_id: str) -> None:
        with self.transaction() as conn:
            for table in ("observation_attribute", "observation_baseline", "observation_change",
                          "observation_coverage", "observation_gap", "observation_run", "observation_cursor",
                          "observe_batch", "observe_commonality", "observe_evidence"):
                families = conn.execute(f"SELECT DISTINCT family_id FROM {table}").fetchall()
                for row in families:
                    try:
                        identity = json.loads(row[0])
                    except ValueError:
                        continue
                    if isinstance(identity, list) and identity and identity[0] == account_id:
                        conn.execute(f"DELETE FROM {table} WHERE family_id=?", (row[0],))

    def attribute_count(self) -> int:
        with self._lock:
            return self._db().execute("SELECT COUNT(*) FROM observation_attribute").fetchone()[0]

    def change_count(self, family_id: str | None = None) -> int:
        with self._lock:
            if family_id is None:
                return self._db().execute("SELECT COUNT(*) FROM observation_change").fetchone()[0]
            return self._db().execute(
                "SELECT COUNT(*) FROM observation_change WHERE family_id=?", (family_id,)
            ).fetchone()[0]

    def prune(self, *, before_ms: int | None = None) -> None:
        with self.transaction() as conn:
            if before_ms is not None:
                self._record_eviction(conn, "at_ms < ?", (before_ms,))
                conn.execute("DELETE FROM observation_change WHERE at_ms < ?", (before_ms,))
                conn.execute("DELETE FROM observation_coverage WHERE end_ms < ?", (before_ms,))
                conn.execute("DELETE FROM observation_gap WHERE at_ms < ?", (before_ms,))
                conn.execute("DELETE FROM observation_run WHERE started_at_ms < ?", (before_ms,))
                conn.execute("DELETE FROM observation_baseline WHERE at_ms < ?", (before_ms,))
                conn.execute("DELETE FROM observation_attribute WHERE active=0 AND last_seen_ms < ?", (before_ms,))
                conn.execute("DELETE FROM observe_evidence WHERE at_ms < ?", (before_ms,))
                conn.execute("DELETE FROM observe_commonality WHERE updated_ms < ?", (before_ms,))
                conn.execute("DELETE FROM observe_evidence WHERE observation_id NOT IN (SELECT id FROM observe_commonality)")
                conn.execute("DELETE FROM observe_batch WHERE created_ms < ?", (before_ms,))
            self._trim_changes(conn)

    def _trim_changes(self, conn: sqlite3.Connection) -> None:
        self._record_eviction(conn, "id <= (SELECT MAX(id)-? FROM observation_change)", (self.max_changes,))
        self._trim_table(conn, "observation_change", self.max_changes)

    @staticmethod
    def _record_eviction(conn, condition, parameters):
        conn.execute(
            "INSERT INTO observation_cursor(family_id, source, record_id) "
            "SELECT family_id, 'retained_floor', MAX(id) FROM observation_change NOT INDEXED "
            f"WHERE replay=0 AND {condition} GROUP BY family_id "
            "ON CONFLICT(family_id, source) DO UPDATE SET record_id=MAX(record_id, excluded.record_id)",
            parameters,
        )

    @staticmethod
    def _trim_table(conn, table, capacity):
        conn.execute(f"DELETE FROM {table} WHERE id <= (SELECT MAX(id)-? FROM {table})", (capacity,))

    def _size_bytes(self) -> int:
        total = self.path.stat().st_size if self.path.exists() else 0
        wal = Path(f"{self.path}-wal")
        return total + (wal.stat().st_size if wal.exists() else 0)
