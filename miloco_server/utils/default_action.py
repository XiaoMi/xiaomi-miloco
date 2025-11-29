# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Default action manager for preset actions.
Provides default actions including Mi Home scene list and Home Assistant automation list.
"""

import logging
from typing import Optional

from miloco_server.schema.trigger_schema import Action
from miloco_server.mcp.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)


class DefaultPresetActionManager:
    """
    Default action manager

    This manager provides some default actions, including:
    1. Get Mi Home scene list
    2. Get Home Assistant automation list
    """

    def __init__(self, tool_executor: ToolExecutor):
        self._tool_executor = tool_executor
        self._mcp_client_manager = tool_executor.mcp_client_manager

    async def get_miot_scene_actions(self, mcp_ids: Optional[list[str]] = None) -> dict[str, Action]:
        # Skip if user has not selected the MIoT manual scenes MCP
        if mcp_ids is not None and "miot_manual_scenes" not in mcp_ids:
            logger.info("Mi Home scene actions skipped: miot_manual_scenes not selected")
            return {}

        # Dynamically get miot client
        miot_client = self._mcp_client_manager.get_client("miot_manual_scenes")
        if miot_client is None:
            logger.error("Mi Home client not initialized or connection failed")
            return {}
        tool = miot_client.get_tool("trigger_manual_scene")
        if tool is None:
            logger.error("Mi Home scene tool not found")
            return {}
        try:
            result = await miot_client.call_tool("get_manual_scenes", {})
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to fetch Mi Home scenes: %s", e)
            return {}
        logger.info("get_miot_scene_actions: %s", result)
        scenes = result.get("result", [])

        actions_dict = {}
        for scene in scenes:
            scene_name = scene["scene_name"]
            scene_id = scene["scene_id"]

            action = Action(
                mcp_client_id="miot_manual_scenes",
                mcp_tool_name="trigger_manual_scene",
                mcp_tool_input={"scene_id": scene_id},
                mcp_server_name=miot_client.config.server_name,
                introduction=f"{scene_name}",
            )
            actions_dict[scene_id] = action

        logger.info("get_miot_scene_actions: %s", actions_dict)

        return actions_dict

    async def get_ha_automation_actions(self, mcp_ids: Optional[list[str]] = None) -> dict[str, Action]:
        # Skip if user has not selected the HA automations MCP
        if mcp_ids is not None and "ha_automations" not in mcp_ids:
            logger.info("Home Assistant automation actions skipped: ha_automations not selected")
            return {}

        # Dynamically get ha client
        ha_client = self._mcp_client_manager.get_client("ha_automations")
        if ha_client is None:
            logger.error("Home Assistant client not initialized or connection failed")
            return {}

        tool = ha_client.get_tool("trigger_automation")
        if tool is None:
            logger.error("Home Assistant automation tool not found")
            return {}
        try:
            result = await ha_client.call_tool("get_automations", {})
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to fetch Home Assistant automations: %s", e)
            return {}
        logger.info("get_ha_automation_actions: %s", result)

        actions_dict = {}
        # Fix: correctly extract automations list from result
        automations = result.get("result", [])
        for automation in automations:
            automation_scene_id = automation.get("automation_id")
            automation_name = automation.get("automation_name")

            action = Action(
                mcp_client_id="ha_automations",
                mcp_tool_name="trigger_automation",
                mcp_tool_input={"automation_id": automation_scene_id},
                mcp_server_name=ha_client.config.server_name,
                introduction=f"{automation_name}",
            )
            actions_dict[automation_scene_id] = action

        logger.info("get_ha_automation_actions: %s", actions_dict)

        return actions_dict
