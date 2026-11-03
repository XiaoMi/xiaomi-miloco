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
    MipsConnectionError,
    MipsSubscribeRejectedError,
    MipsSubscribeTimeoutError,
)
from paho.mqtt.enums import MQTTErrorCode

_LOGGER = logging.getLogger(__name__)

# 无真实网络/凭据/设备依赖 —— CI 的 `miot 单元测试` 步骤按 `-m unit` 选择。
pytestmark = pytest.mark.unit


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
async def test_device_state_subscribes_wildcard():
    """sub_device_state_async issues ONE wildcard subscription per did —
    `device/{did}/state/#` (same filter as xiaomi-ha; probe-verified allowed
    by the broker ACL). Exact leaves are NOT subscribed individually."""
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
            _ack_subscribes(fake, 1),
        )
        topics = {t for t, _, _ in fake.subscribed}
        assert "device/dev-1/state/#" in topics
        assert len(topics) == 1
    finally:
        await mips.deinit_async()


@pytest.mark.parametrize("op", ["online", "offline"])
@pytest.mark.asyncio
async def test_device_state_decoded_event_dispatched(op):
    """Messages arrive on exact leaf topics despite the `#` subscription →
    MIoTDeviceStateEvent with event=op and did parsed from the topic."""
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
            _ack_subscribes(fake, 1),
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
async def test_device_state_unknown_leaf_dropped():
    """We subscribe `state/#`, so ANY other leaf the cloud publishes under
    state/ reaches the decoder too — it must be silently dropped (the
    load-bearing `if not m` guard), not raise or dispatch an illegal event."""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)
    received: list = []
    try:
        await asyncio.gather(
            mips.sub_device_state_async("dev-9", handler=received.append),
            _ack_subscribes(fake, 1),
        )
        fake.fire_message("device/dev-9/state/whatever", b"{}")
        await asyncio.sleep(0.01)
        assert received == []
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_device_state_unsubscribes_wildcard():
    """unsub_device_state_async removes the single wildcard topic."""
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
            _ack_subscribes(fake, 1),
        )
        await mips.unsub_device_state_async("dev-7")
        unsubbed = set(fake.unsubscribed)
        assert "device/dev-7/state/#" in unsubbed
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_subscribe_superseded_by_concurrent_unsubscribe_raises():
    """SUBACK 等待期间同 topic 被并发退订(上层 bind 作废路径的 unsub 弹掉了
    在飞订阅自己的 _subs 条目)时,订阅必须抛 MipsConnectionError——否则 SDK
    在 await 之后记"已订阅"、broker 却已 UNSUB,`did in set` 短路会吞掉之后
    所有重试,该 did 的推送静默丢失直到 mips 实例重建。"""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)

    try:
        subscribe_task = asyncio.create_task(
            mips.sub_device_state_async("dev-x", handler=lambda _m: None)
        )
        # 等 SUBSCRIBE 发出、停在 SUBACK 等待上。
        for _ in range(50):
            if fake.subscribed:
                break
            await asyncio.sleep(0.005)
        _, _, mid = fake.subscribed[-1]

        # 并发退订:弹掉 _subs 条目、发出 UNSUBSCRIBE,但不补 SUBACK。
        await mips.unsub_device_state_async("dev-x")

        # SUBACK 到达后,订阅路径必须发现自己的授权已过期。
        fake.fire_suback(mid, [2])
        with pytest.raises(MipsConnectionError):
            await subscribe_task

        # _subs 里已无该 topic(不会进重连重放),且 UNSUBSCRIBE 已发出。
        with mips._subs_lock:
            assert "device/dev-x/state/#" not in mips._subs
        assert "device/dev-x/state/#" in set(fake.unsubscribed)
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_device_state_reconnect_resubscribes_wildcard():
    """After disconnect/reconnect the wildcard topic is re-issued by the
    _subs replay."""
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
            _ack_subscribes(fake, 1),
        )
        before = len(fake.subscribed)
        fake.fire_disconnect(reason_code=1)
        fake.fire_connect(reason_code=0)
        for _ in range(10):
            await asyncio.sleep(0)
            if len(fake.subscribed) >= before + 1:
                break
        resub_topics = {t for t, _, _ in fake.subscribed[before:]}
        assert "device/dev-rec/state/#" in resub_topics
        for _, _, mid in fake.subscribed[before:]:
            fake.fire_suback(mid, [2])
        await asyncio.sleep(0.01)
    finally:
        await mips.deinit_async()


# ── 属性推送 ──────────────────────────────────────────────────────────


def _props_decoder():
    return MIoTMipsCloud._make_device_props_decoder()


def test_a_properties_changed_push_decodes_into_one_change():
    event = _props_decoder()(
        "device/d1/up/properties_changed/2/1",
        b'{"method":"properties_changed",'
        b'"params":{"did":"d1","siid":2,"piid":1,"value":26}}',
    )

    assert event is not None
    assert event.did == "d1"
    assert [(c.siid, c.piid, c.value) for c in event.changes] == [(2, 1, 26)]
    assert event.timestamp_ms > 0


def test_a_params_list_decodes_into_several_changes():
    """broker 没文档化 params 形态：实测是裸 dict，HA 的惯例是 list，两种都收。"""
    event = _props_decoder()(
        "device/d1/up/properties_changed",
        b'{"method":"properties_changed","params":['
        b'{"siid":2,"piid":1,"value":1},{"siid":3,"piid":4,"value":"x"}]}',
    )

    assert [(c.siid, c.piid, c.value) for c in event.changes] == [
        (2, 1, 1),
        (3, 4, "x"),
    ]


def test_a_topic_outside_the_properties_changed_subtree_is_dropped():
    """订的是通配，别的 leaf 也会落进来 —— 这道闸是白名单，不是防御性冗余。

    payload 必须完全合法，否则后面几道闸会替它把这条拒掉，分不出是谁拒的。
    """
    assert (
        _props_decoder()(
            "device/d1/up/something_else/2/1",
            b'{"method":"properties_changed","params":{"siid":2,"piid":1,"value":1}}',
        )
        is None
    )


def test_a_payload_with_another_method_is_dropped():
    assert (
        _props_decoder()(
            "device/d1/up/properties_changed/2/1",
            b'{"method":"event_occured","params":{"siid":2,"piid":1,"value":1}}',
        )
        is None
    )


def test_a_payload_did_that_contradicts_the_topic_is_dropped():
    """不一致会把一台设备的值写到另一台的路径上。"""
    assert (
        _props_decoder()(
            "device/d1/up/properties_changed/2/1",
            b'{"method":"properties_changed",'
            b'"params":{"did":"d2","siid":2,"piid":1,"value":1}}',
        )
        is None
    )


def test_a_single_entry_that_contradicts_the_topic_iid_is_dropped():
    assert (
        _props_decoder()(
            "device/d1/up/properties_changed/2/1",
            b'{"method":"properties_changed","params":{"siid":9,"piid":9,"value":1}}',
        )
        is None
    )


def test_a_multi_entry_payload_is_not_checked_against_the_topic_iid():
    """topic 只承载一对 iid，多条时无从对应，校验不了。

    首条故意与 topic 的 iid 不一致：一致的话「校验也套到多条上」这个错也能让它绿。
    """
    event = _props_decoder()(
        "device/d1/up/properties_changed/2/1",
        b'{"method":"properties_changed","params":['
        b'{"siid":9,"piid":9,"value":2},{"siid":2,"piid":1,"value":1}]}',
    )

    assert [(c.siid, c.piid) for c in event.changes] == [(9, 9), (2, 1)]


def test_an_entry_without_integer_ids_is_skipped_not_fatal():
    event = _props_decoder()(
        "device/d1/up/properties_changed",
        b'{"method":"properties_changed","params":['
        b'{"siid":"x","piid":1,"value":1},{"siid":3,"piid":4,"value":2}]}',
    )

    assert [(c.siid, c.piid) for c in event.changes] == [(3, 4)]


def test_a_message_that_yields_no_entry_is_dropped():
    assert (
        _props_decoder()(
            "device/d1/up/properties_changed",
            b'{"method":"properties_changed","params":[]}',
        )
        is None
    )


def test_a_non_json_props_payload_is_dropped():
    assert _props_decoder()("device/d1/up/properties_changed/2/1", b"not json") is None


@pytest.mark.asyncio
async def test_a_props_push_reaches_the_handler_through_the_wildcard():
    """通配是必需的：推送落在 .../properties_changed/{siid}/{piid}，
    leaf-only 的 filter 匹配不到任何已发布 topic。"""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)
    received: list = []
    try:
        await asyncio.gather(
            mips.sub_device_props_async("dev-9", handler=received.append),
            _ack_subscribes(fake, 1),
        )
        fake.fire_message(
            "device/dev-9/up/properties_changed/2/1",
            b'{"method":"properties_changed","params":{"siid":2,"piid":1,"value":7}}',
        )
        await asyncio.sleep(0.01)

        assert [(c.siid, c.piid, c.value) for c in received[0].changes] == [(2, 1, 7)]
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_props_unsubscribes_the_same_wildcard_it_subscribed():
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)
    try:
        await asyncio.gather(
            mips.sub_device_props_async("dev-9", handler=lambda _msg: None),
            _ack_subscribes(fake, 1),
        )
        await mips.unsub_device_props_async("dev-9")

        # 判据是「退的和订的是同一个」，不是「退了某个 topic」：只断言后者时，
        # 订阅端改成 leaf 也能绿
        assert set(fake.unsubscribed) == {
            topic for topic, _qos, _mid in fake.subscribed
        }
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_an_event_push_reaches_the_handler_through_the_wildcard():
    """事件那条子树与属性一样要通配，且拼写是 `event_occured`（云端的原文如此）。

    解码那批用例各自把 topic 当参数传进去，所以订阅端把这段拼错时它们全绿 ——
    只有这条能从订阅走到回调。
    """
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)
    received: list = []
    try:
        await asyncio.gather(
            mips.sub_device_events_async("dev-9", handler=received.append),
            _ack_subscribes(fake, 1),
        )
        assert [topic for topic, _qos, _mid in fake.subscribed] == [
            "device/dev-9/up/event_occured/#"
        ]
        fake.fire_message(
            "device/dev-9/up/event_occured/2/1020",
            b'{"method":"event_occured","params":{"siid":2,"eiid":1020,'
            b'"arguments":[{"piid":1,"value":2}]}}',
        )
        await asyncio.sleep(0.01)

        assert (received[0].siid, received[0].eiid) == (2, 1020)
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_events_unsubscribe_the_same_wildcard_they_subscribed():
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)
    try:
        await asyncio.gather(
            mips.sub_device_events_async("dev-9", handler=lambda _msg: None),
            _ack_subscribes(fake, 1),
        )
        await mips.unsub_device_events_async("dev-9")

        assert set(fake.unsubscribed) == {
            topic for topic, _qos, _mid in fake.subscribed
        }
    finally:
        await mips.deinit_async()


@pytest.mark.asyncio
async def test_a_second_handler_on_the_same_topic_is_reported(caplog):
    """一个 topic 只挂一个 handler：第二个注册会静默替换掉第一个，
    两个消费方各订一份时先注册的那个从此收不到推送。"""
    mips, _ = _make_mips()
    holder: dict[str, _FakeMqttClient] = {}

    def factory(client_id: str) -> _FakeMqttClient:
        holder["c"] = _FakeMqttClient(client_id)
        return holder["c"]  # type: ignore[return-value]

    mips._client_factory = factory  # type: ignore[assignment]
    fake = await _connect(mips, holder)
    try:
        await asyncio.gather(
            mips.sub_device_props_async("dev-9", handler=lambda _msg: None),
            _ack_subscribes(fake, 1),
        )
        with caplog.at_level(logging.ERROR, logger="miot.mips_cloud"):
            await asyncio.gather(
                mips.sub_device_props_async("dev-9", handler=lambda _msg: "other"),
                _ack_subscribes(fake, 1),
            )

        assert any(
            "already has a different handler" in r.message for r in caplog.records
        )
    finally:
        await mips.deinit_async()


def test_an_entry_without_a_value_is_skipped():
    """缺 value 不是「值为 None」，是一条残缺的推送 —— 当成 None 写进容器会把真实值
    盖掉，而容器没有时间戳可仲裁。与对齐侧同口径。"""
    event = _props_decoder()(
        "device/d1/up/properties_changed",
        b'{"method":"properties_changed","params":['
        b'{"siid":2,"piid":1},{"siid":3,"piid":4,"value":2}]}',
    )

    assert [(c.siid, c.piid, c.value) for c in event.changes] == [(3, 4, 2)]


def test_a_message_whose_only_entry_lacks_a_value_is_dropped():
    assert (
        _props_decoder()(
            "device/d1/up/properties_changed/2/1",
            b'{"method":"properties_changed","params":{"siid":2,"piid":1}}',
        )
        is None
    )


def test_an_explicit_null_value_is_kept():
    """显式 null 是设备真的报了空值，与缺键不是一回事。"""
    event = _props_decoder()(
        "device/d1/up/properties_changed/2/1",
        b'{"method":"properties_changed","params":{"siid":2,"piid":1,"value":null}}',
    )

    assert [(c.siid, c.piid, c.value) for c in event.changes] == [(2, 1, None)]


# ── 事件推送 ──────────────────────────────────────────────────────────


def _event_decoder():
    return MIoTMipsCloud._make_device_event_decoder()


def test_an_event_occurred_push_decodes_into_one_event():
    """参数带的是数字 piid，不是 spec 里的参数名 —— 名字由上层查 spec 补。"""
    event = _event_decoder()(
        "device/d1/up/event_occured/2/1020",
        b'{"method":"event_occured","params":{"did":"d1","siid":2,"eiid":1020,'
        b'"arguments":[{"piid":1,"value":2}]}}',
    )

    assert event is not None
    assert (event.did, event.siid, event.eiid) == ("d1", 2, 1020)
    assert [(a.piid, a.value) for a in event.args] == [(1, 2)]
    assert event.timestamp_ms > 0


def test_an_event_without_arguments_decodes_with_no_args():
    """spec 允许无参事件。**在这里丢掉的话它的到达就完全不可观测了** —— 帧是真的，
    只是没有可落的参数，交给上层计数。"""
    event = _event_decoder()(
        "device/d1/up/event_occured/2/1020",
        b'{"method":"event_occured","params":{"siid":2,"eiid":1020}}',
    )

    assert event is not None
    assert event.args == []


def test_an_argument_without_a_piid_is_skipped_but_the_event_survives(caplog):
    """缺 piid 的参数去不了任何地方 —— 叶子的键要按 piid 反查参数名。

    丢掉是对的（猜一个 piid 就是把值挂到别的参数上），但**不能静默丢**：没了告警，
    上位只看得见「参数少了一个」，而「设备没上报这个参数」与「上报了但认不出」在
    计数上长得一样。同一条 arguments 里缺失 value 的那条已经会告警，这里同口径。
    """
    with caplog.at_level(logging.WARNING, logger="miot.mips_cloud"):
        event = _event_decoder()(
            "device/d1/up/event_occured/2/1020",
            b'{"method":"event_occured","params":{"siid":2,"eiid":1020,'
            b'"arguments":[{"value":9},{"piid":2,"value":3}]}}',
        )

    assert event is not None
    assert [(a.piid, a.value) for a in event.args] == [(2, 3)]
    assert "carries no usable piid" in caplog.text
    assert "did=d1" in caplog.text and "iid=event.2.1020" in caplog.text


def test_a_non_object_argument_is_skipped_with_the_same_warning(caplog):
    """参数列表里混进非对象（null、字符串、数组）时也走同一条告警。

    这种形态本来就在同一条循环里被默默吃掉，下场与「缺 piid」逐字相同：值真丢了，而上位
    只看得见一个「无参事件」。*不加* type 闸才接得住 —— 加一道 `isinstance` 就等于把它从
    这条告警里漏出去。type= 用来把这种形态与「是个对象但 piid 不可用」分开。
    """
    with caplog.at_level(logging.WARNING, logger="miot.mips_cloud"):
        event = _event_decoder()(
            "device/d1/up/event_occured/2/1020",
            b'{"method":"event_occured","params":{"siid":2,"eiid":1020,'
            b'"arguments":[null,"x",{"piid":2,"value":3}]}}',
        )

    assert event is not None
    assert [(a.piid, a.value) for a in event.args] == [(2, 3)]
    assert caplog.text.count("carries no usable piid") == 2
    assert "type=NoneType" in caplog.text and "type=str" in caplog.text


def test_an_arguments_object_instead_of_a_list_is_not_silently_empty(caplog):
    """整个 arguments 给成对象时逐项取到的是键（字符串）—— 以前全被默默过滤成空。

    后果最坏的一种：帧照常返回、args 为空，看起来就是一个无参事件。这条与「列表里
    混进非对象」是两种形态，文案也分开：那一条说的是某个参数认不出，这一条说的是
    装参数的容器本身不对。
    """
    with caplog.at_level(logging.WARNING, logger="miot.mips_cloud"):
        event = _event_decoder()(
            "device/d1/up/event_occured/2/1020",
            b'{"method":"event_occured","params":{"siid":2,"eiid":1020,'
            b'"arguments":{"5":{"value":9}}}}',
        )

    assert event is not None
    assert event.args == []
    assert "arguments is not a list" in caplog.text
    assert "did=d1" in caplog.text and "iid=event.2.1020" in caplog.text
    assert "type=dict" in caplog.text


@pytest.mark.parametrize(
    ("raw", "type_name"),
    [(b"5", "int"), (b"true", "bool"), (b"0", "int"), (b'""', "str")],
    ids=["number", "bool", "zero", "empty-string"],
)
def test_arguments_that_is_not_a_list_never_escapes_the_decoder_warning(
    caplog, raw, type_name
):
    """arguments 的容器形态不对时，在这里说清楚，不留给上层那句通用失败。

    实际后果比「丢参数」更重：不可迭代的值在循环头抛 TypeError，异常越过本函数，被
    `_on_message` 的通用解码失败兜住，整帧被丢掉，日志里只剩 topic 和一句 TypeError
    —— 没有 did、没有 iid，也没说是参数容器的问题。0 与空串这两种假值单列，是因为
    判据写成 `or []` 时它们会静默变成「无参事件」，而那正是本段要拦的形态。
    """
    with caplog.at_level(logging.WARNING, logger="miot.mips_cloud"):
        event = _event_decoder()(
            "device/d1/up/event_occured/2/1020",
            b'{"method":"event_occured","params":{"siid":2,"eiid":1020,'
            b'"arguments":' + raw + b"}}",
        )

    assert event is not None, raw
    assert event.args == []
    assert "arguments is not a list" in caplog.text
    assert "did=d1" in caplog.text and "iid=event.2.1020" in caplog.text
    assert f"type={type_name}" in caplog.text


def test_absent_or_null_arguments_is_still_a_quiet_no_arg_event():
    """容器闸只认「不是 None 又不是列表」：字段缺失与显式 null 都是合法的无参事件。

    这道闸一旦写成「必须得有列表」，无参事件就会开始刷告警 —— spec 允许它，真机上
    这类事件不少。
    """
    for raw in (b"null", None):
        payload = (
            b'{"method":"event_occured","params":{"siid":2,"eiid":1020}}'
            if raw is None
            else b'{"method":"event_occured","params":{"siid":2,"eiid":1020,'
            b'"arguments":null}}'
        )
        event = _event_decoder()("device/d1/up/event_occured/2/1020", payload)
        assert event is not None
        assert event.args == []


def test_an_argument_without_a_value_is_skipped_but_the_event_survives():
    """与属性侧同口径：缺 value 是残缺的推送，不是「值为 None」。"""
    event = _event_decoder()(
        "device/d1/up/event_occured/2/1020",
        b'{"method":"event_occured","params":{"siid":2,"eiid":1020,'
        b'"arguments":[{"piid":1},{"piid":2,"value":3}]}}',
    )

    assert [(a.piid, a.value) for a in event.args] == [(2, 3)]


def test_a_topic_outside_the_event_occurred_subtree_is_dropped():
    """订的是通配，别的 leaf 也会落进来 —— 这道闸是白名单，不是防御性冗余。"""
    assert (
        _event_decoder()(
            "device/d1/up/properties_changed/2/1020",
            b'{"method":"event_occured","params":{"siid":2,"eiid":1020}}',
        )
        is None
    )


def test_an_event_topic_carrying_a_properties_changed_payload_is_dropped():
    assert (
        _event_decoder()(
            "device/d1/up/event_occured/2/1020",
            b'{"method":"properties_changed","params":{"siid":2,"eiid":1020}}',
        )
        is None
    )


def test_a_params_list_with_one_entry_decodes():
    """broker 没文档化 params 形态：实测是裸 dict，HA 的惯例是 list，两种都收。"""
    event = _event_decoder()(
        "device/d1/up/event_occured/2/1020",
        b'{"method":"event_occured","params":[{"siid":2,"eiid":1020,'
        b'"arguments":[{"piid":1,"value":2}]}]}',
    )

    assert [(a.piid, a.value) for a in event.args] == [(1, 2)]


def test_a_params_list_with_several_entries_is_dropped():
    """与属性侧的形制差异：一次事件只承载一个 (siid, eiid) —— 那是 spec 里事件的键。

    多条时无从对应 topic 那一对，猜一条写下去会把一个事件的值挂到另一个事件的叶子上。
    """
    assert (
        _event_decoder()(
            "device/d1/up/event_occured/2/1020",
            b'{"method":"event_occured","params":['
            b'{"siid":2,"eiid":1020},{"siid":3,"eiid":1}]}',
        )
        is None
    )


def test_an_event_payload_did_that_contradicts_the_topic_is_dropped():
    """不一致会把一台设备的事件写到另一台的路径上。"""
    assert (
        _event_decoder()(
            "device/d1/up/event_occured/2/1020",
            b'{"method":"event_occured","params":{"did":"d2","siid":2,"eiid":1020}}',
        )
        is None
    )


def test_a_payload_iid_that_contradicts_the_topic_is_dropped():
    """事件帧的 topic 那一对永远对得上（一次只有一个事件），对不上就是真矛盾。

    属性侧只在单条时才敢比 —— 那条限制不适用于这里。
    """
    assert (
        _event_decoder()(
            "device/d1/up/event_occured/2/1020",
            b'{"method":"event_occured","params":{"siid":9,"eiid":9}}',
        )
        is None
    )


def test_an_event_topic_without_the_iid_suffix_decodes():
    """订阅通配是 `event_occured/#`，对齐不上时 topic 可能只到方法名那一段。"""
    event = _event_decoder()(
        "device/d1/up/event_occured",
        b'{"method":"event_occured","params":{"siid":2,"eiid":1020}}',
    )

    assert event is not None
    assert (event.siid, event.eiid) == (2, 1020)


def test_a_bridged_child_did_decodes():
    """网关子设备的 did 形如 `blt.x` / `proxy.x` —— topic 里的 did 段不能按数字收。"""
    event = _event_decoder()(
        "device/blt.4.1/up/event_occured/2/1020",
        b'{"method":"event_occured","params":{"siid":2,"eiid":1020}}',
    )

    assert event is not None
    assert event.did == "blt.4.1"


def test_a_non_json_event_payload_is_dropped():
    assert _event_decoder()("device/d1/up/event_occured/2/1020", b"not json") is None
