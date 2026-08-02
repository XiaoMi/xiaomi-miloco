"""habit 状态机测试（自 openclaw habit-suggest.test.ts 迁移）。

时间锁 Asia/Shanghai：所有 fixture ISO 用 +08:00 后缀，localDateKey/过期口径只在该部署下成立。
store 路径经 $MILOCO_HOME 重定向到 tmp。
"""

import json

import pytest

from miloco_cli import habit_store
from miloco_cli.habit_store import apply_habit_action, local_date_key, store_path

# 固定一组测试用 now（Asia/Shanghai +08:00）
D6_10 = "2026-06-06T10:00:00+08:00"
D6_23 = "2026-06-06T23:50:00+08:00"
D6_0730 = "2026-06-06T07:30:00+08:00"  # UTC 仍是 06-05，用于跨时区日界校验
D7_10 = "2026-06-07T10:00:00+08:00"
D14_10 = "2026-06-14T10:00:00+08:00"  # D6 之后第 8 天


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path / "miloco"))
    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")


def record(key, habit, suggestion, now=D6_10, **extra):
    params = {"key": key, "subject": "shared", "habit": habit, "suggestion": suggestion}
    params.update(extra)
    return apply_habit_action("record", params, now)


def mark_asked(key, now=D6_10):
    return apply_habit_action("mark_asked", {"key": key}, now)


def resolve(key, outcome, now=D6_10, **extra):
    params = {"key": key, "outcome": outcome}
    params.update(extra)
    return apply_habit_action("resolve", params, now)


def lst(now=D6_10):
    return apply_habit_action("list", now_override=now)


# ─── 路径 ──────────────────────────────────────────────────────────────────


def test_store_path(tmp_path):
    assert store_path() == tmp_path / "miloco" / "home-profile" / "task-suggestions.json"


# ─── localDateKey ───────────────────────────────────────────────────────────


def test_local_date_key_across_utc_boundary():
    # 07:30+08:00 在 UTC 是前一天 23:30，但上海日历日应为 06-06
    assert local_date_key(D6_0730) == "2026-06-06"
    assert local_date_key(D6_23) == "2026-06-06"


# ─── record：幂等 / 拒绝不复活 ───────────────────────────────────────────────


def test_record_creates_pending():
    r = record("wl_sleep_dim", "23点睡觉", "睡觉时把台灯调暗")
    assert r["ok"] is True
    assert r["status"] == "pending"
    assert r["deduped"] is False
    assert r["key"] == "wl_sleep_dim"


def test_record_same_key_dedupes():
    record("wl_sleep_dim", "23点睡觉", "睡觉时把台灯调暗")
    r2 = record("wl_sleep_dim", "每晚23点入睡", "睡觉调暗灯")
    assert r2["deduped"] is True
    assert lst()["counts"]["pending"] == 1


def test_record_missing_required():
    no_key = apply_habit_action("record", {"subject": "shared", "habit": "x", "suggestion": "y"}, D6_10)
    assert no_key["ok"] is False
    no_habit = apply_habit_action("record", {"key": "k1", "suggestion": "y"}, D6_10)
    assert no_habit["ok"] is False


def test_rejected_key_not_revived():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    mark_asked("wl_sleep_dim")
    resolve("wl_sleep_dim", "rejected")

    again = record("wl_sleep_dim", "每晚23点睡觉习惯", "睡觉调暗灯", now=D7_10)
    assert again["deduped"] is True
    assert again["status"] == "rejected"
    counts = lst(D7_10)["counts"]
    assert counts["rejected"] == 1
    assert counts.get("pending", 0) == 0


def test_list_entries_exposes_terminal():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    mark_asked("wl_sleep_dim")
    resolve("wl_sleep_dim", "rejected")
    entries = lst()["entries"]
    assert len(entries) == 1
    assert entries[0]["key"] == "wl_sleep_dim"
    assert entries[0]["status"] == "rejected"


# ─── 防骚扰闸门 ──────────────────────────────────────────────────────────────


def test_open_question_blocks_second():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    record("zx_whitenoise", "睡前听白噪音", "睡觉放白噪音")
    assert lst()["can_ask_now"] is True

    ask = mark_asked("wl_sleep_dim")
    assert ask["ok"] is True
    assert ask["status"] == "asked"

    assert lst()["can_ask_now"] is False
    ask2 = mark_asked("zx_whitenoise")
    assert ask2["ok"] is False


def test_one_ask_per_day():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    record("zx_whitenoise", "睡前听白噪音", "睡觉放白噪音")
    mark_asked("wl_sleep_dim")
    resolve("wl_sleep_dim", "rejected", now=D6_23)

    # 同一上海日历日：已问过 → 仍不能问
    assert lst(D6_23)["can_ask_now"] is False
    assert mark_asked("zx_whitenoise", now=D6_23)["ok"] is False

    # 次日：无待回应、未问过 → 可以问
    assert lst(D7_10)["can_ask_now"] is True
    assert mark_asked("zx_whitenoise", now=D7_10)["ok"] is True


def test_mark_asked_only_from_pending():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    mark_asked("wl_sleep_dim")
    again = mark_asked("wl_sleep_dim")
    assert again["ok"] is False


# ─── resolve 状态机 ──────────────────────────────────────────────────────────


def test_asked_accepted_created():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    mark_asked("wl_gym")
    acc = resolve("wl_gym", "accepted")
    assert acc["ok"] is True
    assert acc["status"] == "accepted"

    created = resolve("wl_gym", "created", task_id="gym_music")
    assert created["ok"] is True
    assert created["status"] == "created"
    assert created["task_id"] == "gym_music"


def test_created_cannot_be_rejected():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    mark_asked("wl_gym")
    resolve("wl_gym", "created", task_id="gym_music")
    bad = resolve("wl_gym", "rejected")
    assert bad["ok"] is False


def test_resolve_unknown_key():
    r = resolve("nope", "accepted")
    assert r["ok"] is False


def test_pending_to_created_rejected():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    bad = resolve("wl_gym", "created", task_id="x")
    assert bad["ok"] is False
    assert bad["status"] == "pending"


def test_asked_to_created_shortcut():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    mark_asked("wl_gym")
    created = resolve("wl_gym", "created", task_id="gym")
    assert created["ok"] is True
    assert created["status"] == "created"


def test_pending_to_accepted_rejected():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    bad = resolve("wl_gym", "accepted")
    assert bad["ok"] is False
    assert bad["status"] == "pending"


# ─── 7 天过期与重新推荐 ──────────────────────────────────────────────────────


def test_asked_expires_after_7_days():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    mark_asked("wl_sleep_dim")
    counts = lst(D14_10)["counts"]  # 第 8 天：list 触发惰性过期
    assert counts["expired"] == 1
    assert counts.get("asked", 0) == 0
    assert lst(D14_10)["can_ask_now"] is True


def test_expired_revives_on_record():
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")
    mark_asked("wl_sleep_dim")
    lst(D14_10)  # 触发过期

    revived = record("wl_sleep_dim", "每晚23点睡觉", "睡觉调暗灯", now=D14_10)
    assert revived["status"] == "pending"
    assert revived["revived"] is True
    counts = lst(D14_10)["counts"]
    assert counts["pending"] == 1
    assert counts.get("expired", 0) == 0

    ask = mark_asked("wl_sleep_dim", now=D14_10)
    assert ask["ok"] is True
    assert ask["status"] == "asked"


def test_accepted_expires_and_revives():
    record("wl_gym", "傍晚健身", "健身时放运动歌单")
    mark_asked("wl_gym")
    resolve("wl_gym", "accepted")

    counts = lst(D14_10)["counts"]  # accepted 被回收为 expired
    assert counts["expired"] == 1
    assert counts.get("accepted", 0) == 0
    assert lst(D14_10)["can_ask_now"] is True

    revived = record("wl_gym", "傍晚健身", "健身时放运动歌单", now=D14_10)
    assert revived["status"] == "pending"
    assert revived["revived"] is True


def test_give_up_after_3_asks():
    d22 = "2026-06-22T10:00:00+08:00"
    d30 = "2026-06-30T10:00:00+08:00"
    record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯")

    # 第 1 次：问 → 过期 → 复活
    mark_asked("wl_sleep_dim")
    r = record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯", now=D14_10)
    assert r["revived"] is True
    # 第 2 次
    mark_asked("wl_sleep_dim", now=D14_10)
    r = record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯", now=d22)
    assert r["revived"] is True
    # 第 3 次：问 → 过期 → 已问满 3 次，放弃（不再复活）
    mark_asked("wl_sleep_dim", now=d22)
    r = record("wl_sleep_dim", "23点睡觉", "睡觉调暗灯", now=d30)
    assert "revived" not in r
    assert r["status"] == "expired"

    counts = lst(d30)["counts"]
    assert counts["expired"] == 1
    assert counts.get("pending", 0) == 0


# ─── 跨进程锁：并发写不丢条目 ───────────────────────────────────────────────


def test_concurrent_records_no_loss():
    import concurrent.futures

    def job(i):
        return record(f"habit_{i}", f"习惯编号{i}做某事", f"任务点子{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(job, range(10)))
    assert lst()["counts"]["pending"] == 10


# ─── item_id：追踪建议来源 ───────────────────────────────────────────────────


def _view(listing, key):
    return next((e for e in listing["entries"] if e["key"] == key), None)


def test_item_id_roundtrip_and_persist():
    apply_habit_action(
        "record",
        {"key": "wl_fitness", "subject": "王磊", "habit": "傍晚约19点健身", "suggestion": "健身时自动放运动歌单", "item_id": "p-abc123"},
        D6_10,
    )
    assert _view(lst(), "wl_fitness")["item_id"] == "p-abc123"

    mark_asked("wl_fitness")
    resolve("wl_fitness", "created", task_id="t1")
    assert _view(lst(D7_10), "wl_fitness")["item_id"] == "p-abc123"


def test_item_id_refreshed_on_revive():
    apply_habit_action(
        "record",
        {"key": "wl_water", "subject": "王磊", "habit": "下午喝水", "suggestion": "提醒喝水", "item_id": "p-old"},
        D6_10,
    )
    mark_asked("wl_water")
    apply_habit_action(
        "record",
        {"key": "wl_water", "subject": "王磊", "habit": "下午喝水", "suggestion": "提醒喝水", "item_id": "p-new"},
        D14_10,
    )
    assert _view(lst(D14_10), "wl_water")["item_id"] == "p-new"


# ─── 未知 action ─────────────────────────────────────────────────────────────


def test_unknown_action():
    assert apply_habit_action("bogus", now_override=D6_10)["ok"] is False


def test_concurrent_lock_import_guard():
    # habit_store 依赖 fcntl（POSIX）；确保模块导入未被裁剪。
    assert hasattr(habit_store, "apply_habit_action")


# ─── 回归：review 修复项 ─────────────────────────────────────────────────────


def _seed_raw(entries):
    """直接落盘一个 store（绕过 record，模拟历史/损坏/跨工具数据）。"""
    from miloco_cli.config import atomic_write

    atomic_write(store_path(), {"version": 1, "entries": entries})


def test_status_less_entry_does_not_crash():
    # #1：缺 status 字段的条目（手改/损坏/旧格式）不应让 record/mark_asked/resolve 崩 KeyError。
    _seed_raw([{"key": "broken", "habit": "h", "suggestion": "s"}])
    r = record("broken", "h2", "s2")  # 命中既有 key，走 existing 分支
    assert r["ok"] is True
    assert mark_asked("broken")["ok"] is False  # status 非 pending → 优雅拒绝，不崩
    assert resolve("broken", "accepted")["ok"] is False


def test_z_suffixed_timestamp_parses_and_expires():
    # #2：UTC "Z" 时间戳（跨工具/历史）必须能解析，否则 elapsed=0 → 永不过期、永久占位。
    assert local_date_key("2026-06-06T02:00:00Z") == "2026-06-06"  # +08:00 视角
    _seed_raw([{"key": "z", "habit": "h", "suggestion": "s", "status": "asked",
                "asked_at": "2026-06-06T02:00:00Z", "ask_count": 1}])
    # 第 8 天 list 应触发过期（若 Z 解析失败，elapsed=0 永不过期，这里会仍是 asked）
    counts = lst(D14_10)["counts"]
    assert counts.get("expired") == 1
    assert counts.get("asked", 0) == 0


def test_revive_does_not_persist_null_keys():
    # #5：复活后的 pending 不应残留 asked_at/resolved_at/reason 的 null 键。
    record("k", "h", "s")
    mark_asked("k")
    lst(D14_10)  # 过期
    record("k", "h", "s", now=D14_10)  # 复活
    raw = json.loads(store_path().read_text("utf-8"))
    entry = next(e for e in raw["entries"] if e["key"] == "k")
    assert entry["status"] == "pending"
    for k in ("asked_at", "resolved_at", "reason"):
        assert k not in entry, f"复活后不应残留 {k}=null"


def test_created_without_task_id_omits_key():
    # #6：created 未回填 task_id 时，结果不应带 task_id:null（对齐旧 tool 省略语义）。
    record("k", "h", "s")
    mark_asked("k")
    r = resolve("k", "created")  # 无 task_id
    assert r["ok"] is True
    assert r["status"] == "created"
    assert "task_id" not in r


def test_error_result_omits_null_status():
    # #6/中心 compact：找不到 key 的错误结果不带 status:null。
    r = resolve("nope", "rejected")
    assert r["ok"] is False
    assert "status" not in r


def _stored(key):
    raw = json.loads(store_path().read_text("utf-8"))
    return next(e for e in raw["entries"] if e["key"] == key)


def test_resolve_paths_do_not_persist_null_keys():
    # review 复审：created(无 task_id) / rejected(无 reason) 不应在落盘条目里残留 null 键。
    record("a", "h", "s")
    mark_asked("a")
    resolve("a", "created")  # 无 task-id
    assert "task_id" not in _stored("a")

    record("b", "h", "s")
    mark_asked("b")
    resolve("b", "rejected")  # 无 reason
    assert "reason" not in _stored("b")


def test_resolve_created_preserves_existing_task_id():
    # 沿用既有 task_id：accepted→created 若当轮不传 task_id，应保留之前写入的。
    record("c", "h", "s")
    mark_asked("c")
    resolve("c", "created", task_id="t-1")
    # 幂等再 record 命中 created → 永久跳过；直接查落盘 task_id 仍在
    assert _stored("c")["task_id"] == "t-1"
