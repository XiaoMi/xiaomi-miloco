# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task 状态机接进 RuleRunner 的集成测试.

test_rule.py 那批用例不装状态机, 走的是 ``_state_machine is None`` 那条; 接管
之后的行为只有本文件覆盖, 少一条就是一条没人跑过的生产代码。

覆盖:
- 接管判据: 名下有 rule 就接管, 与动作配没配无关
- 动作取数: 只读 task 的动作槽; 某方向留空就是留空, 不看 rule 行
- 许可闸: 四个 fire 点各自被状态机吞掉时的行为
- 注入点: is_condition_satisfied 的三态
"""

from __future__ import annotations

import asyncio

import pytest
from miloco.rule.condition import condition_to_dnf
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


def _runner(rules, monkeypatch, sample_interval_seconds=3.0):
    monkeypatch.setattr(
        "miloco.task_record.service.TaskRecordService.__init__", lambda self: None
    )
    return RuleRunner(
        rules=rules,
        miot_proxy=None,
        rule_log_repo=None,
        sample_interval_seconds=sample_interval_seconds,
        task_record_service=object(),
    )


def _attach(runner, task_id, rules, actions):
    sm = TaskStateMachine(
        is_condition_satisfied=runner.is_condition_satisfied,
        dispatch_action=lambda *_a: None,
    )
    runner.attach_state_machine(sm)
    runner.set_task_actions(task_id, actions)
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


def test_empty_task_actions_do_not_fall_back_to_rule(monkeypatch):
    """六个槽全空 → 选不到动作, 不去捡 rule 行上那份; task 照样接管。

    捡回来的话, 清空动作槽会让迁移前的旧动作重新生效, 而 task get 显示的是空。
    """
    r = _rule(action_descriptions=["rule 侧播报"])
    runner = _runner([r], monkeypatch)
    _attach(runner, "t1", [r], {"on_enter_actions": [], "on_enter_desc": None})

    # 接管判据本身由 test_attach_owns_task_without_boundary_actions 覆盖 (走真
    # 的 attach_task_state_machine); 这里 _attach 是无条件登记的, 断它没有判别力。
    assert runner._select_slot(r, RuleEvent.ENTERED) is None


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


def test_condition_satisfied_computes_from_sources(monkeypatch):
    """现算三值 OR，不读 last_rule_state —— 后者只在聚合确定时更新。"""
    r = _rule()
    runner = _runner([r], monkeypatch)
    runner._ensure_source("r1", "cam1")

    assert runner.is_condition_satisfied("r1") is False
    runner._ensure_source("r1", "cam1").last_bool = True
    assert runner.is_condition_satisfied("r1") is True


def test_condition_satisfied_is_none_after_source_marked_unknown(monkeypatch):
    """置未知之后要报"不知道"，不是报假 —— None 与 False 都是 falsy，断 None。"""
    r = _rule()
    runner = _runner([r], monkeypatch)
    runner._ensure_source("r1", "cam1").last_bool = True

    runner.mark_source_unknown("r1", "cam1")

    assert runner.is_condition_satisfied("r1") is None


def test_condition_satisfied_ignores_unknown_when_another_source_is_true(monkeypatch):
    """三值 OR：任一为真就是真，未知不把它拉下来。"""
    r = _rule()
    runner = _runner([r], monkeypatch)
    runner._ensure_source("r1", "cam1").last_bool = True
    runner._ensure_source("r1", "cam2")
    runner.mark_source_unknown("r1", "cam2")

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
    """出边 rule 触发的退出不动进入侧的条件值。

    只断言"没被改写"这一件事。别写成在验 §5.2 的「挥手白挥」—— 那个场景对纯边沿
    驱动的 rule 靠 diff 天然成立, 对配了 duration_seconds 的 rule 则本就没实现,
    两种情况这条用例都分不开。
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
    runner._ensure_source("r_exit", "cam1").last_bool = True

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


def test_attach_owns_task_without_boundary_actions(tmp_path, monkeypatch):
    """没配动作的 task 照样接管 —— 接管与动作配没配无关。

    绑在一起的话, 清空动作槽会连带把整条 task 退回旧的 per-rule 引擎, 而多 rule
    的 task 在那条引擎上的语义是错的。
    """
    rule_repo = _seed_db(tmp_path, monkeypatch, None)
    runner = _runner(rule_repo.get_all(), monkeypatch)

    from miloco.rule.service import attach_task_state_machine

    attach_task_state_machine(runner, rule_repo)

    assert runner._state_machine is not None
    assert runner._state_machine.owns("t1") is True
    rule = rule_repo.get_all()[0]
    assert runner._select_slot(rule, RuleEvent.ENTERED) is None


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


# ── reconfigure: 配置变了、现实没变 ───────────────────────────────────


async def _session_task_turned_on(monkeypatch, *, sample_interval=3.0, **rule_kw):
    """把一个会话型 task 沿真实路径推到 ``on``, 返回 (runner, sm, 拓扑, 派发记录)。"""
    rule = _rule("r-ses", mode=RuleMode.STATE, **rule_kw)
    runner = _runner([rule], monkeypatch, sample_interval_seconds=sample_interval)
    dispatched: list[tuple[str, ActionSlot]] = []
    sm = TaskStateMachine(
        is_condition_satisfied=runner.is_condition_satisfied,
        dispatch_action=lambda tid, slot, _p: dispatched.append((tid, slot)),
    )
    runner.attach_state_machine(sm)
    runner.set_task_actions("t1", {"on_enter_desc": "进", "on_exit_desc": "出"})
    directions = derive_directions([("r-ses", RuleDirection.SESSION.value)])
    sm.register_task("t1", directions)
    runner._execute_dynamic = _never_dispatch  # ty:ignore[invalid-assignment]

    for _ in range(3):
        await runner.update_state("r-ses", "cam1", True, "")
    await asyncio.sleep(0.05)
    assert sm.runtime_state("t1") is TaskRuntimeState.ON
    dispatched.clear()
    return runner, sm, directions, dispatched


@pytest.mark.asyncio
async def test_reconfigure_keeps_a_task_on_while_its_iot_device_is_offline(monkeypatch):
    """离线是"不知道", 不是"会话结束"。

    误发的 on_exit 是真会对外下指令的, 而 runner 侧的边沿缓存没被清过 —— 设备回来时
    属性没变就只产 STILL_IN, 这个 task 再也不会 enter。
    """
    runner, sm, directions, dispatched = await _session_task_turned_on(monkeypatch)

    runner.mark_source_unknown("r-ses", "cam1")
    assert runner.is_condition_satisfied("r-ses") is None

    sm.reconfigure("t1", directions)

    assert sm.runtime_state("t1") is TaskRuntimeState.ON
    assert dispatched == []


@pytest.mark.asyncio
async def test_reconfigure_turns_a_task_off_when_the_session_condition_is_false(
    monkeypatch,
):
    """上一条的反向: 明确观测到不成立时照样退。

    两条一起才把判据钉在"是不是明确的假"上, 而不是"是不是真"或者恒真。
    """
    runner, sm, directions, dispatched = await _session_task_turned_on(monkeypatch)

    runner._ensure_source("r-ses", "cam1").last_bool = False
    assert runner.is_condition_satisfied("r-ses") is False

    sm.reconfigure("t1", directions)

    assert sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert dispatched == [("t1", ActionSlot.ON_EXIT)]

# ── 换条件形状: 运行态被丢掉, 但「已经进入」要带过去 ──────────────────


def _edited(mode=RuleMode.STATE, **kw):
    """同一条 rule 换了条件文本 —— PATCH 改条件后 add_rule 收到的形态。

    改条件那条路会先把 DNF 那一列置空、再按新条件重建, 所以两列必然不等。
    """
    rule = _rule("r-ses", mode=mode, **kw)
    rule.condition.query = "有猫"
    rule.condition_dnf = condition_to_dnf(rule.condition)
    return rule


def _record_fires(monkeypatch) -> list[tuple[str, RuleEvent]]:
    """记下真发出去的边沿。状态机的 dispatch_action 只覆盖它自己直接派发的那些,
    走 runner 的进入 / 退出不经过那里。"""
    fired: list[tuple[str, RuleEvent]] = []

    async def capture(self, rule, event, *_a, **_kw):
        fired.append((rule.id, event))

    monkeypatch.setattr(RuleRunner, "_fire", capture)
    return fired


@pytest.mark.asyncio
async def test_editing_a_session_rule_still_exits_on_the_first_false(monkeypatch):
    """换条件不当场退出, 但第一次观测报假必须退。

    后半句是「换配置不是一次观测」这条立论的另一半: 基线跟着运行态一起清成假的
    话, 第一次报假与基线相等、退出边沿再也产不出来, 会话卡在 on。
    """
    runner, sm, directions, dispatched = await _session_task_turned_on(
        monkeypatch, exit_debounce_seconds=0
    )
    fired = _record_fires(monkeypatch)

    runner.add_rule(_edited(exit_debounce_seconds=0))
    sm.reconfigure("t1", directions)
    assert sm.runtime_state("t1") is TaskRuntimeState.ON
    assert dispatched == []

    await runner.update_state("r-ses", "cam1", False, "")
    await asyncio.sleep(0.05)

    assert sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert fired == [("r-ses", RuleEvent.EXITED)]


@pytest.mark.asyncio
async def test_editing_a_duration_session_rule_still_exits_on_the_first_false(
    monkeypatch,
):
    """带时长的会话同上, 但拦它的是另一道闸。

    退出边沿被「没配对的 ENTERED」那道闸挡着, 只拨回聚合基线不够 —— 「on_enter
    派发过」这个标记也要带过去。
    """
    runner, sm, directions, _dispatched = await _session_task_turned_on(
        monkeypatch,
        sample_interval=1.0,
        exit_debounce_seconds=0,
        duration_seconds=1,
        duration_ratio=1.0,
    )
    fired = _record_fires(monkeypatch)

    runner.add_rule(
        _edited(exit_debounce_seconds=0, duration_seconds=1, duration_ratio=1.0)
    )
    sm.reconfigure("t1", directions)
    assert sm.runtime_state("t1") is TaskRuntimeState.ON

    await runner.update_state("r-ses", "cam1", False, "")
    await asyncio.sleep(0.05)

    assert sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert fired == [("r-ses", RuleEvent.EXITED)]


@pytest.mark.asyncio
async def test_editing_while_an_exit_debounce_is_pending_keeps_the_exit_reachable(
    monkeypatch,
):
    """抗抖排着队时改条件: 退出边沿发过了、on_exit 还没落地, 状态机仍在 on。

    这一次退出被 reset 撤掉了, 基线不拨回那次边沿之前的话它永远补不上。
    """
    runner, sm, directions, _dispatched = await _session_task_turned_on(
        monkeypatch, exit_debounce_seconds=30
    )
    for _ in range(3):
        await runner.update_state("r-ses", "cam1", False, "")
    assert runner._state["r-ses"].exit_debounce_task is not None
    assert sm.runtime_state("t1") is TaskRuntimeState.ON
    fired = _record_fires(monkeypatch)

    runner.add_rule(_edited(exit_debounce_seconds=0))
    sm.reconfigure("t1", directions)
    assert sm.runtime_state("t1") is TaskRuntimeState.ON

    for _ in range(3):
        await runner.update_state("r-ses", "cam1", False, "")
    await asyncio.sleep(0.05)

    assert sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert fired == [("r-ses", RuleEvent.EXITED)]


@pytest.mark.asyncio
async def test_changing_direction_does_not_carry_the_old_baseline(monkeypatch):
    """换方向 = 这份动作整体换了个家, 旧基线不该跟过去。

    跟过去的话新家的第一次「条件成立」与基线相等, 进入动作一次都不发。
    """
    runner, sm, _directions, _dispatched = await _session_task_turned_on(monkeypatch)
    fired = _record_fires(monkeypatch)

    runner.add_rule(_edited(mode=RuleMode.EVENT, direction=RuleDirection.ENTER))
    sm.reconfigure("t1", derive_directions([("r-ses", RuleDirection.ENTER.value)]))

    await runner.update_state("r-ses", "cam1", True, "")
    await asyncio.sleep(0.05)

    assert fired == [("r-ses", RuleEvent.ENTERED)]


@pytest.mark.asyncio
async def test_re_enabling_a_rule_does_not_carry_the_old_baseline(monkeypatch):
    """停用再启用要从零开始, 否则启用回来后第一次成立一个动作都不发。"""
    runner, sm, directions, _dispatched = await _session_task_turned_on(monkeypatch)
    fired = _record_fires(monkeypatch)

    runner.add_rule(_rule("r-ses", mode=RuleMode.STATE, enabled=False))
    runner.add_rule(_rule("r-ses", mode=RuleMode.STATE, enabled=True))
    sm.suspend("t1")
    sm.reconfigure("t1", directions)

    await runner.update_state("r-ses", "cam1", True, "")
    await asyncio.sleep(0.05)

    assert fired == [("r-ses", RuleEvent.ENTERED)]
