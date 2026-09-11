# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""属性推送的订阅契约：幂等、四种结局、重连重放、镜像重建。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miot.client import MIoTClient
from miot.types import MipsConnectionError


def _client(*, connected=True):
    client = object.__new__(MIoTClient)
    client._props_sub_dids = set()
    client._callback_device_props_changed = None
    client._mips_cloud = SimpleNamespace(
        is_connected=connected,
        sub_device_props_async=AsyncMock(),
        unsub_device_props_async=AsyncMock(),
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


async def test_the_reset_callback_hands_over_the_props_set():
    """镜像重建拿不到这个集合，上层就算不出该重订哪些设备。"""
    client = _client()
    client._props_sub_dids = {"d1"}
    client._meta_sub_dids = {"m1"}
    client._state_sub_dids = {"s1"}
    client._scene_sub_home_ids = {"h1"}
    seen: list = []
    client._callback_subscription_reset = lambda *sets: seen.append(sets)

    await client._fire_subscription_reset()

    assert seen == [({"m1"}, {"s1"}, {"h1"}, {"d1"})]
