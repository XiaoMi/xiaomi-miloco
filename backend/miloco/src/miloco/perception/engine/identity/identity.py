"""Identity Layer —— 跟踪 + 身份识别 编排入口。

每窗口流程：

  1. ``RealTrackingService.analyze`` 跑 SortTracker，产出活跃 track 列表（含 bbox）
  2. ``IdentityEngine.process`` 异步派发 omni 识别，拿回 ``{track_id: face_id}`` 映射
  3. 把 face_id 写回 ``tracking_resp.objects_info[*].face_id`` 供下游消费

设计上把"如何识别"全部下沉到 IdentityEngine：本文件不直接调用 omni，也不关心
crop / motion / frame 选择——这些细节都在 IdentityEngine 内部。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from miloco.perception.engine.config import IdentityConfig
from miloco.perception.engine.identity.engine import IdentityEngine
from miloco.perception.engine.identity.tracking_service import (
    TrackingService,
    create_tracking_service,
)
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    GatePacket,
    IdentityPacket,
    IdentityTarget,
    MotionState,
)


async def run_identity(
    gate_packet: GatePacket,
    config: IdentityConfig,
    tracking_service: TrackingService | None = None,
    identity_engine: IdentityEngine | None = None,
    frame_index_offset: int = 0,
) -> IdentityPacket:
    """Run Identity layer on a GatePacket.

    Args:
        gate_packet:        Gate 层输出
        config:             ``IdentityConfig``（旧字段；本层只用 tracking_service_mode 等）
        tracking_service:   注入的跟踪服务（None 时按 config 实例化）
        identity_engine:    注入的 IdentityEngine（None 时跳过 omni 识别——face_id 全为 "none"）
        frame_index_offset: 累计帧序号起点（重审周期判定用）；默认 0

    Returns:
        IdentityPacket，``targets[].person_id`` 已经回填了 IdentityEngine 当前判定。
    """
    service = tracking_service or create_tracking_service(
        config.tracking_service_mode,
        model_dir=config.perception_model_dir or None,
        use_gpu=config.perception_use_gpu,
        input_width=config.perception_input_width,
        input_height=config.perception_input_height,
    )

    # Step 1: tracking (ONNX inference — offloaded to thread pool so the
    # perception loop stays responsive and multi-device gather can overlap).
    tracking_resp = await asyncio.to_thread(service.analyze, gate_packet.frames, fps=gate_packet.fps)
    output_frames = gate_packet.frames

    # Step 2: 调 IdentityEngine.process 获取 face_id 映射（异步派发 omni）
    if identity_engine is not None:
        latest_frame = output_frames[-1] if output_frames else None
        # 从 tracker 的 last_detections 里挑 FACE 类传给 IdentityEngine —— 它
        # 用来给 unknown crop push 进陌生人池时关联同帧 face（让 C 路径
        # register from-cluster 能拿到 face 备料）。无 tracker / 无 FACE 类
        # 时静默置 None,engine 内部 fallback 走原 face_crop=None 路径。
        face_dets: list[Any] | None = None
        tracker = getattr(service, "tracker", None)
        if tracker is not None and hasattr(tracker, "last_detections"):
            last_dets = tracker.last_detections or []
            if last_dets:
                Detection = type(last_dets[0])
                face_class = getattr(Detection, "CLASS_FACE", None)
                if face_class is not None:
                    face_dets = [d for d in last_dets if d.class_id == face_class]
        face_id_map, bbox_norm_map = await identity_engine.process(
            tracking_results=_to_tracking_dicts(tracking_resp.object_info),
            latest_frame=latest_frame,
            frame_index=frame_index_offset + len(gate_packet.frames),
            now_ts=gate_packet.timestamp,
            face_detections=face_dets,
        )
    else:
        face_id_map = {obj.track_id: "none" for obj in tracking_resp.object_info}
        bbox_norm_map = {}

    # Step 3: 构 IdentityTarget 列表
    targets: list[IdentityTarget] = []
    for obj in tracking_resp.object_info:
        person_id = face_id_map.get(obj.track_id, obj.face_id or "none")
        needs_verify = person_id in ("none", "pending") or person_id.startswith("pending:")
        # 翻身份黏旧名期的 track：旧名不可作名册先验(coasting 窗不在 candidate_tids，靠此兜)
        st = identity_engine.get_state(obj.track_id) if identity_engine is not None else None
        targets.append(IdentityTarget(
            type=obj.type,
            person_id=person_id,
            track_id=obj.track_id,
            needs_omni_verify=needs_verify,
            box_info=obj.box_info,
            bbox_xyxy_norm=bbox_norm_map.get(obj.track_id),
            suppress_as_prior=st is not None and st.reverted_from_confirmed,
        ))

    # 暂不做位移分析，scene_motion 固定为 STATIC（旧 motion_analyzer 已停用）
    scene_motion = MotionState.STATIC

    return IdentityPacket(
        packet_id=str(uuid.uuid4()),
        room_name=gate_packet.room_name,
        timestamp=gate_packet.timestamp,
        frame_info=tracking_resp.frame_info,
        targets=targets,
        scene_motion=scene_motion,
        frames=[],  # cropper / frame_selector 已废弃；crops 长期是 dead code
        all_frames=output_frames,
        audio_clip=gate_packet.audio_clip,
        audio_analysis=AudioAnalysis(type=AudioType.SILENCE, is_urgent=False, energy_level=0.0),
        sample_rate=gate_packet.sample_rate,
        trigger=gate_packet.trigger,
    )


def _to_tracking_dicts(objects) -> list[dict]:
    """把 ``TrackedObject`` 列表转为 ``IdentityEngine.process`` 期待的 dict 形式。

    SortTracker.get_tracking_results 已经直接出 dict；当前 ``RealTrackingService`` 走旧
    PerceptionEngine 时也最终会 convert 成 ``TrackedObject``，需要在这里反向折成 dict。

    每个 dict 字段（与 SortTracker.get_tracking_results 对齐）：
      ``id`` / ``class_id`` / ``bbox`` / ``xyxy`` / ``confidence``
    """
    out: list[dict] = []
    for obj in objects:
        # 取最近一帧的 human_body bbox
        xyxy = (0, 0, 0, 0)
        bbox_xywh = (0, 0, 0, 0)
        if obj.box_info:
            last = obj.box_info[-1]
            body = last.boxes.get("human_body") or last.boxes.get("human")
            if body:
                x, y, w, h = body
                bbox_xywh = (x, y, w, h)
                xyxy = (x, y, x + w, y + h)
        out.append({
            "id": obj.track_id,
            "class_id": 0,
            "bbox": bbox_xywh,
            "xyxy": xyxy,
            "confidence": 1.0,
        })
    return out
