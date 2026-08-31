# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task 运行态状态机测试.

覆盖 §5 的三种形态、§5.1 的稳态交叉判定、§5.2 的基线重置、§5.3 的 milestone
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
        self.baseline_resets: list[str] = []
        self.dispatched: list[tuple[str, ActionSlot]] = []
        self.tracked: list[tuple[TransitionOutcome, TaskSignal]] = []
        self.sm = TaskStateMachine(
            is_condition_satisfied=lambda rid: self.satisfied.get(rid, False),
            reset_edge_baseline=self.baseline_resets.append,
            dispatch_action=lambda t, s, _p=None: self.dispatched.append((t, s)),
            track=lambda o, s: self.tracked.append((o, s)),
        )

    def outcomes(self) -> list[TransitionOutcome]:
        return [o for o, _ in self.tracked]


def _entered(task_id="t1", rule_id="r_enter"):
    return TaskSignal(task_id, rule_id, SignalKind.ENTERED)


def _exited(task_id="t1", rule_id="r_enter"):
    return TaskSignal(task_id, rule_id, SignalKind.EXITED)


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
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})

    assert h.sm.handle(_exited(rule_id="s")) is TransitionOutcome.ALREADY_IN_STATE
    assert h.dispatched == []


# ── 非互反模式 + §5.1 稳态交叉判定 ────────────────────────────────────


def test_exit_rule_entered_edge_is_an_exit_signal():
    """exit 型 rule 的"条件成立"就是"该退出了"。"""
    h = Harness()
    h.sm.register_task("t1", {"a": RuleDirection.ENTER, "x": RuleDirection.EXIT})
    h.sm.handle(_entered(rule_id="a"))

    assert h.sm.handle(_entered(rule_id="x")) is TransitionOutcome.EXITED
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


def test_exit_resets_enter_side_baselines():
    h = Harness()
    h.sm.register_task(
        "t1",
        {"a": RuleDirection.ENTER, "b": RuleDirection.ENTER, "x": RuleDirection.EXIT},
    )
    h.sm.handle(_entered(rule_id="a"))
    h.sm.handle(_entered(rule_id="x"))

    # 相等而非包含: 出边 x 不能在里面 —— 重置出边会让下一次退出被吞掉
    assert sorted(h.baseline_resets) == ["a", "b"]


def test_noop_exit_does_not_reset_baseline():
    """本来就在 off, 没发生转换, 不该动基线。"""
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.handle(_exited(rule_id="s"))

    assert h.baseline_resets == []


# ── §5.3 milestone ────────────────────────────────────────────────────


def test_milestone_fires_only_in_session():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "m": RuleDirection.MILESTONE})
    h.sm.handle(_entered(rule_id="s"))

    assert h.sm.handle(_entered(rule_id="m")) is TransitionOutcome.MILESTONE_FIRED
    assert ("t1", ActionSlot.ON_TARGET) in h.dispatched


def test_milestone_dropped_when_off():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "m": RuleDirection.MILESTONE})

    assert h.sm.handle(_entered(rule_id="m")) is TransitionOutcome.NOT_IN_SESSION
    assert h.dispatched == []


def test_milestone_does_not_change_state_or_reset_baseline():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION, "m": RuleDirection.MILESTONE})
    h.sm.handle(_entered(rule_id="s"))
    h.sm.handle(_entered(rule_id="m"))

    assert h.sm.runtime_state("t1") is TaskRuntimeState.ON
    assert h.baseline_resets == []


# ── §19.4 队列 ────────────────────────────────────────────────────────


def test_queue_overflow_drops_oldest_and_tracks():
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})

    for i in range(SIGNAL_QUEUE_DEPTH + 2):
        h.sm.submit(TaskSignal("t1", f"s{i}", SignalKind.ENTERED))

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
    resets: list[str] = []
    sm = TaskStateMachine(
        is_condition_satisfied=lambda _: None,
        reset_edge_baseline=resets.append,
        dispatch_action=lambda t, s, _p=None: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )
    sm.register_task("t1", {"s": RuleDirection.SESSION})

    # handle 吞掉异常不把消费链带死, 但状态已经写进去了
    sm.handle(_entered(rule_id="s"))

    assert sm.runtime_state("t1") is TaskRuntimeState.ON


# ── 手动触发 ──────────────────────────────────────────────────────────


def test_manual_inject_does_not_reset_baseline():
    """调试入口不代表"感知到条件不再满足", 不重置基线 (§5)。"""
    h = Harness()
    h.sm.register_task("t1", {"s": RuleDirection.SESSION})
    h.sm.manual_inject("t1", ActionSlot.ON_ENTER)
    h.sm.manual_inject("t1", ActionSlot.ON_EXIT)

    assert h.baseline_resets == []
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
