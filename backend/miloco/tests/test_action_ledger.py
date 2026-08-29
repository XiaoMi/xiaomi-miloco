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


@pytest.fixture
def scene_no_retry(monkeypatch):
    """固定 scene_trigger_max_retries=0 —— 存量行为（失败即收尾，不 sleep）。

    场景触发现在默认带 2 次退避重试；测试存量失败形状时若不固定，异常路径会
    多打 2 次执行 + sleep，断言的口径也漂移。
    """
    from miloco.config import reset_settings

    monkeypatch.setenv("MILOCO_RULE__SCENE_TRIGGER_MAX_RETRIES", "0")
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def scene_retry(monkeypatch):
    """固定 2 次重试 / 0s 初始退避 —— 重试行为测试不 sleep。"""
    from miloco.config import reset_settings

    monkeypatch.setenv("MILOCO_RULE__SCENE_TRIGGER_MAX_RETRIES", "2")
    monkeypatch.setenv("MILOCO_RULE__SCENE_TRIGGER_RETRY_DELAY_SEC", "0")
    reset_settings()
    yield
    reset_settings()


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
async def test_scene_trigger_exception_keeps_scene_name(
    bound_client, tmp_path, scene_no_retry
):
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


@pytest.mark.asyncio
async def test_scene_trigger_records_source_rule(bound_client, tmp_path):
    """规则直控场景:台账 source=rule / source_id=rule_id。

    没有这条透传,规则触发的场景在台账里和人工 CLI 触发无法区分,
    「规则触发 → 实际执行了什么」这条链就是断的。
    """
    from miloco.miot.service import _trigger_scene

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    ok = await _trigger_scene(
        svc._miot_proxy, "scene1", source="rule", source_id="rule-77"
    )
    await client.flush()

    assert ok is True
    r = _rows(obs_db)[0]
    assert r["action_type"] == "scene_trigger"
    assert r["source"] == "rule"
    assert r["source_id"] == "rule-77"
    # 场景无 did:did/iid 都占 scene_id,与 CLI 触发同一形状
    assert r["did"] == "scene1"
    assert r["iid"] == "scene1"


@pytest.mark.asyncio
async def test_scene_trigger_failure_records_source_rule(
    bound_client, tmp_path, scene_no_retry
):
    """异常路径也要带 source/source_id——失败的规则动作最需要能回指到规则。"""
    from miloco.middleware.exceptions import MiotServiceException
    from miloco.miot.service import _trigger_scene

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.execute_miot_scene = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(MiotServiceException):
        await _trigger_scene(
            svc._miot_proxy, "scene1", source="rule", source_id="rule-77"
        )
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["success"] == 0
    assert r["source"] == "rule"
    assert r["source_id"] == "rule-77"


@pytest.mark.asyncio
async def test_miot_service_trigger_scene_stays_source_cli(bound_client, tmp_path):
    """MiotService.trigger_scene 不传 source → 仍是 cli,存量台账形状不变。"""
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    await svc.trigger_scene("scene1")
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["source"] == "cli"
    assert r["source_id"] is None


@pytest.mark.asyncio
async def test_scene_not_found_still_writes_ledger(
    bound_client, tmp_path, scene_no_retry
):
    """场景被删是最常见的生产失败;不落台账的话持久审计上一行都没有。"""
    from miloco.middleware.exceptions import ResourceNotFoundException
    from miloco.miot.service import _trigger_scene

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    with pytest.raises(ResourceNotFoundException):
        await _trigger_scene(
            svc._miot_proxy, "scene-gone", source="rule", source_id="rule-77"
        )
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["action_type"] == "scene_trigger"
    assert r["did"] == "scene-gone"
    assert r["success"] == 0
    assert r["source"] == "rule"
    assert r["source_id"] == "rule-77"


@pytest.mark.asyncio
async def test_scene_home_not_allowed_still_writes_ledger(bound_client, tmp_path):
    """越权信号(触发不在允许家庭的场景)更不能只留在 rule_log 里。"""
    from miloco.middleware.exceptions import ValidationException
    from miloco.miot.service import _trigger_scene

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.get_all_scenes = AsyncMock(
        return_value={"scene1": SimpleNamespace(home_id="H-other", scene_name="他家")}
    )
    with pytest.raises(ValidationException):
        await _trigger_scene(
            svc._miot_proxy, "scene1", source="rule", source_id="rule-77"
        )
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["success"] == 0
    assert r["source_id"] == "rule-77"
    assert r["home_id"] == "H-other"


# ---- 场景触发自动重试（settings.rule.scene_trigger_*）----


@pytest.mark.asyncio
async def test_scene_trigger_retries_false_then_succeeds(
    bound_client, tmp_path, scene_retry
):
    """执行返回 false(瞬时失败)→ 重试后成功:2 次执行、单行成功台账。"""
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    calls = {"n": 0}

    async def _flaky(_sid):
        calls["n"] += 1
        return calls["n"] >= 2

    svc._miot_proxy.execute_miot_scene = AsyncMock(side_effect=_flaky)

    ok = await svc.trigger_scene("scene1")
    await client.flush()

    assert ok is True
    assert svc._miot_proxy.execute_miot_scene.await_count == 2
    rows = _rows(obs_db)
    assert len(rows) == 1  # 中间尝试不重复写行
    assert rows[0]["success"] == 1


@pytest.mark.asyncio
async def test_scene_trigger_retries_exception_then_succeeds(
    bound_client, tmp_path, scene_retry
):
    """SDK 抛异常(网络抖动)→ 重试后成功。"""
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    calls = {"n": 0}

    async def _flaky(_sid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("net flake")
        return True

    svc._miot_proxy.execute_miot_scene = AsyncMock(side_effect=_flaky)

    ok = await svc.trigger_scene("scene1")
    await client.flush()

    assert ok is True
    assert svc._miot_proxy.execute_miot_scene.await_count == 2
    rows = _rows(obs_db)
    assert len(rows) == 1
    assert rows[0]["success"] == 1


@pytest.mark.asyncio
async def test_scene_trigger_false_exhausts_retries(
    bound_client, tmp_path, scene_retry
):
    """执行持续返回 false → 3 次尝试后返回 False,单行失败台账。"""
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.execute_miot_scene = AsyncMock(return_value=False)

    ok = await svc.trigger_scene("scene1")
    await client.flush()

    assert ok is False
    assert svc._miot_proxy.execute_miot_scene.await_count == 3
    rows = _rows(obs_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["success"] == 0
    assert r["result_msg"] == "场景触发失败"
    assert r["error"] is None
    # 失败审计完整性:仍能看到当时想触发什么
    assert json.loads(r["value_json"]) == {"scene_name": "回家"}


@pytest.mark.asyncio
async def test_scene_trigger_exception_exhausts_retries_raises(
    bound_client, tmp_path, scene_retry
):
    """SDK 持续抛错 → 重试耗尽抛 MiotServiceException,单行失败台账。"""
    from miloco.middleware.exceptions import MiotServiceException

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.execute_miot_scene = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(MiotServiceException):
        await svc.trigger_scene("scene1")
    await client.flush()

    assert svc._miot_proxy.execute_miot_scene.await_count == 3
    rows = _rows(obs_db)
    assert len(rows) == 1
    assert rows[0]["success"] == 0
    assert "boom" in (rows[0]["error"] or "")


@pytest.mark.asyncio
async def test_scene_trigger_not_found_refreshes_and_heals(
    bound_client, tmp_path, scene_retry
):
    """首轮查不到(缓存陈旧/首拉失败)→ 重试前强制刷新场景列表,刷新后命中成功。"""
    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.refresh_scenes = AsyncMock()
    scenes = {"scene1": SimpleNamespace(home_id="H1", scene_name="回家")}
    svc._miot_proxy.get_all_scenes = AsyncMock(side_effect=[{}, scenes])

    ok = await svc.trigger_scene("scene1")
    await client.flush()

    assert ok is True
    svc._miot_proxy.refresh_scenes.assert_awaited_once()
    assert svc._miot_proxy.get_all_scenes.await_count == 2
    rows = _rows(obs_db)
    assert len(rows) == 1
    assert rows[0]["success"] == 1


@pytest.mark.asyncio
async def test_scene_trigger_not_found_persists_after_refresh(
    bound_client, tmp_path, scene_retry
):
    """刷新后仍查不到 → 视为永久失败:重试耗尽后抛 ResourceNotFoundException。"""
    from miloco.middleware.exceptions import ResourceNotFoundException

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.refresh_scenes = AsyncMock()
    svc._miot_proxy.get_all_scenes = AsyncMock(return_value={})

    with pytest.raises(ResourceNotFoundException):
        await svc.trigger_scene("scene-gone")
    await client.flush()

    # 3 次尝试,后两次都强制刷新
    assert svc._miot_proxy.refresh_scenes.await_count == 2
    assert svc._miot_proxy.get_all_scenes.await_count == 3
    rows = _rows(obs_db)
    assert len(rows) == 1
    assert rows[0]["success"] == 0
    assert rows[0]["did"] == "scene-gone"


@pytest.mark.asyncio
async def test_scene_trigger_validation_never_retried(
    bound_client, tmp_path, scene_retry
):
    """场景不在允许家庭是策略错误:重试配置再多也不重试,执行零调用。"""
    from miloco.middleware.exceptions import ValidationException

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.get_all_scenes = AsyncMock(
        return_value={"scene1": SimpleNamespace(home_id="H-other", scene_name="他家")}
    )

    with pytest.raises(ValidationException):
        await svc.trigger_scene("scene1")
    await client.flush()

    svc._miot_proxy.execute_miot_scene.assert_not_awaited()
    rows = _rows(obs_db)
    assert len(rows) == 1
    assert rows[0]["success"] == 0
