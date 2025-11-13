# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
mDNS server for MIoT.
"""
import asyncio
import base64
import binascii
from enum import Enum
from typing import Dict, List, Optional
import logging

from zeroconf import (
    DNSQuestionType,
    IPVersion,
    ServiceStateChange,
    Zeroconf)
from zeroconf.asyncio import (
    AsyncServiceInfo,
    AsyncZeroconf,
    AsyncServiceBrowser)


_LOGGER = logging.getLogger(__name__)

MDNS_SUPPORT_TYPE_LIST = {
    "_miot-central._tcp.local.": {"name": "MIoT Central Service"},
    "_home-assistant._tcp.local.": {"name": "Home Assistant Service"}
}

MIPS_MDNS_REQUEST_TIMEOUT_MS = 5000
MIPS_MDNS_UPDATE_INTERVAL_S = 600


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
    """Mips service data."""
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

    def __init__(self, service_info: AsyncServiceInfo) -> None:
        if service_info is None:
            raise MdnsServiceError("invalid params")
        properties: Dict = service_info.decoded_properties
        if not properties:
            raise MdnsServiceError("invalid service properties")
        self.profile = properties.get("profile", "")
        if not self.profile:
            raise MdnsServiceError("invalid service profile")
        self.profile_bin = base64.b64decode(self.profile)
        self.name = service_info.name
        self.addresses = service_info.parsed_addresses(
            version=IPVersion.V4Only)
        if not self.addresses:
            raise MdnsServiceError("invalid addresses")
        self.addresses.sort()
        if not service_info.port:
            raise MdnsServiceError("invalid port")
        self.port = service_info.port
        self.type = service_info.type
        self.server = service_info.server or ""
        # Parse profile
        self.did = str(int.from_bytes(self.profile_bin[1:9], byteorder="big"))
        self.group_id = binascii.hexlify(
            self.profile_bin[9:17][::-1]).decode("utf-8")
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
            "suite_mqtt": self.suite_mqtt
        }

    def __str__(self) -> str:
        return str(self.to_dict())


class MdnsService:
    """mDNS service discovery."""
    _aiozc: AsyncZeroconf
    _main_loop: asyncio.AbstractEventLoop
    _aio_browser: AsyncServiceBrowser

    def __init__(
        self, aiozc: Optional[AsyncZeroconf] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._aiozc = aiozc or AsyncZeroconf()
        self._main_loop = loop or asyncio.get_running_loop()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Async exit."""
        await self.deinit_async()

    async def init_async(self) -> None:
        """Init mDNS service."""
        await self._aiozc.zeroconf.async_wait_for_start()

        self._aio_browser = AsyncServiceBrowser(
            zeroconf=self._aiozc.zeroconf,
            type_=list(MDNS_SUPPORT_TYPE_LIST.keys()),
            handlers=[self.__on_service_state_change],
            question_type=DNSQuestionType.QM)

    async def deinit_async(self) -> None:
        """Deinit mDNS service."""
        await self._aio_browser.async_cancel()

    def __on_service_state_change(
            self, zeroconf: Zeroconf, service_type: str, name: str,
            state_change: ServiceStateChange
    ) -> None:
        _LOGGER.debug(
            "mdns service state changed, %s, %s, %s", state_change, name, service_type)
        if state_change is ServiceStateChange.Removed:
            _LOGGER.info("service removed: %s", name)
            return
        self._main_loop.create_task(
            self.__request_service_info_async(zeroconf, service_type, name))

    async def __request_service_info_async(
        self, zeroconf: Zeroconf, service_type: str, name: str
    ) -> None:
        pass
