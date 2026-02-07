# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
RTSP摄像头处理器
使用OpenCV进行RTSP流的拉取和解码，更稳定可靠
"""

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    cv2 = None

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
        return CameraImgPathSeq(
            camera_id=self.camera_id,
            channel=self.channel,
            img_list=[]
        )


class RTSPStreamReader:
    """
    RTSP流读取器
    使用OpenCV进行流拉取和解码，比ffmpeg更稳定
    """

    def __init__(
        self,
        rtsp_url: str,
        camera_id: str,
        channel: int,
        frame_interval: int = 500,  # 毫秒
        on_frame: Optional[Callable[[bytes, int], None]] = None,
        on_status_change: Optional[Callable[[bool], None]] = None,
        jpeg_quality: int = 80  # JPEG压缩质量 (0-100)
    ):
        self._rtsp_url = rtsp_url
        self._camera_id = camera_id
        self._channel = channel
        self._frame_interval = frame_interval
        self._on_frame = on_frame
        self._on_status_change = on_status_change
        self._jpeg_quality = jpeg_quality
        
        self._capture: Optional[Any] = None  # cv2.VideoCapture
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._online = False
        self._last_frame_time = 0
        self._force_reconnect = threading.Event()
        self._lock = threading.Lock()
        
        # 连接参数
        self._connect_timeout = 10  # 连接超时（秒）
        self._read_timeout = 15  # 读取超时（秒）
        self._last_success_read = 0

    @property
    def online(self) -> bool:
        return self._online

    def trigger_reconnect(self):
        """触发立即重连"""
        self._force_reconnect.set()

    async def start(self) -> bool:
        """启动RTSP流读取"""
        if not CV2_AVAILABLE:
            logger.error("OpenCV (cv2) 未安装，无法启动RTSP流: camera_id=%s", self._camera_id)
            return False
            
        if self._running:
            return True

        if not self._rtsp_url:
            logger.warning("RTSP URL为空，无法启动: camera_id=%s, channel=%d", 
                         self._camera_id, self._channel)
            return False

        self._running = True
        self._thread = threading.Thread(target=self._read_stream, daemon=True)
        self._thread.start()
        logger.info("RTSP流读取器启动: camera_id=%s, channel=%d", self._camera_id, self._channel)
        return True

    async def stop(self):
        """停止RTSP流读取"""
        self._running = False
        self._force_reconnect.set()
        
        # 释放capture
        with self._lock:
            if self._capture is not None:
                try:
                    self._capture.release()
                except Exception as e:
                    logger.error("释放VideoCapture时出错: %s", e)
                self._capture = None
        
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

    def _create_capture(self) -> bool:
        """创建VideoCapture对象"""
        try:
            # 释放旧的capture
            with self._lock:
                if self._capture is not None:
                    try:
                        self._capture.release()
                    except Exception:
                        pass
                    self._capture = None
            
            # 使用FFMPEG后端，TCP传输
            capture = cv2.VideoCapture(self._rtsp_url, cv2.CAP_FFMPEG)
            
            # 设置参数优化延迟和稳定性
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 最小缓冲
            capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self._connect_timeout * 1000)
            capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self._read_timeout * 1000)
            
            # 检查是否成功打开
            if not capture.isOpened():
                logger.warning("无法打开RTSP流: camera_id=%s, channel=%d", 
                             self._camera_id, self._channel)
                capture.release()
                return False
            
            with self._lock:
                self._capture = capture
            
            logger.info("成功连接RTSP流: camera_id=%s, channel=%d", 
                       self._camera_id, self._channel)
            return True
            
        except Exception as e:
            logger.error("创建VideoCapture失败: camera_id=%s, error=%s", 
                        self._camera_id, e)
            return False

    def _read_stream(self):
        """读取RTSP流的线程函数"""
        reconnect_delay = 2  # 初始重连延迟（秒）
        max_reconnect_delay = 30  # 最大重连延迟
        consecutive_failures = 0
        max_consecutive_failures = 10  # 连续失败次数阈值
        
        while self._running:
            try:
                # 尝试连接
                if not self._create_capture():
                    self._set_online(False)
                    consecutive_failures += 1
                    
                    if consecutive_failures >= max_consecutive_failures:
                        logger.warning("连续 %d 次连接失败，增加延迟: camera_id=%s", 
                                     consecutive_failures, self._camera_id)
                    
                    # 等待后重试
                    if self._running:
                        interrupted = self._force_reconnect.wait(timeout=reconnect_delay)
                        if interrupted:
                            self._force_reconnect.clear()
                            reconnect_delay = 2
                            logger.info("强制重连被触发: camera_id=%s", self._camera_id)
                        else:
                            reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)
                    continue
                
                # 成功连接
                self._set_online(True)
                reconnect_delay = 2
                consecutive_failures = 0
                self._last_success_read = time.time()
                
                # 读取帧循环
                while self._running:
                    with self._lock:
                        if self._capture is None:
                            break
                        ret, frame = self._capture.read()
                    
                    if not ret or frame is None:
                        # 检查是否超时
                        if time.time() - self._last_success_read > self._read_timeout:
                            logger.warning("读取超时，准备重连: camera_id=%s, channel=%d", 
                                         self._camera_id, self._channel)
                            break
                        time.sleep(0.1)
                        continue
                    
                    self._last_success_read = time.time()
                    
                    # 控制帧率
                    current_time = int(time.time() * 1000)
                    if current_time - self._last_frame_time >= self._frame_interval:
                        self._last_frame_time = current_time
                        
                        # 编码为JPEG
                        try:
                            encode_param = [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality]
                            _, jpeg_data = cv2.imencode('.jpg', frame, encode_param)
                            
                            if self._on_frame and jpeg_data is not None:
                                self._on_frame(jpeg_data.tobytes(), current_time)
                        except Exception as e:
                            logger.error("编码JPEG失败: %s", e)
                    
                    # 检查是否需要强制重连
                    if self._force_reconnect.is_set():
                        self._force_reconnect.clear()
                        logger.info("收到强制重连信号: camera_id=%s", self._camera_id)
                        break
                
                # 循环结束，设置离线
                self._set_online(False)
                
            except Exception as e:
                logger.error("RTSP流读取出错: camera_id=%s, channel=%d, error=%s", 
                           self._camera_id, self._channel, e)
                self._set_online(False)
                consecutive_failures += 1
            
            # 释放资源
            with self._lock:
                if self._capture is not None:
                    try:
                        self._capture.release()
                    except Exception:
                        pass
                    self._capture = None
            
            # 重连等待
            if self._running:
                logger.info("将在 %.1f 秒后重连: camera_id=%s, channel=%d", 
                          reconnect_delay, self._camera_id, self._channel)
                interrupted = self._force_reconnect.wait(timeout=reconnect_delay)
                if interrupted:
                    self._force_reconnect.clear()
                    reconnect_delay = 2


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
        
        # 按需启动标志
        self._auto_start = False  # 是否自动启动流（改为按需启动）
        
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
        """启动摄像头处理器（不自动启动流，按需启动）"""
        self._main_loop = asyncio.get_event_loop()
        logger.info("RTSPCameraHandler 就绪（按需启动流）: id=%s", self._camera_id)

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
        callbacks = self._video_callbacks.get(channel, [])
        for callback in callbacks:
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
        # 如果流读取器正在运行，直接返回状态
        reader_main = self._readers.get(0)
        reader_sub = self._readers.get(1)
        
        online_main = reader_main is not None and reader_main.online
        online_sub = reader_sub is not None and reader_sub.online
        
        # 如果流读取器没有运行，执行快速探测
        if reader_main is None and self._rtsp_url_main:
            online_main = await self._probe_rtsp(self._rtsp_url_main)
        
        if reader_sub is None and self._rtsp_url_sub:
            online_sub = await self._probe_rtsp(self._rtsp_url_sub)
        
        return {
            "online_main": online_main,
            "online_sub": online_sub
        }

    async def _probe_rtsp(self, rtsp_url: str, timeout: float = 5.0) -> bool:
        """快速探测RTSP流是否可用"""
        if not CV2_AVAILABLE:
            return False
        
        def _probe():
            try:
                cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(timeout * 1000))
                
                if cap.isOpened():
                    ret, _ = cap.read()
                    cap.release()
                    return ret
                return False
            except Exception as e:
                logger.debug("RTSP探测失败: %s", e)
                return False
        
        # 在线程中执行探测，避免阻塞
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _probe)

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
        
        # 确保流读取器正在运行
        await self._ensure_reader_running(channel)
        
        return True

    async def _ensure_reader_running(self, channel: int):
        """确保指定通道的流读取器正在运行"""
        reader = self._readers.get(channel)
        
        if reader is None:
            # 需要创建新的读取器
            rtsp_url = self._rtsp_url_main if channel == 0 else self._rtsp_url_sub
            if not rtsp_url:
                logger.warning("RTSP URL为空，无法创建读取器: camera_id=%s, channel=%d", 
                             self._camera_id, channel)
                return
            
            reader = RTSPStreamReader(
                rtsp_url=rtsp_url,
                camera_id=self._camera_id,
                channel=channel,
                frame_interval=self._frame_interval,
                on_frame=lambda data, ts, ch=channel: self._on_frame(ch, data, ts),
                on_status_change=lambda online, ch=channel: self._on_channel_status_change(ch, online)
            )
            self._readers[channel] = reader
            await reader.start()
            logger.info("创建并启动流读取器: camera_id=%s, channel=%d", self._camera_id, channel)
        
        elif not reader._running:
            # 读取器存在但已停止，重新启动
            await reader.start()
            logger.info("重新启动流读取器: camera_id=%s, channel=%d", self._camera_id, channel)
        
        elif not reader.online:
            # 读取器正在运行但不在线，触发立即重连
            reader.trigger_reconnect()
            logger.info("触发流读取器立即重连: camera_id=%s, channel=%d", self._camera_id, channel)

    async def unregister_video_callback(self, channel: int) -> bool:
        """取消注册视频回调"""
        if channel in self._video_callbacks:
            self._video_callbacks[channel].clear()
            logger.info("取消注册视频回调: camera_id=%s, channel=%d", self._camera_id, channel)
        
        # 如果没有回调了，可以考虑停止流以节省资源
        # 但这里保持流运行以便快速响应下次请求
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
