# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""验证 connector.get_connection 把 WAL / busy_timeout / vacuum 等 PRAGMA 设上。"""

from __future__ import annotations

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_pragma.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()
    yield db_file
    reset_settings()


def _pragma(conn, name: str):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_journal_mode_is_wal(fresh_db):
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        assert _pragma(conn, "journal_mode") == "wal"


def test_synchronous_is_normal(fresh_db):
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        # SQLite synchronous 取值: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
        assert _pragma(conn, "synchronous") == 1


def test_busy_timeout_follows_settings(fresh_db):
    """busy_timeout 由 sqlite3.connect(timeout=) 驱动,等于 settings.database.timeout * 1000。"""
    from miloco.config import get_settings
    from miloco.database.connector import get_db_connector

    expected_ms = int(get_settings().database.timeout * 1000)
    with get_db_connector().get_connection() as conn:
        assert _pragma(conn, "busy_timeout") == expected_ms


def test_auto_vacuum_incremental_on_fresh_db(fresh_db):
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        # 0=NONE, 1=FULL, 2=INCREMENTAL
        assert _pragma(conn, "auto_vacuum") == 2


def test_wal_autocheckpoint_1000(fresh_db):
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        assert _pragma(conn, "wal_autocheckpoint") == 1000


def test_foreign_keys_enabled(fresh_db):
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        assert _pragma(conn, "foreign_keys") == 1


def test_incremental_vacuum_callable(fresh_db):
    """auto_vacuum=INCREMENTAL 下 PRAGMA incremental_vacuum 不抛。"""
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute("PRAGMA incremental_vacuum(100)")


def test_incremental_vacuum_helper_reclaims_all_free_pages(fresh_db):
    """incremental_vacuum helper 必须消费结果集逐页回收，把 freelist 清零。

    回归护栏：helper 里的 .fetchall() 一旦被删回去，只 step 一次、每轮仅回收 1 页，
    freelist_count 会停在 before-1，本测试的 after==0 断言即失败。
    """
    from miloco.database.connector import get_db_connector, incremental_vacuum

    with get_db_connector().get_connection() as conn:
        conn.execute("CREATE TABLE t(x BLOB)")
        conn.executemany(
            "INSERT INTO t VALUES(?)", [(b"0" * 4096,) for _ in range(2000)]
        )
        conn.execute("DELETE FROM t")
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert before > 1, f"预置 free page 不足以区分逐页/单页回收: {before}"

        incremental_vacuum(conn)

        after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert after == 0, f"free page 未回收干净，剩 {after}（漏 fetchall 会停在 before-1）"


def test_pre_created_empty_file_runs_fresh_path(tmp_path, monkeypatch):
    """预创建空文件场景:db_file.touch() 后启动,应走 fresh 路径设 auto_vacuum。

    auto_vacuum 只对空库生效(且必须先于建表),如果只按"文件存在性"判断
    fresh,预占位的空文件会走 existing 分支,跳过 db-level PRAGMA。
    """
    db_file = tmp_path / "pre_touched.db"
    db_file.touch()  # 预创建空文件(运维占位 / 测试 fixture)
    assert db_file.stat().st_size == 0

    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        assert _pragma(conn, "auto_vacuum") == 2, (
            "预创建空文件场景必须走 fresh 路径设 auto_vacuum=INCREMENTAL"
        )
        assert _pragma(conn, "journal_mode") == "wal"

    reset_settings()
