"""SortTracker 宠物跟踪分支 (track_human_only=False) 单元测试。

覆盖:
- track_human_only=False 时 CAT/DOG 检测能形成 track
- track_human_only=True 时 CAT/DOG 检测不形成 track（对照组）
- 多类目标（HUMAN + CAT + DOG）同时跟踪时各自独立
- pet track 的生命周期：创建、持续、超时删除
- last_detections 始终保留所有类别（不受 track_human_only 影响）
- FACE/HEAD 类在 track_human_only=False 时仍不被跟踪
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import numpy as np
import pytest

from miloco.perception.engine.identity.sort import SortConfig, SortTracker


# =============================================================================
# Fixtures & Helpers
# =============================================================================


@dataclass
class _FakeDetection:
    """与 Detection dataclass 同字段的轻量替身，避免依赖 ONNX 模型加载。"""

    x: int
    y: int
    w: int
    h: int
    confidence: float
    class_id: int

    CLASS_HUMAN = 0
    CLASS_CAT = 1
    CLASS_DOG = 2
    CLASS_HEAD = 3
    CLASS_FACE = 4

    @property
    def xyxy(self):
        return (self.x, self.y, self.x + self.w, self.y + self.h)


def _make_tracker(track_human_only: bool, fps: int = 1, max_age_sec: float = 2.0) -> SortTracker:
    """构造 SortTracker，使用 mock detector（不会被调用，因为用 update_with_detections）。"""
    config = SortConfig(
        n_init=1,
        max_age_sec=max_age_sec,
        iou_threshold=0.3,
        detector_conf_threshold=0.4,
        track_human_only=track_human_only,
    )
    mock_detector = MagicMock()
    return SortTracker(config=config, detector=mock_detector, fps=fps)


def _det(class_id: int, x: int = 100, y: int = 100, w: int = 80, h: int = 80, conf: float = 0.9):
    """快速创建一个 _FakeDetection。"""
    return _FakeDetection(x=x, y=y, w=w, h=h, confidence=conf, class_id=class_id)


def _frame() -> np.ndarray:
    """空白帧，仅占位用。"""
    return np.zeros((480, 640, 3), dtype=np.uint8)


# =============================================================================
# 核心功能测试：track_human_only=False 时宠物可被跟踪
# =============================================================================


class TestPetTrackingEnabled:
    """验证 track_human_only=False 时 CAT/DOG 能形成 track。"""

    def test_cat_forms_track(self):
        """单只猫的检测结果应形成独立 track。"""
        tracker = _make_tracker(track_human_only=False)
        dets = [_det(_FakeDetection.CLASS_CAT, x=200, y=300)]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 1
        assert results[0]["class_id"] == _FakeDetection.CLASS_CAT

    def test_dog_forms_track(self):
        """单只狗的检测结果应形成独立 track。"""
        tracker = _make_tracker(track_human_only=False)
        dets = [_det(_FakeDetection.CLASS_DOG, x=300, y=200)]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 1
        assert results[0]["class_id"] == _FakeDetection.CLASS_DOG

    def test_human_cat_dog_simultaneous(self):
        """人、猫、狗同框时各自形成独立 track。"""
        tracker = _make_tracker(track_human_only=False)
        dets = [
            _det(_FakeDetection.CLASS_HUMAN, x=100, y=100),
            _det(_FakeDetection.CLASS_CAT, x=300, y=300),
            _det(_FakeDetection.CLASS_DOG, x=500, y=200),
        ]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 3
        class_ids = {r["class_id"] for r in results}
        assert class_ids == {
            _FakeDetection.CLASS_HUMAN,
            _FakeDetection.CLASS_CAT,
            _FakeDetection.CLASS_DOG,
        }

    def test_pet_track_has_correct_fields(self):
        """宠物 track 的输出字段集与人类 track 完全一致。"""
        tracker = _make_tracker(track_human_only=False)
        dets = [_det(_FakeDetection.CLASS_CAT, x=200, y=300, w=60, h=40)]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 1
        r = results[0]
        expected_fields = {"id", "class_id", "bbox", "xyxy", "confidence", "hits", "age", "time_since_update", "detected_this_frame"}
        assert set(r.keys()) == expected_fields

    def test_pet_track_unique_ids(self):
        """多只宠物应获得不同的 track_id。"""
        tracker = _make_tracker(track_human_only=False)
        # 两只猫在不同位置
        dets = [
            _det(_FakeDetection.CLASS_CAT, x=100, y=100),
            _det(_FakeDetection.CLASS_CAT, x=400, y=400),
        ]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 2
        ids = [r["id"] for r in results]
        assert ids[0] != ids[1]


# =============================================================================
# 对照组：track_human_only=True 时宠物不被跟踪
# =============================================================================


class TestPetTrackingDisabled:
    """验证 track_human_only=True (默认) 时 CAT/DOG 不形成 track。"""

    def test_cat_not_tracked(self):
        """猫不应形成 track。"""
        tracker = _make_tracker(track_human_only=True)
        dets = [_det(_FakeDetection.CLASS_CAT, x=200, y=300)]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 0

    def test_dog_not_tracked(self):
        """狗不应形成 track。"""
        tracker = _make_tracker(track_human_only=True)
        dets = [_det(_FakeDetection.CLASS_DOG, x=300, y=200)]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 0

    def test_human_tracked_pet_ignored(self):
        """人被跟踪，宠物被忽略。"""
        tracker = _make_tracker(track_human_only=True)
        dets = [
            _det(_FakeDetection.CLASS_HUMAN, x=100, y=100),
            _det(_FakeDetection.CLASS_CAT, x=300, y=300),
            _det(_FakeDetection.CLASS_DOG, x=500, y=200),
        ]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 1
        assert results[0]["class_id"] == _FakeDetection.CLASS_HUMAN


# =============================================================================
# last_detections 始终保留所有类别
# =============================================================================


class TestLastDetectionsPreserveAll:
    """无论 track_human_only 为何值，last_detections 都保留全部检测结果。"""

    def test_pet_in_last_detections_when_tracking_disabled(self):
        """track_human_only=True 时，宠物检测仍在 last_detections 中。"""
        tracker = _make_tracker(track_human_only=True)
        dets = [
            _det(_FakeDetection.CLASS_HUMAN, x=100, y=100),
            _det(_FakeDetection.CLASS_CAT, x=300, y=300),
        ]
        tracker.update_with_detections(_frame(), dets)

        assert len(tracker.last_detections) == 2
        class_ids = {d.class_id for d in tracker.last_detections}
        assert _FakeDetection.CLASS_CAT in class_ids

    def test_pet_in_last_detections_when_tracking_enabled(self):
        """track_human_only=False 时，last_detections 也保留全部。"""
        tracker = _make_tracker(track_human_only=False)
        dets = [
            _det(_FakeDetection.CLASS_HUMAN, x=100, y=100),
            _det(_FakeDetection.CLASS_DOG, x=300, y=300),
            _det(_FakeDetection.CLASS_FACE, x=110, y=80, w=40, h=40),
        ]
        tracker.update_with_detections(_frame(), dets)

        assert len(tracker.last_detections) == 3


# =============================================================================
# FACE/HEAD 在 track_human_only=False 时仍不被跟踪
# =============================================================================


class TestNonTrackableClasses:
    """即使 track_human_only=False，FACE 和 HEAD 类也不形成 track。"""

    def test_face_not_tracked(self):
        tracker = _make_tracker(track_human_only=False)
        dets = [_det(_FakeDetection.CLASS_FACE, x=200, y=100, w=40, h=40)]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 0

    def test_head_not_tracked(self):
        tracker = _make_tracker(track_human_only=False)
        dets = [_det(_FakeDetection.CLASS_HEAD, x=200, y=100, w=50, h=50)]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 0

    def test_face_head_not_tracked_but_pet_is(self):
        """FACE/HEAD 不跟踪，CAT/DOG 跟踪。"""
        tracker = _make_tracker(track_human_only=False)
        dets = [
            _det(_FakeDetection.CLASS_FACE, x=100, y=100, w=40, h=40),
            _det(_FakeDetection.CLASS_HEAD, x=100, y=50, w=50, h=50),
            _det(_FakeDetection.CLASS_CAT, x=300, y=300),
            _det(_FakeDetection.CLASS_DOG, x=500, y=300),
        ]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 2
        class_ids = {r["class_id"] for r in results}
        assert class_ids == {_FakeDetection.CLASS_CAT, _FakeDetection.CLASS_DOG}


# =============================================================================
# 宠物 track 生命周期
# =============================================================================


class TestPetTrackLifecycle:
    """验证宠物 track 的生命周期行为与人类 track 一致。"""

    def test_pet_track_survives_continuous_detection(self):
        """持续检测到宠物时，track 保持活跃且 id 不变。"""
        tracker = _make_tracker(track_human_only=False, fps=2, max_age_sec=2.0)

        # 猫连续出现 5 帧，微小位移模拟移动
        for i in range(5):
            dets = [_det(_FakeDetection.CLASS_CAT, x=100 + i * 5, y=300)]
            tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 1
        assert results[0]["class_id"] == _FakeDetection.CLASS_CAT
        # 第 1 帧创建 track (hits=0)，后续 4 帧匹配 (hits += 1 each) → hits = 4
        assert results[0]["hits"] == 4

    def test_pet_track_dies_after_max_age(self):
        """宠物消失超过 max_age 后 track 被删除。"""
        tracker = _make_tracker(track_human_only=False, fps=1, max_age_sec=2.0)
        # max_age_frames = round(2.0 * 1) = 2

        # 第 1 帧：猫出现
        dets = [_det(_FakeDetection.CLASS_CAT, x=200, y=300)]
        tracker.update_with_detections(_frame(), dets)
        assert len(tracker.get_tracking_results()) == 1

        # 第 2~3 帧：猫消失（空检测）
        for _ in range(2):
            tracker.update_with_detections(_frame(), [])

        # 第 2 帧后 time_since_update=1（还在 max_age 范围内，但 get_tracking_results 过滤了）
        # 第 3 帧后 time_since_update=2，仍 <= max_age_frames=2，track 还在 _tracks 中
        # 第 4 帧：再次空检测
        tracker.update_with_detections(_frame(), [])
        # time_since_update=3 > max_age_frames=2，track 被删除
        assert len(tracker._tracks) == 0

    def test_pet_track_recovers_within_max_age(self):
        """宠物短暂消失后在 max_age 内重新出现，track 恢复（IoU 匹配上）。"""
        tracker = _make_tracker(track_human_only=False, fps=1, max_age_sec=3.0)
        # max_age_frames = 3

        # 第 1 帧：猫出现
        dets = [_det(_FakeDetection.CLASS_CAT, x=200, y=300, w=80, h=60)]
        tracker.update_with_detections(_frame(), dets)
        initial_id = tracker.get_tracking_results()[0]["id"]

        # 第 2 帧：猫消失
        tracker.update_with_detections(_frame(), [])

        # 第 3 帧：猫重新出现在附近位置（IoU 应可匹配）
        dets = [_det(_FakeDetection.CLASS_CAT, x=205, y=305, w=80, h=60)]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 1
        # 应该匹配回同一个 track（track_id 不变）
        assert results[0]["id"] == initial_id
        # 创建时 hits=0，丢失 1 帧后重新匹配 hits += 1 → hits = 1
        assert results[0]["hits"] == 1

    def test_confidence_threshold_applies_to_pets(self):
        """置信度过低的宠物检测不形成 track。"""
        tracker = _make_tracker(track_human_only=False)
        # 配置的 detector_conf_threshold = 0.4
        dets = [_det(_FakeDetection.CLASS_CAT, x=200, y=300, conf=0.3)]
        tracker.update_with_detections(_frame(), dets)

        results = tracker.get_tracking_results()
        assert len(results) == 0

    def test_reset_clears_pet_tracks(self):
        """reset() 清空所有 track（包括宠物）。"""
        tracker = _make_tracker(track_human_only=False)
        dets = [
            _det(_FakeDetection.CLASS_CAT, x=200, y=300),
            _det(_FakeDetection.CLASS_DOG, x=400, y=200),
        ]
        tracker.update_with_detections(_frame(), dets)
        assert len(tracker.get_tracking_results()) == 2

        tracker.reset()
        assert len(tracker.get_tracking_results()) == 0
        assert len(tracker._tracks) == 0
