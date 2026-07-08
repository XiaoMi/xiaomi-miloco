# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""v1→v2 schema 迁移测试.

覆盖:
- fresh-build 直接落 v2 形态 (rule NOT NULL + FK, cron 表存在, 无 task_link)
- 迁移 A/B/C/D/E 五型 orphan 各自的处置策略
- cron 行迁移 + cron dangling fail-fast
- 迁移后三重不变量
- rollback_v2_to_v1 反向 + internal cron 前置断言
"""

from __future__ import annotations

import sqlite3

import pytest


def _create_v1_baseline(db_path) -> None:
    """建 v1 形态 DB (rule 表无 FK / task_id NULLABLE, 有 task_link 表, user_version=1)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "CREATE TABLE kv (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "key TEXT UNIQUE NOT NULL, value TEXT, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    cursor.execute(
        "CREATE TABLE task (task_id TEXT PRIMARY KEY, description TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'active', paused_at INTEGER, "
        "created_at INTEGER NOT NULL)"
    )
    # rule 表 v1 形态: task_id NULLABLE, 无 FK
    cursor.execute("""
        CREATE TABLE rule (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            task_id TEXT,
            mode TEXT NOT NULL DEFAULT 'event',
            lifecycle TEXT NOT NULL DEFAULT 'permanent',
            enabled BOOLEAN DEFAULT 1,
            condition TEXT NOT NULL,
            actions TEXT NOT NULL DEFAULT '[]',
            action_descriptions TEXT NOT NULL DEFAULT '[]',
            on_enter_actions TEXT NOT NULL DEFAULT '[]',
            on_enter_desc TEXT,
            on_exit_actions TEXT NOT NULL DEFAULT '[]',
            on_exit_desc TEXT,
            on_target_desc TEXT,
            terminate_when TEXT,
            exit_debounce_seconds INTEGER NOT NULL DEFAULT 60,
            duration_seconds INTEGER,
            duration_ratio REAL NOT NULL DEFAULT 0.8,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)
    cursor.execute(
        "CREATE TABLE task_link (task_id TEXT NOT NULL, link_kind TEXT NOT NULL, "
        "link_ref TEXT NOT NULL, PRIMARY KEY (task_id, link_kind, link_ref), "
        "FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE)"
    )
    cursor.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def _insert_task(cursor, task_id: str, description: str = "test task") -> None:
    cursor.execute(
        "INSERT INTO task (task_id, description, created_at) VALUES (?, ?, ?)",
        (task_id, description, 1_700_000_000_000),
    )


def _insert_rule(
    cursor, rule_id: str, task_id: str | None = None, name: str = "test rule"
) -> None:
    cursor.execute(
        "INSERT INTO rule (id, name, task_id, condition, created_at, updated_at) "
        "VALUES (?, ?, ?, 'true', ?, ?)",
        (rule_id, name, task_id, 1_700_000_000_000, 1_700_000_000_000),
    )


def _insert_task_link(
    cursor, task_id: str, kind: str, ref: str
) -> None:
    cursor.execute(
        "INSERT INTO task_link (task_id, link_kind, link_ref) VALUES (?, ?, ?)",
        (task_id, kind, ref),
    )


@pytest.fixture
def v1_db(tmp_path, monkeypatch):
    """v1 baseline DB, 可 populate 之后触发 init_database 走 migration."""
    db_file = tmp_path / "v1.db"
    _create_v1_baseline(db_file)
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    yield db_file
    reset_settings()


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """空 DB, 走 fresh-build 直接建 v2 形态."""
    db_file = tmp_path / "fresh.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()
    yield db_file
    reset_settings()


def _run_init(db_file):
    """触发 init_database 走 v1→v2 步进迁移."""
    import miloco.database.connector as connector_module

    connector_module.init_database()
    return connector_module.get_db_connector()


def _read_rule(conn, rule_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rule WHERE id=?", (rule_id,)
    ).fetchone()


# ── fresh-build ───────────────────────────────────────────────────────


def test_fresh_build_is_v2_form(fresh_db):
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        # user_version = 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        # task_link 表不存在
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "task_link" not in tables
        # cron 表存在
        assert "cron" in tables
        # rule 表 DDL 含 NOT NULL + FK
        rule_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='rule'"
        ).fetchone()[0]
        assert "task_id TEXT NOT NULL" in rule_ddl
        assert "REFERENCES task(task_id) ON DELETE CASCADE" in rule_ddl


def test_fresh_build_cron_check_constraints(fresh_db):
    """cron 表 CHECK 约束正确生效 (internal 必填, external 允许 NULL)."""
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        _insert_task(conn.cursor(), "task-1")
        conn.commit()

        # internal 缺 name 应被 CHECK 拦
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cron (cron_id, task_id, dispatch_owner, kind, "
                "cron_expr, message, created_at, updated_at) "
                "VALUES ('c1', 'task-1', 'internal', 'cron', '* * * * *', "
                "'msg', 0, 0)"
            )
            conn.commit()

        # external 全部业务字段 NULL 应允许
        conn.execute(
            "INSERT INTO cron (cron_id, task_id, dispatch_owner, "
            "created_at, updated_at) VALUES ('c2', 'task-1', 'external', 0, 0)"
        )
        conn.commit()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cron WHERE cron_id='c2'"
            ).fetchone()[0]
            == 1
        )


# ── v1→v2 各型 orphan ─────────────────────────────────────────────────


def test_migrate_c_type_preserved(v1_db):
    """C 型 (task_id NOT NULL, task_link 无) → 原样保留."""
    conn = sqlite3.connect(str(v1_db))
    cursor = conn.cursor()
    _insert_task(cursor, "task-c")
    _insert_rule(cursor, "rule-c", task_id="task-c", name="C-type")
    conn.commit()
    conn.close()

    _run_init(v1_db)

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        row = _read_rule(conn, "rule-c")
        assert row is not None
        assert row["task_id"] == "task-c"


def test_migrate_b_type_backfilled(v1_db):
    """B 型 (task_id NULL, task_link 有) → 从 task_link 回填 task_id."""
    conn = sqlite3.connect(str(v1_db))
    cursor = conn.cursor()
    _insert_task(cursor, "task-b")
    _insert_rule(cursor, "rule-b", task_id=None, name="B-type")
    _insert_task_link(cursor, "task-b", "rule", "rule-b")
    conn.commit()
    conn.close()

    _run_init(v1_db)

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        row = _read_rule(conn, "rule-b")
        assert row is not None
        assert row["task_id"] == "task-b"


def test_migrate_a_type_deleted(v1_db, caplog):
    """A 型 (task_id NULL, task_link 无) → DELETE + log 全字段."""
    conn = sqlite3.connect(str(v1_db))
    cursor = conn.cursor()
    _insert_rule(cursor, "rule-a", task_id=None, name="A-type-orphan")
    conn.commit()
    conn.close()

    import logging

    with caplog.at_level(logging.WARNING):
        _run_init(v1_db)

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        assert _read_rule(conn, "rule-a") is None

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "v1→v2 dropping orphan rule" in log_text
    assert "rule-a" in log_text
    assert "A-type-orphan" in log_text


def test_migrate_e_type_deleted(v1_db, caplog):
    """E 型 (task_id NOT NULL 但 task 已不存在) → DELETE + log."""
    conn = sqlite3.connect(str(v1_db))
    cursor = conn.cursor()
    # 故意不建 task-e, 让 rule.task_id dangling
    _insert_rule(cursor, "rule-e", task_id="task-e-gone", name="E-type-dangling")
    conn.commit()
    conn.close()

    import logging

    with caplog.at_level(logging.WARNING):
        _run_init(v1_db)

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        assert _read_rule(conn, "rule-e") is None

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "v1→v2 dropping orphan rule" in log_text
    assert "rule-e" in log_text


def test_migrate_d_type_fails_fast(v1_db, caplog):
    """D 型 (rule.task_id != task_link.task_id) → fail-fast + log 完整字段."""
    conn = sqlite3.connect(str(v1_db))
    cursor = conn.cursor()
    _insert_task(cursor, "task-d-a")
    _insert_task(cursor, "task-d-b")
    _insert_rule(cursor, "rule-d", task_id="task-d-a", name="D-type-conflict")
    _insert_task_link(cursor, "task-d-b", "rule", "rule-d")
    conn.commit()
    conn.close()

    import logging

    with caplog.at_level(logging.ERROR):
        with pytest.raises(Exception) as exc_info:
            _run_init(v1_db)

    err_msg = str(exc_info.value)
    assert "D-type" in err_msg or "aborted" in err_msg

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "D-type conflict" in log_text
    assert "rule-d" in log_text


# ── cron 迁移 ─────────────────────────────────────────────────────────


def test_migrate_cron_row_moved_to_external(v1_db):
    """task_link.cron 行搬到 cron 表 external 分区."""
    conn = sqlite3.connect(str(v1_db))
    cursor = conn.cursor()
    _insert_task(cursor, "task-with-cron")
    _insert_task_link(cursor, "task-with-cron", "cron", "openclaw-cron-uuid-1")
    conn.commit()
    conn.close()

    _run_init(v1_db)

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        # cron 表有对应 external 行
        row = conn.execute(
            "SELECT * FROM cron WHERE cron_id=?",
            ("openclaw-cron-uuid-1",),
        ).fetchone()
        assert row is not None
        assert row["task_id"] == "task-with-cron"
        assert row["dispatch_owner"] == "external"
        assert row["name"] is None
        assert row["kind"] is None
        # task_link 表已 DROP
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "task_link" not in tables


def test_migrate_cron_dangling_fails_fast(v1_db, caplog):
    """cron dangling (task_link.cron 指向已删 task) → fail-fast + log."""
    conn = sqlite3.connect(str(v1_db))
    cursor = conn.cursor()
    # 建了 task_link.cron 但没建对应的 task
    # 因为 v1 task_link 有 FK CASCADE, 需要先建 task 再删 task 才能造 dangling;
    # 或者直接 disable FK check.
    cursor.execute("PRAGMA foreign_keys=OFF")
    _insert_task_link(cursor, "task-gone", "cron", "dangling-cron")
    conn.commit()
    conn.close()

    import logging

    with caplog.at_level(logging.ERROR):
        with pytest.raises(Exception) as exc_info:
            _run_init(v1_db)

    err_msg = str(exc_info.value)
    assert "cron dangling" in err_msg or "aborted" in err_msg

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "cron dangling" in log_text
    assert "dangling-cron" in log_text


# ── 迁移后不变量 ──────────────────────────────────────────────────────


def test_migrate_final_invariants(v1_db):
    """迁移完成后: user_version=2, task_link DROP, FK 干净, rule.task_id 无 NULL."""
    conn = sqlite3.connect(str(v1_db))
    cursor = conn.cursor()
    _insert_task(cursor, "task-1")
    _insert_task(cursor, "task-2")
    _insert_rule(cursor, "rule-c1", task_id="task-1")
    _insert_rule(cursor, "rule-b1", task_id=None)
    _insert_task_link(cursor, "task-2", "rule", "rule-b1")
    _insert_task_link(cursor, "task-1", "cron", "cron-uuid-1")
    conn.commit()
    conn.close()

    _run_init(v1_db)

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "task_link" not in tables
        assert "cron" in tables
        # FK 检查空
        assert conn.execute("PRAGMA foreign_key_check(rule)").fetchall() == []
        assert conn.execute("PRAGMA foreign_key_check(cron)").fetchall() == []
        # rule.task_id 无 NULL
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM rule WHERE task_id IS NULL"
            ).fetchone()[0]
            == 0
        )


def test_migrate_is_skipped_on_v2_db(fresh_db):
    """已经是 v2 (fresh-build) 的库再次 init 不重跑迁移, 数据无变化."""
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        _insert_task(conn.cursor(), "task-x")
        conn.commit()

    # 重置 db_connector, 再次 init_database, 应无副作用
    import miloco.database.connector as connector_module

    connector_module.db_connector = None
    connector_module.init_database()

    with get_db_connector().get_connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task WHERE task_id='task-x'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


# ── rollback ──────────────────────────────────────────────────────────


def test_rollback_reverses_v2_to_v1(v1_db):
    """rollback_v2_to_v1: rule 反向 rebuild + task_link 重建 + cron DROP + user_version=1."""
    conn = sqlite3.connect(str(v1_db))
    cursor = conn.cursor()
    _insert_task(cursor, "task-a")
    _insert_rule(cursor, "rule-a", task_id="task-a")
    _insert_task_link(cursor, "task-a", "cron", "cron-a")
    conn.commit()
    conn.close()

    _run_init(v1_db)  # v1 → v2

    from miloco.database.connector import get_db_connector, rollback_v2_to_v1

    stats = rollback_v2_to_v1()
    assert stats["rule_reverted_to_link"] == 1
    assert stats["cron_reverted_to_link"] == 1

    with get_db_connector().get_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "task_link" in tables
        assert "cron" not in tables
        # rule 表 v1 形态 (无 FK, task_id NULLABLE)
        rule_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='rule'"
        ).fetchone()[0]
        assert "task_id TEXT NOT NULL" not in rule_ddl
        assert "REFERENCES task(task_id)" not in rule_ddl
        # task_link 有 rule / cron 反写行
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_link WHERE link_kind='rule' AND link_ref='rule-a'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_link WHERE link_kind='cron' AND link_ref='cron-a'"
            ).fetchone()[0]
            == 1
        )


def test_rollback_refuses_when_internal_cron_present(v1_db):
    """rollback 前置断言: internal cron 未清空 → raise."""
    _run_init(v1_db)  # v1 → v2 (无数据)

    from miloco.database.connector import get_db_connector, rollback_v2_to_v1

    # 手工插一条 internal cron
    with get_db_connector().get_connection() as conn:
        _insert_task(conn.cursor(), "task-i")
        conn.execute(
            "INSERT INTO cron (cron_id, task_id, dispatch_owner, name, kind, "
            "cron_expr, message, created_at, updated_at) "
            "VALUES ('int-c1', 'task-i', 'internal', 'test', 'cron', "
            "'* * * * *', 'msg', 0, 0)"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="internal cron"):
        rollback_v2_to_v1()
