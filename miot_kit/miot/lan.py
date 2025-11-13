# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT lan device detector.
"""
import asyncio
from dataclasses import dataclass
import logging
import random
import secrets
import socket
import struct
import threading
import time
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

    def __init__(
        self, manager: "MIoTLan",  did: str, ip: Optional[str] = None
    ) -> None:
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
        self._ka_timer = self._manager.internal_loop.call_later(self._KA_TIMEOUT, self.__switch_offline)
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
        _LOGGER.debug(
            "device status changed, %s, %s", self.did, self._online)
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
            did=self.did, info=MIoTLanDeviceInfo(
                did=self.did,
                online=self._online,
                ip=self._ip
            )
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
    _local_port: Optional[int]
    _scan_timer: Optional[asyncio.TimerHandle]
    _last_scan_interval: Optional[float]
    _callbacks_device_status_changed: Dict[str, _MIoTLanRegDeviceData]

    _init_lock: asyncio.Lock
    _init_done: bool

    def __init__(
        self, net_ifs: List[str], network: MIoTNetwork, virtual_did: Optional[int] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        """Init."""
        self._main_loop = loop or asyncio.get_running_loop()

        self._net_ifs = set(net_ifs)
        self._network = network
        self._lan_devices = {}
        self._virtual_did = str(virtual_did) if (virtual_did is not None) else str(secrets.randbits(64))
        # Init socket probe message
        probe_bytes = bytearray(self.OT_PROBE_LEN)
        probe_bytes[:20] = b"!1\x00\x20\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFFMDID"
        probe_bytes[20:28] = struct.pack(">Q", int(self._virtual_did))
        probe_bytes[28:32] = b"\x00\x00\x00\x00"
        self._probe_msg = bytes(probe_bytes)
        self._read_buffer = bytearray(self.OT_MSG_LEN)

        self._available_net_ifs = set()
        self._broadcast_socks = {}
        self._local_port = None
        self._scan_timer = None
        self._last_scan_interval = None
        self._callbacks_device_status_changed = {}

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
                key="miot_lan",
                handler=self.__on_network_info_change_external_async
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
        await asyncio.sleep(self.OT_PROBE_INTERVAL_MIN/2)

    async def deinit_async(self):
        """Deinit."""
        if not self._init_done:
            _LOGGER.info("miot lan not init")
            return
        self._internal_loop.call_soon_threadsafe(self.__deinit)
        self._thread.join()
        self._internal_loop.close()

        self._lan_devices = {}
        self._broadcast_socks = {}
        self._local_port = None
        self._scan_timer = None
        self._last_scan_interval = None
        self._callbacks_device_status_changed = {}
        _LOGGER.info("miot lan deinit")

    async def get_devices_async(self) -> Dict[str, MIoTLanDeviceInfo]:
        """Get devices."""
        fut = asyncio.run_coroutine_threadsafe(coro=self.__get_devices_internal_async(), loop=self._internal_loop)
        return await asyncio.wrap_future(fut)

    async def register_status_changed_async(
        self, key: str, handler: Callable[[str, MIoTLanDeviceInfo, Any], Coroutine],
        handler_ctx: Any = None
    ) -> bool:
        """Register status changed."""
        if not self._init_done:
            return False
        self._internal_loop.call_soon_threadsafe(
            self.__register_status_changed,
            _MIoTLanRegDeviceData(key=key, handler=handler, handler_ctx=handler_ctx)
        )
        return True

    async def unregister_status_changed_async(self, key: str) -> bool:
        """Unregister status changed."""
        if not self._init_done:
            return False
        self._internal_loop.call_soon_threadsafe(self.__unregister_status_changed, _MIoTLanUnregDeviceData(key=key))
        return True

    async def ping_async(self, if_name: Optional[str] = None, target_ip: Optional[str] = None) -> None:
        """OTU Ping External."""
        if not self._init_done:
            return
        _LOGGER.debug("ping external async")
        fut = asyncio.run_coroutine_threadsafe(
            coro=asyncio.to_thread(self.ping_internal, if_name, target_ip),
            loop=self._internal_loop
        )
        await asyncio.wrap_future(fut)

    def ping_internal(self, if_name: Optional[str] = None, target_ip: Optional[str] = None) -> None:
        """OTU Ping, MUST call with internal loop."""
        self.__sendto(
            if_name=if_name, data=self._probe_msg,
            address=target_ip or "255.255.255.255",
            port=self.OT_PORT
        )

    def broadcast_device_info_changed(self, did: str, info: MIoTLanDeviceInfo) -> None:
        """Broadcast device info changed."""
        for handler in self._callbacks_device_status_changed.values():
            self._main_loop.call_soon_threadsafe(
                self._main_loop.create_task,
                handler.handler(did, info, handler.handler_ctx)
            )

    def __deinit(self) -> None:
        # Release all resources
        if self._scan_timer:
            self._scan_timer.cancel()
            self._scan_timer = None
        for device in self._lan_devices.values():
            device.on_delete()
        self._lan_devices.clear()
        self.__deinit_socket()
        self._internal_loop.stop()

    def __internal_loop_thread(self) -> None:
        _LOGGER.info("miot lan thread start")
        self.__init_socket()
        self._scan_timer = self._internal_loop.call_later(int(3*random.random()), self.__scan_devices)
        self._internal_loop.run_forever()
        _LOGGER.info("miot lan thread exit")

    def __init_socket(self) -> None:
        self.__deinit_socket()
        for if_name in self._net_ifs:
            if if_name not in self._available_net_ifs:
                return
            self.__create_socket(if_name=if_name)

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
            # Set SO_BINDTODEVICE
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, if_name.encode())
            sock.bind(("", self._local_port or 0))
            self._internal_loop.add_reader(sock.fileno(), self.__socket_read_handler, (if_name, sock))
            self._broadcast_socks[if_name] = sock
            self._local_port = self._local_port or sock.getsockname()[1]
            _LOGGER.info("created socket, %s, %s", if_name, self._local_port)
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error("create socket error, %s, %s", if_name, err)

    def __deinit_socket(self) -> None:
        for if_name in list(self._broadcast_socks.keys()):
            self.__destroy_socket(if_name)
        self._broadcast_socks.clear()

    def __destroy_socket(self, if_name: str) -> None:
        sock = self._broadcast_socks.pop(if_name, None)
        if not sock:
            return
        self._internal_loop.remove_reader(sock.fileno())
        sock.close()
        _LOGGER.info("destroyed socket, %s", if_name)

    def __socket_read_handler(self, ctx: tuple[str, socket.socket]) -> None:
        try:
            data_len, addr = ctx[1].recvfrom_into(self._read_buffer, self.OT_MSG_LEN, socket.MSG_DONTWAIT)
            if data_len < 0:
                # Socket error
                _LOGGER.error("socket read error, %s, %s", ctx[0], data_len)
                return
            if addr[1] != self.OT_PORT:
                # Not ot msg
                return
            self.__raw_message_handler(self._read_buffer[:data_len], data_len, addr[0], ctx[0])
        except Exception as err:  # pylint: disable=broad-exception-caught
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

    def __sendto(
        self, if_name: Optional[str], data: bytes, address: str, port: int
    ) -> None:
        if if_name is None:
            # Broadcast
            for if_n, sock in self._broadcast_socks.items():
                _LOGGER.debug("send broadcast, %s", if_n)
                sock.sendto(data, socket.MSG_DONTWAIT, (address, port))
        else:
            # Unicast
            sock = self._broadcast_socks.get(if_name, None)
            if not sock:
                _LOGGER.error("invalid socket, %s", if_name)
                return
            sock.sendto(data, socket.MSG_DONTWAIT, (address, port))

    def __scan_devices(self) -> None:
        if self._scan_timer:
            self._scan_timer.cancel()
            self._scan_timer = None
        try:
            # Scan devices
            self.ping_internal()
        except Exception as err:  # pylint: disable=broad-exception-caught
            # Ignore any exceptions to avoid blocking the loop
            _LOGGER.error("ping device error, %s", err)
        scan_time = self.__get_next_scan_time()
        self._scan_timer = self._internal_loop.call_later(scan_time, self.__scan_devices)
        _LOGGER.debug("next scan time: %ss", scan_time)

    def __get_next_scan_time(self) -> float:
        if not self._last_scan_interval:
            self._last_scan_interval = self.OT_PROBE_INTERVAL_MIN
        self._last_scan_interval = min(self._last_scan_interval*2, self.OT_PROBE_INTERVAL_MAX)
        return self._last_scan_interval

    async def __on_network_info_change_external_async(
        self,
        status: InterfaceStatus,
        info: NetworkInfo
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
        self._internal_loop.call_soon_threadsafe(
            self.__on_network_info_change,
            _MIoTLanNetworkUpdateData(status=status, if_name=info.name)
        )

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
                ip=lan_device.ip
            )
        return devices
