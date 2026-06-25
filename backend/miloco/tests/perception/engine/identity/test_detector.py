"""Detector 模块单元测试 — preprocess / postprocess / nms / _calculate_iou / class filtering。

不依赖 ONNX 模型文件：通过 mock session 或直接调用纯计算函数。
覆盖:
- Detection dataclass 属性 (class_name / bbox / xyxy)
- preprocess: 缩放比 / 填充偏移 / 输出形状 / 像素归一化
- postprocess: 坐标反映射 / 置信度过滤 / 空输入 / 零宽高框过滤
- nms: 同类抑制 / 跨类保留 / 空列表 / 单检测
- _calculate_iou: 完全重叠 / 无交集 / 部分交集 / union=0
- detect(class_ids): detect_pets / detect_humans / detect_faces 类别过滤
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from miloco.perception.engine.identity.tracker.detector import Detection

# =============================================================================
# Detection Dataclass
# =============================================================================


class TestDetectionDataclass:
    def test_class_name_human(self):
        d = Detection(x=0, y=0, w=10, h=10, confidence=0.9, class_id=0)
        assert d.class_name == "human"

    def test_class_name_cat(self):
        d = Detection(x=0, y=0, w=10, h=10, confidence=0.9, class_id=1)
        assert d.class_name == "cat"

    def test_class_name_dog(self):
        d = Detection(x=0, y=0, w=10, h=10, confidence=0.9, class_id=2)
        assert d.class_name == "dog"

    def test_class_name_head(self):
        d = Detection(x=0, y=0, w=10, h=10, confidence=0.9, class_id=3)
        assert d.class_name == "head"

    def test_class_name_face(self):
        d = Detection(x=0, y=0, w=10, h=10, confidence=0.9, class_id=4)
        assert d.class_name == "face"

    def test_class_name_unknown(self):
        d = Detection(x=0, y=0, w=10, h=10, confidence=0.9, class_id=99)
        assert d.class_name == "unknown"

    def test_bbox_property(self):
        d = Detection(x=10, y=20, w=30, h=40, confidence=0.9, class_id=0)
        assert d.bbox == (10, 20, 30, 40)

    def test_xyxy_property(self):
        d = Detection(x=10, y=20, w=30, h=40, confidence=0.9, class_id=0)
        assert d.xyxy == (10, 20, 40, 60)

    def test_class_constants(self):
        assert Detection.CLASS_HUMAN == 0
        assert Detection.CLASS_CAT == 1
        assert Detection.CLASS_DOG == 2
        assert Detection.CLASS_HEAD == 3
        assert Detection.CLASS_FACE == 4


# =============================================================================
# Helper: 构造 mock Detector 实例（绕过 ONNX 加载）
# =============================================================================


def _make_detector(conf_threshold: float = 0.5, iou_threshold: float = 0.7):
    """构造 Detector 实例，mock 掉 ONNX session 加载。"""
    with patch("miloco.perception.inference.ort_utils.make_session") as mock_make:
        mock_session = MagicMock()
        mock_input = MagicMock()
        mock_input.name = "images"
        mock_input.shape = [1, 3, 640, 640]
        mock_session.get_inputs.return_value = [mock_input]
        mock_output = MagicMock()
        mock_output.name = "output0"
        mock_session.get_outputs.return_value = [mock_output]
        mock_make.return_value = mock_session

        from miloco.perception.engine.identity.tracker.detector import Detector
        det = Detector(
            model_path="fake.onnx",
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            use_gpu=False,
        )
    return det


# =============================================================================
# Preprocess
# =============================================================================


class TestPreprocess:
    def test_output_shape(self):
        """输出应为 (1, 3, 640, 640)。"""
        det = _make_detector()
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        tensor, scale, pad_x, pad_y = det.preprocess(img)
        assert tensor.shape == (1, 3, 640, 640)

    def test_output_dtype_float32(self):
        det = _make_detector()
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        tensor, _, _, _ = det.preprocess(img)
        assert tensor.dtype == np.float32

    def test_normalized_range(self):
        """像素值应归一化到 [0, 1]。"""
        det = _make_detector()
        img = np.full((480, 640, 3), 255, dtype=np.uint8)
        tensor, _, _, _ = det.preprocess(img)
        assert tensor.max() <= 1.0
        assert tensor.min() >= 0.0

    def test_scale_square_image(self):
        """正方形图像缩放比为 640/640 = 1.0。"""
        det = _make_detector()
        img = np.zeros((640, 640, 3), dtype=np.uint8)
        _, scale, pad_x, pad_y = det.preprocess(img)
        assert scale == 1.0
        assert pad_x == 0
        assert pad_y == 0

    def test_scale_wide_image(self):
        """宽图(1280x480)应以宽为基准缩放。"""
        det = _make_detector()
        img = np.zeros((480, 1280, 3), dtype=np.uint8)
        _, scale, pad_x, pad_y = det.preprocess(img)
        assert scale == pytest.approx(640 / 1280, abs=1e-6)
        # 缩放后宽=640，高=480*0.5=240，垂直填充 (640-240)//2=200
        assert pad_x == 0
        assert pad_y == 200

    def test_scale_tall_image(self):
        """高图(640x320)应以高为基准缩放。"""
        det = _make_detector()
        img = np.zeros((640, 320, 3), dtype=np.uint8)
        _, scale, pad_x, pad_y = det.preprocess(img)
        assert scale == pytest.approx(640 / 640, abs=1e-6)
        # 缩放后高=640，宽=320*1.0=320，水平填充 (640-320)//2=160
        assert pad_x == 160
        assert pad_y == 0


# =============================================================================
# Postprocess
# =============================================================================


class TestPostprocess:
    def _make_yolo_output(self, boxes: list[tuple], num_classes: int = 5) -> list[np.ndarray]:
        """构造 YOLOv8 格式输出。

        boxes: [(xc, yc, w, h, class_id, confidence), ...]
        YOLOv8 output shape: (1, 4 + num_classes, num_boxes)

        注意：当只有 1 个 box 时，np.squeeze 会把 (1,9,1) 压缩为 (9,) 导致问题。
        自动补一个零置信度的 padding box 避免此问题（模拟真实模型始终多框输出）。
        """
        if len(boxes) == 1:
            # 补一个不会通过置信度的 padding box
            boxes = list(boxes) + [(0, 0, 0, 0, 0, 0.0)]
        num_boxes = len(boxes)
        # (1, num_attrs, num_boxes) 格式
        data = np.zeros((1, 4 + num_classes, num_boxes), dtype=np.float32)
        for i, (xc, yc, w, h, cls_id, conf) in enumerate(boxes):
            data[0, 0, i] = xc  # xc
            data[0, 1, i] = yc  # yc
            data[0, 2, i] = w   # w
            data[0, 3, i] = h   # h
            # 类别分数：目标类别设为 conf，其他设为 0
            data[0, 4 + cls_id, i] = conf
        return [data]

    def test_empty_output(self):
        """无检测时返回空列表。"""
        det = _make_detector()
        output = [np.zeros((1, 9, 0), dtype=np.float32)]
        results = det.postprocess(output, (480, 640), 1.0, 0, 0)
        assert results == []

    def test_below_confidence_filtered(self):
        """低于置信度阈值的检测被过滤。"""
        det = _make_detector(conf_threshold=0.5)
        # 一个置信度 0.3 的人体检测
        output = self._make_yolo_output([(320, 240, 100, 200, 0, 0.3)])
        results = det.postprocess(output, (480, 640), 1.0, 0, 0)
        assert len(results) == 0

    def test_above_confidence_kept(self):
        """高于置信度阈值的检测被保留。"""
        det = _make_detector(conf_threshold=0.5)
        output = self._make_yolo_output([(320, 240, 100, 200, 0, 0.9)])
        results = det.postprocess(output, (480, 640), 1.0, 0, 0)
        assert len(results) == 1
        assert results[0].class_id == 0
        assert results[0].confidence == pytest.approx(0.9, abs=1e-5)

    def test_class_id_correct(self):
        """类别 ID 正确映射。"""
        det = _make_detector(conf_threshold=0.3)
        output = self._make_yolo_output([
            (100, 100, 50, 50, 1, 0.8),  # cat
            (300, 300, 60, 60, 2, 0.7),  # dog
        ])
        results = det.postprocess(output, (480, 640), 1.0, 0, 0)
        assert len(results) == 2
        class_ids = {r.class_id for r in results}
        assert class_ids == {1, 2}

    def test_coordinate_remapping_with_scale_and_pad(self):
        """坐标应根据 scale 和 pad 反映射到原图。"""
        det = _make_detector(conf_threshold=0.3)
        # 模拟 scale=0.5, pad_x=160, pad_y=0
        # 模型坐标 (320, 240, 100, 100) → 去 pad → (160, 240) → 除 scale → (320, 480)
        output = self._make_yolo_output([(320, 240, 100, 100, 0, 0.9)])
        results = det.postprocess(output, (960, 1280), 0.5, 160, 0)
        assert len(results) == 1
        r = results[0]
        # 反映射: x1 = (320-50-160)/0.5 = 220, y1 = (240-50-0)/0.5 = 380
        assert r.x == pytest.approx(220, abs=2)
        assert r.y == pytest.approx(380, abs=2)

    def test_multiple_detections_preserved(self):
        """多个有效检测都被保留（不同位置不触发 NMS）。"""
        det = _make_detector(conf_threshold=0.3, iou_threshold=0.7)
        output = self._make_yolo_output([
            (100, 100, 50, 50, 0, 0.9),
            (400, 400, 50, 50, 1, 0.8),
        ])
        results = det.postprocess(output, (640, 640), 1.0, 0, 0)
        assert len(results) == 2


# =============================================================================
# NMS
# =============================================================================


class TestNms:
    def test_empty_list(self):
        det = _make_detector()
        assert det.nms([], 0.5) == []

    def test_single_detection(self):
        det = _make_detector()
        d = Detection(x=10, y=10, w=50, h=50, confidence=0.9, class_id=0)
        result = det.nms([d], 0.5)
        assert len(result) == 1
        assert result[0] is d

    def test_same_class_high_iou_suppressed(self):
        """同类别高 IoU 的低置信度框被抑制。"""
        det = _make_detector()
        d1 = Detection(x=10, y=10, w=100, h=100, confidence=0.9, class_id=0)
        d2 = Detection(x=15, y=15, w=100, h=100, confidence=0.7, class_id=0)
        result = det.nms([d1, d2], 0.5)
        assert len(result) == 1
        assert result[0].confidence == 0.9

    def test_same_class_low_iou_kept(self):
        """同类别低 IoU 的框都被保留。"""
        det = _make_detector()
        d1 = Detection(x=10, y=10, w=50, h=50, confidence=0.9, class_id=0)
        d2 = Detection(x=200, y=200, w=50, h=50, confidence=0.7, class_id=0)
        result = det.nms([d1, d2], 0.5)
        assert len(result) == 2

    def test_different_class_always_kept(self):
        """不同类别即使完全重叠也保留。"""
        det = _make_detector()
        d1 = Detection(x=10, y=10, w=100, h=100, confidence=0.9, class_id=0)  # human
        d2 = Detection(x=10, y=10, w=100, h=100, confidence=0.8, class_id=1)  # cat
        result = det.nms([d1, d2], 0.5)
        assert len(result) == 2
        class_ids = {r.class_id for r in result}
        assert class_ids == {0, 1}

    def test_confidence_order_preserved(self):
        """结果按置信度降序排列。"""
        det = _make_detector()
        d1 = Detection(x=10, y=10, w=50, h=50, confidence=0.5, class_id=0)
        d2 = Detection(x=200, y=200, w=50, h=50, confidence=0.9, class_id=0)
        d3 = Detection(x=400, y=400, w=50, h=50, confidence=0.7, class_id=0)
        result = det.nms([d1, d2, d3], 0.5)
        assert result[0].confidence == 0.9
        assert result[1].confidence == 0.7
        assert result[2].confidence == 0.5

    def test_pet_classes_independently_suppressed(self):
        """猫和狗各自独立做 NMS。"""
        det = _make_detector()
        # 两只重叠的猫
        cat1 = Detection(x=10, y=10, w=80, h=80, confidence=0.9, class_id=1)
        cat2 = Detection(x=15, y=15, w=80, h=80, confidence=0.6, class_id=1)
        # 一只重叠位置的狗
        dog1 = Detection(x=10, y=10, w=80, h=80, confidence=0.8, class_id=2)
        result = det.nms([cat1, cat2, dog1], 0.5)
        # cat2 被 cat1 抑制，dog1 保留（不同类别）
        assert len(result) == 2
        class_ids = {r.class_id for r in result}
        assert class_ids == {1, 2}


# =============================================================================
# _calculate_iou
# =============================================================================


class TestCalculateIou:
    def test_identical_boxes(self):
        """完全重叠 → IoU = 1.0。"""
        det = _make_detector()
        d1 = Detection(x=10, y=10, w=100, h=100, confidence=0.9, class_id=0)
        d2 = Detection(x=10, y=10, w=100, h=100, confidence=0.8, class_id=0)
        assert det._calculate_iou(d1, d2) == pytest.approx(1.0)

    def test_no_overlap(self):
        """无交集 → IoU = 0.0。"""
        det = _make_detector()
        d1 = Detection(x=0, y=0, w=50, h=50, confidence=0.9, class_id=0)
        d2 = Detection(x=200, y=200, w=50, h=50, confidence=0.8, class_id=0)
        assert det._calculate_iou(d1, d2) == 0.0

    def test_partial_overlap(self):
        """部分交集 → 0 < IoU < 1。"""
        det = _make_detector()
        d1 = Detection(x=0, y=0, w=100, h=100, confidence=0.9, class_id=0)
        d2 = Detection(x=50, y=50, w=100, h=100, confidence=0.8, class_id=0)
        iou = det._calculate_iou(d1, d2)
        # 交集面积 = 50*50 = 2500, 并集 = 10000+10000-2500 = 17500
        assert iou == pytest.approx(2500 / 17500, abs=1e-5)

    def test_one_inside_other(self):
        """一个框完全在另一个内部。"""
        det = _make_detector()
        d1 = Detection(x=0, y=0, w=200, h=200, confidence=0.9, class_id=0)
        d2 = Detection(x=50, y=50, w=50, h=50, confidence=0.8, class_id=0)
        iou = det._calculate_iou(d1, d2)
        # 交集 = 50*50 = 2500, 并集 = 40000+2500-2500 = 40000
        assert iou == pytest.approx(2500 / 40000, abs=1e-5)

    def test_zero_area_box(self):
        """零面积框（无交集）→ IoU = 0.0。"""
        det = _make_detector()
        d1 = Detection(x=10, y=10, w=0, h=0, confidence=0.9, class_id=0)
        d2 = Detection(x=10, y=10, w=50, h=50, confidence=0.8, class_id=0)
        # d1 退化为点 (10,10)，与 d2 无交集 → inter_area=0；
        # union = 0 + 2500 - 0 = 2500（非 0），结果走正常分支返回 0/2500=0。
        assert det._calculate_iou(d1, d2) == 0.0

    def test_zero_union_returns_zero(self):
        """两个零面积框重合 → union=0 → 命中 `else 0` 兜底分支。"""
        det = _make_detector()
        # 两框均为零面积且坐标相同：area1=area2=inter=0 → union=0，
        # 专门锁定 detector.py 中 `if union_area > 0 else 0` 的防御分支，
        # 若该兜底被误删（变为除零），本用例会变红。
        d1 = Detection(x=10, y=10, w=0, h=0, confidence=0.9, class_id=0)
        d2 = Detection(x=10, y=10, w=0, h=0, confidence=0.8, class_id=0)
        assert det._calculate_iou(d1, d2) == 0.0


# =============================================================================
# detect() 类别过滤
# =============================================================================


class TestDetectClassFiltering:
    """测试 detect / detect_pets / detect_humans / detect_faces 的类别过滤。"""

    def _run_detect(self, det, class_ids=None):
        """通过 mock session.run 返回构造好的检测，验证类别过滤。"""
        # mock postprocess 直接返回多类别检测结果
        fake_results = [
            Detection(x=100, y=100, w=50, h=50, confidence=0.9, class_id=0),  # human
            Detection(x=200, y=200, w=40, h=40, confidence=0.8, class_id=1),  # cat
            Detection(x=300, y=300, w=60, h=60, confidence=0.7, class_id=2),  # dog
            Detection(x=150, y=80, w=30, h=30, confidence=0.85, class_id=4),  # face
        ]
        with patch.object(det, "preprocess", return_value=(np.zeros((1, 3, 640, 640)), 1.0, 0, 0)):
            with patch.object(det, "postprocess", return_value=fake_results):
                det.session.run = MagicMock(return_value=[np.zeros((1, 9, 1))])
                return det.detect(np.zeros((480, 640, 3), dtype=np.uint8), class_ids=class_ids)

    def test_detect_all(self):
        """class_ids=None 返回所有类别。"""
        det = _make_detector()
        results = self._run_detect(det, class_ids=None)
        assert len(results) == 4

    def test_detect_pets_only(self):
        """detect_pets 只返回 cat + dog。"""
        det = _make_detector()
        results = self._run_detect(det, class_ids=[Detection.CLASS_CAT, Detection.CLASS_DOG])
        assert len(results) == 2
        class_ids = {r.class_id for r in results}
        assert class_ids == {Detection.CLASS_CAT, Detection.CLASS_DOG}

    def test_detect_humans_only(self):
        """detect_humans 只返回 human。"""
        det = _make_detector()
        results = self._run_detect(det, class_ids=[Detection.CLASS_HUMAN])
        assert len(results) == 1
        assert results[0].class_id == Detection.CLASS_HUMAN

    def test_detect_faces_only(self):
        """detect_faces 只返回 face。"""
        det = _make_detector()
        results = self._run_detect(det, class_ids=[Detection.CLASS_FACE])
        assert len(results) == 1
        assert results[0].class_id == Detection.CLASS_FACE

    def test_detect_empty_class_ids(self):
        """class_ids=[] 返回空列表。"""
        det = _make_detector()
        results = self._run_detect(det, class_ids=[])
        assert len(results) == 0
