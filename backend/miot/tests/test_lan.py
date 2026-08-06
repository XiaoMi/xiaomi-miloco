# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MIoT Lan Test.
"""

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest
from miot.lan import MIoTLan
from miot.network import MIoTNetwork
from miot.types import InterfaceStatus, MIoTLanDeviceInfo, NetworkInfo

_LOGGER = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_network_monitor_loop_async():
    """Test network monitor loop."""
    miot_net = MIoTNetwork()

    async def on_network_status_changed(status: bool):
        _LOGGER.info("on_network_status_changed, %s", status)

    await miot_net.register_status_changed_async(
        key="test", handler=on_network_status_changed
    )

    async def on_network_info_changed(status: InterfaceStatus, info: NetworkInfo):
        _LOGGER.info("on_network_info_changed, %s, %s", status, info)

    await miot_net.register_info_changed_async(
        key="test", handler=on_network_info_changed
    )
    await miot_net.init_async()

    miot_lan = MIoTLan(
        net_ifs=list((await miot_net.get_info_async()).keys()), network=miot_net
    )
    await miot_lan.init_async()

    async def on_device_status_changed(did: str, info: MIoTLanDeviceInfo, ctx: Any):
        del ctx
        _LOGGER.info("on_device_status_changed, %s, %s", did, info)

    await miot_lan.register_status_changed_async(
        key="test", handler=on_device_status_changed
    )

    lan_devices = await miot_lan.get_devices_async()
    _LOGGER.info("lan devices: %s", lan_devices)

    # Get detected devices

    # while True:
    await asyncio.sleep(5)

    await miot_lan.deinit_async()
    await miot_net.deinit_async()


@pytest.mark.asyncio
async def test_init_socket_skips_unavailable_iface():
    """回归：_net_ifs 中不可用的网卡排在有效网卡之前时，有效网卡仍必须建 socket。

    旧代码在 __init_socket 里用 return 而非 continue，遇到第一个不可用网卡就跳出
    整个循环，导致排在其后的有效网卡一律建不了 socket（且无任何错误日志）。
    """
    miot_net = MIoTNetwork()
    miot_lan = MIoTLan(net_ifs=["ghost", "eth0"], network=miot_net)
    # 用 list 固定迭代顺序，把不可用网卡 ghost 稳定排在有效网卡 eth0 之前，
    # 复现旧 return 的触发场景（set 迭代无序，无法稳定复现该 bug）。
    miot_lan._net_ifs = ["ghost", "eth0"]
    miot_lan._available_net_ifs = {"eth0"}

    with patch.object(miot_lan, "_MIoTLan__create_socket") as mock_create:
        miot_lan._MIoTLan__init_socket()

    created = [call.kwargs.get("if_name") for call in mock_create.call_args_list]
    assert "eth0" in created
    assert "ghost" not in created
