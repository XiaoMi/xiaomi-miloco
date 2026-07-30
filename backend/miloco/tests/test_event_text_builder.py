# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for event_text_builder(D3-T4).

3 个 builder 函数的输入输出格式;空输入返 None;空 result → build_agent_text 返 ''.
"""

from miloco.perception.event_text_builder import (
    build_agent_text,
    build_matched_rules_text,
    build_speeches_text,
    build_suggestions_text,
)
from miloco.perception.types import (
    MatchedRule,
    RealtimePerceptionResult,
    Speech,
    Suggestion,
    suggestion_intra_priority,
)


class TestBuildSpeechesText:
    def test_empty_returns_none(self):
        assert build_speeches_text([]) is None

    def test_no_commands_returns_none(self):
        """全是 needs_response=False 或 incomplete → None."""
        speeches = [
            Speech(needs_response=False, speaker="A", content="闲聊", is_complete=True),
            Speech(needs_response=True, speaker="B", content="开...", is_complete=False),
        ]
        assert build_speeches_text(speeches) is None

    def test_single_command(self):
        i = Speech(
            needs_response=True, speaker="用户", content="打开窗户", is_complete=True
        )
        text = build_speeches_text([i])
        assert text is not None
        assert text.startswith("[感知引擎]语音提醒：\n")
        assert "说话人：用户" in text
        assert "语音指令：打开窗户" in text

    def test_multiple_commands_separated(self):
        """多条 voice 用 \\n\\n═══\\n\\n 分隔。"""
        ints = [
            Speech(needs_response=True, speaker="A", content="开灯", is_complete=True),
            Speech(needs_response=True, speaker="B", content="关空调", is_complete=True),
        ]
        text = build_speeches_text(ints)
        assert "\n\n═══\n\n" in text
        assert "语音指令：开灯" in text
        assert "语音指令：关空调" in text

    def test_filters_chat_and_incomplete(self):
        """混合 commands + chat + incomplete:只保留 commands。"""
        ints = [
            Speech(needs_response=False, speaker="A", content="闲聊", is_complete=True),
            Speech(needs_response=True, speaker="B", content="cmd1", is_complete=True),
            Speech(needs_response=True, speaker="C", content="开...", is_complete=False),
            Speech(needs_response=True, speaker="D", content="cmd2", is_complete=True),
        ]
        text = build_speeches_text(ints)
        assert text.count("═══") == 1  # 2 条 command → 1 个分隔
        assert "cmd1" in text
        assert "cmd2" in text
        assert "闲聊" not in text

    def test_caption_in_speech(self):
        """caption 非空时渲染"画面描述"段."""
        i = Speech(
            needs_response=True, speaker="用户", content="关灯", is_complete=True,
            caption="卧室灯亮着",
        )
        text = build_speeches_text([i])
        assert "画面描述：卧室灯亮着" in text

    def test_unknown_speaker_renders_as_unknown_person(self):
        """speaker="未知" 渲染为 "未知人物"."""
        i = Speech(
            needs_response=True, speaker="未知", content="小爱同学", is_complete=True,
        )
        text = build_speeches_text([i])
        assert "说话人：未知人物" in text
        assert "语音指令：小爱同学" in text

    def test_source_meta_in_text(self):
        """room_name / device_name / time_window 非空时按 key:value 出现。"""
        i = Speech(
            needs_response=True, speaker="用户", content="开灯", is_complete=True,
            room_name="客厅", device_name="小米C700",
            time_window="[20:42:25-20:42:28]",
        )
        text = build_speeches_text([i])
        assert "来源：客厅的小米C700" in text
        assert "时间：20:42:25" in text


class TestBuildSuggestionsText:
    def test_empty_returns_none(self):
        assert build_suggestions_text([]) is None

    def test_single_suggestion(self):
        s = Suggestion(
            event="室内高温", action="建议开空调",
            room_name="客厅", device_name="小米C700",
        )
        text = build_suggestions_text([s])
        assert text is not None
        assert text.startswith("[感知引擎]事件提醒：")
        assert "来源：客厅的小米C700" in text
        assert "检测到：室内高温" in text
        assert "建议：建议开空调" in text

    def test_single_no_separator(self):
        """单条无 ═══ 分隔。"""
        text = build_suggestions_text([Suggestion(event="e", action="a")])
        assert "═══" not in text

    def test_multiple_suggestions(self):
        suggs = [
            Suggestion(event="e1", action="a1"),
            Suggestion(event="e2", action="a2"),
        ]
        text = build_suggestions_text(suggs)
        assert text.count("═══") == 1
        assert "检测到：e1" in text
        assert "检测到：e2" in text

    def test_caption_present(self):
        """caption 非空时渲染"画面描述"段."""
        s = Suggestion(
            event="老人摔倒", action="立即拨打急救电话",
            room_name="客厅", device_name="摄像头1",
            caption="老人坐在沙发上",
        )
        text = build_suggestions_text([s])
        assert "画面描述：老人坐在沙发上" in text

    def test_caption_absent(self):
        """caption 空时不渲染"画面描述"段."""
        s = Suggestion(event="e", action="a", caption="")
        text = build_suggestions_text([s])
        assert "画面描述" not in text

    def test_no_source_when_empty(self):
        """room_name / device_name 都空时不渲染"来源"。"""
        text = build_suggestions_text([Suggestion(event="e", action="a")])
        assert "来源" not in text


class TestBuildMatchedRulesText:
    def test_empty_returns_none(self):
        assert build_matched_rules_text([]) is None

    def test_single_rule(self):
        r = MatchedRule(rule_id="rule-001", reason="厨房有人在炒菜")
        text = build_matched_rules_text([r])
        assert text is not None
        assert text.startswith("[感知引擎]规则提醒：")
        assert "触发条件：rule-001" in text
        assert "触发原因：厨房有人在炒菜" in text

    def test_rule_with_name_lookup(self):
        """rule_names dict 传入时用名称替代 rule_id(query 空则 fallback 到 name)."""
        r = MatchedRule(rule_id="rule-001", reason="炒菜")
        text = build_matched_rules_text([r], rule_names={"rule-001": "厨房安全"})
        assert "触发条件：厨房安全" in text
        assert "rule-001" not in text

    def test_rule_with_query(self):
        """rule_queries dict 传入时 '触发条件' 渲染 query 而非 name."""
        r = MatchedRule(rule_id="rule-001", reason="检测到明火")
        text = build_matched_rules_text(
            [r],
            rule_names={"rule-001": "厨房安全"},
            rule_queries={"rule-001": "厨房是否有明火"},
        )
        assert "触发条件：厨房是否有明火" in text
        assert "厨房安全" not in text

    def test_rule_with_source_meta(self):
        """room_name + device_name 非空时按 key:value 渲染"来源"。"""
        r = MatchedRule(
            rule_id="rule-001", reason="厨房有人在炒菜",
            room_name="厨房", device_name="小米智能摄像机C700",
        )
        text = build_matched_rules_text([r])
        assert "来源：厨房的小米智能摄像机C700" in text

    def test_rule_with_caption(self):
        """caption 非空时渲染"画面描述"段."""
        r = MatchedRule(
            rule_id="rule-001", reason="炒菜",
            room_name="厨房", device_name="摄像头",
            caption="厨房内有人在灶台前操作",
        )
        text = build_matched_rules_text([r])
        assert "画面描述：厨房内有人在灶台前操作" in text

    def test_rule_without_caption(self):
        """caption 空时不渲染"画面描述"段."""
        r = MatchedRule(rule_id="rule-001", reason="x")
        text = build_matched_rules_text([r])
        assert "画面描述" not in text

    def test_bare_rule_no_source(self):
        """room_name 空时不渲染"来源"。"""
        text = build_matched_rules_text([MatchedRule(rule_id="r2", reason="x")])
        assert "来源" not in text


class TestBuildAgentText:
    def test_empty_result_returns_empty_string(self):
        r = RealtimePerceptionResult()
        assert build_agent_text(r) == ""

    def test_caption_only_returns_empty(self):
        """纯 caption(无 rule/asr/suggestion)→ DB.text 应为空."""
        from miloco.perception.types import CaptionEntry

        r = RealtimePerceptionResult(
            caption=[CaptionEntry(description="有人在看电视")]
        )
        assert build_agent_text(r) == ""

    def test_all_three_in_order(self):
        """speeches / suggestions / matched_rules 顺序固定."""
        r = RealtimePerceptionResult(
            speeches=[
                Speech(needs_response=True, speaker="u", content="c", is_complete=True)
            ],
            suggestions=[Suggestion(event="e", action="a")],
            matched_rules=[MatchedRule(rule_id="r1", reason="x")],
        )
        text = build_agent_text(r)
        idx_speech = text.index("语音指令")
        idx_sug = text.index("检测到：e")
        idx_rule = text.index("触发原因：x")
        assert idx_speech < idx_sug < idx_rule

    def test_only_present_sections_included(self):
        """仅 suggestions 时,text 不含语音/规则内容."""
        r = RealtimePerceptionResult(
            suggestions=[Suggestion(event="e", action="a")]
        )
        text = build_agent_text(r)
        assert "检测到：e" in text
        assert "语音指令" not in text
        assert "触发条件" not in text

    def test_sections_separated_by_blank_line(self):
        r = RealtimePerceptionResult(
            speeches=[
                Speech(needs_response=True, speaker="u", content="c", is_complete=True)
            ],
            suggestions=[Suggestion(event="e", action="a")],
        )
        text = build_agent_text(r)
        assert "\n\n" in text

    def test_matched_rule_caption_injected_from_result(self):
        """build_agent_text 自动从 result.caption 注入 matched_rule 的 caption."""
        from miloco.perception.types import CaptionEntry

        r = RealtimePerceptionResult(
            caption=[CaptionEntry(
                description="有人在灶台前操作",
                source_device_ids=["cam1"],
            )],
            matched_rules=[MatchedRule(
                rule_id="r1", reason="检测到明火",
                source_device_ids=["cam1"],
            )],
        )
        text = build_agent_text(r)
        assert "画面描述：有人在灶台前操作" in text


class TestSuggestionIntraPriority:
    """urgency → 条目级调度优先级(dispatcher 约定:数字小=优先)。仅供淘汰,不改渲染序。"""

    def test_empty_is_default_zero(self):
        assert suggestion_intra_priority([]) == 0

    def test_low_or_missing_is_zero(self):
        assert suggestion_intra_priority([Suggestion(event="e", action="a")]) == 0
        assert suggestion_intra_priority(
            [Suggestion(event="e", action="a", urgency="low")]
        ) == 0

    def test_medium_and_high_more_negative(self):
        assert suggestion_intra_priority(
            [Suggestion(event="e", action="a", urgency="medium")]
        ) == -1
        assert suggestion_intra_priority(
            [Suggestion(event="e", action="a", urgency="high")]
        ) == -2

    def test_batch_takes_most_urgent(self):
        """一批取最高 urgency 的负 rank — 含 high 即按 high 保护整批。"""
        suggs = [
            Suggestion(event="e1", action="a1", urgency="low"),
            Suggestion(event="e2", action="a2", urgency="high"),
            Suggestion(event="e3", action="a3", urgency="medium"),
        ]
        assert suggestion_intra_priority(suggs) == -2

    def test_unknown_urgency_treated_as_zero(self):
        """未知 urgency 退化为 0(不冒充紧急)。"""
        assert suggestion_intra_priority(
            [Suggestion(event="e", action="a", urgency="???")]
        ) == 0


class TestBuildRuleCallbacksText:
    """runner.py build_rule_callbacks_text 的输出格式：header + 多段 key:value
    元信息 + prompt_text 整块；多条用 \\n\\n═══\\n\\n 分隔。"""

    def test_single_callback_format(self):
        from miloco.rule.runner import build_rule_callbacks_text
        from miloco.rule.schema import RuleEvent, RuleTriggerCallback

        cb = RuleTriggerCallback(
            rule_id="r1", rule_name="坐姿监测", event=RuleEvent.ENTERED,
            triggered_at="2026-06-08T15:30:45+08:00",
            source=["cam1"], room_name="客厅", prompt_text="提醒站起来",
            trigger_reason="用户久坐超过1小时",
            rule_query="用户是否久坐超过1小时",
        )
        text = build_rule_callbacks_text([cb])
        assert text.startswith("[感知引擎]规则提醒：\n")
        assert "时间：15:30:45" in text
        assert "来源：客厅(did=cam1)" in text
        assert "触发条件：用户是否久坐超过1小时" in text
        assert "触发原因：用户久坐超过1小时" in text
        assert "提醒站起来" in text

    def test_callback_fallback_to_rule_name_when_no_query(self):
        """rule_query 为空时 fallback 用 rule_name."""
        from miloco.rule.runner import build_rule_callbacks_text
        from miloco.rule.schema import RuleEvent, RuleTriggerCallback

        cb = RuleTriggerCallback(
            rule_id="r1", rule_name="坐姿监测", event=RuleEvent.ENTERED,
            triggered_at="2026-06-08T10:00:00+08:00",
            source=[], room_name="", prompt_text="提醒",
        )
        text = build_rule_callbacks_text([cb])
        assert "触发条件：坐姿监测" in text

    def test_prompt_text_preserved_as_is(self):
        """prompt_text 整块原样保留（不再 strip 尾句号 / 不加「建议：」前缀）."""
        from miloco.rule.runner import build_rule_callbacks_text
        from miloco.rule.schema import RuleEvent, RuleTriggerCallback

        cb = RuleTriggerCallback(
            rule_id="r1", rule_name="r", event=RuleEvent.ENTERED,
            triggered_at="2026-06-08T10:00:00+08:00",
            source=[], room_name="", prompt_text="请检查。",
        )
        text = build_rule_callbacks_text([cb])
        assert "请检查。" in text
        assert "建议：" not in text

    def test_callback_with_caption_and_device(self):
        from miloco.rule.runner import build_rule_callbacks_text
        from miloco.rule.schema import RuleEvent, RuleTriggerCallback

        cb = RuleTriggerCallback(
            rule_id="r1", rule_name="厨房安全", event=RuleEvent.ENTERED,
            triggered_at="2026-06-08T10:00:00+08:00",
            source=["cam2"], room_name="厨房", prompt_text="检查厨房",
            caption="有人在炒菜", device_name="摄像头2",
            trigger_reason="检测到明火",
            rule_query="厨房是否出现明火",
        )
        text = build_rule_callbacks_text([cb])
        assert "来源：厨房的摄像头2(did=cam2)" in text
        assert "画面描述：有人在炒菜" in text
        assert "触发条件：厨房是否出现明火" in text
        assert "触发原因：检测到明火" in text

    def test_callback_no_caption(self):
        from miloco.rule.runner import build_rule_callbacks_text
        from miloco.rule.schema import RuleEvent, RuleTriggerCallback

        cb = RuleTriggerCallback(
            rule_id="r1", rule_name="r", event=RuleEvent.ENTERED,
            triggered_at="2026-06-08T10:00:00+08:00",
            source=[], room_name="", prompt_text="x",
        )
        text = build_rule_callbacks_text([cb])
        assert "画面描述" not in text

    def test_multiple_callbacks_separated_by_rule(self):
        """多条 callback 用 \\n\\n═══\\n\\n 分隔."""
        from miloco.rule.runner import build_rule_callbacks_text
        from miloco.rule.schema import RuleEvent, RuleTriggerCallback

        cb1 = RuleTriggerCallback(
            rule_id="r1", rule_name="A", event=RuleEvent.ENTERED,
            triggered_at="2026-06-08T10:00:00+08:00",
            source=[], room_name="", prompt_text="**意图**：\nP1\n\n**额外信息**：\n{}",
        )
        cb2 = RuleTriggerCallback(
            rule_id="r2", rule_name="B", event=RuleEvent.ENTERED,
            triggered_at="2026-06-08T10:01:00+08:00",
            source=[], room_name="", prompt_text="**意图**：\nP2\n\n**额外信息**：\n{}",
        )
        text = build_rule_callbacks_text([cb1, cb2])
        assert "\n\n═══\n\n" in text
        assert "P1" in text and "P2" in text

    def test_empty_returns_none(self):
        from miloco.rule.runner import build_rule_callbacks_text

        assert build_rule_callbacks_text([]) is None
