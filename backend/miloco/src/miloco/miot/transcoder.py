# Copyright (C) 2026 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Per-camera live H.264 encoder.

Wraps PyAV's ``libx264`` codec context with `ultrafast + zerolatency` settings
suitable for live transcode of decoded BGR frames into H.264 Annex-B NAL bytes
that browsers can decode without HEVC support.

Encoder runs on a dedicated single-thread executor; callers ``await`` the
async ``encode()`` so the asyncio event loop is never blocked by libx264.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from typing import Optional

import av
import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)


class H264LiveEncoder:
    """libx264 wrapper for per-camera live transcode.

    First call to :meth:`encode` lazy-initialises the encoder using the actual
    BGR frame's resolution. If resolution changes mid-stream, the encoder is
    rebuilt (causing one IDR boundary).
    """

    def __init__(self, gop: int = 30):
        self._gop = gop
        self._codec: Optional[av.codec.CodecContext] = None
        self._width = 0
        self._height = 0
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="h264-enc"
        )
        self._closed = False
        # 自维护单调 PTS 计数器,不信摄像头侧 PTS——SDK 把它读成 c_uint64,摄像头在
        # "PTS 未知"时(典型:PPCS 重连后头 ~30 帧)发哨兵值 0xFFFFFFFFFFFFFFFF。PyAV 17
        # 的 frame.pts setter 是 signed int64,哨兵值会抛 OverflowError 导致整帧被丢、
        # 重连窗口内连刷几十条 error + 画面卡顿。跟 NalClipRecorder(ws.py)同款做法。
        self._pts_counter = 0

    def _open_encoder(self, width: int, height: int) -> None:
        codec = av.codec.CodecContext.create("libx264", "w")
        codec.width = width
        codec.height = height
        codec.pix_fmt = "yuv420p"
        codec.time_base = Fraction(1, 1000)  # pts unit: milliseconds
        # Setting framerate explicitly is critical — without it libx264
        # infers it from time_base=1/1000 (= 1000fps!) and selects an
        # absurdly high H.264 level (6.1+) that browsers' hardware decoders
        # reject. 30fps comfortably covers typical IPC native rates
        # (15–30); a small mismatch with the actual feed doesn't hurt —
        # level only depends on max-fps × resolution, not on per-packet
        # arrival rate.
        codec.framerate = Fraction(30, 1)
        codec.gop_size = self._gop
        codec.max_b_frames = 0
        # x264-params:
        #   keyint / min-keyint    fixed GOP boundary (no early IDR)
        #   scenecut=0             disable scene-change-triggered IDR
        #   bframes=0              redundant with max_b_frames=0 but explicit
        #   repeat-headers=1       prepend SPS/PPS to every IDR — critical so
        #                          late-joining browsers can configure on any
        #                          IDR boundary without server-side caching
        #   level=4.0              hard-cap the declared H.264 level. 4.0
        #                          covers up to 1080p@30 / 720p@60 — fits
        #                          IPC streams comfortably and is universally
        #                          supported by browser/hardware decoders
        # threads=1 + sliced-threads=0 forces libx264 to emit one NAL slice
        # per frame. The default ultrafast preset on multi-core hosts uses
        # slice-based threading (multiple IDR slices per access unit) which
        # browsers' hardware H.264 decoders — particularly Chrome on Linux —
        # silently reject with OperationError, even though the codec string
        # is technically valid. We have our own per-camera encoder thread
        # already, so libx264 internal threading buys us nothing.
        codec.thread_count = 1
        codec.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "x264-params": (
                f"keyint={self._gop}:min-keyint={self._gop}:"
                "scenecut=0:bframes=0:repeat-headers=1:level=4.0:"
                "slices=1:sliced-threads=0"
            ),
        }
        codec.open()
        self._codec = codec
        self._width = width
        self._height = height
        # 重建编码器(分辨率切换)= libx264 重新初始化,PTS 必须从 0 重起,
        # 否则跟新编码器的 SPS/PPS 时间基不一致。
        self._pts_counter = 0
        logger.info(
            "H264 encoder opened %dx%d gop=%d preset=ultrafast tune=zerolatency",
            width, height, self._gop,
        )

    def _encode_sync(
        self, bgr: NDArray[np.uint8], pts_ms: int
    ) -> list[tuple[bytes, bool]]:
        """Encode one BGR frame, return list of (annexb_bytes, is_keyframe)."""
        if self._closed:
            return []
        h, w = bgr.shape[:2]
        if self._codec is None:
            self._open_encoder(w, h)
        elif w != self._width or h != self._height:
            logger.warning(
                "Resolution changed %dx%d → %dx%d, rebuilding encoder",
                self._width, self._height, w, h,
            )
            try:
                # Flush old encoder (drain remaining packets). PyAV 17.x removed
                # VideoCodecContext.close() — draining via encode(None) then
                # dropping the reference (self._codec = None below) lets GC free
                # the underlying context. Calling .close() here used to raise
                # AttributeError every rebuild and spam the log.
                for _ in self._codec.encode(None):
                    pass
            except Exception as e:
                logger.warning("flush old encoder failed: %s", e)
            self._codec = None
            self._open_encoder(w, h)
        # Capture codec ref locally FIRST so a single read is checked and used —
        # close() setting self._codec = None on the main thread can't slip in
        # between the check and the encode.
        codec = self._codec
        if codec is None:
            return []

        frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
        frame = frame.reformat(format="yuv420p")
        # 不用摄像头侧 pts_ms(见 __init__ 注释:哨兵值会撑爆 int64 setter)。本地计数器
        # × 33ms 贴合 codec.time_base=1/1000 + framerate=30(每帧约进 33ms)。这个 PTS
        # **只供 libx264 内部码率核算,不进 wire、不影响浏览器播放**——浏览器的播放时序来自
        # ws 帧头里的相机原始 ts(watch.html 从 16 字节头的第 8-16 字节读 ts 算 jmuxer
        # duration / WebCodecs timestamp)。libx264 只要 PTS 严格单调即可,本计数器满足。pts_ms 入参
        # 保留(不改 ws.py 调用契约)但此路不再使用。
        frame.pts = self._pts_counter * 33
        self._pts_counter += 1

        out: list[tuple[bytes, bool]] = []
        for packet in codec.encode(frame):
            out.append((bytes(packet), bool(packet.is_keyframe)))
        return out

    async def encode(
        self, bgr: NDArray[np.uint8], pts_ms: int
    ) -> list[tuple[bytes, bool]]:
        """Async wrapper around :meth:`_encode_sync` (runs in encoder thread)."""
        if self._closed:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._encode_sync, bgr, pts_ms
        )

    async def close(self) -> None:
        """Flush remaining packets and shut the encoder thread down.

        The drain (``encode(None)``) runs inside the encoder thread — PyAV codec
        contexts are not safe to touch from multiple threads, even sequentially,
        so we keep all access on the same worker. PyAV 17.x removed
        ``VideoCodecContext.close()``; after draining we just drop the ref and
        let GC free the context (no explicit close call anymore).
        """
        if self._closed:
            return
        self._closed = True
        codec = self._codec
        self._codec = None  # block any concurrent _encode_sync immediately
        loop = asyncio.get_running_loop()
        if codec is not None:
            def _drain() -> None:
                # PyAV 17.x removed VideoCodecContext.close(); draining with
                # encode(None) flushes buffered packets, then the local `codec`
                # ref falls out of scope and GC frees the context. The old
                # codec.close() raised AttributeError on every teardown, which
                # (combined with shutdown(wait=False) skipping the join) leaked
                # one codec context + encoder thread per camera toggle-off.
                for _ in codec.encode(None):
                    pass
            try:
                await loop.run_in_executor(self._executor, _drain)
            except Exception as e:
                logger.warning("flush encoder failed: %s", e)
        # wait=True 真 join 编码线程再返回,否则快速 toggle off/on 会攒孤儿线程。
        # join 是同步阻塞,放到**默认** executor 跑(不能提交给 self._executor——它正要
        # 被 shutdown,提交会 RuntimeError),从而不阻塞事件循环。实测单 worker + _drain
        # 已 await 完成时 join 近乎瞬时,但不在 loop 线程上同步 join 更稳。注:默认池是
        # 全进程共享的(min(32,cpu+4) workers),这里 join 瞬时不会长占它——别在此处塞
        # 真正长阻塞的 join,否则会拖累其它 run_in_executor(None,...) 调用方。
        await loop.run_in_executor(None, lambda: self._executor.shutdown(wait=True))
