# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""前提规则: 只提供条件、不产边沿, 进入时要求它成立。

前提只管进入。进去之后条件变假不退出 —— 那是 exit 型 rule 的事。
"""

from __future__ import annotations

import logging

from miloco.task.state_machine import (
    ActionSlot,
    RuleDirection,
    SignalKind,
    TaskRuntimeState,
    TaskSignal,
    TaskStateMachine,
    TaskTopology,
    TransitionOutcome,
    slot_for_edge,
)


class Harness:
    def __init__(self, satisfied: dict[str, bool | None] | None = None):
        self.satisfied = satisfied or {}
        self.dispatched: list[tuple[str, ActionSlot]] = []
        self.tracked: list[tuple[TransitionOutcome, TaskSignal]] = []
        self.sm = TaskStateMachine(
            is_condition_satisfied=lambda rid: self.satisfied.get(rid, False),
            dispatch_action=lambda t, s, _p=None: self.dispatched.append((t, s)),
            track=lambda o, s: self.tracked.append((o, s)),
        )


def _entered(rule_id="r_enter", slot=ActionSlot.ON_ENTER):
    return TaskSignal("t1", rule_id, SignalKind.ENTERED, slot)


# ── 前提不产信号 ──────────────────────────────────────────────────


def test_guard_produces_no_enter_signal():
    assert slot_for_edge(RuleDirection.GUARD, SignalKind.ENTERED) is None


def test_guard_produces_no_exit_signal():
    assert slot_for_edge(RuleDirection.GUARD, SignalKind.EXITED) is None


# ── 前提不是进/出路径 ──────────────────────────────────────────────


def test_guard_is_not_an_entry_path():
    topo = TaskTopology("t1", {"g": RuleDirection.GUARD})
    assert topo.enter_side_rule_ids == set()


def test_guard_is_not_an_exit_path():
    topo = TaskTopology("t1", {"g": RuleDirection.GUARD})
    assert topo.exit_side_rule_ids == set()


def test_guard_does_not_hold_a_session():
    topo = TaskTopology("t1", {"g": RuleDirection.GUARD})
    assert topo.holding_rule_ids == set()


def test_guard_does_not_make_it_session_type():
    topo = TaskTopology("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    assert topo.is_session_type is False


def test_guard_rule_ids_collects_only_guards():
    topo = TaskTopology(
        "t1",
        {
            "a": RuleDirection.ENTER,
            "g": RuleDirection.GUARD,
            "x": RuleDirection.EXIT,
        },
    )
    assert topo.guard_rule_ids == {"g"}


# ── 事件型 task 的前提 ────────────────────────────────────────────
# 事件型在 _handle_enter 最前面就 return, 前提判断必须排在它之前。


def test_event_type_fires_when_guard_holds():
    h = Harness({"g": True})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})

    assert h.sm.handle(_entered("a")) is TransitionOutcome.EVENT_FIRED


def test_event_type_blocked_when_guard_false():
    h = Harness({"g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})

    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD


def test_blocked_event_type_dispatches_nothing():
    h = Harness({"g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    h.sm.handle(_entered("a"))

    assert h.dispatched == []


# ── session 型 task 的前提 ────────────────────────────────────────


def test_session_type_enters_when_guard_holds():
    h = Harness({"g": True})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "g": RuleDirection.GUARD})

    assert h.sm.handle(_entered("s")) is TransitionOutcome.ENTERED


def test_session_type_blocked_when_guard_false():
    h = Harness({"g": False})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "g": RuleDirection.GUARD})

    assert h.sm.handle(_entered("s")) is TransitionOutcome.BLOCKED_BY_GUARD


def test_blocked_session_stays_off():
    h = Harness({"g": False})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "g": RuleDirection.GUARD})
    h.sm.handle(_entered("s"))

    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF


# ── 未就绪不放行 ──────────────────────────────────────────────────


def test_unknown_guard_blocks():
    """设备离线 / 属性没对齐 → 不放行。

    放行的话前提在设备离线期间静默失效, 而用户看不出来; 不放行的表现是规则完全
    不响, 当场能发现。
    """
    h = Harness({"g": None})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})

    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD


# ── 多条前提取合取 ────────────────────────────────────────────────


def test_all_guards_must_hold():
    h = Harness({"g1": True, "g2": False})
    h.sm.register_task(
        "t1",
        {"a": RuleDirection.ENTER, "g1": RuleDirection.GUARD, "g2": RuleDirection.GUARD},
    )

    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD


def test_fires_when_every_guard_holds():
    h = Harness({"g1": True, "g2": True})
    h.sm.register_task(
        "t1",
        {"a": RuleDirection.ENTER, "g1": RuleDirection.GUARD, "g2": RuleDirection.GUARD},
    )

    assert h.sm.handle(_entered("a")) is TransitionOutcome.EVENT_FIRED


# ── 前提只管进入 ──────────────────────────────────────────────────


def test_guard_does_not_block_exit():
    """进去之后前提变假不该拦住退出 —— 拦住就成了开了永远不关。"""
    h = Harness({"g": True})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "g": RuleDirection.GUARD})
    h.sm.handle(_entered("s"))
    h.satisfied["g"] = False

    outcome = h.sm.handle(
        TaskSignal("t1", "s", SignalKind.EXITED, ActionSlot.ON_EXIT)
    )
    assert outcome is TransitionOutcome.EXITED


def test_guard_does_not_affect_tasks_without_one():
    h = Harness()
    h.sm.register_task("t1", {"a": RuleDirection.ENTER})

    assert h.sm.handle(_entered("a")) is TransitionOutcome.EVENT_FIRED


def test_blocked_log_tells_unready_from_not_satisfied(caplog):
    """被拦下时日志要分得出设备离线和条件真的不满足 —— 两种修法不同, 而这次进入
    没有补发路径, 日志是事后唯一的线索。"""
    h = Harness({"g_off": False, "g_unknown": None})
    h.sm.register_task(
        "t1",
        {
            "a": RuleDirection.ENTER,
            "g_off": RuleDirection.GUARD,
            "g_unknown": RuleDirection.GUARD,
        },
    )

    with caplog.at_level(logging.INFO, logger="miloco.task.state_machine"):
        h.sm.handle(_entered("a"))

    assert "g_off=不成立" in caplog.text
    assert "g_unknown=未就绪" in caplog.text
