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

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.database.rule_repo import RuleLogRepo, RuleRepo
from miloco.database.task_repo import TaskRepo
from miloco.middleware.exceptions import ResourceNotFoundException
from miloco.rule.runner import _NO_TASK_ACTIONS, RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleCondition,
    RuleDirection,
    RuleEvent,
    RuleMode,
)
from miloco.rule.service import RuleService, attach_task_state_machine
from miloco.task.state_machine import (
    ActionSlot,
    TaskRuntimeState,
    TransitionOutcome,
)


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


def _duration_record(target, accumulated):
    svc = MagicMock()
    svc.detect_record_kind = MagicMock(return_value="duration")
    svc.state = (target, accumulated)
    svc.read_duration_target_state = MagicMock(side_effect=lambda _t: svc.state)
    return svc


def _build(task_actions=None, rules=(), record=None):
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
        task_record_service=record,
    )
    attach_task_state_machine(runner, rule_repo)
    service = RuleService(
        rule_repo, RuleLogRepo(), runner, None, task_record_service=record
    )
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
async def test_delete_one_of_two_exit_paths_keeps_the_remaining_topology(env):
    """删掉三条中的一条 exit, 剩下的两条要如实进新拓扑。

    原来这条断言的是 ``dispatched == []`` 并说"还剩出路径所以不跑 on_exit" ——
    但用例从没把 task 推进 on, ``reconfigure`` 在 was_on 处就短路了, 断言恒绿。
    改成断言交给状态机的那份拓扑, 不调 reconfigure 就会红。
    """
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

    assert runner.state_machine.owns("t1") is True
    topology = runner.state_machine._topologies["t1"]
    assert topology.directions == {
        ids[0]: RuleDirection.ENTER,
        ids[1]: RuleDirection.EXIT,
    }
    assert topology.enter_side_rule_ids == {ids[0]}
    assert topology.exit_side_rule_ids == {ids[1]}
    # enter + exit 没有 session 条件撑着, 所以 task 不在 on 时也不该被派动作
    assert dispatched == []


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


def test_target_slot_reads_task_column(env):
    """task 配了达标动作、真 fire 的那条 rule 没配 —— 也要取到 task 那份。

    只看 ``rule.on_target_desc`` 的话达标永远发不出去。
    """
    _service, runner, ids, _ = _build(
        {**_ACTIONS, "on_target_desc": "task 侧达标"}, [_rule("[t1] s")]
    )
    rule = RuleRepo().get_by_id(ids[0])
    assert rule.on_target_desc is None

    assert runner._select_task_slot(rule, RuleEvent.TARGET_FIRED) == (
        "dynamic", "task 侧达标",
    )


def test_target_slot_does_not_fall_back_when_task_column_empty(env):
    """接管了但 task 的达标槽是空的 → 不回退捡 rule 上的存量值。

    回退会让用户故意留空的方向重新捡起旧动作。
    """
    _service, runner, ids, _ = _build(
        _ACTIONS, [_rule("[t1] s", on_target_desc="rule 侧达标")]
    )
    rule = RuleRepo().get_by_id(ids[0])
    assert rule.on_target_desc == "rule 侧达标"

    assert runner._select_task_slot(rule, RuleEvent.TARGET_FIRED) is None


def test_target_slot_falls_back_to_rule_when_task_not_owned(env):
    """task 没有任何边界动作 → 不接管 → 按 rule 列取, 与接管前逐字相同。"""
    _service, runner, ids, _ = _build(
        None, [_rule("[t1] s", on_target_desc="rule 侧达标")]
    )
    rule = RuleRepo().get_by_id(ids[0])

    assert runner._select_task_slot(rule, RuleEvent.TARGET_FIRED) is _NO_TASK_ACTIONS
    assert runner._select_slot(rule, RuleEvent.TARGET_FIRED) == (
        "dynamic", "rule 侧达标",
    )


# ── 动作槽按 direction 分 ──────────────────────────────────────────────


def _single_edge_rule(direction, task_id="t1", descs=("做事",)):
    r = Rule(
        name=f"[{task_id}] {direction.value}",
        task_id=task_id,
        mode=RuleMode.EVENT,
        condition=RuleCondition(perceive_device_ids=["cam1"], query="有人"),
        action_descriptions=list(descs),
    )
    r.direction = direction
    return r


def test_enter_rule_writes_only_the_enter_slot(env):
    """enter 型 rule 不该碰 on_exit / on_target —— 那两个槽不属于它管辖。

    从 task set-actions 配好的达标动作, 不能被一次 rule create 顺手清掉。
    """
    from miloco.rule.service import _rule_action_slots

    slots = _rule_action_slots(_single_edge_rule(RuleDirection.ENTER))
    # 管辖的那个槽两列一起给 —— 空着的那列也要写, 否则残留值会继续赢
    assert slots == {"on_enter_actions": [], "on_enter_desc": "1. 做事"}


def test_exit_rule_writes_the_exit_slot(env):
    """exit 型的动作填在 --action-desc 上, 但落的是 on_exit 槽。"""
    from miloco.rule.service import _rule_action_slots

    slots = _rule_action_slots(_single_edge_rule(RuleDirection.EXIT, descs=("该退了",)))
    assert slots == {"on_exit_actions": [], "on_exit_desc": "1. 该退了"}


def test_session_rule_skips_the_slots_it_left_empty(env):
    """整个槽都空的不出现在返回里 —— 空不等于"用户要清空"。

    达标文案的正规配法是 task set-actions, rule 行上恒空。无条件透传的话, 改一次
    会话规则的触发条件就会把 task 上那份清成 None, 连带把代建的达标规则也删掉。
    管辖到的槽两列都写, 达标槽也不例外: rule 侧没有 on_target_actions 字段, 那正
    说明它管辖达标槽时 task 上那一列就该是空的 —— 不写的话残留的静态达标动作会继续
    赢 (选槽静态优先), 这次改动静默失效。
    """
    from miloco.rule.service import _rule_action_slots

    slots = _rule_action_slots(_rule("[t1] s", on_target_desc="达标"))
    assert slots == {
        "on_enter_actions": [],
        "on_enter_desc": "rule 侧",
        "on_target_actions": [],
        "on_target_desc": "达标",
    }

    only_enter = _rule_action_slots(_rule("[t1] s2"))
    assert set(only_enter) == {"on_enter_actions", "on_enter_desc"}


def test_milestone_rule_does_not_touch_task_slots(env):
    """迁移补建的 milestone rule 动作字段恒空, 透传只会清掉 task 上的达标动作。"""
    from miloco.rule.service import _rule_action_slots

    r = _single_edge_rule(RuleDirection.MILESTONE, descs=())
    assert _rule_action_slots(r) == {}


def test_exit_rule_entered_edge_reads_the_exit_slot(env):
    """exit 型的"条件成立"就是 task 该退出 → 它的 ENTERED 边沿取 on_exit 槽。

    按边沿名直接映射会取到 on_enter, 拿到的就是别的 rule 写的进入文本。
    """
    _service, runner, _ids, _ = _build(_ACTIONS, [])
    runner.set_task_actions("t1", _ACTIONS)
    r = _single_edge_rule(RuleDirection.EXIT)

    assert runner._select_task_slot(r, RuleEvent.ENTERED) == (
        "dynamic",
        "task 侧退出",
    )


def test_enter_rule_entered_edge_reads_the_enter_slot(env):
    _service, runner, _ids, _ = _build(_ACTIONS, [])
    runner.set_task_actions("t1", _ACTIONS)
    r = _single_edge_rule(RuleDirection.ENTER)

    assert runner._select_task_slot(r, RuleEvent.ENTERED) == ("dynamic", "task 侧")


def test_single_edge_rule_has_no_exited_slot(env):
    """单方向的 rule 只有一个边沿, EXITED 不该取到任何槽。"""
    _service, runner, _ids, _ = _build(_ACTIONS, [])
    runner.set_task_actions("t1", _ACTIONS)

    for direction in (RuleDirection.ENTER, RuleDirection.EXIT):
        r = _single_edge_rule(direction)
        assert runner._select_task_slot(r, RuleEvent.EXITED) is None


def test_direction_flip_resets_runtime_state(env):
    """enter ↔ exit 互换时 mode 都是 event, 只看 mode 的话残留状态不会清。"""
    _service, runner, _ids, _ = _build(_ACTIONS, [])
    r = _single_edge_rule(RuleDirection.ENTER)
    r.id = "r-flip"
    runner.add_rule(r)
    runner._ensure_state(r.id).last_rule_state = True

    flipped = _single_edge_rule(RuleDirection.EXIT)
    flipped.id = "r-flip"
    runner.add_rule(flipped)

    assert runner._ensure_state(r.id).last_rule_state is False


def test_meaningless_edge_produces_no_signal(env, monkeypatch):
    """单方向的 rule 只有一个边沿, 另一个边沿不该发信号给 task 层。

    发出去只是让 task 层再判一次同样的事, 而且那次判定会进判定记账, 看起来像
    真的发生过一次转换。
    """
    _service, runner, _ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    submitted: list = []
    monkeypatch.setattr(
        type(runner.state_machine),
        "handle",
        lambda self, signal, **kw: submitted.append(signal),
    )
    r = _single_edge_rule(RuleDirection.ENTER)

    assert runner._state_machine_allows(r, RuleEvent.EXITED) is False
    assert submitted == []


def test_signal_carries_the_slot_computed_by_confirmation_layer(env, monkeypatch):
    """确认层算好 slot 填进信号 —— task 层拿到的是意图, 不是「自己去查方向」。"""
    _service, runner, _ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    seen: list = []
    monkeypatch.setattr(
        type(runner.state_machine),
        "handle",
        lambda self, signal, **kw: (seen.append(signal), TransitionOutcome.EXITED)[1],
    )
    r = _single_edge_rule(RuleDirection.EXIT)
    runner.state_machine.register_task(r.task_id, {r.id: "exit"})

    runner._state_machine_allows(r, RuleEvent.ENTERED)

    assert [s.slot for s in seen] == [ActionSlot.ON_EXIT]


# ── 动作透传按"槽"判争用, 不按"有没有兄弟" ──────────────────────────────


def test_write_through_passes_when_siblings_hold_other_slots(env):
    """非互反 task: enter 写 on_enter、exit 写 on_exit, 两条互不相干, 都要写进去。

    按"有没有兄弟"一律跳过的话, 用 rule 侧 flag 建非互反 task 时退出动作会静默
    丢失 —— CLI 每条都返回成功, 只有服务端日志里一行 warning。
    """
    enter_rule = _rule("[t1] 进", mode=RuleMode.EVENT, direction=RuleDirection.ENTER)
    enter_rule.on_enter_desc = None
    enter_rule.action_descriptions = ["进入时播报"]
    exit_rule = _rule("[t1] 出", mode=RuleMode.EVENT, direction=RuleDirection.EXIT)
    exit_rule.on_enter_desc = None
    exit_rule.action_descriptions = ["退出时播报"]

    service, _runner, ids, _ = _build(None, [enter_rule, exit_rule])
    for rule_id in ids:
        service.sync_rule_actions_to_task(RuleRepo().get_by_id(rule_id))

    actions = TaskRepo().get_boundary_actions("t1")
    assert actions["on_enter_desc"] == "1. 进入时播报"
    assert actions["on_exit_desc"] == "1. 退出时播报"


def test_write_through_keeps_task_side_slots_the_rule_left_empty(env):
    """rule 上为空的槽不透传 —— 空不等于"用户要清空"。

    达标文案的正规配法是 task set-actions, 只写 task 列、rule 行恒空。无条件透传
    等于每改一次这条会话规则就把 task 上那份清成 None, reconcile 接着把代建的达标
    规则一起删掉 —— 用户只改了一句触发条件, 达标提醒整体消失。
    """
    session_rule = _rule("[t1] s", mode=RuleMode.STATE, direction=RuleDirection.SESSION)
    service, _runner, ids, _ = _build({"on_target_desc": "该休息啦"}, [session_rule])

    service.sync_rule_actions_to_task(RuleRepo().get_by_id(ids[0]))

    actions = TaskRepo().get_boundary_actions("t1")
    assert actions["on_target_desc"] == "该休息啦"
    # 规则自己填了的那个槽照旧透传, 不是整体停写
    assert actions["on_enter_desc"] == "rule 侧"


def test_write_through_replaces_the_whole_slot_not_just_the_filled_column(env):
    """一个槽的两列一起写。

    只写填了的那一列的话, 用户把动作从设备直控改成 Agent 文案时, task 上残留的直控
    列会继续赢 (选槽时静态优先), CLI 返回成功而行为没变。
    """
    rule = _rule("[t1] 进", mode=RuleMode.EVENT, direction=RuleDirection.ENTER)
    rule.on_enter_desc = None
    rule.action_descriptions = ["改成让 Agent 播报"]
    stale_static = [{"did": "d1", "iid": "prop.2.1", "value": True}]
    service, _runner, ids, _ = _build({"on_enter_actions": stale_static}, [rule])

    service.sync_rule_actions_to_task(RuleRepo().get_by_id(ids[0]))

    actions = TaskRepo().get_boundary_actions("t1")
    assert actions["on_enter_actions"] == []
    assert actions["on_enter_desc"] == "1. 改成让 Agent 播报"


def test_write_through_still_skips_when_siblings_hold_the_same_slot(env, caplog):
    """两条 enter 都管 on_enter —— 从一条单向覆盖会把另一条的动作冲掉, 仍要跳过。"""
    first = _rule("[t1] 进A", mode=RuleMode.EVENT, direction=RuleDirection.ENTER)
    first.on_enter_desc = None
    first.action_descriptions = ["A"]
    second = _rule("[t1] 进B", mode=RuleMode.EVENT, direction=RuleDirection.ENTER)
    second.on_enter_desc = None
    second.action_descriptions = ["B"]

    service, _runner, ids, _ = _build(None, [first, second])
    with caplog.at_level("WARNING"):
        service.sync_rule_actions_to_task(RuleRepo().get_by_id(ids[1]))

    assert TaskRepo().get_boundary_actions("t1")["on_enter_desc"] is None
    assert any("也管着" in r.message for r in caplog.records)


# ── task 被删: task 维度的内存态 ─────────────────────────────────────


def _task_service(rule_service):
    from miloco.task.service import TaskService

    return TaskService(RuleRepo(), rule_service)


@pytest.mark.asyncio
async def test_delete_task_forgets_the_task_dimension_state(env):
    """删 task 要连 task 维度的内存态一起清。

    清 rule 只清 RuleRunner._rules; 拓扑 / 运行态 / 判定跟踪 / 动作快照是按
    task_id 存的另一份, 不清就是一条随删除次数单调增长的泄漏。
    """
    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    sm = runner.state_machine
    runner._state_machine_allows(RuleRepo().get_by_id(ids[0]), RuleEvent.ENTERED)
    assert sm.owns("t1") is True
    assert sm.runtime_state("t1") is TaskRuntimeState.ON
    assert runner.task_owns_actions("t1") is True
    assert runner.tracker.last_decision("t1") is not None
    runner.record_source.arm("t1")
    assert "t1" in runner.record_source._arm_round

    _task_service(service).delete_task("t1")

    assert sm.owns("t1") is False
    assert sm.runtime_state("t1") is TaskRuntimeState.OFF
    assert runner.task_owns_actions("t1") is False
    assert runner.tracker.last_decision("t1") is None
    assert "t1" not in runner.record_source._arm_round


@pytest.mark.asyncio
async def test_rebuilding_a_deleted_task_id_does_not_fire_a_stray_on_exit(env):
    """同名重建不该先收到一条退出动作。

    上一条 task 的运行态留着的话, 新 task 建第一条 rule 时 reconfigure 看到
    was_on=True 而新 rule 还没喂过数据, 判定"没条件撑着"→ 派一次 on_exit。用户
    刚建好, 什么都没发生, 先收到一条退出通知。
    """
    service, runner, ids, dispatched = _build(_ACTIONS, [_rule("[t1] s")])
    runner._state_machine_allows(RuleRepo().get_by_id(ids[0]), RuleEvent.ENTERED)
    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.ON

    _task_service(service).delete_task("t1")
    dispatched.clear()

    TaskRepo().create_task("t1", "d2")
    TaskRepo().set_boundary_actions("t1", **_ACTIONS)
    new_id = RuleRepo().create(_rule("[t1] s2"))
    runner.add_rule(RuleRepo().get_by_id(new_id))
    service.reconfigure_task("t1")

    assert dispatched == []
    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.OFF


@pytest.mark.asyncio
async def test_rebuilding_a_disabled_then_deleted_task_id_is_effectively_enabled(env):
    """先停用再删再同名重建, 新 task 必须真的生效。

    停用标记是内存里的派生量, 删 task 不清的话「有效启用」恒假 —— 新 task 的
    rule 一条都不参与判定, 而 CLI 报成功、rule get 显示 enabled、task get 显示
    active。改动前 task 停用是写 rule.enabled 到库里, 删表就一起没了。
    """
    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    service.apply_task_status("t1", active=False)
    assert runner.is_task_paused("t1") is True

    _task_service(service).delete_task("t1")

    assert runner.is_task_paused("t1") is False

    TaskRepo().create_task("t1", "d2")
    new_id = RuleRepo().create(_rule("[t1] s2"))
    runner.add_rule(RuleRepo().get_by_id(new_id))

    assert [r.id for r in runner.get_enabled_rules()] == [new_id]


@pytest.mark.asyncio
async def test_delete_task_cancels_the_pending_target_timer(env, monkeypatch):
    """删 task 之后不能留下未到点的达标 timer。

    timer 是 asyncio task, 不看 rule 还在不在 —— 不撤的话到点仍会给一条已经不存在
    的 rule 喂一次达标。撤它的是逐条清 rule 那一步(timer 按 rule_id 存), 本条盯的
    是删 task 这条路径最终把它撤掉了。
    """
    service, runner, _ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    # 用生产代码建那条代建 rule, 不手搓形状
    ms = service._build_milestone_rule("t1")
    ms.id = RuleRepo().create(ms)
    runner.add_rule(RuleRepo().get_by_id(ms.id))
    # 还差 10 分钟 —— 排 timer 而不是立刻喂
    monkeypatch.setattr(
        type(runner._task_record_service),
        "read_duration_target_state",
        lambda _self, _tid: (60, 50),
    )

    runner.record_source.arm("t1")
    for _ in range(4):
        await asyncio.sleep(0)
    assert runner.record_source._timers, "前提没成立: timer 没排上, 这条测不到东西"

    _task_service(service).delete_task("t1")

    assert runner.record_source._timers == {}


# ── PATCH 只透传本次动过的槽 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patching_the_condition_leaves_every_action_slot_alone(env, monkeypatch):
    """只改触发条件, 一个动作槽都不许碰。

    task 侧覆盖过的槽会被 rule 上的旧值顶回去（这里 on_enter）; rule 上恒空的槽会
    被清成 None（这里 on_target, 它的正规配法是 task set-actions）, reconcile 接着
    把代建的达标规则一起删掉 —— 用户只改了一句话, 达标提醒整体消失, HTTP 返回 200。
    """
    from miloco.rule.schema import RuleConditionUpdate, RuleUpdate

    service, _runner, ids, _ = _build(
        {**_ACTIONS, "on_target_desc": "该休息啦"}, [_rule("[t1] s")]
    )
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)

    await service.patch_rule(
        ids[0], RuleUpdate(condition=RuleConditionUpdate(query="换个说法"))
    )

    actions = TaskRepo().get_boundary_actions("t1")
    assert actions["on_enter_desc"] == "task 侧"
    assert actions["on_target_desc"] == "该休息啦"


@pytest.mark.asyncio
async def test_clearing_a_rule_side_desc_clears_the_task_column(env, monkeypatch):
    """显式清空必须透传下去。

    按"槽是空的就不写"处理的话, CLI 报成功、rule get 显示已清空, 而 fire 读的
    task 列还是旧文案 —— 静默不生效, 正是透传本身要防的那种。
    """
    from miloco.rule.schema import RuleUpdate

    session_rule = _rule("[t1] s")
    session_rule.on_exit_desc = "结束时播报"
    service, _runner, ids, _ = _build({"on_enter_desc": "task 侧"}, [session_rule])
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)

    await service.patch_rule(ids[0], RuleUpdate(on_enter_desc=None))

    assert TaskRepo().get_boundary_actions("t1")["on_enter_desc"] is None


@pytest.mark.asyncio
async def test_changing_direction_moves_the_actions_to_the_new_slot(env, monkeypatch):
    """改方向 = 动作换了个槽。旧槽要清、新槽要写。

    只按"本次动过的字段"透传的话, `rule update --direction enter` 的字段集里没有
    任何动作字段, 一个槽都不匹配 —— 规则照常触发, 但 task 已被接管、选槽时读到空
    的进入槽就返回 None 且不回退到规则列, 一个动作都执行不了, 而 HTTP 返回 200。
    """
    from miloco.rule.schema import RuleUpdate

    exit_rule = _rule("[t1] 出", mode=RuleMode.EVENT, direction=RuleDirection.EXIT)
    exit_rule.on_enter_desc = None
    exit_rule.action_descriptions = ["把书桌灯关掉"]
    enter_rule = _rule("[t1] 进", mode=RuleMode.EVENT, direction=RuleDirection.ENTER)
    enter_rule.on_enter_desc = None
    enter_rule.action_descriptions = ["开灯"]
    service, _runner, ids, _ = _build(None, [enter_rule, exit_rule])
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)
    service.sync_rule_actions_to_task(RuleRepo().get_by_id(ids[1]))
    assert TaskRepo().get_boundary_actions("t1")["on_exit_desc"] == "1. 把书桌灯关掉"

    # 把出边那条改成入边; 原来那条入边先删掉, 否则两条 enter 争同一个槽
    await service.delete_rule(ids[0])
    await service.patch_rule(ids[1], RuleUpdate(direction=RuleDirection.ENTER))

    actions = TaskRepo().get_boundary_actions("t1")
    assert actions["on_enter_desc"] == "1. 把书桌灯关掉"
    assert actions["on_exit_desc"] is None


@pytest.mark.asyncio
async def test_direction_change_keeps_a_slot_a_sibling_still_owns(env, monkeypatch):
    """清旧槽前要排掉兄弟 rule 也管着的 —— 否则把别人的动作一起抹掉。"""
    from miloco.rule.schema import RuleUpdate

    moving = _rule("[t1] 出A", mode=RuleMode.EVENT, direction=RuleDirection.EXIT)
    moving.on_enter_desc = None
    moving.action_descriptions = ["A"]
    staying = _rule("[t1] 出B", mode=RuleMode.EVENT, direction=RuleDirection.EXIT)
    staying.on_enter_desc = None
    staying.action_descriptions = ["B"]
    service, _runner, ids, _ = _build({"on_exit_desc": "两条都管着"}, [moving, staying])
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)

    await service.patch_rule(ids[0], RuleUpdate(direction=RuleDirection.ENTER))

    assert TaskRepo().get_boundary_actions("t1")["on_exit_desc"] == "两条都管着"


# ── 会话进行中才配上达标 ───────────────────────────────────────────────


async def _settle_arm(src):
    """等 arm 起的后台协程真的跑完 —— sleep(0) 会让「没排 timer」在等得不够和
    代码正确两种情况下给同样的绿。"""
    while src._arming:
        await asyncio.gather(*list(src._arming), return_exceptions=True)


@pytest.mark.asyncio
async def test_milestone_built_mid_session_gets_a_timer(env):
    """task 已经在 on 时才配上达标动作 —— 定时器要当场排出来。

    排 timer 只挂在「进入会话」那个边沿上, 而装配是分步的: 三样齐备的那一刻可能
    落在会话开始之后, 那时进入边沿早过去了。不补排这一天的达标只能靠退出兜底或
    跨零点补发, 而这条通知的全部意义是到点提醒。
    """
    record = _duration_record(target=120, accumulated=30)
    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")], record=record)
    with patch("miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)):
        for _ in range(3):
            await runner.update_state(ids[0], "cam1", True, "")
        await asyncio.sleep(0.1)
    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.ON
    await _settle_arm(runner.record_source)
    assert not runner.record_source._timers

    TaskRepo().set_boundary_actions("t1", on_target_desc="提醒休息")
    service.reconfigure_task("t1")
    await _settle_arm(runner.record_source)

    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.ON
    milestone = next(
        r
        for r in RuleRepo().list_by_task("t1")
        if r.resolved_direction is RuleDirection.MILESTONE
    )
    assert milestone.id in runner.record_source._timers


@pytest.mark.asyncio
async def test_threshold_changed_mid_session_reschedules_the_timer(env):
    """会话进行中改阈值 —— 定时器按新阈值重排。

    旧 timer 留着的话它按旧阈值到点, 当天的达标要么早发要么晚发。断言等待时长而
    不是"排了几个": 按旧阈值重排同样是一个 timer, 只看个数分不开对错。
    """
    record = _duration_record(target=120, accumulated=30)
    service, runner, ids, _ = _build(
        {**_ACTIONS, "on_target_desc": "提醒休息"}, [_rule("[t1] s")], record=record
    )
    service.reconfigure_task("t1")
    await _settle_arm(runner.record_source)

    source_type = type(runner.record_source)
    delays: list[float] = []
    original = source_type._feed_after

    async def spy(self, ref, delay, target_at_arm, this_round):
        delays.append(delay)  # 只记, 不真睡

    source_type._feed_after = spy
    try:
        with patch(
            "miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)
        ):
            for _ in range(3):
                await runner.update_state(ids[0], "cam1", True, "")
            await asyncio.sleep(0.1)
        await _settle_arm(runner.record_source)

        record.state = (45, 30)
        service.notify_record_changed("t1")
        await _settle_arm(runner.record_source)
    finally:
        source_type._feed_after = original

    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.ON
    assert delays == [(120 - 30) * 60, (45 - 30) * 60]


# ── 状态机自己发起的动作按槽名派, 不按代表 rule 的方向反推 ─────────────


def test_state_machine_exit_reaches_the_exit_slot_on_a_non_mutual_task(env):
    """enter + exit 型 task 的强制退出要拿到退出槽。

    这条路的代表 rule 是名下任意一条, 而 _slot_for 只有 session 方向才把 EXITED
    映射成退出槽 —— 让 fire 从代表 rule 的方向反推, enter 和 exit 两种代表都返回
    空槽, 强制退出动作静默不执行, 正是 §19.5 要解的那个卡死。
    """
    _service, runner, _ids, _ = _build(_ACTIONS, [])
    runner.set_task_actions("t1", _ACTIONS)
    representative = _single_edge_rule(RuleDirection.ENTER)

    assert runner._select_task_slot(
        representative, RuleEvent.EXITED, ActionSlot.ON_EXIT
    ) == ("dynamic", "task 侧退出")


def test_state_machine_enter_does_not_pick_up_the_exit_slot(env):
    """反向: 请求进入槽而代表 rule 是 exit 型, 反推出来的是退出槽。

    不钉住的话派出去的是相反的动作 —— 该开灯的时候关灯。
    """
    _service, runner, _ids, _ = _build(_ACTIONS, [])
    runner.set_task_actions("t1", _ACTIONS)
    representative = _single_edge_rule(RuleDirection.EXIT)

    assert runner._select_task_slot(
        representative, RuleEvent.ENTERED, ActionSlot.ON_ENTER
    ) == ("dynamic", "task 侧")


@pytest.mark.asyncio
async def test_losing_the_exit_path_really_dispatches_on_a_non_mutual_task(env):
    """接线版: enter + exit 型 task 失去出路径时, 退出动作真的发出去。

    上面两条钉的是选槽, 这条钉的是 dispatch_task_action 到 fire 的一整条链 ——
    槽正确但没传下去, 上面两条照绿。
    """
    service, runner, ids, _ = _build(
        _ACTIONS,
        [_single_edge_rule(RuleDirection.ENTER), _single_edge_rule(RuleDirection.EXIT)],
    )
    runner.state_machine._dispatch_action = lambda t, sl, _p: (
        runner.dispatch_task_action(t, sl.value)
    )
    enter_rule = RuleRepo().get_by_id(ids[0])
    runner._state_machine_allows(enter_rule, RuleEvent.ENTERED)
    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.ON

    with patch(
        "miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)
    ) as sent:
        await service.delete_rule(ids[1])
        await asyncio.gather(*list(runner._fire_tasks), return_exceptions=True)

    prompts = [
        c.prompt_text for call in sent.call_args_list for c in call.args[1]
    ]
    assert any("task 侧退出" in p for p in prompts), prompts


# ── 整体替换 (PUT) 与部分更新 (PATCH) 的收尾要一致 ──────────────────────


@pytest.mark.asyncio
async def test_full_update_clears_the_slot_it_left_behind(env, monkeypatch):
    """PUT 换方向后旧槽要清 —— 留着就是一份没人认领也没人读得到的动作。

    兄弟 rule 还管着的槽不能连带清掉, 所以这里让翻转前后的槽错开: 出→进, 兄弟
    管着进入槽。
    """
    from miloco.rule.service import RuleService

    moving = _single_edge_rule(RuleDirection.EXIT, descs=("走了",))
    staying = _single_edge_rule(RuleDirection.ENTER, descs=("来了",))
    staying.name = "[t1] 另一条"
    service, _runner, ids, _ = _build(
        {"on_enter_desc": "进", "on_exit_desc": "出"}, [moving, staying]
    )
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)

    flipped = RuleRepo().get_by_id(ids[0])
    flipped.direction = RuleDirection.ENTER
    await service.update_rule(flipped)

    written = TaskRepo().get_boundary_actions("t1")
    assert written["on_exit_desc"] is None
    # 兄弟 rule 管着的进入槽不受影响
    assert written["on_enter_desc"] == "进"


@pytest.mark.asyncio
async def test_full_update_reconfigures_the_task_it_left(env, monkeypatch):
    """PUT 改挂 task 后, 原 task 也要重算拓扑 —— 少了它原 task 还挂着这条 rule。"""
    from miloco.rule.service import RuleService

    TaskRepo().create_task("t2", "d2")
    service, runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    runner.set_task_actions("t2", _ACTIONS)
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)
    sm = runner.state_machine
    assert sm.owns("t1")

    moved = RuleRepo().get_by_id(ids[0])
    moved.task_id = "t2"
    await service.update_rule(moved)

    assert not sm.owns("t1")
    assert sm.owns("t2")


@pytest.mark.asyncio
async def test_full_update_rejects_a_task_that_does_not_exist(env, monkeypatch):
    """task_id 填错要拿到业务错误。

    不拦的话 FK 抛的 IntegrityError 不在 repo 那层的 except 里, 一路冒到全局
    处理器变成 500 —— 而这只是一个参数填错。
    """
    from miloco.rule.service import RuleService

    service, _runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)

    bad = RuleRepo().get_by_id(ids[0])
    bad.task_id = "t_nope"
    with pytest.raises(ResourceNotFoundException):
        await service.update_rule(bad)


@pytest.mark.asyncio
async def test_partial_update_rejects_a_task_that_does_not_exist(env, monkeypatch):
    from miloco.rule.schema import RuleUpdate
    from miloco.rule.service import RuleService

    service, _runner, ids, _ = _build(_ACTIONS, [_rule("[t1] s")])
    monkeypatch.setattr(RuleService, "_validate_perceive_device_ids", _anoop)
    monkeypatch.setattr(RuleService, "_validate_scene_ids", _anoop)

    with pytest.raises(ResourceNotFoundException):
        await service.patch_rule(ids[0], RuleUpdate(task_id="t_nope"))
