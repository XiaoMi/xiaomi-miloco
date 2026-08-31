# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""TaskRecordService — task_record 业务编排层。

职责：
- 跨表事务编排（init / session-end / rollover / terminate）
- 业务规则（progress increment cap / completed flip / negative delta 回退 /
  session 重入 / kind dispatch）
- derived 派生量计算（progress / duration / event 三种多态结果）
- 字段白名单校验（PATCH 路径，按 kind 分发）

时区约定：所有 ISO8601 时间字符串采用服务器本地时区（部署环境约定为
``Asia/Shanghai``），spec §6.3 rollover job 也以 ``Asia/Shanghai`` 为基准。
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from miloco.database.connector import get_db_connector
from miloco.task.schema import (  # noqa: E402
    ActiveSession,
    RecordSummary,
    WindowRemaining,
)
from miloco.task_record.repo import (
    DurationRepo,
    EventRepo,
    ProgressRepo,
    TerminateLogRepo,
    _row_to_duration,
)
from miloco.task_record.schema import (
    PATCH_FIELDS_BY_KIND,
    DerivedDuration,
    DerivedDurationRange,
    DerivedEvent,
    DerivedEventRange,
    DerivedProgress,
    DerivedProgressRange,
    DurationInitContent,
    EventInitContent,
    ProgressInitContent,
    RecordKind,
    RecordStatus,
    TerminateReason,
)
from miloco.utils.time_utils import (
    deploy_timezone,
    ms_to_iso_local,
)
from miloco.utils.time_utils import (
    now_iso as _now_iso,
)

logger = logging.getLogger(__name__)


# ── 异常 ─────────────────────────────────────────────────────────────────────


class TaskNotFoundError(Exception):
    """task 表无对应 task_id（404 task_not_found）。"""


class RecordNotFoundError(Exception):
    """对应 task 无活跃 record（404 no_active_record）。"""


class RecordAlreadyExistsError(Exception):
    """init_record 时已存在活跃 record（409 record_already_exists）。"""


class RecordWrongKindError(Exception):
    """调用的 mutate op 与活跃 record 的 kind 不匹配（422 wrong_kind）。"""


class RecordSchemaError(Exception):
    """content / patch schema 校验失败（422 schema_invalid）。"""


# ── 辅助 ─────────────────────────────────────────────────────────────────────


def _today_prefix() -> str:
    """本地日期前缀(YYYY-MM-DD),按 ``deploy_timezone()`` 解读。

    给 EventRepo.count_entries_with_prefix 用,内部会展开成 ms range。
    """
    return datetime.now(deploy_timezone()).strftime("%Y-%m-%d")


def _local_date_to_ms_range(date_str: str) -> tuple[int, int]:
    """'YYYY-MM-DD' → ``[start_ms, end_ms)`` 按 ``deploy_timezone()``。"""
    parts = date_str.split("-")
    if len(parts) != 3:
        raise ValueError(f"expected YYYY-MM-DD, got {date_str!r}")
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    tz = deploy_timezone()
    start = datetime(y, m, d, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _ms_to_local_date(ms: int) -> str:
    """ms → 'YYYY-MM-DD' 按 ``deploy_timezone()``。"""
    return datetime.fromtimestamp(ms / 1000, tz=deploy_timezone()).strftime(
        "%Y-%m-%d"
    )


def _ensure_task_exists(cursor: sqlite3.Cursor, task_id: str) -> None:
    row = cursor.execute(
        "SELECT 1 FROM task WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"task {task_id!r} 不存在")


def _is_task_paused(cursor: sqlite3.Cursor, task_id: str) -> bool:
    """task 表 status='paused' → True；不存在 / active 时 → False。"""
    row = cursor.execute(
        "SELECT status FROM task WHERE task_id = ?", (task_id,)
    ).fetchone()
    return row is not None and row["status"] == "paused"


_PROGRESS_WINDOW_VALID = {"day", "week", "month", "longterm"}


def _validate_patch_values(kind: RecordKind, patch: dict[str, Any]) -> None:
    """PATCH 值层面校验——仅检查字段名通过白名单后的值合法性。"""
    if kind is RecordKind.PROGRESS:
        if "target" in patch:
            v = patch["target"]
            if not isinstance(v, int) or v <= 0:
                raise RecordSchemaError(
                    f"target 必须为正整数（gt=0），收到 {v!r}"
                )
        if "unit" in patch:
            v = patch["unit"]
            if not isinstance(v, str) or len(v) < 1:
                raise RecordSchemaError(
                    f"unit 必须为非空字符串，收到 {v!r}"
                )
        if "window" in patch:
            v = patch["window"]
            if v not in _PROGRESS_WINDOW_VALID:
                raise RecordSchemaError(
                    f"window 取值非法 {v!r}，仅支持 {sorted(_PROGRESS_WINDOW_VALID)}"
                )
    elif kind is RecordKind.DURATION:
        if "target_minutes" in patch:
            v = patch["target_minutes"]
            if v is not None and (not isinstance(v, int) or v <= 0):
                raise RecordSchemaError(
                    f"target_minutes 必须为正整数（gt=0）或 null，收到 {v!r}"
                )


def _recompute_progress_status(
    cursor: sqlite3.Cursor, task_id: str, now: str
) -> None:
    """progress.target 变更后,按 current vs target 重算 status。

    recurring task 永不翻 completed —— recurring 的语义是循环，没有"完成"终点。
    progress 没有达标通知这条路（record 源本次只支持 duration），所以这里也没有
    防重复要考虑。
    """
    row = ProgressRepo.get_active(cursor, task_id)
    if row is None:
        return
    is_recurring = row["recurring_pattern"] is not None
    current = int(row["current"])
    target = int(row["target"])
    status = row["status"]
    if is_recurring:
        new_status = RecordStatus.ACTIVE.value
    else:
        new_status = (
            RecordStatus.COMPLETED.value
            if current >= target
            else RecordStatus.ACTIVE.value
        )
    if status != new_status:
        ProgressRepo.set_status(
            cursor, task_id=task_id, status=new_status, now=now
        )


def _recompute_duration_status(
    cursor: sqlite3.Cursor, task_id: str, now: str
) -> None:
    """duration.target_minutes 变更后,按 accumulated vs target 重算 status。

    target=None 时 status 永远 active（无达标语义）。
    recurring task 永不翻 completed —— recurring 的语义是循环，没有"完成"
    终点；本周期内达标的"防重复通知"由达标条件项自身的边沿承担（条件已为真就
    产不出新边沿），跨周期归零把它翻假，不入 DB。
    """
    row = DurationRepo.get_active(cursor, task_id)
    if row is None:
        return
    is_recurring = row["recurring_pattern"] is not None
    target = row["target_minutes"]
    status = row["status"]
    if is_recurring or target is None:
        new_status = RecordStatus.ACTIVE.value
    else:
        accumulated_seconds = DurationRepo.sum_seconds_active_period(
            cursor, task_id=task_id
        )
        new_status = (
            RecordStatus.COMPLETED.value
            if accumulated_seconds >= int(target) * 60
            else RecordStatus.ACTIVE.value
        )
    if status != new_status:
        DurationRepo.set_status(
            cursor, task_id=task_id, status=new_status, now=now
        )


def _archived_at_for_rollover(window: str, now: datetime) -> str:
    """rollover 时的 archived_at —— 被归档 period 的最后一刻。

    now 是 rollover 触发时刻（次日 00:05），直接用 now 会让 archived_at
    落到次日，compute --date <昨日> 走 LIKE '<昨日>%' 查不到归档行。
    返回前一 period 的 23:59:59（day）/ 周末 / 月末，让历史日期查询命中。

    window=longterm 不参与 rollover，按 now 兜底（caller 应在更上层拦截）。
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "day":
        period_start = midnight
    elif window == "week":
        period_start = midnight - timedelta(days=now.weekday())
    elif window == "month":
        period_start = midnight.replace(day=1)
    else:
        return ms_to_iso_local(int(now.timestamp() * 1000))
    period_end = period_start - timedelta(seconds=1)
    return ms_to_iso_local(int(period_end.timestamp() * 1000))


def _detect_kind(cursor: sqlite3.Cursor, task_id: str) -> RecordKind | None:
    """检测 task 当前活跃 record 的 kind（progress/duration/event）。"""
    row = cursor.execute(
        """
        SELECT 'progress' AS kind FROM task_record_progress
          WHERE task_id = ? AND archived_at IS NULL
        UNION ALL
        SELECT 'duration' FROM task_record_duration
          WHERE task_id = ? AND archived_at IS NULL
        UNION ALL
        SELECT 'event' FROM task_record_event
          WHERE task_id = ?
        LIMIT 1
        """,
        (task_id, task_id, task_id),
    ).fetchone()
    if row is None:
        return None
    return RecordKind(row["kind"])


def _parse_iso(s: str) -> datetime:
    """ISO8601 → aware datetime。naive 字符串按 ``deploy_timezone()`` 解读。

    兼容 Python 3.10 的 ``fromisoformat``(不接受 ``Z`` 后缀,需替换为 ``+00:00``)。
    """
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise RecordSchemaError(f"invalid ISO8601 timestamp: {s!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=deploy_timezone())
    return dt


def _seconds_between(start: str, end: str) -> int:
    return int((_parse_iso(end) - _parse_iso(start)).total_seconds())


def _minutes_between(start: str, end: str) -> int:
    return int((_parse_iso(end) - _parse_iso(start)).total_seconds() // 60)


# ── derived 计算（按 kind 多态） ──────────────────────────────────────────────


def _derive_progress(row: sqlite3.Row) -> DerivedProgress:
    target = int(row["target"])
    current = int(row["current"])
    remaining = max(target - current, 0)
    pct = (current / target) if target > 0 else 0.0
    return DerivedProgress(remaining=remaining, progress_pct=pct)


def _derive_duration(
    cursor: sqlite3.Cursor, row: dict[str, Any]
) -> DerivedDuration:
    task_id = row["task_id"]
    completed_seconds = DurationRepo.sum_seconds_active_period(
        cursor, task_id=task_id
    )
    active_start = row["active_session_start_at"]
    in_flight_seconds = 0
    if active_start:
        in_flight_seconds = max(
            _seconds_between(active_start, _now_iso()),
            0,
        )
    accumulated_minutes = (completed_seconds + in_flight_seconds) // 60
    target = row["target_minutes"]
    remaining = (
        max(int(target) - accumulated_minutes, 0)
        if target is not None
        else None
    )
    return DerivedDuration(
        accumulated_minutes_today=accumulated_minutes,
        remaining_minutes=remaining,
        active_session_start_at=active_start,
    )


def _derive_event(
    cursor: sqlite3.Cursor, task_id: str
) -> DerivedEvent:
    total = EventRepo.count_entries(cursor, task_id=task_id)
    today = EventRepo.count_entries_with_prefix(
        cursor, task_id=task_id, at_prefix=_today_prefix()
    )
    last_at = EventRepo.last_entry_at(cursor, task_id=task_id)
    return DerivedEvent(count_total=total, count_today=today, last_at=last_at)


# ── summary 装配工具(spec 2026-06-11) ────────────────────────────────────────


def _build_window_remaining(
    window: str, now: datetime
) -> WindowRemaining:
    """距 window 边界的剩余时间。仅 window='day' 调用(到当日 24:00 deploy_timezone())。"""
    if window != "day":
        raise ValueError(
            f"_build_window_remaining only supports window='day', got {window!r}"
        )
    end_of_day = now.replace(
        hour=23, minute=59, second=59, microsecond=0
    ) + timedelta(seconds=1)
    delta = end_of_day - now
    seconds = int(delta.total_seconds())
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        display = f"{h}h {m}m"
    elif seconds >= 60:
        m = seconds // 60
        display = f"{m}m"
    else:
        display = f"{seconds}s"
    return WindowRemaining(seconds=seconds, display=display)


def _build_active_session(
    row: sqlite3.Row | dict, now: datetime
) -> ActiveSession | None:
    """duration kind 主表行的 active_session_start_at 非空 → 构造 ActiveSession。

    ``active_session_start_at`` 可能是 int ms (raw sqlite3.Row) 或 ISO 字符串
    (``_row_to_duration`` 已转),统一转部署时区带偏移 ISO (如 ``+08:00``) 后再算
    elapsed,与 API 出口格式一致。
    """
    started_at = row["active_session_start_at"]
    if not started_at:
        return None
    if isinstance(started_at, int):
        started_at = ms_to_iso_local(started_at)
    now_iso_str = ms_to_iso_local(int(now.timestamp() * 1000))
    elapsed = _minutes_between(started_at, now_iso_str)
    return ActiveSession(started_at=started_at, elapsed_minutes=max(elapsed, 0))


# ── final_snapshot 装配 ──────────────────────────────────────────────────────


def _build_final_snapshot(
    cursor: sqlite3.Cursor, kind: RecordKind, task_id: str
) -> dict[str, Any]:
    if kind is RecordKind.PROGRESS:
        row = ProgressRepo.get_active(cursor, task_id)
        if row is None:
            return {}
        return {
            "target": int(row["target"]),
            "current": int(row["current"]),
            "unit": row["unit"],
            "window": row["window"],
        }
    if kind is RecordKind.DURATION:
        row = DurationRepo.get_active(cursor, task_id)
        if row is None:
            return {}
        accumulated_seconds = DurationRepo.sum_seconds_active_period(
            cursor, task_id=task_id
        )
        # 含 in-flight session (active_session_start_at → now) ——
        # 否则 delete 级联清后这段时长永久丢失
        active_start = row["active_session_start_at"]
        in_flight_seconds = 0
        if active_start is not None:
            in_flight_seconds = max(
                _seconds_between(active_start, _now_iso()), 0
            )
        target = row["target_minutes"]
        return {
            "target_minutes": int(target) if target is not None else None,
            "accumulated_minutes": (accumulated_seconds + in_flight_seconds) // 60,
            "in_flight_minutes": in_flight_seconds // 60,
            "active_session_start_at": active_start,
        }
    if kind is RecordKind.EVENT:
        count = EventRepo.count_entries(cursor, task_id=task_id)
        first_row = cursor.execute(
            "SELECT MIN(at) AS first_at, MAX(at) AS last_at "
            "FROM task_record_event_entry WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        # v10 起 at 是 INTEGER ms,出口转部署时区带偏移 ISO (与 API 出口风格一致)
        return {
            "event_count": count,
            "first_at": ms_to_iso_local(first_row["first_at"]) if first_row else None,
            "last_at": ms_to_iso_local(first_row["last_at"]) if first_row else None,
        }
    raise ValueError(f"unknown kind {kind!r}")


# ── Service ───────────────────────────────────────────────────────────────────


class TaskRecordService:
    def __init__(self) -> None:
        self.db = get_db_connector()

    # ── init / get / patch ──────────────────────────────────────────────────

    def init_record(
        self, task_id: str, kind: RecordKind, content: dict[str, Any]
    ) -> dict[str, Any]:
        """新建活跃 record（spec §6.1 阶段 A'）。

        前置：task 必须已存在（D' 已完成）。重复 init 走 partial UNIQUE 索引
        或 event 表的 UNIQUE(task_id) 约束捕获，统一返 ``RecordAlreadyExistsError``。
        """
        validated_content = self._validate_init_content(kind, content)
        now = _now_iso()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                _ensure_task_exists(cursor, task_id)
                existing_kind = _detect_kind(cursor, task_id)
                if existing_kind is not None:
                    raise RecordAlreadyExistsError(
                        f"task {task_id!r} already has an active "
                        f"{existing_kind.value} record"
                    )
                self._insert_by_kind(cursor, task_id, kind, validated_content, now)
                conn.commit()
            except (
                TaskNotFoundError,
                RecordAlreadyExistsError,
                RecordSchemaError,
            ):
                conn.rollback()
                raise
            except sqlite3.IntegrityError as e:
                conn.rollback()
                msg = str(e)
                if "FOREIGN KEY" in msg:
                    raise TaskNotFoundError(
                        f"task {task_id!r} 不存在（FK violation）"
                    ) from e
                if "UNIQUE" in msg or "uniq_progress_active" in msg or \
                        "uniq_duration_active" in msg:
                    raise RecordAlreadyExistsError(
                        f"task {task_id!r} 已有活跃 record"
                    ) from e
                raise
        return self.get_active_record(task_id)

    def detect_record_kind(self, task_id: str) -> str | None:
        """返活跃 record 的 kind 字符串（progress/duration/event）；无 record → None。

        供 rule service 等外部校验区分「无 record」/「kind 不匹配」错误。
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            kind = _detect_kind(cursor, task_id)
            return kind.value if kind is not None else None

    def read_duration_target_state(
        self, task_id: str
    ) -> tuple[int | None, int] | None:
        """供 rule engine on-target-desc timer 计算用的轻量查询。

        Returns:
            ``(target_minutes, accumulated_minutes_today)`` 元组；
            task 无活跃 duration record 时返 None。target_minutes 可为 None
            （duration record 未设目标）。
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            row = DurationRepo.get_active(cursor, task_id)
            if row is None:
                return None
            derived = _derive_duration(cursor, row)
            return (
                int(row["target_minutes"])
                if row["target_minutes"] is not None
                else None,
                derived.accumulated_minutes_today,
            )

    def get_active_record(self, task_id: str) -> dict[str, Any]:
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            kind = _detect_kind(cursor, task_id)
            if kind is None:
                raise RecordNotFoundError(
                    f"task {task_id!r} 无活跃 record"
                )
            return self._assemble_view(cursor, task_id, kind)

    def patch_active_record(
        self, task_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        if not patch:
            raise RecordSchemaError("empty patch")
        now = _now_iso()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                kind = _detect_kind(cursor, task_id)
                if kind is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 无活跃 record"
                    )
                allowed = PATCH_FIELDS_BY_KIND[kind]
                invalid = set(patch.keys()) - allowed
                if invalid:
                    raise RecordSchemaError(
                        f"fields not allowed for kind {kind.value}: "
                        f"{sorted(invalid)}"
                    )
                try:
                    affected = self._patch_by_kind(cursor, task_id, kind, patch, now)
                except ValueError as e:
                    raise RecordSchemaError(str(e)) from e
                if affected == 0:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 无活跃 record（PATCH 未命中）"
                    )
                conn.commit()
                return self._assemble_view(cursor, task_id, kind)
            except (RecordSchemaError, RecordNotFoundError):
                conn.rollback()
                raise

    # ── progress mutate ─────────────────────────────────────────────────────

    def progress_increment(
        self, task_id: str, delta: int = 1
    ) -> dict[str, Any]:
        now = _now_iso()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                actual_kind = _detect_kind(cursor, task_id)
                if actual_kind is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 无活跃 record"
                    )
                if actual_kind is not RecordKind.PROGRESS:
                    raise RecordWrongKindError(
                        f"task {task_id!r} 的 record kind 是 {actual_kind.value}，"
                        "progress_increment 仅支持 progress kind"
                    )
                if _is_task_paused(cursor, task_id):
                    conn.rollback()
                    row = ProgressRepo.get_active(cursor, task_id)
                    derived = _derive_progress(row) if row else None
                    return {
                        "ok": True,
                        "noop": True,
                        "reason": "task_paused",
                        "current": int(row["current"]) if row else None,
                        "target": int(row["target"]) if row else None,
                        "status": row["status"] if row else None,
                        "derived": derived.model_dump() if derived else None,
                    }
                row = ProgressRepo.get_active(cursor, task_id)
                if row is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 无活跃 progress record"
                    )
                current = int(row["current"])
                target = int(row["target"])
                status = row["status"]

                if status != RecordStatus.ACTIVE.value and delta >= 0:
                    conn.rollback()
                    return {
                        "ok": True,
                        "noop": True,
                        "reason": "inactive",
                        "current": current,
                        "target": target,
                        "status": status,
                        "derived": _derive_progress(row).model_dump(),
                    }

                # recurring task 永不翻 completed（循环没有终点）。
                is_recurring = row["recurring_pattern"] is not None
                if delta >= 0:
                    new_current = min(current + delta, target)
                    if is_recurring:
                        new_status = RecordStatus.ACTIVE.value
                    else:
                        new_status = (
                            RecordStatus.COMPLETED.value
                            if new_current >= target
                            else status
                        )
                else:
                    # 负 delta 撤销：current 下穿 0 floor；status 保持不变——
                    # completed 任务的"撤销最后一次"不让任务重新激活，避免单次
                    # fire 抖动后又被算回来达标。重置需走 task delete + 重建。
                    new_current = max(current + delta, 0)
                    new_status = status

                ProgressRepo.update_progress(
                    cursor,
                    task_id=task_id,
                    new_current=new_current,
                    new_status=new_status,
                    now=now,
                )
                conn.commit()

                refreshed = ProgressRepo.get_active(cursor, task_id)
                derived = _derive_progress(refreshed) if refreshed else None
                return {
                    "ok": True,
                    "current": new_current,
                    "target": target,
                    "status": new_status,
                    "derived": derived.model_dump() if derived else None,
                }
            except (RecordNotFoundError, RecordWrongKindError):
                conn.rollback()
                raise

    # ── event mutate ────────────────────────────────────────────────────────

    def event_append(
        self, task_id: str, description: str, at: str | None = None
    ) -> dict[str, Any]:
        if not description:
            raise RecordSchemaError("description 不能为空")
        at_iso = at or _now_iso()
        _parse_iso(at_iso)
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                actual_kind = _detect_kind(cursor, task_id)
                if actual_kind is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 无活跃 record"
                    )
                if actual_kind is not RecordKind.EVENT:
                    raise RecordWrongKindError(
                        f"task {task_id!r} 的 record kind 是 {actual_kind.value}，"
                        "event_append 仅支持 event kind"
                    )
                if _is_task_paused(cursor, task_id):
                    conn.rollback()
                    derived = _derive_event(cursor, task_id)
                    return {
                        "ok": True,
                        "noop": True,
                        "reason": "task_paused",
                        "derived": derived.model_dump(),
                    }
                entry_id = EventRepo.insert_entry(
                    cursor, task_id=task_id, description=description, at=at_iso
                )
                conn.commit()
                derived = _derive_event(cursor, task_id)
                return {
                    "ok": True,
                    "entry_id": entry_id,
                    "derived": derived.model_dump(),
                }
            except (RecordNotFoundError, RecordSchemaError, RecordWrongKindError):
                conn.rollback()
                raise

    # ── duration mutate ─────────────────────────────────────────────────────

    def session_start(
        self, task_id: str, at: str | None = None
    ) -> dict[str, Any]:
        at_iso = at or _now_iso()
        _parse_iso(at_iso)
        now = _now_iso()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                actual_kind = _detect_kind(cursor, task_id)
                if actual_kind is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 无活跃 record"
                    )
                if actual_kind is not RecordKind.DURATION:
                    raise RecordWrongKindError(
                        f"task {task_id!r} 的 record kind 是 {actual_kind.value}，"
                        "session_start 仅支持 duration kind"
                    )
                if _is_task_paused(cursor, task_id):
                    conn.rollback()
                    row = DurationRepo.get_active(cursor, task_id)
                    derived = _derive_duration(cursor, row) if row else None
                    return {
                        "ok": True,
                        "noop": True,
                        "reason": "task_paused",
                        "derived": derived.model_dump() if derived else None,
                    }
                row = DurationRepo.get_active(cursor, task_id)
                if row is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 无活跃 duration record"
                    )
                if row["active_session_start_at"] is not None:
                    conn.rollback()
                    derived = _derive_duration(cursor, row)
                    return {
                        "ok": True,
                        "already_active": True,
                        "start_at": row["active_session_start_at"],
                        "derived": derived.model_dump(),
                    }
                DurationRepo.set_active_session_start(
                    cursor, task_id=task_id, start_at=at_iso, now=now
                )
                conn.commit()
                refreshed = DurationRepo.get_active(cursor, task_id)
                derived = (
                    _derive_duration(cursor, refreshed)
                    if refreshed
                    else None
                )
                return {
                    "ok": True,
                    "start_at": at_iso,
                    "status": refreshed["status"] if refreshed else None,
                    "derived": derived.model_dump() if derived else None,
                }
            except (RecordNotFoundError, RecordSchemaError, RecordWrongKindError):
                conn.rollback()
                raise

    def session_end(
        self, task_id: str, at: str | None = None
    ) -> dict[str, Any]:
        at_iso = at or _now_iso()
        end_dt = _parse_iso(at_iso)
        now = _now_iso()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                actual_kind = _detect_kind(cursor, task_id)
                if actual_kind is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 无活跃 record"
                    )
                if actual_kind is not RecordKind.DURATION:
                    raise RecordWrongKindError(
                        f"task {task_id!r} 的 record kind 是 {actual_kind.value}，"
                        "session_end 仅支持 duration kind"
                    )
                if _is_task_paused(cursor, task_id):
                    conn.rollback()
                    row = DurationRepo.get_active(cursor, task_id)
                    derived = _derive_duration(cursor, row) if row else None
                    return {
                        "ok": True,
                        "noop": True,
                        "reason": "task_paused",
                        "derived": derived.model_dump() if derived else None,
                    }
                row = DurationRepo.get_active(cursor, task_id)
                if row is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 无活跃 duration record"
                    )
                start_iso = row["active_session_start_at"]
                if start_iso is None:
                    raise RecordSchemaError(
                        "no active session to end"
                    )
                start_dt = _parse_iso(start_iso)
                if end_dt <= start_dt:
                    raise RecordSchemaError(
                        f"end_at {at_iso!r} <= start_at {start_iso!r}"
                    )
                duration_seconds = max(
                    int((end_dt - start_dt).total_seconds()), 0
                )
                DurationRepo.insert_session(
                    cursor,
                    task_id=task_id,
                    start_at=start_iso,
                    end_at=at_iso,
                    duration_seconds=duration_seconds,
                )
                DurationRepo.clear_active_session_start(
                    cursor, task_id=task_id, now=now
                )
                accumulated_seconds = DurationRepo.sum_seconds_active_period(
                    cursor, task_id=task_id
                )
                # recurring task 永不翻 completed（循环没有终点；本周期"已达标"
                # 防重复由达标条件项自身的边沿承担）。
                is_recurring = row["recurring_pattern"] is not None
                target_raw = row["target_minutes"]
                if (
                    not is_recurring
                    and target_raw is not None
                    and accumulated_seconds >= int(target_raw) * 60
                    and row["status"] == RecordStatus.ACTIVE.value
                ):
                    DurationRepo.set_status(
                        cursor,
                        task_id=task_id,
                        status=RecordStatus.COMPLETED.value,
                        now=now,
                    )
                conn.commit()
                refreshed = DurationRepo.get_active(cursor, task_id)
                derived = (
                    _derive_duration(cursor, refreshed)
                    if refreshed
                    else None
                )
                return {
                    "ok": True,
                    "this_session_minutes": duration_seconds // 60,
                    "status": refreshed["status"] if refreshed else None,
                    "derived": derived.model_dump() if derived else None,
                }
            except (RecordNotFoundError, RecordSchemaError, RecordWrongKindError):
                conn.rollback()
                raise

    # ── compute（独立 op，含历史日期 / 跨窗口） ───────────────────────────

    def compute_derived(
        self,
        task_id: str,
        *,
        window: str = "all",
        date: str | None = None,
    ) -> dict[str, Any]:
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            kind = _detect_kind(cursor, task_id)
            if kind is None:
                raise RecordNotFoundError(
                    f"task {task_id!r} 无活跃 record"
                )
            derived = self._compute_derived_inner(
                cursor, task_id, kind, window=window, date=date
            )
            return {
                "kind": kind.value,
                "window": window,
                "date": date,
                "derived": derived.model_dump(),
            }

    # ── 区间 compute（G1）─────────────────────────────────────────────────

    def compute_range(
        self,
        task_id: str,
        *,
        from_date: str,
        to_date: str,
    ) -> dict[str, Any]:
        """区间 derived 聚合（``YYYY-MM-DD`` ~ ``YYYY-MM-DD`` 含两端）。

        各 kind 语义：
        - progress：区间内 archive 行的 current 累加（"这段时间累计完成多少"）
        - duration：区间内 session 子表（按 start_at 过滤）的 duration_seconds
          累加后 // 60；含未归档当前 period 的 session（archived_at IS NULL）
        - event：区间内 entry 按 at 字段过滤的 count

        与 ``compute_derived`` 的 ``window`` / ``date`` 互斥（router 层兜底
        校验）。
        """
        self._validate_date_str(from_date, "from")
        self._validate_date_str(to_date, "to")
        if from_date > to_date:
            raise RecordSchemaError(
                f"from={from_date!r} > to={to_date!r}"
            )

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            kind = _detect_kind(cursor, task_id)
            if kind is None:
                raise RecordNotFoundError(
                    f"task {task_id!r} 无活跃 record"
                )

            from_start_ms, _ = _local_date_to_ms_range(from_date)
            _, to_end_ms = _local_date_to_ms_range(to_date)

            if kind is RecordKind.PROGRESS:
                rows = cursor.execute(
                    "SELECT current, target, archived_at FROM task_record_progress "
                    "WHERE task_id = ? AND archived_at IS NOT NULL "
                    "AND archived_at >= ? AND archived_at < ? "
                    "ORDER BY archived_at DESC",
                    (task_id, from_start_ms, to_end_ms),
                ).fetchall()
                total = sum(int(r["current"]) for r in rows)
                target_recent = int(rows[0]["target"]) if rows else None
                derived = DerivedProgressRange(
                    days_with_data=len(rows),
                    total_current=total,
                    target_recent=target_recent,
                )
            elif kind is RecordKind.DURATION:
                # session 子表按 start_at(ms) 过滤;含未归档(archived_at IS NULL)
                sessions = cursor.execute(
                    "SELECT duration_seconds, start_at "
                    "FROM task_record_duration_session "
                    "WHERE task_id = ? "
                    "AND start_at >= ? AND start_at < ?",
                    (task_id, from_start_ms, to_end_ms),
                ).fetchall()
                total_seconds = sum(int(s["duration_seconds"]) for s in sessions)
                days_with_data = len(
                    {_ms_to_local_date(s["start_at"]) for s in sessions}
                )
                # target_minutes 取当前活跃行或最近归档行
                target_row = cursor.execute(
                    "SELECT target_minutes FROM task_record_duration "
                    "WHERE task_id = ? "
                    "ORDER BY (archived_at IS NULL) DESC, archived_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                target_minutes_recent = (
                    int(target_row["target_minutes"])
                    if target_row and target_row["target_minutes"] is not None
                    else None
                )
                derived = DerivedDurationRange(
                    days_with_data=days_with_data,
                    total_minutes=total_seconds // 60,
                    target_minutes_recent=target_minutes_recent,
                )
            else:  # event
                rows = cursor.execute(
                    "SELECT at FROM task_record_event_entry "
                    "WHERE task_id = ? AND at >= ? AND at < ?",
                    (task_id, from_start_ms, to_end_ms),
                ).fetchall()
                total_count = len(rows)
                days_with_data = len({_ms_to_local_date(r["at"]) for r in rows})
                derived = DerivedEventRange(
                    days_with_data=days_with_data,
                    total_count=total_count,
                )

            return {
                "kind": kind.value,
                "from": from_date,
                "to": to_date,
                "derived": derived.model_dump(),
            }

    # ── archive list（G2）────────────────────────────────────────────────

    def list_archives(self, task_id: str) -> dict[str, Any]:
        """列出某 task 全部 archive 行 + 每日 derived 快照。

        各 kind 列出的字段：
        - progress：每行 ``{date, current, target, status}``（按 archived_at DESC）
        - duration：每日 ``{date, accumulated_minutes}``（GROUP BY session 子表 day）
        - event：每日 ``{date, count}``（GROUP BY entry.at day）
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            kind = _detect_kind(cursor, task_id)
            if kind is None:
                raise RecordNotFoundError(
                    f"task {task_id!r} 无活跃 record"
                )

            if kind is RecordKind.PROGRESS:
                rows = cursor.execute(
                    "SELECT archived_at, current, target, status "
                    "FROM task_record_progress "
                    "WHERE task_id = ? AND archived_at IS NOT NULL "
                    "ORDER BY archived_at DESC",
                    (task_id,),
                ).fetchall()
                archives = [
                    {
                        "date": _ms_to_local_date(r["archived_at"]),
                        "current": int(r["current"]),
                        "target": int(r["target"]),
                        "status": r["status"],
                    }
                    for r in rows
                ]
            elif kind is RecordKind.DURATION:
                # 按 deploy_timezone() 日期聚合在 Python 端做:start_at 是 ms,
                # SQLite GROUP BY 按 ms 字面或 strftime 本地时区都不能精确对齐 deploy_timezone()。
                rows = cursor.execute(
                    "SELECT start_at, duration_seconds "
                    "FROM task_record_duration_session "
                    "WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
                day_to_secs: dict[str, int] = {}
                for r in rows:
                    day = _ms_to_local_date(r["start_at"])
                    day_to_secs[day] = day_to_secs.get(day, 0) + int(
                        r["duration_seconds"]
                    )
                archives = [
                    {"date": day, "accumulated_minutes": secs // 60}
                    for day, secs in sorted(
                        day_to_secs.items(), reverse=True
                    )
                ]
            else:  # event
                rows = cursor.execute(
                    "SELECT at FROM task_record_event_entry WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
                day_to_count: dict[str, int] = {}
                for r in rows:
                    day = _ms_to_local_date(r["at"])
                    day_to_count[day] = day_to_count.get(day, 0) + 1
                archives = [
                    {"date": day, "count": cnt}
                    for day, cnt in sorted(
                        day_to_count.items(), reverse=True
                    )
                ]

            return {"kind": kind.value, "archives": archives}

    # ── summary 聚合(spec 2026-06-11) ───────────────────────────────────────

    def list_active_summaries(
        self, window: str
    ) -> dict[str, RecordSummary]:
        """一次性出所有 task 的 record 摘要,供 summary 接口聚合用。

        - window='day': progress 走 snapshot(忽略 window),duration/event 走今日累计
        - window='all': progress 同 snapshot,duration 仅含 target+session,event 不含 count_today

        单点失败不传染:某条 derive 抛异常 → 该条不进 result,log warning 即可。
        """
        if window not in ("day", "all"):
            raise ValueError(
                f"window must be 'day' or 'all', got {window!r}"
            )

        result: dict[str, RecordSummary] = {}
        now = datetime.now(deploy_timezone())
        window_remaining = (
            _build_window_remaining(window, now) if window == "day" else None
        )

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # progress: 永远 snapshot
            for row in cursor.execute(
                "SELECT * FROM task_record_progress WHERE archived_at IS NULL"
            ).fetchall():
                try:
                    d = _derive_progress(row)
                    derived = {
                        "target": row["target"],
                        "current": row["current"],
                        "unit": row["unit"],
                        "remaining": d.remaining,
                        "progress_pct": d.progress_pct,
                    }
                    result[row["task_id"]] = RecordSummary(
                        kind="progress",
                        completed=(row["status"] == "completed"),
                        active_session=None,
                        window_remaining=window_remaining,
                        derived=derived,
                    )
                except Exception as exc:
                    logger.warning(
                        "summary derive failed task=%s kind=progress: %s",
                        row["task_id"], exc,
                    )

            # duration
            for raw_row in cursor.execute(
                "SELECT * FROM task_record_duration WHERE archived_at IS NULL"
            ).fetchall():
                row = _row_to_duration(raw_row)
                try:
                    if window == "day":
                        d = _derive_duration(cursor, row)
                        derived = {
                            "target_minutes": row["target_minutes"],
                            "accumulated_minutes_today": d.accumulated_minutes_today,
                            "remaining_minutes": d.remaining_minutes,
                            "active_session_start_at": d.active_session_start_at,
                        }
                    else:  # window == 'all'
                        derived = {
                            "target_minutes": row["target_minutes"],
                            "active_session_start_at": row["active_session_start_at"],
                        }
                    result[row["task_id"]] = RecordSummary(
                        kind="duration",
                        completed=(row["status"] == "completed"),
                        active_session=_build_active_session(row, now),
                        window_remaining=window_remaining,
                        derived=derived,
                    )
                except Exception as exc:
                    logger.warning(
                        "summary derive failed task=%s kind=duration: %s",
                        row["task_id"], exc,
                    )

            # event
            for row in cursor.execute(
                "SELECT * FROM task_record_event"
            ).fetchall():
                try:
                    d = _derive_event(cursor, row["task_id"])
                    derived = {
                        "count_total": d.count_total,
                        "last_at": d.last_at,
                    }
                    if window == "day":
                        derived["count_today"] = d.count_today
                    result[row["task_id"]] = RecordSummary(
                        kind="event",
                        completed=(row["status"] == "completed"),
                        active_session=None,
                        window_remaining=window_remaining,
                        derived=derived,
                    )
                except Exception as exc:
                    logger.warning(
                        "summary derive failed task=%s kind=event: %s",
                        row["task_id"], exc,
                    )

        return result

    @staticmethod
    def _validate_date_str(date_str: str, field_name: str) -> None:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError as e:
            raise RecordSchemaError(
                f"{field_name}={date_str!r} 不是合法 YYYY-MM-DD 格式"
            ) from e

    # ── rollover（P3 复用） ─────────────────────────────────────────────────

    def rollover_one(
        self, task_id: str, kind: RecordKind, now: datetime | None = None
    ) -> None:
        """单 task rollover（spec §6.3）。

        progress / duration 各自一笔事务：archive 旧活跃行 + INSERT 新活跃行；
        duration 若有活跃 session 切两段（旧 session end=now、新 period start=now）。
        event kind 不进 rollover（longterm 设计），调用方需保证只传 progress/duration。
        """
        if kind is RecordKind.EVENT:
            return
        now_dt = now or datetime.now(deploy_timezone())
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=deploy_timezone())
        now_iso = ms_to_iso_local(int(now_dt.timestamp() * 1000))
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                if kind is RecordKind.PROGRESS:
                    row = ProgressRepo.get_active(cursor, task_id)
                    if row is None or row["recurring_pattern"] is None:
                        conn.rollback()
                        return
                    archived_at_period = _archived_at_for_rollover(
                        row["window"], now_dt
                    )
                    ProgressRepo.archive_active(
                        cursor, task_id=task_id, archived_at=archived_at_period
                    )
                    ProgressRepo.insert_active(
                        cursor,
                        task_id=task_id,
                        target=int(row["target"]),
                        unit=row["unit"],
                        window=row["window"],
                        recurring_pattern=row["recurring_pattern"],
                        expires_at=row["expires_at"],
                        now=now_iso,
                    )
                elif kind is RecordKind.DURATION:
                    row = DurationRepo.get_active(cursor, task_id)
                    if row is None or row["recurring_pattern"] is None:
                        conn.rollback()
                        return
                    pattern_obj = json.loads(row["recurring_pattern"])
                    window = (
                        pattern_obj.get("window", "day")
                        if isinstance(pattern_obj, dict)
                        else "day"
                    )
                    archived_at_period = _archived_at_for_rollover(window, now_dt)
                    active_start = row["active_session_start_at"]
                    if active_start is not None:
                        duration_seconds = max(
                            _seconds_between(active_start, now_iso),
                            0,
                        )
                        DurationRepo.insert_session(
                            cursor,
                            task_id=task_id,
                            start_at=active_start,
                            end_at=now_iso,
                            duration_seconds=duration_seconds,
                        )
                    DurationRepo.archive_active_sessions(
                        cursor, task_id=task_id, archived_at=archived_at_period
                    )
                    DurationRepo.archive_active(
                        cursor, task_id=task_id, archived_at=archived_at_period
                    )
                    target_carry = row["target_minutes"]
                    DurationRepo.insert_active(
                        cursor,
                        task_id=task_id,
                        target_minutes=(
                            int(target_carry) if target_carry is not None else None
                        ),
                        active_session_start_at=(
                            now_iso if active_start is not None else None
                        ),
                        recurring_pattern=row["recurring_pattern"],
                        expires_at=row["expires_at"],
                        now=now_iso,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ── terminate（P4 复用） ────────────────────────────────────────────────

    def write_terminate_log(
        self, task_id: str, reason: TerminateReason
    ) -> None:
        """own connection 版本（独立单测使用）。**不删 task / record**（调用方负责）。"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                self.write_terminate_log_in_tx(cursor, task_id, reason)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def write_terminate_log_in_tx(
        self,
        cursor: sqlite3.Cursor,
        task_id: str,
        reason: TerminateReason,
    ) -> None:
        """外层事务版本：用 caller 提供的 cursor 写审计行，不 own connection。

        给 ``TaskService.delete_task`` 这类要把"审计 + 删 rule + 删 task"装在
        单事务里的场景用。task 不存在抛 ``TaskNotFoundError``；record 不存在
        静默返（task 可能无 record）。
        """
        task_row = cursor.execute(
            "SELECT description FROM task WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task_row is None:
            raise TaskNotFoundError(f"task {task_id!r} 不存在")
        kind = _detect_kind(cursor, task_id)
        if kind is None:
            return
        snapshot = _build_final_snapshot(cursor, kind, task_id)
        TerminateLogRepo.insert(
            cursor,
            task_id=task_id,
            kind=kind,
            reason=reason.value,
            description=task_row["description"],
            final_snapshot=snapshot,
            terminated_at=_now_iso(),
        )

    def prune_terminate_log(
        self, now: datetime | None = None
    ) -> int:
        """own connection 版本：删 30 天外的审计行。返回删除行数。"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            try:
                deleted = self.prune_terminate_log_in_tx(cursor, now)
                conn.commit()
                return deleted
            except Exception:
                conn.rollback()
                raise

    def prune_terminate_log_in_tx(
        self,
        cursor: sqlite3.Cursor,
        now: datetime | None = None,
    ) -> int:
        """外层事务版本：用 caller 提供的 cursor 删 30 天外的审计行。"""
        now_dt = now or datetime.now(deploy_timezone())
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=deploy_timezone())
        cutoff = ms_to_iso_local(
            int((now_dt - timedelta(days=30)).timestamp() * 1000)
        )
        return TerminateLogRepo.prune_older_than(cursor, cutoff=cutoff)

    # ── 内部 helper ─────────────────────────────────────────────────────────

    def _validate_init_content(
        self, kind: RecordKind, content: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            if kind is RecordKind.PROGRESS:
                return ProgressInitContent.model_validate(content).model_dump()
            if kind is RecordKind.DURATION:
                return DurationInitContent.model_validate(content).model_dump()
            if kind is RecordKind.EVENT:
                return EventInitContent.model_validate(content).model_dump()
        except Exception as e:
            raise RecordSchemaError(f"content schema invalid: {e}") from e
        raise RecordSchemaError(f"unknown kind {kind!r}")

    def _insert_by_kind(
        self,
        cursor: sqlite3.Cursor,
        task_id: str,
        kind: RecordKind,
        content: dict[str, Any],
        now: str,
    ) -> None:
        if kind is RecordKind.PROGRESS:
            ProgressRepo.insert_active(
                cursor,
                task_id=task_id,
                target=content["target"],
                unit=content["unit"],
                window=(
                    content["window"].value
                    if hasattr(content["window"], "value")
                    else content["window"]
                ),
                recurring_pattern=content.get("recurring_pattern"),
                expires_at=content.get("expires_at"),
                now=now,
            )
        elif kind is RecordKind.DURATION:
            DurationRepo.insert_active(
                cursor,
                task_id=task_id,
                target_minutes=content.get("target_minutes"),
                active_session_start_at=None,
                recurring_pattern=content.get("recurring_pattern"),
                expires_at=content.get("expires_at"),
                now=now,
            )
        elif kind is RecordKind.EVENT:
            EventRepo.insert_active(
                cursor,
                task_id=task_id,
                recurring_pattern=content.get("recurring_pattern"),
                expires_at=content.get("expires_at"),
                now=now,
            )

    def _patch_by_kind(
        self,
        cursor: sqlite3.Cursor,
        task_id: str,
        kind: RecordKind,
        patch: dict[str, Any],
        now: str,
    ) -> int:
        _validate_patch_values(kind, patch)
        if kind is RecordKind.PROGRESS:
            affected = ProgressRepo.update_active(
                cursor, task_id=task_id, patch=patch, now=now
            )
            if affected and "target" in patch:
                _recompute_progress_status(cursor, task_id, now)
            return affected
        if kind is RecordKind.DURATION:
            affected = DurationRepo.update_active(
                cursor, task_id=task_id, patch=patch, now=now
            )
            if affected and "target_minutes" in patch:
                _recompute_duration_status(cursor, task_id, now)
            return affected
        return EventRepo.update_active(
            cursor, task_id=task_id, patch=patch, now=now
        )

    def _assemble_view(
        self, cursor: sqlite3.Cursor, task_id: str, kind: RecordKind
    ) -> dict[str, Any]:
        if kind is RecordKind.PROGRESS:
            row = ProgressRepo.get_active(cursor, task_id)
            if row is None:
                raise RecordNotFoundError(
                    f"task {task_id!r} 无活跃 progress record"
                )
            derived = _derive_progress(row)
            return {
                "kind": kind.value,
                "record": _row_to_dict(row),
                "derived": derived.model_dump(),
            }
        if kind is RecordKind.DURATION:
            row = DurationRepo.get_active(cursor, task_id)
            if row is None:
                raise RecordNotFoundError(
                    f"task {task_id!r} 无活跃 duration record"
                )
            sessions = DurationRepo.list_active_sessions(
                cursor, task_id=task_id
            )
            derived = _derive_duration(cursor, row)
            return {
                "kind": kind.value,
                "record": _row_to_dict(row),
                "sessions": [_row_to_dict(s) for s in sessions],
                "derived": derived.model_dump(),
            }
        row = EventRepo.get_active(cursor, task_id)
        if row is None:
            raise RecordNotFoundError(
                f"task {task_id!r} 无活跃 event record"
            )
        entries = EventRepo.list_entries(cursor, task_id=task_id)
        derived = _derive_event(cursor, task_id)
        return {
            "kind": kind.value,
            "record": _row_to_dict(row),
            "entries": [_row_to_dict(e) for e in entries],
            "derived": derived.model_dump(),
        }

    def _compute_derived_inner(
        self,
        cursor: sqlite3.Cursor,
        task_id: str,
        kind: RecordKind,
        *,
        window: str,
        date: str | None,
    ) -> DerivedProgress | DerivedDuration | DerivedEvent:
        """compute op：支持历史日期 / 跨窗口；与 mutate 响应内的 derived 互补。

        ``window`` ∈ ``all`` / ``day`` / ``week`` / ``month``。``date`` 是
        ``YYYY-MM-DD``，与 ``window`` 互斥（``date`` 给定时按 archive_at 日前缀
        查归档行；``window`` 给定时按当前 period 范围内的 entry / session 过滤）。
        """
        if window not in ("all", "day", "week", "month"):
            raise RecordSchemaError(
                f"window 取值非法: {window!r}，仅支持 all/day/week/month"
            )
        if date is not None and window != "all":
            raise RecordSchemaError(
                "date 与 window 互斥：传 date 时 window 必为 all（默认）"
            )

        if kind is RecordKind.PROGRESS:
            if date is not None:
                day_start_ms, day_end_ms = _local_date_to_ms_range(date)
                row = cursor.execute(
                    "SELECT * FROM task_record_progress "
                    "WHERE task_id = ? AND archived_at >= ? AND archived_at < ? "
                    "ORDER BY archived_at DESC LIMIT 1",
                    (task_id, day_start_ms, day_end_ms),
                ).fetchone()
                if row is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 在 {date!r} 无 progress 归档"
                    )
                return _derive_progress(row)
            if window != "all":
                # progress 是单时刻快照（current / target），按 day/week/month 切
                # 窗口语义未定义；统一 reject 避免返"看似成功但语义不明"的结果。
                raise RecordSchemaError(
                    f"progress kind 不支持 window={window!r}；要查历史用 --date YYYY-MM-DD"
                )
            row = ProgressRepo.get_active(cursor, task_id)
            if row is None:
                raise RecordNotFoundError(
                    f"task {task_id!r} 无活跃 progress record"
                )
            return _derive_progress(row)

        if kind is RecordKind.DURATION:
            if date is not None:
                day_start_ms, day_end_ms = _local_date_to_ms_range(date)
                row = cursor.execute(
                    "SELECT * FROM task_record_duration "
                    "WHERE task_id = ? AND archived_at >= ? AND archived_at < ? "
                    "ORDER BY archived_at DESC LIMIT 1",
                    (task_id, day_start_ms, day_end_ms),
                ).fetchone()
                if row is None:
                    raise RecordNotFoundError(
                        f"task {task_id!r} 在 {date!r} 无 duration 归档"
                    )
                sum_row = cursor.execute(
                    "SELECT COALESCE(SUM(duration_seconds), 0) AS total "
                    "FROM task_record_duration_session "
                    "WHERE task_id = ? AND archived_at >= ? AND archived_at < ?",
                    (task_id, day_start_ms, day_end_ms),
                ).fetchone()
                accumulated_seconds = int(sum_row["total"]) if sum_row else 0
                accumulated_minutes = accumulated_seconds // 60
                target = row["target_minutes"]
                remaining = (
                    max(int(target) - accumulated_minutes, 0)
                    if target is not None
                    else None
                )
                return DerivedDuration(
                    accumulated_minutes_today=accumulated_minutes,
                    remaining_minutes=remaining,
                    active_session_start_at=None,
                )
            if window not in ("all", "day"):
                # duration 当前实现按"今日"语义算（_derive_duration 用 today
                # 子表 sum）；week/month 跨期聚合未实现，明确拒。
                raise RecordSchemaError(
                    f"duration kind window={window!r} 未实现；当前仅支持 all/day"
                )
            row = DurationRepo.get_active(cursor, task_id)
            if row is None:
                raise RecordNotFoundError(
                    f"task {task_id!r} 无活跃 duration record"
                )
            return _derive_duration(cursor, row)

        # event kind
        if date is not None:
            total = EventRepo.count_entries_with_prefix(
                cursor, task_id=task_id, at_prefix=date
            )
            day_start_ms, day_end_ms = _local_date_to_ms_range(date)
            row = cursor.execute(
                "SELECT MAX(at) AS last_at FROM task_record_event_entry "
                "WHERE task_id = ? AND at >= ? AND at < ?",
                (task_id, day_start_ms, day_end_ms),
            ).fetchone()
            last_at = (
                ms_to_iso_local(row["last_at"])
                if row and row["last_at"] is not None
                else None
            )
            return DerivedEvent(
                count_total=total, count_today=total, last_at=last_at
            )
        if window == "day":
            today = _today_prefix()
            total = EventRepo.count_entries_with_prefix(
                cursor, task_id=task_id, at_prefix=today
            )
            return DerivedEvent(
                count_total=total,
                count_today=total,
                last_at=EventRepo.last_entry_at(cursor, task_id=task_id),
            )
        if window == "week":
            week_prefixes = _week_date_prefixes(datetime.now(deploy_timezone()))
            total = sum(
                EventRepo.count_entries_with_prefix(
                    cursor, task_id=task_id, at_prefix=p
                )
                for p in week_prefixes
            )
            return DerivedEvent(
                count_total=total,
                count_today=EventRepo.count_entries_with_prefix(
                    cursor, task_id=task_id, at_prefix=_today_prefix()
                ),
                last_at=EventRepo.last_entry_at(cursor, task_id=task_id),
            )
        if window == "month":
            month_prefix = datetime.now(deploy_timezone()).strftime("%Y-%m")
            total = EventRepo.count_entries_with_prefix(
                cursor, task_id=task_id, at_prefix=month_prefix
            )
            return DerivedEvent(
                count_total=total,
                count_today=EventRepo.count_entries_with_prefix(
                    cursor, task_id=task_id, at_prefix=_today_prefix()
                ),
                last_at=EventRepo.last_entry_at(cursor, task_id=task_id),
            )
        # window == "all"
        return _derive_event(cursor, task_id)


def _week_date_prefixes(now: datetime) -> list[str]:
    """当前周（周一起到今天）的日前缀列表，用于 event count 累加。"""
    monday = (now - timedelta(days=now.weekday())).date()
    today = now.date()
    days = (today - monday).days + 1
    return [(monday + timedelta(days=i)).isoformat() for i in range(days)]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d: dict[str, Any] = dict(row)
    pattern = d.get("recurring_pattern")
    if isinstance(pattern, str):
        try:
            d["recurring_pattern"] = json.loads(pattern)
        except json.JSONDecodeError:
            pass
    return d
