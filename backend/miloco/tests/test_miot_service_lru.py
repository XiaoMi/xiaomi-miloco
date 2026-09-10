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
        # 真实代理有这个判据，夹具也要有：缺了它，生产侧任何「先问一句能不能用」
        # 的检查都会在这里撞 AttributeError，而那不是生产缺陷、是夹具没建模。
        is_operational=True,
        _kv_repo=SimpleNamespace(
            db_connector=db,
            get=lambda key, default=None: store.get(key, default),
            set=lambda key, value: store.__setitem__(key, value) or True,
        ),
        set_device_properties=AsyncMock(return_value=[{"code": 0, "siid": 2, "piid": 1}]),
        call_device_action=AsyncMock(return_value={"code": 0}),
        get_devices=AsyncMock(return_value={"dev1": SimpleNamespace(home_id="H1")}),
        # 真实代理另有一对**不触发刷新**的取数口（降级态下写台账走它们，避免为一行
        # 留痕去打一趟注定 401 的云端）。夹具让它们与上面那两个刷新口返回同一份，
        # 缺了它们，被拒那条路会在这里撞 AttributeError。
        cached_devices={"dev1": SimpleNamespace(home_id="H1")},
        get_cached_camera=lambda did: None,
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
