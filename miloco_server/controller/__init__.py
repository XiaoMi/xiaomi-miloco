# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Controller module for the Miloco project.
Contains all API route controllers for different services.
"""

from miloco_server.controller.web_controller import router as web_router
from miloco_server.controller.auth_controller import router as auth_router
from miloco_server.controller.miot_controller import router as miot_router
from miloco_server.controller.ha_controller import router as ha_router
from miloco_server.controller.chat_controller import router as chat_router
from miloco_server.controller.trigger_controller import router as trigger_router
from miloco_server.controller.model_controller import router as model_router
from miloco_server.controller.mcp_controller import router as mcp_router
from miloco_server.controller.rtsp_camera_controller import router as rtsp_camera_router

__all__ = [
    "web_router",
    "auth_router",
    "miot_router",
    "ha_router",
    "chat_router",
    "trigger_router",
    "model_router",
    "mcp_router",
    "rtsp_camera_router",
]
