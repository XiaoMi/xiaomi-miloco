# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MIoT Lan Test.
"""

import asyncio
import errno
import logging
import socket
from typing import Any
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# Unicast probe tests
# ---------------------------------------------------------------------------

def _make_mock_lan(net_ifs=None):
    """Create a MIoTLan with a mocked MIoTNetwork, suitable for unit tests.

    Passes an explicit mock event loop so the constructor doesn't need
    a running asyncio loop.
    """
    net = MagicMock()
    loop = MagicMock()
    miot_lan = MIoTLan(net_ifs=net_ifs or ["eth0"], network=net, loop=loop)
    return miot_lan


def test_set_unicast_targets_before_init_noop():
    """set_unicast_targets before init is a safe no-op, not a crash."""
    miot_lan = _make_mock_lan()
    # _init_done is False at this point
    miot_lan.set_unicast_targets({"did1": "192.168.1.100"})
    assert miot_lan._unicast_targets == {}


def test_set_unicast_targets_dispatches_non_empty():
    """A non-empty target dict is dispatched to the internal loop verbatim."""
    miot_lan = _make_mock_lan()
    miot_lan._init_done = True
    miot_lan._internal_loop = MagicMock()
    miot_lan.set_unicast_targets({"did1": "10.0.0.1"})
    # set_unicast_targets dispatches to __set_unicast_targets via call_soon_threadsafe
    miot_lan._internal_loop.call_soon_threadsafe.assert_called_once()
    args = miot_lan._internal_loop.call_soon_threadsafe.call_args[0]
    assert args[1]["did1"] == "10.0.0.1"


def test_clear_unicast_targets_dispatches_empty():
    """clear_unicast_targets dispatches {} via set_unicast_targets."""
    miot_lan = _make_mock_lan()
    miot_lan._init_done = True
    miot_lan._internal_loop = MagicMock()
    miot_lan.clear_unicast_targets()
    miot_lan._internal_loop.call_soon_threadsafe.assert_called_once()
    args = miot_lan._internal_loop.call_soon_threadsafe.call_args[0]
    assert args[1] == {}


def test_probe_unicast_targets_empty_noop():
    """Empty targets or empty sockets → early return, no sendto calls."""
    miot_lan = _make_mock_lan()

    # No targets, no sockets
    miot_lan._probe_unicast_targets()  # should not raise

    # Has targets but no sockets
    miot_lan._unicast_targets = {"did1": "10.0.0.1"}
    miot_lan._probe_unicast_targets()  # should not raise

    # Has sockets but no targets
    mock_sock = MagicMock()
    miot_lan._unicast_targets = {}
    miot_lan._broadcast_socks = {"eth0": mock_sock}
    miot_lan._probe_unicast_targets()
    mock_sock.sendto.assert_not_called()


def test_probe_unicast_targets_sends_to_ip():
    """Unicast probe sends OTU message to each target IP via a bound socket."""
    miot_lan = _make_mock_lan()
    mock_sock = MagicMock()
    miot_lan._broadcast_socks = {"eth0": mock_sock}
    miot_lan._unicast_targets = {"did1": "10.0.0.1", "did2": "10.0.0.2"}

    miot_lan._probe_unicast_targets()

    assert mock_sock.sendto.call_count == 2
    # First call
    call1_args = mock_sock.sendto.call_args_list[0][0]
    assert call1_args[1] == socket.MSG_DONTWAIT
    assert call1_args[2][1] == miot_lan.OT_PORT
    # Second call
    call2_args = mock_sock.sendto.call_args_list[1][0]
    assert call2_args[1] == socket.MSG_DONTWAIT
    assert call2_args[2][1] == miot_lan.OT_PORT


def test_probe_unicast_targets_skips_enetunreach():
    """ENETUNREACH on first socket → silently try next; EHOSTUNREACH same."""
    miot_lan = _make_mock_lan(net_ifs=["eth0", "wlan0"])

    # sock1 raises ENETUNREACH, sock2 succeeds
    sock1 = MagicMock()
    sock1.sendto.side_effect = OSError(errno.ENETUNREACH, "Network is unreachable")
    sock2 = MagicMock()
    miot_lan._broadcast_socks = {"eth0": sock1, "wlan0": sock2}
    miot_lan._unicast_targets = {"did1": "10.0.0.1"}

    miot_lan._probe_unicast_targets()

    sock1.sendto.assert_called_once()
    sock2.sendto.assert_called_once()

    # Same for EHOSTUNREACH
    sock3 = MagicMock()
    sock3.sendto.side_effect = OSError(errno.EHOSTUNREACH, "No route to host")
    sock4 = MagicMock()
    miot_lan._broadcast_socks = {"eth0": sock3, "wlan0": sock4}
    miot_lan._unicast_targets = {"did1": "10.0.0.1"}

    miot_lan._probe_unicast_targets()

    sock3.sendto.assert_called_once()
    sock4.sendto.assert_called_once()


def test_probe_unicast_targets_skips_empty_ip():
    """Target with empty IP string is skipped without touching sockets."""
    miot_lan = _make_mock_lan()
    mock_sock = MagicMock()
    miot_lan._broadcast_socks = {"eth0": mock_sock}
    miot_lan._unicast_targets = {"did1": "", "did2": "10.0.0.2"}

    miot_lan._probe_unicast_targets()

    # Only one sendto call — empty IP skipped
    assert mock_sock.sendto.call_count == 1
    assert mock_sock.sendto.call_args[0][2][0] == "10.0.0.2"
