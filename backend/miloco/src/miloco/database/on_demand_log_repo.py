"""
On-demand perception query log repo — SQLite persistence.

Stores every on-demand perception query (question + answer + metadata)
for later retrieval via the web dashboard.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from miloco.database.connector import get_db_connector
from miloco.utils.time_utils import now_ms

if TYPE_CHECKING:
    from miloco.perception.schema import OnDemandLogEntry

logger = logging.getLogger(__name__)


class OnDemandLogRepo:
    """Data access object for the on_demand_log table."""

    def __init__(self):
        self.db_connector = get_db_connector()

    @staticmethod
    def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
        sources = row["sources"]
        if isinstance(sources, str):
            sources = json.loads(sources)
        clip_dids = row.get("clip_dids", "[]")
        if isinstance(clip_dids, str):
            clip_dids = json.loads(clip_dids)
        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "query": row["query"],
            "answer": row["answer"],
            "sources": sources,
            "latency_ms": row["latency_ms"],
            "snapshot_count": row.get("snapshot_count", 0),
            "clip_dids": clip_dids,
            "clip_kinds": json.loads(row.get("clip_kinds", "{}")),
            "has_trace": bool(row.get("has_trace", 0)),
        }

    def append(self, entry: OnDemandLogEntry) -> bool:
        """Insert an on-demand query log entry.

        Returns:
            True if inserted, False on error.
        """
        try:
            sql = """
                INSERT INTO on_demand_log
                (id, timestamp, query, answer, sources, latency_ms,
                 snapshot_count, clip_dids, clip_kinds, has_trace, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                entry.id or str(uuid.uuid4()),
                entry.timestamp,
                entry.query,
                entry.answer,
                json.dumps(entry.sources, ensure_ascii=False),
                entry.latency_ms,
                entry.snapshot_count,
                json.dumps(entry.clip_dids, ensure_ascii=False),
                json.dumps(entry.clip_kinds, ensure_ascii=False),
                1 if entry.has_trace else 0,
                now_ms(),
            )
            with self.db_connector.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()

            logger.debug("On-demand log inserted: %s", entry.id)
            return True

        except Exception as e:
            logger.error("Failed to insert on-demand log: %s", e)
            return False

    def query(
        self,
        after_ms: int | None = None,
        before_ms: int | None = None,
        before_id: str | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Query on-demand logs with time filters.

        Returns:
            (logs, count) where logs are dicts ready for API serialization.
        """
        try:
            conditions: list[str] = []
            params: list[Any] = []

            if after_ms is not None:
                conditions.append("timestamp > ?")
                params.append(after_ms)
            elif since_ms is not None:
                conditions.append("timestamp >= ?")
                params.append(since_ms)

            if before_ms is not None:
                if before_id is not None:
                    conditions.append("(timestamp < ? OR (timestamp = ? AND id < ?))")
                    params.extend([before_ms, before_ms, before_id])
                else:
                    conditions.append("timestamp < ?")
                    params.append(before_ms)

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            limit_clause = "LIMIT ?" if limit is not None else ""
            sql = f"""
                SELECT id, timestamp, query, answer, sources, latency_ms,
                       snapshot_count, clip_dids, clip_kinds, has_trace
                FROM on_demand_log
                {where}
                ORDER BY timestamp DESC, id DESC
                {limit_clause}
            """
            if limit is not None:
                params.append(limit)

            results = self.db_connector.execute_query(sql, tuple(params))

            logs = [self._row_to_dict(row) for row in results]

            return logs, len(logs)

        except Exception as e:
            logger.error("Failed to query on-demand logs: %s", e)
            return [], 0

    def get_by_id(self, log_id: str) -> dict[str, Any] | None:
        """Get a single on-demand log entry by ID."""
        try:
            sql = """
                SELECT id, timestamp, query, answer, sources, latency_ms,
                       snapshot_count, clip_dids, clip_kinds, has_trace
                FROM on_demand_log WHERE id = ?
            """
            results = self.db_connector.execute_query(sql, (log_id,))
            if not results:
                return None
            return self._row_to_dict(results[0])
        except Exception as e:
            logger.error("Failed to get on-demand log by id: %s", e)
            return None

    def count_all(self) -> int:
        """Get total count of on-demand log entries."""
        try:
            sql = "SELECT COUNT(*) as count FROM on_demand_log"
            results = self.db_connector.execute_query(sql)
            return results[0]["count"] if results else 0
        except Exception as e:
            logger.error("Failed to count on-demand logs: %s", e)
            return 0

    def delete_before_days(self, days: int) -> int:
        """Delete on-demand logs older than N days.

        Returns:
            Number of deleted rows.
        """
        try:
            cutoff_ms = int((datetime.now().timestamp() - days * 86400) * 1000)
            sql = "DELETE FROM on_demand_log WHERE timestamp < ?"
            return self.db_connector.execute_update(sql, (cutoff_ms,))
        except Exception as e:
            logger.error("Failed to delete old on-demand logs: %s", e)
            return 0
