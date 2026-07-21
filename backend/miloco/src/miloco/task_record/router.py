# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task_record HTTP 路由 (v2)。

record 相关 endpoint (init / get / patch / compute / archives / progress / event / session)。
task_link 相关的 attach_link (POST /tasks/{id}/link) 已在 v2 移除:
rule 关联走 rule.task_id FK CASCADE, cron 关联走 /crons endpoint。

router prefix 在 main.py 加 ``/api``, 实际路径为 ``/api/tasks/...``。
"""

import logging

from fastapi import APIRouter, Depends, Query

from miloco.middleware import verify_token
from miloco.middleware.exceptions import (
    ConflictException,
    ResourceNotFoundException,
    ValidationException,
)
from miloco.schema.common_schema import NormalResponse
from miloco.task_record.schema import (
    EventAppendRequest,
    ProgressIncrementRequest,
    RecordInitRequest,
    RecordPatchRequest,
    SessionAtRequest,
)
from miloco.task_record.service import (
    RecordAlreadyExistsError,
    RecordNotFoundError,
    RecordSchemaError,
    RecordWrongKindError,
    TaskNotFoundError,
    TaskRecordService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["TaskRecord"])


def _service() -> TaskRecordService:
    return TaskRecordService()


# ── record CRUD ──────────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/record", summary="Init Record", response_model=NormalResponse
)
async def init_record(
    task_id: str,
    req: RecordInitRequest,
    current_user: str = Depends(verify_token),
):
    """方案 P 阶段 A'：插主表活跃行。前提 task 已存在。"""
    logger.info(
        "Init record - User: %s, task_id: %s, kind: %s",
        current_user,
        task_id,
        req.kind.value,
    )
    try:
        view = _service().init_record(task_id, req.kind, req.content)
    except TaskNotFoundError as e:
        raise ResourceNotFoundException(f"task_not_found: {e}") from e
    except RecordAlreadyExistsError as e:
        raise ConflictException(f"record_already_exists: {e}") from e
    except RecordSchemaError as e:
        raise ValidationException(f"schema_invalid: {e}") from e
    return NormalResponse(code=0, message="Record initialized", data=view)


@router.get(
    "/{task_id}/record", summary="Get Active Record", response_model=NormalResponse
)
async def get_record(task_id: str, current_user: str = Depends(verify_token)):
    try:
        view = _service().get_active_record(task_id)
    except RecordNotFoundError as e:
        raise ResourceNotFoundException(f"no_active_record: {e}") from e
    return NormalResponse(code=0, message="Record retrieved", data=view)


@router.patch(
    "/{task_id}/record", summary="Patch Active Record", response_model=NormalResponse
)
async def patch_record(
    task_id: str,
    req: RecordPatchRequest,
    current_user: str = Depends(verify_token),
):
    patch = req.model_dump()
    try:
        view = _service().patch_active_record(task_id, patch)
    except RecordNotFoundError as e:
        raise ResourceNotFoundException(f"no_active_record: {e}") from e
    except RecordSchemaError as e:
        raise ValidationException(f"schema_invalid: {e}") from e
    return NormalResponse(code=0, message="Record patched", data=view)


# ── compute ──────────────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/record/compute",
    summary="Compute Derived",
    response_model=NormalResponse,
)
async def compute_record(
    task_id: str,
    window: str = Query("all"),
    date: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    current_user: str = Depends(verify_token),
):
    """派生量计算。三套互斥用法：

    - 默认（无 query）：当前活跃行 derived
    - ``?window=day|week|month``：按当前 period 聚合（按 kind 限制，见 §10.8）
    - ``?date=YYYY-MM-DD``：单日历史归档行 derived
    - ``?from=YYYY-MM-DD&to=YYYY-MM-DD``：区间聚合 derived（G1）

    互斥：``window != 'all'`` 不能配 ``date``；``from/to`` 不能配 ``window``/``date``。
    """
    if (from_ is not None) ^ (to is not None):
        raise ValidationException(
            "schema_invalid: from 和 to 必须成对提供"
        )
    if from_ is not None and (window != "all" or date is not None):
        raise ValidationException(
            "schema_invalid: from/to 与 window/date 互斥"
        )
    try:
        if from_ is not None:
            result = _service().compute_range(
                task_id, from_date=from_, to_date=to
            )
        else:
            result = _service().compute_derived(
                task_id, window=window, date=date
            )
    except RecordNotFoundError as e:
        raise ResourceNotFoundException(f"no_active_record: {e}") from e
    except RecordSchemaError as e:
        raise ValidationException(f"schema_invalid: {e}") from e
    return NormalResponse(code=0, message="Derived computed", data=result)


@router.get(
    "/{task_id}/record/archives",
    summary="List Archives",
    response_model=NormalResponse,
)
async def list_archives(
    task_id: str,
    current_user: str = Depends(verify_token),
):
    """列出该 task 全部 archive 行 + 每日 derived 快照（G2）。"""
    try:
        result = _service().list_archives(task_id)
    except RecordNotFoundError as e:
        raise ResourceNotFoundException(f"no_active_record: {e}") from e
    return NormalResponse(code=0, message="Archives listed", data=result)


# ── progress mutate ──────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/record/progress/increment",
    summary="Progress Increment",
    response_model=NormalResponse,
)
async def progress_increment(
    task_id: str,
    req: ProgressIncrementRequest,
    current_user: str = Depends(verify_token),
):
    try:
        result = _service().progress_increment(task_id, delta=req.delta)
    except RecordNotFoundError as e:
        raise ResourceNotFoundException(f"no_active_record: {e}") from e
    except RecordWrongKindError as e:
        raise ValidationException(f"wrong_kind: {e}") from e
    return NormalResponse(code=0, message="Progress incremented", data=result)


# ── event mutate ─────────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/record/event/append",
    summary="Event Append",
    response_model=NormalResponse,
)
async def event_append(
    task_id: str,
    req: EventAppendRequest,
    current_user: str = Depends(verify_token),
):
    try:
        result = _service().event_append(
            task_id, description=req.description, at=req.at
        )
    except RecordNotFoundError as e:
        raise ResourceNotFoundException(f"no_active_record: {e}") from e
    except RecordSchemaError as e:
        raise ValidationException(f"schema_invalid: {e}") from e
    except RecordWrongKindError as e:
        raise ValidationException(f"wrong_kind: {e}") from e
    return NormalResponse(code=0, message="Event appended", data=result)


# ── duration mutate ──────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/record/session/start",
    summary="Session Start",
    response_model=NormalResponse,
)
async def session_start(
    task_id: str,
    req: SessionAtRequest,
    current_user: str = Depends(verify_token),
):
    try:
        result = _service().session_start(task_id, at=req.at)
    except RecordNotFoundError as e:
        raise ResourceNotFoundException(f"no_active_record: {e}") from e
    except RecordSchemaError as e:
        raise ValidationException(f"schema_invalid: {e}") from e
    except RecordWrongKindError as e:
        raise ValidationException(f"wrong_kind: {e}") from e
    return NormalResponse(code=0, message="Session started", data=result)


@router.post(
    "/{task_id}/record/session/end",
    summary="Session End",
    response_model=NormalResponse,
)
async def session_end(
    task_id: str,
    req: SessionAtRequest,
    current_user: str = Depends(verify_token),
):
    try:
        result = _service().session_end(task_id, at=req.at)
    except RecordNotFoundError as e:
        raise ResourceNotFoundException(f"no_active_record: {e}") from e
    except RecordSchemaError as e:
        raise ValidationException(f"schema_invalid: {e}") from e
    except RecordWrongKindError as e:
        raise ValidationException(f"wrong_kind: {e}") from e
    return NormalResponse(code=0, message="Session ended", data=result)
