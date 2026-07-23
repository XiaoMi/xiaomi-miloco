# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""TaskRepo SQLite 集成测试 (v2: task_link 表已 DROP; rule/cron 关联走 FK CASCADE)."""

import pytest


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """每个测试起全新的 SQLite + initialize_database 建表。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    yield db_file
    reset_settings()


@pytest.fixture
def repo(real_db):
    from miloco.database.task_repo import TaskRepo

    return TaskRepo()


def _insert_rule_row(task_id: str, rule_id: str) -> None:
    """辅助: 塞一条 rule 引用行 (模拟 rule create 已完成)."""
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO rule (id, name, task_id, condition, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 0)",
            (rule_id, f"rule-{rule_id}", task_id, '{"query":"q"}'),
        )
        conn.commit()


def _insert_cron_ref(task_id: str, cron_id: str, dispatch_owner: str = "external") -> None:
    """internal 需要满足 CHECK 约束 (name/kind/message 全 NOT NULL); external 允许 NULL."""
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        if dispatch_owner == "internal":
            conn.execute(
                "INSERT INTO cron (cron_id, task_id, dispatch_owner, name, kind, "
                "cron_expr, message, enabled, created_at, updated_at) "
                "VALUES (?, ?, 'internal', ?, 'cron', '* * * * *', ?, 1, 0, 0)",
                (cron_id, task_id, f"cron-{cron_id}", "test"),
            )
        else:
            conn.execute(
                "INSERT INTO cron (cron_id, task_id, dispatch_owner, enabled, "
                "created_at, updated_at) VALUES (?, ?, 'external', 1, 0, 0)",
                (cron_id, task_id),
            )
        conn.commit()


def test_create_task_inserts_placeholder_only(repo):
    repo.create_task(task_id="drink_water", description="每天喝 8 杯水")
    view = repo.get_full_view("drink_water")
    assert view["status"] == "active"
    assert view["description"] == "每天喝 8 杯水"
    assert view["cron_refs"] == []


def test_create_409_on_duplicate_task_id(repo):
    from miloco.database.task_repo import TaskConflict

    repo.create_task(task_id="drink_water", description="d1")
    with pytest.raises(TaskConflict):
        repo.create_task(task_id="drink_water", description="d2")
    view = repo.get_full_view("drink_water")
    assert view["description"] == "d1"


def test_set_status_paused_then_active(repo):
    repo.create_task(task_id="t1", description="d")
    assert repo.set_status("t1", "paused") == "ok"
    view = repo.get_full_view("t1")
    assert view["status"] == "paused"
    assert view["paused_at"] is not None
    assert repo.set_status("t1", "paused") == "noop"
    assert repo.set_status("t1", "active") == "ok"
    view = repo.get_full_view("t1")
    assert view["status"] == "active"
    assert view["paused_at"] is None


def test_set_status_not_found(repo):
    assert repo.set_status("ghost", "paused") == "not_found"


def test_update_description(repo):
    repo.create_task(task_id="t1", description="old")
    assert repo.update_description("t1", "new") is True
    assert repo.get_full_view("t1")["description"] == "new"
    assert repo.update_description("ghost", "x") is False


def test_delete_task_cascades_rule_and_cron(repo, real_db):
    import sqlite3

    repo.create_task(task_id="t1", description="d")
    _insert_rule_row("t1", "r1")
    _insert_cron_ref("t1", "c1")

    deleted = repo.delete_task("t1")
    assert deleted == 1
    assert repo.get_full_view("t1") is None
    with sqlite3.connect(str(real_db)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        rule_cnt = conn.execute(
            "SELECT COUNT(*) FROM rule WHERE task_id='t1'"
        ).fetchone()[0]
        cron_cnt = conn.execute(
            "SELECT COUNT(*) FROM cron WHERE task_id='t1'"
        ).fetchone()[0]
        assert rule_cnt == 0
        assert cron_cnt == 0


def test_delete_task_idempotent_returns_zero(repo):
    assert repo.delete_task("ghost") == 0


def test_get_full_view_returns_none_for_missing(repo):
    assert repo.get_full_view("nope") is None


def test_get_full_view_returns_cron_refs(repo):
    """v2: cron_refs 从 cron.task_id 直查; rule 归属由 service 层拼 rule_briefs."""
    repo.create_task(task_id="t1", description="d")
    _insert_rule_row("t1", "r1")
    _insert_cron_ref("t1", "c1")
    view = repo.get_full_view("t1")
    assert view["cron_refs"] == [
        {"ref": "c1", "dispatch_owner": "external"}
    ]


def test_list_all_returns_all_tasks_with_cron_refs(repo):
    repo.create_task(task_id="t1", description="d1")
    _insert_rule_row("t1", "r1")
    repo.create_task(task_id="t2", description="d2")
    _insert_cron_ref("t2", "c2", dispatch_owner="internal")
    rows = repo.list_all()
    assert {r["task_id"] for r in rows} == {"t1", "t2"}
    by_id = {r["task_id"]: r for r in rows}
    assert by_id["t1"]["cron_refs"] == []
    assert by_id["t2"]["cron_refs"] == [
        {"ref": "c2", "dispatch_owner": "internal"}
    ]


def test_get_description(repo):
    """get_description 轻量取 task.description（住户日志「所属任务」用）。"""
    repo.create_task(task_id="t_desc", description="健身追踪")
    assert repo.get_description("t_desc") == "健身追踪"
    assert repo.get_description("nonexistent") is None
