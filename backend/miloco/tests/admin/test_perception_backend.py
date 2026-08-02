"""``/api/admin/perception-backend`` 的端点级测试。

这两个端点是本地视觉通路**唯一**的用户入口:切换、探活、能力声明全从这里出。
在此之前它们一行测试都没有,而它们已经因为一次字段改名整体 500 过一次 —— 那次
后端与边车的单测全绿,功能却在界面上完全不可达。所以这里刻意走 HTTP,而不是直接
调函数:改名、依赖注入、序列化这些只有真正发一次请求才暴露的问题,必须被覆盖。
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 在任何 fixture 打补丁之前留住真实实现 —— 有一条测试要把它换回来真跑一遍。
from miloco.admin.router import _rules_with_direct_device_actions as _REAL_RULE_LOOKUP
from miloco.admin.router import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"perception": {"local_vision": {"base_url": "http://127.0.0.1:18800"}}}),
        encoding="utf-8",
    )
    reset_settings()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    yield TestClient(app)
    reset_settings()


class _NoRestartSvc:
    async def stop_to_unconfigured(self):
        return None


class _NoRestartMgr:
    perception_service = _NoRestartSvc()


@pytest.fixture(autouse=True)
def _no_engine_restart(monkeypatch):
    """切换成功后会去软停引擎。测试里没有 manager,拦掉它。"""
    monkeypatch.setattr("miloco.admin.router.get_manager", lambda: _NoRestartMgr())


@pytest.fixture
def health(monkeypatch):
    """替换边车探活。set(...) 决定下一次探活返回什么,raise_() 让它不可达。"""
    from miloco.perception.local_vision import client as lv

    state: dict = {"ret": {"status": "ok", "model_loaded": True,
                           "device": "cuda:0", "backend": "codec"}, "exc": None}

    def _fn(self):
        if state["exc"]:
            raise lv.LocalVisionError(state["exc"])
        return dict(state["ret"])

    monkeypatch.setattr(lv.LocalVisionClient, "health_sync", _fn)

    class _H:
        def set(self, **kw):
            state["ret"].update(kw)
            state["exc"] = None

        def down(self, msg="connection refused"):
            state["exc"] = msg

    return _H()


@pytest.fixture(autouse=True)
def _no_blocking_rules(monkeypatch):
    monkeypatch.setattr(
        "miloco.admin.router._rules_with_direct_device_actions", lambda: []
    )


# ── GET ───────────────────────────────────────────────────────────────────


def test_get_defaults_to_cloud_and_reports_capabilities(client, health):
    r = client.get("/api/admin/perception-backend")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["backend"] == "cloud"
    caps = d["local_capabilities"]
    # 能力声明是界面上那几行"切过去会失去什么"的唯一来源,必须真出现在响应里。
    assert caps["audio"] is False
    # 认人现在由本地 ReID 提供(不经过视觉大模型),默认开 —— 界面上不该再写
    # "切过去会失去身份识别"。这一条跟着配置走,下一个用例钉住关掉后的表现。
    assert caps["identity"] is True
    assert caps["static_rule_execution"] is False
    assert caps["needs_api_key"] is False


def test_identity_capability_follows_the_setting(client, health, monkeypatch):
    """用户关掉本地认人后,界面必须如实说没有这项能力。

    能力声明与实际行为分家是最坏的一种错:界面说"能认人",而每一条描述里都是
    「一名男子」—— 用户会去查身份库、查登记,而问题在一个开关上。
    """
    from miloco.config import get_settings

    monkeypatch.setattr(
        get_settings().perception.local_vision, "identity_enabled", False
    )
    d = client.get("/api/admin/perception-backend").json()["data"]
    assert d["local_capabilities"]["identity"] is False


def test_get_reports_unreachable_without_leaking_probe_detail(client, health):
    """边车不可达时只回粗粒度标记 —— base_url 用户可填,回显原始异常等于内网探针。"""
    health.down("[Errno 111] Connection refused to 10.0.0.7:22")
    d = client.get("/api/admin/perception-backend?probe=1").json()["data"]
    assert d["error"] == "unreachable"
    assert d["health"] is None
    assert "10.0.0.7" not in json.dumps(d)


def test_get_carries_cloud_hint_when_cloud_path_is_not_ready(client, health):
    """没配 Key 时,切回云端不会被拒绝,但必须提前说明它同样起不来。"""
    d = client.get("/api/admin/perception-backend?probe=1").json()["data"]
    assert "API Key" in d["cloud_hint"]


# ── POST:切到 local 的各道闸 ──────────────────────────────────────────────


def test_switch_to_local_succeeds_when_sidecar_is_healthy(client, health):
    r = client.post("/api/admin/perception-backend", json={"backend": "local"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["backend"] == "local"


def test_switch_to_local_refused_when_sidecar_unreachable(client, health):
    health.down()
    r = client.post("/api/admin/perception-backend", json={"backend": "local"})
    assert r.status_code == 400
    assert client.get("/api/admin/perception-backend").json()["data"]["backend"] == "cloud"


def test_switch_to_local_refused_while_model_still_loading(client, health):
    health.set(model_loaded=False, status="loading")
    r = client.post("/api/admin/perception-backend", json={"backend": "local"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "loading"


def test_switch_to_local_refused_when_credentials_are_rejected(client, health):
    """探活能通但凭证不被接受 —— 若放过去,每一窗推理都会 401 而界面一路绿灯。"""
    health.set(auth_required=True, auth_ok=False)
    r = client.post("/api/admin/perception-backend", json={"backend": "local"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "auth_rejected"


def test_switch_to_local_refused_by_direct_device_rules(client, health, monkeypatch):
    monkeypatch.setattr(
        "miloco.admin.router._rules_with_direct_device_actions",
        lambda: ["厨房明火关阀"],
    )
    r = client.post("/api/admin/perception-backend", json={"backend": "local"})
    assert r.status_code == 400
    assert r.json()["detail"]["rules"] == ["厨房明火关阀"]


def test_refusal_works_through_the_real_rule_lookup(client, health, monkeypatch):
    """不 mock 那个 helper,让端点真的去查规则。

    其余用例为了隔离都把 helper 换掉了 —— 全都换掉的话,「查规则 → 拒绝」这条
    实际链路就一次都没被走过,helper 里一个拼写错误可以让整道闸静默失效。
    """
    from types import SimpleNamespace

    class _Repo:
        def get_all(self, enabled_only=False):
            return [SimpleNamespace(id="r1", name="厨房明火关阀", enabled=True,
                                    actions=[object()], on_enter_actions=[],
                                    on_exit_actions=[])]

    # 只把 helper 换回真实实现;monkeypatch.undo() 会连 client / health 两个
    # fixture 的补丁一起撤掉(同一测试里它们共用一个 monkeypatch 实例)。
    monkeypatch.setattr(
        "miloco.admin.router._rules_with_direct_device_actions", _REAL_RULE_LOOKUP
    )
    monkeypatch.setattr("miloco.database.rule_repo.RuleRepo", _Repo)

    r = client.post("/api/admin/perception-backend", json={"backend": "local"})
    assert r.status_code == 400
    assert r.json()["detail"]["rules"] == ["厨房明火关阀"]

    # 同一条链路也要能在规则列表里看到它 —— 卡片就是靠这个字段提前预告的。
    d = client.get("/api/admin/perception-backend?probe=1").json()["data"]
    assert d["blocking_static_rules"] == ["厨房明火关阀"]


# ── POST:凭证与地址的绑定关系 ────────────────────────────────────────────


def test_changing_base_url_without_a_new_token_clears_the_stored_one(client, health):
    client.post("/api/admin/perception-backend",
                json={"backend": "cloud", "token": "secret-1"})
    assert client.get("/api/admin/perception-backend").json()["data"]["local_vision"]["has_token"]

    # 换地址不带新 token:旧凭证不能跟着去新地址(否则拿到 admin 的人可以把它
    # 和随后每一帧画面一起定向到自己控制的服务上)。
    client.post("/api/admin/perception-backend",
                json={"backend": "cloud", "base_url": "http://127.0.0.1:19999"})
    d = client.get("/api/admin/perception-backend").json()["data"]
    assert d["local_vision"]["has_token"] is False
    assert d["local_vision"]["base_url"] == "http://127.0.0.1:19999"


def test_writing_a_base_url_requires_a_successful_probe_even_on_cloud(client, health):
    """否则 backend=cloud 这条分支就是个免校验的任意地址写入口,而 GET 随后会去探它。"""
    health.down()
    r = client.post("/api/admin/perception-backend",
                    json={"backend": "cloud", "base_url": "http://10.0.0.7:22"})
    assert r.status_code == 400
    assert client.get("/api/admin/perception-backend").json()[
        "data"]["local_vision"]["base_url"] == "http://127.0.0.1:18800"


# ── POST:切回云端永远不能被挡住 ──────────────────────────────────────────


def test_switch_back_to_cloud_is_never_blocked(client, health, monkeypatch):
    """云端是本地通路出问题时的退路。缺 Key、边车挂了、有直连规则 —— 都不能挡住它,
    否则用户会被关在一个不工作的后端里,而唯一的出口就在这个端点上。"""
    client.post("/api/admin/perception-backend", json={"backend": "local"})
    health.down()
    monkeypatch.setattr(
        "miloco.admin.router._rules_with_direct_device_actions", lambda: ["某规则"]
    )
    r = client.post("/api/admin/perception-backend", json={"backend": "cloud"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["backend"] == "cloud"


# ── 地址的最低校验 ────────────────────────────────────────────────────────


def test_base_url_with_query_or_fragment_is_rejected(client, health):
    """客户端拼的是 f"{base_url}/health";尾随的 ? 或 # 会让后面那段变成查询串或
    锚点,于是这个配置项变成了任意路径构造器(实测 http://host/admin# 会打到
    /admin)。不做内网黑名单,但也不能让它变成路径构造器。"""
    for bad in ("http://127.0.0.1:18800/admin#", "http://127.0.0.1:18800/?x=1"):
        r = client.post("/api/admin/perception-backend",
                        json={"backend": "cloud", "base_url": bad})
        assert r.status_code == 400, bad
        assert r.json()["detail"]["code"] == "bad_url"


def test_base_url_must_be_http_or_https(client, health):
    r = client.post("/api/admin/perception-backend",
                    json={"backend": "cloud", "base_url": "file:///etc/passwd"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_url"


def test_load_failure_is_reported_instead_of_asking_the_user_to_wait(client, health):
    """权重路径打错是最常见的首次部署翻车点。边车会永远停在 loading,
    让用户"稍后再试"等于让他等一辈子。"""
    health.set(model_loaded=False, status="loading",
               load_error="FileNotFoundError: 权重目录不存在: /no/such/dir")
    r = client.post("/api/admin/perception-backend", json={"backend": "local"})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "load_failed"
    assert "/no/such/dir" in d["detail"]


def test_default_get_is_a_pure_config_read(client, health):
    """默认 GET 不打网络、不查规则库、不摸文件系统。

    仓库把"读配置"和"探活"分成两件事(get_omni_config 零 IO)。「模型」页挂载时
    有两个组件各调一次这个接口,默认探活的话每次进页面就是两次最长 3s 的同步
    HTTP + 两次规则库全表扫描,而其中一个组件只需要知道当前选的是哪条通路。
    """
    from miloco.perception.local_vision import client as lv

    probes: list = []
    monkeypatch_target = lv.LocalVisionClient.health_sync

    def _spy(self):
        probes.append(self.base_url)
        return monkeypatch_target(self)

    lv.LocalVisionClient.health_sync = _spy
    try:
        d = client.get("/api/admin/perception-backend").json()["data"]
    finally:
        lv.LocalVisionClient.health_sync = monkeypatch_target

    assert probes == [], f"纯读的 GET 仍然探活了: {probes}"
    assert d["backend"] == "cloud"
    assert d["health"] is None and d["error"] is None
    assert d["blocking_static_rules"] == []
    assert d["cloud_hint"] == ""
    # 能力声明是静态的,纯读也要给全(卡片靠它渲染那几行"会失去什么")。
    assert d["local_capabilities"]["static_rule_execution"] is False
