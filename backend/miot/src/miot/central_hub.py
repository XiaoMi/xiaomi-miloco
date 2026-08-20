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
# Whole-gateway cooldown: armed when _GW_TIMEOUT_THRESHOLD distinct dids behind
# one gateway time out within _GW_TIMEOUT_WINDOW_SEC. Threshold is 2 rather than
# 1 so a single flaky device cannot demote the entire home to cloud; the cost of
# detecting a genuinely wedged gateway is therefore 2 × the local RPC timeout,
# instead of one timeout per device in the batch.
_GW_COOLDOWN_SEC = 30.0
_GW_TIMEOUT_WINDOW_SEC = 30.0
_GW_TIMEOUT_THRESHOLD = 2

# Minimum spacing between owned-group refetches triggered by discovering an
# unowned gateway. Bounds the cloud calls when many neighbor gateways are on the
# LAN; a genuinely new home still gets picked up within this window / on the
# next periodic refresh.
_OWNED_REFRESH_MIN_INTERVAL_SEC = 60.0
# 拉取家庭列表失败后的重试退避。比上面的节流窗短:那个防的是"未拥有网关反复触发
# 刷新打爆 API",而这里是我们**自己**还没拿到白名单、本地控制完全不可用,值得更快
# 重试。由重连 sweep 驱动(每 _RECONNECT_SWEEP 一轮),不额外起定时器。
_OWNED_REFRESH_RETRY_INTERVAL_SEC = 20.0

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
        self._refresh_cert_task: Optional[asyncio.Task] = None

        # did -> monotonic expiry: local path skipped (route cloud) for a did
        # whose recent local RPC failed/timed out, until the window lapses.
        self._local_cooldown: dict[str, float] = {}
        # group_id -> monotonic expiry: whole-gateway cooldown. Armed once
        # several *distinct* dids behind the same gateway time out in a window,
        # which is the signature of the gateway itself being wedged rather than
        # one flaky device. Without it a batch across N devices behind a dead
        # gateway pays N × the local RPC timeout one device at a time (10 devices
        # ⇒ ~50s, worse than the pure-cloud path this channel replaced).
        self._gw_cooldown: dict[str, float] = {}
        # group_id -> {did: monotonic expiry} — distinct dids that timed out
        # recently, the evidence for arming the gateway-level cooldown above.
        # Keyed by did (not a plain counter) on purpose: one device retrying and
        # timing out repeatedly must NOT get the whole gateway demoted, or a
        # single misbehaving plug would keep pushing the entire home to cloud.
        self._gw_timeouts: dict[str, dict[str, float]] = {}
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
        # Hosts with an in-flight __ensure_client_locked connect (init_async()
        # hasn't returned yet, so self._clients doesn't see them). _ensure_locks
        # is keyed by group_id, not host: the same physical gateway reachable
        # both via mDNS (real group_id) and static config (synthetic
        # "static:host") takes two *different* locks, so the two paths don't
        # serialize against each other — without this set, both can pass the
        # host-dedup loop below concurrently (it only checks self._clients,
        # which is still empty for both), each build a MipsLocalClient with the
        # same MQTT client_id (= the virtual did, independent of group_id), and
        # the broker kicks the loser per MQTT v5 §3.1.4 — the kicked side
        # auto-reconnects and kicks the other back, forever, ~every 6s.
        self._connecting_hosts: set[str] = set()
        # Serialises __refresh_cert. The per-group locks above do NOT cover it:
        # two gateways hold two different locks, so their connect tasks can both
        # enter the check-then-act key/cert flow and write a mismatched pair.
        self._cert_refresh_lock = asyncio.Lock()
        # Static gateway group_ids that were connected and then dropped
        # because their home is not in the enabled set. Recorded so the
        # 20s reconnect sweep doesn't create a connect→drop infinite loop.
        self._static_rejected: set[str] = set()
        # monotonic time of the last owned-group refresh, to throttle the
        # refetch triggered when an unowned gateway is discovered (a dense LAN
        # can surface many neighbor gateways, each otherwise a cloud round-trip).
        self._owned_refreshed_at: float = 0.0
        # 上一次拉取家庭列表是否成功;失败时 _owned_retry_at 给出下次可重试的时刻。
        # 分开记是因为"成功但为空"(用户确实没启用家庭)与"失败"(白名单未知)后果完全
        # 不同:前者该按 60s 节流,后者必须尽快重试,否则本地控制永久不可用。
        self._owned_refresh_ok: bool = False
        self._owned_retry_at: float = 0.0

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
        if self._refresh_cert_task is not None:
            # 只取消"还没到点的定时器"管不到"已经开跑、正卡在云端签发 await 上"
            # 的任务 —— 它没有被别处持有引用、也不检查自己是否还该活着,拆卸落在
            # 它的签发窗口内时会带着已拆卸的 self 跑完并自我续期,形成一条直到
            # 进程退出都停不下来的僵尸链(还可能用旧 did 覆盖新实例刚签的证书)。
            self._refresh_cert_task.cancel()
            self._refresh_cert_task = None
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
        # 抑制,切换后应重新允许告警)、_local_cooldown(旧 did 的冷却窗口)、
        # _gw_cooldown / _gw_timeouts(旧网关的降级状态与超时证据——group_id 换过
        # 之后这些键再也匹配不上,留着只是内存垃圾)。
        self._ensure_locks.clear()
        self._connecting_hosts.clear()
        self._auth_rejected.clear()
        self._local_cooldown.clear()
        self._gw_cooldown.clear()
        self._gw_timeouts.clear()

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
        # 家庭范围确实变了:旧白名单不能留给 init_async 当 fail-open 的兜底值,否则
        # 云端不可达时重连 sweep 会照着旧家庭的 group_id 把旧网关重新连回来(见
        # __refresh_owned_group_ids 失败路径只置 _owned_refresh_ok=False、旧集合
        # 原样保留 —— 那是关停/重启场景要的行为,但这里 scope 本身已经变了)。
        # 注意只在这里清,不能塞进 deinit_async:它也服务于关停/重启,那些场景下
        # 保留旧值恰恰是 fail-open 想要的。
        self._owned_group_ids.clear()
        self._did_group_map.clear()
        self._owned_refresh_ok = False
        self._owned_retry_at = 0.0
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

        Checked gateway-first: when the gateway as a whole is cooling down every
        did behind it routes to cloud, so a batch does not pay one timeout per
        device discovering the same dead gateway over and over.
        """
        gw = self.__gateway_of(did)
        if gw is not None:
            expiry = self._gw_cooldown.get(gw)
            if expiry is not None:
                if time.monotonic() < expiry:
                    return True
                del self._gw_cooldown[gw]  # window lapsed
        expiry = self._local_cooldown.get(did)
        if expiry is None:
            return False
        if time.monotonic() < expiry:
            return True
        del self._local_cooldown[did]  # window lapsed
        return False

    def __gateway_of(self, did: str) -> Optional[str]:
        """group_id of the gateway a did sits behind (None if not in the table)."""
        info = self._dev_table.get(did)
        return info.get("group_id") if info else None

    def note_local_failure(self, did: str) -> None:
        """Arm cooldowns after a local RPC timeout (called only on timeout, not
        on fast failures) → route to cloud for a window.

        Always cools the did. Additionally cools the **gateway** once
        ``_GW_TIMEOUT_THRESHOLD`` distinct dids behind it have timed out inside
        ``_GW_TIMEOUT_WINDOW_SEC``: a wedged gateway (busy process, half-open
        MQTT whose keepalive has not fired yet, gateway restarting) makes every
        device behind it time out separately, and the per-did cooldown alone
        cannot stop a batch from paying that cost device by device.

        Requiring more than one distinct did before demoting the gateway is
        deliberate: it separates "this one plug is flaky" (stay local for the
        rest of the home) from "the gateway is gone" (skip local wholesale). The
        worst case becomes THRESHOLD × timeout instead of N_devices × timeout.
        """
        now = time.monotonic()
        self._local_cooldown[did] = now + _LOCAL_COOLDOWN_SEC
        gw = self.__gateway_of(did)
        if gw is None:
            return
        seen = self._gw_timeouts.setdefault(gw, {})
        for stale_did, expiry in list(seen.items()):  # drop lapsed evidence
            if now >= expiry:
                del seen[stale_did]
        seen[did] = now + _GW_TIMEOUT_WINDOW_SEC
        if len(seen) >= _GW_TIMEOUT_THRESHOLD:
            _LOGGER.warning(
                "central hub: %d distinct dids timed out behind gateway %s → "
                "cooling down the whole gateway for %.0fs (routing to cloud)",
                len(seen), gw, _GW_COOLDOWN_SEC,
            )
            self._gw_cooldown[gw] = now + _GW_COOLDOWN_SEC
            seen.clear()  # evidence consumed; re-accumulate after the window

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

        **任何瞬态失败出口都必须排一次退避重试**,所以兜底收在这一层而不是只挡
        ``except``:续签是一条自维持的链条,所有网关都稳定连接时 reconnect sweep 会
        跳过已连网关、``__ensure_client_locked`` 永不执行,于是链一断就再没有任何
        定时器指向这里 —— 证书在无人察觉下过期,已建立的 MQTT 会话还活着所以"看起来
        一切正常",直到下一次断连才被 mTLS 拒绝,此后所有控制静默退回云端 RTT。
        而 CA / 私钥 / 证书三处落盘失败走的是 ``return False`` 而**不是**抛异常
        (存储层写失败即返回 False),性质与 except 挡的云端超时完全相同(磁盘临时写满、
        目录权限被外部工具改动),只是走不到异常通道,此前正好掉进这个缺口。
        """
        if not self._enabled:
            return True
        if not self._started:
            # deinit_async 落在上一次 __refresh_cert_once 的云端签发 await 里时,
            # 这个任务(create_task 出去、没人持有引用)会带着已拆卸的 self 继续
            # 跑完并重新排期,形成一条到进程退出都停不下来的僵尸链——见 deinit_async
            # 里 _refresh_cert_task.cancel() 那段注释。_started 由 deinit_async
            # 落地时置 False,这里读它当生命周期闸门。
            _LOGGER.debug("central hub: cert refresh skipped (already deinit-ed)")
            return False
        if not self._virtual_did:
            # 不是瞬态故障(身份缺失要靠 init / 换号流程补),无限重试没有意义。
            _LOGGER.error("central hub: no virtual did; cannot refresh cert")
            return False
        ok = await self.__refresh_cert_once()
        if not ok:
            self.__schedule_cert_refresh(_CERT_REFRESH_RETRY_BACKOFF)
        return ok

    async def __refresh_cert_once(self) -> bool:
        """``__refresh_cert`` 的主体。失败只返回 False,退避重试由调用方统一安排。"""
        try:
            # 必须串行:函数体是 check-then-act(load key → 没有就生成并保存 → 组 CSR
            # → 云端签发 → 保存 cert)。三个调用方里 __ensure_client_locked 那个只持
            # **per-group** 锁,两台网关的应答通常同批到达 → 两个连接任务并发进来,
            # 都 load 到 None、各自生成 kA/kB,key 与 cert 的保存都是"后写者赢",最终
            # 可能 key=kB 而 cert=certA:公私钥不配对,mTLS 必失败。而且**没有自愈**
            # —— user_cert_remaining_time_async 只看 subject/有效期,certA 本身合法、
            # CN 也匹配,补签条件永不触发,本地控制死到手工删文件。锁内重新取一次剩余
            # 时间即天然完成 double-check:先赢者已签好,后到者直接复用。
            async with self._cert_refresh_lock:
                if not await self._cert.verify_ca_cert_async():
                    _LOGGER.error(
                        "central hub: CA cert not ready (%s); local control disabled",
                        self._cert.ca_file,
                    )
                    return False
                # Pass the current did so a cert whose CN encodes a *different*
                # did (e.g. the identity changed / migrated) is treated as
                # expired and re-signed — the gateway rejects a client_id/CN
                # mismatch.
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
            # 退避重试由 __refresh_cert 统一排(它对本函数的每一条失败出口都排,
            # 不只是异常这一条)——理由见那边的 docstring。
            return False

    def __schedule_cert_refresh(self, delay_sec: float) -> None:
        if self._refresh_cert_timer:
            self._refresh_cert_timer.cancel()
        self._refresh_cert_timer = self._main_loop.call_later(
            max(delay_sec, 60), self.__spawn_cert_refresh
        )

    def __spawn_cert_refresh(self) -> None:
        # 任务句柄记下来,拆卸时才有东西可 cancel——之前 create_task 的返回值直接
        # 丢弃,deinit_async 只能取消"还没到点的定时器",管不到已经开跑的这个任务。
        self._refresh_cert_task = self._main_loop.create_task(self.__refresh_cert())

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
            self._owned_refresh_ok = True
        except Exception as e:
            # 失败必须与"成功但结果为空"区分开。历史实现在 finally 里无条件盖
            # 时间戳,于是开机自启(launchd 早于网络就绪)那次拉取失败后:白名单为空
            # + 时间戳"刚刷过" → mDNS 首次探到自家网关时被 60s 节流窗吃掉 → 而
            # mDNS 按内容去重,IP/端口稳定的网关**一个进程只触发一次 ADDED** →
            # 回调再也不来,重连 sweep 又全被 `not in _owned_group_ids` continue
            # 掉且从不刷新 → 本地控制永久降级到云端,直到切家庭或重启进程。
            # 现在:失败不盖成功时间戳,只记一个更短的退避点,让 sweep 能重试。
            self._owned_refresh_ok = False
            self._owned_retry_at = (
                time.monotonic() + _OWNED_REFRESH_RETRY_INTERVAL_SEC
            )
            _LOGGER.error(
                "central hub: fetch owned group_ids failed (will retry in %.0fs): %s",
                _OWNED_REFRESH_RETRY_INTERVAL_SEC, e,
            )
            return
        finally:
            # 成功才盖节流时间戳——它的用途是防止"未拥有的网关"反复触发刷新打爆
            # 云端 API(见 __on_service_change 的节流),失败路径用 _owned_retry_at
            # 单独退避,两者不能共用一个戳。
            if self._owned_refresh_ok:
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
            now = time.monotonic()
            # 失败退避也要在发现路径生效:__refresh_owned_group_ids 失败时只记
            # _owned_retry_at、不盖成功戳,所以云端故障期间 _owned_refreshed_at 恒是
            # 陈旧值(或初始 0.0),下面这个节流条件恒通过 —— 每发现一台未拥有的邻居
            # 网关就再打一次 get_homes_async(最长 30s 超时)。开机自启撞上云不可达
            # (本 PR 的重点场景)时,LAN 里每台邻居网关各触发一次。
            if now - self._owned_refreshed_at >= _OWNED_REFRESH_MIN_INTERVAL_SEC and (
                self._owned_refresh_ok or now >= self._owned_retry_at
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
        # connection to a given broker. Note _ensure_locks is keyed by
        # group_id, so the mDNS path and the static path do NOT serialize
        # against each other — this loop alone is not enough, because an
        # in-flight connect (init_async() hasn't returned) is invisible here:
        # self._clients only gets the entry after the handshake completes.
        for gid, client in self._clients.items():
            if gid != group_id and client.host == host and client.is_connected:
                _LOGGER.debug(
                    "central hub: host %s already connected as %s, skip %s",
                    host,
                    gid,
                    group_id,
                )
                return
        if host in self._connecting_hosts:
            # 另一条路径(mDNS 真实 group_id / 静态 "static:host")正在给同一个
            # host 建连、还没跑完 init_async()——上面那段按 self._clients 去重的
            # 循环看不见它。不拦的话两条路都会各建一个 MipsLocalClient,而 MQTT
            # client_id 恒等于虚拟 did、与 group_id 无关(见 mips_local.py),broker
            # 按 MQTT v5 §3.1.4 踢掉后到的会话,被踢的一方自动重连又把对方顶下线,
            # 形成 ~6s 周期的永久互踢(can_control() 每几秒真假翻转)。
            _LOGGER.debug(
                "central hub: host %s connect already in flight, skip %s",
                host,
                group_id,
            )
            return

        self._connecting_hosts.add(host)
        try:
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
            except asyncio.CancelledError:
                # 切家庭 / 进程退出的 deinit 会 cancel 本任务(mDNS 发现回调、20s 重连
                # sweep)。cancel 通常落在 init_async 内部等 CONNACK 的那个 await 上 ——
                # 此时 loop_start() 已经起了 paho 网络线程,而 client 还没进 _clients,
                # 再没有任何人 deinit 它:线程会按 reconnect_delay_set(6, 60) 永久重连
                # 用户网关。CancelledError 是 BaseException,下面两个 except 都接不住,
                # 必须显式兜底。shield 保证清理不被同一轮 cancel 打断。
                try:
                    await asyncio.shield(self.__drop_client(client))
                except asyncio.CancelledError:
                    pass  # 清理已作为独立任务在跑,放原始 cancel 继续传播
                raise
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
        finally:
            # 一旦写进 self._clients(或任何 return/异常)就不再"在途",让 finally
            # 而不是某个具体返回点来清,避免漏改某条出口就重新造出这个坑。写入
            # 与这里之间没有 await,不存在"已清在途但还没进连接表"的空窗。
            self._connecting_hosts.discard(host)
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
        # 兜底刷新白名单:上次拉取失败过且退避到点就重试一次。
        # 这是"启动期云端不可达"能自愈的唯一途径——mDNS 按内容去重,IP/端口稳定的
        # 网关一个进程只触发一次 ADDED,那唯一一次回调如果撞上空白名单就永远没有
        # 第二次机会;而本方法下面两段都以 _owned_group_ids 为前提,自己从不刷新。
        if not self._owned_refresh_ok and time.monotonic() >= self._owned_retry_at:
            _LOGGER.info("central hub: retrying owned group_ids fetch")
            await self.__refresh_owned_group_ids()
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
