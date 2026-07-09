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
    """给 preflight/retry 测试提供可控 mock。set(result) 决定下一次调用返回什么。

    ``call_count`` 记录 probe_omni 被实际调用次数,供冷却期测试断言"没真发 probe"。
    """
    state = {"result": {"ok": True, "code": "ok", "message": "连接正常"}, "n": 0}

    async def _fn(*a, **k):
        state["n"] += 1
        return state["result"]

    monkeypatch.setattr("miloco.admin.router._probe.probe_omni", _fn)

    class _H:
        def set(self, r):
            state["result"] = r

        @property
        def call_count(self):
            return state["n"]

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

    asyncio.run(_fill())
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

    asyncio.run(_fill())
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

    asyncio.run(_fill())

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

    asyncio.run(_fill())

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

    asyncio.run(_fill())
    r = client.post("/api/admin/omni-config/retry")
    assert r.status_code == 200
    # 无 key → cb 现在 code=no_key state=error
    health = r.json()["data"]["active"]["health"]
    assert health["code"] == "no_key"


def test_retry_cooldown_skips_second_probe(client, mock_probe):
    """连续两次 retry:第二次落在冷却期内,不真发 probe(mock 计数不变),返当前 snapshot。"""
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
        for _ in range(3):
            await get_omni_circuit_breaker().record_failure(
                ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
            )

    asyncio.run(_fill())
    n_before_put = mock_probe.call_count  # 前面 PUT 触发过一次 probe

    mock_probe.set({"ok": False, "code": "unreachable", "message": "仍连不上"})
    r1 = client.post("/api/admin/omni-config/retry")
    assert r1.status_code == 200
    n_after_first = mock_probe.call_count
    assert n_after_first == n_before_put + 1  # 第一次 retry 真发了 probe

    # 立即再点 → 冷却期内,skip probe
    r2 = client.post("/api/admin/omni-config/retry")
    assert r2.status_code == 200
    assert mock_probe.call_count == n_after_first  # 计数不变


def test_retry_half_open_short_circuits_no_new_probe(client, mock_probe):
    """HALF_OPEN 短路:tick 自愈已 arm 探测在飞时,用户点重试不并发第二次 probe。

    冷却期兜的是「上次完成」时间差,拦不住「探测中」;retry_now 对 HALF_OPEN 又是
    no-op。修复靠 CLOSED 判定后追加的 HALF_OPEN 短路,直接返当前 snapshot。
    """
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

    from miloco.perception.engine.omni.circuit_breaker import (
        CircuitState,
        get_omni_circuit_breaker,
    )
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    cb = get_omni_circuit_breaker()

    async def _to_half_open():
        for _ in range(3):
            await cb.record_failure(
                ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
            )
        # 模拟 tick 探测已 arm:置 in-flight 位 + 状态推到 HALF_OPEN
        cb._probe_in_flight = True  # noqa: SLF001
        await cb.mark_half_open()

    asyncio.run(_to_half_open())
    assert cb.state_for_test() == CircuitState.HALF_OPEN

    n_before = mock_probe.call_count
    r = client.post("/api/admin/omni-config/retry")
    assert r.status_code == 200
    # 关键断言:HALF_OPEN 短路 → 没跑新的 probe_omni
    assert mock_probe.call_count == n_before
    # 状态保持 HALF_OPEN,不被 no-op retry_now 意外改动
    assert cb.state_for_test() == CircuitState.HALF_OPEN


def test_snapshot_carries_relative_seconds_and_cooldown(client):
    """CB-N2/CB-N3:snapshot 附带 next_probe_in_seconds(相对秒数,不受时钟偏差影响)
    与 retry_cooldown_sec(前端冷却单源),前端直接消费。"""
    import asyncio

    from miloco.perception.engine.omni.circuit_breaker import (
        RETRY_COOLDOWN_SEC,
        get_omni_circuit_breaker,
    )
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    async def _to_recoverable():
        for _ in range(3):
            await get_omni_circuit_breaker().record_failure(
                ClassifiedError("unreachable", "m", ErrorCategory.RECOVERABLE)
            )

    asyncio.run(_to_recoverable())
    r = client.get("/api/admin/omni-config")
    assert r.status_code == 200
    health = r.json()["data"]["active"]["health"]
    # 常量单源:每次 snapshot 都带上,前端不再自己 hardcode
    assert health["retry_cooldown_sec"] == RETRY_COOLDOWN_SEC
    # OPEN_RECOVERABLE 下相对秒数非空且为非负数
    assert health["next_probe_in_seconds"] is not None
    assert health["next_probe_in_seconds"] >= 0
