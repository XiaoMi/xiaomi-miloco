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


# ── 按「模型名 + Base URL」定点清除 ─────────────────────────────────


def _raw_at(repo, ts_ms: int, model: str, base_url: str) -> None:
    with repo.db.get_connection() as conn:
        conn.execute(
            "INSERT INTO token_usage (timestamp, model, base_url, type, input_tokens,"
            " output_tokens, cache_tokens, video_tokens, audio_tokens, created_at)"
            " VALUES (?, ?, ?, 'realtime', 100, 10, 0, 0, 0, ?)",
            (ts_ms, model, base_url, ts_ms),
        )
        conn.commit()


def _daily_at(repo, d: date, model: str, base_url: str) -> None:
    with repo.db.get_connection() as conn:
        conn.execute(
            "INSERT INTO token_usage_daily (date, model, base_url, type, calls,"
            " input_tokens, output_tokens, cache_tokens, video_tokens, audio_tokens)"
            " VALUES (?, ?, ?, 'realtime', 1, 100, 10, 0, 0, 0)",
            (d.isoformat(), model, base_url),
        )
        conn.commit()


def _left(repo) -> tuple[list, list]:
    with repo.db.get_connection() as conn:
        live = [(r[0], r[1]) for r in conn.execute(
            "SELECT model, base_url FROM token_usage ORDER BY model, base_url")]
        daily = [(r[0], r[1]) for r in conn.execute(
            "SELECT model, base_url FROM token_usage_daily ORDER BY model, base_url")]
    return live, daily


def test_per_target_delete_touches_nothing_else(repo):
    """删一个 (模型, endpoint) 不能碰到别的模型，也不能碰到同名的另一个 endpoint。"""
    today = date.today()
    ts = _ts_ms(today)
    _raw_at(repo, ts, "mimo-v2.5", "https://a/v1")
    _raw_at(repo, ts, "mimo-v2.5", "https://a/v1-test")   # 同名、不同 endpoint
    _raw_at(repo, ts, "other", "https://a/v1")            # 同 endpoint、不同模型
    _daily_at(repo, today, "mimo-v2.5", "https://a/v1")
    _daily_at(repo, today, "mimo-v2.5", "https://a/v1-test")

    out = repo.clear_since(None, model="mimo-v2.5", base_url="https://a/v1")
    assert out["token_usage"] == 1
    assert out["token_usage_daily"] == 1

    live, daily = _left(repo)
    assert live == [("mimo-v2.5", "https://a/v1-test"), ("other", "https://a/v1")]
    assert daily == [("mimo-v2.5", "https://a/v1-test")]


def test_empty_base_url_is_targeted_by_value_not_by_truthiness(repo):
    """``base_url=""`` 是有意义的取值（v3 之前的老数据），必须按值精确命中。

    若实现用真值判断（``if base_url:``），空串会被当成「不限 endpoint」，
    于是这次调用会把该模型**所有** endpoint 的数据一起删掉——静默多删。
    """
    today = date.today()
    ts = _ts_ms(today)
    _raw_at(repo, ts, "mimo-v2.5", "")                 # 老数据
    _raw_at(repo, ts, "mimo-v2.5", "https://a/v1")     # 新数据，必须留下
    _daily_at(repo, today, "mimo-v2.5", "")
    _daily_at(repo, today, "mimo-v2.5", "https://a/v1")

    out = repo.clear_since(None, model="mimo-v2.5", base_url="")
    assert out["token_usage"] == 1
    assert out["token_usage_daily"] == 1

    live, daily = _left(repo)
    assert live == [("mimo-v2.5", "https://a/v1")]
    assert daily == [("mimo-v2.5", "https://a/v1")]


def test_target_and_range_combine(repo):
    """时间范围与目标同时生效：只删这一项在该时段内的记录。"""
    today = date.today()
    _raw_at(repo, _ts_ms(today, 20), "m", "https://a/v1")      # 边界之后 → 删
    _raw_at(repo, _ts_ms(today, 8), "m", "https://a/v1")       # 边界之前 → 留
    _raw_at(repo, _ts_ms(today, 20), "m", "https://b/v1")      # 另一 endpoint → 留

    out = repo.clear_since(_ts_ms(today, 12), model="m", base_url="https://a/v1")
    assert out["token_usage"] == 1
    with repo.db.get_connection() as conn:
        rest = sorted(
            (r[0], r[1]) for r in conn.execute("SELECT base_url, timestamp FROM token_usage")
        )
    assert [r[0] for r in rest] == ["https://a/v1", "https://b/v1"]


def test_model_and_base_url_must_come_together(repo):
    """只给一半是调用方 bug：宁可报错，也不要按「不限 endpoint」多删。"""
    _raw_at(repo, _ts_ms(date.today()), "m", "https://a/v1")
    with pytest.raises(ValueError, match="必须同时"):
        repo.clear_since(None, model="m")
    with pytest.raises(ValueError, match="必须同时"):
        repo.clear_since(None, base_url="https://a/v1")
    # 报错后一行都不该少
    assert _counts(repo)[0] == 1


def test_clear_all_still_ignores_target(repo):
    """clear_all 依旧是全清，不受新参数影响。"""
    ts = _ts_ms(date.today())
    _raw_at(repo, ts, "a", "https://x/v1")
    _raw_at(repo, ts, "b", "")
    out = repo.clear_all()
    assert out["token_usage"] == 2
    assert _counts(repo) == (0, 0)


def test_empty_model_name_is_also_targeted_by_value(repo):
    """``model=""`` 同样要按值精确命中，不能被真值判断跳过。

    空模型名不是假想：曾出现过模型名打到一半就触发请求的记录（幽灵行）。
    若实现写成 ``if model:``，传空模型名时过滤条件会被整个跳过，
    这次调用就退化成「只按时间删」——把所有模型一起删掉，且不会报错。
    """
    ts = _ts_ms(date.today())
    _raw_at(repo, ts, "", "")                       # 空模型名 + 空 endpoint
    _raw_at(repo, ts, "mimo-v2.5", "https://a/v1")  # 必须留下
    _raw_at(repo, ts, "mimo-v2.5", "")              # 必须留下

    out = repo.clear_since(None, model="", base_url="")
    assert out["token_usage"] == 1
    live, _ = _left(repo)
    assert live == [("mimo-v2.5", ""), ("mimo-v2.5", "https://a/v1")]
