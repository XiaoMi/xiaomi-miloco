"""tools_habit 防骚扰状态机测试。

对标 openclaw ``habit-suggest.test.ts``（24 条），覆盖：
- record：幂等去重 / 拒绝不复活 / 过期复活 / MAX_ASKS
- mark_asked：防骚扰闸门 / 状态校验 / ask_count
- resolve：accepted → created / rejected / 非法跳转
- expiry：惰性过期 / 复活 / 永久放弃
- list：counts / can_ask_now
- 并发写入安全
- _local_date_key / _to_timestamp 边界
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from miloco_plugin_pkg import tools_habit as th

# ── ISO 常量（与 OpenClaw 测试对齐，+08:00 时区） ──────────────────────────
D6_10 = "2026-06-06T10:00:00+08:00"
D6_23 = "2026-06-06T23:50:00+08:00"
D6_0730 = "2026-06-06T07:30:00+08:00"  # UTC 前一天 23:30
D7_10 = "2026-06-07T10:00:00+08:00"
D14_10 = "2026-06-14T10:00:00+08:00"  # D6 后第 8 天

HABIT = "23点入睡"
SUGGEST = "睡觉时把台灯调暗"


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """临时 store 目录，隔离真实文件。"""
    path = tmp_path / "home-profile"
    path.mkdir(parents=True)
    monkeypatch.setattr(th, "miloco_home", lambda: tmp_path)
    return path


def _record(key, habit=None, suggestion=None, now=D6_10, **kw):
    return th.apply_habit_action(
        {"action": "record", "key": key, "habit": habit or HABIT,
         "suggestion": suggestion or SUGGEST, **kw},
        now,
    )


def _list(now=D6_10):
    return th.apply_habit_action({"action": "list"}, now)


def _mark_asked(key, now=D6_10):
    return th.apply_habit_action({"action": "mark_asked", "key": key}, now)


def _resolve(key, outcome, now=D6_10, **kw):
    return th.apply_habit_action({"action": "resolve", "key": key, "outcome": outcome, **kw}, now)


# ── _local_date_key ─────────────────────────────────────────────────────────

def test_local_date_key_crosses_utc_midnight():
    """07:30+08:00 在 UTC 是前一天 23:30，日历日应为 06-06。"""
    assert th._local_date_key(D6_0730) == "2026-06-06"
    assert th._local_date_key(D6_23) == "2026-06-06"


def test_local_date_key_empty():
    assert th._local_date_key("") == ""
    assert th._local_date_key("garbage") == ""


# ── _to_timestamp ───────────────────────────────────────────────────────────

def test_to_timestamp_number():
    assert th._to_timestamp(1700000000000) == 1700000000000


def test_to_timestamp_iso():
    ts = th._to_timestamp("2026-06-06T10:00:00+08:00")
    assert ts > 0
    assert th._to_timestamp(None) == 0
    assert th._to_timestamp("garbage") == 0


# ── record：幂等去重 ────────────────────────────────────────────────────────

def test_first_record_creates_pending_and_returns_key(tmp_store):
    r = _record("wl_sleep_dim")
    assert r["ok"] is True
    assert r["status"] == "pending"
    assert r["deduped"] is False
    assert r["key"] == "wl_sleep_dim"


def test_same_key_dup_record_deduped_not_new(tmp_store):
    _record("wl_sleep_dim")
    r2 = _record("wl_sleep_dim", "23点入睡", "睡觉调暗灯")
    assert r2["deduped"] is True
    l = _list()
    assert l["counts"]["pending"] == 1


def test_missing_key_or_habit_returns_false(tmp_store):
    no_key = th.apply_habit_action(
        {"action": "record", "habit": "x", "suggestion": "y"}, D6_10)
    assert no_key["ok"] is False
    no_habit = th.apply_habit_action(
        {"action": "record", "key": "k1", "suggestion": "y"}, D6_10)
    assert no_habit["ok"] is False


def test_record_unknown_action(tmp_store):
    r = th.apply_habit_action({"action": "unknown"}, D6_10)
    assert r["ok"] is False


# ── 拒绝不再复活 ─────────────────────────────────────────────────────────────

def test_rejected_key_never_revives(tmp_store):
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    _resolve("wl_sleep_dim", "rejected")
    r = _record("wl_sleep_dim")
    assert r["deduped"] is True
    assert r["status"] == "rejected"
    assert "永久不再推荐" in r["note"]


# ── 过期复活 + MAX_ASKS ─────────────────────────────────────────────────────

def test_expired_key_revives_as_pending(tmp_store):
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    # 跳到 D14（8 天后），惰性过期
    l1 = _list(D14_10)
    assert any(e["status"] == "expired" for e in l1["entries"])
    # 复活
    r = _record("wl_sleep_dim", now=D14_10)
    assert r["revived"] is True
    assert r["status"] == "pending"


def test_expired_after_max_asks_gives_up(tmp_store):
    """累计问满 MAX_ASKS 次仍无果 → 永久放弃。每次需在过期后重新 record+mark_asked。"""
    base = "2026-06-01T10:00:00+08:00"
    for i in range(th.MAX_ASKS):
        day = f"2026-06-{1 + i*8:02d}T10:00:00+08:00"  # D1, D9, D17
        _record("wl_sleep_dim", now=day)
        _mark_asked("wl_sleep_dim", now=day)
        # let it expire by running list on day+8
        expire_day = f"2026-06-{9 + i*8:02d}T10:00:00+08:00"
        _list(expire_day)
    final_day = f"2026-06-{1 + th.MAX_ASKS * 8:02d}T10:00:00+08:00"
    r = _record("wl_sleep_dim", now=final_day)
    assert r["deduped"] is True
    assert "放弃" in r.get("note", "")


# ── created 终态不再推荐 ────────────────────────────────────────────────────

def test_created_key_never_revives(tmp_store):
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    _resolve("wl_sleep_dim", "accepted")
    _resolve("wl_sleep_dim", "created", task_id="task-1")
    r = _record("wl_sleep_dim")
    assert r["deduped"] is True
    assert r["status"] == "created"


# ── can_ask_now 闸门 ────────────────────────────────────────────────────────

def test_can_ask_now_allows_first(tmp_store):
    assert th.can_ask_now(th._load_store(), D6_10)["can"] is True


def test_can_ask_now_blocks_after_asked_today(tmp_store):
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    assert th.can_ask_now(th._load_store(), D6_10)["can"] is False


def test_can_ask_now_opens_next_day(tmp_store):
    """旧询问过期后，次日可以发起新询问。"""
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim", now=D6_10)
    # let it expire
    _list(D14_10)
    # 过期后 can_ask_now 恢复
    assert th.can_ask_now(th._load_store(), D14_10)["can"] is True


# ── mark_asked ───────────────────────────────────────────────────────────────

def test_mark_asked_missing_key(tmp_store):
    r = _mark_asked("nonexistent")
    assert r["ok"] is False


def test_mark_asked_wrong_status(tmp_store):
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    r = _mark_asked("wl_sleep_dim")
    assert r["ok"] is False


def test_mark_asked_gate_blocked(tmp_store):
    _record("k1")
    _mark_asked("k1")
    _record("k2")
    r = _mark_asked("k2")
    assert r["ok"] is False


def test_mark_asked_increments_count(tmp_store):
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    store = th._load_store()
    e = next(x for x in store["entries"] if x["key"] == "wl_sleep_dim")
    assert e["ask_count"] == 1


# ── resolve ─────────────────────────────────────────────────────────────────

def test_resolve_missing_key(tmp_store):
    assert _resolve("nonexistent", "rejected")["ok"] is False


def test_resolve_rejected(tmp_store):
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    r = _resolve("wl_sleep_dim", "rejected")
    assert r["ok"] is True
    assert r["status"] == "rejected"


def test_resolve_rejected_from_expired_not_allowed(tmp_store):
    """expired 状态不可再 reject（与 created 同为不可逆终态）。"""
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    _list(D14_10)  # trigger expiry
    r = _resolve("wl_sleep_dim", "rejected", now=D14_10)
    assert r["ok"] is False


def test_resolve_accepted_wrong_status(tmp_store):
    _record("wl_sleep_dim")
    r = _resolve("wl_sleep_dim", "accepted")  # not asked yet
    assert r["ok"] is False


def test_resolve_created_from_accepted(tmp_store):
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    _resolve("wl_sleep_dim", "accepted")
    r = _resolve("wl_sleep_dim", "created", task_id="task-1")
    assert r["ok"] is True
    assert r["status"] == "created"
    assert r["task_id"] == "task-1"


def test_resolve_created_from_asked_direct(tmp_store):
    _record("wl_sleep_dim")
    _mark_asked("wl_sleep_dim")
    r = _resolve("wl_sleep_dim", "created", task_id="task-1")
    assert r["ok"] is True
    assert r["status"] == "created"


def test_resolve_created_wrong_status(tmp_store):
    _record("wl_sleep_dim")
    r = _resolve("wl_sleep_dim", "created")  # not asked/accepted
    assert r["ok"] is False


# ── list ────────────────────────────────────────────────────────────────────

def test_list_counts_by_status(tmp_store):
    _record("k1")
    _record("k2")
    _mark_asked("k1")
    l = _list()
    assert l["counts"]["pending"] >= 1
    assert l["counts"]["asked"] == 1


def test_list_can_ask_now(tmp_store):
    l = _list()
    assert l["can_ask_now"] is True


def test_list_entries_structure(tmp_store):
    _record("wl_sleep_dim", "23点睡", "调暗灯", subject="shared", evidence="log", item_id="abc")
    l = _list()
    e = l["entries"][0]
    assert e["key"] == "wl_sleep_dim"
    assert e["habit"] == "23点睡"
    assert e["status"] == "pending"
    assert "created_at" not in e  # _view 只导出一组字段


# ── 并发写入安全 ────────────────────────────────────────────────────────────

def test_concurrent_records_threadsafe(tmp_store):
    errors = []

    def do(i):
        try:
            _record(f"k{i}", now=D6_10)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=do, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    l = _list()
    assert l["counts"]["pending"] >= 20


# ── 访问路径 ────────────────────────────────────────────────────────────────

def test_habit_suggestions_path(tmp_store):
    p = th._habit_suggestions_path()
    assert p.name == "task-suggestions.json"
    assert p.parent.name == "home-profile"
