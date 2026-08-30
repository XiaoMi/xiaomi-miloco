# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""场景联动任务测试：RuleRunner 的 max_dwell_seconds 到期自动退出 + SceneTaskService CRUD。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.middleware.exceptions import ValidationException
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleCondition,
    RuleLifecycle,
    RuleMode,
    TriggerOutcome,
)
from miloco.scene_task.schema import (
    SceneTaskCreateRequest,
    SceneTaskUpdateRequest,
)
from miloco.scene_task.service import SceneTaskService
from miloco.task.schema import TaskFullView, TaskUpdateRequest

TASK_ID = 'test_task'
MOCK_HOME_ID = 'home-1'


def _scene_action(scene_id='scene-1', cooldown=5):
    return RuleAction(
        did=scene_id, iid='scene', idempotent=False, cooldown_minutes=cooldown,
    )


def _make_scene_state_rule(
    rule_id='rule-scene',
    task_id=TASK_ID,
    enter_scene='scene-1',
    exit_scene='scene-2',
    max_dwell_seconds=None,
    exit_debounce_seconds=0,
    duration_seconds=None,
    enabled=True,
):
    return Rule(
        id=rule_id,
        name=f'[{task_id}] scene rule',
        task_id=task_id,
        mode=RuleMode.STATE,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=enabled,
        condition=RuleCondition(
            perceive_device_ids=['cam-001'], query='有人在看书'
        ),
        on_enter_actions=[_scene_action(enter_scene)] if enter_scene else [],
        on_exit_actions=[_scene_action(exit_scene)] if exit_scene else [],
        exit_debounce_seconds=exit_debounce_seconds,
        max_dwell_seconds=max_dwell_seconds,
        # 进入确认时间：duration_ratio 恒 1.0（严格连续满足才触发进入场景）
        duration_seconds=duration_seconds,
        duration_ratio=1.0 if duration_seconds else None,
    )


# ---- RuleRunner dwell timer ----


@pytest.fixture
def mock_miot_proxy():
    proxy = AsyncMock()
    proxy.get_all_scenes = AsyncMock(
        return_value={
            'scene-1': SimpleNamespace(home_id=MOCK_HOME_ID, scene_name='开灯'),
            'scene-2': SimpleNamespace(home_id=MOCK_HOME_ID, scene_name='关灯'),
        }
    )
    proxy.execute_miot_scene = AsyncMock(return_value=True)
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
    repo.create = MagicMock(return_value='log-id')
    return repo


@pytest.fixture
def mock_task_record_service():
    svc = MagicMock()
    svc.detect_record_kind = MagicMock(return_value=None)
    svc.read_duration_target_state = MagicMock(return_value=None)
    return svc


@pytest.fixture
def runner(mock_miot_proxy, mock_log_repo, mock_task_record_service):
    return RuleRunner(
        rules=[_make_scene_state_rule(max_dwell_seconds=1)],
        miot_proxy=mock_miot_proxy,
        rule_log_repo=mock_log_repo,
        task_record_service=mock_task_record_service,
    )


@pytest.mark.asyncio
async def test_dwell_timer_scheduled_on_enter(runner):
    out = await runner.update_state('rule-scene', 'cam-001', True)
    await runner.drain()
    assert out is TriggerOutcome.FIRED
    # ENTERED 后应起了 dwell timer
    assert 'rule-scene' in runner._dwell_timers


@pytest.mark.asyncio
async def test_dwell_expiry_forces_exit_and_fires_exit_scene(runner, mock_miot_proxy):
    await runner.update_state('rule-scene', 'cam-001', True)
    await runner.drain()
    assert mock_miot_proxy.execute_miot_scene.await_count == 1  # enter 场景

    # max_dwell=1s：睡过到点，应强制 EXITED 并触发 exit 场景
    await asyncio.sleep(1.2)
    await runner.drain()
    calls = [c.args[0] for c in mock_miot_proxy.execute_miot_scene.await_args_list]
    assert calls == ['scene-1', 'scene-2'], f'enter 后应自动触发 exit 场景: {calls}'
    assert not runner._state['rule-scene'].last_rule_state
    # 退出后重新进入 → 再次触发 enter 场景（冷却 5min 内不重复，但状态机可重入）
    out = await runner.update_state('rule-scene', 'cam-001', True)
    assert out is TriggerOutcome.FIRED


@pytest.mark.asyncio
async def test_dwell_cancelled_on_natural_exit(runner, mock_miot_proxy):
    await runner.update_state('rule-scene', 'cam-001', True)
    await runner.drain()
    assert 'rule-scene' in runner._dwell_timers
    # 条件翻转 False（debounce=0 直接确认退出）→ dwell timer 应被取消
    await runner.update_state('rule-scene', 'cam-001', False)
    await runner.update_state('rule-scene', 'cam-001', False)
    await runner.drain()
    assert 'rule-scene' not in runner._dwell_timers
    # 睡过原定到点时间，不应有额外 exit 场景
    await asyncio.sleep(1.2)
    await runner.drain()
    assert mock_miot_proxy.execute_miot_scene.await_count == 2  # enter + 自然 exit


@pytest.mark.asyncio
async def test_dwell_noop_when_already_exited(runner, mock_miot_proxy):
    # max_dwell=1 但状态在到点前已自然退出 → 到点后不再触发 exit 场景
    rule = _make_scene_state_rule(
        rule_id='rule-dwell-noop', max_dwell_seconds=1
    )
    runner.add_rule(rule)
    await runner.update_state('rule-dwell-noop', 'cam-001', True)
    await runner.update_state('rule-dwell-noop', 'cam-001', False)
    await runner.update_state('rule-dwell-noop', 'cam-001', False)
    await runner.drain()
    await asyncio.sleep(1.2)
    await runner.drain()
    assert mock_miot_proxy.execute_miot_scene.await_count == 2


@pytest.mark.asyncio
async def test_event_mode_does_not_schedule_dwell(runner):
    rule = Rule(
        id='rule-event-dwell',
        name='[t] event',
        task_id='t',
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=RuleCondition(perceive_device_ids=['cam-001'], query='x'),
        actions=[_scene_action('scene-1')],
        max_dwell_seconds=60,
    )
    runner.add_rule(rule)
    await runner.update_state('rule-event-dwell', 'cam-001', True)
    await runner.drain()
    assert (
        'rule-event-dwell' not in runner._dwell_timers
    ), 'event mode 不处理 EXITED，dwell 计时无意义'


@pytest.mark.asyncio
async def test_manual_trigger_schedules_dwell(runner):
    await runner.trigger_rule('rule-scene', 'web_debug')
    await runner.drain()
    assert (
        'rule-scene' in runner._dwell_timers
    ), '手动触发与真实 ENTERED 同口径：应起到期计时'


# ---- 进入确认时间（enter_debounce → rule duration_seconds） ----


@pytest.fixture
def confirm_runner(mock_miot_proxy, mock_log_repo, mock_task_record_service):
    """sample_interval=3s、duration_seconds=6 → maxlen=2 的进入确认窗口。"""
    return RuleRunner(
        rules=[
            _make_scene_state_rule(
                rule_id='rule-confirm', duration_seconds=6,
                exit_debounce_seconds=0,
            )
        ],
        miot_proxy=mock_miot_proxy,
        rule_log_repo=mock_log_repo,
        sample_interval_seconds=3.0,
        task_record_service=mock_task_record_service,
    )


@pytest.mark.asyncio
async def test_enter_confirm_single_true_does_not_fire(
    confirm_runner, mock_miot_proxy
):
    """单次误识别（一帧 True 后不再满足）→ 不触发进入场景。"""
    with patch('miloco.rule.runner.time.time') as mt:
        mt.return_value = 100.0
        out = await confirm_runner.update_state('rule-confirm', 'cam-001', True)
    await confirm_runner.drain()
    assert out is TriggerOutcome.COUNTING
    assert mock_miot_proxy.execute_miot_scene.await_count == 0


@pytest.mark.asyncio
async def test_enter_confirm_continuous_true_fires_after_window(
    confirm_runner, mock_miot_proxy
):
    """条件持续满足 6s（两个采样周期全 True）→ 达标触发进入场景。"""
    with patch('miloco.rule.runner.time.time') as mt:
        mt.return_value = 100.0
        await confirm_runner.update_state('rule-confirm', 'cam-001', True)
        mt.return_value = 103.0
        out = await confirm_runner.update_state('rule-confirm', 'cam-001', True)
    await confirm_runner.drain()
    assert out is TriggerOutcome.FIRED
    assert mock_miot_proxy.execute_miot_scene.await_count == 1
    calls = [c.args[0] for c in mock_miot_proxy.execute_miot_scene.await_args_list]
    assert calls == ['scene-1']


# ---- SceneTaskService ----


def _task_view(task_id=TASK_ID, description='床上看书自动开灯'):
    return TaskFullView(
        task_id=task_id,
        description=description,
        status='active',
        paused_at=None,
        created_at='2026-01-01T00:00:00+08:00',
        rule_briefs=[],
        cron_refs=[],
    )


@pytest.fixture
def rule_service_mock():
    svc = MagicMock()
    svc.create_rule = AsyncMock(return_value='rule-new')
    svc.patch_rule = AsyncMock(return_value=True)
    svc.trigger_rule = AsyncMock(return_value=object())
    # get_all_rules 真身是 async（每次现查 DB）——用 AsyncMock 才能在测试里暴露漏 await
    svc.get_all_rules = AsyncMock(return_value=[_make_scene_state_rule()])
    return svc


@pytest.fixture
def task_service_mock():
    svc = MagicMock()
    svc.get_full_view = MagicMock(return_value=_task_view())
    svc.create_task = MagicMock()
    svc.update_description = MagicMock(return_value=True)
    svc.enable_task = MagicMock()
    svc.disable_task = MagicMock()
    svc.delete_task = MagicMock(return_value=object())
    return svc


@pytest.fixture
def svc(rule_service_mock, task_service_mock, mock_miot_proxy):
    return SceneTaskService(
        rule_service=rule_service_mock,
        task_service=task_service_mock,
        miot_proxy=mock_miot_proxy,
    )


def _create_req(**overrides):
    base = dict(
        description='床上看书自动开灯',
        perceive_device_ids=['cam-001'],
        query='有人在床上看书',
        enter_scene_id='scene-1',
        exit_scene_id='scene-2',
        cooldown_minutes=5,
        exit_debounce_seconds=60,
        max_dwell_seconds=60,
    )
    base.update(overrides)
    return SceneTaskCreateRequest(**base)


@pytest.mark.asyncio
async def test_create_builds_task_and_state_rule(svc, rule_service_mock, task_service_mock):
    created: dict = {}

    def _capture(rule):
        created['rule'] = rule
        return 'rule-new'

    rule_service_mock.create_rule = AsyncMock(side_effect=_capture)
    # create 末尾会回查 get() → _find_scene_rule 按 task_id 找刚建的 rule
    rule_service_mock.get_all_rules = AsyncMock(
        side_effect=lambda enabled_only=False: [created['rule']]
        if 'rule' in created else [],
    )
    view = await svc.create(_create_req())
    # task 占位 + rule 创建
    task_service_mock.create_task.assert_called_once()
    created_task = task_service_mock.create_task.call_args.args[0]
    assert created_task.description == '床上看书自动开灯'
    rule_service_mock.create_rule.assert_awaited_once()
    rule = rule_service_mock.create_rule.await_args.args[0]
    assert rule.mode is RuleMode.STATE
    assert rule.max_dwell_seconds == 60
    assert rule.exit_debounce_seconds == 60
    # 默认进入确认时间 0 = 立即触发 → 不设 duration 滑窗
    assert rule.duration_seconds is None
    assert [a.did for a in rule.on_enter_actions] == ['scene-1']
    assert [a.did for a in rule.on_exit_actions] == ['scene-2']
    # 返回视图带场景名
    assert view.enter_scene_name == '开灯'
    assert view.exit_scene_name == '关灯'
    assert view.enter_debounce_seconds == 0


@pytest.mark.asyncio
async def test_create_maps_enter_confirm_to_rule_duration(
    svc, rule_service_mock
):
    """进入确认时间 >0 → rule 走 duration 前置确认门槛（ratio 恒 1.0 严格连续）。"""
    created: dict = {}

    def _capture(rule):
        created['rule'] = rule
        return 'rule-new'

    rule_service_mock.create_rule = AsyncMock(side_effect=_capture)
    rule_service_mock.get_all_rules = AsyncMock(
        side_effect=lambda enabled_only=False: [created['rule']]
        if 'rule' in created else [],
    )
    view = await svc.create(_create_req(enter_debounce_seconds=30))
    rule = created['rule']
    assert rule.duration_seconds == 30
    assert rule.duration_ratio == 1.0
    assert view.enter_debounce_seconds == 30


@pytest.mark.asyncio
async def test_create_compensates_task_on_rule_failure(
    svc, rule_service_mock, task_service_mock
):
    rule_service_mock.create_rule = AsyncMock(
        side_effect=ValidationException('bad device')
    )
    with pytest.raises(ValidationException):
        await svc.create(_create_req())
    # 补偿删除占位 task，不留孤儿
    task_service_mock.delete_task.assert_called_once()
    args = task_service_mock.delete_task.call_args
    assert args.kwargs.get('reason') == 'abandoned'


def test_create_requires_at_least_one_scene():
    with pytest.raises(ValueError, match='至少配置一个'):
        _create_req(enter_scene_id=None, exit_scene_id=None)


@pytest.mark.asyncio
async def test_list_resolves_scene_names(svc):
    views = await svc.list()
    assert len(views) == 1
    assert views[0].enter_scene_name == '开灯'
    assert views[0].query == '有人在看书'
    assert views[0].enabled is True


@pytest.mark.asyncio
async def test_view_maps_enter_confirm_from_rule_duration(svc, rule_service_mock):
    """视图 enter_debounce_seconds 直接来自 rule.duration_seconds（旧规则 → 0）。"""
    rule_service_mock.get_all_rules = AsyncMock(
        return_value=[_make_scene_state_rule(duration_seconds=30)]
    )
    views = await svc.list()
    assert views[0].enter_debounce_seconds == 30


@pytest.mark.asyncio
async def test_update_sets_enter_confirm(svc, rule_service_mock):
    await svc.update(TASK_ID, SceneTaskUpdateRequest(enter_debounce_seconds=30))
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert update.duration_seconds == 30
    assert update.duration_ratio == 1.0
    # 只动了 duration，其它字段不进 fields_set
    assert update.model_fields_set == {'duration_seconds', 'duration_ratio'}


@pytest.mark.asyncio
async def test_update_clears_enter_confirm(svc, rule_service_mock):
    """0 = 立即触发 → 清空 duration 滑窗。"""
    await svc.update(TASK_ID, SceneTaskUpdateRequest(enter_debounce_seconds=0))
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert update.duration_seconds is None
    assert update.duration_ratio is None


@pytest.mark.asyncio
async def test_update_changes_scene_and_query(svc, rule_service_mock, task_service_mock):
    await svc.update(
        TASK_ID,
        SceneTaskUpdateRequest(
            query='有人在沙发上看书',
            enter_scene_id='scene-A',
            cooldown_minutes=10,
        ),
    )
    rule_service_mock.patch_rule.assert_awaited_once()
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert update.condition.query == '有人在沙发上看书'
    assert [a.did for a in update.on_enter_actions] == ['scene-A']
    assert [a.cooldown_minutes for a in update.on_enter_actions] == [10]
    # 未动的 exit 场景保持原样
    assert [a.did for a in update.on_exit_actions] == ['scene-2']


@pytest.mark.asyncio
async def test_update_clears_enter_scene(svc, rule_service_mock):
    await svc.update(
        TASK_ID,
        SceneTaskUpdateRequest(enter_scene_id=None),
    )
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert update.on_enter_actions == []
    # exit 方向未动 → 不进 fields_set（patch_rule 保留原值）
    assert 'on_exit_actions' not in update.model_fields_set


@pytest.mark.asyncio
async def test_update_description_syncs_task(svc, task_service_mock):
    await svc.update(
        TASK_ID, SceneTaskUpdateRequest(description='新名字'),
    )
    task_service_mock.update_description.assert_called_once()
    req = task_service_mock.update_description.call_args.args[1]
    assert isinstance(req, TaskUpdateRequest)
    assert req.description == '新名字'


def test_enable_disable_delete_delegate(svc, task_service_mock):
    svc.enable(TASK_ID)
    task_service_mock.enable_task.assert_called_once_with(TASK_ID)
    svc.disable(TASK_ID)
    task_service_mock.disable_task.assert_called_once_with(TASK_ID)
    svc.delete(TASK_ID)
    assert task_service_mock.delete_task.call_args.args[0] == TASK_ID


@pytest.mark.asyncio
async def test_trigger_delegates_to_rule_trigger(svc, rule_service_mock):
    await svc.trigger(TASK_ID)
    rule_service_mock.trigger_rule.assert_awaited_once()
    assert rule_service_mock.trigger_rule.await_args.args[0] == 'rule-scene'