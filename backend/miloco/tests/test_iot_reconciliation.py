from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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


def _duration_iot_rule(
    rule_id: str,
    direction: RuleDirection,
    task_id: str = "task-1",
    duration_ratio: float = 1.0,
) -> Rule:
    rule = _iot_rule(rule_id, direction, task_id=task_id, exit_debounce_seconds=0)
    rule.duration_seconds = 1
    rule.duration_ratio = duration_ratio
    return rule


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
async def test_duration_exit_does_not_release_pending_enter():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _duration_iot_rule("exit", RuleDirection.EXIT)
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine(
        [enter_rule, exit_rule, guard_rule]
    )
    source = _FakeIotSource({"enter": True, "exit": True, "guard": False})
    runner._iot_source = source
    runner._record_source.settle = AsyncMock()
    fired: list[tuple[str, RuleEvent]] = []

    async def fire(rule, event, *_args, **_kwargs):
        fired.append((rule.id, event))

    runner._fire = fire

    await runner.update_state("guard", "device-1", False, "", skip_flicker=True)
    await runner.update_state("enter", "device-1", True, "", skip_flicker=True)
    source.values["guard"] = True

    with patch("miloco.rule.runner.time.time") as clock:
        clock.return_value = 100.0
        await runner.update_state("exit", "device-1", True, "", skip_flicker=True)
        clock.return_value = 100.5
        await runner.update_state("exit", "device-1", True, "", skip_flicker=True)

    await runner.drain()

    assert fired == []
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF


@pytest.mark.asyncio
async def test_duration_exit_does_not_reconcile_before_window_is_ready():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _duration_iot_rule("exit", RuleDirection.EXIT)
    exit_rule.duration_seconds = 6
    runner, state_machine = _runner_with_state_machine([enter_rule, exit_rule])
    source = _FakeIotSource({"exit": True})
    runner._iot_source = source
    runner._record_source.settle = AsyncMock()
    runner._fire = AsyncMock()
    _set_task_on(state_machine, "task-1", "enter")

    await runner._feed_iot("exit", True)
    await runner.drain()

    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    runner._record_source.settle.assert_not_awaited()
    runner._fire.assert_not_awaited()
    assert source.compensated_exit_count == 0


@pytest.mark.asyncio
async def test_session_duration_exit_does_not_release_pending_enter():
    session_rule = _duration_iot_rule(
        "session", RuleDirection.SESSION, duration_ratio=0.5
    )
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine([session_rule, guard_rule])
    source = _FakeIotSource({"session": True, "guard": False})
    runner._iot_source = source
    fired: list[tuple[str, RuleEvent]] = []

    async def fire(rule, event, *_args, **_kwargs):
        fired.append((rule.id, event))

    runner._fire = fire

    await runner.update_state("guard", "device-1", False, "", skip_flicker=True)
    await runner.update_state("session", "device-1", True, "", skip_flicker=True)

    with patch("miloco.rule.runner.time.time") as clock:
        clock.return_value = 100.0
        await runner.update_state("session", "device-1", True, "", skip_flicker=True)
        source.values["guard"] = True
        clock.return_value = 100.5
        await runner.update_state("session", "device-1", False, "", skip_flicker=True)

    await runner.drain()

    assert fired == []
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF


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
async def test_exit_compensation_checks_current_value_before_settle():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    exit_rule = _iot_rule("exit", RuleDirection.EXIT)
    runner, state_machine = _runner_with_state_machine([enter_rule, exit_rule])
    source = _FakeIotSource({"exit": False})
    runner._iot_source = source
    _set_task_on(state_machine, "task-1", "enter")
    runner._record_source.settle = AsyncMock()
    runner._fire = AsyncMock()

    await runner._reconcile_iot_task_state("exit", True)
    await runner.drain()

    runner._record_source.settle.assert_not_awaited()
    runner._fire.assert_not_awaited()
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
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
async def test_session_exit_reentry_during_settle_cancels_stale_exit():
    session_rule = _iot_rule("session", RuleDirection.SESSION, exit_debounce_seconds=0)
    runner, state_machine = _runner_with_state_machine([session_rule])
    source = _FakeIotSource({"session": False})
    runner._iot_source = source
    _set_task_on(state_machine, "task-1", "session")
    runner._ensure_source("session", "device-1").last_bool = False
    runner._ensure_state("session").last_rule_state = False

    settle_started = asyncio.Event()
    release_settle = asyncio.Event()

    async def settle(_task_id: str) -> None:
        settle_started.set()
        await release_settle.wait()

    runner._record_source.settle = settle
    runner._record_source.disarm = MagicMock()
    runner._fire = AsyncMock()

    await runner._reconcile_iot_task_state("session", False)
    exit_task = runner._ensure_state("session").exit_debounce_task
    assert exit_task is not None
    await settle_started.wait()

    await runner.update_state("session", "device-1", True, "", skip_flicker=True)
    release_settle.set()
    result = await asyncio.gather(exit_task, return_exceptions=True)

    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    assert isinstance(result[0], asyncio.CancelledError)
    runner._fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_exit_becoming_unknown_during_settle_abandons_exit():
    session_rule = _iot_rule("session", RuleDirection.SESSION, exit_debounce_seconds=0)
    runner, state_machine = _runner_with_state_machine([session_rule])
    runner._iot_source = _FakeIotSource({"session": False})
    _set_task_on(state_machine, "task-1", "session")
    runner._ensure_source("session", "device-1").last_bool = False
    runner._ensure_state("session").last_rule_state = False

    settle_started = asyncio.Event()
    release_settle = asyncio.Event()

    async def settle(_task_id: str) -> None:
        settle_started.set()
        await release_settle.wait()

    runner._record_source.settle = settle
    runner._record_source.disarm = MagicMock()
    runner._fire = AsyncMock()

    await runner._reconcile_iot_task_state("session", False)
    exit_task = runner._ensure_state("session").exit_debounce_task
    assert exit_task is not None
    await settle_started.wait()

    runner.mark_source_unknown("session", "device-1")
    release_settle.set()
    result = await asyncio.gather(exit_task, return_exceptions=True)

    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    assert isinstance(result[0], asyncio.CancelledError)
    runner._fire.assert_not_awaited()

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
        is TriggerOutcome.NOT_FIRED
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
    guard_outcome = await runner.update_state("guard", "device-1", True, "guard true")
    await runner.drain()

    assert guard_outcome is TriggerOutcome.FIRED
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    fired.assert_awaited_once()
    assert fired.await_args.args[5] == "客厅"
    assert fired.await_args.args[6] == ["camera-1"]
    assert fired.await_args.kwargs["caption"] == "有人进入"
    assert fired.await_args.kwargs["device_name"] == "客厅摄像头"

    await runner.update_state("guard", "device-1", True, "guard still true")
    await runner.drain()
    fired.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_guard_releases_pending_enter_and_exits():
    session_rule = _iot_rule(
        "session", RuleDirection.SESSION, exit_debounce_seconds=0
    )
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine([session_rule, guard_rule])
    source = _FakeIotSource({"session": True, "guard": False})
    runner._iot_source = source
    fired = AsyncMock()
    runner._fire = fired

    await runner.update_state("guard", "device-1", False, "guard false")
    blocked = await runner.update_state("session", "device-1", True, "session enter")
    await runner.drain()

    assert blocked is TriggerOutcome.STILL_IN
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    fired.assert_not_awaited()

    source.values["guard"] = True
    released = await runner.update_state("guard", "device-1", True, "guard true")
    await runner.drain()

    assert released is TriggerOutcome.FIRED
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.ON
    fired.assert_awaited_once()
    assert fired.await_args.args[1] is RuleEvent.ENTERED

    exited = await runner.update_state(
        "session", "device-1", False, "session exit", skip_flicker=True
    )
    exit_task = runner._ensure_state("session").exit_debounce_task
    assert exit_task is not None
    await exit_task

    assert exited is TriggerOutcome.NOT_FIRED
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    assert [call.args[1] for call in fired.await_args_list] == [
        RuleEvent.ENTERED,
        RuleEvent.EXITED,
    ]


@pytest.mark.asyncio
async def test_guard_release_does_not_fire_after_main_rule_exits():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine([enter_rule, guard_rule])
    source = _FakeIotSource({"enter": True, "guard": False})
    runner._iot_source = source
    fired = AsyncMock()
    runner._fire = fired

    await runner.update_state("guard", "device-1", False, "guard false")
    blocked = await runner.update_state("enter", "device-1", True, "main true")
    await runner.drain()

    assert blocked is TriggerOutcome.STILL_IN
    fired.assert_not_awaited()

    source.values["enter"] = False
    exited = await runner.update_state(
        "enter", "device-1", False, "main false", skip_flicker=True
    )
    await runner.drain()

    assert exited is TriggerOutcome.NOT_FIRED
    source.values["guard"] = True
    released = await runner.update_state("guard", "device-1", True, "guard true")
    await runner.drain()

    assert released is TriggerOutcome.NOT_FIRED
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    fired.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_guard_recovery_releases_pending_enter():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine([enter_rule, guard_rule])
    source = _FakeIotSource({"guard": None})
    runner._iot_source = source
    fired = AsyncMock()
    runner._fire = fired

    blocked = await runner.update_state("enter", "device-1", True, "main true")
    await runner.drain()

    assert blocked is TriggerOutcome.STILL_IN
    assert runner.is_condition_satisfied("guard") is None
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    fired.assert_not_awaited()

    source.values["guard"] = True
    recovered = await runner.update_state("guard", "device-1", True, "guard recovered")
    await runner.drain()

    assert recovered is TriggerOutcome.FIRED
    assert runner.is_condition_satisfied("guard") is True
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    fired.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_rule_unknown_keeps_pending_enter_until_guard_recovers():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine([enter_rule, guard_rule])
    source = _FakeIotSource({"enter": True, "guard": False})
    runner._iot_source = source
    fired = AsyncMock()
    runner._fire = fired

    await runner.update_state("guard", "device-1", False, "", skip_flicker=True)
    blocked = await runner.update_state("enter", "device-1", True, "enter")
    await runner.drain()
    assert blocked is TriggerOutcome.STILL_IN
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF

    runner.mark_source_unknown("enter", "device-1")
    source.values["enter"] = True
    recovered_main = await runner.update_state(
        "enter", "device-1", True, "enter recovered", skip_flicker=True
    )
    assert recovered_main is TriggerOutcome.STILL_IN

    source.values["guard"] = True
    recovered_guard = await runner.update_state(
        "guard", "device-1", True, "guard recovered", skip_flicker=True
    )
    await runner.drain()

    assert recovered_guard is TriggerOutcome.FIRED
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    fired.assert_awaited_once()
    assert fired.await_args.args[1] is RuleEvent.ENTERED


@pytest.mark.asyncio
async def test_guard_recovery_keeps_pending_enter_while_main_rule_is_unknown():
    enter_rule = _iot_rule("enter", RuleDirection.ENTER)
    guard_rule = _iot_rule("guard", RuleDirection.GUARD, value=True)
    runner, state_machine = _runner_with_state_machine([enter_rule, guard_rule])
    source = _FakeIotSource({"enter": True, "guard": False})
    runner._iot_source = source
    fired = AsyncMock()
    runner._fire = fired

    await runner.update_state("guard", "device-1", False, "", skip_flicker=True)
    blocked = await runner.update_state("enter", "device-1", True, "enter")
    await runner.drain()
    assert blocked is TriggerOutcome.STILL_IN
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF

    runner.mark_source_unknown("enter", "device-1")
    runner.mark_source_unknown("guard", "device-1")

    source.values["guard"] = True
    recovered_guard = await runner.update_state(
        "guard", "device-1", True, "guard recovered", skip_flicker=True
    )
    await runner.drain()

    assert recovered_guard is TriggerOutcome.NOT_FIRED
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    fired.assert_not_awaited()

    source.values["enter"] = True
    recovered_main = await runner.update_state(
        "enter", "device-1", True, "enter recovered", skip_flicker=True
    )
    await runner.drain()

    assert recovered_main is TriggerOutcome.FIRED
    assert state_machine.runtime_state("task-1") is TaskRuntimeState.OFF
    fired.assert_awaited_once()
    assert fired.await_args.args[1] is RuleEvent.ENTERED
