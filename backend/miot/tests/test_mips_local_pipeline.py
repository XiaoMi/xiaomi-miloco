# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Integration tests for `miot.mips_local.MipsLocalClient`.

A fake paho Client is injected via the `client_factory` hook — no real broker
or mTLS. The fake records subscribe/publish/unsubscribe and exposes `fire_*`
helpers to drive paho callbacks. Covers:

  - CONNACK success → base topics subscribed; CONNACK reject → MipsConnectionError
  - RPC request/reply matched by mid (get_prop / getDevList parse)
  - RPC timeout → CODE_TIMEOUT result, request map cleaned
  - reconnect re-subscribes registered broadcasts
  - property / event push dispatched to handlers (bad payloads dropped)
  - deinit fails in-flight requests instead of hanging
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from miot.error import MIoTErrorCode
from miot.mips_local import MipsLocalClient, _MipsMessage
from miot.types import MipsConnectionError
from paho.mqtt.enums import MQTTErrorCode

_LOGGER = logging.getLogger(__name__)
_DID = "1899"


class _FakeReasonCode:
    def __init__(self, value: int) -> None:
        self.value = value


class _FakeMqtt:
    """Subset of paho Client used by MipsLocalClient."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.connect_host = None
        self.connect_port = None
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.published: list[tuple[str, bytes]] = []
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    # -- paho API used by mips_local
    def tls_set(self, **kw):
        pass

    def tls_insecure_set(self, v):
        pass

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def connect(self, host, port, keepalive, clean_start):
        self.connect_host = host
        self.connect_port = port

    def disconnect(self):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def subscribe(self, topic, qos):
        self.subscribed.append(topic)
        return MQTTErrorCode.MQTT_ERR_SUCCESS, 1

    def unsubscribe(self, topic):
        self.unsubscribed.append(topic)
        return MQTTErrorCode.MQTT_ERR_SUCCESS, 1

    def publish(self, topic, payload, qos):
        self.published.append((topic, payload))

    # -- test drivers
    def fire_connect(self, rc=0):
        self.on_connect(self, None, None, _FakeReasonCode(rc), None)

    def fire_disconnect(self, rc=0):
        self.on_disconnect(self, None, None, _FakeReasonCode(rc), None)

    def fire_message(self, topic, payload: bytes):
        class _Msg:
            def __init__(self, t, p):
                self.topic = t
                self.payload = p

        self.on_message(self, None, _Msg(topic, payload))


def _make(did=_DID):
    holder: dict = {}

    def factory(client_id):
        holder["c"] = _FakeMqtt(client_id)
        return holder["c"]

    client = MipsLocalClient(
        did=did,
        host="10.0.0.9",
        group_id="g1",
        ca_file="ca",
        cert_file="crt",
        key_file="key",
        client_factory=factory,
    )
    return client, holder


async def _connect(client, holder, rc=0):
    async def drive():
        for _ in range(50):
            if "c" in holder and holder["c"].connect_host is not None:
                break
            await asyncio.sleep(0.005)
        holder["c"].fire_connect(rc=rc)

    await asyncio.gather(client.init_async(), drive())
    return holder["c"]


async def _reply(client, fake, result_obj, *, timeout=1.0):
    """Wait for a pending request, then fire its reply on {did}/reply."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not client._request_map and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.005)
    assert client._request_map, "no pending request appeared"
    mid = int(next(iter(client._request_map.keys())))
    packed = _MipsMessage.pack(mid=mid, payload=json.dumps(result_obj))
    fake.fire_message(f"{_DID}/reply", packed)


# ============================================================================


@pytest.mark.asyncio
async def test_connack_success_subscribes_base_topics():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        assert client.is_connected
        assert f"{_DID}/#" in fake.subscribed
        assert "master/appMsg/devListChange" in fake.subscribed
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_connack_rejected_raises():
    client, holder = _make()
    with pytest.raises(MipsConnectionError):
        await _connect(client, holder, rc=0x87)  # Not authorized
    assert not client.is_connected


@pytest.mark.asyncio
async def test_get_prop_request_reply_by_mid():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.get_prop_async(_DID, 2, 4))
        await _reply(client, fake, {"value": True})
        assert await task == {"value": True, "code": 0}
        # published to master/proxy/get
        assert any(t == "master/proxy/get" for t, _ in fake.published)
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_get_dev_list_parses_fields():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.get_dev_list_async())
        await _reply(
            client,
            fake,
            {
                "devList": {
                    "d1": {
                        "online": True,
                        "specV2Access": True,
                        "pushAvailable": False,
                    }
                }
            },
        )
        devs = await task
        assert devs["d1"]["online"] is True
        assert devs["d1"]["specv2_access"] is True  # renamed from specV2Access
        assert devs["d1"]["push_available"] is False
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_rpc_timeout_returns_timeout_code_and_cleans_map():
    import miot.mips_local as ml

    orig = ml.MIPS_LOCAL_RPC_TIMEOUT
    ml.MIPS_LOCAL_RPC_TIMEOUT = 0.15
    client, holder = _make()
    await _connect(client, holder)
    try:
        r = await client.set_prop_async(_DID, 2, 4, True)  # no reply → timeout
        assert r["code"] == MIoTErrorCode.CODE_TIMEOUT.value
        assert client._request_map == {}  # timer popped it
    finally:
        ml.MIPS_LOCAL_RPC_TIMEOUT = orig
        await client.deinit_async()


@pytest.mark.asyncio
async def test_reconnect_resubscribes_broadcasts():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        client.sub_prop(did="d2", handler=lambda m, c: None)
        assert "master/appMsg/notify/iot/d2/property/#" in fake.subscribed
        before = len(fake.subscribed)
        fake.fire_disconnect(rc=1)
        fake.fire_connect(rc=0)  # paho auto-reconnect → on_connect again
        resubbed = fake.subscribed[before:]
        assert "master/appMsg/notify/iot/d2/property/#" in resubbed
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_prop_push_dispatched_and_bad_payload_dropped():
    client, holder = _make()
    fake = await _connect(client, holder)
    got: list[dict] = []
    try:
        client.sub_prop(did="d2", handler=lambda m, c: got.append(m))
        # valid push on {did}/appMsg/notify/iot/d2/property/2.4
        good = {"did": "d2", "siid": 2, "piid": 4, "value": False}
        fake.fire_message(
            f"{_DID}/appMsg/notify/iot/d2/property/2.4",
            _MipsMessage.pack(mid=1, payload=json.dumps(good)),
        )
        # missing required key → dropped
        fake.fire_message(
            f"{_DID}/appMsg/notify/iot/d2/property/2.4",
            _MipsMessage.pack(mid=2, payload=json.dumps({"did": "d2"})),
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert got == [good]
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_event_push_dispatched_with_arguments_default():
    client, holder = _make()
    fake = await _connect(client, holder)
    got: list[dict] = []
    try:
        client.sub_event(did="d2", handler=lambda m, c: got.append(m))
        ev = {"did": "d2", "siid": 8, "eiid": 10}
        fake.fire_message(
            f"{_DID}/appMsg/notify/iot/d2/event/8.10",
            _MipsMessage.pack(mid=1, payload=json.dumps(ev)),
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(got) == 1
        assert got[0]["eiid"] == 10
        assert got[0]["arguments"] == []  # defaulted
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_deinit_fails_inflight_request():
    import miot.mips_local as ml

    orig = ml.MIPS_LOCAL_RPC_TIMEOUT
    ml.MIPS_LOCAL_RPC_TIMEOUT = 30  # long, so deinit is what resolves it
    client, holder = _make()
    await _connect(client, holder)
    try:
        task = asyncio.create_task(client.action_async(_DID, 2, 1, []))
        # wait until the request is registered
        for _ in range(50):
            if client._request_map:
                break
            await asyncio.sleep(0.005)
        assert client._request_map
        await client.deinit_async()
        with pytest.raises(MipsConnectionError):
            await task
    finally:
        ml.MIPS_LOCAL_RPC_TIMEOUT = orig
