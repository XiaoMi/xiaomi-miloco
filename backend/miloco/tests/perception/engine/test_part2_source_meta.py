"""Part2：来源 meta（房间/来源相机/时间窗）注入 + 送 Agent 的自然语言消息构造 单测。"""

from __future__ import annotations

import re

from miloco.perception.engine.pipeline import _fmt_time_window, _inject_source_meta
from miloco.perception.engine.types import OmniOutput
from miloco.perception.event_text_builder import (
    build_speeches_text,
    build_suggestions_text,
)
from miloco.perception.types import CaptionEntry, Speech, Suggestion


# ---------------- 注入 ----------------
def test_fmt_time_window_format():
    tw = _fmt_time_window(1780013545225, 1780013548225)
    assert re.fullmatch(r"\[\d\d:\d\d:\d\d-\d\d:\d\d:\d\d\]", tw)


def test_inject_meta_all_three_types():
    from miloco.perception.types import MatchedRule

    out = OmniOutput(
        speeches=[Speech(needs_response=True, speaker="u", content="x", status="complete")],
        caption=[CaptionEntry(changed=True, area="办公室", description="d")],
        suggestions=[Suggestion(event="e", action="a", urgency="low")],
        matched_rules=[MatchedRule(rule_id="r1", reason="test")],
    )
    _inject_source_meta(out, "客厅", ["cam1"], "小米C700", "[20:42:25-20:42:28]")
    for it in (out.speeches[0], out.caption[0], out.suggestions[0]):
        assert it.room_name == "客厅"
        assert it.source_device_ids == ["cam1"]
        assert it.device_name == "小米C700"
        assert it.time_window == "[20:42:25-20:42:28]"
    mr = out.matched_rules[0]
    assert mr.room_name == "客厅"
    assert mr.source_device_ids == ["cam1"]
    assert mr.device_name == "小米C700"
    assert mr.time_window == "[20:42:25-20:42:28]"


def test_inject_overrides_room_name_for_suggestion():
    out = OmniOutput(suggestions=[Suggestion(event="e", action="a", room_name="厨房")])
    _inject_source_meta(out, "客厅", ["cam1"], "小米C700", "[a-b]")
    sg = out.suggestions[0]
    assert sg.room_name == "客厅"
    assert sg.device_name == "小米C700"
    assert sg.source_device_ids == ["cam1"]


# ---------------- 消息构造（key:value 多段竖排） ----------------
def test_suggestion_msg_natural_language():
    s = Suggestion(
        event="老人摔倒", action="查看", urgency="high",
        room_name="客厅", source_device_ids=["1178866901"],
        device_name="小米智能摄像机C700", time_window="[20:42:25-20:42:28]",
    )
    text = build_suggestions_text([s])
    assert "时间：20:42:25" in text
    assert "来源：客厅的小米智能摄像机C700(did=1178866901)" in text
    assert "检测到：老人摔倒" in text
    assert "事件优先级：high" in text
    assert "建议：查看" in text


def test_speech_msg_natural_language():
    e = Speech(
        needs_response=True, speaker="彭于晏", content="关灯", status="complete",
        room_name="卧室", source_device_ids=["did1"], device_name="小米4C", time_window="[a-b]",
    )
    text = build_speeches_text([e])
    assert "来源：卧室的小米4C(did=did1)" in text
    assert "说话人：彭于晏" in text
    assert "语音指令：关灯" in text
    assert "时间：a" in text


def test_suggestion_no_meta_minimal():
    """全空 meta → 只有核心内容,无"来源"。"""
    text = build_suggestions_text([Suggestion(event="e", action="a")])
    assert "来源" not in text
    assert "检测到：e" in text
    assert "建议：a" in text
