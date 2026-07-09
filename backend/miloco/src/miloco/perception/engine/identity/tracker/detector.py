#!/usr/bin/env python3
"""
检测模块 - 基于ONNX模型的目标检测器
支持人体、人脸、宠物(猫/狗)检测
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

# 默认模型路径：包内 miloco/perception/models/det_4C.onnx
# 解析自 __file__，避免依赖进程 cwd（supervisor 启动时 cwd 通常不在该目录下）
# __file__ = .../miloco/perception/engine/identity/tracker/detector.py → 上溯 4 级到 perception/
_DEFAULT_MODEL_PATH = str(
    Path(__file__).resolve().parent.parent.parent.parent / "models" / "det_4C.onnx"
)


@dataclass
class Detection:
    """检测结果数据类"""

    x: int  # 左上角x坐标
    y: int  # 左上角y坐标
    w: int  # 宽度
    h: int  # 高度
    confidence: float  # 置信度
    class_id: int  # 类别ID

    # 类别常量 (根据实际模型定义)
    CLASS_HUMAN = 0  # 人体
    CLASS_CAT = 1  # 猫
    CLASS_DOG = 2  # 狗
    CLASS_HEAD = 3  # 人头
    CLASS_FACE = 4  # 人脸

    @property
    def class_name(self) -> str:
        """获取类别名称"""
        class_names = {
            self.CLASS_HUMAN: "human",
            self.CLASS_CAT: "cat",
            self.CLASS_DOG: "dog",
            self.CLASS_HEAD: "head",
            self.CLASS_FACE: "face",
        }
        return class_names.get(self.class_id, "unknown")

    @property
    def bbox(self) -> Tuple[int, int, int, int]:
        """获取边界框 (x, y, w, h)"""
        return (self.x, self.y, self.w, self.h)

    @property
    def xyxy(self) -> Tuple[int, int, int, int]:
        """获取边界框 (x1, y1, x2, y2)"""
        return (self.x, self.y, self.x + self.w, self.y + self.h)


class Detector:
    """
    基于ONNX模型的目标检测器
    """

    def __init__(
        self,
        model_path: str = _DEFAULT_MODEL_PATH,
        conf_threshold: float = 0.5,
        iou_threshold: float = 0.7,
        use_gpu: bool = False,
    ):
        """
        初始化检测器

        Args:
            model_path: ONNX模型路径
            conf_threshold: 置信度阈值
            iou_threshold: NMS的IoU阈值
            use_gpu: 是否使用GPU推理
        """
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold

        # 加载ONNX模型
        from miloco.perception.inference.ort_utils import make_session

        self.session = make_session(self.model_path, use_gpu=use_gpu)

        # 获取输入输出信息
        self.input_name = self.session.get_inputs()[0].name
        self.input_shape = self.session.get_inputs()[0].shape
        self.output_names = [output.name for output in self.session.get_outputs()]

        # 获取输入尺寸
        self.input_height = self.input_shape[2]
        self.input_width = self.input_shape[3]

    def release(self):
        """释放 ONNX session。

        置 None 让 refcount 归零,触发底层 CoreML Execution 析构链清理其中间
        模型文件(见 ort_utils);与 HumanReID.release 对齐,给高频重建路径一个
        确定性的 teardown 入口,避免只靠 GC 时机不定。
        """
        self.session = None

    def __del__(self):
        self.release()

    def preprocess(self, image: np.ndarray) -> Tuple[np.ndarray, float, float, float]:
        """
        预处理图像

        Args:
            image: 输入图像 (H, W, C)

        Returns:
            预处理后的图像, 缩放比例, 填充x, 填充y
        """
        h, w = image.shape[:2]

        # 调整图像大小以保持宽高比
        scale = min(self.input_width / w, self.input_height / h)
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(image, (new_w, new_h))

        # 创建画布并居中填充
        canvas = np.full((self.input_height, self.input_width, 3), 114, dtype=np.uint8)
        start_x = (self.input_width - new_w) // 2
        start_y = (self.input_height - new_h) // 2
        canvas[start_y : start_y + new_h, start_x : start_x + new_w] = resized

        # 转换为RGB（如果需要）
        if canvas.shape[2] == 3:
            canvas = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)

        # 归一化到[0,1]
        canvas = canvas.astype(np.float32) / 255.0

        # 转换维度顺序: (H, W, C) -> (C, H, W)
        canvas = canvas.transpose(2, 0, 1)

        # 添加批次维度: (C, H, W) -> (1, C, H, W)
        canvas = np.expand_dims(canvas, axis=0)

        return canvas, scale, start_x, start_y

    def postprocess(
        self,
        outputs: List[np.ndarray],
        original_shape: Tuple[int, int],
        scale: float,
        pad_x: float,
        pad_y: float,
    ) -> List[Detection]:
        """
        后处理输出结果

        Args:
            outputs: 模型输出
            original_shape: 原始图像形状 (H, W)
            scale: 缩放比例
            pad_x: x方向填充
            pad_y: y方向填充

        Returns:
            检测结果列表
        """
        # 解析输出
        if isinstance(outputs, list):
            detections = outputs[0] if len(outputs) > 0 else outputs
        else:
            detections = outputs

        # YOLOv8 ONNX输出格式是 (batch, num_attrs, num_boxes)
        # 需要转换为 (num_boxes, num_attrs)
        if len(detections.shape) == 3:
            detections = np.transpose(np.squeeze(detections))

        if detections.shape[1] < 6:
            return []

        orig_h, orig_w = original_shape[:2]

        # 分离坐标和类别分数
        # YOLOv8输出格式: (num_boxes, 4+nc)
        # 前4列是box坐标 (xc, yc, w, h)，后面是类别分数（已sigmoid）
        boxes_data = detections[:, :4]  # 中心点坐标和宽高 (xc, yc, w, h)
        class_scores = detections[:, 4:]  # 类别分数

        # 获取最可能的类别ID和对应的分数
        class_ids = np.argmax(class_scores, axis=1)
        total_confidences = np.max(class_scores, axis=1)

        # 过滤置信度低于阈值的检测结果
        filtered_indices = total_confidences >= self.conf_threshold

        if not np.any(filtered_indices):
            return []

        filtered_boxes = boxes_data[filtered_indices]
        filtered_confidences = total_confidences[filtered_indices]
        filtered_class_ids = class_ids[filtered_indices]

        # 将中心点坐标转换为角点坐标
        xc, yc, w, h = (
            filtered_boxes[:, 0],
            filtered_boxes[:, 1],
            filtered_boxes[:, 2],
            filtered_boxes[:, 3],
        )
        x1 = xc - w / 2
        y1 = yc - h / 2
        x2 = xc + w / 2
        y2 = yc + h / 2

        # 映射回原图尺寸
        x1_orig = np.clip((x1 - pad_x) / scale, 0, orig_w - 1)
        y1_orig = np.clip((y1 - pad_y) / scale, 0, orig_h - 1)
        x2_orig = np.clip((x2 - pad_x) / scale, 0, orig_w - 1)
        y2_orig = np.clip((y2 - pad_y) / scale, 0, orig_h - 1)

        # 转换为x, y, w, h格式
        x = x1_orig.astype(int)
        y = y1_orig.astype(int)
        w = (x2_orig - x1_orig).astype(int)
        h = (y2_orig - y1_orig).astype(int)

        # 构建检测结果列表
        detections_list = []
        for i in range(len(x)):
            if w[i] > 0 and h[i] > 0:  # 确保框的有效性
                det = Detection(
                    x=int(x[i]),
                    y=int(y[i]),
                    w=int(w[i]),
                    h=int(h[i]),
                    confidence=float(filtered_confidences[i]),
                    class_id=int(filtered_class_ids[i]),
                )
                detections_list.append(det)

        # 应用非极大值抑制(NMS)
        return self.nms(detections_list, self.iou_threshold)

    def nms(self, detections: List[Detection], iou_threshold: float) -> List[Detection]:
        """
        非极大值抑制，考虑类别信息

        Args:
            detections: 检测结果列表
            iou_threshold: IoU阈值

        Returns:
            经过NMS处理后的检测结果列表
        """
        if not detections:
            return []

        # 按置信度降序排序
        detections.sort(key=lambda x: x.confidence, reverse=True)

        keep = []
        while detections:
            # 保留置信度最高的框
            current = detections.pop(0)
            keep.append(current)

            if not detections:
                break

            # 获取当前框的类别
            current_class = current.class_id

            # 计算与其他框的IoU，只对同类别框计算
            remaining = []
            for det in detections:
                if det.class_id == current_class:
                    # 计算IoU
                    iou = self._calculate_iou(current, det)
                    if iou < iou_threshold:
                        remaining.append(det)
                else:
                    # 不同类别保留
                    remaining.append(det)

            detections = remaining

        return keep

    def _calculate_iou(self, box1: Detection, box2: Detection) -> float:
        """计算两个框的IoU"""
        x1_1, y1_1, x2_1, y2_1 = box1.xyxy
        x1_2, y1_2, x2_2, y2_2 = box2.xyxy

        # 计算交集区域
        inter_x1 = max(x1_1, x1_2)
        inter_y1 = max(y1_1, y1_2)
        inter_x2 = min(x2_1, x2_2)
        inter_y2 = min(y2_1, y2_2)

        inter_area = max(0, inter_x2 - inter_x1) * max(0, inter_y2 - inter_y1)

        # 计算并集区域
        area1 = box1.w * box1.h
        area2 = box2.w * box2.h
        union_area = area1 + area2 - inter_area

        return inter_area / union_area if union_area > 0 else 0

    def detect(
        self, image: np.ndarray, class_ids: Optional[List[int]] = None
    ) -> List[Detection]:
        """
        检测图像中的目标

        Args:
            image: 输入图像 (H, W, C)
            class_ids: 要检测的类别ID列表，如果为None则检测所有类别

        Returns:
            检测结果列表
        """
        original_shape = image.shape

        # 预处理
        input_tensor, scale, pad_x, pad_y = self.preprocess(image)

        # 推理
        outputs = self.session.run(self.output_names, {self.input_name: input_tensor})

        # 后处理
        results = self.postprocess(outputs, original_shape, scale, pad_x, pad_y)

        # 如果指定了特定类别，则只返回这些类别的检测结果
        if class_ids is not None:
            results = [det for det in results if det.class_id in class_ids]

        return results

    def detect_humans(self, image: np.ndarray) -> List[Detection]:
        """检测人体"""
        return self.detect(image, [Detection.CLASS_HUMAN])

    def detect_faces(self, image: np.ndarray) -> List[Detection]:
        """检测人脸"""
        return self.detect(image, [Detection.CLASS_FACE])

    def detect_pets(self, image: np.ndarray) -> List[Detection]:
        """检测宠物(猫和狗)"""
        return self.detect(image, [Detection.CLASS_CAT, Detection.CLASS_DOG])
