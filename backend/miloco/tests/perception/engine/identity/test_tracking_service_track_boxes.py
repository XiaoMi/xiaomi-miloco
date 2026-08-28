# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tracking Service —— 单帧人像注入的逐帧取框通路。

与隔壁 ``test_tracking_service_det_boxes``（Smart Crop 的 ``main_det_boxes``）是**两套**需求：
那边要"窗口内出现过的一切主体"的空间并集、不带 track_id；这边要"某个 track 在哪一帧最大"，
必须带 track_id。两者共用同一趟帧循环，但口径不同、不能互相替代。

口径若在此漂移，下游只会表现为"注入的人像悄悄换成了另一帧/另一个人"，没有任何测试会响。
"""

import numpy as np
import pytest
from miloco.perception.engine.identity.tracking_service import (
    DeepSortTrackingService,
    RealTrackingService,
    _track_boxes,
)


def _res(track_id: int, xyxy: tuple[int, int, int, int], detected: bool = True) -> dict:
    """一条 get_tracking_results() 记录（只放本通路读的字段）。"""
    return {"id": track_id, "xyxy": xyxy, "detected_this_frame": detected}


class _FakeTracker:
    """每次 update 换一批 get_tracking_results 返回值（模拟 tracker 每帧推进状态）。"""

    def __init__(self, per_frame: list[list[dict]]):
        self._per_frame = per_frame
        self._i = -1
        self._cur: list[dict] = []
        self.last_detections: list = []

    def update(self, frame):
        self._i += 1
        self._cur = self._per_frame[self._i]

    def get_tracking_results(self) -> list[dict]:
        return self._cur


class TestTrackBoxes:
    def test_maps_track_id_to_xyxy(self):
        tracker = _FakeTracker([[_res(3, (10, 20, 30, 60)), _res(7, (100, 0, 140, 90))]])
        tracker.update(None)
        assert _track_boxes(tracker) == {3: (10, 20, 30, 60), 7: (100, 0, 140, 90)}

    def test_drops_coasting_tracks(self):
        """coasting 帧的框是上一次真匹配时的检测框、原地冻结。

        拿它去裁**当前**帧会裁到人已离开的背景、甚至隔壁那个人 —— 而注入是把图绑到
        track_id 上，绑错等于直接喂一个错身份证据。所以这里必须丢。
        """
        tracker = _FakeTracker([[
            _res(1, (10, 10, 50, 90), detected=True),
            _res(2, (200, 10, 240, 90), detected=False),   # coasting → 丢
        ]])
        tracker.update(None)
        assert _track_boxes(tracker) == {1: (10, 10, 50, 90)}

    def test_missing_flag_treated_as_detected(self):
        """缺字段取乐观值，与接缝另一侧（IdentityEngine / extractor）同口径。

        缺字段是"未知"而非"否定"；取 fail-closed 值会让缺字段的路径整条失效（一张图都注入不出），
        是更坏的失败模式。
        """
        tracker = _FakeTracker([[{"id": 5, "xyxy": (1, 2, 3, 4)}]])
        tracker.update(None)
        assert _track_boxes(tracker) == {5: (1, 2, 3, 4)}

    def test_empty_results_is_empty_dict(self):
        tracker = _FakeTracker([[]])
        tracker.update(None)
        assert _track_boxes(tracker) == {}


@pytest.mark.parametrize("cls", [RealTrackingService, DeepSortTrackingService])
class TestAnalyzeCollectsPerFrame:
    """analyze 必须在帧循环**内**取框。

    ``get_tracking_results()`` 在循环外只剩末帧状态 —— 那正是"窗内面积最大帧"算不出来的原因。
    这几条把逐帧收集钉住。
    """

    @staticmethod
    def _service(cls, per_frame: list[list[dict]]):
        # 绕开 __init__：它会加载 ONNX 检测/ReID 模型，单测里既慢又要模型文件。
        svc = object.__new__(cls)
        svc._tracker = _FakeTracker(per_frame)
        return svc

    def _analyze(self, cls, per_frame):
        svc = self._service(cls, per_frame)
        frames = [np.zeros((64, 64, 3), dtype=np.uint8) for _ in per_frame]
        return svc.analyze(frames, fps=1)

    def test_one_dict_per_frame_in_order(self, cls):
        resp = self._analyze(cls, [
            [_res(1, (0, 0, 20, 50))],
            [_res(1, (0, 0, 40, 100)), _res(2, (300, 0, 340, 80))],
            [_res(2, (305, 0, 345, 82))],
        ])
        assert resp.per_frame_track_boxes == [
            {1: (0, 0, 20, 50)},
            {1: (0, 0, 40, 100), 2: (300, 0, 340, 80)},
            {2: (305, 0, 345, 82)},
        ]

    def test_middle_frame_survives_when_last_frame_lost_the_track(self, cls):
        """末帧丢了该 track 时中间帧的框仍在 —— 只取末帧的实现会在这里退成空。

        这正是本能力要的信息：人走近过（中间帧框大）、末帧又转身走远/跟丢，
        注入该拿中间那张，而不是没得可拿。
        """
        resp = self._analyze(cls, [
            [_res(1, (0, 0, 40, 120))],
            [],
            [],
        ])
        assert resp.per_frame_track_boxes == [{1: (0, 0, 40, 120)}, {}, {}]

    def test_coasting_frames_leave_holes(self, cls):
        """窗内 coasting 的帧上该 track 不留框，选帧时自然跳过它。"""
        resp = self._analyze(cls, [
            [_res(1, (0, 0, 40, 120), detected=True)],
            [_res(1, (0, 0, 40, 120), detected=False)],
        ])
        assert resp.per_frame_track_boxes == [{1: (0, 0, 40, 120)}, {}]

    def test_no_frames_returns_empty(self, cls):
        svc = self._service(cls, [])
        assert svc.analyze([], fps=1).per_frame_track_boxes == []
