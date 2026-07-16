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

from miloco.perception.engine.identity.gallery_composite import (
    build_body_composite_png,
    build_face_composite_png,
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
from .home_profile_loader import get_home_profile_prefix
from .provider import LocalMediaInfo, OmniProviderAdapter

RouteType = Literal["video", "audio"]

if TYPE_CHECKING:
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
    short_edge = _get_video_short_edge()
    video_b64, media_info = _encode_batch_video(identity_packets, short_edge=short_edge)
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
        system_prompt = build_system_prompt(scene, include_home_profile=False)
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

    short_edge = _get_video_short_edge()
    video_b64, media_info = _encode_batch_video(packets, short_edge=short_edge)

    # has_speech 只由本轮 VAD 决定：本轮真有人声（含 pending 的延续语音）→ VAD 自然过、
    # 保留 speeches、模型把 <pending_speech> 拼成完整句；本轮无人声 → 剥 speeches，挂着的
    # pending 半句不强行补全（否则模型会就着噪声脑补出一个完成句，正是要根除的幻觉）。
    scene = SceneDescriptor(
        route="video", has_identity=bool(candidates), stream=False,
        has_audio=_batch_video_has_audio(packets),
        has_speech=_batch_video_has_speech(packets),
        identity_match_disabled=matching_moot,
    )
    system_prompt = build_system_prompt(scene, include_home_profile=False)
    user_content = _build_fused_user_content(
        packets=packets,
        context=context,
        candidates=candidates,
        gallery_snapshot=gallery_snapshot,
        video_b64=video_b64,
        media_info=media_info,
        adapter=adapter,
        cfg=cfg,
        label_lookup=label_lookup,
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
    )
    base: dict = {
        "system_prompt": build_system_prompt(scene, include_home_profile=include_home_profile),
        "user_content": _build_user_content(
            packets, context, stream=stream, label_lookup=label_lookup,
        ),
        "crops": [],
    }
    if route == "audio":
        ep = packets[0]
        base["audio_base64"] = _encode_audio_only_mp4(ep.audio_clip, ep.sample_rate)
        base["media_info"] = _audio_only_media_info(ep.sample_rate)
    else:
        short_edge = _get_video_short_edge()
        video_b64, media_info = _encode_batch_video(packets, short_edge=short_edge)
        base["video_base64"] = video_b64
        base["media_info"] = media_info
    return base


# =============================================================================
# System prompt (unified)
# =============================================================================


def build_system_prompt(scene: SceneDescriptor, *, include_home_profile: bool = True) -> str:
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
    adapter: OmniProviderAdapter,
    cfg: FusedPromptConfig,
    label_lookup: "dict[str, str] | None" = None,
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
    candidate_tids = {c.track_id for c in candidates}
    roster_lines = _build_device_header(
        packets, label_lookup=label_lookup, candidate_tids=candidate_tids, emit_bbox_note=False,
    )
    for line in roster_lines:
        content.append({"type": "text", "text": line})

    # 3. 待识别 track 列表（仅数据，识别规则已在 system prompt # 字段说明 中）
    if candidates:
        content.append({"type": "text", "text": "待识别 track："})
        for cand in candidates:
            content.append({"type": "text", "text": _format_track_line(cand)})

    # 已识别人物/陌生人 + 待识别 track 共用一句 bbox 坐标系说明（二者同一 [0,1000] 约定，去重）
    if any("[bbox=" in ln for ln in roster_lines) or candidates:
        content.append({"type": "text", "text": (
            "上方已识别人物、陌生人及待识别 track 中的 bbox=(x1, y1, x2, y2) 均为画面归一化到 [0, 1000] 区间的位置"
            "（左上 0,0；右下 1000,1000），用于把姓名 / track_id 对应到视频里的人。"
        )})

    # 4. gallery（候选成员参考图，紧邻 video 便于视觉比对）
    content.extend(gallery_content)

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


def _render_roster_entry(t: IdentityTarget, label_lookup: "dict[str, str] | None") -> str:
    """名册单项：``名[bbox=(x1, y1, x2, y2)]``；无 bbox（coasting 本帧未检测）退化为纯名。"""
    label = _format_target(t, label_lookup)
    if t.bbox_xyxy_norm is not None:
        x1, y1, x2, y2 = t.bbox_xyxy_norm
        return f"{label}[bbox=({x1}, {y1}, {x2}, {y2})]"
    return label


def _build_device_header(
    packets: list[IdentityPacket],
    label_lookup: "dict[str, str] | None" = None,
    candidate_tids: "set[int] | frozenset[int]" = frozenset(),
    emit_bbox_note: bool = True,
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
    """
    def _bucket(ep: IdentityPacket) -> tuple[list[str], list[str]]:
        members: list[str] = []
        strangers: list[str] = []
        for t in ep.targets:
            # candidate_tids（本窗派发重审）+ suppress_as_prior（翻身份黏旧名 track，
            # coasting 窗不在 candidate_tids 内）均剔出名册，避免旧/当前身份当先验锚定 omni
            if t.track_id in candidate_tids or t.suppress_as_prior:
                continue
            if _is_confirmed_member_pid(t.person_id):
                members.append(_render_roster_entry(t, label_lookup))
            elif _is_stranger_pid(t.person_id):
                strangers.append(_render_roster_entry(t, label_lookup))
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
    if emit_bbox_note and any("[bbox=" in ln for ln in lines):
        lines.append(
            "上方已识别人物、陌生人中 [bbox=(x1, y1, x2, y2)] 为该人在画面中归一化到 [0, 1000] 区间的位置"
            "（左上 0,0；右下 1000,1000），用于把姓名对应到视频里的人。"
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
                frame_data, (target_w, target_h), interpolation=cv2.INTER_AREA,
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
