"""DeepSortTracker — DeepSORT 适配层(对外 API 与 SortTracker 同构)。

让 ``RealTrackingService`` / ``IdentityEngine`` 能透明切换 IoU+Kalman SORT 与
IoU+Kalman+ReID DeepSORT 两种实现:

    >>> from miloco.perception.engine.identity.deep_sort import DeepSortTracker
    >>> from miloco.perception.engine.config import DeepSortConfigDC
    >>> tracker = DeepSortTracker(detector=det, config=DeepSortConfigDC(), fps=1)
    >>> tracker.update(frame)                       # 同 SortTracker
    >>> results = tracker.get_tracking_results()    # 同字段集
    >>> emb = tracker.get_track_embedding(7)        # 额外:取 ReID 快照(零额外推理)
    >>> tracker.reset()                             # 同 SortTracker

本类是 ``MultiObjectTracker`` (DeepSORT 风格,IoU+CIoU+Kalman+ReID 关联级联) +
``HumanReID`` (ONNX) 的薄包装。所有 ReID embedding 都来自 DeepSORT 关联阶段
**已经算出的** ``Track.features`` deque 末尾元素——``get_track_embedding`` 取快照时
**零额外推理**(不再调 ``HumanReID.extract_feature``)。

配置分两层:
    - 业务调参 -> ``DeepSortConfigDC``(yaml 加载,精简 9 字段)
    - 底层 DeepSORT 内部参数 -> ``TrackerConfig`` 默认值(代码,不暴露 yaml)
内部把 ``DeepSortConfigDC`` 字段映射到对应的 ``TrackerConfig`` 字段;其它
``TrackerConfig`` 字段保持默认。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.identity._fps_utils import sec_to_frames

if TYPE_CHECKING:
    from miloco.perception.engine.config import DeepSortConfigDC

logger = logging.getLogger(__name__)


# =============================================================================
# 主类
# =============================================================================


class DeepSortTracker:
    """DeepSORT 多目标跟踪器,API 与 SortTracker 同构。

    Args:
        detector:  Detector(ONNX)实例,与 SortTracker 同源共享。
        config:    业务侧调参(``DeepSortConfigDC``),默认走 dataclass 默认值。
        fps:       主流程帧率,按 ``config.max_age_sec * fps`` 换算 track 存活帧数
                   (``TrackerConfig.max_age`` / ``human_max_lost_frames``)。
                   max_age_sec 是墙钟秒,各 fps 下一致生效(不再有 fps<=1 的特殊覆盖)。
        reid_model_path: ReID ONNX 路径;默认走 ``HumanReID`` 类内默认 path。
        use_gpu:   ReID 推理是否走 GPU(部署侧)。

    track_human_only:由 update() 后置剔除,pet 类目标不进 tracks,只保留 HUMAN class。
    """

    def __init__(
        self,
        detector,
        config: "DeepSortConfigDC | None" = None,
        fps: int = 1,
        reid_model_path: str | None = None,
        use_gpu: bool = False,
    ) -> None:
        from miloco.perception.engine.config import DeepSortConfigDC
        from miloco.perception.engine.identity.tracker.config import TrackerConfig
        from miloco.perception.engine.identity.tracker.detector import Detection
        from miloco.perception.engine.identity.tracker.human_reid import HumanReID
        from miloco.perception.engine.identity.tracker.tracker import MultiObjectTracker

        self.config = config or DeepSortConfigDC()
        self.detector = detector
        self.fps = max(1, int(fps))
        self._Detection = Detection

        # ReID(默认 v2 模型路径在 HumanReID 类内)
        if reid_model_path:
            self._human_reid = HumanReID(model_path=reid_model_path, use_gpu=use_gpu)
        else:
            self._human_reid = HumanReID(use_gpu=use_gpu)

        # 把 DeepSortConfigDC 业务字段映射到 TrackerConfig;其它字段
        # (max_cosine_distance / max_iou_distance / static_displacement_ratio /
        # static_min_abs_px 等)保持 TrackerConfig 默认。
        max_age_frames = sec_to_frames(self.config.max_age_sec, self.fps)
        tracker_cfg = TrackerConfig(
            mode=self.config.mode,
            max_age=max_age_frames,
            human_n_init=self.config.n_init,
            human_init_confidence=self.config.detector_conf_threshold,
            human_max_lost_frames=max_age_frames,
            human_reid_skip_windows=self.config.human_reid_skip_windows,
        )

        self._mot = MultiObjectTracker(
            detector=detector,
            human_reid=self._human_reid,
            config=tracker_cfg,
        )

    # ----- 对外 API(同 SortTracker) -----

    @property
    def human_reid(self):
        """暴露 HumanReID 实例,主要给注册流程做 tier_a 写入时的 emb 兜底抽取
        (TierU 池里 L1/L2 都没拉到 emb 的极端 race;走库的 ``reid_extractor`` 入参)。
        其它代码路径继续走 ``get_track_embedding`` 零额外推理通道。"""
        return self._human_reid

    def reset(self) -> None:
        self._mot.reset()

    def set_fps(self, fps: int) -> None:
        """运行时更新 fps：重算 max_age 帧数并写穿到内部 MultiObjectTracker.config。

        与 SortTracker 同理，fps 仅用于把 ``max_age_sec`` 换算成帧数（构造期算一次、
        烘进 TrackerConfig）。改 fps 无需重建 tracker：``_mot.config`` 是可变 dataclass，
        track 循环每帧现读 ``max_age`` / ``human_max_lost_frames``，改字段下一帧即生效。
        """
        self.fps = max(1, int(fps))
        max_age_frames = sec_to_frames(self.config.max_age_sec, self.fps)
        self._mot.config.max_age = max_age_frames
        self._mot.config.human_max_lost_frames = max_age_frames

    def update_with_detections(self, frame: NDArray[np.uint8], detections: list) -> None:
        """update() 的变体：跳过内部 detect，使用外部传入的检测结果。"""
        self._mot.update_with_detections(frame, detections)
        self._mot.tracks = [
            t for t in self._mot.tracks
            if t.class_id == self._Detection.CLASS_HUMAN
        ]

    def update(self, frame: NDArray[np.uint8]) -> None:
        """对单帧跑检测 + DeepSORT 关联(含 ReID 抽取或 fast 复用)。

        track_human_only 行为:MultiObjectTracker 在 pet 清理后内部已只创建 HUMAN tracks,
        此处 update 后再主动剔除非 HUMAN tracks 是 defensive 双保险——若未来 MOT 重新放开
        非 HUMAN tracks(face / pet),DeepSortTracker 这层仍能保证对外契约与 SortTracker
        同构:FACE/CAT/DOG 不形成 track,但保留在 ``last_detections`` 给 face matching 用。
        """
        self._mot.update(frame)
        self._mot.tracks = [
            t for t in self._mot.tracks
            if t.class_id == self._Detection.CLASS_HUMAN
        ]

    @property
    def last_detections(self) -> list:
        """给 face matching 用的最新一帧检测结果(含 FACE/CAT/DOG)。"""
        return self._mot.last_detections

    def get_tracking_results(self) -> list[dict[str, Any]]:
        """返回当前活跃 track 列表(字段集与 SortTracker 一致)。

        SortTracker 字段:id / class_id / bbox / xyxy / confidence / hits / age /
        time_since_update。DeepSORT Track 字段更多(features/mean/covariance/state),
        本方法做投影只保留对齐字段。
        """
        out: list[dict[str, Any]] = []
        for tr in self._mot.tracks:
            if not tr.is_confirmed:
                continue
            x, y, w, h = tr.bbox
            out.append({
                "id": tr.track_id,
                "class_id": tr.class_id,
                "bbox": (int(x), int(y), int(w), int(h)),
                "xyxy": (int(x), int(y), int(x + w), int(y + h)),
                "confidence": float(tr.confidence),
                "hits": tr.hits,
                "age": tr.age,
                "time_since_update": tr.time_since_update,
                # True ⟺ 本帧 Kalman update 真匹配到 detection；False = 纯预测
                # 残留（coasting）。下游用它拒"人已离开/跟丢"产生的残留框。
                "detected_this_frame": (tr.time_since_update == 0),
            })
        return out

    @property
    def tracks(self):
        """暴露 track 列表(与 SortTracker 同名属性,给调试与 track_id 访问用)。"""
        return self._mot.tracks

    # ----- 额外公开给陌生人池用 -----

    def get_track_embedding(self, track_id: int) -> NDArray[np.float32] | None:
        """取该 track 的最新 ReID embedding 快照(128-dim, L2-normalized)。

        ⚠️ **零额外推理**:直接读 ``Track.features`` deque 末尾元素——DeepSORT 关联
        阶段本来就为每检测框算了 embedding,这里只是把内存里已有的 numpy ndarray
        引用导出来,**不**调 ``HumanReID.extract_feature``。陌生人池调用本方法时,
        任何代码路径都不应该再次调 extract_feature(强制约束,有单测护栏)。

        Returns:
            128-dim float32 ndarray,或 None(track 不存在 / features deque 为空)。
        """
        for tr in self._mot.tracks:
            if tr.track_id != track_id:
                continue
            if not tr.features:
                return None
            return tr.features[-1]
        return None

    def get_track_centroid(
        self, track_id: int,
    ) -> tuple[NDArray[np.float32] | None, int]:
        """取该 track 的**历史特征质心**(features deque 均值再 L2)+ 参与均值的 emb 数。

        与 ``get_track_embedding`` 同源同约束——同样**零额外推理**(只读 ``Track.features``
        deque,不调 ``HumanReID.extract_feature``)——但返回 ``get_history_mean_feat()`` 的
        历史均值而非末尾单帧,抗瞬时噪声,供身份漂移自检拿稳定质心比对参考。

        Returns:
            ``(centroid, n_emb)``:centroid 为 128-dim float32(L2-normalized)或 None
            (track 不存在 / features 为空);n_emb 为当前 deque 长度(0 表示无)。
        """
        for tr in self._mot.tracks:
            if tr.track_id != track_id:
                continue
            n = len(tr.features)
            if n == 0:
                return None, 0
            return tr.get_history_mean_feat(), n
        return None, 0

    def get_track_embedding_age(self, track_id: int) -> int:
        """该 track 上次提取 ReID 距今多少帧(fast 模式 cache 复用判断用)。

        Returns -1 表示 track 不存在 / 从未提取过。
        """
        for tr in self._mot.tracks:
            if tr.track_id != track_id:
                continue
            if tr.last_reid_frame == 0:
                return -1
            return self._mot._global_frame_idx - tr.last_reid_frame
        return -1
