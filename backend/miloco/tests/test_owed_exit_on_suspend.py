# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""停用期间欠下的那次退出, 启用后怎么结。

停用把运行态清成 off 而不派 on_exit —— 那是有意的 (停用是"不再观察", 不是"观察到
条件不成立")。代价是设备上那份由进入动作留下的配置再没人复位: 启用后条件层从假重
建, 退出边沿要的 True → False 跃变不会再来。

两种形态的结法不同, 这里两半都测:

- 互反 (单条 session): 启用后拿到第一次确定的条件值时按值结算, 为假就补发退出。
- 非互反 (enter + exit): 退出条件多是脉冲, 启用后不会自动成立 —— 把在态补回来,
  住户下一次主动退出才走得通。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest
from miloco.database.rule_repo import RuleLogRepo, RuleRepo
from miloco.database.task_repo import TaskRepo
from miloco.rule import runner as runner_module
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import Rule, RuleCondition, RuleDirection, RuleMode
from miloco.rule.service import RuleService, attach_task_state_machine
from miloco.task.state_machine import TaskRuntimeState


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


@pytest.fixture
def sent(monkeypatch):
    """agent 回调的落点。断在这里而不是 ``_execute_dynamic`` 的入参上:

    要验的"补发时 agent 收不到时间戳"是 ``_compose_prompt_text`` 拼出来的那段文本,
    在入参上断言等于绕开了真正决定这件事的那一步。
    """
    captured = []

    async def fake_send(self, callback):
        captured.append(callback)
        return True

    monkeypatch.setattr(RuleRunner, "_send_dynamic_with_retry", fake_send)
    return captured


@pytest.fixture
def clock(monkeypatch):
    """可控时钟。补发要熬过这条规则自己的退出防抖（默认一分钟），真等不现实。"""
    now = [1_000_000_000]
    monkeypatch.setattr(runner_module, "now_ms", lambda: now[0])
    return now


def _rule(name, task_id, direction, dids=("cam1",), exit_debounce_seconds=0):
    return Rule(
        name=name,
        task_id=task_id,
        mode=RuleMode.STATE if direction is RuleDirection.SESSION else RuleMode.EVENT,
        direction=direction,
        condition=RuleCondition(perceive_device_ids=list(dids), query="有人"),
        exit_debounce_seconds=exit_debounce_seconds,
    )


def _build(rules, task_ids=("t1",), actions=None):
    actions = actions or {"on_enter_desc": "进入动作", "on_exit_desc": "退出动作"}
    task_repo = TaskRepo()
    for task_id in task_ids:
        task_repo.create_task(task_id, "d")
        task_repo.set_boundary_actions(task_id, **actions)
    rule_repo = RuleRepo()
    ids = [rule_repo.create(r) for r in rules]
    runner = RuleRunner(
        rules=rule_repo.get_all(enabled_only=False),
        miot_proxy=None,
        rule_log_repo=RuleLogRepo(),
    )
    attach_task_state_machine(runner, rule_repo)
    service = RuleService(rule_repo, RuleLogRepo(), runner, None)
    return service, runner, ids


def _slot_texts(callbacks):
    """每次回调派的是哪一侧的动作 —— 意图那段就是槽里存的文案。"""
    return ["进入" if "进入动作" in c.prompt_text else "退出" for c in callbacks]


def _extra_info(callback) -> dict:
    """agent 解析的是提示语末尾那段 JSON。

    不能改用"提示语里有没有出现过这个键名": task 带 record 时提示语会拼上一段处理
    流程说明, 那段本身就把两个时间戳键名列在里面, 子串判断恒真。
    """
    return json.loads(callback.prompt_text.split("**额外信息**：\n")[-1])


def _iso_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _init_duration_record(task_id="t1", started_minutes_ago=None, content=None):
    from miloco.task_record.schema import RecordKind
    from miloco.task_record.service import TaskRecordService
    from miloco.utils.time_utils import ms_to_iso_local, now_ms

    record = TaskRecordService()
    record.init_record(task_id, RecordKind.DURATION, content or {})
    started_at = None
    if started_minutes_ago is not None:
        started_at = ms_to_iso_local(now_ms() - started_minutes_ago * 60 * 1000)
        record.session_start(task_id, at=started_at)
    return record, started_at


async def _enter(runner, rule_id, did="cam1"):
    await runner.update_state(rule_id, did, True)
    await runner.drain()


async def _observe(runner, rule_id, did, value):
    await runner.update_state(rule_id, did, value)
    await runner.drain()


async def _pause(service, runner, task_id="t1"):
    service.apply_task_status(task_id, False)
    await runner.drain()


async def _resume(service, runner, task_id="t1"):
    service.apply_task_status(task_id, True)
    await runner.drain()


async def _pause_then_resume(service, runner, task_id="t1"):
    await _pause(service, runner, task_id)
    await _resume(service, runner, task_id)


# ── 互反形态: 单条 session 规则 ──────────────────────────────────────


@pytest.fixture
def reciprocal(env, sent, clock):
    service, runner, ids = _build([_rule("[t1] 观影", "t1", RuleDirection.SESSION)])
    return service, runner, ids[0], sent, clock


@pytest.mark.asyncio
async def test_resends_exit_when_resumed_inside_the_window(reciprocal):
    """窗口内启用、人已经走了 → 退出动作补跑一次。"""
    service, runner, rule_id, sent, clock = reciprocal
    await _enter(runner, rule_id)
    assert _slot_texts(sent) == ["进入"]

    await _pause(service, runner)
    await _resume(service, runner)
    # 补发要发生在拿到观测之后, 不是停用或启用那一刻
    assert _slot_texts(sent) == ["进入"]

    await _observe(runner, rule_id, "cam1", False)
    clock[0] += 61 * 1000
    await _observe(runner, rule_id, "cam1", False)

    assert _slot_texts(sent) == ["进入", "退出"]


@pytest.mark.asyncio
async def test_does_not_resend_after_the_window(reciprocal, monkeypatch):
    """失去观察太久 → 一个动作都不派。

    欠账记的是停用那一刻的意图, 隔太久现状多半已经被人动过, 补发是拿过期的意图
    覆盖现状。
    """
    service, runner, rule_id, sent, clock = reciprocal
    await _enter(runner, rule_id)
    monkeypatch.setattr(runner_module, "OWED_EXIT_RESEND_WINDOW_MS", 0)

    await _pause_then_resume(service, runner)
    # 喂够让证据成立的观测, 这样拦住补发的只可能是窗口这一条
    await _observe(runner, rule_id, "cam1", False)
    clock[0] += 61 * 1000
    await _observe(runner, rule_id, "cam1", False)

    assert _slot_texts(sent) == ["进入"]


@pytest.mark.asyncio
async def test_expired_owed_exit_leaves_a_log(reciprocal, caplog):
    """超时不补发要留痕 —— 窗口该取多长, 依据就是这条日志攒出来的分布。"""
    service, runner, rule_id, _sent, clock = reciprocal
    await _enter(runner, rule_id)
    await _pause(service, runner)
    clock[0] += 3600 * 1000
    await _resume(service, runner)

    with caplog.at_level(logging.INFO, logger="miloco.rule.runner"):
        await _observe(runner, rule_id, "cam1", False)

    expired = [r for r in caplog.records if "OWED_EXIT_EXPIRED" in r.getMessage()]
    assert len(expired) == 1
    message = expired[0].getMessage()
    assert "task=t1" in message
    assert "失去观察 3600 秒" in message


@pytest.mark.asyncio
async def test_resend_needs_the_same_evidence_as_a_normal_exit(reciprocal):
    """补发依据的"条件为假", 证据强度不能低于正常退出那条路。

    正常退出要连续两帧为假才确认, 确认之后还要熬过这条规则的退出防抖。而停用把整条
    规则的状态弹掉了, 启用后每个源的上一帧都是初值假 —— 那两道闸一道都不生效。住户
    还在看电影、启用后第一帧恰好漏识, 就会当场把窗帘拉开。
    """
    service, runner, rule_id, sent, clock = reciprocal
    debounce_ms = RuleRepo().get_by_id(rule_id).exit_debounce_seconds * 1000
    await _enter(runner, rule_id)
    await _pause(service, runner)
    await _resume(service, runner)

    await _observe(runner, rule_id, "cam1", False)
    assert _slot_texts(sent) == ["进入"]

    clock[0] += debounce_ms - 1000
    await _observe(runner, rule_id, "cam1", False)
    assert _slot_texts(sent) == ["进入"]

    clock[0] += 2000
    await _observe(runner, rule_id, "cam1", False)
    assert _slot_texts(sent) == ["进入", "退出"]


@pytest.mark.asyncio
async def test_expires_without_waiting_for_a_source_that_never_reports(
    env, sent, clock, caplog
):
    """声明的相机里有一台一直不上报 → 超过窗口就把账结掉, 别无声挂着。

    超过窗口就不动设备了, 条件是真是假都不改变这个结论。等下去的话这笔账会永远悬着,
    连超时那条日志都留不下 —— 而窗口该取多长全靠它攒出来的时长分布。
    """
    service, runner, ids = _build(
        [_rule("[t1] 观影", "t1", RuleDirection.SESSION, dids=("cam1", "cam2"))]
    )
    rule_id = ids[0]
    await _enter(runner, rule_id, "cam1")
    await _pause(service, runner)
    clock[0] += 3600 * 1000
    await _resume(service, runner)

    with caplog.at_level(logging.INFO, logger="miloco.rule.runner"):
        await _observe(runner, rule_id, "cam1", False)

    assert _slot_texts(sent) == ["进入"]
    assert runner.owed_exit_paused_at("t1") is None
    assert [r for r in caplog.records if "OWED_EXIT_EXPIRED" in r.getMessage()]


@pytest.mark.asyncio
async def test_pausing_dispatches_nothing(reciprocal):
    """停用那一刻不动设备。用户关掉自动化不该反过来被理解成"结束观影"。"""
    service, runner, rule_id, sent, clock = reciprocal
    await _enter(runner, rule_id)

    service.apply_task_status("t1", False)
    await runner.drain()

    assert _slot_texts(sent) == ["进入"]


@pytest.mark.asyncio
async def test_condition_still_true_takes_the_normal_enter_path(reciprocal):
    """启用后人还在 → 照常走进入路径, 不压掉这次 on_enter。

    压掉它会连带压掉计时段的开启: 段只能由 agent 收到 ``actual_started_at`` 之后
    去开, 不 fire 就永远不重开, 结果是在态却不再累计。
    """
    service, runner, rule_id, sent, clock = reciprocal
    _init_duration_record()
    await _enter(runner, rule_id)

    await _pause_then_resume(service, runner)
    await _observe(runner, rule_id, "cam1", True)

    assert _slot_texts(sent) == ["进入", "进入"]
    info = _extra_info(sent[-1])
    assert info["record_kind"] == "duration"
    assert "actual_started_at" in info


@pytest.mark.asyncio
async def test_resent_exit_carries_no_session_timestamp(reciprocal):
    """补发只补设备动作, 记账不参与。

    计时段在停用那一刻已经由 ``close_active_session`` 结清了。补发再带上
    ``actual_exited_at``, agent 会照着调一次 session-end, 撞上"没有活跃段"报错。
    """
    service, runner, rule_id, sent, clock = reciprocal
    _init_duration_record()
    await _enter(runner, rule_id)

    await _pause(service, runner)
    await _resume(service, runner)
    assert _slot_texts(sent) == ["进入"]

    await _observe(runner, rule_id, "cam1", False)
    clock[0] += 61 * 1000
    await _observe(runner, rule_id, "cam1", False)

    assert _slot_texts(sent) == ["进入", "退出"]
    info = _extra_info(sent[-1])
    # record 确实在, 否则这条判据验的是"agent 本来就没有记账操作可做"的形态
    assert info["record_kind"] == "duration"
    assert "actual_exited_at" not in info
    assert "actual_started_at" not in info


@pytest.mark.asyncio
async def test_waits_until_every_source_has_reported(env, sent, clock):
    """两台相机的规则, 只回来一台不算数。

    OR 聚合下先到的那台报假不代表整条为假 —— 据此补发退出, 等另一台随后报真又会
    进一次, 设备来回动一趟, 而这正是要避免的那类误动作。
    """
    service, runner, ids = _build(
        [_rule("[t1] 观影", "t1", RuleDirection.SESSION, dids=("cam1", "cam2"))]
    )
    rule_id = ids[0]
    await _enter(runner, rule_id, "cam1")
    await _observe(runner, rule_id, "cam2", True)
    await _pause_then_resume(service, runner)

    await _observe(runner, rule_id, "cam1", False)
    clock[0] += 61 * 1000
    await _observe(runner, rule_id, "cam1", False)
    assert _slot_texts(sent) == ["进入"]

    await _observe(runner, rule_id, "cam2", False)
    clock[0] += 61 * 1000
    await _observe(runner, rule_id, "cam2", False)
    assert _slot_texts(sent) == ["进入", "退出"]


@pytest.mark.asyncio
async def test_second_pause_keeps_the_first_moment(reciprocal):
    """停用 → 启用 → 没结算完又停用 → 再启用: 按第一次欠账的时刻算。

    覆盖成第二次的话失去观察的时长会算短, 早就该作废的欠账反而落回窗口内。
    """
    service, runner, rule_id, _sent, clock = reciprocal
    start = clock[0]
    await _enter(runner, rule_id)

    service.apply_task_status("t1", False)
    first = runner.owed_exit_paused_at("t1")
    service.apply_task_status("t1", True)
    clock[0] += 10 * 60 * 1000
    service.apply_task_status("t1", False)
    service.apply_task_status("t1", True)
    await runner.drain()

    assert first == start
    assert runner.owed_exit_paused_at("t1") == first


@pytest.mark.asyncio
async def test_enter_only_task_owes_nothing(env, sent):
    """只有 enter 规则的 task 不记欠账 —— 它没有出路径, 运行态恒 off。

    每次进信号执行一次动作就完事, 不存在一份留在设备上等着被复位的配置。
    """
    service, runner, ids = _build([_rule("[t1] 打招呼", "t1", RuleDirection.ENTER)])
    await _enter(runner, ids[0])
    assert _slot_texts(sent) == ["进入"]

    service.apply_task_status("t1", False)
    await runner.drain()

    assert runner.owed_exit_paused_at("t1") is None


# ── 非互反形态: enter + exit 两条规则 ────────────────────────────────


@pytest.fixture
def nonreciprocal(env, sent):
    service, runner, ids = _build(
        [
            _rule("[t1] 比手势开灯", "t1", RuleDirection.ENTER, dids=("cam1",)),
            _rule("[t1] 挥手关灯", "t1", RuleDirection.EXIT, dids=("cam2",)),
        ]
    )
    return service, runner, ids[0], ids[1], sent


@pytest.mark.asyncio
async def test_exit_gesture_still_works_after_resume(nonreciprocal):
    """停用再启用之后, 住户做退出动作 → 退出动作真的派发。

    不把在态补回来的话, 退出信号会撞上状态机第一道闸判成"本来就没开着", 而那个
    结论不派动作也不报错 —— 住户只会觉得手势失灵。
    """
    service, runner, enter_id, exit_id, sent = nonreciprocal
    await _enter(runner, enter_id, "cam1")
    assert _slot_texts(sent) == ["进入"]

    await _pause(service, runner)
    await _resume(service, runner)
    # 退出动作要由住户那一下带出来, 不是停用或启用顺手派的
    assert _slot_texts(sent) == ["进入"]

    await _observe(runner, exit_id, "cam2", True)

    assert _slot_texts(sent) == ["进入", "退出"]


@pytest.mark.asyncio
async def test_resume_reopens_the_duration_session(env, sent):
    """恢复在态要连带把计时段重开。

    补回在态之后住户再比一次手势会判"已在态内"、不再 fire on_enter, 而段只能由那
    条路开 —— 不补这一下, "重新触发进入就恢复累计"这条自愈路径就被堵死了。
    """
    from miloco.utils.time_utils import now_ms

    service, runner, ids = _build(
        [
            _rule("[t1] 比手势开灯", "t1", RuleDirection.ENTER, dids=("cam1",)),
            _rule("[t1] 挥手关灯", "t1", RuleDirection.EXIT, dids=("cam2",)),
        ]
    )
    # 起点放在过去: 与当前时刻同秒的段会被收尾那一步当成倒挂区间跳过, 段留着不收,
    # 这条用例的前提就没建起来。
    record, started_at = _init_duration_record(started_minutes_ago=5)

    def active_start():
        return record.get_active_record("t1")["derived"]["active_session_start_at"]

    await _enter(runner, ids[0], "cam1")
    await _pause(service, runner)
    assert active_start() is None

    before_ms = now_ms()
    await _resume(service, runner)

    reopened = active_start()
    assert reopened is not None
    # 起点是重新开始观测的那一刻。接上停用前那一段的起点, 会把没人看着的那段时间
    # 一起算进累计。
    assert _iso_ms(reopened) > _iso_ms(started_at)
    assert _iso_ms(reopened) >= before_ms - 1000


@pytest.mark.asyncio
async def test_resume_does_not_reopen_a_completed_session(env, sent):
    """今日已达标的 task, 恢复在态时不再开计时段。

    达标之后 agent 那条路就不再调 session-end 了（意图里的处理流程第一步拦掉）, 这里
    开的段没人收尾; 非 recurring 的行又不跨日切段, 派生累计会按「到现在」一直涨 ——
    住户第二天打开看到的今日时长是几百分钟。
    """
    service, runner, ids = _build(
        [
            _rule("[t1] 比手势开灯", "t1", RuleDirection.ENTER, dids=("cam1",)),
            _rule("[t1] 挥手关灯", "t1", RuleDirection.EXIT, dids=("cam2",)),
        ]
    )
    record, _ = _init_duration_record(
        started_minutes_ago=2, content={"target_minutes": 1}
    )

    await _enter(runner, ids[0], "cam1")
    await _pause(service, runner)
    assert record.get_active_record("t1")["record"]["status"] == "completed"

    await _resume(service, runner)

    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.ON
    assert record.get_active_record("t1")["derived"]["active_session_start_at"] is None


@pytest.mark.asyncio
async def test_resume_is_not_subject_to_the_time_window(nonreciprocal, monkeypatch):
    """时间闸只管互反那一半。

    补回在态不新开任何口子: 之后的行为全走正常路径, 误识别的退出动作风险与正常在
    态时完全一样。绕开这条去给它单独立一个窗口, 挡掉的是住户真实的退出意图。
    """
    service, runner, enter_id, exit_id, sent = nonreciprocal
    await _enter(runner, enter_id, "cam1")
    monkeypatch.setattr(runner_module, "OWED_EXIT_RESEND_WINDOW_MS", 0)

    await _pause(service, runner)
    await _resume(service, runner)
    assert _slot_texts(sent) == ["进入"]

    await _observe(runner, exit_id, "cam2", True)

    assert _slot_texts(sent) == ["进入", "退出"]


@pytest.mark.asyncio
async def test_resume_needs_an_owed_exit(nonreciprocal):
    """停用时本来就在 off 态 → 启用后仍是 off。

    没有下过进入动作就没有欠账, 一律补成 on 会让从没开始过的 task 显示进行中, 并
    且把随后一次误识别的退出动作放行出去。
    """
    service, runner, _enter_id, _exit_id, _sent = nonreciprocal

    await _pause_then_resume(service, runner)

    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.OFF


@pytest.mark.asyncio
async def test_real_exit_clears_the_debt_with_an_empty_exit_slot(env, sent):
    """住户真的退出过, 账就不欠了 —— 哪怕退出槽没配动作。

    退出槽为空只是没东西可下发, 不改变"这一轮结束了"。账留着的话, 之后一次停用启用
    会把运行态凭空补回 on: 详情页恒显示进行中、达标被武装、计时段又开一段没人收。
    """
    service, runner, ids = _build(
        [
            _rule("[t1] 比手势开灯", "t1", RuleDirection.ENTER, dids=("cam1",)),
            _rule("[t1] 挥手关灯", "t1", RuleDirection.EXIT, dids=("cam2",)),
        ],
        actions={"on_enter_desc": "进入动作"},
    )
    await _enter(runner, ids[0], "cam1")
    await _observe(runner, ids[1], "cam2", True)
    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.OFF
    assert _slot_texts(sent) == ["进入"]

    await _pause_then_resume(service, runner)

    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.OFF


@pytest.mark.asyncio
async def test_forced_exit_on_losing_the_exit_path_clears_the_debt(env, sent):
    """删掉出路径时强发的那次退出, 同样把账结掉。

    那一刻拓扑已经换成新的、没有出路径了, 清账要是也看"有没有出路径"就会漏掉这一次:
    出路径加回来之后一次停用启用, 运行态又凭空回到 on。
    """
    service, runner, ids = _build(
        [
            _rule("[t1] 比手势开灯", "t1", RuleDirection.ENTER, dids=("cam1",)),
            _rule("[t1] 挥手关灯", "t1", RuleDirection.EXIT, dids=("cam2",)),
        ]
    )
    await _enter(runner, ids[0], "cam1")

    await service.delete_rule(ids[1])
    await runner.drain()
    assert _slot_texts(sent) == ["进入", "退出"]

    repo = RuleRepo()
    back = repo.create(_rule("[t1] 挥手关灯", "t1", RuleDirection.EXIT, dids=("cam2",)))
    runner.add_rule(repo.get_by_id(back))
    service.reconfigure_task("t1")
    await _pause_then_resume(service, runner)

    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.OFF


@pytest.mark.asyncio
async def test_entering_again_after_resume_is_idempotent(nonreciprocal):
    """补回在态之后再触发进入 → 判"已在态内", 不重复派进入动作。"""
    service, runner, enter_id, _exit_id, sent = nonreciprocal
    await _enter(runner, enter_id, "cam1")
    await _pause_then_resume(service, runner)

    await _observe(runner, enter_id, "cam1", True)

    assert _slot_texts(sent) == ["进入"]
    assert runner.state_machine.runtime_state("t1") is TaskRuntimeState.ON
