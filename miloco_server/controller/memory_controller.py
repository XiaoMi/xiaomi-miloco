# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Memory Controller - REST API endpoints for memory management.
记忆控制器 - 记忆管理的 REST API 端点
"""

import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Query

from miloco_server.schema.memory_schema import (
    Memory,
    MemoryType,
    MemoryAddRequest,
    MemoryUpdateRequest,
    MemorySearchRequest,
    MemoryNaturalLanguageRequest,
    MemoryListResponse,
    MemoryStats,
    MemorySearchResult,
)
from miloco_server.schema.common_schema import NormalResponse
from miloco_server.service.memory_service import get_memory_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/list", response_model=NormalResponse)
async def get_memories(
    user_id: str = Query(default="default", description="用户ID"),
    include_inactive: bool = Query(default=False, description="是否包含无效记忆"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
):
    """
    获取记忆列表
    """
    try:
        service = get_memory_service()
        if not service.is_initialized:
            return NormalResponse(code=500, message="记忆服务未初始化", data=None)
        
        offset = (page - 1) * page_size
        memories = await service.get_all_memories(
            user_id=user_id,
            include_inactive=include_inactive,
            limit=page_size,
            offset=offset
        )
        
        # 获取总数用于分页
        all_memories = await service.get_all_memories(
            user_id=user_id,
            include_inactive=include_inactive,
            limit=10000
        )
        
        response = MemoryListResponse(
            memories=memories,
            total=len(all_memories),
            page=page,
            page_size=page_size
        )
        
        return NormalResponse(code=0, message="success", data=response.model_dump())
        
    except Exception as e:
        logger.error("Failed to get memories: %s", e)
        return NormalResponse(code=500, message=f"获取记忆失败: {str(e)}", data=None)


@router.post("/add", response_model=NormalResponse)
async def add_memory(
    request: MemoryAddRequest,
    user_id: str = Query(default="default", description="用户ID"),
):
    """
    添加记忆
    """
    try:
        service = get_memory_service()
        if not service.is_initialized:
            return NormalResponse(code=500, message="记忆服务未初始化", data=None)
        
        memory = await service.add_memory(
            content=request.content,
            user_id=user_id,
            memory_type=request.memory_type or MemoryType.CUSTOM,
            metadata=request.metadata
        )
        
        if memory:
            return NormalResponse(code=0, message="记忆添加成功", data=memory.model_dump())
        else:
            return NormalResponse(code=500, message="添加记忆失败", data=None)
            
    except Exception as e:
        logger.error("Failed to add memory: %s", e)
        return NormalResponse(code=500, message=f"添加记忆失败: {str(e)}", data=None)


@router.put("/{memory_id}", response_model=NormalResponse)
async def update_memory(
    memory_id: str,
    request: MemoryUpdateRequest,
):
    """
    更新记忆
    """
    try:
        service = get_memory_service()
        if not service.is_initialized:
            return NormalResponse(code=500, message="记忆服务未初始化", data=None)
        
        success = await service.update_memory(
            memory_id=memory_id,
            content=request.content,
            memory_type=request.memory_type,
            metadata=request.metadata,
            is_active=request.is_active
        )
        
        if success:
            return NormalResponse(code=0, message="记忆更新成功", data={"id": memory_id})
        else:
            return NormalResponse(code=404, message="记忆不存在或更新失败", data=None)
            
    except Exception as e:
        logger.error("Failed to update memory: %s", e)
        return NormalResponse(code=500, message=f"更新记忆失败: {str(e)}", data=None)


@router.delete("/{memory_id}", response_model=NormalResponse)
async def delete_memory(
    memory_id: str,
    soft_delete: bool = Query(default=False, description="是否软删除（默认硬删除）"),
):
    """
    删除记忆
    """
    try:
        service = get_memory_service()
        if not service.is_initialized:
            return NormalResponse(code=500, message="记忆服务未初始化", data=None)
        
        success = await service.delete_memory(memory_id, soft_delete=soft_delete)
        
        if success:
            return NormalResponse(code=0, message="记忆删除成功", data={"id": memory_id})
        else:
            return NormalResponse(code=404, message="记忆不存在或删除失败", data=None)
            
    except Exception as e:
        logger.error("Failed to delete memory: %s", e)
        return NormalResponse(code=500, message=f"删除记忆失败: {str(e)}", data=None)


@router.post("/search", response_model=NormalResponse)
async def search_memories(
    request: MemorySearchRequest,
    user_id: str = Query(default="default", description="用户ID"),
):
    """
    搜索记忆
    """
    try:
        service = get_memory_service()
        if not service.is_initialized:
            return NormalResponse(code=500, message="记忆服务未初始化", data=None)
        
        results = await service.search_memories(
            query=request.query,
            user_id=user_id,
            limit=request.limit,
            memory_types=request.memory_types
        )
        
        data = [
            {
                "memory": r.memory.model_dump(),
                "score": r.score,
                "distance": r.distance
            }
            for r in results
        ]
        
        return NormalResponse(code=0, message="success", data=data)
        
    except Exception as e:
        logger.error("Failed to search memories: %s", e)
        return NormalResponse(code=500, message=f"搜索记忆失败: {str(e)}", data=None)


@router.post("/command", response_model=NormalResponse)
async def handle_natural_language_command(
    request: MemoryNaturalLanguageRequest,
    user_id: str = Query(default="default", description="用户ID"),
):
    """
    处理自然语言记忆管理指令
    
    支持的指令示例：
    - "记住，我的猫叫咪咪"
    - "忘记我说的空调偏好"
    - "把我的睡眠时间改为12点"
    - "你记得我的猫叫什么吗"
    """
    try:
        service = get_memory_service()
        if not service.is_initialized:
            return NormalResponse(code=500, message="记忆服务未初始化", data=None)
        
        result = await service.handle_manual_command(
            command=request.command,
            user_id=user_id
        )
        
        code = 0 if result.get("success") else 400
        return NormalResponse(code=code, message=result.get("message", ""), data=result)
        
    except Exception as e:
        logger.error("Failed to handle natural language command: %s", e)
        return NormalResponse(code=500, message=f"处理指令失败: {str(e)}", data=None)


@router.get("/stats", response_model=NormalResponse)
async def get_memory_stats(
    user_id: str = Query(default="default", description="用户ID"),
):
    """
    获取记忆统计信息
    """
    try:
        service = get_memory_service()
        if not service.is_initialized:
            return NormalResponse(code=500, message="记忆服务未初始化", data=None)
        
        stats = await service.get_stats(user_id=user_id)
        
        return NormalResponse(code=0, message="success", data=stats.model_dump())
        
    except Exception as e:
        logger.error("Failed to get memory stats: %s", e)
        return NormalResponse(code=500, message=f"获取统计失败: {str(e)}", data=None)


@router.get("/context", response_model=NormalResponse)
async def get_memory_context(
    query: str = Query(..., description="查询内容"),
    user_id: str = Query(default="default", description="用户ID"),
    limit: int = Query(default=5, ge=1, le=20, description="最大记忆数"),
):
    """
    获取查询的记忆上下文
    
    用于在回答问题前获取相关记忆
    """
    try:
        service = get_memory_service()
        if not service.is_initialized:
            return NormalResponse(code=500, message="记忆服务未初始化", data=None)
        
        context = await service.get_context_for_query(
            query=query,
            user_id=user_id,
            limit=limit
        )
        
        data = {
            "context_text": context.context_text,
            "memories": [
                {
                    "memory": r.memory.model_dump(),
                    "score": r.score
                }
                for r in context.memories
            ]
        }
        
        return NormalResponse(code=0, message="success", data=data)
        
    except Exception as e:
        logger.error("Failed to get memory context: %s", e)
        return NormalResponse(code=500, message=f"获取上下文失败: {str(e)}", data=None)


@router.get("/types", response_model=NormalResponse)
async def get_memory_types():
    """
    获取所有记忆类型
    """
    types = [
        {"value": t.value, "label": _get_type_label(t)}
        for t in MemoryType
    ]
    return NormalResponse(code=0, message="success", data=types)


def _get_type_label(memory_type: MemoryType) -> str:
    """获取记忆类型的标签"""
    labels = {
        MemoryType.PREFERENCE: "用户偏好",
        MemoryType.FACT: "事实信息",
        MemoryType.HABIT: "生活习惯",
        MemoryType.DEVICE_SETTING: "设备偏好",
        MemoryType.SCHEDULE: "时间安排",
        MemoryType.RELATIONSHIP: "关系信息",
        MemoryType.CUSTOM: "自定义",
    }
    return labels.get(memory_type, memory_type.value)
