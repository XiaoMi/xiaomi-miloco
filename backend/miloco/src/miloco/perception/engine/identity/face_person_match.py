#!/usr/bin/env python3
"""
人脸-人体匹配模块
将检测到的人脸与人体框进行匹配
使用匈牙利算法进行最优匹配
同步自 oms_utils.cpp 中的 compute_affinity / match_body_to_face 系列函数
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Tuple

import numpy as np

if TYPE_CHECKING:
    # 仅 type hint 用;运行时不 import 避免跟 tracker package init 形成循环
    # (tracker/__init__.py 在 init 时会 from face_person_match,如果本模块在
    # module-level 反向 import tracker.detector → tracker package 已在 sys.modules
    # 占位中 → ImportError)。
    from miloco.perception.engine.identity.tracker.detector import Detection


@dataclass
class FaceBodyMatch:
    """人脸-人体匹配结果"""

    face_idx: int  # 人脸索引
    body_idx: int  # 人体索引
    affinity: float  # 匹配亲和度
    face_bbox: Tuple[int, int, int, int]  # 人脸框
    body_bbox: Tuple[int, int, int, int]  # 人体框


class FacePersonMatcher:
    """
    人脸-人体匹配器
    基于匈牙利算法进行最优匹配
    同步自 oms_utils.cpp 中的 match_body_to_face / _match_body_to_face_state_mtom 函数
    """

    def __init__(
        self,
        ioa_threshold: float = 0.70,
        one_to_one_ioa_threshold: float = 0.4,
        match_cost_threshold: float = 0.99,
    ):
        """
        初始化匹配器

        Args:
            ioa_threshold: 人脸在人体框内的最小IoA, 对应C++ OMS_TH_IOA_FACE_IS_THIS_BODY = 0.70
            one_to_one_ioa_threshold: 1对1场景下的宽松IoA阈值, 对应C++ _match_body_to_face_state_1to1中的0.4
            match_cost_threshold: 匈牙利匹配后的cost过滤阈值, 对应C++ cost_matrix(row,col) < 0.99
        """
        self.ioa_threshold = ioa_threshold
        self.one_to_one_ioa_threshold = one_to_one_ioa_threshold
        self.match_cost_threshold = match_cost_threshold

    @staticmethod
    def _clip(val: float, min_val: float, max_val: float) -> float:
        return max(min_val, min(val, max_val))

    @staticmethod
    def _compute_ioa(
        face_bbox: Tuple[int, int, int, int], body_bbox: Tuple[int, int, int, int]
    ) -> float:
        """
        计算IoA (Intersection over Area of face)
        IoA = intersection_area / face_area
        对应C++ _get_IoA: tboxA=face, tboxB=body
        """
        fx1, fy1, fx2, fy2 = face_bbox
        bx1, by1, bx2, by2 = body_bbox

        xmin = max(fx1, bx1)
        ymin = max(fy1, by1)
        xmax = min(fx2, bx2)
        ymax = min(fy2, by2)

        if xmin >= xmax or ymin >= ymax:
            return 0.0

        inter_area = (xmax - xmin) * (ymax - ymin)
        face_area = (fx2 - fx1) * (fy2 - fy1)

        return inter_area / face_area if face_area > 0 else 0.0

    @staticmethod
    def _compute_iou(
        bbox1: Tuple[int, int, int, int], bbox2: Tuple[int, int, int, int]
    ) -> float:
        """计算两个框的IoU"""
        x1_1, y1_1, x2_1, y2_1 = bbox1
        x1_2, y1_2, x2_2, y2_2 = bbox2

        inter_x1 = max(x1_1, x1_2)
        inter_y1 = max(y1_1, y1_2)
        inter_x2 = min(x2_1, x2_2)
        inter_y2 = min(y2_1, y2_2)

        inter_area = max(0, inter_x2 - inter_x1) * max(0, inter_y2 - inter_y1)

        area1 = (x2_1 - x1_1) * (y2_1 - y1_1)
        area2 = (x2_2 - x1_2) * (y2_2 - y1_2)
        union_area = area1 + area2 - inter_area

        return inter_area / union_area if union_area > 0 else 0.0

    def _compute_affinity(
        self,
        face_bbox: Tuple[int, int, int, int],
        body_bbox: Tuple[int, int, int, int],
        last_face_bbox: Optional[Tuple[int, int, int, int]] = None,
    ) -> float:
        """
        计算人脸和人体框的匹配亲和度
        严格对应 oms_utils.cpp 中的 compute_affinity 函数

        逻辑:
        1. P1: 人脸顶部中心到人体顶部中心的归一化距离 → affinity = 1 - rate
        2. IoA < ioa_threshold → 返回 -1.0 (不匹配)
        3. IoA > 0.99 (人脸完全在人体内):
           - P2: 人脸中心与人体中心的H/W方向归一化距离
           - affinity = 0.7 * P1 + 0.3 * P2
           - 若有上一帧人脸框: affinity = 0.6 * affinity + 0.4 * IoU(face, last_face)
        """
        fx1, fy1, fx2, fy2 = face_bbox
        bx1, by1, bx2, by2 = body_bbox

        face_w = fx2 - fx1
        face_h = fy2 - fy1
        body_w = bx2 - bx1
        body_h = by2 - by1

        # P1: 人脸框顶部中心到人体框顶部中心的距离
        face_up_center_x = fx1 + 0.5 * face_w
        face_up_center_y = fy1
        body_up_center_x = bx1 + 0.5 * body_w
        body_up_center_y = by1

        dist = np.sqrt(
            (face_up_center_x - body_up_center_x) ** 2
            + (face_up_center_y - body_up_center_y) ** 2
        )

        # 归一化: 用人体框半对角线长度
        dist_ref = np.sqrt((body_h * 0.5) ** 2 + (body_w * 0.5) ** 2)
        if dist_ref <= 0:
            return -1.0

        rate = self._clip(dist / dist_ref, 0.0, 1.0)
        affinity = 1.0 - rate

        # IoA 检查
        ioa = self._compute_ioa(face_bbox, body_bbox)
        if ioa < self.ioa_threshold:
            return -1.0

        # IoA > 0.99: 人脸完全在人体框内，加入P2亲和力
        if ioa > 0.99:
            face_center_x = fx1 + 0.5 * face_w
            face_center_y = fy1 + 0.5 * face_h
            body_center_x = bx1 + 0.5 * body_w
            body_center_y = by1 + 0.5 * body_h

            max_h_dist = abs(0.5 * (body_h - face_h))
            max_w_dist = abs(0.5 * (body_w - face_w))

            dist_h = abs(body_center_y - face_center_y)
            dist_w = abs(body_center_x - face_center_x)

            rate_cdist_h = (
                self._clip(dist_h / max_h_dist, 0.0, 1.0) if max_h_dist > 0 else 0.0
            )
            rate_cdist_w = (
                self._clip(1.0 - dist_w / max_w_dist, 0.0, 1.0)
                if max_w_dist > 0
                else 0.0
            )

            # 越远(垂直)越高 + 越近(水平)越高
            affinity_cdist = 0.5 * rate_cdist_h + 0.5 * rate_cdist_w

            affinity = 0.7 * affinity + 0.3 * affinity_cdist

            # 上一帧人脸位置一致性
            if last_face_bbox is not None:
                affinity_last_face = self._compute_iou(face_bbox, last_face_bbox)
                affinity = 0.6 * affinity + 0.4 * affinity_last_face

        return affinity

    def match(
        self,
        face_detections: List[Detection],
        body_detections: List[Detection],
        body_last_face_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    ) -> List[FaceBodyMatch]:
        """
        匹配人脸和人体
        同步自 oms_utils.cpp 中的 match_body_to_face 函数:
        - 1对1: 使用宽松IoA阈值(0.4)
        - 多对多: 匈牙利算法 + compute_affinity
        """
        if not face_detections or not body_detections:
            return []

        num_faces = len(face_detections)
        num_bodies = len(body_detections)

        # 1对1特殊处理 (对应C++ _match_body_to_face_state_1to1)
        if num_faces == 1 and num_bodies == 1:
            ioa = self._compute_ioa(face_detections[0].xyxy, body_detections[0].xyxy)
            if ioa >= self.one_to_one_ioa_threshold:
                return [
                    FaceBodyMatch(
                        face_idx=0,
                        body_idx=0,
                        affinity=ioa,
                        face_bbox=face_detections[0].xyxy,
                        body_bbox=body_detections[0].xyxy,
                    )
                ]
            return []

        # 多对多: 匈牙利匹配 (对应C++ _match_body_to_face_state_mtom, HUNGARIAN分支)
        cost_matrix = (
            np.ones((num_faces, num_bodies), dtype=np.float32) * 2.0
        )  # 默认高cost

        for i in range(num_faces):
            for j in range(num_bodies):
                last_face_bbox = None
                if body_last_face_boxes is not None and j < len(body_last_face_boxes):
                    last_face_bbox = body_last_face_boxes[j]

                affinity = self._compute_affinity(
                    face_detections[i].xyxy, body_detections[j].xyxy, last_face_bbox
                )

                if affinity >= 0:
                    cost_matrix[i, j] = 1.0 - affinity
                # affinity < 0 (即 -1.0) 保持 cost=2.0, 不可能被匹配

        # 执行匈牙利算法（scipy 惰性 import：slim 版不安装 scipy）
        from scipy.optimize import linear_sum_assignment

        row_indices, col_indices = linear_sum_assignment(cost_matrix)

        # 收集匹配结果, 阈值过滤 (对应C++ cost_matrix(row, col) < 0.99)
        matches = []
        for row, col in zip(row_indices, col_indices):
            if cost_matrix[row, col] < self.match_cost_threshold:
                matches.append(
                    FaceBodyMatch(
                        face_idx=row,
                        body_idx=col,
                        affinity=1.0 - cost_matrix[row, col],
                        face_bbox=face_detections[row].xyxy,
                        body_bbox=body_detections[col].xyxy,
                    )
                )

        return matches

    def get_unmatched_faces(
        self, face_detections: List[Detection], matches: List[FaceBodyMatch]
    ) -> List[int]:
        """获取未匹配的人脸索引"""
        matched_indices = {match.face_idx for match in matches}
        return [i for i in range(len(face_detections)) if i not in matched_indices]

    def get_unmatched_bodies(
        self, body_detections: List[Detection], matches: List[FaceBodyMatch]
    ) -> List[int]:
        """获取未匹配的人体索引"""
        matched_indices = {match.body_idx for match in matches}
        return [i for i in range(len(body_detections)) if i not in matched_indices]
