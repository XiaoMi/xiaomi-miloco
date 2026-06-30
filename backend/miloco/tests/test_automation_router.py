from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def _build_app():
    fake_manager = ModuleType("miloco.manager")
    fake_manager.get_manager = lambda: None  # type: ignore[attr-defined]
    old_manager = sys.modules.get("miloco.manager")
    old_router = sys.modules.pop("miloco.automation.router", None)
    try:
        sys.modules["miloco.manager"] = fake_manager
        automation_router = importlib.import_module("miloco.automation.router").router
    finally:
        if old_router is not None:
            sys.modules["miloco.automation.router"] = old_router
        else:
            sys.modules.pop("miloco.automation.router", None)
        if old_manager is not None:
            sys.modules["miloco.manager"] = old_manager
        else:
            sys.modules.pop("miloco.manager", None)

    from miloco.middleware.exception_handler import handle_exception

    app = FastAPI()

    @app.middleware("http")
    async def _catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            return handle_exception(request, exc)

    app.include_router(automation_router, prefix="/api")
    return app


def test_automation_snapshot_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
    from miloco.config import reset_settings

    reset_settings()
    snap_dir = tmp_path / "static" / "clips" / "automation"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "shot.jpg").write_bytes(b"jpeg")

    client = TestClient(_build_app())
    resp = client.get("/api/automation/snapshots/shot.jpg")
    assert resp.status_code == 401

    reset_settings()


def test_automation_snapshot_accepts_query_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
    from miloco.config import reset_settings

    reset_settings()
    snap_dir = tmp_path / "static" / "clips" / "automation"
    snap_dir.mkdir(parents=True, exist_ok=True)
    expected = b"jpeg-data"
    (snap_dir / "ok.jpg").write_bytes(expected)

    client = TestClient(_build_app())
    resp = client.get("/api/automation/snapshots/ok.jpg?token=secret-123")
    assert resp.status_code == 200
    assert resp.content == expected

    reset_settings()


def test_automation_snapshot_rejects_invalid_filename_with_http_400(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
    from miloco.config import reset_settings

    reset_settings()

    client = TestClient(_build_app())
    resp = client.get("/api/automation/snapshots/not-a-jpeg.png?token=secret-123")
    assert resp.status_code == 400
    assert resp.json()["code"] == 400

    reset_settings()


def test_automation_snapshot_missing_file_uses_http_404(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
    from miloco.config import reset_settings

    reset_settings()
    snap_dir = Path(tmp_path) / "static" / "clips" / "automation"
    snap_dir.mkdir(parents=True, exist_ok=True)

    client = TestClient(_build_app())
    resp = client.get("/api/automation/snapshots/missing.jpg?token=secret-123")
    assert resp.status_code == 404
    assert resp.json()["code"] == 404

    reset_settings()
    snap_dir = Path(tmp_path) / "static" / "clips" / "automation"
    snap_dir.mkdir(parents=True, exist_ok=True)
    expected = b"jpeg-bits"
    (snap_dir / "shot.jpg").write_bytes(expected)

    client = TestClient(_build_app())
    resp = client.get("/api/automation/snapshots/shot.jpg?token=secret-123")
    assert resp.status_code == 200
    assert resp.content == expected

    reset_settings()
