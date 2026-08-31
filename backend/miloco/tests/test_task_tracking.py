# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""§18.3 判定跟踪的内存部分."""

from __future__ import annotations

from miloco.task.tracking import DecisionTracker


def test_records_last_and_counts():
    """快照与计数是两份数据 —— 快照被高频覆盖, 答不了"偶发是哪一层吃掉的"。"""
    t = DecisionTracker()
    t.record("t1", "r1", "already_in_state", 100)
    t.record("t1", "r1", "already_in_state", 200)
    t.record("t1", "r2", "entered", 300)

    assert t.last_decision("t1").outcome == "entered"
    assert t.last_decision("t1").rule_id == "r2"
    assert t.counts("t1") == {"already_in_state": 2, "entered": 1}


def test_tasks_are_isolated():
    t = DecisionTracker()
    t.record("t1", "r1", "entered", 1)
    t.record("t2", "r2", "exited", 2)

    assert t.last_decision("t1").outcome == "entered"
    assert t.counts("t2") == {"exited": 1}


def test_forget_clears_everything():
    """不清就是一条随 task 数单调增长的内存泄漏。"""
    t = DecisionTracker()
    t.record("t1", "r1", "entered", 1)

    t.forget("t1")

    assert t.last_decision("t1") is None
    assert t.counts("t1") == {}


def test_unknown_task_reads_are_empty_not_error():
    t = DecisionTracker()
    assert t.last_decision("nope") is None
    assert t.counts("nope") == {}
    assert t.summary("nope") is None


def test_suppressed_outcomes_are_flagged():
    """「被压制」是用户问「我的规则怎么没反应」时最需要看到的那一类。"""
    t = DecisionTracker()
    t.record("t1", "r1", "blocked_by_exit_condition", 1)

    s = t.summary("t1")
    assert s["suppressed"] is True
    assert s["abnormal"] is False


def test_abnormal_outcomes_are_flagged():
    t = DecisionTracker()
    t.record("t1", "r1", "signal_dropped", 1)

    s = t.summary("t1")
    assert s["abnormal"] is True
    assert s["suppressed"] is False


def test_normal_fire_is_neither():
    t = DecisionTracker()
    t.record("t1", "r1", "entered", 1)

    s = t.summary("t1")
    assert s["suppressed"] is False
    assert s["abnormal"] is False


def test_counts_returns_a_copy():
    """返回引用的话调用方一改就污染内部计数。"""
    t = DecisionTracker()
    t.record("t1", "r1", "entered", 1)

    t.counts("t1")["entered"] = 999

    assert t.counts("t1") == {"entered": 1}
