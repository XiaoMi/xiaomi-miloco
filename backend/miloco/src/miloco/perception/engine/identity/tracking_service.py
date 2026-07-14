"""Identity Layer — Tracking Service。

外部入口,给 ``identity.run_identity`` 提供"跟踪结果列表"。三种实现：
  - ``MockTrackingService``：返回固定 mock 数据,单测用
  - ``RealTrackingService``：实例化 ``SortTracker``（YOLO 检测 + IoU/Kalman 跟踪,无 ReID）
  - **``DeepSortTrackingService``**（v1.2 新增）：实例化 ``DeepSortTracker``
    （IoU+Kalman+ReID 关联级联）。本期主动注册改造的默认选择——给陌生人池 (M3)
    提供 ReID embedding 复用入口。

mode 取值：``"mock" | "real" | "deep_sort"``。fast/detect_only 档位已取消。
灰度策略：default_config.yaml::tracking_service_mode 先保持 "real",PR 2 上线后切
"deep_sort" 灰度 1 周再放大。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.types import (
    BoxType,
    FrameInfo,
    ObjectType,
    TrackedObject,
    TrackingBoxInfo,
    TrackingResponse,
)


def _build_response(results: list[dict], n_frames: int, fps: int) -> TrackingResponse:
    """将 tracker.get_tracking_results() 转为 TrackingResponse。"""
    from miloco.perception.engine.identity.tracker.detector import Detection

    now_ms = int(time.time() * 1000)
    last_idx = max(0, n_frames - 1)
    object_info: list[TrackedObject] = []
    for r in results:
        x1, y1, x2, y2 = r["xyxy"]
        class_id = r.get("class_id", Detection.CLASS_HUMAN)

        # 根据 class_id 映射 ObjectType 和 box_type
        if class_id in (Detection.CLASS_CAT, Detection.CLASS_DOG):
            obj_type = ObjectType.PET
            box_type = BoxType.PET_BODY
        else:
            obj_type = ObjectType.HUMAN_BODY
            box_type = BoxType.HUMAN_BODY

        box_info = [TrackingBoxInfo(
            frame_index=last_idx,
            boxes={box_type: (x1, y1, x2 - x1, y2 - y1)},
        )]
        object_info.append(TrackedObject(
            type=obj_type,
            face_id="none",
            track_id=r["id"],
            box_info=box_info,
        ))
    return TrackingResponse(
        frame_info=FrameInfo(
            start_timestamp=now_ms - int(n_frames / max(fps, 1) * 1000),
            end_timestamp=now_ms,
            fps=fps,
        ),
        object_info=object_info,
    )


class TrackingService(ABC):
    _detector: Any
    _tracker: Any

    @abstractmethod
    def analyze(self, frames: list[NDArray[np.uint8]], fps: int = 2) -> TrackingResponse: ...

    def reset_session(self) -> None:
        """重置跟踪会话状态，子类可按需实现。"""

    def set_fps(self, fps: int) -> None:
        """运行时更新采样 fps：同步 ``_fps`` 并转调底层 tracker 的 set_fps。

        默认实现覆盖 Real / DeepSort（都持 ``_tracker``）；Mock 无 tracker 时 no-op。
        用于「改 omni_fps 顶起 tracker fps」的运行时热更，免重建引擎。
        """
        self._fps = fps
        tracker = getattr(self, "_tracker", None)
        if tracker is not None and hasattr(tracker, "set_fps"):
            tracker.set_fps(fps)


# =============================================================================
# Mock Service
# =============================================================================


class MockTrackingService(TrackingService):
    _detector = None  # type: ignore[assignment]
    _tracker = None   # type: ignore[assignment]

    def __init__(self, response: TrackingResponse | None = None):
        self._response = response or create_default_mock_response()

    def analyze(self, frames: list[NDArray[np.uint8]], fps: int = 2) -> TrackingResponse:
        return self._response


# =============================================================================
# Real Service — wraps SortTracker
# =============================================================================


class RealTrackingService(TrackingService):
    """实例化 SortTracker（无 ReID）+ Detector（YOLO ONNX）。"""

    def __init__(
        self,
        model_dir: str | None = None,
        use_gpu: bool = False,
        input_width: int = 1280,  # 仅记录，Detector 自身按模型固定输入
        input_height: int = 720,
        sort_config=None,
        fps: int = 1,
    ):
        from miloco.perception.engine.identity.sort import SortConfig, SortTracker
        from miloco.perception.engine.identity.tracker.detector import Detector

        self._input_width = input_width
        self._input_height = input_height
        self._fps = fps

        det_path = self._resolve_model_path(model_dir, "det_4C.onnx")
        self._detector = Detector(model_path=det_path, use_gpu=use_gpu)
        # SortTracker 需要 fps 来把 SortConfig.max_age_sec 换算成 max_age_frames
        self._tracker = SortTracker(
            config=sort_config or SortConfig(),
            detector=self._detector,
            fps=fps,
        )

    def analyze(self, frames: list[NDArray[np.uint8]], fps: int = 2) -> TrackingResponse:
        if not frames:
            now = time.time() * 1000
            return TrackingResponse(
                frame_info=FrameInfo(start_timestamp=now, end_timestamp=now, fps=fps),
                object_info=[],
            )
        for frame in frames:
            self._tracker.update(frame)
        return _build_response(self._tracker.get_tracking_results(), len(frames), fps)

    def reset_session(self) -> None:
        """重置 SortTracker（清空 tracks + _next_track_id 归零）。

        ⚠️ 调用方必须同时调 ``IdentityEngine.reset()``——否则 _next_track_id 从 0
        重新自增的新 track 会在 dead-track grace 期内命中残留 _states，继承已失效
        的 committed_person_id。优先用 ``PerceptionEngine.reset_session()`` 这个
        高层入口，它会联动二者。
        """
        self._tracker.reset()

    @staticmethod
    def _resolve_model_path(model_dir: str | None, filename: str) -> str:
        from pathlib import Path
        if model_dir:
            return str(Path(model_dir) / filename)
        # 默认从包内 miloco/perception/models/ 解析；
        # 不依赖进程 cwd（supervisor 启动时 cwd 不在该目录下）。
        # __file__ = .../miloco/perception/engine/identity/tracking_service.py
        return str(Path(__file__).resolve().parent.parent.parent / "models" / filename)


# =============================================================================
# DeepSort Service — wraps DeepSortTracker (v1.2 主动注册改造启用)
# =============================================================================


class DeepSortTrackingService(TrackingService):
    """实例化 DeepSortTracker(IoU+Kalman+ReID)+ Detector。

    与 RealTrackingService 的区别:跟踪关联多用一层 ReID 外观,跟踪更稳;额外暴露
    ``get_track_embedding(track_id)`` 给陌生人池复用 ReID 快照(零额外推理)。

    配置入口:
        - 业务调参 -> yaml::identity_engine.deep_sort 段(精简的 9 字段)
        - 部署/接口级(use_gpu / ReID 文件名等) -> 构造参数 + 代码默认值
    """

    def __init__(
        self,
        model_dir: str | None = None,
        use_gpu: bool = False,
        input_width: int = 1280,
        input_height: int = 720,
        deep_sort_config=None,
        fps: int = 1,
    ):
        from miloco.perception.engine.config import DeepSortConfigDC
        from miloco.perception.engine.identity.deep_sort import DeepSortTracker
        from miloco.perception.engine.identity.tracker.detector import Detector

        self._input_width = input_width
        self._input_height = input_height
        self._fps = fps

        det_path = RealTrackingService._resolve_model_path(model_dir, "det_4C.onnx")
        self._detector = Detector(model_path=det_path, use_gpu=use_gpu)

        cfg = deep_sort_config or DeepSortConfigDC()
        reid_path = (
            RealTrackingService._resolve_model_path(model_dir, "human_body_reid_v2.onnx")
            if model_dir else None
        )
        self._tracker = DeepSortTracker(
            detector=self._detector,
            config=cfg,
            fps=fps,
            reid_model_path=reid_path,
            use_gpu=use_gpu,
        )

    def analyze(self, frames: list[NDArray[np.uint8]], fps: int = 2) -> TrackingResponse:
        if not frames:
            now = time.time() * 1000
            return TrackingResponse(
                frame_info=FrameInfo(start_timestamp=now, end_timestamp=now, fps=fps),
                object_info=[],
            )
        for frame in frames:
            self._tracker.update(frame)
        return _build_response(self._tracker.get_tracking_results(), len(frames), fps)

    def reset_session(self) -> None:
        self._tracker.reset()

    @property
    def tracker(self):
        """暴露内部 DeepSortTracker,给陌生人池 (M3) 调 get_track_embedding。"""
        return self._tracker


# =============================================================================
# Factory
# =============================================================================


def create_tracking_service(mode: str, **kwargs) -> TrackingService:
    if mode == "mock":
        return MockTrackingService()
    if mode == "real":
        return RealTrackingService(**kwargs)
    if mode == "deep_sort":
        # 调用方若按 real 模式传了 sort_config,silently drop;deep_sort 走
        # deep_sort_config(类型 DeepSortConfigDC),不传则用 dataclass 默认值。
        kwargs.pop("sort_config", None)
        return DeepSortTrackingService(**kwargs)
    # fast / detect_only 已取消——SortTracker 自身已经轻量,不再细分档位
    if mode in ("fast", "detect_only"):
        raise ValueError(
            f"tracking_service_mode={mode!r} 已不再支持,请改为 'real' 或 'deep_sort'。"
        )
    raise ValueError(f"Unknown tracking service mode: {mode}")


# =============================================================================
# Mock Helpers
# =============================================================================


def create_default_mock_response() -> TrackingResponse:
    now = time.time() * 1000
    return TrackingResponse(
        frame_info=FrameInfo(start_timestamp=now - 3000, end_timestamp=now, fps=2),
        object_info=[
            TrackedObject(
                type=ObjectType.HUMAN_WITH_FACE,
                face_id="wangshihao",
                track_id=1,
                box_info=[
                    TrackingBoxInfo(
                        frame_index=i,
                        boxes={
                            BoxType.HUMAN_BODY: (100 + i, 200 + i, 300, 400),
                            BoxType.HUMAN_FACE: (150 + i, 210 + i, 80, 80),
                        },
                    )
                    for i in range(6)
                ],
            ),
        ],
    )


def create_mock_response_with_movement() -> TrackingResponse:
    now = time.time() * 1000
    return TrackingResponse(
        frame_info=FrameInfo(start_timestamp=now - 3000, end_timestamp=now, fps=2),
        object_info=[
            TrackedObject(
                type=ObjectType.HUMAN_WITH_FACE,
                face_id="wangshihao",
                track_id=1,
                box_info=[
                    TrackingBoxInfo(
                        frame_index=i,
                        boxes={BoxType.HUMAN_BODY: (100 + i * 50, 200, 300, 400)},
                    )
                    for i in range(6)
                ],
            ),
        ],
    )


# =============================================================================
# 旧 convert_response 保留——仍可能被外部测试依赖
# =============================================================================


def convert_response(raw: dict[str, Any]) -> TrackingResponse:
    """将旧 PerceptionEngine raw dict 转为 TrackingResponse（已废弃，保留兼容）。"""
    raw_fi = raw.get("frames_info", {})
    frame_info = FrameInfo(
        start_timestamp=raw_fi.get("start_timestamp", 0),
        end_timestamp=raw_fi.get("end_timestamp", 0),
        fps=raw_fi.get("fps", 2),
    )

    object_info: list[TrackedObject] = []
    for obj in raw.get("objects_info", []):
        obj_type = _convert_type(obj.get("type", ""))
        face_id = obj.get("face_id", "none")
        track_id = obj.get("track_id", 0)

        box_info: list[TrackingBoxInfo] = []
        for entry in obj.get("box_info", []):
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                frame_idx = entry[0]
                boxes_raw = entry[1]
                boxes: dict[str, tuple[int, int, int, int]] = {}
                if isinstance(boxes_raw, dict):
                    for box_type, coords in boxes_raw.items():
                        if isinstance(coords, (list, tuple)) and len(coords) == 4:
                            boxes[box_type] = (
                                int(coords[0]),
                                int(coords[1]),
                                int(coords[2]),
                                int(coords[3]),
                            )
                box_info.append(TrackingBoxInfo(frame_index=int(frame_idx), boxes=boxes))

        object_info.append(
            TrackedObject(
                type=obj_type,
                face_id=face_id,
                track_id=track_id,
                box_info=box_info,
            )
        )

    return TrackingResponse(frame_info=frame_info, object_info=object_info)


def _convert_type(raw_type: str) -> ObjectType:
    type_map = {
        "human_with_face": ObjectType.HUMAN_WITH_FACE,
        "human_body": ObjectType.HUMAN_BODY,
        "human_face": ObjectType.HUMAN_FACE,
        "human": ObjectType.HUMAN,
        "pet": ObjectType.PET,
    }
    return type_map.get(raw_type, ObjectType.HUMAN)
