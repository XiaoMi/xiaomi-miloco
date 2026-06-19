import json

import pytest
from aiohttp import web

from hermes import bridge, trace


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))
    trace._turns.clear()
    trace._trace_links.clear()
    yield
    trace._turns.clear()
    trace._trace_links.clear()


class FakeCtx:
    pass


class FakeRequest:
    def __init__(self, body=None, headers=None, *, bad_json=False):
        self._body = body
        self._bad_json = bad_json
        self.headers = headers or {}

    async def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._body if self._body is not None else {}


def test_ok_includes_data_when_provided():
    assert bridge._ok({"x": 1}) == {"code": 0, "message": "ok", "data": {"x": 1}}


def test_ok_omits_data_when_absent():
    assert bridge._ok() == {"code": 0, "message": "ok"}
    assert bridge._ok(None) == {"code": 0, "message": "ok"}


def test_fail_shape():
    assert bridge._fail(1001, "nope") == {"code": 1001, "message": "nope"}


async def test_bridge_unknown_action_returns_404_with_code_2001():
    handler = bridge._make_handler(FakeCtx(), auth_token="")
    resp = await handler(FakeRequest(body={"action": "bogus", "payload": {}}))
    assert resp.status == 404
    data = json.loads(resp.text)
    assert data["code"] == 2001


async def test_bridge_get_trace_unknown_returns_status_unknown():
    handler = bridge._make_handler(FakeCtx(), auth_token="")
    resp = await handler(
        FakeRequest(body={"action": "get_trace", "payload": {"runId": "missing-run"}})
    )
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["code"] == 0
    assert data["data"]["status"] == "unknown"


async def test_bridge_auth_failure_returns_401():
    handler = bridge._make_handler(FakeCtx(), auth_token="secret")
    resp = await handler(
        FakeRequest(body={"action": "get_trace", "payload": {}}, headers={})
    )
    assert resp.status == 401


async def test_bridge_missing_action_returns_400():
    handler = bridge._make_handler(FakeCtx(), auth_token="")
    resp = await handler(FakeRequest(body={"payload": {}}))
    assert resp.status == 400
    data = json.loads(resp.text)
    assert data["code"] == 1001


async def test_bridge_invalid_json_returns_400():
    handler = bridge._make_handler(FakeCtx(), auth_token="")
    resp = await handler(FakeRequest(bad_json=True))
    assert resp.status == 400


async def test_bridge_auth_passes_with_correct_token():
    handler = bridge._make_handler(FakeCtx(), auth_token="secret")
    resp = await handler(
        FakeRequest(
            body={"action": "get_trace", "payload": {"runId": "none"}},
            headers={"Authorization": "Bearer secret"},
        )
    )
    assert resp.status == 200


async def test_handle_agent_returns_runid_status_and_registers_trace(monkeypatch):
    class FakeAgent:
        def run_turn(self, message, run_id=None, extra_system_prompt=None):
            return {"status": "ok", "error": None}

    class FakePool:
        executor = None

        def get_or_create(self, **kwargs):
            return FakeAgent()

    monkeypatch.setattr(
        bridge.AgentSessionPool, "instance", staticmethod(lambda: FakePool())
    )

    payload = {
        "message": "hi",
        "sessionKey": "main",
        "traceId": "trace-X",
        "idempotencyKey": "run-1",
    }
    result = await bridge._handle_agent(FakeCtx(), payload)
    assert result["runId"] == "run-1"
    assert result["status"] == "ok"
    assert trace._trace_links.get("run-1") == "trace-X"


def test_register_bridge_starts_server_with_config(monkeypatch):
    captured = {}

    def fake_start(app, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        return object()

    monkeypatch.setattr(bridge, "_start_server_thread", fake_start)
    from hermes import config

    config.atomic_write_json(
        {"bridge_host": "0.0.0.0", "bridge_port": 19999, "bridge_auth_token": "tok"}
    )
    bridge.register_bridge(FakeCtx())
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 19999
    assert captured["app"] is not None


def test_start_server_thread_runs_serve_in_daemon(monkeypatch):
    started = {}

    async def fake_serve(app, host, port):
        started["called"] = (host, port)

    monkeypatch.setattr(bridge, "_serve", fake_serve)
    t = bridge._start_server_thread(web.Application(), "127.0.0.1", 18789)
    t.join(timeout=5)
    assert started["called"] == ("127.0.0.1", 18789)
    assert t.daemon is True
