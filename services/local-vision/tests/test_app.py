"""HTTP 门面的行为测试 —— 不加载模型,用替身引擎。

这一层此前完全没有覆盖,而它承载着三件只在这里发生的事:鉴权、并发闸、以及
响应模型的字段边界。最后一件尤其要测:pydantic 默认 ``extra="ignore"``,引擎多
返回的字段若没在响应模型上声明,会在**序列化那一刻**被静默丢掉 —— 单测引擎、
单测解析都全绿,而 miloco 侧永远收不到那个字段。
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from local_vision import app as app_mod


class _StubEngine:
    """替身引擎:记录收到的参数,按脚本返回或抛错。不碰 torch。"""

    def __init__(self, out: dict | None = None, boom: bool = False):
        self.calls: list[dict] = []
        self.boom = boom
        self.out = out or {
            "caption": "客厅没有人", "rule_hits": [], "unparsed_rules": 0,
            "truncated": False, "gate_p": None, "backend": "codec",
            "timing_ms": {"total": 1.0}, "raw": "客厅没有人",
        }
        self.ready = True
        self.gate_available = True
        self.gate_error = None
        self.device = "cuda:0"
        self.video_backend = "codec"

    def perceive(self, video_path, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("inference exploded")
        return dict(self.out)


@pytest.fixture
def client():
    return TestClient(app_mod.app)


@pytest.fixture
def engine(monkeypatch):
    e = _StubEngine()
    monkeypatch.setattr(app_mod, "_engine", e)
    return e


def _body(**kw) -> dict:
    return {"video_b64": base64.b64encode(b"fake-mp4-bytes").decode(), **kw}


# ── /health 的鉴权结论 ────────────────────────────────────────────────────


def test_health_without_token_says_auth_not_required(client, engine, monkeypatch):
    monkeypatch.delenv("LOCAL_VISION_TOKEN", raising=False)
    d = client.get("/health").json()
    assert d["auth_required"] is False
    assert d["auth_ok"] is True
    assert d["model_loaded"] is True


def test_health_reports_auth_failure_instead_of_401(client, engine, monkeypatch):
    """凭证不对时 /health 仍要 200 —— 否则调用方分不清「服务没起来」和
    「起来了但 token 不对」,而这两种情况的处理完全不同。"""
    monkeypatch.setenv("LOCAL_VISION_TOKEN", "right")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {**r.json(), "auth_required": True, "auth_ok": False}

    ok = client.get("/health", headers={"Authorization": "Bearer right"}).json()
    assert ok["auth_ok"] is True


def test_a_non_ascii_configured_token_yields_401_not_500(client, engine, monkeypatch):
    """部署者在环境里配了中文 token 的情形。

    HTTP 头只能承载 ASCII,所以这样的 token 永远不可能被匹配上 —— 但结果必须是
    干净的 401。``compare_digest`` 对非 ASCII **str** 抛 TypeError,不先 encode 就
    会让每次鉴权变成未捕获 500,而 /health 仍报 ok:排查时两边完全对不上。
    """
    monkeypatch.setenv("LOCAL_VISION_TOKEN", "密钥-abc")
    assert client.get("/health").json()["auth_ok"] is False
    r = client.post("/v1/perceive", json=_body(),
                    headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 401


# ── /v1/perceive 的入口校验 ──────────────────────────────────────────────


def test_perceive_requires_the_token_when_configured(client, engine, monkeypatch):
    monkeypatch.setenv("LOCAL_VISION_TOKEN", "right")
    assert client.post("/v1/perceive", json=_body()).status_code == 401
    assert client.post("/v1/perceive", json=_body(),
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/v1/perceive", json=_body(),
                       headers={"Authorization": "Bearer right"}).status_code == 200


def test_perceive_rejects_malformed_payloads(client, engine):
    assert client.post("/v1/perceive", json={"video_b64": "!!!not base64!!!"}).status_code == 422
    assert client.post("/v1/perceive", json={"video_b64": ""}).status_code == 422


def test_perceive_is_503_before_the_model_finishes_loading(client, monkeypatch):
    monkeypatch.setattr(app_mod, "_engine", None)
    assert client.post("/v1/perceive", json=_body()).status_code == 503


# ── 并发闸:超限回 503,且**槽位一定收得回来** ────────────────────────────


def test_inflight_slot_is_released_after_a_failed_inference(client, monkeypatch):
    """推理抛错后不还槽位的话,几次之后服务永久 503,只能重启进程。"""
    monkeypatch.setattr(app_mod, "_engine", _StubEngine(boom=True))
    for _ in range(app_mod._MAX_INFLIGHT + 2):
        assert client.post("/v1/perceive", json=_body()).status_code == 500

    monkeypatch.setattr(app_mod, "_engine", _StubEngine())
    assert client.post("/v1/perceive", json=_body()).status_code == 200


def test_over_capacity_returns_503_rather_than_queueing(client, engine, monkeypatch):
    """排队到超时不如立刻说忙:miloco 本来就会把这一窗当"无结论"跳过。"""
    import threading

    sem = threading.Semaphore(0)
    monkeypatch.setattr(app_mod, "_inflight", sem)
    r = client.post("/v1/perceive", json=_body())
    assert r.status_code == 503
    assert "busy" in r.json()["detail"]


# ── 响应边界:引擎产出的字段必须原样到达调用方 ────────────────────────────


def test_unparsed_and_truncated_survive_the_response_model(client, monkeypatch):
    """这两个字段是"规则被静默推成未命中"的唯一线索。响应模型上漏声明一个,
    调用方就再也分不清「模型说不」和「输出被吃掉了」。"""
    monkeypatch.setattr(app_mod, "_engine", _StubEngine({
        "caption": "", "rule_hits": [{"name": "沙发有人", "hit": False, "reason": ""}],
        "unparsed_rules": 1, "truncated": True, "gate_p": 0.31,
        "backend": "frames", "timing_ms": {"total": 2.0}, "raw": "……",
    }))
    d = client.post("/v1/perceive", json=_body()).json()
    assert d["unparsed_rules"] == 1
    assert d["truncated"] is True
    assert d["gate_p"] == pytest.approx(0.31)


def test_every_kwarg_the_app_sends_is_one_the_real_engine_accepts(client, engine):
    """替身用 **kw 全盘接收,所以光比位置参数抓不到关键字改名。

    实测过:把 MageVLEngine.perceive 的 want_gate 改个名,边车 71 条测试全绿,
    而线上每个 /v1/perceive 都会 TypeError → 被兜底 except 变成 500。这里拿真
    签名去 bind 一次实际发出的调用,改名和多传一个参数都会立刻红。
    """
    import inspect

    from local_vision.engine import MageVLEngine

    client.post("/v1/perceive", json=_body(
        scene_ask="门关了吗?", camera_note="对着门口",
        rules=[{"name": "A", "query": "B"}],
        max_new_tokens=300, ngram_guard=32, want_gate=False,
    ))
    inspect.signature(MageVLEngine.perceive).bind(None, "/tmp/x.mp4", **engine.calls[0])


def test_request_fields_reach_the_engine_unchanged(client, engine):
    client.post("/v1/perceive", json=_body(
        scene_ask="门关了吗?", camera_note="这台对着门口",
        rules=[{"name": "沙发有人", "query": "有人在沙发上"}],
        max_new_tokens=300, ngram_guard=32, want_gate=False,
    ))
    kw = engine.calls[0]
    assert kw["scene_ask"] == "门关了吗?"
    assert kw["camera_note"] == "这台对着门口"
    assert kw["rules"] == [{"name": "沙发有人", "query": "有人在沙发上"}]
    assert kw["max_new_tokens"] == 300
    assert kw["ngram_guard"] == 32
    assert kw["want_gate"] is False


def test_ngram_guard_defaults_to_none_so_the_engine_decides(client, engine):
    client.post("/v1/perceive", json=_body())
    assert engine.calls[0]["ngram_guard"] is None


def test_stub_engine_signature_matches_the_real_one():
    """替身吞掉 **kw 的话,真引擎改个参数名它照样绿 —— 而线上每个请求 500。

    miloco 侧对客户端替身用的是同一条检查;两边的替身都必须钉住签名。
    """
    import inspect

    from local_vision.engine import MageVLEngine

    real = [p.name for p in inspect.signature(MageVLEngine.perceive).parameters.values()]
    stub = [p.name for p in inspect.signature(_StubEngine.perceive).parameters.values()]
    assert stub[:2] == real[:2], f"位置参数不一致: {stub[:2]} vs {real[:2]}"
    assert real[0] == "self" and real[1] == "video_path"


def test_max_new_tokens_bound_matches_the_miloco_side_ceiling():
    """边车接受的上限与 miloco 侧的 _MAX_NEW_TOKENS_CEILING 必须一致。

    两者分属独立部署的构件,各测各的话任一侧单独收紧都不会红 —— 而后果是规则一多
    每一窗就 422,在 miloco 的日志里与其它边车故障长得一模一样。
    """
    import pytest as _pytest
    from local_vision.app import PerceiveRequest
    from pydantic import ValidationError

    ok = PerceiveRequest(video_b64="", max_new_tokens=1024)
    assert ok.max_new_tokens == 1024
    with _pytest.raises(ValidationError):
        PerceiveRequest(video_b64="", max_new_tokens=1025)
