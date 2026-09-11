"""
Perception log repo — SQLite persistence with adjacent deduplication.

Each perception cycle increments the inference counter, but only inserts
a new row when the descriptions dict differs from the previous entry.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from miloco.database.connector import get_db_connector
from miloco.utils.time_utils import now_ms

if TYPE_CHECKING:
    from miloco.perception.schema import PerceptionLogEntry

logger = logging.getLogger(__name__)


class PerceptionLogRepo:
    """Data access object for the perception_log table."""

    def __init__(self):
        self.db_connector = get_db_connector()
        # In-memory dedup state
        self._last_descriptions: dict[str, str] | None = None
        # Daily inference counter (resets on date boundary)
        self._today_date: str = ""
        self._today_inference_count: int = 0

    def _check_date_boundary(self) -> None:
        """Reset daily inference counter if date has changed."""
        today = date.today().isoformat()
        if today != self._today_date:
            self._today_date = today
            self._today_inference_count = 0

    def get_today_inference_count(self) -> int:
        self._check_date_boundary()
        return self._today_inference_count

    def append(self, entry: PerceptionLogEntry) -> bool:
        """Append a perception log entry.

        Always increments inference count. Skips DB insert if descriptions
        are identical to the previous entry (adjacent dedup).

        Returns:
            True if a new row was inserted, False if deduplicated.
        """
        self._check_date_boundary()
        self._today_inference_count += 1

        # Adjacent dedup: skip insert if descriptions unchanged
        if (
            self._last_descriptions is not None
            and entry.descriptions == self._last_descriptions
        ):
            logger.debug("Perception log deduplicated, skipping insert: %s", entry.id)
            return False

        self._last_descriptions = entry.descriptions.copy()

        try:
            sql = """
                INSERT INTO perception_log (id, timestamp, descriptions, created_at)
                VALUES (?, ?, ?, ?)
            """
            params = (
                entry.id or str(uuid.uuid4()),
                entry.timestamp,
                json.dumps(entry.descriptions, ensure_ascii=False),
                now_ms(),
            )
            with self.db_connector.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()

            logger.debug("Perception log inserted: %s", entry.id)
            return True

        except Exception as e:
            logger.error("Failed to insert perception log: %s", e)
            return False

    def query(
        self,
        after_ms: int | None = None,
        before_ms: int | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Query perception logs with time filters.

        Args:
            after_ms: Exclusive lower bound. Use with limit=None for forward cursor reads.
            before_ms: Upper bound — only return entries with timestamp < before_ms.
            since_ms: Absolute ms timestamp — only return entries with timestamp >= since_ms.
            limit: Return the latest N entries in the filtered window. None means no limit.

        Returns:
            (logs, count) where logs are dicts with "t" (ISO 8601) and "d" keys.
            ``logs`` is always ordered oldest-first, with or without ``limit``,
            so ``logs[-1]`` is the newest returned row.
        """
        from miloco.utils.time_utils import ms_to_iso_local

        try:
            conditions = []
            params: list[Any] = []

            if after_ms is not None:
                conditions.append("timestamp > ?")
                params.append(after_ms)
            elif since_ms is not None:
                conditions.append("timestamp >= ?")
                params.append(since_ms)

            if before_ms is not None:
                conditions.append("timestamp < ?")
                params.append(before_ms)

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            # limit 语义是“窗口内最新 N 条”：先按 DESC 从最新端取 N 条。
            # 不带 limit 时仍走 ASC，保持原有增量游标查询的顺序。
            order = "DESC" if limit is not None else "ASC"
            limit_clause = "LIMIT ?" if limit is not None else ""
            sql = f"""
                SELECT id, timestamp, descriptions
                FROM perception_log
                {where}
                ORDER BY timestamp {order}
                {limit_clause}
            """
            if limit is not None:
                params.append(limit)

            results = self.db_connector.execute_query(sql, tuple(params))
            # 补偿上面的 DESC；对外返回顺序始终为升序。
            if limit is not None:
                results = list(reversed(results))

            logs = []
            for row in results:
                logs.append(
                    {
                        # 透 id 让前端 ActivityFeed 能用稳定 React key（同 rule_logs）；
                        # 否则前端走 fallback `pl_<t>_<i>` 拼装，分页 / reload 会让
                        # 同一条 perception_log（同 t 同 i）保留 React 内部 state。
                        "id": row["id"],
                        "t": ms_to_iso_local(row["timestamp"]),
                        "d": json.loads(row["descriptions"])
                        if isinstance(row["descriptions"], str)
                        else row["descriptions"],
                    }
                )

            return logs, len(logs)

        except Exception as e:
            logger.error("Failed to query perception logs: %s", e)
            return [], 0

    def count_all(self) -> int:
        """Get total count of perception log entries."""
        try:
            sql = "SELECT COUNT(*) as count FROM perception_log"
            results = self.db_connector.execute_query(sql)
            return results[0]["count"] if results else 0
        except Exception as e:
            logger.error("Failed to count perception logs: %s", e)
            return 0

    def delete_before_days(self, days: int) -> int:
        """Delete perception logs older than N days.

        Returns:
            Number of deleted rows.
        """
        try:
            cutoff_ms = int((datetime.now().timestamp() - days * 86400) * 1000)
            sql = "DELETE FROM perception_log WHERE timestamp < ?"
            return self.db_connector.execute_update(sql, (cutoff_ms,))
        except Exception as e:
            logger.error("Failed to delete old perception logs: %s", e)
            return 0

