"""Pipeline — End-to-end orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from miloco.observability.context import (
    DeviceContext,
    reset_device_context,
    set_device_context,
)
from miloco.observability.metrics_client import get_metrics_client
from miloco.perception.engine.config import PerceptionConfig
from miloco.perception.engine.gate.gate import run_gate
from miloco.perception.engine.identity.identity import run_identity
from miloco.perception.engine.identity.tracking_service import TrackingService
from miloco.perception.engine.omni.omni import (
    run_omni,
    run_omni_batch,
    run_omni_batch_stream,
    run_omni_fused,
    run_omni_stream,
)
from miloco.perception.engine.omni.omni_client import (
    OmniError,
    resolve_live_omni_config,
)

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from miloco.perception.engine.identity.engine import IdentityEngine
    from miloco.perception.engine.types import IdentityPacket
from miloco.perception.engine.types import (
    BatchPipelineResult,
    DevicePipelineResult,
    GatePacket,
    GateTrigger,
    IdentityPacket,
    InputSlice,
    OmniContext,
    OmniOutput,
    PipelineResult,
    QueryOutput,
    RoomPipelineResult,
)
from miloco.perception.types import (
    BatchedSnapshot,
    DeviceSnapshot,
    MatchedRule,
    Speech,
    Suggestion,
    VideoFrame,
    VideoStream,
)
from miloco.utils.time_utils import deploy_timezone

logger = logging.getLogger(__name__)


def downsample_snapshot(snapshot: DeviceSnapshot, target_fps: float) -> DeviceSnapshot:
    """Create a new snapshot with frames downsampled to target_fps.

    Uses DeviceSnapshot.get_frames_at_fps() for timestamp-aware resampling.
    All downstream components (Gate, Edge, Tracker) see only the downsampled frames,
    ensuring frame_index consistency across the pipeline.
    """
    frames = snapshot.get_frames_at_fps(target_fps)
    if not frames:
        return snapshot

    n = len(frames)
    duration_ms = snapshot.duration_ms
    video_frames = [
        VideoFrame(
            data=f,
            timestamp=snapshot.start_timestamp + i * duration_ms / n,
        )
        for i, f in enumerate(frames)
    ]
    h, w = frames[0].shape[:2]

    return DeviceSnapshot(
        device=snapshot.device,
        start_timestamp=snapshot.start_timestamp,
        end_timestamp=snapshot.end_timestamp,
        video=VideoStream(frames=video_frames, width=w, height=h),
        audio=snapshot.audio,
    )


def _publish_gate_event(event_type: str, device_id: str, payload: dict) -> None:
    """gate hold 状态转换事件发到 observability.db.events(参考 rule_runner)。"""
    client = get_metrics_client()
    if client is None:
        return
    client.publish_event(event_type=event_type, source=device_id, payload=payload)


def _ms_since(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _reraise_first(results: list[Any]) -> None:
    """``gather(return_exceptions=True)`` 后按输入顺序复抛第一个异常（保留原对象，
    OmniError 的 ``partial_timing`` 随之上抛），与串行"首个出错即整轮失败"语义一致。"""
    for r in results:
        if isinstance(r, BaseException):
            raise r


def _fmt_time_window(start_ms: float, end_ms: float) -> str:
    """画面时间窗 ``[HH:MM:SS-HH:MM:SS]``（= 该相机本窗 snapshot 起止时刻，部署时区）。

    走 ``deploy_timezone()`` 而非进程/OS 时钟：此时间窗经 ``event_text_builder``
    的「时间」字段直达 agent，若用裸 ``fromtimestamp`` 则在 host TZ≠部署时区时错标
    时刻（如 UTC 主机把 10:52 显示成 02:52），故与 API 出口 ISO 同源统一到部署时区。
    """
    tz = deploy_timezone()
    s = datetime.fromtimestamp(start_ms / 1000, tz=tz).strftime("%H:%M:%S")
    e = datetime.fromtimestamp(end_ms / 1000, tz=tz).strftime("%H:%M:%S")
    return f"[{s}-{e}]"


def _downsample_for_omni(
    packet: "IdentityPacket", src_fps: int, omni_fps: int
) -> "IdentityPacket":
    """给 omni 视频专用的下采副本: all_frames 按 src_fps→omni_fps 抽帧,
    frame_info.fps 设为有效 omni 帧率。

    **以窗口最后一帧为锚点向前抽帧**(非从头 [::step]): 注入 omni 的 track bbox 取自
    ``_latest_bbox`` = 窗口最后一帧的位置, 必须保证该帧出现在 omni 视频里(且是末帧),
    才维持 1fps 时"bbox 所在帧 == omni 视频末帧"的对齐不变量; 顺带丢掉的是最旧的几帧,
    对"识别此刻在场的人"更有利。

    tracker 已在 run_identity 内逐帧消费全部 src_fps 帧, 不受影响; 原 packet 不变(仍供
    PipelineResult / 下游)。omni_fps>=src_fps、无帧、或 step<=1 时原样返回。omni 已是瓶颈,
    解耦后视频帧数不随 tracker 提频而涨。
    """
    if omni_fps <= 0 or omni_fps >= src_fps or not packet.all_frames:
        return packet
    step = max(1, round(src_fps / omni_fps))
    if step <= 1:
        return packet
    n = len(packet.all_frames)
    idxs = list(range(n - 1, -1, -step))[::-1]  # 含末帧, 反向取再翻正
    frames = [packet.all_frames[i] for i in idxs]
    eff_fps = max(1, round(src_fps / step))
    return replace(
        packet,
        all_frames=frames,
        frame_info=replace(packet.frame_info, fps=eff_fps),
    )


def _inject_source_meta(
    omni_output: OmniOutput | None,
    room_name: str,
    source_device_ids: list[str],
    device_name: str = "",
    time_window: str = "",
) -> None:
    """注入来源元信息：room_name(IoT 房间)、source_device_ids(内部 did)、
    device_name(人读相机名)、time_window(画面时间窗)。各项一律以引擎为准覆盖
    （Suggestion 无模型房间来源，room_name 由引擎填）。"""
    if omni_output is None:
        return
    for it in omni_output.speeches:
        it.room_name = room_name
        it.source_device_ids = source_device_ids
        it.device_name = device_name
        it.time_window = time_window
    for cap in omni_output.caption:
        cap.room_name = room_name
        cap.source_device_ids = source_device_ids
        cap.device_name = device_name
        cap.time_window = time_window
    for sg in omni_output.suggestions:
        sg.room_name = room_name
        sg.source_device_ids = source_device_ids
        sg.device_name = device_name
        sg.time_window = time_window
    for mr in omni_output.matched_rules:
        mr.room_name = room_name
        mr.source_device_ids = source_device_ids
        mr.device_name = device_name
        mr.time_window = time_window


def _wrap_speeches_cb(
    cb: Callable[[list[Speech]], Awaitable[None]] | None,
    room_name: str,
    source_device_ids: list[str],
    device_name: str = "",
    time_window: str = "",
) -> Callable[[list[Speech]], Awaitable[None]] | None:
    if cb is None:
        return None

    async def wrapped(speeches: list[Speech]) -> None:
        for it in speeches:
            it.room_name = room_name
            it.source_device_ids = source_device_ids
            it.device_name = device_name
            it.time_window = time_window
        await cb(speeches)

    return wrapped


def _wrap_matched_rules_cb(
    cb: Callable[[list[MatchedRule]], Awaitable[None]] | None,
    room_name: str,
    source_device_ids: list[str],
    time_window: str = "",
) -> Callable[[list[MatchedRule]], Awaitable[None]] | None:
    if cb is None:
        return None

    async def wrapped(rules: list[MatchedRule]) -> None:
        for it in rules:
            it.room_name = room_name
            it.source_device_ids = source_device_ids
            it.time_window = time_window
        await cb(rules)

    return wrapped


def _wrap_suggestions_cb(
    cb: Callable[[list[Suggestion]], Awaitable[None]] | None,
    room_name: str,
    source_device_ids: list[str],
    assign_link: "Callable[[str, Suggestion, float], bool] | None" = None,
    device_name: str = "",
    time_window: str = "",
) -> Callable[[list[Suggestion]], Awaitable[None]] | None:
    """包装流式 suggestion 早出回调。

    两件事：① 注入 room_name 元信息；② 经事件链闸门 ``assign_link``
    （即 ``PerceptionEngine.assign_id_and_update_link``）解析后**只把新链外发**——
    心跳/重复（linked）抑制。这样流式早出与 ``_merge_results`` 共用同一套去重，
    不会把 ``event=""`` 的心跳原样推给 agent。

    ``assign_link`` 在流式线程（与 ``_merge_results`` 同一推理线程）同步调用，
    不跨线程改 ``_sugg_table``。``None`` 时退化为仅注入元信息（单测兜底）。
    """
    if cb is None:
        return None
    did = source_device_ids[0] if source_device_ids else ""

    async def wrapped(suggestions: list[Suggestion]) -> None:
        now = time.monotonic()
        to_report: list[Suggestion] = []
        for s in suggestions:
            s.room_name = room_name
            s.source_device_ids = source_device_ids
            s.device_name = device_name
            s.time_window = time_window
            if assign_link is not None and assign_link(did, s, now):
                continue  # linked（心跳/重复）→ 抑制，不外发
            to_report.append(s)
        if to_report:
            await cb(to_report)

    return wrapped


async def run_pipeline(
    input_slice: InputSlice,
    context: OmniContext,
    config: PerceptionConfig,
    tracking_service: TrackingService | None = None,
    identity_engine: "IdentityEngine | None" = None,
    on_early_speeches: Callable[[list[Speech]], Awaitable[None]] | None = None,
    on_early_matched_rules: Callable[[list[MatchedRule]], Awaitable[None]]
    | None = None,
    on_early_suggestions: Callable[[list[Suggestion]], Awaitable[None]] | None = None,
    assign_suggestion_link: "Callable[[str, Suggestion, float], bool] | None" = None,
    frame_index_offset: int = 0,
) -> PipelineResult:
    """Run full perception pipeline: InputSlice → Gate → Identity → Omni."""
    timing: dict[str, float] = {}
    t0 = time.monotonic()

    # Downsample to target fps at pipeline entry
    input_slice = downsample_snapshot(input_slice, config.input.fps)

    # Gate（本入口为无状态单次调用，无跨窗口基准可传，丢弃 last_checked / 两 ts）。
    # 注意:prev_frame 恒为 None → 视觉 gate 每次都走 cold-start 放行,本入口不会因静止画面 skipped。
    # 若将来把它放进循环复用,需自行在外维护 prev_frame 才能恢复静止 skip 语义。
    gate_packet, gate_timing, _, _, _ = run_gate(input_slice, config.gate, config.input.fps)
    timing["gate_ms"] = gate_timing.total_ms
    timing["gate_video_ms"] = gate_timing.video_ms
    timing["gate_audio_ms"] = gate_timing.audio_ms
    timing["gate_video_pass"] = int(gate_timing.video_pass)
    timing["gate_audio_pass"] = int(gate_timing.audio_pass)

    if gate_packet is None:
        timing["total_ms"] = _ms_since(t0)
        return PipelineResult(input_slice=input_slice, skipped=True, timing=timing)

    # Identity
    t = time.monotonic()
    identity_packet = await run_identity(gate_packet, config.identity, tracking_service,
                                          identity_engine=identity_engine,
                                          frame_index_offset=frame_index_offset)
    timing["identity_ms"] = _ms_since(t)

    # Omni —— 按 omni_call_mode 分流（fused 走身份合并主调用，否则走原 perception 主调用）
    room_name = input_slice.device.room_name or input_slice.device.did
    source_device_ids = [input_slice.device.did]
    device_name = input_slice.device.name
    time_window = _fmt_time_window(input_slice.start_timestamp, input_slice.end_timestamp)

    t = time.monotonic()
    use_fused = (
        identity_engine is not None
        and config.identity_engine.enabled
        and config.identity_engine.omni_call_mode == "fused"
    )
    # omni 视频用专门下采到 omni_fps 的副本(tracker 已逐帧消费全部 input.fps 帧, 不受影响)
    omni_packet = _downsample_for_omni(identity_packet, config.input.fps, config.input.omni_fps)
    # omni 配置热更新:每周期从当前 settings 刷新 model/base_url/api_key,web 改完下个周期生效。
    omni_cfg = resolve_live_omni_config(config.omni)
    if use_fused:
        # fused 模式：identity_assignments 合并进主调用
        omni_output = await run_omni_fused(
            [omni_packet], context, omni_cfg, identity_engine,
        )
    elif omni_cfg.stream:
        omni_output = await run_omni_stream(
            omni_packet,
            context,
            omni_cfg,
            on_early_speeches=_wrap_speeches_cb(
                on_early_speeches, room_name, source_device_ids, device_name, time_window
            ),
            on_early_matched_rules=_wrap_matched_rules_cb(
                on_early_matched_rules, room_name, source_device_ids, time_window
            ),
            # suggestions 早出经事件链闸门解析（_wrap_suggestions_cb → assign_link），
            # 心跳/重复抑制后才外发，与 _merge_results 共用同一去重，避免把
            # event="" 的心跳推给 agent。
            on_early_suggestions=_wrap_suggestions_cb(
                on_early_suggestions, room_name, source_device_ids, assign_suggestion_link,
                device_name, time_window
            ),
        )
    else:
        omni_output = await run_omni(omni_packet, context, omni_cfg)
    timing["omni_ms"] = _ms_since(t)

    _inject_source_meta(omni_output, room_name, source_device_ids, device_name, time_window)

    timing["total_ms"] = _ms_since(t0)

    return PipelineResult(
        input_slice=input_slice,
        gate_packet=gate_packet,
        identity_packet=identity_packet,
        omni_output=omni_output,
        skipped=omni_output.skipped,
        timing=timing,
    )


async def run_batch_pipeline(
    batch: BatchedSnapshot,
    contexts: dict[str, OmniContext],
    config: PerceptionConfig,
    get_tracking_service: Callable[[str, str], TrackingService] | None = None,
    get_identity_engine: Callable[[str, str], "IdentityEngine | None"] | None = None,
    # factory 签名 ``(device_id, room_name) -> ...``——room_name 由 PerceptionEngine
    # 用作 scope_label 拼接（dev 序号在 room 内分配）。
    # contexts key 语义：per-device（device_id），不是 per-room。原因：per-device omni
    # 调用下，每个 device 的 pending_speech 各自独立
    # —— 同 room 两个镜头各自的"上窗未完成语音"不能共享一份。
    on_early_speeches: Callable[[list[Speech]], Awaitable[None]] | None = None,
    on_early_matched_rules: Callable[[list[MatchedRule]], Awaitable[None]]
    | None = None,
    on_early_suggestions: Callable[[list[Suggestion]], Awaitable[None]] | None = None,
    assign_suggestion_link: "Callable[[str, Suggestion, float], bool] | None" = None,
    frame_index_offset: int = 0,
    gate_prev_frames: "dict[str, NDArray[np.uint8]] | None" = None,
    gate_last_visual_pass_ts: "dict[str, float] | None" = None,
    gate_last_audio_pass_ts: "dict[str, float] | None" = None,
    gate_hold_active: "dict[str, bool] | None" = None,
    gate_hold_started_at: "dict[str, float] | None" = None,
) -> BatchPipelineResult:
    """Run perception pipeline for a batch of devices, grouped by room.

    Gate / Identity / **Omni 都按 device 跑**——每个 device 独立调一次 omni，
    输出落到 ``RoomPipelineResult.omni_outputs[device_id]``。
    旧版按 room 合并 packets 调一次 omni，但 ``_encode_batch_video`` 只编首个
    device 的视频，其它 device 的 candidate 没有对应视觉信息 → 识别准确率拉胯。

    Args:
        get_tracking_service: 工厂回调 ``did -> TrackingService``。每个 device 应
                              拿到自己专属的 SortTracker 实例，避免跨镜头 IoU
                              互打 max_age。``None`` 时由 ``run_identity`` 内部
                              按 config 兜底实例化（单测场景）。
        get_identity_engine:  工厂回调 ``did -> IdentityEngine | None``。每个
                              device 一个 engine，状态隔离。返回 ``None`` 表示
                              该 device 退化为无 identity 模式（启动失败兜底）。
        gate_prev_frames:     per-device 跨窗口基准帧字典（预处理后 448 灰度，
                              原地读写）。``None`` 时 prev_frame 恒为 None →
                              每窗 cold-start 放行，视觉 gate 永不因静止 skip，
                              hold 也失效。生产流式循环传共享 ``{}``（首窗自动
                              建基准）。
        gate_last_visual_pass_ts: 调用方持有的 per-device "visual 最近通过时间戳"
                              字典,喂 gate hold 判定;``None`` 时关闭 hold
                              (等同 on-demand 路径)。
        gate_last_audio_pass_ts:  调用方持有的 per-device "audio 最近通过时间戳"
                              字典,仅落 traces 可观测,目前不参与判定。
        gate_hold_active:     per-device 上一窗 hold_pass 状态,用于检测状态转换
                              打 HOLD_START / HOLD_EXPIRED / HOLD_RECOVERED 日志
                              + events 表事件;``None`` 时不打日志。
        gate_hold_started_at: per-device hold 进入时刻(monotonic),退出/恢复时算
                              held_for_ms;与 ``gate_hold_active`` 配对使用。
    """
    batch_timing: dict[str, float] = {}
    t_total = time.monotonic()

    # 多相机运行态:展示本窗参与感知的相机数与 <did>-<设备名>,便于盯并发是否按预期
    # 把全部相机一起跑(如 "n_cam=2 | 1178866901-小米智能摄像机C700 | xxxx-yyyy")。
    logger.info(
        "[multicam] n_cam=%d | %s",
        len(batch.snapshots),
        " | ".join(f"{s.device.did}-{s.device.name}" for s in batch.snapshots),
    )

    async def _run_device(
        snapshot: DeviceSnapshot,
        room_name: str,
        room_timing: dict[str, Any],
    ) -> DevicePipelineResult:
        """单设备 gate→identity→omni。封成 per-device 协程是 gather 并发的硬约束：
        ``set_device_context``(ContextVar) 必须在各自 Task 的 context 副本里 set/reset
        —— gather 为每个协程建独立 Task(启动时 copy_context)，DeviceContext 才不会
        跨设备串号(否则 trace 全记到最后一个 device 名下)。reset 在 per-Task
        下已非必需(副本随 Task 丢弃)，但成对保留无害。
        """
        # Downsample to target fps at pipeline entry
        snapshot = downsample_snapshot(snapshot, config.input.fps)

        did = snapshot.device.did
        device_name = snapshot.device.name
        time_window = _fmt_time_window(snapshot.start_timestamp, snapshot.end_timestamp)
        context = contexts.get(did, OmniContext())
        tracking_service = (
            get_tracking_service(did, room_name) if get_tracking_service else None
        )
        identity_engine = (
            get_identity_engine(did, room_name) if get_identity_engine else None
        )
        # 人类可读设备名挂到 engine,供 tier_c sidecar 记 camera_name(engine 自身只持
        # cam_id=did,名字在 snapshot.device 上)。每窗刷新、幂等;名字稳定无竞态。
        if identity_engine is not None:
            identity_engine.device_name = snapshot.device.name

        device_trace_id = str(uuid.uuid4())
        # 同一 cycle 同一 device 在 traces_device(SQLite)行用这把 UUID,
        # processor._publish_trace 从 timing 读出复用,避免双钥匙。
        room_timing[f"_device_trace_id_{did}"] = device_trace_id

        prev_frame = gate_prev_frames.get(did) if gate_prev_frames is not None else None
        last_v = (
            gate_last_visual_pass_ts.get(did)
            if gate_last_visual_pass_ts is not None else None
        )
        last_a = (
            gate_last_audio_pass_ts.get(did)
            if gate_last_audio_pass_ts is not None else None
        )
        gate_packet, gate_timing, last_checked, new_last_v, new_last_a = run_gate(
            snapshot, config.gate, config.input.fps,
            prev_frame=prev_frame,
            last_visual_pass_ts=last_v,
            last_audio_pass_ts=last_a,
        )
        # 跨窗 gate 状态各 device 用 per-did key、读写均在同步段、键不相交 → 并发 gather 下安全(同 room_timing)。
        # 无论 gate 是否通过都更新基准——始终是"最近实际比较过的画面"。
        if gate_prev_frames is not None and last_checked is not None:
            gate_prev_frames[did] = last_checked
        # 两 ts 仅在 run_gate 返回新值(非 None)时写回;清理由 reset_session 负责
        if gate_last_visual_pass_ts is not None and new_last_v is not None:
            gate_last_visual_pass_ts[did] = new_last_v
        if gate_last_audio_pass_ts is not None and new_last_a is not None:
            gate_last_audio_pass_ts[did] = new_last_a

        # gate hold 状态转换 → 日志 + events 表
        if gate_hold_active is not None and gate_hold_started_at is not None:
            prev_hold = gate_hold_active.get(did, False)
            cur_hold = gate_timing.hold_pass
            cur_visual_pass = gate_timing.video_pass
            now_mono = time.monotonic()
            if cur_hold and not prev_hold:
                # HOLD_START: visual 从通过转入滞回期
                gate_hold_started_at[did] = now_mono
                logger.info(
                    "HOLD_START: device=%s room=%s hold_duration_sec=%.1f",
                    did, room_name, config.gate.hold_duration_sec,
                )
                _publish_gate_event(
                    "gate_hold_start", did,
                    {
                        "room": room_name,
                        "hold_duration_sec": config.gate.hold_duration_sec,
                    },
                )
            elif prev_hold and not cur_hold:
                started_at = gate_hold_started_at.pop(did, None)
                held_for_ms = (
                    int((now_mono - started_at) * 1000) if started_at is not None else None
                )
                if cur_visual_pass:
                    # HOLD_RECOVERED: hold 期内 visual 真恢复
                    logger.info(
                        "HOLD_RECOVERED: device=%s room=%s held_for_ms=%s visual_score=%.3f",
                        did, room_name, held_for_ms, gate_timing.video_score,
                    )
                    _publish_gate_event(
                        "gate_hold_recovered", did,
                        {
                            "room": room_name,
                            "held_for_ms": held_for_ms,
                            "visual_score": gate_timing.video_score,
                        },
                    )
                else:
                    # HOLD_EXPIRED: hold 到期、放弃 video
                    logger.info(
                        "HOLD_EXPIRED: device=%s room=%s held_for_ms=%s",
                        did, room_name, held_for_ms,
                    )
                    _publish_gate_event(
                        "gate_hold_expired", did,
                        {"room": room_name, "held_for_ms": held_for_ms},
                    )
            gate_hold_active[did] = cur_hold

        room_timing[f"gate_{did}_ms"] = gate_timing.total_ms
        room_timing[f"gate_video_{did}_ms"] = gate_timing.video_ms
        room_timing[f"gate_audio_{did}_ms"] = gate_timing.audio_ms
        room_timing[f"gate_vad_{did}_ms"] = gate_timing.vad_ms
        room_timing[f"gate_video_{did}_pass"] = int(gate_timing.video_pass)
        room_timing[f"gate_audio_{did}_pass"] = int(gate_timing.audio_pass)
        room_timing[f"gate_hold_{did}_pass"] = int(gate_timing.hold_pass)
        # gate 真实评估的打分 → traces_device.gate_video_score / gate_audio_energy
        # 经 _merge_results 保留顶层"_"前缀,在 processor._publish_trace 复用。
        # on-demand bypass / 系统异常 fallback 路径不走这里,score 字段保持 NULL。
        room_timing[f"_gate_video_score_{did}"] = gate_timing.video_score
        room_timing[f"_gate_audio_energy_{did}"] = gate_timing.audio_energy
        room_timing[f"_gate_speech_prob_{did}"] = gate_timing.speech_prob
        # intra/cross 拆分 → 经 _merge_results 加 "{room}/" 前缀进 timing_detail JSON。
        room_timing[f"gate_video_intra_score_{did}"] = gate_timing.video_intra_score
        room_timing[f"gate_video_cross_score_{did}"] = gate_timing.video_cross_score

        if gate_packet is None:
            return DevicePipelineResult(
                device_id=did,
                input_slice=snapshot,
                skipped=True,
            )

        t = time.monotonic()
        identity_packet = await run_identity(
            gate_packet, config.identity, tracking_service,
            identity_engine=identity_engine,
            frame_index_offset=frame_index_offset,
        )
        room_timing[f"identity_{did}_ms"] = _ms_since(t)

        # omni 阶段:把 per-device 元数据塞进 ContextVar,供 traces_device 等观测路径读取。
        device_ctx_token = set_device_context(DeviceContext(
            device_trace_id=device_trace_id,
            device_id=did,
            room_name=room_name,
        ))
        t = time.monotonic()
        try:
            # Omni per device —— 按 omni_call_mode 分流
            use_fused = (
                identity_engine is not None
                and config.identity_engine.enabled
                and config.identity_engine.omni_call_mode == "fused"
            )
            # omni 视频用专门下采到 omni_fps 的副本(tracker 已逐帧消费全部 input.fps 帧, 不受影响)
            omni_packet = _downsample_for_omni(
                identity_packet, config.input.fps, config.input.omni_fps
            )
            # omni 配置热更新:每周期从当前 settings 刷新,web 改完下个周期生效。
            omni_cfg = resolve_live_omni_config(config.omni)
            if use_fused:
                omni_output = await run_omni_fused(
                    [omni_packet], context, omni_cfg, identity_engine,
                )
            elif omni_cfg.stream:
                omni_output = await run_omni_batch_stream(
                    [omni_packet],
                    context,
                    omni_cfg,
                    on_early_speeches=_wrap_speeches_cb(
                        on_early_speeches, room_name, [did], device_name, time_window
                    ),
                    on_early_matched_rules=_wrap_matched_rules_cb(
                        on_early_matched_rules, room_name, [did], time_window
                    ),
                    # suggestions 早出经事件链闸门解析（_wrap_suggestions_cb → assign_link），
                    # 心跳/重复抑制后才外发，与 _merge_results 共用同一去重，避免把
                    # event="" 的心跳推给 agent。
                    on_early_suggestions=_wrap_suggestions_cb(
                        on_early_suggestions, room_name, [did], assign_suggestion_link,
                        device_name, time_window
                    ),
                )
            else:
                omni_output = await run_omni_batch(
                    [omni_packet], context, omni_cfg,
                )
            room_timing[f"omni_{did}_ms"] = _ms_since(t)
        except OmniError as omni_err:
            # partial 结果:单设备 omni 失败(超时/429/模型错)→ 记 omni_ms + 失败标记 + log,
            # **不连累整窗**——返回 skipped(omni_output=None;_merge_results line855 会跳过该设备),
            # 健康相机照常 merge+submit。失败时也记 omni_ms 让 rtf_omni 反映真实墙钟(含 timeout 等待)。
            # omni 超时(30s)走这里 → run_omni_fused 已在其 except 调 deliver_fused_failure 清理
            # fused pending(无泄漏)。
            room_timing[f"omni_{did}_ms"] = _ms_since(t)
            # per-device 错误进 timing(_ 前缀顶层 key),_publish_trace 据此给该相机
            # trace 记 omni error(omni_error_count +1)。用 OmniError.code(形如
            # "HTTPStatusError:429" / "ReadTimeout")让 dashboard 错误分类 SQL 能精确
            # 命中,错误详情留给下一行的 logger.warning。
            room_timing[f"_omni_error_{did}"] = omni_err.code
            logger.warning(
                "[omni](room=%s device=%s) 感知API调用失败，错误码=%s(skipped) | %s",
                room_name, f"{device_name}({did})", omni_err.code, omni_err,
            )
            return DevicePipelineResult(
                device_id=did, input_slice=snapshot,
                gate_packet=gate_packet, identity_packet=identity_packet,
                omni_output=None, skipped=True,
            )
        except Exception as e:  # noqa: BLE001 —— 非 omni 的意外错也不连累整窗(同 partial 处理)
            room_timing[f"omni_{did}_ms"] = _ms_since(t)
            room_timing[f"_omni_error_{did}"] = f"{type(e).__name__}: {e}"
            logger.error(
                "[device](room=%s device=%s) 单相机处理异常(skipped) | %s",
                room_name, f"{device_name}({did})", e, exc_info=True,
            )
            return DevicePipelineResult(
                device_id=did, input_slice=snapshot,
                gate_packet=gate_packet, identity_packet=identity_packet,
                omni_output=None, skipped=True,
            )
        finally:
            reset_device_context(device_ctx_token)
        _inject_source_meta(omni_output, room_name, [did], device_name, time_window)

        # per-omni 早送(仅 fused):本相机 omni 一好就送 suggestion/speech,不等其它相机 gather。
        # stream 模式已在 run_omni_batch_stream 内经回调早送;batch(else)仍走 merge。复用同一对
        # _wrap_*_cb 通道:注入 meta + 事件链闸门(给 suggestion 打 id) + 投递。
        # suggestion:把 omni_output.suggestions 裁成只剩「新链」(已早送)——心跳沿用旧语义抑制、
        # 不入 result;新链保留供 _merge_results 落 result.suggestions(dump/上下文完整展示),
        # 防重发交 client 侧 early_sent_sugg_ids。speech 防双发沿用 client 侧 early_sent_contents。
        if use_fused:
            sp_cb = _wrap_speeches_cb(
                on_early_speeches, room_name, [did], device_name, time_window
            )
            if sp_cb is not None:
                await sp_cb(list(omni_output.speeches))
            if on_early_suggestions is not None:
                reported: list[Suggestion] = []

                async def _collect_reported(items: list[Suggestion]) -> None:
                    reported.extend(items)
                    await on_early_suggestions(items)

                sg_cb = _wrap_suggestions_cb(
                    _collect_reported, room_name, [did],
                    assign_suggestion_link, device_name, time_window,
                )
                if sg_cb is not None:
                    await sg_cb(list(omni_output.suggestions))
                    omni_output.suggestions = reported

        return DevicePipelineResult(
            device_id=did,
            input_slice=snapshot,
            gate_packet=gate_packet,
            identity_packet=identity_packet,
            omni_output=omni_output,
        )

    async def _run_room(
        room_name: str, snapshots: list[DeviceSnapshot]
    ) -> tuple[str, RoomPipelineResult]:
        # 类型放宽到 Any:以 "_" 开头的 key 装 per-device 元数据(如 device_trace_id),
        # 经 _merge_results 合并时这类 key 不会被加 "{room}/" 前缀,保留 "_xxx" 形式,
        # 让下游的 `key.startswith("_")` 过滤逻辑(timing_detail / _aggregate_stage_ms)
        # 仍能识别"非耗时元数据"。
        room_timing: dict[str, Any] = {}
        t_room = time.monotonic()

        # room 内各 device 并发。room_timing 由各 device 写 disjoint 的 {did} key,
        # asyncio 单线程下 dict setitem 原子、互不覆盖。**partial**:_run_device 已兜底
        # omni/意外错→返回 skipped,不 raise;故此处正常收 DevicePipelineResult,健康相机照常
        # 入 device_results、失败相机 skipped(merge 跳过)。意外漏到 gather 的异常 log 后跳过、
        # 不连累其它相机(理论不该发生,纯防御)。
        results = await asyncio.gather(
            *(_run_device(s, room_name, room_timing) for s in snapshots),
            return_exceptions=True,
        )
        device_results: dict[str, DevicePipelineResult] = {}
        for s, r in zip(snapshots, results):
            if isinstance(r, BaseException):
                logger.error(
                    "[device](room=%s device=%s) 管线task异常(skipped) | %r",
                    room_name, f"{s.device.name}({s.device.did})", r, exc_info=r,
                )
                continue
            device_results[r.device_id] = r

        room_timing["total_ms"] = _ms_since(t_room)
        all_skipped = all(dr.skipped for dr in device_results.values()) if device_results else True

        return room_name, RoomPipelineResult(
            room_name=room_name,
            device_results=device_results,
            skipped=all_skipped,
            timing=room_timing,
        )

    # 全部设备跨 room 并发:测试集是跨区域 cam1+cam2(不同 room),外层 room 串行才是真
    # 瓶颈,故 room 间也 gather(嵌套)。omni 墙钟 Σ→≈max。
    # 并发安全(已对抗式核查):每 device 独立 IdentityEngine/SortTracker;DeviceContext
    # 经 gather 的 per-Task copy_context 天然隔离(见 _run_device docstring);工厂同步只按
    # did 写;httpx 连接池。共享 TierUPool 的 push/close 都是无 await 的同步段(各自原子,
    # 单线程不中途交错),fused 下 device 间池操作仅"相对顺序"可能异于串行——不影响本窗
    # 感知输出,只让陌生人池聚类顺序偶有不同(池跨窗自愈、有 case-b 兜底),故不加锁。
    by_room_items = list(batch.by_room().items())
    room_results = await asyncio.gather(
        *(_run_room(rn, snaps) for rn, snaps in by_room_items),
        return_exceptions=True,
    )
    rooms: dict[str, RoomPipelineResult] = {}
    for (rn_in, _snaps), rr in zip(by_room_items, room_results):
        if isinstance(rr, BaseException):
            logger.error(
                "[pipeline](room=%s) 管线task异常(skipped) | %r",
                rn_in, rr, exc_info=rr,
            )
            continue
        rn, room_obj = rr
        rooms[rn] = room_obj

    batch_timing["total_ms"] = _ms_since(t_total)

    return BatchPipelineResult(rooms=rooms, timing=batch_timing)


# =============================================================================
# Active query pipeline (skip Gate, keep Edge, query prompt)
# =============================================================================


async def run_query_pipeline(
    batch: BatchedSnapshot,
    query: str,
    config: PerceptionConfig,
    get_tracking_service: Callable[[str, str], TrackingService] | None = None,
    get_identity_engine: Callable[[str, str], "IdentityEngine | None"] | None = None,
    last_captions: dict[str, str] | None = None,
    frame_index_offset: int = 0,
) -> dict[str, QueryOutput]:
    """Active query pipeline: skip Gate, run Identity, Omni with query prompt.

    Flow per device: construct GatePacket (no filtering) → Identity → collect IdentityPackets.
    Flow per room: build query prompt from IdentityPackets + query → call Omni → text answer.

    Returns dict[room_name, answer_text].

    Note: query 路径 omni 调用仍是 **per-room 一次**（产品语义：用户对房间问问题，
    答案是单份文本）—— 此时 ``prompt_builder._encode_batch_video`` 只编首个有 frames
    的 device 视频（已知遗留 degenerate：候选列表含同 room 多 device 的 track，但 omni
    只看到首镜头视频，对非首镜头 track 是"无视觉信息硬猜"）。主路径 realtime_perceive
    已改 per-device omni 规避；query 路径暂保留，后续若产品要求改多视角输入再升级。
    """
    import uuid

    from miloco.perception.engine.omni.omni_client import call_omni
    from miloco.perception.engine.omni.prompt_builder import build_query_prompt
    from miloco.perception.engine.omni.response_parser import parse_query_response
    from miloco.perception.engine.types import QueryOutput

    captions = last_captions or {}

    async def _run_room_query(
        room_name: str, snapshots: list[DeviceSnapshot]
    ) -> "tuple[str, QueryOutput] | None":
        room_identity_packets: list[IdentityPacket] = []

        for snapshot in snapshots:
            # Downsample to target fps at pipeline entry
            snapshot = downsample_snapshot(snapshot, config.input.fps)

            did = snapshot.device.did
            tracking_service = (
                get_tracking_service(did, room_name) if get_tracking_service else None
            )
            identity_engine = (
                get_identity_engine(did, room_name) if get_identity_engine else None
            )

            # Bypass Gate — construct GatePacket directly (always process)
            gate_packet = GatePacket(
                packet_id=str(uuid.uuid4()),
                room_name=snapshot.room_name,
                timestamp=snapshot.end_timestamp,
                trigger=GateTrigger(
                    visual_changed=True,
                    visual_change_score=1.0,
                    audio_active=True,
                    audio_energy_level=1.0,
                ),
                frames=snapshot.frames,
                audio_clip=snapshot.audio_clip,
                sample_rate=snapshot.sample_rate,
                fps=config.input.fps,
            )

            # Run Identity (tracker, motion, frame selector, crop, audio analyzer)
            identity_packet = await run_identity(
                gate_packet, config.identity, tracking_service,
                identity_engine=identity_engine,
                frame_index_offset=frame_index_offset,
                )
            # query 路径的 packet 只喂 build_query_prompt→omni, 同主路径口径下采到
            # omni_fps(否则 fps 提频后 on-demand 查询送 omni 的帧数翻倍、延迟上升)
            identity_packet = _downsample_for_omni(
                identity_packet, config.input.fps, config.input.omni_fps
            )
            room_identity_packets.append(identity_packet)

        if not room_identity_packets:
            return None

        # Build query prompt from IdentityPackets and call Omni
        payload = build_query_prompt(
            identity_packets=room_identity_packets,
            query=query,
            last_caption=captions.get(room_name),
        )
        raw_response = await call_omni(
            payload, resolve_live_omni_config(config.omni), type="on_demand"
        )
        answer = parse_query_response(raw_response)
        if not answer:
            return None
        return room_name, QueryOutput(answer=answer)

    # 各 room 并发（与 run_batch_pipeline 同源：per-room omni 墙钟 Σ→max）。单房间查询
    # 无变化；多房间/全屋查询省 Σ。return_exceptions=True + _reraise_first 保持原"任一
    # room 失败即整体失败"语义（上层 on_demand_perceive 捕获→空答案）。query 路径不走
    # fused、无 set_device_context，并发面比主路径更窄（run_identity 池写仍是原子同步段）。
    room_results = await asyncio.gather(
        *(_run_room_query(rn, snaps) for rn, snaps in batch.by_room().items()),
        return_exceptions=True,
    )
    _reraise_first(room_results)

    results: dict[str, QueryOutput] = {}
    for item in room_results:
        if item is not None:
            rn, out = item
            results[rn] = out
    return results
