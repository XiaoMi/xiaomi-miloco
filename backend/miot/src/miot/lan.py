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

# 单播 socket 的 reader ctx 标签 / keep_alive 的 if_name。广播 socket 用的是真实
# 网卡名（"en1" 等），所以「if_name != 此值」就等价于「这次 keep-alive 是广播喂的」
# ——_probe_unicast_targets 的同网段判据依赖该等价关系，故收成一个常量，避免字面量
# 在某一处被改动后判据静默失效（每次 keep-alive 都被当成广播 ⇒ 同网段永不发单播）。
_UNICAST_IF_NAME: str = "unicast"


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
    # 最近一次**经广播**收到探测应答的时刻（internal_loop 单调钟；从未被广播喂活
    # 过则为 None）。_probe_unicast_targets 用它判断「广播这条路对这台设备到底管
    # 不管用」，见那里的 docstring。
    _last_broadcast_ka: Optional[float]

    _ka_timer: Optional[asyncio.TimerHandle]

    def __init__(self, manager: "MIoTLan", did: str, ip: Optional[str] = None) -> None:
        self._manager = manager
        self.did = did
        self.offset = 0
        self._online = False
        self._ip = ip
        self._if_name = None
        self._last_broadcast_ka = None
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
        if if_name != _UNICAST_IF_NAME:
            # 广播路径确实把这台设备喂活了——记时刻，作为「同网段可以只靠广播」的
            # 唯一证据来源。用 internal_loop 的单调钟，与 _ka_timer 同一时间基准，
            # 不受墙钟跳变影响。
            self._last_broadcast_ka = self._manager.internal_loop.time()
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
    def last_broadcast_ka(self) -> Optional[float]:
        """最近一次经广播喂活的 internal_loop 单调时刻；从未被广播喂活过则 None。"""
        return self._last_broadcast_ka

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

    # 「广播喂活」的保鲜期：距上次经广播收到应答超过这么久，就认为广播对该设备不再
    # 管用，改发单播兜底（见 _probe_unicast_targets）。
    #
    # 取值须落在 (OT_PROBE_INTERVAL_MAX, _MIoTLanDevice._KA_TIMEOUT) 之间：
    # - > 45s：广播正常时每轮扫描都会刷新它，稳态下不会误判成「广播失效」而多发单播
    #   （降频态一轮 45s，留出丢一轮的余量）；
    # - < 100s：广播失效后，必须早于 lan_online 翻 False 就切回单播，否则相机会先掉
    #   出可拉流集，留下一个百秒级盲窗。
    BROADCAST_FRESH_S: float = 90

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
    # 上层推下来的**拉流活跃集**相机 did（在启用家庭 ∧ 未拉黑 ∧ 在线 ∧ 镜头未关
    # ∧ 截到上限）与已连上的 did 集合。已连上的相机可达性已经证实，单播探测按
    # 目标跳过；全部连上时扫描**降频**到 OT_PROBE_INTERVAL_MAX。
    #
    # ⚠️ 这是「已启用」集，不是「已知/全部」集——用户没打开开关的相机不在里面、
    # 永远不会 connected。所以**绝不能据此停表**：停表后集合外的设备（未启用
    # 相机、非相机设备）拿不到探测，lan_online 会在 _KA_TIMEOUT 后翻 False，而
    # toggle_camera 拿 lan_online 当硬门 → 用户再也开不了那台相机，且无恢复路径
    # （__maybe_resume_scan 用同一判据，恒早退）。降频到 45s < _KA_TIMEOUT 100s
    # 才能既省探测流量、又让集合外设备的在线态始终被刷新。
    _camera_dids: Set[str]
    _connected_dids: Set[str]
    # 是否处于「全部已启用相机都已连上」的降频态。__maybe_resume_scan 靠它区分
    # 「正常退避排期（别打扰）」与「降频态（该提前扫一次）」。
    _scan_throttled: bool

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
        self._scan_throttled = False

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
                self._scan_throttled = False
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
        """Set the **enabled** (streaming-active) camera dids.

        This is what lets the LAN layer tell "every enabled camera is
        connected" (→ scanning throttles down to ``OT_PROBE_INTERVAL_MAX``,
        see ``__scan_devices``) from "one is still missing". A newly-appeared
        camera did brings the scan back to full rate.

        Must be the **scoped active set**, not every camera on the account:
        a camera outside the current scope never connects, so including it
        would make "all connected" permanently unreachable and the throttle
        would never engage.

        Because this is the *enabled* set, the throttle must never become a
        full stop — cameras the user has not switched on are absent here yet
        still rely on the shared broadcast to keep their ``lan_online`` fresh.
        Safe to call when not initialized (no-op).
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

    def set_camera_connected(
        self, did: str, connected: bool, keep_alive_on_stop: bool = True
    ) -> None:
        """Mark a camera did as connected (native miss stream up) or not.

        A connected camera is proven reachable, so ``_probe_unicast_targets``
        skips it per-target; once **every enabled** camera is connected the
        scan loop throttles down to ``OT_PROBE_INTERVAL_MAX`` (see
        ``__scan_devices``) and returns to full rate when one disconnects or a
        new camera appears. It never stops outright — devices outside the
        enabled set depend on the shared broadcast. Safe to call when not
        initialized (no-op).
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
            self.__maybe_resume_scan()

    def __all_cameras_connected(self) -> bool:
        """**已启用**（拉流活跃集）相机非空且全部处于已连接状态。

        空集返回 False——「一台相机都还没启用」不该被当成「全都连上了」而降频。

        注意这只是「降频」的判据，不是「停表」的判据：集合外仍有未启用相机与
        非相机设备依赖扫描维持 lan_online，详见 ``__scan_devices`` 的说明。
        """
        return bool(self._camera_dids) and self._camera_dids <= self._connected_dids

    def __maybe_resume_scan(self) -> None:
        """扫描此前因「相机全连上」而降频、现在条件不再成立时，提前扫一次。

        扫描循环**永不停表**（见 ``__scan_devices``），所以这里只做「加速」：把
        已排期的下一次扫描提前到现在并重置退避，让掉线/新增的相机尽快被发现。

        ``_scan_throttled`` 为 False 表示扫描本就在正常退避排期上，无需干预——
        否则每轮 ``set_camera_dids`` 都会把退避重置回 5s，探测流量白涨一个量级。
        """
        if not self._scan_throttled or self.__all_cameras_connected():
            return
        self._scan_throttled = False
        if self._scan_timer is not None:
            self._scan_timer.cancel()
            self._scan_timer = None
        self._last_scan_interval = None
        self._scan_timer = self._internal_loop.call_later(0, self.__scan_devices)

    def _probe_unicast_targets(self) -> None:
        """给已知目标 IP 发单播 OTU 探测。

        跨网段目标必须走单播（广播过不了子网边界）。同网段目标**优先走广播**，但
        只在有证据表明广播对该设备确实管用时才跳过单播——判据是「这台设备在
        ``BROADCAST_FRESH_S`` 内被广播喂活过」(``_last_broadcast_ka``)，而不是
        「同网段 ⇒ 广播必然可行」这个假设。

        该假设在开了广播/组播抑制（BUM filtering / 组播转单播，高密度 AIoT 环境常见）
        的 AP 下不成立：AP 转发客户端之间的单播、但丢弃客户端之间的广播/组播。此时
        同网段设备既不被广播覆盖、又被单播跳过，落进覆盖真空——现场实测该网段广播
        探测 0 应答、单播 5/5 应答，8 台相机全部 ``lan_online=False``，既拉不了流也
        感知不到（``select_active_camera_dids`` 拿 ``lan_online`` 当硬门）。

        判据按**设备**而非按网络，因为「广播收不到」有两种成因——AP 丢广播、或该型号
        不响应广播目的地址的探测——无 root 抓包时区分不了，而按设备判定不需要区分：
        没被广播喂活过就发单播，两种成因被同一条规则覆盖。反之若定义成一个网络级
        全局 flag，就必须先答对成因，答错会永久饿死那台设备。

        跳过同网段目标除了省一次冗余探测，还规避了 macOS 上一个确定性的坑：

        macOS 15+ 的 Local Network Privacy 按**进程的启动上下文**决定是否放行本地
        网络访问（Apple TN3179：launchd daemon / root / 从 Terminal 或 SSH 启动的
        进程及其子进程自动豁免；launchd **agent** 不豁免）。以 launchd agent 形态
        启动的 miloco，对**同网段**目标的 UDP 单播会被内核在发送前直接拦掉、返回
        errno 65 EHOSTUNREACH，而 route/ARP 全程正常、包根本没上过线。判定维度只有
        启动上下文——与代码、uid、ARP、IFSCOPE、socket 复用方式全都无关（2026-07-24
        实测坐实：同一二进制、同一 config、同一用户，仅改启动方式，SSH 启动 100%
        成功、launchd agent 启动 100% errno 65）。

        实测的拦截边界：拦 = 同网段 UDP 单播、全局广播 255.255.255.255、多播 224.x；
        放行 = 定向子网广播（192.168.1.255）与 TCP——这正是广播发现与 miss 拉流在
        宿主机仍能工作、只有同网段单播失效的原因。根治办法是换部署形态（跑 Linux
        容器，或改成 launchd daemon），不在本进程代码内。

        这条坑与上面的判据是相容的，不需要额外分支：那种形态下**定向子网广播是放行
        的**（正是上一段的实测边界）⇒ 广播能把同网段设备喂活 ⇒ 判据判定「广播够用」
        ⇒ 同网段单播根本不会发出，必然失败的路径依旧撞不到。只有广播确实失效时才会
        去试同网段单播——而那时试一次恰恰是我们要的，且失败已被下方 ``except OSError``
        兜住（记 debug、不影响其它目标与广播路径）。

        目标 IP 确定时——用**专用的、不绑网卡的普通 socket** 直接 sendto，出口网卡
        交给系统路由决定；回包由该 socket 的 add_reader 收。不复用广播那些
        IP_BOUND_IF 钉网卡的 socket，避免往到不了目标的网卡盲发，并让单播的发送
        失败不会影响广播 socket 的接收路径。
        """
        if not self._unicast_targets or self._unicast_sock is None:
            return
        for did, ip in self._unicast_targets.items():
            if not ip or did in self._connected_dids:
                continue
            # 同网段：只有拿到「广播确实在喂活它」的证据才跳过。__is_local_subnet
            # 放在前面短路，跨网段目标不必去碰时钟。
            if self.__is_local_subnet(ip) and self.__broadcast_is_feeding(did):
                continue
            try:
                self._unicast_sock.sendto(
                    self._probe_msg, socket.MSG_DONTWAIT, (ip, self.OT_PORT)
                )
            except OSError as e:
                # 无路由/不可达等：记 debug 不刷屏，不影响其它目标与广播。
                _LOGGER.debug("unicast probe to %s failed: %s", ip, e)

    def __broadcast_is_feeding(self, did: str) -> bool:
        """广播这条路最近是否真的把这台设备喂活过（见 ``_probe_unicast_targets``）。

        没见过这个 did、或从未被广播喂活过（只被单播喂活过、或刚建条目还没收到过
        应答）→ False，即「广播不顶用，发单播」。这也让首轮探测天然走单播兜底：
        设备条目此时还不存在，不存在「等广播先证明自己」的冷启动死锁。
        """
        device = self._lan_devices.get(did)
        if device is None:
            return False
        last = device.last_broadcast_ka
        if last is None:
            return False
        return (self._internal_loop.time() - last) < self.BROADCAST_FRESH_S

    def __is_local_subnet(self, ip: str) -> bool:
        """目标 IP 是否与本机某网卡同网段（即广播**有可能**覆盖到它）。

        注意只是「有可能」：同网段不等于广播一定可行（AP 可能抑制客户端广播），所以
        这不足以单独作为跳过单播的理由，须与 ``__broadcast_is_feeding`` 合用。
        """
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

    def is_cross_subnet(self, ip: Optional[str]) -> Optional[bool]:
        """ip 是否跨网段（与本机所有网卡都不同网段）。ip 未知时返回 None。"""
        if not ip:
            return None
        return not self.__is_local_subnet(ip)

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
                sock.fileno(), self.__socket_read_handler, (_UNICAST_IF_NAME, sock)
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
            if (
                ctx[0] == _UNICAST_IF_NAME
                and addr[0] not in self._unicast_targets.values()
            ):
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
        # 全部已启用相机都已连上：可达性已由拉流本身证实（连上时 mark_reachable
        # 直接标在线并取消离线定时器），探测对**这些**相机没必要再打，故把扫描
        # 降到最低频。
        #
        # ⚠️ 只能降频，**不能停表**：_camera_dids 是「已启用」集而非「全部」集，
        # 集合外还有未启用的相机与非相机设备靠这个全局共享的广播维持 lan_online。
        # 一旦停表，它们会在 _KA_TIMEOUT(100s) 后翻 False，而 toggle_camera 拿
        # lan_online 当硬门 → 用户再也开不了那台相机；且 __maybe_resume_scan 用的
        # 是同一判据，推同样的集合不会重排定时器，没有任何恢复路径。
        # OT_PROBE_INTERVAL_MAX(45s) < _KA_TIMEOUT(100s)，降频后集合外设备的在线态
        # 仍能被持续刷新。
        all_connected = self.__all_cameras_connected()
        self._scan_throttled = all_connected
        try:
            # Scan devices — broadcast
            self.ping_internal()
            # Additionally probe known unicast targets (cross-subnet cameras)
            self._probe_unicast_targets()
        except Exception as err:
            # Ignore any exceptions to avoid blocking the loop
            _LOGGER.error("ping device error, %s", err)
        scan_time = (
            self.OT_PROBE_INTERVAL_MAX
            if all_connected
            else self.__get_next_scan_time()
        )
        self._scan_timer = self._internal_loop.call_later(
            scan_time, self.__scan_devices
        )
        _LOGGER.debug(
            "next scan time: %ss%s",
            scan_time,
            " (throttled: all enabled cameras connected)" if all_connected else "",
        )

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
