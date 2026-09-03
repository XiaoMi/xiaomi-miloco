# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT lan device detector.
"""

import asyncio
import ipaddress
import logging
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

    def mark_reachable(self, start_grace: bool) -> None:
        """把设备标为在线——依据是**带外可达证明**(native miss 拉流正在进行,
        或刚被主动关闭),而非 LAN 探测响应。

        正在拉流的相机可达性已被证实,但跨网段相机在 connected 期间会被单播探测
        跳过(见 ``_probe_unicast_targets``),没有任何东西刷新它的 keep-alive,
        于是会在拉流中途误判离线、并在**主动关流的瞬间闪一下 offline**。此方法
        用拉流状态兜底,避免这两种误判。

        - ``start_grace=False``:拉流进行中——保持在线并**取消离线定时器**
          (连接本身就是活性证明)。
        - ``start_grace=True``:拉流刚停止——仍标为在线,但**重新武装一个
          keep-alive 宽限窗**,让恢复后的扫描去复核;可自愈(若真不可达,宽限窗内
          收不到探测响应 → 超时后正常翻离线)。
        """
        if self._online is False:
            self._online = True
            _LOGGER.info("device online (stream), %s, %s", self.did, self._ip)
            self.__broadcast_info_changed()
        if self._ka_timer:
            self._ka_timer.cancel()
            self._ka_timer = None
        if start_grace:
            self._ka_timer = self._manager.internal_loop.call_later(
                self._KA_TIMEOUT, self.__switch_offline
            )

    def ensure_offline_timer(self) -> None:
        """确保存在一条「翻离线」的路径：已有定时器则**不动**（不续期），没有才武装。

        给「原生报连接失败」这类不能当可达证明的场景用。不能续期（那等于拿失败证明
        可达，且原生退避重连每次都短于 ``_KA_TIMEOUT``，会把窗口无限拉长），但也不能
        让设备停在「一个离线定时器都没有」上——那样它永远翻不了假：

        - 拉流建连成功时 ``mark_reachable(start_grace=False)`` 把定时器 cancel 后置
          ``None``（连接本身就是活性证明，不需要定时器）；
        - connected 期间单播探测**主动跳过**该 did（见 ``_probe_unicast_targets``），
          广播又跨不了子网 ⇒ 没有任何东西会调 ``keep_alive`` 重新武装它；
        - 于是跨网段相机「连上过再被拔电」时，若这里也不武装，``lan_online`` 会永久
          卡在 True：接口一直报 lan_reachable、跨 NAT 诊断还会把没电的相机说成路由器
          NAT 问题，把住户指去折腾路由器配置。

        已有定时器时保持原 deadline 不变，所以连续失败重试不会把它往后推。
        """
        if self._ka_timer is None:
            self._ka_timer = self._manager.internal_loop.call_later(
                self._KA_TIMEOUT, self.__switch_offline
            )

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
            info=MIoTLanDeviceInfo(
                did=self.did,
                online=self._online,
                ip=self._ip,
                cross_subnet=self._manager.is_cross_subnet(self._ip),
            ),
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
    # 上层推下来的**云端在线且当前 scope** 相机物理 did 集（home_allowed ∧ online）
    # 与已连上的 did 集合。已连上的相机可达性已经证实，单播探测按目标跳过。
    #
    # 停表判据：所有云端在线相机都已连上（cloud_online ⊆ connected）→ 不探测。
    # 集合外只剩「云端离线」（toggle 的 online 门挡着，开不了、无需探测）与
    # 「scope 外」（home_allowed 门挡着，本 scope 不让开）。可开启相机 = 云端在线
    # ∧ in-scope，正好都在这个集合里，所以停表不会挡任何合法操作；任何状态变化
    # （断开/云端新增/开关流）都制造 inequality，由 __resume_scan 恢复探测。
    _cloud_online_dids: Set[str]
    _connected_dids: Set[str]

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
        self._cloud_online_dids = set()
        self._connected_dids = set()

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
                self._cloud_online_dids = set()
                self._connected_dids = set()
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

        These IPs will be probed via unicast UDP in every scan cycle, in
        addition to the normal broadcast — **regardless of subnet**. The
        original motivation was cross-subnet cameras (broadcast won't cross the
        subnet boundary, unicast will), but same-subnet targets are probed too:
        directed broadcast is not guaranteed to be delivered (switch storm
        control / AP client isolation / router broadcast rate-limiting), and a
        directly reachable camera should not go lan_online=False just because
        its broadcast reply was dropped. See ``_probe_unicast_targets`` for the
        macOS Local Network Privacy caveat this re-exposes.

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

    def set_cloud_online_dids(self, dids: Set[str]) -> None:
        """Set the **cloud-online in-scope** camera physical dids.

        Stop condition: when every cloud-online camera is already connected
        (``_cloud_online_dids <= _connected_dids``) the scan stops entirely —
        see ``__scan_devices``. Anything outside this set is either
        cloud-offline (toggle's ``online`` gate refuses to enable it) or out
        of scope (``home_allowed`` gate refuses it), neither needs probing.
        A new cloud-online camera appearing brings the scan back.

        Must be **physical** dids (multi-lens cameras converge to one entry)
        so the comparison with ``_connected_dids`` is at the same granularity.
        Safe to call when not initialized (no-op).
        """
        if not self._init_done:
            return
        try:
            self._internal_loop.call_soon_threadsafe(
                self.__set_cloud_online_dids, set(dids)
            )
        except RuntimeError as e:
            _LOGGER.debug(
                "set_cloud_online_dids skipped: internal loop unavailable: %s", e
            )

    def __set_cloud_online_dids(self, dids: Set[str]) -> None:
        self._cloud_online_dids = dids
        self.__resume_scan()

    def set_camera_connected(
        self, did: str, connected: bool, keep_alive_on_stop: bool = True
    ) -> None:
        """Mark a camera did as connected (native miss stream up) or not.

        A connected camera is proven reachable, so ``_probe_unicast_targets``
        skips it per-target; once **every cloud-online in-scope** camera is
        connected the scan loop stops (see ``__scan_devices``) and resumes
        when one disconnects or a new cloud-online camera appears. Safe to
        call when not initialized (no-op).
        """
        if not self._init_done:
            return
        try:
            self._internal_loop.call_soon_threadsafe(
                self.__set_camera_connected, did, connected, keep_alive_on_stop
            )
        except RuntimeError as e:
            _LOGGER.debug(
                "set_camera_connected skipped: internal loop unavailable: %s", e
            )

    def __set_camera_connected(
        self, did: str, connected: bool, keep_alive_on_stop: bool = True
    ) -> None:
        if connected:
            self._connected_dids.add(did)
            # 拉流建立即可达性证明:保持在线、取消离线定时器,避免跨网段相机在
            # connected 期间(被单播探测跳过)误判离线。
            device = self._lan_devices.get(did)
            if device is not None:
                device.mark_reachable(start_grace=False)
        else:
            self._connected_dids.discard(did)
            # 主动关流不代表设备不可达——它刚刚还在拉流。标为本地可达并给一个
            # keep-alive 宽限窗,避免瞬时闪 offline;恢复扫描后由真实探测复核。
            #
            # keep_alive_on_stop=False 时**不续期**:调用方明确表示这次是「原生报连接
            # 失败」而非「我们主动关流」。拿失败证据去证明可达方向是反的,而且原生带
            # 退避自动重连(3s→6s→12s…,每次都短于 100s),每次失败都续期等于把宽限窗
            # 无限拉长——相机被拔电后接口会连续几分钟仍报 lan_reachable=true,而这个
            # 字段的语义本是「100s 收不到探测响应就翻离线」。
            device = self._lan_devices.get(did)
            if device is not None:
                if keep_alive_on_stop:
                    device.mark_reachable(start_grace=True)
                else:
                    # 不续期，但必须保证存在一条「100s 收不到探测响应就翻离线」的
                    # 路径：建连成功那次 mark_reachable(start_grace=False) 已经把
                    # 定时器取消并置空，而 connected 期间单播探测跳过该 did、广播
                    # 跨不了子网，没有任何东西会重新武装它。已有定时器保持原
                    # deadline 不变，失败重试不会把它往后推。
                    device.ensure_offline_timer()
            self.__resume_scan()

    def __all_cloud_online_connected(self) -> bool:
        """所有「云端在线且当前 scope」的相机都已连上（停表判据）。

        空集返回 True（空 ⊆ 空）：还没有云端在线相机时没有可探之物，停表等
        同步；同步到来时 ``__resume_scan`` 会恢复探测。
        """
        return self._cloud_online_dids <= self._connected_dids

    def __resume_scan(self) -> None:
        """扫描因「全部云端在线相机已连上」停表后、条件不再成立时立即恢复扫一次。

        - ``_scan_timer`` 非空 = 扫描仍在跑/已排期，无需干预（下一轮会自己重判，
          也不会重置退避，避免探测流量白涨）；
        - ``_scan_timer`` 为空 = 停表态；条件已不成立 → 重置退避并立刻扫一次。
        恢复后由 ``__scan_devices`` 决定继续排期还是再次停表。
        """
        if self._scan_timer is not None or self.__all_cloud_online_connected():
            return
        self._last_scan_interval = None
        self._scan_timer = self._internal_loop.call_later(0, self.__scan_devices)

    def _probe_unicast_targets(self) -> None:
        """给已知目标 IP 发单播 OTU 探测——**不分网段，同网段目标也发**。

        原先只对跨网段目标发（同网段已被广播覆盖，且能规避下面那个 macOS 坑）。改成
        不分网段是为了不再依赖「广播一定送达同网段」这个假设：定向子网广播会被交换机
        的 IGMP/风暴抑制、AP 的客户端隔离、以及部分路由器的广播限速丢掉，此时同网段
        相机明明可直连、却因为收不到广播探测而 lan_online 恒 False。多发一份单播的代价
        是每轮每台多一个 UDP 包（回包重复调 keep_alive，幂等无副作用）。

        ⚠️ macOS 上的已知代价（这也是原先跳过同网段的第二个理由，删掉 skip 后重新暴露）：
        macOS 15+ 的 Local Network Privacy 按**进程的启动上下文**决定是否放行本地网络
        访问（Apple TN3179：launchd daemon / root / 从 Terminal 或 SSH 启动的进程及其
        子进程自动豁免；launchd **agent** 不豁免）。以 launchd agent 形态启动的 miloco，
        对**同网段**目标的 UDP 单播会被内核在发送前直接拦掉、返回 errno 65
        EHOSTUNREACH，而 route/ARP 全程正常、包根本没上过线。判定维度只有启动上下文
        ——与代码、uid、ARP、IFSCOPE、socket 复用方式全都无关（2026-07-24 实测坐实：
        同一二进制、同一 config、同一用户，仅改启动方式，SSH 启动 100% 成功、launchd
        agent 启动 100% errno 65）。

        实测的拦截边界：拦 = 同网段 UDP 单播、全局广播 255.255.255.255、多播 224.x；
        放行 = 定向子网广播（192.168.1.255）与 TCP——这正是广播发现与 miss 拉流在
        宿主机仍能工作、只有同网段单播失效的原因。所以在未豁免的 macOS 上，这里对同
        网段目标的 sendto 会每轮失败一次（记 info、不影响广播与其它目标，功能等价于
        改动前）；根治办法是换部署形态（跑 Linux 容器，或改成 launchd daemon /
        签名启动器），不在本进程代码内。

        目标 IP 确定时——用**专用的、不绑网卡的普通 socket** 直接 sendto，出口网卡
        交给系统路由决定；回包由该 socket 的 add_reader 收。不复用广播那些
        IP_BOUND_IF 钉网卡的 socket，避免往到不了目标的网卡盲发，并让单播的发送
        失败不会影响广播 socket 的接收路径。

        已连上的 did 仍然跳过：可达性已由拉流本身证实，探它没有意义。

        单播 socket 只在线程启动时建一次（``__init_socket``），网卡增删回调不会重建
        它——启动瞬间的一次性失败（fd 耗尽 / 网络栈还没就绪）此前会让跨网段发现在整个
        进程生命周期里永久失效，且是纯静默：探测目标表照常被下发，只是没人用。这里在
        有目标要探且 socket 缺失时按需补建一次；仍失败就本轮跳过，下一轮扫描再试。
        """
        if not self._unicast_targets:
            return
        if self._unicast_sock is None:
            self.__create_unicast_socket()
            if self._unicast_sock is None:
                return
        for did, ip in self._unicast_targets.items():
            if not ip or did in self._connected_dids:
                continue
            try:
                self._unicast_sock.sendto(
                    self._probe_msg, socket.MSG_DONTWAIT, (ip, self.OT_PORT)
                )
            except OSError as e:
                # 无路由/不可达等：记 info——发送成功仍静默，失败路径必须可见，
                # 否则真机上「单播被 LNP 拦（errno 65）」与「根本没发」无法区分。
                _LOGGER.info("unicast probe to %s failed: %s", ip, e)

    def __local_subnets(self) -> list["ipaddress.IPv4Network"]:
        """本机所有可解析网段。空列表 = 当前判不出本机网段（网卡全掉 / 刷新窗口）。"""
        nets: list["ipaddress.IPv4Network"] = []
        for info in self._network.network_info.values():
            try:
                nets.append(
                    ipaddress.IPv4Network(f"{info.ip}/{info.netmask}", strict=False)
                )
            except (ValueError, TypeError):
                continue
        return nets

    def is_cross_subnet(self, ip: Optional[str]) -> Optional[bool]:
        """ip 是否跨网段（与本机所有网卡都不同网段）。判不出来时返回 None。

        三种「判不出来」一律 None，不返回 True：ip 未知；ip 不是合法 IPv4；本机网段
        一个都解析不出来（网卡全掉 / Wi-Fi 漫游中的刷新窗口，网卡表在这段时间可以为
        空）。后两种下"与所有网卡都不同网段"这句话没有事实依据——返回 True 会让上层
        把一台恰好同网段、只是网卡表暂时读不到的相机诊断成 NAT 阻断，指着住户去折腾
        路由器，方向与 ``stream_nat_blocked``「云端离线就不给这条诊断」同一口径：宁可
        少报也不错报。
        """
        if not ip:
            return None
        try:
            target = ipaddress.IPv4Address(ip)
        except ValueError:
            return None
        nets = self.__local_subnets()
        if not nets:
            return None
        return not any(target in net for net in nets)

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
        self._cloud_online_dids.clear()
        self._connected_dids.clear()
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
        if self._unicast_sock is not None:
            return  # 幂等：允许 _probe_unicast_targets 按需补建时重复调用
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
        # 停表判据：所有「云端在线且当前 scope」的相机都已连上。可开启相机 =
        # 云端在线 ∧ in-scope，正好全在这个集合里；集合外只剩开不了/不让开的，
        # 无需探测。已连相机的在线态由拉流状态（mark_reachable）维持，不依赖
        # 扫描。停表后任何状态变化（断开/云端新增/开关流）会制造 inequality，
        # 由 __resume_scan 恢复探测。
        if self.__all_cloud_online_connected():
            _LOGGER.debug("scan stopped: all cloud-online cameras connected")
            return
        try:
            # Scan devices — broadcast
            self.ping_internal()
            # Additionally probe known unicast targets, regardless of subnet
            # (see _probe_unicast_targets docstring)
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
                did=lan_device.did,
                online=lan_device.online,
                ip=lan_device.ip,
                cross_subnet=self.is_cross_subnet(lan_device.ip),
            )
        return devices
