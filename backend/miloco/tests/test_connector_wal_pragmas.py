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
        # 预置量必须落在 helper 默认上限内，否则 after==0 失败的真因是撞上限、
        # 不是漏 fetchall，会把排查方向带偏。
        assert before < 10000, f"预置 free page 超过默认上限，用例失去判别力: {before}"

        incremental_vacuum(conn)

        after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert after == 0, f"free page 未回收干净，剩 {after}（漏 fetchall 会停在 before-1）"


def test_incremental_vacuum_clamps_non_positive_max_pages(fresh_db):
    """max_pages<=0 必须下钳到 1，不能落到 SQLite 原生的"不限页数"语义。

    回归护栏：helper 里 max(1, ...) 一旦被删回 int(max_pages)，0 会被 SQLite 当
    "不限"、一次清空整个 freelist，after 变 0，本测试的 after==before-1 断言即失败。
    """
    from miloco.database.connector import get_db_connector, incremental_vacuum

    with get_db_connector().get_connection() as conn:
        conn.execute("CREATE TABLE t(x BLOB)")
        conn.executemany(
            "INSERT INTO t VALUES(?)", [(b"0" * 4096,) for _ in range(500)]
        )
        conn.execute("DELETE FROM t")
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert before > 1, f"预置 free page 不足以区分下钳/不限: {before}"

        incremental_vacuum(conn, 0)

        after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert after == before - 1, (
            f"0 被当成不限页数了: {before} → {after}（下钳 max(1, ...) 被删？）"
        )


def test_incremental_vacuum_unlimited_when_none(fresh_db):
    """max_pages=None 不限页数、分批搬到清空整个 freelist（供上线追赶积压）。

    预置量（~4200 页）远超单批 chunk_pages(500)，验证 while 分批循环能搬完清零。
    """
    from miloco.database.connector import get_db_connector, incremental_vacuum

    with get_db_connector().get_connection() as conn:
        conn.execute("CREATE TABLE t(x BLOB)")
        conn.executemany(
            "INSERT INTO t VALUES(?)", [(b"0" * 4096,) for _ in range(2000)]
        )
        conn.execute("DELETE FROM t")
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert before > 500, f"预置量需超过单批 chunk 才能验证分批: {before}"

        incremental_vacuum(conn, None)

        after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert after == 0, f"None 应清空整个 freelist，剩 {after}"


def test_incremental_vacuum_splits_into_chunk_sized_statements(fresh_db):
    """回归护栏：必须按 chunk_pages 切成多条短语句，每条一个独立写事务。

    合回单条语句时 freelist 最终值不变、其它用例照样绿，只有这条能 red：
    它数的是"跑了几条 PRAGMA、每条多少页"。
    """
    from miloco.database.connector import get_db_connector, incremental_vacuum

    class _CountingConn:
        def __init__(self, conn):
            self._conn = conn
            self.vacuum_stmts: list[str] = []

        def execute(self, sql, *args):
            if "incremental_vacuum" in sql:
                self.vacuum_stmts.append(sql)
            return self._conn.execute(sql, *args)

    with get_db_connector().get_connection() as conn:
        conn.execute("CREATE TABLE t(x BLOB)")
        conn.executemany(
            "INSERT INTO t VALUES(?)", [(b"0" * 4096,) for _ in range(500)]
        )
        conn.execute("DELETE FROM t")
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert before > 100, f"预置量需超过单批 chunk 才能验证分批: {before}"

        spy = _CountingConn(conn)
        incremental_vacuum(spy, max_pages=None, chunk_pages=100)

        assert len(spy.vacuum_stmts) > 1, (
            f"只跑了 {len(spy.vacuum_stmts)} 条 PRAGMA，分批被合回单条语句？"
        )
        assert all(s == "PRAGMA incremental_vacuum(100)" for s in spy.vacuum_stmts), (
            f"单条语句超出 chunk_pages 上限: {spy.vacuum_stmts}"
        )
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0


def test_incremental_vacuum_skips_non_incremental_db(tmp_path, caplog):
    """回归护栏：auto_vacuum!=INCREMENTAL 的库必须打 warning 后直接返回、不发回收 PRAGMA。

    这是模式前置校验唯一的护栏：一旦校验被删，这条 PRAGMA 在 NONE 库上静默 no-op、
    freelist 永不归零，max_pages=None 会无终止循环挂死整个测试 job。用薄代理数
    incremental_vacuum 语句数、断言"一条都没发"，直接锁住"命中校验、零回收"这个
    不变量，不依赖"默认上限空转多少轮后返回"的隐含前提。
    """
    import logging
    import sqlite3

    from miloco.database.connector import incremental_vacuum

    # 默认建库 auto_vacuum=NONE(0),模拟 fresh-build 路径之前建的老库
    db_file = tmp_path / "none_mode.db"
    conn = sqlite3.connect(db_file)
    try:
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        conn.execute("CREATE TABLE t(x BLOB)")
        conn.executemany("INSERT INTO t VALUES(?)", [(b"0" * 4096,) for _ in range(500)])
        conn.execute("DELETE FROM t")
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert before > 0, f"预置 free page 不足: {before}"

        vacuum_stmts: list[str] = []

        class _RecordingConn:
            def execute(self, sql, *args):
                if "incremental_vacuum" in sql:
                    vacuum_stmts.append(sql)
                return conn.execute(sql, *args)

        with caplog.at_level(logging.WARNING):
            incremental_vacuum(_RecordingConn())

        assert vacuum_stmts == [], f"NONE 模式下仍发了回收 PRAGMA: {vacuum_stmts}(模式校验被删?)"
        assert "Skip incremental_vacuum" in caplog.text, "模式校验分支未走到(warning 未打)"
        after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert after == before, f"NONE 库不该回收任何页: {before} → {after}"
    finally:
        conn.close()


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
