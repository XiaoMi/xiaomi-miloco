# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""构造给 agent / DB 的聚合文本。

格式：单条按 key:value 多行竖排展开（与 rule 一致）；多条同类用 `═══` 分隔；
不同类别（voice / suggestion / rule）之间在 build_agent_text 里仍用 `\\n\\n` 分隔。

- `HEADER_SUGGESTION` / `HEADER_SPEECH` / `HEADER_MATCHED_RULE` — 分类型 header 常量
- `caption_for_dids(captions, dids)` — 按 did 匹配 CaptionEntry 取 description
- `build_speeches_text(speeches)` — speeches 推送文本（过滤 needs_response+complete）
- `build_suggestions_text(suggestions)` — suggestions 推送文本
- `build_matched_rules_text(rules)` — 仅供 build_agent_text 入表使用（client.py 的
  matched_rules 推送走 rule_service.update_state，不拼文本）
- `build_agent_text(result)` — 给 meaningful_events.text 用的聚合函数
"""

from __future__ import annotations

from miloco.perception.types import (
    CaptionEntry,
    MatchedRule,
    RealtimePerceptionResult,
    Speech,
    Suggestion,
)

HEADER_SUGGESTION = "[感知引擎]事件提醒："
HEADER_SPEECH = "[感知引擎]语音提醒："
HEADER_MATCHED_RULE = "[感知引擎]规则提醒："


def _fmt_time_field(time_window: str) -> str:
    """竖排 key:value 用的纯时间字符串：去掉 [] 包裹，取窗口开始时刻（与注入 agent 的 current_time 对齐）。"""
    if not time_window:
        return ""
    tw = time_window.strip("[]")
    return tw.split("-")[0] if "-" in tw else tw


def _fmt_source_field(
    room_name: str, device_name: str, source_device_ids: list[str] | None = None
) -> str:
    """竖排 key:value 用的来源字符串（无 '来自：' 前缀和句号）。"""
    did_tag = f"(did={','.join(source_device_ids)})" if source_device_ids else ""
    if room_name and device_name:
        return f"{room_name}的{device_name}{did_tag}"
    if room_name:
        return f"{room_name}{did_tag}" if did_tag else room_name
    if device_name:
        return f"{device_name}{did_tag}"
    return ""


def _build_lines(*pairs: tuple[str, str]) -> str:
    """key:value 多行拼接，空 value 字段自动跳过。"""
    return "\n".join(f"{k}：{v}" for k, v in pairs if v)


def caption_for_dids(
    captions: list[CaptionEntry],
    source_device_ids: list[str],
) -> str:
    for c in captions:
        for did in source_device_ids:
            if did in c.source_device_ids:
                return c.description
    return ""


def _fmt_suggestion(s: Suggestion) -> str:
    return _build_lines(
        ("时间", _fmt_time_field(s.time_window)),
        ("来源", _fmt_source_field(s.room_name, s.device_name, s.source_device_ids or None)),
        ("画面描述", s.caption.rstrip("。.") if s.caption else ""),
        ("检测到", s.event.rstrip("。.")),
        ("事件优先级", s.urgency),
        ("建议", s.action.rstrip("。.")),
    )


def _fmt_speech(s: Speech) -> str:
    speaker = "未知人物" if s.speaker == "未知" else s.speaker
    return _build_lines(
        ("时间", _fmt_time_field(s.time_window)),
        ("来源", _fmt_source_field(s.room_name, s.device_name, s.source_device_ids or None)),
        ("画面描述", s.caption.rstrip("。.") if s.caption else ""),
        ("说话人", speaker),
        ("语音指令", s.content.rstrip("。.")),
    )


def _fmt_matched_rule(r: MatchedRule, name: str, query: str = "") -> str:
    return _build_lines(
        ("时间", _fmt_time_field(r.time_window)),
        ("来源", _fmt_source_field(r.room_name, r.device_name, r.source_device_ids or None)),
        ("画面描述", r.caption.rstrip("。.") if r.caption else ""),
        ("触发条件", query or name),
        ("触发原因", r.reason.rstrip("。.")),
    )


def build_text(header: str, blocks: list[str]) -> str | None:
    """header + 多条 key:value 块，块间用 `═══` 分隔。"""
    if not blocks:
        return None
    body = "\n\n═══\n\n".join(blocks)
    return f"{header}\n{body}"


def build_speeches_text(speeches: list[Speech]) -> str | None:
    """拼接语音指令文本（过滤 needs_response=True AND is_complete=True 的 Speech）。

    Returns None 表示无满足条件的 Speech（调用方应跳过推送）。
    """
    commands = [s for s in speeches if s.needs_response and s.is_complete]
    if not commands:
        return None
    return build_text(HEADER_SPEECH, [_fmt_speech(s) for s in commands])


def build_suggestions_text(suggestions: list[Suggestion]) -> str | None:
    """拼接建议消息文本。

    Returns None 表示无 suggestion（调用方应跳过推送）。
    """
    if not suggestions:
        return None
    return build_text(HEADER_SUGGESTION, [_fmt_suggestion(s) for s in suggestions])


def build_matched_rules_text(
    matched_rules: list[MatchedRule],
    rule_names: dict[str, str] | None = None,
    rule_queries: dict[str, str] | None = None,
) -> str | None:
    """拼接规则命中文本（仅入表用；client.py 的 matched_rules 推送走 rule_service.update_state，
    不经过本函数）。

    Returns None 表示无 rule 命中。
    """
    if not matched_rules:
        return None
    blocks: list[str] = []
    for r in matched_rules:
        name = (rule_names or {}).get(r.rule_id) or r.rule_name or r.rule_id
        query = (rule_queries or {}).get(r.rule_id, "")
        blocks.append(_fmt_matched_rule(r, name, query))
    return build_text(HEADER_MATCHED_RULE, blocks)


def _with_caption(items: list, captions: list[CaptionEntry]) -> list:
    """model_copy 后按 did 注入 caption，不 mutate 原对象。"""
    if not items or not captions:
        return items
    copies = [item.model_copy() for item in items]
    for item in copies:
        if not item.caption and item.source_device_ids:
            item.caption = caption_for_dids(captions, item.source_device_ids)
    return copies


def build_agent_text(
    result: RealtimePerceptionResult,
    rule_names: dict[str, str] | None = None,
    rule_queries: dict[str, str] | None = None,
) -> str:
    """拼接 meaningful_events.text 字段（聚合三类信息，顺序固定：指令 → 提醒 → 规则）。"""
    parts: list[str] = []
    if sp := build_speeches_text(_with_caption(result.speeches, result.caption)):
        parts.append(sp)
    if sg := build_suggestions_text(_with_caption(result.suggestions, result.caption)):
        parts.append(sg)
    if mr := build_matched_rules_text(_with_caption(result.matched_rules, result.caption), rule_names, rule_queries):
        parts.append(mr)
    return "\n\n".join(parts) if parts else ""
