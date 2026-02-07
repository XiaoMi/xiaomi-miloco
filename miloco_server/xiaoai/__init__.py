# Copyright (C) 2025 willianfu
# XiaoAI Speaker Integration Module for Miloco Server
#
# This module provides integration with Xiaomi XiaoAI speakers,
# allowing voice-controlled AI conversations using Miloco's AI backend.

"""
XiaoAI Speaker Integration Module

This module implements the server-side logic for connecting to Xiaomi XiaoAI speakers
(via open-xiaoai client patch) and provides AI conversation capabilities using
Miloco's existing AI infrastructure (ChatAgent, MCP tools, cameras).

Key Features:
- Multiple speaker support with independent sessions
- Context compression for long conversations
- Voice commands for session management
- Integration with Miloco's AI backend (MCP, cameras, memory)
- Configurable through web UI

Usage:
    from miloco_server.xiaoai import XiaoAIService, get_xiaoai_config
    
    service = XiaoAIService()
    await service.start()
"""

from miloco_server.xiaoai.service import (
    XiaoAIService,
    get_xiaoai_service,
    start_xiaoai_service_if_enabled,
    restart_xiaoai_service,
)
from miloco_server.xiaoai.config import (
    XiaoAIConfig,
    get_xiaoai_config,
    update_xiaoai_config,
    reload_xiaoai_config,
    SessionCommand,
    ContextCompressionConfig,
)

__all__ = [
    "XiaoAIService",
    "XiaoAIConfig",
    "get_xiaoai_service",
    "get_xiaoai_config",
    "update_xiaoai_config",
    "reload_xiaoai_config",
    "start_xiaoai_service_if_enabled",
    "restart_xiaoai_service",
    "SessionCommand",
    "ContextCompressionConfig",
]
__version__ = "2.0.0"
__author__ = "willianfu"
