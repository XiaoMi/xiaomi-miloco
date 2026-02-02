# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

import json
import logging
from typing import Optional

from miloco_server.utils.normal_util import extract_json_from_content

from miloco_server.utils.llm_utils.base_llm_util import BaseLLMUtil
from miloco_server.schema.trigger_schema import Action

logger = logging.getLogger(__name__)


class ConverterResult:
    """Result of action converter"""
    action_description: str
    is_inside: bool
    automation_id: Optional[str]
    action: Optional[Action]

    def __init__(self, action_description: str, is_inside: bool, automation_id: Optional[str], action: Optional[Action]):
        self.action_description = action_description
        self.is_inside = is_inside
        self.automation_id = automation_id
        self.action = action

class ActionDescriptionConverter(BaseLLMUtil):
    """Used to convert natural language of actions to Action structure language"""

    def __init__(
        self,
        request_id: str,
        action_descriptions: list[str],
        preset_actions: dict[str, Action],
    ):
        super().__init__(request_id = request_id, query = None, tools_meta = None)
        self._action_descriptions = action_descriptions
        self._preset_actions = preset_actions

    def _get_system_prompt(self) -> str:
        """Get system prompt"""
        return """
        You are a scenario selection assistant for a user. 
        I will first provide you with a list of automation scenario IDs and names for all users, 
        then a list of scenario names the user wants to select. 
        Help me select the scenario the user wants and return whether the desired scenario is inside (is_inside), 
        along with the corresponding automation scenario ID (automation_id). 
        The response content should be returned in JSON object format with a "results" field containing a list of objects. 
        Each object in the results array should have the following key values:
        1. action_description: the action description the user wants to select
        2. is_inside: whether the desired scenario is inside
        3. automation_id: the corresponding automation scenario ID. If the desired scenario is not inside, automation_id should be null.
        You can only return the results in JSON format, and the result should be an object with a "results" field containing a list of objects.

        Examples:
        All automation IDs and names: 
        {
            "automation_id_1": "read mode"
        }
        User wants to select: ["read", "sleep"]
        {
            "results": [
                {
                    "action_description": "read",
                    "is_inside": true,
                    "automation_id": "automation_id_1"
                },
                {
                    "action_description": "sleep",
                    "is_inside": false,
                    "automation_id": null
                }
            ]
        }
        """
    def _init_conversation(self) -> None:
        """Initialize conversation history"""
        self._chat_history.add_content("system", self._get_system_prompt())

        all_preset_actions = {}
        for automation_id, action in self._preset_actions.items():
            all_preset_actions[automation_id] = action.introduction

        self._chat_history.add_content("user", f"All automation IDs and names: {json.dumps(all_preset_actions)}")
        self._chat_history.add_content("user", f"User wants to select: {json.dumps(self._action_descriptions)}")

    async def run(self) -> list[ConverterResult]:
        try:
            if not self._preset_actions:
                return self._make_no_matched_converter_results()

            self._init_conversation()
            content, _, _ = await self._call_llm()
            json_content = extract_json_from_content(content)
            result = json.loads(json_content)
            if not result:
                raise ValueError(f"No JSON in LLM response: {content}")
            # Extract results array from the response object
            results_list = result.get("results", result if isinstance(result, list) else [])
            if not results_list:
                raise ValueError(f"No results in LLM response: {content}")
            return [ConverterResult(
                action_description=item["action_description"],
                is_inside=item["is_inside"],
                automation_id=item.get("automation_id"),
                action=self._preset_actions.get(item.get("automation_id"))
            ) for item in results_list]

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("[%s] Error occurred during agent execution: %s", self._request_id, str(e))
            return self._make_no_matched_converter_results()

    def _make_no_matched_converter_results(self) -> list[ConverterResult]:
        """Make no matched converter results"""
        return [ConverterResult(
            action_description=action_description,
            is_inside=False,
            automation_id=None,
            action=None
        ) for action_description in self._action_descriptions]