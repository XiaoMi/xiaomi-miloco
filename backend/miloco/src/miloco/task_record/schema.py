# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task_record / task_terminate_log 数据模型 + 字段白名单（spec 2026-06-10）。

包含：
- 3 个 enum：``RecordKind`` / ``RecordStatus`` / ``TerminateReason``
- 1 个辅助 enum：``ProgressWindow``
- 6 个表行模型（``ProgressRecord`` / ``DurationRecord`` / ``DurationSession``
  / ``EventRecord`` / ``EventEntry`` / ``TerminateLogEntry``）
- 3 个 derived 派生量模型（按 kind 多态）
- 4 个 mutate 返回模型
- 3 个 init content 校验模型（``POST /tasks/{id}/record`` body.content 按 kind 分发）
- 3 个字段白名单常量 + 1 个 by-kind 字典

注意 ``task_record_*.status``（``active``/``completed``）与现有
``task.status``（``active``/``paused``）虽字面同名但语义独立，模型层用
``RecordStatus`` enum 与 ``task`` 表的 status 字段隔离。
"""

import re
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field

_TASK_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")


# ── enum ─────────────────────────────────────────────────────────────────────


class RecordKind(str, Enum):
    PROGRESS = "progress"
    DURATION = "duration"
    EVENT = "event"


class RecordStatus(str, Enum):
    """``task_record_*.status`` 取值。仅 2 态，与 ``task.status`` 无关。"""

    ACTIVE = "active"
    COMPLETED = "completed"


class TerminateReason(str, Enum):
    COMPLETED = "completed"
    EXPIRED = "expired"
    ABANDONED = "abandoned"


class ProgressWindow(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    LONGTERM = "longterm"


# ── 表行模型 ──────────────────────────────────────────────────────────────────


class ProgressRecord(BaseModel):
    id: int
    task_id: str
    target: int
    current: int
    unit: str
    window: ProgressWindow
    recurring_pattern: str | None = None
    expires_at: str | None = None
    status: RecordStatus
    archived_at: str | None = None
    created_at: str
    updated_at: str


class DurationRecord(BaseModel):
    id: int
    task_id: str
    target_minutes: int | None = None
    active_session_start_at: str | None = None
    recurring_pattern: str | None = None
    expires_at: str | None = None
    status: RecordStatus
    archived_at: str | None = None
    created_at: str
    updated_at: str


class DurationSession(BaseModel):
    id: int
    task_id: str
    start_at: str
    end_at: str
    duration_seconds: int
    archived_at: str | None = None


class EventRecord(BaseModel):
    id: int
    task_id: str
    recurring_pattern: str | None = None
    expires_at: str | None = None
    status: RecordStatus
    created_at: str
    updated_at: str


class EventEntry(BaseModel):
    id: int
    task_id: str
    description: str
    at: str


class TerminateLogEntry(BaseModel):
    id: int
    task_id: str
    kind: RecordKind
    reason: TerminateReason
    description: str
    final_snapshot: str
    terminated_at: str


# ── derived（按 kind 多态） ───────────────────────────────────────────────────


class DerivedProgress(BaseModel):
    kind: Literal["progress"] = "progress"
    remaining: int
    progress_pct: float


class DerivedDuration(BaseModel):
    kind: Literal["duration"] = "duration"
    accumulated_minutes_today: int
    remaining_minutes: int | None = None
    active_session_start_at: str | None = None


class DerivedEvent(BaseModel):
    kind: Literal["event"] = "event"
    count_total: int
    count_today: int
    last_at: str | None = None


Derived = Union[DerivedProgress, DerivedDuration, DerivedEvent]


# ── range derived（区间聚合，spec G1） ───────────────────────────────────────


class DerivedProgressRange(BaseModel):
    """progress kind 区间聚合 derived。"""

    kind: Literal["progress_range"] = "progress_range"
    days_with_data: int
    total_current: int  # 区间内每日 archive 行的 current 累加（"用户在这段时间累计完成多少"）
    target_recent: int | None = None  # 区间内最近一日 archive 的 target（用户最后设的目标）


class DerivedDurationRange(BaseModel):
    """duration kind 区间聚合 derived（按 session.start_at 过滤，含未归档当前 period）。"""

    kind: Literal["duration_range"] = "duration_range"
    days_with_data: int
    total_minutes: int  # 区间内全部 session 的 duration_seconds 累加后 // 60
    target_minutes_recent: int | None = None


class DerivedEventRange(BaseModel):
    """event kind 区间聚合 derived。"""

    kind: Literal["event_range"] = "event_range"
    days_with_data: int
    total_count: int


# ── mutate 返回 ───────────────────────────────────────────────────────────────


class ProgressIncrementResult(BaseModel):
    ok: bool = True
    current: int | None = None
    target: int | None = None
    status: RecordStatus | None = None
    noop: bool = False
    reason: str | None = None
    error: str | None = None
    derived: DerivedProgress | None = None


class EventAppendResult(BaseModel):
    ok: bool = True
    entry_id: int | None = None
    error: str | None = None
    derived: DerivedEvent | None = None


class SessionStartResult(BaseModel):
    ok: bool = True
    already_active: bool = False
    start_at: str | None = None
    status: RecordStatus | None = None
    error: str | None = None
    derived: DerivedDuration | None = None


class SessionEndResult(BaseModel):
    ok: bool = True
    this_session_minutes: int | None = None
    status: RecordStatus | None = None
    error: str | None = None
    derived: DerivedDuration | None = None


# ── init content（按 kind 分发） ─────────────────────────────────────────────


class ProgressInitContent(BaseModel):
    """``POST /tasks/{id}/record`` body.content，kind=progress。"""

    model_config = {"extra": "forbid"}

    target: int = Field(..., gt=0)
    unit: str = Field(..., min_length=1)
    window: ProgressWindow
    recurring_pattern: dict[str, Any] | None = None
    expires_at: str | None = None


class DurationInitContent(BaseModel):
    model_config = {"extra": "forbid"}

    target_minutes: int | None = Field(None, gt=0)
    recurring_pattern: dict[str, Any] | None = None
    expires_at: str | None = None


class EventInitContent(BaseModel):
    model_config = {"extra": "forbid"}

    recurring_pattern: dict[str, Any] | None = None
    expires_at: str | None = None


# ── 字段白名单（spec §5.1） ──────────────────────────────────────────────────


PROGRESS_PATCH_FIELDS: frozenset[str] = frozenset(
    {"target", "unit", "window", "recurring_pattern", "expires_at"}
)
DURATION_PATCH_FIELDS: frozenset[str] = frozenset(
    {"target_minutes", "recurring_pattern", "expires_at"}
)
EVENT_PATCH_FIELDS: frozenset[str] = frozenset({"recurring_pattern", "expires_at"})

PATCH_FIELDS_BY_KIND: dict[RecordKind, frozenset[str]] = {
    RecordKind.PROGRESS: PROGRESS_PATCH_FIELDS,
    RecordKind.DURATION: DURATION_PATCH_FIELDS,
    RecordKind.EVENT: EVENT_PATCH_FIELDS,
}


def validate_task_id(task_id: str) -> str:
    if not _TASK_ID_RE.match(task_id):
        raise ValueError(
            f"task_id 必须匹配 [a-z0-9_]{{1,32}}，收到: {task_id!r}"
        )
    return task_id


# ── HTTP request 模型 ────────────────────────────────────────────────────────


class RecordInitRequest(BaseModel):
    """``POST /tasks/{task_id}/record`` body。content 按 kind 在 service 层校验。"""

    model_config = {"extra": "forbid"}

    kind: RecordKind
    content: dict[str, Any] = Field(default_factory=dict)


class RecordPatchRequest(BaseModel):
    """``PATCH /tasks/{task_id}/record`` body。字段白名单按 kind 在 service 层校验。"""

    model_config = {"extra": "allow"}


class ProgressIncrementRequest(BaseModel):
    model_config = {"extra": "forbid"}

    delta: int = 1


class EventAppendRequest(BaseModel):
    model_config = {"extra": "forbid"}

    description: str = Field(..., min_length=1)
    at: str | None = None


class SessionAtRequest(BaseModel):
    model_config = {"extra": "forbid"}

    at: str | None = None
