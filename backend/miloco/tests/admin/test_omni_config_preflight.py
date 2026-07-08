"""admin router preflight + retry 端点 + health 字段测试。

复用 test_omni_config.py 的 fixture 结构:_default_probe_success (autouse ok) +
_reset_omni_circuit_breaker (autouse) + client (临时 config.json)。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router


# ─── 复用 test_omni_config.py 的 fixture 集合(简化版) ────────────────────────


@pytest.fixture(autouse=True)
def _reset_cb():
    from miloco.perception.engine.omni.circuit_breaker import (
        reset_omni_circuit_breaker_for_tests,
    )

    reset_omni_circuit_breaker_for_tests()
    yield
    reset_omni_circuit_breaker_for_tests()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    monkeypatch.delenv("MILOCO_MODEL__OMNI__MODEL", raising=False)
    monkeypatch.delenv("MILOCO_MODEL__OMNI__BASE_URL", raising=False)
    import json as _json

    (tmp_path / "config.json").write_text(
        _json.dumps(
            {
                "model": {
                    "omni": {
                        "label": "",
                        "model": "xiaomi/mimo-v2.5",
                        "base_url": "https://api.xiaomimimo.com/v1",
                        "api_key": "",
                    },
                    "omni_profiles": [],
                }
            }
        ),
        encoding="utf-8",
    )
    reset_settings()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    yield TestClient(app)
    reset_settings()


@pytest.fixture
def mock_probe(monkeypatch):
    """给 preflight/retry 测试提供可控 mock。set(result) 决定下一次调用返回什么。"""
    state = {"result": {"ok": True, "code": "ok", "message": "连接正常"}}

    async def _fn(*a, **k):
        return state["result"]

    monkeypatch.setattr("miloco.admin.router._probe_omni", _fn)

    class _H:
        def set(self, r):
            state["result"] = r

    return _H()


# ─── PUT 加 preflight ───────────────────────────────────────────────────────


def test_put_activate_true_success_requires_probe_ok(client, mock_probe):
    mock_probe.set({"ok": True, "code": "ok", "message": "连接正常"})
    r = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-xxxxxx",
            "activate": True,
        },
    )
    assert r.status_code == 200


def test_put_activate_true_rejects_when_probe_fails(client, mock_probe):
    mock_probe.set({"ok": False, "code": "bad_key", "message": "API Key 无效"})
    r = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-bad",
            "activate": True,
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_key"


def test_put_activate_true_rejects_unreachable(client, mock_probe):
    mock_probe.set({"ok": False, "code": "unreachable", "message": "无法连接"})
    r = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://nope/v1",
            "api_key": "sk-x",
            "activate": True,
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "unreachable"


def test_put_activate_true_no_key_400(client, mock_probe):
    """无 key 时不跑 preflight,直接 400 no_key。"""
    r = client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "activate": True,
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "no_key"


def test_put_activate_false_skips_preflight(client, mock_probe):
    """activate=False 只入列表,跳过 preflight,不受 probe 结果影响。"""
    mock_probe.set({"ok": False, "code": "unreachable"})
    r = client.put(
        "/api/admin/omni-config",
        json={
            "label": "备用",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-x",
            "activate": False,
        },
    )
    assert r.status_code == 200
    data = r.json()["data"]
    labels = [p["label"] for p in data["profiles"]]
    assert "备用" in labels


# ─── activate 加 preflight ──────────────────────────────────────────────────


def test_activate_success_when_probe_ok(client, mock_probe):
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-xxxxxx",
            "activate": False,
        },
    )
    mock_probe.set({"ok": True, "code": "ok"})
    r = client.post("/api/admin/omni-config/activate", json={"label": "甲"})
    assert r.status_code == 200


def test_activate_rejected_when_probe_fails(client, mock_probe):
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-xxxxxx",
            "activate": False,
        },
    )
    mock_probe.set({"ok": False, "code": "bad_key", "message": "API Key 无效"})
    r = client.post("/api/admin/omni-config/activate", json={"label": "甲"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_key"


# ─── GET 包含 health 字段 ───────────────────────────────────────────────────


def test_get_includes_health(client, mock_probe):
    data = client.get("/api/admin/omni-config").json()["data"]
    assert "health" in data["active"]
    assert data["active"]["health"]["state"] in ("ok", "warn", "error")


def test_get_health_reflects_open_config(client, mock_probe):
    """熔断到 OPEN_CONFIG 后 GET 里 health.state == error。"""
    import asyncio
    from miloco.perception.engine.omni.circuit_breaker import get_omni_circuit_breaker
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    async def _fill():
        await get_omni_circuit_breaker().record_failure(
            ClassifiedError("bad_key", "无效", ErrorCategory.CONFIG)
        )

    asyncio.get_event_loop().run_until_complete(_fill())
    data = client.get("/api/admin/omni-config").json()["data"]
    assert data["active"]["health"]["state"] == "error"
    assert data["active"]["health"]["code"] == "bad_key"


# ─── retry 端点 ─────────────────────────────────────────────────────────────


def test_retry_when_closed_is_noop(client, mock_probe):
    r = client.post("/api/admin/omni-config/retry")
    assert r.status_code == 200
    assert r.json()["data"]["active"]["health"]["state"] == "ok"


def test_retry_open_recoverable_probes_and_recovers(client, mock_probe):
    """OPEN_RECOVERABLE 状态下 retry 跑 probe,成功 → CLOSED。"""
    # 先塞点 key 让 probe 有 arg,再让 cb 进 warn
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-xxxxxx",
            "activate": True,
        },
    )
    import asyncio
    from miloco.perception.engine.omni.circuit_breaker import get_omni_circuit_breaker
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    cb = get_omni_circuit_breaker()

    async def _fill():
        for _ in range(3):
            await cb.record_failure(
                ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
            )

    asyncio.get_event_loop().run_until_complete(_fill())
    assert cb.snapshot().state == "warn"

    mock_probe.set({"ok": True, "code": "ok"})
    r = client.post("/api/admin/omni-config/retry")
    assert r.status_code == 200
    assert r.json()["data"]["active"]["health"]["state"] == "ok"


def test_retry_open_recoverable_probe_still_fails_stays_warn(client, mock_probe):
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-xxxxxx",
            "activate": True,
        },
    )
    import asyncio
    from miloco.perception.engine.omni.circuit_breaker import get_omni_circuit_breaker
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    cb = get_omni_circuit_breaker()

    async def _fill():
        for _ in range(3):
            await cb.record_failure(
                ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
            )

    asyncio.get_event_loop().run_until_complete(_fill())

    mock_probe.set({"ok": False, "code": "unreachable", "message": "仍连不上"})
    r = client.post("/api/admin/omni-config/retry")
    assert r.status_code == 200
    health = r.json()["data"]["active"]["health"]
    assert health["state"] == "warn" and health["last_probe_result"] == "fail"


def test_retry_open_config_bad_key_stays_error(client, mock_probe):
    """OPEN_CONFIG 下 retry 仍失败(bad_key) → 保持 error 态。"""
    client.put(
        "/api/admin/omni-config",
        json={
            "label": "甲",
            "model": "m1",
            "base_url": "https://x/v1",
            "api_key": "sk-xxxxxx",
            "activate": True,
        },
    )
    import asyncio
    from miloco.perception.engine.omni.circuit_breaker import get_omni_circuit_breaker
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    async def _fill():
        await get_omni_circuit_breaker().record_failure(
            ClassifiedError("bad_key", "旧无效", ErrorCategory.CONFIG)
        )

    asyncio.get_event_loop().run_until_complete(_fill())

    mock_probe.set({"ok": False, "code": "bad_key", "message": "仍无效"})
    r = client.post("/api/admin/omni-config/retry")
    assert r.status_code == 200
    health = r.json()["data"]["active"]["health"]
    assert health["state"] == "error" and health["code"] == "bad_key"


def test_retry_no_key_returns_no_key(client, mock_probe):
    """没配 key 时 retry 直接标记 no_key 错误。"""
    # 让 cb 先进入 warn(否则 retry 会 noop)
    import asyncio
    from miloco.perception.engine.omni.circuit_breaker import get_omni_circuit_breaker
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    async def _fill():
        for _ in range(3):
            await get_omni_circuit_breaker().record_failure(
                ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
            )

    asyncio.get_event_loop().run_until_complete(_fill())
    r = client.post("/api/admin/omni-config/retry")
    assert r.status_code == 200
    # 无 key → cb 现在 code=no_key state=error
    health = r.json()["data"]["active"]["health"]
    assert health["code"] == "no_key"
