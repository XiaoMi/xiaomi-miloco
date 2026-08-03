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

# After a local RPC for a did fails/times out, skip the local path for that did
# for this long and route straight to cloud. Bounds a flaky device to one
# timeout per window instead of N; self-heals when the window lapses.
_LOCAL_COOLDOWN_SEC = 30.0

# Minimum spacing between owned-group refetches triggered by discovering an
# unowned gateway. Bounds the cloud calls when many neighbor gateways are on the
# LAN; a genuinely new home still gets picked up within this window / on the
# next periodic refresh.
_OWNED_REFRESH_MIN_INTERVAL_SEC = 60.0

# Periodically retry connecting to desired-but-unconnected gateways (static or
# mDNS-discovered + owned). A single transient connect failure at discovery
# time (e.g. a cold-ARP "No route to host" the instant a gateway is found)
# would otherwise leave it permanently disconnected — mDNS re-announces carry
# the same data and don't re-fire the change callback.
_RECONNECT_INTERVAL_SEC = 20.0
# 证书续签瞬态失败后的退避重试间隔:保证续签链在云端超时/网络抖动后仍能自愈。
_CERT_REFRESH_RETRY_BACKOFF = 300.0

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
        home_ids_provider: Optional[Callable[[], set[str]]] = None,
        network: Any = None,
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
        # in its KV store); if absent, an ephemeral one is used so the SDK stays
        # usable standalone.
        self._injected_virtual_did = (virtual_did or "").strip() or None
        # Optional home-scope filter: returns the set of home_ids the caller
        # (Miloco) currently has enabled. When set, only gateways in those homes
        # are connected — the account-owned group_ids are intersected with them
        # (empty set → connect to nothing). None → all account-owned homes.
        self._home_ids_provider = home_ids_provider
        # MIoTNetwork (same instance lan.py uses) — the mDNS discovery pulls its
        # active-interface list + IPv4 from here so it emits queries out the
        # right NIC on a multi-homed host.
        self._network = network
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
        # did -> home group_id over all owned homes; lets a static gateway
        # (config has only an IP) be mapped to a home via its devices.
        self._did_group_map: dict[str, str] = {}
        # Per-group_id asyncio.Lock: prevents concurrent connect attempts for
        # the same gateway (mDNS callback + 20s reconnect sweep) from racing,
        # both creating MipsLocalClient instances, and leaking the loser.
        self._ensure_locks: dict[str, asyncio.Lock] = {}
        # Static gateway group_ids that were connected and then dropped
        # because their home is not in the enabled set. Recorded so the
        # 20s reconnect sweep doesn't create a connect→drop infinite loop.
        self._static_rejected: set[str] = set()
        # monotonic time of the last owned-group refresh, to throttle the
        # refetch triggered when an unowned gateway is discovered (a dense LAN
        # can surface many neighbor gateways, each otherwise a cloud round-trip).
        self._owned_refreshed_at: float = 0.0

        self._on_dev_list_changed: Optional[DevListChangedHandler] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        # group_ids we've already logged an "Not authorized" hint for, so the
        # 20s reconnect sweep doesn't spam it. Cleared on a successful connect.
        self._auth_rejected: set[str] = set()
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

        if self._injected_virtual_did:
            self._virtual_did = self._injected_virtual_did
        else:
            # No did injected — SDK used standalone. Use an ephemeral did (not
            # persisted); the business layer (miloco) normally injects a
            # KV-persisted did, which is the single source of truth.
            self._virtual_did = str(secrets.randbits(64))
            _LOGGER.warning(
                "central hub: no virtual did injected; using an ephemeral one "
                "(inject a stable did for a persistent identity)"
            )
        await self.__refresh_owned_group_ids()
        if not await self.__refresh_cert():
            _LOGGER.error(
                "central hub: client cert not ready; local control disabled "
                "(will still run mDNS and retry cert on next connect)"
            )
        try:
            self._mdns = MdnsService(loop=self._main_loop, network=self._network)
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

        self._reconnect_task = self._main_loop.create_task(self.__reconnect_loop())

    async def deinit_async(self) -> None:
        """Stop everything and clear state."""
        self._started = False
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass  # cancel() 是我们主动发的,await 只是等它退出,吞掉是正常语义
            self._reconnect_task = None
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
        # scope 切换(切家/自动选家)后这些状态都是旧家庭的残留,一并清掉:
        # _ensure_locks(过期的 per-group lock)、_auth_rejected(旧网关的"未授权"
        # 抑制,切换后应重新允许告警)、_local_cooldown(旧 did 的冷却窗口)。
        self._ensure_locks.clear()
        self._auth_rejected.clear()
        self._local_cooldown.clear()

    async def refresh_scope_async(self) -> None:
        """Re-evaluate the connectable-home scope after a scope change (e.g. a
        home switch): tear down current gateway connections + discovery and
        re-run init, so the owned-group filter (read live via
        ``home_ids_provider``) is recomputed and only the now-enabled homes'
        gateways are (re)connected. No-op outside cn.

        A full re-init is used deliberately (rather than an incremental
        reconcile): a home switch already re-pulls the device/camera/scene
        lists, so re-pulling the local device table is in kind, and re-init
        reuses the audited connect path. The cert is cached on disk, so no
        re-sign happens; there is a brief window where local control falls back
        to cloud while gateways reconnect.
        """
        if not self._enabled:
            return
        _LOGGER.info("central hub: refreshing home scope (re-init)")
        self._static_rejected.clear()
        await self.deinit_async()
        await self.init_async()

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

    def in_local_cooldown(self, did: str) -> bool:
        """True if did is within a recent local-timeout cooldown window.

        A local RPC **timeout** (not other errors — those fail fast and just
        retry on cloud) cools the did down for a short window. During the window
        the routing layer skips local and routes the did's requests to cloud,
        so a device that has gone flaky on the LAN stays controllable without
        paying the full RPC timeout on every call. Bounds a flaky device's batch
        to one timeout; self-heals when the window lapses.
        """
        expiry = self._local_cooldown.get(did)
        if expiry is None:
            return False
        if time.monotonic() < expiry:
            return True
        del self._local_cooldown[did]  # window lapsed
        return False

    def note_local_failure(self, did: str) -> None:
        """Arm the cooldown after a local RPC timeout → route the did to cloud
        for a window (called only on timeout, not on fast failures)."""
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

    async def __refresh_cert(self) -> bool:
        """Ensure a valid client cert, signing/renewing as needed.

        Reschedules itself ``MIHOME_CERT_EXPIRE_MARGIN`` before expiry. Returns
        True if a usable cert is in place.
        """
        if not self._enabled:
            return True
        if not self._virtual_did:
            _LOGGER.error("central hub: no virtual did; cannot refresh cert")
            return False
        try:
            if not await self._cert.verify_ca_cert_async():
                _LOGGER.error(
                    "central hub: CA cert not ready (%s); local control disabled",
                    self._cert.ca_file,
                )
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
            # 瞬态失败(云端超时/网络抖动)也要安排退避重试,否则续签链断了——所有
            # 网关都稳定连接时 reconnect sweep 跳过已连网关,不会触发证书检查,
            # 证书会在无人察觉下过期,新连接被 mTLS 拒绝。
            self.__schedule_cert_refresh(_CERT_REFRESH_RETRY_BACKOFF)
            return False

    def __schedule_cert_refresh(self, delay_sec: float) -> None:
        if self._refresh_cert_timer:
            self._refresh_cert_timer.cancel()
        self._refresh_cert_timer = self._main_loop.call_later(
            max(delay_sec, 60),
            lambda: self._main_loop.create_task(self.__refresh_cert()),
        )

    async def __refresh_owned_group_ids(self) -> None:
        """Fetch the group_ids of homes to connect to: homes this account owns,
        narrowed to the caller's enabled homes when a home filter is provided."""
        try:
            homes = await self._http_client.get_homes_async()
            enabled: Optional[set[str]] = None
            if self._home_ids_provider is not None:
                try:
                    enabled = {str(h) for h in self._home_ids_provider()}
                except Exception as e:
                    # Fail open: a filter error must not silently disable local
                    # control; fall back to all owned homes.
                    _LOGGER.warning(
                        "central hub: home filter failed, using all owned: %s", e
                    )
                    enabled = None
            self._owned_group_ids = {
                h.group_id
                for hid, h in homes.items()
                if getattr(h, "group_id", None)
                and (enabled is None or str(hid) in enabled)
            }
            # did -> group_id over ALL owned homes (not just enabled), used to
            # resolve which home a *static* gateway belongs to (its config only
            # carries an IP, no group_id) so it can be subjected to the same
            # home-scope filter as mDNS-discovered gateways.
            self._did_group_map = {
                did: h.group_id
                for h in homes.values()
                if getattr(h, "group_id", None)
                for did in (getattr(h, "dids", None) or [])
            }
            _LOGGER.info(
                "central hub: %d connectable home group_ids%s",
                len(self._owned_group_ids),
                "" if enabled is None else f" (filtered to {len(enabled)} enabled)",
            )
        except Exception as e:
            _LOGGER.error("central hub: fetch owned group_ids failed: %s", e)
        finally:
            # Stamp regardless of outcome so a persistent cloud failure doesn't
            # let unowned-gateway discovery hammer the API (see the throttle in
            # __on_service_change).
            self._owned_refreshed_at = time.monotonic()

    async def __on_service_change(
        self, group_id: str, state: MdnsServiceState, data: dict
    ) -> None:
        if state == MdnsServiceState.REMOVED:
            return
        # Only connect to gateways this account owns. mDNS surfaces every
        # gateway on the LAN (incl. neighbors'); a non-owned gateway rejects
        # our mTLS CONNACK with "Not authorized". On a miss, refresh once (a home
        # may have been added after startup) — but throttled, so a LAN full of
        # neighbor gateways can't turn each discovery into a cloud round-trip.
        if group_id not in self._owned_group_ids:
            if (
                time.monotonic() - self._owned_refreshed_at
                >= _OWNED_REFRESH_MIN_INTERVAL_SEC
            ):
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
        # Serialise connect attempts for the same gateway: mDNS callback and
        # 20s reconnect sweep can both enter here concurrently (asyncio tasks),
        # both see self._clients.get(group_id) is None, both create a
        # MipsLocalClient — the loser leaks a connected paho instance.
        lock = self._ensure_locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            await self.__ensure_client_locked(group_id, host, port)

    async def __ensure_client_locked(
        self, group_id: str, host: str, port: int
    ) -> None:
        existing = self._clients.get(group_id)
        if existing is not None:
            if existing.host == host and existing.is_connected:
                return
            # Address changed or dead — replace.
            try:
                await existing.deinit_async()
            except Exception:
                pass  # replacing a dead/stale client; teardown errors are irrelevant
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
        # A cert may not have been ready at init; make sure it is now
        # AND that its CN matches the current virtual did (identity may
        # have been rotated — e.g. authorize_with_code → the old manager's
        # 20s reconnect sweep can pick up a newly-signed cert whose CN
        # encodes a different did, which the gateway rejects).
        if (
            await self._cert.user_cert_remaining_time_async(
                did=self._virtual_did
            )
            <= 0
        ):
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
            self.__log_connect_failure(group_id, e)
            await self.__drop_client(client)
            return
        except Exception as e:
            _LOGGER.error(
                "central hub: unexpected error connecting gateway %s: %s",
                group_id,
                e,
            )
            await self.__drop_client(client)
            return
        self._clients[group_id] = client
        self._auth_rejected.discard(group_id)  # recovered → allow a fresh warn
        _LOGGER.info("central hub: gateway %s connected (%s)", group_id, host)
        await self.__refresh_dev_list(client)

        # Static gateways carry only an IP in config (no group_id), so they
        # skipped the mDNS ownership/home-scope filter at connect time. Now that
        # we have their device list, resolve which home they belong to and, if
        # it is not an enabled home, drop the connection — same net effect as
        # the mDNS filter. (mTLS already guarantees the gateway is owned by this
        # account; this narrows to the *enabled* home.)
        if group_id.startswith("static:"):
            verdict = self.__static_gateway_enabled(client)
            if verdict is False:
                _LOGGER.warning(
                    "central hub: static gateway %s belongs to a home that is "
                    "not enabled in Miloco; dropping and skipping it. Remove "
                    "it from miot.central_hub_gateways, or enable that home.",
                    host,
                )
                self._static_rejected.add(group_id)
                await self.__drop_client(group_id)
            elif verdict is None:
                _LOGGER.warning(
                    "central hub: could not resolve home for static gateway %s; "
                    "keeping it (fail-open)",
                    host,
                )

    def __log_connect_failure(self, group_id: str, err: Exception) -> None:
        """Log a gateway connect failure. For an mTLS "Not authorized" rejection
        emit a distinct, actionable hint (once per gateway until it recovers) —
        it almost always means another central-hub client on the same Mi account
        holds the slot, not a transient/network error."""
        if "Not authorized" in str(err):
            if group_id in self._auth_rejected:
                _LOGGER.debug(
                    "central hub: gateway %s still Not authorized", group_id
                )
                return
            self._auth_rejected.add(group_id)
            _LOGGER.warning(
                "central hub: gateway %s rejected the connection (Not authorized). "
                "A Mi account allows only ONE central-hub client at a time — another "
                "Miloco instance or the Xiaomi Home HA integration signed in with the "
                "same account is holding it. Stop the other instance, or use a "
                "separate account, for local control here.",
                group_id,
            )
            return
        _LOGGER.error("central hub: connect gateway %s failed: %s", group_id, err)

    def __static_gateway_enabled(self, client: MipsLocalClient) -> Optional[bool]:
        """For a static gateway, resolve the home it belongs to (via its
        devices) and check that home is enabled.

        Returns True (keep) / False (drop, belongs to a non-enabled home) /
        None (couldn't resolve → caller keeps, fail-open).
        """
        gid = client.group_id
        dids = [d for d, info in self._dev_table.items() if info.get("group_id") == gid]
        resolved = {self._did_group_map[d] for d in dids if d in self._did_group_map}
        if not resolved:
            return None
        # A gateway's devices all belong to one home; keep if that home's
        # group_id is in the enabled owned set.
        return any(g in self._owned_group_ids for g in resolved)

    async def __drop_client(self, group_id_or_client: "str | MipsLocalClient") -> None:
        """Disconnect a gateway client and remove its device-table entries.

        Accepts either a *group_id* (looks up and removes from ``_clients``) or
        a *MipsLocalClient* instance directly (for cleanup after a failed
        ``init_async`` where the client was never inserted into ``_clients``).
        """
        if isinstance(group_id_or_client, MipsLocalClient):
            client = group_id_or_client
            group_id: str = client.group_id
        else:
            group_id = group_id_or_client
            client = self._clients.pop(group_id, None)
        if client is not None:
            try:
                await client.deinit_async()
            except Exception as e:
                _LOGGER.warning("central hub: drop client %s raised: %s", group_id, e)
        removed = [d for d, i in self._dev_table.items() if i.get("group_id") == group_id]
        for did in removed:
            del self._dev_table[did]
        if removed and self._on_dev_list_changed:
            try:
                await self._on_dev_list_changed([], removed)
            except Exception as e:
                _LOGGER.error("central hub: dev-list-changed handler raised: %s", e)

    # --------------------------------------------------------- reconnect sweep

    async def __reconnect_loop(self) -> None:
        """Periodically (re)connect desired gateways that aren't up — recovers
        from a transient connect failure at discovery time (which mDNS
        re-announces won't otherwise retry)."""
        try:
            while self._started:
                await asyncio.sleep(_RECONNECT_INTERVAL_SEC)
                if not self._started:
                    return
                try:
                    await self.__ensure_desired_connections()
                except Exception as e:
                    _LOGGER.debug("central hub: reconnect sweep error: %s", e)
        except asyncio.CancelledError:
            raise

    async def __ensure_desired_connections(self) -> None:
        # Static gateways (ownership is verified post-connect in __ensure_client).
        for host, port in self._static_gateways:
            group_id = f"static:{host}"
            if group_id in self._static_rejected:
                continue  # already determined not-in-enabled-home → skip
            client = self._clients.get(group_id)
            if client is not None and client.is_connected:
                continue
            try:
                await self.__ensure_client(group_id, host, port)
            except Exception as e:
                _LOGGER.debug("central hub: reconnect static %s failed: %s", host, e)
        # mDNS-discovered gateways owned by an enabled home.
        if self._mdns is None:
            return
        for group_id, data in self._mdns.get_services().items():
            if group_id not in self._owned_group_ids:
                continue
            client = self._clients.get(group_id)
            if client is not None and client.is_connected:
                continue
            addresses = data.get("addresses") or []
            port = data.get("port")
            if not addresses or not port:
                continue
            try:
                await self.__ensure_client(group_id, addresses[0], int(port))
            except Exception as e:
                _LOGGER.debug(
                    "central hub: reconnect gateway %s failed: %s", group_id, e
                )

    async def __on_client_dev_list_changed(
        self, client: MipsLocalClient, dev_list: list
    ) -> None:
        # The push payload carries the changed device list, but we
        # deliberately ignore it and pull a full getDevList instead:
        # __refresh_dev_list's reconcile logic needs the complete set to
        # correctly remove stale dids.  One LAN RPC (~30ms) per change is
        # acceptable; if gateway load ever becomes a concern, we can
        # incrementally apply the push and backfill with a full reconcile.
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
