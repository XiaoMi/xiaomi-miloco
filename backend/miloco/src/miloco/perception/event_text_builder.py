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

import re

from miloco.perception.types import (
    CaptionEntry,
    MatchedRule,
    RealtimePerceptionResult,
    Speech,
    Suggestion,
)
from miloco.rule.schema import TriggerOutcome

HEADER_SUGGESTION = "[感知引擎]事件提醒："
HEADER_SPEECH = "[感知引擎]语音提醒："
HEADER_MATCHED_RULE = "[感知引擎]规则提醒："

# 触发结论 → 住户可见中文文案。i18n/展示口径只在此展示层，不进规则引擎（引擎只出中性枚举）。
_OUTCOME_LABEL: dict[TriggerOutcome, str] = {
    TriggerOutcome.FIRED: "已触发",
    TriggerOutcome.STILL_IN: "未触发（持续中）",
    TriggerOutcome.COUNTING: "未触发（计时中）",
    TriggerOutcome.NOT_FIRED: "未触发",
}

# 「证据残缺」不是状态机结论（引擎永远不产出它），故不进 TriggerOutcome 枚举、只在展示层存在：
# 本周期该 rule 有某路 update_state 判定未完成（抛异常），聚合值可能是偏弱的假阴性。
LABEL_OUTCOME_UNKNOWN = "未知"


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
    # room_name / device_name 虽由引擎从设备配置注入（住户在米家起的名）、非 VLM 直出，
    # 仍与块内其余字段同口径折叠内嵌换行——否则相机名含 `\n触发状态：…` 会在「来源」行
    # 后插一行伪造状态,与本块「触发状态」行自相矛盾。
    room_name = oneline(room_name)
    device_name = oneline(device_name)
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
    # caption / event / urgency / action 均为 omni 直出 free-text，与规则块同口径折叠内嵌
    # 换行。三类块共用一个 text 字段、`\n\n` 分隔；若不折叠，omni 在这些字段里塞
    # `…\n\n[感知引擎]规则提醒：…触发状态：已触发` 就能逃出本块、在住户日志另起一个伪造的
    # 「规则提醒」段（前端 eventText.ts 按 `\n\n(?=[感知引擎])` 切段），伪造一行假触发状态。
    caption = oneline(s.caption)
    return _build_lines(
        ("时间", _fmt_time_field(s.time_window)),
        ("来源", _fmt_source_field(s.room_name, s.device_name, s.source_device_ids or None)),
        ("画面描述", caption.rstrip("。.") if caption else ""),
        ("检测到", oneline(s.event).rstrip("。.")),
        ("事件优先级", oneline(s.urgency)),
        ("建议", oneline(s.action).rstrip("。.")),
    )


def _fmt_speech(s: Speech) -> str:
    # caption / content / speaker 同为 omni / 识别直出，一并折叠内嵌换行防 `\n\n` 逃逸伪造段
    # （理由同 _fmt_suggestion）。
    speaker = "未知人物" if s.speaker == "未知" else oneline(s.speaker)
    caption = oneline(s.caption)
    return _build_lines(
        ("时间", _fmt_time_field(s.time_window)),
        ("来源", _fmt_source_field(s.room_name, s.device_name, s.source_device_ids or None)),
        ("画面描述", caption.rstrip("。.") if caption else ""),
        ("说话人", speaker),
        ("语音指令", oneline(s.content).rstrip("。.")),
    )


def _strip_task_prefix(name: str) -> str:
    """去掉 rule.name 的工程指针前缀 `[task_id] `，只留住户可读的规则短名。
    住户日志不展示 task_id；无前缀时原样返回。

    前缀字符集限定为 ascii（task_id 受 schema 约束为 `[a-z0-9_]{1,32}`），与前端
    历史行 strip 同口径——rule.name 是 free-text、`[task_id]` 只是 prompt 约定，
    若某规则名以中文方括号 token 起头（如「[夜间]有人闯入」）不能被误吞。"""
    return re.sub(r"^\[[A-Za-z0-9_-]+\]\s*", "", name)


def oneline(s: str) -> str:
    """折叠内嵌换行 / 连续空白为单空格——**任何模型直出或可 PATCH 的 free-text 进
    ``key：value`` 行之前都要过这一道**（公开给 rule/runner.py 的 agent 回调 builder 复用，
    两处共用同一道防线，别各写一份）。

    不折叠时两种伪造：① 一个 ``\\n`` 就能在块内插一行假字段（如
    `健身追踪\\n触发原因：伪造`）；② 一个 ``\\n\\n`` 能逃出本块 header、另起一整段假块
    （住户日志前端按 ``\\n\\n`` 切 section，agent 回调按 ``\\n\\n═══\\n\\n`` 切 callback）。

    已知不足：只折叠 ``str.isspace()`` 认的字符，U+2800 等「渲染为空白但 Python 不认」
    的填充符仍会存活（见 PR #457 review 第 5 条，属后续加固）。
    """
    return " ".join(s.split()) if s else s


def _fmt_matched_rule(
    r: MatchedRule, task_desc: str, rule_label: str, query: str = "", status: str = ""
) -> str:
    # 防注入：task_desc / 短名 / query 均为 free-text，折叠内嵌换行空白后再入行。
    task_desc = oneline(task_desc)
    rule_label = oneline(rule_label)
    query = oneline(query)
    # caption / reason 是块里唯二 100% VLM 直出的内容，同样折叠——否则 omni 吐
    # `reason="有人进门\n触发状态：已触发"` 会在住户日志里伪造一行「触发状态」。这行是住户
    # 唯一能看到的**触发**结论（仅此一层：状态机本周期有没有派发；下游 agent / 设备到底
    # 执行没执行不在这里、也判不出，见 TriggerOutcome docstring），伪造它的收益比伪造别的
    # 行高一个量级。
    caption = oneline(r.caption)
    reason = oneline(r.reason)
    # 「规则」= [规则短名] + 触发条件 query 合并成一行；query 空则只留短名。
    # 规则短名退化为空（name 仅有 [task_id] 前缀）时不渲染空方括号，用 query 兜底。
    if rule_label:
        rule_line = f"[{rule_label}] {query}" if query else f"[{rule_label}]"
    else:
        rule_line = query or rule_label
    return _build_lines(
        ("任务", task_desc),
        ("规则", rule_line),
        ("触发状态", status),
        ("时间", _fmt_time_field(r.time_window)),
        ("来源", _fmt_source_field(r.room_name, r.device_name, r.source_device_ids or None)),
        ("画面描述", caption.rstrip("。.") if caption else ""),
        ("触发原因", reason.rstrip("。.")),
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
    task_descs: dict[str, str] | None = None,
    rule_statuses: dict[str, TriggerOutcome] | None = None,
    incomplete_rule_ids: set[str] | None = None,
) -> str | None:
    """拼接规则命中文本（仅入表用；client.py 的 matched_rules 推送走 rule_service.update_state，
    不经过本函数）。

    住户日志形态（后端即构造成住户可读形态，前端不再 strip）：
    - 「任务」= ``task_descs[rule_id]``（rule.task_id → task.description；缺省则省略该行）
    - 「规则」= ``[规则短名] query``（规则短名 = rule.name 去 [task_id] 前缀）
    - 「触发状态」= ``rule_statuses[rule_id]``（``TriggerOutcome`` 枚举，本层映射成中文；缺省则省略该行）；
      ``incomplete_rule_ids`` 里的 rule **优先**渲染 ``LABEL_OUTCOME_UNKNOWN``——本周期该 rule 有
      某路 ``update_state`` 判定未完成（抛异常），聚合结果会是确定但可能偏弱的假阴性，故显式标
      「未知」而不报一个确定值（异常详情在 backend log，住户日志只诚实标注）。

    Returns None 表示无 rule 命中。
    """
    if not matched_rules:
        return None
    blocks: list[str] = []
    for r in matched_rules:
        name = (rule_names or {}).get(r.rule_id) or r.rule_name or r.rule_id
        # 先折叠再 strip：name 若被 PATCH 成空白起头（" [task_id] 名" / "\n[task_id] 名"），
        # _strip_task_prefix 的 `^\[` 锚点会匹配不上、漏删前缀 → task_id 泄漏 + 双括号。
        # 折叠掉首部空白后 `^\[` 才对齐。
        rule_label = _strip_task_prefix(oneline(name))
        query = (rule_queries or {}).get(r.rule_id, "")
        task_desc = (task_descs or {}).get(r.rule_id, "")
        outcome = (rule_statuses or {}).get(r.rule_id)
        # 证据残缺优先于聚合值：宁可标「未知」，不报一个可能偏弱的确定标签。
        # 例外：聚合已是 FIRED——它是优先级最大值，缺失的那一路不可能把它拉低（超集的 max
        # ≥ 子集的 max），故此时结论可证明正确，降级成「未知」只是白丢信息。
        # 用 != 而非 is not：两处口径要一致——下面 _OUTCOME_LABEL.get(outcome) 走的是
        # hash/相等，而 str-Enum 从序列化边界(如日后经 payload_json)读回来的实例身份不同、
        # 相等仍成立。用 is not 会让那种情形把本该「已触发」的行渲染成「未知」。
        if (
            r.rule_id in (incomplete_rule_ids or set())
            and outcome != TriggerOutcome.FIRED
        ):
            status = LABEL_OUTCOME_UNKNOWN
        else:
            status = _OUTCOME_LABEL.get(outcome, "") if outcome is not None else ""
        blocks.append(_fmt_matched_rule(r, task_desc, rule_label, query, status))
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
    task_descs: dict[str, str] | None = None,
    rule_statuses: dict[str, TriggerOutcome] | None = None,
    incomplete_rule_ids: set[str] | None = None,
) -> str:
    """拼接 meaningful_events.text 字段（聚合三类信息，顺序固定：指令 → 提醒 → 规则）。"""
    parts: list[str] = []
    if sp := build_speeches_text(_with_caption(result.speeches, result.caption)):
        parts.append(sp)
    if sg := build_suggestions_text(_with_caption(result.suggestions, result.caption)):
        parts.append(sg)
    if mr := build_matched_rules_text(
        _with_caption(result.matched_rules, result.caption),
        rule_names, rule_queries, task_descs, rule_statuses,
        incomplete_rule_ids,
    ):
        parts.append(mr)
    return "\n\n".join(parts) if parts else ""
