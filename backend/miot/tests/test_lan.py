# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MIoT Lan Test.
"""

import asyncio
import errno
import logging
import socket
import struct
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from miot.lan import MIoTLan
from miot.network import MIoTNetwork
from miot.types import InterfaceStatus, MIoTLanDeviceInfo, NetworkInfo

_LOGGER = logging.getLogger(__name__)

# 全文件纯 mock、无真实网络 I/O：module-level 兜底打 unit，避免个别用例（尤其是
# 只标了 @pytest.mark.asyncio 忘记同时标 unit 的）被 CI 的 `-m unit` 过滤步骤
# 静默 deselect——那是唯一会收集 miot/tests/ 的 CI 步骤（另一步骤靠
# norecursedirs 排除了整个 miot 目录）。
pytestmark = pytest.mark.unit


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


@pytest.mark.unit
def test_set_unicast_targets_before_init_noop():
    """set_unicast_targets before init is a safe no-op, not a crash."""
    miot_lan = _make_mock_lan()
    # _init_done is False at this point
    miot_lan.set_unicast_targets({"did1": "192.168.1.100"})
    assert miot_lan._unicast_targets == {}


@pytest.mark.unit
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


@pytest.mark.unit
def test_probe_unicast_targets_empty_noop():
    """Empty targets or empty sockets → early return, no sendto calls."""
    miot_lan = _make_mock_lan()

    # No targets, no sockets
    miot_lan._probe_unicast_targets()  # should not raise

    # Has targets but no unicast socket yet
    miot_lan._unicast_targets = {"did1": "10.0.0.1"}
    miot_lan._probe_unicast_targets()  # should not raise

    # Has a unicast socket but no targets
    mock_sock = MagicMock()
    miot_lan._unicast_sock = mock_sock
    miot_lan._unicast_targets = {}
    miot_lan._probe_unicast_targets()
    mock_sock.sendto.assert_not_called()


@pytest.mark.unit
def test_probe_unicast_targets_sends_to_ip():
    """Unicast probe sends OTU message to each target IP via the dedicated,
    routed (unbound) unicast socket — not the per-interface broadcast sockets."""
    miot_lan = _make_mock_lan()
    mock_sock = MagicMock()
    miot_lan._unicast_sock = mock_sock
    miot_lan._unicast_targets = {"did1": "10.0.0.1", "did2": "10.0.0.2"}

    miot_lan._probe_unicast_targets()

    assert mock_sock.sendto.call_count == 2
    call1_args = mock_sock.sendto.call_args_list[0][0]
    assert call1_args[1] == socket.MSG_DONTWAIT
    assert call1_args[2] == ("10.0.0.1", miot_lan.OT_PORT)
    call2_args = mock_sock.sendto.call_args_list[1][0]
    assert call2_args[2] == ("10.0.0.2", miot_lan.OT_PORT)


@pytest.mark.unit
def test_probe_unicast_targets_send_error_caught():
    """sendto raising OSError (no route, etc.) is caught and must not stop
    probing the remaining targets, nor propagate."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    mock_sock = MagicMock()
    mock_sock.sendto.side_effect = [
        OSError(errno.EHOSTUNREACH, "No route to host"),
        32,
    ]
    miot_lan._unicast_sock = mock_sock
    miot_lan._unicast_targets = {"did1": "10.0.0.1", "did2": "10.0.0.2"}

    miot_lan._probe_unicast_targets()  # must not raise

    # Both targets attempted despite the first raising.
    assert mock_sock.sendto.call_count == 2


@pytest.mark.unit
def test_probe_unicast_targets_skips_empty_ip():
    """Target with empty IP string is skipped without touching the socket."""
    miot_lan = _make_mock_lan()
    mock_sock = MagicMock()
    miot_lan._unicast_sock = mock_sock
    miot_lan._unicast_targets = {"did1": "", "did2": "10.0.0.2"}

    miot_lan._probe_unicast_targets()

    # Only one sendto call — empty IP skipped
    assert mock_sock.sendto.call_count == 1
    assert mock_sock.sendto.call_args[0][2][0] == "10.0.0.2"


# ---------------------------------------------------------------------------
# Unicast socket read-side source allowlist
# ---------------------------------------------------------------------------


def _ot_probe_bytes(did: int = 123456789) -> bytes:
    b = bytearray(32)
    b[:20] = b"!1\x00\x20\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xffMDID"
    b[20:28] = struct.pack(">Q", did)
    b[28:32] = b"\x00\x00\x00\x00"
    return bytes(b)


def _mock_recv_from(msg: bytes, addr: tuple[str, int]):
    """A recvfrom_into side_effect that writes msg into the caller's buffer."""

    def _write(buf, size, flags):
        del size, flags
        buf[: len(msg)] = msg
        return len(msg), addr

    return _write


@pytest.mark.unit
def test_socket_read_handler_unicast_rejects_unknown_source():
    """The unicast socket doesn't bind to a single interface (by design — see
    __create_unicast_socket), so anything on the LAN can send a UDP packet to
    its port claiming to be an OTU reply. Only source IPs matching the
    current probe targets should be trusted."""
    miot_lan = _make_mock_lan()
    miot_lan._unicast_targets = {"did1": "10.0.0.5"}
    mock_sock = MagicMock()
    mock_sock.recvfrom_into.side_effect = _mock_recv_from(
        _ot_probe_bytes(), ("10.0.0.99", MIoTLan.OT_PORT)
    )

    with patch.object(miot_lan, "_MIoTLan__raw_message_handler") as mock_handler:
        miot_lan._MIoTLan__socket_read_handler(("unicast", mock_sock))

    mock_handler.assert_not_called()


@pytest.mark.unit
def test_socket_read_handler_unicast_accepts_known_source():
    """A reply from an IP that IS a current probe target must still be
    processed normally."""
    miot_lan = _make_mock_lan()
    miot_lan._unicast_targets = {"did1": "10.0.0.5"}
    mock_sock = MagicMock()
    mock_sock.recvfrom_into.side_effect = _mock_recv_from(
        _ot_probe_bytes(), ("10.0.0.5", MIoTLan.OT_PORT)
    )

    with patch.object(miot_lan, "_MIoTLan__raw_message_handler") as mock_handler:
        miot_lan._MIoTLan__socket_read_handler(("unicast", mock_sock))

    mock_handler.assert_called_once()


@pytest.mark.unit
def test_socket_read_handler_broadcast_not_filtered_by_unicast_targets():
    """The source allowlist is unicast-only: broadcast-socket replies must
    still be processed even when the source IP isn't a current unicast
    target (broadcast has no equivalent notion of "expected sender")."""
    miot_lan = _make_mock_lan()
    miot_lan._unicast_targets = {"did1": "10.0.0.5"}
    mock_sock = MagicMock()
    mock_sock.recvfrom_into.side_effect = _mock_recv_from(
        _ot_probe_bytes(), ("192.168.1.50", MIoTLan.OT_PORT)
    )

    with patch.object(miot_lan, "_MIoTLan__raw_message_handler") as mock_handler:
        miot_lan._MIoTLan__socket_read_handler(("eth0", mock_sock))

    mock_handler.assert_called_once()


# ---------------------------------------------------------------------------
# Connected-camera probe skip / scan pause-resume
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_probe_unicast_targets_skips_connected_dids():
    """A did already marked connected must not be re-probed via unicast --
    connectivity is already proven, so probing it is pointless."""
    miot_lan = _make_mock_lan()
    mock_sock = MagicMock()
    miot_lan._unicast_sock = mock_sock
    miot_lan._unicast_targets = {"did1": "10.0.0.1", "did2": "10.0.0.2"}
    miot_lan._connected_dids = {"did1"}

    miot_lan._probe_unicast_targets()

    assert mock_sock.sendto.call_count == 1
    assert mock_sock.sendto.call_args[0][2][0] == "10.0.0.2"


@pytest.mark.unit
def test_scan_devices_pauses_when_all_cameras_connected():
    """全部已知相机都已连上时，__scan_devices 既不探测也不重排——整个扫描停表，
    等掉线 / 新相机经 __maybe_resume_scan 拉起。

    可达性此时由拉流本身证实（mark_reachable 已标在线并取消离线定时器），广播和
    单播都不再产生任何被消费的结果。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1", "did2"}
    miot_lan._connected_dids = {"did1", "did2"}

    with patch.object(miot_lan, "ping_internal") as mock_ping, patch.object(
        miot_lan, "_probe_unicast_targets"
    ) as mock_probe:
        miot_lan._MIoTLan__scan_devices()

    mock_ping.assert_not_called()
    mock_probe.assert_not_called()
    assert miot_lan._scan_timer is None
    miot_lan._internal_loop.call_later.assert_not_called()


@pytest.mark.unit
def test_scan_devices_does_not_pause_when_no_cameras_known():
    """相机集为空时不能当成「全都连上了」——一台相机都还不知道就停扫描，会让
    引导期（尚未 refresh_cameras）的设备发现直接瘫掉。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = set()
    miot_lan._connected_dids = set()

    with patch.object(miot_lan, "ping_internal") as mock_ping, patch.object(
        miot_lan, "_probe_unicast_targets"
    ) as mock_probe:
        miot_lan._MIoTLan__scan_devices()

    mock_ping.assert_called_once()
    mock_probe.assert_called_once()
    miot_lan._internal_loop.call_later.assert_called_once()


@pytest.mark.unit
def test_scan_devices_runs_when_not_all_connected():
    """With at least one camera not yet connected, scanning proceeds and
    reschedules as usual."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1", "did2"}
    miot_lan._connected_dids = {"did1"}

    with patch.object(miot_lan, "ping_internal") as mock_ping, patch.object(
        miot_lan, "_probe_unicast_targets"
    ) as mock_probe:
        miot_lan._MIoTLan__scan_devices()

    mock_ping.assert_called_once()
    mock_probe.assert_called_once()
    miot_lan._internal_loop.call_later.assert_called_once()


@pytest.mark.unit
def test_set_camera_connected_resumes_paused_scan():
    """相机掉线时，若扫描此前因「全连上」而暂停，必须重新拉起。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1"}
    miot_lan._connected_dids = {"did1"}
    miot_lan._scan_timer = None  # 扫描当前处于暂停态

    miot_lan._MIoTLan__set_camera_connected("did1", False)

    assert "did1" not in miot_lan._connected_dids
    miot_lan._internal_loop.call_later.assert_called_once()


@pytest.mark.unit
def test_set_camera_connected_true_does_not_resume():
    """标记连上永远不该自己重启扫描——只有掉线 / 新相机才恢复。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1", "did2"}
    miot_lan._connected_dids = {"did1"}
    miot_lan._scan_timer = None

    miot_lan._MIoTLan__set_camera_connected("did2", True)

    assert miot_lan._connected_dids == {"did1", "did2"}
    miot_lan._internal_loop.call_later.assert_not_called()


@pytest.mark.unit
def test_set_camera_dids_resumes_scan_for_new_camera():
    """新出现的相机 did（尚未连上）必须把暂停的扫描拉起来。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1"}
    miot_lan._connected_dids = {"did1"}
    miot_lan._scan_timer = None

    miot_lan._MIoTLan__set_camera_dids({"did1", "did2"})

    assert miot_lan._camera_dids == {"did1", "did2"}
    miot_lan._internal_loop.call_later.assert_called_once()


@pytest.mark.unit
def test_maybe_resume_scan_noop_when_timer_already_running():
    """扫描已在排期时不能再排一个——否则会出现两条并行的扫描循环。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1"}
    miot_lan._connected_dids = set()
    miot_lan._scan_timer = MagicMock()  # 已排期

    miot_lan._MIoTLan__set_camera_connected("did1", False)

    miot_lan._internal_loop.call_later.assert_not_called()
