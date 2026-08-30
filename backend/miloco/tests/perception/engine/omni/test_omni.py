"""Tests for Omni Layer — Orchestrator."""

import json
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni.omni import (
    _has_loopback_tail,
    _stream_and_parse,
    run_omni,
)
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    FrameInfo,
    FrameResolution,
    IdentityPacket,
    IdentityTarget,
    MotionState,
    ObjectType,
    OmniContext,
    RuleCondition,
    SelectedFrame,
    TrackingBoxInfo,
)

MOCK_RESPONSE = {
    "id": "mock",
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {
                        "caption": "wangshihao 坐在桌前看书，环境安静",
                        "matched_rules": [
                            {
                                "rule_name": "读书开灯",
                                "reason": "看书",
                                "hit": True,
                            }
                        ],
                        "speeches": [],
                        "suggestions": [],
                    }
                ),
            },
        }
    ],
}


def _mock_edge_packet() -> IdentityPacket:
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    return IdentityPacket(
        packet_id="ep-1",
        room_name="study-room",
        timestamp=1000.0,
        frame_info=FrameInfo(start_timestamp=0, end_timestamp=3000, fps=2),
        targets=[
            IdentityTarget(
                type=ObjectType.HUMAN_WITH_FACE,
                person_id="wangshihao",
                track_id=1,
                needs_omni_verify=False,
                box_info=[TrackingBoxInfo(frame_index=0, boxes={"human_body": (10, 10, 50, 80)})],
            )
        ],
        scene_motion=MotionState.STATIC,
        frames=[SelectedFrame(frame_index=0, image=frame, resolution=FrameResolution.HIGH, crops=[])],
        all_frames=[np.zeros((100, 100, 3), dtype=np.uint8)],
        audio_clip=np.array([], dtype=np.int16),
        audio_analysis=AudioAnalysis(type=AudioType.SILENCE, is_urgent=False, energy_level=0.0),
    )


@pytest.mark.asyncio
async def test_run_omni_with_mock():
    ep = _mock_edge_packet()
    ctx = OmniContext(
        rule_conditions=[RuleCondition(rule_id="reading_light", rule_name="读书开灯", query="是否在读书")],
    )
    config = OmniConfig(api_key="test-key")

    with patch("miloco.perception.engine.omni.omni.call_omni", new_callable=AsyncMock, return_value=MOCK_RESPONSE):
        output = await run_omni(ep, ctx, config)

    assert len(output.caption) == 1
    assert "看书" in output.caption[0].description
    assert len(output.matched_rules) == 1
    assert output.matched_rules[0].rule_id == "reading_light"
    assert output.speeches == []


# =============================================================================
# 端侧 ngram 流式复读检测
# =============================================================================


class TestLoopbackTailDetection:
    """_has_loopback_tail 纯函数测试。"""

    def test_short_buffer_not_detected(self):
        """长度 < 20 字符直接返回 False，避免初始 chunk 的误触发。"""
        assert _has_loopback_tail("") is False
        assert _has_loopback_tail("短文本") is False

    def test_normal_json_not_detected(self):
        """正常 JSON 框架（含缩进 / 字段名）不触发。"""
        normal = (
            '{\n  "caption": [{"area": "书房", "description": "用户在看书"}],\n'
            '  "speeches": [], "matched_rules": [], "suggestions": []\n}'
        )
        assert _has_loopback_tail(normal) is False

    def test_json_indentation_not_detected(self):
        """关键防护：连续 12 个空格的 JSON 缩进不能触发（\\S 排除空白 ngram）。"""
        indented = '{\n' + ' ' * 30 + '"needs_response": false'
        assert _has_loopback_tail(indented) is False

    def test_unigram_repeat_detected(self):
        """单字符复读 ≥ 10 次（"这这这..."）命中。"""
        buf = '"content": "这这这这这这这这这这这这'  # 12 个"这"
        assert _has_loopback_tail(buf) is True

    def test_bigram_with_separator_detected(self):
        """带分隔符的 bigram 复读（"那个，那个，那个..."）命中。"""
        buf = '"content": "哎，' + '那个，' * 12
        assert _has_loopback_tail(buf) is True

    def test_quad_gram_cycle_detected(self):
        """4-gram 循环（"嗯，对，嗯，对..."）命中。"""
        buf = '"content": "好，行，' + '嗯，对，' * 11
        assert _has_loopback_tail(buf) is True

    def test_repeat_below_threshold_not_detected(self):
        """重复 < 10 次不触发，避免误伤"哈哈哈"等中文叠词。"""
        assert _has_loopback_tail('"content": "哈哈哈"' + ' ' * 10) is False
        # 9 次重复刚好不命中
        buf = '"content": "好的' + '那个' * 9
        assert _has_loopback_tail(buf) is False

    def test_real_log_sample_loop_complete(self):
        """真实日志样本回归（5-21 19:05:08 LOOP_complete）。"""
        # mimo 实际生成顺序：speeches 字段开始流出后，content 内复读
        buf = (
            '{\n  "speeches": [\n    {\n      "needs_response": false,\n'
            '      "speaker": "未知",\n      "content": "好，行，嗯，对，'
            + '嗯，对，' * 10
        )
        assert _has_loopback_tail(buf) is True


class TestStreamLoopbackAbort:
    """_stream_and_parse 集成测试：mock streaming，验证 ngram 命中后早期 abort。"""

    @pytest.mark.asyncio
    async def test_normal_stream_completes_fully(self):
        """正常 stream（无复读）应完整接收所有 delta。"""
        full_response = (
            '{\n  "speeches": [],\n  "matched_rules": [],\n'
            '  "suggestions": [],\n  "caption": "用户在读书，环境安静"\n}'
        )

        async def mock_stream(payload, config, usage_out=None):
            # 模拟逐字符吐 delta
            for ch in full_response:
                yield ch

        config = OmniConfig(api_key="test-key")
        with patch("miloco.perception.engine.omni.omni.call_omni_stream", mock_stream):
            output = await _stream_and_parse({}, config, None, None, None)

        # 正常完成，caption 被正常解析
        assert len(output.caption) == 1
        assert "读书" in output.caption[0].description

    @pytest.mark.asyncio
    async def test_loopback_stream_aborts_early(self):
        """含复读的 stream 应在 ngram 命中处 break，buffer 不再接收。"""
        # 前 80 字符正常，后 200 字符复读"那个，"
        prefix = (
            '{\n  "speeches": [\n    {\n      "needs_response": false,\n'
            '      "speaker": "未知",\n      "content": "哎，'
        )
        loop_part = '那个，' * 50  # 模拟模型复读
        tail = '"\n    }\n  ]\n}'
        full = prefix + loop_part + tail

        received_chars = []

        async def mock_stream(payload, config, usage_out=None):
            for ch in full:
                received_chars.append(ch)
                yield ch

        config = OmniConfig(api_key="test-key")
        with patch("miloco.perception.engine.omni.omni.call_omni_stream", mock_stream):
            output = await _stream_and_parse({}, config, None, None, None)

        # ngram 命中应该早 break，接收的字符数远小于完整长度
        assert len(received_chars) < len(full)
        # JSON 不完整 → 走 fallback，skipped=True
        assert output.skipped is True
        # break 应该发生在复读累计到 10 次 ngram 附近（约 prefix + 30~50 字符）
        assert len(received_chars) < len(prefix) + len(loop_part)


class TestLogRealtimeRaw:
    """🔥 realtime_perceive response 原始输出打印（choices[0] 安全提取）。"""

    def test_prints_choices0_content(self, caplog):
        import logging

        from miloco.perception.engine.omni import omni as omni_mod

        raw = {"choices": [{"message": {"content": '{"a": 1}'}}]}
        with caplog.at_level(logging.INFO, logger="miloco.perception.engine.omni.omni"):
            omni_mod._log_realtime_raw(raw)
        assert any(
            "🔥 realtime_perceive response: {\"a\": 1}" in r.message
            for r in caplog.records
        )

    def test_whitespace_normalized_to_single_line(self, caplog):
        import logging

        from miloco.perception.engine.omni import omni as omni_mod

        raw = {"choices": [{"message": {"content": '{\n  "a": 1,\n  "b": 2\n}'}}]}
        with caplog.at_level(logging.INFO, logger="miloco.perception.engine.omni.omni"):
            omni_mod._log_realtime_raw(raw)
        assert any(
            "🔥 realtime_perceive response: { \"a\": 1, \"b\": 2 }" in r.message
            for r in caplog.records
        )

    def test_empty_choices_silent(self, caplog):
        import logging

        from miloco.perception.engine.omni import omni as omni_mod

        with caplog.at_level(logging.INFO, logger="miloco.perception.engine.omni.omni"):
            omni_mod._log_realtime_raw({"choices": []})
            omni_mod._log_realtime_raw({"choices": [{"message": {"content": None}}]})
            omni_mod._log_realtime_raw("not a dict")
            omni_mod._log_realtime_raw(None)
            omni_mod._log_realtime_raw({})
        assert not any(
            "🔥 realtime_perceive response" in r.message for r in caplog.records
        )
