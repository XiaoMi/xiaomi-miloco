# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Rule creation tool module for creating and managing automation rules."""

import asyncio
import logging
from typing import List, Optional

from miloco_server.schema.mcp_schema import MCPClientStatus, LocalMcpClientId, choose_mcp_list

from miloco_server.utils.llm_utils.action_converter import ActionDescriptionConverter, ConverterResult
from miloco_server.utils.llm_utils.device_chooser import DeviceChooser

from miloco_server import actor_system
from miloco_server.schema.chat_schema import Confirmation, Dialog, Event, InstructionPayload, Internal
from miloco_server.schema.miot_schema import CameraInfo
from miloco_server.schema.trigger_schema import Action, Notify, TriggerRule, TriggerRuleDetail, ExecuteInfo, ExecuteType, ExecuteInfoDetail
from pydantic.dataclasses import dataclass
from thespian.actors import Actor, ActorAddress, ActorExitRequest

logger = logging.getLogger(__name__)


@dataclass
class RuleCreateMessage:
    name: str
    condition: str
    action_descriptions: List[str]
    location: Optional[str]
    notify: Optional[str]


class RuleCreateTool(Actor):
    """Actor for creating and managing automation rules."""
    def __init__(
        self,
        request_id: str,
        out_actor_address: ActorAddress,
        camera_ids: Optional[List[str]] = None,
        mcp_ids: Optional[List[str]] = None,
    ):
        super().__init__()
        from miloco_server.service.manager import get_manager # pylint: disable=import-outside-toplevel
        self._manager = get_manager()
        self._request_id = request_id
        self._default_preset_action_manager = self._manager.default_preset_action_manager
        self._out_actor_address = out_actor_address
        self._future = None
        self._camera_ids = camera_ids
        self._mcp_ids = mcp_ids
        logger.info("[%s] RuleCreateTool actor initialized", self._request_id)

    def receiveMessage(self, msg, sender):
        """Main method for receiving messages"""
        if isinstance(msg, RuleCreateMessage):
            self._future = asyncio.Future()
            self.send(sender, self._future)
            self._handle_create_rule(msg)
        elif isinstance(msg, Event):
            self._handle_event(msg)
        elif isinstance(msg, ActorExitRequest):
            self._handle_exit_request()
        else:
            logger.warning("[%s] Unknown message format: %s", self._request_id, type(msg))

    def _handle_create_rule(self, message: RuleCreateMessage):
        """Handle rule creation message"""
        asyncio.create_task(
            self._run_create_rule(
                message.name,
                message.condition,
                message.action_descriptions,
                message.location,
                message.notify))

    def _handle_event(self, event: Event):
        """Handle events"""
        try:
            if event.judge_type("Confirmation", "SaveRuleConfirmResult"):
                save_rule_confirm_result = Confirmation.SaveRuleConfirmResult.model_validate_json(event.payload)
                self._handle_save_rule_confirm_result(save_rule_confirm_result)
            else:
                raise ValueError(f"Invalid event: {event.header.namespace}.{event.header.name}")

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("[%s] Error occurred while handling event: %s", self._request_id, str(e))
            self._future.set_result({"error": str(e)})

    async def _run_create_rule(
            self,
            name: str,
            condition: str,
            action_descriptions: list[str],
            location: Optional[str] = None,
            notify: Optional[str] = None) -> str:
        """Run agent to process user query"""
        logger.info(
            "[%s] Starting to process rule create: name: %s, condition: %s, action_description: %s, location: %s, notify: %s",  # pylint: disable=line-too-long
            self._request_id,
            name,
            condition,
            action_descriptions,
            location,
            notify)
        if not notify:
            notify = name

        try:
            chosen_camera_infos, all_camera_infos = await self._choose_camera(location)
            if not chosen_camera_infos:
                chosen_camera_infos = all_camera_infos

            default_actions = await self._default_preset_action_manager.get_all_default_actions(self._mcp_ids)
            miot_scene_actions = default_actions.get(LocalMcpClientId.MIOT_MANUAL_SCENES, {})
            ha_automation_actions = default_actions.get(LocalMcpClientId.HA_AUTOMATIONS, {})

            no_matched_action_descriptions, matched_actions = (
                await self._action_descriptions_to_preset_actions(
                    action_descriptions, miot_scene_actions, ha_automation_actions))

            execute_info = ExecuteInfo(
                ai_recommend_execute_type=ExecuteType.DYNAMIC,
                ai_recommend_action_descriptions=no_matched_action_descriptions,
                automation_actions=matched_actions,
                notify=Notify(content=notify)
            )

            choosed_mcp_list = await self._choose_mcp_list()
            trigger_rule_detail = TriggerRuleDetail(
                name=name,
                cameras=chosen_camera_infos,
                condition=condition,
                execute_info=ExecuteInfoDetail.from_execute_info(
                    execute_info, choosed_mcp_list),
                enabled=True
            )

            save_rule_confirm = Confirmation.SaveRuleConfirm(
                rule=trigger_rule_detail,
                camera_options=all_camera_infos,
                action_options=[
                    action
                    for actions in default_actions.values()
                    for action in actions.values()
                ]
            )

            dispatcher_message = Internal.Dispatcher(next_event_handler=self.myAddress)
            self._send_instruction(dispatcher_message)
            self._send_instruction(save_rule_confirm)

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("[%s] Error occurred during agent execution: %s", self._request_id, str(e), exc_info=True)
            self._send_instruction(Dialog.Exception(message=f"Error occurred during execution: {str(e)}"))
            self._future.set_result({"error": str(e)})


    async def _choose_mcp_list(self) -> list[MCPClientStatus]:
        """Get MCP list"""
        all_mcp_status = await self._manager.mcp_service.get_all_mcp_clients_status()
        return choose_mcp_list(self._mcp_ids, all_mcp_status.clients)

    async def _action_descriptions_to_preset_actions(
            self, action_descriptions: List[str],
            miot_scene_actions: dict[str, Action],
            ha_automation_actions: dict[str, Action]):
        """Convert action descriptions to actions"""

        miot_scene_converter = ActionDescriptionConverter(
            self._request_id, action_descriptions, miot_scene_actions)
        ha_automation_converter = ActionDescriptionConverter(
            self._request_id, action_descriptions, ha_automation_actions)
        task = []
        task.append(miot_scene_converter.run())
        task.append(ha_automation_converter.run())
        results = await asyncio.gather(*task)

        miot_scene_results: list[ConverterResult] = results[0]
        ha_automation_results: list[ConverterResult] = results[1]

        no_matched_action_descriptions = []
        matched_actions = []
        if (len(miot_scene_results) == len(action_descriptions) and
                len(ha_automation_results) == len(action_descriptions)):
            for action_description, miot_scene_result, ha_automation_result in zip(
                    action_descriptions, miot_scene_results, ha_automation_results):
                if not miot_scene_result.is_inside and not ha_automation_result.is_inside:
                    no_matched_action_descriptions.append(action_description)
                    continue

                if miot_scene_result.is_inside:
                    matched_actions.append(miot_scene_result.action)
                if ha_automation_result.is_inside:
                    matched_actions.append(ha_automation_result.action)
        else:
            logger.warning(
                "[%s] Action descriptions to preset actions failed: %s, %s",
                self._request_id, miot_scene_results, ha_automation_results)
            for miot_scene_result in miot_scene_results:
                if not miot_scene_result.is_inside:
                    no_matched_action_descriptions.append(miot_scene_result.action_description)
                else:
                    matched_actions.append(miot_scene_result.action)

            for ha_automation_result in ha_automation_results:
                if not ha_automation_result.is_inside:
                    no_matched_action_descriptions.append(ha_automation_result.action_description)
                else:
                    matched_actions.append(ha_automation_result.action)

        return no_matched_action_descriptions, matched_actions


    async def _choose_camera(self, location: Optional[str] = None) -> tuple[List[CameraInfo], List[CameraInfo]]:
        """Choose camera"""
        device_chooser = DeviceChooser(
            request_id=self._request_id,
            location=location,
            choose_camera_device_ids=self._camera_ids)
        return await device_chooser.run()

    def _handle_save_rule_confirm_result(self, save_rule_confirm_result: Confirmation.SaveRuleConfirmResult):
        """Handle save rule confirmation result"""
        logger.info("[%s] Received save rule confirm result: %s", self._request_id, save_rule_confirm_result)

        if save_rule_confirm_result.confirmed and save_rule_confirm_result.rule is not None:
            asyncio.create_task(self._create_rule_and_respond(save_rule_confirm_result.rule))
        else:
            self._future.set_result({"content": "User refused to save this rule"})

    async def _create_rule_and_respond(self, rule: TriggerRule):
        """Asynchronously create rule and respond with result"""
        try:
            rule_id = await self._manager.trigger_rule_service.create_trigger_rule(rule)
            if rule_id:
                self._future.set_result(
                    {"content": self._simplify_rule_introduction(rule)})
            else:
                self._future.set_result({"error": "Failed to create trigger rule"})
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("[%s] Error creating trigger rule: %s", self._request_id, str(e))
            self._future.set_result({"error": str(e)})

    def _simplify_rule_introduction(self, rule: TriggerRule) -> str:
        """Simplify rule introduction"""
        action_introductions = []
        if rule.execute_info.ai_recommend_action_descriptions:
            action_introductions.append(rule.execute_info.ai_recommend_action_descriptions)
        if rule.execute_info.automation_actions:
            action_introductions.append([action.introduction for action in rule.execute_info.automation_actions])
        if rule.execute_info.notify:
            action_introductions.append(f"notify: {rule.execute_info.notify.content}")

        return (
            f"User modified rule created successfully, finally rule name: {rule.name}, "
            f"condition: {rule.condition}, action_introductions: {action_introductions}"
        )

    def _send_instruction(self, instruction_payload: InstructionPayload):
        actor_system.tell(self._out_actor_address, instruction_payload)

    def _handle_exit_request(self):
        """Handle Actor exit request"""
        logger.info("[%s] RuleCreateTool handling exit request", self._request_id)

    def _match_action_with_preset(
            self,
            generated_action: Action,
            miot_scene_actions: dict[str, Action],
            ha_automation_actions: dict[str, Action]) -> Action:
        """
        Map model-generated action to preset action

        Args:
            generated_action: Model-generated action
            miot_scene_actions: MIoT scene actions
            ha_automation_actions: Home Assistant automation actions

        Returns:
            Action: Mapped action, if can map to preset action then use preset description, otherwise mark as
        """
        try:
            # Match MIoT scene actions
            if (generated_action.mcp_client_id == LocalMcpClientId.MIOT_MANUAL_SCENES and
                generated_action.mcp_tool_name == "trigger_manual_scene" and
                generated_action.mcp_tool_input and
                    generated_action.mcp_tool_input.get("scene_id")):

                scene_id = generated_action.mcp_tool_input.get("scene_id")
                preset_action = miot_scene_actions.get(scene_id)
                if preset_action:
                    logger.info(
                        "[%s] Action mapped to miot scene preset: %s",
                        self._request_id,
                        preset_action.introduction)
                    return preset_action

            # Match Home Assistant automation actions
            elif (generated_action.mcp_client_id == LocalMcpClientId.HA_AUTOMATIONS and
                  generated_action.mcp_tool_name == "trigger_automation" and
                  generated_action.mcp_tool_input and
                  generated_action.mcp_tool_input.get("automation_id")):

                automation_id = generated_action.mcp_tool_input.get("automation_id")
                preset_action = ha_automation_actions.get(automation_id)
                if preset_action:
                    logger.info("[%s] Action mapped to HA automation preset: %s",
                                self._request_id, preset_action.introduction)
                    return preset_action

            # No preset action matched
            logger.info("[%s] Action not mapped to preset: %s", self._request_id, generated_action.introduction)
            return generated_action

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("[%s] Error in action mapping: %s", self._request_id, str(e))
            return generated_action
