# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
mDNS server for MIoT.
"""

import asyncio
import base64
import binascii
import copy
import logging
from enum import Enum
from typing import Callable, Coroutine, Dict, List, Optional, Tuple

from zeroconf import DNSQuestionType, IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

_LOGGER = logging.getLogger(__name__)

# mDNS service type broadcast by the main central hub gateway (role=1).
MIPS_MDNS_TYPE = "_miot-central._tcp.local."

MDNS_SUPPORT_TYPE_LIST = {
    MIPS_MDNS_TYPE: {"name": "MIoT Central Service"},
    "_home-assistant._tcp.local.": {"name": "Home Assistant Service"},
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
        self.addresses = service_info.parsed_addresses(version=IPVersion.V4Only)
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
    """mDNS service discovery."""

    _aiozc: AsyncZeroconf
    _main_loop: asyncio.AbstractEventLoop
    _aio_browser: AsyncServiceBrowser
    # group_id -> service data dict (see MipsServiceData.to_dict)
    _services: Dict[str, dict]
    # (key, group_id) -> handler(group_id, state, data). group_id may be "*".
    _sub_list: Dict[Tuple[str, str], Callable[[str, "MdnsServiceState", dict], Coroutine]]

    def __init__(
        self,
        aiozc: Optional[AsyncZeroconf] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._aiozc = aiozc or AsyncZeroconf()
        self._main_loop = loop or asyncio.get_running_loop()
        self._services = {}
        self._sub_list = {}

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
            question_type=DNSQuestionType.QM,
        )

    async def deinit_async(self) -> None:
        """Deinit mDNS service."""
        await self._aio_browser.async_cancel()
        self._services = {}
        self._sub_list = {}

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

    def __on_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        _LOGGER.debug(
            "mdns service state changed, %s, %s, %s", state_change, name, service_type
        )
        # Ignore mDNS REMOVED packets — let the local MQTT connection close by
        # itself (matches the Xiaomi Home integration behavior).
        if state_change is ServiceStateChange.Removed:
            _LOGGER.info("service removed: %s", name)
            return
        # Only the central hub gateway type carries a mips profile.
        if service_type != MIPS_MDNS_TYPE:
            return
        self._main_loop.create_task(
            self.__request_service_info_async(zeroconf, service_type, name)
        )

    async def __request_service_info_async(
        self, zeroconf: Zeroconf, service_type: str, name: str
    ) -> None:
        info = AsyncServiceInfo(service_type, name)
        await info.async_request(
            zeroconf, MIPS_MDNS_REQUEST_TIMEOUT_MS, question_type=DNSQuestionType.QU
        )
        try:
            service_data = MipsServiceData(info)
            # Only the main gateway (role=1) that advertises the mqtt suite is
            # connectable; sub-gateways and blind gateways are filtered out.
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
                self.__call_service_change(
                    MdnsServiceState.ADDED, self._services[group_id]
                )
        except MdnsServiceError as error:
            _LOGGER.info("invalid mips service, %s, %s", error, name)

    def __call_service_change(self, state: "MdnsServiceState", data: dict) -> None:
        _LOGGER.info("call service change, %s, %s", state, data)
        for keys in list(self._sub_list.keys()):
            if keys[1] in (data.get("group_id"), "*"):
                self._main_loop.create_task(
                    self._sub_list[keys](data["group_id"], state, data)
                )
