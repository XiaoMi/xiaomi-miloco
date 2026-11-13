from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def endpoint(monkeypatch):
    import miloco.manager as manager_module

    manager = SimpleNamespace(
        observation_service=None, observation_family_id=lambda: "home"
    )
    monkeypatch.setattr(manager_module, "get_manager", lambda: manager)
    from miloco.iot_observation import router as module

    monkeypatch.setattr(module, "manager", manager)
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    app.dependency_overrides[module.verify_token] = lambda: None
    with TestClient(app) as client:
        yield client, manager


def test_retired_endpoint_preserves_run_id(endpoint):
    client, _ = endpoint
    result = client.post("/api/iot-observation/run", json={"run_id": "patrol-run"})
    assert result.status_code == 200
    assert result.json()["data"] == {
        "run_id": "patrol-run",
        "status": "retired",
        "reason": "use_home_observe",
    }


def test_endpoint_forwards_batch_without_maturity(endpoint):
    client, manager = endpoint
    manager.observation_service = SimpleNamespace(
        read_batch=Mock(return_value={"status": "empty"})
    )
    result = client.post("/api/iot-observation/batch")
    manager.observation_service.read_batch.assert_called_once_with()
    assert result.json()["data"] == {"status": "empty"}


def test_endpoint_rejects_log_injection_in_run_id(endpoint):
    client, _ = endpoint
    assert (
        client.post(
            "/api/iot-observation/run", json={"run_id": "fake\nstatus=ok"}
        ).status_code
        == 422
    )


def test_disabled_batch_and_submission_validation(endpoint):
    client, _ = endpoint
    assert (
        client.post("/api/iot-observation/batch").json()["data"]["status"] == "disabled"
    )
    assert (
        client.post(
            "/api/iot-observation/submit",
            json={"batch_id": "batch", "observations": [], "confidence": 1},
        ).status_code
        == 422
    )


def test_conflict_and_failure_do_not_expose_private_details(endpoint):
    client, manager = endpoint
    manager.observation_service = SimpleNamespace(
        submit_batch=Mock(side_effect=ValueError("invalid evidence"))
    )
    payload = {"batch_id": "batch", "observations": []}
    assert client.post("/api/iot-observation/submit", json=payload).status_code == 409
    manager.observation_service.submit_batch.side_effect = RuntimeError("private data")
    response = client.post("/api/iot-observation/submit", json=payload)
    assert response.status_code == 503
    assert "private data" not in response.text


def test_sidecar_endpoints_require_auth(endpoint, monkeypatch):
    from miloco.middleware import auth_middleware
    from miloco.middleware.exception_handler import handle_exception
    from miloco.middleware.exceptions import BaseAPIException

    client, _ = endpoint
    monkeypatch.setattr(
        auth_middleware,
        "get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(token="test-only-token")),
    )
    client.app.add_exception_handler(BaseAPIException, handle_exception)
    client.app.middleware_stack = None
    client.app.dependency_overrides.clear()
    assert client.post("/api/iot-observation/batch").status_code in (401, 403)
    assert (
        client.post(
            "/api/iot-observation/submit", json={"batch_id": "b", "observations": []}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/iot-observation/batch",
            headers={"Authorization": "Bearer test-only-token"},
        ).status_code
        == 200
    )
