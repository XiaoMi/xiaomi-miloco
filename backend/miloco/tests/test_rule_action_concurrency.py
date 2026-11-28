# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""静态动作槽的按设备并发执行（设备内保持配置顺序）。"""

import asyncio
from unittest.mock import MagicMock

import pytest
from miloco.rule import runner as runner_module
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleActionExecuteResult,
    RuleCondition,
    RuleEvent,
    RuleLifecycle,
    RuleMode,
)
from miloco.task.state_machine import ActionSlot


def _action(did: str, iid: str = "prop.2.1", value: bool = True) -> RuleAction:
    return RuleAction(did=did, iid=iid, value=value)


@pytest.fixture
def runner():
    return RuleRunner(
        rules=[],
        miot_proxy=MagicMock(),
        rule_log_repo=MagicMock(),
        task_record_service=MagicMock(),
    )


def _ok(action: RuleAction, result: bool = True, error: str | None = None):
    return RuleActionExecuteResult(action=action, result=result, error=error)


@pytest.mark.asyncio
async def test_sequential_mode_keeps_strict_config_order(runner, monkeypatch):
    """默认模式仍是旧行为：严格按配置顺序，一条完成才走下一条。"""
    order: list[str] = []

    async def fake(rule_id, action, **_):
        order.append(action.did)
        await asyncio.sleep(0)
        return _ok(action)

    monkeypatch.setattr(runner, "_execute_action", fake)
    actions = [_action("A"), _action("B"), _action("A", "prop.3.1")]

    results = await runner._execute_static_actions(
        "rule-1", actions, phase=None, trigger_event_id=None, mode="sequential",
    )

    assert order == ["A", "B", "A"]
    assert [r.action for r in results] == actions


@pytest.mark.asyncio
async def test_parallel_by_device_runs_devices_concurrently(runner, monkeypatch):
    """A 的首条动作被卡住时，B 能完成；A 的第二条必须等 A 的第一条。"""
    b_done = asyncio.Event()
    a2_started = asyncio.Event()
    release_a1 = asyncio.Event()

    async def fake(rule_id, action, **_):
        if (action.did, action.iid) == ("A", "prop.1.1"):
            await release_a1.wait()
        elif (action.did, action.iid) == ("A", "prop.1.2"):
            a2_started.set()
        elif action.did == "B":
            b_done.set()
        return _ok(action)

    monkeypatch.setattr(runner, "_execute_action", fake)
    actions = [
        _action("A", "prop.1.1"),
        _action("B", "prop.2.1"),
        _action("A", "prop.1.2"),
    ]

    task = asyncio.create_task(
        runner._execute_static_actions(
            "rule-1", actions, phase=None, trigger_event_id=None,
            mode="parallel_by_device",
        )
    )
    await asyncio.wait_for(b_done.wait(), timeout=1)
    assert not a2_started.is_set()
    release_a1.set()
    results = await asyncio.wait_for(task, timeout=1)

    assert a2_started.is_set()
    assert [r.action for r in results] == actions


@pytest.mark.asyncio
async def test_results_keep_input_order_and_failures_are_per_action(runner, monkeypatch):
    async def fake(rule_id, action, **_):
        if action.did == "B":
            return _ok(action, result=False, error="miot_failed")
        return _ok(action)

    monkeypatch.setattr(runner, "_execute_action", fake)
    actions = [_action("A"), _action("B"), _action("A", "prop.3.1")]

    results = await runner._execute_static_actions(
        "rule-1", actions, phase=None, trigger_event_id=None,
        mode="parallel_by_device",
    )

    assert [r.action for r in results] == actions
    assert [r.result for r in results] == [True, False, True]
    assert results[1].error == "miot_failed"


@pytest.mark.asyncio
async def test_scene_action_falls_back_to_sequential(runner, monkeypatch):
    """场景影响面跨设备，整槽回退串行：前一条没完成，后一条不开始。"""
    release = asyncio.Event()
    scene_started = asyncio.Event()

    async def fake(rule_id, action, **_):
        if action.iid == "scene":
            scene_started.set()
        else:
            await release.wait()
        return _ok(action)

    monkeypatch.setattr(runner, "_execute_action", fake)
    actions = [
        _action("A"),
        RuleAction(did="scene-1", iid="scene", idempotent=False, cooldown_minutes=1),
    ]

    task = asyncio.create_task(
        runner._execute_static_actions(
            "rule-1", actions, phase=None, trigger_event_id=None,
            mode="parallel_by_device",
        )
    )
    await asyncio.sleep(0.01)
    assert not scene_started.is_set()
    release.set()
    results = await asyncio.wait_for(task, timeout=1)
    assert scene_started.is_set()
    assert [r.action for r in results] == actions


@pytest.mark.asyncio
async def test_group_watchdog_bounds_a_hung_device(runner, monkeypatch):
    """一个设备永久不返回时，其余设备照常完成，挂住的那条记超时。"""
    monkeypatch.setattr(runner_module, "_RULE_ACTION_WATCHDOG_SECONDS", 0.05)

    async def fake(rule_id, action, **_):
        if action.did == "A":
            await asyncio.Event().wait()  # 永不到点
        return _ok(action)

    monkeypatch.setattr(runner, "_execute_action", fake)
    actions = [_action("A"), _action("B")]

    results = await asyncio.wait_for(
        runner._execute_static_actions(
            "rule-1", actions, phase=None, trigger_event_id=None,
            mode="parallel_by_device",
        ),
        timeout=1,
    )

    assert results[0].result is False
    assert results[0].error == "action_timeout"
    assert results[1].result is True


@pytest.mark.asyncio
async def test_group_exception_is_isolated(runner, monkeypatch):
    async def fake(rule_id, action, **_):
        if action.did == "B":
            raise RuntimeError("boom")
        return _ok(action)

    monkeypatch.setattr(runner, "_execute_action", fake)
    actions = [_action("A"), _action("B")]

    results = await runner._execute_static_actions(
        "rule-1", actions, phase=None, trigger_event_id=None,
        mode="parallel_by_device",
    )

    assert results[0].result is True
    assert results[1].result is False
    assert results[1].error == "action_group_failed"


@pytest.mark.asyncio
async def test_device_parallelism_is_capped(runner, monkeypatch):
    monkeypatch.setattr(runner_module, "_RULE_DEVICE_PARALLEL_LIMIT", 2)
    in_flight = 0
    peak = 0

    async def fake(rule_id, action, **_):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _ok(action)

    monkeypatch.setattr(runner, "_execute_action", fake)
    actions = [_action(f"dev-{i}") for i in range(5)]

    results = await runner._execute_static_actions(
        "rule-1", actions, phase=None, trigger_event_id=None,
        mode="parallel_by_device",
    )

    assert peak == 2
    assert [r.action for r in results] == actions


def test_watchdog_budget_covers_the_two_calls_of_an_idempotent_action():
    """幂等 prop 动作先预检再下发，单条动作最坏是两次 SDK 调用。

    预检读走云端默认 30s，下发 set 是 15s，本地各先试 5s；单条最坏
    5 + 30 + 15 = 50s。组级预算不能比这更短，否则正常但慢的动作会被
    watchdog 误杀，连台账都来不及写。
    """
    assert runner_module._RULE_ACTION_WATCHDOG_SECONDS >= 50.0


def test_slot_execution_mode_reads_each_slot_independently(runner):
    """三个槽各读各的键；进入配并发不会把退出也带成并发。"""
    assert runner._slot_execution_mode("missing", ActionSlot.ON_ENTER) == "sequential"
    runner.set_task_actions(
        "t1",
        {
            "on_enter_execution_mode": "parallel_by_device",
            "on_exit_execution_mode": "sequential",
            "on_target_execution_mode": "parallel_by_device",
        },
    )
    assert (
        runner._slot_execution_mode("t1", ActionSlot.ON_ENTER)
        == "parallel_by_device"
    )
    assert runner._slot_execution_mode("t1", ActionSlot.ON_EXIT) == "sequential"
    assert (
        runner._slot_execution_mode("t1", ActionSlot.ON_TARGET)
        == "parallel_by_device"
    )
    assert runner._slot_execution_mode("t1", None) == "sequential"

    runner.set_task_actions(
        "t2", {"on_enter_execution_mode": "unknown"}
    )
    assert runner._slot_execution_mode("t2", ActionSlot.ON_ENTER) == "sequential"


def _rule(task_id: str = "t1") -> Rule:
    return Rule(
        id="rule-1",
        name="[t1] r",
        task_id=task_id,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=RuleCondition(perceive_device_ids=["cam-1"], query="有人"),
        actions=[],
    )


@pytest.mark.asyncio
async def test_fire_reads_mode_from_the_selected_slot(runner, monkeypatch):
    """接线：进入 / 退出 / 达标各读自己槽的执行方式，不共用任务级开关。"""
    runner.set_task_actions(
        "t1",
        {
            "on_enter_execution_mode": "parallel_by_device",
            "on_exit_execution_mode": "sequential",
            "on_target_execution_mode": "parallel_by_device",
            "on_enter_actions": [
                {"did": "A", "iid": "prop.2.1", "value": True},
                {"did": "B", "iid": "prop.2.1", "value": True},
            ],
            "on_exit_actions": [
                {"did": "A", "iid": "prop.2.1", "value": False},
            ],
            "on_target_actions": [
                {"did": "A", "iid": "prop.2.1", "value": True},
                {"did": "B", "iid": "prop.2.1", "value": True},
            ],
            "on_enter_desc": None,
            "on_exit_desc": None,
            "on_target_desc": None,
        },
    )
    seen: list[str] = []

    async def fake_execute(
        rule_id, actions, *, phase, trigger_event_id, mode
    ):
        seen.append(mode)
        return [_ok(a) for a in actions]

    monkeypatch.setattr(runner, "_execute_static_actions", fake_execute)
    rule = _rule()

    enter_result = await runner._fire(
        rule, RuleEvent.ENTERED, [], "ctx", "exec-1",
    )
    exit_result = await runner._fire(
        rule, RuleEvent.EXITED, [], "ctx", "exec-2",
        action_slot=ActionSlot.ON_EXIT,
    )
    target_result = await runner._fire(
        rule, RuleEvent.TARGET_FIRED, [], "ctx", "exec-3",
    )

    assert enter_result is not None
    assert exit_result is not None
    assert target_result is not None
    assert seen == [
        "parallel_by_device",
        "sequential",
        "parallel_by_device",
    ]
