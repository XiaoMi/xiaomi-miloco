# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for `miot.mips_cloud`.

These tests inject a fake paho Client via the `client_factory` hook so no
real network or broker is needed. The fake records every call (CONNECT
params, subscribe topics, etc.) and exposes helpers to fire paho-style
callbacks back into MIoTMipsCloud.

Coverage:
  - CONNECT field correctness (host, port, client_id, username, password)
  - SUBACK success path
  - SUBACK rejection path (e.g. ACL 0x87) → MipsSubscribeRejectedError
  - SUBACK timeout path → MipsSubscribeTimeoutError
  - Message dispatch end-to-end through topic_matches_sub
  - Token rotate calls username_pw_set with new password + reconnect
  - Reconnect resubscribes all active topics
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import pytest
from miot.mips_cloud import MIoTMipsCloud
from miot.types import (
    MIoTDeviceBindEvent,
    MIoTDeviceStateEvent,
    MIoTSceneChangedEvent,
    MipsSubscribeRejectedError,
    MipsSubscribeTimeoutError,
)
from paho.mqtt.enums import MQTTErrorCode

_LOGGER = logging.getLogger(__name__)


class _FakeReasonCode:
    """Stand-in for paho.mqtt.reasoncodes.ReasonCode in callbacks."""

    def __init__(self, value: int) -> None:
        self.value = value


class _FakeMqttClient:
    """Subset of paho.mqtt.client.Client used by MIoTMipsCloud.

    Stores all configuration calls so tests can assert on them, and exposes
    `fire_*` helpers to drive callbacks as if the broker had responded.
    """

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.username: str | None = None
        self.password: str | None = None
        self.tls_set_args: tuple = ()
        self.tls_insecure: bool = False
        self.reconnect_delay: tuple[int, int] | None = None
        self.connect_host: str | None = None
        self.connect_port: int | None = None
        self.connect_keepalive: int | None = None
        self.connect_clean_start: bool | None = None
        self._connected: bool = False
        self._next_mid: int = 1
        self.subscribed: list[tuple[str, int, int]] = []  # (topic, qos, mid)
        self.unsubscribed: list[str] = []
        self.username_pw_set_history: list[tuple[str | None, str | None]] = []
        self.reconnect_called: int = 0

        self.on_connect: Callable | None = None
        self.on_disconnect: Callable | None = None
        self.on_subscribe: Callable | None = None
        self.on_message: Callable | None = None

    # ------------------------------------------------------------- paho API

    def username_pw_set(
        self, username: str | None = None, password: str | None = None
    ) -> None:
        self.username = username
        self.password = password
        self.username_pw_set_history.append((username, password))

    def tls_set(self, **kwargs: Any) -> None:
        self.tls_set_args = tuple(kwargs.items())

    def tls_insecure_set(self, value: bool) -> None:
        self.tls_insecure = value

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def enable_logger(self, logger: logging.Logger | None = None) -> None:
        pass

    def connect(self, host: str, port: int, keepalive: int, clean_start: bool) -> None:
        # Real paho would also do TCP/TLS handshake here. We just record args
        # and let the test drive on_connect explicitly via fire_connect.
        self.connect_host = host
        self.connect_port = port
        self.connect_keepalive = keepalive
        self.connect_clean_start = clean_start

    def disconnect(self) -> None:
        self._connected = False
        # Real paho fires on_disconnect from its network thread. Mirror that.
        if self.on_disconnect:
            self.on_disconnect(self, None, None, _FakeReasonCode(0), None)

    def reconnect(self) -> None:
        self.reconnect_called += 1

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass

    def is_connected(self) -> bool:
        return self._connected

    def subscribe(self, topic: str, qos: int) -> tuple[MQTTErrorCode, int]:
        mid = self._next_mid
        self._next_mid += 1
        self.subscribed.append((topic, qos, mid))
        return MQTTErrorCode.MQTT_ERR_SUCCESS, mid

    def unsubscribe(self, topic: str) -> tuple[MQTTErrorCode, int]:
        mid = self._next_mid
        self._next_mid += 1
        self.unsubscribed.append(topic)
        return MQTTErrorCode.MQTT_ERR_SUCCESS, mid

    # ------------------------------------------------------- test-side drivers

    def fire_connect(self, reason_code: int = 0) -> None:
        self._connected = reason_code == 0
        assert self.on_connect is not None
        self.on_connect(self, None, None, _FakeReasonCode(reason_code), None)

    def fire_disconnect(self, reason_code: int = 0) -> None:
        self._connected = False
        assert self.on_disconnect is not None
        self.on_disconnect(self, None, None, _FakeReasonCode(reason_code), None)

    def fire_suback(self, mid: int, reason_codes: list[int]) -> None:
        assert self.on_subscribe is not None
        self.on_subscribe(
            self, None, mid, [_FakeReasonCode(c) for c in reason_codes], None
        )

    def fire_message(self, topic: str, payload: bytes) -> None:
        assert self.on_message is not None

        class _Msg:
            def __init__(self, t: str, p: bytes) -> None:
                self.topic = t
                self.payload = p

        self.on_message(self, None, _Msg(topic, payload))


# --------------------------------------------------------------------- helpers


def _make_mips(token: str = "tok-abc") -> tuple[MIoTMipsCloud, _FakeMqttClient]:
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:  # type: ignore[override]
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips = MIoTMipsCloud(
        uuid="u-test",
        cloud_server="cn",
        app_id="2882303761520431603",
        token=token,
        client_factory=factory,  # type: ignore[arg-type]
    )
    # init_async needs to call factory + run_in_executor(connect) + await CONNACK
    # We construct the client and let the caller drive init.
    return mips, holder.get("c")  # type: ignore[return-value]


async def _connect(
    mips: MIoTMipsCloud, fake_holder: dict[str, _FakeMqttClient]
) -> _FakeMqttClient:
    """Drive a successful init_async → CONNACK in one call."""

    async def drive_connack() -> None:
        # Wait for connect() executor call to populate the factory.
        for _ in range(50):
            if "c" in fake_holder and fake_holder["c"].connect_host is not None:
                break
            await asyncio.sleep(0.01)
        fake_holder["c"].fire_connect(reason_code=0)

    init_task = asyncio.create_task(mips.init_async())
    driver_task = asyncio.create_task(drive_connack())
    await asyncio.gather(init_task, driver_task)
    return fake_holder["c"]


async def _ack_subscribes(
    fake: _FakeMqttClient, count: int, *, timeout: float = 1.0
) -> None:
    """Fire a success SUBACK for each subscribe as it appears, until ``count``
    have been acked. Handles sequential multi-topic subscribes (each
    _subscribe_async awaits its own SUBACK before the next is issued)."""
    acked: set[int] = set()
    deadline = asyncio.get_event_loop().time() + timeout
    while len(acked) < count and asyncio.get_event_loop().time() < deadline:
        for _topic, _qos, mid in list(fake.subscribed):
            if mid not in acked:
                fake.fire_suback(mid, [2])
                acked.add(mid)
        await asyncio.sleep(0.005)


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.asyncio
async def test_subscribe_rejected_by_acl_raises():
    """An ACL rejection (reason_code = 0x87 Not authorized) must surface as
    MipsSubscribeRejectedError, not be silently swallowed. This is the
    explicit user requirement for `user/{uid}/g_op/bind`."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    async def driver() -> None:
        for _ in range(50):
            if fake.subscribed:
                break
            await asyncio.sleep(0.005)
        _, _, mid = fake.subscribed[-1]
        fake.fire_suback(mid, [0x87])  # Not authorized

    try:
        with pytest.raises(MipsSubscribeRejectedError) as excinfo:
            await asyncio.gather(
                mips.sub_user_bind_async("uid-987", handler=lambda _m: None),
                driver(),
            )
        err = excinfo.value
        assert err.topic == "user/uid-987/g_op/bind"
        assert err.reason_code == 0x87
        assert "Not authorized" in err.reason_string
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_subscribe_timeout_raises():
    """SUBACK never arrives → MipsSubscribeTimeoutError after the configured
    timeout. We monkeypatch the timeout to a small value so the test is fast."""
    import miot.mips_cloud as mc

    orig_timeout = mc.MIHOME_MQTT_SUBSCRIBE_TIMEOUT
    mc.MIHOME_MQTT_SUBSCRIBE_TIMEOUT = 0.2

    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    await _connect(mips, holder)

    try:
        with pytest.raises(MipsSubscribeTimeoutError):
            await mips.sub_user_unbind_async("uid-x", handler=lambda _m: None)
    finally:
        mc.MIHOME_MQTT_SUBSCRIBE_TIMEOUT = orig_timeout
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_user_bind_decoded_event_dispatched():
    """user/{uid}/g_op/bind → MIoTDeviceBindEvent with event='bind'."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    received: list[MIoTDeviceBindEvent] = []

    def on_bind(msg: MIoTDeviceBindEvent) -> None:
        received.append(msg)

    async def driver() -> None:
        for _ in range(50):
            if fake.subscribed:
                break
            await asyncio.sleep(0.005)
        _, _, mid = fake.subscribed[-1]
        fake.fire_suback(mid, [2])

    try:
        await asyncio.gather(
            mips.sub_user_bind_async("uid-42", handler=on_bind), driver()
        )
        fake.fire_message("user/uid-42/g_op/bind", b'{"did": "new-device-123"}')
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].uid == "uid-42"
        assert received[0].event == "bind"
        assert received[0].did == "new-device-123"
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_device_meta_subscribes_exact_topics():
    """sub_device_meta_changed_async subscribes the exact per-op leaf topics
    (NOT a `g_op/#` wildcard — the broker ACL rejects the wildcard)."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    try:
        await asyncio.gather(
            mips.sub_device_meta_changed_async("dev-1", handler=lambda _m: None),
            _ack_subscribes(fake, 2),
        )
        topics = {t for t, _, _ in fake.subscribed}
        assert "device/dev-1/g_op/rename" in topics
        assert "device/dev-1/g_op/hr_change" in topics
        assert "device/dev-1/g_op/#" not in topics
    finally:
        await mips.deinit_async()


@pytest.mark.parametrize("op", ["rename", "hr_change"])
@pytest.mark.asyncio
async def test_device_meta_decoded_event_dispatched(op):
    """device/{did}/g_op/{rename,hr_change} → MIoTDeviceBindEvent with
    event=op and did parsed from the topic."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    received: list[MIoTDeviceBindEvent] = []

    try:
        await asyncio.gather(
            mips.sub_device_meta_changed_async("dev-42", handler=received.append),
            _ack_subscribes(fake, 2),
        )
        fake.fire_message(f"device/dev-42/g_op/{op}", b"{}")
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].event == op
        assert received[0].did == "dev-42"
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_home_scene_subscribes_exact_topics():
    """sub_home_scene_changed_async subscribes the exact per-op leaf topics
    (NOT a `scene/#` wildcard — the broker ACL rejects the wildcard)."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    try:
        await asyncio.gather(
            mips.sub_home_scene_changed_async("home-1", handler=lambda _m: None),
            _ack_subscribes(fake, 3),
        )
        topics = {t for t, _, _ in fake.subscribed}
        assert "home/home-1/scene/rename" in topics
        assert "home/home-1/scene/delete" in topics
        assert "home/home-1/scene/edit" in topics
        assert "home/home-1/scene/#" not in topics
    finally:
        await mips.deinit_async()


@pytest.mark.parametrize("op", ["rename", "delete", "edit"])
@pytest.mark.asyncio
async def test_home_scene_decoded_event_dispatched(op):
    """home/{home_id}/scene/{rename,delete,edit} → MIoTSceneChangedEvent with
    event=op, home_id from topic, scene_id from payload."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    received: list[MIoTSceneChangedEvent] = []

    try:
        await asyncio.gather(
            mips.sub_home_scene_changed_async("home-9", handler=received.append),
            _ack_subscribes(fake, 3),
        )
        fake.fire_message(f"home/home-9/scene/{op}", b'{"scene_id": "sc-1"}')
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].home_id == "home-9"
        assert received[0].event == op
        assert received[0].scene_id == "sc-1"
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_token_rotate_updates_password_and_reconnects():
    mips, _ = _make_mips(token="t-old")
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    assert fake.password == "t-old"
    try:
        await mips.update_access_token("t-new")
        assert fake.password == "t-new"
        assert fake.reconnect_called == 1
        # username unchanged
        assert fake.username == "2882303761520431603"
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_reconnect_resubscribes_active_topics():
    """After a disconnect / reconnect cycle the same topics should be
    resubscribed automatically, because the broker forgets state and we must
    re-issue SUBSCRIBE."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    async def driver() -> None:
        for _ in range(50):
            if fake.subscribed:
                break
            await asyncio.sleep(0.005)
        _, _, mid = fake.subscribed[-1]
        fake.fire_suback(mid, [2])

    try:
        await asyncio.gather(
            mips.sub_user_bind_async("uid-rec", handler=lambda _m: None), driver()
        )
        before = len(fake.subscribed)
        # Simulate broker drop + paho auto-reconnect → on_connect fires again.
        fake.fire_disconnect(reason_code=1)
        fake.fire_connect(reason_code=0)
        # Subscribe-on-reconnect is scheduled via call_soon_threadsafe →
        # asyncio task. Yield ticks for the task to call mqtt.subscribe.
        for _ in range(5):
            await asyncio.sleep(0)
            if len(fake.subscribed) > before:
                break
        assert len(fake.subscribed) == before + 1
        assert fake.subscribed[-1][0] == "user/uid-rec/g_op/bind"
        # Drive the SUBACK so the in-flight unattended subscribe task finishes
        # cleanly (otherwise it'd block on SUBACK timeout into deinit).
        _, _, new_mid = fake.subscribed[-1]
        fake.fire_suback(new_mid, [2])
        await asyncio.sleep(0.01)
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_subscribe_after_reconnect_rejection_fires_error_handler():
    """ACL revoked mid-session: reconnect → subscribe SUBACK 0x87 →
    subscribe_error_handler fires + topic dropped from active subs.

    Without this path, a broker ACL change after the first connect would
    silently kill push delivery while /mips_status still claimed
    user_bind_subscribed=true."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    async def initial_driver() -> None:
        for _ in range(50):
            if fake.subscribed:
                break
            await asyncio.sleep(0.005)
        _, _, mid = fake.subscribed[-1]
        fake.fire_suback(mid, [2])

    try:
        await asyncio.gather(
            mips.sub_user_bind_async("uid-acl", handler=lambda _m: None),
            initial_driver(),
        )

        errors: list[tuple[str, int, str]] = []

        def on_err(info: tuple[str, int, str]) -> None:
            errors.append(info)

        mips.register_subscribe_error_handler(on_err)

        # Simulate broker drop + paho auto-reconnect. _on_connect schedules
        # _subscribe_unattended_async which goes through the same
        # _subscribe_async path as first-time subscribes — at the MQTT
        # layer this is just another SUBSCRIBE packet.
        before = len(fake.subscribed)
        fake.fire_disconnect(reason_code=1)
        fake.fire_connect(reason_code=0)
        # Yield until the unattended subscribe issues mqtt.subscribe.
        for _ in range(5):
            await asyncio.sleep(0)
            if len(fake.subscribed) > before:
                break
        assert len(fake.subscribed) == before + 1
        _, _, new_mid = fake.subscribed[-1]
        # Broker SUBACKs with Not authorized (ACL revoked mid-session).
        fake.fire_suback(new_mid, [0x87])
        # _subscribe_unattended_async catches MipsSubscribeRejectedError,
        # then _fire_subscribe_error dispatches via call_soon_threadsafe.
        await asyncio.sleep(0.05)

        assert len(errors) == 1
        topic, code, reason = errors[0]
        assert topic == "user/uid-acl/g_op/bind"
        assert code == 0x87
        assert "Not authorized" in reason
        # Topic must be dropped from _subs so we don't re-issue the doomed
        # subscribe on the *next* reconnect cycle.
        assert "user/uid-acl/g_op/bind" not in mips._subs  # type: ignore[attr-defined]
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_subscribe_after_reconnect_success_fires_success_handler():
    """ACL restored after a prior rejection: reconnect → subscribe SUBACK OK →
    subscribe_success_handler fires, allowing the caller to clear a stale
    mips_user_sub_error flag."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    async def initial_driver() -> None:
        for _ in range(50):
            if fake.subscribed:
                break
            await asyncio.sleep(0.005)
        _, _, mid = fake.subscribed[-1]
        fake.fire_suback(mid, [2])

    try:
        await asyncio.gather(
            mips.sub_user_bind_async("uid-ok", handler=lambda _m: None),
            initial_driver(),
        )

        successes: list[str] = []

        def on_success(topic: str) -> None:
            successes.append(topic)

        mips.register_subscribe_success_handler(on_success)

        # Simulate broker drop + paho auto-reconnect.
        before = len(fake.subscribed)
        fake.fire_disconnect(reason_code=1)
        fake.fire_connect(reason_code=0)
        for _ in range(5):
            await asyncio.sleep(0)
            if len(fake.subscribed) > before:
                break
        assert len(fake.subscribed) == before + 1
        _, _, new_mid = fake.subscribed[-1]
        # Broker SUBACKs with success this time (ACL restored).
        fake.fire_suback(new_mid, [2])
        await asyncio.sleep(0.05)

        assert len(successes) == 1
        assert successes[0] == "user/uid-ok/g_op/bind"
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_subscribe_timeout_keeps_topic_and_reconnect_retries():
    """SUBACK timeout is treated as transient: the topic stays in _subs so the
    next reconnect re-issues SUBSCRIBE. A momentary network blip must not
    permanently disable push delivery."""
    import miot.mips_cloud as mc

    orig_timeout = mc.MIHOME_MQTT_SUBSCRIBE_TIMEOUT
    mc.MIHOME_MQTT_SUBSCRIBE_TIMEOUT = 0.1

    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    try:
        # First subscribe never gets a SUBACK → raises but topic stays.
        with pytest.raises(MipsSubscribeTimeoutError):
            await mips.sub_user_bind_async("uid-to", handler=lambda _m: None)
        assert "user/uid-to/g_op/bind" in mips._subs  # type: ignore[attr-defined]

        # Reconnect should re-issue SUBSCRIBE for the kept topic.
        before = len(fake.subscribed)
        fake.fire_disconnect(reason_code=1)
        fake.fire_connect(reason_code=0)
        for _ in range(5):
            await asyncio.sleep(0)
            if len(fake.subscribed) > before:
                break
        assert len(fake.subscribed) == before + 1
        assert fake.subscribed[-1][0] == "user/uid-to/g_op/bind"
        # Drain the in-flight unattended subscribe so deinit doesn't block.
        _, _, new_mid = fake.subscribed[-1]
        fake.fire_suback(new_mid, [2])
        await asyncio.sleep(0.01)
    finally:
        mc.MIHOME_MQTT_SUBSCRIBE_TIMEOUT = orig_timeout
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_subscribe_transient_rejection_keeps_topic_and_reconnect_retries():
    """Non-permanent SUBACK rejection (0x97 Quota exceeded) is treated as
    transient: topic stays in _subs and reconnect re-issues SUBSCRIBE. Only
    permanent codes (0x87 ACL, 0x8F invalid filter, 0x9E/A1/A2 unsupported)
    pop the topic — covered by test_subscribe_after_reconnect_rejection_*."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    async def driver() -> None:
        for _ in range(50):
            if fake.subscribed:
                break
            await asyncio.sleep(0.005)
        _, _, mid = fake.subscribed[-1]
        fake.fire_suback(mid, [0x97])  # Quota exceeded — transient

    try:
        with pytest.raises(MipsSubscribeRejectedError) as excinfo:
            await asyncio.gather(
                mips.sub_user_bind_async("uid-q", handler=lambda _m: None),
                driver(),
            )
        assert excinfo.value.reason_code == 0x97
        # Transient rejection: topic must stay so reconnect retries.
        assert "user/uid-q/g_op/bind" in mips._subs  # type: ignore[attr-defined]

        before = len(fake.subscribed)
        fake.fire_disconnect(reason_code=1)
        fake.fire_connect(reason_code=0)
        for _ in range(5):
            await asyncio.sleep(0)
            if len(fake.subscribed) > before:
                break
        assert len(fake.subscribed) == before + 1
        assert fake.subscribed[-1][0] == "user/uid-q/g_op/bind"
        _, _, new_mid = fake.subscribed[-1]
        fake.fire_suback(new_mid, [2])
        await asyncio.sleep(0.01)
    finally:
        await mips.deinit_async()


# ============================================================================
# device/{did}/state/{online,offline} — cloud online/offline state subs
# ============================================================================


@pytest.mark.asyncio
async def test_device_state_subscribes_exact_topics():
    """sub_device_state_async subscribes the exact per-op leaf topics
    (NOT a `state/#` wildcard — the broker ACL rejects the wildcard, same as
    the g_op/# case)."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    try:
        await asyncio.gather(
            mips.sub_device_state_async("dev-1", handler=lambda _m: None),
            _ack_subscribes(fake, 2),
        )
        topics = {t for t, _, _ in fake.subscribed}
        assert "device/dev-1/state/online" in topics
        assert "device/dev-1/state/offline" in topics
        assert "device/dev-1/state/#" not in topics
    finally:
        await mips.deinit_async()


@pytest.mark.parametrize("op", ["online", "offline"])
@pytest.mark.asyncio
async def test_device_state_decoded_event_dispatched(op):
    """device/{did}/state/{online,offline} → MIoTDeviceStateEvent with
    event=op and did parsed from the topic; payload kept in `raw`."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    received: list[MIoTDeviceStateEvent] = []

    try:
        await asyncio.gather(
            mips.sub_device_state_async("dev-42", handler=received.append),
            _ack_subscribes(fake, 2),
        )
        fake.fire_message(
            f"device/dev-42/state/{op}",
            b'{"device_id": "dev-42", "event": "' + op.encode() + b'"}',
        )
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].event == op
        assert received[0].did == "dev-42"
        assert received[0].raw.get("device_id") == "dev-42"
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_device_state_unsubscribes_exact_topics():
    """unsub_device_state_async removes both online + offline leaf topics."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    try:
        await asyncio.gather(
            mips.sub_device_state_async("dev-7", handler=lambda _m: None),
            _ack_subscribes(fake, 2),
        )
        await mips.unsub_device_state_async("dev-7")
        unsubbed = set(fake.unsubscribed)
        assert "device/dev-7/state/online" in unsubbed
        assert "device/dev-7/state/offline" in unsubbed
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_device_state_reconnect_resubscribes_both_topics():
    """After disconnect/reconnect both online + offline leaf topics are
    re-issued by the _subs replay."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    try:
        await asyncio.gather(
            mips.sub_device_state_async("dev-rec", handler=lambda _m: None),
            _ack_subscribes(fake, 2),
        )
        before = len(fake.subscribed)
        fake.fire_disconnect(reason_code=1)
        fake.fire_connect(reason_code=0)
        for _ in range(10):
            await asyncio.sleep(0)
            if len(fake.subscribed) >= before + 2:
                break
        resub_topics = {t for t, _, _ in fake.subscribed[before:]}
        assert "device/dev-rec/state/online" in resub_topics
        assert "device/dev-rec/state/offline" in resub_topics
        for _, _, mid in fake.subscribed[before:]:
            fake.fire_suback(mid, [2])
        await asyncio.sleep(0.01)
    finally:
        await mips.deinit_async()
