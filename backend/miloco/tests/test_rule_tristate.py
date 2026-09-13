# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""条件层的三态：未就绪是「不知道」，与「知道它是假」是两件事。

假会驱动退出边沿，不知道不该驱动任何东西。设备离线在 iot 上是常态（电池设备、
休眠设备、网关重启），所以未就绪必须是可回退的当前态，而不是一次性的启动态。
"""

from __future__ import annotations

import asyncio

import pytest
from miloco.rule.runner import RuleRunner, or3
from miloco.rule.schema import Rule, RuleCondition, RuleMode


def _rule(rule_id="r1", task_id="t1", mode=RuleMode.EVENT, **kw):
    return Rule(
        id=rule_id,
        name=rule_id,
        task_id=task_id,
        mode=mode,
        condition=RuleCondition(perceive_device_ids=["cam1", "cam2"], query="有人"),
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


# ── 三值 OR 本身 ──────────────────────────────────────────────────────


def test_or3_unknown_and_false_is_unknown():
    """这一条是三值 OR 存在的全部理由：any([None, False]) 会得出 False。"""
    assert or3([None, False]) is None


def test_or3_true_wins_over_unknown():
    assert or3([None, True]) is True


def test_or3_all_false_is_false():
    assert or3([False, False]) is False


def test_or3_empty_is_unknown():
    assert or3([]) is None


@pytest.mark.parametrize(
    "values",
    [[], [True], [False], [True, False], [False, True], [True, True], [False, False]],
)
def test_or3_matches_any_on_definite_inputs(values):
    """全确定输入下与原来的 any() 逐位相同 —— omni 的热路径不能被改坏。

    空集合是唯一的例外：any([]) 是 False，而三值 OR 给未知。omni 走不到空集合
    （聚合发生在刚喂完一个 source 之后），所以这条差异对它不可达。
    """
    if not values:
        assert or3(values) is None
        return
    assert or3(values) == any(values)


# ── 未知不产退出边沿 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_source_does_not_produce_exit_edge(monkeypatch):
    """一真一未知 → 真；真的那个转假之后 → 未知，不是假，所以不产退出边沿。"""
    # debounce 归零：留着默认的 60 秒，EXITED 在用例时间尺度内永远不 fire，
    # 断言无论改动对错都成立。
    rule = _rule(
        mode=RuleMode.STATE,
        on_enter_desc="进",
        on_exit_desc="出",
        exit_debounce_seconds=0,
    )
    runner = _runner([rule], monkeypatch)
    # 动作装在 task 上；没有槽的话 _fire 直接空转，断言就分不开对错了。
    runner.set_task_actions("t1", {"on_enter_desc": "进", "on_exit_desc": "出"})
    events: list[str] = []

    async def _record(rule_, event, *_a, **_kw):
        events.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    await runner.update_state("r1", "cam1", True, "", skip_flicker=True)
    await asyncio.sleep(0.05)
    assert events == ["ENTERED"]
    runner.mark_source_unknown("r1", "cam2")

    await runner.update_state("r1", "cam1", False, "", skip_flicker=True)
    await asyncio.sleep(0.05)

    assert events == ["ENTERED"]
    assert runner._state["r1"].last_rule_state is True


@pytest.mark.asyncio
async def test_all_false_still_produces_exit_edge(monkeypatch):
    """上一条的反向：没有未知时，转假照样退出。两条一起才把判据钉在「有没有未知」上。"""
    # debounce 归零：留着默认的 60 秒，EXITED 在用例时间尺度内永远不 fire，
    # 断言无论改动对错都成立。
    rule = _rule(
        mode=RuleMode.STATE,
        on_enter_desc="进",
        on_exit_desc="出",
        exit_debounce_seconds=0,
    )
    runner = _runner([rule], monkeypatch)
    # 动作装在 task 上；没有槽的话 _fire 直接空转，断言就分不开对错了。
    runner.set_task_actions("t1", {"on_enter_desc": "进", "on_exit_desc": "出"})
    events: list[str] = []

    async def _record(rule_, event, *_a, **_kw):
        events.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    await runner.update_state("r1", "cam1", True, "", skip_flicker=True)
    await runner.update_state("r1", "cam2", False, "", skip_flicker=True)
    await runner.update_state("r1", "cam1", False, "", skip_flicker=True)
    await asyncio.sleep(0.05)

    assert events == ["ENTERED", "EXITED"]


# ── 置未知撤掉已排队的 exit 抗抖 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_marking_unknown_cancels_queued_exit_debounce(monkeypatch):
    """条件真 → 变假排入 debounce → 置未知：那次 on_exit 不许执行。

    不撤的话 timer 到点仍会 fire，等于未知驱动了动作。
    """
    rule = _rule(
        mode=RuleMode.STATE,
        on_enter_desc="进",
        on_exit_desc="出",
        exit_debounce_seconds=0,
    )
    runner = _runner([rule], monkeypatch)
    # 动作装在 task 上；没有槽的话 _fire 直接空转，断言就分不开对错了。
    runner.set_task_actions("t1", {"on_enter_desc": "进", "on_exit_desc": "出"})
    events: list[str] = []

    async def _record(rule_, event, *_a, **_kw):
        events.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    await runner.update_state("r1", "cam1", True, "", skip_flicker=True)
    await runner.update_state("r1", "cam1", False, "", skip_flicker=True)
    assert runner._state["r1"].exit_debounce_task is not None

    runner.mark_source_unknown("r1", "cam1")
    await asyncio.sleep(0.05)

    assert events == ["ENTERED"]


@pytest.mark.asyncio
async def test_orphaned_debounce_does_not_fire_after_condition_recovers(monkeypatch):
    """置未知只摘句柄不撤 timer 的话，那个 timer 就成了孤儿。

    孤儿 timer 躲得开后续 ENTERED 的取消（句柄已经不在了），到点复查又看到条件已
    恢复为真 —— 于是在条件为真的时候 fire 一次退出。
    """
    rule = _rule(
        mode=RuleMode.STATE,
        on_enter_desc="进",
        on_exit_desc="出",
        exit_debounce_seconds=0,
    )
    runner = _runner([rule], monkeypatch)
    runner.set_task_actions("t1", {"on_enter_desc": "进", "on_exit_desc": "出"})
    events: list[str] = []

    async def _record(rule_, event, *_a, **_kw):
        events.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    await runner.update_state("r1", "cam1", True, "", skip_flicker=True)
    await runner.update_state("r1", "cam1", False, "", skip_flicker=True)
    runner.mark_source_unknown("r1", "cam1")
    await runner.update_state("r1", "cam1", True, "", skip_flicker=True)
    await asyncio.sleep(0.05)

    # 撤抗抖时基线已拨回真，所以恢复为真是 STILL_IN 而不是一次新的进入 —— 两条
    # 一起断：那次退出没被 fire，进入动作也没重复发。
    assert events == ["ENTERED"]


@pytest.mark.asyncio
async def test_exit_still_happens_after_the_device_returns_with_the_same_false(
    monkeypatch,
):
    """抗抖窗口里掉线、设备带同一个假值回来 —— 那次退出必须补上。

    撤抗抖时不把聚合基线拨回真的话，基线停在假、设备回来时聚合也是假，命中
    `old == new` 的早返：退出边沿再也不产生，会话型 task 永久卡在 on。
    """
    rule = _rule(
        mode=RuleMode.STATE,
        on_enter_desc="进",
        on_exit_desc="出",
        exit_debounce_seconds=0,
    )
    runner = _runner([rule], monkeypatch)
    runner.set_task_actions("t1", {"on_enter_desc": "进", "on_exit_desc": "出"})
    events: list[str] = []

    async def _record(rule_, event, *_a, **_kw):
        events.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    await runner.update_state("r1", "cam1", True, "", skip_flicker=True)
    await runner.update_state("r1", "cam1", False, "", skip_flicker=True)
    runner.mark_source_unknown("r1", "cam1")
    await runner.update_state("r1", "cam1", False, "", skip_flicker=True)
    await asyncio.sleep(0.05)

    assert events == ["ENTERED", "EXITED"]


@pytest.mark.asyncio
async def test_debounce_rechecks_condition_before_firing(monkeypatch):
    """到点侧要自己复查一次。撤 timer 与 timer 到点是竞态，只靠撤挡不住已经醒来的
    那一次 —— 这里绕过 mark_source_unknown 直接置未知，模拟撤晚了。
    """
    rule = _rule(
        mode=RuleMode.STATE,
        on_enter_desc="进",
        on_exit_desc="出",
        exit_debounce_seconds=0,
    )
    runner = _runner([rule], monkeypatch)
    runner.set_task_actions("t1", {"on_enter_desc": "进", "on_exit_desc": "出"})
    events: list[str] = []

    async def _record(rule_, event, *_a, **_kw):
        events.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    await runner.update_state("r1", "cam1", True, "", skip_flicker=True)
    await runner.update_state("r1", "cam1", False, "", skip_flicker=True)
    runner._state["r1"].sources["cam1"].last_bool = None
    await asyncio.sleep(0.05)

    assert events == ["ENTERED"]


@pytest.mark.asyncio
async def test_exit_still_happens_after_the_recheck_abandons_it(monkeypatch):
    """到点侧自己放弃那一次退出之后，聚合基线也要跟着拨回真。

    与置未知那一侧同一个命题，只是走的是竞态输掉、由到点侧兜住的那条路：不拨的话
    设备带同一个假值回来时命中 `old == new` 的早返，这次退出永远补不上。
    """
    rule = _rule(
        mode=RuleMode.STATE,
        on_enter_desc="进",
        on_exit_desc="出",
        exit_debounce_seconds=0,
    )
    runner = _runner([rule], monkeypatch)
    runner.set_task_actions("t1", {"on_enter_desc": "进", "on_exit_desc": "出"})
    events: list[str] = []

    async def _record(rule_, event, *_a, **_kw):
        events.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    await runner.update_state("r1", "cam1", True, "", skip_flicker=True)
    await runner.update_state("r1", "cam1", False, "", skip_flicker=True)
    runner._state["r1"].sources["cam1"].last_bool = None
    await asyncio.sleep(0.05)
    # 先钉住这条用例真的走了放弃分支, 否则下面断的可能是另一条路。
    assert events == ["ENTERED"]

    await runner.update_state("r1", "cam1", False, "", skip_flicker=True)
    await asyncio.sleep(0.05)

    assert events == ["ENTERED", "EXITED"]


# ── 事件型 task：重新触发靠条件层 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_event_type_rule_fires_again_after_the_condition_goes_false(monkeypatch):
    """一条 enter 型的 iot rule 独占 task 时，runtime_state 恒 off，每次进信号都执行。

    **必须走完整的假中间态**：只喂两次真是 STILL_IN，条件层压根不产生第二次边沿。
    这是「第一次好用、之后再也不响」那个现象的判定点 —— 它依赖能收到「条件转假」的
    那次推送。
    """
    rule = _rule(mode=RuleMode.EVENT)
    runner = _runner([rule], monkeypatch)
    runner.set_task_actions("t1", {"on_enter_desc": "播报"})
    events: list[str] = []

    async def _record(rule_, event, *_a, **_kw):
        events.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    for value in (True, False, True):
        await runner.update_state("r1", "cam1", value, "", skip_flicker=True)
    await asyncio.sleep(0.05)

    assert events == ["ENTERED", "ENTERED"]


@pytest.mark.asyncio
async def test_event_type_rule_does_not_fire_twice_without_the_false(monkeypatch):
    """上一条的反向：不走假中间态就只有一次。两条一起才把「靠条件层重新触发」钉住。"""
    rule = _rule(mode=RuleMode.EVENT)
    runner = _runner([rule], monkeypatch)
    runner.set_task_actions("t1", {"on_enter_desc": "播报"})
    events: list[str] = []

    async def _record(rule_, event, *_a, **_kw):
        events.append(event.value)
        return None

    runner._fire = _record  # ty:ignore[invalid-assignment]

    for _ in range(3):
        await runner.update_state("r1", "cam1", True, "", skip_flicker=True)
    await asyncio.sleep(0.05)

    assert events == ["ENTERED"]
