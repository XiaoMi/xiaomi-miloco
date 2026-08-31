# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""§19.5 重新配置路径的接线测试.

rule 增删改、rule 单独启停、task 重新 enable 走同一条路。这里测的是**接线**——
service 层动完 rule 之后有没有把新拓扑喂给状态机, 而不是状态机自己的语义
(那部分在 test_task_state_machine.py)。

它解掉的卡死: task 在 on 时删掉或停用唯一的出边 rule, 不走这条路径就永远退不出、
on_exit 永不执行。
"""

from __future__ import annotations

import pytest
from miloco.database.rule_repo import RuleLogRepo, RuleRepo
from miloco.database.task_repo import TaskRepo
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleCondition,
    RuleDirection,
    RuleEvent,
    RuleMode,
)
from miloco.rule.service import RuleService, attach_task_state_machine
from miloco.task.state_machine import ActionSlot, TaskRuntimeState


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "t.db"))
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()
    yield
    reset_settings()


def _rule(name, task_id="t1", mode=RuleMode.STATE, direction=None):
    r = Rule(
        name=name,
        task_id=task_id,
        mode=mode,
        condition=RuleCondition(perceive_device_ids=["cam1"], query="有人"),
        on_enter_desc="rule 侧",
    )
    if direction is not None:
        r.direction = direction
    return r


def _build(task_actions=None, rules=()):
    task_repo = TaskRepo()
    task_repo.create_task("t1", "d")
    if task_actions:
        task_repo.set_boundary_actions("t1", **task_actions)
    rule_repo = RuleRepo()
    ids = [rule_repo.create(r) for r in rules]

    runner = RuleRunner(
        rules=rule_repo.get_all(enabled_only=False),
        miot_proxy=None,
        rule_log_repo=RuleLogRepo(),
    )
    attach_task_state_machine(runner, rule_repo)
    service = RuleService(rule_repo, RuleLogRepo(), runner, None)
    dispatched: list[tuple[str, ActionSlot]] = []
    runner.state_machine._dispatch_action = lambda t, s, _p: dispatched.append((t, s))
    return service, runner, ids, dispatched


_ACTIONS = {"on_enter_desc": "task 侧", "on_exit_desc": "task 侧退出"}


async def _anoop(*_a, **_kw):
    return None


# ── 删掉唯一出边 rule ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_last_exit_rule_runs_on_exit(env):
    """task 在 on 时删掉唯一 session rule → 先跑 on_exit 退回 off。

    不走重新配置路径的话它会永远卡在 on, on_exit 永不执行。
    """
    service, runner, ids, dispatched = _build(_ACTIONS, [_rule("[t1] s")])
    sm = runner.state_machine
    rule = RuleRepo().get_by_id(ids[0])
    runner._state_machine_allows(rule, RuleEvent.ENTERED)
    assert sm.runtime_state("t1") is TaskRuntimeState.ON
    dispatched.clear()

    await service.delete_rule(ids[0])

    assert dispatched == [("t1", ActionSlot.ON_EXIT)]
    assert sm.runtime_state("t1") is TaskRuntimeState.OFF


@pytest.mark.asyncio
async def test_reconfigure_unregisters_task_that_lost_its_rules(env):
    """名下已无 rule → 撤销登记, 而不是留一个空拓扑在那里被后续信号命中。"""
    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    assert runner.state_machine.owns("t1") is True

    await service.delete_rule(ids[0])

    assert runner.state_machine.owns("t1") is False


@pytest.mark.asyncio
async def test_delete_one_of_two_exit_paths_keeps_session(env):
    """还剩出路径 → 不跑 on_exit, 但按 §7 一律回 off。"""
    service, runner, ids, dispatched = _build(
        _ACTIONS,
        [_rule("[t1] a", mode=RuleMode.EVENT), _rule("[t1] x", mode=RuleMode.EVENT)],
    )
    for rid in ids:
        r = RuleRepo().get_by_id(rid)
        r.direction = (
            RuleDirection.EXIT if r.name.endswith("x") else RuleDirection.ENTER
        )
        RuleRepo().update(r)
    runner.add_rule(RuleRepo().get_by_id(ids[0]))
    runner.add_rule(RuleRepo().get_by_id(ids[1]))
    service.reconfigure_task("t1")

    third = RuleRepo().create(_rule("[t1] x2", mode=RuleMode.EVENT))
    r3 = RuleRepo().get_by_id(third)
    r3.direction = RuleDirection.EXIT
    RuleRepo().update(r3)
    runner.add_rule(r3)
    service.reconfigure_task("t1")
    dispatched.clear()

    await service.delete_rule(third)

    assert dispatched == []
    assert runner.state_machine.owns("t1") is True


# ── 新建 / 改 rule 也走这条 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_rule_registers_topology(env, monkeypatch):
    """create_rule 走完之后拓扑要在。

    两个校验器被顶掉: 它们要摸 perception service 与米家场景, 与本条要验的接线
    无关, 留着这条测试就变成在测 Manager 装配。
    """
    service, runner, _ids, _ = _build(_ACTIONS, [])
    assert runner.state_machine.owns("t1") is False
    monkeypatch.setattr(
        RuleService, "_validate_perceive_device_ids", _anoop, raising=True
    )
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop, raising=True)

    await service.create_rule(_rule("[t1] s"))

    assert runner.state_machine.owns("t1") is True


@pytest.mark.asyncio
async def test_reconfigure_refreshes_task_action_snapshot(env):
    """动作是在 task 行上改的; 不刷快照的话 fire 还在用旧的那份。"""
    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    rule = RuleRepo().get_by_id(ids[0])
    assert runner._select_slot(rule, RuleEvent.ENTERED) == ("dynamic", "task 侧")

    TaskRepo().set_boundary_actions("t1", on_enter_desc="改过的")
    service.reconfigure_task("t1")

    assert runner._select_slot(rule, RuleEvent.ENTERED) == ("dynamic", "改过的")


def test_reconfigure_on_task_without_actions_is_noop(env):
    """没边界动作的 task 不该被登记 —— 那是接管判据。"""
    service, runner, _ids, _ = _build(None, [_rule("[t1] s")])

    service.reconfigure_task("t1")

    assert runner.state_machine.owns("t1") is False


# ── task 启停走同一条 ─────────────────────────────────────────────────


def test_apply_task_status_pauses_and_reconfigures(env):
    service, runner, ids, dispatched = _build(_ACTIONS, [_rule("[t1] s")])
    rule = RuleRepo().get_by_id(ids[0])
    runner._state_machine_allows(rule, RuleEvent.ENTERED)
    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.ON

    service.apply_task_status("t1", active=False)

    assert runner.is_task_paused("t1") is True
    assert runner.get_enabled_rules() == []
    # 停用不跑 on_exit (§19.9): 停用是「停止观察」, 不是「观察到条件不再满足」
    assert dispatched == []
    # 但运行态清掉, enable 回来按 §7 重建
    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.OFF
