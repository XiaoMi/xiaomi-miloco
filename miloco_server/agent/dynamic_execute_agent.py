# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Chat Agent"""
import json
import logging
from typing import Optional
from thespian.actors import ActorAddress
from miloco_server.schema.chat_history_schema import ChatHistoryMessages
from miloco_server.utils.chat_companion import ChatCachedData
from miloco_server.schema.chat_schema import Dialog, Event, Confirmation, Nlp
from miloco_server.schema.mcp_schema import CallToolResult, LocalMcpClientId
from miloco_server.schema.trigger_schema import Action
from miloco_server.agent.chat_agent import ChatAgent
from miloco_server.config.prompt_config import PromptConfig, UserLanguage, PromptType
import asyncio

logger = logging.getLogger(__name__)


class ActionDescriptionDynamicExecuteAgent(ChatAgent):
    """Nlp Action Description Dynamic Execute Agent"""
    def __init__(
        self,
        request_id: str,
        out_actor_address: ActorAddress,
        chat_history_messages: Optional[ChatHistoryMessages] = None,
    ):
        
        super().__init__(request_id=request_id, out_actor_address=out_actor_address, 
                        priority=PromptConfig.get_priority(PromptType.ACTION_DESCRIPTION_DYNAMIC_EXECUTE), 
                        chat_history_messages=chat_history_messages)
        self._actions: list[Action] = []

    def _parse_and_handle_event(self, event: Event) -> None:
        """Parse and handle event."""
        if event.judge_type("Nlp", "ActionDescriptionDynamicExecute"):
            payload = Nlp.ActionDescriptionDynamicExecute(**json.loads(event.payload))
            logger.info(
                "[%s] start to process Nlp action description dynamic execute event: %s",
                self._request_id, payload
            )
            self._handle_nlp_action_description_dynamic_execute(payload)
        else:
            raise ValueError(f"Unsupported event: {event.header.namespace}.{event.header.name}")


    def _handle_nlp_action_description_dynamic_execute(self, payload: Nlp.ActionDescriptionDynamicExecute) -> None:
        """Handle Nlp action description dynamic execute."""
        action_descriptions = payload.action_descriptions
        if not action_descriptions:
            logger.warning("[%s] Action descriptions not found, skip dynamic execute", self._request_id)
            self._send_instruction(Dialog.Exception(message="Action descriptions is empty"))
            self._send_dialog_finish(False)
            return

        query = PromptConfig.get_action_description_dynamic_execute_prompt(
            UserLanguage(self._language), action_descriptions
        )
        self._set_tools_meta(payload.mcp_list, exclude_tool_names=["create_rule"])

        self._chat_companion.set_chat_data(
            self._request_id,
            ChatCachedData(
                other_mcp_tools_meta=self._other_mcp_tools_meta,
                camera_ids=payload.camera_ids,
                mcp_ids=payload.mcp_list,
            ))

        asyncio.create_task(self._run_chat(query))


    def _post_process_tool_call(
            self, client_id: str, mcp_server_name: str,
            tool_name: str, parameters: dict, result: CallToolResult) -> None:
        """Post process tool call."""
        if not result.success:
            logger.error("[%s] Tool call failed: %s", self._request_id, result.error_message)
            return

        if client_id == LocalMcpClientId.LOCAL_DEFAULT:
            # local default mcp server, no need to add action
            return

        introduction = f"call {mcp_server_name} {tool_name}"

        action = Action(
            mcp_client_id=client_id,
            mcp_server_name=mcp_server_name,
            mcp_tool_name=tool_name,
            mcp_tool_input=parameters,
            introduction=introduction,
        )

        self._actions.append(action)


    async def _run_finally_do(self, success: bool, error_message: str | None) -> None:
        """Run finally do."""
        if not success:
            self._send_instruction(Dialog.Exception(message=error_message))
        else:
            self._send_instruction(Confirmation.AiGeneratedActions(actions=self._actions))
        self._send_dialog_finish(success)
