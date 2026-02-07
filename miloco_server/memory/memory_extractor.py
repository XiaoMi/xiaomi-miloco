# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Memory Extractor - Extract valuable memories from conversations using LLM.
记忆提取器 - 使用 LLM 从对话中提取有价值的记忆
"""

import json
import logging
from typing import Optional, List, Callable, Coroutine, Any

from miloco_server.schema.memory_schema import (
    Memory,
    MemoryType,
    MemoryAction,
    MemoryExtractionResult,
    ManualMemoryCommand,
)

logger = logging.getLogger(__name__)


# 记忆提取的系统提示词
MEMORY_EXTRACTION_SYSTEM_PROMPT = """你是一个智能家居助手的记忆管理模块。你的任务是分析用户的对话内容，判断是否包含值得长期记忆的信息。

## 判断标准
以下类型的信息应该被记忆：
1. **用户偏好** (preference): 如温度偏好、亮度偏好、设备使用习惯
   - 例：「我怕冷，空调开26度」→ 记忆「用户怕冷，偏好空调温度26度」
2. **事实信息** (fact): 如宠物名字、家庭成员、房间用途
   - 例：「我的猫叫咪咪」→ 记忆「用户的猫名叫咪咪」
3. **生活习惯** (habit): 如作息时间、日常安排
   - 例：「我每天7点起床」→ 记忆「用户每天7点起床」
4. **设备设置偏好** (device_setting): 特定设备的常用设置
   - 例：「客厅灯调到50%亮度就好」→ 记忆「用户偏好客厅灯亮度50%」
5. **时间安排** (schedule): 定期的活动或安排
   - 例：「周末我一般10点才起」→ 记忆「用户周末通常10点起床」
6. **关系信息** (relationship): 家庭成员、朋友等关系
   - 例：「我妈妈住在主卧」→ 记忆「用户的妈妈住在主卧」

以下内容不应该被记忆：
- 简单的问候和寒暄（如「你好」「谢谢」）
- 一次性的操作指令（如「开灯」「关空调」）
- 临时性的状态描述（如「现在有点热」不确定是否是长期偏好）
- 模糊不确定的表述

## 更新和删除判断
如果用户表述暗示需要更新或删除之前的记忆：
- 「我现在喜欢25度了」→ 应该更新之前的温度偏好
- 「忘记我说的空调偏好」→ 应该删除相关记忆
- 「我的睡眠时间改成11点」→ 应该更新睡眠时间记忆

## 输出格式
请以JSON格式输出分析结果：
```json
{
    "should_save": true/false,
    "action": "add/update/delete/none",
    "memories": [
        {
            "content": "记忆内容（简洁清晰的陈述句）",
            "memory_type": "preference/fact/habit/device_setting/schedule/relationship/custom",
            "confidence": 0.0-1.0
        }
    ],
    "reasoning": "判断理由",
    "related_memory_ids": ["如果是更新/删除操作，这里填写相关的记忆ID"]
}
```

注意：
- 记忆内容应该是简洁的陈述句，去除对话中的口语化表达
- 如果一条消息包含多个值得记忆的信息，可以提取多条记忆
- confidence 表示你对这条记忆重要性的判断置信度
"""

# 手动记忆管理的系统提示词
MANUAL_MEMORY_SYSTEM_PROMPT = """你是一个智能家居助手的记忆管理模块。用户正在通过自然语言管理他们的记忆。

## 你的任务
分析用户的指令，判断用户想要进行什么操作：
1. **添加记忆** (add): 用户想让你记住某些信息
   - 例：「记住，我的猫叫咪咪」「帮我记一下，我喜欢26度」
2. **更新记忆** (update): 用户想修改之前的记忆
   - 例：「把我的睡眠时间改成12点」「更新一下，我现在喜欢25度了」
3. **删除记忆** (delete): 用户想删除某些记忆
   - 例：「忘记我说的空调偏好」「删除关于我睡眠时间的记忆」
4. **查询记忆** (query): 用户想查看记忆
   - 例：「你记得我的猫叫什么吗」「我之前说过什么偏好」

## 输出格式
请以JSON格式输出分析结果：
```json
{
    "action": "add/update/delete/query/none",
    "content": "要记忆的内容（简洁的陈述句）",
    "memory_type": "preference/fact/habit/device_setting/schedule/relationship/custom",
    "target_description": "目标记忆的描述（用于更新/删除时查找相关记忆）",
    "confidence": 0.0-1.0
}
```

注意：
- 如果无法确定用户意图，action 设为 "none"
- 对于删除和更新操作，target_description 应该描述要操作的记忆特征
"""


class MemoryExtractor:
    """
    记忆提取器
    
    使用 LLM 分析对话内容，提取值得长期保存的记忆。
    支持：
    - 自动从对话中提取记忆
    - 判断记忆是否有保存价值
    - 识别更新/删除意图
    - 解析手动记忆管理指令
    """
    
    def __init__(
        self,
        llm_call_func: Callable[[List[dict]], Coroutine[Any, Any, dict]],
    ):
        """
        初始化记忆提取器
        
        Args:
            llm_call_func: LLM 调用函数，接收 messages 列表，返回响应字典
        """
        self._llm_call = llm_call_func
    
    async def extract_memories(
        self,
        user_message: str,
        assistant_response: Optional[str] = None,
        context_messages: Optional[List[dict]] = None,
        existing_memories: Optional[List[Memory]] = None,
    ) -> MemoryExtractionResult:
        """
        从对话中提取记忆
        
        Args:
            user_message: 用户消息
            assistant_response: 助手响应（可选）
            context_messages: 上下文消息（可选）
            existing_memories: 已有的相关记忆（用于判断更新/删除）
            
        Returns:
            MemoryExtractionResult: 提取结果
        """
        try:
            # 构建分析内容
            analysis_content = f"用户消息：{user_message}"
            if assistant_response:
                analysis_content += f"\n助手响应：{assistant_response}"
            
            # 添加已有记忆上下文（帮助判断是否需要更新）
            if existing_memories:
                memory_context = "\n已有相关记忆：\n" + "\n".join([
                    f"- [{m.id}] {m.content}" for m in existing_memories[:5]
                ])
                analysis_content += memory_context
            
            # 调用 LLM 分析
            messages = [
                {"role": "system", "content": MEMORY_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": analysis_content}
            ]
            
            response = await self._llm_call(messages)
            content = response.get("content", "")
            
            # 解析 JSON 响应
            result = self._parse_extraction_response(content)
            
            logger.debug("Memory extraction: message='%s', should_save=%s, count=%d", 
                        user_message[:50], result.should_save, len(result.memories))
            
            return result
            
        except Exception as e:
            logger.error("Failed to extract memories: %s", e)
            return MemoryExtractionResult(
                should_save=False,
                action=MemoryAction.NONE,
                memories=[],
                reasoning=f"提取失败: {str(e)}"
            )
    
    async def parse_manual_command(
        self,
        command: str,
    ) -> ManualMemoryCommand:
        """
        解析手动记忆管理指令
        
        Args:
            command: 用户的自然语言指令
            
        Returns:
            ManualMemoryCommand: 解析结果
        """
        try:
            messages = [
                {"role": "system", "content": MANUAL_MEMORY_SYSTEM_PROMPT},
                {"role": "user", "content": command}
            ]
            
            response = await self._llm_call(messages)
            content = response.get("content", "")
            
            # 解析 JSON 响应
            result = self._parse_manual_command_response(content)
            
            logger.debug("Manual command parsed: command='%s', action=%s", 
                        command[:50], result.action)
            
            return result
            
        except Exception as e:
            logger.error("Failed to parse manual command: %s", e)
            return ManualMemoryCommand(
                action=MemoryAction.NONE,
                confidence=0.0
            )
    
    def _parse_extraction_response(self, content: str) -> MemoryExtractionResult:
        """解析 LLM 的提取响应"""
        try:
            # 尝试提取 JSON
            json_str = self._extract_json(content)
            data = json.loads(json_str)
            
            memories = []
            for mem_data in data.get("memories", []):
                memory = Memory(
                    content=mem_data.get("content", ""),
                    memory_type=MemoryType(mem_data.get("memory_type", "custom")),
                    confidence=mem_data.get("confidence", 1.0),
                )
                memories.append(memory)
            
            return MemoryExtractionResult(
                should_save=data.get("should_save", False),
                action=MemoryAction(data.get("action", "none")),
                memories=memories,
                reasoning=data.get("reasoning", ""),
                related_memory_ids=data.get("related_memory_ids", [])
            )
            
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Failed to parse extraction response: %s, content: %s", e, content[:200])
            return MemoryExtractionResult(
                should_save=False,
                action=MemoryAction.NONE,
                memories=[],
                reasoning=f"解析失败: {str(e)}"
            )
    
    def _parse_manual_command_response(self, content: str) -> ManualMemoryCommand:
        """解析 LLM 的手动指令响应"""
        try:
            json_str = self._extract_json(content)
            data = json.loads(json_str)
            
            return ManualMemoryCommand(
                action=MemoryAction(data.get("action", "none")),
                content=data.get("content", ""),
                memory_type=MemoryType(data.get("memory_type", "custom")),
                target_description=data.get("target_description", ""),
                confidence=data.get("confidence", 1.0)
            )
            
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Failed to parse manual command response: %s", e)
            return ManualMemoryCommand(
                action=MemoryAction.NONE,
                confidence=0.0
            )
    
    def _extract_json(self, content: str) -> str:
        """从内容中提取 JSON 字符串"""
        # 尝试找到 JSON 代码块
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            if end > start:
                return content[start:end].strip()
        
        # 尝试找到 JSON 对象
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            return content[start:end]
        
        return content


class SmartMemoryFilter:
    """
    智能记忆过滤器
    
    基于规则快速过滤明显不需要记忆的内容，减少 LLM 调用。
    """
    
    # 不需要记忆的关键词模式
    SKIP_PATTERNS = [
        # 简单问候
        "你好", "您好", "hello", "hi", "嗨",
        # 感谢
        "谢谢", "感谢", "thanks", "thank you",
        # 确认
        "好的", "好", "ok", "行", "可以", "明白",
        # 简单操作指令（无偏好信息）
        "开灯", "关灯", "开空调", "关空调", "打开", "关闭",
    ]
    
    # 表示偏好的关键词
    PREFERENCE_KEYWORDS = [
        "喜欢", "偏好", "习惯", "总是", "通常", "一般",
        "怕冷", "怕热", "喜欢亮", "喜欢暗",
        "度", "亮度", "音量",
        "每天", "每周", "每月",
    ]
    
    # 表示事实的关键词
    FACT_KEYWORDS = [
        "叫", "名字", "是", "住在", "在", "有",
        "我的", "我们的", "家里的",
    ]
    
    # 表示记忆管理的关键词
    MEMORY_MANAGEMENT_KEYWORDS = [
        "记住", "记一下", "帮我记", "别忘了",
        "忘记", "删除", "忘掉", "不要记",
        "修改", "更新", "改成", "改为",
        "你记得", "还记得", "之前说的",
    ]
    
    @classmethod
    def should_skip(cls, message: str) -> bool:
        """
        判断消息是否应该跳过（不需要调用 LLM 分析）
        
        Args:
            message: 用户消息
            
        Returns:
            bool: 是否应该跳过
        """
        message_lower = message.lower().strip()
        
        # 消息太短
        if len(message_lower) < 3:
            return True
        
        # 完全匹配跳过模式
        for pattern in cls.SKIP_PATTERNS:
            if message_lower == pattern.lower():
                return True
        
        # 检查是否包含值得记忆的关键词
        has_preference = any(kw in message for kw in cls.PREFERENCE_KEYWORDS)
        has_fact = any(kw in message for kw in cls.FACT_KEYWORDS)
        has_memory_mgmt = any(kw in message for kw in cls.MEMORY_MANAGEMENT_KEYWORDS)
        
        # 如果包含任何有价值的关键词，不跳过
        if has_preference or has_fact or has_memory_mgmt:
            return False
        
        # 消息较短且不包含有价值关键词，跳过
        if len(message_lower) < 15:
            return True
        
        return False
    
    @classmethod
    def is_memory_management_command(cls, message: str) -> bool:
        """
        判断是否是记忆管理指令
        
        Args:
            message: 用户消息
            
        Returns:
            bool: 是否是记忆管理指令
        """
        return any(kw in message for kw in cls.MEMORY_MANAGEMENT_KEYWORDS)
