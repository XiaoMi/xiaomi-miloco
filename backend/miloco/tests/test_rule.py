# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Tests for Rule modules: schema, runner, service
"""

import asyncio
import json
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.middleware.exceptions import (
    BusinessException,
    ConflictException,
    ResourceNotFoundException,
    ValidationException,
)
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    ConditionItem,
    Rule,
    RuleAction,
    RuleActionExecuteResult,
    RuleCondition,
    RuleConditionDNF,
    RuleConditionUpdate,
    RuleDirection,
    RuleEvent,
    RuleExecuteResult,
    RuleLifecycle,
    RuleLog,
    RuleMode,
    RuleUpdate,
    TriggerOutcome,
    aggregate_outcomes,
)
from miloco.rule.service import RuleService
from miloco.task.state_machine import TaskRuntimeState
from pydantic import ValidationError

# ---- Helpers ----

TASK_ID = "test_task"


def _extra_info(prompt_text: str) -> dict:
    """从 prompt 末尾的 "**额外信息**：\\n{...}" 块解析 JSON。无则返 {}."""
    m = re.search(r"\*\*额外信息\*\*：\n(\{.*\})\s*$", prompt_text, re.DOTALL)
    return json.loads(m.group(1)) if m else {}


def _make_condition(device_ids=None, query="有人经过"):
    return RuleCondition(
        perceive_device_ids=device_ids or ["cam-001"],
        query=query,
    )


def _make_action(
    did="device-001", iid="prop.2.1", value=True, idempotent=True, cooldown=None
):
    return RuleAction(
        did=did, iid=iid, value=value, idempotent=idempotent, cooldown_minutes=cooldown
    )


def _name(task_id, suffix="rule"):
    """V3 要求 name 以 [<task_id>] 开头。"""
    return f"[{task_id}] {suffix}"


def _make_static_rule(
    rule_id="rule-1",
    task_id=TASK_ID,
    name=None,
    enabled=True,
    actions=None,
    condition=None,
):
    return Rule(
        id=rule_id,
        name=name if name is not None else _name(task_id, "static"),
        task_id=task_id,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=enabled,
        condition=condition or _make_condition(),
        actions=actions if actions is not None else [_make_action()],
    )


def _make_dynamic_rule(
    rule_id="rule-d1",
    task_id=TASK_ID,
    name=None,
    enabled=True,
    descriptions=None,
):
    return Rule(
        id=rule_id,
        name=name if name is not None else _name(task_id, "dynamic"),
        task_id=task_id,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=enabled,
        condition=_make_condition(),
        actions=[],
        action_descriptions=descriptions
        if descriptions is not None
        else ["打开客厅灯", "调到暖白模式"],
    )


def _make_state_rule(
    rule_id="rule-s1",
    task_id=TASK_ID,
    name=None,
    enabled=True,
    on_enter_actions=None,
    on_enter_desc=None,
    on_exit_actions=None,
    on_exit_desc=None,
    exit_debounce_seconds=0,
    lifecycle=RuleLifecycle.PERMANENT,
    terminate_when=None,
    condition=None,
):
    return Rule(
        id=rule_id,
        name=name if name is not None else _name(task_id, "state"),
        task_id=task_id,
        mode=RuleMode.STATE,
        lifecycle=lifecycle,
        enabled=enabled,
        condition=condition or _make_condition(),
        on_enter_actions=on_enter_actions or [],
        on_enter_desc=on_enter_desc,
        on_exit_actions=on_exit_actions or [],
        on_exit_desc=on_exit_desc,
        exit_debounce_seconds=exit_debounce_seconds,
        terminate_when=terminate_when,
    )


TRIGGER_CONTEXT = "cam-001 检测到有人经过"

# mock 的米家场景表：场景动作的 did 要在这里面，否则建规则时被存在性校验拦下。
# 场景校验会按家庭白名单过滤，所以桩要带 home_id、KV 里也要有对应的白名单。
MOCK_HOME_ID = "home-1"
SCENE_IID_LITERAL = "scene"
MOCK_SCENES = ("scene-1", "scene-A", "scene-B", "scene-movie", "scene-real")


# ---- Fixtures ----


@pytest.fixture
def mock_miot_proxy():
    proxy = AsyncMock()
    proxy.get_camera_dids = AsyncMock(return_value=["cam-001", "cam-002"])
    proxy.get_device_properties = AsyncMock(return_value=[{"code": 0, "value": False}])
    proxy.set_device_properties = AsyncMock(return_value=[{"code": 0}])
    proxy.call_device_action = AsyncMock(return_value={"code": 0})
    proxy.get_all_scenes = AsyncMock(
        return_value={
            sid: SimpleNamespace(home_id=MOCK_HOME_ID, scene_name=f"mock-{sid}")
            for sid in MOCK_SCENES
        }
    )
    proxy._kv_repo.get = MagicMock(
        side_effect=lambda key, default=None: (
            f'["{MOCK_HOME_ID}"]'
            if key == ScopeConfigKeys.HOME_WHITE_LIST_KEY
            else default
        )
    )
    return proxy


@pytest.fixture
def mock_log_repo():
    repo = MagicMock()
    repo.create = MagicMock(return_value="log-id-1")
    repo.get_all = MagicMock(return_value=[])
    repo.get_by_rule_id = MagicMock(return_value=[])
    repo.count_all = MagicMock(return_value=0)
    repo.count_by_rule_id = MagicMock(return_value=0)
    repo.delete_by_rule_id = MagicMock(return_value=True)
    repo.delete_before_days = MagicMock(return_value=5)
    return repo


@pytest.fixture
def mock_rule_repo():
    repo = MagicMock()
    repo.create = MagicMock(return_value="new-rule-id")
    repo.get_by_id = MagicMock(return_value=None)
    repo.get_all = MagicMock(return_value=[])
    repo.update = MagicMock(return_value=True)
    repo.delete = MagicMock(return_value=True)
    repo.exists = MagicMock(return_value=True)
    repo.exists_by_name = MagicMock(return_value=False)
    return repo


@pytest.fixture
def mock_task_repo():
    repo = MagicMock()
    # rule create 前置校验 task 存在；mock 默认放行
    repo.task_exists = MagicMock(return_value=True)
    # 默认「没配达标动作」。不显式给的话 MagicMock 的返回值恒真, 每条 rule create
    # 都会被当成"这个 task 配了达标动作"去查出路径。
    repo.get_full_view = MagicMock(return_value=None)
    return repo


@pytest.fixture
def mock_task_record_service():
    """默认 mock：detect_record_kind / read_duration_target_state 都返 None；
    具体测试可覆盖返回值测试 record-bound 路径。"""
    svc = MagicMock()
    svc.detect_record_kind = MagicMock(return_value=None)
    svc.read_duration_target_state = MagicMock(return_value=None)
    return svc


@pytest.fixture
def runner(mock_miot_proxy, mock_log_repo, mock_task_record_service):
    rules = [_make_static_rule(), _make_dynamic_rule()]
    return RuleRunner(
        rules=rules, miot_proxy=mock_miot_proxy, rule_log_repo=mock_log_repo,
        task_record_service=mock_task_record_service,
    )


@pytest.mark.asyncio
async def test_rule_static_action_writes_ledger_source_rule(runner, monkeypatch):
    """rule static 直控经 _execute_action 也落 action_ledger(source=rule / source_id=rule_id)。

    修 Zirconi review:rule 直控绕过 MiotService.control_device,原先审计不到——现复用
    同一 _write_action_ledger helper,不再有覆盖盲区。
    """
    from unittest.mock import AsyncMock as _AM

    from miloco.rule.schema import RuleAction

    spy = _AM()
    monkeypatch.setattr("miloco.miot.service._write_action_ledger", spy)

    action = RuleAction(did="dev1", iid="prop.2.1", value=True,
                        idempotent=False, cooldown_minutes=10)
    await runner._execute_action("rule-42", action)

    spy.assert_awaited_once()
    kw = spy.await_args.kwargs
    assert kw["source"] == "rule"
    assert kw["source_id"] == "rule-42"
    assert kw["did"] == "dev1"
    assert kw["action_type"] == "set_property"
    assert kw["success"] is True


@pytest.mark.asyncio
async def test_rule_action_exception_ledger_keeps_attempted_value(
    runner, monkeypatch, mock_miot_proxy
):
    """异常路径台账保留尝试参数(value_json 非 NULL)——失败动作是排查时最有
    价值的场景,不能只落 error 看不到当时想设什么值(review: Zirconi)。"""
    from unittest.mock import AsyncMock as _AM

    from miloco.rule.schema import RuleAction

    spy = _AM()
    monkeypatch.setattr("miloco.miot.service._write_action_ledger", spy)
    mock_miot_proxy.set_device_properties = _AM(side_effect=RuntimeError("net down"))

    action = RuleAction(did="dev1", iid="prop.2.1", value=True, idempotent=False)
    result = await runner._execute_action("rule-9", action)

    assert result.result is False
    spy.assert_awaited_once()
    kw = spy.await_args.kwargs
    assert kw["success"] is False
    assert kw["action_type"] == "set_property"
    assert kw["value_json"] == "true"


def _make_duration_rule(rule_id, duration_seconds):
    """STATE duration 规则；on_enter_desc 让 slot 非空。sample_interval 默认 3s，
    duration_seconds=3 → maxlen=1（首帧即达标 FIRED）；=6 → maxlen=2（首帧 COUNTING）。"""
    return Rule(
        id=rule_id,
        name=_name(TASK_ID, rule_id),
        task_id=TASK_ID,
        mode=RuleMode.STATE,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=_make_condition(),
        on_enter_desc="提醒一下",
        duration_seconds=duration_seconds,
        duration_ratio=1.0,
        exit_debounce_seconds=0,
    )


class TestTriggerOutcome:
    """update_state 返回触发结论（供住户日志「触发状态」展示；引擎不留跨 cycle 状态）。"""

    @pytest.mark.asyncio
    async def test_first_enter_returns_fired(self, runner):
        out = await runner.update_state("rule-1", "cam-001", True)
        await runner.drain()
        assert out is TriggerOutcome.FIRED

    @pytest.mark.asyncio
    async def test_paused_task_rule_does_not_fire(self, runner):
        """task 停用 → 规则不再参与判定。

        两条 rule 的 ``enabled`` 都是 True, 只看 ``rule.enabled`` 的话停用那条
        也会 FIRED —— 停用就成了空操作。
        """
        runner.add_rule(_make_static_rule(rule_id="rule-paused", task_id="task-paused"))
        runner.set_task_paused("task-paused", True)

        paused_out = await runner.update_state("rule-paused", "cam-001", True)
        active_out = await runner.update_state("rule-1", "cam-001", True)
        await runner.drain()

        assert paused_out is TriggerOutcome.NOT_FIRED
        assert active_out is TriggerOutcome.FIRED

    @pytest.mark.asyncio
    async def test_still_in_returns_still_in(self, runner):
        await runner.update_state("rule-1", "cam-001", True)  # ENTER → FIRED
        out = await runner.update_state("rule-1", "cam-001", True)  # 持续
        await runner.drain()
        assert out is TriggerOutcome.STILL_IN

    @pytest.mark.asyncio
    async def test_flicker_absorbed_returns_still_in(self, runner):
        await runner.update_state("rule-1", "cam-001", True)   # ENTER
        await runner.update_state("rule-1", "cam-001", False)  # 单帧 False → pending_exit
        out = await runner.update_state("rule-1", "cam-001", True)  # 抖动吸收
        await runner.drain()
        assert out is TriggerOutcome.STILL_IN

    @pytest.mark.asyncio
    async def test_absorbed_pending_exit_returns_still_in(self, runner):
        """exit_debounce 期被 ENTER 打断（吸收伪退出）→ 规则持续在态 → STILL_IN
        （与帧级抖动吸收同标签）。exit_debounce_seconds 设大，保证 debounce 不在本测试内 fire。"""
        rule = _make_state_rule(
            rule_id="st-absorb",
            on_enter_actions=[_make_action()],
            on_exit_actions=[_make_action()],
            exit_debounce_seconds=60,
        )
        runner.add_rule(rule)
        await runner.update_state("st-absorb", "cam-001", True)   # ENTER
        await runner.update_state("st-absorb", "cam-001", False)  # 单帧 False → pending_exit
        await runner.update_state("st-absorb", "cam-001", False)  # 确认 EXIT → 调度 debounce
        await runner.update_state("st-absorb", "cam-001", True)   # debounce 期 1st true → 观察
        out = await runner.update_state("st-absorb", "cam-001", True)  # 2nd true → 吸收 pending exit
        await runner.drain()
        assert out is TriggerOutcome.STILL_IN

    @pytest.mark.asyncio
    async def test_disabled_rule_returns_not_fired(self, runner):
        runner.add_rule(_make_static_rule(rule_id="off-1", enabled=False))
        out = await runner.update_state("off-1", "cam-001", True)
        assert out is TriggerOutcome.NOT_FIRED

    @pytest.mark.asyncio
    async def test_duration_counting_when_window_not_full(self, runner):
        runner.add_rule(_make_duration_rule("dur-count", duration_seconds=6))
        out = await runner.update_state("dur-count", "cam-001", True)
        await runner.drain()
        assert out is TriggerOutcome.COUNTING

    @pytest.mark.asyncio
    async def test_duration_fired_when_window_met(self, runner):
        # duration_seconds == sample_interval(3) → maxlen 1 → 首帧即达标
        runner.add_rule(_make_duration_rule("dur-fire", duration_seconds=3))
        out = await runner.update_state("dur-fire", "cam-001", True)
        await runner.drain()
        assert out is TriggerOutcome.FIRED

    @pytest.mark.asyncio
    async def test_duration_exit_not_overridden_by_duration_outcome(self, runner):
        """STATE + duration 确认离开的那一周期返回 NOT_FIRED，不能被 dur_outcome 覆盖。

        duration 规则的结论平时一律取 _evaluate_duration，但 EXITED 是 rule 级转换、
        结论该由 _dispatch_event 给（NOT_FIRED）。若照旧走 out() 覆盖，STATE 已 fire 过
        on_enter 时 _evaluate_duration 会早返 STILL_IN（「还在态内」）——恰与「刚确认离开」
        语义相反。今天零后果只因唯一产生 EXITED 的调用点丢弃返回值；这里把它钉住。
        """
        runner.add_rule(_make_duration_rule("dur-exit", duration_seconds=3))
        assert await runner.update_state("dur-exit", "cam-001", True) is TriggerOutcome.FIRED
        await runner.drain()
        # 首帧 False 只置 pending_exit（抗抖观察），第二帧才确认 EXIT
        await runner.update_state("dur-exit", "cam-001", False)
        out = await runner.update_state("dur-exit", "cam-001", False)
        await runner.drain()
        assert out is TriggerOutcome.NOT_FIRED, (
            "EXITED 周期的结论被 duration 的 dur_outcome 覆盖成了语义相反的值"
        )

    @pytest.mark.asyncio
    async def test_duration_exit_cycle_keeps_real_fire(self, runner):
        """确认离开那一周期若 duration **真派发过**，结论必须是 FIRED、不能报 NOT_FIRED。

        ratio<1（默认 0.6）时窗口末尾的 0 被容忍后仍可达标，而窗口填满的那一刻可以正好落在
        确认离开的周期：maxlen=5 / ratio=0.6，喂 T,T,T,F,F —— 第 5 帧 _evaluate_duration
        首次填满窗口、3/5=0.6 达标 → 真 _spawn_fire；同一周期 rule 级 True→False → EXITED。
        此时若照 EXITED 一律取 _dispatch_event 的 NOT_FIRED，就把一次真派发说没了。
        """
        rule = _make_duration_rule("dur-exit-fire", duration_seconds=15)  # /3 → maxlen 5
        rule.duration_ratio = 0.6
        runner.add_rule(rule)
        # round_id = int(time.time() / sample_interval)，同周期内的重复采样会被去重，
        # 故每帧都要推进一个采样周期（sample_interval=3）。
        with patch("miloco.rule.runner.time.time") as mt:
            for i, val in enumerate([True, True, True, False, False]):
                mt.return_value = 300.0 + i * 3
                out = await runner.update_state("dur-exit-fire", "cam-001", val)
        await runner.drain()
        assert out is TriggerOutcome.FIRED, (
            "duration 在确认离开那一周期真派发了，却被 EXITED 分支报成未触发"
        )

    def test_aggregate_outcomes_priority(self):
        assert aggregate_outcomes([]) is None
        assert (
            aggregate_outcomes([TriggerOutcome.STILL_IN, TriggerOutcome.FIRED])
            is TriggerOutcome.FIRED
        )
        assert (
            aggregate_outcomes([TriggerOutcome.NOT_FIRED, TriggerOutcome.STILL_IN])
            is TriggerOutcome.STILL_IN
        )
        assert (
            aggregate_outcomes([TriggerOutcome.COUNTING, TriggerOutcome.STILL_IN])
            is TriggerOutcome.COUNTING
        )

    def test_priority_map_covers_all_members(self):
        """_OUTCOME_PRIORITY 必须覆盖 TriggerOutcome 全部成员。

        aggregate_outcomes 对缺映射成员按最弱(-1)兜底、不抛错(避免在 client 的 finally
        落库路径顶替原始异常),代价是漏补映射运行时不再暴露——只能靠本测试在 CI 拦住。
        """
        from miloco.rule.schema import _OUTCOME_PRIORITY

        assert set(_OUTCOME_PRIORITY) == set(TriggerOutcome)


@pytest.fixture
def service(
    mock_rule_repo, mock_log_repo, mock_miot_proxy, mock_task_repo,
    mock_task_record_service,
):
    r = RuleRunner(
        rules=[], miot_proxy=mock_miot_proxy, rule_log_repo=mock_log_repo,
        task_record_service=mock_task_record_service,
    )
    svc = RuleService(
        mock_rule_repo, mock_log_repo, r, mock_miot_proxy,
        task_repo=mock_task_repo,
        task_record_service=mock_task_record_service,
    )
    # Mock _get_valid_perceive_device_ids to avoid dependency on global Manager singleton
    svc._get_valid_perceive_device_ids = AsyncMock(return_value=["cam-001", "cam-002"])  # ty:ignore[invalid-assignment]
    return svc


# ============================================================
# Schema tests
# ============================================================


class TestRuleSchema:
    def test_rule_action_prop(self):
        action = RuleAction(did="d1", iid="prop.2.1", value=True)
        assert action.idempotent is True
        assert action.cooldown_minutes is None
        assert action.params is None

    def test_rule_action_action_type(self):
        action = RuleAction(did="d1", iid="action.3.1", params=[1, "hello"])
        assert action.iid.startswith("action.")
        assert action.params == [1, "hello"]

    def test_rule_condition(self):
        cond = _make_condition(device_ids=["a", "b"], query="检测到有人")
        assert len(cond.perceive_device_ids) == 2
        assert cond.query == "检测到有人"

    def test_rule_defaults(self):
        rule = _make_static_rule()
        assert rule.enabled is True
        assert rule.action_descriptions == []
        assert rule.actions  # STATIC: actions 非空

    def test_rule_update_all_optional(self):
        update = RuleUpdate()
        assert update.name is None
        assert update.enabled is None
        assert update.condition is None
        assert update.actions is None
        assert update.action_descriptions is None

    def test_rule_update_partial(self):
        update = RuleUpdate(name="new-name", enabled=False)
        assert update.name == "new-name"
        assert update.enabled is False


class TestDirectionDecidesMode:
    """direction 给了就由它定 mode。

    mode 在阶段 A 还是存储字段, 迁移和旧库回退都按它读; 两者打架会让 rule 半边
    生效 —— 比如 session 配 event, task 进得去但退出边沿被 EXITED 分支直接吃掉。
    """

    @staticmethod
    def _rule(**kw):
        return Rule(
            id="r1",
            name="n",
            task_id="t1",
            condition=_make_condition(),
            **kw,
        )

    def test_session_coerces_mode_to_state(self):
        rule = self._rule(
            direction=RuleDirection.SESSION, on_enter_actions=[_make_action()]
        )
        assert rule.mode is RuleMode.STATE

    def test_exit_coerces_mode_to_event(self):
        rule = self._rule(direction=RuleDirection.EXIT, actions=[_make_action()])
        assert rule.mode is RuleMode.EVENT

    def test_milestone_coerces_mode_to_event(self):
        rule = self._rule(direction=RuleDirection.MILESTONE, actions=[_make_action()])
        assert rule.mode is RuleMode.EVENT

    def test_explicit_mismatch_is_rejected(self):
        with pytest.raises(ValidationError, match="direction=session 对应 mode=state"):
            self._rule(
                direction=RuleDirection.SESSION,
                mode=RuleMode.EVENT,
                on_enter_actions=[_make_action()],
            )

    def test_matching_explicit_mode_is_accepted(self):
        rule = self._rule(
            direction=RuleDirection.SESSION,
            mode=RuleMode.STATE,
            on_enter_actions=[_make_action()],
        )
        assert rule.mode is RuleMode.STATE

    def test_no_direction_leaves_mode_alone(self):
        """存量数据 direction 为 NULL, mode 仍是唯一信源, 不能被改写。"""
        rule = self._rule(mode=RuleMode.STATE, on_enter_actions=[_make_action()])
        assert rule.direction is None
        assert rule.mode is RuleMode.STATE
        assert rule.resolved_direction is RuleDirection.SESSION


class TestRuleLogSchema:
    def test_action_execute_result_defaults(self):
        action = _make_action()
        aer = RuleActionExecuteResult(action=action, result=True)
        assert aer.skipped is False

    def test_execute_result_static(self):
        from miloco.rule.schema import RuleEvent

        er = RuleExecuteResult(
            event=RuleEvent.ENTERED,
            action_results=[],
            dynamic_rule_event_sent=False,
        )
        assert er.event == RuleEvent.ENTERED
        assert er.dynamic_rule_event_sent is False

    def test_execute_result_dynamic(self):
        from miloco.rule.schema import RuleEvent

        er = RuleExecuteResult(
            event=RuleEvent.ENTERED,
            dynamic_rule_event_sent=True,
        )
        assert er.action_results == []
        assert er.dynamic_rule_event_sent is True

    def test_log_model(self):
        log = RuleLog(
            id="log-1",
            timestamp=1700000000000,
            rule_id="r1",
            rule_name="test",
            rule_query="有人",
            trigger_context="cam-001 检测到有人",
        )
        assert log.execute_result is None
        assert log.timestamp == 1700000000000
        assert log.trigger_context == "cam-001 检测到有人"


# ============================================================
# Runner tests
# ============================================================


class TestRuleRunnerManagement:
    def test_init_loads_rules(self, runner):
        assert len(runner.get_all_rules()) == 2

    def test_add_rule(self, runner):
        new_rule = _make_static_rule(rule_id="rule-new", name="new")
        runner.add_rule(new_rule)
        assert runner.get_rule("rule-new") is not None
        assert len(runner.get_all_rules()) == 3

    def test_remove_rule(self, runner):
        runner.remove_rule("rule-1")
        assert runner.get_rule("rule-1") is None
        assert len(runner.get_all_rules()) == 1

    def test_remove_rule_cleans_cooldown(self, runner):
        runner._ensure_state("rule-1").action_cooldown[("d1", "prop.2.1")] = time.time()
        runner._ensure_state("rule-d1").action_cooldown[("d1", "prop.2.1")] = time.time()
        runner.remove_rule("rule-1")
        assert ("rule-1", "d1", "prop.2.1") not in runner._action_cooldown_state
        assert ("rule-d1", "d1", "prop.2.1") in runner._action_cooldown_state

    def test_get_enabled_rules(self, runner):
        runner.add_rule(
            _make_static_rule(rule_id="disabled-1", name="off", enabled=False)
        )
        enabled = runner.get_enabled_rules()
        assert all(r.enabled for r in enabled)
        assert len(enabled) == 2

    def test_get_rule_not_found(self, runner):
        assert runner.get_rule("nonexistent") is None


class TestRuleRunnerTrigger:
    @pytest.mark.asyncio
    async def test_trigger_rule_not_found(self, runner):
        result = await runner.trigger_rule("nonexistent", TRIGGER_CONTEXT)
        assert result is None

    @pytest.mark.asyncio
    async def test_trigger_disabled_rule(self, runner):
        runner.add_rule(
            _make_static_rule(rule_id="disabled", name="off", enabled=False)
        )
        result = await runner.trigger_rule("disabled", TRIGGER_CONTEXT)
        assert result is None

    @pytest.mark.asyncio
    async def test_trigger_rule_of_paused_task(self, runner):
        """手动触发也要过有效启用闸: rule.enabled 是 True, 但 task 停用。"""
        runner.add_rule(_make_static_rule(rule_id="rule-paused", task_id="task-paused"))
        runner.set_task_paused("task-paused", True)

        assert await runner.trigger_rule("rule-paused", TRIGGER_CONTEXT) is None
        assert await runner.trigger_rule("rule-1", TRIGGER_CONTEXT) is not None

    @pytest.mark.asyncio
    async def test_trigger_static_rule_success(
        self, runner, mock_miot_proxy, mock_log_repo
    ):
        result = await runner.trigger_rule("rule-1", TRIGGER_CONTEXT)
        assert result is not None
        assert len(result.action_results) == 1  # STATIC: action_results 非空
        assert result.action_results[0].result is True
        mock_log_repo.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_dynamic_rule_without_descriptions_returns_none(self, runner):
        rule = _make_dynamic_rule(
            rule_id="rule-d-empty", name="dyn-no-desc", descriptions=[]
        )
        runner.add_rule(rule)
        result = await runner.trigger_rule("rule-d-empty", TRIGGER_CONTEXT)
        assert result is None

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_trigger_dynamic_rule_with_descriptions_sends(
        self, mock_send, runner, mock_log_repo
    ):
        mock_send.return_value = True
        rule = _make_dynamic_rule(
            rule_id="rule-d2",
            name="dyn-empty",
            descriptions=["打开客厅灯", "调到暖白模式"],
        )
        runner.add_rule(rule)
        result = await runner.trigger_rule("rule-d2", TRIGGER_CONTEXT)
        assert result is not None
        assert result.dynamic_rule_event_sent is True  # DYNAMIC: 回调已发送
        mock_send.assert_called_once()
        mock_log_repo.create.assert_called_once()

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_trigger_dynamic_rule_send_failure(self, mock_send, runner):
        mock_send.return_value = False
        rule = _make_dynamic_rule(
            rule_id="rule-d3",
            name="dyn-fail",
            descriptions=["打开客厅灯", "调到暖白模式"],
        )
        runner.add_rule(rule)
        result = await runner.trigger_rule("rule-d3", TRIGGER_CONTEXT)
        assert result is not None
        assert result.dynamic_rule_event_sent is False


class TestRuleRunnerActionExecution:
    @pytest.mark.asyncio
    async def test_execute_prop_set(self, runner, mock_miot_proxy):
        await runner.trigger_rule("rule-1", TRIGGER_CONTEXT)
        mock_miot_proxy.set_device_properties.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_action_call(self, runner, mock_miot_proxy):
        action = RuleAction(did="d1", iid="action.3.1", params=[1], idempotent=False)
        rule = _make_static_rule(
            rule_id="rule-action", name="action-rule", actions=[action]
        )
        runner.add_rule(rule)
        await runner.trigger_rule("rule-action", TRIGGER_CONTEXT)
        mock_miot_proxy.call_device_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_idempotent_skip_when_already_at_target(
        self, runner, mock_miot_proxy
    ):
        mock_miot_proxy.get_device_properties = AsyncMock(
            return_value=[{"code": 0, "value": True}]
        )
        action = _make_action(value=True, idempotent=True)
        rule = _make_static_rule(rule_id="rule-idem", name="idem", actions=[action])
        runner.add_rule(rule)

        result = await runner.trigger_rule("rule-idem", TRIGGER_CONTEXT)
        assert result.action_results[0].skipped is True
        assert result.action_results[0].result is True
        mock_miot_proxy.set_device_properties.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_executes_when_not_at_target(
        self, runner, mock_miot_proxy
    ):
        mock_miot_proxy.get_device_properties = AsyncMock(
            return_value=[{"code": 0, "value": False}]
        )
        action = _make_action(value=True, idempotent=True)
        rule = _make_static_rule(rule_id="rule-idem2", name="idem2", actions=[action])
        runner.add_rule(rule)

        result = await runner.trigger_rule("rule-idem2", TRIGGER_CONTEXT)
        assert result.action_results[0].skipped is False
        mock_miot_proxy.set_device_properties.assert_called_once()

    @pytest.mark.asyncio
    async def test_cooldown_skips_within_window(self, runner, mock_miot_proxy):
        action = _make_action(idempotent=False, cooldown=10)
        rule = _make_static_rule(rule_id="rule-cd", name="cooldown", actions=[action])
        runner.add_rule(rule)

        result1 = await runner.trigger_rule("rule-cd", TRIGGER_CONTEXT)
        assert result1.action_results[0].skipped is False

        result2 = await runner.trigger_rule("rule-cd", TRIGGER_CONTEXT)
        assert result2.action_results[0].skipped is True

    @pytest.mark.asyncio
    async def test_cooldown_executes_after_window(self, runner, mock_miot_proxy):
        action = _make_action(idempotent=False, cooldown=1)
        rule = _make_static_rule(rule_id="rule-cd2", name="cooldown2", actions=[action])
        runner.add_rule(rule)

        await runner.trigger_rule("rule-cd2", TRIGGER_CONTEXT)
        runner._ensure_state("rule-cd2").action_cooldown[
            ("device-001", "prop.2.1")
        ] = time.time() - 120

        result = await runner.trigger_rule("rule-cd2", TRIGGER_CONTEXT)
        assert result.action_results[0].skipped is False

    @pytest.mark.asyncio
    async def test_invalid_iid_format(self, runner):
        action = RuleAction(did="d1", iid="bad-iid", value=True)
        rule = _make_static_rule(rule_id="rule-bad", name="bad-iid", actions=[action])
        runner.add_rule(rule)

        result = await runner.trigger_rule("rule-bad", TRIGGER_CONTEXT)
        assert result.action_results[0].result is False

    @pytest.mark.asyncio
    async def test_execute_action_exception(self, runner, mock_miot_proxy):
        mock_miot_proxy.set_device_properties = AsyncMock(
            side_effect=Exception("network error")
        )
        result = await runner.trigger_rule("rule-1", TRIGGER_CONTEXT)
        assert result.action_results[0].result is False

    @pytest.mark.asyncio
    async def test_idempotent_check_failure_still_executes(
        self, runner, mock_miot_proxy
    ):
        mock_miot_proxy.get_device_properties = AsyncMock(
            side_effect=Exception("timeout")
        )
        mock_miot_proxy.set_device_properties = AsyncMock(return_value=[{"code": 0}])

        result = await runner.trigger_rule("rule-1", TRIGGER_CONTEXT)
        assert result.action_results[0].result is True
        mock_miot_proxy.set_device_properties.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_actions_executed(self, runner, mock_miot_proxy):
        actions = [
            _make_action(did="d1", iid="prop.2.1", value=True),
            _make_action(did="d2", iid="prop.3.2", value=50),
        ]
        rule = _make_static_rule(rule_id="rule-multi", name="multi", actions=actions)
        runner.add_rule(rule)

        result = await runner.trigger_rule("rule-multi", TRIGGER_CONTEXT)
        assert len(result.action_results) == 2
        assert all(ar.result is True for ar in result.action_results)


# ============================================================
# Service tests
# ============================================================


class TestRuleServiceCreate:
    @pytest.mark.asyncio
    async def test_create_static_rule_success(self, service, mock_rule_repo):
        rule = _make_static_rule(rule_id="")
        rule_id = await service.create_rule(rule)
        assert rule_id == "new-rule-id"
        mock_rule_repo.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_duplicate_name_raises(self, service, mock_rule_repo):
        mock_rule_repo.exists_by_name.return_value = True
        rule = _make_static_rule(name="dup")
        with pytest.raises(ConflictException, match="already exists"):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_invalid_camera_raises(self, service, mock_miot_proxy):
        rule = _make_static_rule(condition=_make_condition(device_ids=["invalid-cam"]))
        with pytest.raises(ValidationException, match="Invalid perception device IDs"):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_enter_without_action_raises_when_task_slot_empty(
        self, service
    ):
        rule = _make_static_rule(rule_id="", actions=[])
        with pytest.raises(
            ValidationException,
            match=r"direction=enter 的规则没有动作可落",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_enter_without_action_ok_when_task_slot_filled(
        self, service, mock_task_repo
    ):
        """多条 enter 共用 task 上那一份动作 —— 第二条起本来就不该带自己的动作。"""
        mock_task_repo.get_full_view.return_value = {
            "actions": {"on_enter_desc": "开灯"}
        }
        rule = _make_static_rule(rule_id="", actions=[])
        assert await service.create_rule(rule) == "new-rule-id"

    @pytest.mark.asyncio
    async def test_create_enter_reads_enter_slot_not_exit_slot(
        self, service, mock_task_repo
    ):
        mock_task_repo.get_full_view.return_value = {
            "actions": {"on_exit_desc": "关灯"}
        }
        rule = _make_static_rule(rule_id="", actions=[])
        with pytest.raises(
            ValidationException,
            match=r"direction=enter 的规则没有动作可落",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_exit_without_action_ok(self, service, mock_rule_repo):
        """不带动作的 exit 只推状态, 是让 task 可重入的正常配置。"""
        mock_rule_repo.list_by_task.return_value = [_make_static_rule()]
        rule = Rule(
            id="",
            name=_name(TASK_ID, "exit"),
            task_id=TASK_ID,
            direction=RuleDirection.EXIT,
            condition=_make_condition(),
        )
        assert await service.create_rule(rule) == "new-rule-id"

    @pytest.mark.asyncio
    async def test_create_enter_rejects_when_task_view_unreadable(
        self, service, mock_task_repo
    ):
        mock_task_repo.get_full_view.side_effect = RuntimeError("db gone")
        rule = _make_static_rule(rule_id="", actions=[])
        with pytest.raises(BusinessException, match=r"无法确认 enter 规则有动作可落"):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_event_with_both_actions_and_descriptions_raises(self, service):
        rule = _make_dynamic_rule(rule_id="")
        rule.actions = [_make_action()]
        with pytest.raises(
            ValidationException,
            match=r"event mode: actions and action_descriptions are mutually exclusive",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_repo_failure_raises(self, service, mock_rule_repo):
        mock_rule_repo.create.return_value = None
        rule = _make_static_rule()
        with pytest.raises(BusinessException, match="Failed to create rule"):
            await service.create_rule(rule)


class TestRuleDurationRatioDefault:
    """duration_ratio 三层优先级：CLI/API 显式 > settings > 代码默认 0.6。"""

    @pytest.fixture(autouse=True)
    def _isolated_settings(self, tmp_path, monkeypatch):
        from miloco.config import reset_settings

        monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
        reset_settings()
        yield
        reset_settings()

    @pytest.mark.asyncio
    async def test_unspecified_falls_back_to_code_default(
        self, service, mock_rule_repo
    ):
        rule = _make_static_rule(rule_id="")
        assert rule.duration_ratio is None  # schema 默认 None
        await service.create_rule(rule)
        persisted = mock_rule_repo.create.call_args[0][0]
        assert persisted.duration_ratio == 0.6

    @pytest.mark.asyncio
    async def test_settings_overrides_code_default(
        self, service, mock_rule_repo, monkeypatch
    ):
        from miloco.config import reset_settings

        monkeypatch.setenv("MILOCO_RULE__DEFAULT_DURATION_RATIO", "0.9")
        reset_settings()
        rule = _make_static_rule(rule_id="")
        await service.create_rule(rule)
        persisted = mock_rule_repo.create.call_args[0][0]
        assert persisted.duration_ratio == 0.9

    @pytest.mark.asyncio
    async def test_explicit_overrides_settings(
        self, service, mock_rule_repo, monkeypatch
    ):
        from miloco.config import reset_settings

        monkeypatch.setenv("MILOCO_RULE__DEFAULT_DURATION_RATIO", "0.9")
        reset_settings()
        rule = _make_static_rule(rule_id="")
        rule.duration_ratio = 0.7
        await service.create_rule(rule)
        persisted = mock_rule_repo.create.call_args[0][0]
        assert persisted.duration_ratio == 0.7


class TestRuleServiceGet:
    @pytest.mark.asyncio
    async def test_get_rule_success(self, service, mock_rule_repo):
        expected = _make_static_rule(rule_id="r1")
        mock_rule_repo.get_by_id.return_value = expected
        result = await service.get_rule("r1")
        assert result.id == "r1"

    @pytest.mark.asyncio
    async def test_get_rule_not_found(self, service, mock_rule_repo):
        mock_rule_repo.get_by_id.return_value = None
        with pytest.raises(ResourceNotFoundException):
            await service.get_rule("nonexistent")

    @pytest.mark.asyncio
    async def test_get_all_rules(self, service, mock_rule_repo):
        mock_rule_repo.get_all.return_value = [_make_static_rule(), _make_dynamic_rule()]
        rules = await service.get_all_rules()
        assert len(rules) == 2

    @pytest.mark.asyncio
    async def test_get_all_rules_enabled_only(self, service, mock_rule_repo):
        await service.get_all_rules(enabled_only=True)
        mock_rule_repo.get_all.assert_called_with(True)


class TestRuleServiceUpdate:
    @pytest.mark.asyncio
    async def test_update_rule_success(self, service, mock_rule_repo):
        rule = _make_static_rule(rule_id="r1")
        mock_rule_repo.get_by_id.return_value = rule
        result = await service.update_rule(rule)
        assert result is True
        mock_rule_repo.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_rule_no_id_raises(self, service):
        rule = _make_static_rule(rule_id="")
        with pytest.raises(ValidationException, match="Rule ID is required"):
            await service.update_rule(rule)

    @pytest.mark.asyncio
    async def test_update_rule_not_found(self, service, mock_rule_repo):
        mock_rule_repo.get_by_id.return_value = None
        rule = _make_static_rule(rule_id="missing")
        with pytest.raises(ResourceNotFoundException):
            await service.update_rule(rule)

    @pytest.mark.asyncio
    async def test_update_rule_name_conflict(self, service, mock_rule_repo):
        mock_rule_repo.exists_by_name.return_value = True
        rule = _make_static_rule(rule_id="r1", name="dup-name")
        mock_rule_repo.get_by_id.return_value = rule
        with pytest.raises(ConflictException):
            await service.update_rule(rule)


class TestRuleServicePatch:
    @pytest.mark.asyncio
    async def test_patch_name(self, service, mock_rule_repo):
        existing = _make_static_rule(rule_id="r1")
        mock_rule_repo.get_by_id.return_value = existing

        new_name = _name(TASK_ID, "renamed")
        update = RuleUpdate(name=new_name)
        result = await service.patch_rule("r1", update)
        assert result is True
        updated_rule = mock_rule_repo.update.call_args[0][0]
        assert updated_rule.name == new_name

    @pytest.mark.asyncio
    async def test_patch_enabled(self, service, mock_rule_repo):
        existing = _make_static_rule(rule_id="r1", enabled=True)
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate(enabled=False)
        await service.patch_rule("r1", update)
        updated_rule = mock_rule_repo.update.call_args[0][0]
        assert updated_rule.enabled is False

    @pytest.mark.asyncio
    async def test_patch_clearing_own_action_cannot_pass_its_own_gate(
        self, service, mock_rule_repo, mock_task_repo
    ):
        """清空动作这一改动不能拿"清空前的 task 槽"给自己放行。

        动作是双写的, 而透传排在校验之后 —— 读写前的槽, 校验看到的正是自己马上
        要清掉的那份值。`rule update <id> --clear action_descriptions` 就走这条路。
        """
        existing = _make_dynamic_rule(rule_id="r1")
        mock_rule_repo.get_by_id.return_value = existing
        mock_task_repo.get_full_view.return_value = {
            "actions": {"on_enter_desc": "1. 开台灯"}
        }

        with pytest.raises(ValidationException, match="没有动作可落"):
            await service.patch_rule("r1", RuleUpdate(action_descriptions=[]))
        mock_rule_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_patch_clearing_a_contended_slot_is_not_blocked(
        self, service, mock_rule_repo, mock_task_repo
    ):
        """兄弟 rule 也管着这个槽时透传整体跳过 —— 预演必须跟着跳过, 不然会 400
        掉一次根本不会造成哑规则的改动, 而错误文案给的两条出路都答不上原因。
        """
        target = _make_dynamic_rule(rule_id="r1")
        sibling = _make_dynamic_rule(
            rule_id="r2", name=_name(TASK_ID, "sibling"), descriptions=["开台灯"]
        )
        mock_rule_repo.get_by_id.return_value = target
        mock_rule_repo.list_by_task.return_value = [target, sibling]
        mock_task_repo.get_full_view.return_value = {
            "actions": {"on_enter_desc": "1. 开台灯"}
        }

        assert (
            await service.patch_rule("r1", RuleUpdate(action_descriptions=[])) is True
        )

    @pytest.mark.asyncio
    async def test_patch_session_to_enter_cannot_pass_on_the_slot_it_vacates(
        self, service, mock_rule_repo, mock_task_repo
    ):
        """换方向时 task 槽会被先清空 (_clear_task_slots), 校验必须把这一步算进去。"""
        existing = Rule(
            id="r1",
            name=_name(TASK_ID, "session"),
            task_id=TASK_ID,
            direction=RuleDirection.SESSION,
            condition=_make_condition(),
            on_enter_desc="开台灯",
        )
        mock_rule_repo.get_by_id.return_value = existing
        mock_rule_repo.list_by_task.return_value = [existing]
        mock_task_repo.get_full_view.return_value = {
            "actions": {"on_enter_desc": "开台灯"}
        }

        with pytest.raises(ValidationException, match="没有动作可落"):
            await service.patch_rule(
                "r1", RuleUpdate(direction=RuleDirection.ENTER, on_enter_desc=None)
            )

    @pytest.mark.asyncio
    async def test_patch_keeps_passing_when_the_slot_survives(
        self, service, mock_rule_repo, mock_task_repo
    ):
        """本次没动动作字段 → 透传不写 on_enter 槽 → 读现值放行, 别把正常改动拦了。"""
        existing = _make_static_rule(rule_id="r1", actions=[])
        mock_rule_repo.get_by_id.return_value = existing
        mock_task_repo.get_full_view.return_value = {
            "actions": {"on_enter_desc": "开台灯"}
        }

        assert await service.patch_rule("r1", RuleUpdate(enabled=False)) is True

    @pytest.mark.asyncio
    async def test_patch_direction_also_writes_mode(self, service, mock_rule_repo):
        """两列必须一起动 —— 只改一个会写出读不回来的行。

        Rule 没开 validate_assignment, 逐字段赋值不重跑构造期校验; 写出
        mode/direction 打架的行之后, 下次构造 Rule 抛 ValidationError, 它是
        ValueError 子类, 正好落进各读取口的 except: 规则从所有列表里消失。
        """
        existing = _make_static_rule(rule_id="r1")
        mock_rule_repo.get_by_id.return_value = existing

        # 方向和动作形态一起改 —— 单方向与会话两边的动作字段本来就不通用
        await service.patch_rule(
            "r1",
            RuleUpdate(
                direction=RuleDirection.SESSION,
                actions=[],
                on_enter_actions=[_make_action()],
            ),
        )

        updated = mock_rule_repo.update.call_args[0][0]
        assert updated.direction is RuleDirection.SESSION
        assert updated.mode is RuleMode.STATE
        # 真正的判据: 这一行还能不能被读回来 (与 _dict_to_rule 同样两列都显式传)
        Rule(**updated.model_dump())

    @pytest.mark.asyncio
    async def test_patch_mode_only_also_writes_direction(self, service, mock_rule_repo):
        """老客户端只发 mode。mode 是有损投影, event 一律回到 enter。"""
        existing = _make_static_rule(rule_id="r1")
        existing.direction = RuleDirection.EXIT
        mock_rule_repo.get_by_id.return_value = existing

        await service.patch_rule("r1", RuleUpdate(mode=RuleMode.EVENT))

        updated = mock_rule_repo.update.call_args[0][0]
        assert updated.direction is RuleDirection.ENTER
        assert updated.mode is RuleMode.EVENT

    @pytest.mark.asyncio
    async def test_patch_conflicting_mode_and_direction_is_rejected(
        self, service, mock_rule_repo
    ):
        mock_rule_repo.get_by_id.return_value = _make_static_rule(rule_id="r1")
        with pytest.raises(ValidationException, match="direction=exit 对应 mode=event"):
            await service.patch_rule(
                "r1",
                RuleUpdate(mode=RuleMode.STATE, direction=RuleDirection.EXIT),
            )
        mock_rule_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_patch_enter_to_session_is_rejected_not_written(
        self, service, mock_rule_repo
    ):
        """enter → session 真的不合法(动作字段两边形态不同), 要当场拒。

        修之前 mode 留在 event, 一致性校验按 event 矩阵放行, 于是写出一行读不回来
        的数据而 HTTP 返回 200。
        """
        mock_rule_repo.get_by_id.return_value = _make_static_rule(rule_id="r1")
        with pytest.raises(ValidationException, match="must not set actions"):
            await service.patch_rule("r1", RuleUpdate(direction=RuleDirection.SESSION))
        mock_rule_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_patch_not_found(self, service, mock_rule_repo):
        mock_rule_repo.get_by_id.return_value = None
        with pytest.raises(ResourceNotFoundException):
            await service.patch_rule("missing", RuleUpdate(name="x"))

    @pytest.mark.asyncio
    async def test_patch_name_conflict(self, service, mock_rule_repo):
        mock_rule_repo.get_by_id.return_value = _make_static_rule(rule_id="r1")
        mock_rule_repo.exists_by_name.return_value = True
        with pytest.raises(ConflictException):
            await service.patch_rule("r1", RuleUpdate(name="dup"))

    @pytest.mark.asyncio
    async def test_patch_condition_validates_cameras(self, service, mock_rule_repo):
        mock_rule_repo.get_by_id.return_value = _make_static_rule(rule_id="r1")
        cond_update = RuleConditionUpdate(perceive_device_ids=["bad-cam"])
        with pytest.raises(ValidationException, match="Invalid perception device IDs"):
            await service.patch_rule("r1", RuleUpdate(condition=cond_update))

    @pytest.mark.asyncio
    async def test_patch_condition_query_only_preserves_devices(
        self, service, mock_rule_repo
    ):
        """`--condition "X"` 不带 `--source` 时，原 perceive_device_ids 必须保留。"""
        existing = _make_static_rule(
            rule_id="r1",
            condition=_make_condition(device_ids=["cam-001"], query="old query"),
        )
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate(condition=RuleConditionUpdate(query="new query"))
        await service.patch_rule("r1", update)

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.condition.query == "new query"
        assert saved.condition.perceive_device_ids == ["cam-001"]

    @pytest.mark.asyncio
    async def test_patch_condition_devices_only_preserves_query(
        self, service, mock_rule_repo
    ):
        """`--source` 不带 `--condition` 时，原 query 文本必须保留。"""
        existing = _make_static_rule(
            rule_id="r1",
            condition=_make_condition(device_ids=["cam-001"], query="keep me"),
        )
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate(
            condition=RuleConditionUpdate(perceive_device_ids=["cam-002"])
        )
        await service.patch_rule("r1", update)

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.condition.query == "keep me"
        assert saved.condition.perceive_device_ids == ["cam-002"]

    @pytest.mark.asyncio
    async def test_patch_clear_terminate_when_with_lifecycle_change(
        self, service, mock_rule_repo
    ):
        """`--lifecycle permanent --clear terminate_when` 必须真正清空 terminate_when，
        不能因为 pydantic 把 JSON null 解成 None 而被 `is not None` 检查吞掉。"""
        existing = _make_state_rule(
            rule_id="r1",
            on_enter_desc="开灯",
            lifecycle=RuleLifecycle.TEMPORARY,
            terminate_when="今晚 23:59 后",
        )
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate(
            lifecycle=RuleLifecycle.PERMANENT,
            terminate_when=None,  # ← 显式置 None；进 model_fields_set
        )
        await service.patch_rule("r1", update)

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.lifecycle == RuleLifecycle.PERMANENT
        assert saved.terminate_when is None

    @pytest.mark.asyncio
    async def test_patch_clear_on_enter_desc(self, service, mock_rule_repo):
        """`--clear on_enter_desc` 配合改 on_enter 为 STATIC：state mode 仍合法。"""
        existing = _make_state_rule(
            rule_id="r1",
            on_enter_desc="原 dynamic desc",
            on_exit_actions=[_make_action(did="exit-d")],
        )
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate(
            on_enter_desc=None,
            on_enter_actions=[_make_action(did="new-enter-d")],
        )
        await service.patch_rule("r1", update)

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.on_enter_desc is None
        assert len(saved.on_enter_actions) == 1
        assert saved.on_enter_actions[0].did == "new-enter-d"

    @pytest.mark.asyncio
    async def test_patch_clear_on_exit_desc(self, service, mock_rule_repo):
        """`--clear on_exit_desc` 留 on_enter_actions 还在：state mode 仍合法。"""
        existing = _make_state_rule(
            rule_id="r1",
            on_enter_actions=[_make_action(did="enter-d")],
            on_exit_desc="原 exit desc",
        )
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate(on_exit_desc=None)
        await service.patch_rule("r1", update)

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.on_exit_desc is None
        # on_enter_actions 没传 → 必须保留
        assert len(saved.on_enter_actions) == 1
        assert saved.on_enter_actions[0].did == "enter-d"

    @pytest.mark.asyncio
    async def test_patch_unset_nullable_field_preserved(
        self, service, mock_rule_repo
    ):
        """update 没传 on_enter_desc，existing.on_enter_desc 必须保留 —— 区分
        '未提供' 和 '显式 null' 的关键 case。"""
        existing = _make_state_rule(
            rule_id="r1",
            on_enter_desc="保留我",
            on_exit_actions=[_make_action(did="exit-d")],
        )
        mock_rule_repo.get_by_id.return_value = existing

        # 只改 enabled，不碰 on_enter_desc
        update = RuleUpdate(enabled=False)
        await service.patch_rule("r1", update)

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.enabled is False
        assert saved.on_enter_desc == "保留我"

    @pytest.mark.asyncio
    async def test_patch_clear_condition_rejected(self, service, mock_rule_repo):
        """显式 condition=None 不允许（Rule.condition 必填）。"""
        mock_rule_repo.get_by_id.return_value = _make_static_rule(rule_id="r1")

        update_explicit_null = RuleUpdate.model_validate({"condition": None})
        with pytest.raises(ValidationException, match="condition cannot be cleared"):
            await service.patch_rule("r1", update_explicit_null)

        # control: 完全不传 condition（不出现 key）不应报错
        update_unset = RuleUpdate.model_validate({"name": _name(TASK_ID, "renamed")})
        await service.patch_rule("r1", update_unset)

    @pytest.mark.asyncio
    async def test_patch_duration_seconds(self, service, mock_rule_repo):
        """CLI rule update --duration-seconds 1800 必须真正写到 existing。"""
        existing = _make_static_rule(rule_id="r1")
        existing.duration_seconds = None
        mock_rule_repo.get_by_id.return_value = existing

        await service.patch_rule("r1", RuleUpdate(duration_seconds=1800))

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.duration_seconds == 1800

    @pytest.mark.asyncio
    async def test_patch_clear_duration_seconds(self, service, mock_rule_repo):
        """--clear duration_seconds → null payload → 清空。"""
        existing = _make_static_rule(rule_id="r1")
        existing.duration_seconds = 1800
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate.model_validate({"duration_seconds": None})
        await service.patch_rule("r1", update)

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.duration_seconds is None

    @pytest.mark.asyncio
    async def test_patch_duration_ratio(self, service, mock_rule_repo):
        existing = _make_static_rule(rule_id="r1")
        existing.duration_seconds = 1800
        existing.duration_ratio = 0.8
        mock_rule_repo.get_by_id.return_value = existing

        await service.patch_rule("r1", RuleUpdate(duration_ratio=0.5))

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.duration_ratio == 0.5

    @pytest.mark.asyncio
    async def test_patch_duration_ratio_none_preserves_existing(
        self, service, mock_rule_repo
    ):
        """duration_ratio 在 Rule 层不允许 None；显式 None 应被忽略保留旧值。"""
        existing = _make_static_rule(rule_id="r1")
        existing.duration_seconds = 1800
        existing.duration_ratio = 0.85
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate.model_validate({"duration_ratio": None})
        await service.patch_rule("r1", update)

        saved = mock_rule_repo.update.call_args[0][0]
        assert saved.duration_ratio == 0.85


class TestRuleServiceDelete:
    @pytest.mark.asyncio
    async def test_delete_success(
        self, service, mock_rule_repo, mock_log_repo, mock_task_repo
    ):
        result = await service.delete_rule("r1")
        assert result is True
        mock_rule_repo.delete.assert_called_once_with("r1")
        mock_log_repo.delete_by_rule_id.assert_called_once_with("r1")
        # v2: delete_link_by_ref 已删除 (task_link 表 DROP), 不再联动

    @pytest.mark.asyncio
    async def test_delete_not_found(self, service, mock_rule_repo):
        mock_rule_repo.exists.return_value = False
        with pytest.raises(ResourceNotFoundException):
            await service.delete_rule("missing")


class TestRuleServiceTrigger:
    @pytest.mark.asyncio
    async def test_trigger_delegates_to_runner(self, service, mock_miot_proxy):
        rule = _make_static_rule(rule_id="r1")
        service._runner.add_rule(rule)
        result = await service.trigger_rule("r1", TRIGGER_CONTEXT)
        assert result is not None
        assert result.action_results  # STATIC dispatched


class TestRuleServiceLogs:
    @pytest.mark.asyncio
    async def test_get_logs(self, service, mock_log_repo):
        mock_log_repo.get_all.return_value = []
        mock_log_repo.count_all.return_value = 0
        logs, total = await service.get_logs(limit=10, after_ts=None)
        assert logs == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_get_logs_by_rule_id(self, service, mock_log_repo):
        mock_log_repo.get_by_rule_id.return_value = []
        mock_log_repo.count_by_rule_id.return_value = 0
        logs, total = await service.get_logs_by_rule_id("r1", limit=5)
        assert total == 0
        mock_log_repo.get_by_rule_id.assert_called_once_with(
            "r1", limit=5, after_ts=None, before_ts=None, kind=None
        )

    @pytest.mark.asyncio
    async def test_cleanup_logs(self, service, mock_log_repo):
        deleted = await service.cleanup_logs(keep_days=7)
        assert deleted == 5
        mock_log_repo.delete_before_days.assert_called_once_with(7)


# ============================================================
# V3 主入口 update_state：差分 + 多源 OR 聚合
# ============================================================


class TestRuleRunnerUpdateState:
    @pytest.mark.asyncio
    async def test_entered_fires_action(self, runner, mock_miot_proxy):
        """false → true 触发 ENTERED，调 set_property 一次。"""
        await runner.update_state("rule-1", "cam-001", True, "进入")
        await runner.drain()
        mock_miot_proxy.set_device_properties.assert_called_once()

    @pytest.mark.asyncio
    async def test_still_in_skipped(self, runner, mock_miot_proxy):
        """连续两次 true 只触发一次（第二次是 STILL_IN）。"""
        await runner.update_state("rule-1", "cam-001", True, "进入")
        await runner.update_state("rule-1", "cam-001", True, "持续")
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_still_out_no_fire(self, runner, mock_miot_proxy):
        """初始 false → false 是 STILL_OUT，不触发。"""
        await runner.update_state("rule-1", "cam-001", False, "")
        await runner.update_state("rule-1", "cam-001", False, "")
        await runner.drain()
        mock_miot_proxy.set_device_properties.assert_not_called()

    @pytest.mark.asyncio
    async def test_event_mode_exited_no_fire(self, runner, mock_miot_proxy):
        """event mode 下 EXITED 不触发动作（连续两帧 False 跨过抗抖窗口）。"""
        await runner.update_state("rule-1", "cam-001", True, "进")
        await runner.update_state("rule-1", "cam-001", False, "出")  # pending
        await runner.update_state("rule-1", "cam-001", False, "出")  # 确认 EXITED
        await runner.drain()
        # 仅 ENTERED 触发，EXITED 在 event mode 下被忽略
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_multi_source_or_aggregation(self, runner, mock_miot_proxy):
        """两个 source 都报 true，rule 级状态从 false→true 后保持，第二次是 STILL_IN。"""
        rule = _make_static_rule(
            rule_id="rule-or",
            condition=_make_condition(device_ids=["cam-001", "cam-002"]),
        )
        runner.add_rule(rule)
        await runner.update_state("rule-or", "cam-001", True, "")
        await runner.update_state("rule-or", "cam-002", True, "")
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_entered_dynamic_callback_carries_trigger_meta(
        self, mock_send, runner
    ):
        """ENTERED 时 trigger_room / trigger_dids 透传进 RuleTriggerCallback。"""
        mock_send.return_value = True
        rule = _make_dynamic_rule(rule_id="rule-meta", name="dyn-meta")
        runner.add_rule(rule)
        await runner.update_state(
            "rule-meta", "perception", True, "进入",
            trigger_room="客厅", trigger_dids=["cam-001"],
        )
        await runner.drain()
        mock_send.assert_called_once()
        # dispatch_event("rule", [callback], builder) — callback is items[0].
        callback = mock_send.call_args[0][1][0]
        assert callback.room_name == "客厅"
        assert callback.source_device_ids == ["cam-001"]

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_exited_dynamic_callback_meta_empty(self, mock_send, runner):
        """EXITED 回调（debounced exit 路径）meta 留空。"""
        mock_send.return_value = True
        rule = _make_state_rule(
            rule_id="rule-meta-exit",
            on_enter_desc="进入提示",
            on_exit_desc="离开提示",
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)
        await runner.update_state(
            "rule-meta-exit", "perception", True, "进",
            trigger_room="客厅", trigger_dids=["cam-001"],
        )
        await runner.update_state("rule-meta-exit", "perception", False, "")  # pending
        await runner.update_state("rule-meta-exit", "perception", False, "")  # 确认 EXIT
        await asyncio.sleep(0.05)
        await runner.drain()
        assert mock_send.call_count == 2
        exit_callback = mock_send.call_args_list[1][0][1][0]
        assert exit_callback.event == RuleEvent.EXITED
        assert exit_callback.room_name == ""
        assert exit_callback.source_device_ids == []

    @pytest.mark.asyncio
    async def test_unknown_rule_silent(self, runner, mock_miot_proxy):
        await runner.update_state("nonexistent", "cam-001", True, "")
        await runner.drain()
        mock_miot_proxy.set_device_properties.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_rule_silent(self, runner, mock_miot_proxy):
        rule = _make_static_rule(rule_id="rule-off", enabled=False)
        runner.add_rule(rule)
        await runner.update_state("rule-off", "cam-001", True, "")
        await runner.drain()
        mock_miot_proxy.set_device_properties.assert_not_called()


# ============================================================
# state mode：on_enter / on_exit 双向独立 + EXITED debounce
# ============================================================


class TestRuleRunnerStateMode:
    @pytest.mark.asyncio
    async def test_on_enter_actions_fired(self, runner, mock_miot_proxy):
        """state mode ENTERED 走 on_enter_actions 槽位。"""
        rule = _make_state_rule(
            rule_id="rule-s",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)
        await runner.update_state("rule-s", "cam-001", True, "进")
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1
        called = mock_miot_proxy.set_device_properties.call_args[0][0][0]
        assert called.did == "enter-d"

    @pytest.mark.asyncio
    async def test_on_exit_fires_after_debounce(self, runner, mock_miot_proxy):
        """EXITED 调度 debounce 任务，到点后执行 on_exit_actions。"""
        rule = _make_state_rule(
            rule_id="rule-deb",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,  # 0 秒 debounce，需要 yield event loop 让 task 跑完
        )
        runner.add_rule(rule)
        await runner.update_state("rule-deb", "cam-001", True, "")
        await runner.update_state("rule-deb", "cam-001", False, "")  # pending
        await runner.update_state("rule-deb", "cam-001", False, "")  # 确认 EXIT → schedule debounce
        # 让 enter fire-and-forget + 0 秒 debounce + exit fire 全部跑完
        await asyncio.sleep(0.05)
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 2
        second = mock_miot_proxy.set_device_properties.call_args_list[1][0][0][0]
        assert second.did == "exit-d"

    @pytest.mark.asyncio
    async def test_re_entry_cancels_pending_exit(self, runner, mock_miot_proxy):
        """exit_debounce 未完成就被 ENTER 打断 → state 从未真正离开，不重复 fire on_enter。

        exit_debounce_seconds 的语义是「连续 N 秒未见才算真退出」。debounce 期间
        被 ENTER cancel 等于这次"退出"被吸收，state 仍处于 ENTERED——不应再触发
        一次 on_enter。否则线上 omni 偶发漏识会反复推送进入通知。
        """
        rule = _make_state_rule(
            rule_id="rule-cx",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=10,  # 长 debounce 给我们时间打断
        )
        runner.add_rule(rule)
        await runner.update_state("rule-cx", "cam-001", True, "")  # ENTERED → fire on_enter
        await runner.update_state("rule-cx", "cam-001", False, "")  # pending
        await runner.update_state("rule-cx", "cam-001", False, "")  # 确认 EXIT → schedule debounce
        await runner.update_state("rule-cx", "cam-001", True, "")  # 1st true 进观察窗，不 cancel
        await runner.update_state("rule-cx", "cam-001", True, "")  # 2nd true 确认 → cancel debounce
        await asyncio.sleep(0.05)
        await runner.drain()
        # 只有最开始那次 ENTERED 真 fire；exit 被吸收不 fire；ENTER 不重复 fire
        assert mock_miot_proxy.set_device_properties.call_count == 1
        assert mock_miot_proxy.set_device_properties.call_args_list[0][0][0][0].did == "enter-d"
        # debounce 已被 cancel，无残留
        assert "rule-cx" not in runner._pending_exit

    @pytest.mark.asyncio
    async def test_single_frame_true_in_debounce_absorbed(
        self, runner, mock_miot_proxy
    ):
        """exit_debounce 期间单帧 True 不 cancel；后续 False 吸收幻觉，debounce 正常完成。"""
        rule = _make_state_rule(
            rule_id="rule-hallu",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,  # 0 秒让 debounce 立刻到点
        )
        runner.add_rule(rule)
        await runner.update_state("rule-hallu", "cam-001", True, "")  # ENTERED
        await runner.update_state("rule-hallu", "cam-001", False, "")  # exit pending
        await runner.update_state("rule-hallu", "cam-001", False, "")  # 确认 EXIT → schedule debounce
        await runner.update_state("rule-hallu", "cam-001", True, "")  # 单帧幻觉 → enter pending
        await runner.update_state("rule-hallu", "cam-001", False, "")  # 第二帧 False 吸收幻觉
        await asyncio.sleep(0.05)
        await runner.drain()
        # enter fire 一次 + exit fire 一次（幻觉没打断 debounce）
        assert mock_miot_proxy.set_device_properties.call_count == 2
        dids = [
            call[0][0][0].did
            for call in mock_miot_proxy.set_device_properties.call_args_list
        ]
        assert dids == ["enter-d", "exit-d"]
        # 残留清理
        assert ("rule-hallu", "cam-001") not in runner._pending_source_enter

    @pytest.mark.asyncio
    async def test_initial_enter_unaffected_by_debounce_streak(
        self, runner, mock_miot_proxy
    ):
        """rule 不在 exit_debounce 阶段时，首帧 True 仍立即 ENTER（响应不变）。"""
        rule = _make_state_rule(
            rule_id="rule-init",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            exit_debounce_seconds=10,
        )
        runner.add_rule(rule)
        # 冷启动 / 长期 inactive → 单帧 True 应立即 fire on_enter
        await runner.update_state("rule-init", "cam-001", True, "")
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1
        # 双帧抗抖只在 _pending_exit 中启用，这里不该有残留
        assert ("rule-init", "cam-001") not in runner._pending_source_enter

    @pytest.mark.asyncio
    async def test_debounced_exit_completion_clears_pending_enter(
        self, runner, mock_miot_proxy
    ):
        """debounce 真完成（fire on_exit）后，pending_source_enter 残留必须清空。

        否则下一轮 debounce 开始时，旧观察窗会把首帧 True 误判为"第二帧"导致
        单帧幻觉直接 cancel。
        """
        rule = _make_state_rule(
            rule_id="rule-clr",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)
        await runner.update_state("rule-clr", "cam-001", True, "")  # ENTERED
        await runner.update_state("rule-clr", "cam-001", False, "")  # exit pending
        await runner.update_state("rule-clr", "cam-001", False, "")  # 确认 EXIT → schedule debounce
        await runner.update_state("rule-clr", "cam-001", True, "")  # 1st true 进观察窗
        # 不发第二帧，让 debounce 自然完成
        await asyncio.sleep(0.05)
        await runner.drain()
        # 残留观察窗必须清空（否则下次 debounce 会错把首帧当第二帧）
        assert ("rule-clr", "cam-001") not in runner._pending_source_enter
        assert "rule-clr" not in runner._pending_exit

    @pytest.mark.asyncio
    async def test_pending_enter_per_source_isolation(
        self, runner, mock_miot_proxy
    ):
        """exit_debounce 阶段 cam-A 单帧 True 不与 cam-B 单帧 True 互相增强。

        若误把 _pending_source_enter 改成只按 rule_id 的 dict，cam-A 首帧
        True 写入后 cam-B 首帧 True 会被错认为"第二帧" → cancel debounce。
        此测试 lock 住 per-source key 的隔离语义。
        """
        rule = _make_state_rule(
            rule_id="rule-multi-h",
            condition=_make_condition(device_ids=["cam-A", "cam-B"]),
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)
        await runner.update_state("rule-multi-h", "cam-A", True, "")   # ENTERED
        await runner.update_state("rule-multi-h", "cam-A", False, "")  # exit pending
        await runner.update_state("rule-multi-h", "cam-A", False, "")  # schedule debounce
        await runner.update_state("rule-multi-h", "cam-A", True, "")   # A 1st true → pending
        await runner.update_state("rule-multi-h", "cam-B", True, "")   # B 1st true → pending
        await runner.update_state("rule-multi-h", "cam-A", False, "")  # A 吸收
        await runner.update_state("rule-multi-h", "cam-B", False, "")  # B 吸收
        await asyncio.sleep(0.05)
        await runner.drain()
        # 没有任何 source 连续两帧 True → debounce 正常完成 → fire exit
        dids = [
            call[0][0][0].did
            for call in mock_miot_proxy.set_device_properties.call_args_list
        ]
        assert dids == ["enter-d", "exit-d"]
        assert ("rule-multi-h", "cam-A") not in runner._pending_source_enter
        assert ("rule-multi-h", "cam-B") not in runner._pending_source_enter

    @pytest.mark.asyncio
    async def test_state_mode_dynamic_on_enter(self, runner):
        """state mode + on_enter_desc：触发 DYNAMIC 回调。"""
        rule = _make_state_rule(
            rule_id="rule-sd",
            on_enter_desc="开灯并播报",
        )
        runner.add_rule(rule)
        with patch(
            "miloco.rule.runner.dispatch_event", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = True
            await runner.update_state("rule-sd", "cam-001", True, "")
            await runner.drain()
            mock_send.assert_called_once()


# ============================================================
# 帧级抗抖：True → 单帧 False 视为 LLM 漏识，需连续 2 帧 False 才算 EXIT
# ============================================================


class TestRuleRunnerFlickerSuppression:
    @pytest.mark.asyncio
    async def test_event_mode_single_frame_false_absorbed(
        self, runner, mock_miot_proxy
    ):
        """event mode：True → False → True 视为抖动，ENTERED 只触发一次。"""
        await runner.update_state("rule-1", "cam-001", True, "")
        await runner.update_state("rule-1", "cam-001", False, "")  # pending
        await runner.update_state("rule-1", "cam-001", True, "")  # 抖动被吸收
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_event_mode_two_false_then_true_fires_again(
        self, runner, mock_miot_proxy
    ):
        """event mode：True → False → False → True，第 4 帧应触发新 ENTERED。"""
        await runner.update_state("rule-1", "cam-001", True, "")  # ENTERED 1
        await runner.update_state("rule-1", "cam-001", False, "")  # pending
        await runner.update_state("rule-1", "cam-001", False, "")  # 确认 EXIT
        await runner.update_state("rule-1", "cam-001", True, "")  # ENTERED 2
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 2

    @pytest.mark.asyncio
    async def test_state_mode_flicker_does_not_schedule_exit(
        self, runner, mock_miot_proxy
    ):
        """state mode：True → False → True 抖动不调度 exit_debounce，on_exit 永不 fire。"""
        rule = _make_state_rule(
            rule_id="rule-flicker",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)
        await runner.update_state("rule-flicker", "cam-001", True, "")
        await runner.update_state("rule-flicker", "cam-001", False, "")  # pending
        await runner.update_state("rule-flicker", "cam-001", True, "")  # 抖动吸收
        await asyncio.sleep(0.05)
        await runner.drain()
        # 仅 enter fire，exit 没被调度
        assert mock_miot_proxy.set_device_properties.call_count == 1
        called = mock_miot_proxy.set_device_properties.call_args[0][0][0]
        assert called.did == "enter-d"

    @pytest.mark.asyncio
    async def test_multi_source_pending_isolated(self, runner, mock_miot_proxy):
        """多 source：A 在 pending 时 B 独立上报 True 不影响 A 的抗抖窗口。"""
        rule = _make_static_rule(
            rule_id="rule-multi",
            condition=_make_condition(device_ids=["cam-A", "cam-B"]),
        )
        runner.add_rule(rule)
        await runner.update_state("rule-multi", "cam-A", True, "")  # ENTERED
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1
        await runner.update_state("rule-multi", "cam-A", False, "")  # A pending
        await runner.update_state("rule-multi", "cam-B", True, "")  # B 独立 True
        await runner.drain()
        # OR 聚合一直 True，没有新 ENTERED
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_remove_rule_clears_pending(self, runner, mock_miot_proxy):
        """remove_rule 应清理 pending，避免重新加 rule 后复用旧观察窗口。"""
        await runner.update_state("rule-1", "cam-001", True, "")
        await runner.update_state("rule-1", "cam-001", False, "")  # pending
        await runner.drain()  # 让首次 ENTERED 的 fire-and-forget 跑完，避免污染 reset_mock
        runner.remove_rule("rule-1")

        # 重新加入同 id 的 rule
        rule = _make_static_rule(rule_id="rule-1")
        runner.add_rule(rule)
        mock_miot_proxy.set_device_properties.reset_mock()
        await runner.update_state("rule-1", "cam-001", True, "")
        await runner.drain()
        # 若 pending 没清，加入新 rule 后第一次 True 不会被识别为新 ENTERED
        assert mock_miot_proxy.set_device_properties.call_count == 1


# ============================================================
# DYNAMIC 回调 payload：terminate_when 透传（temporary lifecycle）
# ============================================================


class TestRuleRunnerDynamicCallback:
    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_temporary_appends_terminate_when(self, mock_send, runner):
        """temporary lifecycle 的 DYNAMIC 触发，prompt_text 应附加 terminate_when 行。"""
        mock_send.return_value = True
        rule = _make_dynamic_rule(rule_id="rule-tmp")
        rule.lifecycle = RuleLifecycle.TEMPORARY
        rule.terminate_when = "用户回家后"
        runner.add_rule(rule)

        await runner.trigger_rule("rule-tmp", "测试")

        mock_send.assert_called_once()
        # dispatch_event("rule", [callback], builder) — callback is items[0].
        callback = mock_send.call_args[0][1][0]
        assert _extra_info(callback.prompt_text).get("terminate_when") == "用户回家后"

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_permanent_does_not_append_terminate_when(
        self, mock_send, runner
    ):
        """permanent lifecycle 不附加 terminate_when（即使设置了也忽略）。"""
        mock_send.return_value = True
        rule = _make_dynamic_rule(rule_id="rule-perm")
        # permanent 默认，terminate_when 设了也无效
        rule.terminate_when = "应该被忽略"
        runner.add_rule(rule)

        await runner.trigger_rule("rule-perm", "测试")

        callback = mock_send.call_args[0][1][0]
        sent_json = callback.model_dump_json()
        assert "terminate_when" not in sent_json


# ============================================================
# Service 层 V3 校验矩阵：state mode、temporary、命名前缀
# ============================================================


class TestRuleServiceV3Validation:
    @pytest.mark.asyncio
    async def test_create_state_rule_success(self, service, mock_rule_repo):
        rule = _make_state_rule(
            rule_id="",
            on_enter_actions=[_make_action()],
            on_exit_desc="关灯",
        )
        rid = await service.create_rule(rule)
        assert rid == "new-rule-id"

    @pytest.mark.asyncio
    async def test_create_state_without_any_direction_raises(self, service):
        rule = _make_state_rule(rule_id="")  # 两个方向都空
        with pytest.raises(
            ValidationException,
            match=r"state mode requires at least one of on_enter / on_exit",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_state_both_actions_and_desc_on_enter_raises(
        self, service
    ):
        rule = _make_state_rule(
            rule_id="",
            on_enter_actions=[_make_action()],
            on_enter_desc="同时设了 desc",
        )
        with pytest.raises(
            ValidationException,
            match=r"state on_enter cannot have both",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_state_with_event_mode_actions_raises(self, service):
        rule = _make_state_rule(
            rule_id="",
            on_enter_actions=[_make_action()],
        )
        rule.actions = [_make_action()]  # state mode 不该塞 event mode 字段
        with pytest.raises(
            ValidationException,
            match=r"state mode must not set actions / action_descriptions",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_temporary_without_terminate_when_raises(self, service):
        rule = _make_static_rule(rule_id="")
        rule.lifecycle = RuleLifecycle.TEMPORARY
        # terminate_when 缺
        with pytest.raises(
            ValidationException,
            match=r"lifecycle=temporary requires terminate_when",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_action_idempotent_false_without_cooldown_raises(
        self, service
    ):
        """idempotent=False 必须配 cooldown_minutes，否则 runner 冷却分支被 None
        短路掉，每次 ENTERED 都会重发 → 通知风暴。service 层必须拦下。"""
        bad = _make_action(
            did="speaker", iid="action.5.3", idempotent=False, cooldown=None
        )
        # action 模式下 _make_action 默认 value=True，但 action.* 应该用 params；
        # 校验逻辑只看 idempotent / cooldown，不查 value/params。
        rule = _make_static_rule(rule_id="", actions=[bad])
        with pytest.raises(
            ValidationException,
            match=r"actions\[0\].*idempotent=false requires cooldown_minutes",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_action_idempotent_false_with_cooldown_passes(
        self, service
    ):
        """配齐 cooldown_minutes 应当通过。"""
        good = _make_action(
            did="speaker", iid="action.5.3", idempotent=False, cooldown=10
        )
        rule = _make_static_rule(rule_id="", actions=[good])
        rid = await service.create_rule(rule)
        assert rid == "new-rule-id"

    @pytest.mark.asyncio
    async def test_create_state_on_enter_action_idempotent_false_without_cooldown_raises(
        self, service
    ):
        """on_enter_actions 槽位同样要校验。"""
        bad = _make_action(
            did="speaker", iid="action.5.3", idempotent=False, cooldown=None
        )
        rule = _make_state_rule(rule_id="", on_enter_actions=[bad])
        with pytest.raises(
            ValidationException,
            match=r"on_enter_actions\[0\].*idempotent=false requires cooldown_minutes",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_create_state_on_exit_action_idempotent_false_without_cooldown_raises(
        self, service
    ):
        """on_exit_actions 槽位同样要校验。"""
        bad = _make_action(
            did="speaker", iid="action.5.3", idempotent=False, cooldown=None
        )
        rule = _make_state_rule(rule_id="", on_exit_actions=[bad])
        with pytest.raises(
            ValidationException,
            match=r"on_exit_actions\[0\].*idempotent=false requires cooldown_minutes",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_patch_adding_idempotent_false_without_cooldown_raises(
        self, service, mock_rule_repo
    ):
        """patch 也要走完整矩阵：把 actions 改成无冷却的非幂等动作必须拦下。"""
        existing = _make_static_rule(rule_id="r1")
        mock_rule_repo.get_by_id.return_value = existing

        bad = _make_action(
            did="speaker", iid="action.5.3", idempotent=False, cooldown=None
        )
        update = RuleUpdate(actions=[bad])
        with pytest.raises(
            ValidationException,
            match=r"actions\[0\].*idempotent=false requires cooldown_minutes",
        ):
            await service.patch_rule("r1", update)

    @pytest.mark.asyncio
    async def test_create_query_with_forbidden_prefix_raises(self, service):
        """`检测到 X` 这种断言性措辞会让感知模型把 query 当成已发生事实，
        service 层必须拦下，不能进 repo。"""
        rule = _make_static_rule(
            rule_id="",
            condition=_make_condition(query="检测到有人摔倒"),
        )
        with pytest.raises(
            ValidationException,
            match=r"condition\.query 不能以断言性词",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_patch_condition_query_with_forbidden_prefix_raises(
        self, service, mock_rule_repo
    ):
        """patch 路径合并后再校验：只改 query 为禁止前缀也要拦下，
        不能因为只动了 query 一个字段就跳过 phrasing 校验。"""
        existing = _make_static_rule(rule_id="r1")
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate(condition=RuleConditionUpdate(query="识别到有人摔倒"))
        with pytest.raises(
            ValidationException,
            match=r"condition\.query 不能以断言性词",
        ):
            await service.patch_rule("r1", update)

    @pytest.mark.asyncio
    async def test_update_with_compliant_query_succeeds(self, service, mock_rule_repo):
        """合规 query（进行时状态/可观测动作描述）不被 phrasing 校验误拦。"""
        rule = _make_static_rule(
            rule_id="r1",
            condition=_make_condition(query="用户正在做出喝水动作"),
        )
        mock_rule_repo.get_by_id.return_value = rule
        assert await service.update_rule(rule) is True


# ============================================================
# 边界场景：并发 / 高频翻转 / 多源 EXIT / cleanup / 部分失败 / 混合校验
# ============================================================


class TestRuleRunnerConcurrencyAndEdgeCases:
    @pytest.mark.asyncio
    async def test_concurrent_update_state_same_rule_fires_once(
        self, runner, mock_miot_proxy
    ):
        """两个 source 通过 gather 并发都报 true，OR 聚合后只触发一次 ENTERED。

        asyncio 单线程协程：update_state 同步段（读写 dict + 计算）不会被打断，
        先到的把 _last_rule_state 翻成 true 后，后到的看到 STILL_IN 静默跳过。
        """
        rule = _make_static_rule(
            rule_id="rule-conc",
            condition=_make_condition(device_ids=["cam-001", "cam-002"]),
        )
        runner.add_rule(rule)

        await asyncio.gather(
            runner.update_state("rule-conc", "cam-001", True, ""),
            runner.update_state("rule-conc", "cam-002", True, ""),
        )
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_rapid_toggle_state_machine_consistent(
        self, runner, mock_miot_proxy
    ):
        """state mode 真状态机切换：每次 False 重复一帧跨过抗抖窗，
        T/F·F/T/F·F/T 应产生 ENTER, EXIT, ENTER, EXIT, ENTER 共 5 次。
        """
        rule = _make_state_rule(
            rule_id="rule-toggle",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)

        for v in [True, False, False, True, False, False, True]:
            await runner.update_state("rule-toggle", "cam-001", v, "")
            await asyncio.sleep(0.02)  # 让 debounce + enter fire task 跑完
        await runner.drain()

        assert mock_miot_proxy.set_device_properties.call_count == 5
        dids = [
            c[0][0][0].did
            for c in mock_miot_proxy.set_device_properties.call_args_list
        ]
        assert dids == ["enter-d", "exit-d", "enter-d", "exit-d", "enter-d"]
        assert runner._last_rule_state["rule-toggle"] is True

    @pytest.mark.asyncio
    async def test_multi_source_only_last_false_triggers_exit(
        self, runner, mock_miot_proxy
    ):
        """多源 OR：A,B 都 true → 只有最后一个 source 翻 false 才触发 EXIT。"""
        rule = _make_state_rule(
            rule_id="rule-mexit",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
            condition=_make_condition(device_ids=["cam-001", "cam-002"]),
        )
        runner.add_rule(rule)

        await runner.update_state("rule-mexit", "cam-001", True, "")  # ENTER
        await runner.update_state("rule-mexit", "cam-002", True, "")  # STILL_IN
        # B 仍 true，A 翻 false 不影响 rule 级状态（两帧 false 跨过抗抖窗）
        await runner.update_state("rule-mexit", "cam-001", False, "")
        await runner.update_state("rule-mexit", "cam-001", False, "")
        await runner.drain()
        assert "rule-mexit" not in runner._pending_exit
        assert mock_miot_proxy.set_device_properties.call_count == 1

        # 最后一个 true source 翻 false → 真 EXIT（同样两帧 false）
        await runner.update_state("rule-mexit", "cam-002", False, "")
        await runner.update_state("rule-mexit", "cam-002", False, "")
        await asyncio.sleep(0.05)
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 2
        last_did = mock_miot_proxy.set_device_properties.call_args_list[-1][0][0][0].did
        assert last_did == "exit-d"

    @pytest.mark.asyncio
    async def test_remove_rule_cancels_pending_exit(self, runner, mock_miot_proxy):
        """长 debounce 期间 remove_rule，pending task 被 cancel，exit 永不触发。"""
        rule = _make_state_rule(
            rule_id="rule-rm",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=10,
        )
        runner.add_rule(rule)
        await runner.update_state("rule-rm", "cam-001", True, "")
        await runner.drain()  # 等 enter fire 落定
        await runner.update_state("rule-rm", "cam-001", False, "")  # pending
        await runner.update_state("rule-rm", "cam-001", False, "")  # 确认 EXIT
        assert "rule-rm" in runner._pending_exit
        pending = runner._pending_exit["rule-rm"]

        runner.remove_rule("rule-rm")
        await asyncio.sleep(0.05)

        assert "rule-rm" not in runner._pending_exit
        assert pending.cancelled()
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_action_middle_failure_others_continue(
        self, runner, mock_miot_proxy
    ):
        """三个 action，第二个抛异常，前后两个仍执行，中间标记 false。"""
        actions = [
            _make_action(did="d1", iid="prop.2.1", value=True, idempotent=False),
            _make_action(did="d2", iid="prop.3.1", value=True, idempotent=False),
            _make_action(did="d3", iid="prop.4.1", value=True, idempotent=False),
        ]
        rule = _make_static_rule(rule_id="rule-mid", actions=actions)
        runner.add_rule(rule)

        async def flaky_set(params):
            if params[0].did == "d2":
                raise Exception("transient network error")
            return [{"code": 0}]

        mock_miot_proxy.set_device_properties = AsyncMock(side_effect=flaky_set)

        result = await runner.trigger_rule("rule-mid", "测试")
        assert len(result.action_results) == 3
        assert result.action_results[0].result is True
        assert result.action_results[1].result is False
        assert "transient network error" in (result.action_results[1].error or "")
        assert result.action_results[2].result is True
        assert mock_miot_proxy.set_device_properties.call_count == 3

    @pytest.mark.asyncio
    async def test_set_property_returns_nonzero_code(self, runner, mock_miot_proxy):
        """set_device_properties 返回负码 → action 标记 false 并附失败释义。

        与 control_device 同口径(summarize_results / #394):负码即失败,error 是失败码
        的中文释义(而非旧的裸 dict repr),台账 result_msg 也据此非 NULL。
        """
        mock_miot_proxy.set_device_properties = AsyncMock(
            return_value=[{"code": -1, "value": None}]
        )
        action = _make_action(idempotent=False)
        rule = _make_static_rule(rule_id="rule-bad-code", actions=[action])
        runner.add_rule(rule)

        result = await runner.trigger_rule("rule-bad-code", "测试")
        assert result.action_results[0].result is False
        # 失败附带释义(summarize_results 的 msg),非 None/空
        assert result.action_results[0].error

    @pytest.mark.asyncio
    async def test_set_property_empty_result_is_failure(
        self, runner, mock_miot_proxy
    ):
        """MIoT 空返回 = 失败(规则执行语义):未确认的执行不能算成功,
        否则会误写 cooldown 压掉后续真实动作(review: Zirconi)。"""
        mock_miot_proxy.set_device_properties = AsyncMock(return_value=[])
        action = _make_action(idempotent=False, cooldown=5)
        rule = _make_static_rule(rule_id="rule-empty-res", actions=[action])
        runner.add_rule(rule)

        result = await runner.trigger_rule("rule-empty-res", "测试")
        assert result.action_results[0].result is False
        assert "无法确认" in (result.action_results[0].error or "")
        # 失败不得写 cooldown——否则空返回后真实动作被压掉
        assert not runner._ensure_state("rule-empty-res").action_cooldown

    @pytest.mark.asyncio
    async def test_call_action_none_result_is_failure(self, runner, mock_miot_proxy):
        """call_device_action 返回 None(不可判定)→ 失败,不按成功处理。"""
        mock_miot_proxy.call_device_action = AsyncMock(return_value=None)
        action = _make_action(iid="action.5.1", value=None, idempotent=False)
        action.params = []
        rule = _make_static_rule(rule_id="rule-none-res", actions=[action])
        runner.add_rule(rule)

        result = await runner.trigger_rule("rule-none-res", "测试")
        assert result.action_results[0].result is False
        assert "无法确认" in (result.action_results[0].error or "")

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_state_temporary_dynamic_full_combo(
        self, mock_send, service, mock_rule_repo
    ):
        """state + temporary + on_enter_desc + terminate_when 完整链路：
        service 层 V3 校验通过 → repo.create → runner.add_rule →
        update_state ENTER → DYNAMIC callback 包含 terminate_when。
        """
        mock_send.return_value = True
        mock_rule_repo.create.return_value = "rule-stc"

        rule = _make_state_rule(
            rule_id="",
            on_enter_desc="开灯",
            on_exit_desc="关灯",
            lifecycle=RuleLifecycle.TEMPORARY,
            terminate_when="主人回家后",
        )
        rid = await service.create_rule(rule)
        assert rid == "rule-stc"

        # service.create_rule 已经把 rule 塞进 runner，直接触发
        await service._runner.update_state("rule-stc", "cam-001", True, "")
        await service._runner.drain()

        mock_send.assert_called_once()
        callback = mock_send.call_args[0][1][0]
        assert _extra_info(callback.prompt_text).get("terminate_when") == "主人回家后"
        assert "开灯" in callback.prompt_text

    @pytest.mark.asyncio
    async def test_patch_mode_to_state_without_slots_raises(
        self, service, mock_rule_repo
    ):
        """把 event mode 规则 patch 成 state mode 但不补 on_enter/exit → 校验失败。

        证明 patch 走的是合并后再校验，不是字段级独立校验。
        """
        existing = _make_static_rule(rule_id="r1")  # event + STATIC + actions
        mock_rule_repo.get_by_id.return_value = existing

        update = RuleUpdate(mode=RuleMode.STATE, actions=[])
        with pytest.raises(
            ValidationException,
            match=r"state mode requires at least one of on_enter / on_exit",
        ):
            await service.patch_rule("r1", update)

    @pytest.mark.asyncio
    async def test_add_rule_resets_state_when_mode_changes(
        self, runner, mock_miot_proxy
    ):
        """Replacing an event rule with a state rule of same id should drop
        the stale _last_rule_state, _last_source_state and pending_exit so the
        next ENTER on the new shape isn't shadowed by old runtime state."""
        # Prime event-mode rule into ENTERED state
        await runner.update_state("rule-1", "cam-001", True, "")
        await runner.drain()  # 等 fire-and-forget 跑完，避免污染后续 reset_mock
        assert runner._last_rule_state.get("rule-1") is True
        assert ("rule-1", "cam-001") in runner._last_source_state

        # Replace with a state-mode rule (same id, different mode)
        replaced = _make_state_rule(
            rule_id="rule-1",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
        )
        runner.add_rule(replaced)

        assert "rule-1" not in runner._last_rule_state
        assert ("rule-1", "cam-001") not in runner._last_source_state

        # Now true → should fire on_enter again (proves state was reset)
        mock_miot_proxy.set_device_properties.reset_mock()
        await runner.update_state("rule-1", "cam-001", True, "")
        await runner.drain()
        mock_miot_proxy.set_device_properties.assert_called_once()
        called = mock_miot_proxy.set_device_properties.call_args[0][0][0]
        assert called.did == "enter-d"

    @pytest.mark.asyncio
    async def test_add_rule_resets_state_when_sources_change(
        self, runner, mock_miot_proxy
    ):
        """Changing condition.perceive_device_ids must drop stale per-source
        state; otherwise an old source's True can keep the OR-aggregate stuck
        true after it's been removed from the rule."""
        rule = _make_state_rule(
            rule_id="rule-src",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
            condition=_make_condition(device_ids=["cam-001", "cam-002"]),
        )
        runner.add_rule(rule)
        await runner.update_state("rule-src", "cam-002", True, "")
        assert runner._last_rule_state["rule-src"] is True

        # Replace with a rule that no longer watches cam-002
        replaced = _make_state_rule(
            rule_id="rule-src",
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
            condition=_make_condition(device_ids=["cam-001"]),
        )
        runner.add_rule(replaced)
        assert ("rule-src", "cam-002") not in runner._last_source_state
        assert "rule-src" not in runner._last_rule_state


# ============================================================
# EVENT mode duration_seconds + duration_ratio：定频采样 + 窗口比例
# ============================================================


def _make_event_duration_rule(
    rule_id="rule-dur",
    duration_seconds=None,
    duration_ratio=0.8,
    did="device-dur",
    iid="prop.2.1",
):
    """EVENT + STATIC rule with optional duration_seconds/ratio."""
    return Rule(
        id=rule_id,
        name=_name(TASK_ID, "duration"),
        task_id=TASK_ID,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=_make_condition(),
        actions=[_make_action(did=did, iid=iid)],
        duration_seconds=duration_seconds,
        duration_ratio=duration_ratio,
    )


def _make_event_duration_dynamic_rule(
    rule_id="rule-dur-dyn",
    duration_seconds=12,
    duration_ratio=0.75,
    descriptions=None,
):
    """EVENT + DYNAMIC rule with duration; used to assert actual_duration metadata."""
    return Rule(
        id=rule_id,
        name=_name(TASK_ID, "duration-dyn"),
        task_id=TASK_ID,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=_make_condition(),
        actions=[],
        action_descriptions=descriptions or ["播报久坐提醒"],
        duration_seconds=duration_seconds,
        duration_ratio=duration_ratio,
    )


class TestRuleRunnerEventDuration:
    """EVENT mode duration_seconds + duration_ratio: 定频采样 deque + 窗口比例."""

    @pytest.fixture
    def runner_fast(self, mock_miot_proxy, mock_log_repo, mock_task_record_service):
        return RuleRunner(
            rules=[],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            sample_interval_seconds=0.1,
            task_record_service=mock_task_record_service,
        )

    @pytest.mark.asyncio
    async def test_duration_none_fires_immediately(
        self, runner_fast, mock_miot_proxy
    ):
        """duration_seconds=None → 立即 fire（兼容回归）."""
        rule = _make_event_duration_rule(rule_id="rule-none", duration_seconds=None)
        runner_fast.add_rule(rule)
        await runner_fast.update_state("rule-none", "cam-001", True, "")
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_duration_full_window_fires(
        self, runner_fast, mock_miot_proxy
    ):
        """duration_seconds=0.2, ratio=1.0 (maxlen=2): 连续两个采样周期 T → fire."""
        rule = _make_event_duration_rule(
            rule_id="rule-full", duration_seconds=1, duration_ratio=1.0
        )
        # 1 / 0.1 = 10 → maxlen=10；为方便用 sample_interval=0.1, duration=0.2 实现 maxlen=2
        # 但 schema ge=1 不允许 0.2；用 duration_seconds=1 (maxlen=10) 也不便
        # 改用 sample_interval=0.5、duration_seconds=1 → maxlen=2
        runner_fast._sample_interval = 0.5
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            mt.return_value = 100.0  # round_id = 200
            await runner_fast.update_state("rule-full", "cam-001", True, "")
            mt.return_value = 100.5  # round_id = 201
            await runner_fast.update_state("rule-full", "cam-001", True, "")
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_duration_partial_window_no_fire(
        self, runner_fast, mock_miot_proxy
    ):
        """duration_seconds=1, ratio=1.0 (maxlen=2, threshold=2): 仅 1 个 T → 不 fire."""
        rule = _make_event_duration_rule(
            rule_id="rule-part", duration_seconds=1, duration_ratio=1.0
        )
        runner_fast._sample_interval = 0.5  # maxlen = 1/0.5 = 2
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            mt.return_value = 100.0
            await runner_fast.update_state("rule-part", "cam-001", True, "")
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 0
        assert sum(runner_fast._duration_window["rule-part"]) == 1

    @pytest.mark.asyncio
    async def test_duration_ratio_intermittent_fires(
        self, runner_fast, mock_miot_proxy
    ):
        """duration_seconds=2, ratio=0.75, sample_interval=0.5 (maxlen=4): T/F/T/T sum=3 → fire."""
        rule = _make_event_duration_rule(
            rule_id="rule-ratio", duration_seconds=2, duration_ratio=0.75
        )
        runner_fast._sample_interval = 0.5  # maxlen = 2/0.5 = 4
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            for i, val in enumerate([True, False, True, True]):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state("rule-ratio", "cam-001", val, "")
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_duration_partial_window_below_maxlen_no_fire(
        self, runner_fast, mock_miot_proxy
    ):
        """窗口未填满（len(win) < maxlen）即使 sum/maxlen 达 ratio 也不 fire。

        防止 duration_seconds * duration_ratio 提早触发（如 30min * 0.8 = 24min）。
        """
        rule = _make_event_duration_rule(
            rule_id="rule-partial-fill", duration_seconds=5, duration_ratio=0.8
        )
        runner_fast._sample_interval = 0.5  # maxlen = 5/0.5 = 10
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            # 前 8 个 T：sum/maxlen=0.8 已达 ratio，但 len(win)=8 < maxlen=10 → 不 fire
            for i in range(8):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-partial-fill", "cam-001", True, ""
                )
            await runner_fast.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 0
            # 第 9、10 个 T：窗口填满 sum=10/10=1.0 → fire
            for i in range(8, 10):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-partial-fill", "cam-001", True, ""
                )
            await runner_fast.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_duration_window_slides_oldest_out(
        self, runner_fast, mock_miot_proxy
    ):
        """老 True 滑出后 sum 下降，最终再凑齐 → 仅在第二次满足时 fire."""
        rule = _make_event_duration_rule(
            rule_id="rule-slide", duration_seconds=1, duration_ratio=1.0
        )
        runner_fast._sample_interval = 0.5  # maxlen=2
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            # round 200: T → win=[1]，sum/2=0.5 不达
            mt.return_value = 100.0
            await runner_fast.update_state("rule-slide", "cam-001", True, "")
            # round 201: F → win=[1,0]，sum/2=0.5 不达
            mt.return_value = 100.5
            await runner_fast.update_state("rule-slide", "cam-001", False, "")
            # round 202: F → win=[0,0]，老 1 滑出
            mt.return_value = 101.0
            await runner_fast.update_state("rule-slide", "cam-001", False, "")
            # round 203: T → win=[0,1], sum/2=0.5 不达
            mt.return_value = 101.5
            await runner_fast.update_state("rule-slide", "cam-001", True, "")
            # round 204: T → win=[1,1], sum/2=1.0 → fire
            mt.return_value = 102.0
            await runner_fast.update_state("rule-slide", "cam-001", True, "")
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_duration_fire_clears_window(
        self, runner_fast, mock_miot_proxy
    ):
        """fire 后清空，仅 maxlen-1 个 T 凑不齐再 fire；补满后才再 fire."""
        rule = _make_event_duration_rule(
            rule_id="rule-clear", duration_seconds=2, duration_ratio=1.0
        )
        runner_fast._sample_interval = 0.5  # maxlen=4, threshold=4
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            for i in range(4):  # round 200..203 凑齐 fire
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state("rule-clear", "cam-001", True, "")
            await runner_fast.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 1
            # 清空后再 3 个 T 凑不齐 maxlen=4
            for i in range(4, 7):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state("rule-clear", "cam-001", True, "")
            await runner_fast.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 1
            # 第 4 个 T 凑齐 → 第二次 fire
            mt.return_value = 100.0 + 7 * 0.5
            await runner_fast.update_state("rule-clear", "cam-001", True, "")
            await runner_fast.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 2

    @pytest.mark.asyncio
    async def test_duration_multi_source_same_round_dedupes(
        self, runner_fast, mock_miot_proxy
    ):
        """同 round 多 source 调用只 append 一次，避免 source 数灌满窗口."""
        rule = _make_event_duration_rule(
            rule_id="rule-dedup", duration_seconds=10, duration_ratio=1.0
        )
        rule.condition = _make_condition(device_ids=["cam-001", "cam-002"])
        runner_fast._sample_interval = 1.0  # maxlen=10
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            mt.return_value = 100.0  # round_id = 100
            await runner_fast.update_state("rule-dedup", "cam-001", True, "")
            await runner_fast.update_state("rule-dedup", "cam-002", True, "")
            await runner_fast.update_state("rule-dedup", "cam-001", True, "")  # STILL_IN
        await runner_fast.drain()
        assert len(runner_fast._duration_window["rule-dedup"]) == 1
        assert sum(runner_fast._duration_window["rule-dedup"]) == 1

    @pytest.mark.asyncio
    async def test_duration_disabled_during_window_no_fire(
        self, runner_fast, mock_miot_proxy
    ):
        """积累期间 disable → 后续 update_state 入口被拦截，窗口冻结但不 fire."""
        rule = _make_event_duration_rule(
            rule_id="rule-dis", duration_seconds=1, duration_ratio=1.0
        )
        runner_fast._sample_interval = 0.5  # maxlen=2, threshold=2
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            mt.return_value = 100.0
            await runner_fast.update_state("rule-dis", "cam-001", True, "")
            # 第二帧前 disable
            disabled = _make_event_duration_rule(
                rule_id="rule-dis", duration_seconds=1, duration_ratio=1.0
            )
            disabled.enabled = False
            runner_fast._rules["rule-dis"] = disabled  # 不走 add_rule 避免 reset
            mt.return_value = 100.5
            await runner_fast.update_state("rule-dis", "cam-001", True, "")
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 0

    @pytest.mark.asyncio
    async def test_duration_remove_rule_clears_window(
        self, runner_fast, mock_miot_proxy
    ):
        """remove_rule 清窗口；重新 add_rule 后从 0 累积."""
        rule = _make_event_duration_rule(
            rule_id="rule-rm", duration_seconds=2, duration_ratio=1.0
        )
        runner_fast._sample_interval = 0.5  # maxlen=4
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            for i in range(2):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state("rule-rm", "cam-001", True, "")
            assert len(runner_fast._duration_window["rule-rm"]) == 2
            runner_fast.remove_rule("rule-rm")
            assert "rule-rm" not in runner_fast._duration_window
            assert "rule-rm" not in runner_fast._last_duration_round
            # 重新加 → 干净窗口
            runner_fast.add_rule(rule)
            mt.return_value = 102.0
            await runner_fast.update_state("rule-rm", "cam-001", True, "")
            assert len(runner_fast._duration_window["rule-rm"]) == 1

    @pytest.mark.asyncio
    async def test_duration_config_change_resets_window(
        self, runner_fast, mock_miot_proxy
    ):
        """add_rule 同 id 但 duration_seconds 变 → reset 窗口."""
        rule1 = _make_event_duration_rule(
            rule_id="rule-cfg", duration_seconds=3, duration_ratio=1.0
        )
        runner_fast._sample_interval = 0.5  # maxlen=6
        runner_fast.add_rule(rule1)
        with patch("miloco.rule.runner.time.time") as mt:
            for i in range(3):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state("rule-cfg", "cam-001", True, "")
            assert len(runner_fast._duration_window["rule-cfg"]) == 3
            rule2 = _make_event_duration_rule(
                rule_id="rule-cfg", duration_seconds=1, duration_ratio=1.0
            )
            runner_fast.add_rule(rule2)
            assert "rule-cfg" not in runner_fast._duration_window
            assert "rule-cfg" not in runner_fast._last_duration_round

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_duration_fire_injects_window_metadata_in_prompt(
        self, mock_send, mock_miot_proxy, mock_log_repo, mock_task_record_service
    ):
        """DYNAMIC + duration fire 时 callback.prompt_text 末尾含 actual_started_at + duration_seconds，
        让 fire-agent 知道用户真实起始时刻和窗口大小（不暴露内部累积秒数）。"""
        mock_send.return_value = True
        runner = RuleRunner(
            rules=[],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            sample_interval_seconds=3.0,
            task_record_service=mock_task_record_service,
        )
        rule = _make_event_duration_dynamic_rule(
            rule_id="rule-meta", duration_seconds=12, duration_ratio=0.75
        )
        runner.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            # maxlen=4, sum needs >=3 to reach ratio 0.75
            for i, val in enumerate([True, False, True, True]):
                mt.return_value = 1000.0 + i * 3.0
                await runner.update_state("rule-meta", "cam-001", val, "")
        await runner.drain()
        mock_send.assert_called_once()
        callback = mock_send.call_args[0][1][0]
        info = _extra_info(callback.prompt_text)
        assert info.get("duration_seconds") == 12
        assert "actual_started_at" in info
        # actual_started_at = 滑窗里第一帧 true 的对齐时间，ISO 8601 含时区；
        # exact string depends on machine TZ, just ensure the field is present.
        assert "actual_duration_seconds" not in info

    @pytest.mark.asyncio
    async def test_duration_sample_gap_decays_window(
        self, mock_miot_proxy, mock_log_repo, mock_task_record_service
    ):
        """断流 gap > 0 补 0 衰减；gap >= maxlen 直接 clear."""
        runner = RuleRunner(
            rules=[],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            sample_interval_seconds=3.0,
            task_record_service=mock_task_record_service,
        )
        rule = _make_event_duration_rule(
            rule_id="rule-gap", duration_seconds=21, duration_ratio=1.0
        )
        runner.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            # 5 个连续 T (round 0..4)
            for r in range(5):
                mt.return_value = r * 3.0
                await runner.update_state("rule-gap", "cam-001", True, "")
            assert list(runner._duration_window["rule-gap"]) == [1, 1, 1, 1, 1]

            # gap=3：跳到 round 8。extend [0,0,0] 然后 append(1)
            # win 从 [1,1,1,1,1] → extend 后被 deque 弹出溢出 → [1,1,1,1,0,0,0]（maxlen=7）
            # → append(1) → [1,1,1,0,0,0,1]
            mt.return_value = 8 * 3.0
            await runner.update_state("rule-gap", "cam-001", True, "")
            assert list(runner._duration_window["rule-gap"]) == [1, 1, 1, 0, 0, 0, 1]

            # 后续两个 T (round 9, 10) gap=0
            mt.return_value = 9 * 3.0
            await runner.update_state("rule-gap", "cam-001", True, "")
            mt.return_value = 10 * 3.0
            await runner.update_state("rule-gap", "cam-001", True, "")
            # [1,1,1,0,0,0,1] → append(1)→[1,1,0,0,0,1,1] → append(1)→[1,0,0,0,1,1,1]
            assert list(runner._duration_window["rule-gap"]) == [1, 0, 0, 0, 1, 1, 1]
            await runner.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 0

            # gap >= maxlen：跳到 round 100，gap=89 >> 7，窗口被 clear，append(1)
            mt.return_value = 100 * 3.0
            await runner.update_state("rule-gap", "cam-001", True, "")
            assert list(runner._duration_window["rule-gap"]) == [1]


def _make_state_duration_rule(
    rule_id="rule-sd-dur",
    duration_seconds=1,
    duration_ratio=1.0,
    exit_debounce_seconds=0,
    on_enter_actions=None,
    on_exit_actions=None,
):
    """STATE + duration rule，默认两侧都给 STATIC action 便于按 did 分辨 fire 来源。"""
    return Rule(
        id=rule_id,
        name=_name(TASK_ID, "state-duration"),
        task_id=TASK_ID,
        mode=RuleMode.STATE,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=_make_condition(),
        on_enter_actions=(
            on_enter_actions
            if on_enter_actions is not None
            else [_make_action(did="enter-d", iid="prop.2.1")]
        ),
        on_exit_actions=(
            on_exit_actions
            if on_exit_actions is not None
            else [_make_action(did="exit-d", iid="prop.2.1")]
        ),
        exit_debounce_seconds=exit_debounce_seconds,
        duration_seconds=duration_seconds,
        duration_ratio=duration_ratio,
    )


class TestRuleRunnerStateDuration:
    """STATE mode + duration_seconds：ENTERED 前置确认，EXITED 走原 debounce。

    跟 EVENT mode 的关键区别：
    - 达标 fire on_enter 后，STILL_IN 期间不重复 fire（用 _state_duration_fired 标记拦截）
    - 未达标就 EXITED → 不 fire on_exit（因为没真正进入），清窗口
    - 达标后 EXITED → 走 exit_debounce，完成后清 fired+窗口
    """

    @pytest.fixture
    def runner_fast(self, mock_miot_proxy, mock_log_repo, mock_task_record_service):
        return RuleRunner(
            rules=[],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            sample_interval_seconds=0.5,
            task_record_service=mock_task_record_service,
        )

    @pytest.mark.asyncio
    async def test_state_duration_no_fire_on_enter_before_window_full(
        self, runner_fast, mock_miot_proxy
    ):
        """窗口未达标 → 不 fire on_enter."""
        rule = _make_state_duration_rule(
            rule_id="rule-sd-noent", duration_seconds=1, duration_ratio=1.0
        )
        # maxlen=2, threshold=2
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            mt.return_value = 100.0
            await runner_fast.update_state("rule-sd-noent", "cam-001", True, "")
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 0
        assert sum(runner_fast._duration_window["rule-sd-noent"]) == 1
        assert "rule-sd-noent" not in runner_fast._state_duration_fired

    @pytest.mark.asyncio
    async def test_state_duration_fires_on_enter_when_window_meets_ratio(
        self, runner_fast, mock_miot_proxy
    ):
        """窗口达标 → fire on_enter 一次，标记 fired."""
        rule = _make_state_duration_rule(
            rule_id="rule-sd-fire", duration_seconds=1, duration_ratio=1.0
        )
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            mt.return_value = 100.0
            await runner_fast.update_state("rule-sd-fire", "cam-001", True, "")
            mt.return_value = 100.5
            await runner_fast.update_state("rule-sd-fire", "cam-001", True, "")
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1
        assert (
            mock_miot_proxy.set_device_properties.call_args[0][0][0].did == "enter-d"
        )
        assert "rule-sd-fire" in runner_fast._state_duration_fired

    @pytest.mark.asyncio
    async def test_state_duration_still_in_after_fire_no_duplicate(
        self, runner_fast, mock_miot_proxy
    ):
        """STATE 关键差异：达标 fire 后继续 STILL_IN，不再重复 fire on_enter."""
        rule = _make_state_duration_rule(
            rule_id="rule-sd-still", duration_seconds=1, duration_ratio=1.0
        )
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            # 凑齐 fire
            for i in range(2):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-sd-still", "cam-001", True, ""
                )
            await runner_fast.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 1
            # STILL_IN 5 个采样周期 —— EVENT 会周期 fire，STATE 不应该
            for i in range(2, 7):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-sd-still", "cam-001", True, ""
                )
            await runner_fast.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 1

    @pytest.mark.asyncio
    async def test_state_duration_exit_before_fire_silent(
        self, runner_fast, mock_miot_proxy
    ):
        """未达标就 EXITED → 完全静默：不 fire on_exit、不启动 debounce、不清窗口。

        窗口靠后续 evaluate 持续 append 0 自然演化，符合 ratio 间歇容忍设计。
        """
        rule = _make_state_duration_rule(
            rule_id="rule-sd-silent",
            duration_seconds=2,
            duration_ratio=0.75,
            exit_debounce_seconds=0,
        )
        # sample_interval=0.5, maxlen=4, threshold=3
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            # 累 2 个 T（未达标 2/4 < 0.75）
            for i in range(2):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-sd-silent", "cam-001", True, ""
                )
            assert sum(runner_fast._duration_window["rule-sd-silent"]) == 2
            # 翻 EXITED，状态机层面 _last_rule_state=False
            mt.return_value = 101.0
            await runner_fast.update_state(
                "rule-sd-silent", "cam-001", False, ""
            )
        await asyncio.sleep(0.05)
        await runner_fast.drain()
        # 不 fire on_exit
        assert mock_miot_proxy.set_device_properties.call_count == 0
        # 窗口保留（关键），evaluate 已 append F → win=[1,1,0]
        assert list(runner_fast._duration_window["rule-sd-silent"]) == [1, 1, 0]
        # 没有 pending debounce task
        assert "rule-sd-silent" not in runner_fast._pending_exit
        # 仍未 fired
        assert "rule-sd-silent" not in runner_fast._state_duration_fired

    @pytest.mark.asyncio
    async def test_state_duration_exit_then_re_enter_can_still_fire(
        self, runner_fast, mock_miot_proxy
    ):
        """未达标 EXITED 后再 ENTERED，复用窗口残留 T 继续累积 → 凑齐照样 fire."""
        rule = _make_state_duration_rule(
            rule_id="rule-sd-cont",
            duration_seconds=2,
            duration_ratio=0.75,
            exit_debounce_seconds=0,
        )
        # sample_interval=0.5, maxlen=4, threshold=3（sum>=3 时 3/4=0.75 达标）
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            # 2 个 T（窗口 [1,1]，未达标）
            for i in range(2):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-sd-cont", "cam-001", True, ""
                )
            # 1 个 F（EXITED 翻转，窗口 [1,1,0]）
            mt.return_value = 101.0
            await runner_fast.update_state(
                "rule-sd-cont", "cam-001", False, ""
            )
            # 1 个 T 又回来（ENTERED 翻转，窗口 [1,1,0,1]，sum=3, 3/4=0.75 → fire）
            mt.return_value = 101.5
            await runner_fast.update_state(
                "rule-sd-cont", "cam-001", True, ""
            )
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1
        assert (
            mock_miot_proxy.set_device_properties.call_args[0][0][0].did
            == "enter-d"
        )

    @pytest.mark.asyncio
    async def test_state_duration_exit_after_fire_debounces_to_on_exit(
        self, runner_fast, mock_miot_proxy
    ):
        """达标 fire on_enter 后 EXITED → debounce 完成 fire on_exit + 清状态."""
        rule = _make_state_duration_rule(
            rule_id="rule-sd-exit",
            duration_seconds=1,
            duration_ratio=1.0,
            exit_debounce_seconds=0,
        )
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            for i in range(2):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-sd-exit", "cam-001", True, ""
                )
            # 帧级抗抖要求两帧 F 才确认 EXIT：第一帧进 pending，第二帧推进 dispatch
            mt.return_value = 101.0
            await runner_fast.update_state("rule-sd-exit", "cam-001", False, "")
            mt.return_value = 101.5
            await runner_fast.update_state("rule-sd-exit", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 2
        first_did = (
            mock_miot_proxy.set_device_properties.call_args_list[0][0][0][0].did
        )
        second_did = (
            mock_miot_proxy.set_device_properties.call_args_list[1][0][0][0].did
        )
        assert first_did == "enter-d"
        assert second_did == "exit-d"
        assert "rule-sd-exit" not in runner_fast._state_duration_fired
        assert "rule-sd-exit" not in runner_fast._duration_window

    @pytest.mark.asyncio
    async def test_state_duration_re_entry_within_debounce_no_extra_fire(
        self, runner_fast, mock_miot_proxy
    ):
        """达标 fire on_enter 后 EXITED→debounce 内 ENTERED → cancel；不重复 fire on_enter."""
        rule = _make_state_duration_rule(
            rule_id="rule-sd-rein",
            duration_seconds=1,
            duration_ratio=1.0,
            exit_debounce_seconds=10,  # 长 debounce 给 cancel 机会
        )
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            for i in range(2):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-sd-rein", "cam-001", True, ""
                )
            # 第一次 fire on_enter
            await runner_fast.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 1

            # 翻转 F → debounce
            mt.return_value = 101.0
            await runner_fast.update_state("rule-sd-rein", "cam-001", False, "")
            # 翻转 T → cancel debounce，且 fired=True 已存 → 不该再 fire on_enter
            mt.return_value = 101.5
            await runner_fast.update_state("rule-sd-rein", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1
        assert "rule-sd-rein" in runner_fast._state_duration_fired

    @pytest.mark.asyncio
    async def test_state_duration_complete_cycle_can_re_fire(
        self, runner_fast, mock_miot_proxy
    ):
        """完整 enter→exit 周期后，重新累积可再次 fire on_enter."""
        rule = _make_state_duration_rule(
            rule_id="rule-sd-cycle",
            duration_seconds=1,
            duration_ratio=1.0,
            exit_debounce_seconds=0,
        )
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            # 第一轮 enter→fire→exit→fire
            for i in range(2):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-sd-cycle", "cam-001", True, ""
                )
            # 抗抖要求两帧 F 才确认 EXIT
            for t in [101.0, 101.5]:
                mt.return_value = t
                await runner_fast.update_state(
                    "rule-sd-cycle", "cam-001", False, ""
                )
            await asyncio.sleep(0.05)
            await runner_fast.drain()
            assert mock_miot_proxy.set_device_properties.call_count == 2

            # 第二轮：重新累积 → fire on_enter
            for t in [102.0, 102.5]:
                mt.return_value = t
                await runner_fast.update_state(
                    "rule-sd-cycle", "cam-001", True, ""
                )
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 3
        third_did = (
            mock_miot_proxy.set_device_properties.call_args_list[2][0][0][0].did
        )
        assert third_did == "enter-d"

    @pytest.mark.asyncio
    async def test_state_duration_config_change_resets_state(
        self, runner_fast, mock_miot_proxy
    ):
        """同 id add_rule 改 duration_seconds → window+fired 全清."""
        rule1 = _make_state_duration_rule(
            rule_id="rule-sd-cfg", duration_seconds=2, duration_ratio=1.0
        )
        runner_fast.add_rule(rule1)
        with patch("miloco.rule.runner.time.time") as mt:
            # 凑齐 fire（maxlen=4，threshold=4）
            for i in range(4):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-sd-cfg", "cam-001", True, ""
                )
            await runner_fast.drain()
            assert "rule-sd-cfg" in runner_fast._state_duration_fired

            rule2 = _make_state_duration_rule(
                rule_id="rule-sd-cfg", duration_seconds=1, duration_ratio=1.0
            )
            runner_fast.add_rule(rule2)
            assert "rule-sd-cfg" not in runner_fast._duration_window
            assert "rule-sd-cfg" not in runner_fast._last_duration_round
            assert "rule-sd-cfg" not in runner_fast._state_duration_fired

    @pytest.mark.asyncio
    async def test_state_duration_disable_enable_resets_state(
        self, runner_fast, mock_miot_proxy
    ):
        """add_rule 切换 enabled 也触发 reset：disable→enable 周期清窗口和 fired."""
        rule = _make_state_duration_rule(
            rule_id="rule-sd-en", duration_seconds=1, duration_ratio=1.0
        )
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            # 凑齐 fire（maxlen=2，threshold=2）
            for i in range(2):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state(
                    "rule-sd-en", "cam-001", True, ""
                )
            await runner_fast.drain()
            assert "rule-sd-en" in runner_fast._state_duration_fired

            # disable：add_rule 同 id 但 enabled=False
            disabled = _make_state_duration_rule(
                rule_id="rule-sd-en", duration_seconds=1, duration_ratio=1.0
            )
            disabled.enabled = False
            runner_fast.add_rule(disabled)
            assert "rule-sd-en" not in runner_fast._duration_window
            assert "rule-sd-en" not in runner_fast._state_duration_fired
            assert "rule-sd-en" not in runner_fast._last_rule_state

            # enable 回来：状态机干净
            runner_fast.add_rule(rule)
            assert "rule-sd-en" not in runner_fast._state_duration_fired


class TestActualExitedAt:
    """state EXITED 真实退出时刻通过 extra_metadata 暴露给 agent。

    bug：EXIT 经过 exit_debounce_seconds 延迟才 fire，agent 拿 triggered_at
    或不带 --at 调 session-end 会让 session.end_at = 真实退出 + D + δ，
    duration 时长虚高。修复：runner 在 _dispatch_event 捕获 wall-clock 作为
    actual_exited_at 透传，_execute_dynamic 通过 extra_metadata 拼到 prompt
    末尾，agent 看 preamble 知道要传给 session-end --at。
    """

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_exited_prompt_contains_actual_exited_at(
        self, mock_send, runner
    ):
        """EXIT debounce 完成后，agent prompt 含 actual_exited_at=<ISO> 行。"""
        mock_send.return_value = True
        rule = _make_state_rule(
            rule_id="rule-aex",
            on_enter_desc="进入业务语义\n\ntask_id=t1",
            on_exit_desc="离开业务语义\n\ntask_id=t1",
            exit_debounce_seconds=0,  # 立即到点
        )
        runner.add_rule(rule)

        # ENTER → 1 帧 True
        await runner.update_state("rule-aex", "cam-001", True, "")
        # EXIT 抗抖：2 帧 False
        await runner.update_state("rule-aex", "cam-001", False, "")
        await runner.update_state("rule-aex", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await runner.drain()

        exit_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.EXITED)
        from datetime import datetime
        info = _extra_info(exit_prompt)
        assert "actual_exited_at" in info, (
            f"EXITED prompt 额外信息缺 actual_exited_at；prompt:\n{exit_prompt}"
        )
        datetime.fromisoformat(info["actual_exited_at"].replace("Z", "+00:00"))

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_entered_prompt_omits_actual_exited_at(
        self, mock_send, runner
    ):
        """ENTERED 是入态时刻不需要 actual_exited_at，prompt 不含此行。"""
        mock_send.return_value = True
        rule = _make_state_rule(
            rule_id="rule-aen",
            on_enter_desc="进入业务语义\n\ntask_id=t2",
            on_exit_desc="离开业务语义\n\ntask_id=t2",
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)

        await runner.update_state("rule-aen", "cam-001", True, "")
        await runner.drain()

        enter_prompt = _last_dispatched_prompt(
            mock_send, event=RuleEvent.ENTERED
        )
        assert "actual_exited_at" not in _extra_info(enter_prompt), (
            f"ENTERED prompt 额外信息不该含 actual_exited_at；prompt:\n{enter_prompt}"
        )


def _last_dispatched_prompt(mock_send, event: RuleEvent) -> str:
    """从 dispatch_event mock 的调用历史里拿到指定 event 的最后一次 prompt_text。"""
    for call in reversed(mock_send.call_args_list):
        items = call.args[1] if len(call.args) >= 2 else call.kwargs.get("items", [])
        for it in items:
            if getattr(it, "event", None) == event:
                return it.prompt_text
    raise AssertionError(f"no {event.value} dispatch found in mock_send history")


class TestActualStartedAt:
    """ENTERED 真实起点时刻通过 extra_metadata 暴露给 agent。

    与 actual_exited_at 对称：
    - duration 模式（_evaluate_duration）：滑窗里第一帧 true 的对齐时间
    - 瞬时翻转模式（_dispatch_event ENTERED）：进入分支瞬间的 wall-clock
    """

    @pytest.fixture
    def runner_fast(self, mock_miot_proxy, mock_log_repo, mock_task_record_service):
        return RuleRunner(
            rules=[],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            sample_interval_seconds=0.1,
            task_record_service=mock_task_record_service,
        )

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_duration_ratio_full_actual_started_equals_window_start(
        self, mock_send, runner_fast
    ):
        """duration_ratio=1.0：窗口全 1，actual_started_at == fire_ts - duration_seconds。"""
        mock_send.return_value = True
        rule = _make_event_duration_dynamic_rule(
            rule_id="rule-as-full", duration_seconds=1, duration_ratio=1.0
        )
        runner_fast._sample_interval = 0.5  # maxlen = 2
        runner_fast.add_rule(rule)

        with patch("miloco.rule.runner.time.time") as mt:
            mt.return_value = 100.0  # round_id = 200
            await runner_fast.update_state("rule-as-full", "cam-001", True, "")
            mt.return_value = 100.5  # round_id = 201
            await runner_fast.update_state("rule-as-full", "cam-001", True, "")
        await runner_fast.drain()

        enter_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.ENTERED)
        from datetime import datetime, timezone
        info = _extra_info(enter_prompt)
        assert "actual_started_at" in info, (
            f"ENTERED prompt 额外信息缺 actual_started_at；prompt:\n{enter_prompt}"
        )
        # round_ids in window = [200, 201], maxlen=2
        # first_true_offset=0 → first_true_round=201-2+1+0=200
        # actual_started_at epoch = 200 * 0.5 = 100.0
        # 注：与旧 fire_ts - duration_seconds = 100.5-1 = 99.5 相差 sample_interval(0.5)，
        # 因 deque 第 0 位是窗口里最早保留的那一帧，比窗口物理起点晚 1 个 sample。
        expected_epoch = 100.0
        parsed = datetime.fromisoformat(info["actual_started_at"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        assert abs(parsed - expected_epoch) < 0.01, (
            f"expected actual_started_at epoch≈{expected_epoch}, got {parsed}"
        )

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_duration_ratio_partial_actual_started_picks_first_true(
        self, mock_send, runner_fast
    ):
        """duration_ratio<1.0：窗口 [0,0,1,1,0,1,1,1]，actual_started_at 对应 index=2。"""
        mock_send.return_value = True
        rule = _make_event_duration_dynamic_rule(
            rule_id="rule-as-part",
            duration_seconds=4,
            duration_ratio=0.5,
        )
        runner_fast._sample_interval = 0.5  # maxlen = 8
        runner_fast.add_rule(rule)

        sequence = [False, False, True, True, False, True, True, True]
        with patch("miloco.rule.runner.time.time") as mt:
            for i, val in enumerate(sequence):
                mt.return_value = 100.0 + i * 0.5
                await runner_fast.update_state("rule-as-part", "cam-001", val, "")
        await runner_fast.drain()

        enter_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.ENTERED)
        from datetime import datetime, timezone
        info = _extra_info(enter_prompt)
        assert "actual_started_at" in info, f"prompt 缺 actual_started_at：{enter_prompt}"
        # round_id at i=7 = 207, maxlen=8
        # first_true_offset=2 → first_true_round = 207-8+1+2 = 202
        # actual_started_at epoch = 202 * 0.5 = 101.0
        expected_epoch = 101.0
        parsed = datetime.fromisoformat(info["actual_started_at"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        assert abs(parsed - expected_epoch) < 0.01, (
            f"expected first-true epoch≈{expected_epoch}, got {parsed}"
        )

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_duration_event_second_fire_recomputes_actual_started(
        self, mock_send, runner_fast
    ):
        """EVENT mode fire 后清窗口；第二轮累积的 actual_started_at 独立计算，不延续旧窗口。"""
        mock_send.return_value = True
        rule = _make_event_duration_dynamic_rule(
            rule_id="rule-as-2nd", duration_seconds=1, duration_ratio=1.0
        )
        runner_fast._sample_interval = 0.5  # maxlen = 2
        runner_fast.add_rule(rule)

        with patch("miloco.rule.runner.time.time") as mt:
            mt.return_value = 100.0
            await runner_fast.update_state("rule-as-2nd", "cam-001", True, "")
            mt.return_value = 100.5
            await runner_fast.update_state("rule-as-2nd", "cam-001", True, "")
            await runner_fast.drain()
            mt.return_value = 200.0
            await runner_fast.update_state("rule-as-2nd", "cam-001", True, "")
            mt.return_value = 200.5
            await runner_fast.update_state("rule-as-2nd", "cam-001", True, "")
        await runner_fast.drain()

        from datetime import datetime, timezone
        enter_prompts = []
        for call in mock_send.call_args_list:
            items = call.args[1] if len(call.args) >= 2 else call.kwargs.get("items", [])
            for it in items:
                if getattr(it, "event", None) == RuleEvent.ENTERED:
                    enter_prompts.append(it.prompt_text)
        assert len(enter_prompts) == 2, f"expected 2 ENTERED fires, got {len(enter_prompts)}"

        epochs = []
        for prompt in enter_prompts:
            info = _extra_info(prompt)
            assert "actual_started_at" in info
            epochs.append(
                datetime.fromisoformat(info["actual_started_at"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
            )
        # 第一次窗口 [200,201] → first_true_round=200 → epoch≈100.0
        # 第二次窗口 [400,401] → first_true_round=400 → epoch≈200.0
        assert abs(epochs[0] - 100.0) < 0.01
        assert abs(epochs[1] - 200.0) < 0.01

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_instant_flip_actual_started_is_now(self, mock_send, runner):
        """STATE 模式无 duration：ENTERED prompt 末尾 actual_started_at ≈ now。"""
        from datetime import datetime, timezone
        mock_send.return_value = True
        rule = _make_state_rule(
            rule_id="rule-as-flip",
            on_enter_desc="进入业务\n\ntask_id=t1",
            on_exit_desc="离开业务\n\ntask_id=t1",
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)

        before = datetime.now(timezone.utc)
        await runner.update_state("rule-as-flip", "cam-001", True, "")
        await runner.drain()
        after = datetime.now(timezone.utc)

        enter_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.ENTERED)
        info = _extra_info(enter_prompt)
        assert "actual_started_at" in info, f"prompt 缺 actual_started_at：{enter_prompt}"
        parsed = datetime.fromisoformat(info["actual_started_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        # isoformat(timespec="seconds") 把毫秒截掉，允许 ±1s 容差
        assert (before - parsed).total_seconds() <= 1, (
            f"actual_started_at {parsed} 早于 {before} 超过 1s"
        )
        assert (parsed - after).total_seconds() <= 1, (
            f"actual_started_at {parsed} 晚于 {after} 超过 1s"
        )


class TestPreambleSelection:
    """preamble 选择按 task 是否有 record 决定（backend 实时查 detect_record_kind），
    desc 里写不写 marker 不影响 preamble 路径。

    backend 在 _compose_prompt_text 阶段统一注入 task_id / record_kind 到
    prompt 末尾 metadata，装配 agent 漏写 marker 不影响 runtime。
    """

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_task_with_record_promotes_both_to_with_record(
        self, mock_send, runner
    ):
        """task 有 record → ENTER 和 EXIT 都走 WITH_RECORD preamble，
        desc 里写不写 marker 不影响。"""
        mock_send.return_value = True
        # 让 detect_record_kind 返回 "duration"，模拟 task 有 record
        runner._task_record_service.detect_record_kind = MagicMock(
            return_value="duration"
        )
        rule = _make_state_rule(
            rule_id="rule-pre-rec",
            on_enter_desc="进入业务语义（无 marker）",
            on_exit_desc="离开业务语义（无 marker）",
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)

        await runner.update_state("rule-pre-rec", "cam-001", True, "")
        await runner.update_state("rule-pre-rec", "cam-001", False, "")
        await runner.update_state("rule-pre-rec", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await runner.drain()

        enter_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.ENTERED)
        exit_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.EXITED)
        marker = "前置闸门"
        assert marker in enter_prompt
        assert marker in exit_prompt
        # backend 统一注入 task_id + record_kind 到额外信息 JSON
        enter_info = _extra_info(enter_prompt)
        exit_info = _extra_info(exit_prompt)
        assert enter_info.get("task_id") == TASK_ID
        assert enter_info.get("record_kind") == "duration"
        assert exit_info.get("task_id") == TASK_ID
        assert exit_info.get("record_kind") == "duration"

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_task_without_record_keeps_pure(self, mock_send, runner):
        """task 无 record → 无 preamble，不注入 task_id / record_kind。"""
        mock_send.return_value = True
        # detect_record_kind 默认返 None（mock_task_record_service fixture 默认行为）
        rule = _make_state_rule(
            rule_id="rule-pre-pure",
            on_enter_desc="进入业务语义（无 marker）",
            on_exit_desc="离开业务语义（无 marker）",
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)

        await runner.update_state("rule-pre-pure", "cam-001", True, "")
        await runner.update_state("rule-pre-pure", "cam-001", False, "")
        await runner.update_state("rule-pre-pure", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await runner.drain()

        enter_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.ENTERED)
        exit_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.EXITED)
        marker = "前置闸门"
        assert marker not in enter_prompt
        assert marker not in exit_prompt
        # 无 record → 不注入 task_id / record_kind
        assert "task_id" not in _extra_info(enter_prompt)
        assert "record_kind" not in _extra_info(enter_prompt)


# ============================================================
# on_target_desc 累计达标 timer 路径（spec 2026-06-15）
# ============================================================


def _make_state_rule_with_target(
    rule_id="rule-tgt",
    task_id=TASK_ID,
    on_enter_desc="开始计时\ntask_id=tgt",
    on_exit_desc="结束计时\ntask_id=tgt",
    on_target_desc="使用手机推送通知:累计达标\ntask_id=tgt",
    exit_debounce_seconds=0,
    duration_seconds=None,
):
    r = _make_state_rule(
        rule_id=rule_id,
        task_id=task_id,
        on_enter_desc=on_enter_desc,
        on_exit_desc=on_exit_desc,
        exit_debounce_seconds=exit_debounce_seconds,
    )
    r.on_target_desc = on_target_desc
    if duration_seconds is not None:
        r.duration_seconds = duration_seconds
        r.duration_ratio = 1.0
    return r


def _make_runner_with_record(
    record_state, miot_proxy, log_repo, rules=None,
):
    """构造带 mock task_record_service 的 RuleRunner。

    record_state: read_duration_target_state 返回值（tuple 或 None）。
    detect_record_kind 跟 record_state 联动：None → None，其它 → "duration"。
    """
    mock_svc = MagicMock()
    mock_svc.read_duration_target_state = MagicMock(return_value=record_state)
    mock_svc.detect_record_kind = MagicMock(
        return_value=None if record_state is None else "duration"
    )
    return RuleRunner(
        rules=rules or [],
        miot_proxy=miot_proxy,
        rule_log_repo=log_repo,
        task_record_service=mock_svc,
    )


_TASK_SLOTS_WITH_TARGET = {
    "on_enter_actions": [],
    "on_enter_desc": "task 侧进入",
    "on_exit_actions": [],
    "on_exit_desc": None,
    "on_target_actions": [],
    "on_target_desc": "task 侧达标",
}


def _make_milestone_rule(rule_id="rule-ms", task_id=TASK_ID, on_target_desc=None):
    """带 record 条件项的 milestone rule —— 达标通知的承载物 (spec §6.4)。

    ``spec.value`` 故意写成一个错的阈值: 真阈值读 record 上的 ``target_minutes``,
    抄进条件项那份只是迁移遗留。写对了就分不出读的是哪一份。
    """
    return Rule(
        id=rule_id,
        name=f"[{task_id}] 累计达标",
        task_id=task_id,
        mode=RuleMode.EVENT,
        direction=RuleDirection.MILESTONE,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=RuleCondition(
            perceive_device_ids=["__milestone_no_camera__"], query="累计达标",
        ),
        condition_dnf=RuleConditionDNF(any_of=[[ConditionItem(
            source_type="record",
            spec={"task_id": task_id, "kind": "duration", "op": ">=",
                  "value": 999999},
        )]]),
        on_target_desc=on_target_desc,
    )


def _fired_events(mock_send):
    return [it.event for call in mock_send.call_args_list for it in call.args[1]]


class TestRecordMilestoneFire:
    """达标通知走 record 源 → 条件层 → milestone rule → task 的达标槽。

    防重复不靠标记, 靠条件项自身的边沿: 条件已经为真就说明发过了, 再喂真不产生
    边沿。跨日归零把它翻假, 第二天达标是一次全新的假→真。
    """

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_fires_when_already_accumulated_at_session_start(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """进 session 时累计已达标 → 立即发达标。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入")
        ms = _make_milestone_rule(on_target_desc="达标了")
        r.add_rule(main)
        r.add_rule(ms)

        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()

        assert RuleEvent.TARGET_FIRED in _fired_events(mock_send)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_fires_from_task_slot_when_milestone_rule_has_none(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """milestone rule 自己没配达标文案、task 列配了 —— 也要发。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入")
        ms = _make_milestone_rule()
        assert ms.on_target_desc is None
        r.add_rule(main)
        r.add_rule(ms)
        r.set_task_actions(TASK_ID, _TASK_SLOTS_WITH_TARGET)

        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()

        assert RuleEvent.TARGET_FIRED in _fired_events(mock_send)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_reads_target_from_record_not_from_condition_item(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """阈值取 record 上的当前值。取条件项里抄的那份 (999999) 就永远发不出。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入")
        r.add_rule(main)
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))

        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()

        prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.TARGET_FIRED)
        assert _extra_info(prompt).get("target_minutes") == 60

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_fire_metadata_carries_the_numbers(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """达标文案要用真实数字，所以目标 / 实际累计 / 达标时刻都得进 prompt。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 75), mock_miot_proxy, mock_log_repo)
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入")
        r.add_rule(main)
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))

        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()

        info = _extra_info(
            _last_dispatched_prompt(mock_send, event=RuleEvent.TARGET_FIRED)
        )
        assert info.get("target_minutes") == 60
        assert info.get("accumulated_at_fire") == 75
        assert "actual_target_at" in info

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_milestone_fire_does_not_carry_session_start(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """达标不是 session 起点 —— 注入 actual_started_at 会让 agent 去建 session。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入")
        r.add_rule(main)
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))

        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()

        info = _extra_info(
            _last_dispatched_prompt(mock_send, event=RuleEvent.TARGET_FIRED)
        )
        assert "actual_started_at" not in info

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_second_arm_in_the_same_period_does_not_refire(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """条件已为真 → 再喂真不产生边沿。这就是原先那个内存标记的全部作用。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        r.add_rule(_make_state_rule(rule_id="rule-main", on_enter_desc="进入"))
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))

        r.record_source.arm(TASK_ID)
        await asyncio.sleep(0.05)
        r.record_source.arm(TASK_ID)
        await asyncio.sleep(0.05)
        await r.drain()

        assert _fired_events(mock_send).count(RuleEvent.TARGET_FIRED) == 1

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_refires_after_cross_day_zeroing(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """归零把条件翻假 → 第二天达标是一次全新的边沿, 必须再发一次。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        r.add_rule(_make_state_rule(rule_id="rule-main", on_enter_desc="进入"))
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))

        r.record_source.arm(TASK_ID)
        await asyncio.sleep(0.05)
        await r.record_source.reset(TASK_ID)
        r.record_source.arm(TASK_ID)
        await asyncio.sleep(0.05)
        await r.drain()

        assert _fired_events(mock_send).count(RuleEvent.TARGET_FIRED) == 2

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_single_false_flips_the_condition(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """record 源喂假只喂一次 —— 留一帧观察窗会把这一次吸收掉, 条件永久停在真。

        帧级抗抖是给会漏识的 omni 设的; record 的值来自 DB 算术, 不会抖。
        """
        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        ms = _make_milestone_rule(on_target_desc="达标了")
        r.add_rule(_make_state_rule(rule_id="rule-main", on_enter_desc="进入"))
        r.add_rule(ms)

        r.record_source.arm(TASK_ID)
        await asyncio.sleep(0.05)
        assert r.is_condition_satisfied(ms.id) is True

        await r.record_source.reset(TASK_ID)

        assert r.is_condition_satisfied(ms.id) is False

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_blocked_when_task_not_in_session(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """task 没在进行时里程碑无处附着 (§5.3)。"""
        from miloco.task.state_machine import TaskRuntimeState, TransitionOutcome

        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        ms = _make_milestone_rule(on_target_desc="达标了")
        r.add_rule(_make_state_rule(rule_id="rule-main", on_enter_desc="进入"))
        r.add_rule(ms)
        sm = MagicMock()
        sm.owns.return_value = True
        sm.handle.return_value = TransitionOutcome.NOT_IN_SESSION
        sm.runtime_state.return_value = TaskRuntimeState.OFF
        r.attach_state_machine(sm)

        r.record_source.arm(TASK_ID)
        await asyncio.sleep(0.05)
        await r.drain()

        assert RuleEvent.TARGET_FIRED not in _fired_events(mock_send)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_zeroing_unlatches_a_condition_left_true_by_a_blocked_fire(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """被拦掉的那次会把条件留在真（置位排在闸门之前），归零是唯一的解锁点。

        名字说的是归零解锁，不是"同日能重试"—— 同日不能，条件已经为真了。真正
        的防线是不在 off 态喂真，那条由
        ``TestRecordMilestoneSessionBoundary::test_exit_rule_does_not_arm_on_its_entering_edge``
        钉住。
        """
        from miloco.task.state_machine import TaskRuntimeState, TransitionOutcome

        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        ms = _make_milestone_rule(on_target_desc="达标了")
        r.add_rule(_make_state_rule(rule_id="rule-main", on_enter_desc="进入"))
        r.add_rule(ms)
        sm = MagicMock()
        sm.owns.return_value = True
        sm.handle.return_value = TransitionOutcome.NOT_IN_SESSION
        sm.runtime_state.return_value = TaskRuntimeState.OFF
        r.attach_state_machine(sm)
        r.record_source.arm(TASK_ID)
        await asyncio.sleep(0.05)

        # 回到 session: 条件得先翻假才能再产生边沿, 所以这里连带验了归零那条路
        sm.handle.return_value = TransitionOutcome.MILESTONE_FIRED
        sm.runtime_state.return_value = TaskRuntimeState.ON
        await r.record_source.reset(TASK_ID)
        r.record_source.arm(TASK_ID)
        await asyncio.sleep(0.05)
        await r.drain()

        assert RuleEvent.TARGET_FIRED in _fired_events(mock_send)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_no_record_item_no_timer(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """task 下没有 record 条件项 → 进 session 不排任何 timer。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 0), mock_miot_proxy, mock_log_repo)
        r.add_rule(_make_state_rule(rule_id="rule-main", on_enter_desc="进入"))

        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)

        assert r.record_source._timers == {}

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_schedules_timer_when_not_yet_reached(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        mock_send.return_value = True
        r = _make_runner_with_record((60, 0), mock_miot_proxy, mock_log_repo)
        ms = _make_milestone_rule(on_target_desc="达标了")
        r.add_rule(_make_state_rule(rule_id="rule-main", on_enter_desc="进入"))
        r.add_rule(ms)

        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)

        assert ms.id in r.record_source._timers
        assert RuleEvent.TARGET_FIRED not in _fired_events(mock_send)
        r.record_source.disarm(TASK_ID)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_no_duration_record_no_fire(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        mock_send.return_value = True
        r = _make_runner_with_record(None, mock_miot_proxy, mock_log_repo)
        r.add_rule(_make_state_rule(rule_id="rule-main", on_enter_desc="进入"))
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))

        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()

        assert RuleEvent.TARGET_FIRED not in _fired_events(mock_send)
        assert r.record_source._timers == {}

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_different_tasks_fire_independently(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """一个 task 发过达标不该让另一个 task 的达标被吞掉。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 60), mock_miot_proxy, mock_log_repo)
        r.add_rule(_make_milestone_rule(rule_id="ms-a", task_id="task_a",
                                        on_target_desc="A 达标"))
        r.add_rule(_make_milestone_rule(rule_id="ms-b", task_id="task_b",
                                        on_target_desc="B 达标"))

        r.record_source.arm("task_a")
        r.record_source.arm("task_b")
        await asyncio.sleep(0.05)
        await r.drain()

        assert _fired_events(mock_send).count(RuleEvent.TARGET_FIRED) == 2

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_removing_the_rule_drops_its_timer(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """timer 存在 record 源那边, 不在 per-rule 的 state 里 —— 删 rule 要连带撤。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 0), mock_miot_proxy, mock_log_repo)
        ms = _make_milestone_rule(on_target_desc="达标了")
        r.add_rule(ms)
        r.record_source.arm(TASK_ID)
        await asyncio.sleep(0.05)
        assert ms.id in r.record_source._timers

        r.remove_rule(ms.id)

        assert r.record_source._timers == {}


class TestRecordMilestoneAtExit:
    """退出那一刻的兜底：timer 按挂起前的时钟排，睡过头醒来时可能早该达标。"""

    @staticmethod
    def _runner_with_switchable_record(initial, miot_proxy, log_repo):
        """``read_duration_target_state`` 的返回值可在测试运行时切换。"""
        mock_svc = MagicMock()
        holder = {"value": initial}
        mock_svc.read_duration_target_state = MagicMock(
            side_effect=lambda task_id: holder["value"]
        )
        mock_svc.detect_record_kind = MagicMock(
            return_value=None if initial is None else "duration"
        )
        r = RuleRunner(
            rules=[], miot_proxy=miot_proxy, rule_log_repo=log_repo,
            task_record_service=mock_svc,
        )
        return r, holder

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_fires_when_accumulated_reaches_during_debounce(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """debounce 窗口内跨过目标 → 退出前必须兑现, 否则这次达标永远丢。"""
        mock_send.return_value = True
        r, holder = self._runner_with_switchable_record(
            (5, 0), mock_miot_proxy, mock_log_repo,
        )
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入",
                                on_exit_desc="离开", exit_debounce_seconds=0)
        r.add_rule(main)
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))
        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)

        holder["value"] = (5, 5)
        await r.update_state("rule-main", "cam-001", False, "")
        await r.update_state("rule-main", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()

        assert RuleEvent.TARGET_FIRED in _fired_events(mock_send)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_silent_when_accumulated_below_target(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        mock_send.return_value = True
        r, _holder = self._runner_with_switchable_record(
            (60, 10), mock_miot_proxy, mock_log_repo,
        )
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入",
                                on_exit_desc="离开", exit_debounce_seconds=0)
        r.add_rule(main)
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))
        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)

        await r.update_state("rule-main", "cam-001", False, "")
        await r.update_state("rule-main", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()

        assert RuleEvent.TARGET_FIRED not in _fired_events(mock_send)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_does_not_refire_when_already_fired_this_period(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """进 session 时就发过了 —— 退出兜底不该再发一次。"""
        mock_send.return_value = True
        r, _holder = self._runner_with_switchable_record(
            (5, 5), mock_miot_proxy, mock_log_repo,
        )
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入",
                                on_exit_desc="离开", exit_debounce_seconds=0)
        r.add_rule(main)
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))
        await r.update_state("rule-main", "cam-001", True, "")
        await asyncio.sleep(0.05)

        await r.update_state("rule-main", "cam-001", False, "")
        await r.update_state("rule-main", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()

        assert _fired_events(mock_send).count(RuleEvent.TARGET_FIRED) == 1


class TestRuleForceCrossDayResetNoSessionTimestamps:
    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_cross_day_fire_omits_session_timestamps(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """跨日 force-reset fire 的 prompt 不得含 actual_exited_at /
        actual_started_at。rollover_one 已切段 session 边界；若注入这些时间戳，
        preamble 会强制 agent 调 session-end --at midnight，与新 record 的
        active_session_start_at（rollover 触发时刻）冲突触发 RecordSchemaError。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            (60, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-xday-no-ts")
        r.add_rule(rule)
        await r.update_state("rule-xday-no-ts", "cam-001", True, "")
        await asyncio.sleep(0.05)
        r.force_cross_day_reset(rule.task_id)
        await asyncio.sleep(0.05)
        await r.drain()
        exit_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.EXITED)
        assert "actual_exited_at" not in _extra_info(exit_prompt), (
            f"跨日 EXITED prompt 不应含 actual_exited_at；实际:\n{exit_prompt}"
        )
        enter_prompts = [
            it.prompt_text
            for call in mock_send.call_args_list
            for it in call.args[1]
            if it.event == RuleEvent.ENTERED
        ]
        assert enter_prompts, "expected at least one ENTERED dispatch"
        # 第一次 ENTERED（用户首次进入）允许带 actual_started_at；
        # 最后一次 ENTERED 是跨日 force-reset，不应带
        assert "actual_started_at" not in _extra_info(enter_prompts[-1]), (
            f"跨日 ENTERED prompt 不应含 actual_started_at；实际:\n{enter_prompts[-1]}"
        )


class TestRuleSelectSlotTargetFired:
    @pytest.mark.asyncio
    async def test_select_slot_target_fired_returns_dynamic(
        self, mock_miot_proxy, mock_log_repo,
    ):
        r = _make_runner_with_record(
            (60, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(
            rule_id="rule-slot-tgt",
            on_target_desc="累计达标通知\ntask_id=slot-tgt",
        )
        r.add_rule(rule)
        slot = r._select_slot(rule, RuleEvent.TARGET_FIRED)
        assert slot is not None
        assert slot[0] == "dynamic"
        assert "累计达标通知" in slot[1]

    @pytest.mark.asyncio
    async def test_select_slot_target_fired_none_when_unset(
        self, mock_miot_proxy, mock_log_repo,
    ):
        r = _make_runner_with_record(
            None, mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule(
            rule_id="rule-slot-no-tgt",
            on_enter_desc="进入\ntask_id=x",
            on_exit_desc="离开\ntask_id=x",
            exit_debounce_seconds=0,
        )
        slot = r._select_slot(rule, RuleEvent.TARGET_FIRED)
        assert slot is None


class TestTargetRecordValidation:
    """达标配套校验：报错信息按当前 record 状态分三 case。"""

    def _make_service_with_record_mock(
        self,
        record_kind: str | None,
        duration_state: tuple[int | None, int] | None,
        mock_miot_proxy,
        mock_log_repo,
        mock_rule_repo,
        mock_task_repo,
    ):
        mock_record_svc = MagicMock()
        mock_record_svc.detect_record_kind = MagicMock(return_value=record_kind)
        mock_record_svc.read_duration_target_state = MagicMock(
            return_value=duration_state
        )
        runner = RuleRunner(
            rules=[],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            task_record_service=mock_record_svc,
        )
        return RuleService(
            mock_rule_repo, mock_log_repo, runner, mock_miot_proxy,
            task_repo=mock_task_repo,
            task_record_service=mock_record_svc,
        )

    def test_no_record_error_includes_init_command(
        self, mock_miot_proxy, mock_log_repo, mock_rule_repo, mock_task_repo,
    ):
        svc = self._make_service_with_record_mock(
            None, None, mock_miot_proxy, mock_log_repo,
            mock_rule_repo, mock_task_repo,
        )
        rule = _make_state_rule_with_target(rule_id="r1", task_id="phone_time")
        with pytest.raises(ValidationException) as exc:
            svc._validate_target_record(rule)
        msg = str(exc.value)
        assert "无活跃 record" in msg
        assert "miloco-cli task record init phone_time --kind duration" in msg

    def test_progress_kind_error_includes_kind_mismatch(
        self, mock_miot_proxy, mock_log_repo, mock_rule_repo, mock_task_repo,
    ):
        svc = self._make_service_with_record_mock(
            "progress", None, mock_miot_proxy, mock_log_repo,
            mock_rule_repo, mock_task_repo,
        )
        rule = _make_state_rule_with_target(rule_id="r1", task_id="drink_8")
        with pytest.raises(ValidationException) as exc:
            svc._validate_target_record(rule)
        msg = str(exc.value)
        assert "kind='progress'" in msg
        assert "仅 duration 支持累计达标" in msg
        assert "miloco-cli task delete drink_8" in msg

    def test_event_kind_error_includes_kind_mismatch(
        self, mock_miot_proxy, mock_log_repo, mock_rule_repo, mock_task_repo,
    ):
        svc = self._make_service_with_record_mock(
            "event", None, mock_miot_proxy, mock_log_repo,
            mock_rule_repo, mock_task_repo,
        )
        rule = _make_state_rule_with_target(rule_id="r1", task_id="fall_alert")
        with pytest.raises(ValidationException) as exc:
            svc._validate_target_record(rule)
        assert "kind='event'" in str(exc.value)

    def test_duration_no_target_minutes_error_includes_update_command(
        self, mock_miot_proxy, mock_log_repo, mock_rule_repo, mock_task_repo,
    ):
        svc = self._make_service_with_record_mock(
            "duration", (None, 0), mock_miot_proxy, mock_log_repo,
            mock_rule_repo, mock_task_repo,
        )
        rule = _make_state_rule_with_target(rule_id="r1", task_id="reading")
        with pytest.raises(ValidationException) as exc:
            svc._validate_target_record(rule)
        msg = str(exc.value)
        assert "target_minutes" in msg
        assert "当前为空" in msg
        assert "miloco-cli task record update reading" in msg

    def test_valid_duration_with_target_passes(
        self, mock_miot_proxy, mock_log_repo, mock_rule_repo, mock_task_repo,
    ):
        svc = self._make_service_with_record_mock(
            "duration", (60, 0), mock_miot_proxy, mock_log_repo,
            mock_rule_repo, mock_task_repo,
        )
        rule = _make_state_rule_with_target(rule_id="r1", task_id="phone_time")
        # 不应 raise
        svc._validate_target_record(rule)

    def test_on_target_desc_empty_skips_check(
        self, mock_miot_proxy, mock_log_repo, mock_rule_repo, mock_task_repo,
    ):
        """on_target_desc=None 时不查 record，校验直接通过。"""
        mock_record_svc = MagicMock()
        mock_record_svc.detect_record_kind = MagicMock()
        runner = RuleRunner(
            rules=[],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            task_record_service=mock_record_svc,
        )
        svc = RuleService(
            mock_rule_repo, mock_log_repo, runner, mock_miot_proxy,
            task_repo=mock_task_repo,
            task_record_service=mock_record_svc,
        )
        rule = _make_state_rule(
            rule_id="r1",
            on_enter_desc="进入\ntask_id=x",
            on_exit_desc="离开\ntask_id=x",
            exit_debounce_seconds=0,
        )
        rule.on_target_desc = None
        svc._validate_target_record(rule)
        # 没调 detect_record_kind 才证明早返
        mock_record_svc.detect_record_kind.assert_not_called()


class TestRuleExitedMetadata:
    """EXITED fire 注入 accumulated_minutes_today / target_minutes metadata，
    供 fire-agent 拼条件通知文案（用户原话明示「每次玩完告诉我」时装的 on-exit-desc）。"""

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_exit_metadata_in_prompt_when_duration_record(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """duration record + target_minutes → EXITED prompt 末尾含 accumulated /
        target metadata。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            (60, 110), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule(
            rule_id="rule-exit-meta",
            on_enter_desc="开始计时",
            on_exit_desc="结束计时；若今日累计已达目标则使用手机推送通知：今日累计已达目标时长",
            exit_debounce_seconds=0,
        )
        r.add_rule(rule)
        await r.update_state("rule-exit-meta", "cam-001", True, "")
        await r.update_state("rule-exit-meta", "cam-001", False, "")
        await r.update_state("rule-exit-meta", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()
        exit_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.EXITED)
        info = _extra_info(exit_prompt)
        assert info.get("accumulated_minutes_today") == 110
        assert info.get("target_minutes") == 60

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_no_exit_metadata_when_no_record(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """无 record → EXITED prompt 不注入 accumulated/target（避免无意义字段）。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            None, mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule(
            rule_id="rule-exit-no-rec",
            on_enter_desc="进入",
            on_exit_desc="离开",
            exit_debounce_seconds=0,
        )
        r.add_rule(rule)
        await r.update_state("rule-exit-no-rec", "cam-001", True, "")
        await r.update_state("rule-exit-no-rec", "cam-001", False, "")
        await r.update_state("rule-exit-no-rec", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()
        exit_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.EXITED)
        info = _extra_info(exit_prompt)
        assert "accumulated_minutes_today" not in info
        assert "target_minutes" not in info

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_no_exit_metadata_when_target_none(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """duration record + target_minutes=None → EXITED 不注入（无达标语义）。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            (None, 30), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule(
            rule_id="rule-exit-no-target",
            on_enter_desc="开始计时",
            on_exit_desc="结束计时",
            exit_debounce_seconds=0,
        )
        r.add_rule(rule)
        await r.update_state("rule-exit-no-target", "cam-001", True, "")
        await r.update_state("rule-exit-no-target", "cam-001", False, "")
        await r.update_state("rule-exit-no-target", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()
        exit_prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.EXITED)
        info = _extra_info(exit_prompt)
        assert "accumulated_minutes_today" not in info
        assert "target_minutes" not in info


class TestRecordMilestoneCrossDay:
    """跨日：兑现旧一天 → 归零 → 按新一天重排。"""

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_settles_old_day_when_snapshot_reached(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """旧一天累计已达标但 timer 还没到点 → 跨日前必须兑现。

        rollover 已经清了累计，读当前值看不出旧一天达标过，只能靠快照。
        """
        mock_send.return_value = True
        r = _make_runner_with_record((60, 0), mock_miot_proxy, mock_log_repo)
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入",
                                on_exit_desc="离开")
        r.add_rule(main)
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))
        r._ensure_state(main.id).last_rule_state = True

        r.force_cross_day_reset(TASK_ID, pre_rollover_state=(60, 90))
        await asyncio.sleep(0.05)
        await r.drain()

        assert RuleEvent.TARGET_FIRED in _fired_events(mock_send)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_silent_when_snapshot_below_target(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        mock_send.return_value = True
        r = _make_runner_with_record((60, 0), mock_miot_proxy, mock_log_repo)
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入",
                                on_exit_desc="离开")
        r.add_rule(main)
        r.add_rule(_make_milestone_rule(on_target_desc="达标了"))
        r._ensure_state(main.id).last_rule_state = True

        r.force_cross_day_reset(TASK_ID, pre_rollover_state=(60, 30))
        await asyncio.sleep(0.05)
        await r.drain()

        assert RuleEvent.TARGET_FIRED not in _fired_events(mock_send)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_rearms_for_the_new_day(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """归零后按 accumulated=0 重排, 否则新一天永远发不出达标。"""
        mock_send.return_value = True
        r = _make_runner_with_record((60, 0), mock_miot_proxy, mock_log_repo)
        main = _make_state_rule(rule_id="rule-main", on_enter_desc="进入",
                                on_exit_desc="离开")
        ms = _make_milestone_rule(on_target_desc="达标了")
        r.add_rule(main)
        r.add_rule(ms)
        r._ensure_state(main.id).last_rule_state = True

        r.force_cross_day_reset(TASK_ID, pre_rollover_state=(60, 90))
        await asyncio.sleep(0.05)

        assert r.is_condition_satisfied(ms.id) is False
        assert ms.id in r.record_source._timers
        r.record_source.disarm(TASK_ID)


class TestRuleRunnerPerDeviceStateIndependence:
    @pytest.mark.asyncio
    async def test_per_did_or_aggregation_holds_until_all_exit(
        self, runner, mock_miot_proxy
    ):
        """rule 绑 [A,B]:A=True 后 B 翻转 False 不应推退 rule;两 did 都 False ×2
        才进 EXITED。锁定 per-did OR 聚合主语义。"""
        rule = _make_state_rule(
            rule_id="rule-or-hold",
            condition=_make_condition(device_ids=["cam-A", "cam-B"]),
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1")],
            on_exit_actions=[_make_action(did="exit-d", iid="prop.2.1")],
            exit_debounce_seconds=0,
        )
        runner.add_rule(rule)
        await runner.update_state("rule-or-hold", "cam-A", True, "")  # ENTERED via A
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1

        # B 两帧 False 不影响 rule(B 桶起始就是 False,这两帧是 noop 维持)
        await runner.update_state("rule-or-hold", "cam-B", False, "")
        await runner.update_state("rule-or-hold", "cam-B", False, "")
        await runner.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1  # 仍只 enter

        # A 翻 False:第一帧 pending(prev=True),第二帧确认 → rule 翻 False → EXITED
        await runner.update_state("rule-or-hold", "cam-A", False, "")  # pending
        await runner.update_state("rule-or-hold", "cam-A", False, "")  # 确认
        await asyncio.sleep(0.05)
        await runner.drain()
        dids = [
            call[0][0][0].did
            for call in mock_miot_proxy.set_device_properties.call_args_list
        ]
        assert dids == ["enter-d", "exit-d"]
        # 内部桶状态:A=False, B=False
        assert runner._last_source_state[("rule-or-hold", "cam-A")] is False
        assert runner._last_source_state[("rule-or-hold", "cam-B")] is False

    @pytest.mark.asyncio
    async def test_pending_exit_isolation_between_dids(
        self, runner, mock_miot_proxy
    ):
        """A 进 pending_source_exit 时,B 帧不应清掉 A 的 pending。
        若误用 dict[rule_id] 单桶存 pending,B 的 update 会写覆盖。"""
        rule = _make_static_rule(
            rule_id="rule-pex-iso",
            condition=_make_condition(device_ids=["cam-A", "cam-B"]),
        )
        runner.add_rule(rule)
        await runner.update_state("rule-pex-iso", "cam-A", True, "")
        await runner.update_state("rule-pex-iso", "cam-B", True, "")
        await runner.drain()
        # A 进 pending_exit(prev=True, current=False, 1st frame)
        await runner.update_state("rule-pex-iso", "cam-A", False, "")
        assert ("rule-pex-iso", "cam-A") in runner._pending_source_exit
        # B 单独 True(STILL TRUE,no-op 内部)— 不应清 A pending
        await runner.update_state("rule-pex-iso", "cam-B", True, "")
        assert ("rule-pex-iso", "cam-A") in runner._pending_source_exit, (
            "A 的 pending_exit 被 B 帧清掉了:pending_source_exit 桶未隔离"
        )
        # B 自己也进 pending_exit
        await runner.update_state("rule-pex-iso", "cam-B", False, "")
        assert ("rule-pex-iso", "cam-B") in runner._pending_source_exit
        assert ("rule-pex-iso", "cam-A") in runner._pending_source_exit
        # 两 source pending 互不干扰
        assert runner._last_source_state[("rule-pex-iso", "cam-A")] is True
        assert runner._last_source_state[("rule-pex-iso", "cam-B")] is True

    @pytest.mark.asyncio
    async def test_duration_seconds_cross_did_or_aggregation(
        self, mock_miot_proxy, mock_log_repo
    ):
        """duration_seconds 窗口跨 source OR 聚合:A 真 1 个采样后切到 B 真,
        累计 2 个连续 round → 满 ratio=1.0 触发 fire。
        锁定 _evaluate_duration 的 effective_state OR 行为(runner.py:277-282)。"""
        runner_fast = RuleRunner(
            rules=[],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            sample_interval_seconds=0.5,
        )
        rule = _make_event_duration_rule(
            rule_id="rule-dur-cross",
            duration_seconds=1,  # maxlen = 1 / 0.5 = 2
            duration_ratio=1.0,
        )
        # 绑双 source 让 OR 聚合参与
        rule.condition = _make_condition(device_ids=["cam-A", "cam-B"])
        runner_fast.add_rule(rule)
        with patch("miloco.rule.runner.time.time") as mt:
            mt.return_value = 100.0  # round_id = 200
            await runner_fast.update_state("rule-dur-cross", "cam-A", True, "")
            # 切到 B:A 上一帧仍 True(_last_source_state 桶为 True),所以这一帧
            # 在 _evaluate_duration 看来 effective_state = current_bool or any(其他桶 true)
            # = False(B 帧) or True(A 桶) = True → 累计满 2 round → fire
            mt.return_value = 100.5  # round_id = 201
            await runner_fast.update_state("rule-dur-cross", "cam-B", True, "")
        await runner_fast.drain()
        assert mock_miot_proxy.set_device_properties.call_count == 1


# ---- 场景 action（iid=SCENE_IID，did 位置放 scene_id）----


def _make_scene_action(scene_id="scene-1", cooldown=5):
    from miloco.rule.schema import SCENE_IID

    return RuleAction(
        did=scene_id, iid=SCENE_IID, idempotent=False, cooldown_minutes=cooldown
    )


class TestRuleRunnerSceneAction:
    """场景没有 siid/aiid 可拆，必须在 iid 解析之前分流；台账走
    miot.service._trigger_scene 统一落，source 标 rule。"""

    @pytest.mark.asyncio
    async def test_scene_action_delegates_with_rule_source(self, runner, monkeypatch):
        from unittest.mock import AsyncMock as _AM

        spy = _AM(return_value=True)
        monkeypatch.setattr("miloco.miot.service._trigger_scene", spy)

        result = await runner._execute_action("rule-77", _make_scene_action())

        assert result.result is True
        assert result.skipped is False
        assert result.error is None
        spy.assert_awaited_once()
        assert spy.await_args.args[1] == "scene-1"
        assert spy.await_args.kwargs["source"] == "rule"
        assert spy.await_args.kwargs["source_id"] == "rule-77"

    @pytest.mark.asyncio
    async def test_scene_action_never_reaches_iid_parsing(self, runner, monkeypatch):
        """回归钉子：'scene' 拆不出 siid/aiid，走到通用解析必然 invalid_iid。"""
        from unittest.mock import AsyncMock as _AM

        monkeypatch.setattr(
            "miloco.miot.service._trigger_scene", _AM(return_value=True)
        )
        result = await runner._execute_action("rule-77", _make_scene_action())
        assert result.error is None or "invalid_iid" not in result.error

    @pytest.mark.asyncio
    async def test_scene_cooldown_skips_within_window(self, runner, monkeypatch):
        from unittest.mock import AsyncMock as _AM

        spy = _AM(return_value=True)
        monkeypatch.setattr("miloco.miot.service._trigger_scene", spy)
        action = _make_scene_action()

        first = await runner._execute_action("rule-cd-scene", action)
        second = await runner._execute_action("rule-cd-scene", action)

        assert first.skipped is False
        assert second.skipped is True
        assert second.result is True
        assert spy.await_count == 1

    @pytest.mark.asyncio
    async def test_scene_cooldown_isolated_per_scene(self, runner, monkeypatch):
        """冷却键是 (did, iid)：同规则的两个场景不能互相把对方压掉。"""
        from unittest.mock import AsyncMock as _AM

        spy = _AM(return_value=True)
        monkeypatch.setattr("miloco.miot.service._trigger_scene", spy)

        await runner._execute_action("rule-multi", _make_scene_action("scene-A"))
        other = await runner._execute_action(
            "rule-multi", _make_scene_action("scene-B")
        )

        assert other.skipped is False
        assert spy.await_count == 2

    @pytest.mark.asyncio
    async def test_scene_failure_reported_and_not_cooled_down(
        self, runner, monkeypatch
    ):
        """执行失败不写冷却，下一次 fire 还能重试。"""
        from unittest.mock import AsyncMock as _AM

        spy = _AM(return_value=False)
        monkeypatch.setattr("miloco.miot.service._trigger_scene", spy)
        action = _make_scene_action()

        first = await runner._execute_action("rule-fail", action)
        second = await runner._execute_action("rule-fail", action)

        assert first.result is False
        assert first.error == "scene_trigger_failed"
        assert second.skipped is False
        assert spy.await_count == 2

    @pytest.mark.asyncio
    async def test_scene_exception_reported(self, runner, monkeypatch):
        """场景不存在 / 不在允许家庭 / SDK 抛错都要落到 execute_result，不吞。"""
        from unittest.mock import AsyncMock as _AM

        monkeypatch.setattr(
            "miloco.miot.service._trigger_scene",
            _AM(side_effect=RuntimeError("scene gone")),
        )
        result = await runner._execute_action("rule-exc", _make_scene_action())

        assert result.result is False
        assert "scene gone" in result.error


class TestRuleServiceSceneActionValidation:
    """场景读不到现值：幂等分支会跳过判定直接下发，等于没有去重。
    service 层必须逼出 idempotent=false，再由既有校验逼出 cooldown_minutes。"""

    @pytest.mark.asyncio
    async def test_scene_action_idempotent_true_raises(self, service):
        bad = RuleAction(
            did="scene-1", iid="scene", idempotent=True, cooldown_minutes=5
        )
        rule = _make_static_rule(rule_id="", actions=[bad])
        with pytest.raises(
            ValidationException,
            match=r"actions\[0\].*iid=scene requires idempotent=false",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_scene_action_without_cooldown_raises(self, service):
        """场景缺冷却命中更具体的那条（下限 1），而非通用的「非幂等要配冷却」。"""
        bad = RuleAction(did="scene-1", iid="scene", idempotent=False)
        rule = _make_static_rule(rule_id="", actions=[bad])
        with pytest.raises(
            ValidationException,
            match=r"actions\[0\].*iid=scene requires cooldown_minutes >= 1",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_scene_action_on_enter_slot_validated(self, service):
        bad = RuleAction(
            did="scene-1", iid="scene", idempotent=True, cooldown_minutes=5
        )
        rule = _make_state_rule(rule_id="", on_enter_actions=[bad])
        with pytest.raises(
            ValidationException,
            match=r"on_enter_actions\[0\].*iid=scene requires idempotent=false",
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_scene_action_valid_passes(self, service):
        good = _make_scene_action()
        rule = _make_static_rule(rule_id="", actions=[good])
        assert await service.create_rule(rule) == "new-rule-id"

    @pytest.mark.asyncio
    async def test_scene_plus_tts_on_enter_passes(self, service):
        """本次改动的目标形态：一个方向装两条 action（场景 + 播报）。"""
        rule = _make_state_rule(
            rule_id="",
            on_enter_actions=[
                _make_scene_action("scene-movie"),
                RuleAction(
                    did="speaker", iid="action.7.3", params=["观影模式已就绪"],
                    idempotent=False, cooldown_minutes=5,
                ),
            ],
        )
        assert await service.create_rule(rule) == "new-rule-id"


class TestRuleRunnerSceneActionGuards:
    """service 校验只在 CRUD 走；runner 从 repo 直接灌规则，执行侧要有独立的闸。"""

    @pytest.mark.asyncio
    async def test_idempotent_scene_action_refused(self, runner, monkeypatch):
        """库里混进 idempotent=true 的场景行 → 每次 fire 都真触发，必须挡住。"""
        from unittest.mock import AsyncMock as _AM

        spy = _AM(return_value=True)
        monkeypatch.setattr("miloco.miot.service._trigger_scene", spy)
        bad = RuleAction(
            did="scene-1", iid="scene", idempotent=True, cooldown_minutes=5
        )

        result = await runner._execute_action("rule-guard", bad)

        assert result.result is False
        assert "idempotent=false" in result.error
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dead_cooldown", [0, None])
    async def test_scene_action_without_cooldown_refused(
        self, runner, monkeypatch, dead_cooldown
    ):
        """冷却是场景唯一的去重手段；runner 把 0/None 当「无冷却」，
        这种行下发就是零限频——和 idempotent=true 同级，执行侧要一起挡。"""
        from unittest.mock import AsyncMock as _AM

        spy = _AM(return_value=True)
        monkeypatch.setattr("miloco.miot.service._trigger_scene", spy)
        bad = RuleAction(
            did="scene-1", iid="scene", idempotent=False,
            cooldown_minutes=dead_cooldown,
        )

        result = await runner._execute_action("rule-guard", bad)

        assert result.result is False
        assert "cooldown_minutes >= 1" in result.error
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scene_like_iid_is_rejected_not_dispatched(
        self, runner, mock_miot_proxy
    ):
        """`scene.1.2` 曾会被当成 call_action，拿 scene_id 当 did 发出去。"""
        action = RuleAction(did="scene-1", iid="scene.1.2", idempotent=False,
                            cooldown_minutes=5)
        result = await runner._execute_action("rule-nearmiss", action)

        assert result.result is False
        assert result.error == "invalid_iid: scene.1.2"
        mock_miot_proxy.call_device_action.assert_not_called()


class TestRuleServiceSceneIdExistence:
    """scene_id 抄错一位 → 规则建成功但永远是哑的；创建期就要挡。"""

    @pytest.mark.asyncio
    async def test_unknown_scene_id_rejected(self, service):
        rule = _make_static_rule(
            rule_id="", actions=[_make_scene_action("scene-typo")]
        )
        with pytest.raises(ValidationException, match=r"Invalid or not-allowed scene IDs: scene-typo"):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_known_scene_id_passes(self, service):
        rule = _make_static_rule(
            rule_id="", actions=[_make_scene_action("scene-real")]
        )
        assert await service.create_rule(rule) == "new-rule-id"

    @pytest.mark.asyncio
    async def test_empty_scene_cache_says_so_not_invalid_id(
        self, service, mock_miot_proxy
    ):
        """拿不到场景表时别谎报「你的 id 无效」——两种失败的修法完全不同。"""
        from unittest.mock import AsyncMock as _AM

        mock_miot_proxy.get_all_scenes = _AM(return_value={})
        rule = _make_static_rule(rule_id="", actions=[_make_scene_action("scene-1")])
        with pytest.raises(ValidationException, match=r"Scene list unavailable"):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_patch_rule_also_validates_scene_id(self, service, mock_rule_repo):
        """PATCH 路径不能是校验缺口。"""
        from miloco.rule.schema import RuleUpdate

        mock_rule_repo.get_by_id = MagicMock(return_value=_make_static_rule())
        with pytest.raises(ValidationException, match=r"Invalid or not-allowed scene IDs: scene-typo"):
            await service.patch_rule(
                "rule-1",
                RuleUpdate(actions=[_make_scene_action("scene-typo")]),
            )

    @pytest.mark.asyncio
    async def test_non_scene_rule_does_not_query_scenes(
        self, service, mock_miot_proxy
    ):
        """没有场景动作时不该白跑一次场景查询。"""
        from unittest.mock import AsyncMock as _AM

        spy = _AM(return_value={})
        mock_miot_proxy.get_all_scenes = spy
        await service.create_rule(_make_static_rule(rule_id=""))
        spy.assert_not_awaited()


class TestRuleServiceSceneHomeScope:
    """场景校验必须与执行侧 is_home_allowed 同口径：校验放行的，运行期要真触发得动。
    `get_all_scenes` 返回账号名下所有家，不过滤会让白名单外的场景建成功后每次 fire 都失败。"""

    @pytest.mark.asyncio
    async def test_scene_from_other_home_rejected(self, service, mock_miot_proxy):
        mock_miot_proxy.get_all_scenes = AsyncMock(
            return_value={
                "scene-other": SimpleNamespace(home_id="home-2", scene_name="他家")
            }
        )
        rule = _make_static_rule(
            rule_id="", actions=[_make_scene_action("scene-other")]
        )
        with pytest.raises(
            ValidationException, match=r"Invalid or not-allowed scene IDs: scene-other"
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_other_home_scene_ids_not_leaked_in_error(
        self, service, mock_miot_proxy
    ):
        """报错串不该把白名单外的 scene_id 列给调用方——那些 id 在 scene list 里看不到。"""
        mock_miot_proxy.get_all_scenes = AsyncMock(
            return_value={
                "scene-1": SimpleNamespace(home_id=MOCK_HOME_ID, scene_name="本家"),
                "secret-other-home": SimpleNamespace(home_id="home-2", scene_name="他家"),
            }
        )
        rule = _make_static_rule(rule_id="", actions=[_make_scene_action("scene-typo")])
        with pytest.raises(ValidationException) as exc:
            await service.create_rule(rule)
        assert "secret-other-home" not in str(exc.value)

    @pytest.mark.asyncio
    async def test_no_home_enabled_says_so(self, service, mock_miot_proxy):
        """未选家时白名单为空，任何场景都触发不了；创建期就说清楚。"""
        mock_miot_proxy._kv_repo.get = MagicMock(return_value=None)
        rule = _make_static_rule(rule_id="", actions=[_make_scene_action("scene-1")])
        with pytest.raises(ValidationException, match=r"No home is enabled"):
            await service.create_rule(rule)


class TestRuleServiceIidShape:
    """iid 形态在 CRUD 层就要挡。执行侧白名单只会让规则静默哑掉，
    调用方拿到的仍是「创建成功」。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_iid", ["scene.1.2", "scenes", "Scene", "prop.2",
                                         "action.1.2.3", "prop.a.b"])
    async def test_malformed_iid_rejected(self, service, bad_iid):
        bad = RuleAction(did="d1", iid=bad_iid, value=True)
        rule = _make_static_rule(rule_id="", actions=[bad])
        with pytest.raises(ValidationException, match=r"iid must be"):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("good_iid", ["prop.2.1", "action.7.3", "scene"])
    async def test_valid_iid_shapes_pass(self, service, good_iid):
        a = (
            _make_scene_action("scene-1")
            if good_iid == SCENE_IID_LITERAL
            else RuleAction(did="d1", iid=good_iid, value=True)
        )
        rule = _make_static_rule(rule_id="", actions=[a])
        assert await service.create_rule(rule) == "new-rule-id"


class TestRulePatchScopedValidation:
    """PATCH 的校验要跟着「这次改了什么」走。场景被删 / 家庭被关之后规则开始
    每次 fire 都失败，而 `rule disable` 本身就是一次 PATCH —— 无条件校验会
    在规则坏掉的那一刻把「先关掉它」这条自救路径堵死。"""

    @pytest.fixture
    def rule_with_dead_scene(self, mock_rule_repo, mock_miot_proxy):
        mock_rule_repo.get_by_id = MagicMock(
            return_value=_make_static_rule(actions=[_make_scene_action("scene-gone")])
        )
        mock_miot_proxy.get_all_scenes = AsyncMock(return_value={})
        return mock_rule_repo

    @pytest.mark.asyncio
    async def test_disable_still_works_when_scene_gone(
        self, service, rule_with_dead_scene
    ):
        from miloco.rule.schema import RuleUpdate

        assert await service.patch_rule("rule-1", RuleUpdate(enabled=False)) is True

    @pytest.mark.asyncio
    async def test_condition_edit_still_works_when_scene_gone(
        self, service, rule_with_dead_scene
    ):
        from miloco.rule.schema import RuleConditionUpdate, RuleUpdate

        upd = RuleUpdate(condition=RuleConditionUpdate(query="有人经过"))
        assert await service.patch_rule("rule-1", upd) is True

    @pytest.mark.asyncio
    async def test_touching_actions_still_validates_scene(
        self, service, rule_with_dead_scene
    ):
        """真改动作时该校验还是要校验。"""
        from miloco.rule.schema import RuleUpdate

        with pytest.raises(ValidationException, match=r"Scene list unavailable"):
            await service.patch_rule(
                "rule-1", RuleUpdate(actions=[_make_scene_action("scene-gone")])
            )


class TestRuleSceneCooldownFloor:
    """场景去重完全靠冷却，runner 把 0 当「无冷却」——填 0 等于零限频。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_cooldown", [0, -1])
    async def test_zero_or_negative_cooldown_rejected(self, service, bad_cooldown):
        bad = RuleAction(
            did="scene-1", iid="scene", idempotent=False,
            cooldown_minutes=bad_cooldown,
        )
        rule = _make_static_rule(rule_id="", actions=[bad])
        with pytest.raises(
            ValidationException, match=r"iid=scene requires cooldown_minutes >= 1"
        ):
            await service.create_rule(rule)

    @pytest.mark.asyncio
    async def test_one_minute_cooldown_passes(self, service):
        good = RuleAction(
            did="scene-1", iid="scene", idempotent=False, cooldown_minutes=1
        )
        rule = _make_static_rule(rule_id="", actions=[good])
        assert await service.create_rule(rule) == "new-rule-id"


class TestRecordMilestoneSessionBoundary:
    """带真状态机的 session 边界。

    上面那几个类用的 runner 没挂状态机，``_state_machine_allows`` 恒放行 ——
    「翻 off 之前」在那里根本不存在，所以两条时序约束在那些用例里测不出来。
    """

    @staticmethod
    def _build(record_state, rules, task_id=TASK_ID):
        from miloco.task.state_machine import TaskStateMachine, derive_directions

        svc = MagicMock()
        svc.read_duration_target_state = MagicMock(return_value=record_state)
        svc.detect_record_kind = MagicMock(return_value="duration")
        r = RuleRunner(
            rules=[], miot_proxy=AsyncMock(), rule_log_repo=MagicMock(),
            task_record_service=svc,
        )
        sm = TaskStateMachine(
            is_condition_satisfied=r.is_condition_satisfied,
            dispatch_action=lambda *_a: None,
        )
        r.attach_state_machine(sm)
        for rule in rules:
            r.add_rule(rule)
        r.set_task_actions(task_id, _TASK_SLOTS_WITH_TARGET)
        sm.register_task(
            task_id,
            derive_directions((x.id, x.resolved_direction.value) for x in rules),
        )
        return r, sm

    @staticmethod
    def _directional_rule(rule_id, direction, task_id=TASK_ID):
        return Rule(
            id=rule_id, name=rule_id, task_id=task_id,
            mode=RuleMode.EVENT, direction=direction,
            lifecycle=RuleLifecycle.PERMANENT,
            condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人"),
            action_descriptions=["播报"],
        )

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_exit_rule_settles_target_before_the_task_flips_off(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """非互反 task（enter + exit 两条 rule）的退出走 exit 型 rule 的进入边沿。

        兑现排在状态机翻 off 之后的话，达标会被 NOT_IN_SESSION 拦掉，而条件已经
        被置成真 —— 这一天再也产不出达标边沿。
        """
        mock_send.return_value = True
        enter_rule = self._directional_rule("r-enter", RuleDirection.ENTER)
        exit_rule = self._directional_rule("r-exit", RuleDirection.EXIT)
        ms = _make_milestone_rule()
        r, sm = self._build((60, 0), [enter_rule, exit_rule, ms])

        await r.update_state("r-enter", "cam-001", True, "")
        await asyncio.sleep(0.05)
        assert sm.runtime_state(TASK_ID) is TaskRuntimeState.ON

        # session 内累计跨过目标，随后 exit 条件成立
        r._task_record_service.read_duration_target_state = MagicMock(
            return_value=(60, 60)
        )
        await r.update_state("r-exit", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()

        assert sm.runtime_state(TASK_ID) is TaskRuntimeState.OFF
        assert _fired_events(mock_send).count(RuleEvent.TARGET_FIRED) == 1

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_exit_rule_does_not_arm_on_its_entering_edge(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """exit 型 rule 的"条件成立"是"该退出了" —— 在那儿排达标 timer 是反的。

        排下去的 timer 到点会在 off 态喂真，条件被锁死，当天再进 session 也发不出。
        """
        mock_send.return_value = True
        enter_rule = self._directional_rule("r-enter", RuleDirection.ENTER)
        exit_rule = self._directional_rule("r-exit", RuleDirection.EXIT)
        ms = _make_milestone_rule()
        r, _sm = self._build((60, 10), [enter_rule, exit_rule, ms])
        await r.update_state("r-enter", "cam-001", True, "")
        await asyncio.sleep(0.05)
        assert ms.id in r.record_source._timers

        await r.update_state("r-exit", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()

        assert r.record_source._timers == {}
        assert r.is_condition_satisfied(ms.id) is not True

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_timer_survives_an_exit_that_another_condition_still_holds(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """两条 session rule，一条断开另一条还撑着 → session 没结束，别撤 timer。

        撤了这一天的达标就丢了。这是 disarm 必须排在许可闸之后的理由。

        用两条 session rule 而不是两条 exit 型：exit 型的条件一成立就已经把 task
        推出去了，再进会被 §5.1 拦住，造不出「两个出口条件同时为真而 task 还在态内」。
        """
        mock_send.return_value = True

        def session_rule(rule_id):
            return Rule(
                id=rule_id, name=rule_id, task_id=TASK_ID, mode=RuleMode.STATE,
                direction=RuleDirection.SESSION,
                lifecycle=RuleLifecycle.PERMANENT,
                condition=RuleCondition(
                    perceive_device_ids=["cam-001"], query="有人"
                ),
                on_enter_desc="开始", on_exit_desc="结束",
                exit_debounce_seconds=0,
            )

        ms = _make_milestone_rule()
        rule_a, rule_b = session_rule("r-ses-a"), session_rule("r-ses-b")
        r, sm = self._build((60, 10), [rule_a, rule_b, ms])
        await r.update_state("r-ses-a", "cam-001", True, "")
        await asyncio.sleep(0.05)
        # b 也看到人: 它这次进入被「已在态内」吞掉, 但条件留在真 —— 于是 a 断开时
        # 出口侧还有 b 撑着。
        await r.update_state("r-ses-b", "cam-001", True, "")
        await asyncio.sleep(0.05)
        entered = sm.runtime_state(TASK_ID)
        assert ms.id in r.record_source._timers

        await r.update_state("r-ses-a", "cam-001", False, "")
        await r.update_state("r-ses-a", "cam-001", False, "")
        await asyncio.sleep(0.1)
        await r.drain()

        # 中间态必须断言: 不断言的话「从没进过 session」与「进了又退出」终态一样
        assert entered is TaskRuntimeState.ON
        assert sm.runtime_state(TASK_ID) is TaskRuntimeState.ON
        assert ms.id in r.record_source._timers
        r.record_source.disarm(TASK_ID)

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_cross_day_does_not_force_an_edge_on_the_milestone_rule(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """milestone 的条件为真只表示今天发过达标，不表示 task 在态内。

        不排除它的话午夜会给它强发一次进入边沿 —— 映射到达标槽，于是每天 00:00
        多播一条达标通知，还标成 ENTERED、绕过许可闸。
        """
        mock_send.return_value = True
        session_rule = Rule(
            id="r-ses", name="r-ses", task_id=TASK_ID, mode=RuleMode.STATE,
            direction=RuleDirection.SESSION, lifecycle=RuleLifecycle.PERMANENT,
            condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人"),
            on_enter_desc="开始", on_exit_desc="结束",
        )
        ms = _make_milestone_rule()
        r, _sm = self._build((60, 60), [session_rule, ms])
        await r.update_state("r-ses", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()
        assert r.is_condition_satisfied(ms.id) is True
        mock_send.reset_mock()

        # rollover_one 已清累计, 所以此刻读到的是新一天的 0
        r._task_record_service.read_duration_target_state = MagicMock(
            return_value=(60, 0)
        )
        r.force_cross_day_reset(TASK_ID, pre_rollover_state=(60, 60))
        await asyncio.sleep(0.1)
        await r.drain()

        milestone_fires = [
            it.rule_id
            for call in mock_send.call_args_list
            for it in call.args[1]
            if it.rule_id == ms.id
        ]
        assert milestone_fires == []
        assert r.is_condition_satisfied(ms.id) is False

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_cross_day_does_not_force_a_pair_on_an_exit_rule(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """exit 型的条件为真是"该退出了", 不表示 task 在态内。

        不排除它的话: 用户 23:00 离开沙发, "沙发上没人"整夜为真, 00:00 强发一次进入
        边沿 → 映射到退出槽 → 在 task 已经关闭的情况下多执行一次退出动作。
        """
        mock_send.return_value = True
        enter_rule = self._directional_rule("r-enter", RuleDirection.ENTER)
        exit_rule = self._directional_rule("r-exit", RuleDirection.EXIT)
        r, sm = self._build((None, 0), [enter_rule, exit_rule])

        await r.update_state("r-enter", "cam-001", True, "")
        await asyncio.sleep(0.05)
        assert sm.runtime_state(TASK_ID) is TaskRuntimeState.ON
        # 离开: exit 条件成立 → 退出。条件之后整夜保持为真。
        await r.update_state("r-exit", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()
        assert sm.runtime_state(TASK_ID) is TaskRuntimeState.OFF
        assert r.is_condition_satisfied(exit_rule.id) is True
        mock_send.reset_mock()

        r.force_cross_day_reset(TASK_ID)
        await asyncio.sleep(0.1)
        await r.drain()

        fired = [
            it.rule_id
            for call in mock_send.call_args_list
            for it in call.args[1]
        ]
        assert fired == []
        assert sm.runtime_state(TASK_ID) is TaskRuntimeState.OFF

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_duration_exit_rule_settles_target_before_flipping_off(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """配了 duration_seconds 的 exit 型 rule 走的是滑窗那条路, 不是瞬时翻转。

        那条路问许可闸之前没有兑现的话, 状态机先翻 off, timer 到点被"不在会话中"
        拦掉, 而条件已经被置真 —— 这一天的达标永久消失。
        """
        mock_send.return_value = True
        enter_rule = self._directional_rule("r-enter", RuleDirection.ENTER)
        exit_rule = self._directional_rule("r-exit", RuleDirection.EXIT)
        # duration_seconds 小于采样间隔 → 滑窗长度 1, 一帧就凑满
        exit_rule.duration_seconds = 1
        exit_rule.duration_ratio = 1.0
        ms = _make_milestone_rule()
        r, sm = self._build((60, 0), [enter_rule, exit_rule, ms])

        await r.update_state("r-enter", "cam-001", True, "")
        await asyncio.sleep(0.05)
        assert sm.runtime_state(TASK_ID) is TaskRuntimeState.ON

        # session 内累计跨过目标, 随后 exit 条件成立
        r._task_record_service.read_duration_target_state = MagicMock(
            return_value=(60, 60)
        )
        await r.update_state("r-exit", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()

        assert sm.runtime_state(TASK_ID) is TaskRuntimeState.OFF, "前提没成立: 没退出"
        assert _fired_events(mock_send).count(RuleEvent.TARGET_FIRED) == 1

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_state_machine_initiated_exit_drops_the_timer(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """重新配置的强制 on_exit 与手动注入也从 session 出来，timer 得撤。

        不撤的话它到点在 off 态喂真，条件锁死。
        """
        mock_send.return_value = True
        session_rule = Rule(
            id="r-ses", name="r-ses", task_id=TASK_ID, mode=RuleMode.STATE,
            direction=RuleDirection.SESSION, lifecycle=RuleLifecycle.PERMANENT,
            condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人"),
            on_enter_desc="开始", on_exit_desc="结束",
        )
        ms = _make_milestone_rule()
        r, _sm = self._build((60, 10), [session_rule, ms])
        await r.update_state("r-ses", "cam-001", True, "")
        await asyncio.sleep(0.05)
        assert ms.id in r.record_source._timers

        r.dispatch_task_action(TASK_ID, "on_exit")
        await r.drain()

        assert r.record_source._timers == {}


# ============================================================
# task 全貌校验：进路径 + session 独占（spec §9）
# ============================================================


def _directions_error(*directions):
    from miloco.rule.schema import task_rule_set_error

    return task_rule_set_error(list(directions))


class TestTaskRuleSetLegality:
    """一个 task 的 rule 集合什么时候合法 —— 只看方向的组合。"""

    def test_empty_task_is_legal(self):
        """装配是分步的，task 可以暂时一条 rule 都没有。"""
        assert _directions_error() is None

    def test_enter_only_is_legal(self):
        assert _directions_error(RuleDirection.ENTER) is None

    def test_session_alone_is_legal(self):
        assert _directions_error(RuleDirection.SESSION) is None

    def test_enter_plus_exit_is_legal(self):
        """非互反模式 —— 进出各一条是 spec 明列的合法形态，别判成非法。"""
        assert _directions_error(RuleDirection.ENTER, RuleDirection.EXIT) is None

    def test_session_with_milestone_is_legal(self):
        """milestone 不改状态，不参与独占（spec §9 的明文例外）。"""
        assert (
            _directions_error(RuleDirection.SESSION, RuleDirection.MILESTONE) is None
        )

    def test_exit_only_has_no_entry_path(self):
        assert "进路径" in _directions_error(RuleDirection.EXIT)

    def test_a_lone_milestone_rule_is_not_judged(self):
        """只挂达标规则不判非法 —— 它是派生物, 而且是装配的必经中间态。

        判非法的话每个配了达标通知的 task 都会经过它, 免责条款一放行, 这道闸对
        它们就永久失效 (spec §9「不能只有 milestone rule」因此不可执行)。
        """
        assert _directions_error(RuleDirection.MILESTONE) is None

    def test_exit_plus_milestone_has_no_entry_path(self):
        """两条都不构成进路径，凑一起也还是进不去。"""
        assert (
            "进路径"
            in _directions_error(RuleDirection.EXIT, RuleDirection.MILESTONE)
        )

    def test_session_with_enter_is_rejected(self):
        assert "独占" in _directions_error(
            RuleDirection.SESSION, RuleDirection.ENTER
        )

    def test_session_with_exit_is_rejected(self):
        assert "独占" in _directions_error(RuleDirection.SESSION, RuleDirection.EXIT)

    def test_two_sessions_are_rejected(self):
        assert "独占" in _directions_error(
            RuleDirection.SESSION, RuleDirection.SESSION
        )


class TestTaskRuleSetWiring:
    """三条写路径都跑这道校验，且只拦这次变更引入的非法。"""

    @staticmethod
    def _rule(rule_id, direction, task_id=TASK_ID, enabled=True):
        # 动作字段跟着 direction 走: 单方向的 rule 填 actions, session 填 on_enter_*
        # —— 与 _rule_action_slots 同一套口径, 混填会被一致性校验先拦掉。
        session = direction is RuleDirection.SESSION
        return Rule(
            id=rule_id,
            name=_name(task_id, rule_id),
            task_id=task_id,
            direction=direction,
            lifecycle=RuleLifecycle.PERMANENT,
            enabled=enabled,
            condition=_make_condition(),
            actions=[] if session else [_make_action()],
            on_enter_actions=[_make_action()] if session else [],
        )

    @pytest.mark.asyncio
    async def test_create_rejects_a_task_without_an_entry_path(
        self, service, mock_rule_repo
    ):
        mock_rule_repo.list_by_task.return_value = []
        with pytest.raises(ValidationException, match="进路径"):
            await service.create_rule(self._rule("r-exit", RuleDirection.EXIT))
        mock_rule_repo.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_rejects_losing_the_exit_path_under_a_target_action(
        self, service, mock_rule_repo, mock_task_repo
    ):
        """配了达标动作的 task 不能只剩 enter —— 达标一次都不会响。

        运行态恒 off, 达标信号被判成"不在会话中"; 更根上的原因是累计时长靠
        session-start / session-end 配对, 没有出路径就永远发不出 session-end。
        """
        mock_task_repo.get_full_view.return_value = {
            "actions": {"on_target_desc": "该休息啦"}
        }
        mock_rule_repo.list_by_task.return_value = []
        with pytest.raises(ValidationException, match="需要一条 direction=exit"):
            await service.create_rule(self._rule("r-enter", RuleDirection.ENTER))
        mock_rule_repo.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_allows_an_enter_rule_next_to_an_exit_one(
        self, service, mock_rule_repo, mock_task_repo
    ):
        """出路径还在就放行 —— 这道闸只挡"配了达标却没有出路径"。"""
        mock_task_repo.get_full_view.return_value = {
            "actions": {"on_target_desc": "该休息啦"}
        }
        mock_rule_repo.list_by_task.return_value = [
            self._rule("r-exit", RuleDirection.EXIT)
        ]
        await service.create_rule(self._rule("r-enter", RuleDirection.ENTER))
        mock_rule_repo.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_rejects_session_next_to_an_existing_rule(
        self, service, mock_rule_repo
    ):
        mock_rule_repo.list_by_task.return_value = [
            self._rule("r-enter", RuleDirection.ENTER)
        ]
        with pytest.raises(ValidationException, match="独占"):
            await service.create_rule(self._rule("r-ses", RuleDirection.SESSION))

    @pytest.mark.asyncio
    async def test_create_counts_a_disabled_rule_as_an_entry_path(
        self, service, mock_rule_repo
    ):
        """enabled 是用户意图（§19.9），停用一条进方向的规则不该让配置变非法。"""
        mock_rule_repo.list_by_task.return_value = [
            self._rule("r-enter", RuleDirection.ENTER, enabled=False)
        ]
        assert await service.create_rule(self._rule("r-exit", RuleDirection.EXIT))

    @pytest.mark.asyncio
    async def test_update_rejects_a_direction_change_that_removes_the_entry_path(
        self, service, mock_rule_repo
    ):
        previous = self._rule("r1", RuleDirection.ENTER)
        mock_rule_repo.get_by_id.return_value = previous
        mock_rule_repo.list_by_task.return_value = [previous]
        with pytest.raises(ValidationException, match="进路径"):
            await service.update_rule(self._rule("r1", RuleDirection.EXIT))
        mock_rule_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_patch_rejects_a_direction_change_that_removes_the_entry_path(
        self, service, mock_rule_repo
    ):
        previous = self._rule("r1", RuleDirection.ENTER)
        mock_rule_repo.get_by_id.return_value = previous
        mock_rule_repo.list_by_task.return_value = [previous]
        with pytest.raises(ValidationException, match="进路径"):
            await service.patch_rule("r1", RuleUpdate(direction=RuleDirection.EXIT))
        mock_rule_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_moving_a_rule_into_a_session_task_is_rejected(
        self, service, mock_rule_repo
    ):
        """改挂过来的那条不算目标 task 的「变更前」。

        算进去的话「变更前」也带着这条、也非法，这次真撞上的独占就被当成存量放行。
        """
        previous = self._rule("r1", RuleDirection.ENTER, task_id="task-other")
        mock_rule_repo.get_by_id.return_value = previous
        mock_rule_repo.list_by_task.return_value = [
            self._rule("r-ses", RuleDirection.SESSION)
        ]
        with pytest.raises(ValidationException, match="独占"):
            await service.update_rule(self._rule("r1", RuleDirection.ENTER))

    @pytest.mark.asyncio
    async def test_an_already_illegal_task_can_still_be_patched(
        self, service, mock_rule_repo
    ):
        """拦住已经非法的等于把「改回合法」和「先停用它」两条自救路一起堵死。"""
        previous = self._rule("r1", RuleDirection.EXIT)
        mock_rule_repo.get_by_id.return_value = previous
        mock_rule_repo.list_by_task.return_value = [previous]
        assert await service.patch_rule("r1", RuleUpdate(enabled=False))


class TestExitRuleEdgeTimestamp:
    """exit 型 rule 的"条件刚成立"走 ENTERED 边沿, 但它是 session 终点。

    记账那步的 CLI 是按 metadata 的键名分派的 (_FIRE_PREAMBLE_WITH_RECORD 第 2 步),
    键名跟着边沿走就会塞成起点 —— agent 把刚该结束的计时段又开一遍, 而 session-end
    永远等不到它那一行, 计时段永不关闭。
    """

    @staticmethod
    def _exit_rule(rule_id, duration_seconds=None):
        return Rule(
            id=rule_id,
            name=_name(TASK_ID, rule_id),
            task_id=TASK_ID,
            direction=RuleDirection.EXIT,
            lifecycle=RuleLifecycle.PERMANENT,
            condition=_make_condition(),
            action_descriptions=["孩子离开书桌"],
            duration_seconds=duration_seconds,
            duration_ratio=1.0,
        )

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_instant_edge_reports_exited_at(self, mock_send, runner):
        mock_send.return_value = True
        runner.add_rule(self._exit_rule("rule-exit-ts"))

        await runner.update_state("rule-exit-ts", "cam-001", True, "")
        await runner.drain()

        info = _extra_info(_last_dispatched_prompt(mock_send, RuleEvent.ENTERED))
        assert "actual_exited_at" in info, f"退出槽的边沿缺 actual_exited_at：{info}"
        assert "actual_started_at" not in info, (
            f"退出槽不该出现 actual_started_at，agent 会去 session-start：{info}"
        )

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_duration_path_reports_exited_at(self, mock_send, runner):
        """滑窗那条路是另一处 _spawn_fire，得单独钉。"""
        mock_send.return_value = True
        # duration_seconds == sample_interval(3) → maxlen 1 → 首帧即达标
        runner.add_rule(self._exit_rule("rule-exit-dur", duration_seconds=3))

        await runner.update_state("rule-exit-dur", "cam-001", True, "")
        await runner.drain()

        info = _extra_info(_last_dispatched_prompt(mock_send, RuleEvent.ENTERED))
        assert "actual_exited_at" in info, f"滑窗路径缺 actual_exited_at：{info}"
        assert "actual_started_at" not in info, (
            f"滑窗路径不该出现 actual_started_at：{info}"
        )

    def test_target_slot_gets_no_edge_timestamp(self):
        """达标不是 session 边界, 一个键都不能给。

        当前 _dispatch_event 在达标那一支提前返回、根本走不到这里, 所以这条只能
        直接测函数。它守的是"哪天把那个提前返回去掉了, 达标也不会突然多出一个
        session-start"。
        """
        from miloco.rule.runner import _edge_timestamp
        from miloco.task.state_machine import ActionSlot

        assert _edge_timestamp(ActionSlot.ON_TARGET, "2026-09-02T10:00:00+08:00") == {}
        assert _edge_timestamp(None, "2026-09-02T10:00:00+08:00") == {}
