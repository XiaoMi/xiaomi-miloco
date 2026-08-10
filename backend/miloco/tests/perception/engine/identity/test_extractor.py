"""抽取算法 (extractor.py) 单测。

不依赖 ONNX 模型——用 mock detector 注入构造好的 Detection 对象,验证三个入口
(image / video / pool) 的核心行为:质量 Gate / 评分 / face 关联 / 短 track 丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import numpy as np
from miloco.perception.engine.identity.extractor import (
    _compute_sharpness,
    _passes_quality_gate,
    _score_candidate,
    extract_from_image,
    extract_from_pool,
    extract_from_video,
)

# =============================================================================
# Mock Detection / Detector
# =============================================================================


@dataclass
class _MockDet:
    x: int
    y: int
    w: int
    h: int
    confidence: float
    class_id: int
    CLASS_HUMAN = 0
    CLASS_FACE = 4

    @property
    def bbox(self):
        return (self.x, self.y, self.w, self.h)

    @property
    def xyxy(self):
        return (self.x, self.y, self.x + self.w, self.y + self.h)


def _mock_detector(dets: list[_MockDet]):
    d = MagicMock()
    d.detect = MagicMock(return_value=dets)
    return d


def _make_frame(h: int = 720, w: int = 1280, fill: int = 128) -> np.ndarray:
    # 加噪声让 Laplacian variance > 50(过 sharpness gate)
    frame = np.full((h, w, 3), fill, dtype=np.uint8)
    rng = np.random.default_rng(42)
    noise = rng.integers(0, 100, (h, w, 3), dtype=np.uint8)
    return cv2_mix(frame, noise)


def cv2_mix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """简单像素混合,避免依赖 cv2 addWeighted。"""
    return ((a.astype(np.uint16) + b.astype(np.uint16)) // 2).astype(np.uint8)


# =============================================================================
# helpers / 评分单测
# =============================================================================


class TestHelpers:
    def test_sharpness_returns_zero_on_empty(self):
        assert _compute_sharpness(None) == 0.0
        assert _compute_sharpness(np.zeros((0, 0, 3), dtype=np.uint8)) == 0.0

    def test_sharpness_distinguishes_flat_vs_textured(self):
        # tier_c 入库锐度门用 50 作地板拦"过糊"画面(见 engine._TIER_C_MIN_SHARPNESS):
        # 纯色/退化画面 Laplacian 方差应远低于 50、高频纹理应远高于 50, 锁住该地板的区分假设。
        flat = np.full((96, 64, 3), 128, dtype=np.uint8)        # 纯色 → 几乎无边缘
        assert _compute_sharpness(flat) < 50.0
        noisy = np.random.default_rng(0).integers(
            0, 256, size=(96, 64, 3), dtype=np.uint8,
        )                                                       # 高频噪声 → 边缘密集
        assert _compute_sharpness(noisy) > 50.0

    def test_quality_gate_passes_normal(self):
        assert _passes_quality_gate(
            area_ratio=0.1, aspect=0.5, sharpness=100.0, detector_conf=0.8,
        )

    def test_quality_gate_rejects_tiny_area(self):
        assert not _passes_quality_gate(
            area_ratio=0.01, aspect=0.5, sharpness=100.0, detector_conf=0.8,
        )

    def test_quality_gate_rejects_extreme_aspect(self):
        assert not _passes_quality_gate(
            area_ratio=0.1, aspect=0.1, sharpness=100.0, detector_conf=0.8,
        )
        assert not _passes_quality_gate(
            area_ratio=0.1, aspect=5.0, sharpness=100.0, detector_conf=0.8,
        )

    def test_quality_gate_rejects_low_sharpness(self):
        assert not _passes_quality_gate(
            area_ratio=0.1, aspect=0.5, sharpness=10.0, detector_conf=0.8,
        )

    def test_score_face_bonus(self):
        no_face = _score_candidate(area_ratio=0.1, detector_conf=0.8,
                                    sharpness=200.0, has_face=False)
        with_face = _score_candidate(area_ratio=0.1, detector_conf=0.8,
                                      sharpness=200.0, has_face=True)
        # face_bonus = 1.2
        assert abs(with_face / no_face - 1.2) < 1e-6


# =============================================================================
# extract_from_image
# =============================================================================


class TestExtractFromImage:
    def test_single_body_returns_one_candidate(self):
        frame = _make_frame()
        dets = [_MockDet(x=200, y=100, w=200, h=400, confidence=0.9,
                          class_id=_MockDet.CLASS_HUMAN)]
        out = extract_from_image(frame, detector=_mock_detector(dets))
        assert len(out) == 1
        assert out[0].track_id is None
        assert out[0].face_crop is None
        assert out[0].score > 0
        assert out[0].body_crop is not None
        assert 0 <= out[0].phash < (1 << 64)

    def test_multi_body_returns_multi_candidates_sorted_by_score(self):
        frame = _make_frame()
        dets = [
            # 两个都过 gate(area_ratio 都 > 0.05),conf 不同分高低
            _MockDet(x=100, y=100, w=200, h=400, confidence=0.6,
                      class_id=_MockDet.CLASS_HUMAN),
            _MockDet(x=400, y=100, w=300, h=600, confidence=0.95,
                      class_id=_MockDet.CLASS_HUMAN),
        ]
        out = extract_from_image(frame, detector=_mock_detector(dets))
        assert len(out) == 2
        # 大 + 高 conf 那个应得高分
        assert out[0].score > out[1].score
        assert out[0].detector_conf == 0.95

    def test_face_association_lifts_score(self):
        frame = _make_frame()
        body = _MockDet(x=200, y=100, w=200, h=400, confidence=0.9,
                         class_id=_MockDet.CLASS_HUMAN)
        face_inside_body = _MockDet(x=240, y=130, w=100, h=100, confidence=0.9,
                                     class_id=_MockDet.CLASS_FACE)
        out = extract_from_image(frame, detector=_mock_detector([body, face_inside_body]))
        assert len(out) == 1
        # face 关联成功 → face_crop 非 None + score 含 face_bonus
        assert out[0].face_crop is not None
        # 同样 body 但无 face → score 比 face 版小 ~17%
        out_no_face = extract_from_image(frame, detector=_mock_detector([body]))
        assert out[0].score > out_no_face[0].score

    def test_no_body_returns_empty(self):
        frame = _make_frame()
        out = extract_from_image(frame, detector=_mock_detector([]))
        assert out == []

    def test_too_small_body_rejected_by_gate(self):
        frame = _make_frame()
        # area_ratio = 50 × 100 / (720 × 1280) ≈ 0.005 < 0.05 阈值
        tiny = _MockDet(x=10, y=10, w=50, h=100, confidence=0.9,
                         class_id=_MockDet.CLASS_HUMAN)
        out = extract_from_image(frame, detector=_mock_detector([tiny]))
        assert out == []

    def test_reid_extractor_called_when_provided(self):
        """图像路径必须显式调 extract_feature 算 emb,不像视频路径能从跟踪侧读。"""
        frame = _make_frame()
        body = _MockDet(x=200, y=100, w=200, h=400, confidence=0.9,
                         class_id=_MockDet.CLASS_HUMAN)
        # 注入 mock reid_extractor
        fake_emb = np.ones(128, dtype=np.float32) / np.sqrt(128)
        reid_extractor = MagicMock()
        reid_extractor.extract_feature = MagicMock(return_value=fake_emb)
        out = extract_from_image(
            frame, detector=_mock_detector([body]), reid_extractor=reid_extractor,
        )
        assert len(out) == 1
        assert out[0].reid_embedding is not None
        np.testing.assert_array_equal(out[0].reid_embedding, fake_emb)
        # 一定被调过 1 次(图像路径必须现场抽 emb)
        assert reid_extractor.extract_feature.call_count == 1

    def test_no_reid_extractor_returns_none_emb(self):
        """不传 reid_extractor 时,reid_embedding 留 None(向后兼容老调用方)。"""
        frame = _make_frame()
        body = _MockDet(x=200, y=100, w=200, h=400, confidence=0.9,
                         class_id=_MockDet.CLASS_HUMAN)
        out = extract_from_image(frame, detector=_mock_detector([body]))
        assert len(out) == 1
        assert out[0].reid_embedding is None


# =============================================================================
# extract_from_pool
# =============================================================================


@dataclass
class _MockCropEntry:
    body_crop: np.ndarray
    face_crop: np.ndarray | None
    sharpness: float
    bbox_xyxy: tuple
    detector_conf: float
    frame_index: int
    captured_at: float
    track_id: int
    cam_id: str
    reid_embedding: np.ndarray | None = None


@dataclass
class _MockClusterCandidate:
    cluster_id: str
    members: list
    representative_crop: Any
    total_crops: int
    span_cam_count: int
    earliest_ts: float
    latest_ts: float
    per_cam_representative: dict


class TestExtractFromPool:
    def _make_crop_entry(self, *, sharpness: float = 200.0,
                          with_emb: bool = True, seed: int = 0):
        body = np.random.default_rng(seed).integers(0, 255, (200, 100, 3), dtype=np.uint8)
        emb = None
        if with_emb:
            rng = np.random.default_rng(seed + 100)
            v = rng.standard_normal(128).astype(np.float32)
            emb = v / np.linalg.norm(v)
        return _MockCropEntry(
            body_crop=body,
            face_crop=None,
            sharpness=sharpness,
            bbox_xyxy=(100, 100, 200, 400),
            detector_conf=0.9,
            frame_index=10,
            captured_at=1700000000.0,
            track_id=7,
            cam_id="cam-a",
            reid_embedding=emb,
        )

    def test_pool_path_carries_emb_to_scored(self):
        ce = self._make_crop_entry()
        cluster = _MockClusterCandidate(
            cluster_id="cid-1",
            members=[("cam-a", 7)],
            representative_crop=ce,
            total_crops=1,
            span_cam_count=1,
            earliest_ts=ce.captured_at,
            latest_ts=ce.captured_at,
            per_cam_representative={"cam-a": ce},
        )
        out = extract_from_pool([cluster])
        assert "cid-1" in out
        assert len(out["cid-1"]) == 1
        sc = out["cid-1"][0]
        assert sc.cluster_id == "cid-1"
        assert sc.reid_embedding is not None
        # cluster 路径已知 cam / track
        assert sc.cam_id == "cam-a"
        assert sc.track_id == 7

    def test_pool_path_dedup_overlapping_reps(self):
        """representative_crop 跟 per_cam_representative 重叠时只算一次。"""
        ce = self._make_crop_entry()
        cluster = _MockClusterCandidate(
            cluster_id="cid-2",
            members=[("cam-a", 7)],
            representative_crop=ce,
            total_crops=1,
            span_cam_count=1,
            earliest_ts=ce.captured_at,
            latest_ts=ce.captured_at,
            per_cam_representative={"cam-a": ce},  # 同一个 ce 对象
        )
        out = extract_from_pool([cluster])
        # 即使 representative 同一对象出现两次,只产 1 个 ScoredCandidate
        assert len(out["cid-2"]) == 1

    def test_pool_path_low_sharpness_rejected(self):
        ce = self._make_crop_entry(sharpness=10.0)  # < 50 阈值
        cluster = _MockClusterCandidate(
            cluster_id="cid-3",
            members=[("cam-a", 7)],
            representative_crop=ce,
            total_crops=1,
            span_cam_count=1,
            earliest_ts=ce.captured_at,
            latest_ts=ce.captured_at,
            per_cam_representative={},
        )
        out = extract_from_pool([cluster])
        assert out == {}  # 被 gate 拒


# =============================================================================
# extract_from_video(DeepSORT 路径)
# =============================================================================


class TestExtractFromVideoDeepSort:
    """验证 extract_from_video 走 DeepSortTracker:
    - 用 deep_sort_tracker_factory(不是 sort_tracker_factory)
    - 每个 track 的 emb 通过 tracker.get_track_embedding(tid) 读取(零额外推理)
    """

    def test_factory_kw_is_deep_sort(self):
        """API 表面强约束:接 deep_sort_tracker_factory 而非旧 sort_tracker_factory。"""
        import inspect
        sig = inspect.signature(extract_from_video)
        params = list(sig.parameters.keys())
        assert "deep_sort_tracker_factory" in params, (
            "extract_from_video 必须接 deep_sort_tracker_factory(主流程 DeepSORT 路径)"
        )
        assert "sort_tracker_factory" not in params, (
            "旧 sort_tracker_factory 参数已被替换"
        )

    def test_emb_pulled_from_get_track_embedding(self, tmp_path):
        """tracker.get_track_embedding 被调,emb 塞进 ScoredCandidate。

        构造 mock tracker 暴露 update / get_tracking_results / get_track_embedding;
        视频用一个最小的 mp4 文件占位(逐帧 mock detector 控制结果)。
        """
        video_path = str(tmp_path / "tiny.mp4")
        self._write_tiny_mp4(video_path)

        body_det = _MockDet(x=200, y=100, w=300, h=600, confidence=0.9,
                             class_id=_MockDet.CLASS_HUMAN)
        det = _mock_detector([body_det])

        # mock DeepSortTracker
        fake_emb = np.ones(128, dtype=np.float32) / np.sqrt(128)
        mock_tracker = MagicMock()
        mock_tracker.update = MagicMock()
        mock_tracker.get_tracking_results = MagicMock(return_value=[{
            "id": 42, "class_id": _MockDet.CLASS_HUMAN,
            "bbox": (200, 100, 300, 600), "xyxy": (200, 100, 500, 700),
            "confidence": 0.9, "hits": 5, "age": 5, "time_since_update": 0,
        }])
        mock_tracker.get_track_embedding = MagicMock(return_value=fake_emb)

        out = extract_from_video(
            video_path,
            detector=det,
            deep_sort_tracker_factory=lambda: mock_tracker,
            max_frames=4,
            min_track_hits=2,
        )
        assert 42 in out
        cands = out[42]
        assert len(cands) >= 2
        # 每个 candidate 的 emb 都该是 mock 返的(从 get_track_embedding 读)
        for c in cands:
            assert c.reid_embedding is not None
            np.testing.assert_array_equal(c.reid_embedding, fake_emb)
        # 验证 get_track_embedding 真的被调过(零额外推理依赖它)
        assert mock_tracker.get_track_embedding.call_count >= len(cands)

    def test_captured_at_uses_real_fps(self, tmp_path):
        """captured_at 用真实 fps (CAP_PROP_FPS) 而非硬编码 0.1 s/帧。

        回归保护: 旧代码硬编码 fidx * 0.1 假设 10 fps, 60 fps 视频上把相邻
        0.35s 帧算成 2.1s, 让 select_topk 的 time_gap_sec_min=1.0 检查失效,
        同人相邻帧通过去重 → topk 选出来"看着差不多"。本测试锁定 fps fix
        行为, 防未来被改回硬编码。
        """
        # 写 6 帧 60 fps mp4 → 真实时间间隔 ≈ 0.0167s
        video_path = str(tmp_path / "60fps.mp4")
        target_fps = 60
        self._write_tiny_mp4(video_path, fps=target_fps)

        body_det = _MockDet(x=200, y=100, w=300, h=600, confidence=0.9,
                             class_id=_MockDet.CLASS_HUMAN)
        det = _mock_detector([body_det])
        fake_emb = np.ones(128, dtype=np.float32) / np.sqrt(128)
        mock_tracker = MagicMock()
        mock_tracker.update = MagicMock()
        mock_tracker.get_tracking_results = MagicMock(return_value=[{
            "id": 1, "class_id": _MockDet.CLASS_HUMAN,
            "bbox": (200, 100, 300, 600), "xyxy": (200, 100, 500, 700),
            "confidence": 0.9, "hits": 5, "age": 5, "time_since_update": 0,
        }])
        mock_tracker.get_track_embedding = MagicMock(return_value=fake_emb)

        out = extract_from_video(
            video_path,
            detector=det,
            deep_sort_tracker_factory=lambda: mock_tracker,
            max_frames=6,
            min_track_hits=2,
        )
        cands = out[1]
        assert len(cands) >= 2, "需要 ≥2 张才能验证相邻 captured_at 间隔"
        # 排序按 frame_index, 验证相邻 captured_at 差 ≈ 1/fps
        cands_sorted = sorted(cands, key=lambda c: c.frame_index)
        # 用 60 fps 期望 0.0167s, 旧硬编码 0.1 会算成 0.1s+, 间隔差 ≥ 6 倍
        for i in range(1, len(cands_sorted)):
            df = cands_sorted[i].frame_index - cands_sorted[i-1].frame_index
            expected = df / target_fps
            actual = cands_sorted[i].captured_at - cands_sorted[i-1].captured_at
            # tolerance 拉大点: 浮点 + 抽样跨度可能不严格 1/fps
            assert abs(actual - expected) < 0.005, (
                f"captured_at 间隔 {actual:.4f}s 偏离真实 fps 期望 {expected:.4f}s; "
                f"可能 fps fix 被改回硬编码 0.1 s/帧 (会让 60fps 视频间隔 ≈ 0.1s)"
            )

    @staticmethod
    def _write_tiny_mp4(path: str, n_frames: int = 6, fps: int = 1) -> None:
        import cv2
        h, w = 720, 1280
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        for _ in range(n_frames):
            writer.write(_make_frame(h, w))
        writer.release()

    @staticmethod
    def _track_dict(tid: int, **extra) -> dict:
        d = {
            "id": tid, "class_id": _MockDet.CLASS_HUMAN,
            "bbox": (200, 100, 300, 600), "xyxy": (200, 100, 500, 700),
            "confidence": 0.9, "hits": 5, "age": 5, "time_since_update": 0,
        }
        d.update(extra)
        return d

    def test_coasting_track_excluded_detected_track_kept(self, tmp_path):
        """coasting(本帧未命中,框停在上一次真匹配位置)的 track 不产出候选,同窗真检测的照常。

        残留框裁出的是背景/他人图,质量门拦不住(置信度沿用最后一次真匹配的值、
        背景裁图清晰度也不低),会进 top-K 并经注册写进身份库。
        """
        video_path = str(tmp_path / "coasting.mp4")
        self._write_tiny_mp4(video_path)

        body_det = _MockDet(x=200, y=100, w=300, h=600, confidence=0.9,
                            class_id=_MockDet.CLASS_HUMAN)
        det = _mock_detector([body_det])

        mock_tracker = MagicMock()
        mock_tracker.update = MagicMock()
        mock_tracker.get_tracking_results = MagicMock(return_value=[
            self._track_dict(42, detected_this_frame=True),
            # 跟丢后仍存活的 track:time_since_update>0、bbox 停在上一次真匹配位置
            self._track_dict(99, detected_this_frame=False, time_since_update=1),
        ])
        mock_tracker.get_track_embedding = MagicMock(
            return_value=np.ones(128, dtype=np.float32) / np.sqrt(128))

        out = extract_from_video(
            video_path, detector=det,
            deep_sort_tracker_factory=lambda: mock_tracker,
            max_frames=4, min_track_hits=2,
        )
        assert 42 in out, "真检测的 track 应照常产出候选"
        assert 99 not in out, "coasting track 的幻影框裁图不应进候选"

    def test_missing_flag_defaults_to_detected(self, tmp_path):
        """tracker 输出不带该字段时按真检测兜底(全链路 fail-open 同口径)。

        取 fail-closed 会让任何漏填该字段的 tracker 实现产不出任何候选,
        注册整条路径静默失效——比放宽是更坏的失败模式。
        """
        video_path = str(tmp_path / "nofield.mp4")
        self._write_tiny_mp4(video_path)

        body_det = _MockDet(x=200, y=100, w=300, h=600, confidence=0.9,
                            class_id=_MockDet.CLASS_HUMAN)
        det = _mock_detector([body_det])

        mock_tracker = MagicMock()
        mock_tracker.update = MagicMock()
        # 刻意不带 detected_this_frame
        mock_tracker.get_tracking_results = MagicMock(return_value=[self._track_dict(7)])
        mock_tracker.get_track_embedding = MagicMock(
            return_value=np.ones(128, dtype=np.float32) / np.sqrt(128))

        out = extract_from_video(
            video_path, detector=det,
            deep_sort_tracker_factory=lambda: mock_tracker,
            max_frames=4, min_track_hits=2,
        )
        assert 7 in out

    def test_same_frame_face_crop_recorded_when_face_associated(self, tmp_path):
        """方案 A: extract_from_video 入池时, 同帧关联到的 face crop 写入
        ScoredCandidate.same_frame_face_crop 字段, 给 V6b helper 后续判 frontal +
        覆盖 face_crop 用。

        构造: 每帧 detector 同时返 1 个 body + 1 个 face (跟 body 重叠), 关联成功
        → cand.same_frame_face_crop 应为非 None ndarray。
        """
        video_path = str(tmp_path / "with_face.mp4")
        self._write_tiny_mp4(video_path)

        # body + face 都返, face bbox 在 body 内 (IoA ≥ 0.5 关联成功)
        body_det = _MockDet(x=200, y=100, w=300, h=600, confidence=0.9,
                             class_id=_MockDet.CLASS_HUMAN)
        face_det = _MockDet(x=300, y=150, w=100, h=120, confidence=0.9,
                             class_id=_MockDet.CLASS_FACE)
        det = _mock_detector([body_det, face_det])

        fake_emb = np.ones(128, dtype=np.float32) / np.sqrt(128)
        mock_tracker = MagicMock()
        mock_tracker.update = MagicMock()
        mock_tracker.get_tracking_results = MagicMock(return_value=[{
            "id": 1, "class_id": _MockDet.CLASS_HUMAN,
            "bbox": (200, 100, 300, 600), "xyxy": (200, 100, 500, 700),
            "confidence": 0.9, "hits": 5, "age": 5, "time_since_update": 0,
        }])
        mock_tracker.get_track_embedding = MagicMock(return_value=fake_emb)

        out = extract_from_video(
            video_path,
            detector=det,
            deep_sort_tracker_factory=lambda: mock_tracker,
            max_frames=4,
            min_track_hits=2,
        )
        cands = out[1]
        # 至少 1 张同帧关联成功的 cand, same_frame_face_crop 非 None
        sf_set = [c for c in cands if c.same_frame_face_crop is not None]
        assert len(sf_set) >= 1, (
            "至少 1 张 cand 应记下同帧 face crop (body+face IoA 关联成功); "
            "若全 None, 方案 A 的 V6b 对齐逻辑失效"
        )
        # 同帧 face 尺寸应跟 mock face_det 的 100x120 接近 (有 padding ±5%)
        sf = sf_set[0].same_frame_face_crop
        assert sf.ndim == 3 and sf.shape[2] == 3, "same_frame_face_crop 应是 BGR ndarray"
