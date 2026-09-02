# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""v2→v3 schema 迁移测试 (task 运行态重构 expand-contract 阶段 A).

覆盖:
- 只加列不删列: v2 的全部旧列在迁移后仍在 (阶段 A 的核心承诺)
- mode → direction 映射
- condition → condition_dnf 的 1×1 DNF 结构
- event / state 两种旧形态的动作搬到 task 边界动作列
- action_descriptions 多条合成一条时与 runner._select_slot 的编号规则一致
- 一 task 多 rule: 动作一致 → 正常搬; 不一致 → 置 paused 且动作不丢
- on_target_desc → 补建 milestone rule; 查不到 target_minutes → 置 paused
- 非 session 的 exit_debounce 非默认值重置
- rule.enabled: 旧 bug 关掉的恢复成 1, 用户手工关掉的不动
- 幂等: 已是 v3 的库不重复跑
"""

from __future__ import annotations

import json
import sqlite3

import pytest

_V2_RULE_COLUMNS = [
    "id",
    "name",
    "task_id",
    "mode",
    "lifecycle",
    "enabled",
    "condition",
    "actions",
    "action_descriptions",
    "on_enter_actions",
    "on_enter_desc",
    "on_exit_actions",
    "on_exit_desc",
    "on_target_desc",
    "terminate_when",
    "exit_debounce_seconds",
    "duration_seconds",
    "duration_ratio",
    "created_at",
    "updated_at",
]


def _create_v2_baseline(db_path) -> None:
    """建 v2 形态 DB (rule 有 FK CASCADE, 有 cron 表, 无 task_link, user_version=2)."""
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute(
        "CREATE TABLE kv (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "key TEXT UNIQUE NOT NULL, value TEXT, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    cursor.execute(
        "CREATE TABLE task (task_id TEXT PRIMARY KEY, description TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'active', paused_at INTEGER, "
        "created_at INTEGER NOT NULL)"
    )
    cursor.execute("""
        CREATE TABLE rule (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            task_id TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'event',
            lifecycle TEXT NOT NULL DEFAULT 'permanent',
            enabled BOOLEAN DEFAULT 1,
            condition TEXT NOT NULL,
            actions TEXT NOT NULL DEFAULT '[]',
            action_descriptions TEXT NOT NULL DEFAULT '[]',
            on_enter_actions TEXT NOT NULL DEFAULT '[]',
            on_enter_desc TEXT,
            on_exit_actions TEXT NOT NULL DEFAULT '[]',
            on_exit_desc TEXT,
            on_target_desc TEXT,
            terminate_when TEXT,
            exit_debounce_seconds INTEGER NOT NULL DEFAULT 60,
            duration_seconds INTEGER,
            duration_ratio REAL NOT NULL DEFAULT 0.8,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
        )
    """)
    cursor.execute("""
        CREATE TABLE task_record_duration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            target_minutes INTEGER,
            active_session_start_at INTEGER,
            recurring_pattern TEXT,
            expires_at INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            archived_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (task_id) REFERENCES task(task_id) ON DELETE CASCADE
        )
    """)
    cursor.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


@pytest.fixture
def v2_db(tmp_path, monkeypatch):
    db_file = tmp_path / "v2.db"
    _create_v2_baseline(db_file)
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    yield db_file
    reset_settings()


def _raw(db_file):
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    return conn


def _add_task(cursor, task_id, status="active"):
    cursor.execute(
        "INSERT INTO task (task_id, description, status, created_at) "
        "VALUES (?, ?, ?, 0)",
        (task_id, f"desc {task_id}", status),
    )


def _add_rule(cursor, rule_id, task_id, **cols):
    payload = {
        "id": rule_id,
        "name": rule_id,
        "task_id": task_id,
        "mode": "event",
        "lifecycle": "permanent",
        "enabled": 1,
        "condition": json.dumps({"perceive_device_ids": ["cam1"], "query": "有人"}),
        "actions": "[]",
        "action_descriptions": "[]",
        "on_enter_actions": "[]",
        "on_enter_desc": None,
        "on_exit_actions": "[]",
        "on_exit_desc": None,
        "on_target_desc": None,
        "terminate_when": None,
        "exit_debounce_seconds": 60,
        "duration_seconds": None,
        "duration_ratio": 0.8,
        "created_at": 0,
        "updated_at": 0,
    }
    payload.update(cols)
    cursor.execute(
        f"INSERT INTO rule ({', '.join(_V2_RULE_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_V2_RULE_COLUMNS))})",
        [payload[c] for c in _V2_RULE_COLUMNS],
    )


def _add_duration_record(cursor, task_id, target_minutes):
    cursor.execute(
        "INSERT INTO task_record_duration (task_id, target_minutes, created_at, "
        "updated_at) VALUES (?, ?, 0, 0)",
        (task_id, target_minutes),
    )


def _migrate(db_file):
    import miloco.database.connector as connector_module

    connector_module.init_database()
    return connector_module.get_db_connector()


def _seed(db_file, fn):
    conn = _raw(db_file)
    fn(conn.cursor())
    conn.commit()
    conn.close()


# ── 阶段 A 的核心承诺: 只加列 ──────────────────────────────────────────


def test_all_v2_columns_survive(v2_db):
    """阶段 A 不删任何列 —— 旧代码读旧列必须照常。"""
    _seed(v2_db, lambda c: (_add_task(c, "t1"), _add_rule(c, "r1", "t1")))
    _migrate(v2_db)

    conn = _raw(v2_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rule)")}
    assert set(_V2_RULE_COLUMNS) <= cols
    assert {"direction", "condition_dnf"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    conn.close()


def test_old_reader_still_parses_condition_column(v2_db):
    """condition 列原样不动 —— DNF 落在新列, 不是就地改写旧列。

    这条钉的是"为什么加新列而不是往原 JSON 里塞 any_of": 塞进去老代码虽读得到,
    但它写回时只序列化自己那两个字段, 会把 DNF 静默丢掉。
    """
    original = json.dumps({"perceive_device_ids": ["cam1"], "query": "有人"})
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", condition=original),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    row = conn.execute("SELECT condition FROM rule WHERE id='r1'").fetchone()
    assert json.loads(row["condition"]) == json.loads(original)
    conn.close()


# ── mode → direction / condition → DNF ────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"), [("event", "enter"), ("state", "session")]
)
def test_mode_maps_to_direction(v2_db, mode, expected):
    _seed(
        v2_db,
        lambda c: (_add_task(c, "t1"), _add_rule(c, "r1", "t1", mode=mode)),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert (
        conn.execute("SELECT direction FROM rule WHERE id='r1'").fetchone()[0]
        == expected
    )
    conn.close()


def test_condition_becomes_one_by_one_dnf(v2_db):
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(
                c,
                "r1",
                "t1",
                condition=json.dumps(
                    {"perceive_device_ids": ["cam1", "cam2"], "query": "有人摔倒"}
                ),
            ),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    dnf = json.loads(
        conn.execute("SELECT condition_dnf FROM rule WHERE id='r1'").fetchone()[0]
    )
    assert dnf == {
        "any_of": [
            [
                {
                    "source_type": "omni",
                    "spec": {
                        "perceive_device_ids": ["cam1", "cam2"],
                        "query": "有人摔倒",
                    },
                    "negate": False,
                }
            ]
        ]
    }
    conn.close()


def test_broken_condition_json_does_not_abort_migration(v2_db):
    """存量脏数据丢该条继续跑, 不卡启动 (§10.1)。"""
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", condition="{不是 JSON"),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    dnf = json.loads(
        conn.execute("SELECT condition_dnf FROM rule WHERE id='r1'").fetchone()[0]
    )
    assert dnf["any_of"][0][0]["spec"] == {"perceive_device_ids": [], "query": ""}
    conn.close()


# ── 动作搬到 task ─────────────────────────────────────────────────────


def test_event_actions_move_to_task_on_enter(v2_db):
    actions = json.dumps([{"did": "d1", "iid": "prop.2.1", "value": True}])
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="event", actions=actions),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    row = conn.execute("SELECT * FROM task WHERE task_id='t1'").fetchone()
    assert json.loads(row["on_enter_actions"]) == json.loads(actions)
    assert row["on_exit_actions"] == "[]"
    conn.close()


def test_action_descriptions_join_matches_runner_numbering(v2_db):
    """合成文本必须与 runner._select_slot 逐字一致 —— 迁移只把拼接提前, 不改文本。

    单条也带 "1. " 前缀: 现状就是无条件编号。
    """
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(
                c,
                "r1",
                "t1",
                mode="event",
                action_descriptions=json.dumps(["播报甲", "推送乙"]),
            ),
            _add_task(c, "t2"),
            _add_rule(
                c,
                "r2",
                "t2",
                mode="event",
                action_descriptions=json.dumps(["只有一条"]),
            ),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert (
        conn.execute("SELECT on_enter_desc FROM task WHERE task_id='t1'").fetchone()[0]
        == "1. 播报甲\n2. 推送乙"
    )
    assert (
        conn.execute("SELECT on_enter_desc FROM task WHERE task_id='t2'").fetchone()[0]
        == "1. 只有一条"
    )
    conn.close()


def test_state_actions_move_to_both_directions(v2_db):
    enter = json.dumps([{"did": "d1", "iid": "prop.2.1", "value": True}])
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(
                c,
                "r1",
                "t1",
                mode="state",
                on_enter_actions=enter,
                on_exit_desc="退出时提醒",
            ),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    row = conn.execute("SELECT * FROM task WHERE task_id='t1'").fetchone()
    assert json.loads(row["on_enter_actions"]) == json.loads(enter)
    assert row["on_exit_desc"] == "退出时提醒"
    conn.close()


def test_rule_lifecycle_moves_to_task(v2_db):
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", lifecycle="temporary"),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert (
        conn.execute("SELECT lifecycle FROM task WHERE task_id='t1'").fetchone()[0]
        == "temporary"
    )
    conn.close()


# ── 一 task 多 rule ───────────────────────────────────────────────────


def test_multi_rule_same_actions_merges(v2_db):
    actions = json.dumps([{"did": "d1", "iid": "prop.2.1", "value": True}])
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", actions=actions),
            _add_rule(c, "r2", "t1", actions=actions),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    row = conn.execute("SELECT * FROM task WHERE task_id='t1'").fetchone()
    assert row["status"] == "active"
    assert json.loads(row["on_enter_actions"]) == json.loads(actions)
    conn.close()


def test_multi_rule_conflicting_actions_pauses_and_keeps_actions(v2_db):
    """动作不一致 → task 置 paused, 动作原样留在 rule 列上, enabled 不动。

    "动作不丢" 是阶段 A 相对一刀切迁移的核心收益, 必须断言原列还在。
    enabled 保持 1: 置 paused 已经让派生量为假, 再写 0 会让用户处置完重新启用后
    这批规则永远起不来。
    """
    a1 = json.dumps([{"did": "d1", "iid": "prop.2.1", "value": True}])
    a2 = json.dumps([{"did": "d2", "iid": "prop.3.1", "value": False}])
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", actions=a1),
            _add_rule(c, "r2", "t1", actions=a2),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    task = conn.execute("SELECT * FROM task WHERE task_id='t1'").fetchone()
    assert task["status"] == "paused"
    assert task["on_enter_actions"] == "[]"
    rules = {r["id"]: r for r in conn.execute("SELECT * FROM rule WHERE task_id='t1'")}
    assert json.loads(rules["r1"]["actions"]) == json.loads(a1)
    assert json.loads(rules["r2"]["actions"]) == json.loads(a2)
    assert rules["r1"]["enabled"] == 1 and rules["r2"]["enabled"] == 1
    conn.close()


def test_differing_target_desc_counts_as_conflict(v2_db):
    """进出动作相同、达标文案不同 → 算冲突, 不能静默取第一条。

    指纹不含达标文案的话两条指纹相等, 按 created_at 取第一条写进 task, 第二条既不
    生效也不进迁移报告 —— 用户无从知道自己配的那句被丢了。
    """
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_duration_record(c, "t1", 60),
            _add_rule(c, "r1", "t1", on_target_desc="推一条"),
            _add_rule(c, "r2", "t1", on_target_desc="推另一条"),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    task = conn.execute("SELECT * FROM task WHERE task_id='t1'").fetchone()
    assert task["status"] == "paused"
    assert task["on_target_desc"] is None
    rules = {r["id"]: r for r in conn.execute("SELECT * FROM rule WHERE task_id='t1'")}
    assert rules["r1"]["on_target_desc"] == "推一条"
    assert rules["r2"]["on_target_desc"] == "推另一条"
    conn.close()


def test_same_target_desc_is_not_conflict(v2_db):
    """达标文案相同就不是冲突 —— 上面那条不能靠"一有达标文案就判冲突"通过。"""
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_duration_record(c, "t1", 60),
            _add_rule(c, "r1", "t1", on_target_desc="推一条"),
            _add_rule(c, "r2", "t1", on_target_desc="推一条"),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    task = conn.execute("SELECT * FROM task WHERE task_id='t1'").fetchone()
    assert task["on_target_desc"] == "推一条"
    conn.close()


def test_event_and_state_with_equivalent_actions_are_not_conflict(v2_db):
    """指纹比的是转换后的 task 四元组, 不是原始列。

    一条 event rule 的 actions 与一条 state rule 的 on_enter_actions 落到 task
    上是同一组值, 按原始列比会误判成冲突、把动作留在 rule 列上不搬。

    判据用"动作搬没搬过去"而不是 task 状态: 这个组合迁移后是 enter + session,
    会被 §19.7 的独占校验另行置 paused, 拿状态当判据的话两件事分不开。
    """
    actions = json.dumps([{"did": "d1", "iid": "prop.2.1", "value": True}])
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="event", actions=actions),
            _add_rule(c, "r2", "t1", mode="state", on_enter_actions=actions),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    moved = conn.execute(
        "SELECT on_enter_actions FROM task WHERE task_id='t1'"
    ).fetchone()[0]
    assert json.loads(moved) == json.loads(actions)
    conn.close()


def test_two_state_rules_on_one_task_are_paused(v2_db):
    """迁移后两条 session 挂同一 task —— §19.7 的永久卡死, 必须置 paused。

    留成 active 等于把一个进得去出不来的 task 交给用户, 而他看不出哪里不对。
    """
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="state", on_enter_desc="开灯"),
            _add_rule(c, "r2", "t1", mode="state", on_enter_desc="开灯"),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert (
        conn.execute("SELECT status FROM task WHERE task_id='t1'").fetchone()[0]
        == "paused"
    )
    # 置 paused 就够了 —— 再写 enabled=0, 用户按报告处置完重新启用也起不来。
    assert [
        r["enabled"]
        for r in conn.execute("SELECT enabled FROM rule WHERE task_id='t1'").fetchall()
    ] == [1, 1]
    conn.close()


def test_a_single_state_rule_task_stays_active(v2_db):
    """对照: 一条 session 独占是合法形态, 别顺手把正常 task 也打成 paused。"""
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="state", on_enter_desc="开灯"),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert (
        conn.execute("SELECT status FROM task WHERE task_id='t1'").fetchone()[0]
        == "active"
    )
    conn.close()


# ── on_target → milestone rule ────────────────────────────────────────


def test_on_target_creates_milestone_rule(v2_db):
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="state", on_target_desc="达标了提醒"),
            _add_duration_record(c, "t1", 60),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert (
        conn.execute("SELECT on_target_desc FROM task WHERE task_id='t1'").fetchone()[0]
        == "达标了提醒"
    )
    milestone = conn.execute(
        "SELECT * FROM rule WHERE task_id='t1' AND direction='milestone'"
    ).fetchone()
    assert milestone is not None
    dnf = json.loads(milestone["condition_dnf"])
    item = dnf["any_of"][0][0]
    assert item["source_type"] == "record"
    # 阈值不进条件项, 与运行时代建的那条同一种形状 (见 RecordRef 的 docstring)
    assert item["spec"] == {"task_id": "t1", "kind": "duration", "op": ">="}
    conn.close()


def test_milestone_legacy_condition_uses_unreachable_did(v2_db):
    """补建的 milestone rule 在旧 condition 列上填一个不存在的 did。

    阶段 A 不删列, 万一退回旧代码, 旧代码只认 perceive_device_ids —— 留空会让
    它把"累计达标"当视觉 query 塞进每台摄像头的 prompt。
    """
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="state", on_target_desc="达标了提醒"),
            _add_duration_record(c, "t1", 60),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    milestone = conn.execute(
        "SELECT condition FROM rule WHERE task_id='t1' AND direction='milestone'"
    ).fetchone()
    legacy = json.loads(milestone["condition"])
    assert legacy["perceive_device_ids"] == ["__milestone_no_camera__"]
    conn.close()


def test_on_target_without_target_minutes_pauses_task(v2_db):
    """没有阈值的达标通知永远不会触发, 留成 active 等于留一个隐形失效项。"""
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="state", on_target_desc="达标了提醒"),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert (
        conn.execute("SELECT status FROM task WHERE task_id='t1'").fetchone()[0]
        == "paused"
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM rule WHERE task_id='t1' AND direction='milestone'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_on_target_on_an_event_rule_pauses_the_task(v2_db):
    """event 型 rule 带达标文案 → 迁成 enter-only + 达标, 达标一次都不会响。

    没有出路径的 task 运行态恒 off, 达标信号被判成不在会话中; 而累计时长靠
    session-start / session-end 配对, 也永远发不出 session-end。留成 active 等于
    留一个隐形失效项, 用户看不出缺什么。
    """
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="event", on_target_desc="达标了提醒"),
            _add_duration_record(c, "t1", 60),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert (
        conn.execute("SELECT status FROM task WHERE task_id='t1'").fetchone()[0]
        == "paused"
    )
    conn.close()


def test_the_report_gives_each_task_its_own_reason(v2_db, capsys):
    """报告里的原因必须是这个 task 自己的那条。

    两种存量形态都会被判不合法, 报告写死一种的话, 撞上另一种的用户照着处置找不到
    对象 —— 这里的 task 名下一条 session 都没有, 报告却让他"只保留那条 session"。
    报告是 stdout 直出的, 让人回去翻日志正好抵消了直出的意义。
    """
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t_no_exit"),
            _add_rule(c, "r1", "t_no_exit", mode="event", on_target_desc="达标了提醒"),
            _add_duration_record(c, "t_no_exit", 60),
        ),
    )
    _migrate(v2_db)

    report = capsys.readouterr().out
    assert "t_no_exit" in report
    assert "direction=exit 或 session 的规则" in report
    assert "只保留那条 session" not in report


# ── 字段清洗 ──────────────────────────────────────────────────────────


def test_non_session_exit_debounce_is_reset(v2_db):
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="event", exit_debounce_seconds=15),
            _add_task(c, "t2"),
            _add_rule(c, "r2", "t2", mode="state", exit_debounce_seconds=15),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert (
        conn.execute("SELECT exit_debounce_seconds FROM rule WHERE id='r1'").fetchone()[
            0
        ]
        == 60
    )
    # session 上该字段有效, 不能动
    assert (
        conn.execute("SELECT exit_debounce_seconds FROM rule WHERE id='r2'").fetchone()[
            0
        ]
        == 15
    )
    conn.close()


def test_paused_task_rules_are_restored_and_user_intent_is_kept(v2_db):
    """旧 bug 关掉的恢复成 1; active task 下用户手工关掉的那条不许被打开。

    恢复: 旧代码停用 task 会把名下 rule 一律写 0, 而新代码重新启用 task 不回写
    enabled —— 不恢复就永久失效, 而界面上看不出。
    不打开: 反方向对齐等于让用户主动停掉的自动化在升级后自己开回来、下指令。
    """
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t_paused", status="paused"),
            _add_rule(c, "r_bug_off", "t_paused", enabled=0),
            _add_task(c, "t_active", status="active"),
            _add_rule(c, "r_user_off", "t_active", enabled=0),
            _add_task(c, "t_active2", status="active"),
            _add_rule(c, "r_on", "t_active2", enabled=1),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    got = {r["id"]: r["enabled"] for r in conn.execute("SELECT id, enabled FROM rule")}
    assert got == {"r_bug_off": 1, "r_user_off": 0, "r_on": 1}
    conn.close()


def test_null_enabled_is_restored_too(v2_db):
    """enabled 列默认 1, 存成 NULL 是缺值不是用户关过, 而读侧把 NULL 当假。"""
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1", status="active"),
            _add_rule(c, "r1", "t1", enabled=None),
        ),
    )
    _migrate(v2_db)

    conn = _raw(v2_db)
    assert conn.execute("SELECT enabled FROM rule WHERE id='r1'").fetchone()[0] == 1
    conn.close()


# ── 幂等 ──────────────────────────────────────────────────────────────


def test_migration_not_rerun_on_v3_db(v2_db):
    """第二次启动不该再补建一条 milestone rule。"""
    _seed(
        v2_db,
        lambda c: (
            _add_task(c, "t1"),
            _add_rule(c, "r1", "t1", mode="state", on_target_desc="达标了提醒"),
            _add_duration_record(c, "t1", 60),
        ),
    )
    _migrate(v2_db)

    import miloco.database.connector as connector_module

    connector_module.db_connector = None
    connector_module.init_database()

    conn = _raw(v2_db)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM rule WHERE task_id='t1' AND direction='milestone'"
        ).fetchone()[0]
        == 1
    )
    conn.close()
