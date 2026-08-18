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
def test_scan_devices_throttles_but_never_stops_when_all_cameras_connected():
    """全部**已启用**相机都已连上时，扫描降频到 OT_PROBE_INTERVAL_MAX —— 但绝不停表。

    _camera_dids 是「已启用」集而非「全部」集：用户没打开开关的相机、以及非相机
    设备都不在里面，它们靠这个全局共享的广播维持 lan_online。一旦停表，它们会在
    _KA_TIMEOUT(100s) 后翻 False，而 toggle_camera 拿 lan_online 当硬门 → 用户再也
    开不了那台相机，且 __maybe_resume_scan 用同一判据恒早退，没有恢复路径。
    45s < 100s 是这个设计的关键不变量。"""
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1", "did2"}
    miot_lan._connected_dids = {"did1", "did2"}

    with patch.object(miot_lan, "ping_internal") as mock_ping, patch.object(
        miot_lan, "_probe_unicast_targets"
    ) as mock_probe:
        miot_lan._MIoTLan__scan_devices()

    # 探测照做：集合外的设备（未启用相机 / 非相机设备）需要它刷新在线态。
    mock_ping.assert_called_once()
    mock_probe.assert_called_once()
    # 定时器照排，间隔为最低频。
    miot_lan._internal_loop.call_later.assert_called_once()
    assert (
        miot_lan._internal_loop.call_later.call_args[0][0]
        == miot_lan.OT_PROBE_INTERVAL_MAX
    )
    assert miot_lan._scan_throttled is True
    # 保活窗必须盖得住降频后的扫描间隔，否则集合外设备照样掉线。
    assert miot_lan.OT_PROBE_INTERVAL_MAX < _MIoTLanDevice._KA_TIMEOUT


@pytest.mark.unit
def test_scan_devices_does_not_pause_when_no_cameras_known():
    """相机集为空时不能当成「全都连上了」——一台相机都还不知道就降频，会让
    引导期（尚未 refresh_cameras）的设备发现变慢。"""
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
    assert miot_lan._scan_throttled is False


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
    miot_lan._scan_timer = MagicMock()  # 已排期
    miot_lan._scan_throttled = True  # 但处于降频态

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
    miot_lan._scan_timer = MagicMock()
    miot_lan._scan_throttled = True

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
    miot_lan._scan_timer = MagicMock()
    miot_lan._scan_throttled = True

    miot_lan._MIoTLan__set_camera_dids({"did1", "did2"})

    assert miot_lan._camera_dids == {"did1", "did2"}
    miot_lan._internal_loop.call_later.assert_called_once()


@pytest.mark.unit
def test_maybe_resume_scan_noop_when_not_throttled():
    """扫描本就在正常退避排期上（未降频）时不该被打扰。

    否则每轮 set_camera_dids / set_camera_connected 都会把退避重置回
    OT_PROBE_INTERVAL_MIN，在「有相机还没连上」的整个窗口里探测流量白涨一个量级。
    """
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"did1"}
    miot_lan._connected_dids = set()
    miot_lan._scan_timer = MagicMock()  # 已排期
    miot_lan._scan_throttled = False  # 且未降频 → 正常排期中

    miot_lan._MIoTLan__set_camera_connected("did1", False)

    miot_lan._internal_loop.call_later.assert_not_called()


@pytest.mark.unit
def test_devices_outside_enabled_set_keep_being_probed_while_throttled():
    """回归（死锁防线）：降频态下集合外的设备仍被探测覆盖。

    场景：camA 已启用并连上（_camera_dids == _connected_dids == {camA}），camB 刚绑定
    还没打开开关，因此不在 _camera_dids 里。旧实现在这一刻停表，camB 拿不到任何探测，
    _KA_TIMEOUT 后 lan_online 掉 False；而 toggle_camera 拿 lan_online 当硬门 →
    用户再也开不了 camB，且 __maybe_resume_scan 用同一判据恒早退，无恢复路径。
    """
    miot_lan = _make_mock_lan()
    miot_lan._internal_loop = MagicMock()
    miot_lan._camera_dids = {"camA"}
    miot_lan._connected_dids = {"camA"}

    with patch.object(miot_lan, "ping_internal") as mock_ping, patch.object(
        miot_lan, "_probe_unicast_targets"
    ) as mock_probe:
        # 连跑两轮，确认降频态是可持续的（每轮都重排定时器，永不停）。
        miot_lan._MIoTLan__scan_devices()
        miot_lan._MIoTLan__scan_devices()

    assert mock_ping.call_count == 2
    assert mock_probe.call_count == 2
    assert miot_lan._internal_loop.call_later.call_count == 2
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

    with patch.object(miot_lan, "_MIoTLan__maybe_resume_scan") as mock_resume:
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
    with patch.object(miot_lan, "_MIoTLan__maybe_resume_scan"):
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

    with patch.object(miot_lan, "_MIoTLan__maybe_resume_scan"):
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

    with patch.object(miot_lan, "_MIoTLan__maybe_resume_scan"):
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

    with patch.object(miot_lan, "_MIoTLan__maybe_resume_scan"):
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
