# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Task SSOT Controller (`/tasks`)."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query

from miloco.database.task_repo import TaskConflict, TaskNotFound
from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.middleware.exceptions import (
    ConflictException,
    ResourceNotFoundException,
)
from miloco.schema.common_schema import NormalResponse
from miloco.task.schema import (
    TaskCreateRequest,
    TaskUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["Tasks"])


@router.post("", summary="Create Task", response_model=NormalResponse)
async def create_task(req: TaskCreateRequest, current_user: str = Depends(verify_token)):
    logger.info("Create task - User: %s, task_id: %s", current_user, req.task_id)
    try:
        get_manager().task_service.create_task(req)
    except TaskConflict as e:
        raise ConflictException(str(e)) from e
    return NormalResponse(code=0, message="Task created", data={"task_id": req.task_id})


@router.get("", summary="List Tasks (dedupe view)", response_model=NormalResponse)
async def list_tasks(current_user: str = Depends(verify_token)):
    logger.info("List tasks - User: %s", current_user)
    views = get_manager().task_service.list_for_dedupe()
    return NormalResponse(
        code=0,
        message=f"Retrieved {len(views)} tasks",
        data=[v.model_dump() for v in views],
    )


@router.get("/summary", summary="Summary Tasks", response_model=NormalResponse)
async def summary_tasks(
    window: Literal["day", "all"] = "day",
    current_user: str = Depends(verify_token),
):
    logger.info("Summary tasks - User: %s, window: %s", current_user, window)
    views = get_manager().task_service.list_summary(window)
    return NormalResponse(
        code=0,
        message=f"Retrieved {len(views)} task summaries",
        data=[v.model_dump() for v in views],
    )


@router.get("/{task_id}", summary="Get Task", response_model=NormalResponse)
async def get_task(task_id: str, current_user: str = Depends(verify_token)):
    logger.info("Get task - User: %s, task_id: %s", current_user, task_id)
    view = get_manager().task_service.get_full_view(task_id)
    if view is None:
        raise ResourceNotFoundException(f"task_not_found: {task_id}")
    return NormalResponse(code=0, message="Task retrieved", data=view.model_dump())


@router.patch(
    "/{task_id}", summary="Update Task Description", response_model=NormalResponse
)
async def update_task(
    task_id: str,
    req: TaskUpdateRequest,
    current_user: str = Depends(verify_token),
):
    logger.info("Update task - User: %s, task_id: %s", current_user, task_id)
    ok = get_manager().task_service.update_description(task_id, req)
    if not ok:
        raise ResourceNotFoundException(f"task_not_found: {task_id}")
    return NormalResponse(code=0, message="Task updated", data=None)


@router.post(
    "/{task_id}/disable", summary="Disable Task", response_model=NormalResponse
)
async def disable_task(task_id: str, current_user: str = Depends(verify_token)):
    logger.info("Disable task - User: %s, task_id: %s", current_user, task_id)
    try:
        result = get_manager().task_service.disable_task(task_id)
    except TaskNotFound as e:
        raise ResourceNotFoundException(str(e)) from e
    return NormalResponse(code=0, message="Task disabled", data=result.model_dump())


@router.post("/{task_id}/enable", summary="Enable Task", response_model=NormalResponse)
async def enable_task(task_id: str, current_user: str = Depends(verify_token)):
    logger.info("Enable task - User: %s, task_id: %s", current_user, task_id)
    try:
        result = get_manager().task_service.enable_task(task_id)
    except TaskNotFound as e:
        raise ResourceNotFoundException(str(e)) from e
    return NormalResponse(code=0, message="Task enabled", data=result.model_dump())


@router.delete("/{task_id}", summary="Delete Task", response_model=NormalResponse)
async def delete_task(
    task_id: str,
    reason: str = Query(
        "completed",
        pattern="^(completed|expired|abandoned)$",
        description="terminate 原因（query 参数；P4 起写入 task_terminate_log）",
    ),
    current_user: str = Depends(verify_token),
):
    """删 task。``reason`` 透传到 ``task_terminate_log.reason``（P4 接入事务体）。"""
    logger.info(
        "Delete task - User: %s, task_id: %s, reason: %s",
        current_user,
        task_id,
        reason,
    )
    result = get_manager().task_service.delete_task(task_id, reason=reason)
    if result is None:
        raise ResourceNotFoundException(f"task_not_found: {task_id}")
    return NormalResponse(code=0, message="Task deleted", data=result.model_dump())
