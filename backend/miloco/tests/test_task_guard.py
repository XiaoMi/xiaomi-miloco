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


class _Clock:
    """可手动推进的单调时钟 —— 超时用例不 sleep。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Harness:
    def __init__(
        self,
        satisfied: dict[str, bool | None] | None = None,
        *,
        timeout: float = 60.0,
        clock: _Clock | None = None,
    ):
        self.satisfied = satisfied or {}
        self.dispatched: list[tuple[str, ActionSlot]] = []
        self.tracked: list[tuple[TransitionOutcome, TaskSignal]] = []
        self.clock = clock or _Clock()
        self.sm = TaskStateMachine(
            is_condition_satisfied=lambda rid: self.satisfied.get(rid, False),
            dispatch_action=lambda t, s, _p=None: self.dispatched.append((t, s)),
            track=lambda o, s: self.tracked.append((o, s)),
            defer_blocked_enter_timeout_sec=timeout,
            clock=self.clock,
        )


def _entered(rule_id="r_enter", slot=ActionSlot.ON_ENTER, payload=None):
    return TaskSignal("t1", rule_id, SignalKind.ENTERED, slot, payload)


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


def test_blocked_enter_is_logged_once_while_waiting_for_guard(caplog):
    h = Harness({"g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})

    with caplog.at_level(logging.INFO, logger="miloco.task.state_machine"):
        assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD
        assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD

    blocked_logs = [
        record for record in caplog.records if "的进入被前提拦下" in record.message
    ]
    assert len(blocked_logs) == 1


def test_blocked_event_type_dispatches_nothing():
    h = Harness({"g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    h.sm.handle(_entered("a"))

    assert h.dispatched == []


def test_blocked_event_can_be_released_after_guard_becomes_true():
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    signal = _entered("a", payload={"caption": "原始上下文"})

    assert h.sm.handle(signal) is TransitionOutcome.BLOCKED_BY_GUARD

    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == [signal]
    assert h.sm.release_pending_enters("t1") == []


def test_blocked_event_releases_all_pending_enters_after_guard_becomes_true():
    h = Harness({"a": True, "b": True, "g": False})
    h.sm.register_task(
        "t1",
        {
            "a": RuleDirection.ENTER,
            "b": RuleDirection.ENTER,
            "g": RuleDirection.GUARD,
        },
    )
    first = _entered("a", payload={"caption": "第一条"})
    second = _entered("b", payload={"caption": "第二条"})

    assert h.sm.handle(first) is TransitionOutcome.BLOCKED_BY_GUARD
    assert h.sm.handle(second) is TransitionOutcome.BLOCKED_BY_GUARD

    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == [first, second]
    assert h.sm.release_pending_enters("t1") == []


def test_releasing_event_keeps_unknown_sibling_pending():
    h = Harness({"a": True, "b": True, "g": False})
    h.sm.register_task(
        "t1",
        {
            "a": RuleDirection.ENTER,
            "b": RuleDirection.ENTER,
            "g": RuleDirection.GUARD,
        },
    )
    first = _entered("a")
    second = _entered("b")

    assert h.sm.handle(first) is TransitionOutcome.BLOCKED_BY_GUARD
    assert h.sm.handle(second) is TransitionOutcome.BLOCKED_BY_GUARD

    h.satisfied["b"] = None
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == [first]

    h.satisfied["b"] = True
    assert h.sm.release_pending_enters("t1") == [second]


def test_event_fire_keeps_unknown_sibling_pending():
    h = Harness({"a": False, "b": True, "g": False})
    h.sm.register_task(
        "t1",
        {
            "a": RuleDirection.ENTER,
            "b": RuleDirection.ENTER,
            "g": RuleDirection.GUARD,
        },
    )
    pending = _entered("b")

    assert h.sm.handle(pending) is TransitionOutcome.BLOCKED_BY_GUARD

    h.satisfied["b"] = None
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == []

    h.satisfied["a"] = True
    assert h.sm.handle(_entered("a")) is TransitionOutcome.EVENT_FIRED

    h.satisfied["b"] = True
    assert h.sm.release_pending_enters("t1") == [pending]


def test_blocked_session_release_enters_once_and_preserves_payload():
    h = Harness({"s": True, "g": False})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "g": RuleDirection.GUARD})
    signal = _entered("s", payload={"room": "客厅"})

    assert h.sm.handle(signal) is TransitionOutcome.BLOCKED_BY_GUARD
    h.satisfied["g"] = True

    released = h.sm.release_pending_enters("t1")
    assert released == [signal]
    assert released[0].payload == {"room": "客厅"}
    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON
    assert h.sm.release_pending_enters("t1") == []


def test_pending_enter_is_dropped_when_main_rule_is_no_longer_true():
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    signal = _entered("a")

    assert h.sm.handle(signal) is TransitionOutcome.BLOCKED_BY_GUARD
    h.satisfied["a"] = False
    h.satisfied["g"] = True

    assert h.sm.release_pending_enters("t1") == []


def test_reconfigure_preserves_pending_enter_that_remains_an_entry_rule():
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    signal = _entered("a")
    assert h.sm.handle(signal) is TransitionOutcome.BLOCKED_BY_GUARD

    h.sm.reconfigure("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == [signal]


def test_reconfigure_drops_pending_enter_removed_from_entry_topology():
    h = Harness({"g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD

    h.sm.reconfigure("t1", {"g": RuleDirection.GUARD})
    assert h.sm.release_pending_enters("t1") == []


def test_suspend_clears_pending_enter():
    h = Harness({"g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD

    h.satisfied["g"] = False
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD
    h.sm.suspend("t1")
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == []


# ── 挂起超时 ──────────────────────────────────────────────────────
# deadline = 最近一次被拦下观测 + 超时, 每次重问续期。到期条目在释放口丢弃,
# 不补发, 也不留在挂起表里。


def test_blocked_event_is_released_before_deadline():
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    signal = _entered("a")
    assert h.sm.handle(signal) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(59.9)
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == [signal]


def test_blocked_event_expires_at_deadline():
    """到点即作废（边界取 >=），不能在超时秒数上再补发一次。"""
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(60.0)
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == []
    assert h.dispatched == []
    assert h.sm.release_pending_enters("t1") == []
    assert [o for o, _ in h.tracked].count(TransitionOutcome.EXPIRED_BLOCKED_ENTER) == 1


def test_blocked_event_expires_while_guard_stays_false():
    """前提一直不成立时条目也要清掉 —— 否则它会永远留在挂起表里。"""
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(60.0)
    assert h.sm.release_pending_enters("t1") == []
    assert h.tracked[-1][0] is TransitionOutcome.EXPIRED_BLOCKED_ENTER


def test_blocked_event_expires_while_condition_is_unknown():
    """主条件未就绪时过期判定也要生效 —— 不能停在「继续等」分支里。"""
    h = Harness({"a": None, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(60.0)
    assert h.sm.release_pending_enters("t1") == []
    assert h.tracked[-1][0] is TransitionOutcome.EXPIRED_BLOCKED_ENTER


def test_requestion_refreshes_deadline():
    """只要还在被观测（每次重问都是一次新观测），超时就不该生效。"""
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    signal = _entered("a", payload={"caption": "首次"})
    assert h.sm.handle(signal) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(50.0)
    assert (
        h.sm.handle(_entered("a", payload={"caption": "重问"}))
        is TransitionOutcome.BLOCKED_BY_GUARD
    )

    h.clock.advance(50.0)  # t=100，早于第二次观测的 deadline=110
    h.satisfied["g"] = True
    released = h.sm.release_pending_enters("t1")
    # 续期只刷新 deadline，信号仍是首次观测的那条（session 起点按首次成立计）。
    assert len(released) == 1
    assert released[0] is signal


def test_requestion_past_deadline_refreshes_instead_of_expiring():
    """过期只在释放口检查: 中间没被检查时, 超时后的重问仍是一次新观测。"""
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    signal = _entered("a", payload={"caption": "首次"})
    assert h.sm.handle(signal) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(60.0)
    assert (
        h.sm.handle(_entered("a", payload={"caption": "超时后重问"}))
        is TransitionOutcome.BLOCKED_BY_GUARD
    )
    assert all(o is not TransitionOutcome.EXPIRED_BLOCKED_ENTER for o, _ in h.tracked)

    h.clock.advance(10.0)
    h.satisfied["g"] = True
    released = h.sm.release_pending_enters("t1")
    assert len(released) == 1
    assert released[0] is signal


def test_expired_enter_is_replaced_by_the_next_observation():
    """过期后新的重问是一次新观测: 用新信号, 而不是旧的那条。"""
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    old = _entered("a", payload={"caption": "旧"})
    assert h.sm.handle(old) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(60.0)
    assert h.sm.release_pending_enters("t1") == []

    new = _entered("a", payload={"caption": "新"})
    assert h.sm.handle(new) is TransitionOutcome.BLOCKED_BY_GUARD
    h.clock.advance(10.0)
    h.satisfied["g"] = True
    released = h.sm.release_pending_enters("t1")
    # TaskSignal.payload 是 compare=False, 用列表相等分辨不出新旧信号。
    assert len(released) == 1
    assert released[0] is new


def test_zero_timeout_never_defers():
    """0 = 不补发: 被拦下的进入直接丢弃, 前提之后成立也不释放。

    但实况评估不受影响 —— 前提成立后新来一次进入仍可正常 fire。
    """
    h = Harness({"a": True, "g": False}, timeout=0.0)
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(10_000.0)
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == []
    assert all(o is not TransitionOutcome.EXPIRED_BLOCKED_ENTER for o, _ in h.tracked)

    assert h.sm.handle(_entered("a")) is TransitionOutcome.EVENT_FIRED


def test_negative_timeout_never_expires():
    """负数 = 不设上限: 前提晚多久成立都还能补发。"""
    h = Harness({"a": True, "g": False}, timeout=-1.0)
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    signal = _entered("a")
    assert h.sm.handle(signal) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(365 * 24 * 3600.0)
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == [signal]


def test_blocked_session_expires_without_entering():
    h = Harness({"s": True, "g": False})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("s")) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(60.0)
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == []
    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.tracked[-1][0] is TransitionOutcome.EXPIRED_BLOCKED_ENTER


def test_reconfigure_preserves_the_original_deadline():
    """重配保留挂起项, 但不等于续期 —— deadline 原样保留。"""
    h = Harness({"a": True, "g": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("a")) is TransitionOutcome.BLOCKED_BY_GUARD

    h.clock.advance(30.0)
    h.sm.reconfigure("t1", {"a": RuleDirection.ENTER, "g": RuleDirection.GUARD})
    h.clock.advance(30.0)  # t=60, 与首次观测的 deadline 相同
    h.satisfied["g"] = True
    assert h.sm.release_pending_enters("t1") == []
    assert h.tracked[-1][0] is TransitionOutcome.EXPIRED_BLOCKED_ENTER


# ── session 型 task 的前提 ────────────────────────────────────────


def test_session_type_enters_when_guard_holds():
    h = Harness({"g": True})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "g": RuleDirection.GUARD})

    assert h.sm.handle(_entered("s")) is TransitionOutcome.ENTERED


def test_session_type_blocked_when_guard_false():
    h = Harness({"g": False})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "g": RuleDirection.GUARD})

    assert h.sm.handle(_entered("s")) is TransitionOutcome.BLOCKED_BY_GUARD


def test_live_session_repeat_enter_is_not_reported_as_blocked():
    """已经在 on 的 task 这次没有进入可拦, 判定摘要不能说它被前提挡住。"""
    h = Harness({"g": True})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "g": RuleDirection.GUARD})
    assert h.sm.handle(_entered("s")) is TransitionOutcome.ENTERED

    h.satisfied["g"] = False

    assert h.sm.handle(_entered("s")) is TransitionOutcome.ALREADY_IN_STATE


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
    """被拦下时日志要分得出设备离线和条件真的不满足 —— 两种修法不同。"""
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
