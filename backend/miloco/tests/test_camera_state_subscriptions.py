# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for MiotProxy cloud online/offline state handling.

Behavior under test:

* _sync_camera_state_subscriptions reconciles the per-camera cloud state
  (online/offline) subscription set to the camera list: new dids subscribed,
  removed dids unsubscribed, tracked set updated.
* A no-op sync issues no sub/unsub calls.
* A subscribe failure does not record the did as subscribed (so a later
  refresh retries it).
* _on_camera_state_changed_event updates _camera_info_dict[did].online
  directly from the event (online→True / offline→False) and forwards to the
  trailing-reconciliation listener. Non-camera devices are ignored.

A bare MiotProxy is built via __new__ with only the attributes these methods
touch, so no MIoTClient / camera / OAuth stack is required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.miot.client import MiotProxy
from miot.types import MIoTDeviceStateEvent


def _bare_proxy() -> MiotProxy:
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._subscribed_state_dids = set()
    proxy._camera_info_dict = {}
    proxy._miot_client = AsyncMock()
    proxy._camera_state_listener = SimpleNamespace(on_event=AsyncMock())
    return proxy


def _cam(did: str, online: bool = False) -> SimpleNamespace:
    return SimpleNamespace(did=did, online=online)


def _state_evt(did: str, event: str = "online") -> MIoTDeviceStateEvent:
    return MIoTDeviceStateEvent(
        did=did, event=event, raw={"device_id": did, "event": event}
    )


# ----------------------------------------------------- _sync_camera_state_subscriptions


@pytest.mark.asyncio
async def test_sync_subscribes_new_and_unsubscribes_removed():
    proxy = _bare_proxy()
    # Already subscribed to A and B; camera list now has B and C.
    proxy._subscribed_state_dids = {"A", "B"}
    proxy._camera_info_dict = {"B": _cam("B"), "C": _cam("C")}

    await proxy._sync_camera_state_subscriptions()

    proxy._miot_client.sub_device_state_async.assert_awaited_once_with("C")
    proxy._miot_client.unsub_device_state_async.assert_awaited_once_with("A")
    assert proxy._subscribed_state_dids == {"B", "C"}


@pytest.mark.asyncio
async def test_sync_skips_dids_containing_slash():
    """Bridged sub-device dids with '/' are never subscribed — the '/' breaks
    the topic and the broker rejects them (same rationale as meta subs)."""
    proxy = _bare_proxy()
    proxy._camera_info_dict = {
        "938000855": _cam("938000855"),
        "huami.32098/12264203": _cam("huami.32098/12264203"),
    }

    await proxy._sync_camera_state_subscriptions()

    proxy._miot_client.sub_device_state_async.assert_awaited_once_with("938000855")
    assert proxy._subscribed_state_dids == {"938000855"}


@pytest.mark.asyncio
async def test_sync_noop_when_already_in_sync():
    proxy = _bare_proxy()
    proxy._subscribed_state_dids = {"A", "B"}
    proxy._camera_info_dict = {"A": _cam("A"), "B": _cam("B")}

    await proxy._sync_camera_state_subscriptions()

    proxy._miot_client.sub_device_state_async.assert_not_awaited()
    proxy._miot_client.unsub_device_state_async.assert_not_awaited()
    assert proxy._subscribed_state_dids == {"A", "B"}


@pytest.mark.asyncio
async def test_sync_subscribe_failure_keeps_did_untracked():
    proxy = _bare_proxy()
    proxy._camera_info_dict = {"C": _cam("C")}
    proxy._miot_client.sub_device_state_async = AsyncMock(
        side_effect=RuntimeError("ACL rejected")
    )

    # Must not raise — failure only logs.
    await proxy._sync_camera_state_subscriptions()

    # Failed subscribe must not be recorded as subscribed.
    assert proxy._subscribed_state_dids == set()


# ----------------------------------------- _on_camera_state_changed_event


@pytest.mark.asyncio
async def test_online_event_sets_camera_online_true():
    proxy = _bare_proxy()
    proxy._camera_info_dict = {"cam-1": _cam("cam-1", online=False)}

    await proxy._on_camera_state_changed_event(_state_evt("cam-1", "online"))

    assert proxy._camera_info_dict["cam-1"].online is True
    proxy._camera_state_listener.on_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_offline_event_sets_camera_online_false():
    proxy = _bare_proxy()
    proxy._camera_info_dict = {"cam-1": _cam("cam-1", online=True)}

    await proxy._on_camera_state_changed_event(_state_evt("cam-1", "offline"))

    assert proxy._camera_info_dict["cam-1"].online is False
    proxy._camera_state_listener.on_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_state_event_for_unknown_did_is_ignored():
    """A state event for a device that is not a camera (or not yet cached)
    must not raise and must still forward to the reconciliation listener —
    the burst itself may warrant a reconciliation even if this did is a no-op
    for the camera cache."""
    proxy = _bare_proxy()
    proxy._camera_info_dict = {"cam-1": _cam("cam-1", online=False)}

    await proxy._on_camera_state_changed_event(_state_evt("non-camera", "online"))

    # cam-1 untouched.
    assert proxy._camera_info_dict["cam-1"].online is False
    proxy._camera_state_listener.on_event.assert_awaited_once()
