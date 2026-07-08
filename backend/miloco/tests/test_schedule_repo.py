# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""CronRepo 集成测试."""

from __future__ import annotations

import pytest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    db_file = tmp_path / "cron.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    from miloco.database.connector import get_db_connector
    from miloco.schedule.repo import CronRepo

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task (task_id, description, created_at) "
            "VALUES ('t1', 'x', 0)"
        )
        conn.commit()

    yield CronRepo()
    reset_settings()


def _make_internal_cron(cron_id: str, task_id: str = "t1"):
    from miloco.schedule.schema import Cron

    return Cron(
        cron_id=cron_id,
        task_id=task_id,
        dispatch_owner="internal",
        name=f"n-{cron_id}",
        kind="cron",
        cron_expr="* * * * *",
        message="msg",
        created_at=0,
        updated_at=0,
    )


def _make_external_cron(cron_id: str, task_id: str = "t1"):
    from miloco.schedule.schema import Cron

    return Cron(
        cron_id=cron_id,
        task_id=task_id,
        dispatch_owner="external",
        created_at=0,
        updated_at=0,
    )


def _make_at_cron(cron_id: str, at_ms: int, task_id: str = "t1"):
    from miloco.schedule.schema import Cron

    return Cron(
        cron_id=cron_id,
        task_id=task_id,
        dispatch_owner="internal",
        name=f"at-{cron_id}",
        kind="at",
        at_ms=at_ms,
        message="ping",
        created_at=0,
        updated_at=0,
    )


def test_insert_get_internal(repo):
    repo.insert(_make_internal_cron("c1"))
    got = repo.get("c1")
    assert got is not None
    assert got.dispatch_owner == "internal"
    assert got.kind == "cron"
    assert got.cron_expr == "* * * * *"


def test_insert_external_nullable_fields(repo):
    repo.insert(_make_external_cron("ext-1"))
    got = repo.get("ext-1")
    assert got.dispatch_owner == "external"
    assert got.name is None and got.kind is None


def test_list_by_task_and_where(repo):
    repo.insert(_make_internal_cron("c1"))
    repo.insert(_make_external_cron("ext-1"))
    assert {c.cron_id for c in repo.list_by_task("t1")} == {"c1", "ext-1"}
    assert {c.cron_id for c in repo.list_where("dispatch_owner='internal'")} == {"c1"}
    assert {
        c.cron_id for c in repo.list_where("dispatch_owner='external'")
    } == {"ext-1"}


def test_delete(repo):
    repo.insert(_make_internal_cron("c1"))
    assert repo.delete("c1") == 1
    assert repo.get("c1") is None
    assert repo.delete("ghost") == 0


def test_set_enabled_updates_flag(repo):
    repo.insert(_make_internal_cron("c1"))
    repo.set_enabled("c1", False)
    assert repo.get("c1").enabled is False
    repo.set_enabled("c1", True)
    assert repo.get("c1").enabled is True


def test_mark_fired_and_delete_is_atomic(repo):
    """mark_fired_and_delete 单事务: 提交后行整行 DELETE, fired_at 中间态读不到."""
    repo.insert(_make_at_cron("at-1", at_ms=1_000_000))
    affected = repo.mark_fired_and_delete("at-1", fired_at=9_999_999)
    assert affected == 1
    assert repo.get("at-1") is None


def test_increment_retry_attempt(repo):
    repo.insert(_make_at_cron("at-1", at_ms=1_000_000))
    assert repo.increment_retry_attempt("at-1") == 1
    assert repo.increment_retry_attempt("at-1") == 2
    assert repo.get("at-1").retry_attempt == 2
    assert repo.increment_retry_attempt("ghost") == -1


def test_task_cascade_deletes_cron(repo, monkeypatch):
    """删 task 触发 FK CASCADE 清 cron 表行."""
    from miloco.database.connector import get_db_connector

    repo.insert(_make_internal_cron("c1"))
    repo.insert(_make_external_cron("ext-1"))
    with get_db_connector().get_connection() as conn:
        conn.execute("DELETE FROM task WHERE task_id='t1'")
        conn.commit()
    assert repo.get("c1") is None
    assert repo.get("ext-1") is None
