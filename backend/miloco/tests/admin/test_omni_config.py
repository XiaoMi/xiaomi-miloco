"""GET/PUT/activate/delete/test/models /api/admin/omni-config 端到端测试。

多档案模型:档案名 label = 唯一 id;active = model.omni;profiles = model.omni_profiles。
- api_key 打码(前3…后4),不泄漏全文;
- PUT 按 label upsert + 激活;original_label 支持改名;重名→409;空名→400;
- api_key 留空 = 沿用该档案原 key(按 label 解析);
- activate / delete 按 label;models / test 按 label 取已存 key。
环境隔离:删 MILOCO_MODEL__OMNI__* 环境变量,否则 env 优先级高会盖过 config.json。
"""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router

# 模块 import 时立刻抓一份真实的 probe_omni 引用,避免被 autouse mock 覆盖后 real_probe fixture 拿到 mock 版
from miloco.perception.engine.omni.probe import probe_omni as _real_probe_omni


@pytest.fixture(autouse=True)
def _default_probe_success(monkeypatch):
    """默认让 preflight 通过——绝大多数用例关心 config CRUD,不关心 probe 结果。
    需要测 preflight 失败或 test_connection 具体错误码的用例请加 real_probe fixture
    还原 admin.router._probe.probe_omni 到真实实现。"""

    async def _ok(*a, **k):
        return {
            "ok": True,
            "code": "ok",
            "status": 200,
            "latency_ms": 1,
            "message": "连接正常",
        }

    monkeypatch.setattr("miloco.admin.router._probe.probe_omni", _ok)


@pytest.fixture(autouse=True)
def _reset_omni_circuit_breaker():
    """避免熔断状态跨 test 泄漏(retry 端点、SSE 事件等)。"""
    from miloco.perception.engine.omni.circuit_breaker import (
        reset_omni_circuit_breaker_for_tests,
    )

    reset_omni_circuit_breaker_for_tests()
    yield
    reset_omni_circuit_breaker_for_tests()


@pytest.fixture
def real_probe(monkeypatch):
    """还原 preflight 的 probe mock,让 test_test_connection_* 真走 probe.py 逻辑
    (用底层 httpx patch 替代)。用模块 import 时抓的原始引用。"""
    monkeypatch.setattr("miloco.admin.router._probe.probe_omni", _real_probe_omni)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    monkeypatch.delenv("MILOCO_MODEL__OMNI__MODEL", raising=False)
    monkeypatch.delenv("MILOCO_MODEL__OMNI__BASE_URL", raising=False)
    # 写空 config.json 覆盖 settings.yaml 出厂档案,给用例确定性的"干净起点"
    import json as _json

    (tmp_path / "config.json").write_text(
        _json.dumps(
            {
                "model": {
                    "omni": {
                        "label": "",
                        "model": "xiaomi/mimo-v2.5",
                        "base_url": "https://api.xiaomimimo.com/v1",
                        "api_key": "",
                    },
                    "omni_profiles": [],
                }
            }
        ),
        encoding="utf-8",
    )
    reset_settings()

    # 验证记录读写要用到 manager.kv_repo;端到端测试只挂 router、不过 lifespan,
    # manager 单例的 _kv_repo 平时由 Manager.initialize() 灌注,这里补齐一份绑定到
    # 本用例 MILOCO_HOME 的 KVRepo。用 monkeypatch 而非裸赋值:退出时自动还原成
    # 进入前的值(而不是一律拍成 None,把 None 泄漏给后续用例)。
    import miloco.database.connector as _connector_module
    from miloco.admin.router import manager as _manager
    from miloco.database.kv_repo import KVRepo as _KVRepo

    monkeypatch.setattr(_connector_module, "db_connector", None)
    _connector_module.init_database()
    monkeypatch.setattr(_manager, "_kv_repo", _KVRepo(), raising=False)

    app = FastAPI()
    app.include_router(router, prefix="/api")
    yield TestClient(app)
    reset_settings()


def _get(client):
    return client.get("/api/admin/omni-config").json()["data"]


# ─── GET / PUT / 档案(label=id) ────────────────────────────────────────────


def test_get_default_active_no_key_not_synthesized(client):
    """出厂未配态:当前生效配置无 key(没有模型在跑),不合成进列表 —— 列表为空,
    前端据此给「未配 API Key」警告,清楚表达「没有模型在跑」。"""
    data = _get(client)
    assert data["active"]["model"] == "xiaomi/mimo-v2.5"
    assert data["active"]["has_key"] is False
    assert data["profiles"] == []


def test_active_with_key_not_in_profiles_is_synthesized(client):
    """当前生效配置「有 key、在跑」但未存档进 profiles 时:合成补到列表头部并标 active,
    且不与已有档案重复 —— 修复「列表看不到正在跑的当前模型」的 BUG。无 key 态不合成(见上例)。"""
    # 先存一套带 key 的档案「甲」并令其生效
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k123456789",
        },
    )
    # 直接把当前生效改成一套「有 key 但未存档」的配置(模拟 active 不在 profiles)
    from miloco.config.settings import get_settings

    s = get_settings()
    s.model.omni.label = "临时未存档"
    s.model.omni.model = "ad-hoc-model"
    s.model.omni.base_url = "https://adhoc/v1"
    s.model.omni.api_key = "sk-adhoc999999"
    data = _get(client)
    # 列表 = 合成的 active(头部) + 原档案「甲」
    assert data["profiles"][0]["label"] == "临时未存档"
    assert data["profiles"][0]["model"] == "ad-hoc-model"
    assert data["profiles"][0]["active"] is True
    assert data["profiles"][0]["has_key"] is True
    assert any(p["label"] == "甲" and p["active"] is False for p in data["profiles"])
    # 「甲」未被重复注入
    assert sum(1 for p in data["profiles"] if p["label"] == "甲") == 1


def test_active_already_in_profiles_not_duplicated(client):
    """active 已存档时:不重复注入,列表恰有一行 active。"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k123456789",
        },
    )  # 默认 activate=true,甲 已存档且生效
    data = _get(client)
    assert len(data["profiles"]) == 1
    assert sum(1 for p in data["profiles"] if p["active"]) == 1
    assert data["profiles"][0]["label"] == "甲"


def test_put_creates_and_activates(client):
    out = client.put(
        "/api/admin/omni-config",
        json={
            "label": "配置1",
            "model": "qwen3-omni-flash",
            "base_url": "https://q/v1",
            "api_key": "sk-faketestkey1234abcd",
        },
    ).json()["data"]
    assert out["active"]["label"] == "配置1"
    assert out["active"]["model"] == "qwen3-omni-flash"
    assert out["active"]["has_key"] is True
    assert out["active"]["api_key_masked"] == "sk-…abcd"
    assert len(out["profiles"]) == 1
    p = out["profiles"][0]
    assert p["label"] == "配置1" and p["active"] is True and p["has_key"] is True


def test_put_empty_label_400(client):
    resp = client.put(
        "/api/admin/omni-config",
        json={
            "label": "  ",
            "model": "m",
            "base_url": "https://x/v1",
            "api_key": "sk-k123456789",
        },
    )
    assert resp.status_code == 400


def test_second_profile_has_independent_key(client):
    # 第一套带 key
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-keyforjia12",
        },
    )
    # 另一套(新 label)不传 key → 不借别人的(key 属该档案);activate=False 因为无 key 无法激活
    out = client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://x/v1",
            "activate": False,
        },
    ).json()["data"]
    # activate=False 只入列表,不切换 active(active 仍是先前的甲)
    assert out["active"]["label"] == "甲"
    assert len(out["profiles"]) == 2
    yi = next(p for p in out["profiles"] if p["label"] == "乙")
    assert yi["has_key"] is False


def test_update_same_label_blank_key_keeps_it(client):
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-keyforjia12",
        },
    )
    # 同名再存、不传 key、改了 model → key 沿用
    out = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m2",
            "base_url": "https://x/v1",
            "original_label": "甲",
        },
    ).json()["data"]
    assert out["active"]["model"] == "m2"
    assert out["active"]["has_key"] is True
    assert len(out["profiles"]) == 1  # 同名 = 同一档案,未新增


def test_rename_via_original_label(client):
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "配置1",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-keyforaaa12",
        },
    )
    out = client.put(
        "/api/admin/omni-config",
        json={
            "label": "生产Q",
            "model": "m1",
            "base_url": "https://x/v1",
            "original_label": "配置1",
        },
    ).json()["data"]
    assert out["active"]["label"] == "生产Q"
    assert out["active"]["has_key"] is True  # key 沿用
    assert len(out["profiles"]) == 1  # 改名而非新增
    assert out["profiles"][0]["label"] == "生产Q"


def test_put_activate_false_only_adds_to_list(client):
    """activate=false:只入列表,不切换当前生效(「保存」按钮的行为)。"""
    out = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k123456789",
            "activate": False,
        },
    ).json()["data"]
    assert out["active"]["label"] != "甲"  # 未切换(active 仍是默认)
    jia = next(p for p in out["profiles"] if p["label"] == "甲")  # 已入列表
    assert jia["active"] is False  # 新加的这条不是当前生效(activate=false)


def test_put_activate_false_editing_active_still_syncs(client):
    """即便 activate=false,编辑的若正是当前生效那套,active 仍同步刷新(改 model/key 即时生效)。"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )  # 默认 activate=true → 甲 成为当前
    out = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m2",
            "base_url": "https://x/v1",
            "original_label": "甲",
            "activate": False,
        },
    ).json()["data"]
    assert out["active"]["label"] == "甲"
    assert out["active"]["model"] == "m2"  # 当前生效那套的改动即时同步


def test_duplicate_label_409(client):
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://x/v1",
            "activate": False,
        },
    )
    # 把「乙」改名成已存在的「甲」→ 409
    resp = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m2",
            "base_url": "https://x/v1",
            "original_label": "乙",
        },
    )
    assert resp.status_code == 409


def test_activate_by_label(client):
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://x/v1",
            "api_key": "sk-k222222222",
        },
    )
    out = client.post("/api/admin/omni-config/activate", json={"label": "甲"}).json()[
        "data"
    ]
    assert out["active"]["label"] == "甲"
    actives = {p["label"]: p["active"] for p in out["profiles"]}
    assert actives == {"甲": True, "乙": False}


def test_activate_missing_404(client):
    resp = client.post("/api/admin/omni-config/activate", json={"label": "不存在"})
    assert resp.status_code == 404


def test_delete_non_active_label(client):
    """删一套非当前生效的档案:列表只剩当前生效那套(甲是最后 PUT、默认生效)。"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://x/v1",
            "api_key": "sk-k222222222",
        },
    )
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )
    out = client.post("/api/admin/omni-config/delete", json={"label": "乙"}).json()[
        "data"
    ]
    assert [p["label"] for p in out["profiles"]] == ["甲"]
    assert out["active"]["label"] == "甲"


def test_delete_active_resets_to_unconfigured(client):
    """删除「当前生效」的档案:回到未配模型态 —— 当前生效配置重置为出厂默认(无 key),
    该档案从列表移除;因 active 无 key 不再合成,列表里没有任何「当前模型」行(感知随之软停)。
    (软停为 best-effort:测试态无活动感知引擎,delete 仍正常返回。)"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://x/v1",
            "api_key": "sk-k222222222",
        },
    )
    # 乙是最后 PUT、默认生效;删掉当前生效的乙
    out = client.post("/api/admin/omni-config/delete", json={"label": "乙"}).json()[
        "data"
    ]
    # 当前生效重置为出厂未配态(无 key)
    assert out["active"]["has_key"] is False
    assert out["active"]["model"] == "xiaomi/mimo-v2.5"
    # 乙已移除;甲 仍在档案列表但非 active;无任何 active 行(无 key 不合成)
    assert not any(p["label"] == "乙" for p in out["profiles"])
    assert any(p["label"] == "甲" for p in out["profiles"])
    assert all(not p["active"] for p in out["profiles"])


class _RecordingPerceptionService:
    """记录 stop_to_unconfigured 被 await 的次数(软停链路的可观测替身)。"""

    def __init__(self):
        self.soft_stop_calls = 0

    async def stop_to_unconfigured(self):
        self.soft_stop_calls += 1


def test_delete_active_awaits_soft_stop(client, monkeypatch):
    """删当前生效:除重置配置外,必须真正 await perception_service.stop_to_unconfigured 一次。
    (此前 delete-active 测试只验配置重置半边,软停因 manager 未初始化、AttributeError 被吞而从未执行。)"""
    from miloco.admin import router as r

    fake = _RecordingPerceptionService()
    monkeypatch.setattr(r.manager, "_perception_service", fake, raising=False)

    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )  # 默认 activate=true → 甲 当前生效
    out = client.post("/api/admin/omni-config/delete", json={"label": "甲"}).json()[
        "data"
    ]
    assert fake.soft_stop_calls == 1  # 软停被 await 恰一次
    assert out["active"]["has_key"] is False  # 当前生效重置为未配


def test_delete_non_active_does_not_soft_stop(client, monkeypatch):
    """删非当前生效:不重置 active、也不触发软停(感知照常运行)。"""
    from miloco.admin import router as r

    fake = _RecordingPerceptionService()
    monkeypatch.setattr(r.manager, "_perception_service", fake, raising=False)

    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://x/v1",
            "api_key": "sk-k222222222",
        },
    )
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )  # 甲 当前生效
    out = client.post("/api/admin/omni-config/delete", json={"label": "乙"}).json()[
        "data"
    ]
    assert fake.soft_stop_calls == 0  # 非生效 → 不软停
    assert out["active"]["label"] == "甲"  # 当前生效不变


def test_delete_synthesized_active_empty_label_resets_and_soft_stops(
    client, monkeypatch
):
    """删「空 label 的当前生效合成行」(env/手改直填 key 的态):按展示 label(model @ base_url)
    定位也判为 active → 重置为未配 + 触发软停(修复空 label 删除静默无效的 bug)。"""
    from miloco.admin import router as r
    from miloco.config.settings import get_settings

    fake = _RecordingPerceptionService()
    monkeypatch.setattr(r.manager, "_perception_service", fake, raising=False)

    # 当前生效:有 key 但 label 为空、未存档进 profiles
    s = get_settings()
    s.model.omni.label = ""
    s.model.omni.model = "ad-hoc-model"
    s.model.omni.base_url = "https://adhoc/v1"
    s.model.omni.api_key = "sk-adhoc999999"
    data = _get(client)
    synth_label = data["profiles"][0]["label"]
    assert synth_label == "ad-hoc-model @ https://adhoc/v1"  # 合成展示 label 非空
    assert data["profiles"][0]["active"] is True

    out = client.post(
        "/api/admin/omni-config/delete", json={"label": synth_label}
    ).json()["data"]
    assert fake.soft_stop_calls == 1  # 软停触发(此前 was_active 误判 False → 静默无效)
    assert out["active"]["has_key"] is False  # 重置为未配
    assert out["profiles"] == []  # 无 key 不再合成,列表清空


def test_edit_synthesized_active_empty_label_syncs_active(client):
    """编辑「空 label 的当前生效合成行」:按展示 label 命中 → 同步刷新 active 并被收编进 profiles
    (修复空 label 当前生效行无法编辑/保存即时生效的 bug)。"""
    from miloco.config.settings import get_settings

    s = get_settings()
    s.model.omni.label = ""
    s.model.omni.model = "ad-hoc-model"
    s.model.omni.base_url = "https://adhoc/v1"
    s.model.omni.api_key = "sk-adhoc999999"
    synth_label = _get(client)["profiles"][0]["label"]

    # 用合成 label 作 original_label 编辑(改 model、key 留空沿用),activate=false
    out = client.put(
        "/api/admin/omni-config",
        json={
            "label": synth_label,
            "model": "ad-hoc-v2",
            "base_url": "https://adhoc/v1",
            "original_label": synth_label,
            "activate": False,
        },
    ).json()["data"]
    assert out["active"]["model"] == "ad-hoc-v2"  # 当前生效那套即时同步
    assert out["active"]["has_key"] is True  # key 留空 → 沿用原 key
    assert any(
        p["label"] == synth_label and p["model"] == "ad-hoc-v2" for p in out["profiles"]
    )


def test_deactivate_active_resets_keeps_profile_and_soft_stops(client, monkeypatch):
    """停用当前生效:active 重置为未配 + 触发软停,但档案保留(与 delete 的区别),可再启用。"""
    from miloco.admin import router as r

    fake = _RecordingPerceptionService()
    monkeypatch.setattr(r.manager, "_perception_service", fake, raising=False)
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )  # 甲 当前生效
    out = client.post("/api/admin/omni-config/deactivate", json={"label": "甲"}).json()[
        "data"
    ]
    assert fake.soft_stop_calls == 1  # 软停触发
    assert out["active"]["has_key"] is False  # 重置为未配
    assert any(p["label"] == "甲" for p in out["profiles"])  # 档案保留(不删)
    assert all(not p["active"] for p in out["profiles"])  # 已无生效行


def test_deactivate_non_active_noop(client, monkeypatch):
    """停用非当前生效那套:no-op —— 不软停、不改 active。"""
    from miloco.admin import router as r

    fake = _RecordingPerceptionService()
    monkeypatch.setattr(r.manager, "_perception_service", fake, raising=False)
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://x/v1",
            "api_key": "sk-k222222222",
        },
    )
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )  # 甲 生效
    out = client.post("/api/admin/omni-config/deactivate", json={"label": "乙"}).json()[
        "data"
    ]
    assert fake.soft_stop_calls == 0  # 非生效 → 不软停
    assert out["active"]["label"] == "甲"  # 当前生效不变


def test_put_hot_reload_visible_to_resolve_live(client):
    """PUT 后 resolve_live_omni_config 立即取到新 model/base_url —— 热生效契约。"""
    from miloco.perception.engine.config import OmniConfig
    from miloco.perception.engine.omni.omni_client import resolve_live_omni_config

    client.put(
        "/api/admin/omni-config",
        json={
            "label": "热",
            "model": "hot-model",
            "base_url": "https://hot.example/v1",
            "api_key": "sk-hotkey123456",
        },
    )
    base = OmniConfig(model="old", base_url="old", api_key="k0", timeout=123.0)
    live = resolve_live_omni_config(base)
    assert live.model == "hot-model"
    assert live.base_url == "https://hot.example/v1"
    assert live.timeout == 123.0  # 非用户字段保持快照


# ─── 测试连接 / 列模型(mock httpx) ─────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


def _fake_async_client(resp=None, exc=None, get_resp=None, post_resp=None):
    # get_resp/post_resp 可分别指定(probe_omni 先 GET /models,404/405 才回退 chat 的 POST)
    g = get_resp if get_resp is not None else resp
    p = post_resp if post_resp is not None else resp

    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if exc:
                raise exc
            return g

        async def post(self, *a, **k):
            if exc:
                raise exc
            return p

    return _C


def test_test_connection_ok_chat_succeeds(client, monkeypatch, real_probe):
    """GET /models 过鉴权/可达预检后,极简 chat 调通 → ok(连接正常)。不再以模型在不在列表为准。"""
    from miloco.perception.engine.omni import probe

    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"data": [{"id": "m1"}]})),
    )
    data = client.post(
        "/api/admin/omni-config/test",
        json={"model": "m1", "base_url": "https://x/v1", "api_key": "sk-xxx"},
    ).json()["data"]
    assert data["ok"] is True
    assert data["code"] == "ok"
    assert data["message"] == "连接正常"


def test_test_connection_ok_even_if_model_not_listed(client, monkeypatch, real_probe):
    """模型不在 /models 列表、但 chat 能调通 → 仍判 ok —— 直接验证模型是否可用,
    不靠「在不在可用列表」这种弱判据(列表常不全)。"""
    from miloco.perception.engine.omni import probe

    # GET /models 返回不含该模型的列表,但 chat(POST)返回 200 → 模型实际可用
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": "other"}]}),
            post_resp=_FakeResp(200),
        ),
    )
    data = client.post(
        "/api/admin/omni-config/test",
        json={"model": "m1", "base_url": "https://x/v1", "api_key": "sk-xxx"},
    ).json()["data"]
    assert data["ok"] is True
    assert data["code"] == "ok"  # 不在列表照样判 ok


def test_test_connection_not_found(client, monkeypatch, real_probe):
    # GET /models 404 → 回退 chat 探测,chat 也 404 → not_found
    from miloco.perception.engine.omni import probe

    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(404), post_resp=_FakeResp(404, text="no such model")
        ),
    )
    data = client.post(
        "/api/admin/omni-config/test",
        json={"model": "m1", "base_url": "https://x/v1", "api_key": "sk-x"},
    ).json()["data"]
    assert data["ok"] is False
    assert data["code"] == "not_found"


def test_test_connection_rejected_authed(client, monkeypatch, real_probe):
    # GET /models 404 → 回退 chat,chat 返 400(鉴权过、仅请求体被拒)→ rejected_authed
    from miloco.perception.engine.omni import probe

    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(404), post_resp=_FakeResp(400, text="bad request")
        ),
    )
    data = client.post(
        "/api/admin/omni-config/test",
        json={"model": "m1", "base_url": "https://x/v1", "api_key": "sk-x"},
    ).json()["data"]
    assert data["ok"] is False
    assert data["code"] == "rejected_authed"


def test_test_connection_bad_key(client, monkeypatch, real_probe):
    from miloco.perception.engine.omni import probe

    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(401, text="unauthorized")),
    )
    data = client.post(
        "/api/admin/omni-config/test",
        json={"model": "m1", "base_url": "https://x/v1", "api_key": "sk-bad"},
    ).json()["data"]
    assert data["ok"] is False
    assert data["code"] == "bad_key"
    assert data["status"] == 401
    assert "API Key" in data["message"]


def test_test_connection_unreachable(client, monkeypatch, real_probe):
    import httpx
    from miloco.perception.engine.omni import probe

    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ConnectError("boom"))
    )
    data = client.post(
        "/api/admin/omni-config/test",
        json={"model": "m1", "base_url": "https://nope.invalid/v1", "api_key": "sk-x"},
    ).json()["data"]
    assert data["ok"] is False
    assert data["code"] == "unreachable"
    assert "无法连接" in data["message"]


def test_test_connection_no_key(client):
    data = client.post(
        "/api/admin/omni-config/test",
        json={"model": "m1", "base_url": "https://x/v1"},
    ).json()["data"]
    assert data["ok"] is False
    assert data["code"] == "no_key"
    assert "未配置" in data["message"]


def test_list_models_ok(client, monkeypatch):
    from miloco.perception.engine.omni import probe

    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"data": [{"id": "b"}, {"id": "a"}]})),
    )
    data = client.post(
        "/api/admin/omni-config/models",
        json={"base_url": "https://x/v1", "api_key": "sk-x"},
    ).json()["data"]
    assert data["ok"] is True
    assert data["models"] == ["a", "b"]  # sorted


def test_list_models_no_key(client, monkeypatch):
    """无 key 但 URL 可达(探测连得上,401 也算可达)→ 报缺 key。"""
    from miloco.perception.engine.omni import probe

    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    data = client.post(
        "/api/admin/omni-config/models", json={"base_url": "https://x/v1"}
    ).json()["data"]
    assert data["ok"] is False
    assert data["code"] == "no_key"
    assert "未配置" in data["message"]


def test_list_models_no_key_unreachable_url_reports_url_first(client, monkeypatch):
    """无 key 且 URL 不可达 → 优先报 URL 错(unreachable),而非被「缺 key」短路掩盖。"""
    import httpx
    from miloco.perception.engine.omni import probe

    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ConnectError("boom"))
    )
    data = client.post(
        "/api/admin/omni-config/models", json={"base_url": "https://nope.invalid/v1"}
    ).json()["data"]
    assert data["ok"] is False
    assert data["code"] == "unreachable"  # URL 错优先于缺 key


def test_list_models_no_key_bad_url_404_reports_url_first(client, monkeypatch):
    """无 key 且 URL 主机可达但地址/端点不对(返回 404,如填错地址命中 openresty 404 页)→
    优先报 URL 错(http_error),而非「未配置 API Key」。这是「先检查 URL 错误」的核心用例。"""
    from miloco.perception.engine.omni import probe

    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(404, text="<html>404 openresty</html>")),
    )
    data = client.post(
        "/api/admin/omni-config/models", json={"base_url": "https://wrong.example/v1"}
    ).json()["data"]
    assert data["ok"] is False
    assert data["code"] == "http_error"  # 地址错优先于缺 key,且不泄漏原始 HTML


# ─── Gemini(原生协议）探测 / 列模型：走 adapter、x-goog-api-key、跳过 GET /models ───


def _recording_async_client(calls, get_resp=None, post_resp=None):
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            calls.append(("GET", a[0] if a else k.get("url"), k.get("headers") or {}))
            return get_resp

        async def post(self, *a, **k):
            calls.append(("POST", a[0] if a else k.get("url"), k.get("headers") or {}))
            return post_resp

    return _C


def test_test_connection_gemini_skips_preflight_uses_goog_key(
    client, monkeypatch, real_probe
):
    """Gemini（非 OpenAI 兼容族）测连通性：跳过 GET /models 预检，直接 adapter 化 chat，
    用 x-goog-api-key + :generateContent。原硬编码 Bearer + /models 会对合法 Gemini 误报失败。"""
    from miloco.perception.engine.omni import probe as p

    calls: list = []
    monkeypatch.setattr(
        p.httpx, "AsyncClient", _recording_async_client(calls, post_resp=_FakeResp(200))
    )
    data = client.post(
        "/api/admin/omni-config/test",
        json={
            "model": "gemini-3-flash-preview",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "k",
        },
    ).json()["data"]
    assert data["ok"] is True and data["code"] == "ok"
    assert "GET" not in [c[0] for c in calls]  # 跳过 OpenAI 特有的 /models 预检
    post = next(c for c in calls if c[0] == "POST")
    assert post[1].endswith(":generateContent")
    assert "x-goog-api-key" in post[2] and "Authorization" not in post[2]


def test_fetch_models_gemini_parses_name_and_goog_key(client, monkeypatch):
    """Gemini 列模型：用 x-goog-api-key，解析 {models:[{name}]} 并剥 "models/" 前缀。"""
    from miloco.perception.engine.omni import probe as p

    calls: list = []
    resp = _FakeResp(200, {"models": [
        {"name": "models/gemini-3.5-flash"},
        {"name": "models/gemini-3-flash-preview"},
    ]})
    monkeypatch.setattr(p.httpx, "AsyncClient", _recording_async_client(calls, get_resp=resp))
    data = client.post(
        "/api/admin/omni-config/models",
        json={"base_url": "https://generativelanguage.googleapis.com/v1beta", "api_key": "k"},
    ).json()["data"]
    assert data["ok"] is True
    assert data["models"] == ["gemini-3-flash-preview", "gemini-3.5-flash"]  # 剥前缀 + sorted
    get = next(c for c in calls if c[0] == "GET")
    assert "x-goog-api-key" in get[2] and "Authorization" not in get[2]


# ─── 测通 + 三元组匹配 active → 主动清熔断(与 put/activate/retry 恢复路径对齐) ───


def _force_breaker_open_config():
    """把熔断打到 OPEN_CONFIG(bad_key),模拟 provider 侧 401 抖动后卡在配置态的场景。"""
    import asyncio

    from miloco.perception.engine.omni.circuit_breaker import (
        get_omni_circuit_breaker,
    )
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    cb = get_omni_circuit_breaker()
    asyncio.run(
        cb.record_probe_result(
            False,
            ClassifiedError("bad_key", "m", ErrorCategory.CONFIG),
        )
    )
    assert cb.snapshot().state == "error"


def test_test_connection_ok_matching_active_clears_breaker(client):
    """测通 + 三元组与当前 active 完全一致 → 熔断从 OPEN_CONFIG 回 CLOSED。
    这是「测通即恢复」的最直觉路径,OPEN_CONFIG 下 tick 不自动探测,不清则用户困在红条上。"""
    from miloco.perception.engine.omni.circuit_breaker import (
        get_omni_circuit_breaker,
    )

    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    _force_breaker_open_config()

    data = client.post(
        "/api/admin/omni-config/test",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    ).json()["data"]
    assert data["ok"] is True

    assert get_omni_circuit_breaker().snapshot().state == "ok"


def test_test_connection_ok_normalizes_base_url_trailing_slash(client):
    """三元组匹配时 base_url 归一化(rstrip '/'):存 https://x/v1、测 https://x/v1/ 仍算匹配。"""
    from miloco.perception.engine.omni.circuit_breaker import (
        get_omni_circuit_breaker,
    )

    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    _force_breaker_open_config()

    client.post(
        "/api/admin/omni-config/test",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1/",  # 结尾多一个斜杠
            "api_key": "sk-active",
        },
    )
    assert get_omni_circuit_breaker().snapshot().state == "ok"
    # 指纹归一化后应与落盘时(rstrip '/')一致,读回验证记录不应因尾斜杠而失配
    profile = next(p for p in _get(client)["profiles"] if p["label"] == "甲")
    assert profile["last_verified"] is not None


def test_test_connection_ok_not_matching_active_leaves_breaker(client):
    """测通但三元组 ≠ 当前 active(测的是另一档案 / 未保存的新配置) → 熔断状态不变,
    防止「测别的档案通了」把 active 的熔断误清。"""
    from miloco.perception.engine.omni.circuit_breaker import (
        get_omni_circuit_breaker,
    )

    # 存 active 档案「甲」
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    # 再存一套非 active 档案「乙」
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://y/v1",
            "api_key": "sk-other",
            "activate": False,
        },
    )
    _force_breaker_open_config()

    # 测「乙」(非 active) → 测通,但不该动 active 的熔断
    data = client.post(
        "/api/admin/omni-config/test",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://y/v1",
            "api_key": "sk-other",
        },
    ).json()["data"]
    assert data["ok"] is True

    assert get_omni_circuit_breaker().snapshot().state == "error"


def test_test_connection_failure_does_not_touch_breaker(client, monkeypatch, real_probe):
    """测失败(不管测的是不是 active) → 不动熔断状态。测失败本就没有"已验可用"的语义。"""
    from miloco.perception.engine.omni import probe
    from miloco.perception.engine.omni.circuit_breaker import (
        get_omni_circuit_breaker,
    )

    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    _force_breaker_open_config()

    # 把 probe 打成 401 → bad_key
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(401, text="unauthorized")),
    )
    data = client.post(
        "/api/admin/omni-config/test",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    ).json()["data"]
    assert data["ok"] is False
    assert data["code"] == "bad_key"

    # 熔断仍是 error
    assert get_omni_circuit_breaker().snapshot().state == "error"


# ─── 复现测试: 验证记录持久化的 5 个问题 ──────────────────────────────────


def test_put_preflight_failure_does_not_clobber_existing_verified_record(
    client, monkeypatch
):
    """编辑当前生效档案「甲」preflight 失败 → 400,配置整体不落盘(仍是旧值),
    kv 里「甲」原本有效的验证记录也不该被换成那份没落盘的新配置的指纹。"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    before = next(p for p in _get(client)["profiles"] if p["label"] == "甲")
    assert before["last_verified"] is not None
    assert before["last_verified"]["ok"] is True

    async def _fail(*a, **k):
        return {"ok": False, "code": "bad_key", "message": "unauthorized"}

    monkeypatch.setattr("miloco.admin.router._probe.probe_omni", _fail)

    resp = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m2",
            "base_url": "https://x/v1",
            "api_key": "sk-new",
            "original_label": "甲",
        },
    )
    assert resp.status_code == 400

    out = _get(client)
    # 配置整体没落盘:active 还是旧的 m1
    assert out["active"]["model"] == "m1"
    after = next(p for p in out["profiles"] if p["label"] == "甲")
    # 「甲」的验证记录不该被那份没落盘的新配置(m2)覆盖
    assert after["last_verified"] is not None
    assert after["last_verified"]["ok"] is True


def test_put_preflight_failure_on_unchanged_active_profile_updates_record(
    client, monkeypatch
):
    """编辑当前生效档案「甲」但一字未改就点保存,preflight 失败 → 400;此时探的
    三元组与「甲」已落盘的那份完全一致,这条失败结论说的就是已落盘配置本身,必须
    写进记录,不能让界面停在旧的绿 ●。"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    before = next(p for p in _get(client)["profiles"] if p["label"] == "甲")
    assert before["last_verified"]["ok"] is True
    fp_before = before["last_verified"]["fingerprint"]

    async def _fail(*a, **k):
        return {"ok": False, "code": "bad_key", "message": "unauthorized"}

    monkeypatch.setattr("miloco.admin.router._probe.probe_omni", _fail)

    resp = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "",  # 留空 = 沿用原 key,即一字未改
            "original_label": "甲",
        },
    )
    assert resp.status_code == 400

    after = next(p for p in _get(client)["profiles"] if p["label"] == "甲")
    assert after["last_verified"]["ok"] is False
    assert after["last_verified"]["code"] == "bad_key"
    assert after["last_verified"]["fingerprint"] == fp_before


def test_test_connection_unsaved_new_config_does_not_touch_active_record(client):
    """测未保存的新配置(与任何已落盘档案三元组都不匹配)不该写记录 —— 尤其不该
    落到当前生效档案「甲」名下,清空它原本的绿态。"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    before = next(p for p in _get(client)["profiles"] if p["label"] == "甲")
    assert before["last_verified"] is not None

    # 测一份完全没保存过的新配置:不传 label,三元组也跟「甲」不同
    client.post(
        "/api/admin/omni-config/test",
        json={
            "model": "m-never-saved",
            "base_url": "https://never-saved/v1",
            "api_key": "sk-never-saved",
        },
    )

    after = next(p for p in _get(client)["profiles"] if p["label"] == "甲")
    assert after["last_verified"] is not None
    assert after["last_verified"] == before["last_verified"]


def test_rename_migrates_verified_record(client):
    """改名(不触发 probe 的场景)应把旧 label 的验证记录迁到新 label,而不是留在旧键
    上永久滞留、新键读不到。"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://y/v1",
            "api_key": "sk-yi123456",
            "activate": False,
        },
    )
    # 显式按 label「乙」测试,写下「乙」名下的验证记录(非激活流程)
    client.post(
        "/api/admin/omni-config/test",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://y/v1",
            "api_key": "sk-yi123456",
        },
    )
    before = next(p for p in _get(client)["profiles"] if p["label"] == "乙")
    assert before["last_verified"] is not None

    # 改名「乙」→「乙2」,不激活(不触发 probe),配置一字节未变
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙2",
            "model": "m2",
            "base_url": "https://y/v1",
            "original_label": "乙",
            "activate": False,
        },
    )
    out = _get(client)
    assert not any(p["label"] == "乙" for p in out["profiles"])
    renamed = next(p for p in out["profiles"] if p["label"] == "乙2")
    assert renamed["last_verified"] is not None


def test_get_omni_config_survives_non_dict_verified_record(client):
    """kv 里某个 label 的验证记录不是 dict(脏数据/外部写入)时,GET 不该 500,
    应按「无记录」处理。"""
    from miloco.admin.router import manager
    from miloco.database.kv_repo import OmniConfigKeys

    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    manager.kv_repo.set(
        OmniConfigKeys.LAST_VERIFIED_KEY, json.dumps({"甲": "corrupted-not-a-dict"})
    )

    resp = client.get("/api/admin/omni-config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    after = next(p for p in data["profiles"] if p["label"] == "甲")
    assert after["last_verified"] is None


def test_test_endpoint_writes_body_label_not_first_triple_match(client):
    """review 回归:三个档案共用完全相同的 model+base_url+api_key,test 端点带
    body.label 精确指向"非列表里第一个"的那套 —— 结果必须记到它自己名下,
    不能被三元组匹配抢到列表里排第一的「甲」头上。"""
    same = {
        "model": "m1",
        "base_url": "https://x/v1",
        "api_key": "sk-same-key1",
    }
    client.put(
        "/api/admin/omni-config",
        json={"label": "甲", "activate": False, **same},
    )
    client.put(
        "/api/admin/omni-config",
        json={"label": "乙", "activate": False, **same},
    )
    client.put(
        "/api/admin/omni-config",
        json={"label": "丙", "activate": False, **same},
    )

    # 先测「甲」,让它落一条真实的验证记录(带真实 at_ms)
    r = client.post("/api/admin/omni-config/test", json={"label": "甲", **same})
    assert r.json()["data"]["ok"] is True
    jia_before = next(p for p in _get(client)["profiles"] if p["label"] == "甲")
    rec_before = jia_before["last_verified"]
    assert rec_before is not None

    # 探活是打桩的,两次 client.post 之间没有真实网络往返,不显式错开时间的话
    # 两次写入可能落在同一毫秒 —— 一旦「丙」的记录被错记到「甲」名下,只比 at_ms
    # 会因为 ok/code/message/fingerprint 全部字节级相同而假绿,抓不到回归。
    time.sleep(0.005)

    # 再测「丙」(列表里排第三,三元组与「甲」完全相同) —— 结果应记到「丙」自己名下
    r = client.post("/api/admin/omni-config/test", json={"label": "丙", **same})
    assert r.json()["data"]["ok"] is True

    out = _get(client)["profiles"]
    bing = next(p for p in out if p["label"] == "丙")
    jia_after = next(p for p in out if p["label"] == "甲")
    assert bing["last_verified"] is not None
    # 关键断言:「甲」的验证记录整条(ok/code/message/fingerprint/at_ms)原样未变,
    # 没有被这次测「丙」覆盖
    assert jia_after["last_verified"] == rec_before
    # 且「丙」确实是新写的一条,不是复用「甲」的旧记录
    assert bing["last_verified"]["at_ms"] > rec_before["at_ms"]


def test_test_endpoint_blank_label_still_no_write_when_unmatched(client):
    """回归防护:body.label 为空且三元组匹配不到任何档案 / active 时,仍然"匹配不到就
    不写",且不回退到 active —— 守住上一轮修好的行为,防止这次改动把它带回来。"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-active",
        },
    )
    before = next(p for p in _get(client)["profiles"] if p["label"] == "甲")
    assert before["last_verified"] is not None
    at_ms_before = before["last_verified"]["at_ms"]

    # 不传 label,三元组也跟「甲」不同、更不是 active —— 不该落到「甲」名下
    client.post(
        "/api/admin/omni-config/test",
        json={
            "model": "m-never-saved",
            "base_url": "https://never-saved/v1",
            "api_key": "sk-never-saved",
        },
    )

    after = next(p for p in _get(client)["profiles"] if p["label"] == "甲")
    assert after["last_verified"]["at_ms"] == at_ms_before


# ─── 删档案自动清 kv 里的验证记录(直读 kv,不只看接口返回) ──────────────────


def test_delete_removes_verified_record_from_kv(client):
    """删档案「甲」:接口层面 profiles 里没了不够,kv 里 LAST_VERIFIED_KEY 存的
    原始记录也必须一并清掉,否则残留的旧验证记录会在同名档案重建后借尸还魂。"""
    from miloco.admin.router import manager
    from miloco.database.kv_repo import OmniConfigKeys

    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-k111111111",
        },
    )  # 默认 activate=true → preflight 通过 → 落一条「甲」的验证记录
    before = json.loads(manager.kv_repo.get(OmniConfigKeys.LAST_VERIFIED_KEY) or "{}")
    assert "甲" in before

    client.post("/api/admin/omni-config/delete", json={"label": "甲"})

    after = json.loads(manager.kv_repo.get(OmniConfigKeys.LAST_VERIFIED_KEY) or "{}")
    assert "甲" not in after


def test_delete_synthesized_active_empty_label_also_clears_kv(client):
    """删「空 label 的当前生效合成行」:除了 profiles 清空,kv 里按展示 label
    (model @ base_url)存的验证记录也必须一并删掉,不留孤儿键。"""
    from miloco.admin.router import manager
    from miloco.config.settings import get_settings
    from miloco.database.kv_repo import OmniConfigKeys

    s = get_settings()
    s.model.omni.label = ""
    s.model.omni.model = "ad-hoc-model"
    s.model.omni.base_url = "https://adhoc/v1"
    s.model.omni.api_key = "sk-adhoc999999"
    data = _get(client)
    synth_label = data["profiles"][0]["label"]
    assert synth_label == "ad-hoc-model @ https://adhoc/v1"

    # 显式测一次这套合成配置,让它按展示 label 落一条验证记录
    client.post(
        "/api/admin/omni-config/test",
        json={
            "label": synth_label,
            "model": "ad-hoc-model",
            "base_url": "https://adhoc/v1",
            "api_key": "sk-adhoc999999",
        },
    )
    before = json.loads(manager.kv_repo.get(OmniConfigKeys.LAST_VERIFIED_KEY) or "{}")
    assert synth_label in before

    client.post("/api/admin/omni-config/delete", json={"label": synth_label})

    after = json.loads(manager.kv_repo.get(OmniConfigKeys.LAST_VERIFIED_KEY) or "{}")
    assert synth_label not in after


def test_kv_verified_record_never_contains_api_key_plaintext(client, monkeypatch):
    """kv 里 LAST_VERIFIED_KEY 的原始字符串不含 api_key 明文,只存 sha256 指纹(16 位十六进制)。
    覆盖两条写入路径:PUT 激活(preflight 成功)与 activate_omni_config(哪怕 probe 失败也写)。"""
    import re

    from miloco.admin.router import manager
    from miloco.database.kv_repo import OmniConfigKeys

    secret_key = "sk-topsecretkeyabcd1234"
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": secret_key,
        },
    )
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "乙",
            "model": "m2",
            "base_url": "https://y/v1",
            "api_key": "sk-anothersecretkey5678",
            "activate": False,
        },
    )

    # activate 路径:probe 失败也要写记录(router.py 里 _record_omni_verified 在
    # result.get("ok") 为假时依旧被调用,先于 400 抛出)
    async def _fail(*a, **k):
        return {"ok": False, "code": "bad_key", "message": "unauthorized"}

    monkeypatch.setattr("miloco.admin.router._probe.probe_omni", _fail)
    client.post("/api/admin/omni-config/activate", json={"label": "乙"})

    raw = manager.kv_repo.get(OmniConfigKeys.LAST_VERIFIED_KEY) or "{}"
    assert secret_key not in raw
    assert "sk-anothersecretkey5678" not in raw

    records = json.loads(raw)
    for rec in records.values():
        assert re.fullmatch(r"[0-9a-f]{16}", rec["fingerprint"])
