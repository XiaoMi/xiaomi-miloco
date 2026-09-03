# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""启动对齐的写入范围与日志分级。

不打真机：MiotProxy 用最小 stub 替代，StateStore 是真的 —— 断言看的是容器里最终
长出什么路径，而不是 stub 被怎么调用。
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace

import pytest
from miloco.miot.state_align import (
    CHUNK_SIZE,
    SAMPLE_LIMIT,
    _collect_params,
    _DeviceMeta,
    _read_values,
    _Samples,
    _write_device,
    align_iot_state,
)
from miloco.state import MISSING, StateStore
from miloco.state.utils import flatten
from miot.types import MIoTGetPropertyParam

KNOWN_CODE = -704220043  # 属性值不正确
UNKNOWN_CODE = -704010000  # 码表里没有


def _device(*, online: bool = True, model: str = "acme-x1") -> SimpleNamespace:
    return SimpleNamespace(online=online, model=model, urn="urn:test")


class _FakeProxy:
    """rows 的键是 (did, siid, piid)，值是不含 id 字段的那半个响应行。"""

    def __init__(self, devices: dict, rows: dict):
        self._devices = devices
        self._rows = rows
        self.enabled_home = True
        self.requested: list[tuple[str, int, int]] = []

    def has_enabled_home(self) -> bool:
        return self.enabled_home

    async def get_devices(self) -> dict:
        return self._devices

    async def devices_in_current_home(self) -> dict:
        return self._devices

    async def get_readable_prop_iids(self, did: str) -> list[str]:
        return [f"prop.{s}.{p}" for (d, s, p) in self._rows if d == did]

    async def get_device_properties(self, params: list) -> list[dict]:
        out = []
        for param in params:
            key = (param.did, param.siid, param.piid)
            self.requested.append(key)
            row = dict(self._rows.get(key, {"code": UNKNOWN_CODE}))
            row.update(did=param.did, siid=param.siid, piid=param.piid)
            out.append(row)
        return out


def _align(store, proxy, *, scope: int = 0, current_scope=None):
    """代号默认恒定不变 —— 只有作用域那几条用例关心它。"""
    return align_iot_state(
        store, proxy, scope=scope, current_scope=current_scope or (lambda: scope)
    )


@pytest.fixture
async def store():
    s = StateStore()
    s.start()
    yield s
    s.stop()


# ── 待办 1：离线设备只写标志，不写属性 ─────────────────────────────────


async def test_online_device_gets_flag_and_properties(store):
    proxy = _FakeProxy(
        {"d1": _device(online=True)},
        {("d1", 2, 1): {"code": 0, "value": 21.5}},
    )

    await _align(store, proxy)

    assert store.get("iot/device/d1/status/online") is True
    assert store.get("iot/device/d1/prop/2.1") == 21.5
    # online 是叶子、prop 是子树，同一层混放 —— 写的顺序不能把谁翻成另一种形态
    assert store.stats()["shape_flips"] == 0


async def test_offline_device_properties_are_not_requested(store):
    proxy = _FakeProxy(
        {"d1": _device(online=False)},
        {("d1", 2, 1): {"code": 0, "value": 21.5}},
    )

    await _align(store, proxy)

    assert proxy.requested == []
    assert store.get("iot/device/d1/prop/2.1") is MISSING


async def test_offline_device_still_gets_its_flag(store):
    """跳过属性但不能整台跳过：容器里没有这台设备，就分不出「离线」和「没接入」。"""
    proxy = _FakeProxy(
        {"d1": _device(online=False)}, {("d1", 2, 1): {"code": 0, "value": 1}}
    )

    await _align(store, proxy)

    assert store.get("iot/device/d1/status/online") is False


async def test_offline_device_does_not_block_online_ones(store):
    proxy = _FakeProxy(
        {"off": _device(online=False), "on": _device(online=True)},
        {("off", 2, 1): {"code": 0, "value": 1}, ("on", 2, 1): {"code": 0, "value": 2}},
    )

    await _align(store, proxy)

    assert store.get("iot/device/on/prop/2.1") == 2
    assert store.get("iot/device/off/prop/2.1") is MISSING
    assert store.get("iot/device/off/status/online") is False


async def test_did_with_slash_gets_neither_flag_nor_properties(store):
    """'/' 是路径分隔符，这种 did 连 online 标志都写不进去。"""
    proxy = _FakeProxy(
        {"a/b": _device(online=True)}, {("a/b", 2, 1): {"code": 0, "value": 1}}
    )

    await _align(store, proxy)

    assert store.snapshot("iot/**") == {}
    assert proxy.requested == []


# ── 待办 2：读失败日志按返回码分级 ─────────────────────────────────────


async def test_known_failure_code_does_not_warn(store, caplog):
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": KNOWN_CODE}})

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []
    assert any(str(KNOWN_CODE) in r.getMessage() for r in caplog.records)


async def test_unknown_failure_code_warns(store, caplog):
    """码表里没有的码是唯一真该看的：它没人解释过。"""
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": UNKNOWN_CODE}})

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(str(UNKNOWN_CODE) in m for m in warnings)


async def test_failure_line_carries_model(store, caplog):
    """光有 did 定位不到属性语义，要靠 model 去 spec 里反查。"""
    proxy = _FakeProxy(
        {"d1": _device(model="cgllc-b1")}, {("d1", 2, 5): {"code": KNOWN_CODE}}
    )

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    lines = [r.getMessage() for r in caplog.records]
    assert any("cgllc-b1" in m and "2.5" in m for m in lines)


async def test_summary_groups_failures_by_meaning_and_model(store, caplog):
    proxy = _FakeProxy(
        {"d1": _device(model="cgllc-b1"), "d2": _device(model="zhimi-ma2")},
        {
            ("d1", 2, 1): {"code": KNOWN_CODE},
            ("d1", 2, 2): {"code": KNOWN_CODE},
            ("d2", 3, 3): {"code": KNOWN_CODE},
        },
    )

    with caplog.at_level(logging.INFO, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    summary = next(
        m for m in (r.getMessage() for r in caplog.records) if "align done" in m
    )
    assert "属性值不正确" in summary
    assert "cgllc-b1" in summary and "zhimi-ma2" in summary


# ── 既有行为的补测 ────────────────────────────────────────────────────


async def test_code_zero_without_value_is_not_written(store):
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": 0}})

    await _align(store, proxy)

    assert store.get("iot/device/d1/prop/2.1") is MISSING


async def test_none_is_written_because_it_is_a_legal_value(store):
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": None}})

    await _align(store, proxy)

    assert store.get("iot/device/d1/prop/2.1") is None


async def test_one_bad_value_does_not_lose_the_whole_device(store):
    """容器的校验是整笔不写，所以整台写失败要退成逐条写。"""
    proxy = _FakeProxy(
        {"d1": _device()},
        {
            ("d1", 2, 1): {"code": 0, "value": float("nan")},
            ("d1", 2, 2): {"code": 0, "value": 7},
        },
    )

    await _align(store, proxy)

    assert store.get("iot/device/d1/prop/2.2") == 7
    assert store.get("iot/device/d1/prop/2.1") is MISSING


async def test_align_never_raises_when_the_proxy_explodes(store):
    class _Boom:
        async def get_devices(self):
            raise RuntimeError("boom")

    await _align(store, _Boom())  # 不抛就算过


async def test_align_reports_when_nothing_is_readable(store, caplog):
    proxy = _FakeProxy({"d1": _device(online=False)}, {})

    with caplog.at_level(logging.INFO, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    assert any("no readable" in r.getMessage() for r in caplog.records)
    # 一条属性都读不到也要先把标志写下去，否则这台设备在容器里根本不存在
    assert store.get("iot/device/d1/status/online") is False


async def test_one_pattern_reaches_every_owner_of_a_device(store):
    """四段路径的意义就在这里：`*` 顶 owner 那一段，`**` 收剩下的。"""
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": 21.5}})
    await _align(store, proxy)
    store.set("omni/device/d1/caption", "有人", source="omni")

    got = store.snapshot("*/device/d1/**")

    assert set(flatten(got)) == {
        "iot/device/d1/status/online",
        "iot/device/d1/prop/2.1",
        "omni/device/d1/caption",
    }


# ── 写入侧的路径校验：拼进路径的东西必须先过容器那套段规则 ──────────────


class _EchoProxy(_FakeProxy):
    """响应行的 did 由 rows 的键决定，与请求里的 did 无关。

    云端返回没请求过的 did 是真实形态 —— `_read_values` 要按 meta 这份名单把它丢掉。
    """

    async def get_device_properties(self, params: list) -> list[dict]:
        out = []
        for param in params:
            self.requested.append((param.did, param.siid, param.piid))
        for (did, siid, piid), row in self._rows.items():
            out.append(dict(row, did=did, siid=siid, piid=piid))
        return out


async def test_property_name_with_slash_does_not_land_a_level_deeper(store):
    """整台写拒了它，逐条写这条退路不能把它放行 —— 否则值落到多一层的地方。"""
    written = _write_device(store, "d1", {"2.2": 5, "a/b": 7}, _Samples())

    assert store.get("iot/device/d1/prop/a/b") is MISSING
    assert store.get("iot/device/d1/prop/2.2") == 5
    assert written == 1


async def test_a_did_with_a_slash_is_still_refused_by_the_write_side(store):
    """名单闸在上游，端到端已走不到这里；留着是防御性重校，放行了 did 会被劈成两段。"""
    written = _write_device(store, "x/y", {"2.1": 99}, _Samples())

    assert written == 0
    assert store.snapshot("iot/device/x/**") == {}


async def test_an_online_flag_lost_to_the_leaf_limit_shows_up_in_the_summary(
    store, caplog, monkeypatch
):
    """撞上叶子上限时容器不抛异常；不看返回值这台设备就悄悄没有在线标志。"""
    monkeypatch.setattr("miloco.state.store.MAX_LEAVES", 1)
    proxy = _FakeProxy({"d1": _device(), "d2": _device()}, {})

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    messages = [r.getMessage() for r in caplog.records]
    summary = next(m for m in messages if "wrote online flags only" in m)
    assert "online_flag_dropped" in summary


async def test_bulk_writes_yield_instead_of_burning_the_pending_gate(
    store, caplog, monkeypatch
):
    """整批写完才让 loop 转的话 pending 会堆过水位闸，而那道闸只报一次、只在 start() 复位。"""
    monkeypatch.setattr("miloco.state.store.PENDING_WARN_THRESHOLD", 3)
    count = 4
    proxy = _FakeProxy(
        {f"d{i}": _device() for i in range(count)},
        {(f"d{i}", 2, 1): {"code": 0, "value": i} for i in range(count)},
    )

    with caplog.at_level(logging.WARNING, logger="miloco.state.store"):
        await _align(store, proxy)

    burned = [r for r in caplog.records if "pending state changes" in r.getMessage()]
    assert not burned


class _SpecFailsProxy(_EchoProxy):
    """指定的 did 拉 spec 会抛：它在 meta 里、也在线，但一条属性都没请求过。"""

    def __init__(self, devices: dict, rows: dict, *, failing: str):
        super().__init__(devices, rows)
        self._failing = failing

    async def get_readable_prop_iids(self, did: str) -> list[str]:
        if did == self._failing:
            raise RuntimeError("spec unavailable")
        return await super().get_readable_prop_iids(did)


async def test_a_cached_row_for_an_offline_device_is_dropped(store):
    """离线设备的属性从来没请求过，云端多回的那行是缓存值，写进去会假装是刚上报的。"""
    proxy = _EchoProxy(
        {"on1": _device(), "off1": _device(online=False)},
        {
            ("on1", 2, 1): {"code": 0, "value": 26},
            ("off1", 2, 1): {"code": 0, "value": 99},
        },
    )

    await _align(store, proxy)

    assert store.get("iot/device/on1/prop/2.1") == 26
    assert store.get("iot/device/off1/prop/2.1") is MISSING
    assert store.get("iot/device/off1/status/online") is False


async def test_a_row_for_a_device_whose_spec_failed_is_dropped(store):
    """spec 拉不到的设备在线、也在 meta 里，一条属性却没请求 —— 只按在线状态判会漏掉它。"""
    proxy = _SpecFailsProxy(
        {"d1": _device(), "d2": _device()},
        {
            ("d1", 2, 1): {"code": 0, "value": 26},
            ("d2", 2, 1): {"code": 0, "value": 99},
        },
        failing="d2",
    )

    await _align(store, proxy)

    assert store.get("iot/device/d1/prop/2.1") == 26
    assert store.get("iot/device/d2/prop/2.1") is MISSING
    assert store.get("iot/device/d2/status/online") is True


async def test_a_wellformed_did_outside_the_home_is_dropped(store, caplog):
    """响应侧多回来的合法 did 不能借这条链绕过家庭过滤。"""
    proxy = _EchoProxy(
        {"d1": _device()},
        {
            ("d1", 2, 1): {"code": 0, "value": 26},
            ("d9", 2, 1): {"code": 0, "value": 99},
        },
    )

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    assert store.get("iot/device/d1/prop/2.1") == 26
    assert store.snapshot("iot/device/d9/**") == {}
    # 丢了要留痕：静默丢弃时排查的人分不出是被挡了还是云端没返回
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("out_of_home row did=d9 iid=prop.2.1" in m for m in warnings)


async def test_a_bridged_did_from_the_response_is_not_blamed_on_the_home_filter(
    store, caplog
):
    """did 带 '/' 的是桥接子设备，报成家庭外会把排查的人带去查家庭白名单。"""
    proxy = _EchoProxy(
        {"d1": _device()},
        {
            ("d1", 2, 1): {"code": 0, "value": 26},
            ("bridge/0", 2, 1): {"code": 0, "value": 99},
        },
    )

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("bad_did_received row did=bridge/0" in m for m in warnings)
    assert not any("out_of_home" in m for m in warnings)


async def test_bridged_dids_do_not_eat_the_out_of_home_sample_budget(store, caplog):
    """桥接子设备成批回来，共用额度的话「绕过家庭过滤」那条一行都打不出。"""
    rows = {
        (f"bridge/{index}", 2, 1): {"code": 0, "value": 1}
        for index in range(SAMPLE_LIMIT + 1)
    }
    # 排在成批的桥接行之后：额度被占满时它就是打不出来的那一条
    rows[("d9", 2, 1)] = {"code": 0, "value": 99}
    rows[("d1", 2, 1)] = {"code": 0, "value": 26}
    proxy = _EchoProxy({"d1": _device()}, rows)

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("out_of_home row did=d9 iid=prop.2.1" in m for m in warnings)


async def test_dict_value_is_dropped_instead_of_becoming_a_subtree(store, caplog):
    """dict 会被容器展开成一层子树，这条属性就不再是叶子，按叶子读的人取到空。"""
    proxy = _FakeProxy(
        {"d1": _device()},
        {
            ("d1", 2, 1): {"code": 0, "value": {"a": 1}},
            ("d1", 2, 2): {"code": 0, "value": 5},
        },
    )

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    # 两条都要：只查子树的话，把 dict 转成字符串塞进去也能过
    assert store.snapshot("iot/device/d1/prop/2.1/**") == {}
    assert store.get("iot/device/d1/prop/2.1") is MISSING
    assert store.get("iot/device/d1/prop/2.2") == 5
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("dict value dropped" in m and "2.1" in m for m in warnings)


async def test_array_value_is_one_leaf_and_not_worth_a_warning(store, caplog):
    """容器把标量数组收成一个元组叶子，读得到也比得了，没有要报的事。"""
    proxy = _FakeProxy(
        {"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": [1, 2, 3]}}
    )

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    assert store.get("iot/device/d1/prop/2.1") == (1, 2, 3)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []


async def test_a_bad_did_from_the_response_is_never_hidden_by_the_request_side(caplog):
    """两侧共用样本额度的话，成批的桥接子设备会把额度吃光，云端异常那条一行都打不出。"""
    store = StateStore()
    samples = _Samples()
    bridges = {f"bridge/{index}": _device() for index in range(SAMPLE_LIMIT + 1)}

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        await _collect_params(_FakeProxy(bridges, {}), samples)
        _write_device(store, "cloud/ghost", {"2.1": 1}, samples)

    assert any("cloud/ghost" in r.getMessage() for r in caplog.records)


async def test_summary_counts_only_devices_that_got_something_written(store, caplog):
    """名单外的 did 不能顶高 written_devices —— 它与 devices 不同源，能大于总数。"""
    proxy = _EchoProxy(
        {"d1": _device()},
        {
            ("d1", 2, 1): {"code": 0, "value": 26},
            ("d9", 2, 1): {"code": 0, "value": 99},
        },
    )

    with caplog.at_level(logging.INFO, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    summary = next(
        m for m in (r.getMessage() for r in caplog.records) if "align done" in m
    )
    assert re.search(r"\bwritten_devices=1 ", summary) and "written=1 " in summary


async def test_a_rejected_property_name_does_not_read_like_a_rejected_value(
    store, caplog
):
    """被拒的是名字，报成「值被拒」会让人照着值去查。"""
    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        _write_device(store, "d1", {"2.1": 5, "a/b": 7}, _Samples())

    warnings = [r.getMessage() for r in caplog.records]
    assert any("iid rejected" in m and "a/b" in m for m in warnings)


async def test_no_field_of_a_device_is_a_bare_leaf(store):
    """字段那一层全是子树，`*` 就不会被某一片叶子骗过去、静默少还给一半。"""
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": 21.5}})
    await _align(store, proxy)

    with pytest.raises(ValueError):
        store.snapshot("iot/device/*/*")


async def test_align_only_walks_the_current_home(store):
    """没启用的家庭在别处一律拒绝访问，容器不该自己开一条旁路把它们拉回来。"""

    class _TwoHomes(_FakeProxy):
        async def devices_in_current_home(self) -> dict:
            return {"d_mine": self._devices["d_mine"]}

    proxy = _TwoHomes(
        {"d_mine": _device(), "d_parents": _device()},
        {
            ("d_mine", 2, 1): {"code": 0, "value": 26},
            ("d_parents", 2, 1): {"code": 0, "value": 99},
        },
    )

    await _align(store, proxy)

    assert store.get("iot/device/d_mine/prop/2.1") == 26
    assert store.snapshot("iot/device/d_parents/**") == {}
    # 也不该为它发请求 —— 每次启动多跑一轮 spec 查询和属性读
    assert all(did == "d_mine" for did, _, _ in proxy.requested)


# ── 逐行失败判定要跟仓库既有口径同一份 ─────────────────────────────────


@pytest.mark.parametrize("code", [-702000000, -702010000, None, 1])
async def test_a_success_code_keeps_its_value(store, caplog, code):
    """既有口径是「只有负码算失败」且 OK 码除外；这里另立一份 code != 0 会丢掉带值的行。"""
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": code, "value": 26}})

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    assert store.get("iot/device/d1/prop/2.1") == 26
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []


async def test_chunk_failure_is_rate_limited_like_every_other_kind(caplog):
    """整批失败是最容易一失全失的一类：云端断了每批都抛同一个异常，逐批打没有新信息。"""

    class _AlwaysBoom(_FakeProxy):
        async def get_device_properties(self, params: list) -> list[dict]:
            raise RuntimeError("boom")

    chunks = SAMPLE_LIMIT + 3
    params = [
        MIoTGetPropertyParam(did="d1", siid=2, piid=index)
        for index in range(CHUNK_SIZE * chunks)
    ]
    samples = _Samples()

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        await _read_values(
            _AlwaysBoom({"d1": _device()}, {}),
            params,
            {"d1": _DeviceMeta(online=True, model="m")},
            {},
            samples,
        )

    lines = [
        m for m in (r.getMessage() for r in caplog.records) if "chunk read failed" in m
    ]
    assert len(lines) == SAMPLE_LIMIT
    assert samples.counts["chunk_failed"] == chunks


async def test_online_flags_are_reported_when_no_property_is_readable(store, caplog):
    """标志是在这条日志之前写的，说 nothing written 会让人把那几条当成另一个 bug 去查。"""
    proxy = _FakeProxy({"d1": _device(online=False), "d2": _device(online=False)}, {})

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    line = next(
        m for m in (r.getMessage() for r in caplog.records) if "no readable" in m
    )
    assert "online flags only" in line
    assert "devices=2" in line


async def test_a_batch_swallowed_by_the_leaf_limit_is_not_counted_as_written(
    store, caplog, monkeypatch
):
    """上限拒收不抛异常，`set` 没抛就当全写进去了会让汇总行报出树里根本没有的条数。"""
    monkeypatch.setattr("miloco.state.store.MAX_LEAVES", 1)
    store._commit("iot/device/other/status/online", True, source="x")

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        written = _write_device(store, "d1", {"2.1": 26, "2.2": 5}, _Samples())

    assert written == 0
    assert store.stats()["leaves"] == 1
    assert any("leaf limit" in r.getMessage() for r in caplog.records)


async def test_row_without_value_reports_the_actual_code(store, caplog):
    """判据换成 is_failure 之后，能走到这条日志的 code 不止 0。"""
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": -702000000}})

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    line = next(m for m in (r.getMessage() for r in caplog.records) if "no value" in m)
    assert "-702000000" in line


async def test_summary_keeps_devices_and_offline_on_the_same_denominator(store, caplog):
    """两个数并排放着，最自然的读法是「一共这么多、其中这些离线」，分母不同就会读错。"""
    proxy = _FakeProxy(
        {
            "on1": _device(online=True),
            "off1": _device(online=False),
            "off2": _device(online=False),
        },
        {("on1", 2, 1): {"code": 0, "value": 26}},
    )

    with caplog.at_level(logging.INFO, logger="miloco.miot.state_align"):
        await _align(store, proxy)

    summary = next(
        m for m in (r.getMessage() for r in caplog.records) if "align done" in m
    )
    # 不能写 "devices=3" —— written_devices=3 会把它顺带匹配上
    assert re.search(r"\bdevices=3 offline=2 ", summary), summary


async def test_leaf_limit_warnings_are_rate_limited(store, caplog, monkeypatch):
    """上限一撞就全撞，剩下每台各打一条会把容器自己那条真正该看的日志淹掉。"""
    monkeypatch.setattr("miloco.state.store.MAX_LEAVES", 1)
    store._commit("iot/device/seed/status/online", True, source="x")
    samples = _Samples()

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        for index in range(SAMPLE_LIMIT + 3):
            _write_device(store, f"d{index}", {"2.1": index}, samples)

    lines = [m for m in (r.getMessage() for r in caplog.records) if "leaf limit" in m]
    assert len(lines) == SAMPLE_LIMIT
    assert samples.counts["leaf_limit"] == SAMPLE_LIMIT + 3


async def test_batch_rejection_warnings_are_rate_limited(store, caplog):
    """畸形值可能整批设备都有（同型号同固件），这条也得受额度约束。"""
    samples = _Samples()

    with caplog.at_level(logging.WARNING, logger="miloco.miot.state_align"):
        for index in range(SAMPLE_LIMIT + 3):
            _write_device(store, f"d{index}", {"2.1": float("nan")}, samples)

    lines = [
        m
        for m in (r.getMessage() for r in caplog.records)
        if "batch write rejected" in m
    ]
    assert len(lines) == SAMPLE_LIMIT


# ── 对齐给出「这一轮跑没跑完」的判定 ───────────────────────────────────


async def test_a_finished_round_reports_success(store):
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": 1}})

    assert await _align(store, proxy) is True


async def test_a_home_without_any_readable_property_still_counts_as_aligned(store):
    """零可读属性是合法终态。判成失败会让属性订阅那道门永远打不开。"""
    proxy = _FakeProxy({"d1": _device()}, {})

    assert await _align(store, proxy) is True


async def test_a_device_that_fails_to_read_does_not_fail_the_round(store):
    """对齐从来不保证全读到，一台坏设备卡死整条链路是更坏的结果。"""
    proxy = _FakeProxy(
        {"ok": _device(), "bad": _device()},
        {("ok", 2, 1): {"code": 0, "value": 1}, ("bad", 2, 1): {"code": KNOWN_CODE}},
    )

    assert await _align(store, proxy) is True


async def test_an_unreachable_device_list_fails_the_round(store):
    class _Boom(_FakeProxy):
        async def devices_in_current_home(self) -> dict:
            raise RuntimeError("boom")

    assert await _align(store, _Boom({}, {})) is False


async def test_no_enabled_home_fails_the_round(store):
    """空作用域没有「对齐完成」可言，这条兜住把对齐排在建立启用集之前的顺序错误。"""
    proxy = _FakeProxy({}, {})
    proxy.enabled_home = False

    assert await _align(store, proxy) is False


async def test_an_enabled_home_with_no_device_counts_as_aligned(store):
    """判据是启用集空不空，不是家庭里有几台设备。

    家庭是空的时候容器本来就该是空的，这已经对齐了。判成失败会让这一代的属性订阅
    门一直关着，之后往这个家庭里加设备也订不上。
    """
    proxy = _FakeProxy({}, {})

    assert await _align(store, proxy) is True


# ── 作用域代号：对的是上一代，一条都不该落地 ───────────────────────────


class _CountingProxy(_FakeProxy):
    """记下拉过几次 spec —— 用来验证代号已经变了就不该再打云端。"""

    def __init__(self, devices: dict, rows: dict):
        super().__init__(devices, rows)
        self.spec_calls = 0

    async def get_readable_prop_iids(self, did: str) -> list[str]:
        self.spec_calls += 1
        return await super().get_readable_prop_iids(did)


async def test_a_round_that_starts_in_a_stale_scope_does_not_touch_the_cloud(store):
    """代号进来就不对，整轮云端请求都是白打的。"""
    proxy = _CountingProxy({"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": 1}})

    landed = await align_iot_state(store, proxy, scope=0, current_scope=lambda: 1)

    assert landed is False
    assert proxy.spec_calls == 0
    assert store.stats()["leaves"] == 0


async def test_a_scope_switch_halfway_through_the_flags_stops_the_rest(store):
    """写在线标志之间也有让出点，切换落在这里同样要停手。"""
    proxy = _FakeProxy(
        {"d1": _device(), "d2": _device()},
        {("d1", 2, 1): {"code": 0, "value": 1}, ("d2", 2, 1): {"code": 0, "value": 2}},
    )

    def current_scope() -> int:
        # 拿容器里的痕迹当触发条件，不数闸被读了几次 —— 读几次是实现细节
        return 1 if store.get("iot/device/d1/status/online", None) is True else 0

    landed = await align_iot_state(store, proxy, scope=0, current_scope=current_scope)

    assert landed is False
    assert store.get("iot/device/d1/status/online") is True
    assert store.get("iot/device/d2/status/online") is MISSING


async def test_a_scope_switch_during_the_read_drops_every_property(store):
    """读属性要几秒，切换最可能落在这里；半轮旧数据比没有数据更难查。"""
    scope = {"now": 0}

    class _SwitchesWhileReading(_FakeProxy):
        async def get_device_properties(self, params: list) -> list[dict]:
            scope["now"] = 1
            return await super().get_device_properties(params)

    proxy = _SwitchesWhileReading(
        {"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": 1}}
    )

    landed = await align_iot_state(
        store, proxy, scope=0, current_scope=lambda: scope["now"]
    )

    assert landed is False
    assert store.snapshot("iot/device/d1/prop/**") == {}


async def test_a_round_that_keeps_its_scope_finishes_normally(store):
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": 1}})

    landed = await align_iot_state(store, proxy, scope=7, current_scope=lambda: 7)

    assert landed is True
    assert store.get("iot/device/d1/prop/2.1") == 1
