"""Integration: control_device / get_device_status 成功路径自动写 LRU。

LRUStore 直接打 SQLite（temp file），不 mock；MiotProxy 用最小 stub 替代以
避免拉起整套客户端栈。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.miot.schema import DeviceControlRequest, PropertyItem
from miloco.miot.service import MiotService


class _DBConnector:
    """与 test_lru_store._TestConnector 同形：execute_update / execute_query。"""

    def __init__(self, path: Path):
        self._path = str(path)
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """
                CREATE TABLE device_lru (
                    did TEXT NOT NULL,
                    key TEXT NOT NULL,
                    touched_at INTEGER NOT NULL,
                    PRIMARY KEY (did, key)
                )
                """
            )

    def execute_update(self, sql, params=None):
        with sqlite3.connect(self._path) as conn:
            cur = conn.cursor()
            cur.execute(sql, params or ())
            conn.commit()
            return cur.rowcount

    def execute_query(self, sql, params=None):
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]


def _make_service(tmp_path: Path) -> tuple[MiotService, _DBConnector]:
    import json

    from miloco.database.kv_repo import ScopeConfigKeys

    db = _DBConnector(tmp_path / "lru.sqlite")
    # 默认启用 H1 家庭，让 control_device 不被空启用集阻断
    store: dict[str, str] = {
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
    }
    proxy = SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=db,
            get=lambda key, default=None: store.get(key, default),
            set=lambda key, value: store.__setitem__(key, value) or True,
        ),
        set_device_properties=AsyncMock(return_value=[{"code": 0, "siid": 2, "piid": 1}]),
        call_device_action=AsyncMock(return_value={"code": 0}),
        get_devices=AsyncMock(return_value={"dev1": SimpleNamespace(home_id="H1")}),
        get_device_properties=AsyncMock(
            return_value=[{"siid": 2, "piid": 1, "value": True, "code": 0}]
        ),
        get_readable_prop_iids=AsyncMock(return_value=["prop.2.1"]),
    )
    return MiotService(miot_proxy=proxy), db


@pytest.mark.asyncio
async def test_set_property_writes_lru(tmp_path):
    svc, _ = _make_service(tmp_path)
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
    await svc.control_device("dev1", req)
    assert (await svc.lru_snapshot())["histories"]["dev1"] == ["prop.2.1"]


@pytest.mark.asyncio
async def test_set_properties_writes_all_iids(tmp_path):
    svc, _ = _make_service(tmp_path)
    svc._miot_proxy.set_device_properties.return_value = [
        {"code": 0, "siid": 2, "piid": 1},
        {"code": 0, "siid": 2, "piid": 2},
    ]
    req = DeviceControlRequest(
        type="set_properties",
        properties=[
            PropertyItem(iid="prop.2.1", value=True),
            PropertyItem(iid="prop.2.2", value=80),
        ],
    )
    await svc.control_device("dev1", req)
    buf = (await svc.lru_snapshot())["histories"]["dev1"]
    # MRU 在前；touch 顺序 prop.2.1 → prop.2.2，所以 prop.2.2 在头部
    assert buf == ["prop.2.2", "prop.2.1"]


@pytest.mark.asyncio
async def test_call_action_writes_lru(tmp_path):
    svc, _ = _make_service(tmp_path)
    req = DeviceControlRequest(type="call_action", iid="action.5.1", params=[])
    await svc.control_device("dev1", req)
    assert (await svc.lru_snapshot())["histories"]["dev1"] == ["action.5.1"]


@pytest.mark.asyncio
async def test_get_device_status_writes_lru_only_when_user_specified(tmp_path):
    svc, _ = _make_service(tmp_path)
    # 用户主动指定 → 写
    await svc.get_device_status("dev1", ["prop.2.1"])
    snap = (await svc.lru_snapshot())["histories"]
    assert snap["dev1"] == ["prop.2.1"]


@pytest.mark.asyncio
async def test_get_device_status_skips_lru_on_full_query(tmp_path):
    svc, _ = _make_service(tmp_path)
    # 不传 iids → 冷查询，不写
    await svc.get_device_status("dev1", None)
    assert (await svc.lru_snapshot())["histories"] == {}


@pytest.mark.asyncio
async def test_lru_failure_does_not_break_control(tmp_path, monkeypatch):
    """LRU 写挂掉时 control 仍要正常返回结果。"""
    svc, _ = _make_service(tmp_path)
    monkeypatch.setattr(
        svc._lru, "touch", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
    result = await svc.control_device("dev1", req)
    assert "results" in result


# ── 陈旧读数 ──────────────────────────────────────────────────────────────
#
# 云端对不可达设备返回**最后已知值 + code 0**,响应里没有任何一处承认存疑。
# 实测真实账号上的读数年龄:离线加湿器 182.4 天、离线驱蚊器 21.1 天,而**在线**的
# 净饮机也有 25.1 小时 —— 所以「在线/离线」不是正确的判据轴,「多老」才是。
#
# 后果是有事故的:一条「主卧灯还亮着就播报睡眠提醒」的定时任务,因为灯离线后缓存
# 永远停在 on=true,00:00~02:30 每 30 分钟朝卧室音箱播报一次。


@pytest.mark.asyncio
async def test_targeted_query_reads_through_to_the_device(tmp_path):
    """传了 iid = 调用方在**拿这个值当判据**,必须读真机。

    读缓存的话它拿到的是一个自称成功、实则可能几个月前的值,而响应里没有任何
    线索能让它分辨。事故那条定时任务的日志行正是 `iid: prop.2.1` —— 走这一档
    之后,离线设备由云端自己返回 -704042011,判据自然为假。
    """
    svc, _ = _make_service(tmp_path)
    await svc.get_device_status("dev1", ["prop.2.1"])
    assert svc._miot_proxy.get_device_properties.await_args.kwargs["datasource"] == 2


@pytest.mark.asyncio
async def test_full_cold_query_stays_on_the_cheap_cache(tmp_path):
    """不传 iid = 面板那种全量冷查询。WebUI 一屏 30+ 台同时打,那边的注释已写明
    会撞上 MiOT 约 10 QPS 的限频 —— 必须留在便宜的缓存读上。"""
    svc, _ = _make_service(tmp_path)
    await svc.get_device_status("dev1", None)
    assert svc._miot_proxy.get_device_properties.await_args.kwargs["datasource"] == 1


@pytest.mark.asyncio
async def test_update_time_is_passed_through(tmp_path):
    """云端每一行状态属性都带 updateTime。此前这里把它丢掉了,于是一个 182 天前的
    读数与一个刚读到的读数在响应里完全同形。"""
    svc, _ = _make_service(tmp_path)
    svc._miot_proxy.get_device_properties.return_value = [
        {"siid": 2, "piid": 1, "value": True, "code": 0, "updateTime": 1769919538}
    ]
    out = await svc.get_device_status("dev1", ["prop.2.1"])
    assert out["properties"][0]["updated_at"] == 1769919538


@pytest.mark.asyncio
async def test_missing_update_time_is_none_not_zero(tmp_path):
    """siid=1 那组静态设备信息(厂商/型号/序列号)本来就没有这个字段。
    给 0 会让调用方算出 56 年的年龄,比不给更糟。"""
    svc, _ = _make_service(tmp_path)
    svc._miot_proxy.get_device_properties.return_value = [
        {"siid": 1, "piid": 1, "value": "xiaomi", "code": 0}
    ]
    out = await svc.get_device_status("dev1", ["prop.1.1"])
    assert out["properties"][0]["updated_at"] is None


@pytest.mark.asyncio
async def test_cloud_reported_codes_are_not_rewritten(tmp_path):
    """**不伪造 code。** 离线设备的返回是混合体:同一次查询里既有缓存命中的
    `code:0`,也有云端自己报的 `-704042011`(实测电饭煲 15 个属性里 4 个如此)。
    在后端合成一个码会覆盖掉云端逐属性给出的真实结果,也正撞 #394 的立意
    (只有云端返回的负码才算失败,本地不许凭空判定)。
    """
    svc, _ = _make_service(tmp_path)
    svc._miot_proxy.get_device_properties.return_value = [
        {"siid": 2, "piid": 1, "value": 4, "code": 0, "updateTime": 1785408130},
        {"siid": 2, "piid": 17, "value": None, "code": -704042011},
    ]
    props = (await svc.get_device_status("dev1", ["prop.2.1", "prop.2.17"]))["properties"]
    assert [p["code"] for p in props] == [0, -704042011]
    assert [p["value"] for p in props] == [4, None]
