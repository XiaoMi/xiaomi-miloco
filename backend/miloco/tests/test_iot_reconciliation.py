from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.rule.iot_source import IotRef, IotSource
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    ConditionItem,
    Rule,
    RuleCondition,
    RuleConditionDNF,
    RuleDirection,
    RuleEvent,
    TriggerOutcome,
)
from miloco.state import StateStore
from miloco.task.state_machine import (
    ActionSlot,
    SignalKind,
    TaskRuntimeState,
    TaskSignal,
    TaskStateMachine,
    TransitionOutcome,
    derive_directions,
)


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_reconcile_rechecks_state_store_without_change_callback():
    store = StateStore()
    store.start()
    store.set("iot/device/d1/status/online", True, source="test")
    store.set("iot/device/d1/prop/5.1", False, source="test")

    refs = [IotRef("r1", "d1", "5.1", "eq", True)]
    fed: list[tuple[str, bool]] = []
    unknown: list[tuple[str, str]] = []
    source = IotSource(
        store=store,
        feed=lambda rule_id, value: _feed(fed, rule_id, value),
        mark_unknown=lambda rule_id, did: unknown.append((rule_id, did)),
        iot_refs=lambda: refs,
        ref_of_rule=lambda rule_id: next(
            (ref for ref in refs if ref.rule_id == rule_id), None
        ),
        reconcile_interval=60,
    )
    source.start()
    await _settle()
    fed.clear()

    source._unsubscribes[0]()
    store.set("iot/device/d1/prop/5.1", True, source="test")
    source._reconcile_once()
    await _settle()

    assert fed == [("r1", True)]
    assert unknown == []
    assert source.diagnostics()["reconcile_count"] == 1

    await source.stop()
    store.stop()


def test_reconcile_log_is_debug(caplog):
    source = IotSource(
        store=MagicMock(),
        feed=AsyncMock(),
        mark_unknown=MagicMock(),
        iot_refs=lambda: [],
        ref_of_rule=MagicMock(),
    )

    with caplog.at_level("DEBUG"):
        source._reconcile_once()

    records = [record for record in caplog.records if record.message.startswith("IOT_RECONCILE:")]
    assert len(records) == 1
    assert records[0].levelname == "DEBUG"


@pytest.mark.asyncio
async def test_reconcile_clears_transient_error_after_success():
    source = IotSource(
        store=MagicMock(),
        feed=AsyncMock(),
        mark_unknown=MagicMock(),
        iot_refs=lambda: [],
        ref_of_rule=MagicMock(),
    )
    reconcile_once = MagicMock(side_effect=[RuntimeError("temporary"), None])
    source._reconcile_once = reconcile_once
    sleeps = 0

    async def sleep(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            assert source._reconcile_exit == ""
            raise asyncio.CancelledError

    original_sleep = asyncio.sleep
    asyncio.sleep = sleep
    try:
        with pytest.raises(asyncio.CancelledError):
            await source._reconcile_loop()
    finally:
        asyncio.sleep = original_sleep

    assert reconcile_once.call_count == 2
    assert source._reconcile_exit == "cancelled"


async def _feed(fed: list[tuple[str, bool]], rule_id: str, value: bool) -> None:
    fed.append((rule_id, value))


class _FakeIotSource:
    def __init__(self, values: dict[str, bool | None]):
        self.values = values
        self.compensated_exit_count = 0

    def evaluate_current(self, rule_id: str) -> tuple[bool | None, object]:
        return self.values[rule_id], None

    def record_compensated_exit(self) -> None:
        self.compensated_exit_count += 1


def _iot_rule(
    rule_id: str,
    direction: RuleDirection,
    task_id: str = "task-1",
    value: bool = True,
    exit_debounce_seconds: int = 60,
) -> Rule:
    return Rule(
        id=rule_id,
        name=f"[{task_id}] {rule_id}",
        task_id=task_id,
        direction=direction,
        condition=RuleCondition(perceive_device_ids=[], query=rule_id),
        condition_dnf=RuleConditionDNF(
            any_of=[
                [
                    ConditionItem(
                        source_type="iot",
                        spec={
                            "did": "device-1",
                            "iid": "5.1",
                            "op": "eq",
                            "value": value,
                        },
                    )
                ]
            ]
        ),
        on_enter_desc="enter",
        on_exit_desc="exit",
        exit_debounce_seconds=exit_debounce_seconds,
    )


def _runner_with_state_machine(rules: list[Rule]):
    runner = RuleRunner(
        rules=rules,
        miot_proxy=AsyncMock(),
        rule_log_repo=MagicMock(),
        task_record_service=MagicMock(),
    )
    state_machine = TaskStateMachine(
        is_condition_satisfied=runner.is_condition_satisfied,
        dispatch_action=lambda *_args: None,
    )
    runner.attach_state_machine(state_machine)
    state_machine.register_task(
        rules[0].task_id,
        derive_directions((rule.id, rule.resolved_direction.value) for rule in rules),
    )
    return runner, state_machine


def _set_task_on(state_machine: TaskStateMachine, task_id: str, rule_id: str) -> None:
    assert state_machine.handle(
        TaskSignal(task_id, rule_id, SignalKind.ENTERED, ActionSlot.ON_ENTER),
        dispatch=False,
    ) is TransitionOutcome.ENTERED


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_exit_rule_compensation_fires_once():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _iot_rule("exit", RuleDirection.EXIT)
    runner, state_machine = _runner_with_state_machine([enter_rule, exit_rule])
    source = _FakeIotSource({"exit": True})
    runner._iot_source = source
    _set_task_on(state_machine, "task-1", "enter")

    settle_seen_on = []

    async def settle(_task_id: str) -> None:
        settle_seen_on.append(state_machine.runtime_state("task-1"))

    runner._record_source.settle = settle
    runner._record_source.disarm = MagicMock()
    fired: list[tuple[RuleEvent, str]] = []

    async def fire(*args, **_kwargs):
        fired.append((args[1], args[3]))

    runner._fire = fire

    await runner._reconcile_iot_task_state("exit", True)
    await runner.drain()
    await runner._reconcile_iot_task_state("exit", True)
    await runner.drain()

    assert settle_seen_on == [TaskRuntimeState.ON]
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    assert fired == [(RuleEvent.ENTERED, "iot_reconcile_exit")]
    assert source.compensated_exit_count == 1
    runner._record_source.disarm.assert_called_once_with("task-1")


@pytest.mark.asyncio
async def test_exit_compensation_aborts_on_stale_value():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _iot_rule("exit", RuleDirection.EXIT)
    runner, state_machine = _runner_with_state_machine([enter_rule, exit_rule])
    source = _FakeIotSource({"exit": True})
    runner._iot_source = source
    _set_task_on(state_machine, "task-1", "enter")
    fired = AsyncMock()

    async def settle(_task_id: str) -> None:
        source.values["exit"] = False

    runner._record_source.settle = settle
    runner._fire = fired

    await runner._reconcile_iot_task_state("exit", True)
    await runner.drain()

    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    fired.assert_not_awaited()
    assert source.compensated_exit_count == 0


@pytest.mark.asyncio
async def test_exit_compensation_aborts_when_rule_disabled_during_settle():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _iot_rule("exit", RuleDirection.EXIT)
    runner, state_machine = _runner_with_state_machine([enter_rule, exit_rule])
    source = _FakeIotSource({"exit": True})
    runner._iot_source = source
    _set_task_on(state_machine, "task-1", "enter")
    fired = AsyncMock()

    async def settle(_task_id: str) -> None:
        exit_rule.enabled = False

    runner._record_source.settle = settle
    runner._fire = fired

    await runner._reconcile_iot_task_state("exit", True)
    await runner.drain()

    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    fired.assert_not_awaited()
    assert source.compensated_exit_count == 0


@pytest.mark.asyncio
async def test_exit_compensation_aborts_when_task_is_already_off():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _iot_rule("exit", RuleDirection.EXIT)
    runner, state_machine = _runner_with_state_machine([enter_rule, exit_rule])
    source = _FakeIotSource({"exit": True})
    runner._iot_source = source
    _set_task_on(state_machine, "task-1", "enter")
    fired = AsyncMock()

    async def settle(_task_id: str) -> None:
        assert state_machine.reconcile_exit("task-1", "exit") is TransitionOutcome.EXITED

    runner._record_source.settle = settle
    runner._fire = fired

    await runner._reconcile_iot_task_state("exit", True)
    await runner.drain()

    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    fired.assert_not_awaited()
    assert source.compensated_exit_count == 0


@pytest.mark.asyncio
async def test_session_compensation_goes_through_debounce_without_resetting_it():
    session_rule = _iot_rule("session", RuleDirection.SESSION, exit_debounce_seconds=60)
    runner, state_machine = _runner_with_state_machine([session_rule])
    runner._iot_source = _FakeIotSource({"session": False})
    _set_task_on(state_machine, "task-1", "session")
    fired = AsyncMock()
    runner._fire = fired

    await runner._reconcile_iot_task_state("session", False)
    state = runner._ensure_state("session")
    first_task = state.exit_debounce_task
    first_at = state.exit_debounce_at
    await runner._reconcile_iot_task_state("session", False)
    await asyncio.sleep(0)

    assert first_task is not None
    assert state.exit_debounce_task is first_task
    assert state.exit_debounce_at == first_at
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    fired.assert_not_awaited()
    await _cancel_task(first_task)


@pytest.mark.asyncio
async def test_session_compensation_counts_after_debounce():
    session_rule = _iot_rule("session", RuleDirection.SESSION, exit_debounce_seconds=0)
    runner, state_machine = _runner_with_state_machine([session_rule])
    source = _FakeIotSource({"session": False})
    runner._iot_source = source
    _set_task_on(state_machine, "task-1", "session")
    runner._ensure_source("session", "device-1").last_bool = False
    runner._fire = AsyncMock()

    await runner._reconcile_iot_task_state("session", False)
    debounce_task = runner._ensure_state("session").exit_debounce_task
    assert debounce_task is not None
    await debounce_task

    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    assert source.compensated_exit_count == 1
    runner._fire.assert_awaited_once()


@pytest.mark.asyncio
async def test_guard_refresh_unblocks_entry():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _iot_rule("exit", RuleDirection.EXIT, value=False)
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine(
        [enter_rule, exit_rule, guard_rule]
    )
    source = _FakeIotSource({"guard": True, "exit": False})
    runner._iot_source = source
    fired = AsyncMock()
    runner._fire = fired

    assert await runner.update_state("guard", "device-1", False, "test") is TriggerOutcome.NOT_FIRED
    outcome = await runner.update_state("enter", "device-1", True, "test")
    await runner.drain()

    assert outcome is TriggerOutcome.FIRED
    assert runner.is_condition_satisfied("guard") is True
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    fired.assert_awaited_once()


@pytest.mark.asyncio
async def test_guard_refresh_marks_unknown_and_blocks_entry():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _iot_rule("exit", RuleDirection.EXIT, value=False)
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine(
        [enter_rule, exit_rule, guard_rule]
    )
    runner._iot_source = _FakeIotSource({"guard": None, "exit": False})
    runner._fire = AsyncMock()

    assert (
        await runner.update_state("guard", "device-1", True, "test")
        is TriggerOutcome.STILL_IN
    )
    await runner.drain()
    runner._fire.reset_mock()

    outcome = await runner.update_state("enter", "device-1", True, "test")
    await runner.drain()

    assert outcome is TriggerOutcome.STILL_IN
    assert runner.is_condition_satisfied("guard") is None
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    runner._fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_refresh_releases_entry_that_was_already_blocked():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _iot_rule("exit", RuleDirection.EXIT, value=False)
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine(
        [enter_rule, exit_rule, guard_rule]
    )
    source = _FakeIotSource({"guard": False, "exit": False})
    runner._iot_source = source
    fired = AsyncMock()
    runner._fire = fired

    await runner.update_state("guard", "device-1", False, "guard false")
    outcome = await runner.update_state(
        "enter",
        "device-1",
        True,
        "主规则进入",
        trigger_room="客厅",
        trigger_dids=["camera-1"],
        caption="有人进入",
        device_name="客厅摄像头",
    )
    await runner.drain()

    assert outcome is TriggerOutcome.STILL_IN
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    fired.assert_not_awaited()

    source.values["guard"] = True
    await runner.update_state("guard", "device-1", True, "guard true")
    await runner.drain()

    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    fired.assert_awaited_once()
    assert fired.await_args.kwargs["trigger_room"] == "客厅"
    assert fired.await_args.kwargs["trigger_dids"] == ["camera-1"]
    assert fired.await_args.kwargs["caption"] == "有人进入"
    assert fired.await_args.kwargs["device_name"] == "客厅摄像头"

    await runner.update_state("guard", "device-1", True, "guard still true")
    await runner.drain()
    fired.assert_awaited_once()
