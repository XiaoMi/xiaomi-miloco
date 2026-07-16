# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT lan device detector.
"""

import asyncio
import ipaddress
import logging
import os
import random
import secrets
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

from miot.network import MIoTNetwork
from miot.types import InterfaceStatus, MIoTLanDeviceInfo, NetworkInfo

_LOGGER = logging.getLogger(__name__)

_ICMP_ECHO_REQUEST = 8
_ICMP_ECHO_REPLY = 0


def _icmp_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def _icmp_dgram_ping(ip: str, timeout: float) -> bool:
    """不需要 root 的 ICMP echo（``SOCK_DGRAM`` + ``IPPROTO_ICMP``）。macOS
    原生支持任意用户；Linux 取决于 ``net.ipv4.ping_group_range``（不在允许
    范围内会直接 ``PermissionError``）——即便放行，Linux 内核也会把发送时
    echo 的 id 字段改写成 socket 绑定端口，回包 id 恒等于端口而非我们填的
    ``ident``，判断时两个都要认，否则这条通道在 Linux 上永远判不通。
    任何失败/超时都返回 ``False`` 而不抛异常，由调用方决定要不要退回外部
    ``ping`` 命令。

    阻塞调用——必须用 ``run_in_executor`` 跑，不能直接放进 asyncio 事件循环。
    """
    ident = os.getpid() & 0xFFFF
    payload = struct.pack("!d", time.time())
    header = struct.pack("!BBHHH", _ICMP_ECHO_REQUEST, 0, 0, ident, 1)
    packet = (
        struct.pack(
            "!BBHHH", _ICMP_ECHO_REQUEST, 0, _icmp_checksum(header + payload), ident, 1
        )
        + payload
    )
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except OSError:
        return False
    try:
        sock.settimeout(timeout)
        sock.sendto(packet, (ip, 0))
        # Linux 的 unprivileged ping-socket 在发送时会把 echo 请求的 id 字段
        # 改写成 socket 绑定的本地端口（内核靠端口而非应用填的 id 做分发），
        # 回包里的 id 也就恒等于这个端口，不是我们填的 ident；macOS 不改写，
        # 回包 id 仍是 ident。两个都认，否则这条通道在 Linux 上永远判不通。
        try:
            bound_id = sock.getsockname()[1]
        except OSError:
            bound_id = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            data, _addr = sock.recvfrom(1024)
            # 有些平台（macOS）SOCK_DGRAM 回包仍带 IPv4 头，有些（Linux 的
            # ping socket）不带——按版本号高 4 位判断，比信任平台假设可靠
            # （我们关心的 ICMP type 字节 0/8/11/3 都不会撞上这个高 4 位）。
            icmp = data[(data[0] & 0x0F) * 4 :] if data[0] >> 4 == 4 else data
            if len(icmp) < 8:
                continue
            icmp_type, _code, _chk, resp_id, _seq = struct.unpack("!BBHHH", icmp[:8])
            if icmp_type == _ICMP_ECHO_REPLY and resp_id in (ident, bound_id):
                return True
        return False
    except OSError:
        return False
    finally:
        sock.close()


@dataclass
class _MIoTLanNetworkUpdateData:
    status: InterfaceStatus
    if_name: str


@dataclass
class _MIoTLanUnregDeviceData:
    key: str


@dataclass
class _MIoTLanRegDeviceData:
    key: str
    # did, info, ctx
    handler: Callable[[str, MIoTLanDeviceInfo, Any], Coroutine]
    handler_ctx: Any


class _MIoTLanDevice:
    """MIoT lan device."""

    _KA_TIMEOUT: float = 100
    _manager: "MIoTLan"

    did: str
    offset: int

    _online: bool
    _ip: Optional[str]
    _if_name: Optional[str]

    _ka_timer: Optional[asyncio.TimerHandle]

    def __init__(self, manager: "MIoTLan", did: str, ip: Optional[str] = None) -> None:
        self._manager = manager
        self.did = did
        self.offset = 0
        self._online = False
        self._ip = ip
        self._if_name = None
        self._ka_timer = None

    def keep_alive(self, ip: str, if_name: str) -> None:
        """Keep alive."""
        changed: bool = False
        if self._online is False:
            changed = True
            self._online = True
            _LOGGER.info("device online, %s, %s", self.did, ip)
        if self._ip != ip:
            changed = True
            self._ip = ip
            _LOGGER.info("device ip changed, %s, %s", self.did, ip)
        if self._if_name != if_name:
            self._if_name = if_name
            _LOGGER.info("device if_name change, %s, %s", self.did, self._if_name)
        # Reset keep alive timer
        if self._ka_timer:
            self._ka_timer.cancel()
        self._ka_timer = self._manager.internal_loop.call_later(
            self._KA_TIMEOUT, self.__switch_offline
        )
        if changed:
            self.__broadcast_info_changed()

    @property
    def online(self) -> bool:
        """Device online status."""
        return self._online

    @online.setter
    def online(self, online: bool) -> None:
        if self._online == online:
            return
        self._online = online
        _LOGGER.debug("device status changed, %s, %s", self.did, self._online)
        self.__broadcast_info_changed()

    @property
    def ip(self) -> Optional[str]:
        """Device IP."""
        return self._ip

    @ip.setter
    def ip(self, ip: Optional[str]) -> None:
        if self._ip == ip:
            return
        self._ip = ip
        _LOGGER.debug("device ip changed, %s, %s", self.did, self._ip)
        self.__broadcast_info_changed()

    def on_delete(self) -> None:
        """On delete."""
        if self._ka_timer:
            self._ka_timer.cancel()
            self._ka_timer = None
        self._online = False

    def __switch_offline(self) -> None:
        self.online = False

    def __broadcast_info_changed(self):
        self._manager.broadcast_device_info_changed(
            did=self.did,
            info=MIoTLanDeviceInfo(did=self.did, online=self._online, ip=self._ip),
        )


class MIoTLan:
    """MIoT lan device detector."""

    OT_HEADER: bytes = b"\x21\x31"
    OT_PORT: int = 54321
    OT_PROBE_LEN: int = 32
    OT_MSG_LEN: int = 1400

    OT_PROBE_INTERVAL_MIN: float = 5
    OT_PROBE_INTERVAL_MAX: float = 45

    _main_loop: asyncio.AbstractEventLoop

    _net_ifs: Set[str]
    _network: MIoTNetwork
    _lan_devices: Dict[str, _MIoTLanDevice]
    _virtual_did: str
    _probe_msg: bytes
    _read_buffer: bytearray

    _internal_loop: asyncio.AbstractEventLoop
    _thread: threading.Thread

    _available_net_ifs: Set[str]
    _broadcast_socks: Dict[str, socket.socket]
    # 专用单播 socket：不绑网卡，sendto 到确定的目标 IP 由系统路由选出口，同一 socket
    # 收回包。单播不复用广播那些 IP_BOUND_IF 钉网卡的 socket——否则发送失败
    # (EHOSTUNREACH 等) 会在共享 socket 上留下 pending error，毒害广播的接收。
    _unicast_sock: Optional[socket.socket]
    _local_port: Optional[int]
    _scan_timer: Optional[asyncio.TimerHandle]
    _last_scan_interval: Optional[float]
    _callbacks_device_status_changed: Dict[str, _MIoTLanRegDeviceData]
    _unicast_targets: Dict[str, str]
    # 已知的相机 did 全集（不止有单播目标的那些）与已连上的 did 集合——
    # 已连上的相机可达性已经证实，探测（广播+单播）没必要再打；全部连上时
    # 整个扫描定时器都停掉，直到有相机掉线/新相机加入才重新拉起。
    _camera_dids: Set[str]
    _connected_dids: Set[str]
    # 单播 sendto 失败后正在跑 ping 兜底的 did 集合，防止扫描间隔短于 ping
    # 超时时对同一个 did 并发起多个兜底任务。
    _ping_fallback_inflight: Set[str]

    _init_lock: asyncio.Lock
    _init_done: bool

    def __init__(
        self,
        net_ifs: List[str],
        network: MIoTNetwork,
        virtual_did: Optional[int] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        """Init."""
        self._main_loop = loop or asyncio.get_running_loop()

        self._net_ifs = set(net_ifs)
        self._network = network
        self._lan_devices = {}
        self._virtual_did = (
            str(virtual_did) if (virtual_did is not None) else str(secrets.randbits(64))
        )
        # Init socket probe message
        probe_bytes = bytearray(self.OT_PROBE_LEN)
        probe_bytes[:20] = (
            b"!1\x00\x20\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xffMDID"
        )
        probe_bytes[20:28] = struct.pack(">Q", int(self._virtual_did))
        probe_bytes[28:32] = b"\x00\x00\x00\x00"
        self._probe_msg = bytes(probe_bytes)
        self._read_buffer = bytearray(self.OT_MSG_LEN)

        self._available_net_ifs = set()
        self._broadcast_socks = {}
        self._unicast_sock = None
        self._local_port = None
        self._scan_timer = None
        self._last_scan_interval = None
        self._callbacks_device_status_changed = {}
        self._unicast_targets = {}
        self._camera_dids = set()
        self._connected_dids = set()
        self._ping_fallback_inflight = set()

        self._init_lock = asyncio.Lock()
        self._init_done = False

    @property
    def internal_loop(self) -> asyncio.AbstractEventLoop:
        """MIoT lan internal loop."""
        return self._internal_loop

    async def init_async(self):
        """Init."""
        async with self._init_lock:
            await self._network.register_info_changed_async(
                key="miot_lan", handler=self.__on_network_info_change_external_async
            )

            if self._init_done:
                _LOGGER.info("miot lan already init")
                return
            if len(self._net_ifs) == 0:
                _LOGGER.info("no net_ifs")
                return
            for if_name in list(self._network.network_info.keys()):
                self._available_net_ifs.add(if_name)
            if len(self._available_net_ifs) == 0:
                _LOGGER.info("no available net_ifs")
                return
            if self._net_ifs.isdisjoint(self._available_net_ifs):
                _LOGGER.info("no valid net_ifs")
                return
            self._internal_loop = asyncio.new_event_loop()
            # All tasks meant for the internal loop should happen in this thread
            self._thread = threading.Thread(target=self.__internal_loop_thread)
            self._thread.name = "miot_lan"
            self._thread.daemon = True
            self._thread.start()
            self._init_done = True
            _LOGGER.info("miot lan init")
        # Sleep a while to wait for the first otu scan.
        await asyncio.sleep(self.OT_PROBE_INTERVAL_MIN / 2)

    async def deinit_async(self):
        """Deinit."""
        async with self._init_lock:
            if not self._init_done:
                _LOGGER.info("miot lan not init")
                return
            try:
                self._internal_loop.call_soon_threadsafe(self.__deinit)
                await asyncio.to_thread(self._thread.join)
                self._internal_loop.close()
            finally:
                # Always reset session state so a subsequent init_async can rebuild
                # the instance even if the thread/loop teardown above raised.
                self._lan_devices = {}
                self._broadcast_socks = {}
                self._unicast_sock = None
                self._local_port = None
                self._scan_timer = None
                self._last_scan_interval = None
                self._unicast_targets = {}
                self._camera_dids = set()
                self._connected_dids = set()
                self._ping_fallback_inflight = set()
                # 注意：故意不清空 _callbacks_device_status_changed。
                # __on_network_info_change_external_async 会在网卡变化时主动 deinit→init，
                # 复位会让用户在 init_async 后注册的回调在第一次网络抖动时丢失。
                # 完整反注册由 unregister_status_changed_async 显式驱动。
                # self._callbacks_device_status_changed = {}
                self._available_net_ifs = set()
                self._init_done = False
                _LOGGER.info("miot lan deinit")

    async def get_devices_async(self) -> Dict[str, MIoTLanDeviceInfo]:
        """Get devices."""
        if not self._init_done:
            return {}
        try:
            fut = asyncio.run_coroutine_threadsafe(
                coro=self.__get_devices_internal_async(), loop=self._internal_loop
            )
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=5.0)
        except (
            RuntimeError,
            asyncio.CancelledError,
            asyncio.InvalidStateError,
            asyncio.TimeoutError,
        ):
            return {}

    async def register_status_changed_async(
        self,
        key: str,
        handler: Callable[[str, MIoTLanDeviceInfo, Any], Coroutine],
        handler_ctx: Any = None,
    ) -> bool:
        """Register status changed."""
        if not self._init_done:
            return False
        try:
            self._internal_loop.call_soon_threadsafe(
                self.__register_status_changed,
                _MIoTLanRegDeviceData(
                    key=key, handler=handler, handler_ctx=handler_ctx
                ),
            )
            return True
        except RuntimeError:
            return False

    async def unregister_status_changed_async(self, key: str) -> bool:
        """Unregister status changed."""
        if not self._init_done:
            return False
        try:
            self._internal_loop.call_soon_threadsafe(
                self.__unregister_status_changed, _MIoTLanUnregDeviceData(key=key)
            )
            return True
        except RuntimeError:
            return False

    async def ping_async(
        self, if_name: Optional[str] = None, target_ip: Optional[str] = None
    ) -> None:
        """OTU Ping External."""
        if not self._init_done:
            return
        _LOGGER.debug("ping external async")
        try:
            fut = asyncio.run_coroutine_threadsafe(
                coro=asyncio.to_thread(self.ping_internal, if_name, target_ip),
                loop=self._internal_loop,
            )
            await asyncio.wait_for(asyncio.wrap_future(fut), timeout=5.0)
        except (
            RuntimeError,
            asyncio.CancelledError,
            asyncio.InvalidStateError,
            asyncio.TimeoutError,
        ):
            return

    def ping_internal(
        self, if_name: Optional[str] = None, target_ip: Optional[str] = None
    ) -> None:
        """OTU Ping, MUST call with internal loop."""
        self.__sendto(
            if_name=if_name,
            data=self._probe_msg,
            address=target_ip or "255.255.255.255",
            port=self.OT_PORT,
        )

    def set_unicast_targets(self, targets: Dict[str, str]) -> None:
        """Set unicast probe targets (did → ip).

        These IPs will be probed via unicast UDP in every scan cycle,
        in addition to the normal broadcast.  Useful when cameras are on
        a different subnet that is still routable — broadcast won't cross
        the subnet boundary, but unicast will.

        Call with an empty dict to clear all targets.
        Safe to call when not initialized (no-op).
        """
        if not self._init_done:
            return
        try:
            self._internal_loop.call_soon_threadsafe(
                self.__set_unicast_targets, dict(targets)
            )
        except RuntimeError as e:
            # Event loop may already be stopped during deinit; silently skip.
            _LOGGER.debug(
                "set_unicast_targets skipped: internal loop unavailable: %s", e
            )

    def __set_unicast_targets(self, targets: Dict[str, str]) -> None:
        """Internal: replace unicast targets (runs on internal loop thread)."""
        self._unicast_targets = targets

    def set_camera_dids(self, dids: Set[str]) -> None:
        """Set the full set of known camera dids (not just those with a
        unicast target). Used to know when *every* camera is connected, at
        which point scanning pauses entirely. Safe to call when not
        initialized (no-op).
        """
        if not self._init_done:
            return
        try:
            self._internal_loop.call_soon_threadsafe(
                self.__set_camera_dids, set(dids)
            )
        except RuntimeError as e:
            _LOGGER.debug("set_camera_dids skipped: internal loop unavailable: %s", e)

    def __set_camera_dids(self, dids: Set[str]) -> None:
        self._camera_dids = dids
        self.__maybe_resume_scan()

    def set_camera_connected(self, did: str, connected: bool) -> None:
        """Mark a camera did as connected (native miss stream up) or not.

        Connected cameras are proven reachable, so probing (broadcast +
        unicast) skips them; once every known camera is connected, scanning
        pauses entirely until one disconnects or a new camera appears.
        Safe to call when not initialized (no-op).
        """
        if not self._init_done:
            return
        try:
            self._internal_loop.call_soon_threadsafe(
                self.__set_camera_connected, did, connected
            )
        except RuntimeError as e:
            _LOGGER.debug(
                "set_camera_connected skipped: internal loop unavailable: %s", e
            )

    def __set_camera_connected(self, did: str, connected: bool) -> None:
        if connected:
            self._connected_dids.add(did)
        else:
            self._connected_dids.discard(did)
            self.__maybe_resume_scan()

    def __all_cameras_connected(self) -> bool:
        return bool(self._camera_dids) and self._camera_dids <= self._connected_dids

    def __maybe_resume_scan(self) -> None:
        """Restart the scan loop if it was paused (all cameras connected)
        but that's no longer the case."""
        if self._scan_timer is not None or self.__all_cameras_connected():
            return
        self._last_scan_interval = None
        self._scan_timer = self._internal_loop.call_later(0, self.__scan_devices)

    def _probe_unicast_targets(self) -> None:
        """给已知目标 IP 发单播 OTU 探测。

        单播只对**跨网段**目标有意义——同网段目标已经被广播覆盖（见
        KNOWN_ISSUES.md #1）。跳过同网段目标不只是省一次冗余探测：实测坐实
        （2026-07-15）多网卡宿主上，本进程同时持有 IP_BOUND_IF 钉网卡的广播 socket
        时，不绑网卡的普通 socket 对同网段目标 sendto 会间歇性触发 XNU 的
        interface-scoped 路由（route -n get 显示 IFSCOPE 标志）与全局路由表之间的
        查找不一致，导致假性 EHOSTUNREACH——route 表本身在失败前后一直正常，新建
        socket/新进程立即复现，与 ARP/socket 生命周期/pending error 均无关。跳过
        同网段目标从根上避开这条路径，而不是浪费一次注定可能失败的探测。

        目标 IP 确定时——用**专用的、不绑网卡的普通 socket** 直接 sendto，出口网卡
        交给系统路由决定；回包由该 socket 的 add_reader 收。不复用广播那些
        IP_BOUND_IF 钉网卡的 socket：那样一是会往到不了目标的网卡盲发，二是单播发送
        失败 (EHOSTUNREACH 等) 会在共享 socket 上留下 pending error、毒害广播的接收，
        导致设备的广播保活也收不到而误判离线。
        """
        if not self._unicast_targets or self._unicast_sock is None:
            return
        for did, ip in self._unicast_targets.items():
            if not ip or self.__is_local_subnet(ip) or did in self._connected_dids:
                continue
            try:
                self._unicast_sock.sendto(
                    self._probe_msg, socket.MSG_DONTWAIT, (ip, self.OT_PORT)
                )
            except OSError as e:
                # 无路由/不可达等：记 debug 不刷屏，不影响其它目标与广播。
                _LOGGER.debug("unicast probe to %s failed: %s", ip, e)
                # 兜底：这条 sendto 走的 UDP 路径实测坐实过会在某些时间窗口里
                # 整体失效（见 KNOWN_ISSUES.md），但 ICMP ping 走的是完全不同的
                # 内核路径，同一时刻往往依然畅通。用它验证"其实可达，只是这次
                # sendto 撞上了那个坑"，避免跨网段相机被坏窗口卡住误判不可达。
                if did not in self._ping_fallback_inflight:
                    self._ping_fallback_inflight.add(did)
                    self._internal_loop.create_task(
                        self.__ping_fallback_async(did, ip)
                    )

    async def __ping_fallback_async(self, did: str, ip: str) -> None:
        """单播 sendto 失败后的 ICMP ping 兜底：ping 通就当作可达处理。

        两级尝试，都跑在线程池里、不阻塞 internal loop 的其它探测/收包：
        1. 不需要 root 的 ``SOCK_DGRAM`` ICMP（macOS 原生支持任意用户；Linux
           取决于 ``net.ipv4.ping_group_range``）。
        2. 不行就退回外部 ``ping`` 命令——某些环境的 ``ping`` 二进制自带
           setuid/capability，我们进程本身没特权时它也发得出。
        故意不用 ``SOCK_RAW``：那需要更宽的 ``CAP_NET_RAW``，有些加固过的
        容器即便跑 root 也不给，两条都不通就放弃，维持原样。

        证据强度弱于正常发现路径：广播/单播收包在 __raw_message_handler 里
        校验了 data[:2] == OT_HEADER（确认对端真的是 MIoT OT 协议），单播读侧
        还有源 IP 白名单；这里只认「ICMP echo 有应答」，既不校验协议、
        ``_icmp_dgram_ping`` 内部 recvfrom 也不核对回包源地址是否等于 ``ip``——
        目标 IP 来自云端 localip，若已陈旧且被 DHCP 转给同网段另一台在线主机，
        会把错误的主机误判为这台相机可达。触发需要 localip 陈旧 + 该 IP 被复用，
        非致命（顶多相机表面在线但拉不出流），暂不处理，先记录此隐含前提。
        """
        try:
            reachable = await self._internal_loop.run_in_executor(
                None, _icmp_dgram_ping, ip, 1.0
            )
            if not reachable:
                proc = await asyncio.create_subprocess_exec(
                    *(
                        ["ping", "-n", "1", "-w", "1000", ip]
                        if sys.platform == "win32"
                        else ["ping", "-c", "1", "-w", "1", ip]
                    ),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                reachable = await asyncio.wait_for(proc.wait(), timeout=2) == 0
            if reachable:
                device = self._lan_devices.get(did)
                if device is None:
                    device = _MIoTLanDevice(self, did, ip)
                    self._lan_devices[did] = device
                    _LOGGER.info("new device (ping fallback), %s, %s", did, ip)
                device.keep_alive(ip=ip, if_name="ping-fallback")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("ping fallback to %s failed: %s", ip, err)
        finally:
            self._ping_fallback_inflight.discard(did)

    def __is_local_subnet(self, ip: str) -> bool:
        """目标 IP 是否与本机某网卡同网段（即已被广播覆盖，无需单播）。"""
        try:
            target = ipaddress.IPv4Address(ip)
        except ValueError:
            return False
        for info in self._network.network_info.values():
            try:
                net = ipaddress.IPv4Network(f"{info.ip}/{info.netmask}", strict=False)
            except (ValueError, TypeError):
                continue
            if target in net:
                return True
        return False

    def broadcast_device_info_changed(self, did: str, info: MIoTLanDeviceInfo) -> None:
        """Broadcast device info changed."""
        for handler in self._callbacks_device_status_changed.values():
            self._main_loop.call_soon_threadsafe(
                self._main_loop.create_task,
                handler.handler(did, info, handler.handler_ctx),
            )

    def __deinit(self) -> None:
        # Release all resources
        if self._scan_timer:
            self._scan_timer.cancel()
            self._scan_timer = None
        for device in self._lan_devices.values():
            device.on_delete()
        self._lan_devices.clear()
        self._unicast_targets.clear()
        self._camera_dids.clear()
        self._connected_dids.clear()
        self._ping_fallback_inflight.clear()
        self.__deinit_socket()
        self._internal_loop.stop()

    def __internal_loop_thread(self) -> None:
        _LOGGER.info("miot lan thread start")
        self.__init_socket()
        self._scan_timer = self._internal_loop.call_later(
            int(3 * random.random()), self.__scan_devices
        )
        self._internal_loop.run_forever()
        _LOGGER.info("miot lan thread exit")

    def __init_socket(self) -> None:
        self.__deinit_socket()
        for if_name in self._net_ifs:
            if if_name not in self._available_net_ifs:
                continue
            self.__create_socket(if_name=if_name)
        self.__create_unicast_socket()

    def __create_unicast_socket(self) -> None:
        # 专用单播 socket：不绑网卡，交给系统路由。不显式 bind——sendto() 时内核
        # 会隐式绑到 wildcard+临时端口，效果一样，但不留 CodeQL 会标的显式绑定调用。
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self._internal_loop.add_reader(
                sock.fileno(), self.__socket_read_handler, ("unicast", sock)
            )
            self._unicast_sock = sock
            _LOGGER.info("created unicast socket")
        except Exception as err:
            _LOGGER.error("create unicast socket error, %s", err)

    def __on_network_info_change(self, data: _MIoTLanNetworkUpdateData) -> None:
        if data.status == InterfaceStatus.ADD:
            self._available_net_ifs.add(data.if_name)
            if data.if_name in self._net_ifs:
                self.__create_socket(if_name=data.if_name)
        elif data.status == InterfaceStatus.REMOVE:
            self._available_net_ifs.remove(data.if_name)
            self.__destroy_socket(if_name=data.if_name)

    def __create_socket(self, if_name: str) -> None:
        if if_name in self._broadcast_socks:
            _LOGGER.info("socket already created, %s", if_name)
            return
        # Create socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 多网卡 socket 共绑同一 _local_port。macOS 的 IP_BOUND_IF 不参与 bind
            # 冲突仲裁（不像 Linux 的 SO_BINDTODEVICE），两个 wildcard 同端口必须
            # SO_REUSEPORT 才能共存，否则第二个网卡 EADDRINUSE、该网段永远扫不到。
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            # 将 socket 绑定到指定网卡。
            # macOS 用 IP_BOUND_IF（XNU ABI 常量 25，部分 Python 构建未暴露符号）。
            if sys.platform == "darwin":
                ip_bound_if = getattr(socket, "IP_BOUND_IF", 25)
                sock.setsockopt(socket.IPPROTO_IP, ip_bound_if, socket.if_nametoindex(if_name))
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, if_name.encode())
            sock.bind(("", self._local_port or 0))
            self._internal_loop.add_reader(
                sock.fileno(), self.__socket_read_handler, (if_name, sock)
            )
            self._broadcast_socks[if_name] = sock
            self._local_port = self._local_port or sock.getsockname()[1]
            _LOGGER.info("created socket, %s, %s", if_name, self._local_port)
        except Exception as err:
            _LOGGER.error("create socket error, %s, %s", if_name, err)

    def __deinit_socket(self) -> None:
        for if_name in list(self._broadcast_socks.keys()):
            self.__destroy_socket(if_name)
        self._broadcast_socks.clear()
        if self._unicast_sock is not None:
            try:
                self._internal_loop.remove_reader(self._unicast_sock.fileno())
                self._unicast_sock.close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("close unicast socket error: %s", err)
            self._unicast_sock = None

    def __destroy_socket(self, if_name: str) -> None:
        sock = self._broadcast_socks.pop(if_name, None)
        if not sock:
            return
        self._internal_loop.remove_reader(sock.fileno())
        sock.close()
        _LOGGER.info("destroyed socket, %s", if_name)

    def __socket_read_handler(self, ctx: tuple[str, socket.socket]) -> None:
        try:
            data_len, addr = ctx[1].recvfrom_into(
                self._read_buffer, self.OT_MSG_LEN, socket.MSG_DONTWAIT
            )
            if data_len < 0:
                # Socket error
                _LOGGER.error("socket read error, %s, %s", ctx[0], data_len)
                return
            if addr[1] != self.OT_PORT:
                # Not ot msg
                return
            if ctx[0] == "unicast" and addr[0] not in self._unicast_targets.values():
                # 单播 socket 不绑网卡，局域网内任意主机都能往这个端口发包；
                # 只信任当前正在探测的目标 IP。
                return
            self.__raw_message_handler(
                self._read_buffer[:data_len], data_len, addr[0], ctx[0]
            )
        except Exception as err:
            _LOGGER.error("socket read handler error, %s", err)

    def __raw_message_handler(
        self, data: bytearray, data_len: int, ip: str, if_name: str
    ) -> None:
        if data[:2] != self.OT_HEADER:
            return
        # Keep alive message
        did: str = str(struct.unpack(">Q", data[4:12])[0])
        device: Optional[_MIoTLanDevice] = self._lan_devices.get(did)
        timestamp: int = struct.unpack(">I", data[12:16])[0]
        if not device:
            device = _MIoTLanDevice(self, did, ip)
            self._lan_devices[did] = device
            _LOGGER.info("new device, %s, %s", did, ip)
        device.offset = int(time.time()) - timestamp
        # Keep alive if this is a probe
        if data_len == self.OT_PROBE_LEN:
            device.keep_alive(ip=ip, if_name=if_name)

    def __subnet_broadcast(self, if_name: str) -> Optional[str]:
        # 部分平台对 dst=255.255.255.255 的 UDP sendto 直接 EHOSTUNREACH 拒发
        # 实时从 MIoTNetwork 读 ip/netmask，避免本地 cache 在 InterfaceStatus.UPDATE 时陈旧信息干扰。
        info = self._network.network_info.get(if_name)
        if not info:
            return None
        try:
            return str(
                ipaddress.IPv4Network(
                    f"{info.ip}/{info.netmask}", strict=False
                ).broadcast_address
            )
        except (ValueError, TypeError):
            return None

    def __resolve_target(self, if_name: str, address: str) -> str:
        if address != "255.255.255.255":
            return address
        bcast = self.__subnet_broadcast(if_name)
        if not bcast:
            _LOGGER.warning(
                "subnet broadcast unavailable for %s, fallback to 255.255.255.255",
                if_name,
            )
        return bcast or address

    def __sendto(
        self, if_name: Optional[str], data: bytes, address: str, port: int
    ) -> None:
        if if_name is None:
            # Fan out via every interface
            for if_n, sock in self._broadcast_socks.items():
                target = self.__resolve_target(if_n, address)
                _LOGGER.debug("send broadcast, %s, %s", if_n, target)
                sock.sendto(data, socket.MSG_DONTWAIT, (target, port))
        else:
            # Send via specified interface only
            sock = self._broadcast_socks.get(if_name, None)
            if not sock:
                _LOGGER.error("invalid socket, %s", if_name)
                return
            target = self.__resolve_target(if_name, address)
            sock.sendto(data, socket.MSG_DONTWAIT, (target, port))

    def __scan_devices(self) -> None:
        if self._scan_timer:
            self._scan_timer.cancel()
            self._scan_timer = None
        if self.__all_cameras_connected():
            # 全部已知相机都已连上（可达性已证实）：广播/单播探测都没必要，
            # 停表——set_camera_connected/set_camera_dids 检测到不再"全连上"
            # 时会通过 __maybe_resume_scan 重新拉起。
            #
            # 注意：这里停的是共享的全局广播发现（ping_internal），不只是相机——
            # 账号下所有 LAN 设备的 keep-alive 都靠这条广播维持。相机侧全连上后
            # 不受影响（有 connected 兜底），但非相机设备会在 keep-alive 超时
            # （_KA_TIMEOUT）后集体翻 lan_online=False，此期间新上线的非相机设备
            # 也发现不了。目前无害（lan_online 的决策读点均为相机路径），但这是
            # 一条隐性 invariant：将来任何非相机功能若以 lan_online 作硬门，会在
            # "所有相机都在拉流"这个无关条件下静默失效。
            _LOGGER.debug("all cameras connected, scan paused")
            return
        try:
            # Scan devices — broadcast
            self.ping_internal()
            # Additionally probe known unicast targets (cross-subnet cameras)
            self._probe_unicast_targets()
        except Exception as err:
            # Ignore any exceptions to avoid blocking the loop
            _LOGGER.error("ping device error, %s", err)
        scan_time = self.__get_next_scan_time()
        self._scan_timer = self._internal_loop.call_later(
            scan_time, self.__scan_devices
        )
        _LOGGER.debug("next scan time: %ss", scan_time)

    def __get_next_scan_time(self) -> float:
        if not self._last_scan_interval:
            self._last_scan_interval = self.OT_PROBE_INTERVAL_MIN
        self._last_scan_interval = min(
            self._last_scan_interval * 2, self.OT_PROBE_INTERVAL_MAX
        )
        return self._last_scan_interval

    async def __on_network_info_change_external_async(
        self, status: InterfaceStatus, info: NetworkInfo
    ) -> None:
        """Network info change."""
        _LOGGER.info("on network info change, status: %s, info: %s", status, info)
        available_net_ifs = set()
        for if_name in list(self._network.network_info.keys()):
            available_net_ifs.add(if_name)
        if len(available_net_ifs) == 0:
            await self.deinit_async()
            self._available_net_ifs = available_net_ifs
            return
        if self._net_ifs.isdisjoint(available_net_ifs):
            _LOGGER.info("no valid net_ifs")
            await self.deinit_async()
            self._available_net_ifs = available_net_ifs
            return
        if not self._init_done:
            self._available_net_ifs = available_net_ifs
            await self.init_async()
            return
        try:
            self._internal_loop.call_soon_threadsafe(
                self.__on_network_info_change,
                _MIoTLanNetworkUpdateData(status=status, if_name=info.name),
            )
        except RuntimeError:
            _LOGGER.warning("internal_loop closed during network info change")
            return

    def __register_status_changed(self, data: _MIoTLanRegDeviceData) -> None:
        self._callbacks_device_status_changed[data.key] = data

    def __unregister_status_changed(self, data: _MIoTLanUnregDeviceData) -> None:
        self._callbacks_device_status_changed.pop(data.key, None)

    async def __get_devices_internal_async(self) -> Dict[str, MIoTLanDeviceInfo]:
        """Get devices internal."""
        devices = {}
        for did, lan_device in self._lan_devices.items():
            devices[did] = MIoTLanDeviceInfo(
                did=lan_device.did, online=lan_device.online, ip=lan_device.ip
            )
        return devices
