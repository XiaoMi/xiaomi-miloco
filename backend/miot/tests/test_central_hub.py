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

import asyncio
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

    ca_file = "ca"
    cert_file = "crt"
    key_file = "key"

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


def _dev(m, did, group_id="g"):
    m._dev_table[did] = {
        "group_id": group_id, "online": True,
        "specv2_access": True, "push_available": True,
    }


@pytest.mark.asyncio
async def test_single_flaky_device_does_not_cool_whole_gateway(monkeypatch):
    """一台设备反复超时只冷却它自己——否则一个抽风的插座会把整个家降级到云端。"""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    m = _make()
    _dev(m, "d1")
    _dev(m, "d2")

    m.note_local_failure("d1")
    m.note_local_failure("d1")  # 同一 did 再超时,不该累计成网关证据
    assert m.in_local_cooldown("d1") is True
    assert m.in_local_cooldown("d2") is False  # 同网关的其它设备仍走本地
    assert "g" not in m._gw_cooldown


@pytest.mark.asyncio
async def test_distinct_dids_timing_out_cools_whole_gateway(monkeypatch):
    """阈值个不同 did 超时 → 判定网关整体卡死,其后同网关所有设备直接走云端。

    回归"批量控制逐个各付一次 5s 超时"(10 设备 ≈ 50s 全失败,比纯云端更差)。
    """
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    m = _make()
    for did in ("d1", "d2", "d3"):
        _dev(m, did)

    m.note_local_failure("d1")
    assert "g" not in m._gw_cooldown  # 一个还不够
    m.note_local_failure("d2")  # 达到 _GW_TIMEOUT_THRESHOLD
    assert "g" in m._gw_cooldown
    # 从未超时过的 d3 也直接走云端——不必再自己撞一次 5s
    assert m.in_local_cooldown("d3") is True

    # 窗口过后自愈
    fake_now["t"] += ch_mod._GW_COOLDOWN_SEC + 1
    assert m.in_local_cooldown("d3") is False
    assert "g" not in m._gw_cooldown


@pytest.mark.asyncio
async def test_gateway_cooldown_is_per_gateway(monkeypatch):
    """一台网关卡死不影响另一台网关后面的设备。"""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    m = _make()
    _dev(m, "a1", "gw1")
    _dev(m, "a2", "gw1")
    _dev(m, "b1", "gw2")

    m.note_local_failure("a1")
    m.note_local_failure("a2")
    assert "gw1" in m._gw_cooldown
    assert m.in_local_cooldown("b1") is False  # gw2 不受影响


@pytest.mark.asyncio
async def test_stale_timeout_evidence_expires(monkeypatch):
    """跨越窗口的两次超时不该凑成网关降级——证据要按时效淘汰。"""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    m = _make()
    _dev(m, "d1")
    _dev(m, "d2")

    m.note_local_failure("d1")
    fake_now["t"] += ch_mod._GW_TIMEOUT_WINDOW_SEC + 1  # d1 的证据过期
    m.note_local_failure("d2")
    assert "g" not in m._gw_cooldown


@pytest.mark.asyncio
async def test_unknown_did_does_not_crash_cooldown(monkeypatch):
    """不在设备表里的 did(尚未同步/已移除)只走设备级,不应抛异常。"""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    m = _make()
    m.note_local_failure("ghost")  # 无 group_id
    assert m.in_local_cooldown("ghost") is True
    assert m._gw_cooldown == {}


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
    m._started = True  # __refresh_cert 的生命周期闸门,真实前提由 init_async 置
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
    m._started = True
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
    # 白名单已成功拉取过 —— 否则 sweep 会先走兜底刷新把它重算成空集
    # (那条路径由 test_sweep_retries_failed_owned_refresh 覆盖)。
    m._owned_refresh_ok = True
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


@pytest.mark.asyncio
async def test_refresh_scope_clears_owned_group_whitelist(monkeypatch):
    """切家时必须清掉旧家庭的白名单，不能留给 init_async 的 fail-open 当兜底值 ——
    否则云端不可达时 __refresh_owned_group_ids 只会置 _owned_refresh_ok=False、
    旧集合原样保留（那是关停/重启场景要的行为），重连 sweep 会照着旧 group_id
    把切走的旧家庭网关重新连回来（用户切到"老家"，控制指令却仍打向"新家"）。"""
    m = _make()
    m._owned_group_ids = {"old_gid"}
    m._did_group_map = {"d1": "old_gid"}
    m._owned_refresh_ok = True
    m._owned_retry_at = 12345.0

    async def fake_deinit():
        pass  # 真实 deinit_async 不清这些字段，这里只隔离 mDNS/client 依赖

    async def fake_init():
        pass  # 不真的去拉云端家庭列表

    monkeypatch.setattr(m, "deinit_async", fake_deinit)
    monkeypatch.setattr(m, "init_async", fake_init)
    await m.refresh_scope_async()

    assert m._owned_group_ids == set()
    assert m._did_group_map == {}
    assert m._owned_refresh_ok is False
    assert m._owned_retry_at == 0.0


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


# ------------------------------- 启动期云端不可达 → 白名单必须能自愈


class _FlakyHttp(_FakeHttp):
    """前 N 次 get_homes_async 抛异常，之后正常返回（模拟开机自启早于网络就绪）。"""

    def __init__(self, homes, fail_times):
        super().__init__(homes)
        self._fail_left = fail_times

    async def get_homes_async(self):
        self.get_homes_calls += 1
        if self._fail_left > 0:
            self._fail_left -= 1
            raise RuntimeError("cloud unreachable")
        return self._homes


@pytest.mark.asyncio
async def test_failed_owned_refresh_does_not_stamp_throttle(monkeypatch):
    """拉取失败不能盖成功时间戳,否则 60s 节流窗会吃掉唯一一次 mDNS ADDED 回调。"""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    m = _make(http=_FlakyHttp({}, fail_times=1))

    await m._CentralHubManager__refresh_owned_group_ids()

    assert m._owned_refresh_ok is False
    assert m._owned_refreshed_at == 0.0  # 未被盖成"刚刷过"
    assert m._owned_retry_at == 1000.0 + ch_mod._OWNED_REFRESH_RETRY_INTERVAL_SEC


@pytest.mark.asyncio
async def test_sweep_retries_failed_owned_refresh(monkeypatch):
    """回归"启动期云端不可达 → 本地控制永久降级":重连 sweep 必须能补刷白名单。

    mDNS 按内容去重,IP/端口稳定的网关一个进程只触发一次 ADDED;那唯一一次回调若
    撞上空白名单就再无第二次机会,所以自愈只能靠 sweep。
    """
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    http = _FlakyHttp({"h1": _home("h1", "gidA")}, fail_times=1)
    m = _make(http=http)
    m._mdns = None  # 只验白名单刷新这一段

    # 启动那次失败 → 白名单空
    await m._CentralHubManager__refresh_owned_group_ids()
    assert m._owned_group_ids == set()
    assert m._owned_refresh_ok is False

    # 退避未到 → sweep 不重试
    await m._CentralHubManager__ensure_desired_connections()
    assert http.get_homes_calls == 1

    # 退避到点 → sweep 补刷成功,白名单填上,本地控制恢复
    fake_now["t"] += ch_mod._OWNED_REFRESH_RETRY_INTERVAL_SEC
    await m._CentralHubManager__ensure_desired_connections()
    assert http.get_homes_calls == 2
    assert m._owned_group_ids == {"gidA"}
    assert m._owned_refresh_ok is True

    # 成功后不再反复拉云端
    await m._CentralHubManager__ensure_desired_connections()
    assert http.get_homes_calls == 2


@pytest.mark.asyncio
async def test_successful_empty_refresh_is_not_retried(monkeypatch):
    """"成功但结果为空"(用户确实没启用家庭)不是失败,不该被 sweep 反复重拉。"""
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ch_mod.time, "monotonic", lambda: fake_now["t"])
    http = _FakeHttp({})  # 成功返回空
    m = _make(http=http)
    m._mdns = None

    await m._CentralHubManager__refresh_owned_group_ids()
    assert m._owned_group_ids == set()
    assert m._owned_refresh_ok is True  # 成功
    assert m._owned_refreshed_at == 1000.0  # 盖了节流戳

    fake_now["t"] += ch_mod._OWNED_REFRESH_RETRY_INTERVAL_SEC + 1
    await m._CentralHubManager__ensure_desired_connections()
    assert http.get_homes_calls == 1  # 没有额外拉取


# --------------------------------------------- cancel during connect (PR #472)


@pytest.mark.asyncio
async def test_cancel_during_connect_deinits_the_orphan_client(monkeypatch):
    """连接中途被 cancel 必须清理那个半成品 client(🔴)。

    init_async 内部先 loop_start()(paho 网络线程已跑)再等 CONNACK;cancel 落在那个
    await 上抛 CancelledError —— 它是 BaseException,`except MipsConnectionError` /
    `except Exception` 都接不住。不显式兜底的话该 client 既不在 _clients 里也没人
    deinit 它,paho 线程会按 6~60s 退避永久重连用户网关,每撞一次 deinit/切家庭就
    多积一个僵尸线程 + socket。
    """
    m = _make()
    m._virtual_did = "vd"
    m._cert = _FakeCert([10_000_000])

    started = asyncio.Event()
    deinited: list[str] = []

    class _HangingClient:
        def __init__(self, **kw):
            self._group_id = kw.get("group_id", "g1")
            self.on_dev_list_changed = None

        @property
        def group_id(self):
            return self._group_id

        @property
        def host(self):
            return "10.0.0.9"

        @property
        def is_connected(self):
            return False

        async def init_async(self):
            started.set()
            await asyncio.sleep(3600)  # 卡在等 CONNACK

        async def deinit_async(self):
            deinited.append(self._group_id)

    monkeypatch.setattr(ch_mod, "MipsLocalClient", _HangingClient)

    task = asyncio.get_event_loop().create_task(
        m._CentralHubManager__ensure_client("g1", "10.0.0.9", 8883)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert deinited == ["g1"], "被 cancel 的半成品 client 必须被 deinit"
    assert "g1" not in m._clients


@pytest.mark.asyncio
async def test_concurrent_static_and_mdns_same_host_only_connects_once(monkeypatch):
    """同一物理网关的静态配置(`static:host`)与 mDNS 真实 group_id 并发建连时,
    只应该建出一个 MipsLocalClient —— 否则两者的 MQTT client_id 都是虚拟 did
    (与 group_id 无关),broker 会把后到的会话踢掉前一个,被踢的一方自动重连又
    把对方顶下线,形成永久互踢(can_control() 每几秒真假翻转)。

    `_ensure_locks` 按 group_id 建键,"static:10.0.0.9" 和 mDNS 的真实 group_id
    拿到的是两把不同的锁、互不互斥;而按 host 去重的循环只看 self._clients,
    在途的连接(init_async() 还没返回)在表里是隐形的 —— 必须靠 _connecting_hosts
    这个在途集合才能挡住第二条路径。
    """
    m = _make()
    m._virtual_did = "vd"
    m._cert = _FakeCert([10_000_000])

    started = asyncio.Event()
    release = asyncio.Event()
    created: list[str] = []

    class _SlowClient:
        def __init__(self, **kw):
            self._group_id = kw["group_id"]
            created.append(self._group_id)
            self.on_dev_list_changed = None

        @property
        def group_id(self):
            return self._group_id

        @property
        def host(self):
            return "10.0.0.9"

        @property
        def is_connected(self):
            return False  # 还没连完,让去重循环看不到自己

        async def init_async(self):
            started.set()
            await release.wait()  # 卡住,制造两条路径重叠的窗口

        async def deinit_async(self):
            pass

    monkeypatch.setattr(ch_mod, "MipsLocalClient", _SlowClient)

    first = asyncio.get_event_loop().create_task(
        m._CentralHubManager__ensure_client("static:10.0.0.9", "10.0.0.9", 8883)
    )
    await asyncio.wait_for(started.wait(), timeout=2)  # 确保第一条已进入在途窗口
    second = asyncio.get_event_loop().create_task(
        m._CentralHubManager__ensure_client("groupid.abcd", "10.0.0.9", 8883)
    )
    await asyncio.sleep(0.05)  # 让第二条任务真正跑到去重检查
    release.set()
    await asyncio.gather(first, second)

    assert created == ["static:10.0.0.9"], "第二条路径必须被在途去重挡住,不建 client"
    assert m._connecting_hosts == set()  # finally 收尾,不留残留


@pytest.mark.asyncio
async def test_refresh_cert_is_serialised(monkeypatch):
    """并发 __refresh_cert 只签一次:两台网关持不同的 per-group 锁,不加这把锁就会
    各自生成 key、各自签 cert,两次保存都是"后写者赢",最终可能 key 与 cert 不配对
    (mTLS 永久失败且无自愈——剩余时间检查只看 subject/有效期,补签条件永不触发)。
    """
    http = _FakeHttp({})
    m = _make(http=http)
    m._started = True
    m._virtual_did = "vd"
    # 第一次进来剩余时间为 0(要签),锁内第二次调用再取时已是健康值 → 直接复用。
    m._cert = _FakeCert([0, 10_000_000, 10_000_000])
    monkeypatch.setattr(m, "_CentralHubManager__schedule_cert_refresh", lambda d: None)

    results = await asyncio.gather(
        m._CentralHubManager__refresh_cert(),
        m._CentralHubManager__refresh_cert(),
    )
    assert results == [True, True]
    assert len(http.central_cert_calls) == 1, "并发只应签发一次"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    ["verify_ca_cert_async", "update_user_key_async", "update_user_cert_async"],
)
async def test_cert_refresh_schedules_retry_on_persist_failure(monkeypatch, broken):
    """落盘类失败也必须排退避重试,不能只挡异常那一条。

    续签是自维持的链条:所有网关都稳定连着时 reconnect sweep 跳过已连网关、
    __ensure_client_locked 永不执行,链一断就再没有任何定时器指向 __refresh_cert ——
    证书在无人察觉下过期(已建的 MQTT 会话还活着所以"看起来正常"),直到下一次断连
    才被 mTLS 拒绝,此后所有控制静默退回云端。而这三处的 False 来自存储层写盘失败
    (磁盘临时写满、目录权限被外部工具改动),性质与云端超时一样是瞬态,只是不走异常
    通道,此前正好掉进缺口。
    """
    m = _make()
    m._started = True
    m._virtual_did = "vd"
    # 第一次剩余 0 → 走重签分支(好让 key/cert 落盘失败真被执行到),重签后是健康值
    # → **不打断的话这次调用会成功且不排重试**,于是下面的断言只可能由 broken 那一步
    # 造成,不会被 "refresh_time <= 0" 这条出口蒙对。
    m._cert = _FakeCert(
        [0, ch_mod.MIHOME_CERT_EXPIRE_MARGIN + 40 * 86400]
    )

    async def _fail(*args, **kwargs):
        return False

    monkeypatch.setattr(m._cert, broken, _fail)
    scheduled: list[float] = []
    monkeypatch.setattr(
        m, "_CentralHubManager__schedule_cert_refresh", scheduled.append
    )

    assert await m._CentralHubManager__refresh_cert() is False
    assert scheduled == [ch_mod._CERT_REFRESH_RETRY_BACKOFF]


@pytest.mark.asyncio
async def test_cert_refresh_no_retry_without_virtual_did(monkeypatch):
    """身份缺失不是瞬态故障(要靠 init / 换号流程补),不该无限重试。"""
    m = _make()
    m._started = True
    m._virtual_did = None
    scheduled: list[float] = []
    monkeypatch.setattr(
        m, "_CentralHubManager__schedule_cert_refresh", scheduled.append
    )

    assert await m._CentralHubManager__refresh_cert() is False
    assert scheduled == []


@pytest.mark.asyncio
async def test_cert_refresh_exception_still_schedules_retry(monkeypatch):
    """异常这条原有路径不能因为兜底上移而丢掉重试。"""
    m = _make()
    m._started = True
    m._virtual_did = "vd"
    m._cert = _FakeCert([0, ch_mod.MIHOME_CERT_EXPIRE_MARGIN + 40 * 86400])

    async def _boom(*args, **kwargs):
        raise RuntimeError("cloud down")

    monkeypatch.setattr(m._cert, "verify_ca_cert_async", _boom)
    scheduled: list[float] = []
    monkeypatch.setattr(
        m, "_CentralHubManager__schedule_cert_refresh", scheduled.append
    )

    assert await m._CentralHubManager__refresh_cert() is False
    assert scheduled == [ch_mod._CERT_REFRESH_RETRY_BACKOFF]


@pytest.mark.asyncio
async def test_cert_refresh_success_does_not_arm_backoff(monkeypatch):
    """成功路径只排"到期前"的正常定时器,绝不能顺带排 300s 退避 —— 否则每次成功
    续签都会在 5 分钟后再打一次云端签发。与上面三条失败用例成对,钉住"退避只在
    失败时武装"这个语义(它们共用同一个 _FakeCert 序列,只差有没有打断)。"""
    m = _make()
    m._started = True
    m._virtual_did = "vd"
    m._cert = _FakeCert([0, ch_mod.MIHOME_CERT_EXPIRE_MARGIN + 40 * 86400])
    scheduled: list[float] = []
    monkeypatch.setattr(
        m, "_CentralHubManager__schedule_cert_refresh", scheduled.append
    )

    assert await m._CentralHubManager__refresh_cert() is True
    assert scheduled and ch_mod._CERT_REFRESH_RETRY_BACKOFF not in scheduled


@pytest.mark.asyncio
async def test_refresh_cert_noop_before_started(monkeypatch):
    """未 init(_started=False)时 __refresh_cert 必须直接短路,不能碰云端/存储。

    真实触发路径:deinit_async 落在上一次 __refresh_cert_once 的云端签发 await
    里时,_started 已被置 False,但那个僵尸任务自己不检查就会继续跑完并自我
    续期——这道闸门就是挡它的地方。"""
    m = _make()
    assert m._started is False  # 未调用 init_async,这是真实前提
    m._virtual_did = "vd"
    scheduled: list[float] = []
    monkeypatch.setattr(
        m, "_CentralHubManager__schedule_cert_refresh", scheduled.append
    )
    cert_called: list[str] = []
    monkeypatch.setattr(
        m._cert, "verify_ca_cert_async", lambda: cert_called.append(1) or True
    )

    assert await m._CentralHubManager__refresh_cert() is False
    assert scheduled == []  # 没排下一轮
    assert cert_called == []  # 没碰证书存储


@pytest.mark.asyncio
async def test_deinit_cancels_pending_cert_refresh_task():
    """deinit_async 必须能拆掉"已经开跑、卡在云端签发 await 上"的续签任务,不能只
    取消还没到点的定时器 —— 否则它会带着已拆卸的 self 跑完并自我续期,形成一条
    到进程退出都停不下来的僵尸链(还可能用旧 did 覆盖新实例刚签的证书)。"""
    m = _make()
    m._started = True

    async def _stuck_forever():
        await asyncio.Event().wait()  # 模拟卡在云端签发那个 await 上

    task = asyncio.ensure_future(_stuck_forever())
    m._refresh_cert_task = task

    await m.deinit_async()
    await asyncio.sleep(0)  # 让事件循环真正处理 cancel(),不只是标记 pending

    assert task.cancelled()
    assert m._refresh_cert_task is None
