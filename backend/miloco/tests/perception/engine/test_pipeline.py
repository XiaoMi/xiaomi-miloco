"""Tests for Pipeline — End-to-end."""

import json
import time
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.config import PerceptionConfig
from miloco.perception.engine.gate.visual_gate import _preprocess
from miloco.perception.engine.identity.tracking_service import (
    MockTrackingService,
    create_default_mock_response,
)
from miloco.perception.engine.input.video_splitter import create_input_slice
from miloco.perception.engine.pipeline import (
    _downsample_for_omni,
    _inject_source_meta,
    _wrap_matched_rules_cb,
    _wrap_suggestions_cb,
    downsample_snapshot,
    run_batch_pipeline,
    run_pipeline,
    run_query_pipeline,
)
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    BatchPipelineResult,
    DevicePipelineResult,
    FrameInfo,
    FrameResolution,
    IdentityPacket,
    MotionState,
    OmniContext,
    RoomPipelineResult,
    RuleCondition,
    SelectedFrame,
)
from miloco.perception.types import (
    AudioFrame,
    AudioStream,
    BatchedSnapshot,
    CaptionEntry,
    DeviceSnapshot,
    MatchedRule,
    PerceptionDevice,
    Speech,
    Suggestion,
    VideoFrame,
    VideoStream,
)

MOCK_OMNI_RESPONSE = {
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


def _solid(r: int, g: int, b: int) -> np.ndarray:
    f = np.zeros((100, 100, 3), dtype=np.uint8)
    f[:, :] = [b, g, r]
    return f


def _make_snapshot(
    room_name: str,
    did: str,
    frames: list[np.ndarray],
    audio: np.ndarray | None = None,
) -> DeviceSnapshot:
    """Helper to create a DeviceSnapshot for testing."""
    device = PerceptionDevice(did=did, name=did, device_type="camera", room_name=room_name)
    now = 1000000.0
    video_frames = [VideoFrame(data=f, timestamp=now + i * 500) for i, f in enumerate(frames)]
    h, w = frames[0].shape[:2] if frames else (0, 0)
    video_stream = VideoStream(frames=video_frames, width=w, height=h) if video_frames else None
    audio_stream = None
    if audio is not None and len(audio) > 0:
        audio_stream = AudioStream(frames=[AudioFrame(data=audio, timestamp=now)])
    return DeviceSnapshot(
        device=device,
        start_timestamp=now,
        end_timestamp=now + 3000,
        video=video_stream,
        audio=audio_stream,
    )


# =============================================================================
# Single-device pipeline tests (existing)
# =============================================================================


@pytest.mark.asyncio
async def test_cold_start_passes_when_no_baseline():
    """run_pipeline 无状态、恒无 prev_frame → 静止窗也走 cold-start 放行建基准,不再 skip。"""
    frame = _solid(100, 100, 100)
    s = create_input_slice("room", [frame] * 6, np.zeros(16000, dtype=np.int16))
    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    ctx = OmniContext()
    tracking = MockTrackingService(create_default_mock_response())

    with patch(
        "miloco.perception.engine.omni.omni.call_omni",
        new_callable=AsyncMock,
        return_value=MOCK_OMNI_RESPONSE,
    ):
        result = await run_pipeline(s, ctx, config, tracking_service=tracking)

    assert not result.skipped
    assert result.gate_packet is not None
    assert result.gate_packet.trigger.visual_changed  # cold-start 放行体现为 visual_changed=True


@pytest.mark.asyncio
async def test_full_pipeline():
    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]
    s = create_input_slice("study-room", frames, np.zeros(16000, dtype=np.int16))

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    ctx = OmniContext(
        rule_conditions=[RuleCondition(rule_id="reading_light", rule_name="读书开灯", query="是否在读书")],
    )
    tracking = MockTrackingService(create_default_mock_response())

    with patch(
        "miloco.perception.engine.omni.omni.call_omni",
        new_callable=AsyncMock,
        return_value=MOCK_OMNI_RESPONSE,
    ):
        result = await run_pipeline(s, ctx, config, tracking_service=tracking)

    assert not result.skipped
    assert result.gate_packet is not None
    assert result.gate_packet.trigger.visual_changed
    assert result.identity_packet is not None
    assert result.identity_packet.targets[0].person_id == "none"
    assert result.omni_output is not None
    assert "看书" in result.omni_output.caption[0].description
    assert result.omni_output.matched_rules[0].rule_id == "reading_light"


# =============================================================================
# Batch pipeline tests
# =============================================================================


@pytest.mark.asyncio
async def test_batch_single_device():
    """Single device in batch should work identically to run_pipeline."""
    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]

    snapshot = _make_snapshot("study-room", "cam-1", frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snapshot])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    # contexts now keyed by device_id (per-device omni)
    contexts = {"cam-1": OmniContext()}
    tracking = MockTrackingService(create_default_mock_response())

    with patch(
        "miloco.perception.engine.omni.omni.call_omni",
        new_callable=AsyncMock,
        return_value=MOCK_OMNI_RESPONSE,
    ):
        result = await run_batch_pipeline(
            batch, contexts, config, get_tracking_service=lambda did, room_name: tracking,
        )

    assert "study-room" in result.rooms
    room = result.rooms["study-room"]
    assert not room.skipped
    assert "cam-1" in room.omni_outputs
    assert "看书" in room.omni_outputs["cam-1"].caption[0].description
    assert "cam-1" in room.device_results
    assert room.device_results["cam-1"].identity_packet is not None


@pytest.mark.asyncio
async def test_batch_multi_device_same_room():
    """Multiple devices in the same room → per-device omni call (2 devices → 2 calls)."""
    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames_with_change = [gray, gray, white, white, white, white]

    snap1 = _make_snapshot("living-room", "cam-1", frames_with_change, np.zeros(16000, dtype=np.int16))
    snap2 = _make_snapshot("living-room", "cam-2", frames_with_change, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap1, snap2])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    contexts = {"cam-1": OmniContext(), "cam-2": OmniContext()}
    tracking = MockTrackingService(create_default_mock_response())

    call_count = 0
    original_mock = MOCK_OMNI_RESPONSE

    async def counting_call_omni(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_mock

    with patch("miloco.perception.engine.omni.omni.call_omni", side_effect=counting_call_omni):
        result = await run_batch_pipeline(
            batch, contexts, config, get_tracking_service=lambda did, room_name: tracking,
        )

    assert "living-room" in result.rooms
    room = result.rooms["living-room"]
    assert not room.skipped
    assert len(room.device_results) == 2
    assert "cam-1" in room.device_results
    assert "cam-2" in room.device_results
    # Per-device omni: 2 devices → 2 omni calls. Each device gets its own omni_output.
    assert call_count == 2
    assert "cam-1" in room.omni_outputs
    assert "cam-2" in room.omni_outputs


@pytest.mark.asyncio
async def test_batch_multi_room():
    """Multiple rooms should produce independent Omni calls (per-device)."""
    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]

    snap1 = _make_snapshot("study-room", "cam-study", frames, np.zeros(16000, dtype=np.int16))
    snap2 = _make_snapshot("kitchen", "cam-kitchen", frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap1, snap2])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    contexts = {
        "cam-study": OmniContext(),
        "cam-kitchen": OmniContext(),
    }
    tracking = MockTrackingService(create_default_mock_response())

    call_count = 0

    async def counting_call_omni(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return MOCK_OMNI_RESPONSE

    with patch("miloco.perception.engine.omni.omni.call_omni", side_effect=counting_call_omni):
        result = await run_batch_pipeline(
            batch, contexts, config, get_tracking_service=lambda did, room_name: tracking,
        )

    assert len(result.rooms) == 2
    assert "study-room" in result.rooms
    assert "kitchen" in result.rooms
    # 2 rooms × 1 device each = 2 omni calls
    assert call_count == 2
    assert "cam-study" in result.rooms["study-room"].omni_outputs
    assert "cam-kitchen" in result.rooms["kitchen"].omni_outputs


@pytest.mark.asyncio
async def test_batch_all_skipped():
    """When all devices have no visual/audio change, all rooms should be skipped."""
    frame = _solid(100, 100, 100)
    static_frames = [frame] * 6

    snap1 = _make_snapshot("study-room", "cam-1", static_frames, np.zeros(16000, dtype=np.int16))
    snap2 = _make_snapshot("kitchen", "cam-2", static_frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap1, snap2])

    config = PerceptionConfig()
    contexts = {}
    # 预置基准 → 非 cold-start 的稳态:静止窗才会被丢
    base = _preprocess(frame)
    gate_prev_frames = {"cam-1": base, "cam-2": base}

    result = await run_batch_pipeline(batch, contexts, config, gate_prev_frames=gate_prev_frames)

    assert len(result.rooms) == 2
    assert result.rooms["study-room"].skipped is True
    assert result.rooms["kitchen"].skipped is True
    assert result.rooms["study-room"].omni_outputs == {}
    assert result.rooms["kitchen"].omni_outputs == {}


@pytest.mark.asyncio
async def test_batch_partial_skip():
    """When some devices skip (gate) and others trigger, only triggered ones go to Omni."""
    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    static_frames = [gray] * 6
    change_frames = [gray, gray, white, white, white, white]

    snap_static = _make_snapshot("living-room", "cam-static", static_frames, np.zeros(16000, dtype=np.int16))
    snap_change = _make_snapshot("living-room", "cam-change", change_frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap_static, snap_change])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    contexts = {"cam-static": OmniContext(), "cam-change": OmniContext()}
    tracking = MockTrackingService(create_default_mock_response())
    # 预置基准 → 非 cold-start:cam-static 才会被 gate 丢、cam-change 靠真实视觉变化通过
    base = _preprocess(gray)
    gate_prev_frames = {"cam-static": base, "cam-change": base}

    with patch(
        "miloco.perception.engine.omni.omni.call_omni",
        new_callable=AsyncMock,
        return_value=MOCK_OMNI_RESPONSE,
    ):
        result = await run_batch_pipeline(
            batch, contexts, config,
            get_tracking_service=lambda did, room_name: tracking,
            gate_prev_frames=gate_prev_frames,
        )

    room = result.rooms["living-room"]
    assert not room.skipped
    assert room.device_results["cam-static"].skipped is True
    assert room.device_results["cam-change"].skipped is False
    assert room.device_results["cam-change"].identity_packet is not None
    # Only cam-change triggered → only cam-change has omni_output
    assert "cam-change" in room.omni_outputs
    assert "cam-static" not in room.omni_outputs


@pytest.mark.asyncio
async def test_cold_start_first_window_passes_then_static_skips():
    """生产流式:首窗(did 未入 gate_prev_frames)cold-start 放行并写回基准;
    第二窗同一静止场景 → 已非 cold-start → skip(验证只放行首窗、有界)。"""
    frame = _solid(100, 100, 100)
    static = [frame] * 6
    silent = np.zeros(16000, dtype=np.int16)
    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    contexts = {"cam-1": OmniContext()}
    tracking = MockTrackingService(create_default_mock_response())
    gate_prev_frames: dict = {}

    with patch(
        "miloco.perception.engine.omni.omni.call_omni",
        new_callable=AsyncMock,
        return_value=MOCK_OMNI_RESPONSE,
    ):
        first = await run_batch_pipeline(
            BatchedSnapshot(snapshots=[_make_snapshot("r1", "cam-1", static, silent)]),
            contexts, config,
            get_tracking_service=lambda did, room_name: tracking,
            gate_prev_frames=gate_prev_frames,
        )
        dr1 = first.rooms["r1"].device_results["cam-1"]
        assert not dr1.skipped
        assert dr1.gate_packet.trigger.visual_changed  # cold-start 放行
        assert "cam-1" in gate_prev_frames  # 基准已写回 → 不再是首窗

        second = await run_batch_pipeline(
            BatchedSnapshot(snapshots=[_make_snapshot("r1", "cam-1", static, silent)]),
            contexts, config,
            get_tracking_service=lambda did, room_name: tracking,
            gate_prev_frames=gate_prev_frames,
        )
        assert second.rooms["r1"].device_results["cam-1"].skipped is True


@pytest.mark.asyncio
async def test_batch_empty():
    """Empty batch should return empty result."""
    batch = BatchedSnapshot(snapshots=[])
    config = PerceptionConfig()

    result = await run_batch_pipeline(batch, {}, config)

    assert len(result.rooms) == 0
    assert result.timing is not None


@pytest.mark.asyncio
async def test_batch_omni_runs_concurrently():
    """跨 room 多设备的 omni 应并发执行（嵌套 gather），而非串行 await。

    用并发计数器断言：两台不同 room 的相机 omni 调用同时在飞（max 并发==2）。
    串行实现下 A 会跑完整段（含 sleep）才轮到 B → max 永远=1；并发实现下两者
    同时进入 → max=2。这是串行 vs 并发的确定性判别（不依赖墙钟，不 flaky）。
    """
    import asyncio as _asyncio

    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]
    snap1 = _make_snapshot("study-room", "cam-study", frames, np.zeros(16000, dtype=np.int16))
    snap2 = _make_snapshot("kitchen", "cam-kitchen", frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap1, snap2])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    contexts = {"cam-study": OmniContext(), "cam-kitchen": OmniContext()}
    tracking = MockTrackingService(create_default_mock_response())

    concurrent = 0
    max_concurrent = 0

    async def tracking_call_omni(*args, **kwargs):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await _asyncio.sleep(0.05)  # 让出事件循环，给另一台相机进入的机会
        concurrent -= 1
        return MOCK_OMNI_RESPONSE

    with patch("miloco.perception.engine.omni.omni.call_omni", side_effect=tracking_call_omni):
        result = await run_batch_pipeline(
            batch, contexts, config, get_tracking_service=lambda did, room_name: tracking,
        )

    assert max_concurrent == 2, f"omni 未并发执行，max_concurrent={max_concurrent}"
    assert "cam-study" in result.rooms["study-room"].omni_outputs
    assert "cam-kitchen" in result.rooms["kitchen"].omni_outputs


@pytest.mark.asyncio
async def test_batch_omni_error_partial():
    """partial:一台相机 omni 抛 OmniError → **不连累整窗**;健康相机照常产出,失败相机 skipped。

    （改自旧 test_batch_omni_error_fails_whole_cycle 的"整窗抛错"语义——多相机下一台 429/挂
    不该让全屋静默。）
    """
    from miloco.perception.engine.omni.omni_client import OmniError
    from miloco.perception.types import RealtimePerceptionResult

    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]
    snap1 = _make_snapshot("study-room", "cam-1", frames, np.zeros(16000, dtype=np.int16))
    snap2 = _make_snapshot("kitchen", "cam-2", frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap1, snap2])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    contexts = {"cam-1": OmniContext(), "cam-2": OmniContext()}
    tracking = MockTrackingService(create_default_mock_response())

    # study-room 的 omni 失败、kitchen 成功（非 fused 路径走 run_omni_batch，edge_packet 带 room_name）
    async def selective_omni_batch(edge_packets, context, cfg):
        if edge_packets[0].room_name == "study-room":
            raise OmniError("cam-1 omni boom")
        return RealtimePerceptionResult(
            caption=[CaptionEntry(changed=True, area="kitchen", description="厨房有人")]
        )

    with patch(
        "miloco.perception.engine.pipeline.run_omni_batch",
        side_effect=selective_omni_batch,
    ):
        result = await run_batch_pipeline(
            batch, contexts, config, get_tracking_service=lambda did, room_name: tracking,
        )

    # 健康相机照常产出
    assert "cam-2" in result.rooms["kitchen"].omni_outputs
    # 失败相机 skipped、无 omni_output、不抛错
    assert result.rooms["study-room"].device_results["cam-1"].skipped is True
    assert "cam-1" not in result.rooms["study-room"].omni_outputs
    # timing 记 _omni_error_<did> = OmniError.code(原始异常类名,用于 dashboard 分类)
    # 这里没传 original,fallback 到类名 "OmniError"
    err = result.rooms["study-room"].timing.get("_omni_error_cam-1", "")
    assert err == "OmniError", err


@pytest.mark.asyncio
async def test_batch_omni_error_all_fail_room_skipped():
    """全部相机 omni 失败 → room skipped、无 omni_output（merge 会得空、不 submit）。"""
    from miloco.perception.engine.omni.omni_client import OmniError

    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]
    snap = _make_snapshot("study-room", "cam-1", frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    contexts = {"cam-1": OmniContext()}
    tracking = MockTrackingService(create_default_mock_response())

    async def always_fail(*args, **kwargs):
        raise OmniError("boom")

    with patch(
        "miloco.perception.engine.pipeline.run_omni_batch", side_effect=always_fail
    ):
        result = await run_batch_pipeline(
            batch, contexts, config, get_tracking_service=lambda did, room_name: tracking,
        )

    assert result.rooms["study-room"].skipped is True
    assert result.rooms["study-room"].omni_outputs == {}


@pytest.mark.asyncio
async def test_query_rooms_run_concurrently():
    """on_demand 查询路径跨 room 并发（run_query_pipeline 嵌套 gather）。

    两个不同 room 的 omni 应同时在飞（max 并发==2，串行下为 1），且结果按 room 聚合。
    """
    import asyncio as _asyncio

    from miloco.perception.engine.pipeline import run_query_pipeline

    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]
    snap1 = _make_snapshot("study-room", "cam-study", frames, np.zeros(16000, dtype=np.int16))
    snap2 = _make_snapshot("kitchen", "cam-kitchen", frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap1, snap2])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    tracking = MockTrackingService(create_default_mock_response())

    concurrent = 0
    max_concurrent = 0

    async def tracking_call_omni(*args, **kwargs):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await _asyncio.sleep(0.05)
        concurrent -= 1
        return {"mock": "resp"}

    with patch(
        "miloco.perception.engine.omni.omni_client.call_omni",
        side_effect=tracking_call_omni,
    ), patch(
        "miloco.perception.engine.omni.response_parser.parse_query_response",
        return_value="有人在",
    ):
        results = await run_query_pipeline(
            batch, "现在有人吗", config,
            get_tracking_service=lambda did, room_name: tracking,
        )

    assert max_concurrent == 2, f"on_demand 未并发, max={max_concurrent}"
    assert results["study-room"].answer == "有人在"
    assert results["kitchen"].answer == "有人在"


# =============================================================================
# Rule data chain test
# =============================================================================

MOCK_RULE_RESPONSE = {
    "id": "mock",
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {
                        "caption": [{"area": "客厅", "description": "爸爸站在门口"}],
                        "matched_rules": [
                            {
                                "rule_id": "rule_dad_home",
                                "reason": "检测到爸爸出现在门口",
                            },
                        ],
                        "speeches": [],
                        "suggestions": [],
                    }
                ),
            },
        }
    ],
}


@pytest.mark.asyncio
async def test_rules_flow_through_pipeline():
    """Verify rules are passed through: OmniContext → prompt → model output → matched_rules."""
    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]
    s = create_input_slice("客厅", frames, np.zeros(16000, dtype=np.int16))

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    ctx = OmniContext(
        rule_conditions=[
            RuleCondition(
                rule_id="rule_dad_home",
                rule_name="爸爸回家播报",
                query="爸爸出现在门口",
            ),
            RuleCondition(rule_id="rule_fall", rule_name="摔倒检测", query="有人摔倒"),
        ],
    )
    tracking = MockTrackingService(create_default_mock_response())

    with patch(
        "miloco.perception.engine.omni.omni.call_omni",
        new_callable=AsyncMock,
        return_value=MOCK_RULE_RESPONSE,
    ):
        result = await run_pipeline(s, ctx, config, tracking_service=tracking)

    assert not result.skipped
    assert result.omni_output is not None

    # Verify rules were in prompt
    # (indirectly verified by model returning matched rule_id)
    assert len(result.omni_output.matched_rules) == 1
    assert result.omni_output.matched_rules[0].rule_id == "rule_dad_home"
    assert "爸爸" in result.omni_output.matched_rules[0].reason


@pytest.mark.asyncio
async def test_rules_in_prompt_content():
    """Verify rule conditions appear in the prompt sent to Omni."""
    from miloco.perception.engine.omni.prompt_builder import build_prompt

    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    ep = IdentityPacket(
        packet_id="ep-1",
        room_name="门口",
        timestamp=1000.0,
        frame_info=FrameInfo(start_timestamp=0, end_timestamp=3000, fps=2),
        targets=[],
        scene_motion=MotionState.STATIC,
        frames=[SelectedFrame(frame_index=0, image=frame, resolution=FrameResolution.HIGH, crops=[])],
        all_frames=[frame],
        audio_clip=np.zeros(100, dtype=np.int16),
        audio_analysis=AudioAnalysis(type=AudioType.SILENCE, is_urgent=False, energy_level=0.0),
    )
    ctx = OmniContext(
        rule_conditions=[
            RuleCondition(rule_id="rule_001", rule_name="爸爸回家", query="爸爸出现在门口"),
            RuleCondition(rule_id="rule_002", rule_name="摔倒检测", query="有人摔倒"),
        ],
    )

    payload = build_prompt(ep, ctx)
    content = payload["user_content"]

    assert "# 待判断规则" in content
    assert "爸爸回家" in content  # 规则按 rule_name 渲染
    assert "爸爸出现在门口" in content
    assert "摔倒检测" in content
    assert "有人摔倒" in content


# =============================================================================
# FPS downsampling tests
# =============================================================================


def _make_omni_packet(n: int) -> IdentityPacket:
    """造一个 all_frames 用序号标识的 packet（函数只按下标取帧，
    用 int 占位即可），便于断言抽帧到底保留了哪几帧。"""
    return IdentityPacket(
        packet_id="dp",
        room_name="r",
        timestamp=0.0,
        frame_info=FrameInfo(start_timestamp=0, end_timestamp=3000, fps=3),
        targets=[],
        scene_motion=MotionState.STATIC,
        frames=[],
        all_frames=list(range(n)),  # type: ignore[arg-type]
        audio_clip=np.zeros(1, dtype=np.int16),
        audio_analysis=AudioAnalysis(
            type=AudioType.SILENCE, is_urgent=False, energy_level=0.0
        ),
    )


@pytest.mark.parametrize(
    "n,expected",
    [(9, [2, 5, 8]), (10, [0, 3, 6, 9]), (7, [0, 3, 6]), (3, [2]), (2, [1]), (1, [0])],
)
def test_downsample_for_omni_anchors_last_frame(n, expected):
    """src_fps=3→omni_fps=1: 以末帧为锚抽帧, 必含末帧 n-1。"""
    out = _downsample_for_omni(_make_omni_packet(n), src_fps=3, omni_fps=1)
    assert out.all_frames == expected  # 含末帧
    assert out.frame_info.fps == 1


def test_downsample_for_omni_noop_when_not_needed():
    """omni_fps>=src_fps 或 step<=1 时原样返回(同一对象), 不复制。"""
    pkt = _make_omni_packet(5)
    assert _downsample_for_omni(pkt, src_fps=3, omni_fps=3) is pkt  # omni>=src
    assert _downsample_for_omni(pkt, src_fps=1, omni_fps=1) is pkt  # step<=1


def _make_high_fps_snapshot(
    room_name: str,
    did: str,
    n_frames: int,
    duration_ms: float = 3000,
    audio: np.ndarray | None = None,
    frames_override: list | None = None,
) -> DeviceSnapshot:
    """Create a snapshot with n_frames (simulating high fps camera)."""
    device = PerceptionDevice(did=did, name=did, device_type="camera", room_name=room_name)
    now = 1000000.0
    frames = frames_override or [np.zeros((100, 100, 3), dtype=np.uint8) for _ in range(n_frames)]
    n_frames = len(frames)
    video_frames = [VideoFrame(data=f, timestamp=now + i * duration_ms / n_frames) for i, f in enumerate(frames)]
    h, w = 100, 100
    video_stream = VideoStream(frames=video_frames, width=w, height=h)
    audio_stream = None
    if audio is not None and len(audio) > 0:
        audio_stream = AudioStream(frames=[AudioFrame(data=audio, timestamp=now)])
    return DeviceSnapshot(
        device=device,
        start_timestamp=now,
        end_timestamp=now + duration_ms,
        video=video_stream,
        audio=audio_stream,
    )


def test_downsample_54_to_9():
    """18fps × 3s = 54 frames → downsample to 3fps × 3s = 9 frames."""
    snapshot = _make_high_fps_snapshot("room", "cam-1", 54, duration_ms=3000)
    assert len(snapshot.frames) == 54

    result = downsample_snapshot(snapshot, target_fps=3)
    assert len(result.frames) == 9
    assert result.audio == snapshot.audio
    assert result.device == snapshot.device


def test_downsample_54_to_6():
    """18fps × 3s = 54 frames → downsample to 2fps × 3s = 6 frames."""
    snapshot = _make_high_fps_snapshot("room", "cam-1", 54, duration_ms=3000)
    result = downsample_snapshot(snapshot, target_fps=2)
    assert len(result.frames) == 6


def test_downsample_already_low():
    """Already 3fps (9 frames) → downsample to 3fps → still 9 frames."""
    snapshot = _make_high_fps_snapshot("room", "cam-1", 9, duration_ms=3000)
    result = downsample_snapshot(snapshot, target_fps=3)
    assert len(result.frames) == 9


def test_downsample_no_video():
    """Snapshot without video → returns unchanged."""
    device = PerceptionDevice(did="cam", name="cam", device_type="camera", room_name="room")
    snapshot = DeviceSnapshot(
        device=device,
        start_timestamp=0,
        end_timestamp=3000,
        video=None,
        audio=None,
    )
    result = downsample_snapshot(snapshot, target_fps=3)
    assert result.frames == []


@pytest.mark.asyncio
async def test_pipeline_downsamples_high_fps():
    """End-to-end: 54 frames → pipeline downsamples → Edge gets 9 frames."""
    # Mix gray and white frames to trigger Gate visual change
    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    high_fps_frames = [gray if i < 27 else white for i in range(54)]
    snapshot = _make_high_fps_snapshot(
        "study-room",
        "cam-1",
        54,
        duration_ms=3000,
        audio=np.zeros(48000, dtype=np.int16),
        frames_override=high_fps_frames,
    )
    assert len(snapshot.frames) == 54

    config = PerceptionConfig()
    config.input.fps = 3
    config.omni.api_key = "test"
    ctx = OmniContext()
    tracking = MockTrackingService(create_default_mock_response())

    with patch(
        "miloco.perception.engine.omni.omni.call_omni",
        new_callable=AsyncMock,
        return_value=MOCK_OMNI_RESPONSE,
    ):
        result = await run_pipeline(snapshot, ctx, config, tracking_service=tracking)

    assert not result.skipped
    # Edge should have received downsampled frames (9, not 54)
    assert result.identity_packet is not None
    # Gate packet should have 9 frames (after downsample)
    assert result.gate_packet is not None
    assert len(result.gate_packet.frames) == 9


@pytest.mark.asyncio
async def test_query_pipeline_downsamples_omni_to_omni_fps():
    """on-demand query 路径: 送 omni 的 packet 必须下采到 omni_fps(1)、不跟随 input.fps(3),
    否则 fps 提频后查询送 omni 的帧数翻 3 倍。断言进 build_query_prompt 的 packet 已是
    1fps / 3 帧(而非 3fps / 9 帧)。"""
    gray = _solid(100, 100, 100)
    white = _solid(255, 255, 255)
    frames = [gray if i < 27 else white for i in range(54)]  # 54 帧 @3s
    snap = _make_high_fps_snapshot(
        "living-room", "cam-1", 54, duration_ms=3000,
        audio=np.zeros(48000, dtype=np.int16), frames_override=frames,
    )
    batch = BatchedSnapshot(snapshots=[snap])

    config = PerceptionConfig()
    config.input.fps = 3       # 下采后 all_frames = 9
    config.input.omni_fps = 1  # 期望 omni 收到 3 帧 / 1fps
    config.omni.api_key = "test"
    tracking = MockTrackingService(create_default_mock_response())

    captured: dict = {}

    def _capture(*, identity_packets, **kwargs):
        captured["packets"] = identity_packets
        return {"messages": [], "video_fps": identity_packets[0].frame_info.fps}

    with patch(
        "miloco.perception.engine.omni.prompt_builder.build_query_prompt",
        side_effect=_capture,
    ), patch(
        "miloco.perception.engine.omni.omni_client.call_omni",
        new_callable=AsyncMock, return_value=MOCK_OMNI_RESPONSE,
    ), patch(
        "miloco.perception.engine.omni.response_parser.parse_query_response",
        return_value="ok",
    ):
        await run_query_pipeline(
            batch, "客厅现在什么情况", config,
            get_tracking_service=lambda did, room: tracking,
        )

    pkts = captured.get("packets")
    assert pkts, "build_query_prompt 未收到 packet"
    assert pkts[0].frame_info.fps == 1          # 解耦到 omni_fps, 非 input.fps=3
    assert len(pkts[0].all_frames) == 3         # 9 帧 → 末帧锚点抽到 [2,5,8]


# =============================================================================
# _merge_results: description normalization & pending_speech timeout
# =============================================================================


def _make_batch_result(
    room_name: str,
    description: str = "场景描述",
    time: str = "",
    interaction_incomplete: bool = False,
    device_id: str | None = None,
    incomplete_content: str = "未说完",
    incomplete_speaker: str = "用户",
) -> BatchPipelineResult:
    """Construct a minimal BatchPipelineResult for _merge_results tests.

    per-device 改造后 omni_output 挂在 ``DevicePipelineResult.omni_output``。fixture
    默认 ``device_id == room_name`` 简化（_last_captions / _pending_speech 也按
    device_id 索引）。
    """
    from miloco.perception.types import RealtimePerceptionResult

    did = device_id or room_name
    omni_output = RealtimePerceptionResult(
        time=time,
        caption=[CaptionEntry(description=description)],
        speeches=[
            Speech(
                needs_response=False,
                speaker=incomplete_speaker,
                content=incomplete_content,
                is_complete=False,
            )
        ]
        if interaction_incomplete
        else [],
    )
    dr = DevicePipelineResult(device_id=did, omni_output=omni_output)
    room = RoomPipelineResult(room_name=room_name, device_results={did: dr})
    return BatchPipelineResult(rooms={room_name: room})


def test_merge_preserves_last_caption_on_no_change():
    """description 空 → 不向下游传播 caption，且不覆盖 last_caption 基准。"""
    engine = PerceptionEngine()
    engine._last_captions["书房"] = "人物坐在桌前看书"

    result = _make_batch_result("书房", description="")
    merged = engine._merge_results(result)

    assert merged.caption == []  # 空描述不传播（caption 去重下沉代码）
    assert engine._last_captions["书房"] == "人物坐在桌前看书"  # cache 不被刷


def test_merge_updates_last_caption_on_real_change():
    """有画面变化（description 非空）时，正常更新 last_caption。"""
    engine = PerceptionEngine()
    engine._last_captions["书房"] = "人物坐在桌前看书"

    result = _make_batch_result("书房", description="人物站起来走向门口")
    merged = engine._merge_results(result)

    assert merged.caption[0].description == "人物站起来走向门口"
    assert engine._last_captions["书房"] == "人物站起来走向门口"


def test_merge_emits_caption_every_window_no_dedup():
    """caption 不再去重：即便与上轮逐字相同也照常下发（避免规则命中窗 caption 被吞，
    日志不直观）。基准仍刷新为本轮原文。"""
    engine = PerceptionEngine()
    engine._last_captions["书房"] = "人物坐在桌前看书"

    # 与上轮逐字相同 → 仍然下发（不去重）
    result = _make_batch_result("书房", description="人物坐在桌前看书")
    merged = engine._merge_results(result)

    assert merged.caption[0].description == "人物坐在桌前看书"  # 不去重，照常外发
    assert engine._last_captions["书房"] == "人物坐在桌前看书"


def test_merge_no_change_preserves_cached_time():
    """description 空时 cache 不变；未传 contexts（无注入）→ time 留空，不现编。"""
    engine = PerceptionEngine()
    engine._last_captions["书房"] = "人物坐在桌前看书"

    result = _make_batch_result("书房", description="")
    merged = engine._merge_results(result)

    assert merged.time == ""  # 本轮没给模型注入时间 → 忠实留空
    assert engine._last_captions["书房"] == "人物坐在桌前看书"


def test_merge_echoes_injected_time():
    """传了 contexts 且 ctx.current_time 非空 → merged.time 回显该注入时间。"""
    engine = PerceptionEngine()
    result = _make_batch_result("书房", description="有人进来")
    merged = engine._merge_results(result, {"书房": OmniContext(current_time="14:30:00")})

    assert merged.time == "14:30:00"  # 回显模型实际看到的注入时间


def test_pending_speech_dropped_after_timeout():
    """pending_speech 超过 max rounds 后被丢弃（per-device 维度）。"""
    engine = PerceptionEngine()
    engine._pending_speech_rounds["书房"] = engine._max_pending_speech_rounds

    result = _make_batch_result("书房", description="新场景", interaction_incomplete=True)
    engine._merge_results(result)

    assert "书房" not in engine._pending_speech
    assert "书房" not in engine._pending_speech_rounds


def test_pending_speech_pure_repeat_breaks_loop():
    """本轮 incomplete content 与上轮 pending_speech 完全一致 → 断链，不再续命。

    反馈环熔断：上轮已有 pending_speech "打开"，本轮模型再次复读 "打开" + incomplete，
    视为模型未真正听到新音频，不写回 pending_speech，下轮 user_content 不再注入。
    """
    engine = PerceptionEngine()
    engine._pending_speech["书房"] = [{"speaker": "用户", "content": "打开"}]
    engine._pending_speech_rounds["书房"] = 1

    result = _make_batch_result(
        "书房",
        description="",
        interaction_incomplete=True,
        incomplete_content="打开",
    )
    engine._merge_results(result)

    assert "书房" not in engine._pending_speech
    assert "书房" not in engine._pending_speech_rounds


def test_pending_speech_partial_repeat_keeps_new_content():
    """本轮 incomplete 既有复读也有新内容 → 仅保留新内容，rounds 计数照常推进。

    防止"一并丢弃"过激：模型同一轮可能输出多条 interaction，部分是复读、部分是
    本轮真听到的新片段，新片段必须保留以支持下轮拼接。
    """
    from miloco.perception.types import RealtimePerceptionResult

    engine = PerceptionEngine()
    engine._pending_speech["书房"] = [{"speaker": "用户", "content": "打开"}]
    engine._pending_speech_rounds["书房"] = 1

    omni_output = RealtimePerceptionResult(
        caption=[CaptionEntry(description="")],
        speeches=[
            Speech(needs_response=False, speaker="用户", content="打开", is_complete=False),
            Speech(needs_response=False, speaker="用户", content="客厅的", is_complete=False),
        ],
    )
    dr = DevicePipelineResult(device_id="书房", omni_output=omni_output)
    room = RoomPipelineResult(room_name="书房", device_results={"书房": dr})
    result = BatchPipelineResult(rooms={"书房": room})

    engine._merge_results(result)

    assert engine._pending_speech["书房"] == [{"speaker": "用户", "content": "客厅的"}]
    assert engine._pending_speech_rounds["书房"] == 2


def test_merge_keeps_underscore_keys_at_top_level():
    """room_timing 中以 "_" 开头的 key(per-device 元数据)合并后不应被 "{room}/" 前缀化,
    顶层保留 "_xxx" 形式,让 timing_detail / _aggregate_stage_ms 的 "_" 前缀过滤生效。
    """
    engine = PerceptionEngine()
    room = RoomPipelineResult(
        room_name="客厅",
        device_results={},
        timing={
            "gate_d1_ms": 1.0,
            "_device_trace_id_d1": "uuid-d1",
        },
    )
    result = BatchPipelineResult(rooms={"客厅": room}, timing={"_pipeline_total_ms": 5.0})
    merged = engine._merge_results(result)
    assert merged.timing is not None
    assert merged.timing["客厅/gate_d1_ms"] == 1.0
    assert merged.timing["_device_trace_id_d1"] == "uuid-d1"
    assert merged.timing["_pipeline_total_ms"] == 5.0
    assert "客厅/_device_trace_id_d1" not in merged.timing


def test_loopback_incomplete_interaction_not_emitted_to_client():
    """反馈环命中时，incomplete interaction 不进入 merged.speeches。

    避免历史上 "dropping" 日志只断 pending_speech 写回、interaction 仍然外发到
    client 的语义错觉。
    """
    engine = PerceptionEngine()
    engine._pending_speech["书房"] = [{"speaker": "用户", "content": "打开"}]
    engine._pending_speech_rounds["书房"] = 1

    result = _make_batch_result(
        "书房",
        description="",
        interaction_incomplete=True,
        incomplete_content="打开",
    )
    merged = engine._merge_results(result)

    assert merged.speeches == []


def test_fallback_skipped_caption_does_not_pollute_last_captions():
    """JSON 解析失败时 _fallback 返回的 OmniOutput(skipped=True, caption=[...])
    不能污染 last_captions 缓存，否则下一轮 prompt "上次场景" 注入会带入复读
    片段，形成反馈环。
    """
    from miloco.perception.types import RealtimePerceptionResult

    engine = PerceptionEngine()
    engine._last_captions["书房"] = "原本的正常场景描述"

    # 模拟 response_parser._fallback 的返回结构
    omni_output = RealtimePerceptionResult(
        skipped=True,
        caption=[CaptionEntry(
            description='[解析失败] Failed to parse JSON: {"content":"No, no, no, no, ...',
        )],
    )
    dr = DevicePipelineResult(device_id="书房", omni_output=omni_output)
    room = RoomPipelineResult(room_name="书房", device_results={"书房": dr})
    result = BatchPipelineResult(rooms={"书房": room})

    merged = engine._merge_results(result)

    # last_captions 不被污染，保留原值
    assert engine._last_captions["书房"] == "原本的正常场景描述"
    # fallback caption 也不外发到 client
    assert merged.caption == []


def test_loopback_does_not_suppress_complete_interaction():
    """同一轮既有反馈环 incomplete 也有 complete interaction → 只压 incomplete。

    complete 是模型本轮真听到的实质对话，必须照常外发。
    """
    from miloco.perception.types import RealtimePerceptionResult

    engine = PerceptionEngine()
    engine._pending_speech["书房"] = [{"speaker": "用户", "content": "打开"}]
    engine._pending_speech_rounds["书房"] = 1

    omni_output = RealtimePerceptionResult(
        caption=[CaptionEntry(description="")],
        speeches=[
            Speech(needs_response=False, speaker="用户", content="打开", is_complete=False),
            Speech(needs_response=True, speaker="用户", content="把灯关了", is_complete=True),
        ],
    )
    dr = DevicePipelineResult(device_id="书房", omni_output=omni_output)
    room = RoomPipelineResult(room_name="书房", device_results={"书房": dr})
    result = BatchPipelineResult(rooms={"书房": room})

    merged = engine._merge_results(result)

    assert len(merged.speeches) == 1
    assert merged.speeches[0].content == "把灯关了"
    assert merged.speeches[0].is_complete


# =============================================================================
# _tracking_service_kwargs: mode-specific 参数构造 (regression for silent-drop bug)
# =============================================================================
#
# Bug 历史:原条件 ``if mode in ("real", "fast", "detect_only")`` 让 deep_sort 落到
# else 分支拿空 dict —— yaml 的 perception_* / identity_engine.deep_sort 段全部
# silent drop,DeepSortTrackingService 用默认参数构造。
# 修复后 deep_sort 模式必须含 common_kwargs(model_dir / use_gpu / fps 等) +
# deep_sort_config,real 模式必须含 common + sort_config,mock 模式仍空。


def test_tracking_kwargs_mock_empty():
    """mock 模式 _tracking_service_kwargs 为空 dict(create_tracking_service 不需要参数)。"""
    config = PerceptionConfig()
    config.identity.tracking_service_mode = "mock"
    engine = PerceptionEngine(config=config)
    assert engine._tracking_service_kwargs == {}


def test_tracking_kwargs_real_has_common_and_sort_config():
    """real 模式必须含 common_kwargs + sort_config,不含 deep_sort_config。"""
    config = PerceptionConfig()
    config.identity.tracking_service_mode = "real"
    engine = PerceptionEngine(config=config)
    kwargs = engine._tracking_service_kwargs

    # common_kwargs 字段必备
    for k in ("model_dir", "use_gpu",
              "input_width", "input_height", "fps"):
        assert k in kwargs, f"real 模式缺 common 字段 {k}"
    # mode-specific
    assert "sort_config" in kwargs, "real 模式缺 sort_config"
    assert "deep_sort_config" not in kwargs, "real 模式不应含 deep_sort_config"


def test_tracking_kwargs_deep_sort_has_common_and_deep_sort_config():
    """deep_sort 模式必须含 common_kwargs + deep_sort_config,不含 sort_config。

    regression for silent-drop bug: 原 if 条件遗漏 deep_sort 导致 kwargs 变空 dict、
    yaml 配置全部失效;修复后 yaml 的 perception_* / identity_engine.deep_sort
    应该真实传给 DeepSortTrackingService。
    """
    config = PerceptionConfig()
    config.identity.tracking_service_mode = "deep_sort"
    engine = PerceptionEngine(config=config)
    kwargs = engine._tracking_service_kwargs

    # common_kwargs 字段必备(silent-drop bug 期间这些都丢)
    for k in ("model_dir", "use_gpu",
              "input_width", "input_height", "fps"):
        assert k in kwargs, f"deep_sort 模式缺 common 字段 {k}"
    # mode-specific:deep_sort 接 deep_sort_config 不接 sort_config
    assert "deep_sort_config" in kwargs, "deep_sort 模式缺 deep_sort_config"
    assert "sort_config" not in kwargs, "deep_sort 模式不应含 sort_config"
    # deep_sort_config 应当是 yaml 配置实例,不是 None / 默认占位
    assert kwargs["deep_sort_config"] is config.identity_engine.deep_sort


# =============================================================================
# 流式 suggestion 早出：经事件链闸门去重（_wrap_suggestions_cb）
# =============================================================================


def _bare_engine_for_chain() -> PerceptionEngine:
    """绕过重 __init__，只准备事件链依赖的字段。

    _embedder=None → assign_id_and_update_link 走精确文本匹配兜底（同文本归并、异文本
    开新链），无需加载真实 bge 模型即可测链接 / 心跳 / 上报逻辑。
    """
    eng = object.__new__(PerceptionEngine)
    eng._sugg_table = {}
    eng._next_sugg_id = {}
    eng._embedder = None
    return eng


@pytest.mark.asyncio
async def test_wrap_suggestions_cb_forwards_new_and_injects_meta():
    """新链 → 外发，且注入 room_name。"""
    eng = _bare_engine_for_chain()
    sent: list[list[str]] = []

    async def cb(suggs):
        sent.append([s.event for s in suggs])

    wrapped = _wrap_suggestions_cb(cb, "客厅", ["cam1"], eng.assign_id_and_update_link)
    s = Suggestion(event="猫推花瓶", action="提醒", urgency="low")
    await wrapped([s])

    assert sent == [["猫推花瓶"]]          # 新链外发
    assert s.room_name == "客厅"            # room_name 注入
    assert s.source_device_ids == ["cam1"]  # 设备 id 注入
    assert s.id == 1                        # engine 回写链 id


@pytest.mark.asyncio
async def test_wrap_suggestions_cb_suppresses_heartbeat():
    """同一持续事件再次出现（语义/文本命中已有链）→ 心跳，抑制不外发。"""
    eng = _bare_engine_for_chain()
    sent: list[list[str]] = []

    async def cb(suggs):
        sent.append([s.event for s in suggs])

    wrapped = _wrap_suggestions_cb(cb, "客厅", ["cam1"], eng.assign_id_and_update_link)
    await wrapped([Suggestion(event="猫推花瓶", action="提醒", urgency="low")])  # 建链 id=1
    sent.clear()

    # 同一事件再次出现 → 命中链 → 心跳抑制
    await wrapped([Suggestion(event="猫推花瓶", action="提醒", urgency="low")])
    assert sent == []  # 心跳被抑制，不重复打扰


@pytest.mark.asyncio
async def test_wrap_suggestions_cb_none_cb_returns_none():
    """cb 为 None（非流式 / 未接早出）→ 直接返回 None，不报错。"""
    eng = _bare_engine_for_chain()
    assert _wrap_suggestions_cb(None, "客厅", ["cam1"], eng.assign_id_and_update_link) is None


# =============================================================================
# Part3 per-omni 异步送（fused）：每相机 omni 一好就早送，suggestion 打事件链 id
# =============================================================================


@pytest.mark.asyncio
async def test_fused_per_omni_early_send_assigns_id_and_meta():
    """fused per-omni:本相机 omni 一好就经 on_early_* 早送 suggestion/speech;
    suggestion 经事件链闸门打 id 并把 omni_output.suggestions 裁成只剩新链(供 _merge_results
    保留进 result.suggestions、防重发交 client 侧 early_sent_sugg_ids),
    并注入 device_name / time_window / room_name(Part2 meta)。"""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from miloco.perception.engine.types import OmniOutput

    gray, white = _solid(100, 100, 100), _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]
    snap = _make_snapshot("客厅", "cam-1", frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    config.identity_engine.enabled = True
    config.identity_engine.omni_call_mode = "fused"
    contexts = {"cam-1": OmniContext()}
    tracking = MockTrackingService(create_default_mock_response())

    sent_sp: list = []
    sent_sg: list = []

    async def on_early_speeches(x):
        sent_sp.extend(x)

    async def on_early_suggestions(x):
        sent_sg.extend(x)

    eng = _bare_engine_for_chain()  # 真 assign_id_and_update_link

    fused_out = OmniOutput(
        speeches=[Speech(needs_response=True, speaker="用户", content="关灯", status="complete")],
        suggestions=[Suggestion(event="老人摔倒", action="查看", urgency="high")],
    )
    ipacket = SimpleNamespace(all_frames=[])

    with patch("miloco.perception.engine.pipeline.run_omni_fused",
               new_callable=AsyncMock, return_value=fused_out), \
         patch("miloco.perception.engine.pipeline.run_identity",
               new_callable=AsyncMock, return_value=ipacket):
        result = await run_batch_pipeline(
            batch, contexts, config,
            get_tracking_service=lambda did, room: tracking,
            get_identity_engine=lambda did, room: MagicMock(),
            on_early_speeches=on_early_speeches,
            on_early_suggestions=on_early_suggestions,
            assign_suggestion_link=eng.assign_id_and_update_link,
        )

    # per-omni 早送:本相机 suggestion/speech 经回调送出
    assert [s.event for s in sent_sg] == ["老人摔倒"]
    assert [s.content for s in sent_sp] == ["关灯"]
    # 闸门打了事件链 id;omni_output.suggestions 裁成只剩这条新链(供 merge 保留进 result)
    assert sent_sg[0].id == 1
    out = result.rooms["客厅"].device_results["cam-1"].omni_output
    assert len(out.suggestions) == 1
    assert out.suggestions[0].id == 1
    # Part2 meta 注入到位
    assert sent_sg[0].device_name == snap.device.name
    assert sent_sg[0].time_window
    assert sent_sg[0].room_name == "客厅"


@pytest.mark.asyncio
async def test_fused_per_omni_prunes_heartbeat_from_output():
    """fused per-omni:同一窗内重复(心跳)的 suggestion 经事件链闸门抑制,既不早送、
    也被从 omni_output.suggestions 裁掉——只剩新链。这样 _merge_results 保留进
    result.suggestions 的是「该上报的新链」,心跳沿用旧语义不入 result。"""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from miloco.perception.engine.types import OmniOutput

    gray, white = _solid(100, 100, 100), _solid(255, 255, 255)
    frames = [gray, gray, white, white, white, white]
    snap = _make_snapshot("客厅", "cam-1", frames, np.zeros(16000, dtype=np.int16))
    batch = BatchedSnapshot(snapshots=[snap])

    config = PerceptionConfig()
    config.omni.api_key = "test-key"
    config.identity_engine.enabled = True
    config.identity_engine.omni_call_mode = "fused"
    contexts = {"cam-1": OmniContext()}
    tracking = MockTrackingService(create_default_mock_response())

    sent_sg: list = []

    async def on_early_suggestions(x):
        sent_sg.extend(x)

    eng = _bare_engine_for_chain()  # 真 assign_id_and_update_link

    # 第一条 = 新链;第二条同一事件再次出现 → 文本命中链 → 心跳(抑制)
    fused_out = OmniOutput(
        suggestions=[
            Suggestion(event="老人摔倒", action="查看", urgency="high"),
            Suggestion(event="老人摔倒", action="查看", urgency="high"),
        ],
    )
    ipacket = SimpleNamespace(all_frames=[])

    with patch("miloco.perception.engine.pipeline.run_omni_fused",
               new_callable=AsyncMock, return_value=fused_out), \
         patch("miloco.perception.engine.pipeline.run_identity",
               new_callable=AsyncMock, return_value=ipacket):
        result = await run_batch_pipeline(
            batch, contexts, config,
            get_tracking_service=lambda did, room: tracking,
            get_identity_engine=lambda did, room: MagicMock(),
            on_early_suggestions=on_early_suggestions,
            assign_suggestion_link=eng.assign_id_and_update_link,
        )

    # 只早送新链
    assert [s.event for s in sent_sg] == ["老人摔倒"]
    # 心跳被裁:omni_output 只剩新链(merge 保留进 result 的也只有它)
    out = result.rooms["客厅"].device_results["cam-1"].omni_output
    assert len(out.suggestions) == 1
    assert out.suggestions[0].event == "老人摔倒"


# =============================================================================
# 流式 matched_rules 早出：注入 room_name / source_device_ids（_wrap_matched_rules_cb）
# =============================================================================


async def test_wrap_matched_rules_cb_injects_meta():
    """早出 matched_rules 外发前注入 room_name / source_device_ids。"""
    sent: list[list[MatchedRule]] = []

    async def cb(rules):
        sent.append(rules)

    wrapped = _wrap_matched_rules_cb(cb, "客厅", ["cam1"])
    r = MatchedRule(rule_id="r1", reason="有人看书")
    await wrapped([r])

    assert sent == [[r]]
    assert r.room_name == "客厅"
    assert r.source_device_ids == ["cam1"]


def test_wrap_matched_rules_cb_none_cb_returns_none():
    assert _wrap_matched_rules_cb(None, "客厅", ["cam1"]) is None


def test_inject_source_meta_covers_all_lists():
    """非流式路径：speeches / caption / suggestions / matched_rules 四类全部注入。"""
    from miloco.perception.engine.types import OmniOutput

    out = OmniOutput(
        caption=[CaptionEntry(description="有人")],
        matched_rules=[MatchedRule(rule_id="r1", reason="有人看书")],
        speeches=[Speech(needs_response=True, speaker="用户", content="开灯")],
        suggestions=[Suggestion(event="陌生人", action="提醒")],
    )
    _inject_source_meta(out, "客厅", ["cam1"])

    for it in (out.caption[0], out.matched_rules[0], out.speeches[0], out.suggestions[0]):
        assert it.room_name == "客厅"
        assert it.source_device_ids == ["cam1"]


class TestGateHoldPipelineIntegration:
    """Section 7.3 C 矩阵 — pipeline 多 device 状态隔离与 dict 写回。"""

    @pytest.mark.asyncio
    async def test_C1_multi_device_independent_hold(self, monkeypatch):
        """两 device:cam-A visual 通过、cam-B 全静止;cam-A dict 入项、cam-B 不入。"""
        gray = _solid(100, 100, 100)
        white = _solid(255, 255, 255)
        frames_with_change = [gray, gray, white, white, white, white]
        frames_static = [gray] * 6
        silent = np.zeros(16000, dtype=np.int16)

        snap_a = _make_snapshot("living-room", "cam-A", frames_with_change, silent)
        snap_b = _make_snapshot("living-room", "cam-B", frames_static, silent)
        batch = BatchedSnapshot(snapshots=[snap_a, snap_b])

        config = PerceptionConfig()
        config.omni.api_key = "test-key"
        contexts = {
            "cam-A": OmniContext(),
            "cam-B": OmniContext(),
        }
        tracking = MockTrackingService(create_default_mock_response())

        last_v: dict = {}
        last_a: dict = {}
        # 预置基准 → 两 cam 均非 cold-start,cam-B 静止才不入 last_v
        base = _preprocess(gray)
        gate_prev_frames = {"cam-A": base, "cam-B": base}

        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ):
            await run_batch_pipeline(
                batch, contexts, config,
                get_tracking_service=lambda did, room_name: tracking,
                gate_prev_frames=gate_prev_frames,
                gate_last_visual_pass_ts=last_v,
                gate_last_audio_pass_ts=last_a,
            )

        # cam-A visual 真通过 → last_v 入项,cam-B 全静(有基准)→ 无入项
        assert "cam-A" in last_v
        assert "cam-B" not in last_v
        # 两 cam audio 都未通过 → last_a 都不入
        assert last_a == {}

    @pytest.mark.asyncio
    async def test_C2_hold_pulls_packet_on_second_call(self, monkeypatch):
        """第一 batch visual 通过、第二 batch 全静 → 因 hold 仍生成 packet 走 omni。"""
        gray = _solid(100, 100, 100)
        white = _solid(255, 255, 255)
        silent = np.zeros(16000, dtype=np.int16)

        config = PerceptionConfig()
        config.omni.api_key = "test-key"
        contexts = {"cam-1": OmniContext()}
        tracking = MockTrackingService(create_default_mock_response())

        last_v: dict = {}
        last_a: dict = {}
        gate_prev_frames: dict = {}  # 跨 batch 共享:batch1 建基准,batch2 才非 cold-start

        # 第一 batch:visual 通过
        batch1 = BatchedSnapshot(snapshots=[
            _make_snapshot("r1", "cam-1", [gray, gray, white, white, white, white], silent),
        ])
        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ):
            await run_batch_pipeline(
                batch1, contexts, config,
                get_tracking_service=lambda did, room_name: tracking,
                gate_prev_frames=gate_prev_frames,
                gate_last_visual_pass_ts=last_v,
                gate_last_audio_pass_ts=last_a,
            )

        assert "cam-1" in last_v
        ts_first = last_v["cam-1"]

        # 第二 batch:全静止(与 batch1 末帧 white 一致,无跨窗变化),距上次 visual 通过 <1s → hold 拉起
        batch2 = BatchedSnapshot(snapshots=[
            _make_snapshot("r1", "cam-1", [white] * 6, silent),
        ])
        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ) as mock_omni:
            result = await run_batch_pipeline(
                batch2, contexts, config,
                get_tracking_service=lambda did, room_name: tracking,
                gate_prev_frames=gate_prev_frames,
                gate_last_visual_pass_ts=last_v,
                gate_last_audio_pass_ts=last_a,
            )

        # hold 期内 packet 仍生成 → omni 被调用
        assert mock_omni.called
        room = result.rooms["r1"]
        assert not room.skipped
        # last_v 未被刷新(本窗 visual 不通过)
        assert last_v["cam-1"] == ts_first

    @pytest.mark.asyncio
    async def test_C3_run_pipeline_no_hold(self):
        """on-demand 单设备 run_pipeline 不传 ts → hold 永远关。"""
        from miloco.perception.engine.input.video_splitter import create_input_slice
        from miloco.perception.engine.pipeline import run_pipeline

        gray = _solid(100, 100, 100)
        silent = np.zeros(16000, dtype=np.int16)
        slice_obj = create_input_slice("r1", [gray] * 6, silent)

        config = PerceptionConfig()
        config.omni.api_key = "test-key"

        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ):
            result = await run_pipeline(
                slice_obj, OmniContext(), config,
                tracking_service=MockTrackingService(create_default_mock_response()),
            )

        # run_pipeline 无 prev_frame → cold-start 放行(非 skip);但不传 last_v → hold 恒关
        assert not result.skipped
        assert result.gate_packet.trigger.visual_changed
        assert result.gate_packet.trigger.hold is False

    @pytest.mark.asyncio
    async def test_C4_room_timing_hold_pass_field(self):
        """room_timing[gate_hold_{did}_pass] 字段被填入。"""
        gray = _solid(100, 100, 100)
        white = _solid(255, 255, 255)
        silent = np.zeros(16000, dtype=np.int16)

        config = PerceptionConfig()
        config.omni.api_key = "test-key"
        contexts = {"cam-1": OmniContext()}
        tracking = MockTrackingService(create_default_mock_response())

        last_v: dict = {}
        last_a: dict = {}
        gate_prev_frames: dict = {}  # 跨 batch 共享:batch2 才非 cold-start、走 hold

        batch1 = BatchedSnapshot(snapshots=[
            _make_snapshot("r1", "cam-1", [gray, gray, white, white, white, white], silent),
        ])
        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ):
            result1 = await run_batch_pipeline(
                batch1, contexts, config,
                get_tracking_service=lambda did, room_name: tracking,
                gate_prev_frames=gate_prev_frames,
                gate_last_visual_pass_ts=last_v,
                gate_last_audio_pass_ts=last_a,
            )

        # 真通过窗口:gate_hold_cam-1_pass=0
        timing1 = result1.rooms["r1"].timing
        assert timing1.get("gate_hold_cam-1_pass") == 0

        # 第二 batch hold 拉起 → 字段=1（与 batch1 末帧 white 一致,无跨窗变化）
        batch2 = BatchedSnapshot(snapshots=[
            _make_snapshot("r1", "cam-1", [white] * 6, silent),
        ])
        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ):
            result2 = await run_batch_pipeline(
                batch2, contexts, config,
                get_tracking_service=lambda did, room_name: tracking,
                gate_prev_frames=gate_prev_frames,
                gate_last_visual_pass_ts=last_v,
                gate_last_audio_pass_ts=last_a,
            )

        timing2 = result2.rooms["r1"].timing
        assert timing2.get("gate_hold_cam-1_pass") == 1


class TestGateHoldStateTransitionLogs:
    """gate hold 状态转换日志 + events 表事件(对照 rule_runner f8167431 风格)。"""

    @pytest.mark.asyncio
    async def test_hold_start_logged_when_visual_stops(self, caplog):
        """visual 真通过 → 下窗静止 → HOLD_START 日志 + gate_hold_start 事件。"""
        import logging
        gray = _solid(100, 100, 100)
        white = _solid(255, 255, 255)
        silent = np.zeros(16000, dtype=np.int16)

        config = PerceptionConfig()
        config.omni.api_key = "test-key"
        contexts = {"cam-1": OmniContext()}
        tracking = MockTrackingService(create_default_mock_response())

        last_v: dict = {}
        last_a: dict = {}
        hold_active: dict = {}
        hold_started: dict = {}
        gate_prev_frames: dict = {}  # 跨 batch 共享:batch2 才非 cold-start、走 hold

        # 第一 batch:visual 真通过
        batch1 = BatchedSnapshot(snapshots=[
            _make_snapshot("r1", "cam-1", [gray, gray, white, white, white, white], silent),
        ])
        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ):
            await run_batch_pipeline(
                batch1, contexts, config,
                get_tracking_service=lambda did, room_name: tracking,
                gate_prev_frames=gate_prev_frames,
                gate_last_visual_pass_ts=last_v,
                gate_last_audio_pass_ts=last_a,
                gate_hold_active=hold_active,
                gate_hold_started_at=hold_started,
            )
        # 第一窗 hold_pass=False
        assert hold_active.get("cam-1") is False

        # 第二 batch:全静止(与 batch1 末帧 white 一致,无跨窗变化）→ hold 拉起 → 应打 HOLD_START
        caplog.clear()
        caplog.set_level(logging.INFO, logger="miloco.perception.engine.pipeline")
        batch2 = BatchedSnapshot(snapshots=[
            _make_snapshot("r1", "cam-1", [white] * 6, silent),
        ])
        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ):
            await run_batch_pipeline(
                batch2, contexts, config,
                get_tracking_service=lambda did, room_name: tracking,
                gate_prev_frames=gate_prev_frames,
                gate_last_visual_pass_ts=last_v,
                gate_last_audio_pass_ts=last_a,
                gate_hold_active=hold_active,
                gate_hold_started_at=hold_started,
            )
        assert hold_active["cam-1"] is True
        assert "cam-1" in hold_started
        assert any("HOLD_START" in r.message and "cam-1" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_hold_recovered_logged_when_visual_returns(self, caplog):
        """hold 期内 visual 再次通过 → HOLD_RECOVERED 日志 + held_for_ms。"""
        import logging
        gray = _solid(100, 100, 100)
        white = _solid(255, 255, 255)
        silent = np.zeros(16000, dtype=np.int16)

        config = PerceptionConfig()
        config.omni.api_key = "test-key"
        contexts = {"cam-1": OmniContext()}
        tracking = MockTrackingService(create_default_mock_response())

        last_v: dict = {}
        last_a: dict = {}
        hold_active = {"cam-1": True}  # 假装上窗已 hold
        hold_started = {"cam-1": time.monotonic() - 1.0}  # 1s 前进入 hold

        # 本窗:visual 真通过
        batch = BatchedSnapshot(snapshots=[
            _make_snapshot("r1", "cam-1", [gray, gray, white, white, white, white], silent),
        ])
        caplog.set_level(logging.INFO, logger="miloco.perception.engine.pipeline")
        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ):
            await run_batch_pipeline(
                batch, contexts, config,
                get_tracking_service=lambda did, room_name: tracking,
                gate_last_visual_pass_ts=last_v,
                gate_last_audio_pass_ts=last_a,
                gate_hold_active=hold_active,
                gate_hold_started_at=hold_started,
            )
        assert hold_active["cam-1"] is False
        assert "cam-1" not in hold_started  # 已清理
        recovered_logs = [r for r in caplog.records if "HOLD_RECOVERED" in r.message]
        assert len(recovered_logs) == 1
        assert "cam-1" in recovered_logs[0].message
        assert "held_for_ms" in recovered_logs[0].message
        assert "visual_score" in recovered_logs[0].message

    @pytest.mark.asyncio
    async def test_hold_expired_logged_when_360s_elapsed(self, caplog):
        """hold 资格因 last_visual_pass_ts 距今 > hold_duration_sec 失效,
        本窗 visual 仍未通过 → HOLD_EXPIRED 日志 + held_for_ms,而非 RECOVERED。
        """
        import logging
        gray = _solid(100, 100, 100)
        silent = np.zeros(16000, dtype=np.int16)

        config = PerceptionConfig()
        config.omni.api_key = "test-key"
        contexts = {"cam-1": OmniContext()}
        tracking = MockTrackingService(create_default_mock_response())

        now = time.monotonic()
        # 上次 visual 真通过在 hold_duration_sec + 余量之前 → 本窗 hold 资格失效
        last_v: dict = {"cam-1": now - (config.gate.hold_duration_sec + 10)}
        last_a: dict = {}
        hold_active = {"cam-1": True}                    # 上窗在 hold 中
        hold_started = {"cam-1": now - config.gate.hold_duration_sec}  # 接近上限
        gate_prev_frames = {"cam-1": _preprocess(gray)}  # 有基准 → 非 cold-start

        # 本窗静止 + 无声 + 有基准:visual_changed=False, audio_active=False,
        # hold_active=False(超时) → cur_hold=False, cur_visual_pass=False
        # 进 elif prev_hold and not cur_hold + 走 EXPIRED 分支(非 RECOVERED)
        batch = BatchedSnapshot(snapshots=[
            _make_snapshot("r1", "cam-1", [gray] * 6, silent),
        ])
        caplog.set_level(logging.INFO, logger="miloco.perception.engine.pipeline")
        with patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=MOCK_OMNI_RESPONSE,
        ):
            await run_batch_pipeline(
                batch, contexts, config,
                get_tracking_service=lambda did, room_name: tracking,
                gate_prev_frames=gate_prev_frames,
                gate_last_visual_pass_ts=last_v,
                gate_last_audio_pass_ts=last_a,
                gate_hold_active=hold_active,
                gate_hold_started_at=hold_started,
            )
        assert hold_active["cam-1"] is False
        assert "cam-1" not in hold_started
        expired_logs = [r for r in caplog.records if "HOLD_EXPIRED" in r.message]
        recovered_logs = [r for r in caplog.records if "HOLD_RECOVERED" in r.message]
        assert len(expired_logs) == 1
        assert len(recovered_logs) == 0  # 必须走 EXPIRED 不是 RECOVERED
        assert "cam-1" in expired_logs[0].message
        assert "held_for_ms" in expired_logs[0].message


# =============================================================================
# Agent-facing 时刻锚定部署时区（_fmt_time_window / _fmt_clock）
#
# 回归 bug：host TZ=UTC、部署时区=Asia/Shanghai 时，感知推送的「时间」字段与注入
# omni 的「当前时间」若用裸 fromtimestamp 会显示 UTC 时刻（把北京 10:52 标成 02:52），
# 导致 agent / 模型编造出「凌晨」等错误时段。二者现均走 deploy_timezone()。
# =============================================================================

# 2023-11-14T22:13:20Z —— 三个时区落在不同 HH，转换正确性可判别：
#   UTC → 22:13:20 / Asia/Shanghai(+08) → 次日 06:13:20 / America/Los_Angeles(PST -08) → 14:13:20
_FIXED_MS = 1_700_000_000_000


@pytest.fixture
def _reset_settings_around():
    """MILOCO_TIMEZONE 用例前后清 settings 缓存，避免污染其它用例。"""
    from miloco.config import reset_settings

    reset_settings()
    yield
    reset_settings()


def test_fmt_time_window_uses_deploy_timezone(monkeypatch, _reset_settings_around):
    """settings.timezone 非 host 时区时，时间窗按部署时区转换（非进程/OS 时钟）。"""
    from miloco.config import reset_settings
    from miloco.perception.engine.pipeline import _fmt_time_window

    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
    reset_settings()
    assert _fmt_time_window(_FIXED_MS, _FIXED_MS + 5000) == "[06:13:20-06:13:25]"

    monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
    reset_settings()
    assert _fmt_time_window(_FIXED_MS, _FIXED_MS + 5000) == "[14:13:20-14:13:25]"


def test_fmt_clock_uses_deploy_timezone(monkeypatch, _reset_settings_around):
    """注入 omni 的「当前时间」按部署时区转换（对齐 _fmt_time_window，非 host 时钟）。"""
    from miloco.config import reset_settings
    from miloco.perception.engine.api import _fmt_clock

    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
    reset_settings()
    assert _fmt_clock(_FIXED_MS) == "06:13:20"

    monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
    reset_settings()
    assert _fmt_clock(_FIXED_MS) == "14:13:20"


def test_display_falls_back_to_os_local_not_shanghai(monkeypatch, tmp_path, _reset_settings_around):
    """(f) 条款回归：settings 未配 + 系统 IANA 反查全失败 → 展示按 OS 本地钟。

    旧兜底猜 Asia/Shanghai 会把"OS 时钟正确、只是反查不出 IANA 名"的非中国宿主
    (docker 普通文件 /etc/localtime 等)恒偏成北京时间；展示路径必须退到 OS 本地。
    """
    import time as _time
    from datetime import datetime

    from miloco.config import reset_settings
    from miloco.perception.engine.api import _fmt_clock
    from miloco.perception.engine.pipeline import _fmt_time_window
    from miloco.utils import time_utils

    monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
    # 隔离 MILOCO_HOME：不读本机真实 config.json 的 timezone
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path / "empty-home"))
    reset_settings()
    monkeypatch.setattr(time_utils, "_system_iana_tz", lambda: None)
    monkeypatch.setattr(time_utils, "_warned_no_iana", False)

    # 用当前时刻：OS 本地格式化(裸 fromtimestamp)与"当前偏移"必然一致,不受 DST 干扰
    now_ms = int(_time.time() * 1000)
    expected = datetime.fromtimestamp(now_ms / 1000).strftime("%H:%M:%S")
    assert _fmt_clock(now_ms) == expected
    assert _fmt_time_window(now_ms, now_ms) == f"[{expected}-{expected}]"
