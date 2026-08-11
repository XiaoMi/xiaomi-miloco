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

    但 hold 的前提是**有画面可看**:本窗无视频帧时 hold 对下游不成立(``trigger.hold``
    置假),只有音频过闸才建 packet 并交给 audio 路由,音频也没过闸就不建 packet。否则
    下游拿到零帧 packet 会被 ``trigger.hold`` 强拉回 video 路由,编码层无帧不加 video 块,
    变成"纯文本问模型这个场景里有什么"——模型只能照着 schema 编。注意 ``timing.hold_pass``
    仍是原始 hold 判定,只有 ``trigger.hold`` 受此约束(两者消费者不同,见下方注释)。

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

    # 空输入闸:视频轨在本窗无数据(解码器等关键帧 / 缓冲区溢出清空 / 掉线重连)时不建 packet。
    #
    # 判据是「hold 能不能撑起 video 路由」而不是「有没有输入」——两者不是一回事:下游
    # ``_is_audio_only`` 把 ``trigger.hold`` 当硬短路(hold=True 一律回 video 路由),而
    # video 路由的 ``_encode_video`` 对零帧直接返回 None、不加 video 块、连 warning 都
    # 不打,于是又变回「纯文本问模型这个场景里有什么」。所以无帧时 hold 对下游不成立:
    # 音频过闸的走 audio 路由(此时 hold=False 才能让 _is_audio_only 放行),音频没过闸的
    # 直接不建 packet。
    #
    # 交回上层的 ``timing.hold_pass`` 仍用原始 ``hold_active``:pipeline 的 HOLD_START /
    # HOLD_EXPIRED / HOLD_RECOVERED 状态机读它,且在 ``gate_packet is None`` 之前执行;
    # 这里若一并置假,仍在 hold 期的设备会被误判成 hold 结束,刷出假的 HOLD_EXPIRED
    # 日志与事件。``trigger.hold`` 全仓只有 ``_is_audio_only`` 一个消费者,改它只影响选路。
    hold_effective = hold_active and bool(input_slice.frames)
    if not any_pass and not hold_effective:
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
            hold=hold_effective,
        ),
        frames=input_slice.frames,
        audio_clip=input_slice.audio_clip,
        sample_rate=input_slice.sample_rate,
        fps=input_fps,
    )
    return packet, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts
