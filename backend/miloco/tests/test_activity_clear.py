# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""「日志」页一键清理的单元/集成测试.

- `POST /api/events/clear`                     — 清空 meaningful_events
- `POST /api/perception/on-demand-logs/clear`  — 清空 on_demand_log
- `POST /api/actions/clear`                    — 清空 action_ledger(「触发场景」等动作台账,
  落在 observability.db)。早期版本漏了这本台账,住户清完日志仍留一屏「触发场景」。

两者的真实 HTTP 端到端断言在 `app/smoke_test.sh`(起完整 slim 服务 → 插行 → 清理 → 数行数)。
这里覆盖三层,失败时定位更快:

1. events 端点的 HTTP 契约 + 幂等;
2. on_demand_log 的 repo 层 `delete_all()`(真实 SQLite);
3. on-demand 端点的 HTTP 契约 —— 用 stub 顶掉 `manager.perception_service`:
   完整 PerceptionService 只在 lifespan 的 `manager.initialize()` 里构造(会去连摄像头),
   单元测试不该拉起它;路由本身(path / method / 响应体 / 鉴权依赖)仍然照测;
4. action_ledger 端点:`obs_db_path` 绑到临时库(observability router 只依赖这个 state,
   不依赖 lifespan),插行 → 清理 → 校验条数与幂等。

verify_token 在 settings.server.token="" 时自动 bypass(默认值,测试无需鉴权)。
"""

import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """每个 case 独立 DB + minimal app(不拉起完整 lifespan)."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module
    import miloco.manager as manager_module

    connector_module.db_connector = None
    connector_module.init_database()
    manager_module.Manager._instance = None
    manager_module.manager_instance = None

    from miloco.middleware.exception_handler import handle_exception

    app = FastAPI()

    @app.middleware("http")
    async def _catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            return handle_exception(request, exc)

    yield app, tmp_path

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    connector_module.db_connector = None
    reset_settings()


@pytest.fixture
def events_client(isolated_app):
    from miloco.perception.events_router import router as events_router

    app, _ = isolated_app
    app.include_router(events_router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def dao(isolated_app):
    from miloco.manager import get_manager

    return get_manager().meaningful_events_dao


@pytest.fixture
def od_repo(isolated_app):
    from miloco.database.on_demand_log_repo import OnDemandLogRepo

    return OnDemandLogRepo()


def _insert_event(dao, **kwargs) -> str:
    eid = str(uuid.uuid4())
    defaults = dict(
        event_id=eid,
        timestamp=int(time.time() * 1000),
        text="t",
        payload_json="{}",
        has_rule_hit=False,
        has_suggestion=False,
        has_asr=False,
        device_ids=["cam_living_01"],
    )
    defaults.update(kwargs)
    assert dao.insert(**defaults) is True
    return eid


def _insert_od_log(repo, *, query: str = "谁在客厅？") -> str:
    from miloco.perception.schema import OnDemandLogEntry

    entry_id = str(uuid.uuid4())
    assert repo.append(
        OnDemandLogEntry(
            id=entry_id,
            timestamp=int(time.time() * 1000),
            query=query,
            answer="没有人",
            sources=["lumi.camera.v1"],
            latency_ms=120,
            snapshot_count=0,
            clip_dids=[],
            clip_kinds={},
            has_trace=False,
        )
    ) is True
    return entry_id


class TestClearEventsHTTP:
    def test_clears_all_rows_and_is_idempotent(self, events_client, dao):
        for _ in range(3):
            _insert_event(dao)
        assert len(events_client.get("/api/events").json()["data"]["events"]) == 3

        resp = events_client.post("/api/events/clear")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["deleted"] == 3
        assert events_client.get("/api/events").json()["data"]["events"] == []

        # 再点一次:没有行可删,不应报错
        again = events_client.post("/api/events/clear")
        assert again.status_code == 200
        assert again.json()["data"]["deleted"] == 0

    def test_does_not_touch_on_demand_logs(self, events_client, dao, od_repo):
        _insert_event(dao)
        _insert_od_log(od_repo)

        assert events_client.post("/api/events/clear").json()["data"]["deleted"] == 1
        assert od_repo.count_all() == 1


class TestClearOnDemandLogsRepo:
    def test_delete_all_removes_every_row(self, od_repo):
        for _ in range(2):
            _insert_od_log(od_repo)
        assert od_repo.count_all() == 2

        assert od_repo.delete_all() == 2
        assert od_repo.count_all() == 0
        # 幂等:表已空时返回 0,不抛
        assert od_repo.delete_all() == 0

    def test_does_not_touch_events(self, od_repo, dao):
        _insert_event(dao)
        _insert_event(dao)
        _insert_od_log(od_repo)

        assert od_repo.delete_all() == 1
        # 事件是另一张表,必须原样保留
        assert len(dao.query(limit=10)) == 2


class TestClearOnDemandLogsHTTP:
    """端点契约:stub 掉只在 lifespan 里构造的 perception_service。"""

    def test_returns_deleted_count(self, isolated_app, monkeypatch):
        from miloco.perception import router as perception_router_module

        calls: list[str] = []

        def _clear() -> int:
            calls.append("clear")
            return 7

        monkeypatch.setattr(
            perception_router_module,
            "manager",
            SimpleNamespace(perception_service=SimpleNamespace(clear_on_demand_logs=_clear)),
        )
        app, _ = isolated_app
        app.include_router(perception_router_module.router, prefix="/api")
        client = TestClient(app)

        resp = client.post("/api/perception/on-demand-logs/clear")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["message"] == "ok"
        assert body["data"]["deleted"] == 7
        assert calls == ["clear"]


@pytest.fixture
def obs_client(isolated_app):
    """observability router(动作台账):只依赖 app.state.obs_db_path,无需 lifespan。"""
    from miloco.observability.metrics_db import connect, init_schema
    from miloco.observability.router import router as observability_router

    app, tmp_path = isolated_app
    obs_db = tmp_path / "observability.db"
    conn = connect(obs_db)  # init_schema 收连接(内部按 user_version 决定建/迁移)
    init_schema(conn)
    conn.close()
    app.state.obs_db_path = obs_db
    app.include_router(observability_router)
    return TestClient(app), obs_db


def _insert_action(obs_db, *, action_id: str | None = None, action_type: str = "scene_trigger") -> str:
    """插一行动作台账(模拟 RuleRunner 触发场景时 `_write_action_ledger` 写的那行)."""
    import sqlite3

    aid = action_id or str(uuid.uuid4())
    conn = sqlite3.connect(str(obs_db))
    conn.execute(
        "INSERT INTO action_ledger (id, timestamp, action_type, did, value_json, success,"
        " source, source_id, home_id) VALUES (?, ?, ?, ?, ?, 1, 'rule', 'rule-1', 'home-1')",
        (aid, int(time.time() * 1000), action_type, "scene.1", '{"scene_name": "阅读"}'),
    )
    conn.commit()
    conn.close()
    return aid


class TestClearActionsHTTP:
    """「触发场景」日志归 /api/actions/clear 管 —— 清理日志必须连它一起清。"""

    def test_clears_ledger_and_is_idempotent(self, obs_client):
        client, obs_db = obs_client
        for _ in range(2):
            _insert_action(obs_db)
        assert len(client.get("/api/actions").json()) == 2

        resp = client.post("/api/actions/clear")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2
        assert client.get("/api/actions").json() == []

        again = client.post("/api/actions/clear")
        assert again.status_code == 200
        assert again.json()["deleted"] == 0

    def test_does_not_touch_meaningful_events(self, obs_client, dao):
        client, obs_db = obs_client
        _insert_event(dao)
        _insert_action(obs_db)

        assert client.post("/api/actions/clear").json()["deleted"] == 1
        # 事件在 miloco.db,台账在 observability.db:两库互不影响,各自清各自的
        assert len(dao.query(limit=10)) == 1

    def test_only_clears_actions_not_other_observability_tables(self, obs_client):
        """清理动作台账不该顺手清掉 traces(那是性能观测数据,页面另有入口)。"""
        import sqlite3

        client, obs_db = obs_client
        _insert_action(obs_db)
        conn = sqlite3.connect(str(obs_db))
        conn.execute(
            "INSERT INTO traces (trace_id, timestamp) VALUES ('t1', ?)",
            (int(time.time() * 1000),),
        )
        conn.commit()
        conn.close()

        assert client.post("/api/actions/clear").json()["deleted"] == 1
        conn = sqlite3.connect(str(obs_db))
        assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1
        conn.close()
