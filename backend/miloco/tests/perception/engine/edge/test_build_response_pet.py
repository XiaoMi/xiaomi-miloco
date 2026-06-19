"""tracking_service._build_response 与 _convert_type 宠物行为测试。

覆盖:
- _build_response 当前将所有 track 硬编码为 HUMAN_BODY（记录当前行为）
- _convert_type 对 "pet" 的正确映射
- _convert_type 对未知类型的 fallback 行为
- convert_response 对 pet 类型的解析（边界 case 扩展）
"""

from __future__ import annotations

import pytest

from miloco.perception.engine.identity.tracking_service import (
    _build_response,
    _convert_type,
    convert_response,
)
from miloco.perception.engine.types import ObjectType


# =============================================================================
# _convert_type
# =============================================================================


class TestConvertType:
    def test_pet_maps_to_pet(self):
        assert _convert_type("pet") == ObjectType.PET

    def test_human_with_face(self):
        assert _convert_type("human_with_face") == ObjectType.HUMAN_WITH_FACE

    def test_human_body(self):
        assert _convert_type("human_body") == ObjectType.HUMAN_BODY

    def test_human_face(self):
        assert _convert_type("human_face") == ObjectType.HUMAN_FACE

    def test_human(self):
        assert _convert_type("human") == ObjectType.HUMAN

    def test_unknown_type_defaults_to_human(self):
        """未知类型 fallback 到 HUMAN（包括 "cat"/"dog" 等无独立映射的类型）。"""
        assert _convert_type("cat") == ObjectType.HUMAN
        assert _convert_type("dog") == ObjectType.HUMAN
        assert _convert_type("unknown") == ObjectType.HUMAN
        assert _convert_type("") == ObjectType.HUMAN


# =============================================================================
# _build_response — 当前行为记录（硬编码 HUMAN_BODY）
# =============================================================================


class TestBuildResponse:
    def test_empty_results(self):
        """空结果返回空 object_info。"""
        resp = _build_response([], n_frames=6, fps=2)
        assert len(resp.object_info) == 0
        assert resp.frame_info.fps == 2

    def test_single_human_track(self):
        """单个 human track 正确转换。"""
        results = [{"id": 0, "xyxy": (100, 200, 300, 400)}]
        resp = _build_response(results, n_frames=6, fps=2)
        assert len(resp.object_info) == 1
        obj = resp.object_info[0]
        assert obj.type == ObjectType.HUMAN_BODY
        assert obj.track_id == 0
        assert obj.face_id == "none"

    def test_all_tracks_mapped_to_human_body(self):
        """当前实现：所有 track（不论实际 class_id）都映射为 HUMAN_BODY。

        这是已知的设计限制——当 track_human_only=False 启用后，
        _build_response 需要根据 class_id 映射到正确的 ObjectType。
        """
        results = [
            {"id": 0, "xyxy": (100, 200, 300, 400)},
            {"id": 1, "xyxy": (300, 100, 400, 200)},
        ]
        resp = _build_response(results, n_frames=6, fps=2)
        assert all(obj.type == ObjectType.HUMAN_BODY for obj in resp.object_info)

    def test_box_info_uses_last_frame_index(self):
        """box_info 的 frame_index 应为最后一帧的索引。"""
        results = [{"id": 0, "xyxy": (10, 20, 110, 120)}]
        resp = _build_response(results, n_frames=6, fps=2)
        box = resp.object_info[0].box_info[0]
        assert box.frame_index == 5  # 0-indexed last = n_frames - 1

    def test_box_coords_converted_to_xywh(self):
        """xyxy 坐标被转换为 (x, y, w, h) 存入 box_info。"""
        results = [{"id": 0, "xyxy": (100, 200, 350, 500)}]
        resp = _build_response(results, n_frames=4, fps=1)
        box = resp.object_info[0].box_info[0]
        assert box.boxes["human_body"] == (100, 200, 250, 300)

    def test_frame_info_timestamps(self):
        """frame_info 的时间戳区间与帧数/fps 一致。"""
        results = [{"id": 0, "xyxy": (0, 0, 10, 10)}]
        resp = _build_response(results, n_frames=10, fps=5)
        # duration = 10/5 = 2 seconds = 2000 ms
        duration_ms = resp.frame_info.end_timestamp - resp.frame_info.start_timestamp
        assert abs(duration_ms - 2000) < 50  # 允许小误差


# =============================================================================
# convert_response — pet 类型边界 case
# =============================================================================


class TestConvertResponsePetCases:
    def test_pet_with_empty_box_info(self):
        """pet 对象 box_info 为空时不崩溃。"""
        raw = {
            "frames_info": {"start_timestamp": 0, "end_timestamp": 3000, "fps": 2},
            "objects_info": [
                {
                    "type": "pet",
                    "face_id": "none",
                    "track_id": 1,
                    "box_info": [],
                }
            ],
        }
        resp = convert_response(raw)
        assert len(resp.object_info) == 1
        assert resp.object_info[0].type == ObjectType.PET
        assert resp.object_info[0].box_info == []

    def test_multiple_pets_different_tracks(self):
        """多只宠物各自有不同 track_id。"""
        raw = {
            "frames_info": {"start_timestamp": 0, "end_timestamp": 3000, "fps": 2},
            "objects_info": [
                {
                    "type": "pet",
                    "face_id": "none",
                    "track_id": 1,
                    "box_info": [[0, {"pet_body": [100, 200, 50, 50]}]],
                },
                {
                    "type": "pet",
                    "face_id": "none",
                    "track_id": 2,
                    "box_info": [[0, {"pet_body": [300, 400, 60, 60]}]],
                },
            ],
        }
        resp = convert_response(raw)
        assert len(resp.object_info) == 2
        track_ids = {obj.track_id for obj in resp.object_info}
        assert track_ids == {1, 2}

    def test_pet_and_human_coexist(self):
        """人和宠物混合解析。"""
        raw = {
            "frames_info": {"start_timestamp": 0, "end_timestamp": 3000, "fps": 2},
            "objects_info": [
                {
                    "type": "human_with_face",
                    "face_id": "person_a",
                    "track_id": 0,
                    "box_info": [[0, {"human_body": [100, 100, 200, 400]}]],
                },
                {
                    "type": "pet",
                    "face_id": "none",
                    "track_id": 1,
                    "box_info": [[1, {"pet_body": [400, 300, 80, 60]}]],
                },
            ],
        }
        resp = convert_response(raw)
        assert len(resp.object_info) == 2
        types = {obj.type for obj in resp.object_info}
        assert types == {ObjectType.HUMAN_WITH_FACE, ObjectType.PET}
