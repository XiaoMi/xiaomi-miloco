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
  - deinit resolves in-flight requests as result-unknown instead of hanging
  - publish rc != SUCCESS → MipsConnectionError (definitely-not-sent)
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


class _FakePubInfo:
    def __init__(self, rc) -> None:
        self.rc = rc


class _FakeMqtt:
    """Subset of paho Client used by MipsLocalClient."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.publish_rc = MQTTErrorCode.MQTT_ERR_SUCCESS
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
        # 真 paho 返回 MQTTMessageInfo,mips_local 现在会检查 .rc(未连接时 paho 不抛
        # 异常只把 rc 置成 MQTT_ERR_NO_CONN,不查就会把"没发出去"当成超时)。
        return _FakePubInfo(self.publish_rc)

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
async def test_deinit_resolves_inflight_request_as_result_unknown():
    """deinit 打断在途请求 → 收尾成 **-10006(结果未知)**,不是抛异常。

    future 只在 publish 成功之后才进 _request_map,所以"在途"必然意味着报文已经发给
    网关。路由层把异常一律当"明确未执行"转云端重发,若这里抛 MipsConnectionError,
    「连接被替换/拆除」(切家庭、切号、20s 重连 sweep 撞上 ≤5s 应答窗口)就会让非幂等
    动作走两遍 —— 正是本 PR 的核心承诺要消灭的形态。
    """
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
        res = await task
        assert res["code"] == MIoTErrorCode.CODE_TIMEOUT.value
    finally:
        ml.MIPS_LOCAL_RPC_TIMEOUT = orig


@pytest.mark.asyncio
async def test_publish_failure_raises_connection_error():
    """publish 的 rc 非 SUCCESS = 报文没进发送队列 = **明确没发出去** → 抛异常。

    不查 rc 的话调用方会干等满超时拿 -10006,而 -10006 被上层按"可能已送达"处置
    (set/action 不回落云端 + 冷却)——一条根本没出本机的指令被当成结果未知,
    用户操作无声丢失。抛异常才让路由层走"安全重发云端"。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    fake.publish_rc = MQTTErrorCode.MQTT_ERR_NO_CONN
    with pytest.raises(MipsConnectionError):
        await client.action_async(_DID, 2, 1, [])
    # 失败后不能留下在途条目/定时器,否则下一轮 deinit 会给它编一个假的"结果未知"。
    assert client._request_map == {}
    await client.deinit_async()


@pytest.mark.asyncio
async def test_unrecognised_reply_shape_returns_result_unknown():
    """网关回了包但结构不认识 → -10041(结果未知),**不是** -10004。

    走真实回包路径而非 fake 网关:上层 client.py 靠这个码决定"不重发云端",
    如果这里退回 -10004,双发防护就失效了(-10004 语义是"两个通道都没结果")。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        # 既没有 result 也没有 error,且不带 code
        await _reply(client, fake, {"unexpected": "shape"})
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_reply_carrying_code_is_passed_through():
    """回包自带 code 时原样上抛,不被重新贴成 -10004/-10041。

    这是 -10040(应答非合法 JSON,由 __request_async 自产)能真正到达 client.py
    路由表的前提 —— 此前它在这里被覆写,导致路由表里那条分支永不命中。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        await _reply(
            client, fake,
            {"code": MIoTErrorCode.CODE_MIPS_INVALID_RESULT.value, "message": "x"},
        )
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_INVALID_RESULT.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_action_unrecognised_reply_shape_returns_result_unknown():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.action_async(_DID, 2, 1, []))
        await _reply(client, fake, {"unexpected": "shape"})
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


# ─── 相邻的畸形回包形状:result 类型错/error 类型错/payload 非对象 ────────────
#
# 上面两条只覆盖了「是 dict 但既没有 result 也没有 error 键」这一种。相邻的几种
# 同样"网关已回包但结构不认识"的形状此前会抛异常(KeyError/TypeError)或退化成
# 非 dict,两条路都被 client.py 判成"肯定没执行"而云端重发 —— 同一条非幂等指令
# 在设备上执行两次,正是这整套分流机制要防的事。


@pytest.mark.asyncio
async def test_set_prop_result_not_a_list_returns_result_unknown():
    """result 是对象而非单元素数组:旧代码在下标访问 result[0] 处抛 KeyError。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        await _reply(client, fake, {"result": {"code": 0}})
        r = await task  # 不能抛异常
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_set_prop_error_not_a_dict_returns_result_unknown():
    """error 是字符串而非对象:旧代码原样返回字符串,上层 isinstance 判 code=None
    →"肯定没执行"→ 云端重发,且整条链路不会打一行异常日志。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        await _reply(client, fake, {"error": "denied"})
        r = await task
        assert isinstance(r, dict)  # 不能是裸字符串
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_set_prop_non_object_payload_returns_result_unknown():
    """payload 是合法 JSON 但非对象(如裸整数):旧代码在 "result" in 5 处抛
    TypeError。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        await _reply(client, fake, 5)
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_action_result_not_a_dict_returns_result_unknown():
    """action 的 result 是非 dict(如裸整数):旧代码在 "code" in result_obj["result"]
    处抛 TypeError。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.action_async(_DID, 2, 1, []))
        await _reply(client, fake, {"result": 5})
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_action_error_not_a_dict_returns_result_unknown():
    """action 的 error 是字符串而非对象,与 set_prop 同理。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.action_async(_DID, 2, 1, []))
        await _reply(client, fake, {"error": "denied"})
        r = await task
        assert isinstance(r, dict)
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()
