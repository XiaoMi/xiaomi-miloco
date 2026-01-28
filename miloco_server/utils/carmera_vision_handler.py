# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Camera vision handler utility for managing camera image streams.
Provides functionality to handle camera image queues and vision processing.
"""

import asyncio
import logging
import time
import threading
from collections import deque
from typing import Any, Callable, Coroutine, List

from miloco_server.schema.miot_schema import CameraImgInfo, CameraImgSeq, CameraInfo
from miot.camera import MIoTCameraInstance
from miot.types import MIoTCameraInfo
from miot.rtsp_camera import RTSPCameraInstance, RtspCameraInfo

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

    def to_list(self) -> List[Any]:
        """Convert to list, from oldest to newest"""
        with self._lock:
            self._filter_old_items()
            return [item[0] for item in self.queue]

    def get_recent(self, n: int) -> List[Any]:
        """Get the most recent n elements, sorted by time from old to new

        Args:
            n: Number of elements to get

        Returns:
            List of the most recent n elements, returns all elements if queue has fewer than n elements
        """
        if n <= 0:
            return []

        with self._lock:
            # Get the most recent n elements, starting from the tail of the queue
            self._filter_old_items()
            actual_n = min(n, len(self.queue))
            # Use negative indexing to get from the end of the queue, maintaining order from old to new
            recent_items = [item[0] for item in self.queue][-actual_n:]
            return recent_items


class BaseCameraVisionHandler:
    """Base camera vision handler strategy."""

    async def register_raw_stream(self, callback: Callable[[str, bytes, int, int, int], Coroutine], channel: int):
        raise NotImplementedError

    async def unregister_raw_stream(self, channel: int):
        raise NotImplementedError

    async def update_camera_info(self, camera_info: Any) -> None:
        raise NotImplementedError

    def get_recents_camera_img(self, channel: int, n: int) -> CameraImgSeq:
        raise NotImplementedError

    async def destroy(self) -> None:
        raise NotImplementedError


class CameraVisionHandler(BaseCameraVisionHandler):
    """Camera vision handler for managing camera image streams"""

    def __init__(self, camera_info: MIoTCameraInfo, miot_camera_instance: MIoTCameraInstance, max_size: int, ttl: int):
        # ttl seconds
        self.camera_info = camera_info
        self.miot_camera_instance = miot_camera_instance
        self.camera_img_queues: dict[int, SizeLimitedQueue] = {}

        for channel in range(self.camera_info.channel_count or 1):
            self.camera_img_queues[channel] = SizeLimitedQueue(max_size=max_size, ttl=ttl)
            asyncio.create_task(self.miot_camera_instance.register_decode_jpg_async(self.add_camera_img, channel))

        logger.info("CameraImgManager init success, camera did: %s", self.camera_info.did)

    async def register_raw_stream(self, callback: Callable[[str, bytes, int, int, int], Coroutine], channel: int):
        await self.miot_camera_instance.register_raw_video_async(callback, channel)

    async def unregister_raw_stream(self, channel: int):
        await self.miot_camera_instance.unregister_raw_video_async(channel)

    async def add_camera_img(self, did: str, data: bytes, ts: int, channel: int):
        logger.debug("add_camera_img camera_id: %s, camera timestamp: %d, image_size: %d", did, ts, len(data))
        self.camera_img_queues[channel].put(CameraImgInfo(data=data, timestamp=int(time.time())))

    async def update_camera_info(self, camera_info: MIoTCameraInfo) -> None:
        self.camera_info = camera_info
        if self.camera_info.online:
            for channel in range(self.camera_info.channel_count or 1):
                await self.miot_camera_instance.register_decode_jpg_async(self.add_camera_img, channel)
        else:
            for channel in range(self.camera_info.channel_count or 1):
                await self.miot_camera_instance.unregister_decode_jpg_async(channel)
                self.camera_img_queues[channel].clear()

    def get_recents_camera_img(self, channel: int, n: int) -> CameraImgSeq:
        if self.camera_info.online:
            return CameraImgSeq(
                camera_info=CameraInfo.model_validate(self.camera_info.model_dump()),
                channel=channel,
                img_list=self.camera_img_queues[channel].get_recent(n))
        else:
            return CameraImgSeq(
                camera_info=CameraInfo.model_validate(self.camera_info.model_dump()),
                channel=channel,
                img_list=[])

    async def destroy(self) -> None:
        for channel in range(self.camera_info.channel_count or 1):
            await self.miot_camera_instance.unregister_decode_jpg_async(channel=channel)
            await self.miot_camera_instance.unregister_raw_video_async(channel=channel)
            self.camera_img_queues[channel].clear()

        await self.miot_camera_instance.destroy_async()

class RTSPEnabledCameraVisionHandler(CameraVisionHandler):
    """Camera vision handler that also forwards video to RTSP server."""

    def __init__(self, camera_info: MIoTCameraInfo, miot_camera_instance: MIoTCameraInstance,
                 rtsp_server, max_size: int, ttl: int):
        # Store RTSP server reference
        self._rtsp_server = rtsp_server
        # Track registered callbacks
        self._stream_reg_ids: dict[int, int] = {}
        # Track detected codecs
        self._detected_video_codec = 0
        self._detected_audio_codec = 0
        self._stream_added = False

        # Call parent constructor
        super().__init__(camera_info, miot_camera_instance, max_size, ttl)

        # Register raw video stream to forward to RTSP
        asyncio.create_task(self._register_rtsp_forwarding())

    async def _register_rtsp_forwarding(self):
        """Register a callback to forward video frames to RTSP server."""
        async def on_video(did: str, data: bytes, ts: int, seq: int, channel: int):  # pylint: disable=unused-argument
            # Detect video codec from data
            codec_id = self._detect_codec_from_data(data)
            if codec_id in [4, 5]:  # Video codecs
                if self._detected_video_codec == 0:
                    self._detected_video_codec = codec_id
                    logger.info("Detected video codec: %s for %s",
                              "H264" if codec_id == 4 else "H265", did)

                # Add RTSP stream if not added yet
                if not self._stream_added:
                    self._stream_added = True
                    # Default to G711A for audio if not detected
                    audio_codec = self._detected_audio_codec if self._detected_audio_codec > 0 else 1027

                    if self._rtsp_server.add_stream(did, self._detected_video_codec, audio_codec):
                        logger.info("RTSP stream added for %s (video: %s, audio: %s)",
                                  did,
                                  "H264" if self._detected_video_codec == 4 else "H265",
                                  {1026: "G711U", 1027: "G711A", 1032: "OPUS"}[audio_codec])
                    else:
                        logger.warning("Failed to add RTSP stream for %s", did)
                        return

                # Forward video frame to RTSP server
                if self._rtsp_server:
                    frame_type = 1 if self._is_i_frame(data) else 0
                    self._rtsp_server.push_frame(did, self._detected_video_codec, data, ts, seq, frame_type)

        async def on_audio(did: str, data: bytes, ts: int, seq: int, channel: int):  # pylint: disable=unused-argument
            # Audio is harder to detect from data, assume common codec
            if self._detected_audio_codec == 0:
                # Default to G711A for Xiaomi cameras
                self._detected_audio_codec = 1027
                logger.info("Using default audio codec: G711A for %s", did)

        # Register video callback
        self._rtsp_reg_id = await self.miot_camera_instance.register_raw_video_async(on_video, multi_reg=True)
        # Register audio callback
        self._rtsp_audio_reg_id = await self.miot_camera_instance.register_raw_audio_async(on_audio, multi_reg=True)

        logger.info("RTSP forwarding enabled for %s", self.camera_info.did)

    def _detect_codec_from_data(self, data: bytes) -> int:
        """Detect codec ID from raw frame data."""
        if not data or len(data) < 5:
            return 0

        # Check for NAL unit start code
        if data[0] == 0x00 and data[1] == 0x00 and data[2] == 0x00 and data[3] == 0x01:
            nal_type = data[4] & 0x1F
            # H.264 NAL types range from 1-23 for single NAL units
            if 1 <= nal_type <= 23:
                return 4  # H.264
            else:
                return 5  # H.265 (simplified detection)

        # Audio codecs are harder to detect from raw data, return 0
        return 0

    def _is_i_frame(self, data: bytes) -> bool:
        """Check if frame is an I-frame based on NAL unit type."""
        if not data or len(data) < 5:
            return False

        if data[0] == 0x00 and data[1] == 0x00 and data[2] == 0x00 and data[3] == 0x01:
            nal_type = data[4] & 0x1F
            # I-frame NAL types: 5 (IDR), 7 (SPS), 8 (PPS) for H.264
            if nal_type in [5, 7, 8]:
                return True
        return False

    async def register_raw_stream(self, callback: Callable[[str, bytes, int, int, int], Coroutine], channel: int):
        # Use multi_reg=True to not override RTSP callback
        reg_id = await self.miot_camera_instance.register_raw_video_async(callback, channel, multi_reg=True)
        self._stream_reg_ids[channel] = reg_id
        return reg_id

    async def unregister_raw_stream(self, channel: int):
        # Only unregister the stream callback, not the RTSP callback
        if channel in self._stream_reg_ids:
            reg_id = self._stream_reg_ids[channel]
            await self.miot_camera_instance.unregister_raw_video_async(channel, reg_id)
            del self._stream_reg_ids[channel]
            logger.info("Unregistered stream callback for channel %s, RTSP continues", channel)


class RtspCameraVisionHandler(BaseCameraVisionHandler):
    """RTSP camera vision handler using libcamera_rtsp."""

    def __init__(self, camera_info: RtspCameraInfo, rtsp_camera_instance: RTSPCameraInstance, max_size: int, ttl: int):
        self.camera_info = camera_info
        self.rtsp_camera_instance = rtsp_camera_instance
        self.camera_img_queues: dict[int, SizeLimitedQueue] = {}

        for channel in range(self.camera_info.channel_count or 1):
            self.camera_img_queues[channel] = SizeLimitedQueue(max_size=max_size, ttl=ttl)
            asyncio.create_task(self.rtsp_camera_instance.register_decode_jpg_async(self.add_camera_img, channel))

        logger.info("RtspCameraVisionHandler init success, camera did: %s", self.camera_info.did)

    async def register_raw_stream(self, callback: Callable[[str, bytes, int, int, int], Coroutine], channel: int):
        await self.rtsp_camera_instance.register_raw_video_async(callback, channel)

    async def unregister_raw_stream(self, channel: int):
        await self.rtsp_camera_instance.unregister_raw_video_async(channel)

    async def add_camera_img(self, did: str, data: bytes, ts: int, channel: int):
        logger.debug("rtsp add_camera_img camera_id: %s, camera timestamp: %d, image_size: %d", did, ts, len(data))
        self.camera_img_queues[channel].put(CameraImgInfo(data=data, timestamp=int(time.time())))

    async def update_camera_info(self, camera_info: RtspCameraInfo) -> None:
        self.camera_info = camera_info
        if self.camera_info.online:
            for channel in range(self.camera_info.channel_count or 1):
                await self.rtsp_camera_instance.register_decode_jpg_async(self.add_camera_img, channel)
        else:
            for channel in range(self.camera_info.channel_count or 1):
                await self.rtsp_camera_instance.unregister_decode_jpg_async(channel)
                self.camera_img_queues[channel].clear()

    def get_recents_camera_img(self, channel: int, n: int) -> CameraImgSeq:
        if self.camera_info.online:
            return CameraImgSeq(
                camera_info=CameraInfo.model_validate(self.camera_info.model_dump()),
                channel=channel,
                img_list=self.camera_img_queues[channel].get_recent(n))
        else:
            return CameraImgSeq(
                camera_info=CameraInfo.model_validate(self.camera_info.model_dump()),
                channel=channel,
                img_list=[])

    async def destroy(self) -> None:
        for channel in range(self.camera_info.channel_count or 1):
            await self.rtsp_camera_instance.unregister_decode_jpg_async(channel=channel)
            await self.rtsp_camera_instance.unregister_raw_video_async(channel=channel)
            self.camera_img_queues[channel].clear()

        await self.rtsp_camera_instance.destroy_async()
