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

import json
import logging
import sqlite3
from typing import Any

from miloco.database.connector import get_db_connector
from miloco.utils.time_utils import iso_to_ms, ms_to_iso_local, now_ms

logger = logging.getLogger(__name__)


class TaskConflict(Exception):
    """409: task PK 撞库 (create_task UNIQUE 冲突)。"""


class TaskNotFound(Exception):
    """404: task 不存在 (toggle / update / delete 时读到 not_found)。"""


def _load_actions(raw: str | None) -> list[dict[str, Any]]:
    """动作列存的是 JSON 数组。解析失败按空处理 —— 存量脏数据不该让读 task 报错。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _boundary_actions_of(row: Any) -> dict[str, Any]:
    """把一行 task 的六个动作列拼成动作槽。行里必须已经 SELECT 过这几列。"""
    return {
        "on_enter_actions": _load_actions(row["on_enter_actions"]),
        "on_enter_desc": row["on_enter_desc"],
        "on_exit_actions": _load_actions(row["on_exit_actions"]),
        "on_exit_desc": row["on_exit_desc"],
        "on_target_actions": _load_actions(row["on_target_actions"]),
        "on_target_desc": row["on_target_desc"],
    }


class TaskRepo:
    def __init__(self):
        self.db = get_db_connector()

    def create_task(
        self,
        task_id: str,
        description: str,
        lifecycle: str = "permanent",
        expires_at: str | None = None,
    ) -> None:
        """INSERT task 行 (占位)。rule / cron 关联挂载由后续 endpoint 完成。"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO task "
                    "(task_id, description, status, created_at, lifecycle, expires_at) "
                    "VALUES (?, ?, 'active', ?, ?, ?)",
                    (
                        task_id,
                        description,
                        now_ms(),
                        lifecycle,
                        iso_to_ms(expires_at) if expires_at else None,
                    ),
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

    def get_description(self, task_id: str) -> str | None:
        """按 task_id 取任务描述（住户日志「所属任务」用）；无此行返回 None。
        轻量单列查询，不联 cron/rule（与 get_full_view 区分）。"""
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT description FROM task WHERE task_id = ?", (task_id,)
            ).fetchone()
            return row["description"] if row else None

    def get_full_view(self, task_id: str) -> dict[str, Any] | None:
        """单 task 视图: task 元信息 + cron_refs (rule_briefs 由 service 层拼装)."""
        with self.db.get_connection() as conn:
            task_row = conn.execute(
                "SELECT task_id, description, status, paused_at, created_at, lifecycle, "
                "expires_at, on_enter_actions, on_enter_desc, on_exit_actions, "
                "on_exit_desc, on_target_actions, on_target_desc "
                "FROM task WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                return None
            cron_rows = conn.execute(
                "SELECT cron_id, dispatch_owner FROM cron WHERE task_id=?",
                (task_id,),
            ).fetchall()
            return {
                "task_id": task_row["task_id"],
                "description": task_row["description"],
                "status": task_row["status"],
                "paused_at": ms_to_iso_local(task_row["paused_at"]),
                "created_at": ms_to_iso_local(task_row["created_at"]),
                "lifecycle": task_row["lifecycle"],
                "expires_at": ms_to_iso_local(task_row["expires_at"]),
                "actions": _boundary_actions_of(task_row),
                "cron_refs": [
                    {
                        "ref": c["cron_id"],
                        "dispatch_owner": c["dispatch_owner"],
                    }
                    for c in cron_rows
                ],
            }

    def list_all(self) -> list[dict[str, Any]]:
        """所有 task 的聚合视图 (service 层接管 rule_briefs JOIN)."""
        with self.db.get_connection() as conn:
            tasks = conn.execute(
                "SELECT task_id, description, status, paused_at, created_at, "
                "lifecycle, expires_at, on_enter_actions, on_enter_desc, "
                "on_exit_actions, on_exit_desc, on_target_actions, on_target_desc "
                "FROM task ORDER BY created_at DESC"
            ).fetchall()
            all_crons = conn.execute(
                "SELECT task_id, cron_id, dispatch_owner FROM cron "
                "WHERE task_id IS NOT NULL"
            ).fetchall()
            crons_by_task: dict[str, list[dict]] = {}
            for c in all_crons:
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
                    "lifecycle": t["lifecycle"],
                    "expires_at": ms_to_iso_local(t["expires_at"]),
                    # 动作槽跟基础字段同一行, 一起 SELECT 不产生额外查询。多条 rule
                    # 的 task 动作只存在这里 —— 列表不带的话住户界面显示成"无动作"。
                    "actions": _boundary_actions_of(t),
                    "cron_refs": crons_by_task.get(t["task_id"], []),
                }
                for t in tasks
            ]

    # ── 边界动作 (expand-contract 阶段 A 新增列) ──────────────────


    def get_boundary_actions(self, task_id: str) -> dict[str, Any] | None:
        """读 task 的三个动作槽 + lifecycle。task 不存在返回 None。

        六个动作列全空 = 该 task 还没迁移过 (或就是没配动作), 调用方按
        ``has_any_action`` 判断要不要回退到 rule 上的旧字段。
        """
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT lifecycle, on_enter_actions, on_enter_desc, "
                "on_exit_actions, on_exit_desc, on_target_actions, on_target_desc "
                "FROM task WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return {"lifecycle": row["lifecycle"], **_boundary_actions_of(row)}

    def set_boundary_actions(self, task_id: str, **slots: Any) -> bool:
        """写动作槽。只更新传进来的键, 未传的不动。

        值为 ``None`` 表示清空该槽 —— 与 ``rule update --clear`` 的语义一致,
        传空串只会存空串。``*_actions`` 三列是 NOT NULL, 清空它们就是写空列表。
        """
        allowed = {
            "on_enter_actions",
            "on_enter_desc",
            "on_exit_actions",
            "on_exit_desc",
            "on_target_actions",
            "on_target_desc",
        }
        unknown = set(slots) - allowed
        if unknown:
            raise ValueError(f"unknown task action slot(s): {sorted(unknown)}")
        if not slots:
            return False
        assignments = ", ".join(f"{k}=?" for k in slots)
        params = [
            json.dumps(v or []) if k.endswith("_actions") else v
            for k, v in slots.items()
        ]
        with self.db.get_connection() as conn:
            cur = conn.execute(
                f"UPDATE task SET {assignments} WHERE task_id=?",
                (*params, task_id),
            )
            conn.commit()
            return cur.rowcount > 0

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

    def update_meta(self, task_id: str, updates: dict[str, Any]) -> bool:
        """partial 改 task 的元信息列。返回 affected>0。

        时间列入参走 ISO, 入库 int ms —— 与 ``create_task`` / task_record 同口径。
        """
        allowed = {"description", "lifecycle", "expires_at"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unknown task meta column(s): {sorted(unknown)}")
        if not updates:
            return False
        params = [
            iso_to_ms(v) if k == "expires_at" and v is not None else v
            for k, v in updates.items()
        ]
        assignments = ", ".join(f"{k}=?" for k in updates)
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE task SET {assignments} WHERE task_id=?",
                (*params, task_id),
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
