# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""rule_only（纯场景触发）模式测试。

覆盖：输出 schema 只剩 matched_rules、system prompt 收敛到规则判定、
user content 无身份名册 / 无家庭档案、pipeline 跳过 tracker/身份链路并剥离音频、
资源校验不再要求端侧检测模型。
"""

import json
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from miloco.perception.engine.config import PerceptionConfig
from miloco.perception.engine.omni.field_registry import SceneDescriptor, render_schema
from miloco.perception.engine.omni.prompt_builder import (
    build_prompt,
    build_system_prompt,
)
from miloco.perception.engine.pipeline import run_batch_pipeline, run_pipeline
from miloco.perception.engine.resource_validator import (
    EngineReadiness,
    validate_resources,
)
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    FrameInfo,
    GateTrigger,
    IdentityPacket,
    MotionState,
    OmniContext,
    RuleCondition,
)
from miloco.perception.types import (
    AudioFrame,
    AudioStream,
    BatchedSnapshot,
    DeviceSnapshot,
    PerceptionDevice,
    VideoFrame,
    VideoStream,
)

MOCK_OMNI_RESPONSE = {
    'id': 'mock',
    'choices': [
        {
            'message': {
                'content': json.dumps({
                    'matched_rules': [
                        {'rule_name': '读书开灯', 'reason': '画面中有人躺在床上看书', 'hit': True},
                    ],
                }),
            },
        }
    ],
}


def _solid(r: int, g: int, b: int) -> np.ndarray:
    return np.full((64, 64, 3), (b, g, r), dtype=np.uint8)


def _make_packet() -> IdentityPacket:
    return IdentityPacket(
        packet_id='ep-1',
        room_name='卧室',
        timestamp=1000.0,
        frame_info=FrameInfo(start_timestamp=0, end_timestamp=3000, fps=2),
        targets=[],
        scene_motion=MotionState.STATIC,
        frames=[],
        all_frames=[_solid(10, 10, 10), _solid(20, 20, 20)],
        audio_clip=np.zeros(16000, dtype=np.int16),
        audio_analysis=AudioAnalysis(type=AudioType.SILENCE, is_urgent=False, energy_level=0.0),
        # rule_only：音频已剥离 → trigger.audio_active=False（与 pipeline 产物一致）
        trigger=GateTrigger(
            visual_changed=True, visual_change_score=1.0,
            audio_active=False, audio_energy_level=0.0,
        ),
    )


def _make_snapshot(room: str, did: str) -> DeviceSnapshot:
    frames = [_solid(10, 10, 10), _solid(255, 255, 255)] * 3
    return DeviceSnapshot(
        device=PerceptionDevice(did=did, name='cam', device_type='camera', room_name=room),
        start_timestamp=0.0,
        end_timestamp=4000.0,
        video=VideoStream(
            frames=[VideoFrame(data=f, timestamp=i * 500) for i, f in enumerate(frames)],
            width=64, height=64,
        ),
        audio=AudioStream(
            frames=[AudioFrame(data=np.ones(8000, dtype=np.int16), timestamp=0.0)],
            sample_rate=16000,
        ),
    )


# ---- 输出 schema：只出 matched_rules ----


def test_rule_only_selected_fields_only_matched_rules():
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=True, has_speech=True)
    names = [f.name for f in scene.selected_fields()]
    assert names == ['matched_rules'], f'rule_only 应只保留 matched_rules: {names}'


def test_rule_only_render_schema_only_matched_rules():
    scene = SceneDescriptor(route='video', rule_only=True)
    schema = render_schema(scene)
    assert 'matched_rules' in schema
    for field in ('caption', 'speeches', 'env_sounds', 'suggestions', 'identities', 'pet_identities'):
        assert field not in schema, f'rule_only schema 不应含 {field}: {schema}'


# ---- system prompt：收敛到规则判定 ----


def test_rule_only_system_prompt_minimal():
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=False, has_speech=False)
    sp = build_system_prompt(scene, include_home_profile=True)
    # 角色 / 任务收敛
    assert '规则判定引擎' in sp
    assert '规则判定' in sp
    # schema 与字段说明只讲 matched_rules
    assert 'matched_rules' in sp
    for field in ('## caption', '## speeches', '## suggestions', '## identities', '## env_sounds'):
        assert field not in sp, f'rule_only system prompt 不应含 {field} 字段说明'
    # 不需要的输出结构全部收敛
    assert '输出实例' not in sp
    assert '通用常识' not in sp
    assert '家庭档案' not in sp


def test_rule_only_build_prompt_no_roster_no_home_profile():
    ep = _make_packet()
    ctx = OmniContext(
        rule_only=True,
        room_name='卧室',
        rule_conditions=[
            RuleCondition(rule_id='reading_light', rule_name='读书开灯', query='有人在床上看书'),
        ],
    )
    payload = build_prompt(ep, ctx)
    sp = payload['system_prompt']
    uc = payload['user_content']
    # 规则条件照常下发（这是唯一判定依据）
    assert '读书开灯' in uc
    assert '有人在床上看书' in uc
    # 无身份名册 / 家庭档案
    assert '已识别人物' not in uc
    assert '位置: 卧室' in uc  # 房间名仍作场景参考
    assert '家庭档案' not in sp
    # 视频照常编码
    assert payload['video_base64'] is not None
    assert payload['media_info'] is not None


# ---- 资源校验：rule_only 不要求端侧检测模型 ----


def test_rule_only_resource_validation_skips_models(tmp_path):
    models_dir = str(tmp_path / 'empty-models')  # 目录不存在 → 常规模式必 MISSING
    result = validate_resources('test-key', models_dir, rule_only=True)
    assert result.status is EngineReadiness.READY, result.message
    # 对照：非 rule_only 缺模型目录 → MODELS_MISSING
    result2 = validate_resources('test-key', models_dir, rule_only=False)
    assert result2.status is EngineReadiness.MODELS_MISSING
    # 无 key 仍拒绝
    result3 = validate_resources('', models_dir, rule_only=True)
    assert result3.status is EngineReadiness.NOT_CONFIGURED


# ---- pipeline：跳过身份链路 + 剥离音频 ----


@pytest.mark.asyncio
async def test_run_pipeline_rule_only_skips_identity_and_audio():
    from miloco.perception.engine.input.video_splitter import create_input_slice

    frames = [_solid(10, 10, 10), _solid(255, 255, 255)] * 3
    s = create_input_slice('卧室', frames, np.ones(16000, dtype=np.int16))
    config = PerceptionConfig(rule_only=True)
    config.omni.api_key = 'test-key'
    ctx = OmniContext(
        rule_conditions=[
            RuleCondition(rule_id='reading_light', rule_name='读书开灯', query='有人在床上看书'),
        ],
    )

    with patch(
        'miloco.perception.engine.omni.omni.call_omni',
        new_callable=AsyncMock,
        return_value=MOCK_OMNI_RESPONSE,
    ):
        result = await run_pipeline(s, ctx, config)

    assert not result.skipped
    # 身份层直通：无 targets、无音频
    assert result.identity_packet is not None
    assert result.identity_packet.targets == []
    assert result.identity_packet.audio_clip.size == 0
    assert result.gate_packet.trigger.audio_active is False
    # 输出只有规则命中
    assert result.omni_output is not None
    assert len(result.omni_output.matched_rules) == 1
    assert result.omni_output.matched_rules[0].rule_id == 'reading_light'
    assert result.omni_output.caption == []
    assert result.omni_output.suggestions == []
    assert result.omni_output.speeches == []


@pytest.mark.asyncio
async def test_batch_rule_only_never_instantiates_tracker():
    """rule_only 下 tracker/identity factory 不被调用（省 ONNX 加载）。"""
    snapshot = _make_snapshot('卧室', 'cam-1')
    batch = BatchedSnapshot(snapshots=[snapshot])
    config = PerceptionConfig(rule_only=True)
    config.omni.api_key = 'test-key'
    contexts = {'cam-1': OmniContext(rule_conditions=[
        RuleCondition(rule_id='reading_light', rule_name='读书开灯', query='有人在床上看书'),
    ])}

    def _boom_tracking(did, room_name):
        raise AssertionError('rule_only 不应实例化 tracker')

    def _boom_identity(did, room_name):
        raise AssertionError('rule_only 不应实例化 IdentityEngine')

    with patch(
        'miloco.perception.engine.omni.omni.call_omni',
        new_callable=AsyncMock,
        return_value=MOCK_OMNI_RESPONSE,
    ):
        result = await run_batch_pipeline(
            batch, contexts, config,
            get_tracking_service=_boom_tracking,
            get_identity_engine=_boom_identity,
        )

    room = result.rooms['卧室']
    out = room.omni_outputs['cam-1']
    assert len(out.matched_rules) == 1
    assert out.matched_rules[0].rule_id == 'reading_light'
    # 音频已剥离：gate 不因音频触发
    dev = room.device_results['cam-1']
    assert dev.gate_packet.trigger.audio_active is False
    assert dev.identity_packet.targets == []