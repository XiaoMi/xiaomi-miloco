# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""启动对齐的写入范围与日志分级。

不打真机：MiotProxy 用最小 stub 替代，StateStore 是真的 —— 断言看的是容器里最终
长出什么路径，而不是 stub 被怎么调用。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from miloco.miot.state_align import align_iot_state
from miloco.state import MISSING, StateStore

KNOWN_CODE = -704220043  # 属性值不正确
UNKNOWN_CODE = -704010000  # 码表里没有


def _device(*, online: bool = True, model: str = "acme-x1") -> SimpleNamespace:
    return SimpleNamespace(online=online, model=model, urn="urn:test")


class _FakeProxy:
    """rows 的键是 (did, siid, piid)，值是不含 id 字段的那半个响应行。"""

    def __init__(self, devices: dict, rows: dict):
        self._devices = devices
        self._rows = rows
        self.requested: list[tuple[str, int, int]] = []

    async def get_devices(self) -> dict:
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

    await align_iot_state(store, proxy)

    assert store.get("iot/d1/online") is True
    assert store.get("iot/d1/prop/2.1") == 21.5
    # online 是叶子、prop 是子树，同一层混放 —— 写的顺序不能把谁翻成另一种形态
    assert store.stats()["shape_flips"] == 0


async def test_offline_device_properties_are_not_requested(store):
    proxy = _FakeProxy(
        {"d1": _device(online=False)},
        {("d1", 2, 1): {"code": 0, "value": 21.5}},
    )

    await align_iot_state(store, proxy)

    assert proxy.requested == []
    assert store.get("iot/d1/prop/2.1") is MISSING


async def test_offline_device_still_gets_its_flag(store):
    """跳过属性但不能整台跳过：容器里没有这台设备，就分不出「离线」和「没接入」。"""
    proxy = _FakeProxy(
        {"d1": _device(online=False)}, {("d1", 2, 1): {"code": 0, "value": 1}}
    )

    await align_iot_state(store, proxy)

    assert store.get("iot/d1/online") is False


async def test_offline_device_does_not_block_online_ones(store):
    proxy = _FakeProxy(
        {"off": _device(online=False), "on": _device(online=True)},
        {("off", 2, 1): {"code": 0, "value": 1}, ("on", 2, 1): {"code": 0, "value": 2}},
    )

    await align_iot_state(store, proxy)

    assert store.get("iot/on/prop/2.1") == 2
    assert store.get("iot/off/prop/2.1") is MISSING
    assert store.get("iot/off/online") is False


async def test_did_with_slash_gets_neither_flag_nor_properties(store):
    """'/' 是路径分隔符，这种 did 连 online 标志都写不进去。"""
    proxy = _FakeProxy(
        {"a/b": _device(online=True)}, {("a/b", 2, 1): {"code": 0, "value": 1}}
    )

    await align_iot_state(store, proxy)

    assert store.snapshot("iot/**") == {}
    assert proxy.requested == []


# ── 待办 2：读失败日志按返回码分级 ─────────────────────────────────────


async def test_known_failure_code_does_not_warn(store, caplog):
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": KNOWN_CODE}})

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await align_iot_state(store, proxy)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []
    assert any(str(KNOWN_CODE) in r.getMessage() for r in caplog.records)


async def test_unknown_failure_code_warns(store, caplog):
    """码表里没有的码是唯一真该看的：它没人解释过。"""
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": UNKNOWN_CODE}})

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await align_iot_state(store, proxy)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(str(UNKNOWN_CODE) in m for m in warnings)


async def test_failure_line_carries_model(store, caplog):
    """光有 did 定位不到属性语义，要靠 model 去 spec 里反查。"""
    proxy = _FakeProxy(
        {"d1": _device(model="cgllc-b1")}, {("d1", 2, 5): {"code": KNOWN_CODE}}
    )

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await align_iot_state(store, proxy)

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
        await align_iot_state(store, proxy)

    summary = next(
        m for m in (r.getMessage() for r in caplog.records) if "align done" in m
    )
    assert "属性值不正确" in summary
    assert "cgllc-b1" in summary and "zhimi-ma2" in summary


# ── 既有行为的补测 ────────────────────────────────────────────────────


async def test_code_zero_without_value_is_not_written(store):
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": 0}})

    await align_iot_state(store, proxy)

    assert store.get("iot/d1/prop/2.1") is MISSING


async def test_none_is_written_because_it_is_a_legal_value(store):
    proxy = _FakeProxy({"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": None}})

    await align_iot_state(store, proxy)

    assert store.get("iot/d1/prop/2.1") is None


async def test_non_scalar_value_is_reported(store, caplog):
    proxy = _FakeProxy(
        {"d1": _device()}, {("d1", 2, 1): {"code": 0, "value": {"a": 1}}}
    )

    with caplog.at_level(logging.DEBUG, logger="miloco.miot.state_align"):
        await align_iot_state(store, proxy)

    assert any("non-scalar" in r.getMessage() for r in caplog.records)


async def test_one_bad_value_does_not_lose_the_whole_device(store):
    """容器的校验是整笔不写，所以整台写失败要退成逐条写。"""
    proxy = _FakeProxy(
        {"d1": _device()},
        {
            ("d1", 2, 1): {"code": 0, "value": float("nan")},
            ("d1", 2, 2): {"code": 0, "value": 7},
        },
    )

    await align_iot_state(store, proxy)

    assert store.get("iot/d1/prop/2.2") == 7
    assert store.get("iot/d1/prop/2.1") is MISSING


async def test_align_never_raises_when_the_proxy_explodes(store):
    class _Boom:
        async def get_devices(self):
            raise RuntimeError("boom")

    await align_iot_state(store, _Boom())  # 不抛就算过


async def test_align_reports_when_nothing_is_readable(store, caplog):
    proxy = _FakeProxy({"d1": _device(online=False)}, {})

    with caplog.at_level(logging.INFO, logger="miloco.miot.state_align"):
        await align_iot_state(store, proxy)

    assert any("nothing" in r.getMessage() for r in caplog.records)
    # 一条属性都读不到也要先把标志写下去，否则这台设备在容器里根本不存在
    assert store.get("iot/d1/online") is False
