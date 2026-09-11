# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task_record / task_terminate_log 数据访问层。

设计与 task_repo.py 风格的差异：
- 所有方法接 ``cursor`` 参数（**caller-managed connection**）——service 层负责
  ``BEGIN``/``COMMIT`` 与跨表事务编排。task_repo 的"each method owns its
  connection"风格不够灵活，session-end / rollover / terminate 这类多语句事务在
  本模块里很常见。
- repo 不做业务规则判断（如 current 是否超 target、status 是否 flip）——只做
  CRUD + 白名单 SET 子句构造。业务规则在 ``TaskRecordService``。
- ``sqlite3.Row`` 直接返回 row dict 风格（``row["column"]``）；service 层视需要
  转 pydantic 模型。
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from miloco.task_record.schema import RecordKind
from miloco.utils.time_utils import deploy_timezone, iso_to_ms, ms_to_iso_local

logger = logging.getLogger(__name__)


# DB 存 INTEGER ms (UTC 绝对时刻),repo 接口接 ISO 字符串 / int / None:
# - INSERT/UPDATE 入参经 ``_maybe_to_ms`` 适配 → 入库 int ms
# - SELECT 出口走 _row_to_X 字段级映射,时间列 inline ``ms_to_iso_local``
#   (与 person_repo / task_repo / rule_repo 风格一致,无全局白名单)
#
# ``_maybe_to_ms`` 是 service-repo 边界适配器:task_record service 内部时间计算
# 用 ISO 字符串(``_seconds_between`` / ``_parse_iso`` 等),给 repo 传时间字段
# 时是 ISO str / now_ms() int / None 多形态。边界统一 1 处 helper 比 ``iso_to_ms``
# 散到 50+ 处调用点干净。


def _maybe_to_ms(v: Any) -> Any:
    """时间字段入参规范化:ISO str → ms;int/None 透传。"""
    if v is None or isinstance(v, int):
        return v
    return iso_to_ms(v)



def _row_to_progress(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """task_record_progress 行 → dict,字段一一展开。"""
    if row is None:
        return None
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "target": row["target"],
        "current": row["current"],
        "unit": row["unit"],
        "window": row["window"],
        "recurring_pattern": row["recurring_pattern"],
        "expires_at": ms_to_iso_local(row["expires_at"]),
        "status": row["status"],
        "archived_at": ms_to_iso_local(row["archived_at"]),
        "created_at": ms_to_iso_local(row["created_at"]),
        "updated_at": ms_to_iso_local(row["updated_at"]),
    }


def _row_to_duration(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """task_record_duration 行 → dict,字段一一展开。"""
    if row is None:
        return None
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "target_minutes": row["target_minutes"],
        "active_session_start_at": ms_to_iso_local(row["active_session_start_at"]),
        "recurring_pattern": row["recurring_pattern"],
        "expires_at": ms_to_iso_local(row["expires_at"]),
        "status": row["status"],
        "archived_at": ms_to_iso_local(row["archived_at"]),
        "created_at": ms_to_iso_local(row["created_at"]),
        "updated_at": ms_to_iso_local(row["updated_at"]),
    }


def _row_to_session(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """task_record_duration_session 行 → dict,字段一一展开。"""
    if row is None:
        return None
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "start_at": ms_to_iso_local(row["start_at"]),
        "end_at": ms_to_iso_local(row["end_at"]),
        "duration_seconds": row["duration_seconds"],
        "archived_at": ms_to_iso_local(row["archived_at"]),
    }


def _row_to_event(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """task_record_event 行 → dict,字段一一展开。"""
    if row is None:
        return None
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "recurring_pattern": row["recurring_pattern"],
        "expires_at": ms_to_iso_local(row["expires_at"]),
        "status": row["status"],
        "created_at": ms_to_iso_local(row["created_at"]),
        "updated_at": ms_to_iso_local(row["updated_at"]),
    }


def _row_to_event_entry(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """task_record_event_entry 行 → dict,字段一一展开。"""
    if row is None:
        return None
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "description": row["description"],
        "at": ms_to_iso_local(row["at"]),
    }


def _row_to_terminate_log(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """task_terminate_log 行 → dict,字段一一展开。"""
    if row is None:
        return None
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "kind": row["kind"],
        "reason": row["reason"],
        "description": row["description"],
        "final_snapshot": row["final_snapshot"],
        "terminated_at": ms_to_iso_local(row["terminated_at"]),
    }


def _prefix_to_ms_range(prefix: str) -> tuple[int, int]:
    """日期前缀 → ``[start_ms, end_ms)``,按 ``deploy_timezone()`` 解读。

    用于 EventRepo 按日 / 周 / 月计数 (老接口接受 ``YYYY[-MM[-DD]]`` 字符串)。

    支持:
    - ``YYYY-MM-DD`` → 当天
    - ``YYYY-MM`` → 当月
    - ``YYYY`` → 当年
    """
    parts = prefix.split("-")
    tz = deploy_timezone()
    if len(parts) == 3:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        start = datetime(y, m, d, tzinfo=tz)
        end = start + timedelta(days=1)
    elif len(parts) == 2:
        y, m = int(parts[0]), int(parts[1])
        start = datetime(y, m, 1, tzinfo=tz)
        end = (
            datetime(y + 1, 1, 1, tzinfo=tz)
            if m == 12
            else datetime(y, m + 1, 1, tzinfo=tz)
        )
    elif len(parts) == 1:
        y = int(parts[0])
        start = datetime(y, 1, 1, tzinfo=tz)
        end = datetime(y + 1, 1, 1, tzinfo=tz)
    else:
        raise ValueError(f"unsupported date prefix: {prefix!r}")
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _build_progress_patch_clause(
    patch: dict[str, Any], now: int
) -> tuple[str, list[Any]]:
    """progress PATCH 字段级展开。

    PATCH 字段:``target`` / ``unit`` / ``window`` / ``recurring_pattern`` / ``expires_at``。
    PATCH 入参 patch dict 已被 service 层 ``iso_to_ms`` 处理为 int(跟 task_repo
    风格一致),repo 这层只字段级展开。
    """
    parts: list[str] = []
    params: list[Any] = []
    if "target" in patch:
        parts.append("target = ?")
        params.append(patch["target"])
    if "unit" in patch:
        parts.append("unit = ?")
        params.append(patch["unit"])
    if "window" in patch:
        parts.append("window = ?")
        params.append(patch["window"])
    if "recurring_pattern" in patch:
        parts.append("recurring_pattern = ?")
        params.append(_serialize_pattern(patch["recurring_pattern"]))
    if "expires_at" in patch:
        parts.append("expires_at = ?")
        params.append(_maybe_to_ms(patch["expires_at"]))
    if not parts:
        raise ValueError("empty patch")
    parts.append("updated_at = ?")
    params.append(_maybe_to_ms(now))
    return ", ".join(parts), params


def _build_duration_patch_clause(
    patch: dict[str, Any], now: int
) -> tuple[str, list[Any]]:
    """duration PATCH 字段级展开:``target_minutes`` / ``recurring_pattern`` / ``expires_at``。"""
    parts: list[str] = []
    params: list[Any] = []
    if "target_minutes" in patch:
        parts.append("target_minutes = ?")
        params.append(patch["target_minutes"])
    if "recurring_pattern" in patch:
        parts.append("recurring_pattern = ?")
        params.append(_serialize_pattern(patch["recurring_pattern"]))
    if "expires_at" in patch:
        parts.append("expires_at = ?")
        params.append(_maybe_to_ms(patch["expires_at"]))
    if not parts:
        raise ValueError("empty patch")
    parts.append("updated_at = ?")
    params.append(_maybe_to_ms(now))
    return ", ".join(parts), params


def _build_event_patch_clause(
    patch: dict[str, Any], now: int
) -> tuple[str, list[Any]]:
    """event PATCH 字段级展开:``recurring_pattern`` / ``expires_at``。"""
    parts: list[str] = []
    params: list[Any] = []
    if "recurring_pattern" in patch:
        parts.append("recurring_pattern = ?")
        params.append(_serialize_pattern(patch["recurring_pattern"]))
    if "expires_at" in patch:
        parts.append("expires_at = ?")
        params.append(_maybe_to_ms(patch["expires_at"]))
    if not parts:
        raise ValueError("empty patch")
    parts.append("updated_at = ?")
    params.append(_maybe_to_ms(now))
    return ", ".join(parts), params


def _serialize_pattern(pattern: dict[str, Any] | str | None) -> str | None:
    if pattern is None:
        return None
    if isinstance(pattern, str):
        return pattern
    return json.dumps(pattern, ensure_ascii=False)


# ── ProgressRepo ──────────────────────────────────────────────────────────────


class ProgressRepo:
    """``task_record_progress`` 主表 CRUD。"""

    @staticmethod
    def insert_active(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        target: int,
        unit: str,
        window: str,
        recurring_pattern: dict[str, Any] | str | None,
        expires_at: str | None,
        now: str,
    ) -> int:
        now_ms_v = _maybe_to_ms(now)
        cursor.execute(
            """
            INSERT INTO task_record_progress (
                task_id, target, current, unit, window, recurring_pattern,
                expires_at, status, archived_at, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, ?, ?, 'active', NULL, ?, ?)
            """,
            (
                task_id,
                target,
                unit,
                window,
                _serialize_pattern(recurring_pattern),
                _maybe_to_ms(expires_at),
                now_ms_v,
                now_ms_v,
            ),
        )
        return cursor.lastrowid or 0

    @staticmethod
    def get_active(
        cursor: sqlite3.Cursor, task_id: str
    ) -> dict[str, Any] | None:
        return _row_to_progress(
            cursor.execute(
                "SELECT * FROM task_record_progress "
                "WHERE task_id = ? AND archived_at IS NULL",
                (task_id,),
            ).fetchone()
        )

    @staticmethod
    def update_active(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        patch: dict[str, Any],
        now: str,
    ) -> int:
        clause, params = _build_progress_patch_clause(patch, now)
        cursor.execute(
            f"UPDATE task_record_progress SET {clause} "
            "WHERE task_id = ? AND archived_at IS NULL",
            [*params, task_id],
        )
        return cursor.rowcount

    @staticmethod
    def update_progress(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        new_current: int,
        new_status: str,
        now: str,
    ) -> int:
        """progress-inc 专用。绕开白名单（current/status 是系统字段，业务层调用）。"""
        cursor.execute(
            "UPDATE task_record_progress "
            "SET current = ?, status = ?, updated_at = ? "
            "WHERE task_id = ? AND archived_at IS NULL",
            (new_current, new_status, _maybe_to_ms(now), task_id),
        )
        return cursor.rowcount

    @staticmethod
    def set_status(
        cursor: sqlite3.Cursor, *, task_id: str, status: str, now: int
    ) -> int:
        cursor.execute(
            "UPDATE task_record_progress SET status = ?, updated_at = ? "
            "WHERE task_id = ? AND archived_at IS NULL",
            (status, _maybe_to_ms(now), task_id),
        )
        return cursor.rowcount

    @staticmethod
    def archive_active(
        cursor: sqlite3.Cursor, *, task_id: str, archived_at: str
    ) -> int:
        archived_ms = _maybe_to_ms(archived_at)
        cursor.execute(
            "UPDATE task_record_progress SET archived_at = ?, updated_at = ? "
            "WHERE task_id = ? AND archived_at IS NULL",
            (archived_ms, archived_ms, task_id),
        )
        return cursor.rowcount

    @staticmethod
    def list_recurring_active(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
        return [
            _row_to_progress(r)
            for r in cursor.execute(
                "SELECT * FROM task_record_progress "
                "WHERE archived_at IS NULL AND recurring_pattern IS NOT NULL"
            )
        ]


# ── DurationRepo（含 session 子表） ──────────────────────────────────────────


class DurationRepo:
    """``task_record_duration`` 主表 + ``task_record_duration_session`` 子表。"""

    @staticmethod
    def insert_active(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        target_minutes: int | None,
        active_session_start_at: str | None,
        recurring_pattern: dict[str, Any] | str | None,
        expires_at: str | None,
        now: str,
    ) -> int:
        now_ms_v = _maybe_to_ms(now)
        cursor.execute(
            """
            INSERT INTO task_record_duration (
                task_id, target_minutes, active_session_start_at,
                recurring_pattern, expires_at, status, archived_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?)
            """,
            (
                task_id,
                target_minutes,
                _maybe_to_ms(active_session_start_at),
                _serialize_pattern(recurring_pattern),
                _maybe_to_ms(expires_at),
                now_ms_v,
                now_ms_v,
            ),
        )
        return cursor.lastrowid or 0

    @staticmethod
    def get_active(
        cursor: sqlite3.Cursor, task_id: str
    ) -> dict[str, Any] | None:
        return _row_to_duration(
            cursor.execute(
                "SELECT * FROM task_record_duration "
                "WHERE task_id = ? AND archived_at IS NULL",
                (task_id,),
            ).fetchone()
        )

    @staticmethod
    def update_active(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        patch: dict[str, Any],
        now: str,
    ) -> int:
        clause, params = _build_duration_patch_clause(patch, now)
        cursor.execute(
            f"UPDATE task_record_duration SET {clause} "
            "WHERE task_id = ? AND archived_at IS NULL",
            [*params, task_id],
        )
        return cursor.rowcount

    @staticmethod
    def set_active_session_start(
        cursor: sqlite3.Cursor, *, task_id: str, start_at: str, now: int
    ) -> int:
        cursor.execute(
            "UPDATE task_record_duration "
            "SET active_session_start_at = ?, updated_at = ? "
            "WHERE task_id = ? AND archived_at IS NULL",
            (_maybe_to_ms(start_at), _maybe_to_ms(now), task_id),
        )
        return cursor.rowcount

    @staticmethod
    def clear_active_session_start(
        cursor: sqlite3.Cursor, *, task_id: str, now: int
    ) -> int:
        cursor.execute(
            "UPDATE task_record_duration "
            "SET active_session_start_at = NULL, updated_at = ? "
            "WHERE task_id = ? AND archived_at IS NULL",
            (now, task_id),
        )
        return cursor.rowcount

    @staticmethod
    def set_status(
        cursor: sqlite3.Cursor, *, task_id: str, status: str, now: int
    ) -> int:
        cursor.execute(
            "UPDATE task_record_duration SET status = ?, updated_at = ? "
            "WHERE task_id = ? AND archived_at IS NULL",
            (status, _maybe_to_ms(now), task_id),
        )
        return cursor.rowcount

    @staticmethod
    def archive_active(
        cursor: sqlite3.Cursor, *, task_id: str, archived_at: str
    ) -> int:
        archived_ms = _maybe_to_ms(archived_at)
        cursor.execute(
            "UPDATE task_record_duration SET archived_at = ?, updated_at = ? "
            "WHERE task_id = ? AND archived_at IS NULL",
            (archived_ms, archived_ms, task_id),
        )
        return cursor.rowcount

    @staticmethod
    def list_recurring_active(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
        return [
            _row_to_duration(r)
            for r in cursor.execute(
                "SELECT * FROM task_record_duration "
                "WHERE archived_at IS NULL AND recurring_pattern IS NOT NULL"
            )
        ]

    @staticmethod
    def insert_session(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        start_at: str,
        end_at: str,
        duration_seconds: int,
    ) -> int:
        cursor.execute(
            "INSERT INTO task_record_duration_session "
            "(task_id, start_at, end_at, duration_seconds, archived_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (
                task_id,
                _maybe_to_ms(start_at),
                _maybe_to_ms(end_at),
                duration_seconds,
            ),
        )
        return cursor.lastrowid or 0

    @staticmethod
    def archive_active_sessions(
        cursor: sqlite3.Cursor, *, task_id: str, archived_at: str
    ) -> int:
        cursor.execute(
            "UPDATE task_record_duration_session SET archived_at = ? "
            "WHERE task_id = ? AND archived_at IS NULL",
            (_maybe_to_ms(archived_at), task_id),
        )
        return cursor.rowcount

    @staticmethod
    def list_active_sessions(
        cursor: sqlite3.Cursor, *, task_id: str
    ) -> list[dict[str, Any]]:
        return [
            _row_to_session(r)
            for r in cursor.execute(
                "SELECT * FROM task_record_duration_session "
                "WHERE task_id = ? AND archived_at IS NULL "
                "ORDER BY start_at",
                (task_id,),
            )
        ]

    @staticmethod
    def sum_seconds_active_period(
        cursor: sqlite3.Cursor, *, task_id: str
    ) -> int:
        row = cursor.execute(
            "SELECT COALESCE(SUM(duration_seconds), 0) AS total "
            "FROM task_record_duration_session "
            "WHERE task_id = ? AND archived_at IS NULL",
            (task_id,),
        ).fetchone()
        return int(row["total"]) if row else 0


# ── EventRepo（含 entry 子表） ───────────────────────────────────────────────


class EventRepo:
    """``task_record_event`` 主表 + ``task_record_event_entry`` 子表。"""

    @staticmethod
    def insert_active(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        recurring_pattern: dict[str, Any] | str | None,
        expires_at: str | None,
        now: str,
    ) -> int:
        now_ms_v = _maybe_to_ms(now)
        cursor.execute(
            """
            INSERT INTO task_record_event (
                task_id, recurring_pattern, expires_at, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (
                task_id,
                _serialize_pattern(recurring_pattern),
                _maybe_to_ms(expires_at),
                now_ms_v,
                now_ms_v,
            ),
        )
        return cursor.lastrowid or 0

    @staticmethod
    def get_active(
        cursor: sqlite3.Cursor, task_id: str
    ) -> dict[str, Any] | None:
        return _row_to_event(
            cursor.execute(
                "SELECT * FROM task_record_event WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        )

    @staticmethod
    def update_active(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        patch: dict[str, Any],
        now: str,
    ) -> int:
        clause, params = _build_event_patch_clause(patch, now)
        cursor.execute(
            f"UPDATE task_record_event SET {clause} WHERE task_id = ?",
            [*params, task_id],
        )
        return cursor.rowcount

    @staticmethod
    def insert_entry(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        description: str,
        at: str,
    ) -> int:
        cursor.execute(
            "INSERT INTO task_record_event_entry (task_id, description, at) "
            "VALUES (?, ?, ?)",
            (task_id, description, _maybe_to_ms(at)),
        )
        return cursor.lastrowid or 0

    @staticmethod
    def count_entries(cursor: sqlite3.Cursor, *, task_id: str) -> int:
        row = cursor.execute(
            "SELECT COUNT(*) AS n FROM task_record_event_entry WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def count_entries_with_prefix(
        cursor: sqlite3.Cursor, *, task_id: str, at_prefix: str
    ) -> int:
        """按日 / 周 / 月前缀统计 entry 数。

        `at` 列从 v10 起是 INTEGER ms,LIKE 不再适用——把前缀展开成 ``[start_ms, end_ms)`` range 比较。
        """
        start_ms, end_ms = _prefix_to_ms_range(at_prefix)
        row = cursor.execute(
            "SELECT COUNT(*) AS n FROM task_record_event_entry "
            "WHERE task_id = ? AND at >= ? AND at < ?",
            (task_id, start_ms, end_ms),
        ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def last_entry_at(
        cursor: sqlite3.Cursor, *, task_id: str
    ) -> str | None:
        row = cursor.execute(
            "SELECT MAX(at) AS last_at FROM task_record_event_entry "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row or row["last_at"] is None:
            return None
        return ms_to_iso_local(row["last_at"])

    @staticmethod
    def list_entries(
        cursor: sqlite3.Cursor, *, task_id: str
    ) -> list[dict[str, Any]]:
        return [
            _row_to_event_entry(r)
            for r in cursor.execute(
                "SELECT * FROM task_record_event_entry "
                "WHERE task_id = ? ORDER BY at",
                (task_id,),
            )
        ]


# ── TerminateLogRepo ─────────────────────────────────────────────────────────


class TerminateLogRepo:
    """``task_terminate_log`` 审计表 CRUD（含 30 天滚动）。"""

    @staticmethod
    def insert(
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        kind: RecordKind | str,
        reason: str,
        description: str,
        final_snapshot: dict[str, Any] | str,
        terminated_at: str,
    ) -> int:
        snapshot_str = (
            final_snapshot
            if isinstance(final_snapshot, str)
            else json.dumps(final_snapshot, ensure_ascii=False)
        )
        kind_str = kind.value if isinstance(kind, RecordKind) else kind
        cursor.execute(
            """
            INSERT INTO task_terminate_log (
                task_id, kind, reason, description, final_snapshot, terminated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                kind_str,
                reason,
                description,
                snapshot_str,
                _maybe_to_ms(terminated_at),
            ),
        )
        return cursor.lastrowid or 0

    @staticmethod
    def prune_older_than(cursor: sqlite3.Cursor, *, cutoff: str) -> int:
        cursor.execute(
            "DELETE FROM task_terminate_log WHERE terminated_at < ?",
            (_maybe_to_ms(cutoff),),
        )
        return cursor.rowcount

    @staticmethod
    def count(cursor: sqlite3.Cursor) -> int:
        row = cursor.execute(
            "SELECT COUNT(*) AS n FROM task_terminate_log"
        ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def list_recent(
        cursor: sqlite3.Cursor, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        return [
            _row_to_terminate_log(r)
            for r in cursor.execute(
                "SELECT * FROM task_terminate_log "
                "ORDER BY terminated_at DESC LIMIT ?",
                (limit,),
            )
        ]


def fetch_active_record_status_by_task_ids(
    cursor: sqlite3.Cursor, task_ids: list[str]
) -> dict[str, str]:
    """批量查给定 task_id 列表当前活跃期 record 主表的 status。

    UNION 三张主表（progress/duration/event）的活跃期行（progress/duration
    带 ``archived_at IS NULL``；event 表无 archived_at，直接全取）。Schema 保证
    一个 task 在三张主表中至多对应一行 active record，返回 ``{task_id: status}``；
    未绑 record 的 task_id 不在返回值里。
    """
    if not task_ids:
        return {}
    placeholders = ",".join(["?"] * len(task_ids))
    sql = (
        f"SELECT task_id, status FROM task_record_progress "
        f"  WHERE task_id IN ({placeholders}) AND archived_at IS NULL "
        f"UNION ALL "
        f"SELECT task_id, status FROM task_record_duration "
        f"  WHERE task_id IN ({placeholders}) AND archived_at IS NULL "
        f"UNION ALL "
        f"SELECT task_id, status FROM task_record_event "
        f"  WHERE task_id IN ({placeholders})"
    )
    params = list(task_ids) * 3
    return {row["task_id"]: row["status"] for row in cursor.execute(sql, params)}


def fetch_active_record_satisfaction_by_task_ids(
    cursor: sqlite3.Cursor, task_ids: list[str]
) -> dict[str, bool]:
    """批量查 task_id 列表当前活跃期 record 是否已「本周期达标」。

    判据（任一为真即视为 satisfied）：

    - ``status == 'completed'``（oneshot 任务终态）
    - progress：``recurring_pattern IS NOT NULL AND current >= target``
    - duration：``recurring_pattern IS NOT NULL AND target_minutes IS NOT NULL
      AND active period 累计秒数 >= target_minutes * 60``
    - event：无 target 概念，仅看 status

    场景：perception filter 装载 event mode rule 前剔除已达标的 task；让 event
    mode 也享有等价于达标条件项边沿的「周期达标静默」机制。

    SQL UNION 三张主表；duration 用 LEFT JOIN 子表算累计秒数。未绑 record 的
    task_id 不在返回值里。
    """
    if not task_ids:
        return {}
    placeholders = ",".join(["?"] * len(task_ids))
    sql = (
        f"SELECT task_id, "
        f"  (status = 'completed' OR "
        f"   (recurring_pattern IS NOT NULL AND current >= target)) "
        f"  AS satisfied "
        f"FROM task_record_progress "
        f"WHERE task_id IN ({placeholders}) AND archived_at IS NULL "
        f"UNION ALL "
        f"SELECT d.task_id, "
        f"  (d.status = 'completed' OR "
        f"   (d.recurring_pattern IS NOT NULL "
        f"    AND d.target_minutes IS NOT NULL "
        f"    AND COALESCE(s.total_seconds, 0) >= d.target_minutes * 60)) "
        f"  AS satisfied "
        f"FROM task_record_duration d "
        f"LEFT JOIN ("
        f"  SELECT task_id, SUM(duration_seconds) AS total_seconds "
        f"  FROM task_record_duration_session "
        f"  WHERE archived_at IS NULL "
        f"  GROUP BY task_id"
        f") s ON d.task_id = s.task_id "
        f"WHERE d.task_id IN ({placeholders}) AND d.archived_at IS NULL "
        f"UNION ALL "
        f"SELECT task_id, (status = 'completed') AS satisfied "
        f"FROM task_record_event "
        f"WHERE task_id IN ({placeholders})"
    )
    params = list(task_ids) * 3
    return {
        row["task_id"]: bool(row["satisfied"])
        for row in cursor.execute(sql, params)
    }
