"""reveal-dir 端点安全校验三态测试."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_and_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "test.db"))

    from miloco.config import reset_settings
    reset_settings()

    from miloco.admin.router import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app, TestClient(app)


def test_inside_packs_200(app_and_client, tmp_path):
    _, client = app_and_client
    target = tmp_path / "packs" / "20260702-100000-abc123"
    target.mkdir(parents=True)
    with patch("subprocess.run"):
        resp = client.post("/api/admin/reveal-dir", json={"path": str(target)})
    assert resp.status_code == 200


def test_outside_packs_403(app_and_client, tmp_path):
    _, client = app_and_client
    (tmp_path / "packs").mkdir(parents=True)
    outside = tmp_path / "etc"
    outside.mkdir()
    resp = client.post("/api/admin/reveal-dir", json={"path": str(outside)})
    assert resp.status_code == 403


def test_missing_dir_404(app_and_client, tmp_path):
    _, client = app_and_client
    (tmp_path / "packs").mkdir(parents=True)
    resp = client.post("/api/admin/reveal-dir", json={"path": str(tmp_path / "packs" / "nope")})
    assert resp.status_code == 404


def test_traversal_attempt_403(app_and_client, tmp_path):
    _, client = app_and_client
    (tmp_path / "packs").mkdir(parents=True)
    evil = tmp_path / "packs_evil"
    evil.mkdir()
    resp = client.post("/api/admin/reveal-dir", json={"path": str(evil)})
    assert resp.status_code == 403
