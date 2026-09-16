#!/usr/bin/env python3
"""
多目标跟踪器模块
人体多目标跟踪
基于DeepSort算法实现
同步自 src/tbd/tracker_mot_v2.cpp, track.cpp, track_method.cpp
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np

from .config import TrackerConfig
from .detector import Detection, Detector
from .human_reid import HumanReID

_LOGGER = logging.getLogger(__name__)

# 算法常量 (不可配置)
INFTY_COST = 1e5
CHI2INV95_4D = 9.4877  # chi-squared 95% threshold for 4D gating
NUM_MAX_KEEP_FEAT = 50


def _compute_ciou(bbox1_xyxy, bbox2_xyxy):
    """
    计算CIoU (Complete IoU)
    同步自 iou.h 中的 ciou 函数
    """
    x1_1, y1_1, x2_1, y2_1 = bbox1_xyxy
    x1_2, y1_2, x2_2, y2_2 = bbox2_xyxy

    # 交集
    inter_x1 = max(x1_1, x1_2)
    inter_y1 = max(y1_1, y1_2)
    inter_x2 = min(x2_1, x2_2)
    inter_y2 = min(y2_1, y2_2)
    inter_area = max(0, inter_x2 - inter_x1) * max(0, inter_y2 - inter_y1)

    # 各自面积
    area1 = max(0, (x2_1 - x1_1)) * max(0, (y2_1 - y1_1))
    area2 = max(0, (x2_2 - x1_2)) * max(0, (y2_2 - y1_2))
    union_area = area1 + area2 - inter_area

    if union_area <= 0:
        return 0.0

    iou = inter_area / union_area

    # 最小包围框
    c_x1 = min(x1_1, x1_2)
    c_y1 = min(y1_1, y1_2)
    c_x2 = max(x2_1, x2_2)
    c_y2 = max(y2_1, y2_2)
    c_diag_sq = (c_x2 - c_x1) ** 2 + (c_y2 - c_y1) ** 2

    if c_diag_sq <= 0:
        return iou

    # 中心距
    cx1 = (x1_1 + x2_1) / 2
    cy1 = (y1_1 + y2_1) / 2
    cx2 = (x1_2 + x2_2) / 2
    cy2 = (y1_2 + y2_2) / 2
    d_sq = (cx1 - cx2) ** 2 + (cy1 - cy2) ** 2

    # 宽高比一致性
    w1 = x2_1 - x1_1
    h1 = y2_1 - y1_1
    w2 = x2_2 - x1_2
    h2 = y2_2 - y1_2

    v = (4 / (np.pi**2)) * (
        np.arctan(w1 / max(h1, 1e-6)) - np.arctan(w2 / max(h2, 1e-6))
    ) ** 2
    alpha = v / (1 - iou + v + 1e-6)

    ciou = iou - d_sq / c_diag_sq - alpha * v
    return ciou


@dataclass
class Track:
    """
    跟踪轨迹数据类
    同步自 track.h / track.cpp
    """

    track_id: int
    class_id: int
    bbox: Tuple[int, int, int, int]  # (x, y, w, h)
    confidence: float = 1.0
    features: deque = field(default_factory=lambda: deque(maxlen=NUM_MAX_KEEP_FEAT))
    boxes: deque = field(default_factory=lambda: deque(maxlen=NUM_MAX_KEEP_FEAT))
    age: int = 0  # m_ages
    hits: int = 0  # m_hits
    time_since_update: int = 0  # last_time_update
    state: str = "tentative"
    n_init: int = 3

    # DeepSORT 卡尔曼滤波状态 (用于 gating)
    mean: np.ndarray = None
    covariance: np.ndarray = None

    # fast 模式扩展字段
    last_reid_frame: int = 0  # 上次提取 ReID 的全局帧号
    is_static: bool = False  # 当前是否判定为静止
    face_id: str | None = None  # 已确认的人脸身份
    face_confirm_hits: int = 0  # 人脸连续命中次数
    _last_face_hit_id: str | None = field(default=None, repr=False)  # 上次命中的 face id

    def __post_init__(self):
        if not isinstance(self.features, deque):
            self.features = deque(self.features, maxlen=NUM_MAX_KEEP_FEAT)
        if not isinstance(self.boxes, deque):
            self.boxes = deque(self.boxes, maxlen=NUM_MAX_KEEP_FEAT)

    @property
    def is_confirmed(self) -> bool:
        return self.state == "confirmed"

    @property
    def is_tentative(self) -> bool:
        return self.state == "tentative"

    @property
    def is_deleted(self) -> bool:
        return self.state == "deleted"

    @property
    def class_name(self) -> str:
        class_names = {
            Detection.CLASS_HUMAN: "human",
            Detection.CLASS_FACE: "face",
            Detection.CLASS_CAT: "cat",
            Detection.CLASS_DOG: "dog",
            Detection.CLASS_HEAD: "head",
        }
        return class_names.get(self.class_id, "unknown")

    @property
    def xyxy(self) -> Tuple[int, int, int, int]:
        x, y, w, h = self.bbox
        return (x, y, x + w, y + h)

    @property
    def xyah(self) -> np.ndarray:
        """(center_x, center_y, aspect_ratio, height) 格式, 用于DeepSORT Kalman"""
        x, y, w, h = self.bbox
        center_x = x + w / 2
        center_y = y + h / 2
        aspect_ratio = w / h if h > 0 else 1.0
        return np.array([center_x, center_y, aspect_ratio, h], dtype=np.float32)

    def predict(self, kalman: "KalmanFilter"):
        """
        预测步骤, 对应C++ predict_v2()
        在每帧匹配前调用, 推进Kalman状态并递增 time_since_update
        """
        if self.mean is not None and self.covariance is not None:
            self.mean, self.covariance = kalman.predict(self.mean, self.covariance)
        self.age += 1
        self.time_since_update += 1

    def update(self, detection: Detection, feature: np.ndarray, kalman: "KalmanFilter"):
        """
        更新轨迹, 对应C++ update_v3()
        """
        self.bbox = detection.bbox
        self.confidence = detection.confidence
        self.features.append(feature)
        self.boxes.append(detection.bbox)
        self.hits += 1
        self.time_since_update = 0

        # 更新Kalman滤波器
        if self.mean is not None and self.covariance is not None:
            measurement = self.xyah
            self.mean, self.covariance = kalman.update(
                self.mean, self.covariance, measurement
            )

        # 状态转移: tentative -> confirmed (同步C++ update_v3)
        if self.is_tentative and self.hits >= self.n_init:
            self.state = "confirmed"

    def mark_missed(self, max_time_can_lost: int):
        """
        标记为丢失, 对应C++ mark_missed()
        注意: 不递增time_since_update (已在predict中递增)
        """
        if self.is_tentative:
            self.state = "deleted"
        elif self.time_since_update > max_time_can_lost:
            self.state = "deleted"

    def mark_deleted(self):
        self.state = "deleted"

    def get_history_mean_feat(self) -> np.ndarray:
        """
        获取历史特征均值并L2归一化
        对应C++ get_history_mean_feat(true)
        """
        if len(self.features) == 0:
            return None
        mean_feat = np.mean(list(self.features), axis=0)
        norm = np.linalg.norm(mean_feat)
        if norm > 0:
            mean_feat = mean_feat / norm
        return mean_feat


class KalmanFilter:
    """
    DeepSORT 卡尔曼滤波器
    同步自 deepsort 实现, 用于状态预测和Mahalanobis门控
    """

    def __init__(self):
        ndim = 4
        self._motion_mat = np.eye(2 * ndim, 2 * ndim)
        for i in range(ndim):
            self._motion_mat[i, ndim + i] = 1.0

        self._update_mat = np.eye(ndim, 2 * ndim)

        self._std_weight_position = 1.0 / 20
        self._std_weight_velocity = 1.0 / 160

    def initiate(self, measurement: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        mean_pos = measurement
        mean_vel = np.zeros_like(mean_pos)
        mean = np.r_[mean_pos, mean_vel]

        std = [
            2 * self._std_weight_position * measurement[3],
            2 * self._std_weight_position * measurement[3],
            1e-2,
            2 * self._std_weight_position * measurement[3],
            10 * self._std_weight_velocity * measurement[3],
            10 * self._std_weight_velocity * measurement[3],
            1e-5,
            10 * self._std_weight_velocity * measurement[3],
        ]
        covariance = np.diag(np.square(std))
        return mean, covariance

    def predict(
        self, mean: np.ndarray, covariance: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        std_pos = [
            self._std_weight_position * mean[3],
            self._std_weight_position * mean[3],
            1e-2,
            self._std_weight_position * mean[3],
        ]
        std_vel = [
            self._std_weight_velocity * mean[3],
            self._std_weight_velocity * mean[3],
            1e-5,
            self._std_weight_velocity * mean[3],
        ]
        motion_cov = np.diag(np.square(np.r_[std_pos, std_vel]))

        mean = self._motion_mat @ mean
        covariance = self._motion_mat @ covariance @ self._motion_mat.T + motion_cov
        return mean, covariance

    def project(
        self, mean: np.ndarray, covariance: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        std = [
            self._std_weight_position * mean[3],
            self._std_weight_position * mean[3],
            1e-1,
            self._std_weight_position * mean[3],
        ]
        innovation_cov = np.diag(np.square(std))

        mean = self._update_mat @ mean
        covariance = self._update_mat @ covariance @ self._update_mat.T + innovation_cov
        return mean, covariance

    def update(
        self, mean: np.ndarray, covariance: np.ndarray, measurement: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        projected_mean, projected_cov = self.project(mean, covariance)

        # K = P H^T S^{-1}, where S = projected_cov (4x4), P H^T (8x4)
        # Solve S x = (P H^T)^T => x = S^{-1} (P H^T)^T, then K = x^T
        cross_cov = covariance @ self._update_mat.T  # 8x4
        chol_factor = np.linalg.cholesky(projected_cov)  # 4x4 lower triangular
        # scipy 惰性 import：slim 版不安装 scipy（DeepSort 链路只在完整版使用）
        import scipy.linalg

        kalman_gain = scipy.linalg.cho_solve(
            (chol_factor, True),
            cross_cov.T,  # solve S x = cross_cov^T (4x8)
        ).T  # x^T = 8x4

        innovation = measurement - projected_mean
        new_mean = mean + innovation @ kalman_gain.T
        new_covariance = covariance - kalman_gain @ projected_cov @ kalman_gain.T

        return new_mean, new_covariance

    def gating_distance(
        self, mean: np.ndarray, covariance: np.ndarray, measurements: np.ndarray
    ) -> np.ndarray:
        """
        计算Mahalanobis门控距离
        对应C++ KF_gating_distance (4D, 非only_position)
        """
        projected_mean, projected_cov = self.project(mean, covariance)

        chol_factor = np.linalg.cholesky(projected_cov)
        d = measurements - projected_mean
        z = np.linalg.solve(chol_factor, d.T).T
        return np.sum(z * z, axis=1)


class MultiObjectTracker:
    """
    多目标跟踪器
    同步自 tracker_mot_v2.cpp
    """

    def __init__(
        self,
        detector: Detector,
        human_reid: HumanReID,
        config: TrackerConfig | None = None,
    ):
        self.detector = detector
        self.human_reid = human_reid
        self.config = config or TrackerConfig()

        self.tracks: List[Track] = []
        self.next_id = 0
        self.kalman = KalmanFilter()

        # 全局帧计数 + 检测结果缓存
        self._global_frame_idx = 0
        self._last_detections: List[Detection] = []

    @property
    def last_detections(self) -> List[Detection]:
        """上一次 update() 的检测结果，供 api 层复用以避免双重检测。"""
        return self._last_detections

    def reset(self):
        """重置跟踪器状态，允许跨调用复用同一实例"""
        self.tracks = []
        self.next_id = 0
        self._global_frame_idx = 0
        self._last_detections = []

    def _extract_feature(self, image: np.ndarray, detection: Detection) -> np.ndarray:
        x, y, w, h = detection.bbox
        crop = image[y : y + h, x : x + w]

        if crop.size == 0:
            return np.zeros(128)

        if detection.class_id == Detection.CLASS_HUMAN:
            return self.human_reid.extract_feature(crop)
        return np.zeros(128)

    # ============================================================
    # fast 模式辅助方法
    # ============================================================

    def _is_track_static(self, track: Track) -> bool:
        """基于最近两帧 bbox 中心位移判断是否静止。"""
        if len(track.boxes) < 2:
            return False
        cur = track.boxes[-1]  # (x, y, w, h)
        prev = track.boxes[-2]
        diag = (cur[2] ** 2 + cur[3] ** 2) ** 0.5
        dx = (cur[0] + cur[2] / 2) - (prev[0] + prev[2] / 2)
        dy = (cur[1] + cur[3] / 2) - (prev[1] + prev[3] / 2)
        displacement = (dx**2 + dy**2) ** 0.5
        cfg = self.config
        return (
            displacement / max(diag, 1.0) < cfg.static_displacement_ratio
            and displacement < cfg.static_min_abs_px
        )

    def _get_reid_interval(self) -> int:
        """计算 ReID 提取间隔帧数。"""
        cfg = self.config
        return int(cfg.window_len_sec * cfg.window_fps * cfg.human_reid_skip_windows)

    def _preliminary_iou_match(
        self, confirmed_tracks: List[Track], detections: List[Detection]
    ) -> Dict[int, Track]:
        """快速 IoU 预匹配 (贪心), 返回 {det_idx: track}。

        仅用于 fast 模式 ReID 跳过决策，不影响正式匹配流程。
        """
        if not confirmed_tracks or not detections:
            return {}

        # 构建 IoU 矩阵
        n_tracks = len(confirmed_tracks)
        n_dets = len(detections)
        iou_matrix = np.zeros((n_tracks, n_dets), dtype=np.float32)
        for i, track in enumerate(confirmed_tracks):
            for j, det in enumerate(detections):
                iou_matrix[i, j] = _compute_ciou(track.xyxy, det.xyxy)

        result: Dict[int, Track] = {}
        used_tracks = set()
        # 贪心: 按 IoU 降序分配
        while True:
            if iou_matrix.size == 0:
                break
            best = np.unravel_index(np.argmax(iou_matrix), iou_matrix.shape)
            best_iou = iou_matrix[best]
            if best_iou < 0.3:  # 最低 IoU 门限
                break
            ti, dj = best
            if ti not in used_tracks and dj not in result:
                result[dj] = confirmed_tracks[ti]
                used_tracks.add(ti)
            iou_matrix[ti, :] = -1
            iou_matrix[:, dj] = -1

        return result

    def _extract_features_fast(
        self,
        image: np.ndarray,
        detections: List[Detection],
        tracks: List[Track],
    ) -> List[np.ndarray]:
        """fast 模式特征提取: 静止 confirmed track 复用缓存特征。"""
        confirmed = [t for t in tracks if t.is_confirmed]
        pre_match = self._preliminary_iou_match(confirmed, detections)
        reid_interval = self._get_reid_interval()

        features = []
        reid_count = 0
        cache_count = 0
        for det_idx, det in enumerate(detections):
            matched_track = pre_match.get(det_idx)
            if (
                matched_track is not None
                and matched_track.is_confirmed
                and self._is_track_static(matched_track)
                and (self._global_frame_idx - matched_track.last_reid_frame)
                < reid_interval
            ):
                # 复用缓存特征
                cached = matched_track.get_history_mean_feat()
                if cached is not None:
                    features.append(cached)
                    matched_track.is_static = True
                    cache_count += 1
                    continue

            # 正常提取
            feat = self._extract_feature(image, det)
            features.append(feat)
            reid_count += 1
            if matched_track is not None:
                matched_track.last_reid_frame = self._global_frame_idx
                matched_track.is_static = False

        if reid_count + cache_count > 0:
            _LOGGER.debug(
                f"  [fast-ReID] human: extracted={reid_count}, cached={cache_count}"
            )
        return features

    # ============================================================
    # 成本矩阵计算 (同步自 track_method.cpp)
    # ============================================================

    def _cost_cosine(
        self,
        tracks: List[Track],
        features: List[np.ndarray],
        track_indices: List[int],
        detection_indices: List[int],
    ) -> np.ndarray:
        """
        余弦距离成本矩阵
        对应C++ cost_cosine + cost_track2det
        """
        cost_matrix = np.zeros(
            (len(track_indices), len(detection_indices)), dtype=np.float32
        )

        for i, track_idx in enumerate(track_indices):
            track = tracks[track_idx]
            track_feat = track.get_history_mean_feat()
            if track_feat is None:
                cost_matrix[i, :] = 1.0
                continue

            for j, det_idx in enumerate(detection_indices):
                det_feat = features[det_idx]
                det_norm = np.linalg.norm(det_feat)
                if det_norm > 0:
                    det_feat_n = det_feat / det_norm
                else:
                    det_feat_n = det_feat

                # 对应C++ _cosine_distance: L2 normalize both, euclidean dist, cos_sim = 1 - d^2/2
                euc_dist = np.linalg.norm(track_feat - det_feat_n)
                cos_sim = 1.0 - (euc_dist * euc_dist * 0.5)
                cost_matrix[i, j] = 1.0 - cos_sim

        return cost_matrix

    def _gate_cost_matrix(
        self,
        cost_matrix: np.ndarray,
        tracks: List[Track],
        features: List[np.ndarray],
        detections: List[Detection],
        track_indices: List[int],
        detection_indices: List[int],
    ) -> np.ndarray:
        """
        Mahalanobis门控, 对应C++ gate_cost_matrix
        超过chi2阈值的entry设为INFTY_COST
        """
        # 构建measurement矩阵: 每个detection的xyah格式
        measurements = np.array(
            [self._det_to_xyah(detections[det_idx]) for det_idx in detection_indices]
        )

        for i, track_idx in enumerate(track_indices):
            track = tracks[track_idx]
            if track.mean is None or track.covariance is None:
                continue

            gating_dist = self.kalman.gating_distance(
                track.mean, track.covariance, measurements
            )

            for j in range(len(detection_indices)):
                if gating_dist[j] > CHI2INV95_4D:
                    cost_matrix[i, j] = INFTY_COST

        return cost_matrix

    def _gated_metric(
        self,
        tracks: List[Track],
        features: List[np.ndarray],
        detections: List[Detection],
        track_indices: List[int],
        detection_indices: List[int],
    ) -> np.ndarray:
        """
        门控度量 = 余弦距离 + Mahalanobis门控
        对应C++ gated_metric = cost_cosine + gate_cost_matrix
        """
        cost_matrix = self._cost_cosine(
            tracks, features, track_indices, detection_indices
        )
        cost_matrix = self._gate_cost_matrix(
            cost_matrix, tracks, features, detections, track_indices, detection_indices
        )
        return cost_matrix

    def _iou_cost(
        self,
        tracks: List[Track],
        detections: List[Detection],
        track_indices: List[int],
        detection_indices: List[int],
    ) -> np.ndarray:
        """
        IoU成本矩阵
        对应C++ iou_cost: 使用CIoU, time_since_update>1的track设为INFTY_COST
        """
        cost_matrix = np.zeros(
            (len(track_indices), len(detection_indices)), dtype=np.float32
        )

        for i, track_idx in enumerate(track_indices):
            track = tracks[track_idx]

            # 对应C++: if (tracks[track_idx]->get_time_since_update() > 1) => INFTY_COST
            if track.time_since_update > 1:
                cost_matrix[i, :] = INFTY_COST
                continue

            track_bbox = track.xyxy
            for j, det_idx in enumerate(detection_indices):
                det_bbox = detections[det_idx].xyxy
                ciou = _compute_ciou(track_bbox, det_bbox)
                cost_matrix[i, j] = 1.0 - ciou

        return cost_matrix

    @staticmethod
    def _det_to_xyah(detection: Detection) -> np.ndarray:
        x, y, w, h = detection.bbox
        return np.array(
            [x + w / 2, y + h / 2, w / h if h > 0 else 1.0, h], dtype=np.float32
        )

    # ============================================================
    # 匹配算法 (同步自 track_method.cpp)
    # ============================================================

    def _min_cost_matching(
        self,
        cost_metric_fn,
        max_distance: float,
        tracks: List[Track],
        features: List[np.ndarray],
        detections: List[Detection],
        track_indices: List[int],
        detection_indices: List[int],
    ) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
        """
        最小成本匹配 (匈牙利算法)
        严格对应C++ min_cost_matching:
        1. 计算成本矩阵
        2. 超过max_distance的entry clamp为 max_distance + 1e-5
        3. 匈牙利求解
        4. 匹配结果中cost > max_distance的拒绝
        """
        if not track_indices or not detection_indices:
            return [], list(track_indices), list(detection_indices)

        # 计算成本矩阵
        cost_matrix = cost_metric_fn(
            tracks, features, detections, track_indices, detection_indices
        )

        # Clamp超过阈值的entry (对应C++ line 322-330)
        cost_matrix[cost_matrix > max_distance] = max_distance + 1e-5

        # 匈牙利求解（scipy 惰性 import：slim 版不安装 scipy）
        from scipy.optimize import linear_sum_assignment

        row_indices, col_indices = linear_sum_assignment(cost_matrix)

        matches = []
        unmatched_tracks = set(range(len(track_indices)))
        unmatched_dets = set(range(len(detection_indices)))

        for row, col in zip(row_indices, col_indices):
            unmatched_tracks.discard(row)
            unmatched_dets.discard(col)

            # 对应C++ line 366: if cost > max_distance => 拒绝
            if cost_matrix[row, col] > max_distance:
                unmatched_tracks.add(row)
                unmatched_dets.add(col)
            else:
                matches.append((track_indices[row], detection_indices[col]))

        unmatched_tracks = [track_indices[i] for i in unmatched_tracks]
        unmatched_dets = [detection_indices[i] for i in unmatched_dets]

        return matches, unmatched_tracks, unmatched_dets

    def _matching_cascade(
        self,
        tracks: List[Track],
        features: List[np.ndarray],
        detections: List[Detection],
        track_indices: List[int],
        detection_indices: List[int],
        cascade_depth: int,
    ) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
        """
        匹配级联
        严格对应C++ matching_cascade:
        按level(0..cascade_depth-1)迭代, 每个level选取time_since_update == 1+level的track,
        对这些track和剩余未匹配detection执行min_cost_matching (gated_metric)
        """
        unmatched_dets = list(detection_indices)
        all_matches = []
        matched_track_set = set()

        # 门控度量的lambda (DIST_METRIC_GATED_METRIC)
        def gated_metric_fn(trks, feats, dets, t_idx, d_idx):
            return self._gated_metric(trks, feats, dets, t_idx, d_idx)

        for level in range(cascade_depth):
            if not unmatched_dets:
                break

            # 选取 time_since_update == 1 + level 的track (对应C++ line 271)
            track_indices_l = [
                k for k in track_indices if tracks[k].time_since_update == 1 + level
            ]
            if not track_indices_l:
                continue

            matches_l, _, unmatched_dets = self._min_cost_matching(
                gated_metric_fn,
                self.config.max_cosine_distance,
                tracks,
                features,
                detections,
                track_indices_l,
                unmatched_dets,
            )

            for m in matches_l:
                all_matches.append(m)
                matched_track_set.add(m[0])

        unmatched_tracks = [i for i in track_indices if i not in matched_track_set]

        return all_matches, unmatched_tracks, unmatched_dets

    def _process_matching(
        self,
        tracks: List[Track],
        features: List[np.ndarray],
        detections: List[Detection],
    ) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
        """
        完整匹配流程
        对应C++ _process_matching_dets_to_track:
        1. confirmed tracks -> matching_cascade (gated_metric)
        2. unconfirmed tracks + 刚丢失1帧的confirmed -> min_cost_matching (IoU)
        """
        confirmed_tracks = [i for i, t in enumerate(tracks) if t.is_confirmed]
        unconfirmed_tracks = [i for i, t in enumerate(tracks) if t.is_tentative]

        detection_indices = list(range(len(detections)))

        # Step 1: cascade matching on confirmed tracks
        matches_s1, unmatched_confirmed, unmatched_dets = self._matching_cascade(
            tracks,
            features,
            detections,
            confirmed_tracks,
            detection_indices,
            cascade_depth=self.config.max_age,
        )

        # Step 2: IoU matching on unconfirmed + recently lost confirmed (time_since_update==1)
        # 对应C++ line 739-748
        iou_match_tracks = list(unconfirmed_tracks)
        for track_idx in unmatched_confirmed:
            if tracks[track_idx].time_since_update == 1:
                iou_match_tracks.append(track_idx)

        # IoU 度量的lambda (DIST_METRIC_IOU_COST)
        def iou_metric_fn(trks, feats, dets, t_idx, d_idx):
            return self._iou_cost(trks, dets, t_idx, d_idx)

        matches_s2, unmatched_iou, unmatched_dets = self._min_cost_matching(
            iou_metric_fn,
            self.config.max_iou_distance,
            tracks,
            features,
            detections,
            iou_match_tracks,
            unmatched_dets,
        )

        matches = matches_s1 + matches_s2
        unmatched_tracks = unmatched_iou + [
            i for i in unmatched_confirmed if tracks[i].time_since_update > 1
        ]

        return matches, unmatched_tracks, unmatched_dets

    # ============================================================
    # 主更新流程 (同步自 tracker_mot_v2.cpp update())
    # ============================================================

    def update_with_detections(
        self, image: np.ndarray, detections: List[Detection]
    ) -> List[Track]:
        """update() 的变体：跳过内部 detect，使用外部传入的检测结果。"""
        self._global_frame_idx += 1
        self._last_detections = detections
        return self._update_core(image, detections)

    def update(self, image: np.ndarray) -> List[Track]:
        self._global_frame_idx += 1

        # 检测
        detections = self.detector.detect(image)
        self._last_detections = detections
        return self._update_core(image, detections)

    def _update_core(self, image: np.ndarray, detections: List[Detection]) -> List[Track]:
        cfg = self.config

        # 仅保留 human 类目标进入跟踪
        human_dets = [d for d in detections if d.class_id == Detection.CLASS_HUMAN]
        human_tracks = [
            t
            for t in self.tracks
            if t.class_id == Detection.CLASS_HUMAN and not t.is_deleted
        ]

        # 更新人体 tracks — 参数全部从 config 读取
        self._update_tracks_by_class(
            image,
            human_dets,
            human_tracks,
            n_init=cfg.human_n_init,
            init_conf_threshold=cfg.human_init_confidence,
            max_time_can_lost=cfg.human_max_lost_frames,
            max_num_trackable=cfg.max_human_targets,
        )

        # 清理已删除的轨迹 (对应C++ Step 5)
        self.tracks = [t for t in self.tracks if not t.is_deleted]

        return [t for t in self.tracks if t.is_confirmed]

    def _update_tracks_by_class(
        self,
        image: np.ndarray,
        detections: List[Detection],
        tracks: List[Track],
        n_init: int,
        init_conf_threshold: float,
        max_time_can_lost: int,
        max_num_trackable: int,
    ):
        """按类别更新轨迹, 对应C++ update() 主流程"""

        # Step 0: Predict (对应C++ predict() 步骤)
        # 在匹配前, 对所有track执行Kalman预测并递增time_since_update
        for track in tracks:
            track.predict(self.kalman)

        if not detections:
            for track in tracks:
                track.mark_missed(max_time_can_lost)
            return

        # Step 1: 提取特征
        if self.config.mode == "fast":
            features = self._extract_features_fast(image, detections, tracks)
        else:
            features = [self._extract_feature(image, det) for det in detections]

        # Step 2: 匹配 (对应C++ _process_matching_dets_to_track)
        matches, unmatched_tracks, unmatched_dets = self._process_matching(
            tracks, features, detections
        )

        # Step 3.1: 更新匹配的轨迹 (对应C++ update_v3)
        for track_idx, det_idx in matches:
            track = tracks[track_idx]
            det = detections[det_idx]
            feature = features[det_idx]
            track.update(det, feature, self.kalman)
            # fast 模式: 新匹配成功时更新 reid 帧号
            if self.config.mode == "fast" and not track.is_static:
                track.last_reid_frame = self._global_frame_idx

        # Step 3.2: 处理未匹配的轨迹 (对应C++ mark_missed)
        for track_idx in unmatched_tracks:
            tracks[track_idx].mark_missed(max_time_can_lost)

        # Step 4: 从未匹配的检测初始化新轨迹
        # 对应C++ line 197-290: 检查最大目标数 + 置信度阈值
        current_track_count = sum(1 for t in tracks if not t.is_deleted)
        for det_idx in unmatched_dets:
            if current_track_count >= max_num_trackable:
                break

            det = detections[det_idx]

            # 置信度阈值过滤 (对应C++ MOT_THRESHV2_INIT_PERSON)
            if det.confidence < init_conf_threshold:
                continue

            feature = features[det_idx]
            self._initiate_track(det, feature, n_init)
            current_track_count += 1

    def _initiate_track(self, detection: Detection, feature: np.ndarray, n_init: int):
        """
        初始化新轨迹
        对应C++ init() + mark_init_track()
        """
        track = Track(
            track_id=self.next_id,
            class_id=detection.class_id,
            bbox=detection.bbox,
            confidence=detection.confidence,
            features=deque([feature], maxlen=NUM_MAX_KEEP_FEAT),
            boxes=deque([detection.bbox], maxlen=NUM_MAX_KEEP_FEAT),
            n_init=n_init,
            hits=1,  # 对应C++ init(): m_hits = 1
            age=1,  # 对应C++ init(): m_ages = 1
            last_reid_frame=self._global_frame_idx,
        )

        # 初始化DeepSORT Kalman滤波
        xyah = track.xyah
        mean, covariance = self.kalman.initiate(xyah)
        track.mean = mean
        track.covariance = covariance

        self.tracks.append(track)
        self.next_id += 1

    def get_tracking_results(self) -> List[Dict]:
        results = []
        for track in self.tracks:
            if track.is_confirmed:
                results.append(
                    {
                        "id": track.track_id,
                        "class": track.class_name,
                        "class_id": track.class_id,
                        "bbox": track.bbox,
                        "xyxy": track.xyxy,
                        "confidence": track.confidence,
                    }
                )
        return results
