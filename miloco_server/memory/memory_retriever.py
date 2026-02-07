# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Memory Retriever - Retrieve relevant memories for context injection.
记忆检索器 - 检索相关记忆用于上下文注入
"""

import logging
from typing import Optional, List

from miloco_server.schema.memory_schema import (
    Memory,
    MemoryType,
    MemorySearchResult,
    MemoryContext,
)
from miloco_server.memory.memory_manager import MemoryManager, get_memory_manager

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """
    记忆检索器
    
    负责从记忆存储中检索与当前对话相关的记忆。
    支持：
    - 语义相似度检索
    - 按类型过滤
    - 上下文格式化
    """
    
    def __init__(self, memory_manager: Optional[MemoryManager] = None):
        """
        初始化记忆检索器
        
        Args:
            memory_manager: 记忆管理器实例
        """
        self._memory_manager = memory_manager or get_memory_manager()
    
    async def retrieve_for_query(
        self,
        query: str,
        user_id: str = "default",
        limit: int = 5,
        min_score: float = 0.3,
        memory_types: Optional[List[MemoryType]] = None,
    ) -> MemoryContext:
        """
        为查询检索相关记忆
        
        Args:
            query: 用户查询/消息
            user_id: 用户ID
            limit: 最大返回数量
            min_score: 最小相关性分数
            memory_types: 过滤的记忆类型
            
        Returns:
            MemoryContext: 记忆上下文
        """
        try:
            # 搜索相关记忆
            search_results = await self._memory_manager.search_memories(
                query=query,
                user_id=user_id,
                limit=limit * 2,  # 获取更多，然后过滤
                memory_types=memory_types,
                include_inactive=False
            )
            
            # 过滤低相关性结果
            filtered_results = [
                r for r in search_results 
                if r.score >= min_score
            ][:limit]
            
            # 构建上下文
            context = MemoryContext(memories=filtered_results)
            context.context_text = context.to_prompt_text()
            
            logger.debug(
                "Retrieved %d memories for query: '%s'", 
                len(filtered_results), 
                query[:50]
            )
            
            return context
            
        except Exception as e:
            logger.error("Failed to retrieve memories: %s", e)
            return MemoryContext()
    
    async def retrieve_by_types(
        self,
        user_id: str = "default",
        memory_types: Optional[List[MemoryType]] = None,
        limit: int = 10,
    ) -> List[Memory]:
        """
        按类型检索记忆
        
        Args:
            user_id: 用户ID
            memory_types: 记忆类型列表
            limit: 最大返回数量
            
        Returns:
            List[Memory]: 记忆列表
        """
        try:
            all_memories = await self._memory_manager.get_all_memories(
                user_id=user_id,
                include_inactive=False,
                limit=limit * 2
            )
            
            if memory_types:
                all_memories = [
                    m for m in all_memories 
                    if m.memory_type in memory_types
                ]
            
            return all_memories[:limit]
            
        except Exception as e:
            logger.error("Failed to retrieve memories by type: %s", e)
            return []
    
    async def retrieve_preferences(
        self,
        user_id: str = "default",
        limit: int = 10,
    ) -> List[Memory]:
        """
        检索用户偏好
        
        Args:
            user_id: 用户ID
            limit: 最大返回数量
            
        Returns:
            List[Memory]: 偏好记忆列表
        """
        return await self.retrieve_by_types(
            user_id=user_id,
            memory_types=[MemoryType.PREFERENCE, MemoryType.DEVICE_SETTING],
            limit=limit
        )
    
    async def retrieve_facts(
        self,
        user_id: str = "default",
        limit: int = 10,
    ) -> List[Memory]:
        """
        检索事实信息
        
        Args:
            user_id: 用户ID
            limit: 最大返回数量
            
        Returns:
            List[Memory]: 事实记忆列表
        """
        return await self.retrieve_by_types(
            user_id=user_id,
            memory_types=[MemoryType.FACT, MemoryType.RELATIONSHIP],
            limit=limit
        )
    
    async def retrieve_habits_and_schedules(
        self,
        user_id: str = "default",
        limit: int = 10,
    ) -> List[Memory]:
        """
        检索习惯和日程
        
        Args:
            user_id: 用户ID
            limit: 最大返回数量
            
        Returns:
            List[Memory]: 习惯/日程记忆列表
        """
        return await self.retrieve_by_types(
            user_id=user_id,
            memory_types=[MemoryType.HABIT, MemoryType.SCHEDULE],
            limit=limit
        )
    
    async def build_full_context(
        self,
        query: str,
        user_id: str = "default",
        max_memories: int = 5,
        min_relevance: float = 0.35,
    ) -> MemoryContext:
        """
        构建记忆上下文（基于语义相关性）
        
        只获取与当前查询语义相关的记忆，避免注入无关内容：
        - 使用向量相似度搜索，只返回相关性高于阈值的记忆
        - 限制最大记忆数量，节省 Token
        
        Args:
            query: 用户查询
            user_id: 用户ID
            max_memories: 最大记忆数量（默认5条，节省token）
            min_relevance: 最小相关性分数（0-1，越高越严格）
            
        Returns:
            MemoryContext: 记忆上下文（只包含相关记忆）
        """
        try:
            # 直接通过语义搜索获取相关记忆
            query_context = await self.retrieve_for_query(
                query=query,
                user_id=user_id,
                limit=max_memories,
                min_score=min_relevance  # 只保留相关性高的记忆
            )
            
            if query_context.memories:
                logger.info(
                    "Found %d relevant memories for query '%s' (scores: %s)", 
                    len(query_context.memories),
                    query[:30],
                    [f"{r.score:.2f}" for r in query_context.memories]
                )
            else:
                logger.debug("No relevant memories found for query: %s", query[:30])
            
            return query_context
            
        except Exception as e:
            logger.error("Failed to build memory context: %s", e, exc_info=True)
            return MemoryContext()


# 全局检索器实例
_memory_retriever: Optional[MemoryRetriever] = None


def get_memory_retriever() -> MemoryRetriever:
    """获取全局记忆检索器实例"""
    global _memory_retriever
    if _memory_retriever is None:
        _memory_retriever = MemoryRetriever()
    return _memory_retriever
