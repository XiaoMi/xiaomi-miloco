import json

import pytest

from hermes import bridge, trace


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))
    trace._turns.clear()
    trace._trace_links.clear()
    bridge._BRIDGE_STARTED = False
    yield
    trace._turns.clear()
    trace._trace_links.clear()


class FakeCtx:
    pass


class FakeRequest:
    def __init__(self, body=None, headers=None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body if self._body is not None else {}


def test_ok_includes_data_when_provided():
    assert bridge._ok({"x": 1}) == {"code": 0, "message": "ok", "data": {"x": 1}}


def test_ok_omits_data_when_absent():
    assert bridge._ok() == {"code": 0, "message": "ok"}


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
    result = await bridge._handle_agent(payload)
    assert result["runId"] == "run-1"
    assert result["status"] == "ok"
    assert trace._trace_links.get("run-1") == "trace-X"


def test_register_bridge_sets_flag():
    assert bridge._BRIDGE_STARTED is False
    bridge.register_bridge(FakeCtx())
    assert bridge._BRIDGE_STARTED is True


def test_register_bridge_idempotent(monkeypatch):
    monkeypatch.setattr(bridge, "_serve", lambda *a: None)
    bridge.register_bridge(FakeCtx())
    bridge.register_bridge(FakeCtx())
    assert bridge._BRIDGE_STARTED is True
