"""GET/PUT /api/admin/scheduler-config 的端到端测试。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
    # 清空可能覆盖 scheduler.enabled 的环境变量
    monkeypatch.delenv("MILOCO_SCHEDULER__ENABLED", raising=False)
    reset_settings()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    yield TestClient(app)
    reset_settings()


def test_get_scheduler_config_default_enabled(client):
    """缺省 = 自动管理开启。"""
    resp = client.get("/api/admin/scheduler-config")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"enabled": True}


def test_put_scheduler_config_disables_and_persists(client, tmp_path):
    resp = client.put("/api/admin/scheduler-config", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["data"] == {"enabled": False}

    # 写盘到 config.json 的 scheduler.enabled
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["scheduler"]["enabled"] is False

    # 再次 GET 读到持久化后的值
    assert client.get("/api/admin/scheduler-config").json()["data"] == {
        "enabled": False
    }


def test_put_scheduler_config_reenable(client):
    client.put("/api/admin/scheduler-config", json={"enabled": False})
    resp = client.put("/api/admin/scheduler-config", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["data"] == {"enabled": True}


def test_put_scheduler_config_invalid_body_422(client):
    resp = client.put("/api/admin/scheduler-config", json={"enabled": "yes"})
    assert resp.status_code == 422
