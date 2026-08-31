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
        # 与 unknown 同口径放宽匹配：容忍附注 / 大小写 / 空格 / 连字符变体（omni 有回显完整标签的
        # 习惯，如 "no_person（3D打印机）" / "no person"），避免格式抖动时静默退化成 unknown，
        # 把本该抑制的误检框又当"陌生人"描述（即 no_person 要修的老 bug 回归）。
        normalized = re.sub(r"[\s\-]+", "_", lower).strip("_")
        is_no_person = normalized.startswith("no_person")
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

    安全提取链（逐层增强，历史教训来自真实模型输出）：
      1. ``_sanitize_content`` —— BOM / CRLF / 控制字符归一（模型偶发夹带，json.loads
         对部分控制字符直接 400）；
      2. ``<think>...</think>`` 及裸 ``</think>`` 剥离（思考内容混入 content 时）；
      3. Markdown 围栏提取（```json / ``` / ```` 四反引号 / 标签后带空格，见
         ``_FENCE_RE``）——Gemini 系模型输出围栏包裹 JSON 的比例很高（实测 trace
         55% 带围栏），围栏内再走 _find_last_valid_json；
      4. 全文兜底：从右往左找最长有效 JSON 子串（容忍前缀/后缀说明文本）；
      5. ``_loose_loads`` —— 标准解析失败时仅做「去尾随逗号」修复重试
         （``{"a":1,}`` / ``[1,2,]`` 是模型最常见 JSON 语法错误；不做单引号/注释
         修复——字符串内误伤风险高、模型场景罕见）。
    """
    if not content:
        raise json.JSONDecodeError("empty content", "", 0)

    cleaned = _sanitize_content(content)
    # Strip <think>...</think> blocks
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", cleaned).strip()
    # Strip everything before a bare </think> (no opening tag)
    cleaned = re.sub(r"^[\s\S]*?</think>", "", cleaned).strip()

    if not cleaned:
        cleaned = _sanitize_content(content).strip()

    # Try each markdown code block (last to first) for valid JSON
    blocks = list(re.finditer(_FENCE_RE, cleaned))
    for block in reversed(blocks):
        result = _find_last_valid_json(block.group(1).strip())
        try:
            return _loose_loads(result)  # 返回可被 json.loads 直接消费的文本
        except (json.JSONDecodeError, ValueError):
            continue

    # Fallback: search the entire content for valid JSON
    return _find_last_valid_json(cleaned)


def _sanitize_content(content: str) -> str:
    """归一化模型输出文本：去 BOM、CRLF → LF、剔除 JSON 非法控制字符。

    只保留 ``\\n`` / ``\\r`` / ``\\t`` 三个空白控制符，其余 < 0x20 的字符（如
    ``\\x00`` / ``\\x1b`` 转义序列残留）剔除——json.loads 对部分控制字符直接抛
    ``Unterminated string``，是「模型输出看似正常却解析失败」的隐藏来源。
    """
    s = content.lstrip("\ufeff")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in s if ch >= " " or ch in "\n\t")


# 围栏正则：3 或 4 个反引号；语言标签可选（json / 无标签 / 任意词），标签后允许空格
# 直接跟换行（```json\n / ``` json\n / ````json```` 均可命中）。
_FENCE_RE = re.compile(
    r"`{3,4}[^\n`]*(?:\n?)([\s\S]*?)(?:\n?)`{3,4}"
)


def _loose_loads(text: str) -> str:
    """标准 ``json.loads`` 失败后，做一次「去尾随逗号」修复重试。

    修复规则：``,`` 后紧跟 ``}`` / ``]``（中间只允许空白）时删除该逗号——
    ``{"a":1,}`` / ``[1,2,]`` / ``{"a":[1,],}`` 等模型高频语法错误。

    返回**解析成功的文本**（可能是修复后的），调用方可直接 ``json.loads``；
    修复无果时抛原始 JSONDecodeError（不吞、不扩大修复面——单引号/注释修复
    字符串内误伤风险高、模型场景罕见，不做）。
    """
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        fixed = re.sub(r",(\s*[}\]])", r"\1", text)
        if fixed == text:
            raise
        json.loads(fixed)  # 修复后仍失败 → 抛新异常（语义等价，调用方走 fallback）
        return fixed


def _find_last_valid_json(content: str) -> str:
    """Find the last valid JSON object in content, searching from end to start."""
    # Find the position of the last }
    last_close = content.rfind("}")
    if last_close < 0:
        # 对象根缺失：退而求其次找数组根（模型偶发输出顶层数组）
        last_close = content.rfind("]")
        if last_close < 0:
            return content.strip()
        open_ch = "["
    else:
        open_ch = "{"

    # Try progressively from different open positions (last to first)
    # to find the longest valid JSON ending at last_close
    best = None

    for i in range(last_close, -1, -1):
        if content[i] == open_ch:
            candidate = content[i : last_close + 1]
            try:
                best = _loose_loads(candidate)  # 返回解析成功的文本（可能已去尾随逗号）
            except (json.JSONDecodeError, ValueError):
                if best is not None:
                    break  # We already found a valid one, stop expanding

    if best is not None:
        return best

    # Fallback: return from last open to last close
    last_open = content.rfind(open_ch)
    if last_open >= 0:
        candidate = content[last_open : last_close + 1]
        try:
            return _loose_loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return candidate

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
    # pet_identities 是 prompt 侧的宠物称呼唯一真源（caption / suggestions / matched_rules 据其
    # 派生），**不入 OmniOutput、不进 IdentityEngine / person 表**（红线）。这里只落审计日志，
    # 便于事后核「叫了谁、凭什么」——**不据此改变任何下游行为**。
    # PET_NAMING_SPEC「规则点名宠物须 conf=high」的代码侧强制留待后续：可靠判定「某条规则是否
    # 在点名某只宠物」需要结构化信息，靠「宠物名是规则名子串」会误吞无关规则（宠物叫「宝宝」时
    # 连「宝宝独处提醒」这类人身安全规则一起丢），代价高于收益。
    pet_high, pet_mid = _parse_pet_identities(parsed.get("pet_identities"))
    if pet_high or pet_mid:
        logger.info(
            "event=pet_identities high=%s mid=%s",
            sorted(_sanitize_for_log(n) for n in pet_high),
            sorted(_sanitize_for_log(n) for n in pet_mid),
        )
    matched_rules = _parse_matched_rules(parsed.get("matched_rules"), rule_name_to_id)
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


def _sanitize_for_log(s: str) -> str:
    """模型可控串进日志前压平换行 + 截断（同 pet_library 的日志注入加固口径）。"""
    return re.sub(r"\s+", " ", str(s)).strip()[:40]


def _parse_pet_identities(raw: Any) -> tuple[set[str], set[str]]:
    """``pet_identities`` → ``(high 名单, mid 名单)``。

    **仅供审计日志**；不入 OmniOutput、不进 IdentityEngine / person 表，也不改变下游行为
    （名字直接来自模型输出、未与花名册核对，不足以作过滤依据）。
    """
    if not isinstance(raw, list):
        return set(), set()
    high: set[str] = set()
    mid: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()[:32]
        if not name:
            continue
        (high if str(item.get("conf", "")).strip().lower() == "high" else mid).add(name)
    return high, mid


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
        # hit 字段模型**时有时无**（真实数据 ~4% 缺失）：缺失时不能默认触发也不能默认丢弃
        # ——由 reason 兜底判定（reason 明确否定 → 丢弃；reason 正常 → 保留，兼容漏写 hit
        # 的合法命中，见 _reason_negates）。
        hit = item.get("hit")
        if hit is False or (
            isinstance(hit, str) and hit.strip().lower() in ("false", "0", "no")
        ):
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
        reason = str(item.get("reason", ""))
        if _reason_negates(reason):
            # reason 与 hit 自相矛盾 / reason 明确否定：模型要么把否定结论写进 reason
            # 却漏写或误写 hit（如「画面中只有床铺和家具，并没有人在床上读书」），
            # 要么把 hit=false 直接写进 reason 文本（旧格式残留）。reason 是判断依据的
            # 一手文本，按它的否定结论丢弃（宁漏勿误），并留痕便于监控。
            logger.warning(
                "omni matched_rules reason 明确否定，丢弃不触发 "
                "(rule_name=%r, hit=%r, reason=%r)",
                name, hit, reason[:160],
            )
            continue
        result.append(
            MatchedRule(
                rule_id=rid,
                rule_name=name,
                reason=reason,
            )
        )
    return result


# reason 明确否定的信号（命中即视为「模型在 reason 里写了否定结论」）。
# 否定词后 15 字内出现规则成立关键词才算命中：覆盖「并没有人在床上读书」/「未见
# 散落的黑色垃圾」/「不满足规则要素」/「hit=false」等矛盾或旧格式输出；同时不误杀
# 合法命中——reason 正常（无否定词）的条目原样保留，包括「不属于线缆」这类排除
# 干扰物的常见表述（「不属于」不在否定词表）。关键词表覆盖动作类与名词类规则主体，
# 可按需扩充。
_REASON_NEGATION_RE = re.compile(
    r"(?:并没有|并未|没有|没在|不在|未在|并非|不是|并不|未见|未发现|未观察到|"
    r"未看到|未检测到|不满足|不符合|不成立|不匹配|未)[^。；;，,]{0,15}"
    r"(?:读书|看书|阅读|纸质书|命中|成立|满足|符合|有人在|人在|看到人|发现人|"
    r"垃圾|杂物|行为|要素)"
    r"|(?:没有人|没有任何人|并无人在|未见人|没看到人|未发现人|未看到人|"
    r"未检测到人|无人在|空无一人|hit\s*[:=]\s*false)"
)


def _reason_negates(reason: str) -> bool:
    """reason 文本是否包含明确的否定结论（hit 缺失/为 true 时用于丢弃）。"""
    return bool(reason) and _REASON_NEGATION_RE.search(reason) is not None


def _parse_speeches(raw: Any) -> list[Speech]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, dict):
            result.append(
                Speech(
                    needs_response=bool(item.get("needs_response", False)),
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
