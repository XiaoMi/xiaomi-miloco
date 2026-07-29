# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for device property push: MIoTClient.sub_device_prop_async tracking
and the mips_cloud `properties_changed` decoder.

Tracking follows the same contract as sub_device_meta_async:

* A successful subscribe (mips connected) records the did.
* A FAILED subscribe must NOT record it — otherwise the idempotency guard
  would short-circuit the proxy-level retry in _sync_prop_subscriptions.
* While mips is disconnected only the intent is recorded (replayed at setup).
* Already-tracked dids are a no-op (idempotent).

The decoder is defensive: the broker payload schema follows the HA
`xiaomi_home` convention (`params` list of {did,siid,piid,value}) but is not
formally documented, so bare-dict payloads are accepted, entries without
integer siid+piid are skipped, and zero decodable entries → None (dropped).
"""

from __future__ import annotations

import json

from unittest.mock import AsyncMock

import pytest
from miot.client import MIoTClient
from miot.mips_cloud import MIoTMipsCloud


class _FakeMips:
    def __init__(self, *, connected: bool = True, fail: bool = False) -> None:
        self.is_connected = connected
        self._fail = fail
        self.sub_device_prop_async = AsyncMock(side_effect=self._maybe_fail)
        self.unsub_device_prop_async = AsyncMock()

    async def _maybe_fail(self, *args, **kwargs) -> None:
        if self._fail:
            raise RuntimeError("SUBACK rejected")


def _bare_client(mips: _FakeMips | None) -> MIoTClient:
    client = MIoTClient.__new__(MIoTClient)
    client._prop_sub_dids = set()
    client._mips_cloud = mips
    client._callback_device_prop_changed = None
    return client


# ------------------------------------------------------------------- tracking


@pytest.mark.asyncio
async def test_sub_prop_success_records_did():
    mips = _FakeMips(connected=True)
    client = _bare_client(mips)
    await client.sub_device_prop_async("did-1")
    assert client._prop_sub_dids == {"did-1"}
    mips.sub_device_prop_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_sub_prop_failure_does_not_record_did():
    mips = _FakeMips(connected=True, fail=True)
    client = _bare_client(mips)
    with pytest.raises(RuntimeError):
        await client.sub_device_prop_async("did-1")
    assert client._prop_sub_dids == set()


@pytest.mark.asyncio
async def test_sub_prop_disconnected_records_intent_without_broker_call():
    mips = _FakeMips(connected=False)
    client = _bare_client(mips)
    await client.sub_device_prop_async("did-1")
    assert client._prop_sub_dids == {"did-1"}
    mips.sub_device_prop_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_sub_prop_idempotent():
    mips = _FakeMips(connected=True)
    client = _bare_client(mips)
    await client.sub_device_prop_async("did-1")
    await client.sub_device_prop_async("did-1")
    assert mips.sub_device_prop_async.await_count == 1


@pytest.mark.asyncio
async def test_unsub_prop_discards_and_forwards():
    mips = _FakeMips(connected=True)
    client = _bare_client(mips)
    await client.sub_device_prop_async("did-1")
    await client.unsub_device_prop_async("did-1")
    assert client._prop_sub_dids == set()
    mips.unsub_device_prop_async.assert_awaited_once()


# -------------------------------------------------------------------- decoder


def _decode(topic: str, payload: bytes):
    return MIoTMipsCloud._make_prop_decoder()(topic, payload)


def test_decoder_params_list():
    payload = json.dumps(
        {
            "id": 1,
            "method": "properties_changed",
            "params": [
                {"did": "d1", "siid": 2, "piid": 1, "value": True},
                {"did": "d1", "siid": 2, "piid": 4, "value": 26},
            ],
        }
    ).encode()
    evt = _decode("device/d1/up/properties_changed", payload)
    assert evt is not None and evt.did == "d1"
    assert [(c.siid, c.piid, c.value) for c in evt.changes] == [
        (2, 1, True),
        (2, 4, 26),
    ]


def test_decoder_bare_dict_payload():
    evt = _decode(
        "device/d1/up/properties_changed",
        json.dumps({"siid": 3, "piid": 2, "value": "lo"}).encode(),
    )
    assert evt is not None
    assert (evt.changes[0].siid, evt.changes[0].piid) == (3, 2)


def test_decoder_skips_entries_without_siid_piid():
    payload = json.dumps(
        {"params": [{"nope": 1}, {"did": "d1", "siid": 2, "piid": 1, "value": 0}]}
    ).encode()
    evt = _decode("device/d1/up/properties_changed", payload)
    assert evt is not None and len(evt.changes) == 1


def test_decoder_drops_garbage_and_empty():
    assert _decode("device/d1/up/properties_changed", b"not json") is None
    assert (
        _decode(
            "device/d1/up/properties_changed",
            json.dumps({"params": [{"nope": 1}]}).encode(),
        )
        is None
    )


def test_decoder_ignores_other_topics():
    assert _decode("device/d1/state/online", b"{}") is None
