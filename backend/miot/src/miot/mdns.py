# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
mDNS discovery for MIoT central hub gateways (``_miot-central._tcp``).

Uses RFC 6762 §6.7 "legacy unicast" queries: the service PTR query is sent
from an *ephemeral* UDP source port and responders reply **unicast** back to
that port. Because we never bind port 5353 we don't contend with a system
mDNS responder — notably macOS ``mDNSResponder`` owns 5353 and starves any
second listener (python-zeroconf receives nothing there, verified on-device),
whereas this pure-stdlib socket approach works on both Linux and macOS with
one code path and no subprocess.

The query is sent as a subnet-directed BROADCAST (not to the 224.0.0.251
multicast group) — see ``__send_query`` for why. A single PTR query returns
the whole record set (PTR + SRV + TXT + A in the additional section), so one
round-trip per gateway yields instance / host / port / IPv4 / profile with no
follow-up lookups. Discovery is poll-based (periodic re-query); service
*removals* are ignored — the local MQTT connection closes on its own (matches
the Xiaomi Home integration behavior).
"""

import asyncio
import base64
import binascii
import copy
import logging
import socket
import struct
import sys
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from .const import MIPS_MDNS_TYPE

_LOGGER = logging.getLogger(__name__)

# Query name on the wire (no trailing dot).
_MDNS_QUERY_NAME = "_miot-central._tcp.local"
_MDNS_PORT = 5353
# The query is sent as a subnet-directed BROADCAST (not the 224.0.0.251
# multicast): mDNSResponder / Avahi bind 0.0.0.0:5353 and answer a broadcast
# query legacy-unicast just the same, and — unlike multicast — a broadcast can
# be pinned to a specific interface on macOS (IP_BOUND_IF works for broadcast;
# IP_MULTICAST_IF fails with "No route to host"). This mirrors lan.py's
# per-interface SO_BROADCAST fan-out.
_MDNS_BROADCAST_FALLBACK = "255.255.255.255"
# Re-query cadence. Gateways are semi-static; a new one is found within one
# interval. Well under the legacy-unicast response-rate limits in RFC 6762.
_MDNS_POLL_INTERVAL_S = 15.0
_MDNS_RECV_BUF = 9000

# DNS record types.
_T_A = 1
_T_PTR = 12
_T_TXT = 16
_T_SRV = 33


class MdnsServiceError(Exception):
    """mDNS service error."""

    code: int
    message: str

    def __init__(self, message: str, code: int = -1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:
        return f"MdnsServiceError: {self.code}, {self.message}"


class MdnsServiceState(str, Enum):
    """mDNS service state."""

    ADDED = "added"
    REMOVED = "removed"
    UPDATED = "updated"


class MipsServiceData:
    """Parsed central hub gateway service (from an mDNS response)."""

    profile: str
    profile_bin: bytes

    name: str
    addresses: List[str]
    port: int
    type: str
    server: str

    did: str
    group_id: str
    role: int
    suite_mqtt: bool

    @classmethod
    def from_raw(
        cls,
        *,
        name: str,
        type_: str,
        server: str,
        addresses: List[str],
        port: int,
        profile: str,
    ) -> "MipsServiceData":
        """Build from resolved fields (instance name, SRV host/port, A address,
        TXT profile)."""
        self = cls()
        if not profile:
            raise MdnsServiceError("invalid service profile")
        self.profile = profile
        # b64decode(validate=False) 只忽略字母表外字符,填充长度不对仍抛
        # binascii.Error(如 profile="abc")。这里的输入是任何能往发现端口发 UDP 的
        # 主机都能构造的字节流,而调用链的顶端是 add_reader 回调——异常逃出去只会
        # 进事件循环的 exception handler,没有业务侧接收方,同一个包里排在后面的
        # 网关条目会全部不被解析。就地转成本模块的领域异常,让外层 skip 分支接住,
        # 这样一条坏记录只丢它自己。
        try:
            self.profile_bin = base64.b64decode(profile)
        except (binascii.Error, ValueError) as e:
            raise MdnsServiceError(f"invalid base64 profile: {profile!r}") from e
        addrs = sorted(a for a in (addresses or []) if a)
        if not addrs:
            raise MdnsServiceError("invalid addresses")
        if not port:
            raise MdnsServiceError("invalid port")
        self.name = name
        self.addresses = addrs
        self.port = int(port)
        self.type = type_
        self.server = server or ""
        self.__parse_profile()
        return self

    def __parse_profile(self) -> None:
        if len(self.profile_bin) < 23:
            raise MdnsServiceError("invalid profile length")
        self.did = str(int.from_bytes(self.profile_bin[1:9], byteorder="big"))
        self.group_id = binascii.hexlify(self.profile_bin[9:17][::-1]).decode("utf-8")
        self.role = int(self.profile_bin[20] >> 4)
        self.suite_mqtt = ((self.profile_bin[22] >> 1) & 0x01) == 0x01

    def valid_service(self) -> bool:
        if self.role != 1:
            return False
        return self.suite_mqtt

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "addresses": self.addresses,
            "port": self.port,
            "type": self.type,
            "server": self.server,
            "did": self.did,
            "group_id": self.group_id,
            "role": self.role,
            "suite_mqtt": self.suite_mqtt,
        }

    def __str__(self) -> str:
        return str(self.to_dict())


class MdnsService:
    """Legacy-unicast mDNS discovery for the central hub gateway type."""

    _main_loop: asyncio.AbstractEventLoop
    # group_id -> service data dict (see MipsServiceData.to_dict)
    _services: Dict[str, dict]
    # (key, group_id) -> handler(group_id, state, data). group_id may be "*".
    _sub_list: Dict[Tuple[str, str], Callable[[str, "MdnsServiceState", dict], Coroutine]]

    def __init__(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        network: Any = None,
    ) -> None:
        self._main_loop = loop or asyncio.get_running_loop()
        self._services = {}
        self._sub_list = {}
        # Interface source: the same MIoTNetwork lan.py uses. Its network_info
        # gives the *active* interfaces with their IPv4 + netmask (not the dozens
        # of virtual/down interfaces socket.if_nameindex() would list). We open
        # one SO_BROADCAST socket per interface, pin it to that interface, and
        # send the query to the interface's subnet-directed broadcast address —
        # a multi-homed host (e.g. a LAN NIC alongside a VM bridge on a different
        # subnet) would otherwise emit out the wrong interface and hear nothing.
        self._network = network
        # label -> (socket, broadcast_target_ip)
        self._socks: Dict[str, Tuple[socket.socket, str]] = {}
        self._poll_task: Optional[asyncio.Task] = None
        # 拆卸标志:网卡变化回调是独立 task,deinit 之后仍可能被调度并重建 socket。
        self._deinited: bool = False

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.deinit_async()

    async def init_async(self) -> None:
        """Open a send/recv socket per interface and start the poll loop."""
        # 复位拆卸标志:当前调用方(central_hub)每次 init 都新建实例、deinit 即丢弃,
        # 所以这行今天是防御性的;不写的话,将来有人改成复用同一实例,重建 socket 的
        # 自愈能力会静默失效(表现为 IP 变化后再也发现不到网关)。
        self._deinited = False
        self.__bind_sockets()
        self._poll_task = self._main_loop.create_task(self.__poll_loop())
        # 订阅网卡变化,与 lan.py 的 "miot_lan" 订阅同源。socket 是 bind 到具体网卡
        # IP 的,Wi-Fi 换频段 / DHCP 续租换地址 / 插拔网线之后,旧 socket 还绑在已经
        # 不存在的地址上,每次 sendto 都 EADDRNOTAVAIL(macOS: Errno 49),而那两处失败
        # 只打 debug、默认级别一行都看不到 —— 表现为新网关永远发现不到、掉线的永远
        # 重连不上(重连扫描依赖发现结果),控制全量退回云端却没有任何可见症状,只有重启
        # 后端才恢复。故变化时整批重建。
        if self._network is not None:
            try:
                await self._network.register_info_changed_async(
                    key="miot_mdns",
                    handler=self.__on_network_info_change_async,
                )
            except Exception as e:
                # 订阅不上不该让发现整体起不来:静态网关与首轮发现仍可用,只是失去
                # IP 变化后的自愈能力。
                _LOGGER.warning("mdns: subscribe network changes failed: %s", e)

    def __bind_sockets(self) -> None:
        """(重)建 per-NIC socket 并挂上 reader。"""
        self._socks = self.__open_interface_sockets()
        for sock, _target in self._socks.values():
            self._main_loop.add_reader(sock.fileno(), self.__on_readable, sock)
        _LOGGER.info(
            "mdns: legacy-unicast discovery started on: %s",
            ", ".join(self._socks.keys()) or "<none>",
        )

    def __close_sockets(self) -> None:
        socks, self._socks = self._socks, {}
        for sock, _target in socks.values():
            try:
                self._main_loop.remove_reader(sock.fileno())
            except Exception:
                pass  # reader may already be gone / loop closing; best-effort cleanup
            try:
                sock.close()
            except Exception:
                pass  # socket may already be closed; best-effort cleanup

    async def __on_network_info_change_async(self, *args: Any, **kwargs: Any) -> None:
        """网卡增删 / IP 变化 → 整批重建 socket,绑到当前生效的地址上。

        故意不做差量(只重建变化的那块):网卡标签与地址的对应关系本身就是这次变化的
        内容,差量判断要依赖变化前的快照,而重建整批的代价只是几个 UDP socket。
        发现结果 ``_services`` 保留不清:网关多半还在,清掉只会让 ADDED 白重放一轮。
        """
        if self._deinited:
            # MIoTNetwork 用 create_task 分发回调,即本协程是**独立任务**入队的:
            # 「网卡变化入队 → deinit 跑完(unregister 只是 dict pop,拦不住已入队的
            # 任务)→ 本协程才被调度」时,下面两句会在拆卸之后重新打开每块网卡的
            # socket 并挂回事件循环,fd 与 reader 永久泄漏。网络切换/睡眠唤醒紧接着
            # 进程退出,在 LaunchAgent 场景正是典型组合。
            _LOGGER.debug("mdns: network info changed after deinit, ignore")
            return
        _LOGGER.info("mdns: network info changed → rebinding per-NIC sockets")
        self.__close_sockets()
        self.__bind_sockets()

    async def deinit_async(self) -> None:
        """Stop discovery and clear state."""
        self._deinited = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self._network is not None:
            try:
                await self._network.unregister_info_changed_async(key="miot_mdns")
            except Exception:
                pass  # 未订阅成功 / 无此 API：best-effort，不阻塞拆卸
        self.__close_sockets()
        self._services = {}
        self._sub_list = {}

    def __open_interface_sockets(self) -> Dict[str, Tuple[socket.socket, str]]:
        """One SO_BROADCAST socket per active interface (from MIoTNetwork),
        pinned to that interface, targeting its subnet-directed broadcast.
        Falls back to a single 255.255.255.255 socket when no interface list is
        available."""
        socks: Dict[str, Tuple[socket.socket, str]] = {}
        for name, ip, netmask in self.__interface_addrs():
            target = self.__subnet_broadcast(ip, netmask) or _MDNS_BROADCAST_FALLBACK
            sock = self.__make_socket(name, ip)
            if sock is not None:
                socks[f"{name}({ip})->{target}"] = (sock, target)
        if not socks:  # no MIoTNetwork / empty → global broadcast, default iface
            sock = self.__make_socket(None)
            if sock is not None:
                socks["default"] = (sock, _MDNS_BROADCAST_FALLBACK)
        return socks

    def __interface_addrs(self) -> List[Tuple[str, str, str]]:
        """(name, ipv4, netmask) for each active interface, from
        MIoTNetwork.network_info (the same source lan.py uses). Loopback /
        link-local are skipped."""
        out: List[Tuple[str, str, str]] = []
        net = self._network
        if net is None:
            return out
        try:
            infos = list(net.network_info.values())
        except Exception as e:
            _LOGGER.debug("mdns: read network_info failed: %s", e)
            return out
        for info in infos:
            ip = getattr(info, "ip", None)
            name = getattr(info, "name", "?")
            netmask = getattr(info, "netmask", "") or ""
            if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                continue
            out.append((name, ip, netmask))
        return out

    @staticmethod
    def __subnet_broadcast(ip: str, netmask: str) -> Optional[str]:
        """Directed broadcast address for an interface: ip | ~netmask."""
        try:
            ip_i = struct.unpack(">I", socket.inet_aton(ip))[0]
            mask_i = struct.unpack(">I", socket.inet_aton(netmask))[0]
            bcast = ip_i | (~mask_i & 0xFFFFFFFF)
            return socket.inet_ntoa(struct.pack(">I", bcast))
        except Exception:
            return None

    def __make_socket(
        self, ifname: Optional[str], bind_ip: Optional[str] = None
    ) -> Optional[socket.socket]:
        try:
            sock = socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
            )
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if ifname:
                self.__bind_to_interface(sock, ifname)
            sock.setblocking(False)
            # Bind to this NIC's own IP with an ephemeral source port so
            # legacy-unicast responses return to us; binding a specific IP (not
            # all interfaces) also scopes the socket to that NIC. The
            # interface-less fallback (no bind_ip) auto-binds on the first sendto.
            if bind_ip:
                sock.bind((bind_ip, 0))
            return sock
        except Exception as e:
            _LOGGER.debug("mdns: skip interface %s: %s", ifname, e)
            try:
                sock.close()
            except Exception:
                pass  # best-effort close of a half-initialised socket; irrelevant
            return None

    @staticmethod
    def __bind_to_interface(sock: socket.socket, ifname: str) -> None:
        """Pin the socket to an interface (broadcast egress). macOS IP_BOUND_IF
        by index / Linux SO_BINDTODEVICE by name — same as lan.py."""
        if sys.platform == "darwin":
            ip_bound_if = getattr(socket, "IP_BOUND_IF", 25)
            sock.setsockopt(socket.IPPROTO_IP, ip_bound_if, socket.if_nametoindex(ifname))
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, ifname.encode())

    def get_services(self, group_id: Optional[str] = None) -> Dict[str, dict]:
        """Return discovered central hub gateway services, keyed by group_id."""
        if group_id:
            if group_id not in self._services:
                return {}
            return {group_id: copy.deepcopy(self._services[group_id])}
        return copy.deepcopy(self._services)

    def sub_service_change(
        self,
        key: str,
        group_id: str,
        handler: Callable[[str, "MdnsServiceState", dict], Coroutine],
    ) -> None:
        """Subscribe to service changes for a group_id ("*" for all)."""
        if key is None or group_id is None or handler is None:
            raise MdnsServiceError("invalid params")
        self._sub_list[(key, group_id)] = handler

    def unsub_service_change(self, key: str) -> None:
        """Remove all subscriptions registered under key."""
        if key is None:
            return
        for keys in list(self._sub_list.keys()):
            if key == keys[0]:
                self._sub_list.pop(keys, None)

    # ------------------------------------------------------------- internals

    async def __poll_loop(self) -> None:
        try:
            # A short initial burst discovers quickly on startup; then settle
            # into the steady poll cadence.
            for _ in range(3):
                self.__send_query()
                await asyncio.sleep(1.0)
            while True:
                self.__send_query()
                await asyncio.sleep(_MDNS_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.warning("mdns: poll loop error: %s", e)

    def __send_query(self) -> None:
        query = self._build_query()
        for label, (sock, target) in list(self._socks.items()):
            try:
                sock.sendto(query, (target, _MDNS_PORT))
            except Exception as e:
                # Subnet-directed broadcast can be refused on some setups; fall
                # back to the global broadcast (mirrors lan.py).
                _LOGGER.debug("mdns: send on %s (%s) failed: %s", label, target, e)
                if target != _MDNS_BROADCAST_FALLBACK:
                    try:
                        sock.sendto(query, (_MDNS_BROADCAST_FALLBACK, _MDNS_PORT))
                    except Exception as e2:
                        _LOGGER.debug("mdns: fallback send on %s failed: %s", label, e2)

    def __on_readable(self, sock: socket.socket) -> None:
        while True:
            try:
                data, _addr = sock.recvfrom(_MDNS_RECV_BUF)
            except (BlockingIOError, InterruptedError):
                return
            except Exception as e:
                _LOGGER.debug("mdns: recv error: %s", e)
                return
            if not data:
                return
            self.__handle_packet(data)

    def __handle_packet(self, data: bytes) -> None:
        try:
            services = self._parse_response(data)
        except Exception as e:
            _LOGGER.debug("mdns: parse failed: %s", e)
            return
        for svc in services:
            try:
                service_data = MipsServiceData.from_raw(
                    name=svc["instance"],
                    type_=MIPS_MDNS_TYPE,
                    server=svc.get("host", ""),
                    addresses=[svc["ip"]] if svc.get("ip") else [],
                    port=svc.get("port", 0),
                    profile=svc.get("profile", ""),
                )
                self.__ingest_service_data(service_data)
            except MdnsServiceError as e:
                _LOGGER.debug("mdns: skip service %s: %s", svc.get("instance"), e)
            except Exception as e:
                # 这个循环跑在 add_reader 回调里:异常逃出去没有接收方,且会让同一
                # 个包里后面的网关条目全部不被解析。兜住并继续下一条,防止将来新增
                # 解析逻辑时再捅穿回调边界。
                _LOGGER.warning(
                    "mdns: unexpected error on service %s: %s",
                    svc.get("instance"), e,
                )

    def __ingest_service_data(self, service_data: "MipsServiceData") -> None:
        """Merge a resolved service into the table and fire ADDED/UPDATED.

        Only the main gateway (role=1) advertising the mqtt suite is
        connectable; others raise MdnsServiceError and are dropped.
        """
        if not service_data.valid_service():
            raise MdnsServiceError("no primary role, no support mqtt connection")
        group_id = service_data.group_id
        if group_id in self._services:
            buffer_data = self._services[group_id]
            if (
                service_data.did != buffer_data["did"]
                or service_data.addresses != buffer_data["addresses"]
                or service_data.port != buffer_data["port"]
            ):
                self._services[group_id].update(service_data.to_dict())
                self.__call_service_change(
                    MdnsServiceState.UPDATED, self._services[group_id]
                )
        else:
            self._services[group_id] = service_data.to_dict()
            self.__call_service_change(MdnsServiceState.ADDED, self._services[group_id])

    def __call_service_change(self, state: "MdnsServiceState", data: dict) -> None:
        _LOGGER.info("call service change, %s, %s", state, data)
        for keys in list(self._sub_list.keys()):
            if keys[1] in (data.get("group_id"), "*"):
                self._main_loop.create_task(
                    self._sub_list[keys](data["group_id"], state, data)
                )

    # ---------------------------------------------------- DNS wire helpers

    @staticmethod
    def _build_query() -> bytes:
        """A single-question PTR query for the central hub service type.

        Transaction id 0, standard query. Sent from an ephemeral source port so
        responders reply unicast (RFC 6762 §6.7)."""
        header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
        qname = b"".join(
            bytes([len(lbl)]) + lbl
            for lbl in _MDNS_QUERY_NAME.encode("utf-8").split(b".")
        ) + b"\x00"
        return header + qname + struct.pack(">HH", _T_PTR, 1)  # PTR, class IN

    @staticmethod
    def _read_name(data: bytes, off: int) -> Tuple[str, int]:
        """Read a (possibly compressed) DNS name. Returns (name, next_offset)."""
        labels: List[bytes] = []
        jumped = False
        next_off = off
        jumps = 0
        while True:
            if jumps > 128:
                raise ValueError("DNS name compression loop detected")
            length = data[off]
            if length & 0xC0 == 0xC0:  # compression pointer
                ptr = struct.unpack(">H", data[off:off + 2])[0] & 0x3FFF
                if not jumped:
                    next_off = off + 2
                off = ptr
                jumped = True
                jumps += 1
                continue
            off += 1
            if length == 0:
                break
            labels.append(data[off:off + length])
            off += length
        if not jumped:
            next_off = off
        return b".".join(labels).decode("utf-8", "ignore"), next_off

    @classmethod
    def _parse_response(cls, data: bytes) -> List[dict]:
        """Parse an mDNS response into a list of service dicts
        ``{instance, host, port, ip, profile}`` (one per SRV record)."""
        if len(data) < 12:
            return []
        qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
        off = 12
        for _ in range(qd):  # skip questions
            _, off = cls._read_name(data, off)
            off += 4
        srv: Dict[str, Tuple[str, int]] = {}   # owner -> (host, port)
        txt: Dict[str, str] = {}               # owner -> profile
        addrs: Dict[str, str] = {}             # host -> ipv4
        for _ in range(an + ns + ar):
            name, off = cls._read_name(data, off)
            if off + 10 > len(data):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
            off += 10
            rdata = data[off:off + rdlen]
            rdstart = off
            off += rdlen
            if rtype == _T_SRV and rdlen >= 6:
                port = struct.unpack(">H", rdata[4:6])[0]
                target, _ = cls._read_name(data, rdstart + 6)
                srv[name] = (target, port)
            elif rtype == _T_TXT:
                i = 0
                while i < len(rdata):
                    ln = rdata[i]
                    i += 1
                    chunk = rdata[i:i + ln].decode("utf-8", "ignore")
                    i += ln
                    if chunk.startswith("profile="):
                        txt[name] = chunk[len("profile="):]
            elif rtype == _T_A and rdlen == 4:
                addrs[name] = ".".join(str(b) for b in rdata)
        out: List[dict] = []
        for owner, (host, port) in srv.items():
            out.append(
                {
                    "instance": owner,
                    "host": host,
                    "port": port,
                    "ip": addrs.get(host),
                    "profile": txt.get(owner),
                }
            )
        return out
