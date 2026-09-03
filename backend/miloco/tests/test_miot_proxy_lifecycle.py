# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Integration: MiotProxy.deinit() → init() rebuilds the bind listener.

Regression guard for the bug where service.unbind_miot (deinit + init)
silently disabled bind/unbind push handling: _bind_listener was created
exactly once in __init__, and deinit() set its _closed=True permanently,
so post-init pushes were dropped while /mips_status still reported
healthy.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.config import reset_settings
from miloco.miot import mips_listeners as bl_module
from miloco.miot import welcome_service as ws_module
from miloco.miot.client import MiotProxy
from miot.types import MIoTDeviceBindEvent


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """$MILOCO_HOME → tmp so get_settings() doesn't touch user data."""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    reset_settings()
    yield
    reset_settings()


def _kv_stub():
    import json

    from miloco.database.kv_repo import ScopeConfigKeys

    store: dict[str, str | None] = {
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])
    }
    return SimpleNamespace(
        get=lambda key, default=None: store.get(key, default),
        set=lambda key, value: store.__setitem__(key, value) or True,
        delete=lambda key: store.pop(key, None),
    )


def _device(did: str, name: str = "新台灯"):
    """Stub that quacks like MIoTDeviceInfo for the listener / log path."""
    return SimpleNamespace(
        did=did,
        name=name,
        model="t.x.1",
        manufacturer="t",
        urn="urn:miot-spec-v2:device:test:0:t:1",
        home_id="H1",
        home_name="家",
        room_id="R1",
        room_name="书房",
        online=True,
        lan_online=True,
        order_time=0,
        parent_id=None,
        owner_nickname=None,
        fw_version="1",
        sub_devices={},
    )


def _make_client_stub():
    """Stub MIoTClient: just enough surface for init / deinit / refresh_devices."""
    c = MagicMock()
    c.init_async = AsyncMock()
    c.deinit_async = AsyncMock()
    c.register_user_bind_callback = MagicMock()
    c.register_mips_connect_callback = MagicMock()
    c.register_subscription_reset_callback = MagicMock()
    c.get_devices_async = AsyncMock(return_value={})
    c.get_cameras_async = AsyncMock(return_value={})
    c.get_manual_scenes_async = AsyncMock(return_value={})
    # bind path subscribes the device's per-device topics (state + meta),
    # unbind path unsubscribes them; the stub must be awaitable for both.
    c.sub_device_state_async = AsyncMock()
    c.sub_device_meta_async = AsyncMock()
    c.unsub_device_state_async = AsyncMock()
    c.unsub_device_meta_async = AsyncMock()
    return c


@pytest.fixture
def proxy_env(monkeypatch):
    """A MiotProxy with the three heavy collaborators stubbed.

    Yields ``(proxy, client_stub)``; teardown cancels the idle token task
    if any test left it running outside a deinit() call.
    """
    # Tight debounce keeps the suite fast.
    monkeypatch.setattr(bl_module, "BIND_DEBOUNCE_SEC", 0.05)
    # The greeting goes through DeviceWelcomeService → dispatch_event.
    monkeypatch.setattr(
        ws_module,
        "dispatch_event",
        AsyncMock(return_value=True),
    )

    p = MiotProxy(uuid="u", redirect_uri="http://x", kv_repo=_kv_stub())

    client_stub = _make_client_stub()
    monkeypatch.setattr(p, "_create_miot_client", lambda: client_stub)

    async def _noop_refresh_info():
        return {}

    monkeypatch.setattr(p, "refresh_miot_info", _noop_refresh_info)

    async def _idle_token_task():
        await asyncio.sleep(3600)

    monkeypatch.setattr(p, "_start_token_refresh_task", _idle_token_task)

    yield p, client_stub

    if p._token_refresh_task and not p._token_refresh_task.done():
        p._token_refresh_task.cancel()


@pytest.mark.asyncio
async def test_init_after_deinit_rebuilds_bind_listener(proxy_env):
    """unbind_miot pattern (deinit + init) must restore push handling.

    Without the fix, the proxy keeps the same _bind_listener after init(),
    whose _closed=True flag set by deinit() permanently drops every
    subsequent push at on_event's first line.
    """
    p, client = proxy_env

    await p.init()
    listener_v1 = p._bind_listener
    assert listener_v1._closed is False
    client.register_user_bind_callback.assert_called_with(p._on_user_bind_event)

    await p.deinit()
    assert listener_v1._closed is True  # old listener fenced

    await p.init()
    listener_v2 = p._bind_listener
    assert listener_v2 is not listener_v1, "init() must build a fresh listener"
    assert listener_v2._closed is False

    # Drive the full forward path: _on_user_bind_event → listener.on_event
    # → debounce timer → _fire → refresh_devices → exist check
    # → welcome_service.welcome → dispatch_event.
    did = "did-1"
    client.get_devices_async.return_value = {did: _device(did)}

    await p._on_user_bind_event(
        MIoTDeviceBindEvent(
            uid="u", event="bind", did=did, raw={"uid": "u", "did": did}
        )
    )

    # Poll for the agent message instead of a fixed sleep — debounce is 50ms,
    # plus task scheduling latency.
    deadline = asyncio.get_event_loop().time() + 1.0
    while asyncio.get_event_loop().time() < deadline:
        if ws_module.dispatch_event.await_count >= 1:
            break
        await asyncio.sleep(0.02)

    assert ws_module.dispatch_event.await_count >= 1, (
        "rebuilt listener should process the bind event end-to-end"
    )
    # dispatch_event("bind", [msg_text], builder) — items list is arg[1].
    sent = ws_module.dispatch_event.await_args.args[1][0]
    assert did in sent and "新台灯" in sent

    await p.deinit()


@pytest.mark.asyncio
async def test_bind_event_subscribes_device(proxy_env):
    """A bind push means the device is back in the account: the handler must
    subscribe state+meta immediately (recorded into the intent mirrors on SDK
    success) so the trailing refresh_devices reconcile no-ops instead of
    re-issuing — and a rebind whose broker subscription survived is an
    idempotent no-op at the SDK."""
    p, client = proxy_env

    await p.init()
    # 原地填充、不换集合对象,保住"记账集合只能原地改"的回归检测。
    state_set = p._subscribed_device_state_dids
    meta_set = p._subscribed_meta_dids
    state_set.clear()
    meta_set.clear()

    await p._on_user_bind_event(
        MIoTDeviceBindEvent(uid="u", event="bind", did="did-1", raw={"did": "did-1"})
    )

    assert p._subscribed_device_state_dids == {"did-1"}
    assert p._subscribed_meta_dids == {"did-1"}
    assert p._subscribed_device_state_dids is state_set
    assert p._subscribed_meta_dids is meta_set
    client.sub_device_state_async.assert_awaited_once_with("did-1")
    client.sub_device_meta_async.assert_awaited_once_with("did-1")

    await p.deinit()


@pytest.mark.asyncio
async def test_unbind_event_forgets_device_subscriptions(proxy_env):
    """An unbind push means the device left the account: the handler must drop
    the did from both intent mirrors (in-place) and unsubscribe (best-effort,
    outside the lock) — the trailing refresh_devices reconcile then no-ops."""
    p, client = proxy_env

    await p.init()
    # Simulate a device already subscribed, then unbound.
    state_set = p._subscribed_device_state_dids
    meta_set = p._subscribed_meta_dids
    state_set.clear()
    state_set.update({"did-1"})
    meta_set.clear()
    meta_set.update({"did-1"})

    await p._on_user_bind_event(
        MIoTDeviceBindEvent(uid="u", event="unbind", did="did-1", raw={"did": "did-1"})
    )

    assert p._subscribed_device_state_dids == set()
    assert p._subscribed_meta_dids == set()
    assert p._subscribed_device_state_dids is state_set
    assert p._subscribed_meta_dids is meta_set
    client.unsub_device_state_async.assert_awaited_once_with("did-1")
    client.unsub_device_meta_async.assert_awaited_once_with("did-1")

    await p.deinit()


@pytest.mark.asyncio
async def test_subscription_reset_rebuilds_intent_sets(proxy_env):
    """When the SDK rebuilds its mips instance it calls the reset callback
    AFTER replay with the post-replay tracked sets; the proxy must rebuild its
    intent mirrors as faithful copies — so a replay-failed entity (dropped
    from the SDK set) is retried next reconcile, and a departed entity that
    replay re-subscribed stays removable."""
    p, client = proxy_env
    await p.init()
    # init() registered the callback — capture it.
    reset_cb = client.register_subscription_reset_callback.call_args.args[0]
    assert reset_cb.__self__ is p
    assert reset_cb.__func__.__name__ == "_on_subscription_reset"

    # 原地填充、不换集合对象,保住"记账集合只能原地改"的回归检测。
    meta_set = p._subscribed_meta_dids
    state_set = p._subscribed_device_state_dids
    scene_set = p._subscribed_scene_home_ids
    meta_set.clear()
    meta_set.update({"stale-1"})
    state_set.clear()
    state_set.update({"stale-1"})
    scene_set.clear()
    scene_set.update({"stale-home"})

    await reset_cb({"did-1"}, {"did-1"}, {"home-9"})

    assert p._subscribed_meta_dids == {"did-1"}
    assert p._subscribed_device_state_dids == {"did-1"}
    assert p._subscribed_scene_home_ids == {"home-9"}
    # 原地改写不变式的真正断言:重建必须原地改,不能换新集合对象。
    assert p._subscribed_meta_dids is meta_set
    assert p._subscribed_device_state_dids is state_set
    assert p._subscribed_scene_home_ids is scene_set

    await p.deinit()


@pytest.mark.asyncio
async def test_bind_event_forwards_despite_sub_failure(proxy_env):
    """A failed bind-triggered subscribe must only log — it must NOT abort
    delivery to _bind_listener.on_event, or this bind's debounce/welcome
    would never fire. The mirror stays untracked so the trailing
    refresh_devices reconcile retries the subscribe."""
    p, client = proxy_env
    await p.init()
    client.sub_device_state_async = AsyncMock(side_effect=RuntimeError("mqtt blip"))
    client.sub_device_meta_async = AsyncMock(side_effect=RuntimeError("mqtt blip"))

    await p._on_user_bind_event(
        MIoTDeviceBindEvent(uid="u", event="bind", did="did-1", raw={"did": "did-1"})
    )

    # Both subs attempted despite raising; local bookkeeping NOT recorded
    # (self-healing: next refresh_devices treats did-1 as to-be-subscribed).
    client.sub_device_state_async.assert_awaited_once_with("did-1")
    client.sub_device_meta_async.assert_awaited_once_with("did-1")
    assert p._subscribed_device_state_dids == set()
    assert p._subscribed_meta_dids == set()
    # The bind debounce timer for did-1 was armed — proves on_event ran.
    assert "did-1" in p._bind_listener._timers

    await p.deinit()


@pytest.mark.asyncio
async def test_unbind_event_forwards_despite_unsub_failure(proxy_env):
    """A failed unbind-triggered unsubscribe must only log — it must NOT abort
    delivery to _bind_listener.on_event (which arms the trailing refresh /
    final-state detection). Local bookkeeping is still cleared: the mirror
    no longer claims the did, so nothing re-subscribes a departed device."""
    p, client = proxy_env
    await p.init()
    state_set = p._subscribed_device_state_dids
    meta_set = p._subscribed_meta_dids
    state_set.clear()
    state_set.update({"did-1"})
    meta_set.clear()
    meta_set.update({"did-1"})
    client.unsub_device_state_async = AsyncMock(side_effect=RuntimeError("mqtt blip"))
    client.unsub_device_meta_async = AsyncMock(side_effect=RuntimeError("mqtt blip"))

    await p._on_user_bind_event(
        MIoTDeviceBindEvent(uid="u", event="unbind", did="did-1", raw={"did": "did-1"})
    )

    # Both unsubs attempted despite raising; local bookkeeping still cleared.
    client.unsub_device_state_async.assert_awaited_once_with("did-1")
    client.unsub_device_meta_async.assert_awaited_once_with("did-1")
    assert p._subscribed_device_state_dids == set()
    assert p._subscribed_meta_dids == set()
    # The bind debounce timer for did-1 was armed — proves on_event ran.
    assert "did-1" in p._bind_listener._timers

    await p.deinit()


@pytest.mark.asyncio
async def test_push_callbacks_registered_before_init_async(proxy_env):
    """Push handlers + live listeners must be in place BEFORE init_async().

    init_async runs _setup_mips_async, whose SUBSCRIBE may trigger an immediate
    broker push; a handler registered afterwards would miss that window (the SDK
    drops the push on `cb is None`). This locks the ordering so a future refactor
    can't silently move registration back below init_async.
    """
    p, client = proxy_env

    seen: dict[str, bool] = {}

    async def recording_init():
        # Captured at the instant init_async runs — i.e. before any post-init
        # wiring in init() has a chance to execute.
        seen["user_bind"] = client.register_user_bind_callback.called
        seen["device_meta"] = client.register_device_meta_changed_callback.called
        seen["scene"] = client.register_scene_changed_callback.called
        seen["listeners_live"] = not (
            p._bind_listener._closed
            or p._meta_listener._closed
            or p._scene_listener._closed
        )

    client.init_async = AsyncMock(side_effect=recording_init)

    await p.init()

    assert seen == {
        "user_bind": True,
        "device_meta": True,
        "scene": True,
        "listeners_live": True,
    }
