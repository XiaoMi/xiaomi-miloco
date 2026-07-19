"""action_ledger v1:control_device / trigger_scene 落审计行 + fail-open。

MetricsClient 打真 SQLite(temp observability db,不 mock);MiotProxy 用最小 stub
(同 test_miot_service_lru 的 SimpleNamespace 手法),避免拉起整套客户端栈。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.miot.schema import DeviceControlRequest
from miloco.miot.service import MiotService
from miloco.observability import metrics_client as mc
from miloco.observability.metrics_client import MetricsClient


class _DBConnector:
    """control_device 成功路径会写 LRU(SQLite),给个最小 device_lru 表。"""

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


def _make_service(tmp_path: Path) -> MiotService:
    from miloco.database.kv_repo import ScopeConfigKeys

    db = _DBConnector(tmp_path / "lru.sqlite")
    store: dict[str, str] = {
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
    }
    dev = SimpleNamespace(home_id="H1", name="台灯", room_name="客厅")
    proxy = SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=db,
            get=lambda key, default=None: store.get(key, default),
            set=lambda key, value: store.__setitem__(key, value) or True,
        ),
        set_device_properties=AsyncMock(
            return_value=[{"code": 0, "siid": 2, "piid": 1}]
        ),
        call_device_action=AsyncMock(return_value={"code": 0}),
        get_devices=AsyncMock(return_value={"dev1": dev}),
        # 摄像头只在 camera cache(MIoTCameraInfo 继承 MIoTDeviceInfo 同字段)
        get_cameras=AsyncMock(
            return_value={
                "cam1": SimpleNamespace(
                    home_id="H1", name="门口摄像头", room_name="门口"
                )
            }
        ),
        get_all_scenes=AsyncMock(
            return_value={"scene1": SimpleNamespace(home_id="H1", scene_name="回家")}
        ),
        execute_miot_scene=AsyncMock(return_value=True),
    )
    return MiotService(miot_proxy=proxy)


@pytest.fixture
async def bound_client(tmp_path):
    """启动真 MetricsClient 并绑到 module-level singleton;测后解绑。"""
    obs_db = tmp_path / "observability.db"
    client = MetricsClient(db_path=obs_db)
    await client.start()
    mc.set_metrics_client(client)
    try:
        yield client, obs_db
    finally:
        mc.set_metrics_client(None)
        await client.stop()


def _rows(obs_db: Path) -> list[dict]:
    conn = sqlite3.connect(str(obs_db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM action_ledger ORDER BY timestamp"
        ).fetchall()]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_set_property_writes_ledger_row(bound_client, tmp_path):
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
    await svc.control_device("dev1", req)
    await client.flush()

    rows = _rows(obs_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["action_type"] == "set_property"
    assert r["did"] == "dev1"
    assert r["iid"] == "prop.2.1"
    assert r["device_name"] == "台灯"
    assert r["room"] == "客厅"
    assert r["success"] == 1
    assert r["result_code"] is None  # 成功无 worst_code
    assert json.loads(r["value_json"]) is True


@pytest.mark.asyncio
async def test_control_device_records_source_cli(bound_client, tmp_path):
    """control_device 路径台账 source=cli(默认)、source_id 空——与 rule static 区分。"""
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    await svc.control_device(
        "dev1", DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
    )
    await client.flush()
    r = _rows(obs_db)[0]
    assert r["source"] == "cli"
    assert r["source_id"] is None


@pytest.mark.asyncio
async def test_writer_records_source_rule(bound_client, tmp_path):
    """公共 helper 带 source=rule / source_id=rule_id 时台账落对应触发源。

    这是 rule static 直控路径复用的同一 helper(RuleRunner._execute_action 调用),
    验证 source 语义:trace_id 为 NULL 也能区分「手动 CLI」与「rule static」。
    """
    from miloco.miot.service import _write_action_ledger

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    await _write_action_ledger(
        svc._miot_proxy,
        action_type="set_property", did="dev1", iid="prop.2.1",
        value_json="true", result_code=0, result_msg=None,
        success=True, error=None, source="rule", source_id="rule-42",
    )
    await client.flush()
    r = _rows(obs_db)[0]
    assert r["source"] == "rule"
    assert r["source_id"] == "rule-42"


@pytest.mark.asyncio
async def test_ledger_records_device_home_id(bound_client, tmp_path):
    """v4:写入时从 device cache 解析设备所属家庭(dev1 ∈ H1),合流页才能按家过滤。"""
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    await svc.control_device(
        "dev1", DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
    )
    await client.flush()
    assert _rows(obs_db)[0]["home_id"] == "H1"


@pytest.mark.asyncio
async def test_ledger_home_id_null_when_device_unknown(bound_client, tmp_path):
    """device cache 查不到 did → home_id 落 NULL(fail-open,不影响审计主体)。"""
    from miloco.miot.service import _write_action_ledger

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    await _write_action_ledger(
        svc._miot_proxy,
        action_type="set_property", did="ghost", iid="prop.2.1",
        value_json="true", result_code=0, result_msg=None,
        success=True, error=None,
    )
    await client.flush()
    assert _rows(obs_db)[0]["home_id"] is None


@pytest.mark.asyncio
async def test_ledger_camera_fallback_resolves_home(bound_client, tmp_path):
    """did 只在 camera cache(get_devices miss)→ 回落 get_cameras 补齐
    home/name/room——否则摄像头动作 home_id=NULL,经 NULL 放行串到所有家。"""
    from miloco.miot.service import _write_action_ledger

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    await _write_action_ledger(
        svc._miot_proxy,
        action_type="set_property", did="cam1", iid="prop.2.1",
        value_json="true", result_code=0, result_msg=None,
        success=True, error=None,
    )
    await client.flush()
    r = _rows(obs_db)[0]
    assert r["home_id"] == "H1"
    assert r["device_name"] == "门口摄像头"
    assert r["room"] == "门口"


@pytest.mark.asyncio
async def test_ledger_explicit_home_skips_camera_fetch(bound_client, tmp_path):
    """home_id 已显式传入(scene_trigger 路径)→ 不回落 get_cameras——
    其 cache miss 会触发网络刷新,场景台账不该为此买单。"""
    from miloco.miot.service import _write_action_ledger

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    await _write_action_ledger(
        svc._miot_proxy,
        action_type="scene_trigger", did="scene-x", iid="scene-x",
        value_json=None, result_code=None, result_msg=None,
        success=True, error=None, home_id="H1",
    )
    await client.flush()
    svc._miot_proxy.get_cameras.assert_not_awaited()
    assert _rows(obs_db)[0]["home_id"] == "H1"


@pytest.mark.asyncio
async def test_scene_trigger_exception_keeps_scene_name(bound_client, tmp_path):
    """场景执行抛异常 → 台账仍带 scene_name(失败审计要能看到想触发什么)。"""
    from miloco.middleware.exceptions import MiotServiceException

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.execute_miot_scene = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(MiotServiceException):
        await svc.trigger_scene("scene1")
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["success"] == 0
    assert json.loads(r["value_json"]) == {"scene_name": "回家"}
    assert r["home_id"] == "H1"


@pytest.mark.asyncio
async def test_call_action_writes_ledger_with_tts_text(bound_client, tmp_path):
    """speaker play-text 也是 call_action:in_params(TTS 全文)进 value_json。"""
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    req = DeviceControlRequest(
        type="call_action", iid="action.5.1", params=["你好,回家啦"]
    )
    await svc.control_device("dev1", req)
    await client.flush()

    rows = _rows(obs_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["action_type"] == "call_action"
    assert r["success"] == 1
    assert json.loads(r["value_json"]) == ["你好,回家啦"]


@pytest.mark.asyncio
async def test_failure_code_decoded_in_ledger(bound_client, tmp_path):
    """设备侧负码 → success=0 + 中文 result_msg。"""
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.set_device_properties.return_value = [
        {"code": -704042011, "siid": 2, "piid": 1}
    ]
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
    await svc.control_device("dev1", req)
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["success"] == 0
    assert r["result_code"] == -704042011
    assert r["result_msg"] == "设备离线"


@pytest.mark.asyncio
async def test_exception_path_writes_failure_row(bound_client, tmp_path):
    """proxy 抛异常 → 落 success=0 + error 行,control_device 仍向上抛。"""
    from miloco.middleware.exceptions import MiotServiceException

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.call_device_action = AsyncMock(side_effect=RuntimeError("boom"))
    req = DeviceControlRequest(
        type="call_action", iid="action.5.1", params=["晚上好,回家啦"]
    )
    with pytest.raises(MiotServiceException):
        await svc.control_device("dev1", req)
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["success"] == 0
    assert r["action_type"] == "call_action"
    assert "boom" in (r["error"] or "")
    # 失败审计完整性:异常路径也保留尝试参数(当时想播什么 TTS/设什么值)
    assert json.loads(r["value_json"]) == ["晚上好,回家啦"]


@pytest.mark.asyncio
async def test_exception_path_keeps_joined_iids_for_set_properties(
    bound_client, tmp_path
):
    """set_properties 异常行 iid 列与成功行同构(逗号拼接),不落 NULL。"""
    from miloco.middleware.exceptions import MiotServiceException

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.set_device_properties = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    req = DeviceControlRequest(
        type="set_properties",
        properties=[
            {"iid": "prop.2.1", "value": True},
            {"iid": "prop.3.1", "value": 50},
        ],
    )
    with pytest.raises(MiotServiceException):
        await svc.control_device("dev1", req)
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["success"] == 0
    # 按 iid 检索失败动作不能漏:set_properties 顶层 iid 恒空,须按 type 重建
    assert r["iid"] == "prop.2.1,prop.3.1"
    assert json.loads(r["value_json"]) == {"prop.2.1": True, "prop.3.1": 50}


@pytest.mark.asyncio
async def test_scene_trigger_writes_ledger_row(bound_client, tmp_path):
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    ok = await svc.trigger_scene("scene1")
    await client.flush()

    assert ok is True
    r = _rows(obs_db)[0]
    assert r["action_type"] == "scene_trigger"
    assert r["did"] == "scene1"
    assert r["iid"] == "scene1"
    assert r["success"] == 1
    assert json.loads(r["value_json"]) == {"scene_name": "回家"}
    # did 是 scene_id、device cache 必 miss——home 由 trigger_scene 显式传入
    # (scene1 ∈ H1),否则场景台账恒 NULL、经 NULL 放行串入他家合流页。
    assert r["home_id"] == "H1"


@pytest.mark.asyncio
async def test_writer_explicit_home_overrides_cache(bound_client, tmp_path):
    """显式 home_id 形参优先于 device cache 解析(dev1 ∈ H1,显式传 H9 应落 H9)。"""
    from miloco.miot.service import _write_action_ledger

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    await _write_action_ledger(
        svc._miot_proxy,
        action_type="set_property", did="dev1", iid="prop.2.1",
        value_json="true", result_code=0, result_msg=None,
        success=True, error=None, home_id="H9",
    )
    await client.flush()
    assert _rows(obs_db)[0]["home_id"] == "H9"


@pytest.mark.asyncio
async def test_ledger_write_failure_does_not_break_control(tmp_path, monkeypatch):
    """ledger 写挂掉(record_action 抛)时,control_device 仍正常返回。"""
    obs_db = tmp_path / "observability.db"
    client = MetricsClient(db_path=obs_db)
    await client.start()
    mc.set_metrics_client(client)
    try:
        def _boom(_record):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(client, "record_action", _boom)
        svc = _make_service(tmp_path)
        req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
        result = await svc.control_device("dev1", req)
        assert "results" in result  # 控制结果不受影响
    finally:
        mc.set_metrics_client(None)
        await client.stop()


@pytest.mark.asyncio
async def test_no_client_bound_control_still_works(tmp_path):
    """singleton 未绑定(get_metrics_client() 返回 None)时 control_device 照常。"""
    mc.set_metrics_client(None)
    svc = _make_service(tmp_path)
    req = DeviceControlRequest(type="set_property", iid="prop.2.1", value=True)
    result = await svc.control_device("dev1", req)
    assert "results" in result
