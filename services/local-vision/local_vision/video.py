"""视频段落盘与帧采样辅助。

codec-native 通路要求输入是**文件路径**(processor 内部要调 ffprobe/cv-preinfer
抽运动矢量与残差),不能直接吃内存里的帧,所以请求体里的字节必须先落成临时文件。
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# codec 通路的分组下限:少于这个帧数 cv-preinfer 连一组都凑不齐,会直接报错。
# 实测 miloco 默认 4s 感知窗只产 ~4 帧,恰好落在下限之下 —— 所以必须能优雅回退。
MIN_CODEC_FRAMES = 8


def write_temp_video(data: bytes, suffix: str = ".mp4") -> Path:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="lv-seg-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return Path(path)


def probe_frame_count(path: str | Path) -> int:
    """数视频帧数;探测失败返回 -1(调用方据此保守选后端)。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return int(out.stdout.strip().splitlines()[0])
    except Exception as e:  # noqa: BLE001
        logger.warning("ffprobe frame count failed for %s: %s", path, e)
        return -1


def pick_backend(preferred: str, frame_count: int) -> str:
    """段太短时把 codec 降级成 frames,而不是让整次感知失败。"""
    if preferred != "codec":
        return preferred
    if frame_count < 0:
        # 探不出帧数(多半是没有 ffprobe)。codec 通路同样依赖外部工具,这时候
        # 硬走 codec 会每一窗 500;按文档承诺的"保守选择"退到帧采样。
        logger.warning("frame count probe failed; falling back to frames backend")
        return "frames"
    if frame_count < MIN_CODEC_FRAMES:
        logger.info(
            "segment has %d frames (< %d): falling back to frames backend",
            frame_count, MIN_CODEC_FRAMES,
        )
        return "frames"
    return "codec"


def sample_frames(video: str, num_frames: int):
    """均匀抽帧(frames 后端用)。与模型自带 inference.py 的取帧方式保持一致。"""
    import cv2
    import numpy as np
    from PIL import Image

    capture = cv2.VideoCapture(video)
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        capture.release()
        raise ValueError(f"could not read video: {video}")
    indices = np.linspace(0, total - 1, min(num_frames, total), dtype=int)
    frames = []
    for idx in indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = capture.read()
        if not ok:
            capture.release()
            raise ValueError(f"could not decode frame {idx} from: {video}")
        frames.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
    capture.release()
    return frames
