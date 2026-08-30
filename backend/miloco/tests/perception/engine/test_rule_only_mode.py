# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""rule_only（纯场景触发）模式测试。

覆盖：输出 schema 只剩 matched_rules、system prompt 收敛到规则判定、
user content 无身份名册 / 无家庭档案、pipeline 跳过 tracker/身份链路并剥离音频、
资源校验不再要求端侧检测模型。
"""

import json
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
import pytest
from miloco.perception.engine.config import PerceptionConfig
from miloco.perception.engine.omni.field_registry import SceneDescriptor, render_schema
from miloco.perception.engine.omni.prompt_builder import (
    _encode_frames_as_jpegs,
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


def _make_packet_with_frames(frames) -> IdentityPacket:
    ep = _make_packet()
    ep.all_frames = frames
    return ep


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




def test_last_frame_only_encoder_keeps_single_frame():
    """last_frame_only=True（默认）→ 只编码窗口最后一帧；False → 多帧全发。"""
    frames = [_solid(10, 10, 10), _solid(250, 250, 250)]
    ep = _make_packet_with_frames(frames)
    # 默认只取末帧
    imgs = _encode_frames_as_jpegs([ep])
    assert len(imgs) == 1
    decoded = cv2.imdecode(np.frombuffer(imgs[0], dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None and decoded.shape[0] > 0
    # 关闭开关 → 两帧全发
    imgs_all = _encode_frames_as_jpegs([ep], last_frame_only=False)
    assert len(imgs_all) == 2


def test_build_prompt_default_last_frame_only_single_image():
    """build_prompt（rule_only）默认只发一帧图（settings 未配 last_frame_only）。"""
    ep = _make_packet()
    ctx = OmniContext(
        rule_only=True,
        rule_conditions=[
            RuleCondition(rule_id='reading_light', rule_name='读书开灯', query='有人在床上看书'),
        ],
    )
    # _rule_only_last_frame_only 内部是 from miloco.config import get_settings（局部导入），
    # 须 patch miloco.config.get_settings 才生效；input 返回真实 dict，避免 video_short_edge
    # 被 mock 值污染（同一 .get("input", {}) 读取点）
    with patch('miloco.config.get_settings') as mock_gs:
        mock_gs.return_value.perception.engine.get.return_value = {
            'last_frame_only': True,
            'video_short_edge': 512,
        }
        payload = build_prompt(ep, ctx)
    assert len(payload['image_frames']) == 1, '默认应只发最后一帧'


def test_build_prompt_last_frame_only_false_sends_all():
    """settings last_frame_only=false → 多帧全发。"""
    ep = _make_packet()
    ctx = OmniContext(rule_only=True)
    with patch('miloco.config.get_settings') as mock_gs:
        mock_gs.return_value.perception.engine.get.return_value = {
            'last_frame_only': False,
            'video_short_edge': 512,
        }
        payload = build_prompt(ep, ctx)
    assert len(payload['image_frames']) == 2, '关闭开关应多帧全发'


def test_rule_only_encodes_clip_for_replay(monkeypatch):
    """rule_only 只发图片送模型，但会额外编码一份 mp4 供 web 日志回看触发片段。

    clip 字节经 _encode_video_mp4 尾部 push_clip_bytes 落进 artifacts.clips →
    save_event_artifacts 落盘 clip.mp4；mp4 不进 payload、不送模型。
    """
    from miloco.perception.engine.omni import prompt_builder

    calls: list = []

    def fake_encode(ep, short_edge=prompt_builder._VIDEO_SHORT_EDGE):
        calls.append(short_edge)
        return None, None

    monkeypatch.setattr(prompt_builder, "_encode_video", fake_encode)
    ep = _make_packet()
    ctx = OmniContext(rule_only=True)
    with patch('miloco.config.get_settings') as mock_gs:
        mock_gs.return_value.perception.engine.get.return_value = {
            'last_frame_only': True,
            'video_short_edge': 512,
        }
        payload = build_prompt(ep, ctx)
    assert payload.get('image_frames'), '送模型的仍是图片'
    assert 'video_base64' not in payload, 'mp4 不进 payload、不送模型'
    assert len(calls) == 1, '应额外编码一份 mp4 用于落盘回看'


def test_rule_only_clip_encode_failure_is_silent(monkeypatch):
    """落盘编码失败只告警，不阻断 rule_only 主链路（payload 照常产出）。"""
    from miloco.perception.engine.omni import prompt_builder

    def boom(ep, short_edge=prompt_builder._VIDEO_SHORT_EDGE):
        raise RuntimeError("encode failed")

    monkeypatch.setattr(prompt_builder, "_encode_video", boom)
    ep = _make_packet()
    ctx = OmniContext(rule_only=True)
    with patch('miloco.config.get_settings') as mock_gs:
        mock_gs.return_value.perception.engine.get.return_value = {
            'last_frame_only': True,
            'video_short_edge': 512,
        }
        payload = build_prompt(ep, ctx)
    assert payload.get('image_frames'), '编码失败不应影响 rule_only 主链路'


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
    assert '家庭档案' not in sp
    # 输入改为逐帧 JPEG 图片（image_url 块），不再编 mp4 视频
    assert 'video_base64' not in payload
    assert 'audio_base64' not in payload
    frames = payload['image_frames']
    assert isinstance(frames, list) and len(frames) > 0, 'rule_only 应下发图片帧'
    assert all(f.startswith(b'\xff\xd8') for f in frames), '帧应为 JPEG（SOI 头）'


def test_rule_only_messages_build_image_url_blocks():
    """_build_messages 遇 image_frames → 逐帧 image_url 块，不落 video/audio。

    关键回归点：URL 里的 base64 解码后必须与原始 JPEG 字节一致——直接把 bytes
    f-string 插值会得到 b'\xff\xd8...' repr，服务端 400 "base64 decode fail"
    （dashscope 实测）。"""
    import base64

    from miloco.perception.engine.omni.omni_client import _build_messages

    payload = {
        'system_prompt': 'sys',
        'user_content': 'user',
        'image_frames': [b'\xff\xd8frame1', b'\xff\xd8frame2'],
    }
    messages = _build_messages(payload, adapter=object())  # image 分支不触 adapter
    content = messages[1]['content']
    imgs = [b for b in content if isinstance(b, dict) and b.get('type') == 'image_url']
    assert len(imgs) == 2
    for block, raw in zip(imgs, payload['image_frames']):
        url = block['image_url']['url']
        assert url.startswith('data:image/jpeg;base64,'), url[:60]
        assert base64.b64decode(url.split(',', 1)[1]) == raw, (
            'URL 内 base64 必须与原始 JPEG 字节一致'
        )
    # 不再出现 video / audio 块
    types = [b.get('type') for b in content if isinstance(b, dict)]
    assert 'video_url' not in types and 'input_audio' not in types


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

    captured: dict = {}

    async def _capture(payload, config, **kwargs):
        captured['payload'] = payload
        return MOCK_OMNI_RESPONSE

    with patch(
        'miloco.perception.engine.omni.omni.call_omni',
        new_callable=AsyncMock,
        side_effect=_capture,
    ):
        result = await run_pipeline(s, ctx, config)

    assert not result.skipped
    # 身份层直通：无 targets、无音频
    assert result.identity_packet is not None
    assert result.identity_packet.targets == []
    assert result.identity_packet.audio_clip.size == 0
    assert result.gate_packet.trigger.audio_active is False
    # 发给模型的载荷是逐帧图片（image_url），无 mp4 视频 / 音频
    payload = captured['payload']
    assert 'video_base64' not in payload and 'audio_base64' not in payload
    assert len(payload.get('image_frames', [])) > 0
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