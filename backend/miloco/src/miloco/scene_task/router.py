# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""场景联动任务 Controller（/scene-tasks）。"""

import logging

from fastapi import APIRouter, Depends

from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.middleware.exceptions import BusinessException
from miloco.scene_task.schema import (
    SceneTaskCreateRequest,
    SceneTaskUpdateRequest,
)
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/scene-tasks', tags=['SceneTasks'])


@router.get('', summary='List Scene Tasks', response_model=NormalResponse)
async def list_scene_tasks(current_user: str = Depends(verify_token)):
    logger.info('List scene tasks - User: %s', current_user)
    views = await get_manager().scene_task_service.list()
    return NormalResponse(
        code=0,
        message=f'Retrieved {len(views)} scene tasks',
        data=[v.model_dump() for v in views],
    )


@router.post('', summary='Create Scene Task', response_model=NormalResponse)
async def create_scene_task(
    req: SceneTaskCreateRequest,
    current_user: str = Depends(verify_token),
):
    logger.info(
        'Create scene task - User: %s, description: %s, enter_scene: %s, exit_scene: %s',
        current_user, req.description, req.enter_scene_id, req.exit_scene_id,
    )
    view = await get_manager().scene_task_service.create(req)
    return NormalResponse(
        code=0, message='Scene task created', data=view.model_dump()
    )


@router.patch(
    '/{task_id}', summary='Update Scene Task', response_model=NormalResponse
)
async def update_scene_task(
    task_id: str,
    req: SceneTaskUpdateRequest,
    current_user: str = Depends(verify_token),
):
    logger.info('Update scene task - User: %s, task_id: %s', current_user, task_id)
    view = await get_manager().scene_task_service.update(task_id, req)
    return NormalResponse(
        code=0, message='Scene task updated', data=view.model_dump()
    )


@router.post(
    '/{task_id}/enable', summary='Enable Scene Task', response_model=NormalResponse
)
async def enable_scene_task(
    task_id: str, current_user: str = Depends(verify_token)
):
    logger.info('Enable scene task - User: %s, task_id: %s', current_user, task_id)
    result = get_manager().scene_task_service.enable(task_id)
    return NormalResponse(code=0, message='Scene task enabled', data=result.model_dump())


@router.post(
    '/{task_id}/disable', summary='Disable Scene Task', response_model=NormalResponse
)
async def disable_scene_task(
    task_id: str, current_user: str = Depends(verify_token)
):
    logger.info(
        'Disable scene task - User: %s, task_id: %s', current_user, task_id
    )
    result = get_manager().scene_task_service.disable(task_id)
    return NormalResponse(
        code=0, message='Scene task disabled', data=result.model_dump()
    )


@router.post(
    '/{task_id}/trigger', summary='Trigger Scene Task (debug)',
    response_model=NormalResponse,
)
async def trigger_scene_task(
    task_id: str, current_user: str = Depends(verify_token)
):
    """调试入口：手动触发一次进入场景（不依赖感知命中）。"""
    logger.info(
        'Trigger scene task (debug) - User: %s, task_id: %s', current_user, task_id
    )
    result = await get_manager().scene_task_service.trigger(task_id)
    if result is None:
        raise BusinessException(f'Scene task {task_id!r} not found or disabled')
    return NormalResponse(
        code=0, message='Scene task triggered', data=result.model_dump()
    )


@router.delete(
    '/{task_id}', summary='Delete Scene Task', response_model=NormalResponse
)
async def delete_scene_task(
    task_id: str, current_user: str = Depends(verify_token)
):
    logger.info('Delete scene task - User: %s, task_id: %s', current_user, task_id)
    result = get_manager().scene_task_service.delete(task_id)
    if result is None:
        raise BusinessException(f'Scene task {task_id!r} not found')
    return NormalResponse(code=0, message='Scene task deleted', data=result.model_dump())