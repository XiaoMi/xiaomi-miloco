"""Gate Layer — Visual Gate (frame differencing)."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.config import GateConfig


@dataclass(frozen=True)
class VisualEvalResult:
    """visual gate 评估输出。

    intra_max / cross_max 分开返回供诊断:跨窗对的时间差 ≈ window_duration,
    远大于窗内邻帧;若 cross_max 持续 >> intra_max,说明 ISP 长周期漂移
    (AGC/IR/AWB)主导,而非真实运动。
    """
    changed: bool
    max_score: float
    intra_max: float
    cross_max: float
    checked_frame_count: int
    last_checked: NDArray[np.uint8] | None


def evaluate_visual(
    frames: list[NDArray[np.uint8]],
    config: GateConfig,
    input_fps: int = 1,
    prev_frame: NDArray[np.uint8] | None = None,
) -> VisualEvalResult:
    """Compare frames using grayscale pixel differencing.

    prev_frame: 上一窗口的比较基准（即上次调用返回的 last_checked，已预处理，
    仅参与比较，不进 packet）——把跨窗口边界的变化也纳入检测，避免"变化恰好
    发生在两个窗口之间"被漏掉。

    last_checked 是本窗口末次被检帧的预处理结果，调用方存下来作为
    下个窗口的 prev_frame。基准取"末次被检帧"而非"窗口末帧"。
    frames 为空时为 None（调用方应保留旧基准）。
    """
    if not frames:
        return VisualEvalResult(False, 0.0, 0.0, 0.0, 0, None)

    # Select frames at check_fps intervals based on actual input fps
    interval = max(1, round(input_fps / config.check_fps))
    check_indices = list(range(0, len(frames), interval))
    if len(check_indices) < 2 and len(frames) >= 2:
        check_indices.append(len(frames) - 1)

    # 每帧只预处理一次;跳过空帧(解码异常兜底,cv2.resize 对空数组会抛错)
    processed = [_preprocess(frames[i]) for i in check_indices if frames[i].size > 0]
    if not processed:
        return VisualEvalResult(False, 0.0, 0.0, 0.0, 0, None)
    last_checked = processed[-1]

    # cross_max: 上窗末帧 vs 本窗首帧的单一 score(仅 prev_frame 存在时)
    # intra_max: 本窗内邻帧对的 max
    cross_max = (
        _diff_processed(prev_frame, processed[0]) if prev_frame is not None else 0.0
    )
    intra_max = 0.0
    for gray_a, gray_b in zip(processed, processed[1:]):
        intra_max = max(intra_max, _diff_processed(gray_a, gray_b))

    max_score = max(cross_max, intra_max)
    # cold-start:无跨窗基准(prev_frame is None)时静止画面 cross=0/intra≈0 会被当无变化丢掉,
    # 导致开机已存在的场景永不被感知。把首个有基准帧的窗视作变化放行以建立基准(空帧窗已在
    # 上面提前 return,不会走到这里)。prev_frame 流式来自 gate_prev_frames dict,故每 device
    # 仅首窗触发、随 dict reset 自动复位。
    changed = bool(prev_frame is None or max_score >= config.change_threshold)
    return VisualEvalResult(
        changed=changed,
        max_score=max_score,
        intra_max=intra_max,
        cross_max=cross_max,
        checked_frame_count=len(processed),
        last_checked=last_checked,
    )


_DIFF_SIZE = (448, 448)


def _preprocess(frame: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Downscale to 448x448 grayscale for differencing.

    Downscale first (cheaper than cvtColor on 4K). INTER_AREA averages
    source-pixel regions, suppressing high-frequency aliasing/noise that
    INTER_LINEAR would propagate into the diff.
    """
    small = cv2.resize(frame, _DIFF_SIZE, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small


def _diff_processed(gray_a: NDArray[np.uint8], gray_b: NDArray[np.uint8], pixel_threshold: int = 25) -> float:
    """Ratio of changed pixels between two preprocessed (448x448 gray) frames."""
    diff = cv2.absdiff(gray_a, gray_b)
    changed_pixels = np.count_nonzero(diff > pixel_threshold)
    return changed_pixels / diff.size


def compute_frame_diff(frame_a: NDArray[np.uint8], frame_b: NDArray[np.uint8], pixel_threshold: int = 25) -> float:
    """Compute ratio of changed pixels between two raw frames.

    Frames are resized to 448x448 before comparison to avoid
    unnecessary computation on high-resolution inputs.
    """
    if frame_a.size == 0 or frame_b.size == 0:
        return 0.0
    return _diff_processed(_preprocess(frame_a), _preprocess(frame_b), pixel_threshold)
