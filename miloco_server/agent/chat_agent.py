# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Chat Agent"""
import json
import logging
from typing import AsyncGenerator, Any, Optional

from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam
from thespian.actors import Actor, ActorAddress, ActorExitRequest

from miloco_server import actor_system
from miloco_server.config import PromptConfig, CHAT_CONFIG
from miloco_server.config.prompt_config import PromptType, UserLanguage
from miloco_server.middleware.exceptions import LLMServiceException, ResourceNotFoundException
from miloco_server.schema.chat_history_schema import ChatHistoryMessages
from miloco_server.schema.chat_schema import Dialog, Event, InstructionPayload, Template
from miloco_server.schema.mcp_schema import CallToolResult, LocalMcpClientId
from miloco_server.utils.chat_companion import ChatCachedData
from miloco_server.utils.local_models import ModelPurpose


logger = logging.getLogger(__name__)


class ChatAgent(Actor):
    """
    Chat Agent implementation - ReActAgent based on think-act-observe loop
    
    This agent can:
    1. Analyze user queries
    2. Execute operations through tool calls
    3. Provide final answers based on results
    
    Refactored using thespian Actor model for better concurrency and state isolation
    """

    def __init__(
        self,
        request_id: str,
        out_actor_address: ActorAddress,
        chat_history_messages: Optional[ChatHistoryMessages] = None,
    ):
        """Initialize ReAct agent Actor.

        Args:
            request_id: Unique identifier for the request.
            out_actor_address: Address of the out actor.
            chat_history_messages_json: JSON string containing chat history.
        """
        super().__init__()
        from miloco_server.service.manager import get_manager  # pylint: disable=import-outside-toplevel
        self._manager = get_manager()

        self._request_id = request_id
        self._chat_companion = self._manager.chat_companion
        self._llm_proxy = self._manager.get_llm_proxy_by_purpose(
            ModelPurpose.PLANNING)
        self._language = self._manager.auth_service.get_user_language(
        ).language
        logger.info("[%s] LLM proxy: %s",self._request_id, self._llm_proxy)
        self._tool_executor = self._manager.tool_executor
        self._local_default_mcp_tools_meta = []
        self._other_mcp_tools_meta = []
        self._all_mcp_tools_meta = []

        self._out_actor_address = out_actor_address
        self._max_steps = CHAT_CONFIG["agent_max_steps"]

        self._init_conversation(chat_history_messages)

        self._chat_companion.set_chat_data(
            self._request_id,
            ChatCachedData(
                out_actor_address=self._out_actor_address,
            ))

        logger.info("[%s] ChatAgent initialized", self._request_id)

    def _set_tools_meta(
            self,
            mcp_list: Optional[list[str]],
            exclude_tool_names: Optional[list[str]] = None,
            ) -> list[ChatCompletionToolParam]:
        """Initialize tool metadata.

        Args:
            mcp_list: List of MCP client IDs.

        Returns:
            List of chat completion tool parameters.
        """
        if mcp_list is None:
            mcp_list = []

        self._local_default_mcp_tools_meta = self._tool_executor.get_mcp_chat_completion_tools(
            mcp_client_ids=[LocalMcpClientId.LOCAL_DEFAULT],
            exclude_tool_names=exclude_tool_names,
        )

        mcp_list = list(
            filter(lambda x: x != LocalMcpClientId.LOCAL_DEFAULT, mcp_list))

        self._other_mcp_tools_meta = self._tool_executor.get_mcp_chat_completion_tools(
            mcp_list, exclude_tool_names=exclude_tool_names)
        self._all_mcp_tools_meta = self._local_default_mcp_tools_meta + self._other_mcp_tools_meta

        logger.info("[%s] Initializing tool metadata: %s", self._request_id,
                    self._all_mcp_tools_meta)

    def _init_conversation(self, chat_history_messages: Optional[ChatHistoryMessages]) -> None:
        """Initialize conversation history.

        Args:
            chat_history_messages: Chat history messages.
        """
        self._chat_history_messages = (
            chat_history_messages if chat_history_messages is not None
            else ChatHistoryMessages())
        if not self._chat_history_messages.has_initialized():
            self._chat_history_messages.add_content("system",
                                                    self._get_system_prompt())

    def receiveMessage(self, msg, sender):
        """
        Actor message receiving method, handles received messages.

        Args:
            message: The message received.
            sender: The sender of the message.
        """
        if isinstance(msg, Event):
            self._handle_event(msg)
        elif isinstance(msg, ActorExitRequest):
            logger.info("[%s] ChatAgent ActorExitRequest received",
                        self._request_id)
            self._handle_exit_request()
        else:
            logger.warning("[%s] Unsupported message: %s", self._request_id,
                           msg)


    def _handle_event(self, event: Event) -> None:
        """Handle event."""
        logger.info("[%s] handle_event: %s", self._request_id, event)
        try:
            self._parse_and_handle_event(event)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("[%s] Unexpected error handling event: %s",
                         self._request_id, e)
            self._send_instruction(
                Dialog.Exception(message=f"ChatAgent handle_event Unexpected Error: {e}"))
            self._send_dialog_finish(False)


    def _parse_and_handle_event(self, event: Event) -> None:
        """Parse and handle event."""
        pass

    async def _run_chat(self, query: str) -> None:
        """Run agent to process user query.

        Args:
            query: The user query to process.
        """
        logger.info(
            "[%s] Starting to process user query: %s", self._request_id, query)
        success = False
        error_message = None
        try:
            self._chat_history_messages.add_content(
                "user", f"request_id: {self._request_id}, query: {query}")

            success, error_message = await self._cyclic_execute()

        except Exception as e:  # pylint: disable=broad-except
            logger.error(
                "[%s] Unexpected error occurred during agent execution: %s",
                self._request_id, str(e), exc_info=True)
            success = False
            error_message = f"Unexpected error: {str(e)}"

        finally:
            logger.info(
                "[%s] finalizing chat, success: %s, error_message: %s",
                self._request_id, success, error_message)
            await self._run_finally_do(success, error_message)

    async def _cyclic_execute(self) -> tuple[bool, str | None]:
        """Cyclic execute agent steps."""
        step_number = 0
        for step in range(self._max_steps):
            step_number = step + 1
            logger.info("[%s] Executing step %d/%d ",
                        self._request_id, step_number, self._max_steps)

            finish_reason = await self._execute_step(step_number)

            if self._is_completion_step(finish_reason):
                logger.info("[%s] Agent has completed the task",
                            self._request_id)
                return True, None

        logger.warning("[%s] Reached maximum number of steps %d",
                       self._request_id, self._max_steps)
        return False, "Maximum operation steps reached"


    async def _run_finally_do(self, success: bool, error_message: str | None) -> None:
        """Run finally do."""
        if not success:
            self._send_instruction(Dialog.Exception(message=error_message))
        self._send_dialog_finish(success)


    async def _execute_step(self, step_number: int) -> Optional[str]:
        """Execute single agent step."""
        try:
            llm_response: AsyncGenerator[dict,
                                         None] = await self._call_llm_stream()

            chunk_content_cache: list[str] = []
            delta_tool_call_list: list[list[ChoiceDeltaToolCall]] = []
            finish_reason = None

            async for chunk in llm_response:
                current_finish_reason, current_tool_calls, content_stream = await self._process_llm_chunk(
                    chunk)
                logger.debug(
                    "[%s] LLM response: %s, current_finish_reason: %s, current_tool_calls: %s, content_stream: %s",
                    self._request_id, chunk, current_finish_reason,
                    current_tool_calls, content_stream)

                if content_stream is not None and content_stream != "":
                    chunk_content_cache.append(content_stream)
                    self._send_instruction(
                        Template.ToastStream(stream=content_stream))

                if current_tool_calls is not None:
                    delta_tool_call_list.append(current_tool_calls)

                if current_finish_reason is not None and current_finish_reason != "":
                    finish_reason = current_finish_reason
                    break

            finalized_content = "".join(chunk_content_cache)
            finalized_tool_calls: list[
                ChatCompletionMessageToolCall] = self._merge_delta_tool_calls(
                    delta_tool_call_list)

            logger.info(
                "[%s] ChatAgent step %d finalized_content: %s, finalized_tool_calls: %s, finish_reason: %s",
                self._request_id, step_number, finalized_content,
                finalized_tool_calls, finish_reason)

            self._chat_history_messages.add_assistant_message(
                finalized_content, finalized_tool_calls)

            if self._has_tool_calls(finalized_tool_calls):
                await self._execute_tools(finalized_tool_calls)

            return finish_reason

        except Exception as e:
            logger.error("[%s] Error occurred while executing agent step: %s",
                         self._request_id, str(e))
            raise LLMServiceException(f"Error occurred while calling LLM: {str(e)}") from e

    async def _call_llm_stream(self) -> AsyncGenerator[dict, None]:
        """Call large language model."""
        try:
            if not self._llm_proxy:  # Not initially configured
                raise ResourceNotFoundException(
                    "Planning model not exit, Please configure on the Model Settings Page")
            chat_messages = self._chat_history_messages.get_messages()
            logger.info("Start to calling LLM: %s. chat_messages: %s", self._request_id, chat_messages)
            return self._llm_proxy.async_call_llm_stream(chat_messages, self._all_mcp_tools_meta)
        except Exception as e:
            logger.error("[%s] Error occurred while calling LLM: %s",
                         self._request_id, str(e))
            raise

    async def _process_llm_chunk(
        self, chunk: dict
    ) -> tuple[Optional[str], Optional[list[ChoiceDeltaToolCall]],
               Optional[str]]:
        """Process large language model streaming response."""
        logger.debug("[%s] Processing LLM chunk: %s", self._request_id, chunk)
        if not chunk.get("success", False):
            error_msg = chunk.get("error", "Unknown error")
            raise RuntimeError(f"LLM stream error: {error_msg}")

        chat_chunk: ChatCompletionChunk = chunk["chunk"]
        if not chat_chunk.choices:
            raise RuntimeError("No choices in LLM response")

        choice = chat_chunk.choices[0]
        delta = choice.delta
        finish_reason = choice.finish_reason

        content_stream = delta.content
        tool_calls = delta.tool_calls

        return finish_reason, tool_calls, content_stream

    def _send_instruction(self, instruction_payload: InstructionPayload):
        """Send instruction to transceiver actor."""
        actor_system.tell(self._out_actor_address, instruction_payload)

    def _send_dialog_finish(self, success: bool):
        """Send dialog finish instruction."""
        logger.info("[%s] send_dialog_finish: %s", self._request_id, success)
        self._send_instruction(Dialog.Finish(success=success))

    def _has_tool_calls(
            self, tool_calls: list[ChatCompletionMessageToolCall]) -> bool:
        """Check if there are tool calls."""
        return tool_calls is not None and len(tool_calls) > 0

    def _merge_delta_tool_calls(
        self, delta_tool_call_list: list[list[ChoiceDeltaToolCall]]
    ) -> list[ChatCompletionMessageToolCall]:
        """Merge delta tool call information."""
        if not delta_tool_call_list:
            return []

        # Aggregation structure: index ->
        # {"id": str|None, "type": "function", "function": {"name": str|None, "arguments": str}}
        aggregated_calls: dict[int, dict[str, Any]] = {}

        for chunk_tool_calls in delta_tool_call_list:
            if not chunk_tool_calls:
                continue
            for delta_tool_call in chunk_tool_calls:
                try:
                    call_index: Optional[int] = getattr(
                        delta_tool_call, "index", None)
                except AttributeError:
                    call_index = None
                if call_index is None:
                    call_index = 0

                if call_index not in aggregated_calls:
                    aggregated_calls[call_index] = {
                        "id": None,
                        "type": "function",
                        "function": {
                            "name": None,
                            "arguments": ""
                        },
                    }

                current = aggregated_calls[call_index]

                # Merge id
                try:
                    delta_id = getattr(delta_tool_call, "id", None)
                except AttributeError:
                    delta_id = None
                if delta_id:
                    current["id"] = delta_id

                # Merge function deltas (name and arguments)
                delta_function = getattr(delta_tool_call, "function", None)
                if delta_function is not None:
                    delta_name = getattr(delta_function, "name", None)
                    if delta_name:
                        current["function"]["name"] = delta_name

                    delta_arguments = getattr(delta_function, "arguments",
                                              None)
                    if delta_arguments:
                        # arguments may arrive in multiple chunks, append sequentially
                        current["function"]["arguments"] += delta_arguments

        # Build finalized ChatCompletionMessageToolCall list ordered by index
        finalized_calls: list[ChatCompletionMessageToolCall] = []
        for call_index in sorted(aggregated_calls.keys()):
            agg = aggregated_calls[call_index]
            call_id = agg.get("id") or f"call_{call_index}"
            function_obj = {
                "name": agg["function"].get("name") or "",
                "arguments": agg["function"].get("arguments") or "",
            }
            finalized_calls.append(
                ChatCompletionMessageToolCall(
                    id=call_id,
                    type="function",
                    function=function_obj,  # type: ignore[arg-type]
                ))

        return finalized_calls

    async def _execute_tools(
            self, tool_calls: list[ChatCompletionMessageToolCall]) -> None:
        """Execute tool calls."""
        for tool_call in tool_calls:
            await self._execute_single_tool(tool_call)

    async def _execute_single_tool(
            self, tool_call: ChatCompletionMessageToolCall) -> None:
        """Execute single tool call."""
        original_tool_name = tool_call.function.name
        tool_id = tool_call.id
        tool_call_content = ""

        try:
            logger.info("[%s] Executing tool: %s", self._request_id,
                        original_tool_name)

            client_id, tool_name, parameters = self._tool_executor.parse_tool_call(
                tool_call)
            service_name = self._tool_executor.get_server_name(client_id)

            self._send_instruction(
                Template.CallTool(id=tool_id,
                                    service_name=service_name,
                                    tool_name=tool_name,
                                    tool_params=tool_call.function.arguments))

            result = await self._tool_executor.execute_tool_by_params(
                client_id=client_id, tool_name=tool_name, parameters=parameters)

            logger.info("[%s] Tool call %s returned: %s", self._request_id,
                        tool_name, result)

            response_json = json.dumps(result.response, ensure_ascii=False)

            self._send_instruction(
                Template.CallToolResult(id=tool_id,
                                        success=result.success,
                                        tool_response=response_json,
                                        error_message=result.error_message))

            if result.success:
                tool_call_content = response_json
            else:
                tool_call_content = result.error_message

            self._chat_history_messages.add_tool_call_res_content(
                    tool_id, tool_name, tool_call_content)

            self._post_process_tool_call(client_id, service_name, tool_name, parameters, result)

        except Exception as e:  # pylint: disable=broad-except
            logger.error(
                "[%s] Unexpected error occurred while executing tool %s: %s",
                self._request_id, original_tool_name, str(e))
            self._send_instruction(Dialog.Exception(message=str(e)))


    def _post_process_tool_call(
            self, client_id: str, mcp_server_name: str,
            tool_name: str, parameters: dict, result: CallToolResult) -> None:
        """Post process tool call."""
        pass


    def _is_completion_step(self, finish_reason: Optional[str]) -> bool:
        """Check if this is a completion step."""
        return finish_reason == "stop"

    def _get_system_prompt(self) -> str:
        """Get system prompt."""
        return PromptConfig.get_system_prompt(PromptType.CHAT,
                                              UserLanguage(self._language))

    def _handle_exit_request(self):
        """Handle Actor exit request."""
        logger.info("[%s] ChatAgent handling exit request", self._request_id)
        self._chat_companion.clear_chat_data(self._request_id)
