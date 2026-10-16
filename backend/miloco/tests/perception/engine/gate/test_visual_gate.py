"""Tests for Gate Layer — Visual Gate."""

import numpy as np
from miloco.perception.engine.config import GateConfig
from miloco.perception.engine.gate.visual_gate import (
    _DIFF_SIZE,
    _preprocess,
    compute_frame_diff,
    evaluate_visual,
)


def _solid_frame(r: int, g: int, b: int, w: int = 100, h: int = 100) -> np.ndarray:
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :] = [b, g, r]  # BGR
    return frame


class TestComputeFrameDiff:
    def test_identical_frames_return_zero(self):
        frame = _solid_frame(128, 128, 128)
        assert compute_frame_diff(frame, frame) == 0.0

    def test_completely_different_frames(self):
        black = _solid_frame(0, 0, 0)
        white = _solid_frame(255, 255, 255)
        score = compute_frame_diff(black, white)
        assert score > 0.9

    def test_slight_difference_below_threshold(self):
        gray = _solid_frame(128, 128, 128)
        slightly = _solid_frame(130, 130, 130)
        score = compute_frame_diff(gray, slightly)
        assert score == 0.0  # diff of 2 < pixel_threshold of 25


class TestEvaluateVisual:
    config = GateConfig()

    def test_no_change_with_identical_frames(self):
        frame = _solid_frame(100, 100, 100)
        frames = [frame] * 6
        # 传基准隔离 cold-start(无 prev 时首窗会被强制放行);此处测的是差分本身
        r = evaluate_visual(frames, self.config, prev_frame=_preprocess(frame))
        assert not r.changed
        assert r.max_score == 0.0

    def test_detects_significant_change(self):
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        frames = [gray, gray, white, white, white, white]
        r = evaluate_visual(frames, self.config)
        assert r.changed
        assert r.max_score > 0.5

    def test_cold_start_passes_without_prev_frame(self):
        """无 prev_frame(cold-start):即便画面全静止也判 changed,用于建立基准。"""
        frame = _solid_frame(100, 100, 100)
        r = evaluate_visual([frame] * 6, self.config)  # 不传 prev_frame
        assert r.changed
        assert r.max_score == 0.0  # 纯 cold-start 放行,并非检测到真实变化

    def test_fewer_than_two_frames(self):
        frame = _solid_frame(100, 100, 100)
        # 传基准隔离 cold-start:单帧窗与基准比较 → 相同则无变化
        r = evaluate_visual([frame], self.config, prev_frame=_preprocess(frame))
        assert not r.changed
        assert r.max_score == 0.0


class TestEvaluateVisualPrevFrame:
    """prev_frame（上一窗口末次被检帧，预处理后）参与跨窗口比较。"""

    config = GateConfig()

    def test_change_across_window_boundary_detected(self):
        """窗口内全静止，但与上一窗口基准不同 → 触发。"""
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        prev = evaluate_visual([gray] * 6, self.config).last_checked
        r = evaluate_visual([white] * 6, self.config, prev_frame=prev)
        assert r.changed
        assert r.max_score > 0.5

    def test_static_scene_with_prev_frame_not_triggered(self):
        """与上一窗口基准也相同 → 不触发。"""
        gray = _solid_frame(100, 100, 100)
        prev = evaluate_visual([gray] * 6, self.config).last_checked
        r = evaluate_visual([gray] * 6, self.config, prev_frame=prev)
        assert not r.changed
        assert r.max_score == 0.0

    def test_single_frame_window_compares_against_prev(self):
        """单帧窗口原本无法比较，有 prev_frame 后可与其比较。"""
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        prev = evaluate_visual([gray], self.config).last_checked
        r = evaluate_visual([white], self.config, prev_frame=prev)
        assert r.changed

    def test_empty_frames_with_prev_frame(self):
        """空帧窗口不比较，且返回 None 基准（调用方保留旧基准）。"""
        gray = _solid_frame(100, 100, 100)
        prev = evaluate_visual([gray], self.config).last_checked
        r = evaluate_visual([], self.config, prev_frame=prev)
        assert not r.changed
        assert r.max_score == 0.0
        assert r.last_checked is None

    def test_last_checked_is_preprocessed(self):
        """返回的基准是预处理结果（448 灰度），存它而非原始帧。"""
        gray = _solid_frame(100, 100, 100)
        last = evaluate_visual([gray] * 6, self.config).last_checked
        assert last.shape == _DIFF_SIZE
        assert last.ndim == 2

    def test_window_tail_change_caught_via_next_window(self):
        """fps=3 抽检 [0,3,6] 时，f6 之后的变化本窗口漏检；
        基准取末检帧 f6 而非末帧 f8，下窗口跨窗口比较必然补获。"""
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        frames_n = [gray] * 7 + [white] * 2  # 变化发生在末次抽检点之后
        # 传基准隔离 cold-start,才能验证"本窗抽检漏检"这个采样限制
        r_n = evaluate_visual(frames_n, self.config, input_fps=3, prev_frame=_preprocess(gray))
        assert not r_n.changed  # 本窗口抽检帧全灰，漏检（已知采样限制）
        r_n1 = evaluate_visual(
            [white] * 9, self.config, input_fps=3, prev_frame=r_n.last_checked,
        )
        assert r_n1.changed  # 基准 = f6（灰）↔ 下窗口 f0（白）→ 补获

    def test_empty_frame_in_window_skipped(self):
        """窗口内混入空帧（解码异常）→ 跳过不参与比较，不抛错。"""
        gray = _solid_frame(100, 100, 100)
        empty = np.empty((0, 0, 3), dtype=np.uint8)
        r = evaluate_visual([gray, empty, gray], self.config, prev_frame=_preprocess(gray))
        assert not r.changed
        assert r.max_score == 0.0
        assert r.checked_frame_count == 2
        assert r.last_checked is not None

    def test_all_empty_frames_returns_none_baseline(self):
        empty = np.empty((0, 0, 3), dtype=np.uint8)
        r = evaluate_visual([empty] * 3, self.config)
        assert not r.changed
        assert r.max_score == 0.0
        assert r.checked_frame_count == 0
        assert r.last_checked is None

    def test_last_checked_equals_last_check_index_frame(self):
        """基准必须是末次被检帧（f6）而非窗口末帧（f8）。"""
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        frames = [gray] * 7 + [white] * 2  # 抽检 [0,3,6] → 末检帧是 f6（灰）
        last = evaluate_visual(frames, self.config, input_fps=3).last_checked
        assert np.array_equal(last, _preprocess(gray))

    def test_intra_and_cross_scores_separated(self):
        """B 方案诊断字段:跨窗 vs 窗内 score 应分别可读。"""
        gray = _solid_frame(100, 100, 100)
        white = _solid_frame(255, 255, 255)
        # 上窗末帧白 → 本窗全灰:cross 大,intra 0
        prev = evaluate_visual([white] * 6, self.config).last_checked
        r = evaluate_visual([gray] * 6, self.config, prev_frame=prev)
        assert r.cross_max > 0.5
        assert r.intra_max == 0.0
        assert r.max_score == r.cross_max

        # 无 prev:cross 必 0
        r2 = evaluate_visual([gray, gray, white, white], self.config)
        assert r2.cross_max == 0.0
        assert r2.intra_max > 0.5


class TestInterAreaDownsampling:
    """INTER_AREA averages source-pixel regions; sparse high-frequency noise
    that survives INTER_LINEAR should be smoothed away."""

    def test_sparse_noise_score_is_low(self):
        """1% 随机散布噪点，INTER_AREA 降采样后应远低于阈值。"""
        rng = np.random.default_rng(42)
        h, w = 1080, 1920  # 1080p source
        clean = np.full((h, w, 3), 128, dtype=np.uint8)
        noisy = clean.copy()
        n_noise = int(h * w * 0.01)
        ys = rng.integers(0, h, n_noise)
        xs = rng.integers(0, w, n_noise)
        noisy[ys, xs] = 255  # 散布的白噪点

        score = compute_frame_diff(clean, noisy)
        # INTER_AREA 把散布噪点平均掉，最终 448×448 上的变化应远低于 0.5% 阈值
        assert score < 0.005, f"INTER_AREA 未抗住散布噪声: {score}"

    def test_large_object_change_preserved(self):
        """大块连通变化应被 INTER_AREA 保留。"""
        h, w = 1080, 1920
        clean = np.full((h, w, 3), 128, dtype=np.uint8)
        with_block = clean.copy()
        # 100×100 的大块变化（模拟近距离人体局部移动）
        with_block[400:500, 800:900] = 255

        score = compute_frame_diff(clean, with_block)
        # 100×100 / 1920×1080 ≈ 0.48%；降采样后应仍能反映出来
        assert score > 0.001, f"大块变化未被保留: {score}"
