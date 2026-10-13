# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""属性推送订阅：账号全量、跳过桥接子设备、增删对账、镜像重建、事件扇出。

订阅账号全量而容器只装当前家庭，这个不对称是有意的：订阅要同时喂容器和（将来的）
属性历史两个消费方，家庭过滤是容器一家的需求，放在写容器那一侧。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from miloco.database.kv_repo import ScopeConfigKeys
from miloco.miot.client import _RECONCILE_CONCURRENCY, MiotProxy

HOME = "H1"


class _FakeKV:
    """只启用一个家庭 —— 让「家庭外的设备」在用例里真的存在。"""

    def __init__(self) -> None:
        self._data = {ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps([HOME])}

    def get(self, key: str) -> str | None:
        return self._data.get(key)


def _proxy(devices):
    """devices 可以是 did 列表（默认当前家庭），或 {did: home_id}。"""
    if not isinstance(devices, dict):
        devices = {did: HOME for did in devices}
    proxy = object.__new__(MiotProxy)
    proxy._kv_repo = _FakeKV()
    proxy._device_info_dict = {
        did: SimpleNamespace(home_id=home) for did, home in devices.items()
    }
    proxy._subscribed_props_dids = set()
    proxy._sub_intent_lock = asyncio.Lock()
    proxy._sub_intent_generation = 0
    proxy._sub_semaphore = asyncio.Semaphore(_RECONCILE_CONCURRENCY)
    proxy._miot_client = SimpleNamespace(
        sub_device_props_async=AsyncMock(),
        unsub_device_props_async=AsyncMock(),
    )
    return proxy


async def test_a_device_outside_the_current_home_is_still_subscribed():
    """属性历史对管辖范围外的设备一样有用，家庭过滤放在写容器那一侧。

    名单里必须真有一台家庭外的设备：全是当前家庭的话，「按家庭过滤」这个错也能绿。
    """
    proxy = _proxy({"d1": HOME, "d-other": "H2"})

    await proxy._sync_props_subscriptions()

    assert proxy._subscribed_props_dids == {"d1", "d-other"}


async def test_a_bridged_did_is_skipped():
    """'/' 会打断 topic 路径，也会打断解码正则。"""
    proxy = _proxy(["d1", "blt/1"])

    await proxy._sync_props_subscriptions()

    assert proxy._subscribed_props_dids == {"d1"}


async def test_a_device_that_left_the_account_is_unsubscribed():
    proxy = _proxy(["d1"])
    await proxy._sync_props_subscriptions()
    proxy._device_info_dict = {}

    await proxy._sync_props_subscriptions()

    assert proxy._subscribed_props_dids == set()
    proxy._miot_client.unsub_device_props_async.assert_awaited_once_with("d1")


async def test_one_failed_subscribe_does_not_block_the_others():
    proxy = _proxy(["d1", "d2"])

    async def flaky(did):
        if did == "d1":
            raise RuntimeError("suback rejected")

    proxy._miot_client.sub_device_props_async = flaky

    await proxy._sync_props_subscriptions()

    assert proxy._subscribed_props_dids == {"d2"}


async def test_the_mirror_is_rebuilt_from_the_sdk_after_a_mips_restart():
    """mips 重建后 broker 上一个订阅都不剩，镜像要按 SDK 重放后的真相重建。"""
    proxy = _proxy(["d1"])
    proxy._subscribed_meta_dids = set()
    proxy._subscribed_device_state_dids = set()
    proxy._subscribed_scene_home_ids = set()
    proxy._subscribed_props_dids = {"stale"}

    await proxy._on_subscription_reset(set(), set(), set(), {"d1"})

    assert proxy._subscribed_props_dids == {"d1"}


async def test_the_lifecycle_reset_clears_the_props_mirror():
    """镜像不清，下一轮对账会算出 to_add 为空、订阅静默丢失。"""
    proxy = _proxy([])
    proxy._subscribed_meta_dids = {"m"}
    proxy._subscribed_device_state_dids = {"s"}
    proxy._subscribed_scene_home_ids = {"h"}
    proxy._subscribed_props_dids = {"d1"}

    proxy._reset_subscription_mirrors()

    assert proxy._subscribed_props_dids == set()


async def test_props_events_reach_every_listener():
    proxy = object.__new__(MiotProxy)
    proxy._props_listeners = []
    seen = AsyncMock()
    proxy.add_device_props_listener(seen)

    await proxy._on_device_props_changed_event("event")

    seen.assert_awaited_once_with("event")


async def test_the_reconcile_loop_covers_the_props_lane():
    """漏挂进循环的后果：只有手工调 _sync_props_subscriptions 才会对账。"""
    proxy = _proxy(["d1"])
    proxy._sub_sync_rerun_requested = False
    proxy._sub_sync_running = True
    for name in (
        "_sync_meta_subscriptions",
        "_sync_device_state_subscriptions",
        "_sync_scene_subscriptions",
    ):
        setattr(proxy, name, AsyncMock())

    await proxy._subscription_sync_loop()

    assert proxy._subscribed_props_dids == {"d1"}
