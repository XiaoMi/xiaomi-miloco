"""把感知窗口的帧编码成一段 H.264 视频,喂给本地视觉边车。

为什么必须编码成视频而不是发帧:本地通路的价值核心是 **codec-native** —— 边车
侧的模型直接消费 H.264 的运动矢量与残差,而不是解码后的稠密像素。实测同一段
家庭画面,codec 通路的视觉 token 比均匀帧采样少约 90%、端到端快 3 倍多。要拿到
这个收益,送过去的就必须是**编码过的码流**。

用 PyAV 而非 ffmpeg 子进程:av 已是本项目依赖(相机链路本来就在用),进程内编码
省掉一次落盘与 fork。
"""

from __future__ import annotations

import io
import logging

from miloco.perception.types import DeviceSnapshot

logger = logging.getLogger(__name__)

# 边车侧另有 MIN_CODEC_FRAMES=8 决定走不走 codec;这里只保证"至少有一帧可编码"。低于此帧数边车会自动降级到帧采样后端,
# 这里不拦 —— 少量帧仍能出可用的场景描述,只是拿不到 codec 的省 token 收益。
_MIN_USEFUL_FRAMES = 1


class EncodeError(RuntimeError):
    """帧序列无法编码成视频(空窗口 / 尺寸异常 / 编码器不可用)。"""


def encode_snapshot_to_h264(
    snapshot: DeviceSnapshot,
    fps: int = 4,
    crf: int = 28,
    max_frames: int = 32,
    short_edge: int = 512,
) -> bytes:
    """把一台设备本窗口的视频帧编码成 mp4(H.264)字节。

    fps 只影响容器时基,不改变送进模型的帧内容;取小值让同样帧数覆盖更长的
    时间跨度,更贴合「一段监控」的语义。crf 偏高(画质换体积)是刻意的 ——
    边车最终只在 16x16 patch 粒度上看运动/残差,过高画质纯属浪费带宽。
    max_frames / short_edge 是**载荷预算**:云端通路在送模型前会两次降采样并限
    短边,本通路必须有对应的约束,否则一台 2K 相机的一个窗口会把上百帧原生画面
    base64 进一个 JSON body。
    """
    frames = list(snapshot.video.frames) if snapshot.has_video else []
    if len(frames) < _MIN_USEFUL_FRAMES:
        raise EncodeError(f"snapshot has {len(frames)} video frames, nothing to encode")

    # 抽帧到预算之内。不设上限时一个窗口可能有上百帧原生分辨率画面,base64 塞进
    # 一个 JSON body 里发走 —— 而边车侧无论如何只会用到 num_frames 张。均匀抽,
    # 保住时间跨度。
    if max_frames > 0 and len(frames) > max_frames:
        # 端点包含式均匀采样:必须取到最后一帧。用 len/max 步长的 floor 采样永远
        # 落不到 n-1,等于把窗口最末尾(最可能含事件的那段)整段丢掉,而
        # end_timestamp 还宣称覆盖了整个窗口。
        n = len(frames)
        if max_frames == 1:
            frames = [frames[-1]]  # 只要一帧就要最新那帧,不是最旧的
        else:
            frames = [
                frames[round(i * (n - 1) / (max_frames - 1))] for i in range(max_frames)
            ]

    container = None
    try:
        import av
        import numpy as np

        h, w = frames[0].data.shape[:2]
        if h <= 0 or w <= 0:
            raise EncodeError(f"invalid frame size {w}x{h}")
        # 按短边缩放:模型只在 16x16 patch 粒度上看运动/残差,原生 2K 纯属浪费
        # 带宽与编码时间。与云端通路的 video_short_edge 是同一个取舍。
        scale = 1.0
        if short_edge > 0 and min(w, h) > short_edge:
            scale = short_edge / float(min(w, h))
            w, h = int(w * scale), int(h * scale)
        # H.264 要求偶数边长;监控源偶尔给奇数分辨率,这里向下取偶避免编码器报错。
        w -= w % 2
        h -= h % 2
        if w <= 0 or h <= 0:
            raise EncodeError("frame too small after scaling")

        buf = io.BytesIO()
        container = av.open(buf, mode="w", format="mp4")
        stream = container.add_stream("libx264", rate=fps)
        stream.width = w
        stream.height = h
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": str(crf), "preset": "veryfast", "tune": "zerolatency"}

        for f in frames:
            arr = f.data
            if arr.ndim != 3 or arr.shape[2] != 3:
                continue
            if scale != 1.0 or arr.shape[0] != h or arr.shape[1] != w:
                arr = _resize_bgr(arr, w, h)
            vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(arr), format="bgr24")
            for packet in stream.encode(vf):
                container.mux(packet)
        for packet in stream.encode():  # flush
            container.mux(packet)
    except EncodeError:
        raise
    except Exception as e:  # noqa: BLE001 —— 含 av 缺失 / 无 libx264 等构建问题
        # 必须收敛成 EncodeError:调用方只捕获它,漏出去的原始异常会穿透
        # per-device 降级、让整轮感知失败。
        raise EncodeError(f"h264 encode failed: {type(e).__name__}: {e}") from e
    finally:
        if container is not None:
            container.close()

    data = buf.getvalue()
    if not data:
        raise EncodeError("encoder produced no output")
    return data


def _resize_bgr(arr, w: int, h: int):
    """缩放到 (w, h)。cv2 已是感知链路的既有依赖。"""
    import cv2

    return cv2.resize(arr, (w, h), interpolation=cv2.INTER_AREA)
