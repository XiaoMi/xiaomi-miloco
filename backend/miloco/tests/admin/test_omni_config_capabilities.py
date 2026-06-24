from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router


def _client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    monkeypatch.delenv("MILOCO_MODEL__OMNI__MODEL", raising=False)
    monkeypatch.delenv("MILOCO_MODEL__OMNI__BASE_URL", raising=False)
    (tmp_path / "config.json").write_text(
        """{
          "model": {
            "omni": {
              "label": "legacy",
              "model": "legacy-model",
              "base_url": "https://legacy/v1",
              "api_key": "sk-legacy1234"
            },
            "omni_profiles": []
          }
        }""",
        encoding="utf-8",
    )
    reset_settings()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    return client, reset_settings


def test_get_backfills_legacy_active_as_enabled_all_capabilities(tmp_path, monkeypatch):
    client, reset_settings = _client(tmp_path, monkeypatch)
    try:
        data = client.get("/api/admin/omni-config").json()["data"]
    finally:
        reset_settings()

    assert data["active"]["enabled"] is True
    assert data["active"]["capabilities"] == ["text", "image", "video", "audio"]


def test_put_persists_enabled_and_capabilities(tmp_path, monkeypatch):
    client, reset_settings = _client(tmp_path, monkeypatch)
    try:
        data = client.put(
            "/api/admin/omni-config",
            json={
                "label": "audio-only",
                "model": "audio-model",
                "base_url": "https://audio/v1",
                "api_key": "sk-audio1234",
                "enabled": True,
                "capabilities": ["audio", "text"],
            },
        ).json()["data"]
    finally:
        reset_settings()

    profile = data["profiles"][0]
    assert profile["enabled"] is True
    assert profile["capabilities"] == ["text", "audio"]
    assert data["active"]["label"] == "audio-only"
    assert data["active"]["enabled"] is True
    assert data["active"]["capabilities"] == ["text", "audio"]


def test_put_can_disable_profile_and_keep_subset_capabilities(tmp_path, monkeypatch):
    client, reset_settings = _client(tmp_path, monkeypatch)
    try:
        client.put(
            "/api/admin/omni-config",
            json={
                "label": "vision",
                "model": "vision-model",
                "base_url": "https://vision/v1",
                "api_key": "sk-vision1234",
                "enabled": True,
                "capabilities": ["image"],
            },
        )
        data = client.put(
            "/api/admin/omni-config",
            json={
                "label": "vision",
                "model": "vision-model",
                "base_url": "https://vision/v1",
                "original_label": "vision",
                "activate": False,
                "enabled": False,
                "capabilities": ["image"],
            },
        ).json()["data"]
    finally:
        reset_settings()

    profile = data["profiles"][0]
    assert profile["enabled"] is False
    assert profile["capabilities"] == ["image"]
