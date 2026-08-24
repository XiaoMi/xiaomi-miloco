# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""v2 → v3 迁移（token usage 加 base_url）与随之而来的 rollup 语义。

这里钉住的每一条，错了都是**静默**的：

- 老数据一律 ''（未记录来源），迁移**不做任何回填**
- 日表主键必须变成四元组，否则两个 endpoint 的同日数据会在 rollup 里被累加成一行，
  而原始行紧接着被 DELETE —— 不可恢复、无报错
- 迁移不丢行、不改数（行数与各度量逐列核对）
- 幂等：重复跑不炸、不重复搬
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta

import pytest


def _make_v2_db(path) -> None:
    """手搓一个 v2 结构的库（含数据），用来验迁移。"""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL, model TEXT NOT NULL, type TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_tokens INTEGER NOT NULL DEFAULT 0, video_tokens INTEGER NOT NULL DEFAULT 0,
            audio_tokens INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
        CREATE INDEX idx_token_usage_timestamp ON token_usage(timestamp);
        CREATE TABLE token_usage_daily (
            date TEXT NOT NULL, model TEXT NOT NULL, type TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_tokens INTEGER NOT NULL DEFAULT 0, video_tokens INTEGER NOT NULL DEFAULT 0,
            audio_tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, model, type));
        CREATE INDEX idx_token_usage_daily_date ON token_usage_daily(date);
    """)
    c.execute(
        "INSERT INTO token_usage (timestamp, model, type, input_tokens, output_tokens,"
        " cache_tokens, video_tokens, audio_tokens, created_at)"
        " VALUES (1000, 'mimo-v2.5', 'realtime', 111, 22, 3, 4, 5, 1000)"
    )
    for d, m, t, calls in (
        ("2026-07-01", "mimo-v2.5", "realtime", 7),
        ("2026-07-01", "mimo-v2.5", "on_demand", 2),
        ("2026-07-02", "other-model", "realtime", 5),
    ):
        c.execute(
            "INSERT INTO token_usage_daily (date, model, type, calls, input_tokens,"
            " output_tokens, cache_tokens, video_tokens, audio_tokens)"
            " VALUES (?, ?, ?, ?, 100, 10, 1, 2, 3)",
            (d, m, t, calls),
        )
    c.execute("PRAGMA user_version = 2")
    c.commit()
    c.close()


def _run_migration(path):
    from miloco.database.connector import _migrate_v2_to_v3

    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    _migrate_v2_to_v3(c)
    return c


def test_old_rows_get_empty_base_url_never_backfilled(tmp_path):
    """老数据一律 ''。回填是禁止的——猜来源或写入口述断言都会让它和记录值混淆。"""
    db = tmp_path / "v2.db"
    _make_v2_db(db)
    c = _run_migration(db)
    live = c.execute("SELECT base_url FROM token_usage").fetchall()
    daily = c.execute("SELECT base_url FROM token_usage_daily").fetchall()
    assert [r["base_url"] for r in live] == [""]
    assert {r["base_url"] for r in daily} == {""}


def test_daily_primary_key_becomes_four_tuple(tmp_path):
    """主键必须含 base_url，否则 rollup 会把两个 endpoint 静默累加成一行。"""
    db = tmp_path / "v2.db"
    _make_v2_db(db)
    c = _run_migration(db)
    pk = [
        r["name"]
        for r in c.execute("PRAGMA table_info(token_usage_daily)").fetchall()
        if r["pk"]
    ]
    assert set(pk) == {"date", "model", "base_url", "type"}


def test_migration_loses_no_rows_and_no_numbers(tmp_path):
    db = tmp_path / "v2.db"
    _make_v2_db(db)
    c = _run_migration(db)
    rows = c.execute(
        "SELECT date, model, type, calls, input_tokens, output_tokens,"
        " cache_tokens, video_tokens, audio_tokens FROM token_usage_daily"
        " ORDER BY date, model, type"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("2026-07-01", "mimo-v2.5", "on_demand", 2, 100, 10, 1, 2, 3),
        ("2026-07-01", "mimo-v2.5", "realtime", 7, 100, 10, 1, 2, 3),
        ("2026-07-02", "other-model", "realtime", 5, 100, 10, 1, 2, 3),
    ]
    live = c.execute("SELECT * FROM token_usage").fetchone()
    assert (live["input_tokens"], live["output_tokens"], live["cache_tokens"]) == (111, 22, 3)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 3


def test_migration_is_idempotent(tmp_path):
    """重复跑不该炸、也不该再搬一遍（列已存在就跳过）。"""
    db = tmp_path / "v2.db"
    _make_v2_db(db)
    c = _run_migration(db)
    before = c.execute("SELECT COUNT(*) FROM token_usage_daily").fetchone()[0]
    from miloco.database.connector import _migrate_v2_to_v3

    _migrate_v2_to_v3(c)
    assert c.execute("SELECT COUNT(*) FROM token_usage_daily").fetchone()[0] == before


# ── rollup 语义：base_url 必须同时在 GROUP BY 与 ON CONFLICT 里 ──────────


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """真库 + 真 repo（走 connector 建表，即 v3 结构）。"""
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "t.db"))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as cm

    monkeypatch.setattr(cm, "db_connector", None)
    cm.init_database()
    import miloco.database.token_usage_repo as rm

    monkeypatch.setattr(rm, "_repo", None)
    yield rm.get_token_usage_repo()
    reset_settings()


def _raw(repo, ts_ms, model, base_url, inp):
    with repo.db.get_connection() as conn:
        conn.execute(
            "INSERT INTO token_usage (timestamp, model, base_url, type, input_tokens,"
            " output_tokens, cache_tokens, video_tokens, audio_tokens, created_at)"
            " VALUES (?, ?, ?, 'realtime', ?, 0, 0, 0, 0, ?)",
            (ts_ms, model, base_url, inp, ts_ms),
        )
        conn.commit()


def _ts(d: date, hour: int = 12) -> int:
    return int(datetime.combine(d, time(hour=hour)).timestamp() * 1000)


def test_rollup_never_merges_different_base_urls(repo):
    """同模型名、同一天、两个 endpoint —— 必须滚成**两行**。

    合并是不可恢复的：rollup 的 DELETE 紧接着就把原始行删了，而且不会报错。
    """
    old = date.today() - timedelta(days=10)
    _raw(repo, _ts(old), "mimo-v2.5", "https://a.example/v1", 100)
    _raw(repo, _ts(old), "mimo-v2.5", "https://b.example/v1", 200)
    repo._maybe_rollup(int(datetime.now().timestamp() * 1000))

    with repo.db.get_connection() as conn:
        rows = conn.execute(
            "SELECT base_url, calls, input_tokens FROM token_usage_daily"
            " WHERE model='mimo-v2.5' ORDER BY base_url"
        ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("https://a.example/v1", 1, 100),
        ("https://b.example/v1", 1, 200),
    ]


def test_rollup_upsert_accumulates_within_same_four_tuple(repo):
    """同四元组重复 rollup 要累加，而不是插重复行或覆盖。"""
    old = date.today() - timedelta(days=10)
    _raw(repo, _ts(old, 8), "m", "https://a/v1", 100)
    repo._maybe_rollup(int(datetime.now().timestamp() * 1000))
    _raw(repo, _ts(old, 9), "m", "https://a/v1", 50)
    repo._maybe_rollup(int(datetime.now().timestamp() * 1000))

    with repo.db.get_connection() as conn:
        rows = conn.execute(
            "SELECT base_url, calls, input_tokens FROM token_usage_daily"
        ).fetchall()
    assert [tuple(r) for r in rows] == [("https://a/v1", 2, 150)]


def test_old_empty_base_url_rows_stay_separate_from_recorded(repo):
    """迁移当天的混合情形：'' 与真 URL 同日同模型，必须各自成行、不并。

    这是真实会发生的——迁移那天的数据还在实时表里（rollup 只碰 3 天前的），
    拿到 ''；当天之后的新插入有真 URL。等这天滚存时应当分裂成两行。
    """
    old = date.today() - timedelta(days=10)
    _raw(repo, _ts(old, 8), "m", "", 100)
    _raw(repo, _ts(old, 9), "m", "https://a/v1", 70)
    repo._maybe_rollup(int(datetime.now().timestamp() * 1000))

    with repo.db.get_connection() as conn:
        rows = conn.execute(
            "SELECT base_url, input_tokens FROM token_usage_daily ORDER BY base_url"
        ).fetchall()
    assert [tuple(r) for r in rows] == [("", 100), ("https://a/v1", 70)]


def test_aggregate_paths_expose_base_url(repo):
    """今日走分桶、近 7 天走日聚合——两条路都必须带 base_url，否则两个视图行为不一致。"""
    _raw(repo, int(datetime.now().timestamp() * 1000), "m", "https://a/v1", 10)
    assert "base_url" in repo.aggregate_buckets()[0]
    assert "base_url" in repo.aggregate_daily()[0]
    assert "base_url" in repo.list_events()[0][0]
