# Copyright (C) 2025 willianfu
# XiaoAI Speaker Integration Module for Miloco Server
#
# AI conversation client using Miloco's ChatAgent.

"""
AI conversation client for XiaoAI integration.

Provides AI conversation capabilities by integrating with Miloco's
existing ChatAgent infrastructure. Supports:
- MCP tool integration
- Camera data access
- Memory management
- Context compression for long conversations
- Session management
"""

import re
import json
import uuid
import asyncio
import logging
import time
from typing import Optional, List, Any
from dataclasses import dataclass, field

from miloco_server.xiaoai.config import XiaoAIConfig, ContextCompressionConfig, TTSPlaybackConfig

logger = logging.getLogger(__name__)


@dataclass
class ConversationMessage:
    """A message in the conversation."""
    role: str  # "user", "assistant", "system", "tool"
    content: str
    timestamp: float = 0.0
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Any]] = None
    
    def to_dict(self) -> dict:
        result = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        return result
    
    def estimate_tokens(self) -> int:
        """Rough token estimation (Chinese ~1.5 chars per token)."""
        return max(1, len(self.content) // 2)


@dataclass
class ResponsePart:
    """AI响应中的一个部分"""
    type: str  # "thinking", "tool_call", "tool_result", "final_answer", "text"
    content: str


@dataclass
class AIResponse:
    """AI响应结果"""
    text: str  # 最终回答文本（仅final_answer内容）
    success: bool
    error_message: Optional[str] = None
    tool_calls_made: int = 0
    full_response: str = ""  # 所有步骤的完整原始文本（包含标签）
    response_parts: List[Any] = field(default_factory=list)  # List[ResponsePart] 有序的响应部分


class ContextCompressor:
    """Handles context compression for long conversations."""
    
    def __init__(self, config: ContextCompressionConfig):
        self.config = config
        self._manager = None
    
    async def _get_manager(self):
        if self._manager is None:
            from miloco_server.service.manager import get_manager
            self._manager = get_manager()
        return self._manager
    
    def needs_compression(self, messages: List[ConversationMessage]) -> bool:
        """Check if messages need compression."""
        if not self.config.enabled:
            return False
        
        # Count non-system messages
        user_assistant_count = sum(
            1 for m in messages if m.role in ("user", "assistant")
        )
        
        if user_assistant_count > self.config.max_messages:
            return True
        
        # Check total tokens
        total_tokens = sum(m.estimate_tokens() for m in messages)
        if total_tokens > self.config.max_tokens:
            return True
        
        return False
    
    async def compress(
        self,
        messages: List[ConversationMessage]
    ) -> List[ConversationMessage]:
        """
        Compress conversation history.
        
        Args:
            messages: List of conversation messages
            
        Returns:
            Compressed message list
        """
        if not self.needs_compression(messages):
            return messages
        
        strategy = self.config.strategy
        
        if strategy == "auto":
            # Auto-select strategy based on conversation length
            total_tokens = sum(m.estimate_tokens() for m in messages)
            if total_tokens > self.config.max_tokens * 2:
                strategy = "summary"
            elif len(messages) > self.config.max_messages * 2:
                strategy = "summary"
            else:
                strategy = "sliding"
        
        if strategy == "summary":
            return await self._compress_with_summary(messages)
        elif strategy == "truncate":
            return self._compress_with_truncate(messages)
        else:  # sliding
            return self._compress_with_sliding(messages)
    
    def _compress_with_sliding(
        self,
        messages: List[ConversationMessage]
    ) -> List[ConversationMessage]:
        """Sliding window compression - keep recent messages."""
        system_msgs = [m for m in messages if m.role == "system"]
        other_msgs = [m for m in messages if m.role != "system"]
        
        keep_count = self.config.keep_recent * 2  # user + assistant pairs
        if len(other_msgs) > keep_count:
            other_msgs = other_msgs[-keep_count:]
        
        return system_msgs + other_msgs
    
    def _compress_with_truncate(
        self,
        messages: List[ConversationMessage]
    ) -> List[ConversationMessage]:
        """Simple truncation - keep first (system) and recent messages."""
        system_msgs = [m for m in messages if m.role == "system"]
        other_msgs = [m for m in messages if m.role != "system"]
        
        keep_count = self.config.keep_recent * 2
        if len(other_msgs) > keep_count:
            other_msgs = other_msgs[-keep_count:]
        
        return system_msgs + other_msgs
    
    async def _compress_with_summary(
        self,
        messages: List[ConversationMessage]
    ) -> List[ConversationMessage]:
        """Summarize old messages using AI."""
        try:
            manager = await self._get_manager()
            from miloco_server.utils.local_models import ModelPurpose
            
            llm_proxy = manager.get_llm_proxy_by_purpose(ModelPurpose.PLANNING)
            if not llm_proxy:
                # Fallback to sliding window
                return self._compress_with_sliding(messages)
            
            system_msgs = [m for m in messages if m.role == "system"]
            other_msgs = [m for m in messages if m.role != "system"]
            
            if len(other_msgs) <= self.config.keep_recent * 2:
                return messages
            
            # Split into old (to summarize) and recent (to keep)
            keep_count = self.config.keep_recent * 2
            old_msgs = other_msgs[:-keep_count]
            recent_msgs = other_msgs[-keep_count:]
            
            # Build conversation text for summarization
            conv_text = "\n".join([
                f"{m.role}: {m.content}" for m in old_msgs
                if m.role in ("user", "assistant")
            ])
            
            # Create summary request
            summary_prompt = f"""请将以下对话历史总结为简洁的摘要，保留关键信息和上下文：

{conv_text}

请用1-3句话总结上述对话的主要内容和结论。"""

            summary_messages = [
                {"role": "system", "content": "你是一个对话摘要助手，请简洁地总结对话内容。"},
                {"role": "user", "content": summary_prompt}
            ]
            
            # Call LLM for summary
            result = await llm_proxy.async_call_llm(summary_messages)
            
            if result.get("success") and result.get("content"):
                summary = result["content"]
                
                # Create summary message
                summary_msg = ConversationMessage(
                    role="system",
                    content=f"[历史对话摘要] {summary}",
                    timestamp=time.time()
                )
                
                return system_msgs + [summary_msg] + recent_msgs
            else:
                # Fallback to sliding window
                return self._compress_with_sliding(messages)
                
        except Exception as e:
            logger.error("Context compression with summary failed: %s", e)
            return self._compress_with_sliding(messages)


class AIConversationClient:
    """
    AI conversation client for a single speaker.
    
    Each speaker has its own AIConversationClient instance for
    isolated conversation management.
    """
    
    def __init__(
        self,
        speaker_id: str,
        config: XiaoAIConfig
    ):
        """
        初始化AI对话客户端
        
        Args:
            speaker_id: 音箱唯一标识
            config: 小爱音箱配置
        """
        self._speaker_id = speaker_id
        self._config = config
        self._conversation_history: List[ConversationMessage] = []
        # session_data: 用于保存到聊天历史的 Event/Instruction 列表
        # 格式与AI对话页面一致，前端 processHistorySocketMessages 可以直接解析
        self._session_data: List[Any] = []
        self._session_id: Optional[str] = None
        self._manager = None
        self._initialized = False
        self._compressor = ContextCompressor(config.context_compression)
    
    @property
    def speaker_id(self) -> str:
        return self._speaker_id
    
    @property
    def session_id(self) -> Optional[str]:
        return self._session_id
    
    async def initialize(self) -> bool:
        """Initialize the AI client."""
        if self._initialized:
            return True
        
        try:
            from miloco_server.service.manager import get_manager
            self._manager = get_manager()
            self._session_id = str(uuid.uuid4())
            self._initialized = True
            logger.info("[%s] AI conversation client initialized, session: %s",
                       self._speaker_id, self._session_id)
            return True
        except Exception as e:
            logger.error("Failed to initialize AI client: %s", e)
            return False
    
    def get_history_summary(self) -> dict:
        """Get a summary of current conversation history."""
        return {
            "session_id": self._session_id,
            "speaker_id": self._speaker_id,
            "message_count": len(self._conversation_history),
            "messages": [
                {"role": m.role, "content": m.content[:100] + "..." if len(m.content) > 100 else m.content}
                for m in self._conversation_history[-10:]
            ]
        }
    
    def clear_history(self):
        """清空对话历史（不保存）"""
        self._conversation_history = []
        self._session_data = []
        self._session_id = str(uuid.uuid4())
        logger.info("[%s] 对话历史已清空，新会话: %s",
                   self._speaker_id, self._session_id)
    
    async def save_and_new_session(self) -> Optional[str]:
        """
        保存当前会话并开始新会话
        
        Returns:
            保存的 session_id 或 None（无需保存时）
        """
        if not self._conversation_history and not self._session_data:
            self._session_id = str(uuid.uuid4())
            return None
        
        old_session_id = self._session_id
        
        try:
            await self._save_to_chat_history()
            
            # 开始新会话
            self._conversation_history = []
            self._session_data = []
            self._session_id = str(uuid.uuid4())
            
            logger.info("[%s] 会话已保存 (%s) 并开始新会话 (%s)",
                       self._speaker_id, old_session_id, self._session_id)
            
            return old_session_id
        except Exception as e:
            logger.error("保存会话失败: %s", e)
            return None
    
    async def incremental_save(self):
        """增量保存当前会话到聊天历史
        
        不会清空对话历史或创建新会话，只是将当前状态保存/更新到数据库。
        每次调用都会覆盖之前保存的同一会话。
        """
        if not self._conversation_history:
            return
        
        try:
            await self._save_to_chat_history()
            logger.info("[%s] 会话增量保存完成，session_id=%s", 
                       self._speaker_id, self._session_id)
        except Exception as e:
            logger.error("[%s] 增量保存失败: %s", self._speaker_id, e, exc_info=True)
    
    async def _save_to_chat_history(self):
        """保存当前会话到聊天历史数据库
        
        使用 _session_data（Event/Instruction 列表）保存，格式与AI对话页面完全一致。
        前端 processHistorySocketMessages 可以直接解析。
        
        _session_data 在 _process_query 中实时填充：
        1. Event (Nlp.Request) - 用户问题
        2. Instruction (Template.ToastStream) - AI思考和文本输出
        3. Instruction (Template.CallTool) - 工具调用
        4. Instruction (Template.CallToolResult) - 工具返回结果
        5. Instruction (Template.CameraImages) - 摄像头图片（如果有）
        6. Instruction (Dialog.Finish) - 对话结束
        """
        if not self._session_data or not self._session_id:
            return
        
        try:
            from miloco_server.schema.chat_history_schema import (
                ChatHistoryStorage, ChatHistorySession, ChatHistoryMessages
            )
            
            # 从第一条用户消息构建标题
            first_user_msg = next(
                (m for m in self._conversation_history if m.role == "user"),
                None
            )
            title = first_user_msg.content[:50] if first_user_msg else "XiaoAI对话"
            if len(first_user_msg.content if first_user_msg else "") > 50:
                title += "..."
            
            # 标记为音箱会话
            title = f"[音箱] {title}"
            
            # 直接使用 _session_data（已是 Event/Instruction 格式）
            session = ChatHistorySession(data=list(self._session_data))
            
            # 同时保存 messages 格式（作为备份）
            messages = ChatHistoryMessages()
            for msg in self._conversation_history:
                if msg.role in ("system", "user", "assistant"):
                    messages.add_content(msg.role, msg.content)
            
            storage = ChatHistoryStorage(
                session_id=self._session_id,
                title=title,
                timestamp=int(time.time() * 1000),
                session=session,
                messages=messages.to_json()
            )
            
            # 保存到数据库
            self._manager.chat_companion.store_chat_history(storage)
            logger.info("[%s] 会话历史已保存，session_id=%s, session_data=%d条", 
                       self._speaker_id, self._session_id, len(self._session_data))
            
        except Exception as e:
            logger.error("[%s] 保存聊天历史失败: %s", self._speaker_id, e, exc_info=True)
    
    async def ask(self, query: str) -> AIResponse:
        """
        Ask the AI a question and get a response.
        
        Args:
            query: The user's question/message
            
        Returns:
            AIResponse with the final text response
        """
        if not self._initialized:
            if not await self.initialize():
                return AIResponse(
                    text="AI服务初始化失败",
                    success=False,
                    error_message="Failed to initialize AI client"
                )
        
        try:
            # Check for context compression before processing
            if self._compressor.needs_compression(self._conversation_history):
                logger.info("[%s] Compressing conversation context", self._speaker_id)
                self._conversation_history = await self._compressor.compress(
                    self._conversation_history
                )
            
            return await self._process_query(query)
        except Exception as e:
            logger.error("Error processing AI query: %s", e, exc_info=True)
            return AIResponse(
                text="抱歉，处理您的请求时出现错误",
                success=False,
                error_message=str(e)
            )
    
    async def _process_query(self, query: str) -> AIResponse:
        """Process a query through the AI system."""
        from miloco_server.utils.local_models import ModelPurpose
        from miloco_server.schema.chat_history_schema import ChatHistoryMessages
        from miloco_server.schema.mcp_schema import LocalMcpClientId
        from miloco_server.config import PromptConfig
        from miloco_server.config.prompt_config import PromptType, UserLanguage
        
        request_id = str(uuid.uuid4())
        logger.info("[%s][%s] Processing query: %s", 
                   self._speaker_id, request_id, query[:50])
        
        # Get LLM proxy
        llm_proxy = self._manager.get_llm_proxy_by_purpose(ModelPurpose.PLANNING)
        if not llm_proxy:
            return AIResponse(
                text="AI模型未配置，请先在设置页面配置模型",
                success=False,
                error_message="LLM proxy not configured"
            )
        
        # Get tool executor
        tool_executor = self._manager.tool_executor
        
        # Build tools metadata
        all_tools = []
        
        # Always include local default tools
        local_tools = tool_executor.get_mcp_chat_completion_tools(
            mcp_client_ids=[LocalMcpClientId.LOCAL_DEFAULT]
        )
        all_tools.extend(local_tools)
        
        # Add user-specified MCP tools
        if self._config.mcp_list:
            filtered_mcp_list = [
                mcp_id for mcp_id in self._config.mcp_list
                if mcp_id != LocalMcpClientId.LOCAL_DEFAULT
            ]
            if filtered_mcp_list:
                other_tools = tool_executor.get_mcp_chat_completion_tools(filtered_mcp_list)
                all_tools.extend(other_tools)
        
        # Build messages
        chat_messages = ChatHistoryMessages()
        
        # Add system prompt
        language = self._manager.auth_service.get_user_language().language
        system_prompt = (
            self._config.system_prompt 
            if self._config.system_prompt 
            else PromptConfig.get_system_prompt(PromptType.CHAT, UserLanguage(language))
        )
        chat_messages.add_content("system", system_prompt)
        
        # 添加对话历史（只包含用户和助手消息，用于LLM上下文）
        for msg in self._conversation_history:
            if msg.role == "user":
                chat_messages.add_content("user", msg.content)
            elif msg.role == "assistant":
                # 历史中不再存储 tool_calls，只存储文本内容
                chat_messages.add_content("assistant", msg.content)
        
        # Add current query
        chat_messages.add_content("user", query)
        
        # 存储摄像头图片和其他元数据供工具使用
        if self._config.camera_ids:
            try:
                from miloco_server.utils.chat_companion import ChatCachedData
                camera_images = await self._manager.miot_service.get_miot_cameras_img(
                    camera_dids=self._config.camera_ids
                )
                self._manager.chat_companion.set_chat_data(
                    request_id,
                    ChatCachedData(
                        camera_images=camera_images if camera_images else None,
                        camera_ids=self._config.camera_ids,
                        mcp_ids=self._config.mcp_list,
                    )
                )
            except Exception as e:
                logger.warning("获取摄像头图片失败: %s", e)
        else:
            # 即使没有摄像头配置，也存储 chat_data 以便工具能找到 request_id
            from miloco_server.utils.chat_companion import ChatCachedData
            self._manager.chat_companion.set_chat_data(
                request_id,
                ChatCachedData(
                    camera_ids=self._config.camera_ids,
                    mcp_ids=self._config.mcp_list,
                )
            )
        
        # === 构建 session_data（用于保存到聊天历史，与AI对话页面格式一致）===
        from miloco_server.schema.chat_schema import (
            Event as ChatEvent, Instruction, Header, Template, Dialog
        )
        
        ts_now = int(time.time() * 1000)
        
        # 添加用户请求事件到 session_data
        user_event = ChatEvent(
            header=Header(
                type="event", namespace="Nlp", name="Request",
                timestamp=ts_now, request_id=request_id,
                session_id=self._session_id
            ),
            payload=json.dumps({
                "query": query,
                "mcp_list": self._config.mcp_list or [],
                "camera_ids": self._config.camera_ids or []
            }, ensure_ascii=False)
        )
        self._session_data.append(user_event)
        
        # 执行AI循环
        all_step_contents = []  # 收集所有步骤的文本
        response_parts = []     # 有序的响应部分（用于TTS控制）
        tool_calls_count = 0
        max_steps = 10
        
        try:
            for step in range(max_steps):
                logger.debug("[%s] 执行步骤 %d/%d", request_id, step + 1, max_steps)
                
                # 调用LLM
                messages = chat_messages.get_messages()
                response_stream = llm_proxy.async_call_llm_stream(messages, all_tools)
                
                # 收集响应
                content_parts = []
                tool_calls_data = []
                finish_reason = None
                
                async for chunk in response_stream:
                    if not chunk.get("success", False):
                        error_msg = chunk.get("error", "Unknown error")
                        logger.error("[%s] LLM错误: %s", request_id, error_msg)
                        break
                    
                    chat_chunk = chunk.get("chunk")
                    if not chat_chunk or not chat_chunk.choices:
                        continue
                    
                    choice = chat_chunk.choices[0]
                    delta = choice.delta
                    
                    if delta.content:
                        content_parts.append(delta.content)
                    
                    if delta.tool_calls:
                        tool_calls_data.append(delta.tool_calls)
                    
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                
                step_content = "".join(content_parts)
                
                if step_content:
                    all_step_contents.append(step_content)
                    # 注意：不在这里解析 response_parts，因为标签可能跨多个步骤
                    # 完整的解析在循环结束后进行
                    
                    # 添加 ToastStream 到 session_data（保留完整内容包含标签）
                    toast_inst = Instruction(
                        header=Header(
                            type="instruction", namespace="Template", name="ToastStream",
                            timestamp=int(time.time() * 1000), request_id=request_id,
                            session_id=self._session_id
                        ),
                        payload=json.dumps({"stream": step_content}, ensure_ascii=False)
                    )
                    self._session_data.append(toast_inst)
                
                merged_tool_calls = self._merge_tool_calls(tool_calls_data)
                chat_messages.add_assistant_message(step_content, merged_tool_calls)
                
                if finish_reason == "stop":
                    break
                
                if merged_tool_calls:
                    tool_calls_count += len(merged_tool_calls)
                    
                    for tool_call in merged_tool_calls:
                        tool_name = tool_call.function.name
                        tool_id = tool_call.id
                        client_id = tool_name.split("__")[0] if "__" in tool_name else ""
                        actual_tool_name = tool_name.split("__")[1] if "__" in tool_name else tool_name
                        
                        # 记录工具调用到响应部分（用于TTS）
                        response_parts.append(ResponsePart(
                            type="tool_call",
                            content=f"调用{self._get_tool_description(actual_tool_name)}工具"
                        ))
                        
                        # 添加 CallTool 到 session_data
                        call_tool_inst = Instruction(
                            header=Header(
                                type="instruction", namespace="Template", name="CallTool",
                                timestamp=int(time.time() * 1000), request_id=request_id,
                                session_id=self._session_id
                            ),
                            payload=json.dumps({
                                "id": tool_id,
                                "service_name": client_id,
                                "tool_name": actual_tool_name,
                                "tool_params": tool_call.function.arguments
                            }, ensure_ascii=False)
                        )
                        self._session_data.append(call_tool_inst)
                        
                        # 执行工具
                        tool_result = await self._execute_tool(
                            tool_executor, tool_call, request_id
                        )
                        chat_messages.add_tool_call_res_content(
                            tool_id, tool_name, tool_result
                        )
                        
                        # 记录工具结果到响应部分（用于TTS）
                        response_parts.append(ResponsePart(
                            type="tool_result",
                            content=f"{self._get_tool_description(actual_tool_name)}执行完成"
                        ))
                        
                        # 添加 CallToolResult 到 session_data
                        is_error = tool_result.startswith("工具执行错误") or tool_result.startswith("Tool execution error")
                        call_result_inst = Instruction(
                            header=Header(
                                type="instruction", namespace="Template", name="CallToolResult",
                                timestamp=int(time.time() * 1000), request_id=request_id,
                                session_id=self._session_id
                            ),
                            payload=json.dumps({
                                "id": tool_id,
                                "success": not is_error,
                                "tool_response": tool_result if not is_error else None,
                                "error_message": tool_result if is_error else None
                            }, ensure_ascii=False)
                        )
                        self._session_data.append(call_result_inst)
                else:
                    if step_content:
                        break
            
            # 合并所有步骤的完整文本
            full_response = "\n".join(all_step_contents)
            
            # 在完整响应上重新解析 response_parts，确保标签顺序正确
            # （标签可能跨多个streaming步骤，只有完整文本才能正确解析）
            text_parts = []  # 临时存储从文本解析出的部分
            self._parse_step_content(full_response, text_parts)
            
            # 合并工具调用部分（response_parts中已有）和文本部分
            # 工具调用应该在对应位置，文本部分按照解析顺序
            # 由于工具调用是按实际执行顺序记录的，我们把文本部分插入到最后
            # 但需要确保 final_answer 在最后
            final_response_parts = []
            
            # 先添加工具调用相关的部分（已经按执行顺序记录）
            for part in response_parts:
                if part.type in ("tool_call", "tool_result"):
                    final_response_parts.append(part)
            
            # 再添加文本解析的部分（thinking 和 final_answer）
            # 但要确保 final_answer 始终在最后
            thinking_parts = [p for p in text_parts if p.type == "thinking"]
            answer_parts = [p for p in text_parts if p.type == "final_answer"]
            other_parts = [p for p in text_parts if p.type not in ("thinking", "final_answer")]
            
            # 按顺序添加：thinking -> 其他文本 -> final_answer
            final_response_parts.extend(thinking_parts)
            final_response_parts.extend(other_parts)
            final_response_parts.extend(answer_parts)
            
            response_parts = final_response_parts
            
            # 提取最终回答
            final_answer = self._extract_final_answer(full_response)
            if not final_answer:
                # 如果没有 final_answer 标签，使用最后一步的内容
                final_answer = self._clean_tags(all_step_contents[-1]) if all_step_contents else ""
            
            # 添加 Dialog.Finish 到 session_data
            finish_inst = Instruction(
                header=Header(
                    type="instruction", namespace="Dialog", name="Finish",
                    timestamp=int(time.time() * 1000), request_id=request_id,
                    session_id=self._session_id
                ),
                payload=json.dumps({"success": True}, ensure_ascii=False)
            )
            self._session_data.append(finish_inst)
            
            # 更新对话历史（仅用于LLM上下文，不存储tool_calls元数据）
            self._conversation_history.append(
                ConversationMessage(role="user", content=query, timestamp=time.time())
            )
            if full_response:
                self._conversation_history.append(
                    ConversationMessage(
                        role="assistant", 
                        content=full_response, 
                        timestamp=time.time()
                    )
                )
            
            # 清理缓存数据
            self._manager.chat_companion.clear_chat_data(request_id)
            
            return AIResponse(
                text=final_answer or "抱歉，我无法生成回复",
                success=bool(final_answer),
                tool_calls_made=tool_calls_count,
                full_response=full_response,
                response_parts=response_parts
            )
            
        except Exception as e:
            logger.error("[%s] AI处理错误: %s", request_id, e, exc_info=True)
            # 添加失败的 Finish 到 session_data
            try:
                fail_inst = Instruction(
                    header=Header(
                        type="instruction", namespace="Dialog", name="Finish",
                        timestamp=int(time.time() * 1000), request_id=request_id,
                        session_id=self._session_id
                    ),
                    payload=json.dumps({"success": False}, ensure_ascii=False)
                )
                self._session_data.append(fail_inst)
            except Exception:
                pass
            return AIResponse(
                text="处理请求时出现错误",
                success=False,
                error_message=str(e)
            )
    
    def _merge_tool_calls(self, tool_calls_chunks: List[List[Any]]) -> List[Any]:
        """Merge streaming tool call chunks."""
        if not tool_calls_chunks:
            return []
        
        from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall
        
        aggregated = {}
        
        for chunk_calls in tool_calls_chunks:
            if not chunk_calls:
                continue
            
            for delta_call in chunk_calls:
                index = getattr(delta_call, "index", 0) or 0
                
                if index not in aggregated:
                    aggregated[index] = {
                        "id": None,
                        "type": "function",
                        "function": {"name": None, "arguments": ""}
                    }
                
                current = aggregated[index]
                
                if getattr(delta_call, "id", None):
                    current["id"] = delta_call.id
                
                delta_func = getattr(delta_call, "function", None)
                if delta_func:
                    if getattr(delta_func, "name", None):
                        current["function"]["name"] = delta_func.name
                    if getattr(delta_func, "arguments", None):
                        current["function"]["arguments"] += delta_func.arguments
        
        result = []
        for idx in sorted(aggregated.keys()):
            agg = aggregated[idx]
            result.append(ChatCompletionMessageToolCall(
                id=agg["id"] or f"call_{idx}",
                type="function",
                function=agg["function"]
            ))
        
        return result
    
    async def _execute_tool(
        self,
        tool_executor,
        tool_call: Any,
        request_id: str
    ) -> str:
        """执行工具调用
        
        对于需要 request_id 的工具（如 vision_understand, create_rule），
        自动注入正确的 request_id 参数。
        """
        try:
            client_id, tool_name, parameters = tool_executor.parse_tool_call(tool_call)
            
            # 自动注入 request_id 到需要它的工具参数中
            if tool_name in ("vision_understand", "create_rule"):
                if isinstance(parameters, dict):
                    parameters["request_id"] = request_id
                    logger.info("[%s] 自动注入 request_id 到工具 %s", request_id, tool_name)
            
            logger.info("[%s] 执行工具: %s.%s", request_id, client_id, tool_name)
            
            result = await tool_executor.execute_tool_by_params(
                client_id=client_id,
                tool_name=tool_name,
                parameters=parameters
            )
            
            if result.success:
                return json.dumps(result.response, ensure_ascii=False)
            else:
                return result.error_message or "工具执行失败"
                
        except Exception as e:
            logger.error("[%s] 工具执行错误: %s", request_id, e)
            return f"工具执行错误: {str(e)}"
    
    @staticmethod
    def _parse_step_content(content: str, parts: List[Any]):
        """解析单步AI输出内容为有序的响应部分
        
        从AI文本中提取:
        - <reflect>...</reflect> → thinking 部分
        - <final_answer>...</final_answer> → final_answer 部分
        - 其他文本 → text 部分
        """
        if not content:
            return
        
        # 匹配 reflect 和 final_answer 标签
        pattern = r'(<reflect>)(.*?)(</reflect>)|(<final_answer>)(.*?)(</final_answer>)'
        last_end = 0
        
        for match in re.finditer(pattern, content, re.DOTALL):
            # 标签前的普通文本
            if match.start() > last_end:
                text_before = content[last_end:match.start()].strip()
                if text_before:
                    parts.append(ResponsePart(type="text", content=text_before))
            
            if match.group(2) is not None:
                # <reflect> 匹配
                thinking_text = match.group(2).strip()
                if thinking_text:
                    parts.append(ResponsePart(type="thinking", content=thinking_text))
            elif match.group(5) is not None:
                # <final_answer> 匹配
                answer_text = match.group(5).strip()
                if answer_text:
                    parts.append(ResponsePart(type="final_answer", content=answer_text))
            
            last_end = match.end()
        
        # 标签后的剩余文本
        if last_end < len(content):
            remaining = content[last_end:].strip()
            if remaining:
                parts.append(ResponsePart(type="text", content=remaining))
    
    @staticmethod
    def _extract_final_answer(text: str) -> str:
        """从AI响应中提取 <final_answer> 标签内的内容"""
        if not text:
            return ""
        match = re.search(r'<final_answer>(.*?)</final_answer>', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""
    
    @staticmethod
    def _clean_tags(text: str) -> str:
        """清理AI响应中的所有标签，只保留纯文本内容"""
        if not text:
            return text
        cleaned = re.sub(r'</?(?:reflect|final_answer|think|thinking|answer|response|result|output)>', '', text)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()
    
    @staticmethod
    def _get_tool_description(tool_name: str) -> str:
        """获取工具的简短中文描述"""
        descriptions = {
            "miot_get_devices": "查询设备",
            "miot_get_device_properties": "查询设备属性",
            "miot_control_device": "控制设备",
            "miot_execute_scene": "执行场景",
            "vision_understand": "视觉理解",
            "create_rule": "创建规则",
            "ha_get_entities": "查询HA实体",
            "ha_control_entity": "控制HA设备",
            "ha_execute_automation": "执行HA自动化",
        }
        return descriptions.get(tool_name, tool_name)
    
    @staticmethod
    def build_tts_text(ai_response: 'AIResponse', tts_config: TTSPlaybackConfig) -> str:
        """根据TTS配置构建语音播报文本
        
        根据配置决定播报哪些内容:
        - play_thinking: 是否播报思考过程
        - play_tool_calls: 是否播报工具调用和结果（简短描述）
        - final_answer: 始终播报
        
        Args:
            ai_response: AI响应对象
            tts_config: TTS播报配置
            
        Returns:
            构建好的TTS文本
        """
        if not ai_response.response_parts:
            # 没有解析的部分，回退到清理后的完整文本
            return AIConversationClient._clean_tags(ai_response.text)
        
        tts_parts = []
        
        for part in ai_response.response_parts:
            if part.type == "thinking" and tts_config.play_thinking:
                tts_parts.append(part.content)
            elif part.type in ("tool_call", "tool_result") and tts_config.play_tool_calls:
                tts_parts.append(part.content)
            elif part.type == "final_answer":
                # 最终回答始终播报
                tts_parts.append(part.content)
            # "text" 类型一般是标签外的内容，跳过
        
        if not tts_parts:
            # 如果没有匹配的部分，至少播报最终回答
            return AIConversationClient._clean_tags(ai_response.text)
        
        # 用换行分隔各部分，确保自然停顿
        return "\n".join(tts_parts)
