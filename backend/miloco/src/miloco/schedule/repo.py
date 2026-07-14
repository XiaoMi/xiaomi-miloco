# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""CronRepo — cron 表 CRUD 数据访问层.

事务原子性陷阱同 task_repo: SQLiteConnector 默认 isolation_level=None (autocommit),
需要显式 ``cursor.execute("BEGIN")`` + ``conn.commit()`` 才能构成原子事务。
"""

from __future__ import annotations

import logging
from typing import Any

from miloco.database.connector import get_db_connector
from miloco.schedule.schema import Cron
from miloco.utils.time_utils import now_ms

logger = logging.getLogger(__name__)


class CronRepo:
    def __init__(self):
        self.db = get_db_connector()

    @staticmethod
    def _row_to_cron(row: dict[str, Any]) -> Cron:
        return Cron(
            cron_id=row["cron_id"],
            task_id=row["task_id"],
            dispatch_owner=row["dispatch_owner"],
            name=row["name"],
            kind=row["kind"],
            cron_expr=row["cron_expr"],
            at_ms=row["at_ms"],
            every_ms=row["every_ms"],
            anchor_ms=row["anchor_ms"],
            tz=row["tz"],
            message=row["message"],
            light_context=bool(row["light_context"]),
            max_delay_seconds=row["max_delay_seconds"],
            enabled=bool(row["enabled"]),
            fired_at=row["fired_at"],
            retry_attempt=row["retry_attempt"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, cron_id: str) -> Cron | None:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM cron WHERE cron_id=?", (cron_id,)
            ).fetchone()
            return self._row_to_cron(dict(row)) if row else None

    def list_all(self) -> list[Cron]:
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT * FROM cron").fetchall()
            return [self._row_to_cron(dict(r)) for r in rows]

    def list_by_task(self, task_id: str) -> list[Cron]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM cron WHERE task_id=?", (task_id,)
            ).fetchall()
            return [self._row_to_cron(dict(r)) for r in rows]

    def list_orphans(self) -> list[Cron]:
        """列出 task_id 为空的孤儿 cron 行 (task 被删或迁移遗留)."""
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM cron WHERE task_id IS NULL"
            ).fetchall()
            return [self._row_to_cron(dict(r)) for r in rows]

    def list_internal(self) -> list[Cron]:
        """列出所有 internal 分区 cron (backend 自管定时器, rebuild 重放用)."""
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM cron WHERE dispatch_owner='internal'"
            ).fetchall()
            return [self._row_to_cron(dict(r)) for r in rows]

    def insert(self, cron: Cron) -> None:
        """INSERT 新 cron 行 (触发 schema CHECK + FK 约束)."""
        with self.db.get_connection() as conn:
            conn.execute(
                """INSERT INTO cron (
                    cron_id, task_id, dispatch_owner, name, kind,
                    cron_expr, at_ms, every_ms, anchor_ms, tz, message,
                    light_context, max_delay_seconds, enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cron.cron_id,
                    cron.task_id,
                    cron.dispatch_owner,
                    cron.name,
                    cron.kind,
                    cron.cron_expr,
                    cron.at_ms,
                    cron.every_ms,
                    cron.anchor_ms,
                    cron.tz,
                    cron.message,
                    int(cron.light_context),
                    cron.max_delay_seconds,
                    int(cron.enabled),
                    cron.created_at,
                    cron.updated_at,
                ),
            )
            conn.commit()

    def delete(self, cron_id: str) -> int:
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM cron WHERE cron_id=?", (cron_id,)
            )
            conn.commit()
            return cursor.rowcount

    def set_enabled(self, cron_id: str, enabled: bool) -> int:
        """UPDATE cron.enabled + updated_at. 返回 affected rows."""
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE cron SET enabled=?, updated_at=? WHERE cron_id=?",
                (int(enabled), now_ms(), cron_id),
            )
            conn.commit()
            return cursor.rowcount

    def mark_fired_and_delete(self, cron_id: str) -> int:
        """at 成功后单事务 DELETE.

        原子性由单事务保证, 无需先 UPDATE fired_at (中间态外部读者永不可见);
        runner 侧 fired_at 已填的 defensive 分支保留作两步事务未来重构的兜底。
        """
        with self.db.get_connection() as conn:
            cursor = conn.execute("DELETE FROM cron WHERE cron_id=?", (cron_id,))
            conn.commit()
            return cursor.rowcount

    def increment_retry_attempt(self, cron_id: str) -> int:
        """status=error 后 retry_attempt +1, 返回新 attempt 值 (-1 = 不存在)."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE cron SET retry_attempt=retry_attempt+1, updated_at=? "
                "WHERE cron_id=?",
                (now_ms(), cron_id),
            )
            if cursor.rowcount == 0:
                conn.commit()
                return -1
            row = cursor.execute(
                "SELECT retry_attempt FROM cron WHERE cron_id=?", (cron_id,)
            ).fetchone()
            conn.commit()
            return row["retry_attempt"] if row else -1
