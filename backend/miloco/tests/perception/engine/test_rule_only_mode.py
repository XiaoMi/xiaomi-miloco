# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""rule_only（纯场景触发）模式测试。

覆盖：输出只有命中规则（rule_only 为**顶层裸数组**——紧凑模式 ``["规则id"]``、
详细模式 ``[{"rule_id","hit","reason"}]``）、system prompt 收敛到规则判定、
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


def _build_rule_only(ep, **input_cfg):
    """rule_only payload 构建 helper：mock settings 的 input 块（热读点同一处）。"""
    ctx = OmniContext(rule_only=True)
    cfg = {
        'video_short_edge': 512,
        'rule_only_input': 'video',
        'rule_only_video_single_frame': False,
        'last_frame_only': True,
    }
    cfg.update(input_cfg)
    with patch('miloco.config.get_settings') as mock_gs:
        mock_gs.return_value.perception.engine.get.return_value = cfg
        return build_prompt(ep, ctx)


def test_build_prompt_default_video_input():
    """build_prompt（rule_only）默认发 mp4 视频（media token 约为图片 1/4）。"""
    ep = _make_packet()  # all_frames = 2 帧
    payload = _build_rule_only(ep)
    assert payload.get('video_base64'), '默认应发视频'
    assert 'image_frames' not in payload, '默认不发图片'
    assert payload['media_info'].frame_count == 2, '全窗口帧合成'


def test_build_prompt_image_mode_switch():
    """rule_only_input="image" → 切回末帧图片输入（旧行为）。"""
    ep = _make_packet()
    payload = _build_rule_only(ep, rule_only_input='image')
    assert 'video_base64' not in payload
    assert len(payload['image_frames']) == 1, '图片模式默认只发最后一帧'


def test_build_prompt_image_mode_last_frame_only_false():
    """图片模式 last_frame_only=false → 多帧全发。"""
    ep = _make_packet()
    payload = _build_rule_only(ep, rule_only_input='image', last_frame_only=False)
    assert len(payload['image_frames']) == 2, '关闭开关应多帧全发'


def test_rule_only_video_single_frame_switch():
    """rule_only_video_single_frame=True → 只合成窗口最后一帧的单帧 mp4（~66 tok）。"""
    ep = _make_packet()  # 2 帧
    payload = _build_rule_only(ep, rule_only_video_single_frame=True)
    assert payload.get('video_base64'), '单帧开关仍是视频模式'
    assert payload['media_info'].frame_count == 1, '只应编码 1 帧'


def test_rule_only_encodes_clip_for_replay(monkeypatch):
    """图片模式只发图片送模型，但会额外编码一份 mp4 供 web 日志回看触发片段。

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
    payload = _build_rule_only(ep, rule_only_input='image')
    assert payload.get('image_frames'), '送模型的仍是图片'
    assert 'video_base64' not in payload, 'mp4 不进 payload、不送模型'
    assert len(calls) == 1, '应额外编码一份 mp4 用于落盘回看'


def test_rule_only_clip_encode_failure_is_silent(monkeypatch):
    """图片模式落盘编码失败只告警，不阻断 rule_only 主链路（payload 照常产出）。"""
    from miloco.perception.engine.omni import prompt_builder

    def boom(ep, short_edge=prompt_builder._VIDEO_SHORT_EDGE):
        raise RuntimeError("encode failed")

    monkeypatch.setattr(prompt_builder, "_encode_video", boom)
    ep = _make_packet()
    payload = _build_rule_only(ep, rule_only_input='image')
    assert payload.get('image_frames'), '编码失败不应影响 rule_only 主链路'


# ---- 输出 schema：只出 matched_rules ----


def test_rule_only_selected_fields_only_matched_rules():
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=True, has_speech=True)
    names = [f.name for f in scene.selected_fields()]
    assert names == ['matched_rules'], f'rule_only 应只保留 matched_rules: {names}'


def test_rule_only_render_schema_only_matched_rules():
    """紧凑（默认 verbose=false）：rule_only 的 schema 是**顶层裸 id 数组**。

    为什么不再有 ``{"matched_rules":(...)}`` 包裹层：rule_only 唯一输出就是命中规则，
    外层 key 白让模型多写 18 个字符、解析层还要再拆一层；紧凑模式连 reason / hit 都不产出
    ——每窗最大的一块输出 token 就省在这里。
    """
    scene = SceneDescriptor(route='video', rule_only=True)
    schema = render_schema(scene)
    assert schema == '["规则id"]', f'rule_only 紧凑 schema 应是裸 id 数组: {schema}'
    # 没有包裹层，也没有详细模式才有的 reason / hit
    assert 'matched_rules' not in schema
    assert 'reason' not in schema and 'hit' not in schema
    for field in ('caption', 'speeches', 'env_sounds', 'suggestions', 'identities', 'pet_identities'):
        assert field not in schema, f'rule_only schema 不应含 {field}: {schema}'


# ---- system prompt：收敛到规则判定 ----


def test_rule_only_system_prompt_minimal():
    # 环境无关：屏蔽用户配置的 rule_only_system_prompt（本机 config.json / yaml 可能
    # 配了自定义全量提示词），只测代码内置装配版。
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=False, has_speech=False)
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(mock_gs, {'rule_only_system_prompt': ''})
        sp = build_system_prompt(scene, include_home_profile=True)
    # 角色 / 任务收敛
    assert '规则判定引擎' in sp
    assert '规则判定' in sp
    # 紧凑 schema 是顶层裸 id 数组；字段说明只讲「命中规则」（无 matched_rules 包裹层）
    assert '["规则id"]' in sp
    # 紧凑模式不产出 reason / hit——提示词出现它们只会诱导模型多吐字段、白烧 token
    assert 'reason' not in sp
    assert 'hit' not in sp
    # 紧凑模式无自由文本输出 → schema 段不带「输出语言」行（省 token）
    assert '输出语言' not in sp
    for field in ('## caption', '## speeches', '## suggestions', '## identities', '## env_sounds'):
        assert field not in sp, f'rule_only system prompt 不应含 {field} 字段说明'
    # 不需要的输出结构全部收敛
    assert '输出实例' not in sp
    assert '通用常识' not in sp
    assert '家庭档案' not in sp


def test_rule_only_compact_prompt_has_no_reason_hit_or_language_line():
    """默认（verbose=false）提示词：schema 是 ["规则id"]，不提 reason / hit，也不带语言行。

    为什么单独锁这条：紧凑模式模型只回 id 数组，提示词里只要露出 reason / hit 字样，模型
    就会顺手多吐字段（每窗最大的一块输出 token）；「输出语言」只约束自由文本，这里没有
    自由文本可约束 → 一并省掉。
    """
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=False, has_speech=False)
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(mock_gs, {'rule_only_system_prompt': ''})
        sp = build_system_prompt(scene)
    assert '["规则id"]' in sp
    assert 'matched_rules' not in sp, '紧凑模式是顶层裸数组，不应再出现 matched_rules 包裹层'
    assert 'reason' not in sp, '紧凑模式不产出 reason，提示词不得提'
    assert 'hit' not in sp, '紧凑模式不产出 hit，提示词不得提'
    assert '输出语言' not in sp, '紧凑模式无自由文本输出，不注入语言行（省 token）'


def test_rule_only_verbose_schema_lists_all_rules_with_unbounded_reason():
    """verbose=true（排查档）：schema 变对象数组，要求逐条列出命中与未命中、reason 不限字数。

    为什么这样分档：排查「该命中没命中 / 不该命中却命中」要的是完整证据链，故恢复对象数组
    + 全量规则结论 + 不限字数的 reason；此时产生了自由文本，schema 段必须带语言行。
    """
    scene = SceneDescriptor(
        route='video', rule_only=True, verbose=True, has_audio=False, has_speech=False
    )
    schema = render_schema(scene)
    assert schema == '[{"rule_id":"规则id","hit":true|false,"reason":"判断依据"}]', schema
    assert 'matched_rules' not in schema, 'rule_only 详细模式同样是顶层裸数组'
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(
            mock_gs, {'rule_only_system_prompt': '', 'output_language': 'zh'}
        )
        sp = build_system_prompt(scene)
    assert '每条都输出 hit 与 reason' in sp and '未命中都列' in sp, '详细模式要求全量结论'
    assert '不限字数' in sp, 'reason 不限字数（完整证据链，排查用）'
    assert '输出语言：中文（简体）' in sp, '详细模式有自由文本 → 必须带语言行'


def test_rule_only_build_prompt_no_roster_no_home_profile(monkeypatch):
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
    # 输入默认是**图片单帧**（settings.yaml: rule_only_input=image / last_frame_only=true）：
    # 图片按张计费、场景规则绝大多数判"此刻状态"，末帧足够；无音频。
    assert 'image_frames' in payload and 'video_base64' not in payload
    assert len(payload['image_frames']) == 1, '图片模式默认只发最后一帧'
    assert 'audio_base64' not in payload
    # 多帧路径仍要保留覆盖：显式关掉开关（monkeypatch 自动还原）→ 全窗口帧都发。
    from miloco.config import get_settings

    monkeypatch.setitem(
        get_settings().perception.engine.setdefault('input', {}), 'last_frame_only', False
    )
    payload_multi = build_prompt(ep, ctx)
    assert len(payload_multi['image_frames']) == 2, '关闭开关应多帧全发'


def _patch_engine_settings(mock_gs, engine: dict):
    """把 perception.engine 的 .get 换成真实 dict 语义（按 key 取、缺省给 default）。"""
    mock_gs.return_value.perception.engine.get.side_effect = (
        lambda key, default=None: engine.get(key, default)
    )


def test_rule_only_system_prompt_override_verbatim():
    """rule_only_system_prompt 非空 → build_system_prompt 原样返回，跳过全部拼接。

    覆盖参数存在时，角色 / 总原则 / schema / 字段说明 / camera_prompt / 家庭档案
    一律不再注入——整段文本就是用户配置的内容（方便把实验 prompt 直接贴进配置调试）。
    """
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=False, has_speech=False)
    override = '你是一个自定义规则引擎：只按我贴的这份 prompt 判定。\n只输出 JSON。'
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(mock_gs, {'rule_only_system_prompt': override})
        sp = build_system_prompt(
            scene,
            include_home_profile=True,
            camera_prompt='本摄像头须知：忽略窗外',
        )
    assert sp == override, '覆盖时应原样返回，不拼接任何段落'


def test_rule_only_payload_uses_override_system_prompt():
    """build_prompt 产出的 payload.system_prompt 直接采用覆盖文本（端到端）。"""
    ep = _make_packet()
    ctx = OmniContext(
        rule_only=True,
        rule_conditions=[
            RuleCondition(rule_id='peace', rule_name='比耶开灯', query='摆出比耶手势'),
        ],
    )
    override = '自定义 rule_only 系统提示词：直接判断规则。'
    engine = {
        'input': {
            'video_short_edge': 512,
            'rule_only_input': 'video',
            'rule_only_video_single_frame': False,
            'last_frame_only': True,
        },
        'rule_only_system_prompt': override,
    }
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(mock_gs, engine)
        payload = build_prompt(ep, ctx)
    assert payload['system_prompt'] == override
    # 规则条件仍照常下发（覆盖只作用于 system prompt）
    assert '比耶开灯' in payload['user_content']


def test_rule_only_system_prompt_override_empty_uses_builtin():
    """rule_only_system_prompt 为空/未设置 → 仍走内置精简装配（默认行为）。"""
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=False, has_speech=False)
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(mock_gs, {'rule_only_system_prompt': ''})
        sp = build_system_prompt(scene, camera_prompt='忽略窗外')
    assert '规则判定引擎' in sp
    assert '["规则id"]' in sp, '未覆盖时走内置紧凑 schema'
    assert '忽略窗外' in sp, '未覆盖时 camera_prompt 照常追加'


# ---- 全局感知系统提示词（web「设置」页，追加而非替换） ----


def test_global_system_prompt_appended_to_rule_only():
    """global_system_prompt 非空 → 追加「# 全局感知须知」，内置内容全部保留。"""
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=False, has_speech=False)
    engine = {
        'rule_only_system_prompt': '',
        'global_system_prompt': '全局：始终关注门口地面；忽略电视屏幕内容。',
    }
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(mock_gs, engine)
        sp = build_system_prompt(scene, camera_prompt='本机位：忽略窗外')
    assert '规则判定引擎' in sp, '内置角色保留'
    assert '["规则id"]' in sp, '内置 schema 保留'
    assert '# 全局感知须知' in sp
    assert '始终关注门口地面' in sp
    assert '本机位：忽略窗外' in sp
    # 全局段在 camera_prompt 之前（全局对所有机位相同 → 与内置段同为共享前缀）
    assert sp.index('# 全局感知须知') < sp.index('本机位：忽略窗外')


def test_global_system_prompt_empty_not_injected():
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=False, has_speech=False)
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(
            mock_gs, {'rule_only_system_prompt': '', 'global_system_prompt': '   '}
        )
        sp = build_system_prompt(scene)
    assert '# 全局感知须知' not in sp


def test_global_system_prompt_not_str_ignored():
    """配置误写成非字符串 → 视为未设置，不注入（防御配置误写）。"""
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=False, has_speech=False)
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(
            mock_gs, {'rule_only_system_prompt': '', 'global_system_prompt': ['x']}
        )
        sp = build_system_prompt(scene)
    assert '# 全局感知须知' not in sp


def test_global_system_prompt_appended_to_full_perception():
    """全量感知（非 rule_only）也追加全局感知须知。"""
    scene = SceneDescriptor(route='video', has_identity=False, stream=False, has_audio=False, has_speech=False)
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(mock_gs, {'global_system_prompt': '全局：注意地面水渍'})
        sp = build_system_prompt(scene, include_home_profile=False)
    assert '家庭场景理解智能助手' in sp
    assert '# 全局感知须知' in sp
    assert '注意地面水渍' in sp


def test_override_wins_over_global_system_prompt():
    """rule_only_system_prompt 全量覆盖仍优先：非空时原样返回，不追加全局须知。"""
    scene = SceneDescriptor(route='video', rule_only=True, has_audio=False, has_speech=False)
    override = '自定义全量 prompt。'
    engine = {'rule_only_system_prompt': override, 'global_system_prompt': '全局：不该出现'}
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(mock_gs, engine)
        sp = build_system_prompt(scene, camera_prompt='也不该出现')
    assert sp == override


# ---- 输出语言注入（perception.engine.output_language 热读 + MILOCO_APP_LANG 跟随）----


def _full_video_scene() -> SceneDescriptor:
    """非 rule_only 的最小 video 场景（rule_only 紧凑模式不注入语言行，见上文）。"""
    return SceneDescriptor(
        route='video', has_identity=False, stream=False, has_audio=False, has_speech=False
    )


def test_output_language_follows_app_lang_zh(monkeypatch):
    """output_language=auto + MILOCO_APP_LANG=zh → 语言行为中文（跟随界面语言）。

    为什么显式 setenv 而不用运行机器 locale：conftest 只清 MILOCO_*，LANG / LC_ALL 会漏进来，
    依赖 locale 的断言在 CI 与开发机会给出不同结果。
    """
    monkeypatch.setenv('MILOCO_APP_LANG', 'zh')
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(
            mock_gs, {'output_language': 'auto', 'global_system_prompt': ''}
        )
        sp = build_system_prompt(_full_video_scene(), include_home_profile=False)
    assert '输出语言：中文（简体）' in sp


def test_output_language_follows_app_lang_en(monkeypatch):
    """output_language=auto + MILOCO_APP_LANG=en → 语言行为 English。"""
    monkeypatch.setenv('MILOCO_APP_LANG', 'en')
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(
            mock_gs, {'output_language': 'auto', 'global_system_prompt': ''}
        )
        sp = build_system_prompt(_full_video_scene(), include_home_profile=False)
    assert '输出语言：English' in sp


def test_explicit_output_language_overrides_env(monkeypatch):
    """显式 perception.engine.output_language=zh 优先于 MILOCO_APP_LANG=en。"""
    monkeypatch.setenv('MILOCO_APP_LANG', 'en')
    with patch('miloco.config.get_settings') as mock_gs:
        _patch_engine_settings(
            mock_gs, {'output_language': 'zh', 'global_system_prompt': ''}
        )
        sp = build_system_prompt(_full_video_scene(), include_home_profile=False)
    assert '输出语言：中文（简体）' in sp
    assert '输出语言：English' not in sp


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
async def test_run_pipeline_rule_only_skips_identity_and_audio(monkeypatch):
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

    async def _run():
        with patch(
            'miloco.perception.engine.omni.omni.call_omni',
            new_callable=AsyncMock,
            side_effect=_capture,
        ):
            return await run_pipeline(s, ctx, config)

    result = await _run()

    assert not result.skipped
    # 身份层直通：无 targets、无音频
    assert result.identity_packet is not None
    assert result.identity_packet.targets == []
    assert result.identity_packet.audio_clip.size == 0
    assert result.gate_packet.trigger.audio_active is False
    # 发给模型的载荷默认是图片**单帧**（settings 默认 last_frame_only=true），无音频
    payload = captured['payload']
    assert 'image_frames' in payload and 'video_base64' not in payload
    assert len(payload['image_frames']) == 1, '默认只送窗口末帧'
    assert 'audio_base64' not in payload
    # 输出只有规则命中
    assert result.omni_output is not None
    assert len(result.omni_output.matched_rules) == 1
    assert result.omni_output.matched_rules[0].rule_id == 'reading_light'
    assert result.omni_output.caption == []
    assert result.omni_output.suggestions == []
    assert result.omni_output.speeches == []
    # 多帧路径仍保留覆盖：显式关掉开关（monkeypatch 自动还原）→ 重跑一次，全窗口帧都发
    # （动作过程 / 手势时序类规则更稳）。
    from miloco.config import get_settings

    monkeypatch.setitem(
        get_settings().perception.engine.setdefault('input', {}), 'last_frame_only', False
    )
    await _run()
    assert len(captured['payload']['image_frames']) > 1, '关闭开关应多帧全发'


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