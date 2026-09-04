# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""TokenUsageRepo 的清空与日表日期区间, 跑在真实 SQLite 文件上.

按范围清空是**不可逆**操作,这里钉住的都是「多删」或「少删」会静默发生的地方:

- 全删(None)两表都空
- 按时间删只动边界之后的行,之前的留住
- **日表只有天粒度**:跨天范围会连带删掉 since 之前、同一天里的记录。
  这是日聚合的精度损失、SQL 绕不过去,所以要么如实返回 daily_from_date 让上层说明,
  要么就是在悄悄多删——本测试固定住「如实多删并报告」这个行为。
- 两表同一事务:只清一张会让总量与明细对不上且不报错
- 边界是闭区间 >=:恰好等于 since 的那一行要被删

另有两条服务于确认窗那句「连带删除某天」——它说不说由日表的日期区间决定:

- daily_date_range 返回表里的最早与最新日期,空表两个都是 None
- 滚存后日表的最新日期必然早于昨天,即「近 24 小时」那档的边界日不可能在表里
  (这是那句提示能被条件化的前提;cutoff 的天对齐语义由 test_token_usage_rollup.py
  钉着,不在这条里)

还有一条服务于 insert 自己:两个 token 列建表是 NOT NULL,而上游发来的 usage 里那些
数字字段可能是 null(按量计费未结算、流式收尾的 chunk),取值写法只兜「键缺席」就会把
None 绑进去、抛 IntegrityError 并被 fire_record 兜成一行 warning——那一笔静默丢失。
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


# ── from_date：界面承诺的那一天，必须就是真被删的那一天 ──────────────────


def test_from_date_overrides_local_derivation(repo):
    """给了 from_date 就以它为日表边界，不再按本机时区推算。

    日表的 date 是按**本机时区**写的，而界面那句「某天更早的记录会被连带删除」
    是浏览器按**它自己的时区**算的。盒子跑 UTC、手机在 +08 时两者差一天，且差错
    的方向可能是「实际删的比说的更多」——这正是那句提示要防的事。
    """
    today = date.today()
    for d in (today, today - timedelta(days=1), today - timedelta(days=2)):
        _daily(repo, d)
    since = _ts_ms(today)  # 本机推算 = 今天

    out = repo.clear_since(since, from_date=(today - timedelta(days=1)).isoformat())

    assert out["daily_from_date"] == (today - timedelta(days=1)).isoformat()
    # 按界面说的那天删：今天与昨天没了，前天还在
    _, daily = _counts(repo)
    assert daily == 1


def test_from_date_absent_keeps_local_derivation(repo):
    """不给就按本机推算——老客户端的行为一个字都不变。"""
    today = date.today()
    _daily(repo, today)
    _daily(repo, today - timedelta(days=1))
    out = repo.clear_since(_ts_ms(today))
    assert out["daily_from_date"] == today.isoformat()
    assert _counts(repo)[1] == 1


def test_from_date_beyond_one_day_is_rejected(repo):
    """只接受时区差那一天的偏移，多了就报错——这个入口不能被用来任意扩大范围。"""
    today = date.today()
    _daily(repo, today)
    _daily(repo, today - timedelta(days=5))
    with pytest.raises(ValueError, match="相差超过一天"):
        repo.clear_since(_ts_ms(today), from_date=(today - timedelta(days=5)).isoformat())
    assert _counts(repo)[1] == 2  # 报错后一行都不许少


def test_from_date_malformed_is_rejected(repo):
    """格式不对直接报错，绝不当成「没给」而按自己的时区悄悄改删别的一天。"""
    _daily(repo, date.today())
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        repo.clear_since(_ts_ms(date.today()), from_date="2026/08/24")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        repo.clear_since(_ts_ms(date.today()), from_date="")
    assert _counts(repo)[1] == 1


def test_insert_tolerates_null_number_fields_in_usage(repo):
    """usage 里的数字字段为 null 时也得落库,不能静默丢掉这一笔。

    两个 token 列建表是 NOT NULL(DEFAULT 只在整列被省略时生效,显式绑 NULL 一律
    IntegrityError),而上游在按量计费未结算、或流式收尾那个 chunk 里会发半空的 usage。
    取值写成 `.get(k, 0)` 只兜键缺席、兜不住值为 null;改回去这条会红——落库直接抛
    IntegrityError,被 fire_record 吞成一行 warning,界面上只是少算、无从察觉。
    """
    repo.insert(
        "m-null",
        "https://a/v1",
        {"prompt_tokens": None, "completion_tokens": 12, "prompt_tokens_details": None},
        "realtime",
    )
    with repo.db.get_connection() as conn:
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cache_tokens FROM token_usage "
            "WHERE model = 'm-null'"
        ).fetchone()
    assert row is not None, "usage 里有 null 字段时这一笔被丢掉了"
    assert tuple(row) == (0, 12, 0), f"null 字段没归零,落库成了 {tuple(row)}"


def test_from_date_compact_form_still_deletes_the_daily_row(repo):
    """紧凑写法过了闸门也必须真删到日表——校验用的值与拼进 SQL 的值得是同一个。

    3.11 起 date.fromisoformat 也收 "20260824"，它能过「与推算日相差不超过一天」那道
    闸门；而日表的 date 列是定宽 YYYY-MM-DD、按字典序比，"-"(0x2D) < "0"(0x30) 使
    'date >= "20260824"' 恒命中 0 行。归一化若被拿掉，这条会红：实时表清了、日表没清，
    留下的正是本文件开头那段说「不会有任何报错」的半清状态。
    """
    today = date.today()
    _daily(repo, today)
    _raw(repo, _ts_ms(today))
    out = repo.clear_since(_ts_ms(today, hour=0), from_date=today.isoformat().replace("-", ""))
    assert out["token_usage_daily"] == 1, "紧凑写法过了闸门，日表却一行没删"
    assert out["daily_from_date"] == today.isoformat(), "回报给上层的边界日也得是归一化后的"
    assert _counts(repo) == (0, 0), f"两表都该清空，实际剩 {_counts(repo)}"


def test_from_date_without_since_is_rejected(repo):
    """全清没有「从哪天起」这回事，给了 from_date 说明调用方想错了。"""
    _daily(repo, date.today())
    with pytest.raises(ValueError, match="只在给了 since_ms"):
        repo.clear_since(None, from_date=date.today().isoformat())
    assert _counts(repo)[1] == 1


def test_from_date_combines_with_target(repo):
    """定点 + 时间范围 + 界面给的那一天，三者同时生效。"""
    today = date.today()
    y = today - timedelta(days=1)
    _daily_at(repo, y, "m1", "https://a/v1")
    _daily_at(repo, y, "m1", "https://b/v1")
    _daily_at(repo, today - timedelta(days=3), "m1", "https://a/v1")

    out = repo.clear_since(
        _ts_ms(today), model="m1", base_url="https://a/v1", from_date=y.isoformat()
    )
    assert out["token_usage_daily"] == 1
    _, daily = _left(repo)
    assert sorted(daily) == [("m1", "https://a/v1"), ("m1", "https://b/v1")]


def test_daily_date_range_reports_both_ends(repo):
    """daily_date_range 给出日表里的最早与最新日期；空表为 (None, None)。

    界面据此决定「清到某一天会不会连带删掉那天更早的记录」这句提示说不说——两头
    各挡一类落空：上界挡「边界日还没滚进日表」，下界挡「边界日早于表里最早那天」。
    """
    assert repo.daily_date_range() == (None, None)  # 空表：界面一律不说

    today = date.today()
    _daily(repo, today - timedelta(days=9))
    _daily(repo, today - timedelta(days=4), model="m2")
    _daily(repo, today - timedelta(days=7), model="m3")
    assert repo.daily_date_range() == (
        (today - timedelta(days=9)).isoformat(),
        (today - timedelta(days=4)).isoformat(),
    )


def test_rollup_keeps_daily_table_behind_yesterday(repo):
    """滚存后日表的最新日期必然早于昨天——「近 24 小时」那档的边界日不可能在表里。

    这是确认窗那句提示能被条件化的前提：滚存截止是天对齐的 today-_RETENTION_DAYS，
    且只搬 timestamp < cutoff 的行，所以最新能进日表的是 today-_RETENTION_DAYS-1。
    它钉的是这个前提本身，钉不住 cutoff 的具体语义——用例里够老到能进日表的只有
    today-9 那条，无论 cutoff 天对齐与否、用 < 还是 <=，latest 都是它。天对齐由
    test_token_usage_rollup.py::test_cutoff_is_day_aligned 钉着。
    """
    today = date.today()
    # 一条足够老的事件（会被滚走）+ 一条今天的（必须留在实时表）
    _raw(repo, _ts_ms(today - timedelta(days=9)))
    _raw(repo, _ts_ms(today))
    # usage 走的是 API 字段名（prompt_tokens / completion_tokens），不是库列名——
    # 写成列名不会报错，那条事件会被静默记成 0/0。
    repo.insert(
        "m1", "https://a/v1", {"prompt_tokens": 1, "completion_tokens": 1}, "realtime"
    )
    with repo.db.get_connection() as conn:
        row = conn.execute(
            "SELECT input_tokens, output_tokens FROM token_usage "
            "WHERE model = 'm1' AND base_url = 'https://a/v1'"
        ).fetchone()
    assert tuple(row) == (1, 1), f"insert 的 usage 字段名没对上，落库成了 {tuple(row)}"

    _, latest = repo.daily_date_range()
    assert latest is not None, "老事件应已滚进日表"
    assert latest < (today - timedelta(days=1)).isoformat(), (
        f"日表最新日期 {latest} 不该晚于前天——否则近 24 小时档的提示又会恒为真"
    )
