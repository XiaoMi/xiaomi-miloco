# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for MIoTClient.sub_device_meta_async / sub_home_scene_async tracking.

`_meta_sub_dids` / `_scene_sub_home_ids` must mirror what is actually
subscribed at the broker, NOT mere intent-to-subscribe:

* A successful subscribe (mips connected) records the did/home.
* A FAILED subscribe (connected but SUBACK rejected / timed out) must NOT
  record it — otherwise the idempotency guard would short-circuit the
  proxy-level retry in _sync_meta_subscriptions and the entity's events would
  be silently lost forever.
* While mips is disconnected only the intent is recorded (replayed at setup).
* Already-tracked entities are a no-op (idempotent).

A bare MIoTClient is built via __new__ with only the attributes these methods
touch — no OAuth / camera / network stack required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miot.client import MIoTClient

from miot import client as client_mod


class _FakeMips:
    def __init__(self, *, connected: bool = True, fail: bool = False) -> None:
        self.is_connected = connected
        self._fail = fail
        self.sub_device_meta_changed_async = AsyncMock(side_effect=self._maybe_fail)
        self.sub_home_scene_changed_async = AsyncMock(side_effect=self._maybe_fail)
        self.sub_device_state_async = AsyncMock(side_effect=self._maybe_fail)

    async def _maybe_fail(self, *args, **kwargs) -> None:
        if self._fail:
            raise RuntimeError("SUBACK rejected")


def _bare_client(mips: _FakeMips | None) -> MIoTClient:
    client = MIoTClient.__new__(MIoTClient)
    client._meta_sub_dids = set()
    client._scene_sub_home_ids = set()
    client._state_sub_dids = set()
    client._mips_cloud = mips
    client._callback_device_meta_changed = None
    client._callback_scene_changed = None
    client._callback_device_state_changed = None
    return client


# ----------------------------------------------------------------- device meta


@pytest.mark.asyncio
async def test_sub_device_meta_records_on_success():
    mips = _FakeMips(connected=True)
    client = _bare_client(mips)

    await client.sub_device_meta_async("dev-1")

    mips.sub_device_meta_changed_async.assert_awaited_once()
    assert client._meta_sub_dids == {"dev-1"}


@pytest.mark.asyncio
async def test_sub_device_meta_failure_leaves_untracked_and_raises():
    """Connected but SUBACK fails → did must NOT be tracked, so the proxy diff
    retries it on the next refresh instead of short-circuiting."""
    mips = _FakeMips(connected=True, fail=True)
    client = _bare_client(mips)

    with pytest.raises(RuntimeError):
        await client.sub_device_meta_async("dev-1")

    assert client._meta_sub_dids == set()

    # A subsequent retry (broker now healthy) succeeds and records the did.
    mips._fail = False
    await client.sub_device_meta_async("dev-1")
    assert client._meta_sub_dids == {"dev-1"}


@pytest.mark.asyncio
async def test_sub_device_meta_disconnected_records_intent_only():
    mips = _FakeMips(connected=False)
    client = _bare_client(mips)

    await client.sub_device_meta_async("dev-1")

    mips.sub_device_meta_changed_async.assert_not_awaited()
    assert client._meta_sub_dids == {"dev-1"}


@pytest.mark.asyncio
async def test_sub_device_meta_idempotent():
    mips = _FakeMips(connected=True)
    client = _bare_client(mips)
    client._meta_sub_dids = {"dev-1"}

    await client.sub_device_meta_async("dev-1")

    mips.sub_device_meta_changed_async.assert_not_awaited()


# ------------------------------------------------------------------ home scene


@pytest.mark.asyncio
async def test_sub_home_scene_records_on_success():
    mips = _FakeMips(connected=True)
    client = _bare_client(mips)

    await client.sub_home_scene_async("home-1")

    mips.sub_home_scene_changed_async.assert_awaited_once()
    assert client._scene_sub_home_ids == {"home-1"}


@pytest.mark.asyncio
async def test_sub_home_scene_failure_leaves_untracked_and_raises():
    mips = _FakeMips(connected=True, fail=True)
    client = _bare_client(mips)

    with pytest.raises(RuntimeError):
        await client.sub_home_scene_async("home-1")

    assert client._scene_sub_home_ids == set()


@pytest.mark.asyncio
async def test_sub_home_scene_disconnected_records_intent_only():
    mips = _FakeMips(connected=False)
    client = _bare_client(mips)

    await client.sub_home_scene_async("home-1")

    mips.sub_home_scene_changed_async.assert_not_awaited()
    assert client._scene_sub_home_ids == {"home-1"}


# --------------------------------------------------- _setup_mips_async replay
#
# The "re-OAuth / fresh setup" layer of the two-tier replay: dids/home_ids
# tracked from a prior mips instance are cleared then re-subscribed one by one
# at setup (mips._subs is wiped on re-setup, so plain-reconnect replay can't
# cover them). Per-entity failures are caught so one bad SUBACK can't abort the
# rest, and only the entities that re-subscribe OK end up back in the records.


class _SetupFakeMips:
    """Minimal mips stand-in for driving _setup_mips_async end-to-end.

    sub_device_meta_changed_async raises for any did in ``fail_meta_dids`` to
    exercise the per-did failure-tolerance branch of the replay loop.
    """

    def __init__(self, *, fail_meta_dids: frozenset[str] = frozenset()) -> None:
        self.is_connected = True
        self._fail_meta_dids = set(fail_meta_dids)
        self._fail_state_dids: set[str] = set()
        self.init_async = AsyncMock()
        self.sub_user_bind_async = AsyncMock()
        self.sub_user_unbind_async = AsyncMock()
        self.sub_device_meta_changed_async = AsyncMock(side_effect=self._meta)
        self.sub_home_scene_changed_async = AsyncMock()
        self.sub_device_state_async = AsyncMock(side_effect=self._state)

    async def _meta(self, did: str, handler) -> None:
        if did in self._fail_meta_dids:
            raise RuntimeError(f"SUBACK rejected for {did}")

    async def _state(self, did: str, handler) -> None:
        if did in self._fail_state_dids:
            raise RuntimeError(f"SUBACK rejected for {did}")

    def register_subscribe_error_handler(self, cb) -> None: ...
    def register_subscribe_success_handler(self, cb) -> None: ...
    def register_mips_state_handler(self, cb) -> None: ...


def _setup_client(
    monkeypatch, fake, *, meta_dids, scene_home_ids, state_dids=()
) -> MIoTClient:
    monkeypatch.setattr(client_mod, "MIoTMipsCloud", lambda **kw: fake)
    client = MIoTClient.__new__(MIoTClient)
    client._oauth_info = SimpleNamespace(
        access_token="tok", user_info=SimpleNamespace(uid="uid-1")
    )
    client._mips_cloud = None
    client._uuid = "uuid"
    client._cloud_server = "cn"
    client._main_loop = None
    client._mips_user_sub_error = "stale"
    client._meta_sub_dids = set(meta_dids)
    client._scene_sub_home_ids = set(scene_home_ids)
    client._state_sub_dids = set(state_dids)
    client._callback_device_meta_changed = None
    client._callback_scene_changed = None
    client._callback_device_state_changed = None
    return client


@pytest.mark.asyncio
async def test_setup_replays_tracked_subscriptions(monkeypatch):
    fake = _SetupFakeMips()
    client = _setup_client(
        monkeypatch, fake, meta_dids={"dev-1", "dev-2"}, scene_home_ids={"home-9"}
    )

    await client._setup_mips_async()

    # Every tracked did/home re-subscribed exactly once at the broker.
    assert {c.args[0] for c in fake.sub_device_meta_changed_async.await_args_list} == {
        "dev-1",
        "dev-2",
    }
    fake.sub_home_scene_changed_async.assert_awaited_once()
    assert fake.sub_home_scene_changed_async.await_args.args[0] == "home-9"
    # Records cleared then re-filled — the mirror stays accurate.
    assert client._meta_sub_dids == {"dev-1", "dev-2"}
    assert client._scene_sub_home_ids == {"home-9"}


@pytest.mark.asyncio
async def test_setup_replay_partial_failure_keeps_failed_untracked(monkeypatch):
    fake = _SetupFakeMips(fail_meta_dids=frozenset({"dev-bad"}))
    client = _setup_client(
        monkeypatch, fake, meta_dids={"dev-ok", "dev-bad"}, scene_home_ids=set()
    )

    # A per-did replay failure is caught + logged; setup must not raise.
    await client._setup_mips_async()

    assert {c.args[0] for c in fake.sub_device_meta_changed_async.await_args_list} == {
        "dev-ok",
        "dev-bad",
    }
    # dev-bad's SUBACK failed → not re-recorded, so the next refresh sync retries
    # it; dev-ok succeeded and is tracked.
    assert client._meta_sub_dids == {"dev-ok"}


# ------------------------------------------------------------ device cloud state
#
# `_state_sub_dids` mirrors what is actually subscribed at the broker, same
# contract as _meta_sub_dids: success records, failure does not, disconnected
# records intent only, idempotent for already-tracked dids.


@pytest.mark.asyncio
async def test_sub_device_state_records_on_success():
    mips = _FakeMips(connected=True)
    client = _bare_client(mips)

    await client.sub_device_state_async("dev-1")

    mips.sub_device_state_async.assert_awaited_once()
    assert client._state_sub_dids == {"dev-1"}


@pytest.mark.asyncio
async def test_sub_device_state_failure_leaves_untracked_and_raises():
    mips = _FakeMips(connected=True, fail=True)
    client = _bare_client(mips)

    with pytest.raises(RuntimeError):
        await client.sub_device_state_async("dev-1")

    assert client._state_sub_dids == set()

    mips._fail = False
    await client.sub_device_state_async("dev-1")
    assert client._state_sub_dids == {"dev-1"}


@pytest.mark.asyncio
async def test_sub_device_state_disconnected_records_intent_only():
    mips = _FakeMips(connected=False)
    client = _bare_client(mips)

    await client.sub_device_state_async("dev-1")

    mips.sub_device_state_async.assert_not_awaited()
    assert client._state_sub_dids == {"dev-1"}


@pytest.mark.asyncio
async def test_sub_device_state_idempotent():
    mips = _FakeMips(connected=True)
    client = _bare_client(mips)
    client._state_sub_dids = {"dev-1"}

    await client.sub_device_state_async("dev-1")

    mips.sub_device_state_async.assert_not_awaited()


# ------------------------------------------- _setup_mips_async state replay


@pytest.mark.asyncio
async def test_setup_replays_state_subscriptions(monkeypatch):
    fake = _SetupFakeMips()
    client = _setup_client(
        monkeypatch,
        fake,
        meta_dids=set(),
        scene_home_ids=set(),
        state_dids={"dev-1", "dev-2"},
    )

    await client._setup_mips_async()

    assert {c.args[0] for c in fake.sub_device_state_async.await_args_list} == {
        "dev-1",
        "dev-2",
    }
    assert client._state_sub_dids == {"dev-1", "dev-2"}


@pytest.mark.asyncio
async def test_setup_replay_state_partial_failure_keeps_failed_untracked(
    monkeypatch,
):
    fake = _SetupFakeMips()
    fake._fail_state_dids = {"dev-bad"}
    client = _setup_client(
        monkeypatch,
        fake,
        meta_dids=set(),
        scene_home_ids=set(),
        state_dids={"dev-ok", "dev-bad"},
    )

    await client._setup_mips_async()

    assert {c.args[0] for c in fake.sub_device_state_async.await_args_list} == {
        "dev-ok",
        "dev-bad",
    }
    # dev-bad failed → not re-recorded; dev-ok succeeded and is tracked.
    assert client._state_sub_dids == {"dev-ok"}


# ------------------------------------ mips_user_sub_error scoping (reconnect)
#
# _on_mips_subscribe_error / _on_mips_subscribe_success run for EVERY unattended
# (post-reconnect) re-subscribe, across all topic families now that this client
# subscribes user-bind, device-meta AND home-scene. The mips_user_sub_error flag
# (surfaced by /mips_status) is about user-bind detection only, so these handlers
# must act exclusively on `user/...` topics — a device-meta `.../g_op/...` result
# must not set or clear it (both share the `/g_op/` substring).


def _err_client() -> MIoTClient:
    client = MIoTClient.__new__(MIoTClient)
    client._mips_user_sub_error = None
    return client


def test_user_bind_subscribe_error_sets_flag():
    client = _err_client()
    client._on_mips_subscribe_error(("user/uid-1/g_op/bind", 0x87, "Not authorized"))
    assert client._mips_user_sub_error is not None
    assert "user/uid-1/g_op/bind" in client._mips_user_sub_error


def test_user_bind_subscribe_success_clears_flag():
    client = _err_client()
    client._mips_user_sub_error = "topic=user/uid-1/g_op/bind ... (after reconnect)"
    client._on_mips_subscribe_success("user/uid-1/g_op/bind")
    assert client._mips_user_sub_error is None


def test_device_meta_subscribe_success_does_not_clear_user_error():
    """Regression: a device-meta re-subscribe SUBACK shares the `/g_op/`
    substring but must NOT clear a genuine user-bind error — otherwise
    /mips_status would falsely report bind detection healthy."""
    client = _err_client()
    client._mips_user_sub_error = "topic=user/uid-1/g_op/bind 0x87 (after reconnect)"
    client._on_mips_subscribe_success("device/dev-1/g_op/rename")
    assert client._mips_user_sub_error == (
        "topic=user/uid-1/g_op/bind 0x87 (after reconnect)"
    )


def test_device_meta_subscribe_error_does_not_set_user_error():
    """Regression: a device-meta SUBACK rejection must not raise a user-bind
    health alarm (it has its own per-did retry path)."""
    client = _err_client()
    client._on_mips_subscribe_error(
        ("device/dev-1/g_op/hr_change", 0x87, "Not authorized")
    )
    assert client._mips_user_sub_error is None


def test_home_scene_subscribe_results_do_not_touch_user_error():
    """home-scene topics lack `/g_op/` entirely, but assert the user/ scoping
    holds for them too (defensive against a future filter rewrite)."""
    client = _err_client()
    client._on_mips_subscribe_error(("home/h1/scene/rename", 0x87, "Not authorized"))
    assert client._mips_user_sub_error is None
    client._mips_user_sub_error = "stale user error"
    client._on_mips_subscribe_success("home/h1/scene/edit")
    assert client._mips_user_sub_error == "stale user error"
