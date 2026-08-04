"""Omni Layer — Prompt Builder.

构建视频 (mp4) + crop images + 文本 prompt 供 MiMo API 调用。

包含两种 prompt 形态：

1. **纯文本 user_content**（``build_prompt`` / ``build_batch_prompt`` /
   ``build_stream_prompt`` / ``build_batch_stream_prompt`` / ``build_query_prompt``）
   —— 通用感知主调用使用。

2. **多模态 user_content list**（``build_fused_payload``）—— 身份识别 fused 主调用
   使用：把成员 body/face composite 图 + 视频 + 待识别 track 列表一次性发给 omni，
   让模型同时输出 caption / speeches / suggestions / identity_assignments，
   省一次独立的识别调用。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import av
import cv2
import numpy as np
from numpy.typing import NDArray

from miloco.config import get_settings
from miloco.perception.engine.identity.gallery_composite import (
    build_body_composite_png,
    build_face_composite_png,
    encode_jpeg_bytes,
    encode_png_bytes,
    hstack_to_height,
)
from miloco.perception.engine.types import IdentityPacket, IdentityTarget, OmniContext

from .constants import (
    _COMMONSENSE,
    _COMMONSENSE_AUDIO,
    _EXAMPLE_CHAIN,
    _EXAMPLE_CHAIN_NO_NAME,
    _EXAMPLE_IDENTITY,
    _HISTORY_HEADER,
    _OUTPUT_MODE_FREE,
    _OUTPUT_MODE_JSON,
    _PRINCIPLE,
    _PRINCIPLE_AUDIO,
    _PRINCIPLE_VIDEO_NO_AUDIO,
    _PRINCIPLE_VIDEO_NO_SPEECH,
    _ROLE,
    _ROLE_AUDIO,
    _USER_REF_BOUNDARY,
    _USER_REF_BOUNDARY_AUDIO,
)
from .field_registry import SceneDescriptor, render_field_spec, render_schema
from .home_profile_loader import get_home_profile_prefix, home_profile_has_pets
from .pet_refs import build_pet_reference_content
from .provider import LocalMediaInfo, OmniProviderAdapter


def _has_pets_for_scene() -> bool:
    """宠物注入门：``pet_recognition`` 开启 **且** 花名册非空。

    先查 feature 再探花名册（短路：关闭时连磁盘都不碰）。判据是**花名册**而非档案渲染出的
    「## 宠物」段——后者会被 token 预算归档 / 删条目 / 直接 ``pet add`` 抹掉，造成花名册与
    参考图都在而识别静默停摆（见 home_profile_has_pets 的 docstring）。
    统一驱动 caption/suggestions/matched_rules 命名纪律、参考图、pet_identities 的注入。
    """
    return get_settings().features.pet_recognition and home_profile_has_pets()

RouteType = Literal["video", "audio"]

if TYPE_CHECKING:
    from collections.abc import Callable

    from miloco.perception.engine.config import CropEnhanceConfig
    from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
    from miloco.perception.engine.identity.library import GallerySamples

logger = logging.getLogger(__name__)



# =============================================================================
# Fused mode 配置
# =============================================================================


@dataclass
class FusedPromptConfig:
    """fused 模式 prompt 渲染参数。

    每人渲染 1 张 body composite + 1 张 face composite——人脸用于精准匹配，
    全身用于体型 / 衣着辅助。比"每人 N 张独立 image_url"省 token 且识别效果更好。
    """

    gallery_body_height: int = 256        # body composite 拼接后高度
    gallery_face_height: int = 256        # face composite 拼接后高度
    # 注入 omni 的 composite 已改 PNG 无损编码, 此字段不再参与编码, 现无任何读取点;
    # 保留仅为配置/反序列化向后兼容(老配置可能仍带此键)。改回 jpeg 才会重新生效。
    jpeg_quality: int = 100
    include_face_composite: bool = True   # 是否带 face composite
    distinguish_strangers: bool = True
    # gallery 渲染上限：超出此值时仅取前 N 人（按 dict 迭代顺序），并 warning 提示。
    # 单人 body+face composite 占约 20-40KB jpeg ≈ 60-120KB base64 ≈ 15-30K tokens；
    # >10 人 prompt 容易超出 omni token 预算，需在配置或上游 gallery_snapshot 处控制。
    max_gallery_persons: int = 10
    # 宠物参考图上限（P2 / C-D1）：最多注入几只宠物（每只带其已存的 ≤3 张多姿态参考图）。
    # 家庭多 1-3 只覆盖 99%，仿人 gallery 上限保护；仅 has_pets（video route）时注入。
    # token 已实测（mimo-v2.5，composite 高 320px）：每只 ≈ +194 prompt tokens（3 只上限 ≈ +582），
    # 相对 fused 视频主体（数 K+）为小增量；注入形态 = 最多 3 只 × 每只 1 张 composite（≤3 姿态横拼）。
    # 待补：端到端延迟 / 成本（需真视频跑完整 fused，随默认开启前的实机验收一起量）。
    max_pet_refs: int = 3


# =============================================================================
# Public API — signatures unchanged, callers need no modification
# =============================================================================


def build_prompt(
    identity_packet: IdentityPacket,
    context: OmniContext,
    label_lookup: "dict[str, str] | None" = None,
) -> dict:
    """Build the prompt payload for the omni model (single device).

    Args:
        label_lookup: person_id (UUID) → 姓名/标签 反查表，渲染 "已识别人物" 段时把
                      UUID 替换为人名。None 时直接渲染 person_id 字段值（与旧行为兼容）。

    Returns dict with keys: system_prompt, user_content, video_base64, media_info, crops.
    """
    return _build_payload([identity_packet], context, stream=False, label_lookup=label_lookup)


def build_batch_prompt(
    identity_packets: list[IdentityPacket],
    context: OmniContext,
    label_lookup: "dict[str, str] | None" = None,
) -> dict:
    """Build the prompt payload for multi-device omni inference (same room)."""
    return _build_payload(identity_packets, context, stream=False, label_lookup=label_lookup)


def build_stream_prompt(
    identity_packet: IdentityPacket,
    context: OmniContext,
    label_lookup: "dict[str, str] | None" = None,
) -> dict:
    """Build prompt payload for streaming omni call (single device, speeches first)."""
    return _build_payload([identity_packet], context, stream=True, label_lookup=label_lookup)


def build_batch_stream_prompt(
    identity_packets: list[IdentityPacket],
    context: OmniContext,
    label_lookup: "dict[str, str] | None" = None,
) -> dict:
    """Build prompt payload for streaming omni call (multi-device, speeches first)."""
    return _build_payload(identity_packets, context, stream=True, label_lookup=label_lookup)


def build_query_prompt(
    identity_packets: list[IdentityPacket],
    query: str,
    last_caption: str | None = None,
    label_lookup: "dict[str, str] | None" = None,
) -> dict:
    """Build prompt for active user query — uses Identity results, free-text output."""
    parts = [
        _ROLE,
        _OUTPUT_MODE_FREE,
        _COMMONSENSE,
    ]
    home_profile = get_home_profile_prefix()
    if home_profile:
        parts.append(home_profile)
    # query 不接 crop(v1 范围外);走 _effective_panorama_short_edge() 兜掉历史 config.json 里
    # 可能残留的 0(早期哨兵),否则 _encode_video_mp4 会算出 scale=0 崩掉按需查询。
    video_b64, media_info = _encode_batch_video(
        identity_packets, short_edge=_effective_panorama_short_edge()
    )
    return {
        "system_prompt": "\n\n".join(parts),
        "user_content": _build_query_user_content(identity_packets, query, last_caption, label_lookup),
        "video_base64": video_b64,
        "media_info": media_info,
        "crops": [],
    }


def build_fused_payload(
    packets: list[IdentityPacket],
    context: OmniContext,
    candidates: list["IdentityQueryItem"],
    gallery_snapshot: dict[str, "GallerySamples"],
    config: FusedPromptConfig | None = None,
    label_lookup: "dict[str, str] | None" = None,
    adapter: OmniProviderAdapter | None = None,
    matching_moot: bool = False,
) -> dict:
    """构造 fused 主调用的 payload（身份识别和场景理解合并到同一次 omni 调用）。

    与 ``build_prompt`` 系列的核心差异：

    1. user content 是**多模态 list**（不再是纯 text）：gallery refs（每个 person
       文本+图）+ 主 video（mp4）+ 待识别 track 文本列表 + 输出 schema 描述。
    2. 输出 JSON 多一个字段 ``identity_assignments``：``[{"track_id":...,"name":...,
       "confidence":...,"reason":...}]``，由 ``response_parser._parse_identity_assignments``
       解析后回流给 ``FusedDispatcher.deliver_response``。

    Args:
        packets:           identity_packets（多设备时多个）
        context:           OmniContext（pending_speech / room_name 等）
        candidates:        本窗口待识别的 ``IdentityQueryItem`` 列表
                           （由 ``FusedDispatcher.take_pending`` 给出）
        gallery_snapshot:  当前候选 person → GallerySamples 的只读快照
        config:            FusedPromptConfig；None 走默认值
        label_lookup:      person_id → 姓名/标签 反查表（供 ``_build_device_header`` 渲染人名）；
                           None 时由本函数自动从 gallery_snapshot 构造
        matching_moot:     身份库为空（无注册成员）→ 成员匹配不可能。True 时 identities 字段
                           改精简版（只判 unknown/no_person）、gallery 段整段不渲染。no_person
                           判定链路不变（见 field_registry.IDENTITY_NO_MATCH）。

    Returns:
        dict，含字段：
          - ``messages``：直接构建好的 OpenAI 兼容 messages 列表（system + user）
          - ``candidate_track_ids``：本次 dispatch 候选 track id 列表（debug + 校验用）
    """
    cfg = config or FusedPromptConfig()
    if adapter is None:
        from miloco.config import get_settings

        from .provider import get_adapter as _get_adapter
        adapter = _get_adapter(get_settings().model.omni.model)
    if not packets:
        raise ValueError("build_fused_payload: packets 不能为空")

    if label_lookup is None:
        label_lookup = {
            pid: format_person_label(s.name, s.role)
            for pid, s in gallery_snapshot.items()
            if s.name
        }

    # audio route：无视觉信息，候选作废。与 video 同款 message 隔离（待判断规则/只读历史
    # 各自独立 user 消息）；本轮事实只放"当前时间 + 音频"——audio 无视频，不渲染名册/gallery/
    # 待识别 track（名册的 bbox 是为"把姓名对应到视频里的人"，audio 场景无意义）。
    if _resolve_route(packets) == "audio":
        scene = SceneDescriptor(route="audio", has_identity=False, stream=False)
        system_prompt = build_system_prompt(scene, include_home_profile=False, camera_prompt=context.camera_prompt)
        ep = packets[0]
        audio_b64 = _encode_audio_only_mp4(ep.audio_clip, ep.sample_rate)
        user_content: list[dict] = []
        if context.current_time:
            user_content.append({"type": "text", "text": f"当前时间: {context.current_time}"})
        if context.room_name:
            user_content.append({"type": "text", "text": f"位置: {context.room_name}"})
        if audio_b64 and len(audio_b64) >= _MIN_AUDIO_B64_LEN:
            user_content.append(adapter.build_audio_block(audio_b64, _audio_only_media_info(ep.sample_rate)))
        elif audio_b64:
            logger.warning(
                "event=fused_audio_b64_too_short size=%d (< %d), 跳过 input_audio 块, "
                "本窗口走 text-only",
                len(audio_b64), _MIN_AUDIO_B64_LEN,
            )
        return {
            "messages": _assemble_fused_messages(
                system_prompt=system_prompt,
                user_content=user_content,
                # audio-only 不做 matched_rules（见 field_registry）→ 不下发「# 待判断规则」段
                rule_conditions=None,
                readonly_history=_build_readonly_history(context),
            ),
            "candidate_track_ids": [],
        }

    # 自适应分辨率(Smart Crop):仅当本窗口无身份候选时才 crop —— 有候选时 fused prompt 的
    # bbox=[0,1000] 锚定全景做身份分配,换成 crop 视频会错位(方案 v1 身份门)。任何失败回退全景。
    # 先试裁切、失败才编全景:免掉裁切命中时白编一遍全景的浪费;且回退时全景字节最后 push,
    # 与模型实际所见一致(避免 clip 存 crop、模型看全景的产物不一致,见 snapshot_context)。
    video_b64: str | None = None
    media_info: "LocalMediaInfo | None" = None
    ref_image_jpeg: bytes | None = None
    # 「crop 生效」与「名册 bbox 会被换算进 crop 坐标系」必须同进同退:在此处一起构好回调,
    # 而不是把 region / frame_size 当两个独立可选参数往下传。漏传一个的后果是静默错配
    # (bbox 是全景坐标、视频却是 crop 画面,把姓名贴到 crop 中央那个人身上),且不会有测试变红。
    bbox_remap: "Callable[[tuple[int, int, int, int]], tuple[int, int, int, int] | None] | None" = None
    if not candidates:
        adaptive = _maybe_encode_adaptive(packets)
        if adaptive is not None:
            from .crop_enhance import remap_bbox_norm_to_crop

            video_b64, media_info = adaptive.video_b64, adaptive.media_info
            ref_image_jpeg = adaptive.ref_image_jpeg
            _region, _frame_size = adaptive.region, adaptive.frame_size

            def bbox_remap(b: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
                return remap_bbox_norm_to_crop(b, _region, _frame_size)
    if video_b64 is None:
        video_b64, media_info = _encode_batch_video(
            packets, short_edge=_effective_panorama_short_edge()
        )

    # has_speech 只由本轮 VAD 决定：本轮真有人声（含 pending 的延续语音）→ VAD 自然过、
    # 保留 speeches、模型把 <pending_speech> 拼成完整句；本轮无人声 → 剥 speeches，挂着的
    # pending 半句不强行补全（否则模型会就着噪声脑补出一个完成句，正是要根除的幻觉）。
    scene = SceneDescriptor(
        route="video", has_identity=bool(candidates), stream=False,
        has_audio=_batch_video_has_audio(packets),
        has_speech=_batch_video_has_speech(packets),
        has_pets=_has_pets_for_scene(),
        identity_match_disabled=matching_moot,
    )
    system_prompt = build_system_prompt(scene, include_home_profile=False, camera_prompt=context.camera_prompt)
    user_content = _build_fused_user_content(
        packets=packets,
        context=context,
        candidates=candidates,
        gallery_snapshot=gallery_snapshot,
        video_b64=video_b64,
        media_info=media_info,
        ref_image_jpeg=ref_image_jpeg,
        bbox_remap=bbox_remap,
        adapter=adapter,
        cfg=cfg,
        label_lookup=label_lookup,
        has_pets=scene.has_pets,  # 复用 scene 已算好的 has_pets，避免注入点再读一次 profile.md
        matching_moot=matching_moot,
    )

    messages = _assemble_fused_messages(
        system_prompt=system_prompt,
        user_content=user_content,
        rule_conditions=_render_rule_conditions(context),
        readonly_history=_build_readonly_history(context),
    )

    return {
        "messages": messages,
        "candidate_track_ids": [c.track_id for c in candidates],
    }


def _assemble_fused_messages(
    *,
    system_prompt: str,
    user_content: list[dict] | str,
    rule_conditions: str | None = None,
    readonly_history: str | None = None,
) -> list[dict]:
    """拼装 fused 调用的 messages：
    ``system → [家庭档案 user] → [待判断规则 user] → [只读历史 user] → 主 user``。

    家庭档案、待判断规则、只读历史均作为 system 之后、主 user 之前的独立 user 消息送入，
    为空则不插入。顺序按"越稳越靠前"（档案/规则变动慢 → 历史每窗变 → 本轮事实）。
    只读历史独占一条消息，靠 message 边界 + 段首声明界定其"仅供参考、非本轮事实"，
    替代散落各处的反污染禁令。
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    home_profile = get_home_profile_prefix()
    if home_profile:
        messages.append({"role": "user", "content": home_profile})
    if rule_conditions:
        messages.append({"role": "user", "content": rule_conditions})
    if readonly_history:
        messages.append({"role": "user", "content": readonly_history})
    messages.append({"role": "user", "content": user_content})
    return messages


def _render_rule_conditions(context: OmniContext) -> str | None:
    """渲染「# 待判断规则」段：每条 ``- <rule.name>：<query>``；无规则返回 None。

    rule_name 是 ``[task_id] 描述`` 形式的完整名称（逐条 rule 唯一），模型在 matched_rules
    里照抄它，response_parser 用 name→rule_id 映射还原回 UUID。sort by rule_id 求顺序确定。

    无规则时返回 None（整段不渲染）：此时 matched_rules 字段仍在 schema 里，「无规则段 →
    matched_rules 必须为空数组」的约束写在 field_registry 的 matched_rules spec 里（恒在
    system prompt 中），无需在此另插空消息破坏 message 结构。
    """
    if not context.rule_conditions:
        return None
    lines = [
        f"- {rc.rule_name or f'[{rc.rule_id}]'}：{rc.query}"
        for rc in sorted(context.rule_conditions, key=lambda x: x.rule_id)
    ]
    return "# 待判断规则\n" + "\n".join(lines)


def _build_readonly_history(context: OmniContext) -> str | None:
    """把历史参考（仅 pending_speech）拼成独立「只读历史」user 消息内容；
    无 pending_speech 时返回 None（首窗 / 常态不插该消息）。

    rule_conditions 不在此（它是"本轮待判断规则"、非历史，单独成「# 待判断规则」段）。
    历史与本轮事实分到不同 user 消息：靠 message 边界 + 段首声明替代散落的反污染禁令。
    """
    # last_caption / last_suggestions 已不再注入（见 _build_context_parts），只剩
    # pending_speech 这类客观跨窗事实可能需要 readonly 段。
    if not context.pending_speech:
        return None
    parts = _build_context_parts(context, stream=False)
    return _HISTORY_HEADER + "\n" + "\n".join(parts)


# =============================================================================
# Unified internal builder
# =============================================================================


def _build_payload(
    packets: list[IdentityPacket],
    context: OmniContext,
    *,
    stream: bool,
    label_lookup: "dict[str, str] | None" = None,
    include_home_profile: bool = True,
) -> dict:
    route = _resolve_route(packets)
    # has_audio：video 路由下音频未过 gate 时为 False → schema 剥掉 speeches/env_sounds，
    # 避免模型就着画面脑补人声。audio 路由恒有音频。
    # has_speech：video 路由下 VAD 判无人声时为 False → 只剥 speeches、保留 env_sounds。
    has_audio = True if route == "audio" else _batch_video_has_audio(packets)
    # has_speech 只由本轮 VAD 决定：本轮真有人声（含 pending 的延续语音）→ VAD 自然过、
    # 拼接照常；本轮无人声 → 剥 speeches，挂着的 pending 半句不强行补全（否则模型会就着
    # 噪声脑补出完成句，正是要根除的幻觉）。
    has_speech = True if route == "audio" else _batch_video_has_speech(packets)
    scene = SceneDescriptor(
        route=route, has_identity=False, stream=stream,
        has_audio=has_audio, has_speech=has_speech,
        has_pets=_has_pets_for_scene(),
    )
    user_text = _build_user_content(
        packets, context, stream=stream, label_lookup=label_lookup,
    )
    base: dict = {
        "system_prompt": build_system_prompt(scene, include_home_profile=include_home_profile, camera_prompt=context.camera_prompt),
        "user_content": user_text,
        "crops": [],
    }
    if route == "audio":
        ep = packets[0]
        base["audio_base64"] = _encode_audio_only_mp4(ep.audio_clip, ep.sample_rate)
        base["media_info"] = _audio_only_media_info(ep.sample_rate)
    else:
        # 自适应分辨率(Smart Crop)只接 fused 生产路径。此路(非 fused/legacy)不裁切:
        # crops 通道把参考图渲染在 video 之后且无说明文字,模型会把局部裁切当整个房间描述
        # (反而比不接更糟)。非生产路径不值得为它复刻 fused 的「参考图在前+说明」结构,
        # 恒走全景 = 字节等同本 PR 之前的行为(零回归)。
        video_b64, media_info = _encode_batch_video(
            packets, short_edge=_effective_panorama_short_edge()
        )
        base["video_base64"] = video_b64
        base["media_info"] = media_info
    return base


# =============================================================================
# System prompt (unified)
# =============================================================================


def build_system_prompt(
    scene: SceneDescriptor,
    *,
    include_home_profile: bool = True,
    camera_prompt: str | None = None,
) -> str:
    """按场景装配 system prompt。

    结构：``角色 → 输出模式 → # 任务 → # 输出格式(schema) → # 字段说明 → # 提醒判定
    → # 通用常识 → # 输出实例 → [家庭档案?]``。schema / 字段说明 / 实例 / 任务行均按
    ``scene`` 选取（audio 场景剥 caption/identity；有身份候选才带 identity 与实例 A），
    同场景前缀稳定，利于 omni 服务端 prefix cache。

    流程不再单列「工作流程」段——各任务（含 suggestions 的触发与 urgency 判定）的细则
    全部内联进对应「# 字段说明」的 ``## 字段`` 块。

    ``include_home_profile=False`` 时不在 system 注入家庭档案——fused 路径改为独立 user
    消息送入（见 ``build_fused_payload`` / ``_assemble_fused_messages``）。
    """
    is_audio = scene.route == "audio"
    role = _ROLE_AUDIO if is_audio else _ROLE
    if is_audio:
        principle = _PRINCIPLE_AUDIO
    elif not scene.has_audio:
        # video 路由但音频未过 gate：用无音频变体，原则不再提 speeches/env_sounds/转录
        principle = _PRINCIPLE_VIDEO_NO_AUDIO
    elif not scene.has_speech:
        # video 路由、音频过 gate 但 VAD 判无人声：用无人声变体，原则不再提 speeches/转录
        principle = _PRINCIPLE_VIDEO_NO_SPEECH
    else:
        principle = _PRINCIPLE
    commonsense = _COMMONSENSE_AUDIO if is_audio else _COMMONSENSE
    parts: list[str] = [
        role,
        _OUTPUT_MODE_JSON,
        principle,
        _render_task_list(scene),
        "# 输出格式\n\n" + _render_schema_section(scene),
        "# 字段说明\n\n" + render_field_spec(scene),
        commonsense,
        _render_examples(scene),
    ]
    if include_home_profile:
        home_profile = get_home_profile_prefix()
        if home_profile:
            parts.append(home_profile)
    # camera_prompt — 低频变动，放在 system prompt 尾部 → prefix cache 能命中前面的共享前缀
    note = camera_prompt.strip() if camera_prompt else ""
    if note:
        parts.append(
            "## 本摄像头须知\n\n"
            "以下是该机位的环境说明（要关注/忽略什么），请严格遵循以下指导进行感知描述——\n" + note
        )
    return "\n\n".join(p for p in parts if p)


def _render_schema_section(scene: SceneDescriptor) -> str:
    """schema 字面量；stream 场景前缀加「严格按字段顺序输出」提示（speeches 先出抢延迟）。"""
    schema = render_schema(scene)
    if scene.stream:
        order = " → ".join(f.name for f in scene.selected_fields())
        return f"必须严格按字段顺序输出：{order}\n{schema}"
    return schema


def _render_task_list(scene: SceneDescriptor) -> str:
    """按场景渲染「# 任务」概览（动态编号）：身份识别仅有候选时、视频理解仅 video 场景；
    规则/建议措辞按 route 取"视频和音频"或"音频"（audio 场景不提视频）。"""
    # 措辞跟随本轮实际模态：video 无音频时只提"视频"，不提音频（与剥离的 schema 一致）
    if scene.route == "audio":
        av = av2 = "音频"
    elif scene.has_audio:
        av, av2 = "视频和音频", "视频、音频"
    else:
        av = av2 = "视频"
    items: list[str] = []
    if scene.has_identity:
        if scene.identity_match_disabled:
            # 库空：没有成员可对照，任务收敛为「判真人 / 非人误检」，与精简版 identities spec 一致，
            # 不再写「对照图片库…库中哪一位」这类成员匹配任务（否则与精简 spec 自相矛盾、白占 token）。
            items.append("身份识别：判断画面中每个目标是真人还是被误检成人的非人物体（本轮无注册成员，不做成员匹配）")
        else:
            items.append("身份识别：对照图片库，识别画面中的人对应库中哪一位（或都不是）")
    if scene.route == "video":
        items.append("视频理解：描述画面中的人、宠物、物体，优先描述动态部分")
    if scene.has_audio:
        # 无人声(VAD 判定)时不提"转录人声"，与剥掉的 speeches schema 一致、不重新诱导脑补
        if scene.has_speech:
            items.append("音频理解：有清晰人声才转录，有明确非人声事件才记环境音")
        else:
            items.append("音频理解：有明确非人声事件才记环境音")
    # matched_rules 仅 video 路由有（audio-only 剥离，见 field_registry），故规则判断任务也仅 video
    if scene.route == "video":
        items.append(f"规则判断：基于本轮{av}判断\"# 待判断规则\"是否成立")
    items.append(f"常识建议：结合通用常识/家庭档案，判断本轮{av2}内是否有事件需要提醒")
    lines = ["# 任务"] + [f"{i}. {t}" for i, t in enumerate(items, 1)]
    return "\n".join(lines)


def _render_examples(scene: SceneDescriptor) -> str:
    """实例 B（事件链）带视觉场景；实例 A（身份）仅有身份候选场景带。

    audio 场景无 caption/identity 字段，两条实例的输出均含 caption（视觉），与 audio
    schema 不符，故 audio 不附实例——其输出字段少、已由「# 字段说明」充分约束。

    has_audio=False（video 路由音频未过 gate）同理：两条实例的输出都含 speeches /
    env_sounds 等音频派生字段，而此时 schema 已把它们剥掉；附上会与 schema 自相矛盾、
    并可能诱导模型照搬音频字段，故一并不附（caption/suggestions 由「# 字段说明」约束）。

    has_speech=False（VAD 判无人声、speeches 已剥）时：实例 A 的输出含 speeches（且是
    needs_response 指令），留着会与剥掉的 schema 矛盾、并重新诱导脑补人声指令，故不附
    实例 A（身份判定已由「## identities」充分约束）；实例 B 无 speeches、照常附。

    identity_match_disabled=True（库空）时同样不附实例 A：它演示的是成员匹配（摆
    ``<gallery>`` 成员、输出成员名 + 五官匹配 reason），与库空的精简版 identities
    spec / schema（只判 unknown/no_person、无 gallery）自相矛盾，且抵消库空省 token 的
    目标；身份任务已由精简版「## identities」充分约束。实例 B 无 identities 字段、照常附。
    """
    if scene.route == "audio" or not scene.has_audio:
        return ""
    examples = []
    if scene.has_identity and scene.has_speech and not scene.identity_match_disabled:
        examples.append(_EXAMPLE_IDENTITY)
    # 库空时实例 B 用泛称版：此窗无成员铺垫（实例 A 已 gate 掉），caption 示范不该叫专名，
    # 与「库空不产成员名」收敛一致。库非空照旧用带名版（其"小明"由上方实例 A 的 gallery 铺垫）。
    examples.append(
        _EXAMPLE_CHAIN_NO_NAME if scene.identity_match_disabled else _EXAMPLE_CHAIN
    )
    return "# 输出实例\n\n" + "\n\n".join(examples)


# =============================================================================
# User content (unified)
# =============================================================================


def _log_user_content(content: "str | list[dict]") -> None:
    """Debug：打印实际传给模型的 user 文本内容（剔除 video/audio/image 等媒体块）。

    非 fused 路径的 content 本就是纯文本 str；fused 路径是 content 块列表，
    只取 type=="text" 的块拼出来看。仅 DEBUG 级生效，避免常态开销。
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    if isinstance(content, str):
        text = content
    else:
        text = "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    # 单行输出：真实换行渲染成字面 \n，方便 grep / 不被其它日志打断
    logger.debug("[user-content] %s", text.replace("\n", "\\n"))


def _build_user_content(
    packets: list[IdentityPacket],
    context: OmniContext,
    *,
    stream: bool = False,
    label_lookup: "dict[str, str] | None" = None,
) -> str:
    # 非 fused 兜底路径：单条 user 文本，规则 + 历史 + 本轮事实内联（fused 路径才把它们
    # 拆成独立 message）。规则用新「# 待判断规则」格式，与 fused 一致。
    parts: list[str] = []
    is_video = _resolve_route(packets) == "video"
    # matched_rules 仅 video 路由有（audio-only 剥离）→ audio 不下发「# 待判断规则」段
    if is_video:
        rule_conditions = _render_rule_conditions(context)
        if rule_conditions:
            parts.append(rule_conditions)
        # 名册是视频特征（定位画面里的人），audio route 无视频 → 不渲染
        parts.extend(_build_device_header(packets, label_lookup=label_lookup))
    parts.extend(_build_context_parts(context, stream=stream))
    if context.current_time:
        parts.append(f"当前时间: {context.current_time}")
    if context.room_name:
        parts.append(f"位置: {context.room_name}")
    parts.append(_USER_REF_BOUNDARY if is_video else _USER_REF_BOUNDARY_AUDIO)
    text = "\n".join(parts)
    _log_user_content(text)
    return text


def _build_fused_user_content(
    *,
    packets: list[IdentityPacket],
    context: OmniContext,
    candidates: list["IdentityQueryItem"],
    gallery_snapshot: dict[str, "GallerySamples"],
    video_b64: str | None,
    media_info: LocalMediaInfo | None,
    ref_image_jpeg: bytes | None = None,
    bbox_remap: "Callable[[tuple[int, int, int, int]], tuple[int, int, int, int] | None] | None" = None,
    adapter: OmniProviderAdapter,
    cfg: FusedPromptConfig,
    label_lookup: "dict[str, str] | None" = None,
    has_pets: bool = False,
    matching_moot: bool = False,
) -> list[dict]:
    """构建 user 消息的 content 列表（text/image_url/video_url 块交错）。

    fused 模式专用：与纯文本版 ``_build_user_content`` 不同，本函数返回
    ``list[dict]``（OpenAI 多模态 content array），不是 ``str``。

    ``matching_moot=True``（身份库为空）时整个 gallery 段不渲染——库空无成员可比对，
    identities 已由精简版 spec 指示"只判 unknown/no_person"（见 build_fused_payload），
    此处不再塞"<gallery>库为空…"这类无用文本。待识别 track 列表仍照常渲染（no_person 判定按 track 给结论）。
    """
    gallery_content: list[dict] = []

    # === gallery refs（仅当本轮有 candidate 时渲染；U4 顺序里置于"待识别 track"之后、video 之前）===
    # 「全或无」语义：任一候选 person 的 body composite 全套兜底都失败，本窗口放弃
    # 整个 gallery 段（等价无 gallery 主调用）。原因：少注入一个人，画面里若真有该
    # 人，omni 容易把他的脸贴到 gallery 里最相似的另一位 → 错认（caption/speeches
    # 全跟着错），代价比"漏识别"高一个量级。face 是 nice-to-have，单人 face 失败不
    # 触发放弃。
    #
    # matching_moot（身份库为空）→ 整段跳过：库空无成员可比对，精简版 identities spec 已
    # 指示只判 unknown/no_person，不需要 gallery 图，也不再塞"库为空"占位文本。
    if candidates and not matching_moot:
        if gallery_snapshot:
            # 渲染上限保护：超出 cfg.max_gallery_persons 仅取前 N 人，避免 prompt token 爆
            gallery_items = list(gallery_snapshot.items())
            if len(gallery_items) > cfg.max_gallery_persons:
                logger.warning(
                    "gallery person count %d > max_gallery_persons=%d，仅渲染前 %d 人",
                    len(gallery_items), cfg.max_gallery_persons, cfg.max_gallery_persons,
                )
                gallery_items = gallery_items[: cfg.max_gallery_persons]

            # 两段式：先 pre-flight 每人都能拿到 body_jpg，全过才进入渲染段
            prepared: list[tuple[str, str, bytes, "bytes | None"]] = []  # (pid, label, body_jpg, face_jpg|None)
            give_up_reason: str | None = None
            for pid, samples in gallery_items:
                body_jpg = _resolve_person_body_jpg(samples, cfg)
                # 同时拦 None / empty / "非 None 但 size 异常小" 的坏 bytes (后者
                # 通常是 library 缓存里的半截损坏 jpeg, 直接进 payload 会让 omni
                # 服务端 400 Multimodal data is corrupted)
                if not body_jpg or len(body_jpg) < _MIN_JPEG_BYTES:
                    give_up_reason = (
                        f"person_id={pid} name={samples.name!r} "
                        f"body composite 全部兜底来源均失败 "
                        f"(jpg={len(body_jpg) if body_jpg else 0} bytes)"
                    )
                    break
                face_jpg = (
                    _resolve_person_face_jpg(samples, cfg)
                    if cfg.include_face_composite else None
                )
                # face 是 nice-to-have, size 不达标降级为 None (跳过本人 face 块,
                # 其他人 body/face 仍渲染), 不触发整 gallery 放弃。
                if face_jpg and len(face_jpg) < _MIN_JPEG_BYTES:
                    logger.warning(
                        "event=fused_face_jpg_too_short person_id=%s name=%r "
                        "size=%d 字节 (< %d), 跳过该人 face 块",
                        pid, samples.name, len(face_jpg), _MIN_JPEG_BYTES,
                    )
                    face_jpg = None
                # 名册/gallery/输出统一用纯真名（角色上下文在「# 家庭档案」里）；
                # name_to_pid 对纯名有 key，omni 输出 name 即可反查回 UUID
                prepared.append((pid, samples.name or pid, body_jpg, face_jpg))

            if give_up_reason is not None:
                # 整 gallery 放弃 —— 不渲染 gallery 段，本窗口等价于无 gallery 主调用
                logger.warning(
                    "event=fused_gallery_giveup 触发整 gallery 放弃（全或无）：%s；"
                    "本窗口跳过 gallery 段，identity 信息退化为 unknown，避免错认",
                    give_up_reason,
                )
            else:
                gallery_content.append({"type": "text", "text": "下方 gallery 为候选成员参考图；图中衣着仅样本采集当时所穿、不保证与本轮一致——衣着只作辅助参考、不作决定性判据，以面部/体型/发型为主"})
                from miloco.perception.snapshot_context import push_gallery_image

                gallery_content.append({"type": "text", "text": "<gallery>"})
                for pid, label, body_jpg, face_jpg in prepared:
                    gallery_content.append({"type": "text", "text": f"【{label}】"})
                    gallery_content.append({"type": "text", "text": "体型/全身参考："})
                    gallery_content.append(_png_block(body_jpg))
                    push_gallery_image(pid, "body", body_jpg)
                    if face_jpg:
                        gallery_content.append({"type": "text", "text": "面部参考："})
                        gallery_content.append(_png_block(face_jpg))
                        push_gallery_image(pid, "face", face_jpg)
                gallery_content.append({"type": "text", "text": "</gallery>"})
        else:
            gallery_content.append({"type": "text", "text": "<gallery>库为空，所有 track 应输出 unknown</gallery>"})

    # 按 U4 顺序组装：当前时间 → 已识别人物 → 待识别 track → gallery → video → identities 约束。
    # 历史参考（pending_speech）与待判断规则已抽到独立 user 消息
    # （见 _assemble_fused_messages），此处主 user 只放本轮事实。
    content: list[dict] = []

    # 1. 当前时间 + 位置
    if context.current_time:
        content.append({"type": "text", "text": f"当前时间: {context.current_time}"})
    if context.room_name:
        content.append({"type": "text", "text": f"位置: {context.room_name}"})

    # 2. 已识别人物 / 陌生人 名册（含 bbox；进入"待识别 track"的 track 从名册剔除，去先验+去冗余）
    #
    # bbox_remap 非 None ⟺ Smart Crop 生效(由 build_fused_payload 与 crop 视频一起构好)。
    # 此时主视频是局部放大画面,而 bbox 由 identity 侧按**全景整帧**归一化
    # (engine._normalize_bbox_to_1000(…, all_frames[-1])),必须换算进 crop 坐标系 —— 否则
    # 拿全景坐标读局部画面是方向性错误(会把姓名贴到 crop 中央那个人身上),不是精度损失。
    # 换算失败(框落在区域外)由 _render_roster_entry 退化为纯名。
    # candidates 侧不需要同等处理:身份门(build_fused_payload)保证有候选就不裁切。
    candidate_tids = {c.track_id for c in candidates}
    roster_lines = _build_device_header(
        packets, label_lookup=label_lookup, candidate_tids=candidate_tids, emit_bbox_note=False,
        bbox_remap=bbox_remap,
    )
    for line in roster_lines:
        content.append({"type": "text", "text": line})

    # 3. 待识别 track 列表（仅数据，识别规则已在 system prompt # 字段说明 中）
    if candidates:
        content.append({"type": "text", "text": "待识别 track："})
        for cand in candidates:
            content.append({"type": "text", "text": _format_track_line(cand)})

    # 已识别人物/陌生人 + 待识别 track 共用一句 bbox 坐标系说明（二者同一 [0,1000] 约定，去重）
    #
    # 恒锚**视频末帧**,crop 与否都一样:crop 生效时 bbox 已在上面换算进 crop 坐标系
    # (bbox_remap),与视频画面同坐标系;不 crop 时视频本就是全景。所以参考图块建不建得出来
    # 不再影响这句话的正确性 —— 这是换算相对"锚全景参考图"方案的净简化。
    #
    # 「末帧」必须写明:bbox 只标末帧位置(engine._normalize_bbox_to_1000(…, all_frames[-1])),
    # 而视频跨整个窗口。窗内有人走动时不说清是哪一帧,模型可能拿它去读中间帧、贴错人。
    # 这个模糊在接 Smart Crop 之前就存在(全景视频同样跨整窗),crop 只是把它放大了
    # (视野变窄后同样的位移在画面里占比更大)。
    # 记下这句到底发没发,供 4.5 的括注复用 —— 两处条件不能各写各的,否则名册 bbox 全被撤掉
    # (换算失败 / 全员 coasting 无框)时,括注会去指一个 prompt 里并不存在的「上文 bbox」。
    bbox_note_emitted = any("[bbox=" in ln for ln in roster_lines) or bool(candidates)
    if bbox_note_emitted:
        content.append({"type": "text", "text": (
            "上方已识别人物、陌生人及待识别 track 中的 bbox=(x1, y1, x2, y2) 均为视频**最后一帧**中"
            "归一化到 [0, 1000] 区间的位置（左上 0,0；右下 1000,1000），"
            "用于把姓名 / track_id 对应到视频里的人；画面中的人在窗口内可能移动，靠前的帧以视觉为准。"
        )})

    # 参考帧图块:引导语与图块同进同退,避免只留文字不留图。
    # 注:唯一调用方 build_fused_payload 侧的 _maybe_encode_adaptive 已用同一条件
    # (len >= _MIN_JPEG_BYTES)校验过,故 except 分支当前不可达;此处是契约防御,不是在修线上 bug。
    # 防御范围仅限本函数产出的 prompt content:旁路落盘的 ref.jpg / has_ref 在
    # _maybe_encode_adaptive 里就已 push_ref_frame,不随这里的降级回滚 —— 真走到该分支时,
    # 复盘页仍会展示一张模型其实没看进 prompt 的参考图(要对齐得把落盘挪到 prompt 之后)。
    ref_block: dict | None = None
    if ref_image_jpeg is not None:
        try:
            ref_block = _jpeg_block(ref_image_jpeg)
        except ValueError:
            logger.warning("event=adaptive_ref_jpeg_bad 跳过参考帧块(引导语同步不发;bbox 已换算进 crop 坐标系,不受影响)")

    # 4. gallery（候选成员参考图，紧邻 video 便于视觉比对）
    content.extend(gallery_content)

    # 4.5. 已登记宠物多姿态参考图（P2）——仅 has_pets 时注入（用上游 scene 已算好的值，不重读盘）；
    # 读盘/编码失败或无图则空，退化为纯文字（PET_NAMING_SPEC + 档案「## 宠物」段仍在，不阻断识别）。
    if has_pets:
        content.extend(build_pet_reference_content(max_pets=cfg.max_pet_refs))

    # 4.6 自适应分辨率:全景参考帧(置于 video 前,「全景图在前、活动区域放大视频在后」)。
    # 它只补全局场景上下文(裁切丢掉的视野),**不**再充当 bbox 锚点 —— bbox 已换算进 crop
    # 坐标系、直接锚视频,措辞不能再把模型往这张图上引。
    # 必须排在宠物参考图之后:引导语写的是「下方第一张图…随后的视频」,中间再插图这话就不成立。
    if ref_block is not None:
        # 括注只在上文真有 bbox 时才发,否则是悬空指代(与引导语/图块同进同退同一条原则)
        anchor_hint = "（上文 bbox 对应放大后的视频，不是这张全景图）" if bbox_note_emitted else ""
        content.append({"type": "text", "text": (
            "下方第一张图为全景场景参考，随后的视频是画面中活动区域的放大——"
            f"请结合两者理解场景与细节{anchor_hint}。"
        )})
        content.append(ref_block)

    # 5. 主 video
    # video_b64 size sanity check — PyAV 编码异常情况下可能返回非空但损坏的极短
    # base64 串, 入 payload 会让 omni 服务端 400 Multimodal data is corrupted。
    # 太短 → 跳过 video_url 块, 退化为"无视频窗口"(text + gallery 仍能识别)。
    if video_b64 and len(video_b64) >= _MIN_VIDEO_B64_LEN:
        content.append(adapter.build_video_block(video_b64, media_info))
    elif video_b64:
        logger.warning(
            "event=fused_video_b64_too_short size=%d (< %d), 跳过 video_url 块, "
            "本窗口走 text-only 识别",
            len(video_b64), _MIN_VIDEO_B64_LEN,
        )

    _log_user_content(content)
    return content


def _is_stranger_pid(pid: str) -> bool:
    """person_id 是否为"已确认陌生人"。兼容 unknown / unknown_<n> / unknown-<scope>-<n>。"""
    return pid == "unknown" or pid.startswith("unknown_") or pid.startswith("unknown-")


def _is_confirmed_member_pid(pid: str) -> bool:
    """person_id 是否为"已确认成员"（真实 UUID）。排除 none/""/pending/pending:/unknown*。"""
    if pid in ("none", "", "pending") or pid.startswith("pending:"):
        return False
    return not _is_stranger_pid(pid)


def _drop_bbox(_b: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """恒撤掉 bbox 的 remap 回调：crop 已生效、但换算基准不适用于当前 packet 时用。

    **不能用 ``None`` 代替** —— ``None`` 在 :func:`_render_roster_entry` 里表示"不必换算"、
    会原样渲染全景坐标；而需要本回调的场景恰恰是 crop 已生效、主视频是局部画面，那正是
    要根除的错配。宁可只给姓名不给位置。
    """
    return None


def _render_roster_entry(
    t: IdentityTarget,
    label_lookup: "dict[str, str] | None",
    bbox_remap: "Callable[[tuple[int, int, int, int]], tuple[int, int, int, int] | None] | None" = None,
) -> str:
    """名册单项：``名[bbox=(x1, y1, x2, y2)]``；无 bbox（coasting 本帧未检测）退化为纯名。

    ``bbox_remap`` 非 None 时（Smart Crop 生效），先把全景 [0,1000] 坐标换算进 crop
    坐标系；换算失败（框落在 crop 区域外）同样退化为纯名——宁可只给姓名不给位置，
    也不能输出与画面错配的坐标。
    """
    label = _format_target(t, label_lookup)
    bbox = t.bbox_xyxy_norm
    if bbox is not None and bbox_remap is not None:
        bbox = bbox_remap(bbox)
    if bbox is not None:
        x1, y1, x2, y2 = bbox
        return f"{label}[bbox=({x1}, {y1}, {x2}, {y2})]"
    return label


def _build_device_header(
    packets: list[IdentityPacket],
    label_lookup: "dict[str, str] | None" = None,
    candidate_tids: "set[int] | frozenset[int]" = frozenset(),
    emit_bbox_note: bool = True,
    bbox_remap: "Callable[[tuple[int, int, int, int]], tuple[int, int, int, int] | None] | None" = None,
) -> list[str]:
    """渲染人物名册段，按身份状态分桶（只放"已定身份"的 track，含归一化位置）：

      - ``已识别人物：`` —— 已确认成员，渲染 ``真名[bbox=(...)]``（恒输出，空则"无"）
      - ``陌生人：``     —— 已确认陌生人，渲染 ``陌生人#n[bbox=(...)]``（无则不输出该行）

    pending / 未识别（none）**不进名册**——它们要么本窗在"待识别 track"列表里被识别、
    要么只在视频里（omni 自行观察），名册不替它们重复声明。

    ``candidate_tids`` 是本窗进入"待识别 track"列表的 track（含到点重审的 confirmed /
    unknown）；这些 track **从名册剔除**：避免把它们的当前身份当先验注入、锚定 omni
    重审投票（破坏投票独立性），同时消除"同一人同窗被注入两次"的冗余。

    ``suppress_as_prior=True`` 的 target（翻身份黏旧名期 track）同样剔除——它本窗若 coasting
    未派发就不在 candidate_tids 里，靠此标记兜住，防黏住的旧名当先验把翻转翻不动。

    ``bbox_remap``（Smart Crop 生效时把全景 bbox 换算进 crop 坐标系）**只对单 packet 成立**：
    回调闭包里的 region / frame_size 取自 ``_maybe_encode_adaptive`` 选中的「首个有帧设备」，
    套到设备 2..N 的名册上会吐出跨设备的错坐标（或被判「落在区域外」而静默丢掉位置）。
    当前 fused 恒单 packet（``run_omni_fused`` 传 ``[omni_packet]``）；多 packet 时主动弃用
    换算、退化为纯名——宁可只给姓名不给位置。
    """
    if bbox_remap is not None and len(packets) > 1:
        logger.warning(
            "event=roster_bbox_remap_multi_packet n_packets=%d "
            "crop 区域只属于首个设备,整体撤掉 bbox 退化为纯名",
            len(packets),
        )
        # 注意不能置 None —— 那会让 _render_roster_entry 回落到**全景原坐标**,而主视频
        # 此时是 crop 视频,正是本次改动要根除的错配。改挂恒撤掉 bbox 的哨兵回调。
        bbox_remap = _drop_bbox

    def _bucket(ep: IdentityPacket) -> tuple[list[str], list[str]]:
        members: list[str] = []
        strangers: list[str] = []
        for t in ep.targets:
            # candidate_tids（本窗派发重审）+ suppress_as_prior（翻身份黏旧名 track，
            # coasting 窗不在 candidate_tids 内）均剔出名册，避免旧/当前身份当先验锚定 omni
            if t.track_id in candidate_tids or t.suppress_as_prior:
                continue
            if _is_confirmed_member_pid(t.person_id):
                members.append(_render_roster_entry(t, label_lookup, bbox_remap))
            elif _is_stranger_pid(t.person_id):
                strangers.append(_render_roster_entry(t, label_lookup, bbox_remap))
            # none / pending / pending:<id> → 不进名册
        return members, strangers

    if len(packets) == 1:
        members, strangers = _bucket(packets[0])
        lines = [f"已识别人物：{', '.join(members) if members else '无'}"]
        if strangers:
            lines.append(f"陌生人：{', '.join(strangers)}")
    else:
        lines = []
        for i, ep in enumerate(packets, 1):
            lines.append(f"--- 设备 {i} ---")
            members, strangers = _bucket(ep)
            lines.append(f"  已识别人物：{', '.join(members) if members else '无'}")
            if strangers:
                lines.append(f"  陌生人：{', '.join(strangers)}")
            lines.append("")

    # 名册含位置时附一句坐标系说明（非 fused 路径用）；fused 路径传 emit_bbox_note=False，
    # 由 _build_fused_user_content 统一出一句覆盖名册 + 待识别 track，避免两处重复。
    # 「最后一帧」与 fused 侧同口径:bbox 只标末帧位置(engine._normalize_bbox_to_1000 按
    # all_frames[-1] 归一化),视频却跨整个窗口,不写明会让模型拿它去读中间帧。
    # 此路(非 fused/legacy)恒走全景、不接 Smart Crop,故无需坐标换算。
    if emit_bbox_note and any("[bbox=" in ln for ln in lines):
        lines.append(
            "上方已识别人物、陌生人中 [bbox=(x1, y1, x2, y2)] 为该人在视频**最后一帧**中归一化到 [0, 1000] 区间的位置"
            "（左上 0,0；右下 1000,1000），用于把姓名对应到视频里的人；画面中的人在窗口内可能移动，靠前的帧以视觉为准。"
        )
    return lines


def _build_context_parts(context: OmniContext, *, stream: bool = False) -> list[str]:
    """构建历史参考段：仅 pending_speech（last_caption / last_suggestions 已停止注入）。

    rule_conditions 不在此——它是"本轮待判断规则"、非历史，由 ``_render_rule_conditions``
    单独渲染成「# 待判断规则」段。本函数被 ``_build_readonly_history``（fused 只读历史
    message）与 ``_build_user_content``（非 fused 内联）共用。
    """
    parts: list[str] = []

    # 注：last_caption / last_suggestions 不再注入——回灌模型自己的上轮结论会形成
    # 回声室、强化幻觉（caption 复读、同一 suggestion 反复重报）。caption 的变化去重
    # 与 suggestion 的事件链去重都已下沉到代码（见 api.py 的 _last_captions 比对、
    # assign_id_and_update_link 的语义匹配）。此处只保留 pending_speech 这类模型无法
    # 重新推导的客观跨窗事实。

    # 上一窗没说完的半句（last_speech）+ 续接判断：data 与指令捆在一起放 user 段。
    # 早先把指令放 system spec、user 只留裸标签 → 实测模型完全不拼接（跨消息检索 +
    # salience 太低）；放这里、且用"看能否拼"的判断式（而非"必须拼"的祈使式），模型才会
    # 按"拼起来语义是否连贯完整"决定拼 / 不拼，且不把指令复读进 content。
    if context.pending_speech:
        contents = "；".join(ps["content"] for ps in context.pending_speech)
        parts.append(
            f"last_speech：{contents}\n"
            f"上一窗有人说到一半没说完=「{contents}」。本轮若也有 speech，看本轮 speech 内容能否"
            f"和上一窗的 last_speech 拼接在一起：拼接后语义连贯完整 → 输出拼接后的整句、"
            f"is_complete=true；拼接后语义不完整 / 矛盾 / 本轮与它无关 → 仅输出本轮 speech 内容，"
            f"不要拼接、也不能用 last_speech 改写本轮。"
        )

    return parts


def format_person_label(name: str | None, role: str | None) -> str | None:
    """把真名 + 家庭角色拼成 prompt 显示标签：``真名(角色:爸爸)``；role 为空只显真名。

    name 为空时返回 None，由调用方兜底为 person_id（理论上 backfill 后 name 必有）。
    """
    if not name:
        return None
    if role:
        return f"{name}(角色:{role})"
    return name


def _format_target(
    t: IdentityTarget,
    label_lookup: "dict[str, str] | None" = None,
) -> str:
    """把 IdentityTarget 渲染成 prompt 中的人物标签。

    person_id 字段值的含义：
      - ``"none"`` / ``""``       → 未识别
      - ``"pending"``             → 待确认
      - ``"pending:<person_id>"`` → 待确认·疑似 X（X 为 label_lookup 反查结果）
      - ``"unknown"``             → 陌生人
      - ``"unknown_<n>"``         → 陌生人#n（distinguish=true 时）
      - 其他                      → 已确认成员（按 label_lookup 反查渲染姓名）

    Args:
        t:              IdentityTarget；person_id 携带状态信息
        label_lookup:   person_id → 姓名/标签 映射；None 时直接显示 person_id
    """
    pid = t.person_id

    if pid == "none" or pid == "":
        return "未识别"

    if pid == "pending":
        return "待确认"

    # pending 阶段带 candidate（"pending:<person_id>"）
    if pid.startswith("pending:"):
        cand = pid.split(":", 1)[1]
        cand_label = (label_lookup or {}).get(cand, cand)
        return f"待确认·疑似{cand_label}"

    # 陌生人：兼容老格式 ``unknown_<n>`` 和新格式 ``unknown-<scope>-<n>``
    if pid == "unknown":
        return "陌生人"
    if pid.startswith("unknown_"):
        idx = pid.split("_", 1)[1]
        return f"陌生人#{idx}"
    if pid.startswith("unknown-"):
        # 新格式：unknown-{scope_label}-{idx}；展示带 scope 帮助 omni 区分跨镜头陌生人
        return f"陌生人#{pid[len('unknown-'):]}"

    # confirmed：反查 label_lookup
    label = (label_lookup or {}).get(pid, pid)
    return label


def _format_track_line(cand: "IdentityQueryItem") -> str:
    """渲染 fused 待识别 track 列表中单个 candidate（track_id + bbox + face_visible）。

    **不注入该 track 的当前/疑似身份**（去先验）：身份先验会锚定 omni 复读旧答案，
    破坏 engine 侧「连续 N 次独立同答才 commit」计数器赖以成立的投票独立性。omni 仅凭
    bbox 在视频里定位该 track、再对照 ``<gallery>`` 独立识别；当前身份/确认全由 engine
    侧状态机管理（confirmed/unknown 的现有身份不进本列表，见 process()）。

    bbox 用 xyxy 格式 ``(x1, y1, x2, y2)``，已由 ``IdentityEngine.process``
    归一化到 mimo 标准的 [0, 1000] 整数区间，与发给 omni 的视频分辨率/宽高比解耦。
    """
    parts = [f"  - track_id={cand.track_id}"]
    if cand.bbox_xyxy_norm is not None:
        x1, y1, x2, y2 = cand.bbox_xyxy_norm
        parts.append(f"bbox=({x1}, {y1}, {x2}, {y2})")
    # (tier_c 污染修复): face_visible 是系统几何关联得出的确定性事实
    # (非 omni 判断), 引导 omni 在无脸时压低置信。None = 未传入 face_dets, 不渲染。
    if cand.face_visible is not None:
        parts.append(f"face_visible={'true' if cand.face_visible else 'false'}")
    return ", ".join(parts)


def _jpeg_block(jpeg_bytes: bytes) -> dict:
    """把 jpeg bytes 包装成 OpenAI image_url 块（fused user content 用）。

    Raises:
        ValueError: jpeg_bytes 为 None / 空 / size < _MIN_JPEG_BYTES。调用方应
            catch 此异常并 skip 该图块, 防"非 None 但实际损坏" 的 bytes 入 payload
            触发 omni 服务端 400 Multimodal data is corrupted。
    """
    if not jpeg_bytes or len(jpeg_bytes) < _MIN_JPEG_BYTES:
        raise ValueError(
            f"jpeg bytes too short ({len(jpeg_bytes) if jpeg_bytes else 0} bytes), "
            f"min {_MIN_JPEG_BYTES}"
        )
    data = base64.b64encode(jpeg_bytes).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{data}"},
    }


def _png_block(png_bytes: bytes) -> dict:
    """把 png bytes 包装成 OpenAI image_url 块（fused user content 用，无损画质）。

    Raises:
        ValueError: png_bytes 为 None / 空 / size < _MIN_JPEG_BYTES。同 ``_jpeg_block``
            的 size gate，防"非 None 但实际损坏" 的 bytes 入 payload 触发 omni
            服务端 400 Multimodal data is corrupted。
    """
    if not png_bytes or len(png_bytes) < _MIN_JPEG_BYTES:
        raise ValueError(
            f"png bytes too short ({len(png_bytes) if png_bytes else 0} bytes), "
            f"min {_MIN_JPEG_BYTES}"
        )
    data = base64.b64encode(png_bytes).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{data}"},
    }


def _resolve_person_body_jpg(
    samples: "GallerySamples",
    cfg: FusedPromptConfig,
) -> bytes | None:
    """分层兜底拿单 person 的 body composite（png 无损字节；函数名 _jpg 为历史叫法）：

    1) 预编码 ``body_composite_jpeg``（library 带 L1+L2 缓存的快路径，现存 png 字节）
    2) 整批 crops hstack + png 现拼（``build_body_composite_png``）
    3) 逐张试：单张 crop 各自 encode 一次，过滤掉损坏的那些再整体拼一次

    任一层成功即返回。全部失败返回 None —— 调用方据此触发"整 gallery 放弃"。

    NOTE: omni 主路径(``_build_fused_user_content``)调用方走的 ``GallerySamples``
    来自 ``library.get_gallery_composites_for_omni`` 新出口,只填 ``body_composite_jpeg``
    不填 ``body_crops``,且 library 出口已过滤掉 ``body_composite_jpeg=None`` 的 person。
    所以新主路径下永远在层 1 命中,层 2/3 实际是 dead code。**层 2/3 保留是为了适配
    老 ``library.get_gallery_for_omni`` 出口**(填 ``body_crops`` 不填 jpeg)的调用方,
    例如离线分析脚本。未来若彻底废弃老出口,可一并清理层 2/3。
    """
    if samples.body_composite_jpeg:
        return samples.body_composite_jpeg
    if not samples.body_crops:
        return None
    try:
        jpg = build_body_composite_png(
            samples.body_crops,
            height=cfg.gallery_body_height,
        )
        if jpg:
            return jpg
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "event=fused_body_compose_fail person_id=%s name=%r 整批现拼失败：%s；"
            "回退到逐张兜底",
            samples.person_id, samples.name, e,
        )
    # 层 3：逐张过滤损坏 crop，再整体拼一次
    usable: list[NDArray[np.uint8]] = []
    for idx, crop in enumerate(samples.body_crops):
        if crop is None or crop.size == 0:
            continue
        try:
            single = build_body_composite_png(
                [crop],
                height=cfg.gallery_body_height,
            )
            if single:
                usable.append(crop)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "event=fused_body_crop_skip person_id=%s name=%r idx=%d 单张编码失败：%s",
                samples.person_id, samples.name, idx, e,
            )
    if not usable:
        return None
    try:
        return build_body_composite_png(
            usable,
            height=cfg.gallery_body_height,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "event=fused_body_compose_fail person_id=%s name=%r 兜底拼接仍失败：%s",
            samples.person_id, samples.name, e,
        )
        return None


def _resolve_person_face_jpg(
    samples: "GallerySamples",
    cfg: FusedPromptConfig,
) -> bytes | None:
    """同 ``_resolve_person_body_jpg`` 的分层兜底，但 face 是 nice-to-have：
    单 person face 全失败仅返回 None，**不**触发整 gallery 放弃（调用方据此跳过该
    person 的 face 段、其它人 face 仍正常渲染）。

    NOTE: 层 2/3 主路径 dead 同 ``_resolve_person_body_jpg`` 的 NOTE,仅适配老
    ``library.get_gallery_for_omni`` 出口。
    """
    if samples.face_composite_jpeg:
        return samples.face_composite_jpeg
    if not samples.face_crops:
        return None
    try:
        jpg = build_face_composite_png(
            samples.face_crops,
            height=cfg.gallery_face_height,
        )
        if jpg:
            return jpg
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "event=fused_face_compose_fail person_id=%s name=%r 整批现拼失败：%s；"
            "回退到逐张兜底",
            samples.person_id, samples.name, e,
        )
    usable: list[NDArray[np.uint8]] = []
    for idx, crop in enumerate(samples.face_crops):
        if crop is None or crop.size == 0:
            continue
        try:
            single = build_face_composite_png(
                [crop],
                height=cfg.gallery_face_height,
            )
            if single:
                usable.append(crop)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "event=fused_face_crop_skip person_id=%s name=%r idx=%d 单张编码失败：%s",
                samples.person_id, samples.name, idx, e,
            )
    if not usable:
        return None
    try:
        return build_face_composite_png(
            usable,
            height=cfg.gallery_face_height,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "event=fused_face_compose_fail person_id=%s name=%r 兜底拼接仍失败：%s",
            samples.person_id, samples.name, e,
        )
        return None


# =============================================================================
# Video encoding (frames + audio → mp4)
# =============================================================================

_VIDEO_SHORT_EDGE = 512  # fallback; runtime value from settings.yaml / config.json via _get_video_short_edge()


def _audio_only_media_info(sample_rate: int) -> LocalMediaInfo:
    return LocalMediaInfo(
        video_width=0, video_height=0, fps=0, frame_count=0,
        has_audio=True, audio_sample_rate=sample_rate,
    )


def _get_video_short_edge() -> int:
    try:
        from miloco.config import get_settings
        return get_settings().perception.engine.get("input", {}).get("video_short_edge", _VIDEO_SHORT_EDGE)
    except Exception:
        return _VIDEO_SHORT_EDGE
_CROP_SIZE = (512, 512)

# 多模态 payload sanity check 下限 — 防"非 None 但实际损坏"的 bytes 入 payload
# 触发 omni 服务端 400 Multimodal data is corrupted。truthy check 只拦 None / b"",
# 拦不住 "header 半截截断" 这种 size 异常小的坏数据。
# - JPEG SOI/EOI + 一个最小 baseline frame ~ 数百字节, < 100 几乎必坏
# - mp4 ftyp box + moov + mdat 最小空 mp4 ~ 几 KB, base64 后 < 1000 基本不可能合法
_MIN_JPEG_BYTES = 100
_MIN_VIDEO_B64_LEN = 1000
# m4a ftyp + moov + mdat 最小容器 ~ 几百字节, base64 后 < 500 几乎不可能合法。
# 跟 video/image 对称的 size gate, 防 PyAV/编码异常情况下产出"非空但极短损坏"
# 的 b64 串入 payload 让 omni 服务端 400 Multimodal data is corrupted。
_MIN_AUDIO_B64_LEN = 500

# 总开关：False 时所有窗口都走 video route（等价于改动前的行为）。
# 用于一键回滚 / A/B 对比 / 上游不兼容时的应急关闭。
_AUDIO_ONLY_ENABLED = True


def _packet_audio_included(ep: IdentityPacket) -> bool:
    """该 packet 的音频是否会被合成进 mp4：audio gate 通过即带（trigger=None 视为通过，
    兼容主动查询 / 旧路径）。speeches / env_sounds 字段的取舍与此一致——没喂音频就别问。"""
    trig = ep.trigger
    return trig is None or trig.audio_active


def _batch_video_has_audio(packets: list[IdentityPacket]) -> bool:
    """video 路由最终合进 mp4 的音频是否存在。

    与 ``_encode_batch_video`` 选设备口径一致（首个有 frames 的 device），据该 device 的
    audio gate 结果判定。用于给 SceneDescriptor.has_audio 赋值，使 schema 是否含
    speeches / env_sounds 与"实际有没有发音频"严格对齐。
    """
    for ep in packets:
        if ep.all_frames:
            return _packet_audio_included(ep)
    return False


def _packet_has_speech(ep: IdentityPacket) -> bool:
    """该 packet 的 VAD 是否判出有真人声（trigger=None 视为有，兼容主动查询 / 旧路径）。
    用于 has_speech：仅决定是否带 speeches 字段，不影响 has_audio / 喂音频 / env_sounds。"""
    trig = ep.trigger
    return trig is None or trig.speech_active


def _batch_video_has_speech(packets: list[IdentityPacket]) -> bool:
    """video 路由本轮 VAD 是否判出有真人声（口径同 ``_batch_video_has_audio``）。
    用于给 SceneDescriptor.has_speech 赋值——无人声时只剥 speeches、保留 env_sounds。"""
    for ep in packets:
        if ep.all_frames:
            return _packet_has_speech(ep)
    return False


def _encode_video(
    identity_packet: IdentityPacket,
    short_edge: int = _VIDEO_SHORT_EDGE,
) -> tuple[str | None, LocalMediaInfo | None]:
    """Encode all frames + audio into mp4 video, return ``(base64, media_info)``。"""
    frames = identity_packet.all_frames
    if not frames:
        return None, None

    audio = (
        identity_packet.audio_clip
        if _packet_audio_included(identity_packet)
        else np.empty(0, dtype=np.int16)
    )
    return _encode_video_mp4(
        frames,
        audio,
        identity_packet.sample_rate,
        fps=identity_packet.frame_info.fps,
        short_edge=short_edge,
    )


def _encode_video_mp4(
    frames: list[NDArray[np.uint8]],
    audio_clip: NDArray[np.int16],
    sample_rate: int,
    fps: int,
    short_edge: int = _VIDEO_SHORT_EDGE,
) -> tuple[str | None, LocalMediaInfo | None]:
    """Encode BGR frames + PCM audio into mp4 using PyAV.

    Returns ``(base64_str, media_info)``。

    Uses a temp file because mp4 container requires seekable output.

    在 read mp4 bytes 之后,调 push_clip_bytes(mp4_bytes) 把字节旁路给
    meaningful_events 复用 — 字节级 = omni 上传的 mp4(零重编).若 ContextVar
    `event_artifacts_scope` 在当前 task 中激活,artifacts.clips 会被填上
    {device_id: (bytes, kind)};scope 未激活时 push 静默 no-op.
    对齐 "clip ≡ omni 看到的字节" 设计原则.
    """
    import os
    import tempfile

    from miloco.perception.snapshot_context import push_clip_bytes

    if not frames:
        return None, None

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        container = av.open(tmp_path, "w")

        h0, w0 = frames[0].shape[:2]
        scale = short_edge / min(h0, w0)
        target_w = int(w0 * scale) // 2 * 2
        target_h = int(h0 * scale) // 2 * 2
        # 缩小用 INTER_AREA(区域平均,抗锯齿最好);放大时 INTER_AREA 会退化成近似最近邻
        # (它按源像素落到的目标格子做平均,放大时每格只摊到一个源像素),必须换重采样核。
        # LANCZOS4 是离线对照里实测的那一个(与 CUBIC 统计上无法区分 p=0.63,取被测过的)。
        #
        # 注意 scale **没有钳到 1.0**,本函数被全景 / query / legacy / crop 四条路径共用,
        # 所以「源短边 < 目标短边」时它们同样走放大分支 —— miloco 恒定拉相机子码流(LOW,
        # 见「全链分辨率」表②),其像素数由机型决定;离线实测到 720p,此时用户选 768/1080
        # 档就命中(scale=1.07 / 1.50),是常见配置而非边角。这条路径本来就在放大,只是此前
        # 用的是退化成最近邻的 INTER_AREA;换核后画质更好但**编码字节会变**,落盘 clip.mp4
        # 随之变化。即:双闸全关的用户走的也是被本行改过的路径,不是零回归。
        interp = cv2.INTER_LANCZOS4 if scale > 1.0 else cv2.INTER_AREA
        v_stream = container.add_stream("h264", rate=fps)
        v_stream.width = target_w
        v_stream.height = target_h
        v_stream.pix_fmt = "yuv420p"

        # Audio stream (if enough samples for AAC)
        _AAC_FRAME_SIZE = 1024
        has_audio = audio_clip is not None and audio_clip.size >= _AAC_FRAME_SIZE
        if has_audio:
            # Pad audio with silence to match video duration to avoid corrupt mp4
            video_duration_samples = int(len(frames) / fps * sample_rate)
            if audio_clip.size < video_duration_samples:
                audio_clip = np.pad(audio_clip, (0, video_duration_samples - audio_clip.size))
            a_stream = container.add_stream("aac", rate=sample_rate)
            a_stream.layout = "mono"

        for frame_data in frames:
            resized = cv2.resize(
                frame_data, (target_w, target_h), interpolation=interp,
            )
            frame = av.VideoFrame.from_ndarray(resized, format="bgr24")
            for packet in v_stream.encode(frame):
                container.mux(packet)
        for packet in v_stream.encode():
            container.mux(packet)

        # Encode audio
        if has_audio:
            pts = 0
            for i in range(0, audio_clip.size, _AAC_FRAME_SIZE):
                chunk = audio_clip[i : i + _AAC_FRAME_SIZE]
                if chunk.size < _AAC_FRAME_SIZE:
                    chunk = np.pad(chunk, (0, _AAC_FRAME_SIZE - chunk.size))
                audio_frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
                audio_frame.sample_rate = sample_rate
                audio_frame.pts = pts
                pts += _AAC_FRAME_SIZE
                for packet in a_stream.encode(audio_frame):
                    container.mux(packet)
            for packet in a_stream.encode():
                container.mux(packet)

        container.close()

        with open(tmp_path, "rb") as f:
            mp4_bytes = f.read()
        push_clip_bytes(mp4_bytes, "mp4")
        media_info = LocalMediaInfo(
            video_width=target_w,
            video_height=target_h,
            fps=fps,
            frame_count=len(frames),
            has_audio=has_audio,
            audio_sample_rate=sample_rate if has_audio else 0,
        )
        return base64.b64encode(mp4_bytes).decode(), media_info
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# =============================================================================
# Crop encoding (tracker crop images)
# =============================================================================


def _encode_crops(edge_packet: IdentityPacket) -> list[dict[str, str]]:
    """Encode tracker crop images (not panoramic — video already has full scene)."""
    crops: list[dict[str, str]] = []
    for frame in edge_packet.frames:
        for crop in frame.crops:
            resized_crop = cv2.resize(crop.image, _CROP_SIZE)
            _, crop_png = cv2.imencode(".png", resized_crop)
            crops.append({"data": base64.b64encode(crop_png.tobytes()).decode(), "media_type": "image/png"})
    return crops


# =============================================================================
# Batch video/crop helpers
# =============================================================================


def _is_audio_only(packets: list[IdentityPacket]) -> bool:
    """所有 packet 都满足 audio_active=True 且 visual_changed=False 且 hold=False。

    batch 场景任一 device visual_changed=True → 整 batch 走全多模态（保守，避免
    同次调用 prompt schema 不一致）。trigger 为 None 时视为非 audio-only（兼容旧路径）。
    总开关 _AUDIO_ONLY_ENABLED=False 时直接返回 False，等价回滚。
    Hold 短路:trigger.hold=True 表示 visual 在滞回期内,虽本窗 visual 不通过但
    不应降级到 audio-only,保持 video 路由。
    """
    if not _AUDIO_ONLY_ENABLED:
        return False
    if not packets:
        return False
    return all(
        p.trigger is not None
        and p.trigger.audio_active
        and not p.trigger.visual_changed
        and not p.trigger.hold
        for p in packets
    )


def _resolve_route(packets: list[IdentityPacket]) -> RouteType:
    """决定本次调用走 video route 还是 audio route。

    - audio：所有 packet 都满足 audio_active=True 且 visual_changed=False
    - video：其他所有情况（含 batch 混合、trigger=None 兼容旧路径）
    """
    return "audio" if _is_audio_only(packets) else "video"


def _encode_audio_only_mp4(
    audio_clip: NDArray[np.int16],
    sample_rate: int,
) -> str | None:
    """audio route 专用：真 m4a 容器（ftyp = "M4A "）+ AAC LC 编码。

    用 ffmpeg 的 ipod muxer 而非默认 mp4 muxer —— mp4 muxer 写出来 ftyp =
    isom/mp42，被 MiMo 后端容器 sniff 拒掉（"invalid audio format"）；
    ipod muxer 强制 ftyp = M4A，是 m4a 标准要求的 brand。

    在 read 字节之后,调 push_clip_bytes 把 m4a 字节旁路给 meaningful_events 复用
    (跟 _encode_video_mp4 对称,UI 端用同一个 <video> 控件播放;m4a 容器虽然只
    有音频,HTML5 <video> 也能 render audio-only track).
    """
    import os
    import tempfile

    from miloco.perception.snapshot_context import push_clip_bytes

    _AAC_FRAME_SIZE = 1024
    if audio_clip is None or audio_clip.size < _AAC_FRAME_SIZE:
        return None

    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        container = av.open(tmp_path, "w", format="ipod")
        a_stream = container.add_stream("aac", rate=sample_rate)
        a_stream.layout = "mono"

        pts = 0
        for i in range(0, audio_clip.size, _AAC_FRAME_SIZE):
            chunk = audio_clip[i : i + _AAC_FRAME_SIZE]
            if chunk.size < _AAC_FRAME_SIZE:
                chunk = np.pad(chunk, (0, _AAC_FRAME_SIZE - chunk.size))
            audio_frame = av.AudioFrame.from_ndarray(
                chunk.reshape(1, -1), format="s16", layout="mono"
            )
            audio_frame.sample_rate = sample_rate
            audio_frame.pts = pts
            pts += _AAC_FRAME_SIZE
            for packet in a_stream.encode(audio_frame):
                container.mux(packet)
        for packet in a_stream.encode():
            container.mux(packet)

        container.close()

        with open(tmp_path, "rb") as f:
            m4a_bytes = f.read()
        # 旁路把 audio-only 的 m4a 字节 push 给 meaningful_events 复用(零重编)
        push_clip_bytes(m4a_bytes, "m4a")
        return base64.b64encode(m4a_bytes).decode()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _encode_batch_video(
    edge_packets: list[IdentityPacket],
    short_edge: int = _VIDEO_SHORT_EDGE,
) -> tuple[str | None, LocalMediaInfo | None]:
    """Encode video from the first device that has frames.

    audio route 由 _build_payload 短路，不会进入本函数。
    返回 ``(base64_str, media_info)``。
    """
    for ep in edge_packets:
        b64, media_info = _encode_video(ep, short_edge=short_edge)
        if b64 is not None:
            return b64, media_info
    return None, None


def _encode_batch_crops(edge_packets: list[IdentityPacket]) -> list[dict[str, str]]:
    crops: list[dict[str, str]] = []
    for ep in edge_packets:
        crops.extend(_encode_crops(ep))
    return crops


# =============================================================================
# 自适应分辨率(Smart Crop): 推理前定向裁切活动区域 → crop 视频 + 全景参考帧
# =============================================================================
#
# 全链分辨率的来源与转换 —— 读本节前先过这张表,免得把中途某一档当成"源分辨率"。
# 每条都已对着当前代码核过,行号随代码漂移,按符号名找。
#
# ① 相机拍摄分辨率:本仓库**完全不参与**,也没有任何配置项表达它。
# ② 拉流档位:miot/client.py 里 `camera_instance.start_async(enable_reconnect=True,
#    enable_audio=True)` **没传 qualities**,取 SDK 默认 MIoTCameraVideoQuality.LOW
#    (枚举只有 LOW=1 / HIGH=3,见 miot 包 types.py)。即恒定拉**子码流**,且当前没有
#    任何旋钮能改。LOW 对应多少像素由相机固件/机型决定(在 native libmiot_camera 里,
#    本仓库看不到);离线对照里实测到 720p,那是**观测值不是代码保证**,别当常量用。
# ③ 解码:SDK decoder.py 只做 `frame.to_ndarray(format="bgr24")`,不缩放;
#    perception/collect/ 与 pipeline.py 全程无 resize —— 所以 packet.all_frames 就是
#    ②的原生尺寸,是全链唯一的"源"。任何消费者都不得原地改它。
# ④ 中途消费者各自缩放,互不影响、也不回写 all_frames:
#    - 视觉门 gate/visual_gate.py `_preprocess` → 448x448 灰度(只用来算帧差比例)
#    - 人形检测 tracker/detector.py `preprocess` → 等比缩到 **ONNX 自带的 416x416**
#      + letterbox 填 114;postprocess 按 scale/pad 还原,故 last_detections、由它逐帧
#      累积的 main_det_boxes、以及 box_info 都是**原生像素坐标**(crop 读的是
#      main_det_boxes,因此不需要再换算)。注意 IdentityConfig.perception_input_width/
#      height(1280/720)是**死配置**:传进 RealTrackingService 存成 _input_width/
#      _input_height 后再没人读,真正决定输入尺寸的是模型的 416x416;engine/api.py
#      还把它当调试信息暴露出去,容易被读成"感知输入分辨率"。
#    - ReID tracker/human_reid.py `preprocess` → 人体 crop 缩到 192x96
#    - 身份 crop 进 omni:本文件 _CROP_SIZE = (512,512)
# ⑤ omni 推理分辨率 = `video_short_edge`(**本 PR 前后都只有这一个旋钮**):
#    settings.yaml 默认 512、UI 档位 [360,512,768,1080]、admin API 收 64..2160。
#    _encode_video_mp4 里 `scale = short_edge / min(h0,w0)` **没有钳到 1.0**,所以
#    「源短边 < 用户档」时**本 PR 之前就已经在放大**了,只是用了为缩小设计的
#    INTER_AREA(放大退化成近似最近邻);本 PR 只改了放大时用哪个重采样核。target
#    还要 //2*2 取偶(h264 yuv420p),故实际编码短边可能比档位低 1-2px。该函数被
#    fused 全景 / query / legacy / crop **四条路径共用**。
# ⑥ crop 分辨率(本 PR 新增)= min(预算, cap_se):预算按用户档等比跟随(见
#    _crop_short_edge_budget),cap_se 见 _maybe_encode_adaptive。它**可以 > 区域原生
#    短边**,即放大。而同附的参考帧走 _resize_short_edge、是**钳死只缩不放**的 ——
#    720p 源 + 1080 档时 crop 视频放大到 759、参考帧仍停在原生 720,两者口径不一致
#    (有意:参考帧只补裁切丢掉的全局视野、不承载坐标定位。不跟着放大是**成本**考量、
#     不是"放大无用"—— 按⑦的机制,放大它同样会抬高它分到的 token,但那份 token 花在
#     一张静态全局图上收益从未实测,不如留给 crop 视频里的主体;
#     bbox 已换算进 crop 坐标系锚视频,不读这张图)。
# ⑦ provider 端还有第二层"分辨率",按 **token** 不按像素、与①~⑥独立:MiMo adapter
#    硬编码 media_resolution="max";Gemini 读 input.media_resolution(""/low=66 tok
#    每帧、high=264);Qwen 不传、从 mp4 自读。见 omni/provider.py。
#    ⑥ 那个 +7.8pp 的收益,机制推测正落在这一层(像素网格大小 → 模型给主体分配多少
#    token,而非信息量),所以它**是 provider 相关的**,换模型/adapter 可能蒸发。

def _effective_panorama_short_edge() -> int:
    """全景视频短边 = 用户设定值;非正值兜回 512 默认。

    Smart Crop 与本值 **正交**(裁不裁看 crop_enhance 的双 key,不再看这里),所以用户选的
    768/1080 在 crop 回退全景时依然生效。非正值兜底纯属防御:admin API 已拒 <64,但历史
    config.json 可能残留过 0(早期「自适应」哨兵),0 会让 _encode_video_mp4 算出 scale=0 崩掉。
    """
    se = _get_video_short_edge()
    return _VIDEO_SHORT_EDGE if se <= 0 else se


def _crop_short_edge_budget(cfg: "CropEnhanceConfig", panorama_short_edge: int) -> int:
    """crop 视频短边预算,按用户分辨率档等比缩放(360p→253 / 512p→360 / 768p→540 / 1080p→759)。

    cfg.crop_short_edge(默认 360)是 **512 档下的基准**,不是固定值:按比例跟随才能让用户
    升档对 crop 同样生效(固定 360 配 1080 档等于把升档吞掉)。

    注意:短边不足预算时会放大到预算后,编码像素 = 预算² × 区域长宽比、与区域面积无关,
    所以 crop_max_area_ratio=0.49≈(360/512)² 已**不再**保证「像素开销 ≈ 同档全景」——
    扁长区域最坏可达同档全景的 1.48 倍。详见 CropEnhanceConfig.crop_short_edge 的注释。
    """
    return max(1, round(panorama_short_edge * cfg.crop_short_edge / _VIDEO_SHORT_EDGE))


def _resize_short_edge(frame: NDArray[np.uint8], short_edge: int) -> NDArray[np.uint8]:
    """等比缩放到目标短边(只缩不放)。"""
    h, w = frame.shape[:2]
    m = min(h, w)
    if m == 0 or m <= short_edge:
        return frame
    scale = short_edge / m
    return cv2.resize(
        frame, (max(1, int(w * scale)), max(1, int(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


@dataclass
class _AdaptiveResult:
    video_b64: str
    media_info: "LocalMediaInfo | None"
    ref_image_jpeg: bytes  # 全景末帧 JPEG(短边=用户分辨率档),作场景上下文参考(帧序见下方注释)
    # crop 区域(全景像素 xyxy)与全景帧尺寸 (w, h) —— 供 prompt 层把名册 bbox 从全景
    # [0,1000] 换算进 crop 坐标系(remap_bbox_norm_to_crop),否则坐标与画面错配。
    region: tuple[int, int, int, int]
    frame_size: tuple[int, int]


def _maybe_encode_adaptive(packets: list[IdentityPacket]) -> "_AdaptiveResult | None":
    """Smart Crop 开启时算 crop 区域、编码 crop 视频 + 全景参考帧。

    返回 None = 回退全景(既有路径)。触发 None 的情形:双闸任一为 false(发版级开关 enabled /
    单机用户开关 user_enabled)、无帧、无 crop 依据、面积超上限、crop/编码/JPEG 失败或产物过短。
    crop 视频与参考帧的短边都跟随用户分辨率档(见 _crop_short_edge_budget),与「裁不裁」正交。
    all_frames 只读不改。
    """
    from .crop_enhance import (
        compute_crop_region,
        compute_motion_blocks,
        crop_enhance_config_from_settings,
        crop_frames,
    )

    cfg = crop_enhance_config_from_settings()
    # 双闸:发版级开关 AND 单机用户开关。任一为 false → 回退全景路径(不裁切)。注意这不等于
    # 字节回到接本特性前 —— 全景走的 _encode_video_mp4 放大分支已换重采样核,见该函数注释。
    if not (cfg.enabled and cfg.user_enabled):
        return None
    ep = next((p for p in packets if p.all_frames), None)  # 同 _encode_batch_video:首个有帧设备
    if ep is None:
        logger.info("event=adaptive_crop_fallback reason=no_frames")
        return None
    frames = ep.all_frames
    try:
        # 主体框取窗口内**每一抽帧**的 human/cat/dog 检测框(tracking 层累积、随 packet 透传):
        # 裁出的区域要包住窗口内出现过的一切,所以不能用 targets/box_info —— 那只有末帧一个框,
        # 且宠物永远不在其中(tracker 只对 HUMAN 建 track)。
        # motion_blocks 只算一次,既喂 compute_crop_region 又用于诊断日志(免重复算 CV)。
        det_boxes = list(ep.main_det_boxes)
        motion_blocks = compute_motion_blocks(frames, cfg)
        n_det, n_motion = len(det_boxes), len(motion_blocks)
        region = compute_crop_region(
            det_boxes, frames, cfg, motion_blocks=motion_blocks
        )
        if region is None:
            # 灰度诊断。区域是"包住一切"的并集,所以回退时要打出并集框本身:超上限时能看出是
            # 哪一侧把它撑开(远处误检 / 大幅走动 / 运动块),否则日志只说面积不合格、查不出根因。
            # 面积有两条相反的回退路径,按并集面积二分成两个 reason —— 处置完全不同:
            #   area_too_large 查是什么撑开了并集;area_too_small 是几何天花板(目标贴边、
            #   绕中心放大被 clamp 截断后仍不足下限),裁了也没有等效分辨率收益,无需处置。
            all_boxes = det_boxes + motion_blocks
            union = (
                (min(b[0] for b in all_boxes), min(b[1] for b in all_boxes),
                 max(b[2] for b in all_boxes), max(b[3] for b in all_boxes))
                if all_boxes else None
            )
            reason = "no_activity"
            if union is not None:
                h0, w0 = frames[0].shape[:2]
                u_ratio = (union[2] - union[0]) * (union[3] - union[1]) / max(1, w0 * h0)
                reason = (
                    "area_too_large" if u_ratio > cfg.crop_max_area_ratio else "area_too_small"
                )
            # n_det_boxes 是**框数**(逐帧累积,≈ 帧数 × 主体数),不是主体数 —— 与 n_frames
            # 一起看才有意义;旧字段名 n_det 曾是末帧主体数(0-3),别拿两版日志的数字直接比。
            logger.info(
                "event=adaptive_crop_fallback reason=%s n_det_boxes=%d n_frames=%d "
                "n_motion=%d union=%s",
                reason, n_det, len(frames), n_motion, union,
            )
            return None
        cropped = crop_frames(frames, region)
        if not cropped or cropped[0].size == 0:
            logger.info("event=adaptive_crop_fallback reason=crop_empty region=%s", region)
            return None
        ch, cw = cropped[0].shape[:2]
        fh, fw = frames[0].shape[:2]
        pano_se = _effective_panorama_short_edge()
        # 短边不足预算时**放大**到预算(不再只缩不放)。离线对照:720p 源下裁出区域短边中位
        # 283、够不上 360 预算的窗口占 57%,这批窗口原生裁切只有 +0.6pp(p=1.0),插值放大到
        # 预算后 +7.8pp。机制疑在送进视觉 encoder 的像素网格尺寸决定主体分到多少 token
        # (两种插值核互比无差异 p=0.63,若靠恢复细节则更锐的核该更好)——因此它是 provider
        # 相关的,换模型/adapter 可能失效,别当成图像本身的性质。
        #
        # 上限:放大后**长边**不超过 crop 前原图长边。因区域必在帧内(max(ch,cw) <=
        # max(fh,fw)),cap_se 恒 >= 原生短边,故本上限只会限制放大、绝不引入缩小(261 个真实
        # 区域上验过 0 反例)。超限按上限回压短边(部分放大),不整体回退。
        #
        # 它约束的**只有长边**,不是逐轴:基准取 max(fh,fw) 而不分区域朝向,所以竖长区域落在
        # 横向帧里时,是拿帧宽去限制 crop 的高 —— 帧 1920x1080、区域 377w×553h、1080 档会编出
        # 758x1112,高度 1112 已超过帧高 1080。别把本上限读成「像素网格不超过整帧」。
        # 改成逐轴(min(fw/cw, fh/ch))更保守,但在真实数据上会从生效 1/261 变成 34/261、压低
        # 33 个窗口的短边,且全是竖长人形区域 —— 而 +7.8pp 正是在无逐轴上限下测的,故未改。
        cap_se = max(1, int(max(fh, fw) * min(ch, cw) / max(ch, cw)))
        cse = min(_crop_short_edge_budget(cfg, pano_se), cap_se)
        audio = (
            ep.audio_clip
            if _packet_audio_included(ep)
            else np.empty(0, dtype=np.int16)
        )
        # fps 沿用 frame_info.fps(下采样后真实帧间隔),与全景视频一致——crop 逐帧不抽帧,
        # 用独立帧率会让视频时长/音画错位(全景用的正是这个 fps)。
        video_b64, media_info = _encode_video_mp4(
            cropped, audio, ep.sample_rate, fps=ep.frame_info.fps, short_edge=cse,
        )
        if not video_b64 or len(video_b64) < _MIN_VIDEO_B64_LEN:
            logger.info("event=adaptive_crop_fallback reason=video_too_short region=%s", region)
            return None
        # 参考帧取末帧:与 crop 视频的时间轴对齐(视频末帧正是这一帧的裁切结果),模型对照
        # 「全景 → 放大」时看到的是同一时刻的场景,不会被窗内位移错开。
        # 注:它**不**是 bbox 的坐标基准 —— bbox 已由 remap_bbox_norm_to_crop 换算进 crop
        # 坐标系、直接锚视频(见 build_fused_payload 的 bbox_remap)。
        # 短边跟用户分辨率档(不是硬编码 512):档位升高时全局场景上下文也该更清楚。
        ref_jpeg = encode_jpeg_bytes(_resize_short_edge(frames[-1], pano_se))
        if not ref_jpeg or len(ref_jpeg) < _MIN_JPEG_BYTES:
            logger.info("event=adaptive_crop_fallback reason=jpeg_too_short region=%s", region)
            return None
        logger.info(
            "event=adaptive_crop region=%s crop_ratio=%.3f short_edge=%d upscale=%.2f "
            "n_det_boxes=%d n_frames=%d n_motion=%d",
            region,
            ((region[2] - region[0]) * (region[3] - region[1])) / (fw * fh),
            cse,
            # 灰度期要能从日志看出放大倍率的真实分布(>1 即放大,离线实测中位 1.27、max 2.02)
            cse / max(1, min(ch, cw)),
            n_det, len(frames), n_motion,
        )
        # 旁路落盘:参考帧字节 + crop 元数据(坐标进 trace),供 badcase 复盘对照。
        # 无 active scope / device_ctx 时静默 no-op,不影响推理主流程。
        from miloco.perception.snapshot_context import push_crop_meta, push_ref_frame

        push_ref_frame(ref_jpeg)
        # short_edge 记的是**目标**短边;实际编码值会被 _encode_video_mp4 的 //2*2 取偶
        # (以及浮点截断)下调 1-2px,按它反算送模型的像素网格会有这点误差。
        push_crop_meta(region=region, frame_size=(fw, fh), short_edge=cse)
        return _AdaptiveResult(video_b64, media_info, ref_jpeg, region, (fw, fh))
    except Exception:  # noqa: BLE001 —— 任何失败都回退全景,不让 crop 打断推理
        # 统一 event 名(adaptive_crop_fallback),灰度期按单一 event grep 不漏异常回退
        logger.warning("event=adaptive_crop_fallback reason=exception 回退全景", exc_info=True)
        return None


# =============================================================================
# Active query user content (separate — different structure entirely)
# =============================================================================


def _build_query_user_content(
    edge_packets: list[IdentityPacket],
    query: str,
    last_caption: str | None,
    label_lookup: "dict[str, str] | None" = None,
) -> str:
    parts: list[str] = []

    for i, ep in enumerate(edge_packets):
        if len(edge_packets) > 1:
            parts.append(f"--- 设备 {i + 1} ---")
        if not ep.targets:
            parts.append("检测结果：未检测到目标")
        else:
            parts.append("检测结果：")
            for t in ep.targets:
                parts.append(_format_target(t, label_lookup))
        parts.append(f"场景状态：{ep.scene_motion.value}")
        parts.append(f"音频：{ep.audio_analysis.type.value}（能量: {ep.audio_analysis.energy_level:.3f}）")
        parts.append("")

    if last_caption:
        parts.append(f"当前场景参考：{last_caption}")

    parts.append(f"\n用户问题：{query}")
    return "\n".join(parts)


# =============================================================================
# 写 tier_c 前的 omni 1v1 同人校验(设计文档 E7)
# =============================================================================

# V12/V13 真实降质 crop 实证定稿的 prompt + confidence 语义。规则 1/2/4 是"踩坑→修复"
# 换来的(眼镜幻觉 + 略糊就拒曾让真本人 0/4); 少一条 TPR 就崩, 改动前务必回看设计文档。
_TIER_C_VERIFY_SYSTEM_PROMPT = """你需要判断 QUERY 与 GALLERY 是否为同一人。
GALLERY 是某已登记成员的多张参考(全身+人脸);QUERY 是一张待入库样本(全身+人脸,来自家用摄像头)。以人脸为主,结合体型、发型。

判据优先级:人脸五官(眼/鼻/嘴形状与间距)、脸型轮廓 > 体型 > 发型。

规则:
1. 性别:仅当确实看清且明显不同时才判为不同人;看不清/不确定性别时不要臆测,回到五官。
2. 眼镜:只有确实看清 GALLERY 或 QUERY 中至少一方戴眼镜时,才把眼镜作为线索。若没有明确看到任何一方戴眼镜,禁止提及眼镜、禁止以"眼镜差异"作判据。一方明确戴/另一方明确不戴,或镜框样式明显不同,偏向不同人。
3. 衣着颜色/款式不作强依据(会换衣服)。
4. 画质宽容:QUERY 来自家用摄像头可能偏小/偏糊。只要还能看出大致五官/脸型/体型轮廓,就基于可见信息判断并降低置信;不要仅因"略糊"就判不同人。只有完全无法辨认任何人物特征时,才 same_person=false 并注明"无法辨认"。

confidence 语义:你对本次 same_person 判断的信心(0-1)。判 true 时是"确为同一人"的信心、判 false 时是"确为不同人"的信心;二者互斥(同人信心 + 不同人信心 = 1.0)。

严格输出 JSON:{"same_person": true|false, "confidence": 0.0-1.0, "reason": "≤30字"}"""


def build_tier_c_verify_payload(
    query_body_crop: NDArray[np.uint8],
    query_face_crop: NDArray[np.uint8],
    gallery_body_crops: list[NDArray[np.uint8]],
    gallery_face_crops: list[NDArray[np.uint8]],
    *,
    height: int = 256,
    quality: int = 100,        # 历史签名保留;两图走 PNG 无损, 此值不生效(详见 docstring)
) -> dict | None:
    """构造"写 tier_c 前同人校验"(设计文档 E7)的 omni 调用 payload。

    QUERY = 本帧 body+face 合成一张;GALLERY = 该成员 tier_a body+face 合成一张。
    两侧合成都"限高不限宽"(``max_total_width=None``), 保住人脸分辨率(1v1 判别信号)。
    两图均 PNG 无损编码注入 omni;``quality`` 入参为历史签名保留, PNG 不受其影响。
    返回 None 表示图像无效(上层跳过本次校验)。
    """
    # QUERY 至少要有 body(调用方 _enqueue_tier_c_candidate 已保证 body/face 均非空,
    # 此守卫仅防未来误用传入 None/空)。hstack_to_height 会静默过滤 None 元素。
    if query_body_crop is None or query_body_crop.size == 0:
        return None
    query_img = hstack_to_height(
        [query_body_crop, query_face_crop], height, max_total_width=None,
    )
    gallery_img = hstack_to_height(
        [*gallery_body_crops, *gallery_face_crops], height, max_total_width=None,
    )
    if query_img is None or gallery_img is None:
        return None
    q_png = encode_png_bytes(query_img)
    g_png = encode_png_bytes(gallery_img)
    if not q_png or not g_png:
        return None
    return {
        "system_prompt": _TIER_C_VERIFY_SYSTEM_PROMPT,
        "user_content": "图序:第一张是 QUERY(待入库样本),第二张是 GALLERY(已登记成员参考)。",
        "crops": [
            {"media_type": "image/png", "data": base64.b64encode(q_png).decode("ascii")},
            {"media_type": "image/png", "data": base64.b64encode(g_png).decode("ascii")},
        ],
    }
