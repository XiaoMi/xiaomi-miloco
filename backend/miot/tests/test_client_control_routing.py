# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for the local↔cloud control routing in `miot.client.MIoTClient`.

Exercises `set_props_async` / `get_props_async` / `action_async` in isolation
by attaching a fake central hub + fake http client to a bare MIoTClient
instance (no OAuth / storage / network). Asserts the failure-classification
matrix:

  - not locally controllable / in cooldown  → cloud
  - local timeout (set/action)               → error, NO cloud, cooldown armed
  - local timeout (get)                      → cloud (idempotent), cooldown armed
  - other local failure                      → cloud, NO cooldown
  - success / device-level rejection         → returned as-is (no cloud)
  - cloud batch raises with local survivors  → local results kept, cloud=error
"""

from __future__ import annotations

import pytest
from miot.client import MIoTClient
from miot.error import MIoTErrorCode
from miot.types import (
    MIoTActionParam,
    MIoTGetPropertyParam,
    MIoTSetPropertyParam,
)

_TIMEOUT = MIoTErrorCode.CODE_TIMEOUT.value
_INTERNAL = MIoTErrorCode.CODE_INTERNAL_ERROR.value


class FakeCentralHub:
    """Minimal stand-in for CentralHubManager used by the routing methods."""

    def __init__(self, controllable=None, cooldown=None, responses=None):
        self.enabled = True
        self._controllable = set(controllable or [])
        self._cooldown = set(cooldown or [])
        # did -> value/dict returned by set/get/action, or an Exception to raise
        self._responses = responses or {}
        self.cooled = []  # dids passed to note_local_failure
        self.local_calls = []  # dids that actually hit a local RPC

    def can_control(self, did):
        return did in self._controllable

    def in_local_cooldown(self, did):
        return did in self._cooldown

    def note_local_failure(self, did):
        self.cooled.append(did)

    def _resolve(self, did):
        self.local_calls.append(did)
        r = self._responses.get(did)
        if isinstance(r, Exception):
            raise r
        return r

    async def set_prop_async(self, did, siid, piid, value):
        return self._resolve(did)

    async def get_prop_async(self, did, siid, piid):
        return self._resolve(did)

    async def action_async(self, did, siid, aiid, in_list):
        return self._resolve(did)


class FakeHttpClient:
    def __init__(self, raise_exc=None):
        self._raise = raise_exc
        self.set_calls = []
        self.get_calls = []
        self.action_calls = []

    async def set_props_async(self, params):
        self.set_calls.append(list(params))
        if self._raise:
            raise self._raise
        return [
            {"did": p.did, "siid": p.siid, "piid": p.piid, "code": 0} for p in params
        ]

    async def get_props_async(self, params):
        self.get_calls.append(list(params))
        if self._raise:
            raise self._raise
        return [
            {"did": p.did, "siid": p.siid, "piid": p.piid, "value": "cloud", "code": 0}
            for p in params
        ]

    async def action_async(self, param):
        self.action_calls.append(param)
        if self._raise:
            raise self._raise
        return {"did": param.did, "siid": param.siid, "aiid": param.aiid, "code": 0}


def _make_client(ch, http):
    client = object.__new__(MIoTClient)  # bypass heavy __init__
    client._central_hub = ch
    client._http_client = http
    return client


def _sp(did, value=1):
    return MIoTSetPropertyParam(did=did, siid=2, piid=1, value=value)


def _gp(did):
    return MIoTGetPropertyParam(did=did, siid=2, piid=1)


# ------------------------------------------------------------------ set_props


@pytest.mark.asyncio
async def test_set_not_controllable_goes_cloud():
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0
    assert ch.local_calls == []  # never tried local
    assert len(http.set_calls) == 1


@pytest.mark.asyncio
async def test_set_in_cooldown_goes_cloud_without_local():
    ch = FakeCentralHub(controllable=["A"], cooldown=["A"])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    await client.set_props_async([_sp("A")])
    assert ch.local_calls == []
    assert len(http.set_calls) == 1


@pytest.mark.asyncio
async def test_set_timeout_no_cloud_and_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _TIMEOUT}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == _TIMEOUT
    assert http.set_calls == []  # NO cloud retry on timeout
    assert ch.cooled == ["A"]  # cooldown armed


@pytest.mark.asyncio
async def test_set_other_failure_falls_back_cloud_no_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _INTERNAL}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0  # cloud result
    assert len(http.set_calls) == 1
    assert ch.cooled == []  # non-timeout does not cool down


@pytest.mark.asyncio
async def test_set_exception_falls_back_cloud_no_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": RuntimeError("boom")})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0
    assert len(http.set_calls) == 1
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_set_success_returned_as_is_no_cloud():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": 1}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0  # 1 normalized to 0
    assert http.set_calls == []


@pytest.mark.asyncio
async def test_set_device_rejection_returned_as_is():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": -704002000}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == -704002000
    assert http.set_calls == []


@pytest.mark.asyncio
async def test_set_mixed_batch_preserves_order():
    # A local-success, B not controllable (cloud), C local-timeout (error).
    ch = FakeCentralHub(
        controllable=["A", "C"],
        responses={"A": {"code": 0}, "C": {"code": _TIMEOUT}},
    )
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A"), _sp("B"), _sp("C")])
    assert [r["did"] for r in res] == ["A", "B", "C"]
    assert res[0]["code"] == 0
    assert res[1]["code"] == 0  # cloud
    assert res[2]["code"] == _TIMEOUT
    assert [p.did for p in http.set_calls[0]] == ["B"]  # only B went to cloud


@pytest.mark.asyncio
async def test_set_cloud_failure_keeps_local_survivors():
    # A succeeds locally, B needs cloud but cloud raises → A kept, B error.
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": 0}})
    http = FakeHttpClient(raise_exc=RuntimeError("cloud down"))
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A"), _sp("B")])
    assert res[0]["code"] == 0  # local survivor
    assert res[1]["code"] == _INTERNAL  # cloud error fill


@pytest.mark.asyncio
async def test_set_all_cloud_failure_raises():
    # Nothing handled locally → preserve cloud-only contract (raise).
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient(raise_exc=RuntimeError("cloud down"))
    client = _make_client(ch, http)
    with pytest.raises(RuntimeError):
        await client.set_props_async([_sp("A"), _sp("B")])


# ------------------------------------------------------------------ get_props


@pytest.mark.asyncio
async def test_get_local_success():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"value": 42, "code": 0}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["value"] == 42
    assert res[0]["code"] == 0
    assert http.get_calls == []


@pytest.mark.asyncio
async def test_get_timeout_falls_back_cloud_and_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _TIMEOUT}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["value"] == "cloud"  # read fell back
    assert len(http.get_calls) == 1
    assert ch.cooled == ["A"]  # get timeout still cools down


@pytest.mark.asyncio
async def test_get_fast_error_cloud_no_cooldown():
    """get_prop 快速错误(网关拒绝,非超时)→ 云端、不冷却。"""
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": -10004}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["value"] == "cloud"
    assert ch.cooled == []  # 快速错误不冷却


@pytest.mark.asyncio
async def test_get_exception_falls_back_cloud_no_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": RuntimeError("x")})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["value"] == "cloud"
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_get_cloud_failure_keeps_local_survivors():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"value": 7, "code": 0}})
    http = FakeHttpClient(raise_exc=RuntimeError("cloud down"))
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A"), _gp("B")])
    assert res[0]["value"] == 7
    assert res[1]["code"] == _INTERNAL


# -------------------------------------------------------------------- action


def _ap(did):
    return MIoTActionParam(did=did, siid=2, aiid=1, in_=[])


@pytest.mark.asyncio
async def test_action_timeout_no_cloud_and_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _TIMEOUT}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == _TIMEOUT
    assert http.action_calls == []
    assert ch.cooled == ["A"]


@pytest.mark.asyncio
async def test_action_other_failure_falls_back_cloud():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _INTERNAL}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == 0  # cloud
    assert len(http.action_calls) == 1
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_action_success_as_is():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": 0, "out": []}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == 0
    assert http.action_calls == []


@pytest.mark.asyncio
async def test_action_not_controllable_goes_cloud():
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    await client.action_async(_ap("A"))
    assert ch.local_calls == []
    assert len(http.action_calls) == 1


@pytest.mark.asyncio
async def test_central_hub_disabled_uses_cloud_directly():
    ch = FakeCentralHub(controllable=["A"])
    ch.enabled = False
    http = FakeHttpClient()
    client = _make_client(ch, http)
    await client.set_props_async([_sp("A")])
    assert ch.local_calls == []
    assert len(http.set_calls) == 1


# --------------------------------------------------------------- edge cases


@pytest.mark.asyncio
async def test_set_empty_params():
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    assert await client.set_props_async([]) == []
    assert http.set_calls == []  # nothing to route


@pytest.mark.asyncio
async def test_get_empty_params():
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    assert await client.get_props_async([]) == []


class ShortHttpClient(FakeHttpClient):
    """Cloud returns fewer results than requested (broker dropped some)."""

    async def set_props_async(self, params):
        self.set_calls.append(list(params))
        return [{"did": params[0].did, "siid": params[0].siid, "piid": params[0].piid, "code": 0}]


@pytest.mark.asyncio
async def test_set_short_cloud_result_backfilled_with_error():
    # Two not-locally-controllable dids → both cloud; cloud returns only 1
    # result → the missing slot must be filled with an internal-error code,
    # never left as None.
    ch = FakeCentralHub(controllable=[])
    http = ShortHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A"), _sp("B")])
    assert len(res) == 2
    assert res[0]["code"] == 0
    assert res[1]["code"] == _INTERNAL  # backfilled, not None
    assert None not in res
