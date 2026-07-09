# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""/api/crons REST 端点集成测试.

scheduler in-memory 侧用 stub 替换 (真跑 APScheduler 会引入异步 event loop
和 wall clock 依赖, 与 REST 语义无关)。runner 的 _fire / rebuild 走独立单测。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


class _StubRunner:
    """替代 ScheduleRunner: 记录调用不实际启动 APScheduler."""

    def __init__(self):
        self.apply_calls: list = []
        self.remove_calls: list = []

    def apply_enabled_state(self, cron):
        self.apply_calls.append(cron.cron_id)

    def remove_job(self, cron_id):
        self.remove_calls.append(cron_id)


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_file = tmp_path / "cron.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task (task_id, description, created_at) "
            "VALUES ('t1', 'x', 0)"
        )
        conn.commit()

    import miloco.schedule.router as sched_router

    stub = _StubRunner()
    monkeypatch.setattr(sched_router, "get_runner", lambda: stub)

    from miloco.middleware.exception_handler import handle_exception

    app = FastAPI()

    @app.middleware("http")
    async def _catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            return handle_exception(request, exc)

    app.include_router(sched_router.router, prefix="/api")

    yield app, stub

    connector_module.db_connector = None
    reset_settings()


@pytest.fixture
def client(app_db):
    app, _ = app_db
    return TestClient(app)


# ── POST /crons 校验 ─────────────────────────────────────────────────────


def test_post_kind_cron_ok(client, app_db):
    _, stub = app_db
    r = client.post(
        "/api/crons",
        json={
            "name": "水提醒",
            "kind": "cron",
            "task_id": "t1",
            "cron_expr": "0 9 * * *",
            "tz": "Asia/Shanghai",
            "message": "喝水啦",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0
    cron_id = body["data"]["cron_id"]
    assert cron_id
    assert stub.apply_calls == [cron_id]


def test_post_kind_at_ok(client):
    from datetime import datetime, timedelta, timezone

    at_iso = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    r = client.post(
        "/api/crons",
        json={
            "name": "闹钟",
            "kind": "at",
            "task_id": "t1",
            "at_iso": at_iso,
            "message": "起床",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["code"] == 0


def test_post_kind_every_ok(client):
    r = client.post(
        "/api/crons",
        json={
            "name": "每小时",
            "kind": "every",
            "task_id": "t1",
            "every_ms": 3600_000,
            "message": "hourly",
        },
    )
    assert r.status_code == 200, r.text


def test_post_kind_every_rejects_below_60s(client):
    r = client.post(
        "/api/crons",
        json={
            "name": "bad",
            "kind": "every",
            "task_id": "t1",
            "every_ms": 30_000,
            "message": "x",
        },
    )
    assert r.status_code == 422


def test_post_cron_missing_expr_rejected(client):
    r = client.post(
        "/api/crons",
        json={"name": "bad", "kind": "cron", "task_id": "t1", "message": "x"},
    )
    assert r.status_code == 422


def test_post_kind_at_rejects_past_at_iso(client):
    from datetime import datetime, timedelta, timezone

    past_iso = (datetime.now(tz=timezone.utc) - timedelta(minutes=10)).isoformat(
        timespec="seconds"
    )
    r = client.post(
        "/api/crons",
        json={
            "name": "过去",
            "kind": "at",
            "task_id": "t1",
            "at_iso": past_iso,
            "message": "已过期",
        },
    )
    assert r.status_code == 422, r.text


def test_post_at_missing_at_iso_rejected(client):
    r = client.post(
        "/api/crons",
        json={"name": "bad", "kind": "at", "task_id": "t1", "message": "x"},
    )
    assert r.status_code == 422


def test_post_at_iso_naive_rejected(client):
    """裸 ISO(不含 offset) 拒收: agent 心智里 time-compute 出的一律带 offset,
    naive 说明来源不明, 直接 422 让上游查。"""
    r = client.post(
        "/api/crons",
        json={
            "name": "bad",
            "kind": "at",
            "task_id": "t1",
            "at_iso": "2026-06-11T09:00:00",
            "message": "x",
        },
    )
    assert r.status_code == 422, r.text


def test_post_at_iso_malformed_rejected(client):
    r = client.post(
        "/api/crons",
        json={
            "name": "bad",
            "kind": "at",
            "task_id": "t1",
            "at_iso": "not-a-date",
            "message": "x",
        },
    )
    assert r.status_code == 422, r.text


def test_post_task_id_not_found_returns_404(client):
    r = client.post(
        "/api/crons",
        json={
            "name": "任务不存在",
            "kind": "cron",
            "task_id": "ghost",
            "cron_expr": "* * * * *",
            "message": "x",
        },
    )
    # ResourceNotFoundException code=2001
    assert r.status_code == 200
    assert r.json()["code"] == 2001


def test_post_invalid_tz_rejected(client):
    r = client.post(
        "/api/crons",
        json={
            "name": "bad",
            "kind": "cron",
            "task_id": "t1",
            "cron_expr": "* * * * *",
            "tz": "GMT+8",
            "message": "x",
        },
    )
    # ValidationException 被 middleware catch → 非 0 状态 (200+code!=0 或 4xx/5xx)
    assert r.status_code >= 400 or r.json().get("code") != 0


def test_post_bad_cron_expr_rejected(client):
    r = client.post(
        "/api/crons",
        json={
            "name": "bad",
            "kind": "cron",
            "task_id": "t1",
            "cron_expr": "not a cron",
            "message": "x",
        },
    )
    assert r.status_code >= 400 or r.json().get("code") != 0


def test_post_max_delay_zero_only_for_at(client):
    r = client.post(
        "/api/crons",
        json={
            "name": "bad",
            "kind": "cron",
            "task_id": "t1",
            "cron_expr": "* * * * *",
            "message": "x",
            "max_delay_seconds": 0,
        },
    )
    assert r.status_code == 422


# ── kill switch ─────────────────────────────────────────────────────────


def test_post_returns_503_when_kill_switch_active(client, monkeypatch):
    import miloco.schedule.router as sched_router

    monkeypatch.setattr(sched_router, "_schedule_enabled", lambda: False)
    r = client.post(
        "/api/crons",
        json={
            "name": "n",
            "kind": "cron",
            "task_id": "t1",
            "cron_expr": "* * * * *",
            "message": "x",
        },
    )
    assert r.status_code == 503


# ── GET /crons ──────────────────────────────────────────────────────────


def test_get_lists_and_filters(client, app_db):
    _, stub = app_db
    # 建 1 个 internal + 1 个 external (external 走 raw INSERT)
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO cron (cron_id, task_id, dispatch_owner, enabled, "
            "created_at, updated_at) VALUES ('ext-1', 't1', 'external', 1, 0, 0)"
        )
        conn.commit()

    r = client.post(
        "/api/crons",
        json={
            "name": "n",
            "kind": "cron",
            "task_id": "t1",
            "cron_expr": "* * * * *",
            "message": "x",
        },
    )
    internal_id = r.json()["data"]["cron_id"]

    all_rows = client.get("/api/crons").json()["data"]
    assert {r["cron_id"] for r in all_rows} == {"ext-1", internal_id}

    ext_only = client.get("/api/crons?dispatch_owner=external").json()["data"]
    assert [r["cron_id"] for r in ext_only] == ["ext-1"]

    by_task = client.get("/api/crons?task_id=t1").json()["data"]
    assert len(by_task) == 2


def test_get_single_and_404(client):
    r = client.post(
        "/api/crons",
        json={
            "name": "n",
            "kind": "cron",
            "task_id": "t1",
            "cron_expr": "* * * * *",
            "message": "x",
        },
    )
    cid = r.json()["data"]["cron_id"]

    got = client.get(f"/api/crons/{cid}").json()["data"]
    assert got["cron_id"] == cid
    assert got["dispatch_owner"] == "internal"

    r = client.get("/api/crons/ghost")
    assert r.json()["code"] == 2001


# ── DELETE 分派 ─────────────────────────────────────────────────────────


def test_delete_internal_calls_scheduler(client, app_db):
    _, stub = app_db
    r = client.post(
        "/api/crons",
        json={
            "name": "n",
            "kind": "cron",
            "task_id": "t1",
            "cron_expr": "* * * * *",
            "message": "x",
        },
    )
    cid = r.json()["data"]["cron_id"]

    r = client.delete(f"/api/crons/{cid}")
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True
    assert r.json()["data"]["agent_pending"] == []
    assert cid in stub.remove_calls


def test_delete_external_produces_agent_pending(client, app_db):
    _, stub = app_db
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO cron (cron_id, task_id, dispatch_owner, enabled, "
            "created_at, updated_at) VALUES ('ext-1', 't1', 'external', 1, 0, 0)"
        )
        conn.commit()

    r = client.delete("/api/crons/ext-1")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["deleted"] is True
    assert data["agent_pending"] == [
        {
            "kind": "cron",
            "ref": "ext-1",
            "action": "remove",
            "source": "openclaw",
        }
    ]
    # external 不走 scheduler
    assert "ext-1" not in stub.remove_calls


def test_delete_not_found(client):
    r = client.delete("/api/crons/ghost")
    assert r.json()["code"] == 2001


# ── enable / disable ──────────────────────────────────────────────────


def test_enable_disable_internal_calls_apply(client, app_db):
    _, stub = app_db
    r = client.post(
        "/api/crons",
        json={
            "name": "n",
            "kind": "cron",
            "task_id": "t1",
            "cron_expr": "* * * * *",
            "message": "x",
        },
    )
    cid = r.json()["data"]["cron_id"]
    stub.apply_calls.clear()

    client.post(f"/api/crons/{cid}/disable")
    assert stub.apply_calls == [cid]  # apply_enabled_state 重跑, 内部会 remove 若 disabled

    client.post(f"/api/crons/{cid}/enable")
    assert stub.apply_calls == [cid, cid]


def test_enable_disable_external_produces_agent_pending(client, app_db):
    _, stub = app_db
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO cron (cron_id, task_id, dispatch_owner, enabled, "
            "created_at, updated_at) VALUES ('ext-1', 't1', 'external', 1, 0, 0)"
        )
        conn.commit()

    r = client.post("/api/crons/ext-1/disable")
    assert r.status_code == 200
    assert r.json()["data"]["agent_pending"] == [
        {
            "kind": "cron",
            "ref": "ext-1",
            "action": "disable",
            "source": "openclaw",
        }
    ]
