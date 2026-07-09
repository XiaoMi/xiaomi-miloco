"""流式视频测试：模拟视频流方式调用 perception 管线，逐窗口评测。

用法：
    cd server/src/perception

    # 快速跑通（mock omni，不等待）
    .venv/bin/python -m tests.run_stream_test engine/test.mp4 --mock-omni --no-wait

    # 完整管线（含真实 Omni API）
    .venv/bin/python -m tests.run_stream_test engine/test.mp4 --no-wait

    # 模拟实时推送（每窗口间隔 3s）
    .venv/bin/python -m tests.run_stream_test engine/test.mp4 --mock-omni

    # 自定义参数
    .venv/bin/python -m tests.run_stream_test engine/test.mp4 --fps 3 --period 3 --mock-omni --no-wait
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from miloco.perception.engine.config import (
    GateConfig,
    IdentityConfig,
    InputConfig,
    PerceptionConfig,
)
from miloco.perception.engine.gate.gate import run_gate
from miloco.perception.engine.identity.cropper import crop_targets
from miloco.perception.engine.identity.frame_selector import select_frames
from miloco.perception.engine.identity.motion_analyzer import analyze_motion
from miloco.perception.engine.identity.tracking_service import create_tracking_service
from miloco.perception.engine.omni.omni_client import call_omni
from miloco.perception.engine.omni.prompt_builder import build_prompt
from miloco.perception.engine.omni.response_parser import parse_omni_response
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    IdentityPacket,
    InputSlice,
    OmniContext,
    RuleCondition,
    SelectedFrame,
)
from miloco.perception.types import (
    AudioFrame,
    AudioStream,
    PerceptionDevice,
    VideoFrame,
    VideoStream,
)
from numpy.typing import NDArray

# ---------------------------------------------------------------------------
# Mock omni response
# ---------------------------------------------------------------------------
MOCK_OMNI_RESPONSE = {
    "id": "mock",
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {
                        "caption": "[MOCK] 场景描述占位",
                        "matched_rules": [],
                        "speeches": [],
                        "suggestions": [],
                    }
                ),
            },
        }
    ],
}


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------
@dataclass
class StageTimer:
    """Records latency for each pipeline stage."""

    downsample_ms: float = 0.0
    gate_ms: float = 0.0
    tracking_ms: float = 0.0
    motion_ms: float = 0.0
    frame_select_ms: float = 0.0
    crop_ms: float = 0.0
    audio_analyze_ms: float = 0.0
    video_encode_ms: float = 0.0
    omni_prompt_ms: float = 0.0
    omni_api_ms: float = 0.0
    omni_parse_ms: float = 0.0
    total_ms: float = 0.0

    @property
    def edge_total_ms(self) -> float:
        return self.tracking_ms + self.motion_ms + self.frame_select_ms + self.crop_ms + self.audio_analyze_ms

    @property
    def omni_total_ms(self) -> float:
        return self.video_encode_ms + self.omni_prompt_ms + self.omni_api_ms + self.omni_parse_ms


# ---------------------------------------------------------------------------
# Video stream simulator
# ---------------------------------------------------------------------------
def extract_full_audio(file_path: str, sample_rate: int = 16000) -> NDArray[np.int16]:
    """Extract full audio track as 16kHz mono PCM int16 using PyAV."""
    try:
        import av

        container = av.open(file_path)
        audio_stream = next((s for s in container.streams if s.type == "audio"), None)
        if audio_stream is None:
            container.close()
            return np.array([], dtype=np.int16)

        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        chunks: list[NDArray[np.int16]] = []

        for frame in container.decode(audio=0):
            resampled = resampler.resample(frame)
            for r in resampled:
                arr = r.to_ndarray().flatten().astype(np.int16)
                chunks.append(arr)

        container.close()
        if not chunks:
            return np.array([], dtype=np.int16)
        return np.concatenate(chunks)
    except Exception as e:
        print(f"  [WARN] 音频提取失败: {e}")
        return np.array([], dtype=np.int16)


_WORKING_SIZE = (1280, 720)


def simulate_stream(
    video_path: str, fps: int, period_sec: int, *, native_fps: bool = False, audio_overlap_ms: int = 100
) -> list[tuple[InputSlice, float]]:
    """Read video and split into windows of (period_sec * fps) frames.

    Frames are resized to 1280x720 at input stage so all downstream
    stages (Gate, Tracker, Cropper) work in a unified coordinate space.

    Args:
        native_fps: If True, use video's original fps (no downsampling).

    Returns list of (InputSlice, window_start_sec).
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    video_fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    video_duration = total_frames_count / video_fps if video_fps > 0 else 0
    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if native_fps:
        # Read all frames but downsample per window to target fps for pipeline
        sample_interval = 1
        effective_fps = video_fps
    else:
        effective_fps = fps
        sample_interval = max(1, int(video_fps / fps))
    frames_per_window = int(effective_fps * period_sec)

    print(f"  视频 FPS: {video_fps:.1f}, 总帧数: {total_frames_count}, 时长: {video_duration:.1f}s")
    print(f"  原始分辨率: {orig_w}x{orig_h} → 工作分辨率: {_WORKING_SIZE[0]}x{_WORKING_SIZE[1]}")
    print(f"  采样间隔: 每 {sample_interval} 帧取 1 帧, 每窗口 {frames_per_window} 帧")

    # Read all sampled frames, resize to working resolution
    all_frames: list[NDArray[np.uint8]] = []
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % sample_interval == 0:
            frame = cv2.resize(frame, _WORKING_SIZE)
            all_frames.append(frame)
        frame_idx += 1
    cap.release()

    print(f"  采样后总帧数: {len(all_frames)}")

    # Extract full audio once, then slice per window
    sample_rate = 16000
    overlap_samples = int(audio_overlap_ms / 1000 * sample_rate)
    full_audio = extract_full_audio(video_path, sample_rate)
    has_audio = len(full_audio) > 0
    if has_audio:
        overlap_info = f", 重叠 {audio_overlap_ms}ms ({overlap_samples} 样本)" if overlap_samples > 0 else ""
        print(f"  音频: {len(full_audio)} 样本, {len(full_audio) / sample_rate:.1f}s{overlap_info}")
    else:
        print("  音频: 无")

    # Split into windows
    windows: list[tuple[InputSlice, float]] = []
    for win_idx in range(0, len(all_frames), frames_per_window):
        win_frames = all_frames[win_idx : win_idx + frames_per_window]
        if len(win_frames) < 2:
            break

        # Note: frame downsampling is handled by pipeline.downsample_snapshot()

        window_start_sec = win_idx / effective_fps

        # Slice audio for this window, prepend overlap from previous window boundary
        if has_audio:
            audio_end = int((window_start_sec + period_sec) * sample_rate)
            if win_idx > 0 and overlap_samples > 0:
                audio_start = max(0, int(window_start_sec * sample_rate) - overlap_samples)
            else:
                audio_start = int(window_start_sec * sample_rate)
            audio = full_audio[audio_start:audio_end]
        else:
            audio = np.array([], dtype=np.int16)

        now = time.time() * 1000
        device = PerceptionDevice(
            did="test-room",
            name="test-room",
            device_type="camera",
            room_name="test-room",
        )
        video_frames = [
            VideoFrame(data=f, timestamp=now - (len(win_frames) - i) / fps * 1000) for i, f in enumerate(win_frames)
        ]
        h, w = win_frames[0].shape[:2] if win_frames else (0, 0)
        video_stream = VideoStream(frames=video_frames, width=w, height=h) if video_frames else None
        audio_stream = (
            AudioStream(frames=[AudioFrame(data=audio, timestamp=now - period_sec * 1000)]) if audio.size > 0 else None
        )
        input_slice = InputSlice(
            device=device,
            start_timestamp=now - period_sec * 1000,
            end_timestamp=now,
            video=video_stream,
            audio=audio_stream,
        )
        windows.append((input_slice, window_start_sec))

    return windows


# ---------------------------------------------------------------------------
# Pipeline with per-stage timing
# ---------------------------------------------------------------------------
async def run_pipeline_with_timing(
    input_slice: InputSlice,
    context: OmniContext,
    config: PerceptionConfig,
    tracking_service,
    mock_omni: bool,
) -> tuple[dict, StageTimer]:
    """Run pipeline step by step with timing for each stage."""
    timer = StageTimer()
    result: dict = {
        "input_slice": input_slice,
        "gate_packet": None,
        "identity_packet": None,
        "omni_output": None,
        "skipped": False,
        "tracking_response": None,
    }

    total_start = time.monotonic()

    # --- Downsample to target fps ---
    from miloco.perception.engine.pipeline import downsample_snapshot

    t0 = time.monotonic()
    input_slice = downsample_snapshot(input_slice, config.input.fps)
    timer.downsample_ms = (time.monotonic() - t0) * 1000

    # --- Gate ---
    t0 = time.monotonic()
    # 流式 ad-hoc 入口无跨窗状态(prev_frame / hold ts),丢弃后 3 个返回项
    gate_packet, _, _, _, _ = await run_gate(input_slice, config.gate)
    timer.gate_ms = (time.monotonic() - t0) * 1000
    result["gate_packet"] = gate_packet

    if gate_packet is None:
        result["skipped"] = True
        timer.total_ms = (time.monotonic() - total_start) * 1000
        return result, timer

    # --- Edge: tracking ---
    t0 = time.monotonic()
    tracking_resp = tracking_service.analyze(gate_packet.frames, fps=3)
    timer.tracking_ms = (time.monotonic() - t0) * 1000
    result["tracking_response"] = tracking_resp

    # --- Edge: motion ---
    t0 = time.monotonic()
    targets, scene_motion = analyze_motion(tracking_resp.object_info, config.identity)
    timer.motion_ms = (time.monotonic() - t0) * 1000

    # --- Edge: frame selection ---
    t0 = time.monotonic()
    all_box_info = [t.box_info for t in targets]
    frame_selections = select_frames(len(gate_packet.frames), scene_motion, all_box_info, config.identity)
    timer.frame_select_ms = (time.monotonic() - t0) * 1000

    # --- Edge: crop ---
    t0 = time.monotonic()
    selected_frames: list[SelectedFrame] = []
    for frame_idx, resolution in frame_selections:
        if frame_idx >= len(gate_packet.frames):
            continue
        frame_image = gate_packet.frames[frame_idx]
        target_dicts = [{"track_id": t.track_id, "box_info": t.box_info} for t in targets]
        crops = crop_targets(frame_image, frame_idx, target_dicts, resolution, config.identity)
        selected_frames.append(
            SelectedFrame(
                frame_index=frame_idx,
                image=frame_image,
                resolution=resolution,
                crops=crops,
            )
        )
    timer.crop_ms = (time.monotonic() - t0) * 1000

    # Audio analysis skipped — Omni handles audio directly from video

    identity_packet = IdentityPacket(
        packet_id=str(uuid.uuid4()),
        room_name=gate_packet.room_name,
        timestamp=gate_packet.timestamp,
        frame_info=tracking_resp.frame_info,
        targets=targets,
        scene_motion=scene_motion,
        frames=selected_frames,
        all_frames=gate_packet.frames,
        audio_clip=gate_packet.audio_clip,
        audio_analysis=AudioAnalysis(type=AudioType.SILENCE, is_urgent=False, energy_level=0.0),
        sample_rate=gate_packet.sample_rate,
    )
    result["identity_packet"] = identity_packet

    # --- Omni ---
    # --- Omni: video encode + prompt ---
    from miloco.perception.engine.omni.prompt_builder import _encode_video

    t0 = time.monotonic()
    _video_b64, _media_info = _encode_video(identity_packet)
    timer.video_encode_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    payload = build_prompt(identity_packet, context)
    # Replace video with pre-encoded one (avoid double encoding in timing)
    timer.omni_prompt_ms = (time.monotonic() - t0) * 1000 - timer.video_encode_ms  # subtract re-encoding

    if mock_omni:
        t0 = time.monotonic()
        raw_resp = MOCK_OMNI_RESPONSE
        timer.omni_api_ms = (time.monotonic() - t0) * 1000

        t0 = time.monotonic()
        omni_output = parse_omni_response(raw_resp)
        timer.omni_parse_ms = (time.monotonic() - t0) * 1000
    else:
        t0 = time.monotonic()
        raw_resp = await call_omni(payload, config.omni)
        timer.omni_api_ms = (time.monotonic() - t0) * 1000
        result["omni_raw_response"] = raw_resp

        t0 = time.monotonic()
        omni_output = parse_omni_response(raw_resp)
        timer.omni_parse_ms = (time.monotonic() - t0) * 1000

    video_size = len(payload.get("video_base64", "") or "") * 3 // 4  # approx bytes
    crop_count = len(payload.get("crops", []))
    result["video_size_bytes"] = video_size
    result["crop_count"] = crop_count
    usage = raw_resp.get("usage", {}) if isinstance(raw_resp, dict) else {}
    result["omni_usage"] = usage

    result["omni_output"] = omni_output
    timer.total_ms = (time.monotonic() - total_start) * 1000

    return result, timer


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def print_divider(char: str = "=", width: int = 78):
    print(char * width)


def print_window_result(
    win_idx: int,
    win_start: float,
    result: dict,
    timer: StageTimer,
    *,
    debug: bool = False,
):
    print_divider()
    print(f"  窗口 #{win_idx + 1}  |  视频位置: {win_start:.1f}s - {win_start + 3:.1f}s")
    print_divider("-")

    s = result["input_slice"]
    print(
        f"\n[输入]  帧数={len(s.frames)}, "
        f"帧尺寸={s.frames[0].shape[1]}x{s.frames[0].shape[0] if s.frames else 'N/A'}, "
        f"音频样本={len(s.audio_clip)}"
    )

    # Downsample + Gate
    print(f"\n[下采样] {timer.downsample_ms:.1f}ms → {len(result['input_slice'].frames)}帧")
    print(f"[Gate]  耗时={timer.gate_ms:.1f}ms")
    if result["skipped"]:
        print("  结果: SKIPPED（无变化）")
        print(f"\n  总耗时: {timer.total_ms:.1f}ms")
        return

    gt = result["gate_packet"].trigger
    print(f"  视觉: {'YES' if gt.visual_changed else 'NO'} (score={gt.visual_change_score:.6f})")
    print(f"  音频: {'YES' if gt.audio_active else 'NO'} (energy={gt.audio_energy_level:.6f})")

    # Edge
    ep = result["identity_packet"]
    tr = result["tracking_response"]
    print(f"\n[Edge]  总耗时={timer.edge_total_ms:.1f}ms")
    print(f"  tracking:     {timer.tracking_ms:.1f}ms  →  {len(tr.object_info)} 个原始目标")
    for obj in tr.object_info:
        print(
            f"    - type={obj.type.value}, face_id={obj.face_id}, track_id={obj.track_id}, bbox帧数={len(obj.box_info)}"
        )
        # Validate bbox coordinates
        if obj.box_info:
            bi = obj.box_info[0]
            frame_h, frame_w = s.frames[0].shape[:2]
            for box_type, (bx, by, bw, bh) in bi.boxes.items():
                in_bounds = 0 <= bx < frame_w and 0 <= by < frame_h
                tag = "" if in_bounds else " [OUT OF BOUNDS!]"
                print(f"      {box_type}: ({bx},{by},{bw},{bh}){tag}")

    print(f"  motion:       {timer.motion_ms:.1f}ms  →  场景={ep.scene_motion.value}")
    for t in ep.targets:
        verify = " [需Omni验证]" if t.needs_omni_verify else ""
        print(f"    - {t.type.value} id={t.person_id}{verify}")

    print(f"  frame_select: {timer.frame_select_ms:.1f}ms  →  {len(ep.frames)} 帧")
    print(f"  crop:         {timer.crop_ms:.1f}ms")
    for f in ep.frames:
        print(f"    frame[{f.frame_index}]: {f.resolution.value}, crops={len(f.crops)}")
        for c in f.crops:
            print(f"      track={c.track_id}: {c.image.shape[1]}x{c.image.shape[0]}")

    print("  audio:        (VAD跳过，Omni直接处理)")

    # Speech accumulation state
    # Audio type summary
    audio_label = {
        "speech": "→ 人声（含在视频中）",
        "non_speech": "→ 非人声（含在视频中）",
        "silence": "→ 静音",
    }.get(ep.audio_analysis.type.value, ep.audio_analysis.type.value)
    print(f"  audio_route: {audio_label}")

    # Omni
    oo = result["omni_output"]
    if oo:
        usage = result.get("omni_usage", {})
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens)
        video_kb = result.get("video_size_bytes", 0) / 1024
        crop_count = result.get("crop_count", 0)

        print(f"\n[Omni]  总耗时={timer.omni_total_ms:.1f}ms")
        print(
            f"  video: {video_kb:.0f}KB  |  crops: {crop_count}  |  tokens: {input_tokens} in + {output_tokens} out = {total_tokens}"
        )
        print(f"  video_encode: {timer.video_encode_ms:.1f}ms")
        print(f"  prompt:       {timer.omni_prompt_ms:.1f}ms")
        print(f"  api:          {timer.omni_api_ms:.1f}ms")
        print(f"  parse:        {timer.omni_parse_ms:.1f}ms")
        for env in oo.caption:
            print(f"  环境: [{env.room_name}] {env.description}")
        for rule in oo.matched_rules:
            print(f"  规则: [{rule.rule_id}] {rule.reason}")
        for interaction in oo.speeches:
            status_tag = " ⚠️INCOMPLETE" if not interaction.is_complete else ""
            needs = "✓" if interaction.needs_response else "-"
            print(f"  交互: [{needs}:{interaction.speaker}] {interaction.content}{status_tag}")
        for sug in oo.suggestions:
            print(f"  建议: {sug.event} → {sug.action}")

        # Debug: model raw output
        if debug and result.get("omni_raw_response"):
            raw_content = result["omni_raw_response"].get("choices", [{}])[0].get("message", {}).get("content", "")
            is_failed = oo.caption and "解析失败" in oo.caption[0].description
            if is_failed or debug:
                print(f"\n  [DEBUG] 模型原始输出 ({len(raw_content)} chars):")
                # Print first and last 300 chars
                if len(raw_content) <= 600:
                    for line in raw_content.split("\n"):
                        print(f"    {line}")
                else:
                    print("    --- 前 300 字 ---")
                    for line in raw_content[:300].split("\n"):
                        print(f"    {line}")
                    print("    --- ... ---")
                    print("    --- 后 300 字 ---")
                    for line in raw_content[-300:].split("\n"):
                        print(f"    {line}")

                if is_failed:
                    from miloco.perception.engine.omni.response_parser import (
                        extract_json,
                    )

                    extracted = extract_json(raw_content)
                    print(f"  [DEBUG] extract_json 结果 ({len(extracted)} chars):")
                    print(f"    {extracted[:300]}")

    # Timing summary
    print(
        f"\n  总耗时: {timer.total_ms:.1f}ms "
        f"(ds={timer.downsample_ms:.0f} + gate={timer.gate_ms:.0f} + edge={timer.edge_total_ms:.0f} "
        f"+ video={timer.video_encode_ms:.0f} + api={timer.omni_api_ms:.0f} + parse={timer.omni_parse_ms:.0f})"
    )


def print_summary(timers: list[StageTimer], total_windows: int, skipped: int):
    print_divider("=")
    print("\n  汇总")
    print_divider("-")
    triggered = total_windows - skipped
    print(f"  总窗口: {total_windows}  |  触发: {triggered}  |  跳过: {skipped}")

    if not timers:
        return

    def _stats(values):
        return f"avg={sum(values) / len(values):.1f}  min={min(values):.1f}  max={max(values):.1f}"

    print("\n  延迟统计 (ms, 仅触发窗口):")
    print(f"    Downsample:   {_stats([t.downsample_ms for t in timers])}")
    print(f"    Gate:         {_stats([t.gate_ms for t in timers])}")
    print(f"    Tracking:     {_stats([t.tracking_ms for t in timers])}")
    print(f"    Motion:       {_stats([t.motion_ms for t in timers])}")
    print(f"    FrameSelect:  {_stats([t.frame_select_ms for t in timers])}")
    print(f"    Crop:         {_stats([t.crop_ms for t in timers])}")
    print(f"    AudioAnalyze: {_stats([t.audio_analyze_ms for t in timers])}")
    print(f"    Edge Total:   {_stats([t.edge_total_ms for t in timers])}")
    print(f"    VideoEncode:  {_stats([t.video_encode_ms for t in timers])}")
    print(f"    OmniPrompt:   {_stats([t.omni_prompt_ms for t in timers])}")
    print(f"    OmniAPI:      {_stats([t.omni_api_ms for t in timers])}")
    print(f"    OmniParse:    {_stats([t.omni_parse_ms for t in timers])}")
    print(f"    Omni Total:   {_stats([t.omni_total_ms for t in timers])}")
    print(f"    E2E Total:    {_stats([t.total_ms for t in timers])}")


# ---------------------------------------------------------------------------
# Save artifacts
# ---------------------------------------------------------------------------
def save_window_artifacts(
    output_dir: str,
    win_idx: int,
    win_start: float,
    result: dict,
    timer: StageTimer,
    context: OmniContext | None = None,
):
    """Save images and metadata for a single window."""
    import json as _json

    win_dir = os.path.join(output_dir, f"window_{win_idx:03d}_{win_start:.0f}s")
    os.makedirs(win_dir, exist_ok=True)

    input_slice: InputSlice = result["input_slice"]

    # Save input frames (resized to 1280 width for space)
    for fi, frame in enumerate(input_slice.frames):
        h, w = frame.shape[:2]
        scale = min(1.0, 1280 / w)
        small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1.0 else frame
        cv2.imwrite(os.path.join(win_dir, f"input_frame_{fi:02d}.jpg"), small)

    if result["skipped"]:
        # Save minimal metadata
        meta = {
            "window_idx": win_idx,
            "time_range": f"{win_start:.1f}s - {win_start + 3:.1f}s",
            "gate": "SKIPPED",
            "latency_ms": {"gate": timer.gate_ms, "total": timer.total_ms},
        }
        with open(os.path.join(win_dir, "metadata.json"), "w") as f:
            _json.dump(meta, f, indent=2, ensure_ascii=False)
        return

    gate_packet = result["gate_packet"]
    tracking_resp = result["tracking_response"]
    identity_packet = result["identity_packet"]
    omni_output = result["omni_output"]

    # Save annotated panoramic frame (first selected frame with bboxes)
    if identity_packet and identity_packet.frames and tracking_resp:
        sel = identity_packet.frames[0]
        vis = sel.image.copy()
        colors = [(0, 255, 0), (0, 0, 255), (255, 0, 0), (255, 255, 0), (0, 255, 255)]
        for oi, obj in enumerate(tracking_resp.object_info):
            color = colors[oi % len(colors)]
            for bi in obj.box_info:
                if bi.frame_index != sel.frame_index:
                    continue
                for _box_type, (bx, by, bw, bh) in bi.boxes.items():
                    cv2.rectangle(vis, (bx, by), (bx + bw, by + bh), color, 4)
                    label = f"{obj.type.value} face={obj.face_id} t={obj.track_id}"
                    cv2.putText(
                        vis,
                        label,
                        (bx, max(by - 10, 20)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1.2,
                        color,
                        3,
                    )

        h, w = vis.shape[:2]
        scale = min(1.0, 1920 / w)
        vis_small = cv2.resize(vis, (int(w * scale), int(h * scale))) if scale < 1.0 else vis
        cv2.imwrite(os.path.join(win_dir, "annotated_panoramic.jpg"), vis_small)

    # Save all crop images
    if identity_packet:
        for sf in identity_packet.frames:
            for crop in sf.crops:
                fname = f"crop_frame{sf.frame_index:02d}_track{crop.track_id}.jpg"
                cv2.imwrite(os.path.join(win_dir, fname), crop.image)

    # Save omni prompt and raw response (if available)
    if identity_packet:
        prompt_payload = build_prompt(identity_packet, context)
        with open(os.path.join(win_dir, "omni_system_prompt.txt"), "w") as f:
            f.write(prompt_payload["system_prompt"])
        with open(os.path.join(win_dir, "omni_user_content.txt"), "w") as f:
            f.write(prompt_payload["user_content"])
        # Save image count info (without base64 data)
        with open(os.path.join(win_dir, "omni_images_info.json"), "w") as f:
            img_info = [
                {"media_type": img["media_type"], "size_bytes": len(img["data"])}
                for img in prompt_payload.get("images", [])
            ]
            _json.dump(img_info, f, indent=2)

    if result.get("omni_raw_response"):
        with open(os.path.join(win_dir, "omni_raw_response.json"), "w") as f:
            _json.dump(result["omni_raw_response"], f, indent=2, ensure_ascii=False)

        # Save model raw content text
        raw_content = result["omni_raw_response"].get("choices", [{}])[0].get("message", {}).get("content", "")
        with open(os.path.join(win_dir, "omni_raw_output.txt"), "w") as f:
            f.write(raw_content)

        # Save parser debug info
        from miloco.perception.engine.omni.response_parser import extract_json

        extracted = extract_json(raw_content)
        parse_ok = False
        parse_error = None
        try:
            import json as _j2

            _j2.loads(extracted)
            parse_ok = True
        except Exception as e:
            parse_error = str(e)

        with open(os.path.join(win_dir, "omni_parser_debug.json"), "w") as f:
            _json.dump(
                {
                    "raw_content_length": len(raw_content),
                    "raw_content_preview": raw_content[:300],
                    "extracted_json_length": len(extracted),
                    "extracted_json_preview": extracted[:500],
                    "parse_success": parse_ok,
                    "parse_error": parse_error,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

    # Save metadata JSON
    meta = {
        "window_idx": win_idx,
        "time_range": f"{win_start:.1f}s - {win_start + 3:.1f}s",
        "frame_count": len(input_slice.frames),
        "frame_size": f"{input_slice.frames[0].shape[1]}x{input_slice.frames[0].shape[0]}"
        if input_slice.frames
        else "N/A",
        "gate": {
            "result": "TRIGGERED",
            "visual_changed": bool(gate_packet.trigger.visual_changed),
            "visual_score": float(gate_packet.trigger.visual_change_score),
            "audio_active": bool(gate_packet.trigger.audio_active),
            "audio_energy": float(gate_packet.trigger.audio_energy_level),
        },
        "edge": {
            "scene_motion": identity_packet.scene_motion.value if identity_packet else None,
            "targets": [
                {
                    "type": t.type.value,
                    "person_id": t.person_id,
                    "track_id": t.track_id,
                    "needs_verify": t.needs_omni_verify,
                    "bbox_frames": len(t.box_info),
                }
                for t in (identity_packet.targets if identity_packet else [])
            ],
            "selected_frames": len(identity_packet.frames) if identity_packet else 0,
            "total_crops": sum(len(f.crops) for f in identity_packet.frames) if identity_packet else 0,
            "audio_type": identity_packet.audio_analysis.type.value if identity_packet else None,
            "audio_urgent": bool(identity_packet.audio_analysis.is_urgent) if identity_packet else None,
        },
        "omni": {
            "image_count": result.get("omni_image_count", 0),
            "usage": result.get("omni_usage", {}),
            "caption": [{"room_name": e.room_name, "description": e.description} for e in omni_output.caption]
            if omni_output
            else [],
        },
        "latency_ms": {
            "gate": round(timer.gate_ms, 1),
            "tracking": round(timer.tracking_ms, 1),
            "motion": round(timer.motion_ms, 1),
            "frame_select": round(timer.frame_select_ms, 1),
            "crop": round(timer.crop_ms, 1),
            "audio_analyze": round(timer.audio_analyze_ms, 1),
            "edge_total": round(timer.edge_total_ms, 1),
            "omni_prompt": round(timer.omni_prompt_ms, 1),
            "omni_api": round(timer.omni_api_ms, 1),
            "omni_parse": round(timer.omni_parse_ms, 1),
            "omni_total": round(timer.omni_total_ms, 1),
            "total": round(timer.total_ms, 1),
        },
    }
    with open(os.path.join(win_dir, "metadata.json"), "w") as f:
        _json.dump(meta, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="流式视频 perception 管线测试")
    parser.add_argument("video", help="视频文件路径")
    parser.add_argument("--fps", type=int, default=3, help="采样 FPS (默认 3)")
    parser.add_argument("--period", type=int, default=3, help="窗口时长秒 (默认 3)")
    parser.add_argument("--mock-omni", action="store_true", help="使用 mock omni（不调 API）")
    parser.add_argument("--no-wait", action="store_true", help="不等待，连续处理所有窗口")
    parser.add_argument("--max-windows", type=int, default=0, help="最大窗口数 (0=不限)")
    parser.add_argument("--rule", action="append", default=[], help="规则: id:name:conditions")
    parser.add_argument(
        "--use-mock-tracker",
        action="store_true",
        help="使用 mock tracker 而非真实 tracker",
    )
    parser.add_argument("--native-fps", action="store_true", help="使用视频原始帧率，不下采样")
    parser.add_argument("--output-dir", type=str, default="", help="保存中间结果（图片/JSON）到指定目录")
    parser.add_argument("--debug", action="store_true", help="打印详细 debug 信息（模型原始输出等）")
    parser.add_argument("--audio-overlap-ms", type=int, default=100, help="窗口边界音频重叠时长 ms（默认 100，0=禁用）")
    return parser.parse_args()


async def main():
    args = parse_args()

    video_path = args.video
    if not Path(video_path).exists():
        print(f"[ERROR] 视频文件不存在: {video_path}")
        return

    fps = args.fps
    period = args.period

    # Config
    config = PerceptionConfig(
        input=InputConfig(fps=fps, period_sec=period),
        gate=GateConfig(check_fps=1, change_threshold=0.005),
        identity=IdentityConfig(tracking_service_mode="real" if not args.use_mock_tracker else "mock"),
    )

    # Tracking service
    if args.use_mock_tracker:
        tracking_service = create_tracking_service("mock")
    else:
        tracking_service = create_tracking_service(
            "real",
            input_width=config.identity.perception_input_width,
            input_height=config.identity.perception_input_height,
        )

    # Rules
    rules: list[RuleCondition] = []
    for r in args.rule:
        parts = r.split(":", 2)
        if len(parts) == 3:
            rules.append(RuleCondition(rule_id=parts[0], rule_name=parts[1], query=parts[2]))
    if not rules:
        rules.append(
            RuleCondition(
                rule_id="reading_light",
                rule_name="读书开灯",
                query="当前是否有人在读书",
            )
        )

    context = OmniContext(
        rule_conditions=rules,
    )

    mode = "MOCK OMNI" if args.mock_omni else f"LIVE OMNI ({config.omni.model})"
    tracker_mode = "MOCK" if args.use_mock_tracker else "REAL (PerceptionEngine)"
    output_dir = args.output_dir
    native_fps = args.native_fps
    audio_overlap_ms = args.audio_overlap_ms

    print("\n  流式 Perception 管线测试")
    print_divider("-")
    print(f"  视频: {video_path}")
    fps_label = "原始帧率" if native_fps else f"{fps}"
    overlap_label = f"{audio_overlap_ms}ms" if audio_overlap_ms > 0 else "禁用"
    print(f"  FPS: {fps_label}, 窗口: {period}s, 音频重叠: {overlap_label}")
    print(f"  Omni: {mode}")
    print(f"  Tracker: {tracker_mode}")
    print(f"  规则: {[r.rule_id + ': ' + r.query for r in rules]}")
    print(f"  等待: {'否' if args.no_wait else f'是 (每窗口间隔 {period}s)'}")
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        print(f"  输出目录: {output_dir}")

    # Simulate stream
    print("\n[准备] 读取视频并切分窗口...")
    t0 = time.monotonic()
    windows = simulate_stream(video_path, fps, period, native_fps=native_fps, audio_overlap_ms=audio_overlap_ms)
    prep_ms = (time.monotonic() - t0) * 1000
    print(f"  切分完成: {len(windows)} 个窗口, 耗时 {prep_ms:.0f}ms")

    if args.max_windows > 0:
        windows = windows[: args.max_windows]
        print(f"  限制为前 {args.max_windows} 个窗口")

    # Process windows
    triggered_timers: list[StageTimer] = []
    skipped_count = 0

    for i, (input_slice, win_start) in enumerate(windows):
        if not args.no_wait and i > 0:
            print(f"\n  [等待 {period}s 模拟实时推送...]")
            await asyncio.sleep(period)

        result, timer = await run_pipeline_with_timing(
            input_slice,
            context,
            config,
            tracking_service,
            args.mock_omni,
        )

        print_window_result(i, win_start, result, timer, debug=args.debug)

        if output_dir:
            save_window_artifacts(output_dir, i, win_start, result, timer, context)

        if result["skipped"]:
            skipped_count += 1
        else:
            triggered_timers.append(timer)

            if result["omni_output"]:
                oo = result["omni_output"]
                # Update pending_speech for cross-window continuation
                incomplete = [i for i in oo.speeches if not i.is_complete]
                if incomplete:
                    context.pending_speech = [{"speaker": i.speaker, "content": i.content} for i in incomplete]
                    print(f"  [pending_speech] 携带未完成语音到下一窗口: {context.pending_speech}")
                else:
                    context.pending_speech = None

    # Summary
    print_summary(triggered_timers, len(windows), skipped_count)
    print()


if __name__ == "__main__":
    asyncio.run(main())
