"""probe.py 单元测试。用 monkeypatch 替换 httpx.AsyncClient 走 fake 响应。"""
from __future__ import annotations

import httpx
import pytest

from miloco.perception.engine.omni import probe


class _FakeResp:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


def _fake_async_client(resp: _FakeResp | None = None, *, exc: Exception | None = None,
                      get_resp: _FakeResp | None = None, post_resp: _FakeResp | None = None):
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


# ─── probe_reachable ────────────────────────────────────────────────────────


async def test_probe_reachable_returns_none_on_200(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(200, {"data": []})))
    assert await probe.probe_reachable("https://ok.example/v1") is None


async def test_probe_reachable_returns_none_on_401(monkeypatch):
    """401 表示"地址对、只是需 key",不算 URL 错。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(401)))
    assert await probe.probe_reachable("https://ok.example/v1") is None


async def test_probe_reachable_unreachable_on_connect_error(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(exc=httpx.ConnectError("dns fail")))
    r = await probe.probe_reachable("https://nope.example/v1")
    assert r == {"code": "unreachable", "message": "无法连接 Base URL（ConnectError）"}


async def test_probe_reachable_http_error_on_404(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(404)))
    r = await probe.probe_reachable("https://ok.example/v1")
    assert r == {"code": "http_error", "message": "服务返回异常（HTTP 404）"}


# ─── fetch_models ───────────────────────────────────────────────────────────


async def test_fetch_models_ok(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(200, {"data": [{"id": "m1"}, {"id": "m2"}]})))
    r = await probe.fetch_models("https://ok/v1", "sk-x")
    assert r == {"ok": True, "models": ["m1", "m2"]}


async def test_fetch_models_bad_key_on_401(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(401)))
    r = await probe.fetch_models("https://ok/v1", "sk-x")
    assert r["ok"] is False and r["code"] == "bad_key"


async def test_fetch_models_unreachable_on_exception(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(exc=httpx.ConnectError("nope")))
    r = await probe.fetch_models("https://ok/v1", "sk-x")
    assert r["ok"] is False and r["code"] == "unreachable"


# ─── probe_chat ─────────────────────────────────────────────────────────────


async def test_probe_chat_ok(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(200)))
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is True and r["code"] == "ok" and r["status"] == 200
    assert "latency_ms" in r


async def test_probe_chat_bad_key(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(401)))
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is False and r["code"] == "bad_key" and r["status"] == 401


async def test_probe_chat_not_found(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(404)))
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "not_found" and r["status"] == 404


async def test_probe_chat_rejected_authed_on_400(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(400)))
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "rejected_authed"


async def test_probe_chat_rejected_authed_on_422(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(422)))
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "rejected_authed"


async def test_probe_chat_http_error_on_500(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(resp=_FakeResp(500)))
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "http_error"


async def test_probe_chat_unreachable_on_exception(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(exc=httpx.ReadTimeout("slow")))
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "unreachable"


# ─── probe_omni (两阶段) ────────────────────────────────────────────────────


async def test_probe_omni_get_401_short_circuits_to_bad_key(monkeypatch):
    """GET /models 401 立刻判 bad_key,不走 chat。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(get_resp=_FakeResp(401),
                                           post_resp=_FakeResp(200)))
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "bad_key"


async def test_probe_omni_get_500_short_circuits_to_http_error(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(get_resp=_FakeResp(500),
                                           post_resp=_FakeResp(200)))
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "http_error"


async def test_probe_omni_get_ok_then_chat_ok(monkeypatch):
    """GET /models 200 后调 chat,chat 200 → ok。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(get_resp=_FakeResp(200, {"data": [{"id": "m1"}]}),
                                           post_resp=_FakeResp(200)))
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is True and r["code"] == "ok"


async def test_probe_omni_get_ok_then_chat_not_found(monkeypatch):
    """模型不在列表但 GET 200:走 chat,chat 404 → not_found。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(get_resp=_FakeResp(200, {"data": [{"id": "other"}]}),
                                           post_resp=_FakeResp(404)))
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "not_found"


async def test_probe_omni_connect_error(monkeypatch):
    monkeypatch.setattr(probe.httpx, "AsyncClient",
                        _fake_async_client(exc=httpx.ConnectError("nope")))
    r = await probe.probe_omni("m1", "https://nope/v1", "sk-x")
    assert r["code"] == "unreachable"
