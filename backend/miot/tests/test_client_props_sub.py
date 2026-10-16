# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""属性推送的订阅契约：幂等、四种结局、重连重放、镜像重建。

事件推送与属性推送共用同一套契约（逐条同形），所以两族用例在这个文件里并排 ——
事件那条通道的订阅多一层 `event_occured` 的通配，契约本身不该各写一份。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miot.client import MIoTClient
from miot.types import MipsConnectionError

# 无真实网络/凭据/设备依赖 —— CI 的 `miot 单元测试` 步骤按 `-m unit` 选择。
# 这个文件此前漏了这个标记，于是它的用例在 CI 里一条都不跑（同族的 meta 那份是有的）。
pytestmark = pytest.mark.unit


def _client(*, connected=True):
    client = object.__new__(MIoTClient)
    client._props_sub_dids = set()
    client._props_sub_sources = {}
    client._props_source_lock = asyncio.Lock()
    # 无中枢 → 一律云端，这一组用例只钉「不换通道」那半边。
    # 来源选择本身见 test_client_props_source.py。
    client._central_hub = None
    client._callback_device_props_changed = None
    client._event_sub_dids = set()
    client._callback_device_event = None
    client._mips_cloud = SimpleNamespace(
        is_connected=connected,
        sub_device_props_async=AsyncMock(),
        unsub_device_props_async=AsyncMock(),
        sub_device_events_async=AsyncMock(),
        unsub_device_events_async=AsyncMock(),
    )
    return client


async def test_a_subscribe_records_the_did():
    client = _client()

    await client.sub_device_props_async("d1")

    assert client._props_sub_dids == {"d1"}
    client._mips_cloud.sub_device_props_async.assert_awaited_once()


async def test_a_second_subscribe_is_a_no_op():
    client = _client()
    await client.sub_device_props_async("d1")

    await client.sub_device_props_async("d1")

    assert client._mips_cloud.sub_device_props_async.await_count == 1


async def test_subscribing_without_a_connection_raises_and_records_nothing():
    """记进集合的前提是真订上了：记早了，下一轮 diff 会认为它已经有了。"""
    client = _client(connected=False)

    with pytest.raises(MipsConnectionError):
        await client.sub_device_props_async("d1")

    assert client._props_sub_dids == set()


async def test_a_failed_subscribe_records_nothing():
    client = _client()
    client._mips_cloud.sub_device_props_async = AsyncMock(
        side_effect=RuntimeError("suback rejected")
    )

    with pytest.raises(RuntimeError):
        await client.sub_device_props_async("d1")

    assert client._props_sub_dids == set()


async def test_an_unsubscribe_forgets_the_did_even_if_mips_is_gone():
    client = _client()
    await client.sub_device_props_async("d1")
    client._mips_cloud = None

    await client.unsub_device_props_async("d1")

    assert client._props_sub_dids == set()


async def test_the_registered_callback_receives_decoded_events():
    client = _client()
    seen: list = []
    client.register_device_props_changed_callback(seen.append)

    client._on_device_props_msg("event")

    assert seen == ["event"]


async def test_no_callback_registered_is_not_an_error():
    client = _client()

    client._on_device_props_msg("event")


async def test_the_reset_callback_hands_over_every_tracked_set():
    """镜像重建拿不到这些集合，上层就算不出该重订哪些设备。

    事件那一路断了不会有人来补（云端没有再读一次事件的接口），所以它必须和属性那条
    一起交出去 —— 少一个的后果是重连后那条命名空间静默停在旧值上。
    """
    client = _client()
    client._props_sub_dids = {"d1"}
    client._meta_sub_dids = {"m1"}
    client._state_sub_dids = {"s1"}
    client._scene_sub_home_ids = {"h1"}
    client._event_sub_dids = {"e1"}
    seen: list = []
    client._callback_subscription_reset = lambda *sets: seen.append(sets)

    await client._fire_subscription_reset()

    assert seen == [({"m1"}, {"s1"}, {"h1"}, {"d1"}, {"e1"})]


# ── 事件推送：与属性同一条契约 ────────────────────────────────────────


async def test_an_event_subscribe_records_the_did():
    client = _client()

    await client.sub_device_events_async("d1")

    assert client._event_sub_dids == {"d1"}
    client._mips_cloud.sub_device_events_async.assert_awaited_once()


async def test_a_second_event_subscribe_is_a_no_op():
    client = _client()
    await client.sub_device_events_async("d1")

    await client.sub_device_events_async("d1")

    assert client._mips_cloud.sub_device_events_async.await_count == 1


async def test_an_event_subscribe_without_a_connection_raises_and_records_nothing():
    """记进集合的前提是真订上了：记早了，下一轮 diff 会认为它已经有了。"""
    client = _client(connected=False)

    with pytest.raises(MipsConnectionError):
        await client.sub_device_events_async("d1")

    assert client._event_sub_dids == set()


async def test_a_failed_event_subscribe_records_nothing():
    client = _client()
    client._mips_cloud.sub_device_events_async = AsyncMock(
        side_effect=RuntimeError("suback rejected")
    )

    with pytest.raises(RuntimeError):
        await client.sub_device_events_async("d1")

    assert client._event_sub_dids == set()


async def test_an_event_unsubscribe_forgets_the_did_even_if_mips_is_gone():
    client = _client()
    await client.sub_device_events_async("d1")
    client._mips_cloud = None

    await client.unsub_device_events_async("d1")

    assert client._event_sub_dids == set()


def test_the_registered_event_callback_receives_decoded_events():
    client = _client()
    seen: list = []
    client.register_device_event_callback(seen.append)

    client._on_device_event_msg("event")

    assert seen == ["event"]


async def test_an_event_callback_that_is_a_coroutine_function_is_scheduled():
    """注册进来的那个回调是 ``async def`` —— miloco 侧注册的正是。

    只写 ``cb(msg)`` 不调度的话，返回的协程当场被丢掉：事件到不了容器，现场只剩一条
    RuntimeWarning，而「事件怎么没进来」从计数上完全看不出来。所以这两行不是防御性
    冗余，它是这条通道在生产里唯一的走法。
    """
    client = _client()
    seen: list = []

    async def cb(msg):
        seen.append(msg)

    client.register_device_event_callback(cb)

    client._on_device_event_msg("event")
    assert seen == []  # 调度出去，不是就地在调用点跑完

    await asyncio.sleep(0)
    assert seen == ["event"]


def test_no_event_callback_registered_is_not_an_error():
    client = _client()

    client._on_device_event_msg("event")
