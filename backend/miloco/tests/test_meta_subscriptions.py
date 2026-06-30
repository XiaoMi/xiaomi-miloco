# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for MiotProxy._sync_meta_subscriptions.

Behavior under test:

* The per-device meta (rename/hr_change) subscription set is reconciled to
  the device list: new dids subscribed, removed dids unsubscribed, tracked
  set updated.
* A no-op sync issues no sub/unsub calls.
* A subscribe failure does not record the did as subscribed (so a later
  refresh retries it).

A bare MiotProxy is built via __new__ with only the attributes these methods
touch, so no MIoTClient / camera / OAuth stack is required.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.automation.schema import MiotEventMapping
from miloco.miot import client as client_module
from miloco.miot.client import MiotProxy


def _bare_proxy() -> MiotProxy:
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._subscribed_meta_dids = set()
    proxy._subscribed_property_dids = set()
    proxy._subscribed_event_dids = set()
    proxy._subscribed_scene_home_ids = set()
    proxy._device_info_dict = {}
    proxy._camera_info_dict = {}
    proxy._scene_info_dict = {}
    proxy._miot_client = AsyncMock()
    proxy._kv_repo = object()  # only passed through to is_home_allowed
    return proxy


@pytest.mark.asyncio
async def test_sync_subscribes_new_and_unsubscribes_removed():
    proxy = _bare_proxy()
    # Already subscribed to A and B; device list now has B and C.
    proxy._subscribed_meta_dids = {"A", "B"}
    proxy._device_info_dict = {
        "B": SimpleNamespace(did="B"),
        "C": SimpleNamespace(did="C"),
    }

    await proxy._sync_meta_subscriptions()

    proxy._miot_client.sub_device_meta_async.assert_awaited_once_with("C")
    proxy._miot_client.unsub_device_meta_async.assert_awaited_once_with("A")
    assert proxy._subscribed_meta_dids == {"B", "C"}


@pytest.mark.asyncio
async def test_sync_skips_dids_containing_slash():
    """Bridged sub-device dids with '/' (e.g. huami.32098/12264203) are never
    subscribed — the '/' breaks the topic and the broker rejects them."""
    proxy = _bare_proxy()
    proxy._device_info_dict = {
        "938000855": SimpleNamespace(did="938000855"),
        "huami.32098/12264203": SimpleNamespace(did="huami.32098/12264203"),
    }

    await proxy._sync_meta_subscriptions()

    proxy._miot_client.sub_device_meta_async.assert_awaited_once_with("938000855")
    assert proxy._subscribed_meta_dids == {"938000855"}


@pytest.mark.asyncio
async def test_sync_noop_when_already_in_sync():
    proxy = _bare_proxy()
    proxy._subscribed_meta_dids = {"A", "B"}
    proxy._device_info_dict = {
        "A": SimpleNamespace(did="A"),
        "B": SimpleNamespace(did="B"),
    }

    await proxy._sync_meta_subscriptions()

    proxy._miot_client.sub_device_meta_async.assert_not_awaited()
    proxy._miot_client.unsub_device_meta_async.assert_not_awaited()
    assert proxy._subscribed_meta_dids == {"A", "B"}


@pytest.mark.asyncio
async def test_sync_property_subscriptions_only_tracks_enabled_device_mappings(
    monkeypatch,
):
    proxy = _bare_proxy()
    proxy._subscribed_property_dids = {"OLD", "B"}
    proxy._device_info_dict = {
        "B": SimpleNamespace(did="B"),
        "C": SimpleNamespace(did="C"),
        "dev/skip": SimpleNamespace(did="dev/skip"),
    }

    mappings = [
        MiotEventMapping(
            source_type="device",
            source_id="B",
            enabled=True,
            event_kinds=["device_prop"],
        ),
        MiotEventMapping(
            source_type="device",
            source_id="C",
            enabled=True,
            event_kinds=["event.2.1"],
        ),
        MiotEventMapping(source_type="device", source_id="C", enabled=False),
        MiotEventMapping(source_type="device", source_id="dev/skip", enabled=True),
        MiotEventMapping(source_type="device", source_id="missing", enabled=True),
    ]
    fake_mgr = SimpleNamespace(
        automation_service=SimpleNamespace(list_mappings=lambda: mappings)
    )
    fake_manager = ModuleType("miloco.manager")
    fake_manager.get_manager = lambda: fake_mgr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "miloco.manager", fake_manager)

    await proxy._sync_property_subscriptions()

    proxy._miot_client.sub_device_property_changed_async.assert_not_awaited()
    proxy._miot_client.unsub_device_property_changed_async.assert_awaited_once_with(
        "OLD"
    )
    assert proxy._subscribed_property_dids == {"B"}


@pytest.mark.asyncio
async def test_sync_event_subscriptions_only_tracks_device_event_mappings(monkeypatch):
    proxy = _bare_proxy()
    proxy._subscribed_event_dids = {"OLD"}
    proxy._device_info_dict = {
        "B": SimpleNamespace(did="B"),
        "C": SimpleNamespace(did="C"),
        "dev/skip": SimpleNamespace(did="dev/skip"),
    }

    mappings = [
        MiotEventMapping(
            source_type="device",
            source_id="B",
            enabled=True,
            event_kinds=["event.2.1"],
        ),
        MiotEventMapping(
            source_type="device",
            source_id="C",
            enabled=True,
            event_kinds=["device_prop"],
        ),
        MiotEventMapping(
            source_type="device",
            source_id="dev/skip",
            enabled=True,
            event_kinds=["event.2.1"],
        ),
    ]
    fake_mgr = SimpleNamespace(
        automation_service=SimpleNamespace(list_mappings=lambda: mappings)
    )
    fake_manager = ModuleType("miloco.manager")
    fake_manager.get_manager = lambda: fake_mgr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "miloco.manager", fake_manager)

    await proxy._sync_event_subscriptions()

    proxy._miot_client.sub_device_event_occurred_async.assert_awaited_once_with("B")
    proxy._miot_client.unsub_device_event_occurred_async.assert_awaited_once_with(
        "OLD"
    )
    assert proxy._subscribed_event_dids == {"B"}


@pytest.mark.asyncio
async def test_sync_property_subscriptions_adds_new_enabled_mapping(monkeypatch):
    proxy = _bare_proxy()
    proxy._device_info_dict = {"C": SimpleNamespace(did="C")}

    mappings = [MiotEventMapping(source_type="device", source_id="C", enabled=True)]
    fake_mgr = SimpleNamespace(
        automation_service=SimpleNamespace(list_mappings=lambda: mappings)
    )
    fake_manager = ModuleType("miloco.manager")
    fake_manager.get_manager = lambda: fake_mgr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "miloco.manager", fake_manager)

    await proxy._sync_property_subscriptions()

    proxy._miot_client.sub_device_property_changed_async.assert_awaited_once_with("C")
    proxy._miot_client.unsub_device_property_changed_async.assert_not_awaited()
    assert proxy._subscribed_property_dids == {"C"}


@pytest.mark.asyncio
async def test_sync_subscribe_failure_keeps_did_untracked():
    proxy = _bare_proxy()
    proxy._device_info_dict = {"C": SimpleNamespace(did="C")}
    proxy._miot_client.sub_device_meta_async = AsyncMock(
        side_effect=RuntimeError("ACL rejected")
    )

    # Must not raise — failure only logs.
    await proxy._sync_meta_subscriptions()

    # Failed subscribe must not be recorded as subscribed.
    assert proxy._subscribed_meta_dids == set()


def test_collect_home_ids_unions_devices_cameras_scenes():
    proxy = _bare_proxy()
    proxy._device_info_dict = {"d1": SimpleNamespace(home_id="H1")}
    proxy._camera_info_dict = {"c1": SimpleNamespace(home_id="H2")}
    proxy._scene_info_dict = {"s1": SimpleNamespace(home_id="H3")}
    # A None home_id is ignored.
    proxy._device_info_dict["d2"] = SimpleNamespace(home_id=None)

    assert proxy._collect_home_ids() == {"H1", "H2", "H3"}


@pytest.mark.asyncio
async def test_sync_scene_subscribes_new_and_unsubscribes_removed(monkeypatch):
    monkeypatch.setattr(client_module, "is_home_allowed", lambda _kv, _h: True)
    proxy = _bare_proxy()
    # Already subscribed to H1 and H2; home set now (from devices) is H2 + H3.
    proxy._subscribed_scene_home_ids = {"H1", "H2"}
    proxy._device_info_dict = {
        "d1": SimpleNamespace(home_id="H2"),
        "d2": SimpleNamespace(home_id="H3"),
    }

    await proxy._sync_scene_subscriptions()

    proxy._miot_client.sub_home_scene_async.assert_awaited_once_with("H3")
    proxy._miot_client.unsub_home_scene_async.assert_awaited_once_with("H1")
    assert proxy._subscribed_scene_home_ids == {"H2", "H3"}


@pytest.mark.asyncio
async def test_sync_scene_noop_when_already_in_sync(monkeypatch):
    monkeypatch.setattr(client_module, "is_home_allowed", lambda _kv, _h: True)
    proxy = _bare_proxy()
    proxy._subscribed_scene_home_ids = {"H1"}
    proxy._device_info_dict = {"d1": SimpleNamespace(home_id="H1")}

    await proxy._sync_scene_subscriptions()

    proxy._miot_client.sub_home_scene_async.assert_not_awaited()
    proxy._miot_client.unsub_home_scene_async.assert_not_awaited()
    assert proxy._subscribed_scene_home_ids == {"H1"}


@pytest.mark.asyncio
async def test_sync_scene_skips_out_of_scope_homes(monkeypatch):
    """Out-of-scope homes are never subscribed; a home leaving scope is
    unsubscribed. Unlike device-meta (account-wide for move-into-scope),
    scene subs follow the managed-home whitelist."""
    allowed = {"H_OK"}
    monkeypatch.setattr(
        client_module, "is_home_allowed", lambda _kv, h: h in allowed
    )
    proxy = _bare_proxy()
    # H_OLD was managed and subscribed; now only H_OK is in scope. Devices
    # span a managed (H_OK) and an out-of-scope (H_DENY) home.
    proxy._subscribed_scene_home_ids = {"H_OK", "H_OLD"}
    proxy._device_info_dict = {
        "d1": SimpleNamespace(home_id="H_OK"),
        "d2": SimpleNamespace(home_id="H_DENY"),
    }

    await proxy._sync_scene_subscriptions()

    # H_DENY never subscribed; H_OLD (now out of scope) unsubscribed.
    proxy._miot_client.sub_home_scene_async.assert_not_awaited()
    proxy._miot_client.unsub_home_scene_async.assert_awaited_once_with("H_OLD")
    assert proxy._subscribed_scene_home_ids == {"H_OK"}
