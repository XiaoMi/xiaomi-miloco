"""DeepSortTracker 宠物过滤行为测试。

验证 DeepSortTracker 的 update() / update_with_detections() 正确剔除 pet track，
与 SortTracker 的 test_sort_pet_tracking.py 形成对称覆盖。

实现方式：mock MultiObjectTracker 内部状态，避免依赖 ONNX 模型文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# =============================================================================
# Fake Track (模拟 DeepSORT 内部 Track 对象)
# =============================================================================


@dataclass
class _FakeTrack:
    track_id: int
    class_id: int
    confidence: float = 0.9
    is_confirmed: bool = True
    time_since_update: int = 0
    hits: int = 3
    age: int = 5
    features: list = None

    def __post_init__(self):
        if self.features is None:
            self.features = [np.random.randn(128).astype(np.float32)]

    @property
    def bbox(self):
        return (100 + self.track_id * 50, 100, 80, 80)


def _frame():
    return np.zeros((480, 640, 3), dtype=np.uint8)


# =============================================================================
# Helper: 构造 mock DeepSortTracker
# =============================================================================


def _make_deep_sort_tracker():
    """构造 DeepSortTracker，mock 掉所有模型依赖。"""
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
        detector = Detector(model_path="fake.onnx", use_gpu=False)

    with patch("miloco.perception.engine.identity.tracker.human_reid.HumanReID.__init__", return_value=None):
        from miloco.perception.engine.identity.deep_sort import DeepSortTracker
        from miloco.perception.engine.config import DeepSortConfigDC
        tracker = DeepSortTracker(
            detector=detector,
            config=DeepSortConfigDC(),
            fps=1,
            reid_model_path="fake_reid.onnx",
        )

    return tracker


# =============================================================================
# update_with_detections 宠物过滤
# =============================================================================


class TestUpdateWithDetectionsPetFiltering:
    """验证 update_with_detections() 后 tracks 中不含 pet。"""

    def test_pet_tracks_filtered_after_update_with_detections(self):
        """update_with_detections 后 pet track 被剔除。"""
        tracker = _make_deep_sort_tracker()

        from miloco.perception.engine.identity.tracker.detector import Detection

        # 模拟 MOT 内部产生了 human + cat + dog tracks
        human_track = _FakeTrack(track_id=0, class_id=Detection.CLASS_HUMAN)
        cat_track = _FakeTrack(track_id=1, class_id=Detection.CLASS_CAT)
        dog_track = _FakeTrack(track_id=2, class_id=Detection.CLASS_DOG)

        # mock _mot.update_with_detections 直接设置 tracks
        def fake_update(frame, dets):
            tracker._mot.tracks = [human_track, cat_track, dog_track]

        tracker._mot.update_with_detections = fake_update
        tracker._mot.tracks = []

        tracker.update_with_detections(_frame(), [])

        # 验证只剩 human track
        assert len(tracker._mot.tracks) == 1
        assert tracker._mot.tracks[0].class_id == Detection.CLASS_HUMAN

    def test_all_pets_removed_no_human(self):
        """只有 pet 时，tracks 为空。"""
        tracker = _make_deep_sort_tracker()

        from miloco.perception.engine.identity.tracker.detector import Detection

        cat_track = _FakeTrack(track_id=0, class_id=Detection.CLASS_CAT)
        dog_track = _FakeTrack(track_id=1, class_id=Detection.CLASS_DOG)

        def fake_update(frame, dets):
            tracker._mot.tracks = [cat_track, dog_track]

        tracker._mot.update_with_detections = fake_update
        tracker._mot.tracks = []

        tracker.update_with_detections(_frame(), [])
        assert len(tracker._mot.tracks) == 0


# =============================================================================
# update() 宠物过滤
# =============================================================================


class TestUpdatePetFiltering:
    """验证 update() 后 tracks 中不含 pet。"""

    def test_pet_tracks_filtered_after_update(self):
        """update() 后 pet track 被剔除。"""
        tracker = _make_deep_sort_tracker()

        from miloco.perception.engine.identity.tracker.detector import Detection

        human_track = _FakeTrack(track_id=0, class_id=Detection.CLASS_HUMAN)
        cat_track = _FakeTrack(track_id=1, class_id=Detection.CLASS_CAT)

        def fake_update(frame):
            tracker._mot.tracks = [human_track, cat_track]

        tracker._mot.update = fake_update
        tracker._mot.tracks = []

        tracker.update(_frame())

        assert len(tracker._mot.tracks) == 1
        assert tracker._mot.tracks[0].class_id == Detection.CLASS_HUMAN


# =============================================================================
# last_detections 保留所有类别
# =============================================================================


class TestLastDetectionsPreservesAll:
    """验证 last_detections 不受 pet 过滤影响。"""

    def test_last_detections_includes_pets(self):
        """last_detections 应包含所有类别（含 cat/dog）。"""
        tracker = _make_deep_sort_tracker()

        from miloco.perception.engine.identity.tracker.detector import Detection

        fake_dets = [
            Detection(x=100, y=100, w=50, h=50, confidence=0.9, class_id=Detection.CLASS_HUMAN),
            Detection(x=200, y=200, w=40, h=40, confidence=0.8, class_id=Detection.CLASS_CAT),
            Detection(x=300, y=300, w=60, h=60, confidence=0.7, class_id=Detection.CLASS_DOG),
        ]
        # last_detections 是只读 property，mock 底层属性
        tracker._mot._last_detections = fake_dets

        assert len(tracker.last_detections) == 3
        class_ids = {d.class_id for d in tracker.last_detections}
        assert Detection.CLASS_CAT in class_ids
        assert Detection.CLASS_DOG in class_ids


# =============================================================================
# get_tracking_results 输出
# =============================================================================


class TestGetTrackingResultsNoPets:
    """验证 get_tracking_results() 输出无 pet 类 track。"""

    def test_only_confirmed_human_tracks_returned(self):
        """只有 confirmed 的 human track 出现在结果中。"""
        tracker = _make_deep_sort_tracker()

        from miloco.perception.engine.identity.tracker.detector import Detection

        # 设置 _mot.tracks 只含 human（因为 update 时已过滤）
        human_track = _FakeTrack(track_id=0, class_id=Detection.CLASS_HUMAN, is_confirmed=True)
        tracker._mot.tracks = [human_track]

        results = tracker.get_tracking_results()
        assert len(results) == 1
        assert results[0]["class_id"] == Detection.CLASS_HUMAN

    def test_unconfirmed_tracks_excluded(self):
        """未确认的 track 不出现在结果中。"""
        tracker = _make_deep_sort_tracker()

        from miloco.perception.engine.identity.tracker.detector import Detection

        unconfirmed = _FakeTrack(track_id=0, class_id=Detection.CLASS_HUMAN, is_confirmed=False)
        tracker._mot.tracks = [unconfirmed]

        results = tracker.get_tracking_results()
        assert len(results) == 0
