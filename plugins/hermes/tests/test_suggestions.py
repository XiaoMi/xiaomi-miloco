import os

import pytest

from hermes import suggestions as s


D6_10 = "2026-06-06T10:00:00+08:00"
D6_23 = "2026-06-06T23:50:00+08:00"
D6_0730 = "2026-06-06T07:30:00+08:00"
D7_10 = "2026-06-07T10:00:00+08:00"
D14_10 = "2026-06-14T10:00:00+08:00"


def record(key, habit, suggestion, now=D6_10):
    return s.apply_habit_action(
        {"action": "record", "key": key, "subject": "shared", "habit": habit,
         "suggestion": suggestion},
        now,
    )


def list_now(now=D6_10):
    return s.apply_habit_action({"action": "list"}, now)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))
    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
    return home


def _entry(l, key):
    for e in l["entries"]:
        if e["key"] == key:
            return e
    return None


def test_habit_suggestions_path(_isolate):
    assert s.habit_suggestions_path() == (
        _isolate / "home-profile" / "task-suggestions.json"
    )


def test_local_date_key_crosses_utc_day_boundary():
    assert s.local_date_key(D6_0730) == "2026-06-06"
    assert s.local_date_key(D6_23) == "2026-06-06"


def test_record_creates_pending_and_echoes_key():
    r = record("wl_sleep_dim", "23点睡觉", "睡觉时把台灯调暗")
    assert r["ok"] is True
    assert r["status"] == "pending"
    assert r["deduped"] is False
    assert r["key"] == "wl_sleep_dim"


def test_record_same_key_dedupes_no_duplicate():
    record("wl_sleep_dim", "23点睡觉", "睡觉时把台灯调暗")
    r2 = record("wl_sleep_dim", "每晚23点入睡", "睡觉调暗灯")
    assert r2["deduped"] is True
    l = list_now()
    assert l["counts"].get("pending") == 1


def test_record_missing_fields_fails():
    no_key = s.apply_habit_action(
        {"action": "record", "subject": "shared", "habit": "x", "suggestion": "y"},
        D6_10,
    )
    assert no_key["ok"] is False
    no_habit = s.apply_habit_action(
        {"action": "record", "key": "k1", "suggestion": "y"}, D6_10
    )
    assert no_habit["ok"] is False


def test_rejected_key_not_revived_on_record():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10)
    s.apply_habit_action(
        {"action": "resolve", "key": "wl_sleep_dim", "outcome": "rejected"}, D6_10
    )
    again = record("wl_sleep_dim", "每晚23点睡觉习惯", "睡觉调暗灯", D7_10)
    assert again["deduped"] is True
    assert again["status"] == "rejected"
    l = list_now(D7_10)
    assert l["counts"].get("rejected") == 1
    assert l["counts"].get("pending", 0) == 0


def test_list_entries_exposes_all_including_terminal():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10)
    s.apply_habit_action(
        {"action": "resolve", "key": "wl_sleep_dim", "outcome": "rejected"}, D6_10
    )
    l = list_now(D6_10)
    assert len(l["entries"]) == 1
    assert _entry(l, "wl_sleep_dim")["status"] == "rejected"


def test_open_slot_blocks_second_mark_asked():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    record("zx_whitenoise", "睡前听白噪音", "睡觉放白噪音")
    assert list_now()["can_ask_now"] is True

    ask = s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10)
    assert ask["ok"] is True
    assert ask["status"] == "asked"

    assert list_now()["can_ask_now"] is False
    ask2 = s.apply_habit_action(
        {"action": "mark_asked", "key": "zx_whitenoise"}, D6_10
    )
    assert ask2["ok"] is False


def test_cross_day_max_one_new_per_day():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    record("zx_whitenoise", "睡前听白噪音", "睡觉放白噪音")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10)
    s.apply_habit_action(
        {"action": "resolve", "key": "wl_sleep_dim", "outcome": "rejected"}, D6_23
    )

    assert list_now(D6_23)["can_ask_now"] is False
    ask2 = s.apply_habit_action(
        {"action": "mark_asked", "key": "zx_whitenoise"}, D6_23
    )
    assert ask2["ok"] is False

    assert list_now(D7_10)["can_ask_now"] is True
    ask3 = s.apply_habit_action(
        {"action": "mark_asked", "key": "zx_whitenoise"}, D7_10
    )
    assert ask3["ok"] is True


def test_mark_asked_only_from_pending():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10)
    again = s.apply_habit_action(
        {"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10
    )
    assert again["ok"] is False


def test_resolve_accepted_then_created():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_gym"}, D6_10)

    acc = s.apply_habit_action(
        {"action": "resolve", "key": "wl_gym", "outcome": "accepted"}, D6_10
    )
    assert acc["ok"] is True
    assert acc["status"] == "accepted"

    created = s.apply_habit_action(
        {"action": "resolve", "key": "wl_gym", "outcome": "created",
         "task_id": "gym_music"},
        D6_10,
    )
    assert created["ok"] is True
    assert created["status"] == "created"
    assert created["task_id"] == "gym_music"


def test_created_cannot_be_rejected():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_gym"}, D6_10)
    s.apply_habit_action(
        {"action": "resolve", "key": "wl_gym", "outcome": "created",
         "task_id": "gym_music"},
        D6_10,
    )
    bad = s.apply_habit_action(
        {"action": "resolve", "key": "wl_gym", "outcome": "rejected"}, D6_10
    )
    assert bad["ok"] is False


def test_resolve_unknown_key_fails():
    r = s.apply_habit_action(
        {"action": "resolve", "key": "nope", "outcome": "accepted"}, D6_10
    )
    assert r["ok"] is False


def test_pending_to_created_blocked():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    bad = s.apply_habit_action(
        {"action": "resolve", "key": "wl_gym", "outcome": "created", "task_id": "x"},
        D6_10,
    )
    assert bad["ok"] is False
    assert bad["status"] == "pending"


def test_asked_direct_created_allowed():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_gym"}, D6_10)
    created = s.apply_habit_action(
        {"action": "resolve", "key": "wl_gym", "outcome": "created", "task_id": "gym"},
        D6_10,
    )
    assert created["ok"] is True
    assert created["status"] == "created"


def test_pending_direct_accepted_blocked():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    bad = s.apply_habit_action(
        {"action": "resolve", "key": "wl_gym", "outcome": "accepted"}, D6_10
    )
    assert bad["ok"] is False
    assert bad["status"] == "pending"


def test_asked_expires_after_7_days_releases_slot():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10)

    l = list_now(D14_10)
    assert l["counts"].get("expired") == 1
    assert l["counts"].get("asked", 0) == 0
    assert l["can_ask_now"] is True


def test_expired_revives_on_record():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10)
    list_now(D14_10)

    revived = record("wl_sleep_dim", "每晚23点睡觉", "睡觉调暗灯", D14_10)
    assert revived["status"] == "pending"
    assert revived["revived"] is True
    counts = list_now(D14_10)["counts"]
    assert counts.get("pending") == 1
    assert counts.get("expired", 0) == 0

    ask = s.apply_habit_action(
        {"action": "mark_asked", "key": "wl_sleep_dim"}, D14_10
    )
    assert ask["ok"] is True
    assert ask["status"] == "asked"


def test_accepted_expires_after_7_days():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_gym"}, D6_10)
    s.apply_habit_action(
        {"action": "resolve", "key": "wl_gym", "outcome": "accepted"}, D6_10
    )

    l = list_now(D14_10)
    assert l["counts"].get("expired") == 1
    assert l["counts"].get("accepted", 0) == 0
    assert l["can_ask_now"] is True

    revived = record("wl_gym", "傍晚健身", "健身时放运动歌单", D14_10)
    assert revived["status"] == "pending"
    assert revived["revived"] is True


def test_three_asks_then_give_up():
    D22 = "2026-06-22T10:00:00+08:00"
    D30 = "2026-06-30T10:00:00+08:00"
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")

    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10)
    r = record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯", D14_10)
    assert r["revived"] is True

    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D14_10)
    r = record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯", D22)
    assert r["revived"] is True

    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D22)
    r = record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯", D30)
    assert "revived" not in r
    assert r["status"] == "expired"

    counts = list_now(D30)["counts"]
    assert counts.get("expired") == 1
    assert counts.get("pending", 0) == 0


def test_load_open_questions_returns_unstale_only():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    s.apply_habit_action({"action": "mark_asked", "key": "wl_sleep_dim"}, D6_10)
    assert len(s.load_open_questions(D6_23)) == 1
    assert len(s.load_open_questions(D14_10)) == 0


def test_concurrent_record_all_persist():
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [
            ex.submit(record, f"habit_{i}", f"习惯编号{i}做某事", f"任务点子{i}")
            for i in range(10)
        ]
        for f in futures:
            f.result()
    assert list_now()["counts"].get("pending") == 10


def test_item_id_recorded_and_preserved_after_created():
    s.apply_habit_action(
        {
            "action": "record",
            "key": "wl_fitness",
            "subject": "王磊",
            "habit": "傍晚约19点健身",
            "suggestion": "健身时自动放运动歌单",
            "item_id": "p-abc123",
        },
        D6_10,
    )
    assert _entry(list_now(), "wl_fitness")["item_id"] == "p-abc123"

    s.apply_habit_action({"action": "mark_asked", "key": "wl_fitness"}, D6_10)
    s.apply_habit_action(
        {"action": "resolve", "key": "wl_fitness", "outcome": "created",
         "task_id": "t1"},
        D6_10,
    )
    assert _entry(list_now(D7_10), "wl_fitness")["item_id"] == "p-abc123"


def test_item_id_refreshed_on_revive():
    s.apply_habit_action(
        {
            "action": "record",
            "key": "wl_water",
            "subject": "王磊",
            "habit": "下午喝水",
            "suggestion": "提醒喝水",
            "item_id": "p-old",
        },
        D6_10,
    )
    s.apply_habit_action({"action": "mark_asked", "key": "wl_water"}, D6_10)
    s.apply_habit_action(
        {
            "action": "record",
            "key": "wl_water",
            "subject": "王磊",
            "habit": "下午喝水",
            "suggestion": "提醒喝水",
            "item_id": "p-new",
        },
        D14_10,
    )
    assert _entry(list_now(D14_10), "wl_water")["item_id"] == "p-new"
