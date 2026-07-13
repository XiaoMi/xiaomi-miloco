"""Gate Layer — Orchestrator."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.config import GateConfig
from miloco.perception.engine.gate.audio_gate import evaluate_audio
from miloco.perception.engine.gate.speech_vad import evaluate_speech
from miloco.perception.engine.gate.visual_gate import evaluate_visual
from miloco.perception.engine.types import (
    GatePacket,
    GateTiming,
    GateTrigger,
    InputSlice,
)

logger = logging.getLogger(__name__)


async def run_gate(
    input_slice: InputSlice,
    config: GateConfig,
    input_fps: int = 1,
    prev_frame: NDArray[np.uint8] | None = None,
    last_visual_pass_ts: float | None = None,
    last_audio_pass_ts: float | None = None,
) -> tuple[
    GatePacket | None,
    GateTiming,
    NDArray[np.uint8] | None,
    float | None,
    float | None,
]:
    """Run Gate layer.

    Returns ``(packet | None, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts)``。

    Hold 滞回:hold 资格只依赖 ``last_visual_pass_ts``。本窗 visual 不通过 + 距上次 visual
    通过 <= ``config.hold_duration_sec`` 时,即使 audio 也不通过也会生成 packet 并打
    ``trigger.hold=True``,下游 ``_is_audio_only`` 短路保 video 路由。

    on-demand 单次调用路径不传两 ts(默认 None),hold 自然关闭。
    """
    t = time.monotonic()
    visual = evaluate_visual(
        input_slice.frames, config, input_fps, prev_frame=prev_frame,
    )
    visual_changed = visual.changed
    visual_score = visual.max_score
    last_checked = visual.last_checked
    video_ms = (time.monotonic() - t) * 1000

    t = time.monotonic()
    audio_active, audio_energy = evaluate_audio(input_slice.audio_clip, config)
    audio_ms = (time.monotonic() - t) * 1000

    # 仅在音频过能量 gate 时跑 VAD：没过 gate 音频本就不喂、speeches 已被剥，无需判人声。
    # VAD 单独计时（vad_ms），不混进 audio_ms——否则 gate_audio_ms 会从亚毫秒跳到 ~数十 ms
    # 触发既有监控阈值。
    t = time.monotonic()
    speech_active, speech_prob = (
        await asyncio.to_thread(evaluate_speech, input_slice.audio_clip, config)
        if audio_active
        else (False, 0.0)
    )
    vad_ms = (time.monotonic() - t) * 1000

    now = time.monotonic()
    any_pass = visual_changed or audio_active
    hold_active = (
        not visual_changed
        and last_visual_pass_ts is not None
        and config.hold_duration_sec > 0
        and (now - last_visual_pass_ts) <= config.hold_duration_sec
    )

    new_last_visual_pass_ts = now if visual_changed else last_visual_pass_ts
    new_last_audio_pass_ts = now if audio_active else last_audio_pass_ts

    timing = GateTiming(
        video_ms=video_ms, audio_ms=audio_ms,
        video_pass=visual_changed, audio_pass=audio_active,
        video_score=visual_score, audio_energy=audio_energy,
        vad_ms=vad_ms, speech_prob=speech_prob,
        hold_pass=hold_active,
        video_intra_score=visual.intra_max,
        video_cross_score=visual.cross_max,
    )

    if not any_pass and not hold_active:
        return None, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts

    packet = GatePacket(
        packet_id=str(uuid.uuid4()),
        room_name=input_slice.room_name,
        timestamp=input_slice.end_timestamp,
        trigger=GateTrigger(
            visual_changed=visual_changed,
            visual_change_score=visual_score,
            audio_active=audio_active,
            audio_energy_level=audio_energy,
            speech_active=speech_active,
            hold=hold_active,
        ),
        frames=input_slice.frames,
        audio_clip=input_slice.audio_clip,
        sample_rate=input_slice.sample_rate,
        fps=input_fps,
    )
    return packet, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts
