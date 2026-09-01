# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task 状态机接进 RuleRunner 的集成测试.

这些用例是唯一走"接管后"那条路的 —— 存量 test_rule.py 的 225 个用例在内存里造
Rule、不建 task 动作, 全部命中回退分支 (expand-contract 阶段 A 的接管判据)。
所以接管路径的覆盖只能靠本文件, 少一条就是一条没人跑过的生产代码。

覆盖:
- 接管判据: 有 task 动作才接管, 没有则逐字走旧路径
- 动作取数: task 优先; task 接管后某方向留空不回退到 rule
- 许可闸: 四个 fire 点各自被状态机吞掉时的行为
- 注入点: is_condition_satisfied 的三态
"""

from __future__ import annotations

import asyncio

import pytest
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleCondition,
    RuleDirection,
    RuleEvent,
    RuleMode,
)
from miloco.task.state_machine import (
    ActionSlot,
    TaskRuntimeState,
    TaskStateMachine,
    derive_directions,
)


def _rule(rule_id="r1", task_id="t1", mode=RuleMode.EVENT, **kw):
    return Rule(
        id=rule_id,
        name=rule_id,
        task_id=task_id,
        mode=mode,
        condition=RuleCondition(perceive_device_ids=["cam1"], query="有人"),
        **kw,
    )


def _runner(rules, monkeypatch):
    monkeypatch.setattr(
        "miloco.task_record.service.TaskRecordService.__init__", lambda self: None
    )
    return RuleRunner(
        rules=rules,
        miot_proxy=None,
        rule_log_repo=None,
        task_record_service=object(),
    )


def _attach(runner, task_id, rules, actions):
    sm = TaskStateMachine(
        is_condition_satisfied=runner.is_condition_satisfied,
        dispatch_action=lambda *_a: None,
    )
    runner.attach_state_machine(sm)
    runner.set_task_actions(task_id, actions)
    if runner.task_owns_actions(task_id):
        sm.register_task(
            task_id,
            derive_directions((r.id, r.resolved_direction.value) for r in rules),
        )
    return sm


_TASK_DESC = {"on_enter_desc": "task 侧进入播报"}


async def _never_dispatch(*_a, **_kw):
    """动作层替身 —— 本文件测的是状态迁移, 不是动作是否发出去。"""
    return True


# ── 接管判据 ──────────────────────────────────────────────────────────


def test_empty_task_actions_falls_back_to_rule(monkeypatch):
    """六个槽全空 = 还没迁移 / 没配动作 → 逐字走旧路径。"""
    r = _rule(action_descriptions=["rule 侧播报"])
    runner = _runner([r], monkeypatch)
    sm = _attach(runner, "t1", [r], {"on_enter_actions": [], "on_enter_desc": None})

    assert sm.owns("t1") is False
    assert runner._select_slot(r, RuleEvent.ENTERED) == ("dynamic", "1. rule 侧播报")


def test_task_actions_take_priority_over_rule(monkeypatch):
    r = _rule(action_descriptions=["rule 侧播报"])
    runner = _runner([r], monkeypatch)
    _attach(runner, "t1", [r], _TASK_DESC)

    assert runner._select_slot(r, RuleEvent.ENTERED) == ("dynamic", "task 侧进入播报")


def test_owned_task_does_not_fall_back_for_empty_direction(monkeypatch):
    """task 接管后某方向留空就是留空 —— 回退会把用户故意清掉的动作重新捡起来。"""
    r = _rule(mode=RuleMode.STATE, on_exit_desc="rule 侧退出播报")
    runner = _runner([r], monkeypatch)
    _attach(runner, "t1", [r], _TASK_DESC)

    assert runner._select_slot(r, RuleEvent.EXITED) is None


def test_task_static_actions_are_parsed(monkeypatch):
    r = _rule()
    runner = _runner([r], monkeypatch)
    _attach(
        runner,
        "t1",
        [r],
        {"on_enter_actions": [{"did": "d1", "iid": "prop.2.1", "value": True}]},
    )

    kind, value = runner._select_slot(r, RuleEvent.ENTERED)
    assert kind == "static"
    assert value[0].did == "d1"


# ── 许可闸 ────────────────────────────────────────────────────────────


def test_gate_passes_when_not_owned(monkeypatch):
    r = _rule()
    runner = _runner([r], monkeypatch)
    _attach(runner, "t1", [r], None)

    assert runner._state_machine_allows(r, RuleEvent.ENTERED) is True


def test_gate_passes_when_no_state_machine(monkeypatch):
    r = _rule()
    runner = _runner([r], monkeypatch)

    assert runner._state_machine_allows(r, RuleEvent.ENTERED) is True


def test_event_type_gate_always_passes(monkeypatch):
    """事件型恒 off, 每次进信号都该放行。"""
    r = _rule(action_descriptions=["x"])
    runner = _runner([r], monkeypatch)
    _attach(runner, "t1", [r], _TASK_DESC)

    for _ in range(3):
        assert runner._state_machine_allows(r, RuleEvent.ENTERED) is True


def test_session_second_enter_is_blocked(monkeypatch):
    """对称模式已在 on, 第二次进信号不该重复 fire。"""
    r = _rule(mode=RuleMode.STATE, on_enter_desc="x")
    runner = _runner([r], monkeypatch)
    sm = _attach(runner, "t1", [r], _TASK_DESC)

    assert runner._state_machine_allows(r, RuleEvent.ENTERED) is True
    assert sm.runtime_state("t1") is TaskRuntimeState.ON
    assert runner._state_machine_allows(r, RuleEvent.ENTERED) is False


def test_exit_when_off_is_blocked(monkeypatch):
    r = _rule(mode=RuleMode.STATE, on_enter_desc="x")
    runner = _runner([r], monkeypatch)
    _attach(runner, "t1", [r], _TASK_DESC)

    assert runner._state_machine_allows(r, RuleEvent.EXITED) is False


def test_milestone_edge_requires_session(monkeypatch):
    """milestone 的进入边沿只在 task 处于 on 时放行 (§5.3)。

    走 ENTERED 而不是 TARGET_FIRED: 达标信号现在来自一条独立的 milestone rule,
    它的进入边沿由 ``slot_for_edge`` 映射成达标槽 —— 许可闸不再单判达标。
    """
    enter_rule = _rule(rule_id="r-enter", mode=RuleMode.STATE, on_enter_desc="x")
    milestone = _rule(
        rule_id="r-ms", mode=RuleMode.EVENT, direction=RuleDirection.MILESTONE
    )
    exit_rule = _rule(
        rule_id="r-exit", mode=RuleMode.EVENT, direction=RuleDirection.EXIT
    )
    runner = _runner([enter_rule, milestone, exit_rule], monkeypatch)
    _attach(runner, "t1", [enter_rule, milestone, exit_rule], _TASK_DESC)

    assert runner._state_machine_allows(milestone, RuleEvent.ENTERED) is False
    runner._state_machine_allows(enter_rule, RuleEvent.ENTERED)
    assert runner._state_machine_allows(milestone, RuleEvent.ENTERED) is True


def test_milestone_edge_does_not_change_state(monkeypatch):
    """从 off 观测: 方向映射错了会把 milestone 当成进入边沿、把 task 推进 on。

    必须从 off 观测 —— 从 on 观测的话映射错了也会命中"已在态内", 状态照旧是 on,
    对错给同样结果。
    """
    milestone = _rule(
        rule_id="r-ms", mode=RuleMode.EVENT, direction=RuleDirection.MILESTONE
    )
    exit_rule = _rule(
        rule_id="r-exit", mode=RuleMode.EVENT, direction=RuleDirection.EXIT
    )
    runner = _runner([milestone, exit_rule], monkeypatch)
    sm = _attach(runner, "t1", [milestone, exit_rule], _TASK_DESC)

    runner._state_machine_allows(milestone, RuleEvent.ENTERED)

    assert sm.runtime_state("t1") is TaskRuntimeState.OFF


def test_non_edge_event_is_not_allowed(monkeypatch):
    """许可闸只认进/出边沿。收到别的事件要拒, 不能 KeyError 崩在感知热路径上。"""
    r = _rule(mode=RuleMode.STATE, on_enter_desc="x")
    runner = _runner([r], monkeypatch)
    _attach(runner, "t1", [r], _TASK_DESC)

    assert runner._state_machine_allows(r, RuleEvent.TARGET_FIRED) is False


# ── 注入点: is_condition_satisfied ────────────────────────────────────


def test_condition_satisfied_is_none_before_any_observation(monkeypatch):
    """未观测和"观测到假"必须分开 —— last_rule_state 的初值 False 分不出来。"""
    r = _rule()
    runner = _runner([r], monkeypatch)

    assert runner.is_condition_satisfied("r1") is None


def test_condition_satisfied_is_none_when_state_exists_but_no_source(monkeypatch):
    r = _rule()
    runner = _runner([r], monkeypatch)
    runner._ensure_state("r1")

    assert runner.is_condition_satisfied("r1") is None


def test_condition_satisfied_reflects_last_rule_state(monkeypatch):
    r = _rule()
    runner = _runner([r], monkeypatch)
    runner._ensure_source("r1", "cam1")

    assert runner.is_condition_satisfied("r1") is False
    runner._state["r1"].last_rule_state = True
    assert runner.is_condition_satisfied("r1") is True


# ── 对称模式: 退出之后必须还能再进来 ──────────────────────────────────


@pytest.mark.asyncio
async def test_session_task_can_re_enter_after_exiting(monkeypatch):
    """对称模式退出一次之后, 条件再次成立要能重新进入。

    这是会话型 task 的核心循环 —— 断了的话每台设备一天只工作一次, 且从规则本身
    看不出任何异常。
    """
    rule = _rule("r-ses", mode=RuleMode.STATE, exit_debounce_seconds=1)
    runner = _runner([rule], monkeypatch)
    sm = _attach(runner, "t1", [rule], {"on_enter_desc": "进", "on_exit_desc": "出"})
    runner._execute_dynamic = _never_dispatch  # ty:ignore[invalid-assignment]

    async def feed(value, ticks=3):
        for _ in range(ticks):
            await runner.update_state("r-ses", "cam1", value, "")
        await asyncio.sleep(0.05)

    await feed(True)
    assert sm.runtime_state("t1") is TaskRuntimeState.ON

    await feed(False)
    await asyncio.sleep(1.3)
    assert sm.runtime_state("t1") is TaskRuntimeState.OFF

    # 人走后摄像头继续报假, 然后人回来
    await feed(False)
    await feed(True)
    assert sm.runtime_state("t1") is TaskRuntimeState.ON


@pytest.mark.asyncio
async def test_exit_leaves_the_condition_at_what_was_observed(monkeypatch):
    """退出不改条件层的值。

    改了的话 ②层对外说的和实际观测到的对不上, 而 runner 自己的边沿 diff 也读
    这个值 —— 它会以为 rule 还在态内, 把下一次变假当成又一次退出。
    """
    rule = _rule("r-ses", mode=RuleMode.STATE, exit_debounce_seconds=1)
    runner = _runner([rule], monkeypatch)
    sm = _attach(runner, "t1", [rule], {"on_enter_desc": "进", "on_exit_desc": "出"})
    runner._execute_dynamic = _never_dispatch  # ty:ignore[invalid-assignment]

    for _ in range(3):
        await runner.update_state("r-ses", "cam1", True, "")
    for _ in range(3):
        await runner.update_state("r-ses", "cam1", False, "")
    await asyncio.sleep(1.3)

    assert sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert runner.is_condition_satisfied("r-ses") is False


# ── 非互反: 退出后不立即重进 ──────────────────────────────────────────


def test_exit_by_another_rule_leaves_the_enter_condition_untouched(monkeypatch):
    """出边 rule 触发的退出不动进入侧的条件值 (§5.2)。

    进入条件此刻仍为真, 基线也就仍是真 —— 下一周期无翻转、不重进, 「挥手白挥」
    这个场景靠的就是这一点, 不需要额外置位。
    """
    enter_rule = _rule("r_enter", mode=RuleMode.EVENT)
    exit_rule = _rule("r_exit", mode=RuleMode.EVENT)
    exit_rule.direction = RuleDirection.EXIT
    runner = _runner([enter_rule, exit_rule], monkeypatch)
    sm = _attach(runner, "t1", [enter_rule, exit_rule], _TASK_DESC)

    runner._ensure_source("r_enter", "cam1").last_bool = True
    runner._ensure_state("r_enter").last_rule_state = True
    assert runner._state_machine_allows(enter_rule, RuleEvent.ENTERED)
    assert sm.runtime_state("t1") is TaskRuntimeState.ON

    assert runner._state_machine_allows(exit_rule, RuleEvent.ENTERED)
    # 退出真的发生了才谈得上"退出没动条件值"
    assert sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert runner.is_condition_satisfied("r_enter") is True


def test_entry_blocked_when_exit_condition_true_end_to_end(monkeypatch):
    """§5.1: 出边条件此刻已为真 → 拒绝进入, 否则开了永远不关。"""
    enter_rule = _rule("r_enter")
    exit_rule = _rule("r_exit")
    exit_rule.direction = RuleDirection.EXIT
    runner = _runner([enter_rule, exit_rule], monkeypatch)
    sm = _attach(runner, "t1", [enter_rule, exit_rule], _TASK_DESC)
    runner._ensure_source("r_exit", "cam1")
    runner._state["r_exit"].last_rule_state = True

    assert runner._state_machine_allows(enter_rule, RuleEvent.ENTERED) is False
    assert sm.runtime_state("t1") is TaskRuntimeState.OFF


def test_unseeded_exit_condition_does_not_block_entry(monkeypatch):
    """出边 rule 还没被观测过 → None → 不拦。"""
    enter_rule = _rule("r_enter")
    exit_rule = _rule("r_exit")
    exit_rule.direction = RuleDirection.EXIT
    runner = _runner([enter_rule, exit_rule], monkeypatch)
    _attach(runner, "t1", [enter_rule, exit_rule], _TASK_DESC)

    assert runner._state_machine_allows(enter_rule, RuleEvent.ENTERED) is True


# ── resolved_direction ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(RuleMode.EVENT, RuleDirection.ENTER), (RuleMode.STATE, RuleDirection.SESSION)],
)
def test_resolved_direction_falls_back_to_mode(mode, expected):
    assert _rule(mode=mode).resolved_direction is expected


def test_resolved_direction_prefers_explicit_field():
    r = _rule(mode=RuleMode.EVENT)
    r.direction = RuleDirection.MILESTONE
    assert r.resolved_direction is RuleDirection.MILESTONE


@pytest.mark.parametrize("slot", list(ActionSlot))
def test_action_slot_values_are_stable(slot):
    """槽名进 DB 列名与日志, 改了会静默错位。"""
    assert slot.value in {"on_enter", "on_exit", "on_target"}


# ── attach_task_state_machine: 启动路径 ───────────────────────────────


def _seed_db(tmp_path, monkeypatch, task_actions: dict | None):
    """建一个真库, 塞一个 task + 一条 rule, 可选写 task 边界动作。"""
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "t.db"))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    from miloco.database.rule_repo import RuleRepo
    from miloco.database.task_repo import TaskRepo

    task_repo = TaskRepo()
    task_repo.create_task("t1", "desc")
    rule_repo = RuleRepo()
    rule_repo.create(_rule(mode=RuleMode.STATE, on_enter_desc="rule 侧"))
    if task_actions:
        task_repo.set_boundary_actions("t1", **task_actions)
    return rule_repo


def test_attach_skips_task_without_boundary_actions(tmp_path, monkeypatch):
    """没配动作的 task 不接管 —— 未迁移的库启动后行为与接管前逐字相同。"""
    rule_repo = _seed_db(tmp_path, monkeypatch, None)
    runner = _runner(rule_repo.get_all(), monkeypatch)

    from miloco.rule.service import attach_task_state_machine

    attach_task_state_machine(runner, rule_repo)

    assert runner._state_machine is not None
    assert runner._state_machine.owns("t1") is False
    assert runner.task_owns_actions("t1") is False


def test_attach_owns_task_with_boundary_actions(tmp_path, monkeypatch):
    rule_repo = _seed_db(tmp_path, monkeypatch, {"on_enter_desc": "task 侧"})
    runner = _runner(rule_repo.get_all(), monkeypatch)

    from miloco.rule.service import attach_task_state_machine

    attach_task_state_machine(runner, rule_repo)

    assert runner._state_machine.owns("t1") is True
    assert runner._state_machine.runtime_state("t1") is TaskRuntimeState.OFF
    rule = rule_repo.get_all()[0]
    assert runner._select_slot(rule, RuleEvent.ENTERED) == ("dynamic", "task 侧")


def test_attach_registers_direction_from_db(tmp_path, monkeypatch):
    """rule 经 repo 落库时 direction 已写成 resolved 值, 拓扑应认出 session。"""
    rule_repo = _seed_db(tmp_path, monkeypatch, {"on_enter_desc": "task 侧"})
    runner = _runner(rule_repo.get_all(), monkeypatch)

    from miloco.rule.service import attach_task_state_machine

    attach_task_state_machine(runner, rule_repo)
    rule = rule_repo.get_all()[0]

    assert rule.direction is RuleDirection.SESSION
    assert runner._state_machine_allows(rule, RuleEvent.ENTERED) is True
    assert runner._state_machine.runtime_state("t1") is TaskRuntimeState.ON
