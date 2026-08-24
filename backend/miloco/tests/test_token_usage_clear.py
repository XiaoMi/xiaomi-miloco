# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for TokenUsageRepo.clear_since / clear_all against a real SQLite file.

按范围清空是**不可逆**操作,这里钉住的都是「多删」或「少删」会静默发生的地方:

- 全删(None)两表都空
- 按时间删只动边界之后的行,之前的留住
- **日表只有天粒度**:跨天范围会连带删掉 since 之前、同一天里的记录。
  这是日聚合的精度损失、SQL 绕不过去,所以要么如实返回 daily_from_date 让上层说明,
  要么就是在悄悄多删——本测试固定住「如实多删并报告」这个行为。
- 两表同一事务:只清一张会让总量与明细对不上且不报错
- 边界是闭区间 >=:恰好等于 since 的那一行要被删
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()
    import miloco.database.token_usage_repo as repo_module

    monkeypatch.setattr(repo_module, "_repo", None)
    yield db_file
    reset_settings()


@pytest.fixture
def repo(real_db):
    from miloco.database.token_usage_repo import get_token_usage_repo

    return get_token_usage_repo()


def _ts_ms(d: date, hour: int = 12) -> int:
    return int(datetime.combine(d, time(hour=hour)).timestamp() * 1000)


def _raw(repo, ts_ms: int, model: str = "m1") -> None:
    with repo.db.get_connection() as conn:
        conn.execute(
            "INSERT INTO token_usage (timestamp, model, type, input_tokens, "
            "output_tokens, cache_tokens, video_tokens, audio_tokens, created_at) "
            "VALUES (?, ?, 'realtime', 100, 10, 0, 0, 0, ?)",
            (ts_ms, model, ts_ms),
        )
        conn.commit()


def _daily(repo, d: date, model: str = "m1") -> None:
    with repo.db.get_connection() as conn:
        conn.execute(
            "INSERT INTO token_usage_daily (date, model, type, calls, input_tokens, "
            "output_tokens, cache_tokens, video_tokens, audio_tokens) "
            "VALUES (?, ?, 'realtime', 1, 100, 10, 0, 0, 0)",
            (d.isoformat(), model),
        )
        conn.commit()


def _counts(repo) -> tuple[int, int]:
    with repo.db.get_connection() as conn:
        a = conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]
        b = conn.execute("SELECT COUNT(*) FROM token_usage_daily").fetchone()[0]
    return a, b


def test_clear_all_empties_both_tables(repo):
    today = date.today()
    _raw(repo, _ts_ms(today))
    _raw(repo, _ts_ms(today - timedelta(days=1)))
    _daily(repo, today - timedelta(days=9))
    assert _counts(repo) == (2, 1)

    out = repo.clear_all()
    assert out["token_usage"] == 2
    assert out["token_usage_daily"] == 1
    # 全删时没有「按天多删」这回事，故 from_date 为 None
    assert out["daily_from_date"] is None
    assert _counts(repo) == (0, 0)


def test_clear_since_keeps_older_rows(repo):
    today = date.today()
    old = today - timedelta(days=10)
    _raw(repo, _ts_ms(old))            # 早于边界 → 留
    _raw(repo, _ts_ms(today))          # 晚于边界 → 删
    _daily(repo, old)                  # 早于边界那天 → 留
    _daily(repo, today)                # 边界当天 → 删

    out = repo.clear_since(_ts_ms(today, hour=0))
    assert out["token_usage"] == 1
    assert out["token_usage_daily"] == 1
    assert out["daily_from_date"] == today.isoformat()
    assert _counts(repo) == (1, 1)


def test_boundary_is_inclusive(repo):
    """恰好等于 since 的那一行必须被删——差一个 = 会留下一条本该消失的记录。"""
    ts = _ts_ms(date.today(), hour=8)
    _raw(repo, ts)
    _raw(repo, ts - 1)
    out = repo.clear_since(ts)
    assert out["token_usage"] == 1
    assert _counts(repo)[0] == 1


def test_daily_granularity_overdeletes_within_the_boundary_day(repo):
    """日表只有天粒度：跨天范围会连带删掉 since 之前、同一天里的记录。

    这不是 bug 而是日聚合的固有精度损失（那些行的原始时间戳在 rollup 时已不存在）。
    本测试把「如实多删 + 用 daily_from_date 报告」这个行为固定住——若哪天有人
    改成「只删 since 之后的天」，边界当天的数据就会残留，界面上表现为清了却还有数。
    """
    today = date.today()
    _daily(repo, today)                        # 边界当天的整天聚合
    _daily(repo, today - timedelta(days=1))    # 前一天

    # since = 今天 20:00 —— 意图只删今天晚上的，但日表只能整天删
    out = repo.clear_since(_ts_ms(today, hour=20))
    assert out["token_usage_daily"] == 1
    assert out["daily_from_date"] == today.isoformat()
    # 前一天必须留住
    with repo.db.get_connection() as conn:
        left = [r[0] for r in conn.execute("SELECT date FROM token_usage_daily")]
    assert left == [(today - timedelta(days=1)).isoformat()]


def test_clear_all_delegates_to_clear_since_none(repo):
    """clear_all 只是 clear_since(None) 的别名——两条路径不该各自维护一份 SQL。"""
    _raw(repo, _ts_ms(date.today()))
    _daily(repo, date.today())
    a = repo.clear_since(None)
    _raw(repo, _ts_ms(date.today()))
    _daily(repo, date.today())
    b = repo.clear_all()
    assert a == b
