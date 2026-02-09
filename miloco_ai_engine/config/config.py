# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
AI Engine configuration module
Unified management of project paths and configuration information
Load configuration from ai_engine_config.yaml
"""
import os
from miloco_ai_engine.config.config_loader import load_yaml_config, get_project_root

# Get project root directory
PROJECT_ROOT = get_project_root()

# Configuration file path
CONFIG_FILE = PROJECT_ROOT.parent / "config" / "ai_engine_config.yaml"

# Load configuration
_config = load_yaml_config(CONFIG_FILE)

# Log directory path
LOG_PATH = PROJECT_ROOT.parent / ".log" / "ai_engine"
# Will update to miloco_ai_engine_timestamp.log
LOG_FILE_NAME = LOG_PATH / "miloco_ai_engine.log"

# Logging configuration
LOGGING_CONFIG = {
    "log_level": os.getenv("AI_ENGINE_LOG_LEVEL", None) or _config["logging"]["log_level"],
    "enable_console_logging": _config["logging"]["enable_console_logging"],
    "enable_file_logging": _config["logging"]["enable_file_logging"],
}

# Server configuration
SERVER_CONFIG = {
    "host": os.getenv("AI_ENGINE_HOST", None) or _config["server"]["host"],
    "port": int(os.getenv("AI_ENGINE_PORT", None) or _config["server"]["port"])
}

# Application information configuration
APP_CONFIG = {
    "title": _config["app"]["title"],
    "service_name": _config["app"]["service_name"],
    "description": _config["app"]["description"],
    "version": _config["app"]["version"]
}

# Models configuration
MODELS_CONFIG = _config["models"]

# Concurrency control
SERVER_CONCURRENCY = {
    "max_queue_size": _config["server_concurrency"]["max_queue_size"],
    "abandon_low_priority": _config["server_concurrency"]["abandon_low_priority"],
    "queue_wait_timeout": _config["server_concurrency"]["queue_wait_timeout"]
}

# Automatic optimization model configuration by vram in loading
AUTO_OPT_VRAM = _config["auto_opt_vram"]
