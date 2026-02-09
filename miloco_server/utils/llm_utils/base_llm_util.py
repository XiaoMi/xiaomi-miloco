# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

import json
import logging
from typing import Any, List, Optional

from miloco_server.schema.chat_history_schema import ChatHistoryMessages
from miloco_server.utils.local_models import ModelPurpose
from openai.types.chat import ChatCompletion, ChatCompletionToolParam
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall

logger = logging.getLogger(__name__)


class BaseLLMUtil:
    """Used to convert natural language of actions to Action structure language"""

    def __init__(
        self,
        request_id: str,
        query: Optional[str] = None,
        tools_meta: Optional[List[ChatCompletionToolParam]] = None,
    ):
        from miloco_server.service.manager import get_manager # pylint: disable=import-outside-toplevel
        self._manager = get_manager()

        self._request_id = request_id
        self._llm_proxy = self._manager.get_llm_proxy_by_purpose(ModelPurpose.PLANNING) # use PLANNING as default
        self._tool_executor = self._manager.tool_executor
        self._chat_history = ChatHistoryMessages()
        self._query = query
        self._tools_meta = tools_meta
        self._priority: Optional[int] = None  # Subclasses can override if they need priority


    def _get_system_prompt(self) -> str:
        """Get system prompt"""
        pass

    async def run(self) -> Any:
        """Run agent to process user query"""
        pass

    def _init_conversation(self) -> None:
        """Initialize conversation history"""
        pass

    async def _call_llm(
            self) -> tuple[str, List[ChatCompletionMessageToolCall], str]:
        """Call large language model"""
        try:
            if not self._llm_proxy:
                raise RuntimeError(
                    "LLM proxy not exit, Please configure on the Model Settings Page.")
            chat_messages = self._chat_history.get_messages()
            # Only pass priority if it's set and non-zero
            priority = self._priority if self._priority and self._priority > 0 else None
            llm_result = await self._llm_proxy.async_call_llm(
                chat_messages, self._tools_meta, priority)

            logger.info("[%s] LLM response: %s", self._request_id, llm_result)

            if not llm_result.get("success", False):
                error = llm_result.get("error", "Unknown error")
                raise RuntimeError(f"LLM call failed: {error}")

            # Extract message information from dictionary response
            response_data: ChatCompletion = llm_result["response"]
            choices = response_data.choices

            if not choices:
                raise RuntimeError("No choices in LLM response")

            choice = choices[0]
            message = choice.message
            finish_reason = choice.finish_reason

            # Add message to conversation history
            self._chat_history.add_assistant_message(message.content,
                                                     message.tool_calls)

            content = message.content
            tool_calls = message.tool_calls

            return content, tool_calls, finish_reason

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("[%s] Error occurred while calling LLM: %s",
                         self._request_id, str(e))
            raise e


    def _has_tool_calls(self, tool_calls: List[ChatCompletionMessageToolCall]) -> bool:
        """Check if there are tool calls"""
        return bool(tool_calls)


    async def _execute_tools(self, tool_calls: List[ChatCompletionMessageToolCall]) -> None:
        """Execute tool calls"""
        for tool_call in tool_calls:
            await self._execute_tool(tool_call)


    async def _execute_tool(self, tool_call: ChatCompletionMessageToolCall) -> None:
        """Execute tool call"""
        tool_call_id = tool_call.id
        function_name = tool_call.function.name
        try:
            logger.info("[%s] Executing tool: %s", self._request_id, function_name)
            result = await self._tool_executor.execute_tool(tool_call)
            logger.info("[%s] Call tool %s returned: %s", self._request_id, function_name, result)
            
            result_content = (json.dumps(result.response, ensure_ascii=False) 
                            if result.success else result.error_message)
            self._chat_history.add_tool_call_res_content(tool_call_id, function_name, result_content)

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("[%s] Error occurred during tool %s execution: %s",
                        self._request_id, function_name, str(e), exc_info=True)


    def _is_completion_step(self, finish_reason: str) -> bool:
        """Check if it is a completion step"""
        return finish_reason == "stop"
