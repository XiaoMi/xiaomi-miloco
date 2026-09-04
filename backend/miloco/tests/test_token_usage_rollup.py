# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for TokenUsageRepo._maybe_rollup against a real SQLite file.

Covers the load-bearing edge cases of the cold-storage path:

- 早退（无旧数据）：fast SELECT 1 LIMIT 1 命中 None → 不开事务
- cutoff 按本地日 00:00 对齐：(today - N) 当天的数据保留在 raw，不被劈裂
- ON CONFLICT 累加：同 (date, model, base_url, type) 多次 rollup 不重复，度量字段累加
- 多 (model, type) 维度：一次 rollup 产生多行 daily
- 所有 modality 列（input/output/cache/video/audio）一起 SUM 入 daily
- rollup 后 raw 被 DELETE
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

# ---- Fixtures: 真实 SQLite 隔离 ----


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """每个 case 起全新的 SQLite，自动创建表、自动清理。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()

    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    # token_usage_repo 也有自己的 module-level 单例，要顺便清掉
    import miloco.database.token_usage_repo as repo_module

    monkeypatch.setattr(repo_module, "_repo", None)

    yield db_file

    reset_settings()


@pytest.fixture
def repo(real_db):
    from miloco.database.token_usage_repo import get_token_usage_repo

    return get_token_usage_repo()


# ---- Helpers ----


def _ts_ms(d: date, hour: int = 12) -> int:
    """Convert (date, hour-local) to ms epoch."""
    return int(datetime.combine(d, time(hour=hour)).timestamp() * 1000)


def _insert_raw(
    repo,
    *,
    ts_ms: int,
    model: str = "mimo-v2-omni",
    type_: str = "realtime",
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_tokens: int = 0,
    video_tokens: int = 0,
    audio_tokens: int = 0,
):
    """Insert a row directly with a controlled timestamp.

    Bypasses TokenUsageRepo.insert() because that one always stamps `time.time()`
    — we need historical timestamps to exercise rollup.
    """
    with repo.db.get_connection() as conn:
        conn.execute(
            "INSERT INTO token_usage "
            "(timestamp, model, type, input_tokens, output_tokens, "
            " cache_tokens, video_tokens, audio_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts_ms, model, type_,
                input_tokens, output_tokens,
                cache_tokens, video_tokens, audio_tokens,
                ts_ms,
            ),
        )
        conn.commit()


def _count(repo, table: str) -> int:
    with repo.db.get_connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0]


def _fetch_daily(repo) -> list[dict]:
    with repo.db.get_connection() as conn:
        rows = conn.execute(
            "SELECT date, model, type, calls, input_tokens, output_tokens, "
            "       cache_tokens, video_tokens, audio_tokens "
            "FROM token_usage_daily ORDER BY date, model, type"
        ).fetchall()
        return [dict(r) for r in rows]


# ---- Tests ----


def test_early_return_when_no_old_data(repo):
    """raw 表里没有早于 cutoff 的数据 → _maybe_rollup 直接 return，daily 表保持空。"""
    today = date.today()
    # 只塞今天的事件
    _insert_raw(repo, ts_ms=_ts_ms(today, hour=10))
    _insert_raw(repo, ts_ms=_ts_ms(today, hour=15))

    repo._maybe_rollup(_ts_ms(today, hour=20))

    assert _count(repo, "token_usage") == 2          # 没动 raw
    assert _count(repo, "token_usage_daily") == 0    # daily 还是空


def test_rollup_moves_old_events_to_daily(repo):
    """超过保留期的事件被聚合进 daily，raw 表对应行被删。"""
    today = date.today()
    old = today - timedelta(days=5)   # 远早于 cutoff (today - 3)

    _insert_raw(repo, ts_ms=_ts_ms(old, hour=9), input_tokens=100, output_tokens=10)
    _insert_raw(repo, ts_ms=_ts_ms(old, hour=14), input_tokens=200, output_tokens=20)
    # 今天一条，不该动
    _insert_raw(repo, ts_ms=_ts_ms(today, hour=10), input_tokens=50)

    repo._maybe_rollup(_ts_ms(today, hour=20))

    assert _count(repo, "token_usage") == 1          # 旧的两条被删除，今天那条留下
    rows = _fetch_daily(repo)
    assert len(rows) == 1
    assert rows[0]["date"] == old.isoformat()
    assert rows[0]["calls"] == 2
    assert rows[0]["input_tokens"] == 300            # 100 + 200
    assert rows[0]["output_tokens"] == 30


def test_cutoff_is_day_aligned(repo):
    """cutoff = (today - 3 days) 00:00 本地——刚好 3 天前的 00:01 应进 daily，
    而 (today - 3) 当天的事件应保留在 raw（无论几点）。"""
    today = date.today()
    boundary_day = today - timedelta(days=3)
    just_before_cutoff_day = today - timedelta(days=4)

    # 4 天前 23:59 一定要进 daily
    _insert_raw(repo, ts_ms=_ts_ms(just_before_cutoff_day, hour=23))
    # 3 天前 00:01 应该留在 raw（cutoff 对齐到那天 00:00，等于不严格小于）
    _insert_raw(repo, ts_ms=_ts_ms(boundary_day, hour=0) + 60_000)
    # 3 天前 23:59 也应该留在 raw
    _insert_raw(repo, ts_ms=_ts_ms(boundary_day, hour=23))

    repo._maybe_rollup(_ts_ms(today, hour=12))

    rows = _fetch_daily(repo)
    daily_dates = {r["date"] for r in rows}
    assert daily_dates == {just_before_cutoff_day.isoformat()}, (
        "boundary day should stay in raw, never split"
    )
    assert _count(repo, "token_usage") == 2          # boundary 当天 2 条都留下


def test_on_conflict_accumulates_when_run_twice(repo):
    """同一 (date, model, base_url, type) 多次 rollup → daily 行被 UPDATE 累加，不重复插入。

    本用例只出一行，是因为两次都不传 base_url、落在同一个默认空串上（即同一个四元组）；
    跨 endpoint 不会合并，那条由 test_token_usage_base_url_migration 钉着。
    """
    today = date.today()
    old = today - timedelta(days=5)

    # 第 1 次 rollup
    _insert_raw(repo, ts_ms=_ts_ms(old, hour=8), input_tokens=100, output_tokens=10)
    repo._maybe_rollup(_ts_ms(today, hour=12))
    rows = _fetch_daily(repo)
    assert len(rows) == 1
    assert rows[0]["calls"] == 1
    assert rows[0]["input_tokens"] == 100

    # 又一批旧数据进来（比如重启后老 raw 残留）
    _insert_raw(repo, ts_ms=_ts_ms(old, hour=9), input_tokens=50, output_tokens=5)
    repo._maybe_rollup(_ts_ms(today, hour=13))
    rows = _fetch_daily(repo)
    assert len(rows) == 1, "should not insert a 2nd row for the same key"
    assert rows[0]["calls"] == 2
    assert rows[0]["input_tokens"] == 150            # 100 + 50
    assert rows[0]["output_tokens"] == 15


def test_multiple_model_and_type_dimensions(repo):
    """一次 rollup 产生多个 (model, type) 行。"""
    today = date.today()
    old = today - timedelta(days=5)

    _insert_raw(repo, ts_ms=_ts_ms(old, hour=8),  model="mimo-v2", type_="realtime",  input_tokens=100)
    _insert_raw(repo, ts_ms=_ts_ms(old, hour=9),  model="mimo-v2", type_="realtime",  input_tokens=200)
    _insert_raw(repo, ts_ms=_ts_ms(old, hour=10), model="mimo-v2", type_="on_demand", input_tokens=300)
    _insert_raw(repo, ts_ms=_ts_ms(old, hour=11), model="mimo-v3", type_="realtime",  input_tokens=400)

    repo._maybe_rollup(_ts_ms(today, hour=12))

    rows = _fetch_daily(repo)
    assert len(rows) == 3
    by_key = {(r["model"], r["type"]): r for r in rows}
    assert by_key[("mimo-v2", "realtime")]["calls"] == 2
    assert by_key[("mimo-v2", "realtime")]["input_tokens"] == 300
    assert by_key[("mimo-v2", "on_demand")]["calls"] == 1
    assert by_key[("mimo-v2", "on_demand")]["input_tokens"] == 300
    assert by_key[("mimo-v3", "realtime")]["calls"] == 1
    assert by_key[("mimo-v3", "realtime")]["input_tokens"] == 400


def test_all_modality_columns_sum_into_daily(repo):
    """input / output / cache / video / audio 全部 SUM 累加入 daily。"""
    today = date.today()
    old = today - timedelta(days=5)

    _insert_raw(
        repo, ts_ms=_ts_ms(old, hour=8),
        input_tokens=1000, output_tokens=50,
        cache_tokens=200, video_tokens=600, audio_tokens=100,
    )
    _insert_raw(
        repo, ts_ms=_ts_ms(old, hour=9),
        input_tokens=2000, output_tokens=70,
        cache_tokens=400, video_tokens=1200, audio_tokens=200,
    )

    repo._maybe_rollup(_ts_ms(today, hour=12))

    rows = _fetch_daily(repo)
    assert len(rows) == 1
    r = rows[0]
    assert r["calls"] == 2
    assert r["input_tokens"]  == 3000
    assert r["output_tokens"] == 120
    assert r["cache_tokens"]  == 600
    assert r["video_tokens"]  == 1800
    assert r["audio_tokens"]  == 300


def test_raw_table_is_emptied_after_rollup(repo):
    """rollup 完所有过期行从 raw 表 DELETE 干净。"""
    today = date.today()
    old = today - timedelta(days=5)

    for hour in (8, 9, 10, 11, 12):
        _insert_raw(repo, ts_ms=_ts_ms(old, hour=hour))

    assert _count(repo, "token_usage") == 5
    repo._maybe_rollup(_ts_ms(today, hour=12))
    assert _count(repo, "token_usage") == 0
    assert _count(repo, "token_usage_daily") == 1   # 5 条聚合成 1 行


def test_aggregate_daily_no_double_count_at_boundary(repo):
    """rollup 跑完后调 aggregate_daily()，边界日不能在 daily 表和 raw 表同时出现。

    回归 commit f0e8eb7 修过的 bug：早期版本 cutoff 是毫秒精度，导致 cutoff 落
    在某天中间，那天的事件一半进 daily 一半留 raw → aggregate_daily 返回两行
    同 (date, model, type) 键。修复后 cutoff 对齐 00:00，那天要么全 rollup 要
    么全 raw。
    """
    today = date.today()
    boundary_day = today - timedelta(days=3)
    older = today - timedelta(days=5)

    # 边界日 boundary_day 当天两次
    _insert_raw(repo, ts_ms=_ts_ms(boundary_day, hour=5), input_tokens=100)
    _insert_raw(repo, ts_ms=_ts_ms(boundary_day, hour=20), input_tokens=200)
    # 更早一条
    _insert_raw(repo, ts_ms=_ts_ms(older, hour=10), input_tokens=500)

    repo._maybe_rollup(_ts_ms(today, hour=12))

    rows = repo.aggregate_daily()
    # 检查不重复：每个 (date, model, type) 只出现一次
    keys = [(r["date"], r["model"], r["type"]) for r in rows]
    assert len(keys) == len(set(keys)), f"duplicate (date, model, type) rows: {keys}"
    # boundary 当天还在 raw（live 聚合得出），older 在 daily
    by_date = {r["date"]: r for r in rows}
    assert boundary_day.isoformat() in by_date
    assert by_date[boundary_day.isoformat()]["calls"] == 2
    assert by_date[boundary_day.isoformat()]["input_tokens"] == 300
    assert older.isoformat() in by_date
    assert by_date[older.isoformat()]["calls"] == 1
    assert by_date[older.isoformat()]["input_tokens"] == 500


# ---- aggregate_buckets：服务端按时间桶聚合（today 视图）----


def test_aggregate_buckets_groups_by_bin_model_type(repo):
    """按 bin 分桶 + model/type 聚合；桶数由窗口/bin 决定，与事件数无关。"""
    base = _ts_ms(date(2026, 1, 1), hour=0)  # 当天 00:00
    minute = 60_000

    # bucket 0（00:00–01:00）：两条 realtime
    _insert_raw(repo, ts_ms=base + 5 * minute, type_="realtime",
                input_tokens=100, output_tokens=10, video_tokens=50, audio_tokens=5, cache_tokens=20)
    _insert_raw(repo, ts_ms=base + 40 * minute, type_="realtime",
                input_tokens=200, output_tokens=20, video_tokens=100, audio_tokens=10, cache_tokens=30)
    # bucket 1（01:00–02:00）：一条 on_demand
    _insert_raw(repo, ts_ms=base + 90 * minute, type_="on_demand",
                input_tokens=300, output_tokens=30, video_tokens=0, audio_tokens=50, cache_tokens=40)
    # bucket 3（03:00–04:00）：一条 realtime
    _insert_raw(repo, ts_ms=base + 200 * minute, type_="realtime",
                input_tokens=400, output_tokens=40)

    rows = repo.aggregate_buckets(base, base + 24 * 60 * minute, bin_minutes=60)
    by = {(r["bucket_ms"], r["type"]): r for r in rows}

    assert len(rows) == 3  # (b0,realtime) (b1,on_demand) (b3,realtime)

    b0 = by[(base, "realtime")]
    assert b0["calls"] == 2
    assert b0["input_tokens"] == 300 and b0["output_tokens"] == 30
    assert b0["video_tokens"] == 150 and b0["audio_tokens"] == 15 and b0["cache_tokens"] == 50

    b1 = by[(base + 60 * minute, "on_demand")]
    assert b1["calls"] == 1 and b1["audio_tokens"] == 50

    b3 = by[(base + 180 * minute, "realtime")]
    assert b3["calls"] == 1 and b3["input_tokens"] == 400


def test_aggregate_buckets_bin_changes_bucket_assignment(repo):
    """更细的 bin 把同一批事件分进更多桶（桶数随 bin 变，不随事件数变）。"""
    base = _ts_ms(date(2026, 1, 1), hour=0)
    minute = 60_000
    for off in (5, 40, 90, 200):  # 4 条 realtime，分散在不同时刻
        _insert_raw(repo, ts_ms=base + off * minute, type_="realtime")

    hourly = repo.aggregate_buckets(base, base + 24 * 60 * minute, bin_minutes=60)
    fine = repo.aggregate_buckets(base, base + 24 * 60 * minute, bin_minutes=30)
    # 60 分：5/40 同桶0、90 在桶1、200 在桶3 → 3 桶；30 分：各自独立 → 4 桶
    assert len(hourly) == 3
    assert len(fine) == 4
    # 总调用数不随 bin 变
    assert sum(r["calls"] for r in hourly) == sum(r["calls"] for r in fine) == 4


def test_clear_all_empties_both_tables(repo):
    """clear_all 删空实时表 + 日聚合，返回各表删除条数。"""
    repo.insert(
        "mimo", "https://a/v1", {"prompt_tokens": 100, "completion_tokens": 10}, "realtime"
    )
    repo.insert(
        "mimo", "https://a/v1", {"prompt_tokens": 200, "completion_tokens": 20}, "on_demand"
    )

    events, _ = repo.list_events(None, None, 100)
    assert len(events) == 2

    deleted = repo.clear_all()
    # clear_all 现在走 clear_since(None)，额外带回 daily_from_date（全清时为 None）
    assert deleted == {
        "token_usage": 2,
        "token_usage_daily": 0,
        "daily_from_date": None,
    }

    events_after, _ = repo.list_events(None, None, 100)
    assert events_after == []
    # 幂等：再清一次返回全 0
    assert repo.clear_all() == {
        "token_usage": 0,
        "token_usage_daily": 0,
        "daily_from_date": None,
    }


def test_fire_record_persists_from_ephemeral_loop(repo):
    """回归:感知主路径每窗在 ``asyncio.run`` 起的临时 loop 里调 fire_record。

    旧实现用 ``asyncio.create_task`` 排到该临时 loop,窗末 ``asyncio.run`` 退出会把它
    cancel(``CancelledError`` 又躲过 ``except Exception``)→ realtime 用量静默丢失
    (Linux fsync 慢几乎必丢)。现同步直写,临时 loop 退出后行必须已落库。
    """
    import asyncio

    from miloco.database.token_usage_repo import fire_record

    async def one_window() -> None:
        fire_record(
            "mimo-v2.5",
            "https://api.example/v1",
            {"prompt_tokens": 3000, "completion_tokens": 200},
            "realtime",
        )
        # 窗末通常还有少量收尾 await 再退出;旧实现这点时间不足以让写盘 task 完成。

    asyncio.run(one_window())  # 临时 loop 在此退出

    events, _ = repo.list_events(None, None, 100)
    assert len(events) == 1, "临时 loop 退出后 realtime 用量必须已落库,不能被丢"
    assert events[0]["type"] == "realtime"
    assert events[0]["input_tokens"] == 3000
    # base_url 也必须一起落库——它是模型身份的一半
    assert events[0]["base_url"] == "https://api.example/v1"


def test_rollup_failure_keeps_the_new_event(repo, monkeypatch):
    """滚存失败不该把当天这条新用量一起拒收。

    持续失败（磁盘满、写锁超时）时，若每条事件都先撞上滚存失败再丢掉自己，
    用量统计就冻在原地而感知照常跑——界面上只剩「用量不再增长」这一个线索，
    而记用量的入口把异常降级成 warning。标记位仍然不置，下次插入自动重试。
    """
    boom_calls = {"n": 0}

    def boom(_ts_ms):
        boom_calls["n"] += 1
        raise RuntimeError("disk full")

    monkeypatch.setattr(repo, "_maybe_rollup", boom)
    repo._last_archive_check = None

    repo.insert("m1", "https://a/v1", {"prompt_tokens": 10, "completion_tokens": 2}, "realtime")

    with repo.db.get_connection() as conn:
        rows = conn.execute("SELECT model, input_tokens FROM token_usage").fetchall()
    assert [(r["model"], r["input_tokens"]) for r in rows] == [("m1", 10)]
    assert boom_calls["n"] == 1

    # 标记位没被置上 → 下一条插入还会再试一次
    repo.insert("m1", "https://a/v1", {"prompt_tokens": 5, "completion_tokens": 1}, "realtime")
    assert boom_calls["n"] == 2
    with repo.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0] == 2
