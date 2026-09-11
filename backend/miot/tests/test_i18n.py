# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unit test for miot_network.py.
"""

import asyncio
import logging

import pytest
from miot.i18n import MIoTI18n

_LOGGER = logging.getLogger(__name__)

# 无真实网络/凭据/设备依赖 —— CI 的 `miot 单元测试` 步骤按 `-m unit` 选择。
pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_translate_async():
    """Test network monitor loop."""
    i18n = MIoTI18n(lang="zh-Hans", loop=asyncio.get_running_loop())
    await i18n.init_async()
    # test get dict
    mcp_info = await i18n.translate_async(domain="mcp", key="miot_manual_scenes")
    _LOGGER.debug("mcp.miot_manual_scenes, %s", mcp_info)
    # test get value
    mcp_info = await i18n.translate_async(domain="mcp", key="miot_devices.name")
    _LOGGER.debug("mcp.miot_devices, %s", mcp_info)
    # test get value with replace
    mcp_content = await i18n.translate_async(
        domain="mcp",
        key="miot_devices.prompts.prompt_send_ctrl_rpc.content",
        replace={
            "tool_name_get_devices": "获取设备列表",
            "tool_name_get_device_spec": "获取设备SPEC定义",
            "tool_name_send_ctrl_rpc": "给设备发送控制RPC",
        },
    )
    _LOGGER.debug("miot_devices.prompts.prompt_send_ctrl_rpc.content, %s", mcp_content)

    await i18n.deinit_async()
