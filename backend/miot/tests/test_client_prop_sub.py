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

The decoder is defensive: real pushes carry ONE change per message with
`params` as a bare dict, on topic `.../properties_changed/{siid}/{piid}`;
the HA `xiaomi_home` convention documents a `params` list on the leaf topic.
Neither is formally specified by the broker, so both shapes are accepted,
entries without integer siid+piid are skipped, and zero decodable entries →
None (dropped).
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


# ------------------------------------------------- real broker payload shape
#
# Captured 2026-07-29 from cn-ha.mqtt.io.mi.com against a live account: the
# topic carries siid/piid and `params` is a single bare dict, not a list.


def test_decoder_real_push_shape():
    """Topic with /{siid}/{piid} suffix + bare-dict params (production shape)."""
    payload = json.dumps(
        {
            "method": "properties_changed",
            "params": {
                "did": "436264078",
                "siid": 2,
                "piid": 1,
                "value": False,
                "parent": "70009373680",
            },
        }
    ).encode()
    evt = _decode("device/436264078/up/properties_changed/2/1", payload)
    assert evt is not None and evt.did == "436264078"
    assert [(c.siid, c.piid, c.value) for c in evt.changes] == [(2, 1, False)]


@pytest.mark.parametrize(
    "value",
    [True, False, 26.0, 1140, "0,100,1,1", None],
)
def test_decoder_real_push_value_types(value):
    """Live traffic mixes bool / int / float / str / null values."""
    payload = json.dumps(
        {"method": "properties_changed", "params": {"siid": 12, "piid": 3, "value": value}}
    ).encode()
    evt = _decode("device/d1/up/properties_changed/12/3", payload)
    assert evt is not None and evt.changes[0].value == value


def test_decoder_accepts_deep_topic_but_not_sibling_subtree():
    """`properties_changed/#` must decode; `event_occured` must not."""
    payload = json.dumps({"params": {"siid": 9, "piid": 5, "value": 318.7}}).encode()
    assert _decode("device/d1/up/properties_changed/9/5", payload) is not None
    assert _decode("device/d1/up/event_occured/9/8", payload) is None


# ----------------------------------------------------------- subscribe topic


@pytest.mark.asyncio
async def test_subscribe_uses_wildcard_subtree_topic():
    """The leaf-only filter is rejected by the broker ACL with 0x87 and matches
    no published topic — the subtree wildcard is mandatory (verified against
    72 devices, 2026-07-29). Guard against a silent regression to the leaf.
    """
    from unittest.mock import AsyncMock as _AsyncMock

    mips = MIoTMipsCloud.__new__(MIoTMipsCloud)
    mips._subscribe_async = _AsyncMock()
    mips._unsubscribe_async = _AsyncMock()
    await MIoTMipsCloud.sub_device_prop_async(mips, "did-1", lambda _evt: None)
    topic = mips._subscribe_async.await_args.args[0]
    assert topic == "device/did-1/up/properties_changed/#"

    await MIoTMipsCloud.unsub_device_prop_async(mips, "did-1")
    assert (
        mips._unsubscribe_async.await_args.args[0]
        == "device/did-1/up/properties_changed/#"
    )
