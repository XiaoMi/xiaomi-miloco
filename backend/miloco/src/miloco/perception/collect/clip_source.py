"""固定输入源（本地视频 clip）——没有摄像头时也能跑通整个感知管线。

配置 ``perception.engine.input.clip_source = "/path/to/clip.mp4"`` 后：

- 无论是否有摄像头在线，collect 层都以该视频作为**唯一**的输入画面（替换摄像头画面，
  语义等同"默认至少有一路摄像头在线"）。
- 每个感知窗口把整段 clip 的帧作为输入；窗口之间整体旋转 1 帧——模型看到的始终是
  这段视频，且相邻窗口画面不同（clip 内有运动时视觉 gate 正常放行，不会被静止跳过）。
- 无摄像头、无端侧模型（rule_only 模式）的环境也能本地测试
  collect → gate → omni → 规则判定的整条链路。

配置方式：CLI ``miloco-cli config set perception.engine.input.clip_source <path>``
或 config.json；热读，下个感知窗口生效（直接改 settings.yaml 需重启）。置空 = 关闭，
恢复摄像头输入。

解码结果按路径缓存（只解一次），窗口取帧走旋转游标。
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)

# 虚拟设备标识（出现在感知日志 / 规则派发 / 前端设备列表里）
DID = "clip-source"
ROOM = "本地测试"

_DEFAULT_FPS = 3
_DEFAULT_PERIOD_SEC = 4
# 解码帧数上限：防超长视频一次性烧 token / 拖慢编码。按需调大。
_MAX_FRAMES = 240

_lock = threading.Lock()
_cache: dict[str, list[NDArray[np.uint8]]] = {}
_cursor: dict[str, int] = {}


def clip_source_path() -> str:
    """热读 ``perception.engine.input.clip_source``；空 / 读取失败返回 ""（关闭）。"""
    try:
        from miloco.config import get_settings

        val = get_settings().perception.engine.get("input", {}).get("clip_source", "")
        return str(val or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def clip_source_device():
    """clip_source 已配置时返回虚拟设备元信息（不解码视频）；未配置返回 None。

    供 ``MultimodalCollector.get_all_active_sources`` 追加——runner 的 tick 循环据此
    判定"有源可跑"（无摄像头时感知循环也会继续向下跑），web 引擎状态里也能看到这一路源。
    """
    if not clip_source_path():
        return None
    from miloco.perception.types import PerceptionDevice

    return PerceptionDevice(
        did=DID,
        name="本地视频源(clip)",
        device_type="camera",
        room_name=ROOM,
    )


def _decode_clip(path: str) -> list[NDArray[np.uint8]]:
    """用 cv2 解码 clip 为 BGR 帧列表（上限 _MAX_FRAMES）。"""
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"clip_source 视频无法打开: {path}")
    frames: list[NDArray[np.uint8]] = []
    while len(frames) < _MAX_FRAMES:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    if not frames:
        raise RuntimeError(f"clip_source 视频没有可解码帧: {path}")
    if len(frames) >= _MAX_FRAMES:
        logger.warning(
            "event=clip_source_truncated path=%s frames>=%d 仅取前 %d 帧",
            path, _MAX_FRAMES, _MAX_FRAMES,
        )
    logger.info("event=clip_source_decoded path=%s frames=%d", path, len(frames))
    return frames


def take_window_frames(path: str) -> list[NDArray[np.uint8]]:
    """取一个窗口的全部 clip 帧；窗口之间整体旋转 1 帧（相邻窗口画面不同，gate 放行）。"""
    with _lock:
        frames = _cache.get(path)
        if frames is None:
            frames = _decode_clip(path)
            _cache[path] = frames
            _cursor[path] = 0
        n = len(frames)
        cur = _cursor[path] % n
        out = [frames[(cur + i) % n] for i in range(n)]
        _cursor[path] = cur + 1
        return out


def build_clip_device_data(path: str):
    """把 clip 当前窗口帧组装成 DeviceData（无音频），供 collect_batch 直接使用。

    解码失败 / 无帧时返回 None（上层告警并空窗，不阻断）。
    """
    from miloco.config import get_settings
    from miloco.perception.schema import DecodedVideoFrame, DeviceData

    try:
        frames = take_window_frames(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("event=clip_source_unavailable path=%s err=%s", path, exc)
        return None

    inp = get_settings().perception.engine.get("input", {})
    fps = int(inp.get("fps", _DEFAULT_FPS)) or 1
    period_sec = int(inp.get("period_sec", _DEFAULT_PERIOD_SEC)) or 1

    window_end_ms = int(time.time() * 1000)
    window_start_ms = window_end_ms - period_sec * 1000
    step_ms = max(1, int(1000 / fps))

    device = clip_source_device()
    video = [
        DecodedVideoFrame(
            frame=f,
            stream_ts=i * step_ms,
            wall_ms=window_start_ms + i * step_ms,
            unix_ms=window_start_ms + i * step_ms,
            recv_unix_ms=window_start_ms + i * step_ms,
        )
        for i, f in enumerate(frames)
    ]
    return DeviceData(
        meta=device,
        video=video,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        window_start_unix_ms=window_start_ms,
        window_end_unix_ms=window_end_ms,
    )


def reset_cache() -> None:
    """清空解码缓存与游标（测试 / 配置改路径后由上层决定是否调用）。"""
    with _lock:
        _cache.clear()
        _cursor.clear()
