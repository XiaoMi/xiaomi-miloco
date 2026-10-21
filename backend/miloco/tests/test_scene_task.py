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
async def test_update_sets_exit_debounce_zero(svc, rule_service_mock):
    """退出确认时间 0 是合法新值（条件一变假立即退出），必须原样下发不丢。

    回归：service 层若写成 ``req.exit_debounce_seconds or 60``（把 0 当"未设置"），
    web 改 0 会被静默改回 60。
    """
    await svc.update(TASK_ID, SceneTaskUpdateRequest(exit_debounce_seconds=0))
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert update.exit_debounce_seconds == 0
    assert 'exit_debounce_seconds' in update.model_fields_set


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


# ---- 场景补充说明（逐规则随 JSONL 行注入 omni 的场景细节 / 注意事项） ----

def _capturing_rule_service(rule_service_mock):
    """create_rule 捕获传入的 rule，并让 get_all_rules 回查该 rule（供 create 末尾 get()）。"""
    created: dict = {}

    def _capture(rule):
        created['rule'] = rule
        return 'rule-new'

    rule_service_mock.create_rule = AsyncMock(side_effect=_capture)
    rule_service_mock.get_all_rules = AsyncMock(
        side_effect=lambda enabled_only=False: [created['rule']] if 'rule' in created else [],
    )
    return created


@pytest.mark.asyncio
async def test_create_passes_scene_notes(svc, rule_service_mock):
    created = _capturing_rule_service(rule_service_mock)
    view = await svc.create(_create_req(scene_notes='目标区只限白色地板；鞋影不算'))
    assert created['rule'].condition.scene_notes == '目标区只限白色地板；鞋影不算'
    # 视图回读同值（web 编辑抽屉据此回填）
    assert view.scene_notes == '目标区只限白色地板；鞋影不算'


@pytest.mark.asyncio
async def test_create_blank_scene_notes_normalized_to_none(svc, rule_service_mock):
    created = _capturing_rule_service(rule_service_mock)
    await svc.create(_create_req(scene_notes='   '))
    assert created['rule'].condition.scene_notes is None


@pytest.mark.asyncio
async def test_list_returns_scene_notes(svc, rule_service_mock):
    rule = _make_scene_state_rule()
    rule.condition.scene_notes = '必须连续两帧看到'
    rule_service_mock.get_all_rules = AsyncMock(return_value=[rule])
    views = await svc.list()
    assert views[0].scene_notes == '必须连续两帧看到'


@pytest.mark.asyncio
async def test_update_sets_scene_notes(svc, rule_service_mock):
    await svc.update(TASK_ID, SceneTaskUpdateRequest(scene_notes='新细则：忽略窗外'))
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert update.condition.scene_notes == '新细则：忽略窗外'
    assert 'scene_notes' in update.condition.model_fields_set


@pytest.mark.asyncio
async def test_update_clears_scene_notes(svc, rule_service_mock):
    """显式传 None（或 ""）→ 清空该规则的场景补充说明（合法新值，非"不动"）。"""
    await svc.update(TASK_ID, SceneTaskUpdateRequest(scene_notes=None))
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert update.condition.scene_notes is None
    assert 'scene_notes' in update.condition.model_fields_set


@pytest.mark.asyncio
async def test_update_without_scene_notes_untouched(svc, rule_service_mock):
    """未传 scene_notes → 不进 condition fields_set，patch_rule 保留原值。"""
    await svc.update(TASK_ID, SceneTaskUpdateRequest(query='新条件'))
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert 'scene_notes' not in update.condition.model_fields_set


# ---- 字段改名过渡：旧键 system_context → scene_notes ----
# 该字段短暂用过 system_context 这个名字（未发布）。老 DB 行（condition JSON blob）
# 与老前端缓存请求可能仍带旧键；schema 用 validation_alias 读旧键、写恒用新键，
# 保证不 422、不静默丢配置。


def test_rule_condition_reads_legacy_system_context_key():
    """rule_repo 反序列化旧行：condition blob 里的 system_context 读进 scene_notes。"""
    cond = RuleCondition(
        perceive_device_ids=['cam-001'], query='条件',
        **{'system_context': '旧键细则'},
    )
    assert cond.scene_notes == '旧键细则'
    dumped = cond.model_dump()
    assert dumped['scene_notes'] == '旧键细则'
    assert 'system_context' not in dumped


def test_scene_task_create_accepts_legacy_system_context_key():
    """旧键请求体（extra='forbid' 下）仍可接受，且规范化成 scene_notes。"""
    req = SceneTaskCreateRequest(
        description='床上看书自动开灯',
        perceive_device_ids=['cam-001'],
        query='有人在床上看书',
        enter_scene_id='scene-1',
        **{'system_context': '旧键细则'},
    )
    assert req.scene_notes == '旧键细则'
    assert req.model_dump()['scene_notes'] == '旧键细则'


def test_scene_task_update_accepts_legacy_system_context_key():
    """旧键 PATCH：字段进入 scene_notes 的 fields_set（仍能被 service 识别为已传）。"""
    req = SceneTaskUpdateRequest(**{'system_context': None})
    assert req.scene_notes is None
    assert 'scene_notes' in req.model_fields_set

# ---- 动态 TTS（进入 / 退出可播报、可与自动化场景同时设置）----

from miloco.rule.schema import SCENE_TTS_IID, RuleEvent, SceneTtsSlot  # noqa: E402
from miloco.scene_task.schema import (  # noqa: E402
    SceneTaskCreateRequest as _Req,
)
from miloco.scene_task.schema import (
    compose_tts_prompt,
    merge_scene_notes,
    parse_tts_prompts,
    split_scene_notes,
)


def _tts(device='speaker-1', dynamic=True, prompt='结合我的穿搭夸一句', text=None):
    return _Req(
        description='出门夸夸',
        perceive_device_ids=['cam-001'],
        query='准备出门',
        enter_scene_id='scene-1',
        enter_tts_device_id=device,
        enter_tts=SceneTtsSlot(tts_content=dynamic, tts_prompt=prompt, tts_text=text),
        cooldown_minutes=5,
    )


def _speaker_rule(
    enter_tts=True,
    dynamic=True,
    prompt='结合我的穿搭夸一句',
    exit_tts=False,
    exit_prompt=None,
    static_text=None,
    scene_notes=None,
):
    """带 TTS 动作的场景联动规则（与 service 写出的形状一致）。"""
    notes = merge_scene_notes(
        scene_notes,
        compose_tts_prompt(
            SceneTtsSlot(tts_content=True, tts_prompt=prompt)
            if enter_tts and dynamic else None,
            SceneTtsSlot(tts_content=True, tts_prompt=exit_prompt)
            if exit_tts and dynamic else None,
        )
        if (enter_tts and dynamic) or (exit_tts and dynamic)
        else None,
    )
    return Rule(
        id='rule-scene',
        name=f'[{TASK_ID}] 出门夸夸',
        task_id=TASK_ID,
        mode=RuleMode.STATE,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=RuleCondition(
            perceive_device_ids=['cam-001'],
            query='准备出门',
            scene_notes=notes,
            tts_enabled=bool((enter_tts and dynamic) or (exit_tts and dynamic)),
        ),
        on_enter_actions=(
            [_scene_action('scene-1')]
            + (
                [RuleAction(
                    did='speaker-1', iid=SCENE_TTS_IID, idempotent=False,
                    cooldown_minutes=5, tts_text=static_text,
                )]
                if enter_tts else []
            )
        ),
        on_exit_actions=(
            [RuleAction(
                did='speaker-1', iid=SCENE_TTS_IID, idempotent=False,
                cooldown_minutes=5,
            )]
            if exit_tts else []
        ),
        exit_debounce_seconds=0,
    )


def test_create_accepts_tts_only_direction():
    """只配 TTS、不配自动化场景也是合法任务（至少一个动作即可）。"""
    req = SceneTaskCreateRequest(
        description='出门夸夸',
        perceive_device_ids=['cam-001'],
        query='准备出门',
        enter_tts_device_id='speaker-1',
        enter_tts=SceneTtsSlot(tts_content=False, tts_text='出门顺利'),
    )
    assert req.enter_scene_id is None
    assert req.enter_tts.tts_text == '出门顺利'


def test_create_rejects_tts_device_without_config():
    with pytest.raises(ValueError, match='必须同时配置'):
        SceneTaskCreateRequest(
            description='x',
            perceive_device_ids=['cam-001'],
            query='准备出门',
            enter_tts_device_id='speaker-1',
        )


def test_create_rejects_tts_without_any_text():
    with pytest.raises(ValueError, match='至少'):
        SceneTaskCreateRequest(
            description='x',
            perceive_device_ids=['cam-001'],
            query='准备出门',
            enter_scene_id='scene-1',
            exit_tts_device_id='speaker-1',
            exit_tts=SceneTtsSlot(tts_content=True),
        )


@pytest.mark.asyncio
async def test_create_builds_scene_and_tts_actions_together(svc, rule_service_mock):
    """同一方向：自动化场景 + TTS 两条动作并存；动态 TTS 写进 condition 与 scene_notes。"""
    created = _capturing_rule_service(rule_service_mock)
    view = await svc.create(_tts())
    rule = created['rule']
    assert [(a.iid, a.did) for a in rule.on_enter_actions] == [
        ('scene', 'scene-1'), (SCENE_TTS_IID, 'speaker-1'),
    ]
    assert all(not a.idempotent and a.cooldown_minutes == 5 for a in rule.on_enter_actions)
    assert rule.condition.tts_enabled is True
    assert '## 动态播报要求' in rule.condition.scene_notes
    assert '结合我的穿搭夸一句' in rule.condition.scene_notes
    # 视图回读：两边动作都在；住户自己的 scene_notes 不含合成段
    assert view.enter_scene_id == 'scene-1'
    assert view.enter_tts is not None
    assert view.enter_tts.device_id == 'speaker-1'
    assert view.enter_tts.tts_content is True
    assert view.enter_tts.tts_prompt == '结合我的穿搭夸一句'
    assert view.scene_notes is None
    assert view.exit_tts is None


@pytest.mark.asyncio
async def test_create_dynamic_tts_without_prompt_still_enables(svc, rule_service_mock):
    """动态模式只给兜底文案、不写播报要求：仍算动态（模型按内置默认要求生成）。"""
    created = _capturing_rule_service(rule_service_mock)
    view = await svc.create(_tts(prompt=None, text='兜底一句'))
    rule = created['rule']
    assert rule.condition.tts_enabled is True
    assert view.enter_tts.tts_content is True
    assert view.enter_tts.tts_text == '兜底一句'
    # 规则动作上带静态兜底文案；scene_notes 里用缺省要求
    tts_action = rule.on_enter_actions[1]
    assert tts_action.tts_text == '兜底一句'
    enter_prompts, _ = parse_tts_prompts(rule.condition.scene_notes)
    assert enter_prompts  # 默认要求文案


@pytest.mark.asyncio
async def test_create_static_tts_keeps_tts_disabled(svc, rule_service_mock):
    created = _capturing_rule_service(rule_service_mock)
    view = await svc.create(_tts(dynamic=False, text='出门顺利'))
    rule = created['rule']
    assert rule.condition.tts_enabled is False
    assert rule.condition.scene_notes is None
    assert rule.on_enter_actions[1].tts_text == '出门顺利'
    assert view.enter_tts.tts_content is False


@pytest.mark.asyncio
async def test_view_reads_dynamic_prompts_per_direction(svc, rule_service_mock):
    """进入动态 + 退出固定：两个方向的 tts_content 各自判定，互不串味。"""
    rule = _speaker_rule(exit_tts=True, exit_prompt=None)
    rule.on_exit_actions = [RuleAction(
        did='speaker-1', iid=SCENE_TTS_IID, idempotent=False,
        cooldown_minutes=5, tts_text='再见，路上小心',
    )]
    rule.condition.scene_notes = merge_scene_notes(
        None, compose_tts_prompt(SceneTtsSlot(tts_content=True, tts_prompt='夸穿搭'), None)
    )
    rule_service_mock.get_all_rules = AsyncMock(return_value=[rule])
    view = (await svc.list())[0]
    assert view.enter_tts.tts_content is True
    assert view.enter_tts.tts_prompt == '夸穿搭'
    assert view.exit_tts.tts_content is False
    assert view.exit_tts.tts_text == '再见，路上小心'


@pytest.mark.asyncio
async def test_update_adds_tts_action(svc, rule_service_mock):
    await svc.update(
        TASK_ID,
        SceneTaskUpdateRequest(
            enter_tts_device_id='speaker-1',
            enter_tts=SceneTtsSlot(tts_content=True, tts_prompt='夸一句'),
        ),
    )
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert [(a.iid, a.did) for a in update.on_enter_actions] == [
        ('scene', 'scene-1'), (SCENE_TTS_IID, 'speaker-1'),
    ]
    assert update.condition.tts_enabled is True
    assert '夸一句' in update.condition.scene_notes
    # 退出方向没动 → 不进 fields_set
    assert 'on_exit_actions' not in update.model_fields_set


@pytest.mark.asyncio
async def test_update_keeps_other_direction_dynamic_prompt(svc, rule_service_mock):
    """只改进入方向（换成固定文案）时，退出方向原有的动态要求必须保留。"""
    rule = _speaker_rule(exit_tts=True)
    rule.condition.scene_notes = merge_scene_notes(
        None,
        compose_tts_prompt(
            SceneTtsSlot(tts_content=True, tts_prompt='夸穿搭'),
            SceneTtsSlot(tts_content=True, tts_prompt='说再见'),
        ),
    )
    rule_service_mock.get_all_rules = AsyncMock(return_value=[rule])
    await svc.update(
        TASK_ID,
        SceneTaskUpdateRequest(
            enter_tts_device_id='speaker-1',
            enter_tts=SceneTtsSlot(tts_content=False, tts_text='固定的夸'),
        ),
    )
    update = rule_service_mock.patch_rule.await_args.args[1]
    enter_prompts, exit_prompts = parse_tts_prompts(update.condition.scene_notes)
    assert enter_prompts is None
    assert exit_prompts == '说再见'
    assert update.condition.tts_enabled is True
    enter_tts = next(a for a in update.on_enter_actions if a.iid == SCENE_TTS_IID)
    assert enter_tts.tts_text == '固定的夸'


@pytest.mark.asyncio
async def test_update_dynamic_without_prompt_keeps_previous_prompt(
    svc, rule_service_mock
):
    """把固定文案方向切成「动态」但没写要求：沿用该方向原来的要求（没有才用内置默认）。"""
    rule = _speaker_rule(dynamic=False, static_text='固定的夸')
    # 造一份「进入固定 + 退出动态」的规则：退出方向要有 TTS 动作，要求写在 scene_notes
    rule.on_exit_actions = [RuleAction(
        did='speaker-1', iid=SCENE_TTS_IID, idempotent=False, cooldown_minutes=5,
    )]
    rule.condition.scene_notes = merge_scene_notes(
        None, compose_tts_prompt(None, SceneTtsSlot(tts_content=True, tts_prompt='说再见'))
    )
    rule_service_mock.get_all_rules = AsyncMock(return_value=[rule])
    await svc.update(
        TASK_ID,
        SceneTaskUpdateRequest(
            enter_tts_device_id='speaker-1',
            # 不写要求时必须给兜底文案（schema 校验），否则这次播报无话可说
            enter_tts=SceneTtsSlot(tts_content=True, tts_text='兜底一句'),
        ),
    )
    update = rule_service_mock.patch_rule.await_args.args[1]
    enter_prompts, exit_prompts = parse_tts_prompts(update.condition.scene_notes)
    assert enter_prompts  # 内置缺省要求，非空
    assert exit_prompts == '说再见'
    assert update.condition.tts_enabled is True


@pytest.mark.asyncio
async def test_update_removes_tts_only_when_both_nulls(svc, rule_service_mock):
    rule = _speaker_rule()
    rule_service_mock.get_all_rules = AsyncMock(return_value=[rule])
    await svc.update(
        TASK_ID,
        SceneTaskUpdateRequest(enter_tts_device_id=None, enter_tts=None),
    )
    update = rule_service_mock.patch_rule.await_args.args[1]
    assert [a.iid for a in update.on_enter_actions] == ['scene']
    assert update.condition.tts_enabled is False
    assert update.condition.scene_notes is None


@pytest.mark.asyncio
async def test_update_rejects_removing_every_action(svc, rule_service_mock):
    rule = _speaker_rule()
    rule.on_enter_actions = [a for a in rule.on_enter_actions if a.iid == SCENE_TTS_IID]
    rule.on_exit_actions = []
    rule_service_mock.get_all_rules = AsyncMock(return_value=[rule])
    with pytest.raises(ValidationException, match='至少配置一个动作'):
        await svc.update(
            TASK_ID,
            SceneTaskUpdateRequest(enter_tts_device_id=None, enter_tts=None),
        )


def test_update_rejects_half_tts_pair():
    with pytest.raises(ValueError, match='必须同时提交'):
        SceneTaskUpdateRequest(enter_tts_device_id='speaker-1')


def test_tts_note_helpers_roundtrip():
    section = compose_tts_prompt(
        SceneTtsSlot(tts_content=True, tts_prompt='夸穿搭'),
        SceneTtsSlot(tts_content=False, tts_text='不看'),
    )
    merged = merge_scene_notes('鞋影不算', section)
    assert split_scene_notes(merged) == '鞋影不算'
    assert parse_tts_prompts(section) == ('夸穿搭', None)
    assert split_scene_notes(None) is None


@pytest.mark.asyncio
async def test_list_voice_devices_maps_names(svc, mock_miot_proxy):
    mock_miot_proxy.refresh_voice_devices = AsyncMock(return_value={
        'speaker-1': SimpleNamespace(
            did='speaker-1', name='客厅音箱', home_id=MOCK_HOME_ID,
            room_name='客厅', online=True, model='xiaomi.wifispeaker.lx06',
        ),
    })
    devices = await svc.list_voice_devices(refresh=True)
    assert len(devices) == 1
    assert devices[0].did == 'speaker-1'
    assert devices[0].name == '客厅音箱'
    assert devices[0].room_name == '客厅'
    assert devices[0].online is True


# ---- RuleRunner：TTS 动作与自动化场景同时执行 ----


def _make_tts_scene_rule(tts_text=None):
    """进入 = 自动场景 + TTS，退出 = 只有 TTS（验证两个方向都能播）。"""
    return Rule(
        id='rule-tts',
        name=f'[{TASK_ID}] 出门夸夸',
        task_id=TASK_ID,
        mode=RuleMode.STATE,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=RuleCondition(
            perceive_device_ids=['cam-001'], query='准备出门', tts_enabled=True
        ),
        on_enter_actions=[
            _scene_action('scene-1'),
            RuleAction(
                did='speaker-1', iid=SCENE_TTS_IID, idempotent=False,
                cooldown_minutes=5, tts_text=tts_text,
            ),
        ],
        on_exit_actions=[
            RuleAction(
                did='speaker-1', iid=SCENE_TTS_IID, idempotent=False,
                cooldown_minutes=5,
            ),
        ],
        exit_debounce_seconds=0,
    )


@pytest.fixture
def tts_runner(mock_miot_proxy, mock_log_repo, mock_task_record_service):
    with patch('miloco.miot.voice.speak', new_callable=AsyncMock,
               return_value=(True, None)) as speak_mock:
        r = RuleRunner(
            rules=[_make_tts_scene_rule()],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            task_record_service=mock_task_record_service,
        )
        r._speak_mock = speak_mock  # type: ignore[attr-defined]
        yield r


@pytest.mark.asyncio
async def test_enter_fires_automation_and_dynamic_tts(tts_runner, mock_miot_proxy):
    """进入：自动化场景照常触发，模型给出的动态文案同时播报。"""
    out = await tts_runner.update_state(
        'rule-tts', 'cam-001', True, '穿搭好看', tts_text='今天这身配色很精神',
    )
    await tts_runner.drain()
    assert out is TriggerOutcome.FIRED
    mock_miot_proxy.execute_miot_scene.assert_awaited_once()
    assert mock_miot_proxy.execute_miot_scene.await_args.args[0] == 'scene-1'
    tts_runner._speak_mock.assert_awaited_once()
    assert tts_runner._speak_mock.await_args.args[1] == 'speaker-1'
    assert tts_runner._speak_mock.await_args.args[2] == '今天这身配色很精神'


@pytest.mark.asyncio
async def test_enter_without_dynamic_text_skips_tts_but_fires_scene(
    tts_runner, mock_miot_proxy
):
    """模型没给文案（且没配兜底）→ 跳过播报，但自动化照常触发。"""
    await tts_runner.update_state('rule-tts', 'cam-001', True, '穿搭好看')
    await tts_runner.drain()
    mock_miot_proxy.execute_miot_scene.assert_awaited_once()
    tts_runner._speak_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_text_falls_back_to_static(
    mock_miot_proxy, mock_log_repo, mock_task_record_service
):
    with patch('miloco.miot.voice.speak', new_callable=AsyncMock,
               return_value=(True, None)) as speak_mock:
        r = RuleRunner(
            rules=[_make_tts_scene_rule(tts_text='出门顺利')],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            task_record_service=mock_task_record_service,
        )
        await r.update_state('rule-tts', 'cam-001', True, '穿搭好看')
        await r.drain()
    speak_mock.assert_awaited_once()
    assert speak_mock.await_args.args[2] == '出门顺利'


@pytest.mark.asyncio
async def test_exit_fires_tts_only(tts_runner, mock_miot_proxy):
    """退出方向只挂了 TTS：离开边沿（debounce=0）播报，不触发任何自动化场景。"""
    with patch('miloco.miot.voice.speak', new_callable=AsyncMock,
               return_value=(True, None)) as speak_mock:
        tts_runner._speak_mock = speak_mock
        await tts_runner.update_state('rule-tts', 'cam-001', True, '准备出门')
        await tts_runner.drain()
        mock_miot_proxy.execute_miot_scene.reset_mock()
        speak_mock.reset_mock()
        # 单帧 False 只是抗抖观察窗；第二帧才确认 EXIT
        await tts_runner.update_state('rule-tts', 'cam-001', False)
        await tts_runner.update_state('rule-tts', 'cam-001', False)
        await tts_runner.drain()
    mock_miot_proxy.execute_miot_scene.assert_not_awaited()
    # 退出边沿没有动态文案、退出动作也没有兜底 → 跳过（不报失败）
    speak_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_failure_does_not_block_scene(
    mock_miot_proxy, mock_log_repo, mock_task_record_service
):
    with patch('miloco.miot.voice.speak', new_callable=AsyncMock,
               return_value=(False, 'device offline')):
        r = RuleRunner(
            rules=[_make_tts_scene_rule()],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            task_record_service=mock_task_record_service,
        )
        await r.update_state('rule-tts', 'cam-001', True, '穿搭好看', tts_text='夸一句')
        await r.drain()
    mock_miot_proxy.execute_miot_scene.assert_awaited_once()
    # 两次 fire（自动化 + TTS）各写一条 rule_log；TTS 那条记失败与原因
    kinds = [c.args[0].kind.value for c in mock_log_repo.create.call_args_list]
    assert kinds.count('RULE_TRIGGER_SUCCESS') == 1
    assert kinds.count('RULE_TRIGGER_FAILURE') == 1
    failed = next(
        c.args[0] for c in mock_log_repo.create.call_args_list
        if c.args[0].kind.value == 'RULE_TRIGGER_FAILURE'
    )
    assert failed.execute_result.action_results[0].error == 'device offline'


def test_select_slot_splits_tts_actions(tts_runner):
    """ENTERED 槽只含自动化动作；TTS 挪到 ON_ENTERED（没有 TTS 时 ON_* 为空槽）。"""
    rule = tts_runner.get_rule('rule-tts')
    entered = tts_runner._select_slot(rule, RuleEvent.ENTERED)
    assert entered is not None and [a.iid for a in entered[1]] == ['scene']
    on_enter = tts_runner._select_slot(rule, RuleEvent.ON_ENTERED)
    assert on_enter is not None and [a.iid for a in on_enter[1]] == [SCENE_TTS_IID]
    # 退出方向只有 TTS → EXITED 槽为空（不写自动化日志），ON_EXITED 负责播报
    assert tts_runner._select_slot(rule, RuleEvent.EXITED) is None
    assert tts_runner._select_slot(rule, RuleEvent.ON_EXITED) is not None


def test_select_slot_plain_rule_has_no_tts_slot(runner):
    rule = runner.get_rule('rule-scene')
    assert runner._select_slot(rule, RuleEvent.ON_ENTERED) is None
    assert runner._select_slot(rule, RuleEvent.ON_EXITED) is None


@pytest.mark.asyncio
async def test_enter_confirm_fires_tts_after_window(
    mock_miot_proxy, mock_log_repo, mock_task_record_service
):
    """带「进入确认时间」的规则：滑窗达标那一次同时触发自动化与 TTS（不因走 duration
    路径而漏播）。窗口 = 6s / 3s 采样 = 2 个采样（与 confirm_runner 同口径）。"""
    rule = _make_tts_scene_rule()
    rule.duration_seconds = 6
    rule.duration_ratio = 1.0
    rule.exit_debounce_seconds = 0
    with patch('miloco.miot.voice.speak', new_callable=AsyncMock,
               return_value=(True, None)) as speak_mock:
        r = RuleRunner(
            rules=[rule],
            miot_proxy=mock_miot_proxy,
            rule_log_repo=mock_log_repo,
            sample_interval_seconds=3.0,
            task_record_service=mock_task_record_service,
        )
        with patch('miloco.rule.runner.time.time') as mt:
            mt.return_value = 100.0
            first = await r.update_state('rule-tts', 'cam-001', True, '准备出门')
            mt.return_value = 103.0
            second = await r.update_state(
                'rule-tts', 'cam-001', True, '准备出门',
                tts_text='今天这身颜色真精神',
            )
        await r.drain()
    assert first is TriggerOutcome.COUNTING
    assert second is TriggerOutcome.FIRED
    mock_miot_proxy.execute_miot_scene.assert_awaited_once()
    speak_mock.assert_awaited_once()
    assert speak_mock.await_args.args[2] == '今天这身颜色真精神'
