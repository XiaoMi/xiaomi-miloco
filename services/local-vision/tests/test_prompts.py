"""提示词解析的单测。

样本全部取自 Mage-VL 在真实家庭片段上的**实际输出**(见 README「实测」一节),
不是臆造的格式 —— 模型并不总按要求的格式回答,解析器必须容忍它自发的几种写法。
"""

from __future__ import annotations

import pytest

from local_vision.prompts import build_prompt, parse_response

RULES = [
    {"name": "沙发有人", "query": "有人在客厅沙发上"},
    {"name": "有宠物", "query": "画面里有宠物"},
    {"name": "用电脑", "query": "有人正在使用电脑或手机"},
]


def _hits(raw):
    return [h["hit"] for h in parse_response(raw, RULES)[1]]


def test_prompt_includes_each_rule_condition():
    p = build_prompt("描述画面", RULES)
    assert "规则1: 有人在客厅沙发上" in p
    assert "规则3: 有人正在使用电脑或手机" in p
    # 必须显式要求描述独立于规则,否则模型会把描述写成判定复述(实测过)
    assert "不要复述" in p


def test_prompt_without_rules_is_just_the_ask():
    assert build_prompt("描述画面", []) == "描述画面"


def test_variant_compact():
    """实测变体 A:编号 + 紧凑判定,无依据。"""
    raw = "1. 客厅里有人在用笔记本。\n\n2. 规则1: 是\n规则2: 否\n规则3: 是"
    cap, hits = parse_response(raw, RULES)
    assert cap == "客厅里有人在用笔记本。"
    assert [h["hit"] for h in hits] == [True, False, True]


def test_variant_reason_on_next_line():
    """实测变体 B:依据另起一行。"""
    raw = (
        "描述: 客厅里没有人。\n\n"
        "规则1: 否\n依据: 客厅沙发上没有人。\n\n"
        "规则2: 否\n依据: 画面里没有宠物。\n\n"
        "规则3: 否\n依据: 没有人在用电脑。"
    )
    cap, hits = parse_response(raw, RULES)
    assert cap == "客厅里没有人。"  # 「描述:」是格式标记,不能留在正文里
    assert [h["hit"] for h in hits] == [False, False, False]


def test_variant_period_separator():
    """实测变体 C:中文句号分隔判定与依据。"""
    raw = "描述: 有人在弯腰整理东西。\n规则1: 否。画面中没有人在客厅沙发上。\n规则2: 否。没有宠物。\n规则3: 否。"
    cap, hits = parse_response(raw, RULES)
    assert cap == "有人在弯腰整理东西。"
    assert hits[0]["reason"] == "画面中没有人在客厅沙发上。"
    assert [h["hit"] for h in hits] == [False, False, False]


def test_unparseable_output_fails_closed():
    """模型完全不守格式时:描述保住,但一条规则都不许算命中。"""
    cap, hits = parse_response("客厅里空无一人，很安静。", RULES)
    assert cap == "客厅里空无一人，很安静。"
    assert [h["hit"] for h in hits] == [False, False, False]
    assert all(h["reason"] == "模型未给出判定" for h in hits)


@pytest.mark.parametrize(
    "body,expected",
    [
        ("是", True),
        ("成立", True),
        ("是 - 沙发上有人", True),
        ("否", False),
        ("不成立", False),          # 含「成立」二字,顺序错了就会误判
        ("没有 - 画面里没人", False),
        ("无法判断", False),         # 读不懂 → fail-closed
    ],
)
def test_verdict_words(body, expected):
    _, hits = parse_response(f"描述: x\n规则1: {body}", RULES[:1])
    assert hits[0]["hit"] is expected


def test_missing_rule_line_is_not_a_hit():
    """只回了一条判定时,其余规则必须落未命中,而不是继承上一条。"""
    _, hits = parse_response("描述: x\n规则1: 是 - 有人", RULES)
    assert [h["hit"] for h in hits] == [True, False, False]
