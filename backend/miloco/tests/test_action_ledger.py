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
from miloco.miot.client import refusal_reason_for
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
        # 真实代理有这个判据，夹具也要有：缺了它，生产侧任何「先问一句能不能用」
        # 的检查都会在这里撞 AttributeError，而那不是生产缺陷、是夹具没建模。
        is_operational=True,
        # 真实代理还有「有没有绑」这一档，以及据它分档的拒绝文案。夹具取生产那
        # 份纯函数、不另写一份措辞——复刻件会与本体漂移，而漂移之后测试照样绿。
        is_authenticated=True,
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
        # 真实代理上这是「只读已在手的那份、不触发刷新」的取数口，返回的就是
        # get_all_scenes 命中缓存时那份。夹具照样建模，否则被拒那条路会撞
        # AttributeError——那不是生产缺陷、是替身没建模。
        cached_scenes={"scene1": SimpleNamespace(home_id="H1", scene_name="回家")},
        # 真实代理另有一对**不触发刷新**的取数口（降级态下写台账走它们，避免为一行
        # 留痕去打一趟注定 401 的云端）。夹具让它们与上面那两个刷新口返回同一份，
        # 缺了它们，被拒那条路会在这里撞 AttributeError。
        cached_devices={"dev1": dev},
        get_cached_camera=lambda did: {
            "cam1": SimpleNamespace(
                home_id="H1", name="门口摄像头", room_name="门口"
            )
        }.get(did),
        execute_miot_scene=AsyncMock(return_value=True),
    )
    proxy.refusal_reason = lambda what: refusal_reason_for(
        what,
        operational=proxy.is_operational,
        authenticated=proxy.is_authenticated,
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
async def test_ledger_falls_back_to_the_sole_enabled_home(bound_client, tmp_path):
    """两级缓存都查不到 did 时，只启用了一个家的话就归它，而不是落空。

    降级态下这两级都填不回来——填它们要走云端，而那正是被拒的原因。缺了这一列的
    行会被查询侧的 NULL 放行捞进每一个家的合流页，别家的设备号与「那个家授权坏了」
    就此跨家可见；紧邻那条摄像头回落的用例记的是同一件事。
    """
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
    assert _rows(obs_db)[0]["home_id"] == "H1"


@pytest.mark.asyncio
async def test_ledger_still_writes_when_the_home_is_genuinely_ambiguous(
    bound_client, tmp_path
):
    """多家同时启用且查不到 did 时仍落空——这是已知残留，但那一行必须照样落库。

    反向钉住两件事：兜底只在「唯一启用」时才敢认（多家时认一个就是认错家），以及
    解析不出归属绝不能连带把审计吞掉（这条 fail-open 是原有语义）。
    """
    import json as _json

    from miloco.database.kv_repo import ScopeConfigKeys
    from miloco.miot.service import _write_action_ledger

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy._kv_repo.set(
        ScopeConfigKeys.HOME_WHITE_LIST_KEY, _json.dumps(["H1", "H2"])
    )
    await _write_action_ledger(
        svc._miot_proxy,
        action_type="set_property", did="ghost", iid="prop.2.1",
        value_json="true", result_code=0, result_msg=None,
        success=True, error=None,
    )
    await client.flush()
    rows = _rows(obs_db)
    assert len(rows) == 1, "解析不出归属不该把这一行审计吞掉"
    assert rows[0]["home_id"] is None


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
async def test_scene_refusal_surfaces_the_reason(bound_client, tmp_path):
    """场景是第三条下发面：被拒时住户要拿到「需要重新授权」，不是「场景触发失败」。

    故意用一个**不在列表里**的场景号：降级态下拉场景列表本身就会被拒，重启之后
    本地那份缓存是空的，于是每一个场景都「不存在」。失效判定排在存在性之后的话，
    住户点一个明明还在的场景，拿到的是「场景不存在」，真正的原因被盖掉。
    """
    from miloco.middleware.exceptions import MiotAuthUnavailableError

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.is_operational = False

    with pytest.raises(MiotAuthUnavailableError) as e:
        await svc.trigger_scene("no-such-scene")
    assert "Rebind" in str(e.value) or "重新授权" in str(e.value)
    # 判为不可用就不该再去试云端——那一次注定被闸门拒掉。
    assert not svc._miot_proxy.execute_miot_scene.called
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["success"] == 0
    assert "refused" in (r["error"] or ""), "台账要记明是被拒，而不是笼统失败"
    # 家庭标识不能空：查询侧对空标识有一条刻意的放行（迁移前的老行没有标记），
    # 漏传的行会被捞进**每一个**家的合流页，别家的场景号与「那个家授权坏了」
    # 就此跨家可见。这里场景表里没有这个号，靠「只启用了一个家」这一级问出来。
    assert r["home_id"] == "H1", "被拒的行漏了家庭标识，会串进每一个家的台账"
    # 通用文案会把原因盖掉：下游取 `result_msg or error`，这一列有值就再也看不到
    # 「被拒」。控制那条路的被拒留痕同样是留空 result_msg、原因写在 error 里。
    assert not r["result_msg"], "被拒的行不该再写通用失败文案"


@pytest.mark.asyncio
async def test_control_refusal_beats_device_not_found(bound_client, tmp_path):
    """降级态冷启动时点一台设备，要拿到「请重新授权」，不是「设备不存在」。

    家庭校验靠两份纯内存的设备缓存，而填满它们要走云端——降级态下重启一次，两级都
    是空的且填不回来。判定排在校验之后的话，住户拿到 404 加「设备不存在」，会去米家
    App 里翻设备列表，而不是去点状态条上的「重新绑定」；并且那一次被拒连一行台账都
    不落，因为「设备不存在」是原样上抛的。
    """
    from miloco.middleware.exceptions import MiotAuthUnavailableError

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    # 冷启动：两份缓存都填不回来
    svc._miot_proxy.get_devices = AsyncMock(return_value={})
    svc._miot_proxy.get_cameras = AsyncMock(return_value={})
    svc._miot_proxy.is_operational = False

    with pytest.raises(MiotAuthUnavailableError) as e:
        await svc.control_device(
            "lumi.acn003",
            SimpleNamespace(type="set_property", iid="prop.2.1", value=True),
        )
    assert "Rebind" in str(e.value) or "重新授权" in str(e.value)
    await client.flush()

    r = _rows(obs_db)[0]
    assert r["success"] == 0
    assert "refused" in (r["error"] or ""), "被拒的下发同样要留痕"
    assert r["home_id"] == "H1", "留痕缺了家庭标识会串进每一个家的台账"


@pytest.mark.asyncio
async def test_refused_dispatch_makes_no_doomed_cloud_call(bound_client, tmp_path):
    """被拒的下发不许为了写一行台账去打注定 401 的云端。

    台账要填设备名与所属家，而那两个取数口见缓存为空会先去拉一次——降级态下那一次
    必然被云端拒掉、缓存依然为空，于是住户每被拒一次就多 1-2 趟无效往返，还各自漏
    出一条 WARNING / ERROR，把这条路径刻意压成 INFO 的噪声抬回去。不钉住的话，这条
    约束只活在注释里。
    """
    from miloco.middleware.exceptions import MiotAuthUnavailableError

    client, _ = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.refresh_devices = AsyncMock(return_value=None)
    svc._miot_proxy.refresh_cameras = AsyncMock(return_value=None)
    svc._miot_proxy.is_operational = False

    with pytest.raises(MiotAuthUnavailableError):
        await svc.control_device(
            "lumi.acn003",
            SimpleNamespace(type="set_property", iid="prop.2.1", value=True),
        )
    await client.flush()

    assert not svc._miot_proxy.get_devices.called, "降级态下不该走会触发刷新的取数口"
    assert not svc._miot_proxy.get_cameras.called
    assert not svc._miot_proxy.refresh_devices.called
    assert not svc._miot_proxy.refresh_cameras.called


@pytest.mark.asyncio
async def test_never_bound_is_not_reported_as_expired(bound_client, tmp_path):
    """从未绑定 / 刚解绑的机器上，别把「没绑」说成「凭据失效」。

    判据是「凭据存在**且**未被云端拒绝」的与，而解绑会清空凭据、同时把健康度复位
    成全新的正常态——于是判据为假、降级却并没有发生。此时照搬「授权已失效」会让
    三处同时错：文案让住户去「重新绑定」一个他没绑过的账号；台账的原因列写着凭据
    失效；而同一行的授权状态列取的是健康度、写着正常，自相矛盾——网页那个失效角标
    恰好按状态列判，于是住户看到的是一条毫无解释的失败记录。
    """
    from miloco.middleware.exceptions import MiotAuthUnavailableError

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.is_operational = False
    svc._miot_proxy.is_authenticated = False  # 没绑，而不是绑了但废了

    with pytest.raises(MiotAuthUnavailableError) as e:
        await svc.trigger_scene("scene1")
    msg = str(e.value)
    assert "not bound" in msg, "应当说「没绑定」"
    assert "no longer valid" not in msg, "不该说成「凭据已失效」"
    await client.flush()

    r = _rows(obs_db)[0]
    assert "not bound" in (r["error"] or ""), "台账的原因列同样要分档"


@pytest.mark.asyncio
async def test_scene_refusal_prefers_the_scene_own_home(bound_client, tmp_path):
    """场景表里有这个号时，家庭标识取场景自己的家，而不是「唯一启用的那个家」。

    两级回落在夹具默认值下答案相同，只验被拒那条的话，把第一级删掉照样全绿。这里
    让场景属于另一个家，两级的答案才分得开。
    """
    from miloco.middleware.exceptions import MiotAuthUnavailableError

    client, obs_db = bound_client
    svc = _make_service(tmp_path)
    svc._miot_proxy.cached_scenes = {
        "scene-elsewhere": SimpleNamespace(home_id="H2", scene_name="另一个家")
    }
    svc._miot_proxy.is_operational = False

    with pytest.raises(MiotAuthUnavailableError):
        await svc.trigger_scene("scene-elsewhere")
    await client.flush()

    assert _rows(obs_db)[0]["home_id"] == "H2"


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
async def test_scene_trigger_failure_records_source_rule(bound_client, tmp_path):
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
async def test_scene_not_found_still_writes_ledger(bound_client, tmp_path):
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
