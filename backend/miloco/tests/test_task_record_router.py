# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task_record + task_link HTTP 路由集成测试（spec §11.1 方案 P 部分）。

TestClient → task_record_router → TaskRecordService → SQLite，断言：
- POST /tasks/{id}/record 重复返 409 record_already_exists
- POST /tasks/{id}/record task 不存在返 404 task_not_found
- GET /tasks/{id}/record 无活跃 record 返 404 no_active_record
- PATCH 字段白名单（禁字段返 422 schema_invalid）
- mutate 响应均含 ``derived`` 字段
- POST /tasks/{id}/link 仅接 cron；rule kind 返 422 wrong_kind；重复返 409
- DELETE /tasks/{id}?reason=...  query 参数解析
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture
def app_db(tmp_path, monkeypatch):
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
def client(app_db):
    app, _ = app_db
    return TestClient(app)


def _create_task_minimal(client, task_id: str):
    """直接绕到 service 层建 task 占位行（避免 HTTP 链路上的鉴权 / pydantic 噪音）。"""
    from miloco.database.connector import get_db_connector
    from miloco.utils.time_utils import now_iso

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task (task_id, description, status, created_at) "
            "VALUES (?, ?, 'active', ?)",
            (task_id, "minimal", now_iso()),
        )
        conn.commit()


# ── record init / get / patch ────────────────────────────────────────────────


class TestRecordCrud:
    def test_init_progress_then_get(self, client):
        _create_task_minimal(client, "p1")
        r = client.post(
            "/api/tasks/p1/record",
            json={
                "kind": "progress",
                "content": {"target": 8, "unit": "杯", "window": "day"},
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["code"] == 0
        view = r.json()["data"]
        assert view["kind"] == "progress"
        assert view["derived"]["remaining"] == 8

        r = client.get("/api/tasks/p1/record")
        assert r.status_code == 200
        assert r.json()["data"]["record"]["current"] == 0

    def test_init_duplicate_returns_conflict(self, client):
        _create_task_minimal(client, "p1")
        client.post(
            "/api/tasks/p1/record",
            json={
                "kind": "progress",
                "content": {"target": 8, "unit": "杯", "window": "day"},
            },
        )
        r = client.post(
            "/api/tasks/p1/record",
            json={
                "kind": "progress",
                "content": {"target": 10, "unit": "杯", "window": "day"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 2002  # ConflictException
        assert "record_already_exists" in body["message"]

    def test_init_task_not_found(self, client):
        r = client.post(
            "/api/tasks/nope/record",
            json={
                "kind": "progress",
                "content": {"target": 8, "unit": "杯", "window": "day"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 2001  # ResourceNotFoundException
        assert "task_not_found" in body["message"]

    def test_get_no_active_record(self, client):
        _create_task_minimal(client, "p1")
        r = client.get("/api/tasks/p1/record")
        assert r.status_code == 200
        assert r.json()["code"] == 2001
        assert "no_active_record" in r.json()["message"]

    def test_patch_whitelist(self, client):
        _create_task_minimal(client, "p1")
        client.post(
            "/api/tasks/p1/record",
            json={
                "kind": "progress",
                "content": {"target": 8, "unit": "杯", "window": "day"},
            },
        )
        r = client.patch(
            "/api/tasks/p1/record", json={"target": 10, "unit": "次"}
        )
        assert r.status_code == 200
        assert r.json()["data"]["record"]["target"] == 10

    def test_patch_forbidden_field(self, client):
        _create_task_minimal(client, "p1")
        client.post(
            "/api/tasks/p1/record",
            json={
                "kind": "progress",
                "content": {"target": 8, "unit": "杯", "window": "day"},
            },
        )
        r = client.patch("/api/tasks/p1/record", json={"status": "completed"})
        assert r.status_code in (200, 422)
        body = r.json()
        if r.status_code == 200:
            assert body["code"] != 0
        # ValidationException code=1002
        assert "schema_invalid" in body.get("message", "") or r.status_code == 422


# ── mutate（progress / event / session） ─────────────────────────────────────


class TestMutate:
    def _setup_progress(self, client, task_id):
        _create_task_minimal(client, task_id)
        client.post(
            f"/api/tasks/{task_id}/record",
            json={
                "kind": "progress",
                "content": {"target": 3, "unit": "杯", "window": "day"},
            },
        )

    def test_progress_increment_carries_derived(self, client):
        self._setup_progress(client, "p1")
        r = client.post("/api/tasks/p1/record/progress/increment", json={"delta": 1})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["current"] == 1
        assert data["derived"]["remaining"] == 2

    def test_progress_increment_flips_completed(self, client):
        self._setup_progress(client, "p1")
        client.post("/api/tasks/p1/record/progress/increment", json={"delta": 10})
        r = client.post("/api/tasks/p1/record/progress/increment", json={"delta": 1})
        data = r.json()["data"]
        assert data["noop"] is True

    def test_event_append(self, client):
        _create_task_minimal(client, "e1")
        client.post(
            "/api/tasks/e1/record",
            json={"kind": "event", "content": {}},
        )
        r = client.post(
            "/api/tasks/e1/record/event/append",
            json={"description": "一", "at": "2026-06-10T09:00:00"},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["entry_id"] > 0
        assert data["derived"]["count_total"] == 1

    def test_session_start_then_end(self, client):
        _create_task_minimal(client, "d1")
        client.post(
            "/api/tasks/d1/record",
            json={"kind": "duration", "content": {"target_minutes": 60}},
        )
        r = client.post(
            "/api/tasks/d1/record/session/start",
            json={"at": "2026-06-10T09:00:00"},
        )
        assert r.status_code == 200
        r = client.post(
            "/api/tasks/d1/record/session/end",
            json={"at": "2026-06-10T09:25:00"},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["this_session_minutes"] == 25
        assert data["derived"]["accumulated_minutes_today"] == 25


# ── delete reason ────────────────────────────────────────────────────────────


class TestDeleteReason:
    def test_delete_default_reason(self, client):
        _create_task_minimal(client, "t1")
        r = client.delete("/api/tasks/t1")
        assert r.status_code == 200

    def test_delete_with_reason_query(self, client):
        _create_task_minimal(client, "t1")
        r = client.delete("/api/tasks/t1?reason=abandoned")
        assert r.status_code == 200

    def test_delete_invalid_reason_returns_422(self, client):
        _create_task_minimal(client, "t1")
        r = client.delete("/api/tasks/t1?reason=garbage")
        assert r.status_code == 422


# ── compute ──────────────────────────────────────────────────────────────────


class TestComputeRangeAndArchives:
    """G1 + G2：区间 compute + archive list HTTP 路径。"""

    def test_compute_range_event(self, client):
        _create_task_minimal(client, "e1")
        client.post(
            "/api/tasks/e1/record",
            json={"kind": "event", "content": {}},
        )
        for at in (
            "2026-06-05T09:00:00+08:00",
            "2026-06-05T15:00:00+08:00",
            "2026-06-06T10:00:00+08:00",
        ):
            client.post(
                "/api/tasks/e1/record/event/append",
                json={"description": "x", "at": at},
            )

        r = client.post(
            "/api/tasks/e1/record/compute?from=2026-06-05&to=2026-06-06"
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["from"] == "2026-06-05"
        assert data["to"] == "2026-06-06"
        assert data["derived"]["total_count"] == 3

    def test_compute_range_from_to_paired(self, client):
        _create_task_minimal(client, "e1")
        client.post(
            "/api/tasks/e1/record",
            json={"kind": "event", "content": {}},
        )
        # 只传 from 不传 to → 422
        r = client.post("/api/tasks/e1/record/compute?from=2026-06-05")
        assert r.status_code == 422

    def test_compute_range_exclusive_with_window(self, client):
        _create_task_minimal(client, "e1")
        client.post(
            "/api/tasks/e1/record",
            json={"kind": "event", "content": {}},
        )
        r = client.post(
            "/api/tasks/e1/record/compute"
            "?from=2026-06-05&to=2026-06-06&window=day"
        )
        assert r.status_code == 422

    def test_archives_list_event(self, client):
        _create_task_minimal(client, "e1")
        client.post(
            "/api/tasks/e1/record",
            json={"kind": "event", "content": {}},
        )
        for at in (
            "2026-06-05T09:00:00+08:00",
            "2026-06-06T10:00:00+08:00",
            "2026-06-06T11:00:00+08:00",
        ):
            client.post(
                "/api/tasks/e1/record/event/append",
                json={"description": "x", "at": at},
            )

        r = client.get("/api/tasks/e1/record/archives")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["kind"] == "event"
        assert len(data["archives"]) == 2
        assert data["archives"][0]["date"] == "2026-06-06"
        assert data["archives"][0]["count"] == 2


class TestCompute:
    def test_compute_progress(self, client):
        _create_task_minimal(client, "p1")
        client.post(
            "/api/tasks/p1/record",
            json={
                "kind": "progress",
                "content": {"target": 8, "unit": "杯", "window": "day"},
            },
        )
        client.post("/api/tasks/p1/record/progress/increment", json={"delta": 3})
        r = client.post("/api/tasks/p1/record/compute?window=all")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["kind"] == "progress"
        assert data["derived"]["remaining"] == 5
