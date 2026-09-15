"""Tests for Omni Layer — Orchestrator."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    GateTrigger,
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


# =============================================================================
# 图像推理模式:audio route 窗口整窗跳过
#
# 判据在 prompt_builder.should_skip_for_input_mode(那里有独立单测);此处只钉
# omni.py 各调用点的**副作用契约** —— 尤其 fused 那条:候选已被 take_fused_pending
# 取走(inflight 置 True),跳过时必须按既有失败语义回填,否则那些 track 永久挂住。
# =============================================================================


def _pending_with(n_candidates: int):
    """identity_engine.take_fused_pending() 的返回值替身。"""
    pending = SimpleNamespace(
        candidates=[
            SimpleNamespace(track_id=i, bbox_xyxy_norm=(0, 0, 10, 10))
            for i in range(n_candidates)
        ],
        gallery_snapshot={},
    )
    return pending


def _fake_identity_engine(n_candidates: int):
    eng = MagicMock()
    eng.take_fused_pending.return_value = _pending_with(n_candidates)
    eng.deliver_fused_failure = AsyncMock()
    return eng


def _audio_only_edge_packet() -> IdentityPacket:
    """零帧 + 音频过闸 = audio route(见 prompt_builder._resolve_route)。"""
    ep = _mock_edge_packet()
    ep.all_frames = []
    ep.frames = []
    ep.audio_analysis = AudioAnalysis(
        type=AudioType.SPEECH, is_urgent=True, energy_level=0.9
    )
    ep.trigger = GateTrigger(
        visual_changed=False,
        visual_change_score=0.05,
        audio_active=True,
        audio_energy_level=0.6,
    )
    return ep


@pytest.mark.asyncio
async def test_run_omni_skips_without_calling_model():
    """跳过 = 一次调用都不发(不是发一次注定空转的请求)。"""
    ep = _audio_only_edge_packet()
    with patch(
        "miloco.perception.engine.omni.omni.should_skip_for_input_mode",
        return_value=True,
    ), patch(
        "miloco.perception.engine.omni.omni.call_omni",
        new_callable=AsyncMock,
    ) as call:
        output = await run_omni(ep, OmniContext(), OmniConfig(api_key="k"))
    assert output.skipped is True
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_omni_fused_releases_candidates_on_skip():
    """fused 跳过时必须回填候选 —— 候选已 take 走(inflight=True),不回填就永久挂住:
    _gc_dead_tracks 跳过 inflight、needs_omni_call 也不再派发它们。
    """
    from miloco.perception.engine.omni.omni import run_omni_fused

    eng = _fake_identity_engine(n_candidates=2)
    with patch(
        "miloco.perception.engine.omni.omni.should_skip_for_input_mode",
        return_value=True,
    ), patch(
        "miloco.perception.engine.omni.omni._call_omni_messages",
        new_callable=AsyncMock,
    ) as call:
        output = await run_omni_fused(
            [_audio_only_edge_packet()], OmniContext(), OmniConfig(api_key="k"), eng
        )
    assert output.skipped is True
    eng.deliver_fused_failure.assert_awaited_once()
    call.assert_not_awaited()  # 跳过即不发请求


@pytest.mark.asyncio
async def test_run_omni_fused_skip_without_candidates_skips_backfill():
    """无候选 → 没有挂住的 track 要放,不必多调一次回填。"""
    from miloco.perception.engine.omni.omni import run_omni_fused

    eng = _fake_identity_engine(n_candidates=0)
    with patch(
        "miloco.perception.engine.omni.omni.should_skip_for_input_mode",
        return_value=True,
    ), patch(
        "miloco.perception.engine.omni.omni._call_omni_messages",
        new_callable=AsyncMock,
    ):
        output = await run_omni_fused(
            [_audio_only_edge_packet()], OmniContext(), OmniConfig(api_key="k"), eng
        )
    assert output.skipped is True
    eng.deliver_fused_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_omni_stream_skip_fires_no_early_callbacks():
    """流式跳过时 early callbacks 一个都不触发 —— 该窗口没有任何可报内容。"""
    from miloco.perception.engine.omni.omni import run_omni_stream

    on_speeches = AsyncMock()
    with patch(
        "miloco.perception.engine.omni.omni.should_skip_for_input_mode",
        return_value=True,
    ), patch(
        "miloco.perception.engine.omni.omni._stream_and_parse", new_callable=AsyncMock
    ) as stream:
        output = await run_omni_stream(
            _audio_only_edge_packet(), OmniContext(), OmniConfig(api_key="k"),
            on_early_speeches=on_speeches,
        )
    assert output.skipped is True
    stream.assert_not_awaited()
    on_speeches.assert_not_awaited()
