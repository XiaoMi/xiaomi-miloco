# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Prompt configuration file
Unified management of system prompts in different languages
Load configuration from prompt_config.yaml
"""

from enum import Enum
from typing import List
from miloco_server.config.config_loader import load_yaml_config, get_project_root


class UserLanguage(str, Enum):
    """User language enumeration - avoid circular imports"""
    CHINESE = "zh"
    ENGLISH = "en"


class PromptType(str, Enum):
    """Prompt type enumeration"""
    CHAT = "chat"                           # Chat conversation prompts
    TRIGGER_RULE_CONDITION = "trigger_rule_condition"  # Trigger rule condition prompts
    VISION_UNDERSTANDING = "vision_understanding"  # Vision understanding prompts
    ACTION_DESCRIPTION_DYNAMIC_EXECUTE = "action_description_dynamic_execute"  # dynamic execute prompts

# Get project root directory
PROJECT_ROOT = get_project_root()

# Load server config for template variables (avoid circular import by loading directly)
SERVER_CONFIG_FILE = PROJECT_ROOT.parent / "config" / "server_config.yaml"
_server_config = load_yaml_config(SERVER_CONFIG_FILE)

# Get configuration values for template variables
CAMERA_IMG_FRAME_INTERVAL = _server_config["camera"]["frame_interval"]
TRIGGER_RULE_RUNNER_VISION_USE_IMG_COUNT = _server_config["trigger_rule_runner"]["vision_use_img_count"]
CHAT_VISION_USE_IMG_COUNT = _server_config["chat"]["vision_use_img_count"]

# Load prompt configuration
PROMPT_CONFIG_FILE = PROJECT_ROOT.parent / "config" / "prompt_config.yaml"
_config = load_yaml_config(PROMPT_CONFIG_FILE)

# Priority range constants
PRIORITY_MIN = 0
PRIORITY_MAX = 100


def _validate_priority(value: int, prompt_type: str) -> int:
    """Validate priority value is within valid range"""
    if not PRIORITY_MIN <= value <= PRIORITY_MAX:
        raise ValueError(
            f"Priority for '{prompt_type}' must be between {PRIORITY_MIN} and {PRIORITY_MAX}, got {value}"
        )
    return value


def _format_vision_system_prompt(prompt_text: str) -> str:
    """Format prompt text, replace template variables"""
    return prompt_text.format(
        frame_interval=CAMERA_IMG_FRAME_INTERVAL,
        vision_use_img_count=TRIGGER_RULE_RUNNER_VISION_USE_IMG_COUNT
    )

class PromptConfig:
    """Prompt configuration class - supports multiple types of prompts"""
    # Task priorities (validated on load)
    PRIORITIES = {
        PromptType.CHAT: _validate_priority(
            _config["prompts"]["chat"].get("priority", 0), "chat"
        ),
        PromptType.TRIGGER_RULE_CONDITION: _validate_priority(
            _config["prompts"]["trigger_rule_condition"].get("priority", 0), "trigger_rule_condition"
        ),
        PromptType.VISION_UNDERSTANDING: _validate_priority(
            _config["prompts"]["vision_understanding"].get("priority", 0), "vision_understanding"
        ),
        PromptType.ACTION_DESCRIPTION_DYNAMIC_EXECUTE: _validate_priority(
            _config["prompts"]["action_description_dynamic_execute"].get("priority", 0),
            "action_description_dynamic_execute"
        ),
    }

    # Chat conversation prompts
    CHAT_PROMPTS = {
        UserLanguage.CHINESE: _format_vision_system_prompt(_config["prompts"]["chat"]["chinese"]),
        UserLanguage.ENGLISH: _format_vision_system_prompt(_config["prompts"]["chat"]["english"])
    }

    # Trigger rule condition prompts
    TRIGGER_RULE_CONDITION_PROMPTS = {
        UserLanguage.CHINESE: _format_vision_system_prompt(_config["prompts"]["trigger_rule_condition"]["chinese"]),
        UserLanguage.ENGLISH: _format_vision_system_prompt(_config["prompts"]["trigger_rule_condition"]["english"])
    }

    # Vision understanding prompts
    VISION_UNDERSTANDING_PROMPTS = {
        UserLanguage.CHINESE: _format_vision_system_prompt(_config["prompts"]["vision_understanding"]["chinese"]),
        UserLanguage.ENGLISH: _format_vision_system_prompt(_config["prompts"]["vision_understanding"]["english"])
    }

    # Vision understanding UI text prefixes
    VISION_UNDERSTANDING_PREFIXES = {
        UserLanguage.CHINESE: _config["prompts"]["vision_understanding_prefixes"]["chinese"],
        UserLanguage.ENGLISH: _config["prompts"]["vision_understanding_prefixes"]["english"]
    }

    # Trigger rule condition UI text prefixes
    TRIGGER_RULE_CONDITION_PREFIXES = {
        UserLanguage.CHINESE: _config["prompts"]["trigger_rule_condition_prefixes"]["chinese"],
        UserLanguage.ENGLISH: _config["prompts"]["trigger_rule_condition_prefixes"]["english"]
    }

    # Action description dynamic execute prompts (template format)
    ACTION_DESCRIPTION_DYNAMIC_EXECUTE_PROMPTS = {
        UserLanguage.CHINESE: _config["prompts"]["action_description_dynamic_execute"]["chinese"],
        UserLanguage.ENGLISH: _config["prompts"]["action_description_dynamic_execute"]["english"]
    }

    # Unified mapping of all prompts
    ALL_PROMPTS = {
        PromptType.CHAT: CHAT_PROMPTS,
        PromptType.TRIGGER_RULE_CONDITION: TRIGGER_RULE_CONDITION_PROMPTS,
        PromptType.VISION_UNDERSTANDING: VISION_UNDERSTANDING_PROMPTS,
        PromptType.ACTION_DESCRIPTION_DYNAMIC_EXECUTE: ACTION_DESCRIPTION_DYNAMIC_EXECUTE_PROMPTS,
    }

    @classmethod
    def get_priority(cls, prompt_type: PromptType) -> int:
        """
        Get task priority for a prompt type

        Args:
            prompt_type: Prompt type

        Returns:
            Priority value (0-100)
        """
        return cls.PRIORITIES.get(prompt_type, 0)

    @classmethod
    def get_prompt(cls, prompt_type: PromptType, language: UserLanguage) -> str:
        """
        Get prompt based on type and language

        Args:
            prompt_type: Prompt type
            language: User language, defaults to Chinese

        Returns:
            Prompt corresponding to the type and language
        """
        prompts_dict = cls.ALL_PROMPTS.get(prompt_type, cls.CHAT_PROMPTS)
        return prompts_dict.get(language, prompts_dict[UserLanguage.CHINESE])

    @classmethod
    def get_system_prompt(cls, prompt_type: PromptType, language: UserLanguage) -> str:
        """
        Get prompt (backward compatibility)
        
        Args:
            prompt_type: Prompt type
            language: User language, defaults to Chinese
            
        Returns:
            Prompt
        """
        return cls.get_prompt(prompt_type, language)

    @classmethod
    def get_vision_understanding_prefixes(cls, language: UserLanguage) -> dict[str, str]:
        """
        Get vision understanding UI text prefixes based on language
        
        Args:
            language: User language, defaults to Chinese
            
        Returns:
            Dictionary containing user_content, camera_prefix, channel_prefix, sequence_prefix
        """
        return cls.VISION_UNDERSTANDING_PREFIXES.get(language, cls.VISION_UNDERSTANDING_PREFIXES[UserLanguage.CHINESE])

    @classmethod
    def get_trigger_rule_condition_prefixes(cls, language: UserLanguage) -> dict[str, str]:
        """
        Get trigger rule condition UI text prefixes based on language
        
        Args:
            language: User language, defaults to Chinese
            
        Returns:
            Dictionary containing image_sequence_prefix, condition_question_template
        """
        return cls.TRIGGER_RULE_CONDITION_PREFIXES.get(
            language, cls.TRIGGER_RULE_CONDITION_PREFIXES[UserLanguage.CHINESE]
        )

    @classmethod
    def get_action_description_dynamic_execute_prompt(
        cls, language: UserLanguage, action_descriptions: List[str]
    ) -> str:
        """
        Get action description dynamic execute prompt with formatted action descriptions
        
        Args:
            language: User language, defaults to Chinese
            action_descriptions: List of action descriptions to format into the prompt
            
        Returns:
            Formatted prompt string
        """
        template = cls.ACTION_DESCRIPTION_DYNAMIC_EXECUTE_PROMPTS.get(
            language, cls.ACTION_DESCRIPTION_DYNAMIC_EXECUTE_PROMPTS[UserLanguage.CHINESE]
        )
        # Format list as numbered list for better readability
        formatted_descriptions = "\n".join(f"{i+1}. {desc}" for i, desc in enumerate(action_descriptions))
        return template.format(action_descriptions=formatted_descriptions)
