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

    def test_hit_missing_defaults_to_matched(self):
        """hit 缺省（旧 prompt 无此字段）视作命中，向后兼容。"""
        data = {"matched_rules": [{"rule_name": "[read] 阅读", "reason": "正在看书"}]}
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert len(result.matched_rules) == 1

    def test_hit_string_false_dropped(self):
        """模型输出字符串 "false" 也被正确拦截。"""
        data = {"matched_rules": [{"rule_name": "[x] test", "reason": "no", "hit": "false"}]}
        result = parse_omni_response(_wrap(json.dumps(data)))
        assert result.matched_rules == []

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
