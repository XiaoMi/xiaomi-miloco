# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT network utilities.
"""
import asyncio
import ipaddress
import logging
import platform
import socket
import subprocess
from typing import Callable, Coroutine, Optional
import aiohttp
import psutil

from miot.types import NetworkInfo, InterfaceStatus

_LOGGER = logging.getLogger(__name__)


class MIoTNetwork:
    """MIoT network utilities."""
    _IP_ADDRESS_LIST: list[str] = [
        "1.2.4.8",          # CNNIC sDNS
        "8.8.8.8",          # Google Public DNS
        "9.9.9.9"           # Quad9
    ]
    _URL_ADDRESS_LIST: list[str] = [
        "https://www.bing.com",
        "https://www.google.com",
        "https://www.baidu.com"
    ]
    _REFRESH_INTERVAL = 30
    _DETECT_TIMEOUT = 6

    _main_loop: asyncio.AbstractEventLoop

    _ip_addr_map: dict[str, float]
    _http_addr_map: dict[str, float]
    _http_session: aiohttp.ClientSession

    _refresh_interval: int
    _refresh_task: Optional[asyncio.Task]
    _refresh_timer: Optional[asyncio.TimerHandle]

    _network_status: bool
    _network_info: dict[str, NetworkInfo]

    _callbacks_status_changed: dict[str, Callable[[bool], Coroutine]]
    _callbacks_info_changed: dict[str, Callable[[InterfaceStatus, NetworkInfo], Coroutine]]
    _done_event: asyncio.Event

    def __init__(
        self,
        ip_addr_list: Optional[list[str]] = None,
        url_addr_list: Optional[list[str]] = None,
        refresh_interval: Optional[int] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._main_loop = loop or asyncio.get_running_loop()
        self._ip_addr_map = {ip: self._DETECT_TIMEOUT for ip in ip_addr_list or self._IP_ADDRESS_LIST}
        self._http_addr_map = {url: self._DETECT_TIMEOUT for url in url_addr_list or self._URL_ADDRESS_LIST}
        self._http_session = aiohttp.ClientSession()
        self._refresh_interval = refresh_interval or self._REFRESH_INTERVAL

        self._refresh_task = None
        self._refresh_timer = None

        self._network_status = False
        self._network_info = {}

        self._callbacks_status_changed = {}
        self._callbacks_info_changed = {}

        self._done_event = asyncio.Event()

    async def init_async(self) -> bool:
        """Init."""
        self.__refresh_timer_handler()
        # MUST get network info before starting
        return await self._done_event.wait()

    async def deinit_async(self) -> None:
        """Deinit."""
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self._refresh_timer:
            self._refresh_timer.cancel()
            self._refresh_timer = None
        await self._http_session.close()

        self._network_status = False
        self._network_info.clear()
        self._callbacks_status_changed.clear()
        self._callbacks_info_changed.clear()
        self._done_event.clear()

    @property
    def network_status(self) -> bool:
        """Network status."""
        return self._network_status

    @property
    def network_info(self) -> dict[str, NetworkInfo]:
        """Network info."""
        return self._network_info

    async def update_addr_list_async(
        self,
        ip_addr_list: Optional[list[str]] = None,
        url_addr_list: Optional[list[str]] = None,
    ) -> None:
        """Update address list."""
        new_ip_map: dict = {}
        for ip in ip_addr_list or self._IP_ADDRESS_LIST:
            if ip in self._ip_addr_map:
                new_ip_map[ip] = self._ip_addr_map[ip]
            else:
                new_ip_map[ip] = self._DETECT_TIMEOUT
        self._ip_addr_map = new_ip_map
        new_url_map: dict = {}
        for url in url_addr_list or self._URL_ADDRESS_LIST:
            if url in self._http_addr_map:
                new_url_map[url] = self._http_addr_map[url]
            else:
                new_url_map[url] = self._DETECT_TIMEOUT
        self._http_addr_map = new_url_map

    async def register_status_changed_async(self, key: str, handler: Callable[[bool], Coroutine]) -> None:
        """Subscribe network status."""
        self._callbacks_status_changed[key] = handler

    async def unregister_status_changed_async(self, key: str) -> None:
        """Unsubscribe network status."""
        self._callbacks_status_changed.pop(key, None)

    async def register_info_changed_async(
        self, key: str, handler: Callable[[InterfaceStatus, NetworkInfo], Coroutine]
    ) -> None:
        """Register network info."""
        self._callbacks_info_changed[key] = handler

    async def unregister_info_changed_async(self, key: str) -> None:
        """Register network info."""
        self._callbacks_info_changed.pop(key, None)

    async def refresh_async(self) -> None:
        """Refresh network status."""
        self.__refresh_timer_handler()

    async def get_status_async(self) -> bool:
        """Get network status."""
        try:
            ip_addr: str = ""
            ip_ts: float = self._DETECT_TIMEOUT
            for ip, ts in self._ip_addr_map.items():
                if ts < ip_ts:
                    ip_addr = ip
                    ip_ts = ts
            if ip_ts < self._DETECT_TIMEOUT and await self.ping_multi_async(ip_list=[ip_addr]):
                return True
            url_addr: str = ""
            url_ts: float = self._DETECT_TIMEOUT
            for http, ts in self._http_addr_map.items():
                if ts < url_ts:
                    url_addr = http
                    url_ts = ts
            if url_ts < self._DETECT_TIMEOUT and await self.http_multi_async(url_list=[url_addr]):
                return True
            # Detect all addresses
            results = await asyncio.gather(*[self.ping_multi_async(), self.http_multi_async()])
            return any(results)
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error("get network status error, %s", err)
        return False

    async def get_info_async(self) -> dict[str, NetworkInfo]:
        """Get network info."""
        return await self._main_loop.run_in_executor(None, self.__get_network_info)

    async def ping_multi_async(self, ip_list: Optional[list[str]] = None) -> bool:
        """Ping multi addresses."""
        addr_list = ip_list or list(self._ip_addr_map.keys())
        tasks = []
        for addr in addr_list:
            tasks.append(self.__ping_async(addr))
        results = await asyncio.gather(*tasks)
        for addr, ts in zip(addr_list, results):
            if addr in self._ip_addr_map:
                self._ip_addr_map[addr] = ts
        return any(ts < self._DETECT_TIMEOUT for ts in results)

    async def http_multi_async(
        self, url_list: Optional[list[str]] = None
    ) -> bool:
        """Http request multi addresses."""
        addr_list = url_list or list(self._http_addr_map.keys())
        tasks = []
        for addr in addr_list:
            tasks.append(self.__http_async(url=addr))
        results = await asyncio.gather(*tasks)
        for addr, ts in zip(addr_list, results):
            if addr in self._http_addr_map:
                self._http_addr_map[addr] = ts
        return any(ts < self._DETECT_TIMEOUT for ts in results)

    def __calc_network_address(self, ip: str, netmask: str) -> str:
        return str(ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False).network_address)

    async def __ping_async(self, address: Optional[str] = None) -> float:
        start_ts: float = self._main_loop.time()
        try:
            process = await asyncio.create_subprocess_exec(
                *(
                    ["ping", "-n", "1", "-w", str(self._DETECT_TIMEOUT*1000), address]
                    if platform.system().lower() == "windows" else
                    ["ping", "-c", "1", "-w", str(self._DETECT_TIMEOUT), address]
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            await process.communicate()
            if process.returncode == 0:
                return self._main_loop.time() - start_ts
            return self._DETECT_TIMEOUT
        except Exception as err:  # pylint: disable=broad-exception-caught
            print(err)
            return self._DETECT_TIMEOUT

    async def __http_async(self, url: str) -> float:
        start_ts: float = self._main_loop.time()
        try:
            async with self._http_session.get(url, timeout=self._DETECT_TIMEOUT):
                return self._main_loop.time() - start_ts
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        return self._DETECT_TIMEOUT

    def __get_network_info(self) -> dict[str, NetworkInfo]:
        interfaces = psutil.net_if_addrs()
        results: dict[str, NetworkInfo] = {}
        for name, addresses in interfaces.items():
            # Skip hassio and docker* interface
            if name == "hassio" or name.startswith("docker"):
                continue
            for address in addresses:
                if address.family != socket.AF_INET or not address.address or not address.netmask:
                    continue
                # skip lo interface
                if address.address == "127.0.0.1":
                    continue
                results[name] = NetworkInfo(
                    name=name,
                    ip=address.address,
                    netmask=address.netmask,
                    net_seg=self.__calc_network_address(address.address, address.netmask)
                )
        return results

    def __call_network_info_change(
        self, status: InterfaceStatus, info: NetworkInfo
    ) -> None:
        for handler in self._callbacks_info_changed.values():
            self._main_loop.create_task(handler(status, info))

    async def __update_status_and_info_async(self) -> None:
        try:
            status: bool = await self.get_status_async()
            infos = await self.get_info_async()

            if self._network_status != status:
                for handler in self._callbacks_status_changed.values():
                    self._main_loop.create_task(handler(status))
                self._network_status = status

            for name in list(self._network_info.keys()):
                info = infos.pop(name, None)
                if info:
                    # Update
                    if info.ip != self._network_info[name].ip or info.netmask != self._network_info[name].netmask:
                        self._network_info[name] = info
                        self.__call_network_info_change(InterfaceStatus.UPDATE, info)
                else:
                    # Remove
                    self.__call_network_info_change(InterfaceStatus.REMOVE, self._network_info.pop(name))
            # Add
            for name, info in infos.items():
                self._network_info[name] = info
                self.__call_network_info_change(InterfaceStatus.ADD, info)

            if not self._done_event.is_set():
                self._done_event.set()
        except asyncio.CancelledError:
            _LOGGER.error("update_status_and_info task was cancelled")

    def __refresh_timer_handler(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.cancel()
            self._refresh_timer = None
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self._main_loop.create_task(self.__update_status_and_info_async())
        self._refresh_timer = self._main_loop.call_later(self._refresh_interval, self.__refresh_timer_handler)
