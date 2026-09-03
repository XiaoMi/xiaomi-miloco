# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task 运行态状态机测试.

覆盖 §5 的三种形态、§5.1 的稳态交叉判定、§5.2（本次不实现，见 state_machine 退出分支的注释）、§5.3 的 milestone
路由、§19.4 的队列溢出、§19.5 的重新配置、§19.6 的动作失败不反噬。
"""

from __future__ import annotations

import asyncio

import pytest
from miloco.task.state_machine import (
    SIGNAL_QUEUE_DEPTH,
    ActionSlot,
    RuleDirection,
    SignalKind,
    TaskRuntimeState,
    TaskSignal,
    TaskStateMachine,
    TaskTopology,
    TransitionOutcome,
    derive_directions,
)


class Harness:
    """把四个注入点收成可断言的记录。"""

    def __init__(self, satisfied: dict[str, bool | None] | None = None):
        # 默认 False 而非 None: 用 .get 的 None 兜底会让每个未显式登记的 rule 都
        # 处在"未就绪"态, 任何与 None 有关的改动都波及全部用例, 变异校验分不出锅。
        self.satisfied = satisfied or {}
        self.dispatched: list[tuple[str, ActionSlot]] = []
        self.tracked: list[tuple[TransitionOutcome, TaskSignal]] = []
        self.sm = TaskStateMachine(
            is_condition_satisfied=lambda rid: self.satisfied.get(rid, False),
            dispatch_action=lambda t, s, _p=None: self.dispatched.append((t, s)),
            track=lambda o, s: self.tracked.append((o, s)),
        )

    def outcomes(self) -> list[TransitionOutcome]:
        return [o for o, _ in self.tracked]


def _entered(task_id="t1", rule_id="r_enter", slot=ActionSlot.ON_ENTER):
    """slot 由确认层算好传进来, 这里显式写期望值 —— 复用 ``slot_for_edge`` 会让
    映射错了测试照样绿。"""
    return TaskSignal(task_id, rule_id, SignalKind.ENTERED, slot)


def _exited(task_id="t1", rule_id="r_enter", slot=ActionSlot.ON_EXIT):
    return TaskSignal(task_id, rule_id, SignalKind.EXITED, slot)


# ── 形态推导 (§4.3) ───────────────────────────────────────────────────


def test_all_enter_is_event_type():
    topo = TaskTopology("t1", {"a": RuleDirection.ENTER, "b": RuleDirection.ENTER})
    assert topo.is_session_type is False


def test_session_alone_is_session_type():
    topo = TaskTopology("t1", {"a": RuleDirection.SESSION})
    assert topo.is_session_type is True


def test_enter_plus_exit_is_session_type():
    topo = TaskTopology("t1", {"a": RuleDirection.ENTER, "b": RuleDirection.EXIT})
    assert topo.is_session_type is True


def test_milestone_does_not_make_it_session_type():
    """milestone 不构成出路径, 只挂它的 task 仍是事件型 (§4.3)。"""
    topo = TaskTopology("t1", {"a": RuleDirection.ENTER, "m": RuleDirection.MILESTONE})
    assert topo.is_session_type is False


# ── 事件型: 恒 off, 每次都执行 ────────────────────────────────────────


def test_event_type_stays_off_and_fires_every_time():
    h = Harness()
    h.sm.register_task("t1", {"r": RuleDirection.ENTER})

    for _ in range(3):
        assert h.sm.handle(_entered(rule_id="r")) is TransitionOutcome.EVENT_FIRED

    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.dispatched == [("t1", ActionSlot.ON_ENTER)] * 3


# ── 对称模式 (session) ────────────────────────────────────────────────


def test_session_enters_and_exits():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})

    assert h.sm.handle(_entered(rule_id="s")) is TransitionOutcome.ENTERED
    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON
    assert h.sm.handle(_exited(rule_id="s")) is TransitionOutcome.EXITED
    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.dispatched == [
        ("t1", ActionSlot.ON_ENTER),
        ("t1", ActionSlot.ON_EXIT),
    ]


def test_session_rule_does_not_block_itself():
    """对称模式下进出是同一条 rule; 拿自己的条件当"对侧"会永远进不去。"""
    h = Harness(satisfied={"s": True})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})

    assert h.sm.handle(_entered(rule_id="s")) is TransitionOutcome.ENTERED


def test_second_enter_is_idempotent():
    """多条路径同时进, 边界动作只执行一次。"""
    h = Harness()
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})

    h.sm.handle(_entered(rule_id="a"))
    assert h.sm.handle(_entered(rule_id="a")) is TransitionOutcome.ALREADY_IN_STATE
    assert h.dispatched.count(("t1", ActionSlot.ON_ENTER)) == 1


def test_exit_when_already_off_is_noop():
    """记成 ALREADY_OFF 而不是 ALREADY_IN_STATE —— 后者的判定摘要正好说反。"""
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})

    assert h.sm.handle(_exited(rule_id="s")) is TransitionOutcome.ALREADY_OFF
    assert h.dispatched == []


# ── 非互反模式 + §5.1 稳态交叉判定 ────────────────────────────────────


def test_exit_rule_entered_edge_is_an_exit_signal():
    """exit 型 rule 的"条件成立"就是"该退出了"。"""
    h = Harness()
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})
    h.sm.handle(_entered(rule_id="a"))

    assert (
        h.sm.handle(_entered(rule_id="x", slot=ActionSlot.ON_EXIT))
        is TransitionOutcome.EXITED
    )
    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF


def test_entry_blocked_when_exit_condition_already_true():
    """§5.1: 不查对侧稳态的话, 这里会进 on 且永远退不出。"""
    h = Harness(satisfied={"x": True})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})

    assert (
        h.sm.handle(_entered(rule_id="a"))
        is TransitionOutcome.BLOCKED_BY_EXIT_CONDITION
    )
    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.dispatched == []


def test_exit_held_when_another_source_still_true():
    """OR 的退出条件是「全部都不成立」。

    两条 session 挂同一 task 会被 task 全貌校验拒(session 必须独占), 所以本例钉
    的是状态机自己的契约, 不是能配出来的场景 —— 合法配置下"别的会话条件"这个集合
    恒为空, 这道闸不会触发。见 ``_other_session_holds`` 的注释。
    """
    h = Harness(satisfied={"s1": False, "s2": False})
    h.sm.register_task("t1", {"s1": RuleDirection.SESSION, "s2": RuleDirection.SESSION})
    h.sm.handle(_entered(rule_id="s1"))
    h.satisfied["s2"] = True  # 另一条的条件此刻成立
    h.dispatched.clear()

    outcome = h.sm.handle(_exited(rule_id="s1"))

    assert outcome is TransitionOutcome.STILL_HELD
    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON
    assert h.dispatched == []


def test_two_exit_rules_both_true_can_still_exit():
    """两条 exit 同时成立时, 任一条的边沿都要能把 task 退出去。

    exit 型的"条件成立"是"该退出了"。拿它当"还撑着"极性正好反 —— 两条会互相判
    STILL_HELD, 谁也出不去, 而「一条 enter + 两条 exit」是合法配置。
    """
    h = Harness(satisfied={"a": True, "x1": True, "x2": True})
    h.sm.register_task(
        "t1",
        {
            "a": RuleDirection.ENTER,
            "x1": RuleDirection.EXIT,
            "x2": RuleDirection.EXIT,
        },
    )
    # 进入时出口条件必须为假, 否则被 §5.1 拦住 —— 那是另一条闸。
    h.satisfied["x1"] = False
    h.satisfied["x2"] = False
    assert h.sm.handle(_entered(rule_id="a")) is TransitionOutcome.ENTERED
    h.dispatched.clear()

    # 用户离开时顺手关灯: 两个退出条件同一拍变真。
    h.satisfied["x1"] = True
    h.satisfied["x2"] = True
    outcome = h.sm.handle(_exited(rule_id="x1"))

    assert outcome is TransitionOutcome.EXITED
    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.dispatched == [("t1", ActionSlot.ON_EXIT)]


def test_reconfigure_does_not_let_a_true_exit_condition_keep_the_task_on():
    """重新配置时, exit 条件为真不表示"还撑着"。

    把它算成撑着的话, 一个 enter + exit 的 task 在退出条件成立期间改配置会被判定
    "保持 on", 而现实是该关的 —— 从此卡住。
    """
    h = Harness(satisfied={"a": False, "x": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})
    h.sm.handle(_entered(rule_id="a"))
    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON
    # 进去之后退出条件才成立 —— 进入那一刻为真会被 §5.1 拦掉, 到不了这里。
    h.satisfied["x"] = True
    h.dispatched.clear()

    h.sm.reconfigure("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})

    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.dispatched == [("t1", ActionSlot.ON_EXIT)]


def test_exit_proceeds_when_nothing_else_holds():
    """出口侧全都不成立 → 照常退出并派发 on_exit。"""
    h = Harness(satisfied={"s1": False, "s2": False})
    h.sm.register_task("t1", {"s1": RuleDirection.SESSION, "s2": RuleDirection.SESSION})
    h.sm.handle(_entered(rule_id="s1"))
    h.dispatched.clear()

    outcome = h.sm.handle(_exited(rule_id="s1"))

    assert outcome is TransitionOutcome.EXITED
    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.dispatched == [("t1", ActionSlot.ON_EXIT)]


def test_single_rule_exit_not_blocked_by_its_own_condition():
    """单 rule: 排除自己后出口侧为空, 行为与加这道检查之前逐字相同。"""
    h = Harness(satisfied={"s": True})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.handle(_entered(rule_id="s"))
    h.dispatched.clear()

    outcome = h.sm.handle(_exited(rule_id="s"))

    assert outcome is TransitionOutcome.EXITED
    assert h.dispatched == [("t1", ActionSlot.ON_EXIT)]


def test_unknown_steady_state_does_not_block_entry():
    """None = 未就绪 / 脉冲型无稳态。拿"不知道"当"成立"会把正常进入全挡掉。"""
    h = Harness(satisfied={"x": None})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})

    assert h.sm.handle(_entered(rule_id="a")) is TransitionOutcome.ENTERED


def test_false_steady_state_does_not_block_entry():
    h = Harness(satisfied={"x": False})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})

    assert h.sm.handle(_entered(rule_id="a")) is TransitionOutcome.ENTERED


# ── §5.2 基线重置 ─────────────────────────────────────────────────────


def test_exit_flips_the_task_off():
    h = Harness()
    h.sm.register_task(
        "t1",
        {"a": RuleDirection.ENTER, "b": RuleDirection.ENTER, "x": RuleDirection.EXIT},
    )
    h.sm.handle(_entered(rule_id="a"))
    h.sm.handle(_entered(rule_id="x", slot=ActionSlot.ON_EXIT))

    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF


# ── §5.3 milestone ────────────────────────────────────────────────────


def test_milestone_fires_only_in_session():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "m": RuleDirection.MILESTONE})
    h.sm.handle(_entered(rule_id="s"))

    assert (
        h.sm.handle(_entered(rule_id="m", slot=ActionSlot.ON_TARGET))
        is TransitionOutcome.MILESTONE_FIRED
    )
    assert ("t1", ActionSlot.ON_TARGET) in h.dispatched


def test_milestone_dropped_when_off():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "m": RuleDirection.MILESTONE})

    assert (
        h.sm.handle(_entered(rule_id="m", slot=ActionSlot.ON_TARGET))
        is TransitionOutcome.NOT_IN_SESSION
    )
    assert h.dispatched == []


def test_milestone_does_not_change_state_or_reset_baseline():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "m": RuleDirection.MILESTONE})
    h.sm.handle(_entered(rule_id="s"))
    h.sm.handle(_entered(rule_id="m", slot=ActionSlot.ON_TARGET))

    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON


# ── §19.4 队列 ────────────────────────────────────────────────────────


def test_queue_overflow_drops_oldest_and_tracks():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})

    for i in range(SIGNAL_QUEUE_DEPTH + 2):
        h.sm.submit(TaskSignal("t1", f"s{i}", SignalKind.ENTERED, ActionSlot.ON_ENTER))

    assert h.outcomes().count(TransitionOutcome.SIGNAL_DROPPED) == 2


def test_submit_to_unknown_task_is_tracked_not_raised():
    h = Harness()
    h.sm.submit(_entered(task_id="nope"))

    assert h.outcomes() == [TransitionOutcome.UNKNOWN_RULE]


@pytest.mark.asyncio
async def test_consumer_drains_queue_serially():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.start("t1")

    h.sm.submit(_entered(rule_id="s"))
    h.sm.submit(_exited(rule_id="s"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert h.dispatched == [
        ("t1", ActionSlot.ON_ENTER),
        ("t1", ActionSlot.ON_EXIT),
    ]
    h.sm.shutdown()


# ── §19.5 重新配置 ────────────────────────────────────────────────────


def test_reconfigure_runs_on_exit_when_losing_all_exit_paths():
    """在 on 时把出路径改没了, 不跑 on_exit 就永远退不出。"""
    h = Harness()
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})
    h.sm.handle(_entered(rule_id="a"))
    h.dispatched.clear()

    h.sm.reconfigure("t1", {"a": RuleDirection.ENTER})

    assert h.dispatched == [("t1", ActionSlot.ON_EXIT)]


def test_reconfigure_while_off_does_not_run_on_exit():
    h = Harness()
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})

    h.sm.reconfigure("t1", {"a": RuleDirection.ENTER})

    assert h.dispatched == []


def test_reconfigure_keeps_on_when_exit_side_still_holds():
    """配置变了而现实没变 —— 改个防抖参数不该把 on 打成 off。

    清运行态会同时丢两样: 那次退出的 on_exit, 以及与 runner 边沿的一致性 ——
    runner 仍认为在态内、不会再发 enter 信号, 两边就此分叉且不会自愈。
    """
    h = Harness(satisfied={"s": True})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.handle(_entered(rule_id="s"))
    h.dispatched.clear()

    h.sm.reconfigure("t1", {"s": RuleDirection.SESSION})

    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON
    assert h.dispatched == []


def test_reconfigure_exits_when_exit_side_no_longer_holds():
    """撑着 on 的那条 rule 被删了 → 该退出, 且退出动作不能漏。"""
    h = Harness(satisfied={"s": True, "s2": False})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "s2": RuleDirection.SESSION})
    h.sm.handle(_entered(rule_id="s"))
    h.dispatched.clear()

    h.sm.reconfigure("t1", {"s2": RuleDirection.SESSION})

    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.dispatched == [("t1", ActionSlot.ON_EXIT)]


def test_reconfigure_ignores_unseeded_rule_when_deciding():
    """新加进来的 rule 还没喂过数据, 不能拿它否掉老 rule 撑着的 on。"""
    h = Harness(satisfied={"s": True, "new": None})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.handle(_entered(rule_id="s"))
    h.dispatched.clear()

    h.sm.reconfigure("t1", {"s": RuleDirection.SESSION, "new": RuleDirection.SESSION})

    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON
    assert h.dispatched == []


def test_reconfigure_exits_when_whole_exit_side_unseeded():
    """整套 rule 被换掉 → 无从确认还撑着, 保守退出而不是静默卡在 on。"""
    h = Harness(satisfied={"s": True, "new": None})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.handle(_entered(rule_id="s"))
    h.dispatched.clear()

    h.sm.reconfigure("t1", {"new": RuleDirection.SESSION})

    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.dispatched == [("t1", ActionSlot.ON_EXIT)]


def test_suspend_clears_state_without_running_on_exit():
    """停用是「停止观察」: 清运行态让 enable 后重建, 但不当作观察到退出。"""
    h = Harness(satisfied={"s": True})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.handle(_entered(rule_id="s"))
    h.dispatched.clear()

    h.sm.suspend("t1")

    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert h.dispatched == []


def test_reconfigure_drops_queued_signals():
    """旧信号对应改动前的配置, 排队只会把过期信号灌进新配置。"""
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.submit(_entered(rule_id="s"))
    h.sm.submit(_entered(rule_id="s"))

    h.sm.reconfigure("t1", {"s": RuleDirection.SESSION})

    assert h.outcomes().count(TransitionOutcome.SIGNAL_DROPPED) == 2


# ── §19.6 动作失败不反噬 ──────────────────────────────────────────────


def test_dispatch_failure_does_not_roll_back_state():
    """回滚会让下一帧重新触发, 形成无限重试。"""
    sm = TaskStateMachine(
        is_condition_satisfied=lambda _: None,
        dispatch_action=lambda t, s, _p=None: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )
    sm.register_task("t1", {"s": RuleDirection.SESSION})

    # handle 吞掉异常不把消费链带死, 但状态已经写进去了
    sm.handle(_entered(rule_id="s"))

    assert sm.runtime_state("t1") is TaskRuntimeState.ON


# ── 手动触发 ──────────────────────────────────────────────────────────


def test_manual_inject_leaves_an_event_task_off():
    """事件型 task 注入进入信号后运行态仍是 off。

    无条件置 on 的话它永久停在 on(没有出路径, 收不到退信号), 而达标只看运行态 ——
    本该恒不触发的达标从此开始触发。
    """
    h = Harness(satisfied={"a": True})
    h.sm.register_task("t1", {"a": RuleDirection.ENTER})

    outcome = h.sm.manual_inject("t1", ActionSlot.ON_ENTER)

    assert outcome is TransitionOutcome.ENTERED
    assert h.dispatched == [("t1", ActionSlot.ON_ENTER)]
    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF


def test_manual_inject_turns_a_session_task_on():
    """会话型仍要置 on —— 上面那条不能靠"注入从不改状态"通过。"""
    h = Harness(satisfied={"s": False})
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})

    h.sm.manual_inject("t1", ActionSlot.ON_ENTER)

    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON


def test_manual_inject_does_not_reset_baseline():
    """调试入口不代表"感知到条件不再满足", 不重置基线 (§5)。"""
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.manual_inject("t1", ActionSlot.ON_ENTER)
    h.sm.manual_inject("t1", ActionSlot.ON_EXIT)

    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF


# ── 杂项 ──────────────────────────────────────────────────────────────


def test_derive_directions_skips_unknown():
    out = derive_directions([("a", "enter"), ("b", "nonsense"), ("c", "session")])

    assert out == {"a": RuleDirection.ENTER, "c": RuleDirection.SESSION}


def test_unregister_clears_everything():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.handle(_entered(rule_id="s"))
    h.sm.unregister_task("t1")

    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
    h.sm.submit(_entered(rule_id="s"))
    assert h.outcomes()[-1] is TransitionOutcome.UNKNOWN_RULE


# ── 方向映射：确认层的最后一步 (§2.1 ③层) ──────────────────────────────


def test_slot_for_edge_full_table():
    """四个方向 × 两种边沿的全表。这是 ③→④ 的唯一契约, 写死不靠推导。"""
    from miloco.task.state_machine import slot_for_edge

    table = {
        (d, k.value): slot_for_edge(d, k)
        for d in ("enter", "exit", "session", "milestone")
        for k in (SignalKind.ENTERED, SignalKind.EXITED)
    }
    assert table == {
        ("enter", "entered"): ActionSlot.ON_ENTER,
        ("enter", "exited"): None,
        ("exit", "entered"): ActionSlot.ON_EXIT,
        ("exit", "exited"): None,
        ("session", "entered"): ActionSlot.ON_ENTER,
        ("session", "exited"): ActionSlot.ON_EXIT,
        ("milestone", "entered"): ActionSlot.ON_TARGET,
        ("milestone", "exited"): None,
    }


def test_task_layer_dispatches_by_signal_slot_not_by_direction():
    """task 层按 ``signal.slot`` 分派, 不再自己查方向。

    喂一个「登记为 enter 型、但 slot 说这是出信号」的信号: 若本层还在查方向,
    它会走进入分支 (already_in_state); 按 slot 走才会真的退出。
    """
    h = Harness()
    # 要有出路径才谈得上「模式开着」—— 纯 enter 型 task 是事件型, 恒 off (§4.3)
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})
    h.sm.handle(_entered(rule_id="a"))
    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON

    outcome = h.sm.handle(_entered(rule_id="a", slot=ActionSlot.ON_EXIT))

    assert outcome is TransitionOutcome.EXITED
    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF


def test_reconfigure_keeps_an_enter_exit_task_on_when_no_exit_condition_holds():
    """enter + exit 型没有会话条件, 不能拿"有没有条件撑着"去问。

    问了答案恒为否 —— 改个防抖参数都会把 task 打成退出、白跑一次退出动作, 而那次
    动作是真会对外下指令的。它该看的是"出口条件都没成立"。
    """
    h = Harness()
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})
    h.sm.handle(_entered(rule_id="a"))
    h.dispatched.clear()
    h.satisfied["x"] = False

    h.sm.reconfigure("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})

    assert h.dispatched == []
    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON


def test_reconfigure_exits_an_enter_exit_task_whose_exit_condition_is_true():
    """出口条件确实成立 → 该退。"""
    h = Harness()
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})
    h.sm.handle(_entered(rule_id="a"))
    h.dispatched.clear()
    h.satisfied["x"] = True

    h.sm.reconfigure("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})

    assert h.dispatched == [("t1", ActionSlot.ON_EXIT)]
    assert h.sm.runtime_state("t1") is TaskRuntimeState.OFF
