# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for device property history: DevicePropHistoryDao + DevicePropPoller.

Poller 的核心正确性约束:
* 值变化才落库(同值周期零写入,连日志都不刷);
* 首见 key 对 DB 最新行 diff——重启后值未变不写重复基线行,停机窗口内变了补一行;
* watchlist = 在线设备 prop.2.1 + prop_history_poll_extra 配置项(非法条目跳过)。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()
    yield db_file
    reset_settings()


@pytest.fixture
def dao(real_db):
    from miloco.database.prop_history_dao import DevicePropHistoryDao

    return DevicePropHistoryDao(retention_days=30)


# ------------------------------------------------------------------------ DAO


def _now() -> int:
    import time

    return int(time.time() * 1000)


def test_dao_insert_and_query_roundtrip(dao):
    assert dao.insert_changes("d1", [(2, 1, True), (2, 4, 26)], ts_ms := _now())
    rows = dao.query("d1")
    assert len(rows) == 2
    by_iid = {r["iid"]: r for r in rows}
    # bool / int JSON 往返无损
    assert by_iid["prop.2.1"]["value"] is True
    assert by_iid["prop.2.4"]["value"] == 26
    assert by_iid["prop.2.1"]["ts"] == ts_ms


def test_dao_query_single_prop_and_time_range(dao):
    t1, t2 = _now() - 1000, _now()
    dao.insert_changes("d1", [(2, 1, False)], t1)
    dao.insert_changes("d1", [(2, 1, True)], t2)
    dao.insert_changes("d1", [(3, 1, "x")], t2)
    dao.insert_changes("d2", [(2, 1, True)], t2)

    rows = dao.query("d1", siid=2, piid=1)
    assert [(r["ts"], r["value"]) for r in rows] == [(t2, True), (t1, False)]

    rows = dao.query("d1", siid=2, piid=1, since_ms=t1 + 500)
    assert len(rows) == 1 and rows[0]["ts"] == t2

    rows = dao.query("d1", siid=2, piid=1, until_ms=t1 + 500)
    assert len(rows) == 1 and rows[0]["ts"] == t1

    assert len(dao.query("d1", limit=2)) == 2  # limit 生效


def test_dao_null_and_string_values(dao):
    dao.insert_changes("d1", [(2, 1, None), (2, 2, "auto")], _now())
    by_iid = {r["iid"]: r["value"] for r in dao.query("d1")}
    assert by_iid["prop.2.1"] is None
    assert by_iid["prop.2.2"] == "auto"


def test_dao_empty_changes_noop(dao):
    assert dao.insert_changes("d1", [], _now())
    assert dao.query("d1") == []


# --------------------------------------------------------------------- poller


def _make_poller(monkeypatch, dao, *, devices, results, extra=None):
    """Build a DevicePropPoller against fakes.

    devices: {did: online}; results: get_props_async 返回值。
    """
    from miloco.miot.prop_poller import DevicePropPoller

    settings = SimpleNamespace(
        miot=SimpleNamespace(
            prop_history_enabled=True,
            prop_history_poll_interval_sec=60,
            prop_history_poll_extra=extra or [],
            prop_history_retention_days=30,
        )
    )
    import miloco.miot.prop_poller as poller_mod

    monkeypatch.setattr(poller_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "miloco.manager.get_manager",
        lambda: SimpleNamespace(device_prop_history_dao=dao),
    )

    proxy = SimpleNamespace(
        is_authenticated=True,
        _device_info_dict={
            did: SimpleNamespace(online=online) for did, online in devices.items()
        },
        _miot_client=SimpleNamespace(
            http_client=SimpleNamespace(get_props_async=AsyncMock(return_value=results))
        ),
    )
    return DevicePropPoller(proxy), proxy


@pytest.mark.asyncio
async def test_poller_first_sight_writes_baseline_once(monkeypatch, dao):
    results = [{"did": "d1", "siid": 2, "piid": 1, "value": True}]
    poller, _ = _make_poller(monkeypatch, dao, devices={"d1": True}, results=results)

    await poller._poll_once()
    assert len(dao.query("d1")) == 1  # 空库首见 → 基线行

    await poller._poll_once()
    assert len(dao.query("d1")) == 1  # 同值周期零写入


@pytest.mark.asyncio
async def test_poller_writes_on_change_only(monkeypatch, dao):
    results = [{"did": "d1", "siid": 2, "piid": 1, "value": True}]
    poller, proxy = _make_poller(monkeypatch, dao, devices={"d1": True}, results=results)
    await poller._poll_once()

    proxy._miot_client.http_client.get_props_async.return_value = [
        {"did": "d1", "siid": 2, "piid": 1, "value": False}
    ]
    await poller._poll_once()
    rows = dao.query("d1", siid=2, piid=1)
    assert [r["value"] for r in rows] == [False, True]


@pytest.mark.asyncio
async def test_poller_restart_skips_baseline_when_db_matches(monkeypatch, dao):
    # 模拟重启前库里已有同值最新行
    dao.insert_changes("d1", [(2, 1, True)], _now() - 60000)
    results = [{"did": "d1", "siid": 2, "piid": 1, "value": True}]
    poller, _ = _make_poller(monkeypatch, dao, devices={"d1": True}, results=results)
    await poller._poll_once()
    assert len(dao.query("d1")) == 1  # 不产生重复基线行

    # 停机窗口内值变了 → 补一行保持时间线连续
    dao2_results = [{"did": "d1", "siid": 2, "piid": 1, "value": False}]
    poller2, _ = _make_poller(monkeypatch, dao, devices={"d1": True}, results=dao2_results)
    await poller2._poll_once()
    assert [r["value"] for r in dao.query("d1")] == [False, True]


@pytest.mark.asyncio
async def test_poller_skips_failed_entries_and_offline_devices(monkeypatch, dao):
    results = [
        {"did": "d1", "siid": 2, "piid": 1},  # 无 value = 云端读失败/设备无此属性
        {"did": "d1", "siid": 2, "code": -704},  # 缺 piid
    ]
    poller, proxy = _make_poller(
        monkeypatch, dao, devices={"d1": True, "d-off": False}, results=results
    )
    await poller._poll_once()
    assert dao.query("d1") == []
    # 离线设备不进 watchlist
    params = proxy._miot_client.http_client.get_props_async.await_args.args[0]
    assert all(p.did != "d-off" for p in params)


@pytest.mark.asyncio
async def test_poller_extra_watchlist_and_invalid_entries(monkeypatch, dao):
    poller, proxy = _make_poller(
        monkeypatch,
        dao,
        devices={"d1": True},
        results=[],
        extra=["d1:prop.4.1", "d9:5.2", "garbage", "d1:prop.2.1"],  # 最后一条与默认重复
    )
    await poller._poll_once()
    params = proxy._miot_client.http_client.get_props_async.await_args.args[0]
    keys = {(p.did, p.siid, p.piid) for p in params}
    assert keys == {("d1", 2, 1), ("d1", 4, 1), ("d9", 5, 2)}


# ------------------------------------------- push path: spec 分类 + 节流落库
#
# 这一段守的是 2026-07-29 实测暴露的两个真实问题:
# 1. 推送 topic 带 /{siid}/{piid} 两级,订阅与 decoder 都必须覆盖(否则静默丢弃);
# 2. 遥测属性秒级刷屏(一台空调 30 分钟 858 条),必须在落库前节流。


def _proxy_with_spec(spec: dict, *, urn: str = "urn:x:ac"):
    """Bare MiotProxy carrying just what _classify_prop reads."""
    from miloco.miot.client import MiotProxy

    proxy = MiotProxy.__new__(MiotProxy)
    proxy._device_info_dict = {"ac": SimpleNamespace(urn=urn)}
    proxy._spec_cache = {urn: spec}
    return proxy


def test_classify_prop_reads_spec_semantics():
    from miloco.miot.prop_throttle import DISCRETE, TELEMETRY

    proxy = _proxy_with_spec(
        {
            "prop.2.1": {"format": "bool", "writeable": True},          # 开关
            "prop.2.2": {"format": "uint8", "value_list": [{"value": 2}]},  # 模式
            "prop.2.4": {"format": "float", "unit": "celsius",           # 设定温度
                         "writeable": True, "value_range": [16, 31, 0.5]},
            "prop.4.9": {"format": "uint8", "unit": "percentage",        # 环境湿度
                         "writeable": False, "value_range": [0, 100, 1]},
        }
    )
    assert proxy._classify_prop("ac", 2, 1) == (DISCRETE, None)
    assert proxy._classify_prop("ac", 2, 2) == (DISCRETE, None)
    # 可写 > 有单位:设定温度是用户意图,不能因 unit=celsius 被当成遥测
    assert proxy._classify_prop("ac", 2, 4) == (DISCRETE, None)
    # 只读 + 量程 → 遥测,并带回满量程跨度供幅度判定
    assert proxy._classify_prop("ac", 4, 9) == (TELEMETRY, 100.0)


def test_classify_prop_unknown_device_or_cold_cache():
    proxy = _proxy_with_spec({"prop.2.1": {"format": "bool"}})
    assert proxy._classify_prop("missing-did", 2, 1) is None
    proxy._spec_cache = {}                      # 缓存冷 → 交给频率启发式
    assert proxy._classify_prop("ac", 2, 1) is None


def test_classify_prop_vendor_slot_absent_from_spec():
    """厂商私有槽位(实测空调 prop.12.*)按最严格遥测处理,而不是放行。"""
    from miloco.miot.prop_throttle import TELEMETRY

    proxy = _proxy_with_spec({"prop.2.1": {"format": "bool"}})
    assert proxy._classify_prop("ac", 12, 3) == (TELEMETRY, None)


def test_push_handler_throttles_before_persisting(monkeypatch, dao):
    """整包重发里的同值条目不落库,开关变化必须落库。"""
    from miloco.miot.client import MiotProxy
    from miloco.miot.prop_throttle import PropChangeThrottle

    monkeypatch.setattr(
        "miloco.manager.get_manager",
        lambda: SimpleNamespace(device_prop_history_dao=dao),
    )
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._prop_throttle = PropChangeThrottle(classify=None)
    proxy._prop_throttle_log_countdown = 10_000

    def push(changes, ts):
        proxy._on_device_prop_changed_event(
            SimpleNamespace(
                did="ac",
                timestamp_ms=ts,
                changes=[SimpleNamespace(siid=s, piid=p, value=v) for s, p, v in changes],
            )
        )

    base = _now()
    push([(2, 1, True)], base)
    push([(2, 1, True)], base + 1_000)   # 整包重发,同值 → 不落库
    push([(2, 1, False)], base + 2_000)
    rows = dao.query("ac", siid=2, piid=1, limit=10)
    assert [r["value"] for r in rows] == [False, True]


def test_push_handler_without_throttle_persists_everything(monkeypatch, dao):
    """节流关闭时行为不变(排障用的逃生开关)。"""
    from miloco.miot.client import MiotProxy

    monkeypatch.setattr(
        "miloco.manager.get_manager",
        lambda: SimpleNamespace(device_prop_history_dao=dao),
    )
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._prop_throttle = None
    proxy._prop_throttle_log_countdown = 10_000
    base = _now()
    for ts in (base, base + 1_000, base + 2_000):
        proxy._on_device_prop_changed_event(
            SimpleNamespace(
                did="ac",
                timestamp_ms=ts,
                changes=[SimpleNamespace(siid=12, piid=3, value=1140)],
            )
        )
    assert len(dao.query("ac", siid=12, piid=3, limit=10)) == 3


# ---------------------------------------------------------------- API 参数校验


def test_prop_history_endpoint_rejects_malformed_iid():
    """非法 iid 必须返回 400，而不是把 TypeError 冒到 500。

    本仓库的 HTTPException 签名是 ``(message, status_code)``，与 FastAPI 的
    ``(status_code, detail)`` 不同；按后者写会在构造异常时就 TypeError，用户
    拿到的是 500 而非参数错误。这条守住调用签名。
    """
    import asyncio

    from miloco.middleware.exceptions import HTTPException
    from miloco.miot.router import get_prop_history

    with pytest.raises(HTTPException) as ei:
        asyncio.run(get_prop_history(did="d1", iid="不是iid", current_user="t"))
    assert ei.value.http_status == 400
    assert "prop.S.P" in str(ei.value.message)
