"""适配进程契约：{action, payload} → {code, data} 翻译、鉴权、幂等、错误码。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from adapter_pkg import server as srv
from adapter_pkg.hermes_client import _looks_like_overflow
from adapter_pkg.server import _IdempotencyCache


# ---------- _looks_like_overflow ----------

def test_overflow_detection():
    assert _looks_like_overflow("context overflow exceeded")
    assert _looks_like_overflow("maximum context length exceeded")
    assert _looks_like_overflow("Context Window too small")
    assert not _looks_like_overflow("network error")
    assert not _looks_like_overflow(None)
    assert not _looks_like_overflow("")


# ---------- _IdempotencyCache ----------

def test_idem_cache_set_get():
    c = _IdempotencyCache()
    c.set("k1", {"runId": "r1", "status": "ok"})
    assert c.get("k1") == {"runId": "r1", "status": "ok"}
    assert c.get("missing") is None


def test_idem_cache_expiry(monkeypatch):
    c = _IdempotencyCache()
    c.set("k1", {"status": "ok"})
    base = time.time()
    monkeypatch.setattr("adapter_pkg.server.time.time", lambda: base + 4000)
    assert c.get("k1") is None


# ---------- HTTP 契约（用 FakeHermesClient） ----------
# 新版 aiohttp 的 TestClient 必须在运行中的事件循环内构造，故每条用例
# 包一层 async 场景 + asyncio.run。

class FakeHermesClient:
    """记录调用、按预设返回结果的假客户端。"""

    def __init__(self, result: dict[str, Any] | Exception | None = None) -> None:
        self.result = result if result is not None else {"runId": "r-1", "status": "ok"}
        self.calls: list[dict] = []

    async def run_turn(self, payload: dict) -> dict[str, Any]:
        self.calls.append(payload)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result  # type: ignore[return-value]


def _run(coro):
    return asyncio.run(coro)


async def _post_json(client: TestClient, body, bearer="secret"):
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    resp = await client.post("/miloco/webhook", json=body, headers=headers)
    return resp.status, await resp.json()


async def _post_raw(client: TestClient, data: str, bearer="secret"):
    resp = await client.post("/miloco/webhook", data=data, headers={"Authorization": f"Bearer {bearer}"})
    return resp.status, await resp.json()


def test_auth_rejected():
    fake = FakeHermesClient()
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            return await _post_json(client, {"action": "agent", "payload": {}}, bearer="wrong")
    status, _ = _run(sc())
    assert status == 401


def test_missing_auth_header():
    fake = FakeHermesClient()
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            return await _post_json(client, {"action": "agent", "payload": {}}, bearer=None)
    status, _ = _run(sc())
    assert status == 401


def test_bad_json():
    fake = FakeHermesClient()
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            return await _post_raw(client, "not json")
    status, body = _run(sc())
    assert status == 400
    assert body["code"] == 1001


def test_unknown_action():
    fake = FakeHermesClient()
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            return await _post_json(client, {"action": "bogus", "payload": {}})
    status, body = _run(sc())
    assert status == 404
    assert body["code"] == 2001
    assert "bogus" in body["message"]


def test_get_trace_returns_done():
    fake = FakeHermesClient()
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            return await _post_json(client, {"action": "get_trace", "payload": {"runId": "x"}})
    _, body = _run(sc())
    assert body == {"code": 0, "message": "ok", "data": {"status": "done"}}
    assert fake.calls == []  # get_trace 不触达 Hermes


def test_agent_turn_ok():
    fake = FakeHermesClient({"runId": "r-1", "status": "ok"})
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            return await _post_json(client, {"action": "agent", "payload": {"message": "hi", "sessionKey": "s1", "lane": "L1"}})
    _, body = _run(sc())
    assert body["code"] == 0
    assert body["data"] == {"runId": "r-1", "status": "ok"}
    assert len(fake.calls) == 1
    assert fake.calls[0]["message"] == "hi"


def test_agent_turn_idempotency():
    fake = FakeHermesClient({"runId": "r-1", "status": "ok"})
    payload = {"message": "hi", "sessionKey": "s1", "idempotencyKey": "idem-1"}
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            _, b1 = await _post_json(client, {"action": "agent", "payload": payload})
            _, b2 = await _post_json(client, {"action": "agent", "payload": payload})
            return b1, b2
    b1, b2 = _run(sc())
    assert b1 == b2
    assert b1["data"]["runId"] == "r-1"
    assert len(fake.calls) == 1  # 幂等命中：第二次不再调 Hermes


def test_agent_turn_error_propagates():
    fake = FakeHermesClient({"runId": "r-1", "status": "error", "error": "boom"})
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            return await _post_json(client, {"action": "agent", "payload": {"message": "hi"}})
    _, body = _run(sc())
    assert body["data"]["status"] == "error"
    assert body["data"]["error"] == "boom"


def test_agent_handler_exception_returns_3000():
    fake = FakeHermesClient(RuntimeError("adapter blew up"))
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            return await _post_json(client, {"action": "agent", "payload": {"message": "hi"}})
    status, body = _run(sc())
    assert status == 500
    assert body["code"] == 3000


def test_health():
    fake = FakeHermesClient()
    async def sc():
        app = srv.create_app(auth_bearer="secret", hermes_client=fake)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            return resp.status, await resp.json()
    status, body = _run(sc())
    assert status == 200
    assert body == {"status": "ok"}
