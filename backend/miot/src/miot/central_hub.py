# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
Central hub gateway (中枢网关) coordinator.

Ties together the pieces of the local control path so ``MIoTClient`` and the
business layer don't have to:

  - ``MIoTCert``   — mTLS client cert lifecycle (sign on first use, auto-renew).
  - ``MdnsService`` — discover main gateways (``_miot-central``, role=1).
  - ``MipsLocalClient`` — one local MQTT connection per discovered ``group_id``.
  - a merged device table ``did -> {group_id, online, specv2_access,
    push_available}`` kept fresh from ``getDevList`` + ``devListChange``.

Public surface used by the rest of the SDK / business layer:
  - ``enabled`` / ``is_ready``          — region gate + at least one live gateway
  - ``local_device(did)``               — routing info for a did (or None)
  - ``can_control(did)`` / ``can_push`` — routing predicates
  - ``set_prop_async`` / ``get_prop_async`` / ``action_async`` — local control
  - ``on_dev_list_changed`` (callback)  — fires when the device table changes

Central hub control is only supported in mainland China; outside
``SUPPORT_CENTRAL_GATEWAY_CTRL`` the manager stays disabled and every predicate
returns False, so callers transparently fall back to the cloud path.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any, Callable, Coroutine, Optional

from .cert import MIoTCert
from .cloud import MIoTHttpClient
from .const import MIHOME_CERT_EXPIRE_MARGIN, SUPPORT_CENTRAL_GATEWAY_CTRL
from .mdns import MdnsService, MdnsServiceState
from .mips_local import MipsLocalClient
from .storage import MIoTStorage
from .types import MipsConnectionError

_LOGGER = logging.getLogger(__name__)

# Storage location of the persistent per-instance virtual did.
_VIRTUAL_DID_DOMAIN = "cert"
_VIRTUAL_DID_NAME = "virtual_did.txt"

# After a local RPC for a did fails/times out, skip the local path for that did
# for this long and route straight to cloud. Bounds a flaky device to one
# timeout per window instead of N; self-heals when the window lapses.
_LOCAL_COOLDOWN_SEC = 30.0

# Callback: (added_dids, removed_dids) -> awaitable. Fires after the device
# table changes (gateway discovered, devListChange, getDevList refresh).
DevListChangedHandler = Callable[[list, list], Coroutine]


class CentralHubManager:
    """Owns cert lifecycle, mDNS discovery, and per-group local MQTT clients."""

    def __init__(
        self,
        storage: MIoTStorage,
        http_client: MIoTHttpClient,
        uid: str,
        cloud_server: str,
        static_gateways: Optional[list[tuple[str, int]]] = None,
        virtual_did: Optional[str] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._storage = storage
        self._http_client = http_client
        self._uid = uid
        self._cloud_server = cloud_server
        # User-configured gateway endpoints (host, port) used in addition to
        # mDNS — for environments where mDNS multicast can't reach the gateway
        # (e.g. a container on a different subnet). Trusted (no ownership
        # filter); the mTLS connect itself validates they are our main hub.
        self._static_gateways = list(static_gateways or [])
        # Client identity. Preferably injected by the caller (Miloco persists it
        # in its KV store); if absent, fall back to a self-managed file-backed
        # one so the SDK stays usable standalone.
        self._injected_virtual_did = (virtual_did or "").strip() or None
        self._main_loop = loop or asyncio.get_running_loop()

        self._enabled = cloud_server in SUPPORT_CENTRAL_GATEWAY_CTRL
        self._cert = MIoTCert(storage, uid, cloud_server, loop=self._main_loop)
        self._mdns: Optional[MdnsService] = None
        self._virtual_did: Optional[str] = None
        self._refresh_cert_timer: Optional[asyncio.TimerHandle] = None

        # did -> monotonic expiry: local path skipped (route cloud) for a did
        # whose recent local RPC failed/timed out, until the window lapses.
        self._local_cooldown: dict[str, float] = {}
        # group_id -> live local MQTT client
        self._clients: dict[str, MipsLocalClient] = {}
        # did -> {group_id, online, specv2_access, push_available}
        self._dev_table: dict[str, dict] = {}
        # group_ids of homes this account owns. A gateway only authorizes its
        # owner's account (mTLS CONNACK 0x87 "Not authorized" otherwise), and
        # mDNS surfaces every gateway on the LAN — including neighbors'. So we
        # only connect to gateways whose group_id is in this set.
        self._owned_group_ids: set[str] = set()

        self._on_dev_list_changed: Optional[DevListChangedHandler] = None
        self._started = False

    # ------------------------------------------------------------------ props

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_ready(self) -> bool:
        """True when the local path is usable (enabled + a live gateway)."""
        return self._enabled and any(c.is_connected for c in self._clients.values())

    @property
    def on_dev_list_changed(self) -> Optional[DevListChangedHandler]:
        return self._on_dev_list_changed

    @on_dev_list_changed.setter
    def on_dev_list_changed(self, func: Optional[DevListChangedHandler]) -> None:
        self._on_dev_list_changed = func

    # --------------------------------------------------------------- lifecycle

    async def init_async(self) -> None:
        """Start cert lifecycle + mDNS discovery (no-op outside cn)."""
        if not self._enabled:
            _LOGGER.info(
                "central hub disabled for cloud_server=%s (only %s)",
                self._cloud_server,
                SUPPORT_CENTRAL_GATEWAY_CTRL,
            )
            return
        if self._started:
            return
        self._started = True

        self._virtual_did = (
            self._injected_virtual_did or await self.__ensure_virtual_did()
        )
        await self.__refresh_owned_group_ids()
        if not await self.__refresh_cert():
            _LOGGER.error(
                "central hub: client cert not ready; local control disabled "
                "(will still run mDNS and retry cert on next connect)"
            )
        try:
            self._mdns = MdnsService(loop=self._main_loop)
            await self._mdns.init_async()
            self._mdns.sub_service_change("central_hub", "*", self.__on_service_change)
            _LOGGER.info("central hub: mDNS discovery started")
        except Exception as e:
            _LOGGER.error("central hub: mDNS init failed: %s", e)

        # Connect user-configured gateways directly (mDNS-independent). Trusted,
        # so no ownership filter; a synthetic group_id keys the client (the
        # gateway's real group_id is only in the mDNS profile, which we may not
        # have here). The mTLS connect validates it is actually our main hub.
        for host, port in self._static_gateways:
            try:
                await self.__ensure_client(f"static:{host}", host, port)
            except Exception as e:
                _LOGGER.error(
                    "central hub: static gateway %s:%d connect failed: %s",
                    host,
                    port,
                    e,
                )

    async def deinit_async(self) -> None:
        """Stop everything and clear state."""
        self._started = False
        if self._refresh_cert_timer:
            self._refresh_cert_timer.cancel()
            self._refresh_cert_timer = None
        if self._mdns:
            try:
                self._mdns.unsub_service_change("central_hub")
                await self._mdns.deinit_async()
            except Exception as e:
                _LOGGER.warning("central hub: mDNS deinit raised: %s", e)
            self._mdns = None
        for client in list(self._clients.values()):
            try:
                await client.deinit_async()
            except Exception as e:
                _LOGGER.warning("central hub: client deinit raised: %s", e)
        self._clients.clear()
        self._dev_table.clear()

    # ------------------------------------------------------------ routing API

    def local_device(self, did: str) -> Optional[dict]:
        """Routing info for a did, or None if not behind a live gateway."""
        return self._dev_table.get(did)

    def can_control(self, did: str) -> bool:
        """True if a control command for did should go local (vs cloud)."""
        info = self._dev_table.get(did)
        if not info or not info.get("online") or not info.get("specv2_access"):
            return False
        client = self._clients.get(info["group_id"])
        return bool(client and client.is_connected)

    def can_push(self, did: str) -> bool:
        """True if state pushes for did are available from the gateway."""
        info = self._dev_table.get(did)
        if not info or not info.get("online") or not info.get("push_available"):
            return False
        client = self._clients.get(info["group_id"])
        return bool(client and client.is_connected)

    def local_control_ready(self, did: str) -> bool:
        """``can_control`` AND not in a recent local-failure cooldown.

        The routing layer uses this instead of ``can_control`` so a device that
        is nominally controllable but currently unreachable does not make every
        call pay the full local RPC timeout — after one failure it is routed to
        cloud for a window, then retried.
        """
        if not self.can_control(did):
            return False
        expiry = self._local_cooldown.get(did)
        if expiry is not None:
            if time.monotonic() < expiry:
                return False
            del self._local_cooldown[did]  # window lapsed — allow local again
        return True

    def note_local_failure(self, did: str) -> None:
        """Mark a did's local path as failed; route it to cloud for a window."""
        self._local_cooldown[did] = time.monotonic() + _LOCAL_COOLDOWN_SEC

    async def set_prop_async(self, did: str, siid: int, piid: int, value: Any) -> dict:
        return await self.__client_for(did).set_prop_async(did, siid, piid, value)

    async def get_prop_async(self, did: str, siid: int, piid: int) -> Any:
        return await self.__client_for(did).get_prop_async(did, siid, piid)

    async def action_async(
        self, did: str, siid: int, aiid: int, in_list: list
    ) -> dict:
        return await self.__client_for(did).action_async(did, siid, aiid, in_list)

    def __client_for(self, did: str) -> MipsLocalClient:
        info = self._dev_table.get(did)
        client = self._clients.get(info["group_id"]) if info else None
        if client is None or not client.is_connected:
            raise MipsConnectionError(f"no live gateway for did={did}")
        return client

    # --------------------------------------------------------------- internals

    async def __ensure_virtual_did(self) -> str:
        """Load the persisted virtual did, generating + persisting one if absent."""
        data = await self._storage.load_file_async(
            domain=_VIRTUAL_DID_DOMAIN, name_with_suffix=_VIRTUAL_DID_NAME
        )
        if data:
            did = data.decode("utf-8").strip()
            if did:
                return did
        did = str(secrets.randbits(64))
        await self._storage.save_file_async(
            domain=_VIRTUAL_DID_DOMAIN,
            name_with_suffix=_VIRTUAL_DID_NAME,
            data=did.encode("utf-8"),
        )
        _LOGGER.info("central hub: generated virtual did")
        return did

    async def __refresh_cert(self) -> bool:
        """Ensure a valid client cert, signing/renewing as needed.

        Reschedules itself ``MIHOME_CERT_EXPIRE_MARGIN`` before expiry. Returns
        True if a usable cert is in place.
        """
        if not self._enabled:
            return True
        try:
            if not await self._cert.verify_ca_cert_async():
                _LOGGER.error("central hub: CA cert not ready")
                return False
            # Pass the current did so a cert whose CN encodes a *different* did
            # (e.g. the identity changed / migrated) is treated as expired and
            # re-signed — the gateway rejects a client_id/CN mismatch.
            refresh_time = (
                await self._cert.user_cert_remaining_time_async(
                    did=self._virtual_did
                )
                - MIHOME_CERT_EXPIRE_MARGIN
            )
            if refresh_time <= 60:
                user_key = await self._cert.load_user_key_async()
                if not user_key:
                    user_key = self._cert.gen_user_key()
                    if not await self._cert.update_user_key_async(user_key):
                        _LOGGER.error("central hub: persist user key failed")
                        return False
                csr = self._cert.gen_user_csr(user_key, did=self._virtual_did)
                crt = await self._http_client.get_central_cert_async(csr)
                if not await self._cert.update_user_cert_async(crt):
                    _LOGGER.error("central hub: persist user cert failed")
                    return False
                refresh_time = (
                    await self._cert.user_cert_remaining_time_async(
                        did=self._virtual_did
                    )
                    - MIHOME_CERT_EXPIRE_MARGIN
                )
                if refresh_time <= 0:
                    _LOGGER.error("central hub: signed cert already near expiry")
                    return False
                _LOGGER.info("central hub: user cert signed/renewed")
            self.__schedule_cert_refresh(refresh_time)
            return True
        except Exception as e:
            _LOGGER.error("central hub: refresh cert failed: %s", e)
            return False

    def __schedule_cert_refresh(self, delay_sec: float) -> None:
        if self._refresh_cert_timer:
            self._refresh_cert_timer.cancel()
        self._refresh_cert_timer = self._main_loop.call_later(
            max(delay_sec, 60),
            lambda: self._main_loop.create_task(self.__refresh_cert()),
        )

    async def __refresh_owned_group_ids(self) -> None:
        """Fetch the group_ids of homes this account owns."""
        try:
            homes = await self._http_client.get_homes_async()
            self._owned_group_ids = {
                h.group_id for h in homes.values() if getattr(h, "group_id", None)
            }
            _LOGGER.info(
                "central hub: %d owned home group_ids", len(self._owned_group_ids)
            )
        except Exception as e:
            _LOGGER.error("central hub: fetch owned group_ids failed: %s", e)

    async def __on_service_change(
        self, group_id: str, state: MdnsServiceState, data: dict
    ) -> None:
        if state == MdnsServiceState.REMOVED:
            return
        # Only connect to gateways this account owns. mDNS surfaces every
        # gateway on the LAN (incl. neighbors'); a non-owned gateway rejects
        # our mTLS CONNACK with "Not authorized". Refresh once on a miss in
        # case a home was added after startup.
        if group_id not in self._owned_group_ids:
            await self.__refresh_owned_group_ids()
            if group_id not in self._owned_group_ids:
                _LOGGER.debug(
                    "central hub: gateway %s not owned by this account, skip",
                    group_id,
                )
                return
        addresses = data.get("addresses") or []
        port = data.get("port")
        if not addresses or not port:
            _LOGGER.warning("central hub: gateway %s missing address/port", group_id)
            return
        await self.__ensure_client(group_id, addresses[0], int(port))

    async def __ensure_client(self, group_id: str, host: str, port: int) -> None:
        existing = self._clients.get(group_id)
        if existing is not None:
            if existing.host == host and existing.is_connected:
                return
            # Address changed or dead — replace.
            try:
                await existing.deinit_async()
            except Exception:
                pass
            self._clients.pop(group_id, None)

        # Dedup by host: the same physical gateway may be reached both via mDNS
        # (real group_id) and static config (synthetic "static:host"). Only one
        # connection to a given broker.
        for gid, client in self._clients.items():
            if gid != group_id and client.host == host and client.is_connected:
                _LOGGER.debug(
                    "central hub: host %s already connected as %s, skip %s",
                    host,
                    gid,
                    group_id,
                )
                return

        if not self._virtual_did:
            _LOGGER.error("central hub: no virtual did; cannot connect gateway")
            return
        # A cert may not have been ready at init; make sure it is now.
        if await self._cert.user_cert_remaining_time_async() <= 0:
            if not await self.__refresh_cert():
                _LOGGER.error(
                    "central hub: cert unavailable, skip gateway %s", group_id
                )
                return

        client = MipsLocalClient(
            did=self._virtual_did,
            host=host,
            group_id=group_id,
            ca_file=self._cert.ca_file,
            cert_file=self._cert.cert_file,
            key_file=self._cert.key_file,
            port=port,
            loop=self._main_loop,
        )
        client.on_dev_list_changed = self.__on_client_dev_list_changed
        try:
            await client.init_async()
        except MipsConnectionError as e:
            _LOGGER.error("central hub: connect gateway %s failed: %s", group_id, e)
            return
        self._clients[group_id] = client
        _LOGGER.info("central hub: gateway %s connected (%s)", group_id, host)
        await self.__refresh_dev_list(client)

    async def __on_client_dev_list_changed(
        self, client: MipsLocalClient, dev_list: list
    ) -> None:
        await self.__refresh_dev_list(client)

    async def __refresh_dev_list(self, client: MipsLocalClient) -> None:
        """Re-pull getDevList for one gateway and reconcile the device table."""
        try:
            devices = await client.get_dev_list_async()
        except Exception as e:
            _LOGGER.error(
                "central hub: getDevList failed for %s: %s", client.group_id, e
            )
            return
        group_id = client.group_id
        added: list = []
        removed: list = []
        # Upsert everything reported by this gateway.
        for did, info in devices.items():
            if did not in self._dev_table:
                added.append(did)
            self._dev_table[did] = {
                "group_id": group_id,
                "online": info.get("online", False),
                "specv2_access": info.get("specv2_access", False),
                "push_available": info.get("push_available", False),
            }
        # Drop dids previously under this gateway but no longer present.
        for did in list(self._dev_table.keys()):
            if self._dev_table[did]["group_id"] == group_id and did not in devices:
                del self._dev_table[did]
                removed.append(did)
        _LOGGER.info(
            "central hub: %s device table +%d -%d (total %d)",
            group_id,
            len(added),
            len(removed),
            len(self._dev_table),
        )
        if (added or removed) and self._on_dev_list_changed:
            try:
                await self._on_dev_list_changed(added, removed)
            except Exception as e:
                _LOGGER.error("central hub: dev-list-changed handler raised: %s", e)
