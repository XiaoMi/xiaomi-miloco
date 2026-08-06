"""tracker → TrackedObject → IdentityEngine 接缝的字段透传测试。

``detected_this_frame`` / ``confidence`` 由 tracker 逐帧产出、``IdentityEngine`` 直接
消费，中间要穿过 ``_build_response``(dict → TrackedObject) 与 ``_to_tracking_dicts``
(TrackedObject → dict) 两次折叠。任一环丢掉都不会报错——engine 侧只会取默认值
(恒 True / 恒 1.0)让对应的闸静默失效，所以在接缝上钉死。

历史问题：``TrackedObject`` 早期装不下这两个字段，DeepSORT 路径输出的 coasting
(人已离开或跟丢后仍存活的 track，其框停在上一次真匹配的位置)到 engine 时一律变成
"本帧真检测到"，抗遮挡 / omni 候选 / 名册位置 / no_person / tier_u 推图 / tier_c
入队各闸全部空转；``confidence`` 被硬编码成 1.0 则让 tier_u 候选打分里权重最大的
那一维退化成常数。
"""

from __future__ import annotations

import pytest
from miloco.perception.engine.identity.identity import _to_tracking_dicts
from miloco.perception.engine.identity.tracking_service import _build_response

# DeepSortTracker.get_tracking_results 的输出形状：coasting track 照常输出，
# 靠 detected_this_frame=False 与真检测框区分。
COASTING = {
    "id": 7, "class_id": 0, "bbox": (100, 100, 100, 200),
    "xyxy": (100, 100, 200, 300), "confidence": 0.31,
    "hits": 9, "age": 30, "time_since_update": 3,
    "detected_this_frame": False,
}
DETECTED = {
    "id": 8, "class_id": 0, "bbox": (400, 100, 100, 200),
    "xyxy": (400, 100, 500, 300), "confidence": 0.87,
    "hits": 12, "age": 12, "time_since_update": 0,
    "detected_this_frame": True,
}


def _seam(results: list[dict]) -> list[dict]:
    """跑完整接缝：tracker dict → TrackedObject → engine 侧 dict。"""
    resp = _build_response(results, n_frames=6, fps=2)
    return _to_tracking_dicts(resp.object_info)


class TestDetectedThisFrameSurvivesSeam:
    """coasting 标记必须穿过 TrackedObject。"""

    def test_coasting_stays_false(self):
        """coasting track 到 engine 侧仍是 False，不被默认值顶成 True。"""
        assert _seam([dict(COASTING)])[0]["detected_this_frame"] is False

    def test_detected_stays_true(self):
        """真检测命中的 track 保持 True。"""
        assert _seam([dict(DETECTED)])[0]["detected_this_frame"] is True

    def test_mixed_tracks_keep_own_flag(self):
        """同窗混合时各 track 带各自的标记，不互相串。"""
        flags = {d["id"]: d["detected_this_frame"] for d in _seam([dict(COASTING), dict(DETECTED)])}
        assert flags == {7: False, 8: True}

    def test_key_present_so_engine_default_never_applies(self):
        """engine 取值走 ``tr.get("detected_this_frame", True)``：键必须真实存在，
        缺键会让 coasting 被默认值静默顶成 True。"""
        assert "detected_this_frame" in _seam([dict(COASTING)])[0]


class TestDetectorConfidenceSurvivesSeam:
    """检测置信度必须穿过 TrackedObject（tier_u 质量门与候选打分消费）。"""

    def test_low_confidence_not_inflated(self):
        """低置信框不能在接缝上被抬成 1.0，否则 detector_conf_min 门永不拒。"""
        assert _seam([dict(COASTING)])[0]["confidence"] == pytest.approx(0.31)

    def test_per_track_confidence(self):
        """每个 track 保留自己的置信度。"""
        confs = {d["id"]: d["confidence"] for d in _seam([dict(COASTING), dict(DETECTED)])}
        assert confs[7] == pytest.approx(0.31)
        assert confs[8] == pytest.approx(0.87)


class TestSeamDefaults:
    """tracker 未给字段时的回退（mock / 旧 convert_response 路径）。"""

    def test_missing_fields_treated_as_detected(self):
        """缺字段视为真检测 + 满置信：这些路径不产生 coasting，也不参与质量筛选。"""
        out = _seam([{"id": 1, "xyxy": (0, 0, 10, 20), "class_id": 0}])[0]
        assert out["detected_this_frame"] is True
        assert out["confidence"] == pytest.approx(1.0)
