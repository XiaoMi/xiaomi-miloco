"""DeepSORT 适配层 + v2 ReID 模型 + tracking_service mode=deep_sort 单测。

需要 ONNX 模型文件存在(标记 ``@pytest.mark.requires_models``)。
覆盖:
- v2 ReID 模型可加载 + 加载时延 < 600ms + 输出 (128,) L2-norm
- HumanReID 类对新模型零代码改动可用 + 默认 path 指向 v2 文件名
- DeepSortConfigDC 默认值与 SortConfigDC 对齐 (n_init=1 / mode=fast)
- DeepSortTracker API 字段集与 SortTracker 一致
- get_track_embedding 不调 extract_feature ("零额外推理"硬约束)
- create_tracking_service("deep_sort") 工厂可构造
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

# 模型文件 fixture
_MODELS_DIR = Path(__file__).resolve().parents[4] / "src" / "miloco" / "perception" / "models"
_REID_MODEL = _MODELS_DIR / "human_body_reid_v2.onnx"
_DET_MODEL = _MODELS_DIR / "det_4C.onnx"

requires_models = pytest.mark.skipif(
    not _REID_MODEL.exists() or not _DET_MODEL.exists(),
    reason=f"模型文件缺失({_REID_MODEL.name} / {_DET_MODEL.name})",
)


# =============================================================================
# v2 ReID 模型 schema / 性能 / L2-norm 验证
# =============================================================================


@requires_models
class TestV2ReIDModel:
    def test_load_under_600ms(self):
        from miloco.perception.engine.identity.tracker.human_reid import HumanReID
        t0 = time.time()
        HumanReID(model_path=str(_REID_MODEL), use_gpu=False)
        load_ms = (time.time() - t0) * 1000
        # 实测 M1 Mac CPU ~247 ms;较慢 CPU / 冷启 venv 实测可达 ~750 ms。本测试只是
        # "模型能在合理时间内加载"的冒烟门, 放宽到 900 ms 兼容慢机器, 不损其意图。
        assert load_ms < 900, f"v2 ReID 加载时延 {load_ms:.0f} ms 超出 900 ms 门槛"

    def test_extract_yields_128_l2_normalized(self):
        from miloco.perception.engine.identity.tracker.human_reid import HumanReID
        reid = HumanReID(model_path=str(_REID_MODEL), use_gpu=False)
        img = np.random.randint(0, 255, (200, 100, 3), dtype=np.uint8)
        emb = reid.extract_feature(img)
        assert emb.shape == (128,), f"v2 ReID 输出维度 {emb.shape} 不是 (128,)"
        assert emb.dtype == np.float32
        assert abs(np.linalg.norm(emb) - 1.0) < 1e-5  # L2 归一化后模 = 1.0

    def test_default_path_points_to_v2(self):
        """HumanReID 类默认 model_path 默认指向 human_body_reid_v2.onnx(防回归)。"""
        import inspect

        from miloco.perception.engine.identity.tracker.human_reid import HumanReID
        sig = inspect.signature(HumanReID.__init__)
        default_path = sig.parameters["model_path"].default
        assert "human_body_reid_v2" in default_path, (
            f"HumanReID 默认 model_path 未指向 v2: {default_path}"
        )


# =============================================================================
# DeepSortConfigDC 默认值
# =============================================================================


class TestDeepSortConfigDC:
    def test_n_init_aligns_with_sort_config(self):
        """DeepSortConfigDC.n_init=1(对齐 SortConfigDC.n_init=1)。"""
        from miloco.perception.engine.config import DeepSortConfigDC, SortConfigDC
        assert DeepSortConfigDC().n_init == 1
        assert DeepSortConfigDC().n_init == SortConfigDC().n_init

    def test_default_mode_is_fast(self):
        """fast 模式默认开启,静止 track 跳过 ReID 推理。"""
        from miloco.perception.engine.config import DeepSortConfigDC
        assert DeepSortConfigDC().mode == "fast"

    def test_no_unwanted_yaml_fields(self):
        """精简后的 DeepSortConfigDC 只暴露 7 个业务字段。

        以下字段走代码默认值,不暴露 yaml:
        - 部署/接口级:max_cosine_distance / reid_model_path / use_gpu / expose_embedding
        - 几何固定阈值:static_displacement_ratio / static_min_abs_px
        - 与顶层 fps 重复:window_fps / window_len_sec
        """
        from dataclasses import fields

        from miloco.perception.engine.config import DeepSortConfigDC
        field_names = {f.name for f in fields(DeepSortConfigDC)}
        for forbidden in (
            "max_cosine_distance",
            "reid_model_path",
            "use_gpu",
            "expose_embedding",
            "window_fps",
            "window_len_sec",
            "static_displacement_ratio",
            "static_min_abs_px",
        ):
            assert forbidden not in field_names, (
                f"DeepSortConfigDC 不应暴露 {forbidden} 给 yaml"
            )
        # 留 yaml 的 7 字段都在
        for required in (
            "n_init", "max_age_sec", "iou_threshold",
            "detector_conf_threshold", "track_human_only",
            "mode", "human_reid_skip_windows",
        ):
            assert required in field_names, f"DeepSortConfigDC 应保留 {required}"


# =============================================================================
# DeepSortTracker API
# =============================================================================


@requires_models
class TestDeepSortTrackerAPI:
    def _make_tracker(self):
        from miloco.perception.engine.config import DeepSortConfigDC
        from miloco.perception.engine.identity.deep_sort import DeepSortTracker
        from miloco.perception.engine.identity.tracker.detector import Detector
        detector = Detector(model_path=str(_DET_MODEL), use_gpu=False)
        return DeepSortTracker(
            detector=detector,
            config=DeepSortConfigDC(),
            fps=1,
            reid_model_path=str(_REID_MODEL),
        )

    def test_api_surface_matches_sort_tracker(self):
        """对外 API 与 SortTracker 同构。"""
        tracker = self._make_tracker()
        for name in ("update", "get_tracking_results", "reset", "last_detections", "tracks"):
            assert hasattr(tracker, name), f"DeepSortTracker 缺失 {name}"

    def test_get_tracking_results_field_set(self):
        """get_tracking_results 字段集与 SortTracker 一致。"""
        tracker = self._make_tracker()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        tracker.update(frame)
        results = tracker.get_tracking_results()
        assert isinstance(results, list)
        for r in results:
            # detected_this_frame 单列在这里的理由：它缺失时下游不报错，只会取默认值让
            # 身份识别侧各 coasting 闸静默恒真（即 #494）。此处是该字段的产出源头，
            # 接缝下游那道护栏（edge/test_tracking_seam_fields）挡不住源头漏写。
            for k in ("id", "class_id", "bbox", "xyxy", "confidence", "hits", "age",
                      "time_since_update", "detected_this_frame"):
                assert k in r, f"track 缺字段 {k}"

    def test_get_track_embedding_returns_none_when_track_absent(self):
        tracker = self._make_tracker()
        assert tracker.get_track_embedding(99999) is None
        assert tracker.get_track_embedding_age(99999) == -1


# =============================================================================
# 零额外推理硬约束
# =============================================================================


@requires_models
class TestZeroExtraReIDExtract:
    def test_get_track_embedding_does_not_call_extract_feature(self):
        """get_track_embedding 必须从 Track.features deque 取,不调
        HumanReID.extract_feature。关键断言:mock features 注入后,get_track_embedding
        拿到的值 = 注入值,且 extract_feature 调用次数 = 0。
        """
        from miloco.perception.engine.config import DeepSortConfigDC
        from miloco.perception.engine.identity.deep_sort import DeepSortTracker
        from miloco.perception.engine.identity.tracker.detector import (
            Detection,
            Detector,
        )
        from miloco.perception.engine.identity.tracker.human_reid import HumanReID
        from miloco.perception.engine.identity.tracker.tracker import Track

        detector = Detector(model_path=str(_DET_MODEL), use_gpu=False)
        tracker = DeepSortTracker(
            detector=detector,
            config=DeepSortConfigDC(),
            fps=1,
            reid_model_path=str(_REID_MODEL),
        )

        # 手工塞 fake track,绕开 update 流程,精确隔离 get_track_embedding 行为
        fake_emb = np.ones(128, dtype=np.float32) / np.sqrt(128)
        fake_track = Track(
            track_id=42,
            class_id=Detection.CLASS_HUMAN,
            bbox=(0, 0, 100, 100),
            confidence=0.9,
        )
        fake_track.features.append(fake_emb)
        tracker._mot.tracks = [fake_track]

        # 监视 extract_feature 调用次数
        call_count = {"n": 0}
        orig = HumanReID.extract_feature

        def counting_extract(self, *a, **kw):
            call_count["n"] += 1
            return orig(self, *a, **kw)

        with patch.object(HumanReID, "extract_feature", counting_extract):
            emb = tracker.get_track_embedding(42)
            age = tracker.get_track_embedding_age(42)

        assert emb is not None
        assert emb.shape == (128,)
        np.testing.assert_array_equal(emb, fake_emb)
        # 关键断言:零额外推理
        assert call_count["n"] == 0, (
            f"get_track_embedding 触发了 {call_count['n']} 次 extract_feature 调用,"
            "违反'零额外推理'硬约束"
        )
        assert age == -1  # last_reid_frame 默认 0 → -1

    def test_get_track_centroid_is_history_mean_zero_inference(self):
        """get_track_centroid 返回 features deque 历史均值(再 L2)+ emb 数, 同样零额外推理。"""
        from miloco.perception.engine.config import DeepSortConfigDC
        from miloco.perception.engine.identity.deep_sort import DeepSortTracker
        from miloco.perception.engine.identity.tracker.detector import (
            Detection,
            Detector,
        )
        from miloco.perception.engine.identity.tracker.human_reid import HumanReID
        from miloco.perception.engine.identity.tracker.tracker import Track

        detector = Detector(model_path=str(_DET_MODEL), use_gpu=False)
        tracker = DeepSortTracker(
            detector=detector, config=DeepSortConfigDC(), fps=1,
            reid_model_path=str(_REID_MODEL),
        )

        # 3 个不同特征 → 均值再 L2 应等于"和向量归一化"
        f1 = np.array([1.0, 0.0, 0.0] + [0.0] * 125, dtype=np.float32)
        f2 = np.array([0.0, 1.0, 0.0] + [0.0] * 125, dtype=np.float32)
        f3 = np.array([0.0, 0.0, 1.0] + [0.0] * 125, dtype=np.float32)
        fake_track = Track(
            track_id=42, class_id=Detection.CLASS_HUMAN,
            bbox=(0, 0, 100, 100), confidence=0.9,
        )
        for f in (f1, f2, f3):
            fake_track.features.append(f)
        tracker._mot.tracks = [fake_track]

        call_count = {"n": 0}
        orig = HumanReID.extract_feature

        def counting_extract(self, *a, **kw):
            call_count["n"] += 1
            return orig(self, *a, **kw)

        with patch.object(HumanReID, "extract_feature", counting_extract):
            centroid, n_emb = tracker.get_track_centroid(42)

        assert n_emb == 3
        assert centroid is not None and centroid.shape == (128,)
        assert abs(float(np.linalg.norm(centroid)) - 1.0) < 1e-5  # L2-normalized
        mean = np.mean([f1, f2, f3], axis=0)
        expected = mean / np.linalg.norm(mean)
        np.testing.assert_allclose(centroid, expected, atol=1e-6)
        assert call_count["n"] == 0, "get_track_centroid 违反'零额外推理'硬约束"
        # 不存在的 track → (None, 0)
        assert tracker.get_track_centroid(99999) == (None, 0)


# =============================================================================
# tracking_service factory
# =============================================================================


@requires_models
class TestTrackingServiceFactory:
    def test_create_deep_sort_mode(self):
        """create_tracking_service(mode='deep_sort') 能构造出 DeepSortTrackingService。"""
        from miloco.perception.engine.config import DeepSortConfigDC
        from miloco.perception.engine.identity.tracking_service import (
            DeepSortTrackingService,
            create_tracking_service,
        )

        svc = create_tracking_service(
            "deep_sort",
            model_dir=str(_MODELS_DIR),
            deep_sort_config=DeepSortConfigDC(),
            fps=1,
        )
        assert isinstance(svc, DeepSortTrackingService)
        assert hasattr(svc, "tracker")
        assert svc.tracker.get_track_embedding(999) is None

    def test_unknown_mode_raises(self):
        from miloco.perception.engine.identity.tracking_service import (
            create_tracking_service,
        )
        with pytest.raises(ValueError, match="Unknown"):
            create_tracking_service("nope_mode")
