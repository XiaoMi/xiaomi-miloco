# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Memory Service - Business logic layer for memory management.
记忆服务层 - 记忆管理的业务逻辑层
"""

import logging
import re
from typing import Optional, List, Dict, Any, Callable, Coroutine

from miloco_server.schema.memory_schema import (
    Memory,
    MemoryType,
    MemoryAction,
    MemoryExtractionResult,
    MemorySearchResult,
    MemoryContext,
    MemoryStats,
    ManualMemoryCommand,
)
from miloco_server.memory.memory_manager import MemoryManager, get_memory_manager, initialize_memory_manager
from miloco_server.memory.memory_extractor import MemoryExtractor, SmartMemoryFilter
from miloco_server.memory.memory_retriever import MemoryRetriever, get_memory_retriever

logger = logging.getLogger(__name__)


class MemoryService:
    """
    记忆服务
    
    提供完整的记忆管理业务逻辑：
    - 自动记忆提取和存储
    - 手动记忆管理
    - 记忆检索和上下文构建
    - 记忆统计
    """
    
    def __init__(
        self,
        llm_call_func: Optional[Callable[[List[dict]], Coroutine[Any, Any, dict]]] = None,
    ):
        """
        初始化记忆服务
        
        Args:
            llm_call_func: LLM 调用函数
        """
        self._memory_manager: Optional[MemoryManager] = None
        self._memory_extractor: Optional[MemoryExtractor] = None
        self._memory_retriever: Optional[MemoryRetriever] = None
        self._llm_call_func = llm_call_func
        self._initialized = False
    
    async def initialize(self) -> bool:
        """
        初始化服务
        
        Returns:
            bool: 是否初始化成功
        """
        try:
            # 初始化记忆管理器
            self._memory_manager = get_memory_manager()
            success = await self._memory_manager.initialize()
            if not success:
                logger.error("Failed to initialize memory manager")
                return False
            
            # 初始化检索器
            self._memory_retriever = get_memory_retriever()
            
            # 初始化提取器（需要 LLM）
            if self._llm_call_func:
                self._memory_extractor = MemoryExtractor(self._llm_call_func)
            
            self._initialized = True
            logger.info("MemoryService initialized successfully")
            return True
            
        except Exception as e:
            logger.error("Failed to initialize MemoryService: %s", e)
            return False
    
    def set_llm_call_func(self, llm_call_func: Callable[[List[dict]], Coroutine[Any, Any, dict]]):
        """设置 LLM 调用函数"""
        self._llm_call_func = llm_call_func
        self._memory_extractor = MemoryExtractor(llm_call_func)
    
    @property
    def is_initialized(self) -> bool:
        """是否已初始化"""
        return self._initialized
    
    # ==================== 自动记忆功能 ====================
    
    async def process_conversation(
        self,
        user_message: str,
        assistant_response: Optional[str] = None,
        user_id: str = "default",
        context_messages: Optional[List[dict]] = None,
    ) -> Optional[MemoryExtractionResult]:
        """
        处理对话，自动提取并保存记忆
        
        Args:
            user_message: 用户消息
            assistant_response: 助手响应
            user_id: 用户ID
            context_messages: 上下文消息
            
        Returns:
            MemoryExtractionResult: 提取结果（如果有）
        """
        if not self._initialized:
            logger.warning("MemoryService not initialized")
            return None
        
        # 快速过滤
        if SmartMemoryFilter.should_skip(user_message):
            logger.debug("Message skipped by filter: %s", user_message[:30])
            return None
        
        # 检查是否是记忆管理指令
        if SmartMemoryFilter.is_memory_management_command(user_message):
            # 这是手动记忆管理指令，由其他方法处理
            return None
        
        # 如果没有 LLM，尝试使用规则提取
        if not self._memory_extractor:
            logger.info("Using rule-based memory extraction (no LLM extractor)")
            return await self._extract_memory_with_rules(user_message, user_id)
        
        try:
            logger.info("Using LLM-based memory extraction for: %s", user_message[:50])
            
            # 获取已有的相关记忆（用于判断更新）
            existing_memories = await self._memory_manager.search_memories(
                query=user_message,
                user_id=user_id,
                limit=5
            )
            existing_memory_list = [r.memory for r in existing_memories]
            logger.debug("Found %d existing memories for context", len(existing_memory_list))
            
            # 提取记忆
            result = await self._memory_extractor.extract_memories(
                user_message=user_message,
                assistant_response=assistant_response,
                context_messages=context_messages,
                existing_memories=existing_memory_list
            )
            logger.info("LLM extraction result: should_save=%s, action=%s, memories=%d",
                       result.should_save, result.action, len(result.memories))
            
            if not result.should_save:
                return result
            
            # 根据操作类型处理
            if result.action == MemoryAction.ADD:
                for memory in result.memories:
                    # 检查是否有高度相似的记忆（去重）
                    similar = await self._memory_manager.find_similar_memories(
                        content=memory.content,
                        user_id=user_id,
                        threshold=0.85
                    )
                    
                    if similar:
                        # 更新已有记忆而不是添加新的
                        await self._memory_manager.update_memory(
                            memory_id=similar[0].memory.id,
                            content=memory.content
                        )
                        logger.info("Updated similar memory instead of adding: %s", memory.content[:50])
                    else:
                        # 添加新记忆
                        await self._memory_manager.add_memory(
                            content=memory.content,
                            user_id=user_id,
                            memory_type=memory.memory_type,
                            source="auto"
                        )
                        logger.info("Added new memory: %s", memory.content[:50])
            
            elif result.action == MemoryAction.UPDATE:
                # 更新相关记忆
                for memory_id in result.related_memory_ids:
                    if result.memories:
                        await self._memory_manager.update_memory(
                            memory_id=memory_id,
                            content=result.memories[0].content
                        )
            
            elif result.action == MemoryAction.DELETE:
                # 删除相关记忆
                for memory_id in result.related_memory_ids:
                    await self._memory_manager.delete_memory(memory_id, soft_delete=True)
            
            return result
            
        except Exception as e:
            logger.error("Failed to process conversation for memory: %s", e)
            return None
    
    async def _extract_memory_with_rules(
        self,
        user_message: str,
        user_id: str = "default",
    ) -> Optional[MemoryExtractionResult]:
        """
        使用规则提取记忆（无 LLM 时的回退方案）
        
        Args:
            user_message: 用户消息
            user_id: 用户ID
            
        Returns:
            MemoryExtractionResult: 提取结果
        """
        # 规则模式匹配
        patterns = [
            # 时间习惯
            (r"每天.*?(\d+)点.*?(起床|睡觉|下班|上班|出门|回家)", MemoryType.HABIT, 
             lambda m: f"用户每天{m.group(1)}点{m.group(2)}"),
            (r"每天都.*?(\d+)点.*?(下班|上班|后)", MemoryType.HABIT,
             lambda m: f"用户每天{m.group(1)}点后{m.group(2) if m.group(2) != '后' else '下班'}"),
            (r"(\d+)点.*?(下班|上班|回家|出门)", MemoryType.HABIT,
             lambda m: f"用户{m.group(1)}点{m.group(2)}"),
            (r"周末.*?(\d+)点.*?(起床|睡觉)", MemoryType.SCHEDULE,
             lambda m: f"用户周末{m.group(1)}点{m.group(2)}"),
            # 最近状态
            (r"最近.*?很忙", MemoryType.HABIT,
             lambda m: "用户最近很忙"),
            (r"这个月.*?很忙", MemoryType.HABIT,
             lambda m: "用户这个月很忙"),
            
            # 温度偏好
            (r"(喜欢|偏好|习惯).*?空调.*?(\d+)度", MemoryType.PREFERENCE,
             lambda m: f"用户{m.group(1)}空调温度{m.group(2)}度"),
            (r"空调.*?(开|调).*?(\d+)度", MemoryType.PREFERENCE,
             lambda m: f"用户偏好空调温度{m.group(2)}度"),
            (r"(怕冷|怕热)", MemoryType.PREFERENCE,
             lambda m: f"用户{m.group(1)}"),
            
            # 事实信息
            (r"我的(猫|狗|宠物)叫(.+?)(?:[，。！？]|$)", MemoryType.FACT,
             lambda m: f"用户的{m.group(1)}叫{m.group(2).strip()}"),
            (r"我的生日是(.+?)(?:[，。！？]|$)", MemoryType.FACT,
             lambda m: f"用户的生日是{m.group(1).strip()}"),
            (r"我(今年)?(\d+)岁", MemoryType.FACT,
             lambda m: f"用户{m.group(2)}岁"),
            
            # 关系信息
            (r"(爸爸|妈妈|父亲|母亲|老婆|老公|孩子).*?住在(.+?)(?:[，。！？]|$)", MemoryType.RELATIONSHIP,
             lambda m: f"用户的{m.group(1)}住在{m.group(2).strip()}"),
            
            # 设备偏好
            (r"(客厅|卧室|书房).*?灯.*?(\d+)%", MemoryType.DEVICE_SETTING,
             lambda m: f"用户偏好{m.group(1)}灯亮度{m.group(2)}%"),
            
            # 通用习惯描述
            (r"我(一般|通常|习惯)(.+?)(?:[。！？]|$)", MemoryType.HABIT,
             lambda m: f"用户{m.group(1)}{m.group(2).strip()}"),
        ]
        
        extracted_memories = []
        
        for pattern, memory_type, content_func in patterns:
            match = re.search(pattern, user_message)
            if match:
                try:
                    content = content_func(match)
                    if content and len(content) > 3:
                        memory = Memory(
                            id="",  # 会在添加时生成
                            user_id=user_id,
                            content=content,
                            memory_type=memory_type,
                            source="auto"
                        )
                        extracted_memories.append(memory)
                        logger.info("Rule-extracted memory: %s", content)
                except Exception as e:
                    logger.warning("Failed to extract memory with pattern: %s", e)
        
        if extracted_memories:
            # 保存提取的记忆
            for mem in extracted_memories:
                # 检查是否已有相似记忆
                similar = await self._memory_manager.find_similar_memories(
                    content=mem.content,
                    user_id=user_id,
                    threshold=0.8
                )
                
                if similar:
                    # 更新已有记忆
                    await self._memory_manager.update_memory(
                        memory_id=similar[0].memory.id,
                        content=mem.content
                    )
                    logger.info("Updated similar memory: %s", mem.content)
                else:
                    # 添加新记忆
                    await self._memory_manager.add_memory(
                        content=mem.content,
                        user_id=user_id,
                        memory_type=mem.memory_type,
                        source="auto"
                    )
                    logger.info("Added new memory: %s", mem.content)
            
            return MemoryExtractionResult(
                should_save=True,
                action=MemoryAction.ADD,
                memories=extracted_memories,
                reasoning="基于规则提取"
            )
        
        return None

    # ==================== 手动记忆管理 ====================
    
    async def handle_manual_command(
        self,
        command: str,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """
        处理手动记忆管理指令
        
        Args:
            command: 自然语言指令
            user_id: 用户ID
            
        Returns:
            Dict: 处理结果
        """
        if not self._initialized or not self._memory_manager:
            return {"success": False, "message": "记忆服务未初始化"}
        
        # 如果没有 LLM，尝试使用简单的规则匹配
        if not self._memory_extractor:
            return await self._handle_command_with_rules(command, user_id)
        
        try:
            # 解析指令
            parsed = await self._memory_extractor.parse_manual_command(command)
            
            if parsed.action == MemoryAction.NONE:
                return {
                    "success": False,
                    "message": "无法理解您的指令，请尝试更明确的表述",
                    "action": "none"
                }
            
            if parsed.action == MemoryAction.ADD:
                # 添加记忆
                memory = await self._memory_manager.add_memory(
                    content=parsed.content,
                    user_id=user_id,
                    memory_type=parsed.memory_type,
                    source="manual"
                )
                return {
                    "success": True,
                    "message": f"已记住：{parsed.content}",
                    "action": "add",
                    "memory": memory.model_dump() if memory else None
                }
            
            elif parsed.action == MemoryAction.UPDATE:
                # 查找要更新的记忆
                search_results = await self._memory_manager.search_memories(
                    query=parsed.target_description,
                    user_id=user_id,
                    limit=1
                )
                
                if not search_results:
                    return {
                        "success": False,
                        "message": f"没有找到与 '{parsed.target_description}' 相关的记忆",
                        "action": "update"
                    }
                
                # 更新记忆
                success = await self._memory_manager.update_memory(
                    memory_id=search_results[0].memory.id,
                    content=parsed.content
                )
                
                return {
                    "success": success,
                    "message": f"已更新记忆：{parsed.content}" if success else "更新失败",
                    "action": "update"
                }
            
            elif parsed.action == MemoryAction.DELETE:
                # 查找要删除的记忆
                search_results = await self._memory_manager.search_memories(
                    query=parsed.target_description,
                    user_id=user_id,
                    limit=1
                )
                
                if not search_results:
                    return {
                        "success": False,
                        "message": f"没有找到与 '{parsed.target_description}' 相关的记忆",
                        "action": "delete"
                    }
                
                # 删除记忆
                success = await self._memory_manager.delete_memory(
                    memory_id=search_results[0].memory.id,
                    soft_delete=True
                )
                
                return {
                    "success": success,
                    "message": "已忘记该记忆" if success else "删除失败",
                    "action": "delete"
                }
            
            elif parsed.action == MemoryAction.QUERY:
                # 查询记忆
                search_results = await self._memory_manager.search_memories(
                    query=parsed.target_description or command,
                    user_id=user_id,
                    limit=5
                )
                
                memories = [r.memory.model_dump() for r in search_results]
                
                return {
                    "success": True,
                    "message": f"找到 {len(memories)} 条相关记忆",
                    "action": "query",
                    "memories": memories
                }
            
            return {"success": False, "message": "未知操作", "action": "none"}
            
        except Exception as e:
            logger.error("Failed to handle manual command: %s", e)
            return {"success": False, "message": f"处理失败: {str(e)}"}
    
    async def _handle_command_with_rules(
        self,
        command: str,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """
        使用简单规则处理记忆管理指令（无 LLM 时的回退方案）
        
        Args:
            command: 用户指令
            user_id: 用户ID
            
        Returns:
            Dict: 处理结果
        """
        command_lower = command.lower().strip()
        
        # 添加记忆的模式
        add_patterns = [
            r"^记住[，,：:\s]*(.+)$",
            r"^记得[，,：:\s]*(.+)$",
            r"^请记住[，,：:\s]*(.+)$",
            r"^帮我记住[，,：:\s]*(.+)$",
            r"^remember[,:\s]*(.+)$",
        ]
        
        for pattern in add_patterns:
            match = re.match(pattern, command, re.IGNORECASE)
            if match:
                content = match.group(1).strip()
                if content:
                    memory = await self._memory_manager.add_memory(
                        content=content,
                        user_id=user_id,
                        memory_type=MemoryType.CUSTOM,
                        source="manual"
                    )
                    return {
                        "success": True,
                        "message": f"已记住：{content}",
                        "action": "add",
                        "memory": memory.model_dump() if memory else None
                    }
        
        # 删除/忘记记忆的模式
        delete_patterns = [
            r"^忘记[，,：:\s]*(.+)$",
            r"^忘掉[，,：:\s]*(.+)$",
            r"^删除[，,：:\s]*(.+)$",
            r"^forget[,:\s]*(.+)$",
        ]
        
        for pattern in delete_patterns:
            match = re.match(pattern, command, re.IGNORECASE)
            if match:
                target = match.group(1).strip()
                if target:
                    # 搜索相关记忆
                    search_results = await self._memory_manager.search_memories(
                        query=target,
                        user_id=user_id,
                        limit=1
                    )
                    
                    if not search_results:
                        return {
                            "success": False,
                            "message": f"没有找到与 '{target}' 相关的记忆",
                            "action": "delete"
                        }
                    
                    # 删除找到的记忆
                    success = await self._memory_manager.delete_memory(
                        memory_id=search_results[0].memory.id,
                        soft_delete=True
                    )
                    
                    return {
                        "success": success,
                        "message": "已忘记该记忆" if success else "删除失败",
                        "action": "delete"
                    }
        
        # 查询记忆的模式
        query_patterns = [
            r"^你记得.+吗[？?]?$",
            r"^我.+是什么[？?]?$",
            r"^查找[，,：:\s]*(.+)$",
            r"^搜索[，,：:\s]*(.+)$",
        ]
        
        for pattern in query_patterns:
            match = re.match(pattern, command, re.IGNORECASE)
            if match:
                query = match.group(1).strip() if match.lastindex else command
                search_results = await self._memory_manager.search_memories(
                    query=query or command,
                    user_id=user_id,
                    limit=5
                )
                
                memories = [r.memory.model_dump() for r in search_results]
                
                return {
                    "success": True,
                    "message": f"找到 {len(memories)} 条相关记忆",
                    "action": "query",
                    "memories": memories
                }
        
        # 如果没有匹配到任何模式，尝试作为添加记忆处理
        # 对于简单的陈述句，直接添加为记忆
        if len(command) > 5 and not command.endswith('?') and not command.endswith('？'):
            memory = await self._memory_manager.add_memory(
                content=command,
                user_id=user_id,
                memory_type=MemoryType.CUSTOM,
                source="manual"
            )
            return {
                "success": True,
                "message": f"已记住：{command}",
                "action": "add",
                "memory": memory.model_dump() if memory else None
            }
        
        return {
            "success": False,
            "message": "无法理解指令。请使用如：'记住，...' 或 '忘记...' 格式",
            "action": "none"
        }
    
    # ==================== 记忆检索 ====================
    
    async def get_context_for_query(
        self,
        query: str,
        user_id: str = "default",
        limit: int = 5,
    ) -> MemoryContext:
        """
        获取查询的记忆上下文
        
        Args:
            query: 用户查询
            user_id: 用户ID
            limit: 最大记忆数
            
        Returns:
            MemoryContext: 记忆上下文
        """
        if not self._memory_retriever:
            return MemoryContext()
        
        return await self._memory_retriever.retrieve_for_query(
            query=query,
            user_id=user_id,
            limit=limit
        )
    
    async def get_full_context(
        self,
        query: str,
        user_id: str = "default",
    ) -> MemoryContext:
        """
        获取完整的记忆上下文（包含查询相关和常用记忆）
        
        Args:
            query: 用户查询
            user_id: 用户ID
            
        Returns:
            MemoryContext: 完整记忆上下文
        """
        if not self._memory_retriever:
            return MemoryContext()
        
        return await self._memory_retriever.build_full_context(
            query=query,
            user_id=user_id
        )
    
    # ==================== CRUD 操作 ====================
    
    async def add_memory(
        self,
        content: str,
        user_id: str = "default",
        memory_type: MemoryType = MemoryType.CUSTOM,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Memory]:
        """添加记忆"""
        return await self._memory_manager.add_memory(
            content=content,
            user_id=user_id,
            memory_type=memory_type,
            metadata=metadata,
            source="manual"
        )
    
    async def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        memory_type: Optional[MemoryType] = None,
        metadata: Optional[Dict[str, Any]] = None,
        is_active: Optional[bool] = None,
    ) -> bool:
        """更新记忆"""
        return await self._memory_manager.update_memory(
            memory_id=memory_id,
            content=content,
            memory_type=memory_type,
            metadata=metadata,
            is_active=is_active
        )
    
    async def delete_memory(self, memory_id: str, soft_delete: bool = True) -> bool:
        """删除记忆"""
        return await self._memory_manager.delete_memory(memory_id, soft_delete)
    
    async def get_all_memories(
        self,
        user_id: str = "default",
        include_inactive: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Memory]:
        """获取所有记忆"""
        return await self._memory_manager.get_all_memories(
            user_id=user_id,
            include_inactive=include_inactive,
            limit=limit,
            offset=offset
        )
    
    async def search_memories(
        self,
        query: str,
        user_id: str = "default",
        limit: int = 10,
        memory_types: Optional[List[MemoryType]] = None,
    ) -> List[MemorySearchResult]:
        """搜索记忆"""
        return await self._memory_manager.search_memories(
            query=query,
            user_id=user_id,
            limit=limit,
            memory_types=memory_types
        )
    
    async def get_stats(self, user_id: str = "default") -> MemoryStats:
        """获取统计信息"""
        return await self._memory_manager.get_stats(user_id)
    
    async def cleanup(self):
        """清理资源"""
        if self._memory_manager:
            await self._memory_manager.cleanup()


# 全局服务实例
_memory_service: Optional[MemoryService] = None


def get_memory_service() -> MemoryService:
    """获取全局记忆服务实例"""
    global _memory_service
    if _memory_service is None:
        _memory_service = MemoryService()
    return _memory_service


async def initialize_memory_service(
    llm_call_func: Optional[Callable[[List[dict]], Coroutine[Any, Any, dict]]] = None
) -> bool:
    """初始化全局记忆服务"""
    service = get_memory_service()
    if llm_call_func:
        service.set_llm_call_func(llm_call_func)
    return await service.initialize()
