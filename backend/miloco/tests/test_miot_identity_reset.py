# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for the local central-hub identity reset in MiotProxy.

Exercises `_ensure_virtual_did` / `delete_central_cert_async` /
`reset_central_identity_async` on a bare MiotProxy (bypassing the heavy
__init__) with an in-memory KV and a fake SDK client. Asserts logout / account
switch drops the cert + rotates the virtual did, while the cloud identity
(device uuid) is left untouched.
"""

from __future__ import annotations

import pytest
from miloco.database.kv_repo import SystemConfigKeys
from miloco.miot.client import MiotProxy

DID_KEY = SystemConfigKeys.MIOT_VIRTUAL_DID_KEY
UUID_KEY = SystemConfigKeys.DEVICE_UUID_KEY


class FakeKV:
    def __init__(self, initial=None):
        self._d = dict(initial or {})

    def get(self, key, default_value=None):
        return self._d.get(key, default_value)

    def set(self, key, value):
        self._d[key] = value
        return True

    def delete(self, key):
        self._d.pop(key, None)
        return True


class FakeClient:
    def __init__(self):
        self.delete_calls = 0
        self.scope_calls = 0
        self._central_hub_virtual_did: str | None = None
        self.teardown_calls = 0

    @property
    def central_hub_virtual_did(self) -> str | None:
        return self._central_hub_virtual_did

    @central_hub_virtual_did.setter
    def central_hub_virtual_did(self, value: str | None) -> None:
        self._central_hub_virtual_did = value

    async def delete_central_cert_async(self):
        self.delete_calls += 1

    async def refresh_central_hub_scope_async(self):
        self.scope_calls += 1

    async def teardown_central_hub_async(self):
        self.teardown_calls += 1


def _proxy(kv, client=None):
    p = object.__new__(MiotProxy)  # bypass heavy __init__
    p._kv_repo = kv
    p._miot_client = client
    return p


def test_ensure_virtual_did_generates_and_persists_stable():
    kv = FakeKV()
    p = _proxy(kv)
    did1 = p._ensure_virtual_did()
    assert did1 and did1.isdigit()  # str(secrets.randbits(64))
    assert kv.get(DID_KEY) == did1  # persisted
    # Second call returns the same persisted value (stable across restarts).
    assert p._ensure_virtual_did() == did1


def test_ensure_virtual_did_returns_existing():
    kv = FakeKV({DID_KEY: "  persisted-did  "})
    p = _proxy(kv)
    assert p._ensure_virtual_did() == "persisted-did"  # trimmed


@pytest.mark.asyncio
async def test_delete_central_cert_noop_without_client():
    p = _proxy(FakeKV(), client=None)
    await p.delete_central_cert_async()  # must not raise


@pytest.mark.asyncio
async def test_refresh_central_hub_scope_delegates():
    client = FakeClient()
    p = _proxy(FakeKV(), client)
    await p.refresh_central_hub_scope_async()
    assert client.scope_calls == 1


@pytest.mark.asyncio
async def test_refresh_central_hub_scope_noop_without_client():
    p = _proxy(FakeKV(), client=None)
    await p.refresh_central_hub_scope_async()  # must not raise


@pytest.mark.asyncio
async def test_reset_rotates_did_and_deletes_cert_keeps_uuid():
    kv = FakeKV({DID_KEY: "old-did-123", UUID_KEY: "cloud-uuid-xyz"})
    client = FakeClient()
    p = _proxy(kv, client)
    p._virtual_did = "old-did-123"

    await p.reset_central_identity_async()

    assert client.delete_calls == 1  # SDK cert removed
    new_did = kv.get(DID_KEY)
    assert new_did and new_did != "old-did-123"  # rotated + re-persisted
    assert p._virtual_did == new_did  # in-memory updated to match KV
    assert client.teardown_calls == 1  # hub torn down for identity rotation
    assert client.central_hub_virtual_did == new_did  # new did injected
    # Cloud identity is untouched.
    assert kv.get(UUID_KEY) == "cloud-uuid-xyz"
