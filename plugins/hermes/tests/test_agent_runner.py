import sys
import types

import pytest

from hermes import agent_runner


@pytest.fixture(autouse=True)
def _reset_singleton():
    agent_runner.AgentSessionPool._instance = None
    yield
    inst = agent_runner.AgentSessionPool._instance
    if inst is not None:
        try:
            inst._executor.shutdown(wait=False)
        except Exception:
            pass
    agent_runner.AgentSessionPool._instance = None


def test_pool_is_singleton():
    a = agent_runner.AgentSessionPool.instance()
    b = agent_runner.AgentSessionPool.instance()
    assert a is b


def test_pool_delete_missing_returns_false():
    pool = agent_runner.AgentSessionPool.instance()
    assert pool.delete("does-not-exist") is False


def test_pool_caches_agent(monkeypatch):
    created = []

    class FakeAgent:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def close(self):
            pass

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent

    class FakeSessionDB:
        def create_session(self, **kwargs):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = FakeSessionDB

    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(
        agent_runner,
        "_resolve_runtime",
        lambda: {"model": "test-model", "api_key": "k", "base_url": "http://x", "provider": "openai"},
    )

    pool = agent_runner.AgentSessionPool.instance()
    first = pool.get_or_create(session_key="sess-1")
    second = pool.get_or_create(session_key="sess-1")
    assert first is second
    assert len(created) == 1
    assert created[0]["model"] == "test-model"


def test_pool_delete_existing_closes_and_returns_true(monkeypatch):
    closed = []

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def close(self):
            closed.append(True)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent

    class FakeSessionDB:
        def create_session(self, **kwargs):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = FakeSessionDB

    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setitem(
        sys.modules,
        "agent",
        types.ModuleType("agent"),
    )
    aux = types.ModuleType("agent.auxiliary_client")
    aux.cleanup_stale_async_clients = lambda: None
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", aux)
    monkeypatch.setattr(
        agent_runner,
        "_resolve_runtime",
        lambda: {"model": "test-model", "api_key": "k", "base_url": "http://x", "provider": "openai"},
    )

    pool = agent_runner.AgentSessionPool.instance()
    pool.get_or_create(session_key="sess-2")
    assert pool.delete("sess-2") is True
    assert closed == [True]
    assert "sess-2" not in pool._agents
