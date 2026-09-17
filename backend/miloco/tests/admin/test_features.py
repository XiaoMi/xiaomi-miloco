"""GET/POST /api/admin/features — 实验性功能开关端到端测试。

隔离 $MILOCO_HOME；删 MILOCO_FEATURES__* 环境变量（env 优先级高会盖过 config.json）。
"""

from types import SimpleNamespace

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
    # 拨动 pet_recognition 会触发家庭档案重渲；测试里桩掉 commit，避免碰真实 home。
    monkeypatch.setattr(
        "miloco.admin.router.get_manager",
        lambda: SimpleNamespace(home_profile_service=SimpleNamespace(commit=lambda: {})),
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    yield TestClient(app)
    reset_settings()


def test_features_default_off(client):
    # 四个开关的**出厂默认全关**（settings.yaml::features）：母开关 pet_recognition 关闭时
    # 前端隐藏入口、后端不注入宠物命名规则；三个子开关是内部调优项，默认同样关，
    # 只在住户自己打开 pet_recognition 后才谈得上生效。
    d = client.get("/api/admin/features").json()["data"]
    assert d == {
        "pet_recognition": False,
        "pet_head_grounding": False,
        "pet_body_grounding": False,
        "pet_reid_diverse": False,
    }


def test_features_toggle_on_persists(client):
    out = client.post(
        "/api/admin/features", json={"pet_recognition": True}
    ).json()["data"]
    assert out["pet_recognition"] is True
    # 开母开关**不会**顺带把子开关拨开：子开关各有自己的默认（关），要开得显式开。
    assert out["pet_head_grounding"] is False
    assert out["pet_body_grounding"] is False
    assert out["pet_reid_diverse"] is False
    # 写进 config.json，再 GET 仍为 True
    assert client.get("/api/admin/features").json()["data"]["pet_recognition"] is True


def test_features_partial_update_keeps_others(client):
    """局部更新只动传进来的键，其余保持**各自当前值**（这里是出厂默认关）。"""
    client.post("/api/admin/features", json={"pet_recognition": True})
    out = client.post(
        "/api/admin/features", json={"pet_head_grounding": True}
    ).json()["data"]
    assert out == {
        "pet_recognition": True,
        "pet_head_grounding": True,   # 本请求显式打开
        "pet_body_grounding": False,  # 未被顺带改动，保持默认关
        "pet_reid_diverse": False,
    }


def test_features_soft_close_toggle_off(client):
    client.post("/api/admin/features", json={"pet_recognition": True})
    out = client.post(
        "/api/admin/features", json={"pet_recognition": False}
    ).json()["data"]
    assert out["pet_recognition"] is False


def test_features_reid_diverse_via_admin(client):
    # pet_reid_diverse 也经 admin 端点暴露、可改——与 CLI / backend 口径一致。
    # 默认关，所以这里验「打开 + 持久化」，再验「关回去」。
    assert client.get("/api/admin/features").json()["data"]["pet_reid_diverse"] is False
    out = client.post(
        "/api/admin/features", json={"pet_reid_diverse": True}
    ).json()["data"]
    assert out["pet_reid_diverse"] is True
    assert client.get("/api/admin/features").json()["data"]["pet_reid_diverse"] is True
    out = client.post(
        "/api/admin/features", json={"pet_reid_diverse": False}
    ).json()["data"]
    assert out["pet_reid_diverse"] is False
