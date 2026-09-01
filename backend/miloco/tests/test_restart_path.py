# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""重启后状态重建的正向覆盖（spec §7）。

重启后经过的代码在本次整段重写（rule 直接 fire → rule 产边沿 → 状态机 → 动作层），
而 §21.6 决定不补「与旧行为逐项相同」的基线对拍。这里做的是另一件事：**把重启后
实际会发生什么钉死**，包括 §7 自己声明为已知缺陷、明确不修的那两条。

不钉住的话，「已知缺陷」和「后来不小心改坏了」在代码里长得一模一样。

重启的建模是真的重启：把 runner 与状态机整个丢掉，从同一个库重新建一套。只有这样
「内存态没了、DB 还在」这个前提才是真的。
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.rule.schema import Rule, RuleCondition, RuleMode

TASK_ID = "piano"


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "restart.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    connector_module.db_connector = None
    connector_module.init_database()

    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO task (task_id, description, status, created_at) "
        "VALUES (?, '练琴', 'active', 0)",
        (TASK_ID,),
    )
    conn.commit()
    conn.close()

    yield db_file

    connector_module.db_connector = None
    reset_settings()


class _Boot:
    """一次「进程启动」：从库里重建 runner + 状态机 + record 源。"""

    def __init__(self, accumulated=0, target=30):
        from miloco.database.rule_repo import RuleRepo
        from miloco.database.task_repo import TaskRepo
        from miloco.rule.runner import RuleRunner
        from miloco.rule.service import RuleService, attach_task_state_machine

        self.accumulated = accumulated
        record_svc = MagicMock()
        record_svc.detect_record_kind = MagicMock(return_value="duration")
        record_svc.read_duration_target_state = MagicMock(
            side_effect=lambda _t: (target, self.accumulated)
        )
        self.repo = RuleRepo()
        self.runner = RuleRunner(
            rules=self.repo.get_all(),
            miot_proxy=MagicMock(),
            rule_log_repo=MagicMock(),
            task_record_service=record_svc,
        )
        self.service = RuleService(
            self.repo,
            MagicMock(),
            self.runner,
            MagicMock(),
            task_repo=TaskRepo(),
            task_record_service=record_svc,
        )
        self.service._get_valid_perceive_device_ids = AsyncMock(
            return_value=["cam-001"]
        )
        attach_task_state_machine(self.runner, self.repo)
        self.sm = self.runner.state_machine

    def state(self):
        from miloco.task.state_machine import TaskRuntimeState

        return self.sm.runtime_state(TASK_ID) if self.sm else TaskRuntimeState.OFF


def _session_rule(on_target_desc=None):
    rule = Rule(
        name=f"[{TASK_ID}] 练琴计时",
        task_id=TASK_ID,
        mode=RuleMode.STATE,
        condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人在弹琴"),
        exit_debounce_seconds=1,
    )
    rule.on_enter_desc = "记录计时起点"
    rule.on_exit_desc = "结束计时"
    if on_target_desc:
        rule.on_target_desc = on_target_desc
    return rule


async def _feed(runner, rule_id, value, ticks=3):
    for _ in range(ticks):
        await runner.update_state(rule_id, "cam-001", value, "")
    await asyncio.sleep(0.1)


def _events(mock):
    out = []
    for call in mock.call_args_list:
        for cb in call.args[1]:
            out.append((cb.rule_name, cb.event.value))
    return out


# ── §7 的四条 + 一条本次新增的已知缺陷 ────────────────────────────────


@pytest.mark.asyncio
async def test_task_starts_off_after_restart(db):
    """runtime_state 只存内存，重启从 off 起（§7-1）。

    落库恢复 on 的代价不能自愈：非互反模式的退出条件多为脉冲，错过就没有第二次，
    会永久卡在 on。
    """
    from miloco.task.state_machine import TaskRuntimeState

    boot1 = _Boot()
    rule_id = await boot1.service.create_rule(_session_rule())
    with patch("miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)):
        await _feed(boot1.runner, rule_id, True)
    assert boot1.state() is TaskRuntimeState.ON

    assert _Boot().state() is TaskRuntimeState.OFF


@pytest.mark.asyncio
async def test_a_still_true_condition_brings_the_task_back_on(db):
    """持续型进入条件在第一个判定周期重新产边沿，模式自然回到 on（§7-2）。"""
    from miloco.task.state_machine import TaskRuntimeState

    boot1 = _Boot()
    rule_id = await boot1.service.create_rule(_session_rule())

    boot2 = _Boot()
    # 先确认是从 off 起的 —— 少了这句, 初始状态被改成 on 时下面那句恒真
    assert boot2.state() is TaskRuntimeState.OFF
    with patch("miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)):
        await _feed(boot2.runner, rule_id, True)
    assert boot2.state() is TaskRuntimeState.ON


@pytest.mark.asyncio
async def test_restart_re_runs_the_enter_action(db):
    """**已知缺陷，明确不修**（§7）：重启后正在满足的规则会重跑一次进入动作。

    last_rule_state 从 False 起，首个判定周期 diff 出 ENTERED 就重新 fire。
    action_cooldown 与幂等状态同样只在内存、重启一起清零，两道去重闸同时失效，
    所以通知类动作会真的再播一次。

    修它要引入「首次观测只作 baseline」的 seed 语义，是行为变更，单独立项。
    """
    boot1 = _Boot()
    rule_id = await boot1.service.create_rule(_session_rule())
    with patch("miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)):
        await _feed(boot1.runner, rule_id, True)

    boot2 = _Boot()
    with patch(
        "miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)
    ) as mock:
        await _feed(boot2.runner, rule_id, True)

    assert ("[piano] 练琴计时", "ENTERED") in _events(mock)


@pytest.mark.asyncio
async def test_a_condition_that_is_no_longer_true_stays_off(db):
    """**已知限制**（§7）：进入条件是脉冲时重启后回不到 on，那次 on_exit 不执行。

    重启原则是「能重新观测到的就回来，观测不到的归 off」——归 off 的代价能自愈，
    下次条件成立就回正轨。
    """
    from miloco.task.state_machine import TaskRuntimeState

    boot1 = _Boot()
    rule_id = await boot1.service.create_rule(_session_rule())
    with patch("miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)):
        await _feed(boot1.runner, rule_id, True)

    boot2 = _Boot()
    with patch(
        "miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)
    ) as mock:
        await _feed(boot2.runner, rule_id, False)

    assert boot2.state() is TaskRuntimeState.OFF
    assert _events(mock) == []


@pytest.mark.asyncio
async def test_the_record_source_rearms_from_the_current_accumulated(db):
    """timer 由源层按**当前** accumulated 重排，不是从零（§7-2 / §6.4）。

    从零重排的话，重启一次就把这一天已经攒的时长白丢，达标推迟整整一个目标时长。
    """
    from miloco.database.task_repo import TaskRepo

    boot1 = _Boot()
    await boot1.service.create_rule(_session_rule(on_target_desc="达标推送"))
    assert TaskRepo().get_boundary_actions(TASK_ID).get("on_target_desc")

    boot2 = _Boot(accumulated=29)
    rule_id = next(
        r.id for r in boot2.repo.get_all() if r.resolved_direction.value == "session"
    )
    delays: list[float] = []
    original = type(boot2.runner.record_source)._feed_after

    async def spy(self, ref, delay, target_at_arm):
        delays.append(delay)  # 只记, 不真睡

    type(boot2.runner.record_source)._feed_after = spy
    try:
        with patch(
            "miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)
        ):
            await _feed(boot2.runner, rule_id, True)
    finally:
        type(boot2.runner.record_source)._feed_after = original

    # 目标 30、已累计 29 → 只该等剩下的 1 分钟。断言时长而不是"排了几个":
    # 从零重排同样是一个 timer, 只看个数分不开对错。
    assert delays == [60]
    boot2.runner.record_source.disarm(TASK_ID)


@pytest.mark.asyncio
async def test_restart_re_sends_a_target_notification_already_sent_today(db):
    """**已知缺陷，与 §7 的进入动作同源**：今天已经发过的达标通知，重启后会再发。

    防重复靠的是条件项自身的边沿，而条件的值和 last_rule_state 一样只在内存 ——
    重启后从假起，重新算出「够了」就是一次新的假→真。

    旧实现的 target_fired 内存标记同样如此，所以这不是本次引入的回归；钉在这里是
    为了它被修掉（seed 语义）时有测试跟着红。
    """
    boot1 = _Boot(accumulated=60)
    await boot1.service.create_rule(_session_rule(on_target_desc="达标推送"))

    boot2 = _Boot(accumulated=60)
    rule_id = next(
        r.id for r in boot2.repo.get_all() if r.resolved_direction.value == "session"
    )
    with patch(
        "miloco.rule.runner.dispatch_event", new=AsyncMock(return_value=True)
    ) as mock:
        await _feed(boot2.runner, rule_id, True)

    assert ("[piano] 累计达标", "TARGET_FIRED") in _events(mock)
