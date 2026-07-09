# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task 表数据访问层 (v2)。

v2 起 task_link 表已 DROP: rule 归属由 rule.task_id FK CASCADE 表达,
cron 归属由 cron.task_id FK CASCADE 表达。task 视图数据源改成 rule / cron
两表 JOIN, 老 task_link 中转路径消失。

事务原子性陷阱: SQLiteConnector 默认 ``isolation_level=None`` (autocommit),
每条 execute 自动提交。必须显式 ``cursor.execute("BEGIN")`` + 末尾
``conn.commit()`` 才能让多条 INSERT 构成原子事务。
"""

import logging
import sqlite3
from typing import Any

from miloco.database.connector import get_db_connector
from miloco.utils.time_utils import ms_to_iso_local, now_ms

logger = logging.getLogger(__name__)


class TaskConflict(Exception):
    """409: task PK 撞库 (create_task UNIQUE 冲突)。"""


class TaskNotFound(Exception):
    """404: task 不存在 (toggle / update / delete 时读到 not_found)。"""


class TaskRepo:
    def __init__(self):
        self.db = get_db_connector()

    def create_task(self, task_id: str, description: str) -> None:
        """INSERT task 行 (占位)。rule / cron 关联挂载由后续 endpoint 完成。"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO task (task_id, description, status, created_at) "
                    "VALUES (?, ?, 'active', ?)",
                    (task_id, description, now_ms()),
                )
                conn.commit()
                logger.info("Task created (placeholder): task_id=%s", task_id)
            except sqlite3.IntegrityError as e:
                conn.rollback()
                msg = str(e)
                if "task.task_id" in msg or "UNIQUE" in msg:
                    raise TaskConflict(f"task_id {task_id!r} 已存在") from e
                raise

    def task_exists(self, task_id: str) -> bool:
        """task 表是否含此 task_id。"""
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM task WHERE task_id = ?", (task_id,)
            ).fetchone()
            return row is not None

    def get_full_view(self, task_id: str) -> dict[str, Any] | None:
        """单 task 视图: task 元信息 + rule / cron 引用清单。

        `links` 字段: 兼容旧前端契约, 由 rule.task_id + cron.task_id 联合构造。
        v2 后 rule / cron 是权威源, task_link 表已 DROP; caller 逐步迁到读
        `rule_briefs` (由 service 层拼装 RuleBrief) / `cron_refs` 两个独立字段。
        """
        with self.db.get_connection() as conn:
            task_row = conn.execute(
                "SELECT task_id, description, status, paused_at, created_at "
                "FROM task WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                return None
            rule_refs = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM rule WHERE task_id=?", (task_id,)
                ).fetchall()
            ]
            cron_rows = conn.execute(
                "SELECT cron_id, dispatch_owner FROM cron WHERE task_id=?",
                (task_id,),
            ).fetchall()
            links = [{"kind": "rule", "ref": rid} for rid in rule_refs] + [
                {"kind": "cron", "ref": c["cron_id"]} for c in cron_rows
            ]
            return {
                "task_id": task_row["task_id"],
                "description": task_row["description"],
                "status": task_row["status"],
                "paused_at": ms_to_iso_local(task_row["paused_at"]),
                "created_at": ms_to_iso_local(task_row["created_at"]),
                "links": links,
                "cron_refs": [
                    {
                        "ref": c["cron_id"],
                        "dispatch_owner": c["dispatch_owner"],
                    }
                    for c in cron_rows
                ],
            }

    def list_all(self) -> list[dict[str, Any]]:
        """所有 task 的聚合视图 (service 层接管 rule_briefs JOIN)。

        `links` 字段沿用老契约兼容前端。task 量级 (< 1000) 下按 task 循环单查
        性能足够, 不做全表 JOIN 展开。
        """
        with self.db.get_connection() as conn:
            tasks = conn.execute(
                "SELECT task_id, description, status, paused_at, created_at "
                "FROM task ORDER BY created_at DESC"
            ).fetchall()
            all_rules = conn.execute(
                "SELECT task_id, id AS ref FROM rule WHERE task_id IS NOT NULL"
            ).fetchall()
            all_crons = conn.execute(
                "SELECT task_id, cron_id, dispatch_owner FROM cron "
                "WHERE task_id IS NOT NULL"
            ).fetchall()
            links_by_task: dict[str, list[dict]] = {}
            crons_by_task: dict[str, list[dict]] = {}
            for r in all_rules:
                links_by_task.setdefault(r["task_id"], []).append(
                    {"kind": "rule", "ref": r["ref"]}
                )
            for c in all_crons:
                links_by_task.setdefault(c["task_id"], []).append(
                    {"kind": "cron", "ref": c["cron_id"]}
                )
                crons_by_task.setdefault(c["task_id"], []).append(
                    {"ref": c["cron_id"], "dispatch_owner": c["dispatch_owner"]}
                )
            return [
                {
                    "task_id": t["task_id"],
                    "description": t["description"],
                    "status": t["status"],
                    "paused_at": ms_to_iso_local(t["paused_at"]),
                    "created_at": ms_to_iso_local(t["created_at"]),
                    "links": links_by_task.get(t["task_id"], []),
                    "cron_refs": crons_by_task.get(t["task_id"], []),
                }
                for t in tasks
            ]

    def set_status(self, task_id: str, status: str) -> str:
        """改 task.status。返回 'ok' | 'noop' | 'not_found'。"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            current = cursor.execute(
                "SELECT status FROM task WHERE task_id=?", (task_id,)
            ).fetchone()
            if current is None:
                return "not_found"
            if current["status"] == status:
                return "noop"
            paused_at = now_ms() if status == "paused" else None
            cursor.execute(
                "UPDATE task SET status=?, paused_at=? WHERE task_id=?",
                (status, paused_at, task_id),
            )
            conn.commit()
            return "ok"

    def update_description(self, task_id: str, description: str) -> bool:
        """改 task.description。返回 affected>0。"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE task SET description=? WHERE task_id=?",
                (description, task_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_task(self, task_id: str) -> int:
        """删 task 行 (FK CASCADE 自动清 rule / cron / task_record_*)。"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM task WHERE task_id=?", (task_id,))
            conn.commit()
            return cursor.rowcount

    @staticmethod
    def delete_task_in_tx(cursor, task_id: str) -> int:
        """外层事务版本: 用 caller 提供的 cursor 删 task, 不 own connection。"""
        cursor.execute("DELETE FROM task WHERE task_id=?", (task_id,))
        return cursor.rowcount
