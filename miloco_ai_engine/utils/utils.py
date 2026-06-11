# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Utility functions module
Contains general-purpose helper functions
"""
import uuid
import time
import json
import re
import platform
from miloco_ai_engine.config import config
from typing import Any, Dict, List, Optional
from datetime import datetime
import os
import sys
import psutil


def get_uvicorn_log_config(enable_file_logging: Optional[bool] = None, enable_console_logging: Optional[bool] = None):
    """
    Get uvicorn logging configuration

    Args:
        enable_file_logging: Whether to enable file logging, reads from config if None
        enable_console_logging: Whether to enable console logging, reads from config if None
    """
    # Get configuration parameters
    log_level = config.LOGGING_CONFIG["log_level"].upper()
    file_logging = config.LOGGING_CONFIG.get(
        "enable_file_logging", True) if enable_file_logging is None else enable_file_logging
    console_logging = config.LOGGING_CONFIG.get(
        "enable_console_logging", True) if enable_console_logging is None else enable_console_logging

    # Build handlers
    handlers = {}
    handler_list = []

    if console_logging:
        handlers["console"] = {
            "class": "logging.StreamHandler", "formatter": "default", "level": log_level}
        handler_list.append("console")

    if file_logging:
        log_dir = config.LOG_PATH
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        config.LOG_FILE_NAME = os.path.join(log_dir, f"miloco_ai_engine_{timestamp}.log")

        handlers["file"] = {
            "class": "logging.FileHandler",
            "filename": config.LOG_FILE_NAME,
            "mode": "a",
            "formatter": "default",
            "level": log_level,
            "encoding": "utf-8"
        }
        handler_list.append("file")

    # Basic logging configuration
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s", "datefmt": "%Y-%m-%d %H:%M:%S"}},
        "handlers": handlers,
        "root": {"level": log_level, "handlers": handler_list},
        "loggers": {
            "uvicorn": {"level": log_level, "handlers": handler_list, "propagate": False},
            "uvicorn.access": {"level": log_level, "handlers": handler_list, "propagate": False},
            "uvicorn.error": {"level": log_level, "handlers": handler_list, "propagate": False}
        }
    }


def generate_id() -> str:
    """Generate unique ID"""
    return f"chatcmpl-{uuid.uuid4().hex[:8]}"


def format_timestamp(timestamp: Optional[float] = None) -> str:
    """Format timestamp"""
    if timestamp is None:
        timestamp = time.time()
    return datetime.fromtimestamp(timestamp).isoformat()


def safe_json_dumps(obj: Any) -> str:
    """Safe JSON serialization"""
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(obj), ensure_ascii=False)


def parse_json_safely(text: str) -> Optional[Dict[str, Any]]:
    """Safely parse JSON"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def truncate_text(text: str, max_length: int = 100) -> str:
    """Truncate text"""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def count_tokens(text: str) -> int:
    """Simple token count estimation (split by space)"""
    return len(text.split())


def sanitize_filename(filename: str) -> str:
    """Sanitize filename"""
    # Remove or replace unsafe characters
    filename = re.sub(r"[<>:\"/\\|?*]", "_", filename)
    # Limit length
    if len(filename) > 255:
        filename = filename[:255]
    return filename


def format_bytes(bytes_value: int) -> str:
    """Format bytes"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_value < 1024.0:
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.1f} PB"


def format_duration(seconds: float) -> str:
    """Format duration"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}min"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}h"


def merge_dicts(dict1: Dict[str, Any], dict2: Dict[str, Any]) -> Dict[str, Any]:
    """Merge dictionaries, dict2 values will override dict1 values"""
    result = dict1.copy()
    result.update(dict2)
    return result


def flatten_list(nested_list: List[Any]) -> List[Any]:
    """Flatten nested list"""
    result = []
    for item in nested_list:
        if isinstance(item, list):
            result.extend(flatten_list(item))
        else:
            result.append(item)
    return result


def chunk_list(lst: List[Any], chunk_size: int) -> List[List[Any]]:
    """Split list into chunks"""
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]


def validate_model_path(model_path: str) -> bool:
    """Validate model path"""
    return os.path.exists(model_path) and os.path.isfile(model_path)


def get_file_size(file_path: str) -> Optional[int]:
    """Get file size"""
    try:
        return os.path.getsize(file_path)
    except OSError:
        return None


def create_directory_if_not_exists(directory: str) -> bool:
    """Create directory if it doesn't exist"""
    try:
        os.makedirs(directory, exist_ok=True)
        return True
    except OSError:
        return False


def is_valid_port(port: int) -> bool:
    """Validate port number"""
    return 1 <= port <= 65535


def is_valid_host(host: str) -> bool:
    """Validate host address"""
    # Simple IP address validation
    ip_pattern = r"^(\d{1,3}\.){3}\d{1,3}$"
    if re.match(ip_pattern, host):
        parts = host.split(".")
        return all(0 <= int(part) <= 255 for part in parts)
    return host in ["localhost", "127.0.0.1", "0.0.0.0"]


def get_system_info() -> Dict[str, Any]:
    """Get system information"""
    info = {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "architecture": platform.architecture()[0],
        "processor": platform.processor(),
        "cpu_count": psutil.cpu_count(),
        "memory_total": psutil.virtual_memory().total,
        "memory_available": psutil.virtual_memory().available,
    }

    return info


def is_wsl():
    try:
        # Check /proc/version file for WSL indicators
        with open("/proc/version", "r", encoding="utf-8") as f:
            version_info = f.read().lower()
            if "microsoft" in version_info or "wsl" in version_info:
                return True
    except (FileNotFoundError, PermissionError):
        print("⚠️ Failed to check WSL environment")
    return False


def is_linux():
    return sys.platform.startswith("linux")


def is_macos():
    return sys.platform == "darwin"
