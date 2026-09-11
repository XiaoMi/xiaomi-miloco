#!/usr/bin/env python3
"""
人体ReID核心功能模块
基于ONNX模型实现人体特征提取和相似度度量
"""

import logging
from typing import Any, List, Tuple

import cv2
import numpy as np

_LOGGER = logging.getLogger(__name__)


class HumanReID:
    """
    人体ReID类 - 用于人体重识别
    """

    # 距离度量方法常量
    M_G_MAX_SIM = 1  # 最大相似度
    M_G_MEAN_SIM = 2  # 平均相似度
    M_G_MEAN_FEAT_SIM = 3  # 平均特征相似度（推荐一对多时使用）
    M_G_TIME_WEIGHTED = 4  # 时间加权相似度

    # 推荐阈值
    THRESHOLD_SUGGEST = 0.9  # 推荐阈值

    def __init__(
        self,
        model_path: str = "models/human_body_reid_v2.onnx",
        use_gpu: bool = False,
        session: Any | None = None,
    ):
        # v2 模型(10 MB)综合性能均优于旧版,且 input [1,3,192,96] BGR + output
        # head/out_emb:0 [1,1,1,128] + L2-norm 后处理与旧版完全一致,本类的
        # preprocess / extract_feature 无需改动。
        """
        初始化人体ReID

        Args:
            model_path: ONNX模型路径
            use_gpu: 是否使用GPU推理
            session: 可选的共享 ONNX InferenceSession
        """
        self.net_h = 192
        self.net_w = 96
        self.feat_dim = 128
        self.output_node = "head/out_emb:0"

        self.session = None
        self.input_name = None
        self.output_name = None
        self.model_path = model_path

        if session is None:
            self.init(model_path, use_gpu)
        else:
            self.session = session
            self.input_name = session.get_inputs()[0].name
            self.output_name = self.output_node

    def init(self, model_path: str, use_gpu: bool = False) -> bool:
        """
        初始化ONNX模型

        Args:
            model_path: ONNX模型路径
            use_gpu: 是否使用GPU推理

        Returns:
            bool: 初始化成功返回True，否则返回False
        """
        try:
            from miloco.perception.inference.ort_utils import make_session

            self.session = make_session(model_path, use_gpu=use_gpu)
            self.input_name = self.session.get_inputs()[0].name
            self.output_name = self.output_node
            self.model_path = model_path

            return True

        except Exception as e:
            _LOGGER.error(f"模型初始化失败: {e}")
            return False

    def release(self):
        """释放资源"""
        self.session = None

    def __del__(self):
        self.release()

    def preprocess(self, image: np.ndarray) -> np.ndarray:
        """
        图像预处理

        Args:
            image: 输入图像 (H, W, C) BGR格式

        Returns:
            np.ndarray: 预处理后的图像 (1, C, H, W)
        """
        if image is None or image.size == 0:
            raise ValueError("输入图像为空")

        # 调整图像大小 (人体通常是竖向的，192x96)
        resized = cv2.resize(
            image, (self.net_w, self.net_h), interpolation=cv2.INTER_LINEAR
        )

        # HWC -> CHW
        resized = np.transpose(resized, (2, 0, 1))

        # 增加batch维度并转换为float32
        return np.expand_dims(resized, axis=0).astype(np.float32)

    def extract_feature(self, image: np.ndarray, normalize: bool = True) -> np.ndarray:
        """
        提取人体特征

        Args:
            image: 输入图像 (H, W, C) BGR格式
            normalize: 是否对特征进行L2归一化

        Returns:
            np.ndarray: 特征向量 (feat_dim,)
        """
        if self.session is None:
            raise RuntimeError("模型未初始化，请先调用 init() 方法")

        # 预处理并推理
        input_tensor = self.preprocess(image)
        outputs = self.session.run([self.output_name], {self.input_name: input_tensor})

        # 提取特征
        feature = outputs[0].squeeze()

        # 确保特征维度正确
        if feature.shape[0] != self.feat_dim:
            raise ValueError(
                f"特征维度不匹配: 期望 {self.feat_dim}, 实际 {feature.shape[0]}"
            )

        # L2归一化
        if normalize:
            norm = np.linalg.norm(feature)
            if norm > 0:
                feature = feature / norm

        return feature

    def similar_one2one(self, feature1: np.ndarray, feature2: np.ndarray) -> float:
        """
        计算两个特征向量的相似度（需要向量已归一化）

        Args:
            feature1: 特征向量1
            feature2: 特征向量2

        Returns:
            float: 相似度 (0~1)
        """
        if feature1.shape != feature2.shape:
            raise ValueError(f"特征形状不匹配: {feature1.shape} vs {feature2.shape}")

        dist = np.linalg.norm(feature1 - feature2)
        similarity = 1.0 - 0.5 * dist * dist

        return max(0.0, min(1.0, similarity))

    def similar_one2group(
        self,
        query_feat: np.ndarray,
        gallery_feats: List[np.ndarray],
        method: int = None,
    ) -> Tuple[float, int]:
        """
        一对多相似度度量

        Args:
            query_feat: 查询特征向量
            gallery_feats: 画廊特征向量列表
            method: 距离度量方法（默认使用平均特征相似度）

        Returns:
            Tuple[float, int]: (相似度, 最佳匹配索引)
        """
        if method is None:
            method = self.M_G_MEAN_FEAT_SIM

        if not gallery_feats:
            return -1.0, -1

        if method == self.M_G_MEAN_FEAT_SIM:
            # 平均特征相似度
            mean_feat = np.mean(gallery_feats, axis=0)
            norm = np.linalg.norm(mean_feat)
            if norm > 0:
                mean_feat = mean_feat / norm
            similarity = self.similar_one2one(query_feat, mean_feat)
            return similarity, -1

        elif method == self.M_G_MAX_SIM:
            # 最大相似度
            max_sim = -1.0
            ret_idx = -1
            for i, feat in enumerate(gallery_feats):
                tmp_sim = self.similar_one2one(query_feat, feat)
                if tmp_sim > max_sim:
                    max_sim = tmp_sim
                    ret_idx = i
            return max_sim, ret_idx

        elif method == self.M_G_MEAN_SIM:
            # 平均相似度
            total_sim = sum(
                self.similar_one2one(query_feat, feat) for feat in gallery_feats
            )
            return total_sim / len(gallery_feats), -1

        elif method == self.M_G_TIME_WEIGHTED:
            # 时间加权相似度
            weighted_sim = 0.0
            for i, feat in enumerate(gallery_feats):
                tmp_sim = self.similar_one2one(query_feat, feat)
                if i == 0:
                    weighted_sim = tmp_sim
                else:
                    weighted_sim = 0.5 * weighted_sim + 0.5 * tmp_sim
            return weighted_sim, -1

        else:
            raise ValueError(f"未知的度量方法: {method}")

    def similar_group2group(
        self, query_feats: List[np.ndarray], gallery_feats: List[np.ndarray]
    ) -> Tuple[np.ndarray, float]:
        """
        多对多相似度度量

        Args:
            query_feats: 查询特征向量列表
            gallery_feats: 画廊特征向量列表

        Returns:
            Tuple[np.ndarray, float]: (相似度矩阵, 平均相似度)
        """
        if not query_feats or not gallery_feats:
            raise ValueError("查询或画廊特征不能为空")

        num_query = len(query_feats)
        num_gallery = len(gallery_feats)

        similarity_matrix = np.zeros((num_query, num_gallery), dtype=np.float32)
        total_sim = 0.0

        for i, q_feat in enumerate(query_feats):
            for j, g_feat in enumerate(gallery_feats):
                sim = self.similar_one2one(q_feat, g_feat)
                similarity_matrix[i, j] = sim
                total_sim += sim

        avg_similarity = total_sim / (num_query * num_gallery)
        return similarity_matrix, avg_similarity

    def judge_is_same(self, similarity: float, threshold: float = None) -> bool:
        """
        根据阈值判断是否是同一个目标

        Args:
            similarity: 相似度值
            threshold: 判断阈值（默认使用推荐阈值0.9）

        Returns:
            bool: 是否是同一个目标
        """
        if threshold is None:
            threshold = self.THRESHOLD_SUGGEST

        return similarity >= threshold

    def get_threshold(self, threshold: float = None) -> float:
        """获取阈值"""
        if threshold is None:
            return self.THRESHOLD_SUGGEST
        return threshold
