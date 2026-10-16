# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Camera vision handler utility for managing camera image streams.
Provides functionality to handle camera image queues and vision processing.
"""

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

from av.audio.frame import AudioFrame
from av.video.frame import VideoFrame
from miot.camera import MIoTCamera, MIoTCameraInstance
from miot.types import MIoTCameraCodec, MIoTCameraInfo

from miloco.miot.schema import CameraImgInfo, CameraImgSeq, CameraInfo

logger = logging.getLogger(__name__)


class SizeLimitedQueue:
    """Size-limited queue that automatically removes oldest elements"""

    def __init__(self, max_size: int, ttl: int):
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self.max_size = max_size
        self.ttl = ttl
        self.queue = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def _filter_old_items(self) -> None:
        """Filter old items"""
        current_time = time.time()
        while self.queue and current_time - self.queue[0][1] > self.ttl:
            self.queue.popleft()

    def clear(self) -> None:
        """Clear queue"""
        with self._lock:
            self.queue.clear()

    def put(self, item: Any) -> None:
        """Add element, automatically removes oldest element if queue is full"""
        with self._lock:
            self._filter_old_items()
            self.queue.append((item, time.time()))

    def get(self) -> Any:
        """Get and remove the oldest element"""
        with self._lock:
            if not self.queue:
                raise IndexError("Queue is empty")
            self._filter_old_items()
            if not self.queue:
                raise IndexError("Queue is empty after filtering")
            return self.queue.popleft()[0]

    def peek(self) -> Any:
        """View the oldest element without removing it"""
        with self._lock:
            if not self.queue:
                raise IndexError("Queue is empty")
            self._filter_old_items()
            if not self.queue:
                raise IndexError("Queue is empty after filtering")
            return self.queue[0][0]

    def size(self) -> int:
        """Return current queue size"""
        with self._lock:
            self._filter_old_items()
            return len(self.queue)

    def is_empty(self) -> bool:
        """Check if queue is empty"""
        with self._lock:
            self._filter_old_items()
            return len(self.queue) == 0

    def is_full(self) -> bool:
        """Check if queue is full"""
        with self._lock:
            self._filter_old_items()
            return len(self.queue) == self.max_size

    def to_list(self) -> list[Any]:
        """Convert to list, from oldest to newest"""
        with self._lock:
            self._filter_old_items()
            return [item[0] for item in self.queue]

    def get_recent(self, n: int) -> list[Any]:
        """Get the most recent n elements, sorted by time from old to new

        Args:
            n: Number of elements to get

        Returns:
            List of the most recent n elements, returns all elements if queue has fewer than n elements
        """
        if n <= 0:
            return []

        with self._lock:
            self._filter_old_items()
            actual_n = min(n, len(self.queue))
            recent_items = [entry[0] for entry in self.queue][-actual_n:]
            return recent_items


class CameraVisionHandler:
    """Camera vision handler for managing camera image streams"""

    _CODEC_ID_MAP: dict = {
        MIoTCameraCodec.AUDIO_OPUS: "opus",
        MIoTCameraCodec.AUDIO_G711A: "g711a",
        MIoTCameraCodec.AUDIO_G711U: "g711u",
    }

    def __init__(
        self,
        camera_info: MIoTCameraInfo,
        miot_camera_instance: MIoTCameraInstance,
        miot_camera_manager: MIoTCamera,
        max_size: int,
        ttl: int,
    ):
        # ttl seconds
        self.camera_info = camera_info
        self.miot_camera_instance = miot_camera_instance
        # 需要 manager 引用走 destroy_camera_async 清 _camera_map cache；
        # 直调 instance.destroy_async() 不 evict cache，下次 create_camera_async
        # 会 "camera already exists" 短路返回已 free 的 instance，register/start 全部
        # 返回 -1。
        self._miot_camera_manager = miot_camera_manager
        self.camera_img_queues: dict[int, SizeLimitedQueue] = {}
        self._audio_codec: dict[int, str | None] = {}

        for channel in range(self.camera_info.channel_count or 1):
            self.camera_img_queues[channel] = SizeLimitedQueue(
                max_size=max_size, ttl=ttl
            )
            self._audio_codec[channel] = None
            asyncio.create_task(
                self.miot_camera_instance.register_decode_jpg_async(
                    self.add_camera_img, channel
                )
            )

        logger.info(
            "CameraImgManager init success, camera did: %s", self.camera_info.did
        )

    async def register_raw_stream(
        self, callback: Callable[[str, bytes, int, int, int], Coroutine], channel: int
    ):
        await self.miot_camera_instance.register_raw_video_async(callback, channel)

    async def unregister_raw_stream(self, channel: int):
        await self.miot_camera_instance.unregister_raw_video_async(channel)

    async def add_camera_img(self, did: str, data: bytes, ts: int, channel: int):
        logger.debug(
            "add_camera_img camera_id: %s, camera timestamp: %d, image_size: %d",
            did,
            ts,
            len(data),
        )
        self.camera_img_queues[channel].put(
            CameraImgInfo(data=data, timestamp=int(time.time()))
        )

    async def update_camera_info(self, camera_info: MIoTCameraInfo) -> None:
        self.camera_info = camera_info
        if self.camera_info.connected:
            for channel in range(self.camera_info.channel_count or 1):
                await self.miot_camera_instance.register_decode_jpg_async(
                    self.add_camera_img, channel
                )
        else:
            for channel in range(self.camera_info.channel_count or 1):
                await self.miot_camera_instance.unregister_decode_jpg_async(channel)
                self.camera_img_queues[channel].clear()

    def get_recent_camera_img(self, channel: int, n: int) -> CameraImgSeq:
        if self.camera_info.connected:
            return CameraImgSeq(
                camera_info=CameraInfo.model_validate(self.camera_info.model_dump()),
                channel=channel,
                img_list=self.camera_img_queues[channel].get_recent(n),
            )
        else:
            return CameraImgSeq(
                camera_info=CameraInfo.model_validate(self.camera_info.model_dump()),
                channel=channel,
                img_list=[],
            )

    def get_audio_codec(self, channel: int) -> str | None:
        """Get detected audio codec for a channel."""
        return self._audio_codec.get(channel)

    async def register_raw_audio_stream(
        self, callback: Callable[[str, bytes, int, int, int], Coroutine], channel: int
    ):
        async def _detecting_wrapper(
            did: str, data: bytes, ts: int, seq: int, ch: int, codec_id: MIoTCameraCodec
        ):
            if self._audio_codec.get(ch) is None:
                self._audio_codec[ch] = self._CODEC_ID_MAP.get(codec_id, "opus")
                logger.info(
                    "Detected audio codec for camera %s channel %d: %s",
                    did,
                    ch,
                    self._audio_codec[ch],
                )
            await callback(did, data, ts, seq, ch)

        await self.miot_camera_instance.register_raw_audio_async(
            _detecting_wrapper, channel
        )

    async def unregister_raw_audio_stream(self, channel: int):
        await self.miot_camera_instance.unregister_raw_audio_async(channel)
        self._audio_codec[channel] = None

    async def register_decode_video_frame_stream(
        self,
        callback: Callable[[str, VideoFrame, int, int, int, int], Coroutine],
        channel: int,
        *,
        full_rate: bool = False,
    ) -> int:
        """Register decoded VideoFrame callback (multi_reg, coexists with internal decode_jpg)."""
        return await self.miot_camera_instance.register_decode_video_frame_async(
            callback, channel, multi_reg=True, full_rate=full_rate
        )

    async def unregister_decode_video_frame_stream(self, channel: int, reg_id: int):
        await self.miot_camera_instance.unregister_decode_video_frame_async(
            channel, reg_id
        )

    async def register_decode_audio_frame_stream(
        self, callback: Callable[[str, AudioFrame, int, int, int, int], Coroutine], channel: int
    ) -> int:
        """Register decoded AudioFrame callback (multi_reg)."""
        return await self.miot_camera_instance.register_decode_audio_frame_async(
            callback, channel, multi_reg=True
        )

    async def unregister_decode_audio_frame_stream(self, channel: int, reg_id: int):
        await self.miot_camera_instance.unregister_decode_audio_frame_async(
            channel, reg_id
        )

    async def destroy(self) -> None:
        for channel in range(self.camera_info.channel_count or 1):
            await self.miot_camera_instance.unregister_decode_jpg_async(channel=channel)
            await self.miot_camera_instance.unregister_raw_video_async(channel=channel)
            await self.miot_camera_instance.unregister_raw_audio_async(channel=channel)
            self.camera_img_queues[channel].clear()

        # 走 manager 入口让 SDK 从 _camera_map cache 里 evict，否则下次
        # create_camera_async 会短路返回这个已 free 的 instance（"camera already
        # exists"），register/start 全部 -1，无法重新拉流。
        await self._miot_camera_manager.destroy_camera_async(
            did=self.camera_info.did
        )
