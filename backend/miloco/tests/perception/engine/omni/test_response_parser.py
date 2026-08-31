"""Tests for Omni Layer — Response Parser (new format)."""

import json

from miloco.perception.engine.omni.response_parser import (
    extract_json,
    parse_omni_response,
    parse_tier_c_verify_response,
)


def _wrap(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class TestExtractJson:
    def test_markdown_code_block(self):
        assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_raw_json(self):
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_plain_text(self):
        assert extract_json("hello") == "hello"

    def test_strips_think_tags(self):
        content = '<think>reasoning here</think>\n{"a": 1}'
        assert extract_json(content) == '{"a": 1}'

    # ── 安全提取增强（真实模型输出变体）─────────────────────────────────

    def test_matched_rules_fenced_example(self):
        # 用户报告的真实形态：```json 围栏包裹 matched_rules
        content = (
            '```json\n{"matched_rules":[{"rule_name":"燕麦片",'
            '"reason":"画面中可见散落的黑色燕麦片","hit":true},'
            '{"rule_name":"有人读书","reason":"床边无人坐着看书","hit":false}]}\n```'
        )
        parsed = json.loads(extract_json(content))
        assert len(parsed["matched_rules"]) == 2
        assert parsed["matched_rules"][0]["rule_name"] == "燕麦片"

    def test_fence_label_variants(self):
        # 大写标签 / 标签后空格 / 四反引号 / 无标签
        for content in (
            '```JSON\n{"a": 1}\n```',
            '``` json\n{"a": 1}\n```',
            '````json\n{"a": 1}\n````',
            '```\n{"a": 1}\n```',
            '```json{"a": 1}```',  # 无换行
        ):
            assert extract_json(content) == '{"a": 1}', content

    def test_trailing_comma_recovery(self):
        # 尾随逗号：模型高频语法错误，应修复后可解析
        assert json.loads(extract_json('{"a": 1,}')) == {"a": 1}
        assert json.loads(extract_json('{"a": [1, 2,],}')) == {"a": [1, 2]}
        assert json.loads(extract_json('```json\n{"a": [1,],}\n```')) == {"a": [1]}

    def test_bom_crlf_control_chars(self):
        # BOM / CRLF / 非法控制字符归一
        assert extract_json('\ufeff\r\n```json\r\n{"a": 1}\r\n```\r\n') == '{"a": 1}'
        assert extract_json('{"a": 1}\x00\x1b 尾部说明') == '{"a": 1}'

    def test_prefix_suffix_text(self):
        assert extract_json('好的，结果如下：{"a": 1} 完毕') == '{"a": 1}'

    def test_multiple_fences_picks_valid(self):
        content = '```text\n说明文字\n```\n```json\n{"b": 2}\n```'
        assert extract_json(content) == '{"b": 2}'

    def test_think_block_plus_fence(self):
        content = '<thinking>分析中</thinking>\n```json\n{"a": 1}\n```'
        assert extract_json(content) == '{"a": 1}'

    def test_trailing_fence_with_code(self):
        # 围栏后还有别的代码块（如 ```text），取 json 块
        content = '```json\n{"a": 1}\n```\n```text\n备注\n```'
        assert extract_json(content) == '{"a": 1}'

    def test_empty_content_raises(self):
        import pytest

        with pytest.raises(json.JSONDecodeError):
            extract_json("")


class TestParseOmniResponse:
    def test_complete_response(self):
        data = {
            "caption": "一个人坐在沙发上看电视",
            "matched_rules": [{"rule_name": "[read] 检测到读书", "reason": "检测到读书行为", "hit": True}],
            "speeches": [
                {
                    "needs_response": True,
                    "speaker": "爸爸",
                    "content": "把灯打开",
                    "is_complete": True,
                }
            ],
            "env_sounds": "键盘敲击声",
            "suggestions": [{"prev_id": None, "event": "环境正常", "action": "无需操作", "urgency": "low"}],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert len(result.caption) == 1
        assert result.caption[0].description == "一个人坐在沙发上看电视"
        assert len(result.matched_rules) == 1
        assert result.matched_rules[0].rule_name == "[read] 检测到读书"
        assert result.matched_rules[0].rule_id == "[read] 检测到读书"  # 无 mapping → best-effort 用 name
        assert len(result.speeches) == 1
        assert result.speeches[0].needs_response is True
        assert result.speeches[0].is_complete is True
        assert result.speeches[0].content == "把灯打开"
        assert result.env_sounds == ["键盘敲击声"]
        assert len(result.suggestions) == 1

    def test_empty_arrays(self):
        data = {
            "caption": [],
            "matched_rules": [],
            "speeches": [],
            "suggestions": [],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert result.caption == []
        assert result.matched_rules == []

    def test_hit_false_dropped(self):
        """hit=false 的规则被丢弃，hit=true 的保留。"""
        data = {
            "matched_rules": [
                {"rule_name": "[drink] 喝水提醒", "reason": "画面未出现目标人", "hit": False},
                {"rule_name": "[posture] 颈椎提醒", "reason": "检测到低头超过30分钟", "hit": True},
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert len(result.matched_rules) == 1
        assert result.matched_rules[0].rule_id == "[posture] 颈椎提醒"

    def test_hit_missing_with_negation_reason_dropped(self):
        """hit 缺失 + reason 明确否定 → 丢弃（11:51:25 误触发案例的读书条目）。

        回归：模型漏写 hit 字段、把否定结论写进 reason（「画面中只有床铺和家具，
        并没有人在床上读书」）——旧代码缺省命中导致误触发进入阅读场景。
        """
        data = {
            "matched_rules": [
                {"rule_name": "[read] 有人读书", "reason": "画面中只有床铺和家具，并没有人在床上读书。"},
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert result.matched_rules == []

    def test_hit_missing_with_normal_reason_kept(self):
        """hit 缺失但 reason 正常 → 保留（同窗口的垃圾条目不能误杀）。

        hit 字段模型时有时无（真实数据 ~4% 缺失）；缺失时由 reason 兜底——
        reason 无否定即视为命中，兼容漏写 hit 的合法触发。
        """
        data = {
            "matched_rules": [
                {"rule_name": "[vacuum] 白色地面有黑色垃圾", "reason": "画面右下角白色地板上可见数个分散的小黑点垃圾，不属于线缆、鞋影或机器人。"},
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert [r.rule_id for r in result.matched_rules] == ["[vacuum] 白色地面有黑色垃圾"]

    def test_hit_string_false_dropped(self):
        """模型输出字符串 "false" 也被正确拦截。"""
        data = {"matched_rules": [{"rule_name": "[x] test", "reason": "no", "hit": "false"}]}
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert result.matched_rules == []

    def test_reason_negation_with_hit_true_dropped(self):
        """hit=true 但 reason 明确否定（自相矛盾）→ 按 reason 丢弃。

        回归 2026-08-31 11:51:27 误触发：同窗口两条命中，reason 原文——
        「画面中只有床铺和家具，并没有人在床上读书」的读书条目被丢弃，
        垃圾条目（reason 正常）保留。
        """
        data = {
            "matched_rules": [
                {
                    "rule_name": "[read] 有人读书",
                    "reason": "画面中只有床铺和家具，并没有人在床上读书。",
                    "hit": True,
                },
                {
                    "rule_name": "[vacuum] 白色地面有黑色垃圾",
                    "reason": "画面右下角白色地板上可见数个分散的小黑点垃圾，不属于线缆、鞋影或机器人。",
                    "hit": True,
                },
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert [r.rule_id for r in result.matched_rules] == ["[vacuum] 白色地面有黑色垃圾"]

    def test_reason_negation_laptop_dropped(self):
        """hit=true 但 reason 写「并非在看纸质书」（用电脑被当读书）→ 丢弃。

        回归 2026-08-29 rule_log：reason「...双手放在键盘区域，视线朝向电脑
        屏幕，并非在看纸质书」仍触发过 ENTERED。
        """
        data = {
            "matched_rules": [
                {
                    "rule_name": "[read] 有人读书",
                    "reason": "画面中有人躺在床上，面前放置着一台笔记本电脑，双手放在键盘区域，视线朝向电脑屏幕，并非在看纸质书",
                    "hit": True,
                },
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert result.matched_rules == []

    def test_reason_old_format_hit_false_text_dropped(self):
        """旧格式残留：把 hit=false 写进 reason 文本（无结构化 hit）→ 丢弃。"""
        data = {
            "matched_rules": [
                {"rule_name": "[vacuum] 白色地面有黑色垃圾", "reason": "画面中白色地面上未见散落的黑色垃圾，地毯外地面干净，hit=false"},
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert result.matched_rules == []

    def test_reason_exclusion_phrase_not_misjudged(self):
        """「不属于线缆/鞋影/机器人」这类排除干扰物的表述不是否定结论，必须保留。"""
        data = {
            "matched_rules": [
                {
                    "rule_name": "[vacuum] 白色地面有黑色垃圾",
                    "reason": "画面右下角白色地板上可见数个分散的小黑点垃圾，不属于线缆、鞋影或机器人。",
                    "hit": True,
                },
                {
                    "rule_name": "[read] 有人读书",
                    "reason": "画面中有人坐在床沿，双手拿着纸质书低头阅读，连续多帧保持该姿势。",
                    "hit": True,
                },
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert [r.rule_id for r in result.matched_rules] == [
            "[vacuum] 白色地面有黑色垃圾",
            "[read] 有人读书",
        ]

    def test_reason_negation_x_but_y_not_misjudged(self):
        """「不是X而是Y」句式不误杀：否定词后紧跟的 X 不在关键词内即放行。"""
        data = {
            "matched_rules": [
                {
                    "rule_name": "[read] 有人读书",
                    "reason": "画面中不是空床，而是有人坐在床上拿着纸质书在看。",
                    "hit": True,
                },
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert [r.rule_id for r in result.matched_rules] == ["[read] 有人读书"]

    def test_partial_fields(self):
        data = {"caption": "安静"}
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert len(result.caption) == 1
        assert result.caption[0].description == "安静"
        assert result.matched_rules == []
        assert result.speeches == []
        assert result.suggestions == []

    def test_malformed_json(self):
        result = parse_omni_response(_wrap("not json at all"))
        assert len(result.caption) == 1
        assert "解析失败" in result.caption[0].description

    def test_empty_choices(self):
        result = parse_omni_response({"choices": []})
        assert "解析失败" in result.caption[0].description

    def test_needs_response_flag(self):
        data = {
            "caption": [],
            "speeches": [
                {"needs_response": True, "speaker": "用户", "content": "开灯", "is_complete": True},
                {"needs_response": False, "speaker": "妈妈", "content": "今天天气不错", "is_complete": True},
                {"needs_response": False, "speaker": "", "content": "门铃响", "is_complete": True},
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert len(result.speeches) == 3
        assert result.speeches[0].needs_response is True
        assert result.speeches[1].needs_response is False
        assert result.speeches[2].needs_response is False

    def test_rule_name_resolved_to_uuid(self):
        """非空 mapping 命中 → rule_name 还原为 rule_id(UUID)。"""
        data = {"matched_rules": [{"rule_name": "[read] 阅读", "reason": "正在看书", "hit": True}]}
        mapping = {"[read] 阅读": "uuid-1234"}
        result = parse_omni_response(_wrap(json.dumps(data)), mapping)
        assert len(result.matched_rules) == 1
        assert result.matched_rules[0].rule_id == "uuid-1234"
        assert result.matched_rules[0].rule_name == "[read] 阅读"

    def test_hallucinated_rule_dropped_empty_mapping(self, caplog):
        """空 dict mapping（本轮零规则）+ 模型输出某 rule_name → 判定幻觉、丢弃、记 error。"""
        import logging

        data = {"matched_rules": [{"rule_name": "[smoke_alarm] 烟雾报警器响", "reason": "听到警报", "hit": True}]}
        with caplog.at_level(logging.ERROR):
            result = parse_omni_response(_wrap(json.dumps(data)), {})
        assert result.matched_rules == []
        assert any("幻觉" in r.message for r in caplog.records)

    def test_hallucinated_rule_dropped_unknown_name(self):
        """非空 mapping 但模型输出列表外的 rule_name → 丢弃。"""
        data = {"matched_rules": [{"rule_name": "[ghost] 不存在的规则", "reason": "x", "hit": True}]}
        result = parse_omni_response(_wrap(json.dumps(data)), {"[read] 阅读": "uuid-1234"})
        assert result.matched_rules == []

    def test_none_mapping_keeps_name_best_effort(self):
        """mapping=None（未提供，测试 / benchmark 路径）→ best-effort 保留 name。"""
        data = {"matched_rules": [{"rule_name": "[read] 阅读", "reason": "看书", "hit": True}]}
        result = parse_omni_response(_wrap(json.dumps(data)), None)
        assert len(result.matched_rules) == 1
        assert result.matched_rules[0].rule_id == "[read] 阅读"

    def test_suggestion_events(self):
        data = {
            "caption": [],
            "suggestions": [
                {"event": "触电风险", "action": "提醒"},
                {"event": "开始健身", "action": "建议休息"},
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert len(result.suggestions) == 2
        assert result.suggestions[0].event == "触电风险"
        assert result.suggestions[1].event == "开始健身"

    def test_ignore_urgency_dropped(self):
        """urgency=ignore 的建议被剔除，不冒泡上报；未知 urgency 仍 coerce 成 low。"""
        data = {
            "caption": [],
            "suggestions": [
                {"event": "看手机", "action": "无", "urgency": "ignore"},
                {"event": "触电风险", "action": "提醒", "urgency": "high"},
                {"event": "整理桌面", "action": "无", "urgency": "bogus"},
            ],
        }
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert len(result.suggestions) == 2
        assert result.suggestions[0].event == "触电风险"
        assert result.suggestions[0].urgency == "high"
        assert result.suggestions[1].event == "整理桌面"
        assert result.suggestions[1].urgency == "low"

    def test_think_tags_stripped(self):
        content = "<think>let me think...</think>\n" + json.dumps(
            {
                "caption": "正常",
            }
        )
        result = parse_omni_response(_wrap(content))
        assert len(result.caption) == 1
        assert result.caption[0].description == "正常"


class TestParseTierCVerifyResponse:
    """parse_tier_c_verify_response: 同人校验 1v1 响应解析, 失败/缺字段一律保守降级 same_person=False。"""

    def test_valid_response(self):
        out = parse_tier_c_verify_response(
            _wrap('{"same_person": true, "confidence": 0.9, "reason": "脸型一致"}')
        )
        assert out == {"same_person": True, "confidence": 0.9, "reason": "脸型一致"}

    def test_malformed_json_falls_back(self):
        out = parse_tier_c_verify_response(_wrap("not json at all"))
        assert out["same_person"] is False
        assert out["confidence"] == 0.0

    def test_missing_same_person_defaults_false(self):
        out = parse_tier_c_verify_response(_wrap('{"confidence": 0.7}'))
        assert out["same_person"] is False
        assert out["confidence"] == 0.7

    def test_empty_choices_falls_back(self):
        out = parse_tier_c_verify_response({"choices": []})
        assert out["same_person"] is False

    def test_confidence_clamped_to_unit(self):
        out = parse_tier_c_verify_response(_wrap('{"same_person": true, "confidence": 5}'))
        assert out["confidence"] == 1.0

    def test_non_numeric_confidence_defaults_zero(self):
        out = parse_tier_c_verify_response(_wrap('{"same_person": true, "confidence": "high"}'))
        assert out["confidence"] == 0.0
