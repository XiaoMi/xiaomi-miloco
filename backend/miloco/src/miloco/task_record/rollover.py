# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task_record rollover 调度（spec 2026-06-10 §6.3）。

每日 0:05 (Asia/Shanghai) 触发：扫 progress / duration 主表所有
recurring + 活跃行，按 ``window`` 边界（day/week/month）判断今天是否到点，
到点则调 ``TaskRecordService.rollover_one`` 完成 archive + insert new。

设计选择（偏离 plan §P3，原因记在 commit message）：
- 不引入 ``apscheduler``——沿用 ``_daily_maintenance_loop`` 的 ``asyncio.sleep`` 模式，
  在 ``main.py`` lifespan 中 ``create_task`` 一个 daemon coroutine。
- ``recurring_pattern`` 不做 cron-like 解析（plugin 端无此 parser，spec 也未
  约束格式）——只识别 ``{window: "day|week|month|longterm"}`` 这一种结构。
  progress kind 优先用 ``progress.window`` 字段，duration kind 用
  ``recurring_pattern.window``，缺省按 ``day`` 处理。

self-heal：daemon 启动时立即跑一次，跨日重启窗口的漏滚动场景由
``_should_rollover_today`` 内的"last_archived_at < 当前 period_start"判断
覆盖，重复跑无副作用（partial unique index 兜底）。
"""

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from miloco.task_record.repo import DurationRepo, ProgressRepo
from miloco.task_record.schema import RecordKind
from miloco.task_record.service import TaskRecordService
from miloco.utils.time_utils import deploy_timezone


def _to_aware(dt: datetime) -> datetime:
    """naive datetime 按 deploy_timezone() 解读补 tzinfo，aware 保持不变。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=deploy_timezone())
    return dt


def _parse_iso_aware(s) -> datetime:
    """ISO 字符串 → aware datetime;int 视为 Unix ms。

    DB 时间字段 v10 起是 INTEGER ms,某些 helper (如 _last_archived_at 直接 SELECT MAX)
    返回 int;repo 出口转字符串。两种入参都接受。
    """
    from datetime import timezone

    if isinstance(s, int):
        return datetime.fromtimestamp(s / 1000, tz=timezone.utc).astimezone(
            deploy_timezone()
        )
    return _to_aware(datetime.fromisoformat(s.replace("Z", "+00:00")))

logger = logging.getLogger(__name__)


def _parse_pattern(pattern: str | None) -> dict[str, Any] | None:
    if not pattern:
        return None
    try:
        loaded = json.loads(pattern)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _window_period_start(window: str, now: datetime) -> datetime:
    """该 window 当前 period 的起点（与 ``now`` 同时区——naive 进 naive 出）。"""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "day":
        return midnight
    if window == "week":
        return midnight - timedelta(days=now.weekday())
    if window == "month":
        return midnight.replace(day=1)
    return datetime.min.replace(tzinfo=now.tzinfo)  # longterm：永远不到边界


def _should_rollover(
    row: sqlite3.Row,
    kind: RecordKind,
    now: datetime,
    prev_archived_at: str | None,
) -> bool:
    """判断 task 是否需要在本次 daily job 跑 rollover。

    判据：
    1. recurring_pattern 非空（必须是 recurring 任务）
    2. window 非 longterm
    3. 上次 archive 早于上一 period 的结束（含从未 archive）

    archived_at 现在写「归档 period 的 period_end」（不是 rollover 触发时刻），
    所以判据用 prev_archived_at < prev_period_end —— 等于上 period 的 end
    时（刚归档过）即 noop；从未归档时回退看 created_at。
    """
    pattern = _parse_pattern(row["recurring_pattern"])
    if pattern is None:
        return False
    if kind is RecordKind.PROGRESS:
        window = row["window"]
    else:
        window = pattern.get("window", "day")
    if window == "longterm":
        return False

    now_aware = _to_aware(now)
    period_start = _window_period_start(window, now_aware)
    prev_period_end = period_start - timedelta(seconds=1)

    if prev_archived_at is None:
        try:
            created_dt = _parse_iso_aware(row["created_at"])
        except ValueError:
            return True
        return created_dt < period_start

    try:
        prev_dt = _parse_iso_aware(prev_archived_at)
    except ValueError:
        return True
    return prev_dt < prev_period_end


def _last_archived_at(
    cursor: sqlite3.Cursor, table: str, task_id: str
) -> str | None:
    row = cursor.execute(
        f"SELECT MAX(archived_at) AS ts FROM {table} "
        "WHERE task_id = ? AND archived_at IS NOT NULL",
        (task_id,),
    ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None


def rollover_daily_job(
    service: TaskRecordService,
    now: datetime | None = None,
    on_duration_rollover: Callable[
        [str, tuple[int | None, int] | None], None
    ] | None = None,
) -> dict[str, int]:
    """扫所有 recurring + 到边界的 progress / duration 活跃行做 rollover。

    ``now`` 缺省时用 ``datetime.now(deploy_timezone())`` —— 不能用 naive
    ``datetime.now()`` 兜底，否则在 UTC 容器（Docker 默认）下会拿到 UTC
    时间，被 ``_to_aware`` 误标本地后比真实部署时间差几个小时,
    period_start 也跟着错位，导致 rollover 静默跳过。

    ``on_duration_rollover``：每条 duration record rollover 成功后调；签名
    ``(task_id, pre_rollover_state)``，pre_rollover_state 是 rollover_one
    执行前 snapshot 的 ``(target_minutes, accumulated_minutes_today)``，用于
    rule engine 跨日 fire on_target 兜底（rollover 已清旧累计，必须用
    snapshot 判断旧一天是否达标）。回调异常单独 try/except 包住，不影响后续 task。

    Returns:
        统计 ``{"progress": N, "duration": N, "skipped": N, "failed": N}``。
    """
    now_dt = now or datetime.now(deploy_timezone())
    counts = {"progress": 0, "duration": 0, "skipped": 0, "failed": 0}

    with service.db.get_connection() as conn:
        cursor = conn.cursor()
        progress_rows = ProgressRepo.list_recurring_active(cursor)
        duration_rows = DurationRepo.list_recurring_active(cursor)

    for row in progress_rows:
        task_id = row["task_id"]
        with service.db.get_connection() as conn:
            prev = _last_archived_at(conn.cursor(), "task_record_progress", task_id)
        if not _should_rollover(row, RecordKind.PROGRESS, now_dt, prev):
            counts["skipped"] += 1
            continue
        try:
            service.rollover_one(task_id, RecordKind.PROGRESS, now_dt)
            counts["progress"] += 1
        except Exception as e:  # noqa: BLE001
            logger.error("Progress rollover failed for %s: %s", task_id, e)
            counts["failed"] += 1

    for row in duration_rows:
        task_id = row["task_id"]
        with service.db.get_connection() as conn:
            prev = _last_archived_at(conn.cursor(), "task_record_duration", task_id)
        if not _should_rollover(row, RecordKind.DURATION, now_dt, prev):
            counts["skipped"] += 1
            continue
        # rollover_one 会清旧累计；snapshot 必须先于 archive 取，否则 rule
        # engine 跨日兜底拿不到"旧一天达标"信号。
        pre_state = service.read_duration_target_state(task_id)
        try:
            service.rollover_one(task_id, RecordKind.DURATION, now_dt)
            counts["duration"] += 1
        except Exception as e:  # noqa: BLE001
            logger.error("Duration rollover failed for %s: %s", task_id, e)
            counts["failed"] += 1
            continue
        if on_duration_rollover is not None:
            try:
                on_duration_rollover(task_id, pre_state)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Duration rollover hook (rule engine notify) "
                    "failed for %s: %s",
                    task_id, e,
                )

    logger.info("Rollover daily job done: %s", counts)
    return counts


def seconds_until_next_run(now: datetime, hour: int = 0, minute: int = 5) -> float:
    """到下一个 ``HH:MM`` 时刻的秒数（用于 daily loop sleep）。"""
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()
