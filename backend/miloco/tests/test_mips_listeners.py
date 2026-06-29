# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for the MIPS push-event listeners (bind / device-meta / scene).

Mirrors the source layout: all three listeners share the ``_TrailingDebounce``
skeleton in ``miloco.miot.mips_listeners``, so their tests live together here.

Coverage:
  * Bind  — per-did debounce; present→delegate welcome, absent→unbind; bursts
    collapse per did; deinit cancels + fences.
  * Meta  — global debounce → refresh devices+cameras+scenes; move-in welcome
    flag greets after refresh; bursts collapse globally; deinit cancels.
  * Scene — global debounce → refresh scenes; bursts collapse; deinit cancels.

The welcome ACTION (scope gate, message, send, dedup) is covered separately by
test_welcome_service.py; here the welcome callable is a stub.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.miot import mips_listeners as ml
from miloco.miot.mips_listeners import (
    BindEventListener,
    CameraStateEventListener,
    DeviceMetaEventListener,
    SceneEventListener,
)
from miot.types import (
    MIoTDeviceBindEvent,
    MIoTDeviceStateEvent,
    MIoTSceneChangedEvent,
)


def _device(did: str, name: str = "测试设备", room: str = "卧室", home: str = "测试家"):
    """Stub that quacks like MIoTDeviceInfo for the listener."""
    return SimpleNamespace(
        did=did, name=name, home_id="H1", home_name=home, room_name=room
    )


async def _wait_did(listener, did: str, *, timeout: float = 1.0):
    """Wait until the per-did timer for ``did`` fired AND its task completed."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)
        if did not in listener._timers:
            await asyncio.sleep(0.02)
            return
    pytest.fail(f"debounce timer for did={did} did not fire within {timeout}s")


async def _wait_global(listener, *, timeout: float = 1.0):
    """Wait until the single global timer fired AND its task completed."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)
        if not listener._timers:
            await asyncio.sleep(0.02)
            return
    pytest.fail(f"debounce did not fire within {timeout}s")


# ============================================================ Bind (per-did)


@pytest.fixture
def bind_env(monkeypatch):
    """BindEventListener with refresh + welcome stubbed and a tight window."""
    monkeypatch.setattr(ml, "BIND_DEBOUNCE_SEC", 0.05)
    state = SimpleNamespace(
        devices={},
        refresh_calls=0,
        camera_refresh_calls=0,
        scene_refresh_calls=0,
        welcome=AsyncMock(return_value=True),
    )

    async def fake_refresh():
        state.refresh_calls += 1
        return state.devices

    async def fake_refresh_cameras():
        state.camera_refresh_calls += 1

    async def fake_refresh_scenes():
        state.scene_refresh_calls += 1

    state.listener = BindEventListener(
        refresh_devices=fake_refresh,
        get_device=lambda did: state.devices.get(did),
        welcome=state.welcome,
        refresh_cameras=fake_refresh_cameras,
        refresh_scenes=fake_refresh_scenes,
    )
    return state


def _bind_evt(did: str, event: str = "bind") -> MIoTDeviceBindEvent:
    return MIoTDeviceBindEvent(
        uid="42", event=event, did=did, raw={"uid": "42", "did": did}
    )


@pytest.mark.asyncio
async def test_single_bind_delegates_welcome(bind_env):
    env = bind_env
    did = "1000001"
    env.devices = {did: _device(did)}
    await env.listener.on_event(_bind_evt(did))
    assert env.welcome.await_count == 0  # not until the window settles

    await _wait_did(env.listener, did)
    assert env.refresh_calls == 1
    assert env.camera_refresh_calls == 1
    assert env.scene_refresh_calls == 1
    env.welcome.assert_awaited_once_with(did)


@pytest.mark.asyncio
async def test_bind_then_unbind_collapses_to_unbind(bind_env):
    env = bind_env
    did = "1000002"
    env.devices = {did: _device(did)}
    await env.listener.on_event(_bind_evt(did))

    # Second push within the window; device now absent after refresh → unbind.
    env.devices = {}
    await env.listener.on_event(_bind_evt(did, event="unbind"))

    await _wait_did(env.listener, did)
    assert env.refresh_calls == 1  # trailing-edge fire only
    assert env.camera_refresh_calls == 1  # cameras/scenes refresh regardless
    assert env.scene_refresh_calls == 1
    env.welcome.assert_not_awaited()  # absent → no greeting


@pytest.mark.asyncio
async def test_two_dids_have_independent_timers(bind_env):
    env = bind_env
    env.devices = {"A001": _device("A001"), "B001": _device("B001")}
    await env.listener.on_event(_bind_evt("A001"))
    await env.listener.on_event(_bind_evt("B001"))
    assert "A001" in env.listener._timers
    assert "B001" in env.listener._timers

    await _wait_did(env.listener, "A001")
    await _wait_did(env.listener, "B001")
    assert env.refresh_calls == 2
    welcomed = {c.args[0] for c in env.welcome.await_args_list}
    assert welcomed == {"A001", "B001"}


@pytest.mark.asyncio
async def test_burst_for_same_did_collapses_to_one_welcome(bind_env):
    env = bind_env
    did = "1000003"
    env.devices = {did: _device(did)}
    for _ in range(5):
        await env.listener.on_event(_bind_evt(did))
        await asyncio.sleep(0.01)

    await _wait_did(env.listener, did)
    assert env.refresh_calls == 1
    env.welcome.assert_awaited_once_with(did)


@pytest.mark.asyncio
async def test_bind_deinit_cancels_pending_timers(bind_env):
    env = bind_env
    did = "1000004"
    env.devices = {did: _device(did)}
    await env.listener.on_event(_bind_evt(did))
    assert did in env.listener._timers

    env.listener.deinit()
    assert env.listener._timers == {}
    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0
    env.welcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_deinit_fences_inflight_task(bind_env):
    env = bind_env
    did = "1000005"
    env.devices = {did: _device(did)}

    refresh_blocker = asyncio.Event()

    async def blocking_refresh():
        env.refresh_calls += 1
        await refresh_blocker.wait()
        return env.devices

    env.listener._refresh = blocking_refresh
    await env.listener.on_event(_bind_evt(did))
    await _wait_did(env.listener, did)
    assert env.refresh_calls == 1

    env.listener.deinit()
    refresh_blocker.set()
    await asyncio.sleep(0.05)
    env.welcome.assert_not_awaited()  # bailed at the _closed check post-refresh


@pytest.mark.asyncio
async def test_bind_on_event_after_deinit_is_ignored(bind_env):
    env = bind_env
    env.listener.deinit()
    await env.listener.on_event(_bind_evt("X"))
    assert env.listener._timers == {}
    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0
    env.welcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_event_with_empty_did_is_ignored(bind_env):
    env = bind_env
    await env.listener.on_event(
        MIoTDeviceBindEvent(uid="42", event="bind", did=None, raw={})
    )
    assert env.listener._timers == {}
    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0
    env.welcome.assert_not_awaited()


# ===================================================== Device-meta (global)


@pytest.fixture
def meta_env(monkeypatch):
    """DeviceMetaEventListener with devices/cameras/scenes/welcome stubbed."""
    monkeypatch.setattr(ml, "META_DEBOUNCE_SEC", 0.05)
    state = SimpleNamespace(refresh_calls=0, camera_calls=0, scene_calls=0, welcomed=[])

    async def fake_refresh():
        state.refresh_calls += 1
        return {}

    async def fake_cameras():
        state.camera_calls += 1
        return {}

    async def fake_scenes():
        state.scene_calls += 1
        return {}

    async def fake_welcome(did):
        state.welcomed.append(did)
        return True

    state.listener = DeviceMetaEventListener(
        refresh_devices=fake_refresh,
        refresh_cameras=fake_cameras,
        refresh_scenes=fake_scenes,
        welcome=fake_welcome,
    )
    return state


def _meta_evt(did: str, event: str = "rename") -> MIoTDeviceBindEvent:
    return MIoTDeviceBindEvent(
        uid="42", event=event, did=did, raw={"uid": "42", "did": did}
    )


@pytest.mark.parametrize("event", ["rename", "hr_change"])
@pytest.mark.asyncio
async def test_meta_single_refreshes_after_debounce(meta_env, event):
    env = meta_env
    await env.listener.on_event(_meta_evt("dev-1", event=event))
    assert env.refresh_calls == 0  # before the window settles

    await _wait_global(env.listener)
    assert env.refresh_calls == 1
    assert env.camera_calls == 1
    assert env.scene_calls == 1
    assert env.welcomed == []  # no welcome flag → no greeting


@pytest.mark.asyncio
async def test_meta_welcome_flag_greets_after_refresh(meta_env):
    """on_event(welcome=True) → the did is welcomed once the refresh settles."""
    env = meta_env
    await env.listener.on_event(_meta_evt("dev-9", event="hr_change"), welcome=True)
    assert env.welcomed == []  # deferred until the window settles

    await _wait_global(env.listener)
    assert env.refresh_calls == 1
    assert env.welcomed == ["dev-9"]


@pytest.mark.asyncio
async def test_meta_push_during_refresh_defers_to_next_round(meta_env):
    """A welcome push arriving WHILE the settled refresh is in flight must land
    in the next round's pending set (greeted after a fresh refresh), not be
    welcomed in the current round against a device list that predates it.

    Guards the drain-before-refresh ordering in _fire: if someone moved the
    drain to after the refresh, d2 would be welcomed in the same round and this
    test would fail.
    """
    env = meta_env
    gate = asyncio.Event()

    async def blocking_refresh():
        env.refresh_calls += 1
        await gate.wait()
        return {}

    env.listener._refresh = blocking_refresh

    # Round 1: d1 flagged welcome; once the window settles _fire drains {d1}
    # and blocks inside refresh.
    await env.listener.on_event(_meta_evt("d1", event="hr_change"), welcome=True)
    await asyncio.sleep(0.07)  # > META_DEBOUNCE_SEC=0.05 → _fire is in blocking_refresh
    assert env.refresh_calls == 1

    # d2 arrives DURING the in-flight refresh → must enter a fresh pending set.
    await env.listener.on_event(_meta_evt("d2", event="hr_change"), welcome=True)
    assert env.listener._pending_welcome_dids == {"d2"}  # not folded into round 1

    # Unblock round 1: it greets only d1 (its drained set), leaving d2 pending.
    gate.set()
    await asyncio.sleep(0.02)
    assert env.welcomed == ["d1"]
    assert env.listener._pending_welcome_dids == {"d2"}

    # Round 2 (the timer d2's on_event scheduled) fires → d2 greeted after its
    # own refresh.
    await _wait_global(env.listener)
    assert env.welcomed == ["d1", "d2"]
    assert env.refresh_calls == 2


@pytest.mark.asyncio
async def test_meta_burst_collapses_to_single_refresh(meta_env):
    env = meta_env
    await env.listener.on_event(_meta_evt("dev-1", event="rename"))
    await env.listener.on_event(_meta_evt("dev-2", event="hr_change"))
    await env.listener.on_event(_meta_evt("dev-3", event="rename"))

    await _wait_global(env.listener)
    assert env.refresh_calls == 1


@pytest.mark.asyncio
async def test_meta_deinit_cancels_pending_refresh(meta_env):
    env = meta_env
    await env.listener.on_event(_meta_evt("dev-1"))
    env.listener.deinit()

    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0
    # A post-deinit event is ignored (listener fenced).
    await env.listener.on_event(_meta_evt("dev-2", event="hr_change"))
    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0


# =========================================================== Scene (global)


@pytest.fixture
def scene_env(monkeypatch):
    """SceneEventListener with refresh_scenes stubbed and a tight window."""
    monkeypatch.setattr(ml, "SCENE_DEBOUNCE_SEC", 0.05)
    state = SimpleNamespace(refresh_calls=0)

    async def fake_refresh():
        state.refresh_calls += 1
        return {}

    state.listener = SceneEventListener(refresh_scenes=fake_refresh)
    return state


def _scene_evt(home_id: str, event: str = "edit", scene_id: str = "sc-1"):
    return MIoTSceneChangedEvent(
        home_id=home_id, event=event, scene_id=scene_id, raw={}
    )


@pytest.mark.parametrize("event", ["rename", "delete", "edit"])
@pytest.mark.asyncio
async def test_scene_single_refreshes_after_debounce(scene_env, event):
    env = scene_env
    await env.listener.on_event(_scene_evt("home-1", event=event))
    assert env.refresh_calls == 0  # before the window settles

    await _wait_global(env.listener)
    assert env.refresh_calls == 1


@pytest.mark.asyncio
async def test_scene_burst_collapses_to_single_refresh(scene_env):
    env = scene_env
    await env.listener.on_event(_scene_evt("home-1", event="rename"))
    await env.listener.on_event(_scene_evt("home-1", event="edit"))
    await env.listener.on_event(_scene_evt("home-2", event="delete"))

    await _wait_global(env.listener)
    assert env.refresh_calls == 1


@pytest.mark.asyncio
async def test_scene_deinit_cancels_pending_refresh(scene_env):
    env = scene_env
    await env.listener.on_event(_scene_evt("home-1"))
    env.listener.deinit()

    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0
    # A post-deinit event is ignored (listener fenced).
    await env.listener.on_event(_scene_evt("home-2", event="delete"))
    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0


# ================================================ Camera state (global debounce)


@pytest.fixture
def camera_state_env(monkeypatch):
    """CameraStateEventListener with refresh_camera_online_status stubbed and
    a tight window. Each event already updated the cached `online` field in
    MiotProxy; this listener only does the trailing reconciliation."""
    monkeypatch.setattr(ml, "CAMERA_STATE_DEBOUNCE_SEC", 0.05)
    state = SimpleNamespace(refresh_calls=0)

    async def fake_refresh():
        state.refresh_calls += 1

    state.listener = CameraStateEventListener(refresh_camera_online_status=fake_refresh)
    return state


def _state_evt(did: str, event: str = "online") -> MIoTDeviceStateEvent:
    return MIoTDeviceStateEvent(
        did=did, event=event, raw={"device_id": did, "event": event}
    )


@pytest.mark.parametrize("event", ["online", "offline"])
@pytest.mark.asyncio
async def test_camera_state_single_refreshes_after_debounce(camera_state_env, event):
    env = camera_state_env
    await env.listener.on_event(_state_evt("cam-1", event=event))
    assert env.refresh_calls == 0  # before the window settles

    await _wait_global(env.listener)
    assert env.refresh_calls == 1  # trailing reconciliation fired


@pytest.mark.asyncio
async def test_camera_state_burst_collapses_to_single_refresh(camera_state_env):
    """A flapping camera (online→offline→online) within the window triggers
    only one reconciliation, re-armed on each event."""
    env = camera_state_env
    await env.listener.on_event(_state_evt("cam-1", event="online"))
    await env.listener.on_event(_state_evt("cam-1", event="offline"))
    await env.listener.on_event(_state_evt("cam-1", event="online"))

    await _wait_global(env.listener)
    assert env.refresh_calls == 1


@pytest.mark.asyncio
async def test_camera_state_multi_did_collapses_globally(camera_state_env):
    """State events from different dids share the single global timer — one
    reconciliation covers the whole burst."""
    env = camera_state_env
    await env.listener.on_event(_state_evt("cam-1", event="online"))
    await env.listener.on_event(_state_evt("cam-2", event="offline"))

    await _wait_global(env.listener)
    assert env.refresh_calls == 1


@pytest.mark.asyncio
async def test_camera_state_deinit_cancels_pending_refresh(camera_state_env):
    env = camera_state_env
    await env.listener.on_event(_state_evt("cam-1"))
    env.listener.deinit()

    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0
    # A post-deinit event is ignored (listener fenced).
    await env.listener.on_event(_state_evt("cam-2", event="offline"))
    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0


@pytest.mark.asyncio
async def test_camera_state_event_after_deinit_is_ignored(camera_state_env):
    env = camera_state_env
    env.listener.deinit()
    await env.listener.on_event(_state_evt("cam-1"))
    assert env.listener._timers == {}
    await asyncio.sleep(0.1)
    assert env.refresh_calls == 0
