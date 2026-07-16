"""GET/POST /api/admin/features — 实验性功能开关端到端测试。

隔离 $MILOCO_HOME；删 MILOCO_FEATURES__* 环境变量（env 优先级高会盖过 config.json）。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_FEATURES__PET_RECOGNITION", raising=False)
    monkeypatch.delenv("MILOCO_FEATURES__PET_HEAD_GROUNDING", raising=False)
    reset_settings()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    yield TestClient(app)
    reset_settings()


def test_features_default_off(client):
    # pet_recognition 默认关（功能需住户显式开）；pet_head_grounding 默认开（头部定位子能力）。
    d = client.get("/api/admin/features").json()["data"]
    assert d == {
        "pet_recognition": False,
        "pet_head_grounding": True,
        "pet_body_grounding": True,
    }


def test_features_toggle_on_persists(client):
    out = client.post(
        "/api/admin/features", json={"pet_recognition": True}
    ).json()["data"]
    assert out["pet_recognition"] is True
    assert out["pet_head_grounding"] is True  # 保持默认开
    # 写进 config.json，再 GET 仍为 True
    assert client.get("/api/admin/features").json()["data"]["pet_recognition"] is True


def test_features_partial_update_keeps_others(client):
    client.post("/api/admin/features", json={"pet_recognition": True})
    out = client.post(
        "/api/admin/features", json={"pet_head_grounding": True}
    ).json()["data"]
    assert out == {
        "pet_recognition": True,
        "pet_head_grounding": True,
        "pet_body_grounding": True,
    }


def test_features_soft_close_toggle_off(client):
    client.post("/api/admin/features", json={"pet_recognition": True})
    out = client.post(
        "/api/admin/features", json={"pet_recognition": False}
    ).json()["data"]
    assert out["pet_recognition"] is False
