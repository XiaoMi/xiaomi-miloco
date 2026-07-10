# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Tests for Rule modules: schema, runner, service
"""

import asyncio
import json
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.middleware.exceptions import (
    BusinessException,
    ConflictException,
    ResourceNotFoundException,
    ValidationException,
)
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleActionExecuteResult,
    RuleCondition,
    RuleConditionUpdate,
    RuleEvent,
    RuleExecuteResult,
    RuleLifecycle,
    RuleLog,
    RuleMode,
    RuleUpdate,
)
from miloco.rule.service import RuleService

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


# ---- Fixtures ----


@pytest.fixture
def mock_miot_proxy():
    proxy = AsyncMock()
    proxy.get_camera_dids = AsyncMock(return_value=["cam-001", "cam-002"])
    proxy.get_device_properties = AsyncMock(return_value=[{"code": 0, "value": False}])
    proxy.set_device_properties = AsyncMock(return_value=[{"code": 0}])
    proxy.call_device_action = AsyncMock(return_value={"code": 0})
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
    repo.delete_link_by_ref = MagicMock(return_value=1)
    # 方案 P：rule create 前置校验 task 存在；mock 默认放行
    repo.task_exists = MagicMock(return_value=True)
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
    async def test_create_event_without_any_action_raises(self, service):
        rule = _make_static_rule(rule_id="", actions=[])
        with pytest.raises(
            ValidationException,
            match=r"event mode requires one of actions / action_descriptions",
        ):
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
        mock_rule_repo.exists.return_value = False
        rule = _make_static_rule(rule_id="missing")
        with pytest.raises(ResourceNotFoundException):
            await service.update_rule(rule)

    @pytest.mark.asyncio
    async def test_update_rule_name_conflict(self, service, mock_rule_repo):
        mock_rule_repo.exists_by_name.return_value = True
        rule = _make_static_rule(rule_id="r1", name="dup-name")
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
        mock_task_repo.delete_link_by_ref.assert_called_once_with("rule", "r1")

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
    async def test_update_with_compliant_query_succeeds(self, service):
        """合规 query（进行时状态/可观测动作描述）不被 phrasing 校验误拦。"""
        rule = _make_static_rule(
            rule_id="r1",
            condition=_make_condition(query="用户正在做出喝水动作"),
        )
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
        """set_device_properties 返回 code!=0 → action 标记 false 并附 miot_failed。"""
        mock_miot_proxy.set_device_properties = AsyncMock(
            return_value=[{"code": -1, "value": None}]
        )
        action = _make_action(idempotent=False)
        rule = _make_static_rule(rule_id="rule-bad-code", actions=[action])
        runner.add_rule(rule)

        result = await runner.trigger_rule("rule-bad-code", "测试")
        assert result.action_results[0].result is False
        assert "miot_failed" in (result.action_results[0].error or "")

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


class TestRuleRunnerOnTargetDesc:
    """spec §9 测试矩阵 T1-T8 的核心子集（不真等分钟级时延）。"""

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_target_immediate_when_already_accumulated(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """T8: ENTERED 时 accumulated 已 ≥ target → 立即 fire on_target。"""
        mock_send.return_value = True
        # target=60min, accumulated=60min → remaining=0
        r = _make_runner_with_record(
            (60, 60), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-immediate")
        r.add_rule(rule)
        await r.update_state("rule-tgt-immediate", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()
        events = [
            it.event
            for call in mock_send.call_args_list
            for it in call.args[1]
        ]
        assert RuleEvent.TARGET_FIRED in events

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_target_scheduled_and_fired_after_delay(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """T1: target=1min, accumulated=0 → schedule timer，到点 fire on_target。

        测试不等真 60s：mock target=1s，await 1.2s 后验 TARGET_FIRED。
        """
        mock_send.return_value = True
        # target=1min（被 minutes 单位放大为 60s）；为加速测试，直接调内部 helper
        r = _make_runner_with_record(
            (60, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-sched")
        r.add_rule(rule)
        # 直接走 ENTERED schedule 路径
        await r.update_state("rule-tgt-sched", "cam-001", True, "")
        # ENTERED 已 spawn timer，但 60min 太长——手动 cancel + 用短延迟重 schedule
        # 通过覆盖 task_record_service 让 remaining=0 已在上面覆盖；
        # 这个 case 通过直接调 _await_and_fire_target 验达标 fire 即可
        assert "rule-tgt-sched" in r._target_timers
        # 取消长 timer，模拟到点：直接调 _await_and_fire_target with 0 delay
        _sched_timer = r._state["rule-tgt-sched"].target_timer
        r._state["rule-tgt-sched"].target_timer = None
        _sched_timer.cancel()
        # 直接调达标 fire
        await r._await_and_fire_target(
            rule, ["cam-001"], "ctx", 0.0, target_minutes=60,
        )
        await r.drain()
        events = [
            it.event
            for call in mock_send.call_args_list
            for it in call.args[1]
        ]
        assert RuleEvent.TARGET_FIRED in events
        assert rule.id in r._target_fired

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_target_dropped_when_state_false_at_fire(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """T3: timer 到点但 condition 已 false → drop，不 fire on_target。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            (60, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-drop")
        r.add_rule(rule)
        await r.update_state("rule-tgt-drop", "cam-001", True, "")
        _drop_timer = r._state["rule-tgt-drop"].target_timer
        r._state["rule-tgt-drop"].target_timer = None
        _drop_timer.cancel()
        # 模拟 condition 已 false
        r._state[rule.id].last_rule_state = False
        await r._await_and_fire_target(
            rule, ["cam-001"], "ctx", 0.0, target_minutes=60,
        )
        await r.drain()
        events = [
            it.event
            for call in mock_send.call_args_list
            for it in call.args[1]
        ]
        assert RuleEvent.TARGET_FIRED not in events
        assert rule.id not in r._target_fired

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_exited_cancels_timer_keeps_fired_marker(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """EXITED 真 fire 时 cancel pending timer，但保留 _target_fired（record-session
        维度，跨日才清）——同一天 EXITED 后再 ENTERED 不该重复 fire。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            (60, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(
            rule_id="rule-tgt-exit", exit_debounce_seconds=0,
        )
        r.add_rule(rule)
        await r.update_state("rule-tgt-exit", "cam-001", True, "")
        assert "rule-tgt-exit" in r._target_timers
        # 模拟 timer 已 fire（手动设 fired 标记）
        r._state[rule.id].target_fired = True
        # EXITED
        await r.update_state("rule-tgt-exit", "cam-001", False, "")
        await r.update_state("rule-tgt-exit", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()
        assert "rule-tgt-exit" not in r._target_timers
        # fired 标记必须保留，防同一天重复 fire
        assert rule.id in r._target_fired

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_same_day_reentered_does_not_refire_target(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """同一天 fire on_target 后 EXITED → 再 ENTERED（accumulated 仍 ≥ target），
        _schedule_target_timer_if_needed 守卫早返，不重复 fire。"""
        mock_send.return_value = True
        # mock 一直返回 (60, 65)（达标后用户继续累积，accumulated 涨到 65）
        r = _make_runner_with_record(
            (60, 65), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(
            rule_id="rule-tgt-noref", exit_debounce_seconds=0,
        )
        r.add_rule(rule)
        # 第一次 ENTERED → 立即 fire（accumulated ≥ target）
        await r.update_state("rule-tgt-noref", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()
        fire_count_first = sum(
            1 for call in mock_send.call_args_list
            for it in call.args[1]
            if it.event == RuleEvent.TARGET_FIRED
        )
        assert fire_count_first == 1
        # EXITED
        await r.update_state("rule-tgt-noref", "cam-001", False, "")
        await r.update_state("rule-tgt-noref", "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()
        # 再 ENTERED（同一天，accumulated 仍 ≥ target）
        await r.update_state("rule-tgt-noref", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()
        fire_count_after = sum(
            1 for call in mock_send.call_args_list
            for it in call.args[1]
            if it.event == RuleEvent.TARGET_FIRED
        )
        # 关键断言：第二次 ENTERED 不重复 fire
        assert fire_count_after == 1, (
            f"expected 1 TARGET_FIRED, got {fire_count_after}"
        )

    @pytest.mark.asyncio
    async def test_no_target_desc_no_timer(
        self, mock_miot_proxy, mock_log_repo,
    ):
        """on_target_desc=None → ENTERED 不 schedule timer。"""
        r = _make_runner_with_record(
            (60, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule(
            rule_id="rule-no-tgt",
            on_enter_desc="开始计时\ntask_id=no-tgt",
            on_exit_desc="结束计时\ntask_id=no-tgt",
            exit_debounce_seconds=0,
        )
        rule.on_target_desc = None
        r.add_rule(rule)
        await r.update_state("rule-no-tgt", "cam-001", True, "")
        await r.drain()
        assert "rule-no-tgt" not in r._target_timers

    @pytest.mark.asyncio
    async def test_target_skipped_when_no_duration_record(
        self, mock_miot_proxy, mock_log_repo,
    ):
        """read_duration_target_state=None → 不 schedule。"""
        r = _make_runner_with_record(
            None, mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-none")
        r.add_rule(rule)
        await r.update_state("rule-tgt-none", "cam-001", True, "")
        await r.drain()
        assert "rule-tgt-none" not in r._target_timers

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_force_cross_day_reset_fires_exit_then_enter(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """T6: 跨日 force-reset 真 fire on_exit + on_enter + 重 schedule timer。"""
        mock_send.return_value = True
        # 第一次进入时 accumulated=30，第二次（跨日后）accumulated=0
        states = iter([(60, 30), (60, 0)])
        mock_svc = MagicMock()
        mock_svc.detect_record_kind = MagicMock(return_value="duration")
        mock_svc.read_duration_target_state = MagicMock(
            side_effect=lambda task_id: next(states)
        )
        r = RuleRunner(
            rules=[],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            task_record_service=mock_svc,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-xday")
        r.add_rule(rule)
        await r.update_state("rule-tgt-xday", "cam-001", True, "")
        await asyncio.sleep(0.05)
        assert "rule-tgt-xday" in r._target_timers
        # 跨日触发
        r.force_cross_day_reset(rule.task_id)
        await asyncio.sleep(0.05)
        await r.drain()
        events = [
            it.event
            for call in mock_send.call_args_list
            for it in call.args[1]
        ]
        # 应该看到 ENTERED（首次） + EXITED（跨日） + ENTERED（跨日 force）
        assert events.count(RuleEvent.ENTERED) >= 2
        assert RuleEvent.EXITED in events
        # 重 schedule 后 timer 仍在（新一天 accumulated=0 → remaining=60min）
        assert "rule-tgt-xday" in r._target_timers

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_cross_day_reset_clears_fired_marker(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """跨日 force-reset 必须清 _target_fired，新一天才能再 fire。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            (60, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-xday-fired")
        r.add_rule(rule)
        await r.update_state("rule-tgt-xday-fired", "cam-001", True, "")
        await asyncio.sleep(0.05)
        # 模拟已 fire 过 on_target
        r._state[rule.id].target_fired = True
        # 跨日 reset
        r.force_cross_day_reset(rule.task_id)
        await asyncio.sleep(0.05)
        await r.drain()
        # fired 必须清掉
        assert rule.id not in r._target_fired
        # 新一天 timer 重新挂上
        assert "rule-tgt-xday-fired" in r._target_timers

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_cross_day_reset_fires_target_when_pre_state_reached(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """旧一天累计已达标但 timer 还没到点 → 跨日兜底 fire TARGET。"""
        mock_send.return_value = True
        # 新一天 state=(5, 0)，旧一天 pre_state=(5, 5) 已达标
        r = _make_runner_with_record(
            (5, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-xday-reach")
        r.add_rule(rule)
        await r.update_state("rule-tgt-xday-reach", "cam-001", True, "")
        await asyncio.sleep(0.05)
        # ENTERED 时 state=(5,0) 未达标，未 fire TARGET
        assert rule.id not in r._target_fired
        r.force_cross_day_reset(rule.task_id, pre_rollover_state=(5, 5))
        await asyncio.sleep(0.05)
        await r.drain()
        target_count = sum(
            1
            for call in mock_send.call_args_list
            for it in call.args[1]
            if it.event == RuleEvent.TARGET_FIRED
        )
        assert target_count == 1

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_cross_day_reset_skips_target_when_pre_state_below(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """旧一天累计 < target → 跨日不 fire TARGET。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            (5, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-xday-below")
        r.add_rule(rule)
        await r.update_state("rule-tgt-xday-below", "cam-001", True, "")
        await asyncio.sleep(0.05)
        r.force_cross_day_reset(rule.task_id, pre_rollover_state=(5, 2))
        await asyncio.sleep(0.05)
        await r.drain()
        events = [
            it.event
            for call in mock_send.call_args_list
            for it in call.args[1]
        ]
        assert RuleEvent.TARGET_FIRED not in events

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_cross_day_reset_skips_target_when_pre_state_none(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """pre_rollover_state 缺省 → 不调兜底（向后兼容）。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            (5, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-xday-none")
        r.add_rule(rule)
        await r.update_state("rule-tgt-xday-none", "cam-001", True, "")
        await asyncio.sleep(0.05)
        r.force_cross_day_reset(rule.task_id)
        await asyncio.sleep(0.05)
        await r.drain()
        events = [
            it.event
            for call in mock_send.call_args_list
            for it in call.args[1]
        ]
        assert RuleEvent.TARGET_FIRED not in events


class TestRuleOnTargetMetadata:
    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_immediate_fire_metadata_in_prompt(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """已达标立即 fire：prompt_text 末尾含 target_minutes / actual_target_at /
        accumulated_at_fire 三个 metadata 行。"""
        mock_send.return_value = True
        r = _make_runner_with_record(
            (60, 75), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-meta-imm")
        r.add_rule(rule)
        await r.update_state("rule-tgt-meta-imm", "cam-001", True, "")
        await asyncio.sleep(0.05)
        await r.drain()
        prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.TARGET_FIRED)
        info = _extra_info(prompt)
        assert info.get("target_minutes") == 60
        assert info.get("accumulated_at_fire") == 75
        assert "actual_target_at" in info

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_timer_fire_metadata_in_prompt(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """timer 到点 fire：prompt_text 末尾含三 metadata 行；
        accumulated_at_fire 取 fire 时刻最新读到的累计值。"""
        mock_send.return_value = True
        # 起 timer 时 (60, 0)，fire 时读到 (60, 60)（mock 同一返回值即可）
        r = _make_runner_with_record(
            (60, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(rule_id="rule-tgt-meta-timer")
        r.add_rule(rule)
        await r.update_state("rule-tgt-meta-timer", "cam-001", True, "")
        _meta_timer = r._state["rule-tgt-meta-timer"].target_timer
        r._state["rule-tgt-meta-timer"].target_timer = None
        _meta_timer.cancel()
        # 改 mock，模拟 timer 到点时 accumulated 已涨到 60
        r._task_record_service.read_duration_target_state = MagicMock(
            return_value=(60, 60)
        )
        await r._await_and_fire_target(
            rule, ["cam-001"], "ctx", 0.0, target_minutes=60,
        )
        await r.drain()
        prompt = _last_dispatched_prompt(mock_send, event=RuleEvent.TARGET_FIRED)
        info = _extra_info(prompt)
        assert info.get("target_minutes") == 60
        assert info.get("accumulated_at_fire") == 60
        assert "actual_target_at" in info


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


class TestRuleServiceOnTargetDescValidation:
    """on_target_desc 配套校验：报错信息按当前 record 状态分三 case。"""

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
            svc._validate_on_target_desc_compat(rule)
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
            svc._validate_on_target_desc_compat(rule)
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
            svc._validate_on_target_desc_compat(rule)
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
            svc._validate_on_target_desc_compat(rule)
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
        svc._validate_on_target_desc_compat(rule)

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
        svc._validate_on_target_desc_compat(rule)
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


class TestRuleExitDebounceTargetCheck:
    """EXIT debounce 完成、cancel target timer 前的兜底：若此刻 accumulated 已
    ≥ target，必须 fire TARGET 兑现累计达标承诺，否则 60s debounce 窗口内跨过
    target 的累计永远丢失通知。"""

    @staticmethod
    def _make_runner_with_dynamic_state(
        initial_state, miot_proxy, log_repo,
    ):
        """构造 read_duration_target_state 可在测试运行时切换返回值的 runner。
        返回 (runner, state_holder)；测试代码改 state_holder["value"] 即可让
        后续 read 返回新值。"""
        mock_svc = MagicMock()
        state_holder = {"value": initial_state}
        mock_svc.read_duration_target_state = MagicMock(
            side_effect=lambda task_id: state_holder["value"]
        )
        mock_svc.detect_record_kind = MagicMock(
            return_value=None if initial_state is None else "duration"
        )
        r = RuleRunner(
            rules=[],
            miot_proxy=miot_proxy,
            rule_log_repo=log_repo,
            task_record_service=mock_svc,
        )
        return r, state_holder

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_exit_fires_target_when_accumulated_reaches_during_debounce(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """ENTERED 时未达标起 timer，EXIT 时跨过 target → 兜底 fire TARGET + EXITED。"""
        mock_send.return_value = True
        r, state_holder = self._make_runner_with_dynamic_state(
            (5, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(
            rule_id="rule-exit-target-reached", exit_debounce_seconds=0,
        )
        r.add_rule(rule)
        await r.update_state(rule.id, "cam-001", True, "")
        await asyncio.sleep(0.05)
        assert rule.id in r._target_timers
        # 模拟 debounce 窗口内累计跨过 target
        state_holder["value"] = (5, 5)
        await r.update_state(rule.id, "cam-001", False, "")
        await r.update_state(rule.id, "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()
        events = [
            it.event
            for call in mock_send.call_args_list
            for it in call.args[1]
        ]
        assert RuleEvent.TARGET_FIRED in events
        assert RuleEvent.EXITED in events
        assert rule.id in r._target_fired

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_exit_skips_target_when_accumulated_below_target(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """EXIT 时 accumulated 仍 < target → 不 fire TARGET，只 fire EXITED + cancel timer。"""
        mock_send.return_value = True
        r, state_holder = self._make_runner_with_dynamic_state(
            (5, 0), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(
            rule_id="rule-exit-below-target", exit_debounce_seconds=0,
        )
        r.add_rule(rule)
        await r.update_state(rule.id, "cam-001", True, "")
        await asyncio.sleep(0.05)
        state_holder["value"] = (5, 2)
        await r.update_state(rule.id, "cam-001", False, "")
        await r.update_state(rule.id, "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()
        events = [
            it.event
            for call in mock_send.call_args_list
            for it in call.args[1]
        ]
        assert RuleEvent.TARGET_FIRED not in events
        assert RuleEvent.EXITED in events
        assert rule.id not in r._target_fired
        assert rule.id not in r._target_timers

    @pytest.mark.asyncio
    @patch("miloco.rule.runner.dispatch_event", new_callable=AsyncMock)
    async def test_exit_does_not_refire_target_when_already_fired(
        self, mock_send, mock_miot_proxy, mock_log_repo,
    ):
        """ENTERED 时已达标 fire 过 TARGET → EXIT 兜底检查不重复 fire。"""
        mock_send.return_value = True
        r, state_holder = self._make_runner_with_dynamic_state(
            (5, 5), mock_miot_proxy, mock_log_repo,
        )
        rule = _make_state_rule_with_target(
            rule_id="rule-exit-already-fired", exit_debounce_seconds=0,
        )
        r.add_rule(rule)
        await r.update_state(rule.id, "cam-001", True, "")
        await asyncio.sleep(0.05)
        assert rule.id in r._target_fired
        await r.update_state(rule.id, "cam-001", False, "")
        await asyncio.sleep(0.05)
        await r.drain()
        target_count = sum(
            1
            for call in mock_send.call_args_list
            for it in call.args[1]
            if it.event == RuleEvent.TARGET_FIRED
        )
        assert target_count == 1


# ============================================================
# Per-device 状态机隔离(层 2): perception client 调用侧把 source_did 从字符串
# "perception" 换成真 did 后,runner 内部 `_last_source_state[(rule_id, did)]`
# 多桶天然 fan-out。下列 case lock 住 OR 聚合 / pending_exit 隔离 / duration 跨
# 源 OR 三条核心语义,防止后续 regression。
# ============================================================


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

