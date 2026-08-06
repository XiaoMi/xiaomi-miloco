# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for `miot.central_hub.CentralHubManager` coordination logic.

No real broker / mDNS / cloud. A real MIoTStorage(tmpdir) satisfies the cert
object's constructor (no I/O at construction); http + local clients are fakes.
Covers: can_control/can_push truth table, cooldown window, device-table
reconcile + callback, owned-group filtering / home-scope / fail-open, and the
unowned-gateway refresh throttle.
"""

from __future__ import annotations

import tempfile

import miot.central_hub as ch_mod
import pytest
from miot.central_hub import CentralHubManager
from miot.mdns import MdnsServiceState
from miot.storage import MIoTStorage
from miot.types import MIoTHomeInfo, MipsConnectionError


class _FakeLocalClient:
    def __init__(self, group_id, connected=True, host="10.0.0.9", dev_list=None):
        self._group_id = group_id
        self._connected = connected
        self._host = host
        self._dev_list = dev_list or {}

    @property
    def group_id(self):
        return self._group_id

    @property
    def host(self):
        return self._host

    @property
    def is_connected(self):
        return self._connected

    async def get_dev_list_async(self):
        return self._dev_list

    async def deinit_async(self):
        self._connected = False


def _home(home_id, gid):
    return MIoTHomeInfo(
        home_id=home_id,
        home_name=f"home-{home_id}",
        share_home=False,
        uid="u1",
        room_list={},
        create_ts=0,
        dids=[],
        group_id=gid,
    )


class _FakeHttp:
    def __init__(self, homes):
        self._homes = homes
        self.get_homes_calls = 0
        self.central_cert_calls = []

    async def get_homes_async(self):
        self.get_homes_calls += 1
        return self._homes

    async def get_central_cert_async(self, csr):
        self.central_cert_calls.append(csr)
        return "SIGNED-CERT-PEM"


class _FakeCert:
    """Controls the remaining-time sequence so __refresh_cert's re-sign branch
    can be driven without real crypto."""

    def __init__(self, remaining_seq):
        self._seq = list(remaining_seq)

    async def verify_ca_cert_async(self):
        return True

    async def user_cert_remaining_time_async(self, did=None):
        return self._seq.pop(0) if self._seq else 10_000_000

    async def load_user_key_async(self):
        return None

    def gen_user_key(self):
        return "KEY"

    async def update_user_key_async(self, key):
        return True

    def gen_user_csr(self, key, did=None):
        return "CSR"

    async def update_user_cert_async(self, crt):
        return True


def _make(http=None, home_ids_provider=None, loop=None):
    storage = MIoTStorage(tempfile.mkdtemp(prefix="ch_"), loop=loop)
    return CentralHubManager(
        storage=storage,
        http_client=http or _FakeHttp({}),
        uid="u1",
        cloud_server="cn",
        home_ids_provider=home_ids_provider,
        loop=loop,
    )


# ------------------------------------------------------------- can_control/push


@pytest.mark.asyncio
async def test_can_control_truth_table():
    m = _make()
    m._clients["g"] = _FakeLocalClient("g", connected=True)
    base = {"group_id": "g", "online": True, "specv2_access": True, "push_available": True}
    m._dev_table["d"] = dict(base)
    assert m.can_control("d") is True

    m._dev_table["d"] = {**base, "online": False}
    assert m.can_control("d") is False

    m._dev_table["d"] = {**base, "specv2_access": False}
    assert m.can_control("d") is False

    m._dev_table["d"] = dict(base)
    m._clients["g"] = _FakeLocalClient("g", connected=False)
    assert m.can_control("d") is False

    assert m.can_control("unknown") is False


@pytest.mark.asyncio
async def test_can_push_requires_push_available_and_live_client():
    m = _make()
    m._clients["g"] = _FakeLocalClient("g", connected=True)
    base = {"group_id": "g", "online": True, "specv2_access": True, "push_available": True}
    m._dev_table["d"] = dict(base)
    assert m.can_push("d") is True
    m._dev_table["d"] = {**base, "push_available": False}
    assert m.can_push("d") is False


# ----------------------------------------------------------------- cooldown


@pytest.mark.asyncio
async def test_cooldown_window_and_self_heal(monkeypatch):
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    m = _make()
    assert m.in_local_cooldown("d") is False
    m.note_local_failure("d")
    assert m.in_local_cooldown("d") is True
    # advance past the window → heals and forgets the did
    fake_now["t"] += ch_mod._LOCAL_COOLDOWN_SEC + 1
    assert m.in_local_cooldown("d") is False
    assert "d" not in m._local_cooldown


# --------------------------------------------------- device-table reconcile


@pytest.mark.asyncio
async def test_refresh_dev_list_add_then_remove_fires_callback():
    m = _make()
    events: list[tuple[list, list]] = []

    async def on_change(added, removed):
        events.append((added, removed))

    m.on_dev_list_changed = on_change
    refresh = m._CentralHubManager__refresh_dev_list  # name-mangled private

    cli = _FakeLocalClient(
        "g",
        dev_list={
            "d1": {"online": True, "specv2_access": True, "push_available": True},
            "d2": {"online": True, "specv2_access": False, "push_available": False},
        },
    )
    await refresh(cli)
    assert set(m._dev_table) == {"d1", "d2"}
    assert events[-1][0] and set(events[-1][0]) == {"d1", "d2"}  # added
    assert events[-1][1] == []  # removed

    # d2 disappears under the same gateway → removed
    cli._dev_list = {"d1": {"online": True, "specv2_access": True, "push_available": True}}
    await refresh(cli)
    assert set(m._dev_table) == {"d1"}
    assert events[-1][1] == ["d2"]


# ------------------------------------------------- owned-group / home-scope


@pytest.mark.asyncio
async def test_owned_group_ids_no_filter_uses_all_homes():
    http = _FakeHttp({"h1": _home("h1", "gid1"), "h2": _home("h2", "gid2")})
    m = _make(http=http, home_ids_provider=None)
    await m._CentralHubManager__refresh_owned_group_ids()
    assert m._owned_group_ids == {"gid1", "gid2"}


@pytest.mark.asyncio
async def test_owned_group_ids_home_scope_narrows():
    http = _FakeHttp({"h1": _home("h1", "gid1"), "h2": _home("h2", "gid2")})
    m = _make(http=http, home_ids_provider=lambda: {"h1"})
    await m._CentralHubManager__refresh_owned_group_ids()
    assert m._owned_group_ids == {"gid1"}  # only enabled home


@pytest.mark.asyncio
async def test_owned_group_ids_empty_scope_connects_nothing():
    http = _FakeHttp({"h1": _home("h1", "gid1")})
    m = _make(http=http, home_ids_provider=lambda: set())
    await m._CentralHubManager__refresh_owned_group_ids()
    assert m._owned_group_ids == set()


@pytest.mark.asyncio
async def test_owned_group_ids_provider_error_fails_open():
    http = _FakeHttp({"h1": _home("h1", "gid1"), "h2": _home("h2", "gid2")})

    def boom():
        raise RuntimeError("filter broke")

    m = _make(http=http, home_ids_provider=boom)
    await m._CentralHubManager__refresh_owned_group_ids()
    # fail-open → all owned homes rather than silently disabling local control
    assert m._owned_group_ids == {"gid1", "gid2"}


# ------------------------------------------------- unowned-gateway throttle


@pytest.mark.asyncio
async def test_unowned_gateway_refresh_is_throttled(monkeypatch):
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    # Owned homes never include "neighbor", so every discovery is a miss.
    http = _FakeHttp({"h1": _home("h1", "gid1")})
    m = _make(http=http)
    on_change = m._CentralHubManager__on_service_change
    data = {"addresses": ["10.0.0.5"], "port": 8883}

    # 1st discovery: _owned_refreshed_at=0 → refresh runs (get_homes #1), stamps now
    await on_change("neighbor", MdnsServiceState.ADDED, data)
    assert http.get_homes_calls == 1

    # 2nd discovery within the window → throttled, no extra cloud call
    fake_now["t"] += 10
    await on_change("neighbor", MdnsServiceState.ADDED, data)
    assert http.get_homes_calls == 1

    # after the window lapses → refresh again
    fake_now["t"] += ch_mod._OWNED_REFRESH_MIN_INTERVAL_SEC + 1
    await on_change("neighbor", MdnsServiceState.ADDED, data)
    assert http.get_homes_calls == 2


# ---------------------------------------------------------- region gate


@pytest.mark.asyncio
async def test_disabled_outside_cn():
    storage = MIoTStorage(tempfile.mkdtemp(prefix="ch_"))
    m = CentralHubManager(
        storage=storage, http_client=_FakeHttp({}), uid="u1", cloud_server="us"
    )
    assert m.enabled is False
    assert m.is_ready is False
    # init is a cheap no-op outside cn
    await m.init_async()
    assert m._clients == {}


# ---------------------------------------------------------- cert re-sign


@pytest.mark.asyncio
async def test_refresh_cert_resigns_when_near_expiry():
    http = _FakeHttp({})
    m = _make(http=http)
    m._virtual_did = "1234"
    # remaining just at the margin → refresh_time <= 60 → must re-sign, then a
    # healthy remaining afterwards.
    m._cert = _FakeCert([ch_mod.MIHOME_CERT_EXPIRE_MARGIN, ch_mod.MIHOME_CERT_EXPIRE_MARGIN + 40 * 86400])
    ok = await m._CentralHubManager__refresh_cert()
    assert ok is True
    assert http.central_cert_calls == ["CSR"]  # a fresh cert was requested
    if m._refresh_cert_timer:
        m._refresh_cert_timer.cancel()


@pytest.mark.asyncio
async def test_refresh_cert_skips_when_healthy():
    http = _FakeHttp({})
    m = _make(http=http)
    m._virtual_did = "1234"
    m._cert = _FakeCert([ch_mod.MIHOME_CERT_EXPIRE_MARGIN + 40 * 86400])  # plenty left
    ok = await m._CentralHubManager__refresh_cert()
    assert ok is True
    assert http.central_cert_calls == []  # no re-sign
    if m._refresh_cert_timer:
        m._refresh_cert_timer.cancel()


# ------------------------------------------------- multi-gateway reconcile


@pytest.mark.asyncio
async def test_two_gateways_independent_reconcile():
    m = _make()
    refresh = m._CentralHubManager__refresh_dev_list
    g1 = _FakeLocalClient(
        "g1", dev_list={"a": {"online": True, "specv2_access": True, "push_available": True}}
    )
    g2 = _FakeLocalClient(
        "g2", dev_list={"b": {"online": True, "specv2_access": True, "push_available": True}}
    )
    await refresh(g1)
    await refresh(g2)
    assert m._dev_table["a"]["group_id"] == "g1"
    assert m._dev_table["b"]["group_id"] == "g2"
    # g1 loses its device → only 'a' drops, 'b' (under g2) untouched.
    g1._dev_list = {}
    await refresh(g1)
    assert "a" not in m._dev_table
    assert "b" in m._dev_table


# ------------------------------------------------- scope refresh (home switch)


@pytest.mark.asyncio
async def test_log_connect_failure_not_authorized_warn_once():
    m = _make()
    log = m._CentralHubManager__log_connect_failure
    log("g", MipsConnectionError("CONNACK 0x87 Not authorized: ..."))
    assert "g" in m._auth_rejected  # first Not-authorized → tracked (warned)
    log("g", MipsConnectionError("Not authorized"))
    assert m._auth_rejected == {"g"}  # deduped, not warned again
    # a generic failure is not treated as an auth rejection
    log("g2", MipsConnectionError("CONNACK timeout"))
    assert "g2" not in m._auth_rejected


@pytest.mark.asyncio
async def test_reconnect_sweep_targets_unconnected(monkeypatch):
    m = _make()
    m._static_gateways = [("10.0.0.9", 8883)]
    m._owned_group_ids = {"gidA", "gidB"}
    m._clients["gidA"] = _FakeLocalClient("gidA", connected=True)  # already up

    class FakeMdns:
        def get_services(self):
            return {
                "gidA": {"addresses": ["1.1.1.1"], "port": 8883},  # owned, connected
                "gidB": {"addresses": ["2.2.2.2"], "port": 8883},  # owned, down → retry
                "gidX": {"addresses": ["3.3.3.3"], "port": 8883},  # not owned → skip
            }

    m._mdns = FakeMdns()
    calls = []

    async def fake_ensure(group_id, host, port):
        calls.append((group_id, host, port))

    monkeypatch.setattr(m, "_CentralHubManager__ensure_client", fake_ensure)
    await m._CentralHubManager__ensure_desired_connections()

    assert ("static:10.0.0.9", "10.0.0.9", 8883) in calls  # static retried
    assert ("gidB", "2.2.2.2", 8883) in calls              # owned+down retried
    assert all(c[0] != "gidA" for c in calls)              # connected → skip
    assert all(c[0] != "gidX" for c in calls)              # not owned → skip


@pytest.mark.asyncio
async def test_refresh_scope_reinits(monkeypatch):
    m = _make()
    order = []

    async def fake_deinit():
        order.append("deinit")

    async def fake_init():
        order.append("init")

    monkeypatch.setattr(m, "deinit_async", fake_deinit)
    monkeypatch.setattr(m, "init_async", fake_init)
    await m.refresh_scope_async()
    assert order == ["deinit", "init"]  # re-init in order


# ------------------------------------------- static gateway home verification


def _seed_static(m, gid="static:10.0.0.9", dids=("d1", "d2")):
    m._clients[gid] = _FakeLocalClient(gid)
    for d in dids:
        m._dev_table[d] = {
            "group_id": gid, "online": True, "specv2_access": True, "push_available": True
        }
    return m._clients[gid]


@pytest.mark.asyncio
async def test_static_gateway_enabled_true_when_home_enabled():
    m = _make()
    cli = _seed_static(m)
    m._did_group_map = {"d1": "gidX", "d2": "gidX"}
    m._owned_group_ids = {"gidX"}  # enabled
    assert m._CentralHubManager__static_gateway_enabled(cli) is True


@pytest.mark.asyncio
async def test_static_gateway_enabled_false_when_home_not_enabled():
    m = _make()
    cli = _seed_static(m)
    m._did_group_map = {"d1": "gidX", "d2": "gidX"}
    m._owned_group_ids = {"gidOTHER"}  # a different home is enabled
    assert m._CentralHubManager__static_gateway_enabled(cli) is False


@pytest.mark.asyncio
async def test_static_gateway_enabled_none_when_unresolvable():
    m = _make()
    cli = _seed_static(m)
    m._did_group_map = {}  # devices not found in any home → can't resolve
    m._owned_group_ids = {"gidX"}
    assert m._CentralHubManager__static_gateway_enabled(cli) is None  # fail-open


@pytest.mark.asyncio
async def test_drop_client_removes_client_and_dev_entries():
    m = _make()
    _seed_static(m, dids=("d1", "d2"))
    # an unrelated gateway's device must survive
    m._clients["g2"] = _FakeLocalClient("g2")
    m._dev_table["keep"] = {"group_id": "g2", "online": True, "specv2_access": True, "push_available": True}
    await m._CentralHubManager__drop_client("static:10.0.0.9")
    assert "static:10.0.0.9" not in m._clients
    assert "d1" not in m._dev_table and "d2" not in m._dev_table
    assert "keep" in m._dev_table  # other gateway untouched


@pytest.mark.asyncio
async def test_refresh_scope_noop_when_disabled(monkeypatch):
    storage = MIoTStorage(tempfile.mkdtemp(prefix="ch_"))
    m = CentralHubManager(
        storage=storage, http_client=_FakeHttp({}), uid="u1", cloud_server="us"
    )
    called = []
    monkeypatch.setattr(m, "deinit_async", lambda: called.append("d"))
    monkeypatch.setattr(m, "init_async", lambda: called.append("i"))
    await m.refresh_scope_async()
    assert called == []  # disabled → no-op
