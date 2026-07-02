"""Omni Layer — Response Parser.

Parses raw omni model JSON response into RealtimePerceptionResult (OmniOutput).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from miloco.perception.engine.types import OmniOutput
from miloco.perception.types import CaptionEntry, MatchedRule, Speech, Suggestion

logger = logging.getLogger(__name__)


def parse_omni_response(
    raw: dict[str, Any],
    rule_name_to_id: "dict[str, str] | None" = None,
) -> OmniOutput:
    """Parse raw omni model response into OmniOutput (RealtimePerceptionResult).

    ``rule_name_to_id``：本窗规则的 ``rule_name → rule_id(UUID)`` 映射。模型在
    matched_rules 里照抄 rule_name（``[task_id] 描述``），这里还原回 rule_id 供下游
    去重/触发用。None 时退化为原样保留 name（best-effort）。
    """
    content = _extract_content(raw)
    if content is None:
        return _fallback("No content in model response")

    json_str = extract_json(content)
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return _fallback(f"Failed to parse JSON: {json_str[:200]}")

    if not isinstance(parsed, dict):
        return _fallback("Response is not an object")

    return _build_output(parsed, rule_name_to_id)


def parse_identity_assignments(
    raw: "dict[str, Any] | str",
    name_to_pid: "dict[str, str] | None" = None,
    *,
    prompt_track_ids: "set[int] | None" = None,
    distinguish: bool = False,
    confidence_cutoff: float = 0.5,
) -> list[dict]:
    """fused 主调用 response 中 ``identity_assignments`` 字段的抽取 + 校验。

    fused 模式下 omni 主调用 response JSON 多一个 ``identity_assignments`` 字段：
        [{"track_id":<int>, "name":"<姓名|unknown|no_person>", "confidence":0~1, "reason":"..."}]

    Args:
        raw:               omni 完整响应（dict）或累积的 streaming text（str）
        name_to_pid:      真名 / 角色 → person_id 反查表（不区分大小写）；None 时原样保留
        prompt_track_ids:  prompt 中给出的合法 track_id 集合；None 时不校验。**校验 1**：
                           response 里出现的 track_id 不在此集合则丢弃（防 omni 幻觉 track_id）
        distinguish:       陌生人编号开关。**校验 2**：``distinguish=False`` 时 ``unknown_<n>``
                           形式规范化为 ``unknown``
        confidence_cutoff: 置信度下限。**校验 3**：``confidence < cutoff`` 时强制视作 unknown

    Returns:
        list[dict]：每项含 ``track_id`` / ``person_id`` / ``confidence`` / ``reason`` /
                   ``raw_name``（原始 omni 输出 name，调试用）；解析失败或字段缺失返回 ``[]``。
    """
    if isinstance(raw, dict):
        content = _extract_content(raw)
        if content is None:
            return []
        json_str = extract_json(content)
    else:
        json_str = extract_json(raw)

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []
    return _parse_identity_assignments(
        parsed.get("identities", parsed.get("identity_assignments")),
        name_to_pid,
        prompt_track_ids=prompt_track_ids,
        distinguish=distinguish,
        confidence_cutoff=confidence_cutoff,
    )


def _parse_identity_assignments(
    raw: Any,
    name_to_pid: "dict[str, str] | None" = None,
    *,
    prompt_track_ids: "set[int] | None" = None,
    distinguish: bool = False,
    confidence_cutoff: float = 0.5,
) -> list[dict]:
    """规范化 ``identity_assignments`` 字段——容错每条结构 + 真名/角色反查 + 三个校验。"""
    import logging
    logger = logging.getLogger(__name__)

    if not isinstance(raw, list):
        return []

    # 规范化反查表：lower-case key
    lookup: dict[str, str] = {}
    if name_to_pid:
        for k, v in name_to_pid.items():
            if k:
                lookup[str(k).strip().lower()] = v

    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            tid = int(item.get("track_id"))
        except (TypeError, ValueError):
            continue

        # 校验 1：track_id 必须在 prompt 列表中（防 omni 幻觉 track_id）
        if prompt_track_ids is not None and tid not in prompt_track_ids:
            logger.warning("identity_assignment track_id=%d 不在 prompt 列表，丢弃", tid)
            continue

        # 兼容旧字段名 person_id（向后兼容）
        raw_name = item.get("name", item.get("person_id"))
        if raw_name in ("", None):
            raw_name = "unknown"
        raw_name_str = str(raw_name).strip()

        # 校验 2：distinguish=false 时 unknown_<n> / unknown-<scope>-<n> 规范化为 unknown
        # （match: unknown / unknown_<digit/track_id> / unknown_xxx / unknown-<scope>-<n>）
        lower = raw_name_str.lower()
        is_unknown_n = lower.startswith("unknown_") or lower.startswith("unknown-")
        # no_person：omni 判该框内确无人（非人误检），区别于 unknown（有人但认不出）。
        # 不走 gallery 反查、不打"不在 gallery"warning；person_id 记 None，下游靠 no_person 标志分流。
        is_no_person = lower == "no_person"
        if is_unknown_n and not distinguish:
            logger.info("distinguish=false 但收到 %r，规范化为 'unknown'", raw_name_str)
            raw_name_str = "unknown"

        # no_person / unknown / unknown_<n> 等 → person_id=None
        if is_no_person or raw_name_str.lower() == "unknown" or is_unknown_n:
            person_id: str | None = None
        else:
            # 反查
            if name_to_pid is None:
                person_id = raw_name_str
            else:
                hit = lookup.get(raw_name_str.lower())
                if not hit:
                    # omni 常把 gallery 里的完整标签"真名(角色:X)"整串回显; 精确命中失败时
                    # 剥掉尾部括号附注(半/全角)再试一次, 把"真名(角色:爸爸)"退回"真名"反查。
                    stripped = re.sub(r"[（(].*$", "", raw_name_str).strip()
                    if stripped and stripped != raw_name_str:
                        hit = lookup.get(stripped.lower())
                if hit:
                    person_id = hit
                else:
                    logger.warning("omni 输出 name=%r 不在 gallery，按 unknown 处理", raw_name_str)
                    person_id = None

        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        # 校验 3：confidence < cutoff 时强制 unknown
        if conf < confidence_cutoff and person_id is not None:
            logger.info("track_id=%d confidence=%.3f < cutoff=%.3f, 强制 unknown",
                        tid, conf, confidence_cutoff)
            person_id = None

        reason = str(item.get("reason", ""))[:50]

        out.append({
            "track_id": tid,
            "person_id": person_id if person_id is not None else "unknown",
            "confidence": conf,
            "reason": reason,
            "raw_name": raw_name_str,
            "no_person": is_no_person,
        })
    return out


def parse_query_response(raw: dict[str, Any]) -> str:
    """Parse raw omni model response for query pipeline — extract plain text."""
    content = _extract_content(raw)
    return content.strip() if content else ""


def extract_json(content: str) -> str:
    """Extract JSON from model response.

    MiMo often outputs: [garbage/thinking] + [valid JSON at the end].
    Strategy: try code blocks first, then search the full content for valid JSON.
    """
    # Strip <think>...</think> blocks
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
    # Strip everything before a bare </think> (no opening tag)
    cleaned = re.sub(r"^[\s\S]*?</think>", "", cleaned).strip()

    if not cleaned:
        cleaned = content.strip()

    # Try each markdown code block (last to first) for valid JSON
    blocks = list(re.finditer(r"```(?:\w*)?\s*\n?([\s\S]*?)\n?```", cleaned))
    for block in reversed(blocks):
        result = _find_last_valid_json(block.group(1).strip())
        try:
            json.loads(result)
            return result
        except (json.JSONDecodeError, ValueError):
            continue

    # Fallback: search the entire content for valid JSON
    return _find_last_valid_json(cleaned)


def _find_last_valid_json(content: str) -> str:
    """Find the last valid JSON object in content, searching from end to start."""
    # Find the position of the last }
    last_close = content.rfind("}")
    if last_close < 0:
        return content.strip()

    # Try progressively from different { positions (last to first)
    # to find the longest valid JSON ending at last_close
    best = None

    for i in range(last_close, -1, -1):
        if content[i] == "{":
            candidate = content[i : last_close + 1]
            try:
                json.loads(candidate)
                best = candidate  # Keep the longest valid JSON
            except json.JSONDecodeError:
                if best is not None:
                    break  # We already found a valid one, stop expanding

    if best is not None:
        return best

    # Fallback: return from last { to last }
    last_open = content.rfind("{")
    if last_open >= 0:
        return content[last_open : last_close + 1]

    return content.strip()


def _extract_content(raw: dict) -> str | None:
    choices = raw.get("choices", [])
    if not choices:
        return None
    return choices[0].get("message", {}).get("content")


def _build_output(
    parsed: dict,
    rule_name_to_id: "dict[str, str] | None" = None,
) -> OmniOutput:
    caption = _parse_caption(parsed.get("caption"))
    matched_rules = _parse_matched_rules(
        parsed.get("matched_rules"), rule_name_to_id
    )
    speeches = _parse_speeches(parsed.get("speeches"))
    env_sounds = _parse_env_sounds(parsed.get("env_sounds"))
    suggestions = _parse_suggestions(parsed.get("suggestions"))
    return OmniOutput(
        caption=caption,
        matched_rules=matched_rules,
        speeches=speeches,
        env_sounds=env_sounds,
        suggestions=suggestions,
    )


def _parse_caption(raw: Any) -> list[CaptionEntry]:
    """caption 是单串（仅画面变化时输出）→ 包成 1 元 CaptionEntry；无/空 → []。"""
    if isinstance(raw, str):
        text = raw.strip()
        return [CaptionEntry(description=text)] if text else []
    return []


def _resolve_rule_name(name: str, mapping: "dict[str, str] | None") -> "str | None":
    """rule_name → rule_id(UUID)。``mapping is None``（调用方未提供映射，仅测试 / streaming
    benchmark 走此路）时原样返回 name（best-effort）；有映射（含空 dict=本轮零规则）但匹配
    不上时返回 None（丢弃，防 bogus rule_id 入下游）。容错模型对名称的轻微改写。"""
    if mapping is None:
        return name or None
    if name in mapping:
        return mapping[name]
    for rn, rid in mapping.items():
        if rn and (rn in name or name in rn):
            return rid
    return None


def _parse_matched_rules(
    raw: Any,
    rule_name_to_id: "dict[str, str] | None" = None,
) -> list[MatchedRule]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # B 结构：hit=false = 模型评估为"未命中"（reason 是否定理由），直接丢弃、不触发下游。
        # hit 缺省视作命中，兼容旧 prompt 输出（无 hit 字段）。
        hit = item.get("hit", True)
        if hit is False or (isinstance(hit, str) and hit.strip().lower() in ("false", "0", "no")):
            continue
        # rule_name（模型照抄的完整规则名）→ 还原 rule_id（下游稳定键）；rule_name 一并存供展示
        name = str(item.get("rule_name", ""))
        rid = _resolve_rule_name(name, rule_name_to_id)
        if rid is None:
            logger.error(
                "omni 输出了不在「# 待判断规则」列表中的 rule_name=%r，判定为幻觉，丢弃不触发",
                name,
            )
            continue
        result.append(
            MatchedRule(
                rule_id=rid,
                rule_name=name,
                reason=str(item.get("reason", "")),
            )
        )
    return result


def _parse_speeches(raw: Any) -> list[Speech]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, dict):
            result.append(
                Speech(
                    # TODO 临时禁用语音指令链路：强制 needs_response=false。
                    # 影响：① client.py 不再 dispatch_event("interaction") → agent 不会被语音触发；
                    #      ② event_classifier.has_asr 恒 false → 仅 ASR 的窗口不入 meaningful_events 表。
                    # 恢复：删除下面 False 覆盖行，启用上面被注释的原行。
                    # needs_response=bool(item.get("needs_response", False)),
                    needs_response=False,
                    speaker=str(item.get("speaker", "")),
                    content=str(item.get("content", "")),
                    is_complete=bool(item.get("is_complete", True)),
                )
            )
    return result


def _parse_env_sounds(raw: Any) -> list[str]:
    """env_sounds 是单串（有非人声事件才输出）→ 包成 1 元 list；空/无 → []。"""
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    return []


def _parse_suggestions(raw: Any) -> list[Suggestion]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, dict):
            urgency = item.get("urgency", "low")
            # urgency=ignore 是 prompt 给微小动作的"丢弃"档：模型偶尔会违规把它写进
            # suggestions，这里直接剔除，避免被下面 coerce 成 low 反而冒泡上报。
            if urgency == "ignore":
                continue
            if urgency not in ("high", "medium", "low"):
                urgency = "low"
            result.append(
                Suggestion(
                    event=str(item.get("event", "")),
                    action=str(item.get("action", "")),
                    urgency=urgency,
                )
            )
    return result


def _fallback(error: str) -> OmniOutput:
    return OmniOutput(
        skipped=True,  # 解析失败的情况，直接 skip
        caption=[CaptionEntry(description=f"[解析失败] {error}")],
    )


# =============================================================================
# Streaming support — early extraction and text-based parsing
# =============================================================================


def _try_extract_array(buffer: str, key: str) -> list | None:
    """Extract a JSON array value by key from a partial streaming buffer.

    Uses a bracket-depth state machine to detect when "key":[...] is fully
    closed. Returns the parsed list on success, None if not yet complete.
    """
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", buffer)
    cleaned = re.sub(r"^[\s\S]*?</think>", "", cleaned)
    if not cleaned:
        cleaned = buffer

    match = re.search(rf'"{key}"\s*:\s*\[', cleaned)
    if not match:
        return None

    start = match.end() - 1  # position of the opening [
    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(cleaned)):
        c = cleaned[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c in ("[", "{"):
            depth += 1
        elif c in ("]", "}"):
            depth -= 1
            if depth == 0:
                array_str = cleaned[start : i + 1]
                try:
                    return json.loads(array_str)
                except json.JSONDecodeError:
                    return None

    return None  # array not yet closed


def try_extract_speeches(buffer: str) -> list[Speech] | None:
    """Try to extract the speeches array from a partial streaming buffer."""
    raw = _try_extract_array(buffer, "speeches")
    if raw is None:
        return None
    return _parse_speeches(raw)


def try_extract_matched_rules(
    buffer: str,
    rule_name_to_id: "dict[str, str] | None" = None,
) -> list[MatchedRule] | None:
    """Try to extract the matched_rules array from a partial streaming buffer."""
    raw = _try_extract_array(buffer, "matched_rules")
    if raw is None:
        return None
    return _parse_matched_rules(raw, rule_name_to_id)


def try_extract_suggestions(buffer: str) -> list[Suggestion] | None:
    """Try to extract the suggestions array from a partial streaming buffer."""
    raw = _try_extract_array(buffer, "suggestions")
    if raw is None:
        return None
    return _parse_suggestions(raw)


def parse_omni_response_from_text(
    text: str,
    rule_name_to_id: "dict[str, str] | None" = None,
) -> OmniOutput:
    """Parse accumulated streaming text directly into OmniOutput.

    Unlike parse_omni_response() which expects the full API response dict,
    this takes the raw text content (concatenated delta tokens).
    """
    json_str = extract_json(text)
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return _fallback(f"Failed to parse JSON: {json_str[:200]}")

    if not isinstance(parsed, dict):
        return _fallback("Response is not an object")

    return _build_output(parsed, rule_name_to_id)


def parse_tier_c_verify_response(raw: dict[str, Any]) -> dict[str, Any]:
    """解析"写 tier_c 前同人校验"(设计文档 E7)的 omni 1v1 响应。

    期望输出 JSON: ``{"same_person": bool, "confidence": 0-1, "reason": str}``。
    解析失败 / 字段缺失一律降级为 same_person=False(保守:判不出就不写库)。
    """
    fallback = {"same_person": False, "confidence": 0.0, "reason": "校验响应解析失败"}
    content = _extract_content(raw)
    if not content:
        return fallback
    try:
        data = json.loads(extract_json(content))
    except (json.JSONDecodeError, ValueError, TypeError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return {
        "same_person": bool(data.get("same_person", False)),
        "confidence": max(0.0, min(1.0, conf)),
        "reason": str(data.get("reason", ""))[:50],
    }
