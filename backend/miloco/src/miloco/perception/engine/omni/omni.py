"""Omni Layer — Orchestrator."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from miloco.database.token_usage_repo import fire_record
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni.circuit_breaker import (
    CircuitOpenError,
    get_omni_circuit_breaker,
)
from miloco.perception.engine.omni.constants import MILOCO_USER_AGENT
from miloco.perception.engine.omni.error_classifier import (
    ClassifiedError,
    ErrorCategory,
    classify_exception,
    classify_response,
)
from miloco.perception.engine.omni.omni_client import (
    OmniError,
    _collect_stream_response,
    call_omni,
    call_omni_stream,
    extract_usage,
    resolve_api_key,
)
from miloco.perception.engine.omni.prompt_builder import (
    FusedPromptConfig,
    build_batch_prompt,
    build_batch_stream_prompt,
    build_fused_payload,
    build_prompt,
    build_stream_prompt,
    format_person_label,
)
from miloco.perception.engine.omni.provider import get_adapter
from miloco.perception.engine.omni.response_parser import (
    parse_identity_assignments,
    parse_omni_response,
    parse_omni_response_from_text,
    try_extract_matched_rules,
    try_extract_speeches,
    try_extract_suggestions,
)
from miloco.perception.engine.types import IdentityPacket, OmniContext, OmniOutput
from miloco.perception.snapshot_context import push_omni_trace
from miloco.perception.types import MatchedRule, Speech, Suggestion

if TYPE_CHECKING:
    from miloco.perception.engine.identity.engine import IdentityEngine
    from miloco.perception.engine.omni.provider import OmniProviderAdapter

logger = logging.getLogger(__name__)


# 端侧 ngram 流式复读检测：buffer 末尾出现"首字符非空白、长度 1-5 字符"的子串
# 连续重复 ≥ 10 次时命中。\S 排除 JSON 缩进的连续空格误触发；末尾窗口 100 字符
# 覆盖最长形态（5-gram × 10 = 50 字符）。命中后立即 abort stream，避免模型继续
# 复读跑满 max_tokens（实测 56 次/15h 跑满 512 → JSON 截断 → fallback）。
_LOOPBACK_NGRAM = re.compile(r"(\S(?:.{0,4}?))\1{9,}")
_LOOPBACK_TAIL_WINDOW = 100


def _has_loopback_tail(buffer: str) -> bool:
    """检测 buffer 末尾窗口是否出现 ngram 复读 ≥ 10 次。"""
    if len(buffer) < 20:
        return False
    return bool(_LOOPBACK_NGRAM.search(buffer[-_LOOPBACK_TAIL_WINDOW:]))


def _rule_name_to_id(context: OmniContext) -> dict[str, str]:
    """本窗 rule_name → rule_id(UUID) 映射，供 response_parser 把 matched_rules 里模型
    照抄的 rule_name 还原回 rule_id（下游去重/触发用 UUID）。

    key 必须与 _render_rule_conditions 写进 prompt 的标识一致：rule_name 为空时同样回退
    [rule_id]，否则模型照抄的 [rule_id] 在映射里找不到，命中的 matched_rules 会被静默丢弃。"""
    return {
        (rc.rule_name or f"[{rc.rule_id}]"): rc.rule_id
        for rc in context.rule_conditions
    }


async def run_omni(
    edge_packet: IdentityPacket, context: OmniContext, config: OmniConfig
) -> OmniOutput:
    """Run Omni layer: build prompt → call model → parse response."""
    # 截图模式双模型拆分
    vision_cfg, audio_cfg = resolve_split_model_configs(config)
    if vision_cfg and audio_cfg:
        return await run_omni_split(
            [edge_packet], context, vision_cfg, audio_cfg,
        )
    payload = build_prompt(edge_packet, context)
    raw_response = await call_omni(payload, config)
    output = parse_omni_response(raw_response, _rule_name_to_id(context))
    output.usage = extract_usage(raw_response)
    return output


async def run_omni_batch(
    edge_packets: list[IdentityPacket], context: OmniContext, config: OmniConfig
) -> OmniOutput:
    """Run Omni layer for multiple devices in the same room."""
    # 截图模式双模型拆分
    vision_cfg, audio_cfg = resolve_split_model_configs(config)
    if vision_cfg and audio_cfg:
        return await run_omni_split(
            edge_packets, context, vision_cfg, audio_cfg,
        )
    payload = build_batch_prompt(edge_packets, context)
    raw_response = await call_omni(payload, config)
    output = parse_omni_response(raw_response, _rule_name_to_id(context))
    output.usage = extract_usage(raw_response)
    return output


# =============================================================================
# 截图模式双模型拆分 —— vision + audio 并发调用
# =============================================================================


def resolve_split_model_configs(
    base_config: OmniConfig,
) -> tuple[OmniConfig | None, OmniConfig | None]:
    """从当前 settings 解析 vision/audio 模型配置。

    返回 ``(vision_config, audio_config)``；任一为 None 表示不启用拆分。
    仅当 transmission_mode=screenshot 且两个 split model 都已配置时才返回非 None。
    """
    from dataclasses import replace

    from miloco.config import get_settings
    from miloco.perception.engine.config import OmniConfig

    settings = get_settings()
    engine_input = settings.perception.engine.get("input", {})
    if engine_input.get("transmission_mode") != "screenshot":
        return None, None

    vision_s = settings.model.vision_model
    audio_s = settings.model.audio_model
    if vision_s is None or audio_s is None:
        return None, None

    # 构造 vision config：未配字段回退到 base_config（omni）
    vision_cfg = replace(
        base_config,
        model=vision_s.model or base_config.model,
        base_url=vision_s.base_url or base_config.base_url,
        api_key=vision_s.api_key or base_config.api_key,
    )
    # 构造 audio config
    audio_cfg = replace(
        base_config,
        model=audio_s.model or base_config.model,
        base_url=audio_s.base_url or base_config.base_url,
        api_key=audio_s.api_key or base_config.api_key,
    )
    return vision_cfg, audio_cfg


def _merge_split_outputs(vision_out: OmniOutput, audio_out: OmniOutput) -> OmniOutput:
    """合并 vision 和 audio 模型的输出。

    规则：vision 取 caption/identities/matched_rules/suggestions，
    audio 取 speeches/env_sounds。usage 合并。
    """
    merged = vision_out.model_copy()
    merged.speeches = audio_out.speeches
    merged.env_sounds = audio_out.env_sounds
    # 合并 usage
    if vision_out.usage or audio_out.usage:
        v = vision_out.usage or {}
        a = audio_out.usage or {}
        merged.usage = {
            "input_tokens": v.get("input_tokens", 0) + a.get("input_tokens", 0),
            "output_tokens": v.get("output_tokens", 0) + a.get("output_tokens", 0),
            "cached_tokens": v.get("cached_tokens", 0) + a.get("cached_tokens", 0),
        }
    return merged


def _build_vision_only_payload(
    edge_packets: list[IdentityPacket],
    context: OmniContext,
    stream: bool = False,
    label_lookup: "dict[str, str] | None" = None,
) -> dict:
    """构建仅含视觉信息的 payload（剥离音频 schema 字段）。

    与 _build_payload 的区别：SceneDescriptor 的 has_audio=False，
    使 schema 剥离 speeches/env_sounds 字段（视觉模型不需要输出这些）。
    """
    from miloco.perception.engine.omni.field_registry import SceneDescriptor
    from miloco.perception.engine.omni.prompt_builder import (
        _build_user_content,
        _encode_batch_screenshots,
        _get_video_short_edge,
        build_system_prompt,
    )

    short_edge = _get_video_short_edge()
    screenshots_b64, media_info = _encode_batch_screenshots(edge_packets, short_edge=short_edge)
    # 视觉专用：has_audio=False → schema 剥掉 speeches/env_sounds
    scene = SceneDescriptor(
        route="video", has_identity=False, stream=stream,
        has_audio=False, has_speech=False,
    )
    return {
        "system_prompt": build_system_prompt(scene, include_home_profile=True),
        "user_content": _build_user_content(edge_packets, context, stream=stream, label_lookup=label_lookup),
        "screenshots_b64": screenshots_b64,
        "media_info": media_info,
        "crops": [],
    }


def _build_audio_only_payload(
    edge_packets: list[IdentityPacket],
    context: OmniContext,
) -> dict:
    """构建仅含音频信息的 payload（复用 audio route 逻辑）。

    与 audio route 完全一致：has_audio=True，schema 只含 speeches/env_sounds/suggestions。
    """
    from miloco.perception.engine.omni.field_registry import SceneDescriptor
    from miloco.perception.engine.omni.prompt_builder import (
        _build_user_content,
        _encode_audio,
        _audio_only_media_info,
        build_system_prompt,
    )
    from miloco.config import get_settings

    scene = SceneDescriptor(
        route="audio", has_identity=False, stream=False,
        has_audio=True, has_speech=True,
    )
    ep = edge_packets[0]
    audio_format = get_settings().model.omni.audio_format or "m4a"
    audio_result = _encode_audio(ep.audio_clip, ep.sample_rate, audio_format)
    return {
        "system_prompt": build_system_prompt(scene, include_home_profile=True),
        "user_content": _build_user_content(edge_packets, context, stream=False),
        "audio_base64": audio_result[0] if audio_result else None,
        "audio_format": audio_result[1] if audio_result else audio_format,
        "media_info": _audio_only_media_info(ep.sample_rate),
        "crops": [],
    }


async def run_omni_split(
    edge_packets: list[IdentityPacket],
    context: OmniContext,
    vision_config: OmniConfig,
    audio_config: OmniConfig,
    label_lookup: "dict[str, str] | None" = None,
) -> OmniOutput:
    """截图模式双模型拆分：并发调用 vision 和 audio 模型，合并结果。"""
    vision_payload = _build_vision_only_payload(edge_packets, context, label_lookup=label_lookup)
    audio_payload = _build_audio_only_payload(edge_packets, context)

    vision_task = call_omni(vision_payload, vision_config)
    audio_task = call_omni(audio_payload, audio_config)

    vision_raw, audio_raw = await asyncio.gather(vision_task, audio_task)

    vision_out = parse_omni_response(vision_raw, _rule_name_to_id(context))
    vision_out.usage = extract_usage(vision_raw)

    audio_out = parse_omni_response(audio_raw, _rule_name_to_id(context))
    audio_out.usage = extract_usage(audio_raw)

    return _merge_split_outputs(vision_out, audio_out)


# =============================================================================
# Fused 模式 —— 主调用同时返回 identity_assignments，省一次 omni 调用
# =============================================================================


async def run_omni_fused(
    edge_packets: list[IdentityPacket],
    context: OmniContext,
    config: OmniConfig,
    identity_engine: "IdentityEngine",
    fused_prompt_config: FusedPromptConfig | None = None,
) -> OmniOutput:
    """fused 主调用：构 prompt（含 gallery）→ 调 omni → 解 OmniOutput + identity_assignments。

    流程：
      1. 从 ``identity_engine.take_fused_pending()`` 取本窗口候选 + gallery
      2. ``build_fused_payload`` 构造 messages
      3. ``_call_omni_messages`` 直接发 messages（区别于 ``call_omni`` 的固定结构）
      4. 解析 ``identity_assignments`` 并通过 ``identity_engine.deliver_fused_response`` 写回 state
      5. 返回标准 ``OmniOutput``
    """
    pending = identity_engine.take_fused_pending()
    if pending is not None:
        candidates = list(pending.candidates)
        gallery_snapshot = pending.gallery_snapshot
    else:
        candidates = []
        gallery_snapshot = {}

    # 一次 list_persons 同时构造两张表（始终从 library 构造，不依赖 gallery_snapshot——
    # 后者在 candidates 空时为 {}，会让主调用 prompt「已识别人物：」段渲染出 UUID 而非姓名）：
    #   name_lookup  —— pid → 纯真名（不含角色），喂给 prompt「已识别人物/陌生人」名册显示。
    #                   名册里只展示真名（如"张三"），不展示"张三(角色:爸爸)"——角色不进名册。
    #   name_to_pid  —— 真名 / 角色 / 完整标签 → pid 反查。omni 输出 name 字段可能是真名、
    #                   角色, 也可能把 gallery 里看到的完整标签"真名(角色:X)"整串回显。真名与
    #                   完整标签恒做 key；角色因可空、不唯一，仅在全局唯一时才做 key（否则多人
    #                   同角色，纯角色反查会误命中最早遍历到的那个 pid）。
    name_lookup: dict[str, str] = {}
    name_to_pid: dict[str, str] = {}
    role_counts: Counter[str] = (
        Counter()
    )  # library 全局角色计数，role 唯一性判断的权威来源
    # 身份库(persons)为空 → 成员匹配不可能，改用精简版 identities prompt（只判 unknown/no_person、
    # 不做成员匹配，见 field_registry.IDENTITY_NO_MATCH）。仅在 list_persons 成功且确为 0 时置
    # True；读库异常（下方 except 落到 pass）保持 False，不把一次 IO 抖动误判成"库空"而错误关掉匹配。
    person_lib_empty = False
    try:
        persons = list(identity_engine.library.list_persons())
        person_lib_empty = len(persons) == 0
        role_counts = Counter(r.role for r in persons if r.role)
        for ref in persons:
            if ref.name:
                name_lookup[ref.person_id] = ref.name
                label = format_person_label(ref.name, ref.role)
                if label:
                    name_to_pid.setdefault(label, ref.person_id)
                name_to_pid.setdefault(ref.name, ref.person_id)
                if ref.role and role_counts[ref.role] == 1:
                    name_to_pid.setdefault(ref.role, ref.person_id)
            name_to_pid.setdefault(ref.person_id, ref.person_id)
    except Exception:  # noqa: BLE001
        pass

    # build_fused_payload 与主调用同一个失败兜底：任何阶段抛异常都必须调
    # deliver_fused_failure，否则 mark_dispatched 已置 inflight=True 的 track
    # 永远不会被 GC（_gc_dead_tracks 跳过 inflight）也不会被重新派发
    # （needs_omni_call 返回 False）。
    adapter = get_adapter(config.model)

    # 截图模式双模型拆分检测
    vision_cfg, audio_cfg = resolve_split_model_configs(config)
    use_split = vision_cfg is not None and audio_cfg is not None

    try:
        if use_split:
            # 双模型拆分：vision（fused，无音频 schema）+ audio（audio-only）并发
            vision_payload = build_fused_payload(
                packets=edge_packets,
                context=context,
                candidates=candidates,
                gallery_snapshot=gallery_snapshot,
                config=fused_prompt_config,
                label_lookup=name_lookup,
                adapter=adapter,
                matching_moot=person_lib_empty,
                force_no_audio=True,
            )
            audio_payload = _build_audio_only_payload(edge_packets, context)
            vision_task = _call_omni_messages(vision_payload["messages"], config, adapter=adapter)
            audio_task = call_omni(audio_payload, audio_cfg)
            raw_response, audio_raw_response = await asyncio.gather(vision_task, audio_task)
        else:
            payload = build_fused_payload(
                packets=edge_packets,
                context=context,
                candidates=candidates,
                gallery_snapshot=gallery_snapshot,
                config=fused_prompt_config,
                label_lookup=name_lookup,
                adapter=adapter,
                matching_moot=person_lib_empty,
            )
            raw_response = await _call_omni_messages(payload["messages"], config, adapter=adapter)
            audio_raw_response = None
    except OmniError as e:
        # omni API / 网络错:_call_omni_messages 已在源头打日志(omni API 调用失败),
        # 这里只做 inflight track 清理 + 上抛,不重复打。
        if candidates:
            await identity_engine.deliver_fused_failure(str(e))
        raise
    except Exception as e:  # noqa: BLE001 —— payload 构造失败(非 omni 调用)
        logger.error("[omni] payload 构造失败 | %s", e, exc_info=True)
        if candidates:
            await identity_engine.deliver_fused_failure(str(e))
        raise

    # inflight 加固(议题三):把"解析 + 写回 state"整段包进 try/finally。此前这段在 HTTP try 之外——
    # parse_omni_response / extract_usage / parse_identity_assignments / deliver_fused_response 任一
    # 中途抛异常 → mark_dispatched 已置 inflight=True 的 track 漏清(永不 GC、永不重派,直到进程重启)。
    delivered = False
    try:
        omni_output = parse_omni_response(raw_response, _rule_name_to_id(context))
        omni_output.usage = extract_usage(raw_response)

        # 双模型拆分：合并 audio 模型的 speeches/env_sounds
        if use_split and audio_raw_response is not None:
            audio_output = parse_omni_response(audio_raw_response, _rule_name_to_id(context))
            omni_output.speeches = audio_output.speeches
            omni_output.env_sounds = audio_output.env_sounds
            audio_usage = extract_usage(audio_raw_response)
            if omni_output.usage or audio_usage:
                v = omni_output.usage or {}
                a = audio_usage or {}
                omni_output.usage = {
                    "input_tokens": v.get("input_tokens", 0) + a.get("input_tokens", 0),
                    "output_tokens": v.get("output_tokens", 0) + a.get("output_tokens", 0),
                    "cached_tokens": v.get("cached_tokens", 0) + a.get("cached_tokens", 0),
                }

        # 抽 identity_assignments 并写回 state（仅当有 candidate 才有意义）
        if candidates:
            # name_to_pid 来源 1（library 全量）已在上面构造好。这里补来源 2：gallery_snapshot
            # 兜底（library 查询失败时仍有可用反查），真名 / 角色 / 完整标签都做 key。角色沿用
            # 来源 1 的 library 全局唯一性（role_counts）：来源 1 因不唯一跳过的角色这里不会加回；
            # library 查询整体失败时 role_counts 为空，角色一律不做 key（从严，避免误命中）。
            for pid, samples in gallery_snapshot.items():
                if samples.name:
                    name_to_pid.setdefault(samples.name, pid)
                    lbl = format_person_label(samples.name, samples.role)
                    if lbl:
                        name_to_pid.setdefault(lbl, pid)
                if samples.role and role_counts[samples.role] == 1:
                    name_to_pid.setdefault(samples.role, pid)
                name_to_pid.setdefault(pid, pid)

            # 解析 identity_assignments 时的校验参数（防 omni 输出 track_id 越权 / 幻觉成员）
            prompt_track_ids = {c.track_id for c in candidates}
            distinguish = identity_engine.config.stranger.distinguish
            confidence_cutoff = identity_engine.config.confidence_cutoff

            assignments = parse_identity_assignments(
                raw_response,
                name_to_pid=name_to_pid,
                prompt_track_ids=prompt_track_ids,
                distinguish=distinguish,
                confidence_cutoff=confidence_cutoff,
            )
            await identity_engine.deliver_fused_response(assignments)
        delivered = True
        return omni_output
    finally:
        # 兜底:任何未走到 deliver_fused_response 的退出(含未来新增的会抛步骤)都清 inflight。
        # deliver_fused_failure 幂等——deliver_response 成功已置 _pending=None → 此处短路 no-op;
        # 仅"有候选 且 未走完 deliver"时才回 on_result(failure)、清 inflight。
        if candidates and not delivered:
            await identity_engine.deliver_fused_failure(
                "run_omni_fused parse/deliver incomplete"
            )


# fused 模式共享 httpx.AsyncClient（连接池 + keepalive），避免每窗口一次 TLS 握手
# （省 ~50-100ms 连接延迟）。
#
# AsyncClient 绑定到创建时所在的 event loop；当那个 loop 被关闭（测试用例
# asyncio.run、CLI 一次性运行、FastAPI 重启等），cached client 后续使用会抛
# "Event loop is closed"。所以这里按 loop 缓存：每个 loop 一个 client，loop
# 不匹配（前一个 loop 已关闭）时重建。生产长跑场景下 loop 是稳定的同一个，
# 与单 client 等价，没有性能损失。
# 客户端不显式关闭（进程退出由 OS 回收）。
# omni_client.py 的 non-fused 路径暂不复用（改动面更大），后续可统一。
_fused_http_client: "httpx.AsyncClient | None" = None
_fused_http_client_loop: "asyncio.AbstractEventLoop | None" = None


def _get_fused_http_client(timeout: float) -> httpx.AsyncClient:
    global _fused_http_client, _fused_http_client_loop
    loop = asyncio.get_running_loop()
    if (
        _fused_http_client is None
        or _fused_http_client_loop is not loop
        or _fused_http_client.is_closed
    ):
        # 旧 client 绑定的 loop 已不可用——丢弃（GC 自然回收），新 loop 重建。
        _fused_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )
        _fused_http_client_loop = loop
    return _fused_http_client


async def _call_omni_messages(
    messages: list[dict],
    config: OmniConfig,
    type: str = "realtime",
    adapter: "OmniProviderAdapter | None" = None,
) -> dict[str, Any]:
    """调 omni——直接传 messages（fused 模式专用）。

    与 ``omni_client.call_omni`` 的差异：``call_omni`` 期望 ``payload[user_content]`` 是
    text 字符串，再由 ``_build_messages`` 拼接 video/crops；本函数允许调用方完全自定义
    messages（含 image_url / video_url 等多模态块）。
    """
    api_key = resolve_api_key(config)
    if not api_key:
        raise ValueError(
            "MILOCO_MODEL__OMNI__API_KEY is not set; cannot call fused omni"
        )

    if adapter is None:
        adapter = get_adapter(config.model)
    body = adapter.build_request_body(
        messages,
        model=config.model,
        max_tokens=config.max_completion_tokens,
        temperature=config.temperature,
        top_p=config.top_p,
        stream=False,
    )

    forced_stream = body.get("stream", False)
    url = adapter.endpoint(config.base_url, config.model, stream=forced_stream)

    client = _get_fused_http_client(config.timeout)
    cb = get_omni_circuit_breaker()
    t0 = time.monotonic()
    raw: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    short_circuited = False
    headers = {
        "Content-Type": "application/json",
        **adapter.auth_headers(api_key),
        "User-Agent": MILOCO_USER_AGENT,
    }
    try:
        await cb.before_call()
        if not forced_stream:
            resp = await client.post(url, headers=headers, json=body)
            classified = classify_response(resp)
            if classified is not None:
                await cb.record_failure(classified)
                logger.error(
                    "[omni] omni API 调用失败，错误码=%d | %s",
                    resp.status_code,
                    resp.text[:500],
                )
                if resp.status_code == 400:
                    logger.error(
                        "[omni] omni 400 payload 摘要 | %s",
                        _summarize_multimodal_payload(messages),
                    )
                resp.raise_for_status()
            raw = adapter.parse_response(resp.json())
        else:
            # forced-stream (Qwen 等 adapter 强制 stream=True) 走 _collect_stream_response,
            # 内部对非 200 直接 raise_for_status → HTTPStatusError。下方 except 守卫
            # `not isinstance(e, HTTPStatusError)` 会跳过 record_failure(防非流路径双重
            # 计数),导致 forced-stream 路径的 4xx/5xx 熔断器完全看不到。此处显式补一次
            # classify + record_failure 后再抛,与非流路径行为对齐。
            try:
                raw = await _collect_stream_response(
                    client, url, headers, body, adapter
                )
            except httpx.HTTPStatusError as e:
                classified = classify_response(e.response)
                if classified is not None:
                    await cb.record_failure(classified)
                    logger.error(
                        "[omni] omni API 调用失败(stream)，错误码=%d | %s",
                        e.response.status_code,
                        e.response.text[:500],
                    )
                raise
        # 服务端在 fused 大 payload 下偶发返回非 dict body (~1.5%);此处校验
        # 形态并 dump 截断后的原始响应,便于事后定位服务端返回了什么。
        if not isinstance(raw, dict):
            raw_cls = raw.__class__.__name__
            detail = f"type={raw_cls} body={str(raw)[:1000]}"
            if not forced_stream:
                detail = f"status={resp.status_code} {detail}"
            logger.error("[omni-fused] unexpected response shape | %s", detail)
            await cb.record_failure(
                ClassifiedError(
                    "bad_response",
                    f"non-dict body ({raw_cls})",
                    ErrorCategory.RECOVERABLE,
                )
            )
            raise OmniError(f"omni response is not a dict (got {raw_cls})")
        await cb.record_success()
        fire_record(config.model, raw.get("usage", {}), type)
        return raw
    except CircuitOpenError as ce:
        short_circuited = True
        error = {"code": ce.code, "msg": ce.message[:512]}
        raise OmniError(
            f"_call_omni_messages short-circuited: {ce.message}", original=ce
        ) from ce
    except OmniError:
        raise
    except Exception as e:
        if not isinstance(e, httpx.HTTPStatusError):
            await cb.record_failure(classify_exception(e))
        error = {"code": e.__class__.__name__, "msg": str(e)[:512]}
        raise OmniError(
            f"_call_omni_messages failed: {e.__class__.__name__}: {e}",
            original=e,
        ) from e
    finally:
        latency_ms = 0.0 if short_circuited else (time.monotonic() - t0) * 1000
        push_omni_trace(
            request_messages=messages,
            response_raw=raw,
            latency_ms=latency_ms,
            error=error,
            model=config.model,
            inference_params={
                "temperature": config.temperature,
                "top_p": config.top_p,
                "max_tokens": config.max_completion_tokens,
            },
        )


def _summarize_multimodal_payload(messages: list[dict]) -> str:
    """扫 fused messages, 输出每个多模态块 (image_url / video_url / input_audio)
    的 type + base64 长度 summary, 用于 400 错误事后定位是哪个块损坏。

    仅输出尺寸 (不输出实际 base64 内容, 避免 log 巨量数据)。``#N`` 是该块在所在
    message 的 content 数组内的下标 (含前置 text 块, 不是 per-type 序号; 每条
    message 各自从 0 重数, 故多 message payload 下 #N 不全局唯一)。fused payload
    通常只有单条 user message, content=[text, text, image, image, video] 时两张
    图标 #2/#3。输出形如:
    "text=12 blocks, image_url=[#2:42130b, #3:38904b], video_url=[#4:524288b],
     input_audio=[none]"
    """
    text_count = 0
    image_sizes: list[str] = []
    video_sizes: list[str] = []
    audio_sizes: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for idx, block in enumerate(content):
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "text":
                text_count += 1
            elif btype == "image_url":
                url = block.get("image_url", {}).get("url", "")
                # data URL: "data:image/jpeg;base64,XXX" → 取 XXX 长度
                b64 = url.split(",", 1)[1] if "," in url else ""
                image_sizes.append(f"#{idx}:{len(b64)}b")
            elif btype == "video_url":
                url = block.get("video_url", {}).get("url", "")
                b64 = url.split(",", 1)[1] if "," in url else ""
                video_sizes.append(f"#{idx}:{len(b64)}b")
            elif btype == "input_audio":
                data = block.get("input_audio", {}).get("data", "")
                b64 = data.split(",", 1)[1] if "," in data else ""
                audio_sizes.append(f"#{idx}:{len(b64)}b")
    return (
        f"text={text_count} blocks, "
        f"image_url=[{', '.join(image_sizes) or 'none'}], "
        f"video_url=[{', '.join(video_sizes) or 'none'}], "
        f"input_audio=[{', '.join(audio_sizes) or 'none'}]"
    )


# =============================================================================
# Streaming variants — early extraction of speeches, matched_rules, suggestions
# =============================================================================


async def run_omni_stream(
    edge_packet: IdentityPacket,
    context: OmniContext,
    config: OmniConfig,
    on_early_speeches: Callable[[list[Speech]], Awaitable[None]] | None = None,
    on_early_matched_rules: Callable[[list[MatchedRule]], Awaitable[None]]
    | None = None,
    on_early_suggestions: Callable[[list[Suggestion]], Awaitable[None]] | None = None,
) -> OmniOutput:
    """Run Omni layer with streaming — extracts actionable fields early via callbacks."""
    payload = build_stream_prompt(edge_packet, context)
    return await _stream_and_parse(
        payload,
        config,
        on_early_speeches,
        on_early_matched_rules,
        on_early_suggestions,
        rule_name_to_id=_rule_name_to_id(context),
    )


async def run_omni_batch_stream(
    edge_packets: list[IdentityPacket],
    context: OmniContext,
    config: OmniConfig,
    on_early_speeches: Callable[[list[Speech]], Awaitable[None]] | None = None,
    on_early_matched_rules: Callable[[list[MatchedRule]], Awaitable[None]]
    | None = None,
    on_early_suggestions: Callable[[list[Suggestion]], Awaitable[None]] | None = None,
) -> OmniOutput:
    """Run Omni layer for multiple devices with streaming — extracts actionable fields early."""
    payload = build_batch_stream_prompt(edge_packets, context)
    return await _stream_and_parse(
        payload,
        config,
        on_early_speeches,
        on_early_matched_rules,
        on_early_suggestions,
        rule_name_to_id=_rule_name_to_id(context),
    )


async def _stream_and_parse(
    payload: dict,
    config: OmniConfig,
    on_early_speeches: Callable[[list[Speech]], Awaitable[None]] | None,
    on_early_matched_rules: Callable[[list[MatchedRule]], Awaitable[None]] | None,
    on_early_suggestions: Callable[[list[Suggestion]], Awaitable[None]] | None,
    rule_name_to_id: "dict[str, str] | None" = None,
) -> OmniOutput:
    """Stream omni response, extract actionable fields early, then parse full output."""
    buffer = ""
    speeches_done = False
    matched_rules_done = False
    suggestions_done = False
    usage_out: dict = {}

    async for delta in call_omni_stream(payload, config, usage_out=usage_out):
        buffer += delta

        # 端侧 ngram 复读熔断：模型陷入末位 token 复读时立即 abort，避免继续
        # 生成到 max_tokens 触发 JSON 截断 + fallback 反馈环。
        if _has_loopback_tail(buffer):
            logger.warning(
                "loopback ngram detected mid-stream at %d chars, aborting stream: ...%s",
                len(buffer),
                buffer[-80:],
            )
            break

        # Skip extraction once all actionable fields are done
        if speeches_done and matched_rules_done and suggestions_done:
            continue

        if not speeches_done:
            result = try_extract_speeches(buffer)
            if result is not None:
                speeches_done = True
                logger.info(
                    "speeches extracted early at %d chars: %s",
                    len(buffer),
                    [(i.speaker, i.content, i.is_complete) for i in result],
                )
                if on_early_speeches and result:
                    await on_early_speeches(result)

        if not matched_rules_done:
            result = try_extract_matched_rules(buffer, rule_name_to_id)
            if result is not None:
                matched_rules_done = True
                logger.info(
                    "matched_rules extracted early at %d chars: %s",
                    len(buffer),
                    [(m.rule_id, m.reason) for m in result],
                )
                if on_early_matched_rules and result:
                    await on_early_matched_rules(result)

        if not suggestions_done:
            result = try_extract_suggestions(buffer)
            if result is not None:
                suggestions_done = True
                logger.info(
                    "suggestions extracted early at %d chars: %s",
                    len(buffer),
                    [(s.event, s.action) for s in result],
                )
                if on_early_suggestions and result:
                    await on_early_suggestions(result)

    output = parse_omni_response_from_text(buffer, rule_name_to_id)
    if usage_out:
        output.usage = dict(usage_out)
    return output
