# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""client._filter_completed_event_rules 单测。

覆盖：四个 direction × completed/active/未绑 record 的组合，以及 batch SQL 查询的正确性。
"""

from __future__ import annotations

import pytest
from miloco.perception.client import _filter_completed_event_rules


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_filter_rules.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()
    yield db_file
    reset_settings()


def _insert_task(task_id: str) -> None:
    from miloco.database.connector import get_db_connector
    from miloco.utils.time_utils import now_iso

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task (task_id, description, status, created_at) "
            "VALUES (?, ?, 'active', ?)",
            (task_id, f"desc-{task_id}", now_iso()),
        )
        conn.commit()


def _insert_progress_record(task_id: str, status: str) -> None:
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task_record_progress "
            "(task_id, target, unit, window, status, created_at, updated_at) "
            "VALUES (?, 1, '次', 'day', ?, '2026-06-13T00:00:00', '2026-06-13T00:00:00')",
            (task_id, status),
        )
        conn.commit()


def _insert_duration_record(task_id: str, status: str) -> None:
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task_record_duration "
            "(task_id, target_minutes, status, created_at, updated_at) "
            "VALUES (?, 60, ?, '2026-06-13T00:00:00', '2026-06-13T00:00:00')",
            (task_id, status),
        )
        conn.commit()


def _make_rule(
    rule_id: str, task_id: str, mode: str = "event", direction: str | None = None
) -> dict:
    """带 direction 的 rule dict。

    direction 缺省为 None 是刻意的：未迁移的库和内存直接构造的 Rule 都是这个形态,
    闸必须靠 mode 兜住。
    """
    return {"id": rule_id, "task_id": task_id, "mode": mode, "direction": direction}


def test_event_rule_with_completed_record_filtered_out(real_db):
    _insert_task("t_drink")
    _insert_progress_record("t_drink", "completed")

    rules = [_make_rule("r1", "t_drink", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert kept == []
    assert skipped == ["t_drink"]


def test_event_rule_with_active_record_kept(real_db):
    _insert_task("t_drink")
    _insert_progress_record("t_drink", "active")

    rules = [_make_rule("r1", "t_drink", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert len(kept) == 1
    assert kept[0]["id"] == "r1"
    assert skipped == []


def test_event_rule_without_record_kept(real_db):
    _insert_task("t_no_record")

    rules = [_make_rule("r1", "t_no_record", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert len(kept) == 1
    assert kept[0]["id"] == "r1"
    assert skipped == []


def test_state_rule_with_completed_record_kept(real_db):
    _insert_task("t_shower")
    _insert_duration_record("t_shower", "completed")

    rules = [_make_rule("r1", "t_shower", "state")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert len(kept) == 1
    assert kept[0]["id"] == "r1"
    assert skipped == []


def test_empty_rules_returns_empty(real_db):
    assert _filter_completed_event_rules([]) == ([], [])


def test_mixed_batch(real_db):
    _insert_task("t_done")
    _insert_progress_record("t_done", "completed")
    _insert_task("t_active")
    _insert_progress_record("t_active", "active")
    _insert_task("t_none")
    _insert_task("t_state_done")
    _insert_duration_record("t_state_done", "completed")

    rules = [
        _make_rule("r_done", "t_done", "event"),
        _make_rule("r_active", "t_active", "event"),
        _make_rule("r_none", "t_none", "event"),
        _make_rule("r_state", "t_state_done", "state"),
    ]
    kept, skipped = _filter_completed_event_rules(rules)
    kept_ids = {r["id"] for r in kept}
    assert kept_ids == {"r_active", "r_none", "r_state"}
    assert skipped == ["t_done"]


def test_duration_completed_event_rule_filtered_out(real_db):
    _insert_task("t_run")
    _insert_duration_record("t_run", "completed")

    rules = [_make_rule("r1", "t_run", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert kept == []
    assert skipped == ["t_run"]


def test_rollover_restores_injection(real_db):
    """模拟 rollover：旧行 archived_at=非NULL+completed，新行 archived_at=NULL+active。
    SQL 的 WHERE archived_at IS NULL 必须只看新行，让规则恢复注入。"""
    from miloco.database.connector import get_db_connector

    _insert_task("t_water")
    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task_record_progress "
            "(task_id, target, current, unit, window, status, archived_at, created_at, updated_at) "
            "VALUES ('t_water', 8, 8, '杯', 'day', 'completed', "
            " '2026-06-12T23:59:59', '2026-06-12T09:00:00', '2026-06-12T20:00:00')",
        )
        conn.execute(
            "INSERT INTO task_record_progress "
            "(task_id, target, current, unit, window, status, created_at, updated_at) "
            "VALUES ('t_water', 8, 0, '杯', 'day', 'active', "
            " '2026-06-13T00:05:00', '2026-06-13T00:05:00')",
        )
        conn.commit()

    rules = [_make_rule("r1", "t_water", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert len(kept) == 1
    assert kept[0]["id"] == "r1"
    assert skipped == []


# ============================================================
# recurring + 当期达标 → 等价 completed 视作 satisfied
# event mode 没有 _target_fired 兜底，靠 perception filter 在装载侧切断。
# ============================================================


def _insert_progress_record_full(
    task_id: str,
    *,
    target: int,
    current: int,
    recurring: bool,
    status: str = "active",
) -> None:
    from miloco.database.connector import get_db_connector

    pattern = '{"window": "day"}' if recurring else None
    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task_record_progress "
            "(task_id, target, current, unit, window, recurring_pattern, "
            " status, created_at, updated_at) "
            "VALUES (?, ?, ?, '次', 'day', ?, ?, "
            " '2026-06-13T00:00:00', '2026-06-13T00:00:00')",
            (task_id, target, current, pattern, status),
        )
        conn.commit()


def _insert_duration_record_full(
    task_id: str,
    *,
    target_minutes: int | None,
    recurring: bool,
    session_seconds: int = 0,
    status: str = "active",
) -> None:
    from miloco.database.connector import get_db_connector

    pattern = '{"window": "day"}' if recurring else None
    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task_record_duration "
            "(task_id, target_minutes, recurring_pattern, status, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, "
            " '2026-06-13T00:00:00', '2026-06-13T00:00:00')",
            (task_id, target_minutes, pattern, status),
        )
        if session_seconds > 0:
            conn.execute(
                "INSERT INTO task_record_duration_session "
                "(task_id, start_at, end_at, duration_seconds) "
                "VALUES (?, '2026-06-13T09:00:00', '2026-06-13T09:30:00', ?)",
                (task_id, session_seconds),
            )
        conn.commit()


def test_progress_recurring_reached_target_filtered_out(real_db):
    """每日 N 杯水当天喝够 → status 仍 active（recurring 不翻），但应被剔除。"""
    _insert_task("t_drink8")
    _insert_progress_record_full(
        "t_drink8", target=8, current=8, recurring=True, status="active"
    )

    rules = [_make_rule("r1", "t_drink8", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert kept == []
    assert skipped == ["t_drink8"]


def test_progress_recurring_partial_kept(real_db):
    """recurring 未达标 → 保留。"""
    _insert_task("t_drink8")
    _insert_progress_record_full(
        "t_drink8", target=8, current=3, recurring=True, status="active"
    )

    rules = [_make_rule("r1", "t_drink8", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert len(kept) == 1
    assert skipped == []


def test_duration_recurring_reached_target_filtered_out(real_db):
    """duration recurring 累计满 target_minutes → 应剔除。"""
    _insert_task("t_phone")
    _insert_duration_record_full(
        "t_phone", target_minutes=5, recurring=True, session_seconds=300
    )

    rules = [_make_rule("r1", "t_phone", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert kept == []
    assert skipped == ["t_phone"]


def test_duration_recurring_below_target_kept(real_db):
    """duration recurring 累计未到 target → 保留。"""
    _insert_task("t_phone")
    _insert_duration_record_full(
        "t_phone", target_minutes=5, recurring=True, session_seconds=120
    )

    rules = [_make_rule("r1", "t_phone", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert len(kept) == 1
    assert skipped == []


def test_duration_recurring_null_target_kept(real_db):
    """duration target_minutes=NULL 时无达标语义，不剔除。"""
    _insert_task("t_no_target")
    _insert_duration_record_full(
        "t_no_target", target_minutes=None, recurring=True, session_seconds=99999
    )

    rules = [_make_rule("r1", "t_no_target", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert len(kept) == 1
    assert skipped == []


def test_progress_non_recurring_active_partial_kept(real_db):
    """oneshot progress 未达 target+active → 保留（status 路径）。"""
    _insert_task("t_oneshot")
    _insert_progress_record_full(
        "t_oneshot", target=10, current=3, recurring=False, status="active"
    )

    rules = [_make_rule("r1", "t_oneshot", "event")]
    kept, skipped = _filter_completed_event_rules(rules)
    assert len(kept) == 1
    assert skipped == []


def test_exit_rule_with_completed_record_kept(real_db):
    """达标后 exit 型 rule 必须继续下发。

    exit 存的 mode 就是 event, 按 mode 判会把它一起剔掉 —— 而配达标通知的前提
    (duration record + target_minutes) 正是这道闸的命中条件, 于是每个配了达标的
    task 达标那一刻就永久失去退出边沿, 卡在 on。
    """
    _insert_task("t_focus")
    _insert_duration_record("t_focus", "completed")

    rules = [_make_rule("r-exit", "t_focus", "event", "exit")]
    kept, skipped = _filter_completed_event_rules(rules)

    assert [r["id"] for r in kept] == ["r-exit"]
    assert skipped == []


def test_enter_rule_with_completed_record_still_filtered_out(real_db):
    """enter 型仍要剔 —— 上面那条不能靠"什么都不剔"来通过。"""
    _insert_task("t_focus")
    _insert_duration_record("t_focus", "completed")

    rules = [_make_rule("r-enter", "t_focus", "event", "enter")]
    kept, skipped = _filter_completed_event_rules(rules)

    assert kept == []
    assert skipped == ["t_focus"]


def test_milestone_rule_with_completed_record_kept(real_db):
    """达标规则的条件来自 record 源, 不走摄像头, 剔它没有意义。"""
    _insert_task("t_focus")
    _insert_duration_record("t_focus", "completed")

    rules = [_make_rule("r-ms", "t_focus", "event", "milestone")]
    kept, skipped = _filter_completed_event_rules(rules)

    assert [r["id"] for r in kept] == ["r-ms"]
    assert skipped == []


def test_direction_absent_falls_back_to_mode(real_db):
    """未迁移的库里 direction 是 NULL, 闸靠 mode 兜住, 行为与迁移前一致。"""
    _insert_task("t_focus")
    _insert_duration_record("t_focus", "completed")

    kept, skipped = _filter_completed_event_rules(
        [_make_rule("r-old", "t_focus", "event", None)]
    )
    assert kept == []
    assert skipped == ["t_focus"]

    kept, skipped = _filter_completed_event_rules(
        [_make_rule("r-old-state", "t_focus", "state", None)]
    )
    assert [r["id"] for r in kept] == ["r-old-state"]
    assert skipped == []
