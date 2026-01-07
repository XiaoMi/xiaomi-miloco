# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Normal configuration module
Unified management of project paths and configuration information
Load configuration from server_config.yaml
"""

import os
from pathlib import Path
from .config_loader import load_yaml_config, get_project_root

# Get project root directory
PROJECT_ROOT = get_project_root()

# Configuration file path
CONFIG_FILE = PROJECT_ROOT.parent / "config" / "server_config.yaml"

# Load configuration
_config = load_yaml_config(CONFIG_FILE)

# Directory path configuration
TEMPLATES_DIR = PROJECT_ROOT / _config["directories"]["templates"]
STATIC_DIR = PROJECT_ROOT / _config["directories"]["static"]

# Storage directory with environment variable override support
# Allows both relative and absolute paths
def _get_storage_dir() -> Path:
    """Get storage directory path with environment variable override support."""
    storage_config_path = _config["directories"]["storage"]
    if os.getenv("MILOCO_SERVER_STORAGE_DIR"):
        # Use absolute path from environment variable
        return Path(os.getenv("MILOCO_SERVER_STORAGE_DIR"))
    else:
        # Use relative path from config
        return PROJECT_ROOT / storage_config_path

STORAGE_DIR = _get_storage_dir()

IMAGE_DIR = STORAGE_DIR / "images"
MIOT_CACHE_DIR = STORAGE_DIR / "miot_cache"
CERT_DIR = STORAGE_DIR / "cert"
LOG_DIR = STORAGE_DIR / "log"

# Database configuration
DATABASE_CONFIG = {
    "path": STORAGE_DIR / _config["database"]["path"],
    "timeout": _config["database"]["timeout"],
    "check_same_thread": _config["database"]["check_same_thread"],
    "isolation_level": _config["database"]["isolation_level"],
}

# Server configuration
SERVER_CONFIG = {
    "host": os.getenv("BACKEND_HOST", None) or _config["server"]["host"],
    "port": int(os.getenv("BACKEND_PORT", None) or _config["server"]["port"]),
    "log_level": os.getenv("BACKEND_LOG_LEVEL", None) or _config["server"]["log_level"],
    "enable_console_logging": _config["server"]["enable_console_logging"],
    "enable_file_logging": _config["server"]["enable_file_logging"],
    "ssl_certfile": CERT_DIR / "cert.pem",
    "ssl_keyfile": CERT_DIR / "key.pem",
}

# Application information configuration
APP_CONFIG = {
    "title": _config["app"]["title"],
    "service_name": _config["app"]["service_name"],
    "description": _config["app"]["description"],
    "version": _config["app"]["version"]
}

# JWT authentication configuration
JWT_CONFIG = {
    "secret_key": os.getenv("SECRET_KEY", _config["jwt"]["secret_key"]),
    "algorithm": _config["jwt"]["algorithm"],
    "access_token_expire_minutes": _config["jwt"]["access_token_expire_minutes"],
}

# Local model configuration
LOCAL_MODEL_CONFIG = {
    "host": os.getenv("AI_ENGINE_HOST", None) or _config["local_model"]["host"],
    "port": int(os.getenv("AI_ENGINE_PORT", None) or _config["local_model"]["port"])
}

# Chat configuration
CHAT_CONFIG = {
    "agent_max_steps": _config["chat"]["agent_max_steps"],
    "vision_use_img_count": _config["chat"]["vision_use_img_count"],
    "chat_history_ttl": _config["chat"]["chat_history_ttl"],
}

# Trigger rule runner configuration
TRIGGER_RULE_RUNNER_CONFIG = {
    "interval_seconds": _config["trigger_rule_runner"]["interval_seconds"],
    "vision_use_img_count": _config["trigger_rule_runner"]["vision_use_img_count"],
    "trigger_rule_log_ttl": _config["trigger_rule_runner"]["trigger_rule_log_ttl"],
}

# Camera configuration
CAMERA_CONFIG = {
    "frame_interval": _config["camera"]["frame_interval"],
    "camera_img_cache_max_size": max(
        TRIGGER_RULE_RUNNER_CONFIG["vision_use_img_count"],
        CHAT_CONFIG["vision_use_img_count"]
    ),
}

# RTSP server configuration
RTSP_SERVER_CONFIG = _config["camera"]["rtsp_server"]

# RTSP camera configuration
RTSP_CAMERA_CONFIG = _config.get("rtsp_cameras", [])

# MIoT dynamic configuration
MIOT_CONFIG = {
    "cloud_server": _config["miot"]["cloud_server"],
}
