# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""设备转上线后补齐容器里缺的属性。

启动时离线的设备只有在线标志、没有属性，而对齐一个作用域只跑一次 —— 不补的话，它在
整个作用域代内对规则和 agent 都是「没有可读属性」的样子。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.miot import mips_listeners
from miloco.miot.state_align import top_up_missing_props
from miloco.state import StateStore


@pytest.fixture
async def store():
    s = StateStore()
    s.start()
    yield s
    s.stop()


def _proxy(iids, rows):
    """iids = 这台设备的可读属性；rows = 云端会回的行。"""
    calls: list[list] = []

    async def get_readable_prop_iids(did):
        return list(iids)

    async def get_device_properties(params):
        calls.append(sorted((p.siid, p.piid) for p in params))
        wanted = {(p.did, p.siid, p.piid) for p in params}
        return [row for row in rows if (row["did"], row["siid"], row["piid"]) in wanted]

    return SimpleNamespace(
        get_readable_prop_iids=get_readable_prop_iids,
        get_device_properties=get_device_properties,
        calls=calls,
    )


def _row(did, siid, piid, value, code=0):
    return {"did": did, "siid": siid, "piid": piid, "value": value, "code": code}


async def _top_up(store, proxy, did, *, scope=0, current_scope=None):
    """代号默认恒定不变 —— 只有作用域那条用例关心它。"""
    return await top_up_missing_props(
        store,
        proxy,
        did,
        scope=scope,
        current_scope=current_scope or (lambda: scope),
    )


async def test_a_missing_property_is_filled_in(store):
    proxy = _proxy(["prop.2.1"], [_row("d1", 2, 1, 26)])

    _requested, written = await _top_up(store, proxy, "d1")

    assert written == 1
    assert store.get("iot/device/d1/prop/2.1") == 26


async def test_a_property_already_in_the_container_is_not_requested(store):
    """整台重拉会把补拉窗口里刚推来的新值用云端缓存值盖回去。"""
    store.set("iot/device/d1/prop/2.1", 99, source="iot_push")
    proxy = _proxy(["prop.2.1", "prop.3.1"], [_row("d1", 3, 1, True)])

    await _top_up(store, proxy, "d1")

    assert proxy.calls == [[(3, 1)]]
    assert store.get("iot/device/d1/prop/2.1") == 99


async def test_nothing_missing_means_no_cloud_call(store):
    store.set("iot/device/d1/prop/2.1", 26, source="iot_align")
    proxy = _proxy(["prop.2.1"], [])

    _requested, written = await _top_up(store, proxy, "d1")

    assert written == 0
    assert proxy.calls == []


async def test_the_top_up_does_not_wipe_the_devices_other_leaves(store):
    store.set("iot/device/d1/status/online", True, source="iot_align")
    store.set("iot/device/d1/prop/2.1", 26, source="iot_align")
    proxy = _proxy(["prop.2.1", "prop.3.1"], [_row("d1", 3, 1, False)])

    await _top_up(store, proxy, "d1")

    assert store.get("iot/device/d1/status/online") is True
    assert store.get("iot/device/d1/prop/2.1") == 26
    assert store.get("iot/device/d1/prop/3.1") is False


async def test_a_failed_row_is_skipped_without_killing_the_batch(store):
    proxy = _proxy(
        ["prop.2.1", "prop.3.1"],
        [_row("d1", 2, 1, None, code=-704220043), _row("d1", 3, 1, 7)],
    )

    _requested, written = await _top_up(store, proxy, "d1")

    assert written == 1
    assert store.get("iot/device/d1/prop/3.1") == 7


async def test_a_bridged_did_is_not_topped_up(store):
    """did 带 '/' 当不了路径段，连云端都不该打。"""
    proxy = _proxy(["prop.2.1"], [])

    _requested, written = await _top_up(store, proxy, "blt/1")

    assert written == 0
    assert proxy.calls == []


async def test_an_online_event_schedules_a_top_up(monkeypatch):
    monkeypatch.setattr(mips_listeners, "PROP_TOPUP_DEBOUNCE_SEC", 0.01)
    top_up = AsyncMock()
    listener = mips_listeners.PropTopUpListener(top_up=top_up)

    await listener.on_event(SimpleNamespace(did="d1", event="online"))
    await asyncio.sleep(0.05)

    top_up.assert_awaited_once_with("d1")
    listener.deinit()


async def test_an_offline_event_schedules_nothing(monkeypatch):
    """离线设备的属性拉不到 —— 对齐当初跳过它就是这个原因。

    观测点必须是「只发 offline」：先 offline 再 online 会被防抖折叠成一次，
    「一律排」和「只排上线」给出同样的调用次数。
    """
    monkeypatch.setattr(mips_listeners, "PROP_TOPUP_DEBOUNCE_SEC", 0.01)
    top_up = AsyncMock()
    listener = mips_listeners.PropTopUpListener(top_up=top_up)

    await listener.on_event(SimpleNamespace(did="d1", event="offline"))
    await asyncio.sleep(0.05)

    top_up.assert_not_awaited()
    listener.deinit()


async def test_a_flapping_device_is_topped_up_once(monkeypatch):
    monkeypatch.setattr(mips_listeners, "PROP_TOPUP_DEBOUNCE_SEC", 0.05)
    top_up = AsyncMock()
    listener = mips_listeners.PropTopUpListener(top_up=top_up)

    for _ in range(3):
        await listener.on_event(SimpleNamespace(did="d1", event="online"))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.15)

    top_up.assert_awaited_once_with("d1")
    listener.deinit()


def _wired_proxy(store, iids, rows):
    """一台裸 proxy，挂上写入器和补拉两个消费方 —— 与 Manager 的接线同形。"""
    return _wired_proxy_from(store, _proxy(iids, rows))


def _wired_proxy_from(store, fake, *, home=("d1",), online=True):
    """同上，但用调用方给的假 proxy（要看云端被打了几次的用例需要拿着它）。"""
    from miloco.manager import Manager
    from miloco.miot.client import MiotProxy

    proxy = object.__new__(MiotProxy)
    proxy._state_listeners = []
    proxy._props_listeners = []
    proxy._camera_info_dict = {}
    proxy._device_info_dict = {}
    proxy._cameras_loaded = True
    proxy._camera_state_listener = SimpleNamespace(on_event=AsyncMock())

    proxy.get_readable_prop_iids = fake.get_readable_prop_iids
    proxy.get_device_properties = fake.get_device_properties
    proxy.calls = fake.calls

    async def devices_in_current_home():
        return {did: SimpleNamespace(home_id="H1", online=online) for did in home}

    proxy.devices_in_current_home = devices_in_current_home

    manager = object.__new__(Manager)
    manager._state_store = store
    manager._miot_proxy = proxy
    manager._scope = 0
    manager._aligned_scope = 0
    manager._top_up_attempts = {}
    manager._wire_iot_push()
    return proxy, manager


async def test_an_online_event_writes_the_flag_and_tops_up(store, monkeypatch):
    """接线漏了补拉的后果：启动时离线的设备整代没有属性，而在线标志看着是正常的。"""
    monkeypatch.setattr(mips_listeners, "PROP_TOPUP_DEBOUNCE_SEC", 0.01)
    proxy, manager = _wired_proxy(store, ["prop.2.1"], [_row("d1", 2, 1, 26)])

    await proxy._on_device_state_changed_event(
        SimpleNamespace(did="d1", event="online")
    )
    await asyncio.sleep(0.05)

    assert store.get("iot/device/d1/status/online") is True
    assert store.get("iot/device/d1/prop/2.1") == 26
    manager._prop_top_up.deinit()


async def test_an_offline_event_writes_the_flag_and_tops_up_nothing(store, monkeypatch):
    monkeypatch.setattr(mips_listeners, "PROP_TOPUP_DEBOUNCE_SEC", 0.01)
    proxy, manager = _wired_proxy(store, ["prop.2.1"], [_row("d1", 2, 1, 26)])

    await proxy._on_device_state_changed_event(
        SimpleNamespace(did="d1", event="offline")
    )
    await asyncio.sleep(0.05)

    assert store.get("iot/device/d1/status/online") is False
    assert proxy.calls == []
    manager._prop_top_up.deinit()


async def test_the_top_up_budget_runs_out_within_one_scope(store):
    """永久不可读的属性永远不进容器，所以「容器里没有」会让反复掉线的设备每次上线都
    重新请求同一批读不到的属性 —— 额度是挡这个的。"""
    from miloco.manager import TOP_UP_MAX_ATTEMPTS

    proxy = _proxy(["prop.2.1"], [])  # 云端一条都不回，叶子永远补不上
    _, manager = _wired_proxy_from(store, proxy)

    for _ in range(TOP_UP_MAX_ATTEMPTS + 2):
        await manager._top_up_props("d1")

    assert len(proxy.calls) == TOP_UP_MAX_ATTEMPTS


async def test_switching_the_scope_restores_the_budget(store):
    """切家庭要重新对齐，那一代的补拉额度也该重新算。"""
    from miloco.manager import TOP_UP_MAX_ATTEMPTS

    proxy = _proxy(["prop.2.1"], [])
    _, manager = _wired_proxy_from(store, proxy)
    for _ in range(TOP_UP_MAX_ATTEMPTS):
        await manager._top_up_props("d1")

    manager.begin_scope_switch()
    await manager._top_up_props("d1")

    assert len(proxy.calls) == TOP_UP_MAX_ATTEMPTS + 1


async def test_the_budget_is_per_device(store):
    """一台设备用完额度不该拖累另一台。"""
    from miloco.manager import TOP_UP_MAX_ATTEMPTS

    proxy = _proxy(["prop.2.1"], [])
    _, manager = _wired_proxy_from(store, proxy, home=("d1", "d2"))
    for _ in range(TOP_UP_MAX_ATTEMPTS):
        await manager._top_up_props("d1")

    await manager._top_up_props("d2")

    assert len(proxy.calls) == TOP_UP_MAX_ATTEMPTS + 1


async def test_a_device_outside_the_current_home_is_not_topped_up(store):
    """上下线订阅是账号全量的，别人家的设备一样会推上线事件过来 —— 补拉不判家庭，
    它的属性就会被写进喂给规则和 agent 的那份数据源。"""
    proxy = _proxy(["prop.2.1"], [_row("d-other", 2, 1, 26)])
    _, manager = _wired_proxy_from(store, proxy, home=("d1",))

    await manager._top_up_props("d-other")

    assert proxy.calls == []
    assert store.snapshot("iot/**") == {}


async def test_a_device_that_went_offline_again_is_not_topped_up(store):
    """离线设备云端给的是缓存里的最后一次上报、可能任意旧 —— 对齐当初跳过它就是这个
    理由，补拉不该在防抖窗口里又掉线之后照拉。"""
    proxy = _proxy(["prop.2.1"], [_row("d1", 2, 1, 26)])
    _, manager = _wired_proxy_from(store, proxy, online=False)

    await manager._top_up_props("d1")

    assert proxy.calls == []


async def test_a_scope_switch_during_the_cloud_round_trip_discards_the_result(store):
    """补拉要打一趟云端，回来时可能已经切了家庭 —— 写进刚清空的树就是幽灵设备。"""
    scope = {"now": 0}
    proxy = _proxy(["prop.2.1"], [_row("d1", 2, 1, 26)])
    original = proxy.get_device_properties

    async def switch_then_return(params):
        scope["now"] += 1
        return await original(params)

    proxy.get_device_properties = switch_then_return

    requested, written = await _top_up(
        store, proxy, "d1", scope=0, current_scope=lambda: scope["now"]
    )

    assert (requested, written) == (1, 0)
    assert store.snapshot("iot/**") == {}


async def test_a_run_that_asks_the_cloud_nothing_does_not_spend_budget(store):
    """空跑消耗额度会把「留几次给瞬时不可读」这个用意直接吃掉。"""
    from miloco.manager import TOP_UP_MAX_ATTEMPTS

    store.set("iot/device/d1/prop/2.1", 26, source="iot_align")
    proxy = _proxy(["prop.2.1"], [])
    _, manager = _wired_proxy_from(store, proxy)

    for _ in range(TOP_UP_MAX_ATTEMPTS + 2):
        await manager._top_up_props("d1")

    assert manager._top_up_attempts == {}


async def test_the_shutdown_cancels_the_pending_top_up(store, monkeypatch):
    """不取消的话，关闭期间计时器到点会对着拆掉一半的 proxy 打云端。"""
    monkeypatch.setattr(mips_listeners, "PROP_TOPUP_DEBOUNCE_SEC", 0.05)
    proxy = _proxy(["prop.2.1"], [_row("d1", 2, 1, 26)])
    _, manager = _wired_proxy_from(store, proxy)
    await manager._prop_top_up.on_event(SimpleNamespace(did="d1", event="online"))

    manager.deinit_iot_push()
    await asyncio.sleep(0.15)

    assert proxy.calls == []
