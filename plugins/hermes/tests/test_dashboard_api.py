import pytest
from fastapi.testclient import TestClient

from hermes.dashboard.api import router
from hermes import trace
from hermes.agent_runner import AgentSessionPool


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))
    trace._turns.clear()
    trace._trace_links.clear()
    AgentSessionPool._instance = None
    yield
    trace._turns.clear()
    trace._trace_links.clear()
    inst = AgentSessionPool._instance
    if inst is not None:
        try:
            inst._executor.shutdown(wait=False)
        except Exception:
            pass
    AgentSessionPool._instance = None


@pytest.fixture
def client():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_webhook_missing_action_returns_400(client):
    resp = client.post("/webhook", json={"payload": {}})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


def test_webhook_unknown_action_returns_404(client):
    resp = client.post("/webhook", json={"action": "bogus", "payload": {}})
    assert resp.status_code == 404
    assert resp.json()["code"] == 2001


def test_webhook_get_trace_unknown_returns_unknown(client):
    resp = client.post(
        "/webhook",
        json={"action": "get_trace", "payload": {"runId": "missing"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "unknown"


def test_webhook_get_trace_missing_runid_returns_error(client):
    resp = client.post(
        "/webhook",
        json={"action": "get_trace", "payload": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "error"


def test_handle_agent_returns_runid_and_registers_trace(monkeypatch):
    from hermes.dashboard import api

    class FakeAgent:
        def run_turn(self, message, run_id=None, extra_system_prompt=None):
            return {"status": "ok", "error": None}

    class FakePool:
        executor = None

        def get_or_create(self, **kwargs):
            return FakeAgent()

    monkeypatch.setattr(
        api.AgentSessionPool, "instance", staticmethod(lambda: FakePool())
    )

    import asyncio

    result = asyncio.get_event_loop().run_until_complete(
        api._handle_agent(
            {
                "message": "hi",
                "sessionKey": "main",
                "traceId": "trace-X",
                "idempotencyKey": "run-1",
            }
        )
    )
    assert result["runId"] == "run-1"
    assert result["status"] == "ok"
    assert trace._trace_links.get("run-1") == "trace-X"
