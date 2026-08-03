# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for the pure-Python legacy-unicast mDNS discovery (miot.mdns).

No network: the query builder, DNS response parser (name compression + SRV /
TXT / A extraction), MipsServiceData.from_raw, and the end-to-end ingest are
exercised against two REAL captured ``_miot-central`` response packets.
"""

from __future__ import annotations

import base64
import struct

import pytest
from miot.mdns import (
    MIPS_MDNS_TYPE,
    MdnsService,
    MdnsServiceError,
    MdnsServiceState,
    MipsServiceData,
)

# Real mDNS responses captured on-device (two main gateways).
PKT_4399 = base64.b64decode(
    "EjSEAAABAAEAAAAEDV9taW90LWNlbnRyYWwEX3RjcAVsb2NhbAAADAABwAwADAABAAAACgAb"
    "GHhpYW9taS1nYXRld2F5LWh1YjEtNDM5OcAMwDYAIQABAAAACgAQAAAAACKzB0FuZHJvaWTA"
    "H8A2ABAAAQAAAAoAKShwcm9maWxlPUZBQUFBQUJDRWtnL0Jvd3JmK3VEOGpzZ0FBQVJBQUk9"
    "wGMAAQABAAAACgAEwKgBmMBjABwAAQAAAAoAEP6AAAAAAAAAUogR//75678="
)
PKT_0855 = base64.b64decode(
    "EjSEAAABAAEAAAAEDV9taW90LWNlbnRyYWwEX3RjcAVsb2NhbAAADAABwAwADAABAAAACgAb"
    "GHhpYW9taS1nYXRld2F5LWh1YjEtMDg1NcAMwDYAIQABAAAACgANAAAAACKzBG9oMXDAH8A2"
    "ABAAAQAAAAoAKShwcm9maWxlPUJBQUFBQUEzNk1IWDdvMk5UVGppRzg0UEFBQVNBQUk9wGMA"
    "AQABAAAACgAEwKgBgMBjABwAAQAAAAoAEP6AAAAAAAAAkvtd//4+TwM="
)


# --------------------------------------------------------------- query build

def test_build_query_is_ptr_for_service_type():
    q = MdnsService._build_query()
    qd = struct.unpack(">H", q[4:6])[0]
    assert qd == 1
    name, off = MdnsService._read_name(q, 12)
    assert name == "_miot-central._tcp.local"
    qtype, qclass = struct.unpack(">HH", q[off:off + 4])
    assert qtype == 12 and qclass == 1  # PTR / IN


# --------------------------------------------------------------- parse response

def test_parse_response_4399():
    svcs = MdnsService._parse_response(PKT_4399)
    assert len(svcs) == 1
    s = svcs[0]
    assert s["instance"].startswith("xiaomi-gateway-hub1-4399")
    assert s["host"] == "Android.local"
    assert s["port"] == 8883
    assert s["ip"] == "192.168.1.152"
    assert s["profile"] == "FAAAAABCEkg/Bowrf+uD8jsgAAARAAI="


def test_parse_response_0855():
    s = MdnsService._parse_response(PKT_0855)[0]
    assert s["host"] == "oh1p.local"
    assert s["port"] == 8883
    assert s["ip"] == "192.168.1.128"
    assert s["profile"] == "BAAAAAA36MHX7o2NTTjiG84PAAASAAI="


def test_parse_response_garbage_is_empty():
    assert MdnsService._parse_response(b"\x00\x01\x02") == []


# --------------------------------------------------------------- from_raw

def test_from_raw_parses_profile():
    s = MdnsService._parse_response(PKT_4399)[0]
    sd = MipsServiceData.from_raw(
        name=s["instance"], type_=MIPS_MDNS_TYPE, server=s["host"],
        addresses=[s["ip"]], port=s["port"], profile=s["profile"],
    )
    assert sd.group_id == "3bf283eb7f2b8c06"
    assert sd.role == 1 and sd.suite_mqtt is True and sd.valid_service()
    assert sd.did.isdigit()


def test_from_raw_rejects_bad_input():
    with pytest.raises(MdnsServiceError):
        MipsServiceData.from_raw(name="x", type_=MIPS_MDNS_TYPE, server="h",
                                 addresses=["1.2.3.4"], port=8883, profile="")
    with pytest.raises(MdnsServiceError):
        MipsServiceData.from_raw(name="x", type_=MIPS_MDNS_TYPE, server="h",
                                 addresses=[], port=8883,
                                 profile="FAAAAABCEkg/Bowrf+uD8jsgAAARAAI=")
    with pytest.raises(MdnsServiceError):
        MipsServiceData.from_raw(name="x", type_=MIPS_MDNS_TYPE, server="h",
                                 addresses=["1.2.3.4"], port=0,
                                 profile="FAAAAABCEkg/Bowrf+uD8jsgAAARAAI=")


@pytest.mark.parametrize("bad", ["abc", "a", "zz=", "AAAA="])
def test_from_raw_rejects_malformed_base64_as_domain_error(bad):
    """畸形 base64 必须转成 MdnsServiceError,不能让 binascii.Error 漏出去。

    解析跑在 add_reader 回调里,binascii.Error 逃出去没有接收方,还会让同一个包
    里后面的网关条目全部不被解析(表现为"局域网明明有中枢却一直发现不到")。
    """
    with pytest.raises(MdnsServiceError):
        MipsServiceData.from_raw(
            name="x", type_=MIPS_MDNS_TYPE, server="h",
            addresses=["1.2.3.4"], port=8883, profile=bad,
        )


# --------------------------------------------------------------- end-to-end ingest

@pytest.mark.asyncio
async def test_handle_packet_ingests_and_fires_added():
    m = MdnsService()
    seen = []

    async def on_change(group_id, state, data):
        seen.append((group_id, state, data))

    m.sub_service_change("t", "*", on_change)
    # feed a real packet through the private handler
    m._MdnsService__handle_packet(PKT_4399)
    import asyncio
    await asyncio.sleep(0)  # let the dispatched task run
    svcs = m.get_services()
    assert "3bf283eb7f2b8c06" in svcs
    assert svcs["3bf283eb7f2b8c06"]["addresses"] == ["192.168.1.152"]
    assert len(seen) == 1
    gid, state, data = seen[0]
    assert gid == "3bf283eb7f2b8c06" and state == MdnsServiceState.ADDED


class _NI:
    def __init__(self, name, ip, netmask="255.255.255.0"):
        self.name = name
        self.ip = ip
        self.netmask = netmask


class _FakeNetwork:
    def __init__(self, infos):
        self.network_info = {ni.name: ni for ni in infos}


@pytest.mark.asyncio
async def test_interface_addrs_filters_loopback_and_linklocal():
    net = _FakeNetwork([
        _NI("lo0", "127.0.0.1"),
        _NI("en1", "192.168.1.239"),
        _NI("bridge100", "192.168.64.1"),
        _NI("en9", "169.254.10.2"),
    ])
    m = MdnsService(network=net)
    addrs = m._MdnsService__interface_addrs()
    names = {n for n, _, _ in addrs}
    assert names == {"en1", "bridge100"}
    assert all(not ip.startswith(("127.", "169.254.")) for _, ip, _ in addrs)


@pytest.mark.asyncio
async def test_interface_addrs_empty_without_network():
    assert MdnsService()._MdnsService__interface_addrs() == []


def test_subnet_broadcast():
    sb = MdnsService._MdnsService__subnet_broadcast
    assert sb("192.168.1.239", "255.255.255.0") == "192.168.1.255"
    assert sb("10.0.5.7", "255.255.0.0") == "10.0.255.255"
    assert sb("bad", "255.255.255.0") is None


@pytest.mark.asyncio
async def test_handle_packet_dedup_no_duplicate_added():
    m = MdnsService()
    seen = []

    async def on_change(group_id, state, data):
        seen.append(state)

    m.sub_service_change("t", "*", on_change)
    m._MdnsService__handle_packet(PKT_0855)
    m._MdnsService__handle_packet(PKT_0855)  # identical → no second ADDED
    import asyncio
    await asyncio.sleep(0)
    assert seen == [MdnsServiceState.ADDED]


# ------------------------------------- 网卡 IP 变化 → per-NIC socket 必须重建


class _SubscribableNetwork(_FakeNetwork):
    """记录 register/unregister，并能把网卡表换成新地址后触发回调。"""

    def __init__(self, infos):
        super().__init__(infos)
        self.handlers: dict = {}

    async def register_info_changed_async(self, key, handler):
        self.handlers[key] = handler

    async def unregister_info_changed_async(self, key):
        self.handlers.pop(key, None)

    async def fire(self, infos):
        self.network_info = {ni.name: ni for ni in infos}
        for h in list(self.handlers.values()):
            await h(None, None)


@pytest.mark.asyncio
async def test_rebinds_sockets_on_network_change():
    """IP 变了必须重新 bind：旧 socket 绑在已消失的地址上，sendto 会一直
    EADDRNOTAVAIL，而那处失败只打 debug —— 表现为新网关永远发现不到、掉线的
    永远重连不上，且 status / doctor 看不出异常，只有重启后端才恢复。
    """
    net = _SubscribableNetwork([_NI("en1", "127.0.0.1")])  # 可 bind 的地址
    m = MdnsService(network=net)
    await m.init_async()
    try:
        assert "miot_mdns" in net.handlers  # 订阅上了
        first = dict(m._socks)
        assert first, "首轮应至少绑上一个网卡"
        # 127.0.0.1 会被 __interface_addrs 过滤掉 → 走 default fallback socket 分支。
        # 明确断出来,免得读者以为这条覆盖的是 per-NIC(IP_BOUND_IF / SO_BINDTODEVICE)
        # 路径:那条路径需要真网卡地址,单测环境里造不出来。
        assert list(first.keys()) == ["default"]
        old_sock = next(iter(first.values()))[0]
        # 预先塞一条发现记录,好让下面"保留不清"的断言真的有东西可断
        # (原来断 `_services == {}`,全程没喂过应答包,恒真 → 有人在重建里顺手清空
        # _services 也照样绿)。
        m._services["g-existing"] = {"addresses": ["10.0.0.9"], "port": 8883}

        await net.fire([_NI("en1", "127.0.0.1")])  # 触发变化

        assert m._socks, "重建后仍应有 socket"
        new_sock = next(iter(m._socks.values()))[0]
        assert new_sock is not old_sock, "socket 必须是新建的，不能复用旧的"
        assert old_sock.fileno() == -1, "旧 socket 必须已关闭"
        # 发现结果不因重建而清空（网关多半还在，清掉只会让 ADDED 白重放一轮）
        assert "g-existing" in m._services
    finally:
        await m.deinit_async()


@pytest.mark.asyncio
async def test_deinit_unsubscribes_network_change():
    net = _SubscribableNetwork([_NI("en1", "127.0.0.1")])
    m = MdnsService(network=net)
    await m.init_async()
    assert "miot_mdns" in net.handlers
    await m.deinit_async()
    assert "miot_mdns" not in net.handlers  # 不留悬挂回调
    assert m._socks == {}


@pytest.mark.asyncio
async def test_init_survives_subscribe_failure():
    """订阅失败只降级掉自愈能力，不该让发现整体起不来。"""

    class _BadNetwork(_FakeNetwork):
        async def register_info_changed_async(self, key, handler):
            raise RuntimeError("no subscription support")

    m = MdnsService(network=_BadNetwork([_NI("en1", "127.0.0.1")]))
    await m.init_async()
    try:
        assert m._socks, "订阅失败仍应完成 socket 绑定"
    finally:
        await m.deinit_async()


@pytest.mark.asyncio
async def test_network_change_after_deinit_does_not_reopen_sockets():
    """deinit 之后到达的网卡变化回调不得重建 socket。

    MIoTNetwork 用 create_task 分发回调,即回调是**独立任务**入队的:
    「网卡变化入队 → deinit 跑完(unregister 只是 dict pop,拦不住已入队的任务)→
    回调才被调度」时,没有拆卸标志的话会重新打开每块网卡的 socket 并挂回事件循环,
    deinit 返回后 _socks 非空、fd 与 reader 永久泄漏,收到应答还往已清空的 _services
    里写。网络切换/睡眠唤醒紧接着进程退出,在 LaunchAgent 场景正是典型组合。
    """
    net = _SubscribableNetwork([_NI("en1", "127.0.0.1")])
    m = MdnsService(network=net)
    await m.init_async()
    handler = net.handlers["miot_mdns"]
    await m.deinit_async()
    assert m._socks == {}

    await handler(None, None)  # 模拟 deinit 之后才被调度的那个 task

    assert m._socks == {}, "拆卸后不得重建 socket"
