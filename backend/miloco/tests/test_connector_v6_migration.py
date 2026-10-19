# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""v5→v6 schema 迁移测试 (挂起补发改为按 task 显式开启).

覆盖:
- 只加一列 ``defer_blocked_enter``, task 的旧列一个不少
- 存量 task 一律落 0 —— 升级后默认关闭, 不继承旧的"拦下就挂起"行为
- 幂等: 已是 v6 的库不重复跑
- 形状识别: v5 库不被认成 v6; 带新列的库认成 6
"""

from __future__ import annotations

import sqlite3

import pytest
from miloco.database.connector import _DB_SCHEMA_VERSION

# v5 形态的 task 表 (13 列, 没有 defer_blocked_enter)。
_V5_TASK_COLUMNS = [
    "task_id",
    "description",
    "status",
    "paused_at",
    "created_at",
    "lifecycle",
    "expires_at",
    "on_enter_actions",
    "on_enter_desc",
    "on_exit_actions",
    "on_exit_desc",
    "on_target_actions",
    "on_target_desc",
]


def _create_v5_baseline(db_path) -> None:
    """建最小的 v5 形态 DB: 只有 task 表, user_version=5。

    迁移链只跑 v5→v6, 其余表由 init 的缺表兜底按当前 schema 建, 不影响这一级。
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE task ("
        "task_id TEXT PRIMARY KEY, "
        "description TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'active', "
        "paused_at INTEGER, "
        "created_at INTEGER NOT NULL, "
        "lifecycle TEXT NOT NULL DEFAULT 'permanent', "
        "expires_at INTEGER, "
        "on_enter_actions TEXT NOT NULL DEFAULT '[]', "
        "on_enter_desc TEXT, "
        "on_exit_actions TEXT NOT NULL DEFAULT '[]', "
        "on_exit_desc TEXT, "
        "on_target_actions TEXT NOT NULL DEFAULT '[]', "
        "on_target_desc TEXT)"
    )
    conn.execute(
        "INSERT INTO task (task_id, description, status, created_at, lifecycle) "
        "VALUES ('entry_welcome_new', '门口迎宾', 'active', 1791615584889, "
        "'permanent')"
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()


@pytest.fixture
def v5_db(tmp_path, monkeypatch):
    db_file = tmp_path / "v5.db"
    _create_v5_baseline(db_file)
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    yield db_file
    reset_settings()


def _raw(db_file):
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    return conn


def _migrate():
    import miloco.database.connector as connector_module

    connector_module.init_database()


def test_v5_columns_survive(v5_db):
    _migrate()

    conn = _raw(v5_db)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(task)")}
    assert set(_V5_TASK_COLUMNS) <= cols
    assert "defer_blocked_enter" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == _DB_SCHEMA_VERSION
    conn.close()


def test_existing_tasks_default_to_defer_off(v5_db):
    """存量 task 不继承旧的挂起行为 —— 升级后默认关闭, 要显式开启。"""
    _migrate()

    conn = _raw(v5_db)
    row = conn.execute(
        "SELECT description, defer_blocked_enter FROM task "
        "WHERE task_id='entry_welcome_new'"
    ).fetchone()
    assert row["description"] == "门口迎宾"
    assert row["defer_blocked_enter"] == 0
    conn.close()


def test_migration_is_idempotent(v5_db, monkeypatch):
    _migrate()

    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    _migrate()

    conn = _raw(v5_db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == _DB_SCHEMA_VERSION
    assert (
        conn.execute(
            "SELECT defer_blocked_enter FROM task WHERE task_id='entry_welcome_new'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


# ── 形状识别 ──────────────────────────────────────────────────────────


def _shape_only_db(tmp_path, tables: dict[str, list[str]]):
    conn = sqlite3.connect(str(tmp_path / "shape.db"))
    for name, columns in tables.items():
        conn.execute(f"CREATE TABLE {name} ({', '.join(columns)})")
    conn.commit()
    return conn


def test_a_v5_shaped_db_is_not_detected_as_v6(tmp_path):
    """任务表没有新列 → 认成 5, 不能跳过这一级迁移。"""
    from miloco.database.connector import _detect_schema_version

    conn = _shape_only_db(
        tmp_path,
        {
            "rule": ["id", "mode", "direction", "condition_dnf"],
            "task": _V5_TASK_COLUMNS,
            "on_demand_log": [
                "id",
                "visual_artifact_kind",
                "image_frame_counts",
                "has_audio_artifact",
            ],
        },
    )
    assert _detect_schema_version(conn) == 5
    conn.close()


def test_a_v6_shaped_db_is_detected_as_v6(tmp_path):
    from miloco.database.connector import _detect_schema_version

    conn = _shape_only_db(
        tmp_path,
        {
            "rule": ["id", "mode", "direction", "condition_dnf"],
            "task": [*_V5_TASK_COLUMNS, "defer_blocked_enter"],
            "on_demand_log": [
                "id",
                "visual_artifact_kind",
                "image_frame_counts",
                "has_audio_artifact",
            ],
        },
    )
    assert _detect_schema_version(conn) == _DB_SCHEMA_VERSION
    conn.close()
