# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""推送写容器：两道闸、值形态、路径形状。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from miloco.miot.state_push import SOURCE, IotPushWriter, write_online
from miloco.state import MISSING, StateStore


@pytest.fixture
async def store():
    s = StateStore()
    s.start()
    yield s
    s.stop()


def _writer(store, *, home=("d1",), scope=0, aligned=True):
    """proxy 只提供 devices_in_current_home；代号与对齐由两个可调对象给。"""

    async def devices_in_current_home():
        return {did: SimpleNamespace(home_id="H1") for did in home}

    return IotPushWriter(
        store,
        SimpleNamespace(devices_in_current_home=devices_in_current_home),
        current_scope=lambda: scope,
        scope_is_aligned=lambda: aligned,
    )


def _props(did="d1", changes=((2, 1, 26),)):
    return SimpleNamespace(
        did=did,
        changes=[SimpleNamespace(siid=s, piid=p, value=v) for s, p, v in changes],
    )


async def _settle(rounds: int = 200) -> None:
    """投递是异步的，等事件循环转够圈数。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


def _state(did="d1", event="online"):
    return SimpleNamespace(did=did, event=event)


async def test_a_property_push_lands_on_the_leaf(store):
    await _writer(store).on_device_props(_props())

    assert store.get("iot/device/d1/prop/2.1") == 26
    assert store.get_entry("iot/device/d1/prop/2.1").source == SOURCE


async def test_a_property_push_does_not_wipe_its_siblings(store):
    store.set("iot/device/d1/prop/3.1", True, source="iot_align")

    await _writer(store).on_device_props(_props())

    assert store.get("iot/device/d1/prop/3.1") is True


async def test_a_property_push_before_the_alignment_is_dropped(store):
    """对齐拿的是云端缓存值，推送先落地会被随后的对齐用旧值静默盖掉。"""
    await _writer(store, aligned=False).on_device_props(_props())

    assert store.get("iot/device/d1/prop/2.1") is MISSING


async def test_a_property_push_for_a_device_outside_the_home_is_dropped(store):
    await _writer(store, home=()).on_device_props(_props())

    assert store.get("iot/device/d1/prop/2.1") is MISSING


async def test_a_dict_value_is_refused_instead_of_becoming_a_subtree(store):
    """容器对 dict 不抛错，它会把叶子翻成子树，订阅旧形状的消费方从此收不到。"""
    await _writer(store).on_device_props(_props(changes=((2, 1, {"a": 1}),)))

    assert store.get("iot/device/d1/prop/2.1") is MISSING
    assert store.get("iot/device/d1/prop/2.1/a") is MISSING


async def test_a_scalar_list_is_written_as_is(store):
    await _writer(store).on_device_props(_props(changes=((2, 1, [1, 2]),)))

    assert store.get("iot/device/d1/prop/2.1") == (1, 2)


async def test_one_bad_entry_does_not_drop_the_rest_of_the_batch(store):
    await _writer(store).on_device_props(_props(changes=((2, 1, {"a": 1}), (2, 2, 7))))

    assert store.get("iot/device/d1/prop/2.2") == 7


async def test_an_online_push_lands_without_waiting_for_the_alignment(store):
    """online 的容器值与设备缓存同源，早写晚写都一样，不该被对齐门挡住。"""
    await _writer(store, aligned=False).on_device_state(_state())

    assert store.get("iot/device/d1/status/online") is True


async def test_an_offline_push_flips_the_flag(store):
    store.set("iot/device/d1/status/online", True, source="iot_align")

    await _writer(store).on_device_state(_state(event="offline"))

    assert store.get("iot/device/d1/status/online") is False


async def test_an_online_push_for_a_device_outside_the_home_is_dropped(store):
    await _writer(store, home=()).on_device_state(_state())

    assert store.get("iot/device/d1/status/online") is MISSING


async def test_a_bridged_did_never_reaches_the_container(store):
    """did 带 '/' 会把路径劈成两段，值落到别的设备下面。"""
    await _writer(store, home=("blt/1",)).on_device_props(_props(did="blt/1"))

    assert store.snapshot("iot/**") == {}


async def test_rewriting_the_same_online_value_delivers_no_event(store):
    """每次刷新设备都重写在线标志（见 refresh_devices），同值不产事件才敢这么写。

    观测点必须是投递而不是 `last_changed`：两次写落在同一毫秒里，时间戳分不开对错。
    """
    seen: list = []
    store.subscribe("iot/**", seen.append)
    write_online(store, "d1", True)
    await _settle()
    seen.clear()

    write_online(store, "d1", True)
    await _settle()

    assert seen == []


async def test_rewriting_a_changed_online_value_delivers_an_event(store):
    """上一条的反面 —— 少了它，「一律不投递」也能让上一条绿。"""
    seen: list = []
    store.subscribe("iot/**", seen.append)
    write_online(store, "d1", True)
    await _settle()
    seen.clear()

    write_online(store, "d1", False)
    await _settle()

    assert [change.new for change in seen] == [False]


async def test_the_proxy_hands_state_events_to_every_listener():
    """一个 listener 抛异常不该让另一个收不到 —— 缓存更新和写容器互不背锅。"""
    from unittest.mock import AsyncMock

    from miloco.miot.client import MiotProxy

    proxy = object.__new__(MiotProxy)
    proxy._camera_info_dict = {}
    proxy._device_info_dict = {}
    proxy._cameras_loaded = True
    proxy._camera_state_listener = SimpleNamespace(on_event=AsyncMock())
    proxy._state_listeners = []

    boom = AsyncMock(side_effect=RuntimeError("boom"))
    ok = AsyncMock()
    proxy.add_device_state_listener(boom)
    proxy.add_device_state_listener(ok)

    await proxy._on_device_state_changed_event(_state())

    boom.assert_awaited_once()
    ok.assert_awaited_once()


async def test_the_manager_wires_both_push_lanes_into_the_container(store):
    """漏挂一条 listener 的后果是推送静默不进容器，生产里看不出来。"""
    from miloco.manager import Manager
    from miloco.miot.client import MiotProxy

    proxy = object.__new__(MiotProxy)
    proxy._state_listeners = []
    proxy._props_listeners = []

    async def devices_in_current_home():
        return {"d1": SimpleNamespace(home_id="H1")}

    proxy.devices_in_current_home = devices_in_current_home

    manager = object.__new__(Manager)
    manager._state_store = store
    manager._miot_proxy = proxy
    manager._scope = 0
    manager._aligned_scope = 0

    manager._wire_iot_push()

    for callback in proxy._props_listeners:
        await callback(_props())
    for callback in proxy._state_listeners:
        await callback(_state())

    assert store.get("iot/device/d1/prop/2.1") == 26
    assert store.get("iot/device/d1/status/online") is True


async def test_a_push_travels_from_the_sdk_callback_into_the_container(store):
    """每段单测都过不代表接上了：这条从 proxy 注册给 SDK 的那个回调开始走。"""
    from miloco.manager import Manager
    from miloco.miot.client import MiotProxy
    from miot.types import MIoTDevicePropsEvent, MIoTPropChange

    proxy = object.__new__(MiotProxy)
    proxy._state_listeners = []
    proxy._props_listeners = []

    async def devices_in_current_home():
        return {"d1": SimpleNamespace(home_id="H1")}

    proxy.devices_in_current_home = devices_in_current_home

    manager = object.__new__(Manager)
    manager._state_store = store
    manager._miot_proxy = proxy
    manager._scope = 0
    manager._aligned_scope = 0
    manager._wire_iot_push()

    await proxy._on_device_props_changed_event(
        MIoTDevicePropsEvent(
            did="d1",
            changes=[MIoTPropChange(siid=2, piid=1, value=26)],
            timestamp_ms=1,
        )
    )

    assert store.get("iot/device/d1/prop/2.1") == 26
    manager._prop_top_up.deinit()
