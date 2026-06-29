# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT cloud MQTT (mips_cloud) client.

Implements a real-time subscription path to the MIoT pub/sub broker so that the
local SDK is pushed `properties_changed` / `event_occured` / device-state /
user-level bind/unbind events instead of polling the HTTP `device_list_page`
endpoint.

Connection params:
  host       = f"{cloud_server}-{MIHOME_MQTT_BROKER_HOST_SUFFIX}"
  port       = 8883 (TLS, MQTT v5)
  client_id  = f"miloco:{uuid}"  (per Mqtt接入规范 §二: 身份标识:身份信息)
  username   = app_id  (OAuth2 client_id)
  password   = OAuth2 access_token  (rotated via update_access_token_async)
  keepalive  = 60s
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import threading
from typing import Any, Awaitable, Callable, Optional, Union

from paho.mqtt.client import (
    CallbackAPIVersion,
    Client,
    MQTTMessage,
    MQTTv5,
    topic_matches_sub,
)
from paho.mqtt.enums import MQTTErrorCode

from .const import (
    MIHOME_MQTT_BROKER_HOST_SUFFIX,
    MIHOME_MQTT_KEEPALIVE,
    MIHOME_MQTT_PORT,
    MIHOME_MQTT_RECONNECT_MAX_SEC,
    MIHOME_MQTT_RECONNECT_MIN_SEC,
    MIHOME_MQTT_SUBSCRIBE_TIMEOUT,
)
from .types import (
    MIoTDeviceBindEvent,
    MIoTDeviceStateEvent,
    MIoTSceneChangedEvent,
    MipsConnectionError,
    MipsSubscribeRejectedError,
    MipsSubscribeTimeoutError,
)

_LOGGER = logging.getLogger(__name__)

# QoS 2 (exactly-once) matches HA's MIPS_QOS for both subscriptions and
# publishes. Cloud broker bandwidth is not our bottleneck, and bind/unbind
# events are unsafe to deduplicate at the app layer.
_DEFAULT_QOS: int = 2

# MQTT v5 SUBACK reason codes that count as success per spec §3.9.3.
# (Granted QoS 0/1/2 == codes 0x00/0x01/0x02.)
_SUBACK_SUCCESS_CODES = frozenset({0x00, 0x01, 0x02})

# SUBACK rejections that retry can't fix; other failures (incl. timeout) are
# kept in _subs so the next reconnect retries automatically.
_PERMANENT_SUBACK_FAILURES = frozenset(
    {
        0x87,  # Not authorized (ACL)
        0x8F,  # Topic filter invalid
        0x9E,  # Shared subscriptions not supported
        0xA1,  # Subscription identifiers not supported
        0xA2,  # Wildcard subscriptions not supported
    }
)

# Account-level g_op bind/unbind: `user/{uid}/g_op/{bind,unbind}`.
_TOPIC_USER_OP = re.compile(r"^user/([^/]+)/g_op/(bind|unbind)$")

# Device-level g_op meta changes: `device/{did}/g_op/{rename,hr_change}`
# (name change / home+room reassignment). did = group(1), op = group(2).
# Subscribed as EXACT leaf topics (one per op) — the broker ACL rejects the
# `device/{did}/g_op/#` wildcard with 0x87 Not authorized. The Literal in
# MIoTDeviceBindEvent.event must stay in sync with these ops.
_DEVICE_META_OPS = ("rename", "hr_change")
_TOPIC_DEVICE_META = re.compile(
    r"^device/([^/]+)/g_op/(" + "|".join(_DEVICE_META_OPS) + r")$"
)

# Home-level scene changes: `home/{home_id}/scene/{rename,delete,edit}`.
# home_id = group(1), op = group(2). Also subscribed as EXACT leaf topics
# (the `home/{home_id}/scene/#` wildcard is likewise ACL-rejected). The
# Literal in MIoTSceneChangedEvent.event must stay in sync with these ops.
_HOME_SCENE_OPS = ("rename", "delete", "edit")
_TOPIC_HOME_SCENE = re.compile(
    r"^home/([^/]+)/scene/(" + "|".join(_HOME_SCENE_OPS) + r")$"
)

# Device-level cloud online/offline state: `device/{did}/state/{online,
# offline}`. did = group(1), event = group(2). Subscribed as EXACT leaf
# topics (one per op) — the broker ACL rejects the `device/{did}/state/#`
# wildcard with 0x87 Not authorized, same as the g_op/# case above. The
# Literal in MIoTDeviceStateEvent.event must stay in sync with these ops.
_DEVICE_STATE_OPS = ("online", "offline")
_TOPIC_DEVICE_STATE = re.compile(
    r"^device/([^/]+)/state/(" + "|".join(_DEVICE_STATE_OPS) + r")$"
)


# Handler signatures accepted by sub_*_async methods. They receive a fully
# decoded message object and may be sync or async — both are dispatched on the
# main asyncio loop.
BindHandler = Callable[[MIoTDeviceBindEvent], Union[None, Awaitable[None]]]
SceneChangedHandler = Callable[[MIoTSceneChangedEvent], Union[None, Awaitable[None]]]
DeviceStateHandler = Callable[[MIoTDeviceStateEvent], Union[None, Awaitable[None]]]
MipsStateHandler = Callable[[bool], Union[None, Awaitable[None]]]
# Fired when an unattended subscribe (no awaiter, e.g. the reconnect-time
# re-issue in _on_connect) fails. Arg tuple = (topic, reason_code,
# reason_string). reason_code is the MQTT v5 SUBACK code (e.g. 0x87 Not
# authorized); -1 if the local subscribe() call itself failed before
# reaching the broker. First-time subscribes have an awaiter and raise
# MipsSubscribeRejectedError directly — those don't go through this handler.
SubscribeErrorHandler = Callable[[tuple[str, int, str]], Union[None, Awaitable[None]]]
# Fired when an unattended subscribe succeeds (SUBACK with success code).
# Arg = topic string. Symmetric with SubscribeErrorHandler — allows the
# caller to clear a stale error flag on successful reconnect resubscribe.
SubscribeSuccessHandler = Callable[[str], Union[None, Awaitable[None]]]


class _Subscription:
    """Bookkeeping for one active subscription topic."""

    __slots__ = ("topic", "qos", "handler", "decoder")

    def __init__(
        self,
        topic: str,
        qos: int,
        handler: Callable[[Any], Union[None, Awaitable[None]]],
        decoder: Callable[[str, bytes], Optional[Any]],
    ) -> None:
        self.topic = topic
        self.qos = qos
        self.handler = handler
        self.decoder = decoder


class MIoTMipsCloud:
    """Cloud MQTT subscription client for the MIoT pub/sub broker.

    Lifecycle:
        client = MIoTMipsCloud(uuid, "cn", app_id, access_token)
        await client.init_async()                     # connects + waits CONNACK
        await client.sub_user_bind_async(uid, h)      # waits SUBACK
        ...
        await client.deinit_async()                   # disconnects + joins thread

    Threading:
        - paho runs its own network thread (loop_start). All paho callbacks
          fire on that thread.
        - All app-facing handlers are dispatched to ``loop`` via
          ``call_soon_threadsafe``. Sync handlers run inline on the loop tick;
          async handlers are wrapped with ``asyncio.create_task``.
    """

    def __init__(
        self,
        uuid: str,
        cloud_server: str,
        app_id: str,
        token: str,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        port: int = MIHOME_MQTT_PORT,
        client_factory: Optional[Callable[..., Client]] = None,
    ) -> None:
        if not uuid:
            raise ValueError("uuid is required")
        if not cloud_server:
            raise ValueError("cloud_server is required")
        if not app_id:
            raise ValueError("app_id is required")
        # token may be empty until login completes; allow but warn at connect time.

        self._uuid = uuid
        self._cloud_server = cloud_server
        self._app_id = app_id
        self._token = token
        self._main_loop = loop or asyncio.get_running_loop()
        self._host = f"{cloud_server}-{MIHOME_MQTT_BROKER_HOST_SUFFIX}"
        self._port = port
        self._client_id = f"miloco:{uuid}"
        # Allow tests to inject a fake paho Client.
        self._client_factory = client_factory or self._default_client_factory

        # paho client + its network thread are created lazily on init_async().
        self._mqtt: Optional[Client] = None

        # State guards
        self._state_lock = threading.Lock()
        self._connected: bool = False
        self._connect_future: Optional[asyncio.Future[None]] = None

        # mid → Future[ list[int reason_codes] ] for awaiting SUBACK
        self._pending_subscribes: dict[int, asyncio.Future[list[int]]] = {}
        self._pending_lock = threading.Lock()

        # Active subscriptions, keyed by topic. Used to resubscribe on reconnect.
        self._subs: dict[str, _Subscription] = {}
        self._subs_lock = threading.Lock()

        # External connect/disconnect state listeners.
        self._mips_state_handlers: list[MipsStateHandler] = []
        # Fires when an unattended subscribe (no awaiter) is rejected — see
        # SubscribeErrorHandler. First-time rejections still raise.
        self._subscribe_error_handlers: list[SubscribeErrorHandler] = []
        # Symmetric with error handlers — fires on successful unattended subscribe.
        self._subscribe_success_handlers: list[SubscribeSuccessHandler] = []
        self._handlers_lock = threading.Lock()

    # ------------------------------------------------------------------ props

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ----------------------------------------------------------- factory hook

    @staticmethod
    def _default_client_factory(client_id: str) -> Client:
        return Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=MQTTv5,
        )

    # --------------------------------------------------------------- lifecycle

    async def init_async(self) -> None:
        """Connect to the broker and wait for CONNACK.

        Raises:
            MipsConnectionError: connection refused or timed out.
        """
        if self._mqtt is not None:
            _LOGGER.warning("MIoTMipsCloud already initialized")
            return

        mqtt = self._client_factory(self._client_id)
        mqtt.username_pw_set(username=self._app_id, password=self._token)
        mqtt.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        mqtt.reconnect_delay_set(
            min_delay=int(MIHOME_MQTT_RECONNECT_MIN_SEC),
            max_delay=int(MIHOME_MQTT_RECONNECT_MAX_SEC),
        )

        mqtt.on_connect = self._on_connect
        mqtt.on_disconnect = self._on_disconnect
        mqtt.on_subscribe = self._on_subscribe
        mqtt.on_message = self._on_message

        self._mqtt = mqtt

        # Future fulfilled by on_connect / set_exception by on_connect on error.
        self._connect_future = self._main_loop.create_future()

        try:
            # paho's connect() will block on DNS + TCP/TLS handshake. Run in
            # an executor so we don't stall the main loop.
            await self._main_loop.run_in_executor(
                None,
                lambda: mqtt.connect(
                    host=self._host,
                    port=self._port,
                    keepalive=MIHOME_MQTT_KEEPALIVE,
                    clean_start=True,
                ),
            )
        except Exception as e:
            self._connect_future = None
            self._mqtt = None
            raise MipsConnectionError(f"mips_cloud TCP/TLS connect failed: {e}") from e

        mqtt.loop_start()

        try:
            await asyncio.wait_for(self._connect_future, timeout=15.0)
        except asyncio.TimeoutError as e:
            self._connect_future = None
            try:
                mqtt.loop_stop()
                mqtt.disconnect()
            except Exception:
                pass
            self._mqtt = None
            raise MipsConnectionError("mips_cloud CONNACK timeout") from e
        except MipsConnectionError:
            self._connect_future = None
            try:
                mqtt.loop_stop()
                mqtt.disconnect()
            except Exception:
                pass
            self._mqtt = None
            raise

        _LOGGER.info(
            "mips_cloud connected, host=%s client_id=%s", self._host, self._client_id
        )

    async def deinit_async(self) -> None:
        """Disconnect and stop the paho network thread."""
        mqtt = self._mqtt
        if mqtt is None:
            return
        self._mqtt = None
        try:
            await self._main_loop.run_in_executor(None, mqtt.disconnect)
        except Exception as e:
            _LOGGER.warning("mips_cloud disconnect raised: %s", e)
        try:
            await self._main_loop.run_in_executor(None, mqtt.loop_stop)
        except Exception as e:
            _LOGGER.warning("mips_cloud loop_stop raised: %s", e)

        with self._state_lock:
            self._connected = False
        with self._pending_lock:
            for fut in self._pending_subscribes.values():
                if not fut.done():
                    self._main_loop.call_soon_threadsafe(
                        fut.set_exception,
                        MipsConnectionError("mips_cloud deinit during subscribe"),
                    )
            self._pending_subscribes.clear()
        with self._subs_lock:
            self._subs.clear()

    # ---------------------------------------------------------- token rotate

    async def update_access_token(self, token: str) -> None:
        """Replace the MQTT password with a fresh OAuth2 access_token.

        paho's username_pw_set takes effect on the *next* CONNECT, so we also
        force a reconnect so the new token is actually used. While the
        reconnect cycles, on_disconnect → on_connect will resubscribe all
        active topics from ``self._subs`` (see _on_connect).
        """
        self._token = token
        mqtt = self._mqtt
        if mqtt is None:
            return
        mqtt.username_pw_set(username=self._app_id, password=token)
        try:
            await self._main_loop.run_in_executor(None, mqtt.reconnect)
        except Exception as e:
            _LOGGER.warning("mips_cloud reconnect after token rotate failed: %s", e)

    # ------------------------------------------------------ state listeners

    def register_mips_state_handler(self, handler: MipsStateHandler) -> None:
        with self._handlers_lock:
            self._mips_state_handlers.append(handler)

    def unregister_mips_state_handler(self, handler: MipsStateHandler) -> None:
        with self._handlers_lock:
            try:
                self._mips_state_handlers.remove(handler)
            except ValueError:
                pass

    def register_subscribe_error_handler(self, handler: SubscribeErrorHandler) -> None:
        """Register a callback for unattended-subscribe failures.

        The handler fires with (topic, reason_code, reason_string) when an
        unattended subscribe (currently: the reconnect-time re-issue in
        _on_connect) returns a non-success SUBACK reason code, or fails
        locally before reaching the broker.
        """
        with self._handlers_lock:
            self._subscribe_error_handlers.append(handler)

    def unregister_subscribe_error_handler(
        self, handler: SubscribeErrorHandler
    ) -> None:
        with self._handlers_lock:
            try:
                self._subscribe_error_handlers.remove(handler)
            except ValueError:
                pass

    def register_subscribe_success_handler(
        self, handler: SubscribeSuccessHandler
    ) -> None:
        """Register a callback for unattended-subscribe success.

        Symmetric with register_subscribe_error_handler. Fires with the
        topic string when an unattended subscribe (reconnect-time re-issue)
        returns a success SUBACK. Allows the caller to clear a stale error
        flag (e.g. ``mips_user_sub_error``) without a full re-OAuth cycle.
        """
        with self._handlers_lock:
            self._subscribe_success_handlers.append(handler)

    def unregister_subscribe_success_handler(
        self, handler: SubscribeSuccessHandler
    ) -> None:
        with self._handlers_lock:
            try:
                self._subscribe_success_handlers.remove(handler)
            except ValueError:
                pass

    # ------------------------------------------------------- public sub APIs

    async def sub_user_bind_async(self, uid: str, handler: BindHandler) -> None:
        """Subscribe to `user/{uid}/g_op/bind`.

        SUBACK rejection raises MipsSubscribeRejectedError. The caller is
        responsible for surfacing this loudly (no silent fallback).
        """
        topic = f"user/{uid}/g_op/bind"
        await self._subscribe_async(topic, handler, self._make_bind_decoder("bind"))

    async def sub_user_unbind_async(self, uid: str, handler: BindHandler) -> None:
        topic = f"user/{uid}/g_op/unbind"
        await self._subscribe_async(topic, handler, self._make_bind_decoder("unbind"))

    async def unsub_user_bind_async(self, uid: str) -> None:
        await self._unsubscribe_async(f"user/{uid}/g_op/bind")

    async def unsub_user_unbind_async(self, uid: str) -> None:
        await self._unsubscribe_async(f"user/{uid}/g_op/unbind")

    async def sub_device_meta_changed_async(
        self, did: str, handler: BindHandler
    ) -> None:
        """Subscribe a device's meta topics: `device/{did}/g_op/{rename,
        hr_change}`.

        One SUBSCRIBE per exact op leaf — the broker ACL rejects the
        `device/{did}/g_op/#` wildcard. Ops share one decoder. SUBACK
        rejection on any op raises MipsSubscribeRejectedError.
        """
        decoder = self._make_device_meta_decoder()
        for op in _DEVICE_META_OPS:
            await self._subscribe_async(f"device/{did}/g_op/{op}", handler, decoder)

    async def unsub_device_meta_changed_async(self, did: str) -> None:
        for op in _DEVICE_META_OPS:
            await self._unsubscribe_async(f"device/{did}/g_op/{op}")

    async def sub_home_scene_changed_async(
        self, home_id: str, handler: SceneChangedHandler
    ) -> None:
        """Subscribe a home's scene topics: `home/{home_id}/scene/{rename,
        delete,edit}`.

        One SUBSCRIBE per exact op leaf — the broker ACL rejects the
        `home/{home_id}/scene/#` wildcard. Ops share one decoder. SUBACK
        rejection on any op raises MipsSubscribeRejectedError.
        """
        decoder = self._make_scene_decoder()
        for op in _HOME_SCENE_OPS:
            await self._subscribe_async(f"home/{home_id}/scene/{op}", handler, decoder)

    async def unsub_home_scene_changed_async(self, home_id: str) -> None:
        for op in _HOME_SCENE_OPS:
            await self._unsubscribe_async(f"home/{home_id}/scene/{op}")

    async def sub_device_state_async(
        self, did: str, handler: DeviceStateHandler
    ) -> None:
        """Subscribe a device's state topics: `device/{did}/state/{online,
        offline}`.

        One SUBSCRIBE per exact op leaf — the broker ACL rejects the
        `device/{did}/state/#` wildcard (same as the g_op/# case). Ops share
        one decoder. SUBACK rejection on any op raises
        MipsSubscribeRejectedError.
        """
        decoder = self._make_device_state_decoder()
        for op in _DEVICE_STATE_OPS:
            await self._subscribe_async(f"device/{did}/state/{op}", handler, decoder)

    async def unsub_device_state_async(self, did: str) -> None:
        for op in _DEVICE_STATE_OPS:
            await self._unsubscribe_async(f"device/{did}/state/{op}")

    # ------------------------------------------------------- subscribe core

    async def _subscribe_async(
        self,
        topic: str,
        handler: Callable[[Any], Union[None, Awaitable[None]]],
        decoder: Callable[[str, bytes], Optional[Any]],
        qos: int = _DEFAULT_QOS,
        *,
        unattended: bool = False,
    ) -> None:
        """Core subscribe: issue SUBSCRIBE, await SUBACK, validate codes.

        When *unattended* is False (default), failures raise to the caller.
        When True (reconnect-time re-issue with no awaiter), failures are
        dispatched to ``_subscribe_error_handlers`` and successes to
        ``_subscribe_success_handlers`` instead of raising.
        """
        try:
            mqtt = self._mqtt
            if mqtt is None or not self._connected:
                raise MipsConnectionError(
                    f"mips_cloud not connected; cannot subscribe {topic}"
                )

            future: asyncio.Future[list[int]] = self._main_loop.create_future()

            # Record the subscription before issuing it, so a SUBACK arriving
            # before this coroutine yields still finds the entry.
            sub = _Subscription(topic=topic, qos=qos, handler=handler, decoder=decoder)
            with self._subs_lock:
                self._subs[topic] = sub

            result, mid = mqtt.subscribe(topic, qos=qos)
            if result != MQTTErrorCode.MQTT_ERR_SUCCESS or mid is None:
                with self._subs_lock:
                    self._subs.pop(topic, None)
                raise MipsConnectionError(
                    f"subscribe({topic}) failed locally: result={result} mid={mid}"
                )

            with self._pending_lock:
                self._pending_subscribes[mid] = future

            try:
                reason_codes = await asyncio.wait_for(
                    future, timeout=MIHOME_MQTT_SUBSCRIBE_TIMEOUT
                )
            except asyncio.TimeoutError:
                # Transient: keep topic in _subs so reconnect retries.
                with self._pending_lock:
                    self._pending_subscribes.pop(mid, None)
                raise MipsSubscribeTimeoutError(topic) from None

            for code in reason_codes:
                if code not in _SUBACK_SUCCESS_CODES:
                    if code in _PERMANENT_SUBACK_FAILURES:
                        with self._subs_lock:
                            self._subs.pop(topic, None)
                    raise MipsSubscribeRejectedError(
                        topic=topic,
                        reason_code=code,
                        reason_string=_describe_reason_code(code),
                    )

        except Exception as exc:
            if not unattended:
                raise
            # Unattended mode: translate exception → error handler dispatch.
            if isinstance(exc, MipsSubscribeRejectedError):
                _LOGGER.error(
                    "mips_cloud subscribe (after reconnect) REJECTED "
                    "topic=%s code=0x%02x reason=%s",
                    exc.topic,
                    exc.reason_code,
                    exc.reason_string,
                )
                self._fire_subscribe_error(
                    exc.topic, exc.reason_code, exc.reason_string
                )
            elif isinstance(exc, MipsSubscribeTimeoutError):
                _LOGGER.error(
                    "mips_cloud subscribe (after reconnect) SUBACK timeout topic=%s",
                    exc.topic,
                )
                self._fire_subscribe_error(exc.topic, -1, "SUBACK timeout")
            else:
                _LOGGER.error(
                    "mips_cloud subscribe (after reconnect) failed unexpectedly "
                    "topic=%s: %s",
                    topic,
                    exc,
                )
                self._fire_subscribe_error(topic, -1, f"subscribe failed: {exc}")
        else:
            _LOGGER.debug("mips_cloud subscribed topic=%s qos=%d", topic, qos)
            if unattended:
                self._fire_subscribe_success(topic)

    async def _unsubscribe_async(self, topic: str) -> None:
        mqtt = self._mqtt
        with self._subs_lock:
            self._subs.pop(topic, None)
        if mqtt is None or not self._connected:
            return
        try:
            mqtt.unsubscribe(topic)
        except Exception as e:
            _LOGGER.warning("mips_cloud unsubscribe(%s) raised: %s", topic, e)

    # ----------------------------------------------------------- paho hooks

    def _on_connect(
        self,
        client: Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        # paho v5: reason_code is a paho.mqtt.reasoncodes.ReasonCode.
        # value 0 == "Success".
        rc_value = getattr(reason_code, "value", reason_code)
        if rc_value != 0:
            _LOGGER.error(
                "mips_cloud CONNACK rejected: reason_code=%s properties=%s",
                reason_code,
                properties,
            )
            self._fire_connect_future(
                MipsConnectionError(f"CONNACK reason_code={reason_code}")
            )
            return

        with self._state_lock:
            self._connected = True
        _LOGGER.info("mips_cloud CONNACK success")

        # Broker forgot our session on reconnect — re-issue every active
        # subscribe. MQTT-wise this is the same SUBSCRIBE packet as the
        # first-time path. The only difference: no caller is awaiting, so
        # exceptions get routed to _fire_subscribe_error instead of raised.
        # paho's on_connect is sync + on a different thread, so we hand off
        # to the main loop via call_soon_threadsafe.
        with self._subs_lock:
            active = list(self._subs.values())
        for sub in active:
            self._main_loop.call_soon_threadsafe(self._spawn_resubscribe, sub)

        self._fire_connect_future(None)
        self._dispatch_state_handlers(True)

    def _spawn_resubscribe(self, sub: _Subscription) -> None:
        """Spawn an unattended resubscribe task. Called via call_soon_threadsafe."""
        asyncio.create_task(
            self._subscribe_async(
                sub.topic,
                sub.handler,
                sub.decoder,
                sub.qos,
                unattended=True,
            )
        )

    def _on_disconnect(
        self,
        client: Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        rc_value = getattr(reason_code, "value", reason_code)
        log = _LOGGER.info if rc_value == 0 else _LOGGER.warning
        log("mips_cloud disconnected, reason_code=%s", reason_code)
        with self._state_lock:
            self._connected = False
        # paho will auto-reconnect on its own thread per reconnect_delay_set.
        self._dispatch_state_handlers(False)

    def _on_subscribe(
        self,
        client: Client,
        userdata: Any,
        mid: int,
        reason_codes: Any,
        properties: Any,
    ) -> None:
        # paho v5: reason_codes is list[ReasonCode]; their .value is the int.
        codes_int = [getattr(rc, "value", rc) for rc in reason_codes]
        with self._pending_lock:
            future = self._pending_subscribes.pop(mid, None)
        if future is None:
            _LOGGER.debug(
                "mips_cloud SUBACK for unknown mid=%d codes=%s", mid, codes_int
            )
            return
        if not future.done():
            self._main_loop.call_soon_threadsafe(future.set_result, codes_int)

    def _on_message(self, client: Client, userdata: Any, msg: MQTTMessage) -> None:
        topic = msg.topic
        payload = msg.payload
        # Find the matching subscription. Topic comparisons need to honour
        # MQTT wildcards because we may have subscribed `device/{did}/state/#`
        # while the actual incoming topic is `device/{did}/state/online`.
        with self._subs_lock:
            subs_snapshot = list(self._subs.values())
        for sub in subs_snapshot:
            if not topic_matches_sub(sub.topic, topic):
                continue
            try:
                decoded = sub.decoder(topic, payload)
            except Exception as e:
                _LOGGER.warning(
                    "mips_cloud failed to decode payload for topic=%s: %s",
                    topic,
                    e,
                )
                continue
            if decoded is None:
                continue
            self._dispatch_handler(sub.handler, decoded)

    # ------------------------------------------------------- dispatch utils

    def _dispatch_handler(
        self,
        handler: Callable[[Any], Union[None, Awaitable[None]]],
        arg: Any,
    ) -> None:
        def _run() -> None:
            try:
                ret = handler(arg)
            except Exception as e:
                _LOGGER.error("mips_cloud handler raised: %s", e)
                return
            if asyncio.iscoroutine(ret):
                asyncio.ensure_future(ret)

        self._main_loop.call_soon_threadsafe(_run)

    def _dispatch_state_handlers(self, connected: bool) -> None:
        with self._handlers_lock:
            handlers = list(self._mips_state_handlers)
        for h in handlers:
            self._dispatch_handler(h, connected)

    def _fire_subscribe_error(self, topic: str, code: int, reason: str) -> None:
        with self._handlers_lock:
            handlers = list(self._subscribe_error_handlers)
        info = (topic, code, reason)
        for h in handlers:
            self._dispatch_handler(h, info)

    def _fire_subscribe_success(self, topic: str) -> None:
        with self._handlers_lock:
            handlers = list(self._subscribe_success_handlers)
        for h in handlers:
            self._dispatch_handler(h, topic)

    def _fire_connect_future(self, error: Optional[Exception]) -> None:
        future = self._connect_future
        if future is None or future.done():
            return
        if error is None:
            self._main_loop.call_soon_threadsafe(future.set_result, None)
        else:
            self._main_loop.call_soon_threadsafe(future.set_exception, error)

    # ----------------------------------------------------------- decoders

    @staticmethod
    def _make_bind_decoder(
        kind: str,
    ) -> Callable[[str, bytes], Optional[MIoTDeviceBindEvent]]:
        # Account-level bind/unbind: did lives in the payload, uid in the topic.
        def decode(topic: str, payload: bytes) -> Optional[MIoTDeviceBindEvent]:
            m = _TOPIC_USER_OP.match(topic)
            if not m or m.group(2) != kind:
                return None
            uid = m.group(1)
            raw = _parse_json_payload(payload) or {}
            did = raw.get("did") if isinstance(raw, dict) else None
            return MIoTDeviceBindEvent(
                uid=uid,
                event=kind,  # type: ignore[arg-type]  # kind ∈ {bind,unbind}
                did=str(did) if did is not None else None,
                raw=raw if isinstance(raw, dict) else {},
                timestamp_ms=_now_ms(),
            )

        return decode

    @staticmethod
    def _make_device_meta_decoder() -> Callable[
        [str, bytes], Optional[MIoTDeviceBindEvent]
    ]:
        # Device-level rename / hr_change: did + op come from the topic; the
        # payload is undocumented and kept verbatim in `raw`. Only the exact op
        # leaves are subscribed (no `#` wildcard), so a non-matching topic
        # should never arrive — the `if not m` guard is purely defensive.
        def decode(topic: str, payload: bytes) -> Optional[MIoTDeviceBindEvent]:
            m = _TOPIC_DEVICE_META.match(topic)
            if not m:
                return None
            did = m.group(1)
            op = m.group(2)  # rename | hr_change
            raw = _parse_json_payload(payload) or {}
            return MIoTDeviceBindEvent(
                uid=str(raw.get("uid", "")) if isinstance(raw, dict) else "",
                event=op,  # type: ignore[arg-type]  # op ∈ {rename,hr_change}
                did=did,
                raw=raw if isinstance(raw, dict) else {},
                timestamp_ms=_now_ms(),
            )

        return decode

    @staticmethod
    def _make_scene_decoder() -> Callable[
        [str, bytes], Optional[MIoTSceneChangedEvent]
    ]:
        # Home-level scene rename/delete/edit: home_id + op from the topic;
        # scene_id pulled from the (undocumented) payload if present. Only the
        # exact op leaves are subscribed (no `#` wildcard), so a non-matching
        # topic should never arrive — the `if not m` guard is purely defensive.
        def decode(topic: str, payload: bytes) -> Optional[MIoTSceneChangedEvent]:
            m = _TOPIC_HOME_SCENE.match(topic)
            if not m:
                return None
            home_id = m.group(1)
            op = m.group(2)  # rename | delete | edit
            raw = _parse_json_payload(payload) or {}
            scene_id = raw.get("scene_id") if isinstance(raw, dict) else None
            return MIoTSceneChangedEvent(
                home_id=home_id,
                event=op,  # type: ignore[arg-type]  # op ∈ {rename,delete,edit}
                scene_id=str(scene_id) if scene_id is not None else None,
                raw=raw if isinstance(raw, dict) else {},
                timestamp_ms=_now_ms(),
            )

        return decode

    @staticmethod
    def _make_device_state_decoder() -> Callable[
        [str, bytes], Optional[MIoTDeviceStateEvent]
    ]:
        # Device-level cloud online/offline: did + event come from the topic;
        # the payload is undocumented and kept verbatim in `raw`. Only the
        # exact op leaves are subscribed (no `#` wildcard), so a non-matching
        # topic should never arrive — the `if not m` guard is purely
        # defensive.
        def decode(topic: str, payload: bytes) -> Optional[MIoTDeviceStateEvent]:
            m = _TOPIC_DEVICE_STATE.match(topic)
            if not m:
                return None
            did = m.group(1)
            op = m.group(2)  # online | offline
            raw = _parse_json_payload(payload) or {}
            return MIoTDeviceStateEvent(
                did=did,
                event=op,  # type: ignore[arg-type]  # op ∈ {online,offline}
                raw=raw if isinstance(raw, dict) else {},
                timestamp_ms=_now_ms(),
            )

        return decode


# ---------------------------------------------------------------- helpers


def _parse_json_payload(payload: bytes) -> Optional[dict]:
    if not payload:
        return None
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(decoded, dict):
        return decoded
    return None


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


# Human-readable summary of MQTT v5 SUBACK reason codes likely to surface
# from the MIoT broker. Codes not listed fall back to the hex value.
_REASON_CODES = {
    0x00: "Granted QoS 0",
    0x01: "Granted QoS 1",
    0x02: "Granted QoS 2",
    0x80: "Unspecified error",
    0x83: "Implementation specific error",
    0x87: "Not authorized",
    0x8F: "Topic filter invalid",
    0x91: "Packet identifier in use",
    0x97: "Quota exceeded",
    0x9E: "Shared subscriptions not supported",
    0xA1: "Subscription identifiers not supported",
    0xA2: "Wildcard subscriptions not supported",
}


def _describe_reason_code(code: int) -> str:
    return _REASON_CODES.get(code, f"reason_code=0x{code:02x}")
