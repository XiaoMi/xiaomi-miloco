# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unit test for miot_network.py.
"""

import asyncio
import logging
import socket
from collections import namedtuple
from unittest.mock import patch

import pytest
from miot.network import InterfaceStatus, MIoTNetwork, NetworkInfo

_LOGGER = logging.getLogger(__name__)

# psutil 命名元组的最小替身，字段名与 snicaddr / snicstats 对齐。
_Addr = namedtuple("Addr", "family address netmask broadcast ptp")
_Stats = namedtuple("Stats", "isup duplex speed mtu flags")


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
    _LOGGER.info("delay 3000ms")
    await asyncio.sleep(3)
    _LOGGER.info("net status: %s", miot_net.network_status)
    _LOGGER.info("net info: %s", miot_net.network_info)
    await miot_net.deinit_async()


@pytest.mark.asyncio
async def test_get_network_info_skips_down_iface():
    """管理状态 DOWN（isup=False，如无载波的 virbr0）的网卡不应进入 network_info。"""
    miot_net = MIoTNetwork()
    addrs = {
        "eth0": [_Addr(socket.AF_INET, "192.168.1.10", "255.255.255.0", None, None)],
        "virbr0": [_Addr(socket.AF_INET, "192.168.122.1", "255.255.255.0", None, None)],
    }
    stats = {
        "eth0": _Stats(True, 2, 1000, 1500, "up,broadcast,running"),
        "virbr0": _Stats(False, 0, 0, 1500, "up,broadcast"),
    }
    with (
        patch("miot.network.psutil.net_if_addrs", return_value=addrs),
        patch("miot.network.psutil.net_if_stats", return_value=stats),
    ):
        info = miot_net._MIoTNetwork__get_network_info()
    assert set(info.keys()) == {"eth0"}


@pytest.mark.asyncio
async def test_get_network_info_keeps_iface_missing_from_stats():
    """net_if_addrs / net_if_stats 两次调用之间的竞态：stats 缺该 key 时保守保留接口。"""
    miot_net = MIoTNetwork()
    addrs = {
        "eth0": [_Addr(socket.AF_INET, "192.168.1.10", "255.255.255.0", None, None)],
    }
    with (
        patch("miot.network.psutil.net_if_addrs", return_value=addrs),
        patch("miot.network.psutil.net_if_stats", return_value={}),
    ):
        info = miot_net._MIoTNetwork__get_network_info()
    assert set(info.keys()) == {"eth0"}
