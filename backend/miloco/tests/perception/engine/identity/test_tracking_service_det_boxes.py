"""Tracking Service —— Smart Crop 的主体框通路。

这条通路是 Smart Crop 的**取框口径**所在:crop 区域必须包住窗口内出现过的一切主体,
所以框要逐帧累积、且要含 tracker 不建 track 的宠物类。类别过滤与累积都只在这里做,
``crop_enhance`` 只拿到一串 xyxy —— 口径若在此漂移,下游没有任何测试会响。
"""

import numpy as np
import pytest
from miloco.perception.engine.identity.tracker.detector import Detection
from miloco.perception.engine.identity.tracking_service import (
    DeepSortTrackingService,
    RealTrackingService,
    _main_det_boxes,
)


def _det(class_id: int, xyxy: tuple[int, int, int, int]) -> Detection:
    x1, y1, x2, y2 = xyxy
    return Detection(x=x1, y=y1, w=x2 - x1, h=y2 - y1, confidence=0.9, class_id=class_id)


class _FakeTracker:
    """每次 update 换一批 last_detections(模拟 tracker 每帧覆盖该字段)。"""

    def __init__(self, per_frame: list[list[Detection]]):
        self._per_frame = per_frame
        self._i = -1
        self.last_detections: list[Detection] = []

    def update(self, frame):
        self._i += 1
        self.last_detections = self._per_frame[self._i]

    def get_tracking_results(self) -> list[dict]:
        return []


class TestMainDetBoxes:
    def test_keeps_human_and_pets_drops_head_face(self):
        """宠物必须在、head/face 必须不在。

        宠物只在这里拿得到(tracker 只对 HUMAN 建 track,box_info 里永远没有宠物);
        head/face 散布范围大,纳入并集会把区域撑满全图、面积超上限后永久回退全景。
        """
        tracker = _FakeTracker([[]])
        tracker.last_detections = [
            _det(Detection.CLASS_HUMAN, (10, 10, 50, 90)),
            _det(Detection.CLASS_CAT, (100, 100, 130, 120)),
            _det(Detection.CLASS_DOG, (200, 20, 250, 80)),
            _det(Detection.CLASS_HEAD, (20, 10, 40, 30)),
            _det(Detection.CLASS_FACE, (22, 14, 38, 28)),
        ]
        assert _main_det_boxes(tracker) == [
            (10, 10, 50, 90),
            (100, 100, 130, 120),
            (200, 20, 250, 80),
        ]

    def test_no_tracker_attr_or_empty_is_empty(self):
        # 无 tracker / 无 last_detections 属性 → 空(crop 侧退化为只用运动块,不抛)
        assert _main_det_boxes(None) == []
        assert _main_det_boxes(object()) == []
        assert _main_det_boxes(_FakeTracker([[]])) == []


@pytest.mark.parametrize("cls", [RealTrackingService, DeepSortTrackingService])
class TestAnalyzeAccumulates:
    """analyze 必须在帧循环**内**取框。

    ``last_detections`` 每帧被覆盖,循环外只剩最后一帧那批 —— 那正是本 PR 要修的
    「末帧快照」语义。这两条用例把逐帧累积钉住。
    """

    @staticmethod
    def _service(cls, per_frame: list[list[Detection]]):
        # 绕开 __init__:它会加载 ONNX 检测/ReID 模型,单测里既慢又要模型文件。
        svc = object.__new__(cls)
        svc._tracker = _FakeTracker(per_frame)
        return svc

    def test_boxes_from_every_frame_present(self, cls):
        per_frame = [
            [_det(Detection.CLASS_HUMAN, (10, 10, 30, 40))],
            [_det(Detection.CLASS_CAT, (200, 200, 240, 230))],
            [_det(Detection.CLASS_HUMAN, (12, 12, 32, 42))],
        ]
        svc = self._service(cls, per_frame)
        frames = [np.zeros((64, 64, 3), dtype=np.uint8) for _ in per_frame]
        resp = svc.analyze(frames, fps=1)
        assert resp.main_det_boxes == [
            (10, 10, 30, 40),
            (200, 200, 240, 230),
            (12, 12, 32, 42),
        ]

    def test_first_frame_box_survives_when_last_frame_empty(self, cls):
        """末帧无检测时首帧的框仍在 —— 只取末帧的实现会在这里退成空。"""
        per_frame = [[_det(Detection.CLASS_HUMAN, (10, 10, 30, 40))], [], []]
        svc = self._service(cls, per_frame)
        frames = [np.zeros((64, 64, 3), dtype=np.uint8) for _ in per_frame]
        assert svc.analyze(frames, fps=1).main_det_boxes == [(10, 10, 30, 40)]

    def test_no_frames_returns_empty_boxes(self, cls):
        svc = self._service(cls, [])
        assert svc.analyze([], fps=1).main_det_boxes == []
