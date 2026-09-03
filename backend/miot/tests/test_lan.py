# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MIoT Lan Test.
"""

import errno
import logging
import socket
import struct
from unittest.mock import MagicMock, patch

import pytest
from miot.lan import MIoTLan, _MIoTLanDevice
from miot.network import MIoTNetwork

_LOGGER = logging.getLogger(__name__)

# 全文件纯 mock、无真实网络 I/O：module-level 兜底打 unit，避免个别用例（尤其是
# 只标了 @pytest.mark.asyncio 忘记同时标 unit 的）被 CI 的 `-m unit` 过滤步骤
# 静默 deselect——那是唯一会收集 miot/tests/ 的 CI 步骤（另一步骤靠
# norecursedirs 排除了整个 miot 目录）。
pytestmark = pytest.mark.unit


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
def test_probe_unicast_targets_includes_same_subnet_target():
    """同网段目标也要发单播——不再依赖「广播一定送达同网段」这个假设。

    定向子网广播会被交换机的 IGMP/风暴抑制、AP 客户端隔离、路由器广播限速丢掉，此时
    同网段相机明明可直连、却因为收不到广播探测而 lan_online 恒 False。
    （代价见 _probe_unicast_targets docstring 里的 macOS LNP 说明。）
    """
    miot_lan = _make_mock_lan()
    # 让 192.168.1.x 判定为同网段：本机网卡就在这个网段。
    miot_lan._network.network_info = {
        "eth0": MagicMock(ip="192.168.1.10", netmask="255.255.255.0")
    }
    assert miot_lan.is_cross_subnet("192.168.1.50") is False

    mock_sock = MagicMock()
    miot_lan._unicast_sock = mock_sock
    miot_lan._unicast_targets = {"same": "192.168.1.50", "cross": "10.0.0.2"}

    miot_lan._probe_unicast_targets()

    sent = {call[0][2][0] for call in mock_sock.sendto.call_args_list}
    assert sent == {"192.168.1.50", "10.0.0.2"}


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
def test_probe_unicast_targets_recreates_socket_when_missing():
    """单播 socket 缺失（启动时一次性建 socket 失败）时，探测应按需补建一次而不是
    永久 no-op——旧代码只在线程启动时建一次，网卡增删回调不会重建它，一次瞬时失败
    （fd 耗尽 / 网络栈未就绪）会让跨网段发现在整个进程生命周期里静默失效。
    """
    miot_lan = _make_mock_lan()
    miot_lan._unicast_sock = None
    miot_lan._unicast_targets = {"did1": "10.0.0.1"}
    miot_lan._internal_loop = MagicMock()

    created_sock = MagicMock()
    with patch("socket.socket", return_value=created_sock):
        miot_lan._probe_unicast_targets()

    assert miot_lan._unicast_sock is created_sock
    created_sock.sendto.assert_called_once()


@pytest.mark.unit
def test_probe_unicast_targets_skips_this_round_when_recreate_fails():
    """补建仍失败（比如 fd 依旧耗尽）——本轮直接跳过，不抛异常，下一轮扫描再试。"""
    miot_lan = _make_mock_lan()
    miot_lan._unicast_sock = None
    miot_lan._unicast_targets = {"did1": "10.0.0.1"}
    miot_lan._internal_loop = MagicMock()

    with patch("socket.socket", side_effect=OSError("fd exhausted")):
        miot_lan._probe_unicast_targets()  # must not raise

    assert miot_lan._unicast_sock is None


@pytest.mark.unit
def test_create_unicast_socket_noop_when_already_exists():
    """__create_unicast_socket 幂等：已有 socket 时按需补建调用直接跳过，
    不能悄悄换掉一个还在用的 socket（会丢了它 add_reader 注册的回包路径）。"""
    miot_lan = _make_mock_lan()
    existing = MagicMock()
    miot_lan._unicast_sock = existing
    miot_lan._internal_loop = MagicMock()

    with patch("socket.socket") as mock_socket_ctor:
        miot_lan._MIoTLan__create_unicast_socket()

    mock_socket_ctor.assert_not_called()
    assert miot_lan._unicast_sock is existing


# ---------------------------------------------------------------------------
# is_cross_subnet: 「判不出来」必须是 None，不能被当成「确定跨网段」
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_cross_subnet_none_when_network_table_empty():
    """本机网卡表为空（Wi-Fi 漫游 / 网卡 down 的刷新窗口）时，is_cross_subnet 必须
    返回 None（判不出来），不能返回 True（确定跨网段）。

    回归动机：旧实现里 __is_local_subnet 对「网卡表为空」和「目标确实不在任何已知
    网段」共用同一个 False 返回值，is_cross_subnet 直接对它取反，于是"判不出来"被
    当成了"确定跨网段"上报给上层——跨 NAT 提示会在网卡表暂时读不到的窗口里，把一台
    其实同网段、只是网卡表刚好为空的相机诊断成 NAT 阻断，指着住户去折腾路由器。
    """
    miot_lan = _make_mock_lan()
    miot_lan._network.network_info = {}
    assert miot_lan.is_cross_subnet("192.168.1.50") is None


@pytest.mark.unit
def test_is_cross_subnet_true_for_genuinely_different_subnet():
    """本机网段能解析、目标确实不在其中 → 照常判 True（回归防护：上面那条修复
    不能把"确实跨网段"也误改成 None）。"""
    miot_lan = _make_mock_lan()
    miot_lan._network.network_info = {
        "eth0": MagicMock(ip="192.168.1.10", netmask="255.255.255.0")
    }
    assert miot_lan.is_cross_subnet("10.0.0.2") is True


@pytest.mark.unit
def test_is_cross_subnet_false_for_same_subnet():
    miot_lan = _make_mock_lan()
    miot_lan._network.network_info = {
        "eth0": MagicMock(ip="192.168.1.10", netmask="255.255.255.0")
    }
    assert miot_lan.is_cross_subnet("192.168.1.50") is False


@pytest.mark.unit
def test_is_cross_subnet_none_when_ip_unknown_or_invalid():
    miot_lan = _make_mock_lan()
    miot_lan._network.network_info = {
        "eth0": MagicMock(ip="192.168.1.10", netmask="255.255.255.0")
    }
    assert miot_lan.is_cross_subnet(None) is None
    assert miot_lan.is_cross_subnet("") is None
    assert miot_lan.is_cross_subnet("not-an-ip") is None


@pytest.mark.unit
def test_scan_devices_stops_when_all_cloud_online_connected():
    """所有「云端在线 ∧ 当前 scope」相机都已连上 → 扫描停表：不探测、不排下一轮。

    可开启相机 = 云端在线 ∧ in-scope，正好全在 _cloud_online_dids 里；集合外只剩
    云端离线（toggle 的 online 门挡着）与 scope 外（home_allowed 门挡着），无需
    探测。已连相机在线态由拉流状态（mark_reachable）维持，不依赖扫描。
    """
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._cloud_online_dids = {"did1", "did2"}
    miot_lan._connected_dids = {"did1", "did2"}

    with patch.object(miot_lan, "ping_internal") as mock_ping, patch.object(
        miot_lan, "_probe_unicast_targets"
    ) as mock_probe:
        miot_lan._MIoTLan__scan_devices()

    # 停表：不探测、不排下一轮。
    mock_ping.assert_not_called()
    mock_probe.assert_not_called()
    miot_lan._internal_loop.call_later.assert_not_called()


@pytest.mark.unit
def test_scan_devices_stops_when_no_cloud_online_cameras():
    """没有云端在线相机（空集 ⊆ 空集）→ 停表，等同步。

    刻意不做空集防御：还没同步时停表，等 set_cloud_online_dids 推下集来，
    __resume_scan 会恢复探测，广播等同步后再发。
    """
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._cloud_online_dids = set()
    miot_lan._connected_dids = set()

    with patch.object(miot_lan, "ping_internal") as mock_ping, patch.object(
        miot_lan, "_probe_unicast_targets"
    ) as mock_probe:
        miot_lan._MIoTLan__scan_devices()

    mock_ping.assert_not_called()
    mock_probe.assert_not_called()
    miot_lan._internal_loop.call_later.assert_not_called()


@pytest.mark.unit
def test_scan_devices_runs_when_not_all_connected():
    """有一台云端在线相机尚未连上 → 探测照做并正常重排。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._cloud_online_dids = {"did1", "did2"}
    miot_lan._connected_dids = {"did1"}

    with patch.object(miot_lan, "ping_internal") as mock_ping, patch.object(
        miot_lan, "_probe_unicast_targets"
    ) as mock_probe:
        miot_lan._MIoTLan__scan_devices()

    mock_ping.assert_called_once()
    mock_probe.assert_called_once()
    miot_lan._internal_loop.call_later.assert_called_once()


@pytest.mark.unit
def test_set_camera_connected_disconnect_resumes_stopped_scan():
    """相机掉线时，若扫描此前因「全云在线相机已连上」而停表，必须恢复。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._cloud_online_dids = {"did1"}
    miot_lan._connected_dids = {"did1"}
    miot_lan._scan_timer = None  # 停表态

    miot_lan._MIoTLan__set_camera_connected("did1", False)

    assert "did1" not in miot_lan._connected_dids
    miot_lan._internal_loop.call_later.assert_called_once()
    assert miot_lan._internal_loop.call_later.call_args[0][0] == 0


@pytest.mark.unit
def test_set_camera_connected_true_does_not_resume():
    """标记连上永远不该自己重启扫描——连接只会让条件更满足，恢复由掉线/新增驱动。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._cloud_online_dids = {"did1", "did2"}
    miot_lan._connected_dids = {"did1"}
    miot_lan._scan_timer = None

    miot_lan._MIoTLan__set_camera_connected("did2", True)

    assert miot_lan._connected_dids == {"did1", "did2"}
    miot_lan._internal_loop.call_later.assert_not_called()


@pytest.mark.unit
def test_set_cloud_online_dids_resumes_scan_for_new_camera():
    """新出现的云端在线相机 did（尚未连上）必须把停表的扫描拉起来。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._cloud_online_dids = {"did1"}
    miot_lan._connected_dids = {"did1"}
    miot_lan._scan_timer = None  # 停表态

    miot_lan._MIoTLan__set_cloud_online_dids({"did1", "did2"})

    assert miot_lan._cloud_online_dids == {"did1", "did2"}
    miot_lan._internal_loop.call_later.assert_called_once()
    assert miot_lan._internal_loop.call_later.call_args[0][0] == 0


@pytest.mark.unit
def test_resume_scan_noop_while_scan_running():
    """扫描本就在排期上（_scan_timer 非空）时，集合变化不该重排。

    只有停表态（_scan_timer 为空）才恢复；否则下一轮自己重判，退避不被重置，
    探测流量不会白涨一个量级。
    """
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._cloud_online_dids = {"did1"}
    miot_lan._connected_dids = set()
    miot_lan._scan_timer = MagicMock()  # 已排期

    miot_lan._MIoTLan__set_camera_connected("did1", False)

    miot_lan._internal_loop.call_later.assert_not_called()


@pytest.mark.unit
def test_unconnected_cloud_online_camera_keeps_scan_running():
    """回归（旧死锁防线）：一台云端在线但未连上的相机让扫描保持运行。

    场景：camA 已连上（在 connected），camB 云端在线但还没连上。旧实现用「已启用」
    活跃集判「全连上」，camB 不在里面会被当成全连而降频停表 → camB lan_online 掉
    False → toggle 硬门挡 → 再也开不了。新判据用云端在线集，camB 在里面且未连 →
    inequality → 扫描持续探它，lan_online 保持新鲜，随时能开。
    """
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._cloud_online_dids = {"camA", "camB"}
    miot_lan._connected_dids = {"camA"}

    with patch.object(miot_lan, "ping_internal") as mock_ping, patch.object(
        miot_lan, "_probe_unicast_targets"
    ) as mock_probe:
        miot_lan._MIoTLan__scan_devices()

    assert mock_ping.call_count == 1
    assert mock_probe.call_count == 1
    assert miot_lan._internal_loop.call_later.call_count == 1
    assert miot_lan._scan_timer is not None


# ---------------------------------------------------------------------------
# keep_alive_on_stop: 别拿「连不上」去续期「可达性」
# ---------------------------------------------------------------------------


def _real_device(miot_lan, did="did1", ip="10.0.0.9"):
    """真实 _MIoTLanDevice + 可观测的假定时器。

    这几条断言的核心是「究竟有没有定时器、deadline 有没有被推后」，MagicMock 的
    device 只能断言「mark_reachable 有没有被调到」，看不见这个——上一版就是这么漏掉
    「一个定时器都不存在」这条路径的。
    """
    clock = {"now": 0.0}
    handles = []

    def _call_later(delay, cb):
        h = MagicMock()
        h.when.return_value = clock["now"] + delay
        handles.append(h)
        return h

    loop = MagicMock()
    loop.call_later.side_effect = _call_later
    miot_lan._internal_loop = loop
    device = _MIoTLanDevice(manager=miot_lan, did=did, ip=ip)
    miot_lan._lan_devices = {did: device}
    return device, clock


@pytest.mark.unit
def test_native_failure_does_not_rearm_keepalive_grace():
    """keep_alive_on_stop=False → 只移出已连集并恢复扫描，不给宽限窗续期。

    调用方（相机状态回调）用它表示「原生报连接失败」而非「我们主动关流」。拿失败
    证据去证明可达方向是反的，而且原生带退避自动重连（3s→6s→12s…，每次都短于
    _KA_TIMEOUT=100s），每次失败都续期等于把宽限窗无限拉长——相机被拔电后接口会
    连续几分钟仍报 lan_reachable=true，而这个字段的语义本是「100s 收不到探测响应
    就翻离线」。
    """
    miot_lan = _make_mock_lan()
    device = MagicMock()
    miot_lan._lan_devices = {"did1": device}
    miot_lan._connected_dids = {"did1"}

    with patch.object(miot_lan, "_MIoTLan__resume_scan") as mock_resume:
        miot_lan._MIoTLan__set_camera_connected(
            "did1", False, keep_alive_on_stop=False
        )

    device.mark_reachable.assert_not_called()
    assert "did1" not in miot_lan._connected_dids
    mock_resume.assert_called_once()


@pytest.mark.unit
def test_cross_subnet_camera_still_has_offline_path_after_unplug():
    """跨网段相机「连上过再被拔电」必须仍存在一条翻离线的路径。

    这条路径上两个 schedule 点都被绕开了：keep_alive() 要先收到探测应答（可
    connected 期间单播探测**主动跳过**该 did、广播又跨不了子网），而建连那次
    mark_reachable(start_grace=False) 把定时器 cancel 后置成了 None。若失败分支
    也不武装，设备就永远没有定时器 ⇒ lan_online 永久为真 ⇒ 接口一直报
    lan_reachable、跨 NAT 诊断还把没电的相机说成路由器 NAT 问题。
    """
    miot_lan = _make_mock_lan()
    device, _ = _real_device(miot_lan)

    # 拉流建连成功：连接本身是活性证明，定时器被取消且置空。
    miot_lan._MIoTLan__set_camera_connected("did1", True)
    assert device._ka_timer is None

    # 相机被拔电，原生报 DISCONNECTED（非主动关流）。
    with patch.object(miot_lan, "_MIoTLan__resume_scan"):
        miot_lan._MIoTLan__set_camera_connected(
            "did1", False, keep_alive_on_stop=False
        )

    assert device._ka_timer is not None, "必须留一条 100s 后翻离线的路径"


@pytest.mark.unit
def test_repeated_native_failures_do_not_push_offline_deadline():
    """连续失败重试不得把 deadline 往后推——否则等于用失败证据无限续期可达性。

    原生退避是 3s→6s→12s…，每一档都短于 _KA_TIMEOUT(100s)，一旦每次都续期，窗口
    会被连续顶到退避涨过 100s 才断，相机物理消失后接口还要说好几分钟的谎。
    """
    miot_lan = _make_mock_lan()
    device, clock = _real_device(miot_lan)
    miot_lan._MIoTLan__set_camera_connected("did1", True)

    with patch.object(miot_lan, "_MIoTLan__resume_scan"):
        miot_lan._MIoTLan__set_camera_connected(
            "did1", False, keep_alive_on_stop=False
        )
        first_deadline = device._ka_timer.when()

        # 3s 后原生重试又失败。
        clock["now"] += 3
        miot_lan._MIoTLan__set_camera_connected(
            "did1", False, keep_alive_on_stop=False
        )

    assert device._ka_timer.when() == first_deadline, "deadline 不得被失败重试推后"


@pytest.mark.unit
def test_deliberate_stop_does_extend_deadline():
    """反向对照：主动关流走 mark_reachable(start_grace=True)，deadline 确实会重排。"""
    miot_lan = _make_mock_lan()
    device, clock = _real_device(miot_lan)
    miot_lan._MIoTLan__set_camera_connected("did1", True)

    with patch.object(miot_lan, "_MIoTLan__resume_scan"):
        miot_lan._MIoTLan__set_camera_connected("did1", False)
        first_deadline = device._ka_timer.when()
        clock["now"] += 3
        miot_lan._MIoTLan__set_camera_connected("did1", False)

    assert device._ka_timer.when() > first_deadline


@pytest.mark.unit
def test_deliberate_stop_still_rearms_keepalive_grace():
    """默认（我们主动关流）仍要续期宽限窗，防跨网段相机在关流瞬间闪 offline。

    跨网段相机在 connected 期间被单播探测跳过，没有任何东西刷新它的 keep-alive，
    所以主动关流那一瞬必须先兜住在线态、由恢复后的扫描去复核。
    """
    miot_lan = _make_mock_lan()
    device = MagicMock()
    miot_lan._lan_devices = {"did1": device}
    miot_lan._connected_dids = {"did1"}

    with patch.object(miot_lan, "_MIoTLan__resume_scan"):
        miot_lan._MIoTLan__set_camera_connected("did1", False)

    device.mark_reachable.assert_called_once_with(start_grace=True)


@pytest.mark.unit
def test_connected_path_ignores_keep_alive_on_stop():
    """connected=True 时该参数无意义：连上就是活性证明，取消离线定时器。"""
    miot_lan = _make_mock_lan()
    device = MagicMock()
    miot_lan._lan_devices = {"did1": device}

    miot_lan._MIoTLan__set_camera_connected("did1", True, keep_alive_on_stop=False)

    device.mark_reachable.assert_called_once_with(start_grace=False)
    assert "did1" in miot_lan._connected_dids
