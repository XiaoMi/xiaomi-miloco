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
# 默认路径**是 codec**:miloco 的 4s 窗口按相机原生帧率解码(不做限流),再由
# encode_snapshot_to_h264 抽到 max_frames(默认 32)——远在下限之上。实测在真实
# 部署上连续 246 个窗口全部走 codec。回退是边缘情形(窗口被截短、相机刚上线只出了
# 几帧),不是常态;但它必须存在,否则那几个窗口会整窗失败而不是少一次 token 削减。
MIN_CODEC_FRAMES = 8


#: 启动时清扫多久以前的残留段。请求侧超时 60s,所以这个年龄的文件绝不可能还在飞;
#: 取足够大的值是为了不误删**另一个实例**正在用的段(一机多卡跑两个边车是合理部署)。
_STALE_SEGMENT_AGE_SEC = 3600.0


def sweep_stale_segments(older_than: float = _STALE_SEGMENT_AGE_SEC) -> int:
    """清掉上一次进程遗留的视频段,返回清掉的个数。

    单次请求的清理写在 ``finally`` 里,进程被 SIGKILL / 掉电打断时不会执行,于是
    每被中断一次就在 /tmp 里留下一段几百 KB 的视频 —— 一台长期运行、偶尔重启的
    机器上会慢慢堆起来,而且那是**家里的画面**,不该无限期留在磁盘上。
    """
    import time

    cutoff = time.time() - older_than
    removed = 0
    for p in Path(tempfile.gettempdir()).glob("lv-seg-*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:  # 竞态或权限:跳过即可,清扫是尽力而为
            continue
    if removed:
        logger.info("swept %d stale segment file(s) from previous runs", removed)
    return removed


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
    """均匀抽帧(frames 后端用)。与模型自带 inference.py 的取帧方式保持一致。

    整段裹在 try/finally 里。此前只在**已知的**两处 raise 之前手动 release,
    而 ``cv2.cvtColor`` / ``Image.fromarray`` 同样会抛(通道数不对、尺寸异常),
    那两条路径上 VideoCapture 就泄漏了。这是常驻通路 —— 每窗一次,泄漏会累积到
    耗尽文件描述符,而症状是"跑了几小时之后突然什么都打不开",与这里毫无表面关联。
    """
    import cv2
    import numpy as np
    from PIL import Image

    capture = cv2.VideoCapture(video)
    try:
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            raise ValueError(f"could not read video: {video}")
        indices = np.linspace(0, total - 1, min(num_frames, total), dtype=int)
        frames = []
        for idx in indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
            ok, frame = capture.read()
            if not ok:
                raise ValueError(f"could not decode frame {idx} from: {video}")
            frames.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        return frames
    finally:
        capture.release()
