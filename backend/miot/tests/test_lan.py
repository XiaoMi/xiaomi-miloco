# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MIoT Lan Test.
"""

import asyncio
import errno
import logging
import os
import socket
import struct
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
    # The ping-fallback coroutine spawned for the failed target is never
    # actually run here (create_task is mocked) — close it to avoid an
    # "coroutine was never awaited" warning leaking into other tests.
    for call in miot_lan._internal_loop.create_task.call_args_list:
        call.args[0].close()


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
    """Once every known camera did is connected, __scan_devices must not
    ping/probe nor reschedule itself — scanning stays paused until a
    camera disconnects or a new one appears."""
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
    """Marking a camera disconnected while scanning was paused (all
    connected) must restart the scan loop."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1"}
    miot_lan._connected_dids = {"did1"}
    miot_lan._scan_timer = None  # scanning currently paused

    miot_lan._MIoTLan__set_camera_connected("did1", False)

    assert "did1" not in miot_lan._connected_dids
    miot_lan._internal_loop.call_later.assert_called_once()


@pytest.mark.unit
def test_set_camera_connected_true_does_not_resume():
    """Marking a camera connected must never itself restart scanning —
    only disconnects / newly-appeared cameras should."""
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
    """A newly-appeared camera did (not yet connected) must resume a
    paused scan."""
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
    """If a scan is already scheduled, __maybe_resume_scan must not
    schedule a second one."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1"}
    miot_lan._connected_dids = set()
    miot_lan._scan_timer = MagicMock()  # already scheduled

    miot_lan._MIoTLan__set_camera_connected("did1", False)

    miot_lan._internal_loop.call_later.assert_not_called()


# ---------------------------------------------------------------------------
# Ping fallback on unicast sendto failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_probe_unicast_targets_spawns_ping_fallback_on_failure():
    """A failed unicast sendto must spawn a ping-fallback task for that did,
    and mark it in-flight so a fast-repeating scan doesn't stack duplicates."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    mock_sock = MagicMock()
    mock_sock.sendto.side_effect = OSError(errno.EHOSTUNREACH, "No route to host")
    miot_lan._unicast_sock = mock_sock
    miot_lan._unicast_targets = {"did1": "10.0.0.1"}

    miot_lan._probe_unicast_targets()

    assert "did1" in miot_lan._ping_fallback_inflight
    miot_lan._internal_loop.create_task.assert_called_once()
    miot_lan._internal_loop.create_task.call_args[0][0].close()


@pytest.mark.unit
def test_probe_unicast_targets_does_not_stack_duplicate_ping_fallback():
    """A did already being ping-checked must not get a second fallback task
    spawned on the next scan cycle before the first one finishes."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    mock_sock = MagicMock()
    mock_sock.sendto.side_effect = OSError(errno.EHOSTUNREACH, "No route to host")
    miot_lan._unicast_sock = mock_sock
    miot_lan._unicast_targets = {"did1": "10.0.0.1"}
    miot_lan._ping_fallback_inflight = {"did1"}  # already in flight

    miot_lan._probe_unicast_targets()

    miot_lan._internal_loop.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_ping_fallback_async_dgram_success_skips_ping_command():
    """A successful unprivileged SOCK_DGRAM ICMP echo must mark the device
    online without ever falling back to the external ping command."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()  # keep_alive() schedules its KA timer here
    miot_lan._internal_loop.run_in_executor = AsyncMock(return_value=True)
    miot_lan._ping_fallback_inflight = {"did1"}

    with patch("asyncio.create_subprocess_exec", AsyncMock()) as mock_exec:
        await miot_lan._MIoTLan__ping_fallback_async("did1", "10.0.0.5")

    mock_exec.assert_not_called()
    assert "did1" in miot_lan._lan_devices
    assert miot_lan._lan_devices["did1"].online is True
    assert miot_lan._lan_devices["did1"].ip == "10.0.0.5"
    assert "did1" not in miot_lan._ping_fallback_inflight
    miot_lan._internal_loop.call_later.assert_called_once()


@pytest.mark.asyncio
async def test_ping_fallback_async_falls_back_to_ping_command():
    """When the unprivileged SOCK_DGRAM ICMP attempt fails/is unavailable
    (e.g. no CAP_NET_RAW-free path on this platform), it must fall back to
    the external ping command and still mark the device online on success."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._internal_loop.run_in_executor = AsyncMock(return_value=False)
    miot_lan._ping_fallback_inflight = {"did1"}
    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)
    ) as mock_exec:
        await miot_lan._MIoTLan__ping_fallback_async("did1", "10.0.0.5")

    mock_exec.assert_called_once()
    assert "did1" in miot_lan._lan_devices
    assert miot_lan._lan_devices["did1"].online is True
    assert "did1" not in miot_lan._ping_fallback_inflight


@pytest.mark.asyncio
async def test_ping_fallback_async_both_fail_leaves_device_absent():
    """Neither the SOCK_DGRAM attempt nor the ping command succeeding must
    not fabricate a device / online state, but must still clear the
    in-flight marker so future scans can retry."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._internal_loop.run_in_executor = AsyncMock(return_value=False)
    miot_lan._ping_fallback_inflight = {"did1"}
    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock(return_value=1)

    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)
    ):
        await miot_lan._MIoTLan__ping_fallback_async("did1", "10.0.0.5")

    assert "did1" not in miot_lan._lan_devices
    assert "did1" not in miot_lan._ping_fallback_inflight


@pytest.mark.asyncio
async def test_ping_fallback_async_subprocess_error_clears_inflight():
    """If spawning the ping subprocess itself raises (e.g. binary missing,
    as in some container images), the in-flight marker must still be
    cleared (no error must propagate)."""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._internal_loop.run_in_executor = AsyncMock(return_value=False)
    miot_lan._ping_fallback_inflight = {"did1"}

    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=OSError("ping not found")),
    ):
        await miot_lan._MIoTLan__ping_fallback_async("did1", "10.0.0.5")  # must not raise

    assert "did1" not in miot_lan._ping_fallback_inflight


# ---------------------------------------------------------------------------
# Unprivileged SOCK_DGRAM ICMP echo (_icmp_dgram_ping)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_icmp_checksum_deterministic_and_in_range():
    from miot.lan import _icmp_checksum

    data = b"\x08\x00\x00\x00\x12\x34\x00\x01"
    csum = _icmp_checksum(data)
    assert csum == _icmp_checksum(data)
    assert 0 <= csum <= 0xFFFF


@pytest.mark.unit
def test_icmp_dgram_ping_matches_reply_with_our_ident(monkeypatch):
    """macOS doesn't rewrite the echo id — reply carries back the ident we
    sent, no IPv4 header prepended."""
    from miot.lan import _icmp_dgram_ping

    ident = os.getpid() & 0xFFFF
    reply = struct.pack("!BBHHH", 0, 0, 0, ident, 1) + b"\x00" * 8

    mock_sock = MagicMock()
    mock_sock.getsockname.return_value = ("0.0.0.0", 54321)  # unrelated to ident
    mock_sock.recvfrom.return_value = (reply, ("1.2.3.4", 0))
    monkeypatch.setattr(socket, "socket", MagicMock(return_value=mock_sock))

    assert _icmp_dgram_ping("1.2.3.4", 1.0) is True


@pytest.mark.unit
def test_icmp_dgram_ping_matches_reply_with_ip_header(monkeypatch):
    """macOS's SOCK_DGRAM ICMP reply includes the IPv4 header (confirmed by
    live testing) — must be detected and stripped by the version nibble,
    not assumed absent just because it's a DGRAM socket."""
    from miot.lan import _icmp_dgram_ping

    ident = os.getpid() & 0xFFFF
    icmp = struct.pack("!BBHHH", 0, 0, 0, ident, 1) + b"\x00" * 8
    ip_header = bytes([0x45]) + b"\x00" * 19  # version=4, IHL=5 (20 bytes)
    reply = ip_header + icmp

    mock_sock = MagicMock()
    mock_sock.getsockname.return_value = ("0.0.0.0", 54321)
    mock_sock.recvfrom.return_value = (reply, ("1.2.3.4", 0))
    monkeypatch.setattr(socket, "socket", MagicMock(return_value=mock_sock))

    assert _icmp_dgram_ping("1.2.3.4", 1.0) is True


@pytest.mark.unit
def test_icmp_dgram_ping_matches_reply_with_kernel_rewritten_id(monkeypatch):
    """Linux's unprivileged ping-socket rewrites the echo id to the socket's
    bound local port at send time (kernel demuxes by port, not by the id we
    filled in) — the reply's id is therefore the port, never our
    ``os.getpid() & 0xFFFF``. Regression for the exact gap flagged in
    review: without accepting ``bound_id`` too, this path never matches on
    Linux even when the host is genuinely reachable."""
    from miot.lan import _icmp_dgram_ping

    ident = os.getpid() & 0xFFFF
    bound_port = 54321
    assert bound_port != ident  # test only proves something if these differ
    reply = struct.pack("!BBHHH", 0, 0, 0, bound_port, 1) + b"\x00" * 8

    mock_sock = MagicMock()
    mock_sock.getsockname.return_value = ("0.0.0.0", bound_port)
    mock_sock.recvfrom.return_value = (reply, ("1.2.3.4", 0))
    monkeypatch.setattr(socket, "socket", MagicMock(return_value=mock_sock))

    assert _icmp_dgram_ping("1.2.3.4", 1.0) is True


@pytest.mark.unit
def test_icmp_dgram_ping_ignores_reply_matching_neither_ident_nor_port(monkeypatch):
    """A reply for someone else's echo request (id matches neither our
    ident nor our bound port) must not be treated as our own — keep waiting
    instead (here: time out immediately since recvfrom is mocked to return
    the same non-matching reply every call would hang, so simulate an
    immediate timeout instead)."""
    from miot.lan import _icmp_dgram_ping

    mock_sock = MagicMock()
    mock_sock.getsockname.return_value = ("0.0.0.0", 54321)
    mock_sock.recvfrom.side_effect = socket.timeout()
    monkeypatch.setattr(socket, "socket", MagicMock(return_value=mock_sock))

    assert _icmp_dgram_ping("1.2.3.4", 0.05) is False


@pytest.mark.unit
def test_icmp_dgram_ping_socket_creation_failure_returns_false(monkeypatch):
    """No CAP_NET_RAW-free ICMP path available (e.g. Linux
    net.ipv4.ping_group_range doesn't include us) → False, not raise."""
    from miot.lan import _icmp_dgram_ping

    monkeypatch.setattr(
        socket, "socket", MagicMock(side_effect=OSError("Permission denied"))
    )

    assert _icmp_dgram_ping("1.2.3.4", 1.0) is False
