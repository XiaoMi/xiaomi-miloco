# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task_record 模块：3 kind × 1~2 表 + terminate 审计表的数据访问与业务编排。

模块边界：
- ``schema``  : pydantic 模型 + enum + 字段白名单常量
- ``repo``    : 4 张 Repo class（薄包装，接 cursor，由 service 管事务）
- ``service`` : TaskRecordService — 跨表事务编排 + derived 派生量计算

设计与 spec 2026-06-10 task-record-backend-migration-design 对齐。
"""

from miloco.task_record.schema import (
    PATCH_FIELDS_BY_KIND,
    DurationInitContent,
    EventAppendRequest,
    EventInitContent,
    ProgressIncrementRequest,
    ProgressInitContent,
    ProgressWindow,
    RecordInitRequest,
    RecordKind,
    RecordPatchRequest,
    RecordStatus,
    SessionAtRequest,
    TerminateReason,
)
from miloco.task_record.service import (
    RecordAlreadyExistsError,
    RecordNotFoundError,
    RecordSchemaError,
    RecordWrongKindError,
    TaskNotFoundError,
    TaskRecordService,
)

__all__ = [
    "DurationInitContent",
    "EventAppendRequest",
    "EventInitContent",
    "PATCH_FIELDS_BY_KIND",
    "ProgressIncrementRequest",
    "ProgressInitContent",
    "ProgressWindow",
    "RecordAlreadyExistsError",
    "RecordInitRequest",
    "RecordKind",
    "RecordNotFoundError",
    "RecordPatchRequest",
    "RecordSchemaError",
    "RecordStatus",
    "RecordWrongKindError",
    "SessionAtRequest",
    "TaskNotFoundError",
    "TaskRecordService",
    "TerminateReason",
]
