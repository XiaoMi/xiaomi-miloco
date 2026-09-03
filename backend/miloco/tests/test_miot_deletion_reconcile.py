# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""刷新设备时删掉容器里已不属于当前家庭的设备。

设备换家、单台解绑、设备被删，三种情况同一个形态：订阅侧的 diff 会自然收敛，但容器
里那台设备的叶子没人删，会留下一台永远不更新也不消失的幽灵设备。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.miot.client import MiotProxy
from miloco.state import MISSING, StateStore

HOME = "H1"


class _FakeKV:
    def __init__(self, homes: list[str]):
        self._data = {ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(homes)}

    def get(self, key: str) -> str | None:
        return self._data.get(key)


class _FakeManager:
    def __init__(self, store: StateStore):
        self.state_store = store
        self._scope = 0

    def current_scope(self) -> int:
        return self._scope


def _device(home_id: str = HOME) -> SimpleNamespace:
    return SimpleNamespace(home_id=home_id)


@pytest.fixture
async def scene():
    """(proxy, store, manager)；proxy 只装 refresh_devices 这条路要用到的东西。"""
    store = StateStore()
    store.start()
    manager = _FakeManager(store)

    proxy = object.__new__(MiotProxy)
    proxy._refresh_devices_lock = asyncio.Lock()
    proxy._device_info_dict = {}
    proxy._kv_repo = _FakeKV([HOME])
    proxy._miot_client = SimpleNamespace(get_devices_async=AsyncMock(return_value={}))
    # 订阅对账已派后台任务(见 _spawn_subscription_sync)，与容器调和无关，置空
    proxy._spawn_subscription_sync = lambda: None

    import miloco.manager as manager_module

    original = manager_module.get_manager
    manager_module.get_manager = lambda: manager
    yield SimpleNamespace(proxy=proxy, store=store, manager=manager)
    manager_module.get_manager = original
    store.stop()


def _seed(store: StateStore, *dids: str) -> None:
    for did in dids:
        store.set(f"iot/device/{did}/status/online", True, source="iot_align")


async def test_a_device_that_left_the_current_home_loses_its_leaves(scene):
    _seed(scene.store, "gone", "stay")
    scene.proxy._miot_client.get_devices_async.return_value = {"stay": _device()}

    await scene.proxy.refresh_devices()

    assert scene.store.get("iot/device/gone/status/online") is MISSING
    assert scene.store.get("iot/device/stay/status/online") is True


async def test_a_device_that_moved_to_another_home_loses_its_leaves(scene):
    """设备还在账号里，只是搬去了别的家庭 —— 对当前作用域来说和消失了一样。"""
    _seed(scene.store, "moved")
    scene.proxy._miot_client.get_devices_async.return_value = {
        "moved": _device(home_id="H2")
    }

    await scene.proxy.refresh_devices()

    assert scene.store.get("iot/device/moved/status/online") is MISSING


async def test_nothing_is_deleted_when_the_refresh_failed(scene):
    """拿不到「应该在」的集合时做差集，会把整棵树算成多余的。"""
    _seed(scene.store, "d1")
    scene.proxy._miot_client.get_devices_async.side_effect = RuntimeError("cloud down")

    assert await scene.proxy.refresh_devices() is None
    assert scene.store.get("iot/device/d1/status/online") is True


async def test_nothing_is_deleted_when_the_cloud_returned_no_device_at_all(scene):
    """家里一台设备都没有和接口出问题长得一样，宁可留一台幽灵也不清空整棵树。"""
    _seed(scene.store, "d1")
    scene.proxy._miot_client.get_devices_async.return_value = {}

    await scene.proxy.refresh_devices()

    assert scene.store.get("iot/device/d1/status/online") is True


async def test_an_empty_current_home_still_reconciles(scene):
    """云端有设备、只是一台都不在当前家庭 —— 这不是接口出问题，该删。"""
    _seed(scene.store, "d1")
    scene.proxy._miot_client.get_devices_async.return_value = {
        "other": _device(home_id="H2")
    }

    await scene.proxy.refresh_devices()

    assert scene.store.get("iot/device/d1/status/online") is MISSING


async def test_nothing_is_deleted_when_the_scope_changed_during_the_refresh(scene):
    """refresh_devices 是 MIPS 重连回调，切家过程中会被触发。

    不挡的话，一个旧回调会拿旧家庭的设备集合去和刚清空重建的新树做差集，把新家庭的
    设备全删掉。
    """
    _seed(scene.store, "new_home_device")

    async def _switch_meanwhile() -> dict:
        scene.manager._scope += 1
        return {"old_home_device": _device()}

    scene.proxy._miot_client.get_devices_async.side_effect = _switch_meanwhile

    await scene.proxy.refresh_devices()

    assert scene.store.get("iot/device/new_home_device/status/online") is True
