"""SortTracker — 轻量级 SORT 跟踪器，仅 IoU + Kalman，无 ReID 特征。

算法：SORT (Simple Online and Realtime Tracking, Bewley et al. 2016)。
本文件不引入 `filterpy` 依赖，自行实现卡尔曼滤波器（7 维状态：[x, y, s, r, vx, vy, vs]）。

适配本工程的差异：
- `update(frame)` 内部调用 `Detector` 跑检测，而非接收 `dets` 数组（与旧 `MultiObjectTracker` 接口一致）。
- 仅跟踪 HUMAN 类目标（`SortConfig.track_human_only=True`）；其他类（FACE/CAT/DOG/HEAD）
  不形成 track，但保留在 `last_detections` 中供 face matching 使用。
- `get_tracking_results()` 输出 dict 列表，字段含 ``id / class_id / bbox / xyxy /
  confidence / hits / age / time_since_update``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import linear_sum_assignment

from miloco.perception.engine.identity._fps_utils import sec_to_frames

if TYPE_CHECKING:
    from miloco.perception.engine.identity.tracker.detector import Detection, Detector


# =============================================================================
# 配置
# =============================================================================


@dataclass
class SortConfig:
    """SortTracker 调参集合。

    n_init / max_age_sec 默认值偏松：减少 SORT 漏检瞬时目标的概率，让短暂丢失的 track 多坚持。
    """

    n_init: int = 1                  # 首帧 confirmed bbox 即形成 track
    max_age_sec: float = 1.0         # 丢失多久（**真实世界秒数**）后删 track；
                                     # 与 SortConfigDC 默认值对齐（避免单测 / RealTrackingService(sort_config=None) 走错档）
                                     # 实际帧数 = max_age_sec × fps（SortTracker 实例化时换算）
    iou_threshold: float = 0.3       # 匈牙利匹配 IoU 下限
    detector_conf_threshold: float = 0.5  # 检测置信下限（< 此值的 detection 不入 track）
    track_human_only: bool = True    # 只对 HUMAN 类形成 track；其他类不跟踪


# =============================================================================
# 卡尔曼滤波器（自实现，不依赖 filterpy）
# =============================================================================


class _KalmanFilter:
    """7 维状态恒速模型卡尔曼滤波器。

    状态向量 x = [cx, cy, s, r, vcx, vcy, vs]:
        cx, cy : bbox 中心点
        s      : bbox 面积 (= w * h)
        r      : 宽高比 (= w / h)
        vcx, vcy, vs : 对应一阶导（r 假定恒定，无 vr）

    观测向量 z = [cx, cy, s, r]（4 维）。

    与 SORT 原版（filterpy 实现）等效，但使用纯 numpy。
    """

    def __init__(self) -> None:
        self.dim_x = 7
        self.dim_z = 4

        # 状态转移矩阵 F（7x7，恒速模型）
        self.F = np.eye(self.dim_x, dtype=np.float64)
        self.F[0, 4] = 1.0  # cx += vcx
        self.F[1, 5] = 1.0  # cy += vcy
        self.F[2, 6] = 1.0  # s  += vs

        # 观测矩阵 H（4x7，从状态取 [cx, cy, s, r]）
        self.H = np.zeros((self.dim_z, self.dim_x), dtype=np.float64)
        self.H[0, 0] = 1.0
        self.H[1, 1] = 1.0
        self.H[2, 2] = 1.0
        self.H[3, 3] = 1.0

        # 测量噪声 R（4x4，对 s 维度噪声放大 10×，与 SORT 原版一致）
        self.R = np.eye(self.dim_z, dtype=np.float64)
        self.R[2:, 2:] *= 10.0

        # 状态协方差 P 初始化（7x7，速度维度赋大不确定性）
        self.P = np.eye(self.dim_x, dtype=np.float64) * 10.0
        self.P[4:, 4:] *= 1000.0  # 速度维度高不确定

        # 过程噪声 Q（7x7，s 速度噪声压低）
        self.Q = np.eye(self.dim_x, dtype=np.float64)
        self.Q[6, 6] *= 0.01
        self.Q[4:, 4:] *= 0.01

        # 状态向量
        self.x = np.zeros((self.dim_x, 1), dtype=np.float64)

    def init(self, z: NDArray[np.float64]) -> None:
        """用首次观测初始化状态。z = [cx, cy, s, r]。"""
        self.x = np.zeros((self.dim_x, 1), dtype=np.float64)
        self.x[:4, 0] = z

    def predict(self) -> None:
        """前进一步状态预测。"""
        # SORT 原版的小修正：当 s + vs ≤ 0 时把 vs 清零，避免面积变负
        if (self.x[6, 0] + self.x[2, 0]) <= 0:
            self.x[6, 0] = 0.0
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q

    def update(self, z: NDArray[np.float64]) -> None:
        """根据观测 z 更新状态。z = [cx, cy, s, r]。"""
        z = z.reshape(self.dim_z, 1)
        y = z - self.H @ self.x                    # innovation
        S = self.H @ self.P @ self.H.T + self.R    # innovation covariance
        K = self.P @ self.H.T @ np.linalg.inv(S)   # Kalman gain
        self.x = self.x + K @ y
        eye = np.eye(self.dim_x, dtype=np.float64)
        self.P = (eye - K @ self.H) @ self.P


# =============================================================================
# 单 track 包装
# =============================================================================


class KalmanBoxTracker:
    """单个 track 的卡尔曼跟踪状态包装。

    track id 由调用方（``SortTracker``）显式传入，避免类级全局计数器导致多
    SortTracker 实例间 ID 互相干扰（如多房间共存时一个 tracker 的 reset 重
    置全局计数器，会让其他 tracker 后续新建的 track 拿到与已有 IdentityEngine
    state key 冲突的 ID）。
    """

    def __init__(
        self,
        bbox_xyxy: NDArray[np.float64],
        class_id: int,
        confidence: float,
        track_id: int,
    ) -> None:
        self.kf = _KalmanFilter()
        self.kf.init(_xyxy_to_z(bbox_xyxy))

        self.id: int = track_id

        self.class_id: int = class_id
        self.last_confidence: float = confidence

        # SORT 生命周期统计
        self.time_since_update: int = 0    # 距上次匹配 detection 的帧数
        self.hits: int = 0                 # 累计匹配到的 detection 数
        self.hit_streak: int = 0           # 连续匹配的帧数
        self.age: int = 0                  # 总生命周期（帧数）

    def predict(self) -> NDArray[np.float64]:
        """前进一步并返回预测 bbox（[x1,y1,x2,y2]）。"""
        self.kf.predict()
        self.age += 1
        if self.time_since_update > 0:
            self.hit_streak = 0
        self.time_since_update += 1
        return _z_to_xyxy(self.kf.x[:4, 0])

    def update(self, bbox_xyxy: NDArray[np.float64], confidence: float) -> None:
        """用观测 bbox 更新状态。"""
        self.time_since_update = 0
        self.hits += 1
        self.hit_streak += 1
        self.last_confidence = confidence
        self.kf.update(_xyxy_to_z(bbox_xyxy))

    def get_state(self) -> NDArray[np.float64]:
        """返回当前估计 bbox（[x1,y1,x2,y2]）。"""
        return _z_to_xyxy(self.kf.x[:4, 0])


# =============================================================================
# bbox 形态转换
# =============================================================================


def _xyxy_to_z(bbox: NDArray[np.float64]) -> NDArray[np.float64]:
    """[x1, y1, x2, y2] → [cx, cy, s, r]。"""
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    cx = bbox[0] + w / 2.0
    cy = bbox[1] + h / 2.0
    s = w * h
    # 防止 h=0 时除零
    r = w / max(h, 1e-6)
    return np.array([cx, cy, s, r], dtype=np.float64)


def _z_to_xyxy(z: NDArray[np.float64]) -> NDArray[np.float64]:
    """[cx, cy, s, r] → [x1, y1, x2, y2]。"""
    s = max(float(z[2]), 0.0)
    r = max(float(z[3]), 1e-6)
    w = float(np.sqrt(s * r))
    h = s / max(w, 1e-6)
    return np.array([z[0] - w / 2.0, z[1] - h / 2.0, z[0] + w / 2.0, z[1] + h / 2.0], dtype=np.float64)


# =============================================================================
# 匹配工具
# =============================================================================


def _iou_matrix(dets: NDArray[np.float64], trks: NDArray[np.float64]) -> NDArray[np.float64]:
    """批量 IoU 矩阵。dets, trks 形状 (N, 4) / (M, 4) 都是 [x1,y1,x2,y2]。返回 (N, M)。"""
    if len(dets) == 0 or len(trks) == 0:
        return np.zeros((len(dets), len(trks)), dtype=np.float64)
    dets = np.asarray(dets, dtype=np.float64)
    trks = np.asarray(trks, dtype=np.float64)
    # 广播：(N, 1, 4) vs (1, M, 4)
    d = dets[:, np.newaxis, :]
    t = trks[np.newaxis, :, :]
    xx1 = np.maximum(d[..., 0], t[..., 0])
    yy1 = np.maximum(d[..., 1], t[..., 1])
    xx2 = np.minimum(d[..., 2], t[..., 2])
    yy2 = np.minimum(d[..., 3], t[..., 3])
    w = np.maximum(0.0, xx2 - xx1)
    h = np.maximum(0.0, yy2 - yy1)
    inter = w * h
    area_d = (d[..., 2] - d[..., 0]) * (d[..., 3] - d[..., 1])
    area_t = (t[..., 2] - t[..., 0]) * (t[..., 3] - t[..., 1])
    union = area_d + area_t - inter
    return inter / np.maximum(union, 1e-9)


def _associate(
    dets: NDArray[np.float64],
    trks: NDArray[np.float64],
    iou_threshold: float,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """关联 detection 和 tracker。

    返回：
        matches:               list of (det_idx, trk_idx)
        unmatched_detections:  list of det_idx
        unmatched_trackers:    list of trk_idx
    """
    n_det, n_trk = len(dets), len(trks)
    if n_det == 0:
        return [], [], list(range(n_trk))
    if n_trk == 0:
        return [], list(range(n_det)), []

    iou = _iou_matrix(dets, trks)

    # 用匈牙利算法求 cost = -IoU 的最小化匹配
    # scipy.optimize.linear_sum_assignment 返回 (row_idx, col_idx)
    row_idx, col_idx = linear_sum_assignment(-iou)

    matches: list[tuple[int, int]] = []
    matched_d, matched_t = set(), set()
    for r, c in zip(row_idx, col_idx):
        if iou[r, c] < iou_threshold:
            continue
        matches.append((int(r), int(c)))
        matched_d.add(int(r))
        matched_t.add(int(c))

    unmatched_dets = [i for i in range(n_det) if i not in matched_d]
    unmatched_trks = [i for i in range(n_trk) if i not in matched_t]
    return matches, unmatched_dets, unmatched_trks


# =============================================================================
# 主类
# =============================================================================


class SortTracker:
    """SORT 多目标跟踪器（无 ReID 特征）。

    内部持有 Detector，每次 ``update(frame)`` 跑一次检测 + 跟踪。
    与现网 ``MultiObjectTracker`` 接口兼容：
      - ``update(frame: NDArray[uint8]) -> None``
      - ``last_detections: list[Detection]`` （供 face matching 用）
      - ``get_tracking_results() -> list[dict]``
      - ``reset() -> None``
    """

    def __init__(
        self,
        config: SortConfig,
        detector: "Detector",
        fps: int = 1,
    ) -> None:
        self.config = config
        self.detector = detector
        self.fps = max(1, int(fps))

        # 把秒数换算成帧数（暴露给配置层的是真实秒数 max_age_sec）；换算与 set_fps
        # 共用 sec_to_frames，杜绝构造期↔热更公式漂移。
        self._max_age_frames: int = sec_to_frames(config.max_age_sec, self.fps)

        self._tracks: list[KalmanBoxTracker] = []
        self._frame_count: int = 0
        # 实例级 track id 计数器（避免与同进程其他 SortTracker 实例互相干扰）
        self._next_track_id: int = 0
        self.last_detections: list["Detection"] = []

    def reset(self) -> None:
        """清空跟踪状态（重新开始跟踪场景时调用，如切换摄像头）。"""
        self._tracks.clear()
        self._frame_count = 0
        self._next_track_id = 0
        self.last_detections = []

    def set_fps(self, fps: int) -> None:
        """运行时更新 fps：仅重算 max_age 帧数阈值，不清跟踪状态。

        fps 在本 tracker 里只用于把 ``max_age_sec`` 换算成帧数（构造期算一次）；
        Kalman / 关联逻辑均工作在帧单位、与 fps 无关。故改 fps 无需重建 tracker，
        重算 ``_max_age_frames`` 后活跃 track 下一帧的删除判定即用新阈值。
        """
        self.fps = max(1, int(fps))
        self._max_age_frames = sec_to_frames(self.config.max_age_sec, self.fps)

    def update_with_detections(
        self, frame: NDArray[np.uint8], all_dets: list
    ) -> None:
        """update() 的变体：跳过内部 detect，使用外部传入的检测结果。"""
        self._frame_count += 1
        self.last_detections = all_dets
        self._update_core(frame, all_dets)

    def update(self, frame: NDArray[np.uint8]) -> None:
        """对单帧跑检测 + 跟踪。

        Args:
            frame: BGR uint8 图像，shape (H, W, 3)
        """

        self._frame_count += 1

        # 1. 检测
        all_dets: list[Detection] = self.detector.detect(frame)
        self.last_detections = all_dets
        self._update_core(frame, all_dets)

    def _update_core(self, frame: NDArray[np.uint8], all_dets: list) -> None:
        """检测结果已就绪，执行过滤 + 跟踪逻辑。"""
        from miloco.perception.engine.identity.tracker.detector import Detection

        # 2. 过滤待跟踪类（默认仅 HUMAN）
        if self.config.track_human_only:
            track_dets = [
                d for d in all_dets
                if d.class_id == Detection.CLASS_HUMAN
                and d.confidence >= self.config.detector_conf_threshold
            ]
        else:
            # 跟踪 HUMAN + CAT + DOG（未来可能用）
            track_dets = [
                d for d in all_dets
                if d.class_id in (Detection.CLASS_HUMAN, Detection.CLASS_CAT, Detection.CLASS_DOG)
                and d.confidence >= self.config.detector_conf_threshold
            ]

        # 3. 已有 tracker 前进一步预测
        predicted_xyxy: list[NDArray[np.float64]] = []
        to_del: list[int] = []
        for i, trk in enumerate(self._tracks):
            pos = trk.predict()
            if np.any(np.isnan(pos)):
                to_del.append(i)
                continue
            predicted_xyxy.append(pos)
        # NaN tracker 的预测从未进入 predicted_xyxy，只需从 _tracks 中倒序剔除
        for idx in reversed(to_del):
            self._tracks.pop(idx)

        # 4. 关联
        det_xyxy_arr = np.array(
            [list(d.xyxy) for d in track_dets],
            dtype=np.float64,
        ) if track_dets else np.empty((0, 4), dtype=np.float64)
        trk_xyxy_arr = np.array(predicted_xyxy, dtype=np.float64) if predicted_xyxy else np.empty((0, 4), dtype=np.float64)

        matches, unmatched_dets, unmatched_trks = _associate(
            det_xyxy_arr, trk_xyxy_arr, self.config.iou_threshold,
        )

        # 5. 已匹配的 tracker 用 detection 更新
        for det_idx, trk_idx in matches:
            d = track_dets[det_idx]
            self._tracks[trk_idx].update(np.asarray(d.xyxy, dtype=np.float64), d.confidence)

        # 6. 未匹配的 detection 形成新 tracker
        for det_idx in unmatched_dets:
            d = track_dets[det_idx]
            new_trk = KalmanBoxTracker(
                np.asarray(d.xyxy, dtype=np.float64),
                d.class_id,
                d.confidence,
                track_id=self._next_track_id,
            )
            self._next_track_id += 1
            self._tracks.append(new_trk)

        # 7. 删除死 tracker（time_since_update > max_age）
        self._tracks = [t for t in self._tracks if t.time_since_update <= self._max_age_frames]

    def get_tracking_results(self) -> list[dict[str, Any]]:
        """返回当前活跃 track 列表。

        过滤规则（与 SORT 原版一致）：
          - 仅返回 ``time_since_update < 1`` 的 track（本帧匹配上的）
          - 且满足 ``hit_streak >= n_init`` 或 ``frame_count <= n_init``（早期帧不卡 n_init）

        每条 dict 字段：
            id (int):           track_id
            class_id (int):     detector 类别 id
            bbox (tuple):       (x, y, w, h) 整数像素
            xyxy (tuple):       (x1, y1, x2, y2) 整数像素
            confidence (float): 最近一次匹配的 detection 置信度
            hits (int):         累计匹配数
            age (int):          生命周期
            time_since_update (int): 距上次匹配的帧数
        """
        results: list[dict[str, Any]] = []
        for trk in self._tracks:
            if trk.time_since_update >= 1:
                continue
            if not (trk.hit_streak >= self.config.n_init or self._frame_count <= self.config.n_init):
                continue
            xyxy = trk.get_state()
            x1, y1, x2, y2 = (int(round(v)) for v in xyxy)
            w, h = max(0, x2 - x1), max(0, y2 - y1)
            results.append({
                "id": trk.id,
                "class_id": trk.class_id,
                "bbox": (x1, y1, w, h),
                "xyxy": (x1, y1, x2, y2),
                "confidence": float(trk.last_confidence),
                "hits": trk.hits,
                "age": trk.age,
                "time_since_update": trk.time_since_update,
                # SortTracker 在 L419 已 pre-filter time_since_update >= 1，此处
                # 永远为 True；保留字段是为消费端代码统一（DeepSORT 路径会变）。
                "detected_this_frame": True,
            })
        return results

    @property
    def tracks(self) -> list[KalmanBoxTracker]:
        """暴露 track 列表，供调试与外部访问 track_id。"""
        return self._tracks
