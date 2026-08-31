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

import sqlite3

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


def _rule(name, task_id="t1", mode=RuleMode.STATE, direction=None, on_target_desc=None):
    r = Rule(
        name=name,
        task_id=task_id,
        mode=mode,
        condition=RuleCondition(perceive_device_ids=["cam1"], query="有人"),
        on_enter_desc="rule 侧",
        on_target_desc=on_target_desc,
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
async def test_delete_last_exit_rule_actually_fires_on_exit(env, monkeypatch):
    """**动作真的被执行**, 不只是"状态机请求了"。

    这条与下面那条 recorder 版本的区别很重要: recorder 顶掉派发口之后, 无论
    真实派发能不能拿到归属 rule, 断言都会绿。而删掉最后一条 rule 时 runner 内存
    里那条随时会被摘掉, 动作就无处归属、被跳过 —— 恰好是 §19.5 要解的那个卡死。
    """
    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    # 换回真实派发口 —— _build 里那个 recorder 恰好遮住本条要验的东西
    runner.state_machine._dispatch_action = lambda t, sl, _p: (
        runner.dispatch_task_action(t, sl.value)
    )
    rule = RuleRepo().get_by_id(ids[0])
    runner._state_machine_allows(rule, RuleEvent.ENTERED)
    fired: list[tuple[str, RuleEvent]] = []
    monkeypatch.setattr(
        RuleRunner,
        "_spawn_fire",
        lambda self, r, ev, *a, **kw: fired.append((r.id, ev)),
    )

    await service.delete_rule(ids[0])

    assert fired == [(ids[0], RuleEvent.EXITED)]


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


# ── 动作写入透传 (§10.3 阶段 A 的写侧) ────────────────────────────────


@pytest.mark.asyncio
async def test_rule_action_edit_reaches_task_column(env, monkeypatch):
    """迁移后用现有 CLI 改动作必须生效。

    读侧回退只解决"读哪一份"; 写侧不透传的话 rule 列改了、fire 读的是 task 列的
    旧值, 而 CLI 返回成功、rule get 也显示新值 —— 静默不生效。
    """
    from miloco.rule.schema import RuleUpdate

    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)
    rule = RuleRepo().get_by_id(ids[0])
    assert runner._select_slot(rule, RuleEvent.ENTERED) == ("dynamic", "task 侧")

    await service.patch_rule(ids[0], RuleUpdate(on_enter_desc="改过的文案"))

    updated = RuleRepo().get_by_id(ids[0])
    assert runner._select_slot(updated, RuleEvent.ENTERED) == (
        "dynamic",
        "改过的文案",
    )


@pytest.mark.asyncio
async def test_write_through_skipped_when_task_has_other_rules(env, monkeypatch):
    """多 rule 时从一条单向覆盖会把另一条的动作悄悄冲掉 —— 口径同迁移。"""
    from miloco.rule.schema import RuleUpdate

    service, _runner, ids, _ = _build(_ACTIONS, [_rule("[t1] a"), _rule("[t1] b")])
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)

    await service.patch_rule(ids[0], RuleUpdate(on_enter_desc="只改了 a"))

    assert TaskRepo().get_boundary_actions("t1")["on_enter_desc"] == "task 侧"


def test_write_through_warns_when_task_row_missing(env, caplog):
    """task 行不存在 → 只是没写进去, 不抛。但必须留日志。"""
    service, _runner, _ids, _ = _build(_ACTIONS, [])
    orphan = _rule("[nope] r", task_id="does_not_exist")
    orphan.id = "r-orphan"

    with caplog.at_level("WARNING"):
        service.sync_rule_actions_to_task(orphan)

    assert any("没同步过去" in r.message for r in caplog.records)


def test_write_through_survives_repo_exception(env, monkeypatch, caplog):
    """rule 写入是主要效果, 不能被 task 侧同步抛出的异常带崩。

    真实触发形态是 task 表缺失（部分单测库就是这样）—— 传一个不存在的 task_id
    不会抛, 只是 rowcount 0, 所以那条路走不到这个分支。
    """
    from miloco.database.task_repo import TaskRepo as _TR

    service, _runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    rule = RuleRepo().get_by_id(ids[0])

    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("no such table: task")

    monkeypatch.setattr(_TR, "set_boundary_actions", _boom)

    with caplog.at_level("WARNING"):
        service.sync_rule_actions_to_task(rule)

    assert any("不会生效" in r.message for r in caplog.records)


def test_self_initiated_action_needs_a_rule_for_attribution(env, monkeypatch):
    """名下无 rule 时动作无处归属（日志与冷却按 rule 记）→ 跳过, 不崩。"""
    _service, runner, _ids, _ = _build(_ACTIONS, [])
    fired: list = []
    monkeypatch.setattr(
        RuleRunner, "_spawn_fire", lambda self, *a, **kw: fired.append(a)
    )

    assert runner.dispatch_task_action("t1", "on_exit") is False
    assert fired == []


def test_self_initiated_action_rejects_unknown_slot(env):
    _service, runner, _ids, _ = _build(_ACTIONS, [_rule("[t1] s")])

    assert runner.dispatch_task_action("t1", "on_nonsense") is False


# ── runtime_state / lifecycle 进视图 ──────────────────────────────────


def test_task_full_view_exposes_runtime_state(env):
    from miloco.task.service import TaskService

    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    task_service = TaskService(rule_repo=RuleRepo(), rule_service=service)
    assert task_service.get_full_view("t1").runtime_state == "off"

    runner._state_machine_allows(RuleRepo().get_by_id(ids[0]), RuleEvent.ENTERED)

    assert task_service.get_full_view("t1").runtime_state == "on"


def test_task_full_view_exposes_lifecycle(env):
    from miloco.task.service import TaskService

    service, _runner, _ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    TaskRepo().set_boundary_actions("t1", lifecycle="temporary")

    view = TaskService(rule_repo=RuleRepo(), rule_service=service).get_full_view("t1")

    assert view.lifecycle == "temporary"


# ── 判定跟踪接线 (§18.5) ──────────────────────────────────────────────


def test_last_decision_reaches_task_view(env):
    """状态机吞掉一次进入时, 视图要能说出是被哪一层压制的。

    不接这条线的话「被对侧条件拦住」和「已在态内」在住户那边表现完全一样 ——
    都是"没触发"。
    """
    from miloco.task.service import TaskService

    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    task_service = TaskService(rule_repo=RuleRepo(), rule_service=service)
    rule = RuleRepo().get_by_id(ids[0])

    runner._state_machine_allows(rule, RuleEvent.ENTERED)
    assert task_service.get_full_view("t1").last_decision["outcome"] == "entered"

    runner._state_machine_allows(rule, RuleEvent.ENTERED)
    d = task_service.get_full_view("t1").last_decision
    assert d["outcome"] == "already_in_state"
    assert d["suppressed"] is True


def test_tracking_cleared_when_task_unregistered(env):
    from miloco.task.service import TaskService

    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    task_service = TaskService(rule_repo=RuleRepo(), rule_service=service)
    runner._state_machine_allows(RuleRepo().get_by_id(ids[0]), RuleEvent.ENTERED)
    assert task_service.get_full_view("t1").last_decision is not None

    runner.state_machine.unregister_task("t1")

    assert task_service.get_full_view("t1").last_decision is None


# ── 达标闸门与动作内容同源 (§10.3 阶段 A 的读侧) ──────────────────────


def test_target_gate_reads_task_slot(env):
    """task 配了达标动作、真 fire 的那条 rule 没配 —— 闸门必须放行。

    只看 ``rule.on_target_desc`` 的话 timer 永远起不来, 达标发不出去。
    """
    _service, runner, ids, _ = _build(
        {**_ACTIONS, "on_target_desc": "task 侧达标"}, [_rule("[t1] s")]
    )
    rule = RuleRepo().get_by_id(ids[0])
    assert rule.on_target_desc is None

    assert runner._has_target_slot(rule) is True


def test_target_gate_does_not_fall_back_when_task_slot_empty(env):
    """接管了但 task 的达标槽是空的 → 不回退捡 rule 上的存量值。

    回退会让用户故意留空的方向重新捡起旧动作 —— 与 ``_select_task_slot`` 同口径。
    """
    _service, runner, ids, _ = _build(
        _ACTIONS, [_rule("[t1] s", on_target_desc="rule 侧达标")]
    )
    rule = RuleRepo().get_by_id(ids[0])
    assert rule.on_target_desc == "rule 侧达标"

    assert runner._has_target_slot(rule) is False


def test_target_gate_falls_back_to_rule_when_task_not_owned(env):
    """task 没有任何边界动作 → 不接管 → 闸门按 rule 列判, 与接管前逐字相同。"""
    _service, runner, ids, _ = _build(
        None, [_rule("[t1] s", on_target_desc="rule 侧达标")]
    )
    rule = RuleRepo().get_by_id(ids[0])

    assert runner._has_target_slot(rule) is True
