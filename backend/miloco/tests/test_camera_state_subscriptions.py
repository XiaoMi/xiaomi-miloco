# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for MiotProxy cloud online/offline state handling.

Behavior under test:

* _sync_device_state_subscriptions reconciles the ACCOUNT-WIDE cloud state
  subscription set to the device list (cameras + non-camera devices): new
  dids subscribed, removed dids unsubscribed, tracked set updated.
* A no-op sync issues no sub/unsub calls.
* A subscribe failure does not record the did as subscribed (so a later
  refresh retries it).
* _on_device_state_changed_event updates _camera_info_dict[did].online and
  _device_info_dict[did].online directly from the event (online→True /
  offline→False). The trailing camera reconciliation debounce is re-armed
  for camera hits, for dids unknown to both caches, and while the camera
  list has never loaded successfully (self-heal, tracked by _cameras_loaded
  — NOT "the cache is empty": a zero-camera account loads successfully into
  an empty dict); known non-camera devices whose camera list HAS loaded do
  not re-arm it.

A bare MiotProxy is built via __new__ with only the attributes these methods
touch, so no MIoTClient / camera / OAuth stack is required.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.miot.client import _RECONCILE_CONCURRENCY, MiotProxy
from miot.types import MIoTDeviceStateEvent


def _bare_proxy() -> MiotProxy:
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._subscribed_device_state_dids = set()
    proxy._camera_info_dict = {}
    proxy._device_info_dict = {}
    proxy._sub_intent_lock = asyncio.Lock()
    proxy._sub_intent_generation = 0
    proxy._sub_semaphore = asyncio.Semaphore(_RECONCILE_CONCURRENCY)
    proxy._cameras_loaded = False
    proxy._background_syncs = set()
    proxy._sub_sync_rerun_requested = False
    proxy._sub_sync_running = False
    proxy._miot_client = AsyncMock()
    proxy._camera_state_listener = SimpleNamespace(on_event=AsyncMock())
    proxy._state_listeners = []
    return proxy


def _cam(did: str, online: bool = False) -> SimpleNamespace:
    return SimpleNamespace(did=did, online=online)


def _dev(did: str, online: bool = False) -> SimpleNamespace:
    return SimpleNamespace(did=did, online=online)


def _state_evt(did: str, event: str = "online") -> MIoTDeviceStateEvent:
    return MIoTDeviceStateEvent(
        did=did, event=event, raw={"device_id": did, "event": event}
    )


# ----------------------------------------- _sync_device_state_subscriptions


@pytest.mark.asyncio
async def test_sync_device_state_subscribes_new_and_unsubscribes_removed():
    proxy = _bare_proxy()
    # Already subscribed to A and B; device list now has B and C.
    proxy._subscribed_device_state_dids = {"A", "B"}
    proxy._device_info_dict = {"B": _dev("B"), "C": _dev("C")}

    await proxy._sync_device_state_subscriptions()

    proxy._miot_client.sub_device_state_async.assert_awaited_once_with("C")
    proxy._miot_client.unsub_device_state_async.assert_awaited_once_with("A")
    assert proxy._subscribed_device_state_dids == {"B", "C"}


@pytest.mark.asyncio
async def test_sync_device_state_subscribes_all_devices_not_just_cameras():
    """The account-wide state sync covers every device, cameras + gateway
    children (blt.*/proxy.*) included; only '/' bridged dids are skipped.
    Gateway children are NOT excluded — a live probe showed their state
    subscribes return SUBACK 0x00 (the earlier 0x87 was a broken-instance
    artifact)."""
    proxy = _bare_proxy()
    proxy._device_info_dict = {
        "lamp-1": _dev("lamp-1"),
        "cam-1": _dev("cam-1"),
        "huami.32098/12264203": _dev("huami.32098/12264203"),
        "blt.3.1pku383ht8o00": _dev("blt.3.1pku383ht8o00"),
        "proxy.foo.1": _dev("proxy.foo.1"),
    }

    await proxy._sync_device_state_subscriptions()

    assert proxy._miot_client.sub_device_state_async.await_count == 4
    proxy._miot_client.sub_device_state_async.assert_any_await("lamp-1")
    proxy._miot_client.sub_device_state_async.assert_any_await("cam-1")
    proxy._miot_client.sub_device_state_async.assert_any_await("blt.3.1pku383ht8o00")
    proxy._miot_client.sub_device_state_async.assert_any_await("proxy.foo.1")
    assert proxy._subscribed_device_state_dids == {
        "lamp-1",
        "cam-1",
        "blt.3.1pku383ht8o00",
        "proxy.foo.1",
    }


@pytest.mark.asyncio
async def test_sync_device_state_noop_when_already_in_sync():
    """When the subscribed set already matches the device list, the sync is a
    no-op — no subscribe/unsubscribe calls at all (locks the shared
    _reconcile_subscriptions early-return)."""
    proxy = _bare_proxy()
    proxy._subscribed_device_state_dids = {"A", "B"}
    proxy._device_info_dict = {"A": _dev("A"), "B": _dev("B")}

    await proxy._sync_device_state_subscriptions()

    proxy._miot_client.sub_device_state_async.assert_not_awaited()
    proxy._miot_client.unsub_device_state_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_device_state_subscribe_failure_keeps_did_untracked():
    proxy = _bare_proxy()
    proxy._device_info_dict = {"lamp-1": _dev("lamp-1")}
    proxy._miot_client.sub_device_state_async = AsyncMock(
        side_effect=RuntimeError("ACL rejected")
    )

    await proxy._sync_device_state_subscriptions()

    assert proxy._subscribed_device_state_dids == set()


# ----------------------------------------- _on_device_state_changed_event


@pytest.mark.asyncio
async def test_online_event_sets_camera_online_true():
    proxy = _bare_proxy()
    proxy._camera_info_dict = {"cam-1": _cam("cam-1", online=False)}

    await proxy._on_device_state_changed_event(_state_evt("cam-1", "online"))

    assert proxy._camera_info_dict["cam-1"].online is True
    proxy._camera_state_listener.on_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_offline_event_sets_camera_online_false():
    proxy = _bare_proxy()
    proxy._camera_info_dict = {"cam-1": _cam("cam-1", online=True)}

    await proxy._on_device_state_changed_event(_state_evt("cam-1", "offline"))

    assert proxy._camera_info_dict["cam-1"].online is False
    proxy._camera_state_listener.on_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_state_event_for_unknown_did_rearms_reconciliation():
    """A state event for a did in neither cache must not raise or touch either
    cache, but DOES re-arm the camera reconciliation debounce — it may be a
    camera the camera cache hasn't picked up yet (stale/empty restart case),
    and the debounce is the self-heal that rebuilds the cache."""
    proxy = _bare_proxy()
    proxy._camera_info_dict = {"cam-1": _cam("cam-1", online=False)}

    await proxy._on_device_state_changed_event(_state_evt("unknown-did", "online"))

    # cam-1 untouched.
    assert proxy._camera_info_dict["cam-1"].online is False
    proxy._camera_state_listener.on_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_offline_event_sets_device_online_false():
    """A known non-camera device's offline event updates _device_info_dict so
    `device list` reflects it immediately, and does NOT re-arm the camera
    reconciliation debounce (camera list loaded non-empty, lamp not in it)."""
    proxy = _bare_proxy()
    proxy._cameras_loaded = True
    proxy._camera_info_dict = {"cam-1": _cam("cam-1")}
    proxy._device_info_dict = {"lamp-1": _dev("lamp-1", online=True)}

    await proxy._on_device_state_changed_event(_state_evt("lamp-1", "offline"))

    assert proxy._device_info_dict["lamp-1"].online is False
    proxy._camera_state_listener.on_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_state_event_rearms_until_camera_list_loaded():
    """While the camera list has never loaded successfully (refresh_cameras
    failed at startup), even a known device's state event must re-arm the
    reconciliation — otherwise the only self-heal path for the camera list
    never runs. Once loaded (even to an empty dict for a camera-less
    account), the re-arming stops for known non-cameras."""
    proxy = _bare_proxy()
    proxy._device_info_dict = {"lamp-1": _dev("lamp-1", online=True)}

    await proxy._on_device_state_changed_event(_state_evt("lamp-1", "offline"))

    assert proxy._device_info_dict["lamp-1"].online is False
    proxy._camera_state_listener.on_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_camera_event_also_updates_device_cache():
    """A camera lives in BOTH caches; the device cache must follow the event
    too, or `device list` would keep showing a stale online for it."""
    proxy = _bare_proxy()
    proxy._camera_info_dict = {"cam-1": _cam("cam-1", online=True)}
    proxy._device_info_dict = {"cam-1": _dev("cam-1", online=True)}

    await proxy._on_device_state_changed_event(_state_evt("cam-1", "offline"))

    assert proxy._camera_info_dict["cam-1"].online is False
    assert proxy._device_info_dict["cam-1"].online is False


# ------------------------------------------- refresh_devices wiring


@pytest.mark.asyncio
async def test_refresh_devices_invokes_device_state_sync():
    """refresh_devices must wire in the account-wide state subscription — not
    just leave _sync_device_state_subscriptions as an uncalled helper."""
    proxy = _bare_proxy()
    proxy._refresh_devices_lock = asyncio.Lock()
    proxy._miot_client.get_devices_async = AsyncMock(
        return_value={"lamp-1": _dev("lamp-1")}
    )
    # Stub the sibling syncs so the wiring under test is isolated.
    proxy._sync_meta_subscriptions = AsyncMock()
    proxy._sync_scene_subscriptions = AsyncMock()

    await proxy.refresh_devices()
    await asyncio.gather(*list(proxy._background_syncs))

    proxy._miot_client.sub_device_state_async.assert_awaited_once_with("lamp-1")


@pytest.mark.asyncio
async def test_refresh_cameras_also_syncs_device_state(monkeypatch):
    """Standalone refresh_cameras (not refresh_devices) must also reconcile
    state subscriptions — otherwise /refresh_miot_cameras & friends leave a
    newly-bound camera late-subscribed / a removed camera leaked."""
    proxy = _bare_proxy()
    proxy._refresh_cameras_lock = asyncio.Lock()
    proxy._camera_img_managers = {}
    proxy._camera_awake_cache = {}
    proxy._kv_repo = SimpleNamespace()
    proxy._miot_client.get_cameras_async = AsyncMock(return_value={})
    # The real _sync_device_state_subscriptions reads _device_info_dict; set it
    # so the assertion below proves refresh_cameras actually invoked the sync.
    proxy._device_info_dict = {"cam-1": _dev("cam-1")}
    # refresh_cameras spawns the sync as a BACKGROUND task; stub the sibling
    # syncs and await the spawned task below.
    proxy._sync_meta_subscriptions = AsyncMock()
    proxy._sync_scene_subscriptions = AsyncMock()
    # Skip the streaming-set selection (KV / home / blacklist heavy).
    monkeypatch.setattr(
        "miloco.miot.client.select_active_camera_dids", lambda *a, **k: []
    )

    await proxy.refresh_cameras()
    await asyncio.gather(*list(proxy._background_syncs))

    proxy._miot_client.sub_device_state_async.assert_awaited_once_with("cam-1")


@pytest.mark.asyncio
async def test_refresh_camera_online_status_spawns_subscription_sync():
    """refresh_camera_online_status must also reconcile state subscriptions —
    it's the self-heal path that picks up a reconcile dropped in the sync-loop
    teardown window; without it a freshly-bound device's online/offline pushes
    stay silent until the next device/camera list load."""
    proxy = _bare_proxy()
    proxy._refresh_cameras_lock = asyncio.Lock()
    proxy._camera_awake_cache = {}
    # 相机不带 home_id → is_home_allowed 短路为 False,read_cameras_awake 不会
    # 被调,测试聚焦在尾部派对账这一句。
    proxy._kv_repo = SimpleNamespace(get=lambda key, default=None: None)
    proxy._device_info_dict = {"cam-1": _dev("cam-1")}
    proxy._miot_client.get_cameras_async = AsyncMock(
        return_value={"cam-1": _cam("cam-1")}
    )
    proxy._sync_meta_subscriptions = AsyncMock()
    proxy._sync_scene_subscriptions = AsyncMock()

    await proxy.refresh_camera_online_status()
    await asyncio.gather(*list(proxy._background_syncs))

    proxy._miot_client.sub_device_state_async.assert_awaited_once_with("cam-1")


@pytest.mark.asyncio
async def test_refresh_cameras_state_sync_sees_sdk_device_buffer(monkeypatch):
    """refresh_cameras' trailing state sync must see devices merged in-place
    into the SDK buffer by get_cameras_async — i.e. _device_info_dict must be
    the SAME object as the buffer, not a frozen snapshot. If someone changed
    `self._device_info_dict = devices` to a deepcopy, a device bound between
    refresh_devices and refresh_cameras would be treated as "should unsub"."""
    proxy = _bare_proxy()
    proxy._refresh_devices_lock = asyncio.Lock()
    proxy._refresh_cameras_lock = asyncio.Lock()
    proxy._camera_img_managers = {}
    proxy._camera_awake_cache = {}
    proxy._kv_repo = SimpleNamespace()
    buffer: dict = {}
    proxy._miot_client.get_devices_async = AsyncMock(return_value=buffer)
    proxy._sync_meta_subscriptions = AsyncMock()
    proxy._sync_scene_subscriptions = AsyncMock()

    async def _get_cameras():
        # Mimic the SDK: get_cameras_async internally runs get_devices_async,
        # which merges newly-bound devices into the SAME buffer object.
        buffer["lamp-2"] = _dev("lamp-2")
        return {}

    proxy._miot_client.get_cameras_async = AsyncMock(side_effect=_get_cameras)
    monkeypatch.setattr(
        "miloco.miot.client.select_active_camera_dids", lambda *a, **k: []
    )

    await proxy.refresh_devices()  # binds _device_info_dict to the buffer
    await proxy.refresh_cameras()  # this sync must see lamp-2
    await asyncio.gather(*list(proxy._background_syncs))

    proxy._miot_client.sub_device_state_async.assert_any_await("lamp-2")


@pytest.mark.asyncio
async def test_spawn_subscription_sync_coalesces_overlapping_spawns():
    """同一窗口内多次 _spawn_subscription_sync 只产生一个对账任务:后续触发折叠成
    rerun 标志,由循环补一轮吸收,而不是各建一个任务(轮次重叠会重复发包)。"""
    proxy = _bare_proxy()
    proxy._sync_meta_subscriptions = AsyncMock()
    proxy._sync_scene_subscriptions = AsyncMock()

    started = asyncio.Event()
    release = asyncio.Event()

    async def _controlled_state_sync() -> None:
        started.set()
        await release.wait()

    proxy._sync_device_state_subscriptions = _controlled_state_sync

    proxy._spawn_subscription_sync()
    await started.wait()  # 第 1 轮已起飞(差集已算完),state 停在等 release
    proxy._spawn_subscription_sync()  # 轮次飞行期间的触发 → 只置 rerun 标志

    # 只建了一个任务(第二次触发没有新建)。
    assert len(proxy._background_syncs) == 1

    release.set()
    await asyncio.gather(*list(proxy._background_syncs))

    # rerun 标志让循环补跑了一轮:meta/scene 各执行两次。
    assert proxy._sync_meta_subscriptions.await_count == 2
    assert proxy._sync_scene_subscriptions.await_count == 2


@pytest.mark.asyncio
async def test_spawn_subscription_sync_after_loop_returns_reruns():
    """循环协程已经 return、但 done_callback(discard)还排在就绪队列里没执行时
    再次触发,必须起新一轮——不能因为 _background_syncs 还残留旧任务就把触发
    折叠成没人读的 rerun 标志(这次对账会被静默丢掉)。"""
    proxy = _bare_proxy()
    proxy._sync_meta_subscriptions = AsyncMock()
    proxy._sync_device_state_subscriptions = AsyncMock()
    proxy._sync_scene_subscriptions = AsyncMock()

    proxy._spawn_subscription_sync()
    task = next(iter(proxy._background_syncs))

    # 泵到循环任务完成:sleep(0) 的续体排在 discard 之前,所以回到这里时
    # task.done() 已为 True 而 discard 还没执行——正是 add_done_callback 的
    # call_soon 窗口。
    while not task.done():
        await asyncio.sleep(0)

    proxy._spawn_subscription_sync()
    await asyncio.gather(*list(proxy._background_syncs))

    assert proxy._sync_meta_subscriptions.await_count == 2
    assert proxy._sync_device_state_subscriptions.await_count == 2
    assert proxy._sync_scene_subscriptions.await_count == 2
