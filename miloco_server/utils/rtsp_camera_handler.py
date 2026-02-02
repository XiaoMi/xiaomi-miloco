# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
RTSP摄像头处理器
负责RTSP流的拉取、解码和帧管理
"""

import asyncio
import io
import logging
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CameraImg:
    """摄像头图像数据"""
    data: bytes  # JPEG图像数据
    timestamp: int  # 时间戳（毫秒）


@dataclass
class CameraImgSeq:
    """摄像头图像序列"""
    camera_id: str
    channel: int
    img_list: List[CameraImg] = field(default_factory=list)

    async def store_to_path(self):
        """存储图像到路径（兼容现有接口）"""
        from miloco_server.schema.miot_schema import CameraImgPathSeq
        # 这里简化处理，实际使用时可能需要存储到文件
        return CameraImgPathSeq(
            camera_id=self.camera_id,
            channel=self.channel,
            img_list=[]  # 简化处理
        )


class RTSPStreamReader:
    """
    RTSP流读取器
    使用ffmpeg进行流拉取和解码
    """

    def __init__(
        self,
        rtsp_url: str,
        camera_id: str,
        channel: int,
        frame_interval: int = 500,  # 毫秒
        on_frame: Optional[Callable[[bytes, int], None]] = None,
        on_status_change: Optional[Callable[[bool], None]] = None
    ):
        self._rtsp_url = rtsp_url
        self._camera_id = camera_id
        self._channel = channel
        self._frame_interval = frame_interval
        self._on_frame = on_frame
        self._on_status_change = on_status_change
        
        self._process: Optional[subprocess.Popen] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._online = False
        self._last_frame_time = 0

    @property
    def online(self) -> bool:
        return self._online

    async def start(self) -> bool:
        """启动RTSP流读取"""
        if self._running:
            return True

        if not self._rtsp_url:
            logger.warning("RTSP URL为空，无法启动: camera_id=%s, channel=%d", self._camera_id, self._channel)
            return False

        self._running = True
        self._thread = threading.Thread(target=self._read_stream, daemon=True)
        self._thread.start()
        logger.info("RTSP流读取器启动: camera_id=%s, channel=%d", self._camera_id, self._channel)
        return True

    async def stop(self):
        """停止RTSP流读取"""
        self._running = False
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception as e:
                logger.error("停止ffmpeg进程时出错: %s", e)
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        
        self._set_online(False)
        logger.info("RTSP流读取器停止: camera_id=%s, channel=%d", self._camera_id, self._channel)

    def _set_online(self, online: bool):
        """设置在线状态"""
        if self._online != online:
            self._online = online
            if self._on_status_change:
                try:
                    self._on_status_change(online)
                except Exception as e:
                    logger.error("状态变化回调出错: %s", e)

    def _read_stream(self):
        """读取RTSP流的线程函数"""
        reconnect_delay = 5  # 重连延迟（秒）
        max_reconnect_delay = 60  # 最大重连延迟
        
        while self._running:
            try:
                # 使用ffmpeg拉取RTSP流并转换为JPEG图像
                # -rtsp_transport tcp: 使用TCP传输
                # -fflags nobuffer: 减少延迟
                # -flags low_delay: 低延迟模式
                # -r: 帧率
                # -f image2pipe: 输出为图像流
                # -vcodec mjpeg: 输出MJPEG格式
                cmd = [
                    'ffmpeg',
                    '-rtsp_transport', 'tcp',
                    '-fflags', 'nobuffer',
                    '-flags', 'low_delay',
                    '-i', self._rtsp_url,
                    '-r', str(1000 // self._frame_interval),  # 根据帧间隔计算帧率
                    '-f', 'image2pipe',
                    '-vcodec', 'mjpeg',
                    '-q:v', '5',  # JPEG质量
                    '-'
                ]
                
                logger.info("启动ffmpeg: %s", ' '.join(cmd[:6] + ['<url>', '-r', cmd[8]] + cmd[9:]))
                
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=10**6
                )
                
                # 成功连接
                self._set_online(True)
                reconnect_delay = 5  # 重置重连延迟
                
                # 读取JPEG帧
                buffer = b''
                jpeg_start = b'\xff\xd8'
                jpeg_end = b'\xff\xd9'
                
                while self._running and self._process.poll() is None:
                    chunk = self._process.stdout.read(4096)
                    if not chunk:
                        break
                    
                    buffer += chunk
                    
                    # 查找完整的JPEG帧
                    while True:
                        start_idx = buffer.find(jpeg_start)
                        if start_idx == -1:
                            buffer = b''
                            break
                        
                        end_idx = buffer.find(jpeg_end, start_idx + 2)
                        if end_idx == -1:
                            # 保留从start_idx开始的数据
                            buffer = buffer[start_idx:]
                            break
                        
                        # 提取完整的JPEG帧
                        jpeg_data = buffer[start_idx:end_idx + 2]
                        buffer = buffer[end_idx + 2:]
                        
                        # 控制帧率
                        current_time = int(time.time() * 1000)
                        if current_time - self._last_frame_time >= self._frame_interval:
                            self._last_frame_time = current_time
                            if self._on_frame:
                                try:
                                    self._on_frame(jpeg_data, current_time)
                                except Exception as e:
                                    logger.error("帧回调出错: %s", e)
                
                # 进程结束
                self._set_online(False)
                if self._process:
                    stderr = self._process.stderr.read()
                    if stderr:
                        logger.warning("ffmpeg stderr: %s", stderr.decode('utf-8', errors='ignore')[-500:])
                
            except Exception as e:
                logger.error("RTSP流读取出错: camera_id=%s, channel=%d, error=%s", 
                           self._camera_id, self._channel, e)
                self._set_online(False)
            
            # 重连逻辑
            if self._running:
                logger.info("将在 %d 秒后重连: camera_id=%s, channel=%d", 
                          reconnect_delay, self._camera_id, self._channel)
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)


class RTSPCameraHandler:
    """
    RTSP摄像头处理器
    管理单个摄像头的多个通道
    """

    # 图像缓存配置
    MAX_CACHE_SIZE = 30  # 最大缓存帧数
    CACHE_TTL = 30  # 缓存过期时间（秒）

    def __init__(
        self,
        camera_id: str,
        name: str,
        rtsp_url_main: str,
        rtsp_url_sub: str = "",
        frame_interval: int = 500,
        on_status_change: Optional[Callable[[str, int, bool], Coroutine]] = None
    ):
        self._camera_id = camera_id
        self._name = name
        self._rtsp_url_main = rtsp_url_main
        self._rtsp_url_sub = rtsp_url_sub
        self._frame_interval = frame_interval
        self._on_status_change = on_status_change
        
        # 流读取器
        self._readers: Dict[int, RTSPStreamReader] = {}
        
        # 图像缓存: {channel: deque of (timestamp, jpeg_data)}
        self._image_cache: Dict[int, deque] = {0: deque(maxlen=self.MAX_CACHE_SIZE)}
        if rtsp_url_sub:
            self._image_cache[1] = deque(maxlen=self.MAX_CACHE_SIZE)
        
        # 视频回调: {channel: List[callback]}
        self._video_callbacks: Dict[int, List[Callable]] = {0: [], 1: []}
        
        # 主循环
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        
        logger.info("RTSPCameraHandler 创建: id=%s, name=%s", camera_id, name)

    @property
    def camera_id(self) -> str:
        return self._camera_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def channel_count(self) -> int:
        return 2 if self._rtsp_url_sub else 1

    async def start(self):
        """启动摄像头处理器"""
        self._main_loop = asyncio.get_event_loop()
        
        # 创建主码流读取器
        if self._rtsp_url_main:
            reader_main = RTSPStreamReader(
                rtsp_url=self._rtsp_url_main,
                camera_id=self._camera_id,
                channel=0,
                frame_interval=self._frame_interval,
                on_frame=lambda data, ts: self._on_frame(0, data, ts),
                on_status_change=lambda online: self._on_channel_status_change(0, online)
            )
            self._readers[0] = reader_main
            await reader_main.start()
        
        # 创建子码流读取器
        if self._rtsp_url_sub:
            reader_sub = RTSPStreamReader(
                rtsp_url=self._rtsp_url_sub,
                camera_id=self._camera_id,
                channel=1,
                frame_interval=self._frame_interval,
                on_frame=lambda data, ts: self._on_frame(1, data, ts),
                on_status_change=lambda online: self._on_channel_status_change(1, online)
            )
            self._readers[1] = reader_sub
            await reader_sub.start()
        
        logger.info("RTSPCameraHandler 启动完成: id=%s", self._camera_id)

    async def destroy(self):
        """销毁摄像头处理器"""
        for reader in self._readers.values():
            await reader.stop()
        self._readers.clear()
        self._image_cache.clear()
        self._video_callbacks.clear()
        logger.info("RTSPCameraHandler 销毁: id=%s", self._camera_id)

    def _on_frame(self, channel: int, data: bytes, timestamp: int):
        """帧数据回调"""
        # 添加到缓存
        if channel in self._image_cache:
            self._image_cache[channel].append((timestamp, data))
        
        # 调用视频回调
        for callback in self._video_callbacks.get(channel, []):
            try:
                if self._main_loop:
                    asyncio.run_coroutine_threadsafe(
                        callback(self._camera_id, data, timestamp, 0, channel),
                        self._main_loop
                    )
            except Exception as e:
                logger.error("视频回调出错: %s", e)

    def _on_channel_status_change(self, channel: int, online: bool):
        """通道状态变化回调"""
        if self._on_status_change and self._main_loop:
            asyncio.run_coroutine_threadsafe(
                self._on_status_change(self._camera_id, channel, online),
                self._main_loop
            )

    async def check_status(self) -> Dict[str, bool]:
        """检查摄像头状态"""
        return {
            "online_main": self._readers.get(0, None) is not None and self._readers[0].online,
            "online_sub": self._readers.get(1, None) is not None and self._readers[1].online
        }

    async def register_video_callback(
        self, 
        channel: int, 
        callback: Callable[[str, bytes, int, int, int], Coroutine]
    ) -> bool:
        """注册视频回调"""
        if channel not in self._video_callbacks:
            self._video_callbacks[channel] = []
        
        if callback not in self._video_callbacks[channel]:
            self._video_callbacks[channel].append(callback)
            logger.info("注册视频回调: camera_id=%s, channel=%d", self._camera_id, channel)
        
        return True

    async def unregister_video_callback(self, channel: int) -> bool:
        """取消注册视频回调"""
        if channel in self._video_callbacks:
            self._video_callbacks[channel].clear()
            logger.info("取消注册视频回调: camera_id=%s, channel=%d", self._camera_id, channel)
        return True

    def get_recent_images(self, channel: int, count: int) -> Optional[CameraImgSeq]:
        """获取最近的图像"""
        if channel not in self._image_cache:
            return None
        
        cache = self._image_cache[channel]
        if not cache:
            return None
        
        # 获取最近的count张图像
        images = list(cache)[-count:]
        current_time = int(time.time() * 1000)
        
        # 过滤过期的图像
        valid_images = [
            CameraImg(data=data, timestamp=ts)
            for ts, data in images
            if current_time - ts < self.CACHE_TTL * 1000
        ]
        
        if not valid_images:
            return None
        
        return CameraImgSeq(
            camera_id=self._camera_id,
            channel=channel,
            img_list=valid_images
        )
