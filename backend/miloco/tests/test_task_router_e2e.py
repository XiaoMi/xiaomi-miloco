# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Task router e2e 集成测试 (v2)。

TestClient → task_router / task_record_router → 各 Service → SQLite。

1. ``POST /api/tasks`` 仅占位 (body 不接 refs)
2. rule create 只写 rule 表 (rule.task_id FK CASCADE)
3. ``DELETE /api/tasks/{id}?reason=X`` 写 terminate_log + FK CASCADE 清表
"""

import sqlite3

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from miloco.database.rule_repo import RuleRepo
from miloco.rule.schema import (
    Rule,
    RuleCondition,
    RuleLifecycle,
    RuleMode,
)


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module
    import miloco.manager as manager_module

    connector_module.db_connector = None
    connector_module.init_database()

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    m = manager_module.get_manager()

    from miloco.task.service import TaskService

    m._task_service = TaskService()

    from miloco.middleware.exception_handler import handle_exception
    from miloco.task.router import router as task_router
    from miloco.task_record.router import router as task_record_router

    app = FastAPI()

    @app.middleware("http")
    async def _catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            return handle_exception(request, exc)

    app.include_router(task_router, prefix="/api")
    app.include_router(task_record_router, prefix="/api")

    yield app, db_file

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    connector_module.db_connector = None
    reset_settings()


@pytest.fixture
def client(isolated_app):
    app, _ = isolated_app
    return TestClient(app)


def _create_task(client, task_id: str, description: str = "x"):
    return client.post(
        "/api/tasks", json={"task_id": task_id, "description": description}
    )


def _make_rule(task_id: str, name: str | None = None) -> str:
    rule = Rule(
        name=name or f"[{task_id}] r",
        task_id=task_id,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        condition=RuleCondition(perceive_device_ids=["d1"], query="人在客厅"),
        actions=[],
        action_descriptions=["开台灯"],
    )
    return RuleRepo().create(rule)


def test_create_get_lifecycle(client, isolated_app):
    """方案 P 完整 lifecycle：占位 create → 自动 link rule → get → delete。"""
    _, db_file = isolated_app

    r = _create_task(client, "living_room_light", "客厅有人时开台灯")
    assert r.status_code == 200, r.text
    assert r.json()["code"] == 0
    assert r.json()["data"] == {"task_id": "living_room_light"}

    # rule create 内部自动写 task_link
    rule_id = _make_rule("living_room_light")

    r = client.get("/api/tasks/living_room_light")
    body = r.json()["data"]
    assert body["task_id"] == "living_room_light"
    assert len(body["rule_briefs"]) == 1
    assert body["rule_briefs"][0]["rule_id"] == rule_id

    r = client.delete("/api/tasks/living_room_light?reason=completed")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["backend_synced"]["rules_deleted"] == [rule_id]

    with sqlite3.connect(str(db_file)) as conn:
        for tbl in ("task", "rule"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE task_id='living_room_light'"
            ).fetchone()[0]
            assert n == 0, f"{tbl} not cleaned"


def test_create_body_rejects_refs(client):
    """方案 P：body 含 refs 字段返 422 unknown_field。"""
    r = client.post(
        "/api/tasks",
        json={
            "task_id": "t1",
            "description": "x",
            "rule_refs": ["rxx"],
        },
    )
    assert r.status_code == 422


def test_create_conflict_on_duplicate_task_id(client):
    """重复 task_id 返 ConflictException (HTTP 200 + code=2002)。"""
    _create_task(client, "dup_task")
    r = _create_task(client, "dup_task")
    assert r.status_code == 200
    assert r.json()["code"] == 2002


def test_rule_create_requires_existing_task(client):
    """方案 P：rule.task_id 对应 task 不存在 → FK violation 兜底。

    应用层 ``RuleService.create_rule`` 会提前 404 ``task_not_found``，但
    直接走 ``RuleRepo.create`` 时由 FK CONSTRAINT 拦截抛 IntegrityError。
    """
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _make_rule("nonexistent_task")


def test_delete_writes_terminate_log_with_reason(client, isolated_app):
    _, db_file = isolated_app
    _create_task(client, "t1", description="喝水")
    _make_rule("t1")

    # 挂个 record
    client.post(
        "/api/tasks/t1/record",
        json={
            "kind": "progress",
            "content": {"target": 8, "unit": "杯", "window": "day"},
        },
    )
    client.post("/api/tasks/t1/record/progress/increment", json={"delta": 5})

    r = client.delete("/api/tasks/t1?reason=abandoned")
    assert r.status_code == 200

    with sqlite3.connect(str(db_file)) as conn:
        conn.row_factory = sqlite3.Row
        log = conn.execute(
            "SELECT reason, kind, description FROM task_terminate_log WHERE task_id='t1'"
        ).fetchone()
        assert log["reason"] == "abandoned"
        assert log["kind"] == "progress"
        assert log["description"] == "喝水"
        # FK CASCADE 清 rule / task_record_*; task_link 表已 DROP
        for tbl in ("task", "rule", "task_record_progress"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE task_id='t1'"
            ).fetchone()[0]
            assert n == 0, f"{tbl} not cleaned"


def test_delete_invalid_reason_rejected(client):
    _create_task(client, "t1")
    r = client.delete("/api/tasks/t1?reason=garbage")
    assert r.status_code == 422


def test_list_returns_rule_briefs(client):
    _create_task(client, "t1")
    rid1 = _make_rule("t1", name="[t1] r1")
    _create_task(client, "t2")
    rid2 = _make_rule("t2", name="[t2] r2")

    r = client.get("/api/tasks")
    items = r.json()["data"]
    by_id = {v["task_id"]: v for v in items}
    assert by_id["t1"]["rule_briefs"][0]["rule_id"] == rid1
    assert by_id["t2"]["rule_briefs"][0]["rule_id"] == rid2


def test_disable_enable_flow(client):
    _create_task(client, "t1")
    rid = _make_rule("t1")

    r = client.post("/api/tasks/t1/disable")
    body = r.json()["data"]
    assert body["status"] == "paused"
    assert body["backend_synced"]["rules"][0]["rule_id"] == rid

    r = client.post("/api/tasks/t1/enable")
    assert r.json()["data"]["status"] == "active"


def test_update_description(client):
    _create_task(client, "t1", description="old")
    r = client.patch("/api/tasks/t1", json={"description": "new"})
    assert r.status_code == 200
    r = client.get("/api/tasks/t1")
    assert r.json()["data"]["description"] == "new"


def test_e2e_summary_full_flow(client):
    """summary endpoint 完整流程:3 个 task / 三种 kind / 真实 progress/event mutate。"""
    _create_task(client, "morning_workout", "每天做 20 个俯卧撑")
    _create_task(client, "deep_work", "每天深度工作 2 小时")
    _create_task(client, "guest_visits", "记录客人到访")

    client.post(
        "/api/tasks/morning_workout/record",
        json={
            "kind": "progress",
            "content": {"target": 20, "unit": "次", "window": "day"},
        },
    )
    client.post(
        "/api/tasks/morning_workout/record/progress/increment",
        json={"delta": 12},
    )

    client.post(
        "/api/tasks/deep_work/record",
        json={"kind": "duration", "content": {"target_minutes": 120}},
    )

    client.post(
        "/api/tasks/guest_visits/record",
        json={"kind": "event", "content": {}},
    )
    client.post(
        "/api/tasks/guest_visits/record/event/append",
        json={"description": "客人到访", "at": "2026-06-11T11:42:13+08:00"},
    )

    resp = client.get("/api/tasks/summary?window=day")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    by_id = {item["task_id"]: item for item in body["data"]}

    assert by_id["morning_workout"]["record"]["kind"] == "progress"
    assert by_id["morning_workout"]["record"]["derived"]["current"] == 12
    assert by_id["morning_workout"]["record"]["derived"]["remaining"] == 8

    assert by_id["deep_work"]["record"]["kind"] == "duration"
    assert by_id["deep_work"]["record"]["derived"]["target_minutes"] == 120

    assert by_id["guest_visits"]["record"]["kind"] == "event"
    assert by_id["guest_visits"]["record"]["derived"]["count_total"] == 1


def test_e2e_summary_window_invalid_returns_422(client):
    resp = client.get("/api/tasks/summary?window=week")
    assert resp.status_code == 422


def test_e2e_summary_empty(client):
    resp = client.get("/api/tasks/summary")
    assert resp.status_code == 200
    assert resp.json()["data"] == []
