# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT Decoder.
"""

import asyncio
import logging
import subprocess
import threading
import time
from collections import deque
from io import BytesIO
from typing import Callable, Coroutine, List, Optional

from av.audio.codeccontext import AudioCodecContext
from av.audio.frame import AudioFrame
from av.audio.resampler import AudioResampler
from av.codec import CodecContext
from av.packet import Packet
from av.video.codeccontext import VideoCodecContext
from av.video.frame import VideoFrame
from PIL import Image

from .error import MIoTMediaDecoderError
from .types import MIoTCameraCodec, MIoTCameraFrameData

_LOGGER = logging.getLogger(__name__)


# H.264 NAL unit types that contain an IDR coded slice.
_H264_IDR_NAL_TYPE = 5
# H.265 NAL unit types that mark a random-access point (IDR / CRA / BLA);
# any of these starts a new GOP, so we treat them all as "key" for the
# purpose of buffer-overflow eviction.
_H265_IRAP_NAL_TYPES = frozenset({16, 17, 18, 19, 20, 21})


def _is_key_access_unit(item: MIoTCameraFrameData) -> bool:
    """Return True if this access unit contains a key (IDR/IRAP) slice.

    Inspects the Annex-B byte stream directly rather than trusting
    ``item.frame_type`` — the SDK wrapper has historically dropped that
    field for some codec_id paths, but the actual NAL unit type lives in
    the bitstream itself per the H.264 / H.265 spec and is always
    accurate. Walking the first few NAL headers per frame is cheap.
    """
    data: bytes = item.data
    n = len(data)
    is_h264 = item.codec_id == MIoTCameraCodec.VIDEO_H264
    is_h265 = item.codec_id == MIoTCameraCodec.VIDEO_H265
    if not (is_h264 or is_h265):
        return False
    i = 0
    while i + 3 < n:
        # Match Annex-B start code (3- or 4-byte).
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 0 and data[i + 3] == 1:
            sc_len = 4
        elif data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            sc_len = 3
        else:
            i += 1
            continue
        # Truncated frame: start code matched at the tail with no NAL header
        # byte following. Stop scanning rather than IndexError.
        if i + sc_len >= n:
            break
        nal_byte = data[i + sc_len]
        if is_h264:
            if (nal_byte & 0x1F) == _H264_IDR_NAL_TYPE:
                return True
        else:  # is_h265
            if ((nal_byte >> 1) & 0x3F) in _H265_IRAP_NAL_TYPES:
                return True
        i += sc_len
    return False


class MIoTMediaRingBuffer:
    """Ring buffer."""

    _maxlen: int
    _video_buffer: deque[MIoTCameraFrameData]
    _audio_buffer: deque[MIoTCameraFrameData]
    _cond: threading.Condition

    def __init__(self, maxlen: int = 20):
        self._maxlen = maxlen
        self._video_buffer = deque(maxlen=maxlen)
        self._audio_buffer = deque(maxlen=maxlen)
        self._cond = threading.Condition()

    def put_video(self, item: MIoTCameraFrameData) -> None:
        with self._cond:
            # When the queue is full, prefer dropping a non-key frame so the
            # downstream PyAV decoder doesn't lose a reference frame.
            if len(self._video_buffer) >= self._maxlen:
                if _is_key_access_unit(item):
                    removed: bool = False
                    for i in range(len(self._video_buffer)):
                        if not _is_key_access_unit(self._video_buffer[i]):
                            del self._video_buffer[i]
                            removed = True
                            break
                    if not removed:
                        self._video_buffer.popleft()
                    self._video_buffer.append(item)
                    self._cond.notify()
                else:
                    # Drop non-key incoming frame
                    pass
                _LOGGER.info(
                    "drop non-key frame, %s, %s", item.codec_id, item.timestamp
                )
            else:
                self._video_buffer.append(item)
                self._cond.notify()

    def put_audio(self, item: MIoTCameraFrameData) -> None:
        with self._cond:
            self._audio_buffer.append(item)
            self._cond.notify()

    def step(
        self,
        on_video_frame: Callable[[MIoTCameraFrameData], None],
        on_audio_frame: Callable[[MIoTCameraFrameData], None],
        timeout: float = 0.2,
    ) -> None:
        on_frame: Callable[[MIoTCameraFrameData], None] = on_video_frame
        frame_data: Optional[MIoTCameraFrameData] = None
        # get frame
        with self._cond:
            if self._video_buffer:
                frame_data = self._video_buffer.popleft()
            elif self._audio_buffer:
                frame_data = self._audio_buffer.popleft()
                on_frame = on_audio_frame
            else:
                self._cond.wait(timeout=timeout)
        # handle frame
        if frame_data:
            on_frame(frame_data)

    def stop(self):
        del self._cond
        self._video_buffer.clear()
        self._audio_buffer.clear()


class MIoTMediaDecoder(threading.Thread):
    """MIoT Decoder."""

    _main_loop: asyncio.AbstractEventLoop
    _running: bool
    _frame_interval: int
    _enable_hw_accel: bool
    _enable_audio: bool

    # format: did, data, ts, channel
    _video_callback: Callable[[bytes, int, int], Coroutine]
    # format: did, data, ts, channel
    _audio_callback: Callable[[bytes, int, int], Coroutine]
    # format: video_frame, ts, channel, recv_unix_ms, decoded_unix_ms
    # recv_unix_ms: host unix ms when the raw encoded frame first arrived
    # decoded_unix_ms: host unix ms right after the decoder emitted the frame
    # (so decode latency = decoded_unix_ms - recv_unix_ms)
    _video_frame_callback: Optional[
        Callable[[VideoFrame, int, int, int, int], Coroutine]
    ]
    # format: audio_frame, ts, channel, recv_unix_ms, decoded_unix_ms
    _audio_frame_callback: Optional[
        Callable[[AudioFrame, int, int, int, int], Coroutine]
    ]

    _queue: MIoTMediaRingBuffer
    _video_decoder: Optional[CodecContext]
    _audio_decoder: Optional[CodecContext]
    _resampler: AudioResampler

    _current_jpg_width: int
    _current_jpg_height: int
    _last_jpeg_ts: int

    def __init__(
        self,
        frame_interval: int,
        video_callback: Callable[[bytes, int, int], Coroutine],
        audio_callback: Optional[Callable[[bytes, int, int], Coroutine]] = None,
        video_frame_callback: Optional[
            Callable[[VideoFrame, int, int, int, int], Coroutine]
        ] = None,
        audio_frame_callback: Optional[
            Callable[[AudioFrame, int, int, int, int], Coroutine]
        ] = None,
        enable_hw_accel: bool = False,
        enable_audio: bool = False,
        main_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__()
        self._main_loop = main_loop or asyncio.get_running_loop()
        self._running = False
        self._frame_interval = frame_interval
        self._enable_hw_accel = enable_hw_accel
        self._enable_audio = enable_audio

        self._video_callback = video_callback
        self._video_frame_callback = video_frame_callback
        self._audio_frame_callback = audio_frame_callback
        if enable_audio:
            if not audio_callback:
                raise MIoTMediaDecoderError(
                    "audio_callback is required when enable audio"
                )
            else:
                self._audio_callback = audio_callback

        self._queue = MIoTMediaRingBuffer()
        self._video_decoder = None
        self._audio_decoder = None
        self._resampler = None  # type: ignore

        self._last_jpeg_ts = 0

    def run(self) -> None:
        """Start the decoder."""
        self._running = True
        while self._running:
            try:
                self._queue.step(
                    on_video_frame=self._on_video_callback,
                    on_audio_frame=self._on_audio_callback,
                )
            except Exception as e:
                _LOGGER.error("frame data handle error, %s", e)
                if self._main_loop.is_closed():
                    break
        _LOGGER.info("decoder stopped")

    def stop(self) -> None:
        """Stop the decoder."""
        self._running = False
        self._queue.stop()
        self._video_decoder = None
        self._audio_decoder = None
        self.join()

    def push_video_frame(self, frame_data: MIoTCameraFrameData) -> None:
        self._queue.put_video(frame_data)

    def push_audio_frame(self, frame_data: MIoTCameraFrameData) -> None:
        self._queue.put_audio(frame_data)

    def detect_hwaccel(self):
        try:
            result = subprocess.run(
                ["ffmpeg", "-hwaccels"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            hw_list = result.stdout.strip().split("\n")[1:]
            return hw_list
        except FileNotFoundError:
            return []

    def choose_hw_decoder(self, codec_name, hw_methods):
        if codec_name in ("h264", "hevc"):
            if f"{codec_name}_v4l2m2m" in hw_methods:
                return f"{codec_name}_v4l2m2m"
        return codec_name

    def _on_video_callback(self, frame_data: MIoTCameraFrameData) -> None:
        if not self._video_decoder:
            # Create video decoder
            if frame_data.codec_id == MIoTCameraCodec.VIDEO_H264:
                self._video_decoder = VideoCodecContext.create("h264", "r")
            elif frame_data.codec_id == MIoTCameraCodec.VIDEO_H265:
                self._video_decoder = VideoCodecContext.create("hevc", "r")
            else:
                _LOGGER.error("unsupported video codec: %s, skipping frame", frame_data.codec_id)
                return
            _LOGGER.info("video decoder created, %s", frame_data.codec_id)
        pkt = Packet(frame_data.data)
        frames: List[VideoFrame] = self._video_decoder.decode(pkt)  # type: ignore
        decoded_unix_ms = int(time.time() * 1000)
        # Emit decoded frames as BGR numpy arrays (no rate limiting).
        # Converting to ndarray HERE in the decoder thread avoids cross-thread
        # FFmpeg access — the main thread only ever sees numpy data.
        if self._video_frame_callback and frames:
            for frame in frames:
                try:
                    bgr = frame.to_ndarray(format="bgr24").astype("uint8")
                except Exception as e:
                    _LOGGER.warning("Failed to convert frame to ndarray: %s", e)
                    continue
                self._main_loop.call_soon_threadsafe(
                    self._main_loop.create_task,
                    self._video_frame_callback(
                        bgr,
                        frame_data.timestamp,
                        frame_data.channel,
                        frame_data.recv_unix_ms,
                        decoded_unix_ms,
                    ),
                )
        # Rate-limited JPEG conversion for preview callback
        now_ts = int(time.time() * 1000)
        if now_ts - self._last_jpeg_ts >= self._frame_interval:
            if not frames:
                _LOGGER.info(
                    "video frame is empty, %d, %d",
                    frame_data.codec_id,
                    frame_data.timestamp,
                )
                self._last_jpeg_ts = now_ts
                return
            frame = frames[0]
            rgb_frame: VideoFrame = frame.to_rgb()
            img: Image.Image = rgb_frame.to_image()
            buf: BytesIO = BytesIO()
            img.save(buf, format="JPEG", quality=90)
            jpeg_data = buf.getvalue()
            self._main_loop.call_soon_threadsafe(
                self._main_loop.create_task,
                self._video_callback(
                    jpeg_data, frame_data.timestamp, frame_data.channel
                ),
            )
            self._last_jpeg_ts = now_ts

    def _on_audio_callback(self, frame_data: MIoTCameraFrameData) -> None:
        if not self._audio_decoder:
            # Create audio decoder
            if frame_data.codec_id == MIoTCameraCodec.AUDIO_OPUS:
                self._audio_decoder = AudioCodecContext.create("opus", "r")
            elif frame_data.codec_id == MIoTCameraCodec.AUDIO_G711A:
                self._audio_decoder = AudioCodecContext.create("pcm_alaw", "r")
                self._audio_decoder.sample_rate = 8000
                self._audio_decoder.layout = "mono"
            elif frame_data.codec_id == MIoTCameraCodec.AUDIO_G711U:
                self._audio_decoder = AudioCodecContext.create("pcm_mulaw", "r")
                self._audio_decoder.sample_rate = 8000
                self._audio_decoder.layout = "mono"
            else:
                _LOGGER.error("unsupported audio codec: %s, skipping frame", frame_data.codec_id)
                return
            self._resampler = AudioResampler(format="s16", layout="mono", rate=16000)
            _LOGGER.info("audio decoder created, %s", frame_data.codec_id)
        pkt = Packet(frame_data.data)
        frames: List[AudioFrame] = self._audio_decoder.decode(pkt)  # type: ignore
        # Stamp decode time right after av.decode() returns, i.e. BEFORE the
        # resample loop, so downstream "decode latency" reflects the decoder
        # proper and excludes resample cost. Symmetric with the video path.
        decoded_unix_ms = int(time.time() * 1000)
        # Resample once in the decoder thread, share result with both callbacks.
        # This avoids cross-thread FFmpeg access (resampler is not thread-safe).
        pcm_bytes: bytes = b""
        pcm_ndarrays = []
        for frame in frames:
            try:
                rs_frames = self._resampler.resample(frame)
                for rs_frame in rs_frames:
                    nd = rs_frame.to_ndarray().flatten()
                    pcm_bytes += nd.tobytes()
                    pcm_ndarrays.append(nd)
            except Exception as e:
                _LOGGER.warning("Failed to resample audio frame: %s", e)
        # Emit resampled PCM ndarray for perception pipeline
        if self._audio_frame_callback and pcm_ndarrays:
            for pcm_nd in pcm_ndarrays:
                self._main_loop.call_soon_threadsafe(
                    self._main_loop.create_task,
                    self._audio_frame_callback(
                        pcm_nd,
                        frame_data.timestamp,
                        frame_data.channel,
                        frame_data.recv_unix_ms,
                        decoded_unix_ms,
                    ),
                )
        # Existing PCM bytes callback
        self._main_loop.call_soon_threadsafe(
            self._main_loop.create_task,
            self._audio_callback(pcm_bytes, frame_data.timestamp, frame_data.channel),
        )


class MIoTMediaRecorder(threading.Thread):
    """MIoT Recorder."""

    _main_loop: asyncio.AbstractEventLoop
