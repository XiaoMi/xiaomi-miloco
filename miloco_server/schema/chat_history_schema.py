# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Chat history data models
Define data structures related to chat history
"""

import json
import logging
from typing import Any, List, Optional, Union

from openai.types.chat import ChatCompletionMessageToolCall, ChatCompletionMessageToolCallParam
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.chat.chat_completion_message_tool_call_param import Function
from pydantic import BaseModel, Field

from miloco_server.schema.chat_schema import Event, Instruction, Template

logger = logging.getLogger(__name__)


class ChatHistoryMessages:
    """Chat history messages manager"""

    def __init__(self, messages: Optional[list[ChatCompletionMessageParam]] = None):
        self._messages: list[ChatCompletionMessageParam] = messages if messages is not None else []
        # Track up to which length the history has been checked for incomplete tool calls.
        self._last_sanitized_len: int = 0

    def _extract_tool_call_ids(self, tool_calls: Optional[list]) -> set[str]:
        """Extract tool_call ids from assistant tool_calls field (supports dict or pydantic models)."""
        ids: set[str] = set()
        if not tool_calls:
            return ids
        for tool_call in tool_calls:
            tc_id = None
            if isinstance(tool_call, dict):
                tc_id = tool_call.get("id")
            else:
                tc_id = getattr(tool_call, "id", None)
            if tc_id:
                ids.add(tc_id)
        return ids

    def _sanitize_incomplete_tool_calls(self) -> None:
        """
        Remove assistant messages that issued tool_calls but lack corresponding tool responses.

        OpenAI API requires every assistant tool_call to be followed by tool messages that respond
        to each tool_call_id. When a conversation is interrupted (e.g., user clicks Stop), the
        assistant tool_call may remain without tool replies, causing subsequent LLM calls to fail.
        This sanitizer drops such incomplete pairs to keep the message history valid.
        """
        if not self._messages:
            return

        if len(self._messages) <= self._last_sanitized_len:
            return

        # Process only the new tail (plus one lookback to bridge sequences).
        start_idx = max(0, self._last_sanitized_len - 1)
        cleaned: list[ChatCompletionMessageParam] = self._messages[:start_idx]
        removed = False
        i = start_idx
        while i < len(self._messages):
            msg = self._messages[i]
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)

            if role == "assistant":
                tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
                tool_call_ids = self._extract_tool_call_ids(tool_calls)

                if tool_call_ids:
                    tool_msgs: list[ChatCompletionMessageParam] = []
                    j = i + 1
                    while j < len(self._messages):
                        next_msg = self._messages[j]
                        next_role = next_msg.get("role") if isinstance(next_msg, dict) else getattr(next_msg, "role", None)
                        if next_role == "tool":
                            tool_msgs.append(next_msg)
                            j += 1
                            continue
                        break

                    matched_ids: set[str] = set()
                    for tool_msg in tool_msgs:
                        tc_id = tool_msg.get("tool_call_id") if isinstance(tool_msg, dict) else getattr(tool_msg, "tool_call_id", None)
                        if tc_id:
                            matched_ids.add(tc_id)

                    if not tool_call_ids.issubset(matched_ids):
                        # Also drop the triggering user message if it is immediately before this assistant.
                        if cleaned:
                            prev_msg = cleaned[-1]
                            prev_role = prev_msg.get("role") if isinstance(prev_msg, dict) else getattr(prev_msg, "role", None)
                            if prev_role == "user":
                                cleaned.pop()
                        removed = True
                        i = j
                        continue

                    cleaned.append(msg)
                    cleaned.extend(tool_msgs)
                    i = j
                    continue

            cleaned.append(msg)
            i += 1

        if removed:
            logger.info("Removed %d incomplete tail tool call messages from history", len(self._messages) - len(cleaned))
            self._messages = cleaned

        # Mark sanitized up to current length
        self._last_sanitized_len = len(self._messages)

    def add_content(self, role: str, content: str):
        """
        Add message
        """
        self._messages.append({"role": role, "content": content})

    def add_content_list(self, role: str, content_list: list[dict]):
        """
        Add message
        """
        add_content = []
        for content_dict in content_list:
            add_content.append(content_dict)
        self._messages.append({"role": role, "content": add_content})

    def add_tool_call_res_content(self, tool_call_id: str, name: str,
                                  content: str):
        """
        Add tool call content
        """
        self._messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content
        })

    def add_message(self, message: dict[str, Any]):
        self._messages.append(message)

    def add_assistant_message(
            self,
            content: str,
            tool_calls: list[ChatCompletionMessageToolCall] = None):
        message = {"role": "assistant", "content": content}
        if tool_calls:
            message[
                "tool_calls"] = ChatHistoryMessages.message_tool_call_2_param(
                    tool_calls)
        self._messages.append(message)

    def has_initialized(self) -> bool:
        """
        Check if initialized
        """
        return len(
            self._messages) > 0 and self._messages[0].get("role") == "system"

    def get_messages(self) -> list[ChatCompletionMessageParam]:
        """
        Get messages
        """
        self._sanitize_incomplete_tool_calls()
        return self._messages

    def to_json(self) -> str:
        """
        Convert messages to JSON
        """
        try:
            self._sanitize_incomplete_tool_calls()
            return json.dumps(self._messages, ensure_ascii=False)
        except (ValueError, TypeError) as e:
            logger.error("Error serializing messages: %s", e, exc_info=True)
            return ""

    @staticmethod
    def from_json(json_str: Optional[str]) -> "ChatHistoryMessages":
        """
        Convert JSON to messages
        """
        if json_str is None or json_str == "":
            return ChatHistoryMessages()
        messages_data = json.loads(json_str)
        chat_history_messages = ChatHistoryMessages(messages_data)
        return chat_history_messages

    @staticmethod
    def message_tool_call_2_param(
        tool_calls: list[ChatCompletionMessageToolCall]
    ) -> list[ChatCompletionMessageToolCallParam]:
        """Convert ChatCompletionMessageToolCall to ChatCompletionMessageToolCallParam"""
        return [
            ChatCompletionMessageToolCallParam(
                id=tool_call.id,
                type="function",
                function=Function(name=tool_call.function.name,
                                  arguments=tool_call.function.arguments))
            for tool_call in tool_calls
        ]


class ChatHistorySession(BaseModel):
    """Chat history session"""
    data: List[Union[Event, Instruction]] = Field(default_factory=list, description="Session list")

    def add_event(self, event: Event):
        self.data.append(event)

    def add_instruction(self, instruction: Instruction):
        self.data.append(instruction)

    def zip_toast_stream(self) -> None:
        """Merge ToastStream"""
        session = []
        current_toast_stream_header = None
        toast = ""
        for item in self.data:
            if (isinstance(item, Instruction) and item.header.type == "instruction" and
                    item.header.namespace == "Template" and item.header.name == "ToastStream"):
                if not current_toast_stream_header:
                    current_toast_stream_header = item.header.model_copy(deep=True)
                    toast = json.loads(item.payload).get("stream", "")
                else:
                    toast += json.loads(item.payload).get("stream", "")
            else:
                if current_toast_stream_header:
                    toast_stream = Template.ToastStream(stream=toast)
                    instruction = Instruction(
                        header=current_toast_stream_header,
                        payload=toast_stream.model_dump_json())
                    session.append(instruction)
                    current_toast_stream_header = None
                    toast = ""

                session.append(item)

        if current_toast_stream_header:
            toast_stream = Template.ToastStream(stream=toast)
            instruction = Instruction(
                header=current_toast_stream_header,
                payload=toast_stream.model_dump_json())
            session.append(instruction)
            current_toast_stream_header = None
            toast = ""

        self.data = session


class ChatHistorySimpleInfo(BaseModel):
    session_id: str = Field(..., description="Record ID, UUID")
    title: str = Field(..., description="Conversation title")
    timestamp: int = Field(..., description="Timestamp")

class ChatHistoryResponse(ChatHistorySimpleInfo):
    session: ChatHistorySession = Field(
        ...,
        description="Session content, alternating Event/Instruction sequence, can be empty"
    )


class ChatHistoryStorage(ChatHistoryResponse):
    messages: Optional[str] = Field(None, description="Message content serialized JSON schema")
