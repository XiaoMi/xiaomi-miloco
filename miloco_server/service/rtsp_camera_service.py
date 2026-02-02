# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
RTSP摄像头服务层
处理RTSP摄像头的业务逻辑
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

from miloco_server.dao.rtsp_camera_dao import RTSPCameraDAO
from miloco_server.schema.rtsp_camera_schema import (
    RTSPCameraCreate,
    RTSPCameraInfo,
    RTSPCameraUpdate,
)
from miloco_server.utils.rtsp_camera_handler import RTSPCameraHandler

logger = logging.getLogger(__name__)


class RTSPCameraService:
    """RTSP摄像头服务类"""

    def __init__(self, rtsp_camera_dao: RTSPCameraDAO):
        self._dao = rtsp_camera_dao
        # 摄像头处理器映射: {camera_id: RTSPCameraHandler}
        self._handlers: Dict[str, RTSPCameraHandler] = {}
        # 启动时初始化所有启用的摄像头
        self._init_task: Optional[asyncio.Task] = None
        logger.info("RTSPCameraService 初始化完成")

    async def initialize(self):
        """初始化服务，启动所有启用的摄像头"""
        cameras = self._dao.get_all(enabled_only=True)
        for camera in cameras:
            try:
                await self._create_handler(camera)
                logger.info("RTSP摄像头处理器创建成功: %s", camera.get("name"))
            except Exception as e:
                logger.error("创建RTSP摄像头处理器失败: %s, error=%s", camera.get("name"), e)

    async def _create_handler(self, camera_data: Dict[str, Any]) -> Optional[RTSPCameraHandler]:
        """
        创建摄像头处理器

        Args:
            camera_data: 摄像头数据

        Returns:
            Optional[RTSPCameraHandler]: 处理器实例
        """
        camera_id = camera_data.get("id")
        if not camera_id:
            return None

        if camera_id in self._handlers:
            # 先销毁旧的处理器
            await self._handlers[camera_id].destroy()

        handler = RTSPCameraHandler(
            camera_id=camera_id,
            name=camera_data.get("name", ""),
            rtsp_url_main=camera_data.get("rtsp_url_main", ""),
            rtsp_url_sub=camera_data.get("rtsp_url_sub", ""),
            on_status_change=self._on_status_change
        )
        
        await handler.start()
        self._handlers[camera_id] = handler
        return handler

    async def _on_status_change(self, camera_id: str, channel: int, online: bool):
        """
        摄像头状态变化回调

        Args:
            camera_id: 摄像头ID
            channel: 通道号
            online: 是否在线
        """
        self._dao.update_online_status(camera_id, channel, online)
        logger.info("RTSP摄像头状态更新: id=%s, channel=%d, online=%s", camera_id, channel, online)

    def create_camera(self, camera_create: RTSPCameraCreate) -> Optional[RTSPCameraInfo]:
        """
        创建RTSP摄像头

        Args:
            camera_create: 创建请求数据

        Returns:
            Optional[RTSPCameraInfo]: 创建的摄像头信息
        """
        camera_data = camera_create.model_dump()
        camera_id = self._dao.create(camera_data)
        
        if camera_id:
            camera = self._dao.get_by_id(camera_id)
            if camera:
                # 异步创建处理器
                asyncio.create_task(self._create_handler(camera))
                return RTSPCameraInfo(**camera)
        
        return None

    def get_camera(self, camera_id: str) -> Optional[RTSPCameraInfo]:
        """
        获取单个摄像头信息

        Args:
            camera_id: 摄像头ID

        Returns:
            Optional[RTSPCameraInfo]: 摄像头信息
        """
        camera = self._dao.get_by_id(camera_id)
        if camera:
            return RTSPCameraInfo(**camera)
        return None

    def get_all_cameras(self, enabled_only: bool = False) -> List[RTSPCameraInfo]:
        """
        获取所有摄像头

        Args:
            enabled_only: 是否只返回启用的摄像头

        Returns:
            List[RTSPCameraInfo]: 摄像头列表
        """
        cameras = self._dao.get_all(enabled_only=enabled_only)
        return [RTSPCameraInfo(**camera) for camera in cameras]

    async def update_camera(self, camera_id: str, camera_update: RTSPCameraUpdate) -> Optional[RTSPCameraInfo]:
        """
        更新摄像头信息

        Args:
            camera_id: 摄像头ID
            camera_update: 更新数据

        Returns:
            Optional[RTSPCameraInfo]: 更新后的摄像头信息
        """
        # 过滤None值
        update_data = {k: v for k, v in camera_update.model_dump().items() if v is not None}
        
        if self._dao.update(camera_id, update_data):
            camera = self._dao.get_by_id(camera_id)
            if camera:
                # 如果URL有变化，重新创建处理器
                if "rtsp_url_main" in update_data or "rtsp_url_sub" in update_data:
                    await self._create_handler(camera)
                # 如果启用状态变化
                elif "enabled" in update_data:
                    if update_data["enabled"]:
                        await self._create_handler(camera)
                    else:
                        if camera_id in self._handlers:
                            await self._handlers[camera_id].destroy()
                            del self._handlers[camera_id]
                
                return RTSPCameraInfo(**camera)
        
        return None

    async def delete_camera(self, camera_id: str) -> bool:
        """
        删除摄像头

        Args:
            camera_id: 摄像头ID

        Returns:
            bool: 删除是否成功
        """
        # 先停止处理器
        if camera_id in self._handlers:
            await self._handlers[camera_id].destroy()
            del self._handlers[camera_id]
        
        return self._dao.delete(camera_id)

    async def check_camera_status(self, camera_id: str) -> Dict[str, Any]:
        """
        检查摄像头状态

        Args:
            camera_id: 摄像头ID

        Returns:
            Dict[str, Any]: 状态信息
        """
        camera = self._dao.get_by_id(camera_id)
        if not camera:
            return {"error": "摄像头不存在"}

        handler = self._handlers.get(camera_id)
        if handler:
            status = await handler.check_status()
            # 更新数据库状态
            self._dao.update_online_status(camera_id, 0, status.get("online_main", False))
            self._dao.update_online_status(camera_id, 1, status.get("online_sub", False))
            return status
        
        return {
            "online_main": False,
            "online_sub": False,
            "error": "处理器未运行"
        }

    async def start_video_stream(
        self, 
        camera_id: str, 
        channel: int,
        callback: Callable[[str, bytes, int, int, int], Coroutine]
    ) -> bool:
        """
        启动视频流

        Args:
            camera_id: 摄像头ID
            channel: 通道号
            callback: 视频帧回调函数

        Returns:
            bool: 是否成功
        """
        handler = self._handlers.get(camera_id)
        if not handler:
            camera = self._dao.get_by_id(camera_id)
            if camera:
                handler = await self._create_handler(camera)
        
        if handler:
            return await handler.register_video_callback(channel, callback)
        
        return False

    async def stop_video_stream(self, camera_id: str, channel: int) -> bool:
        """
        停止视频流

        Args:
            camera_id: 摄像头ID
            channel: 通道号

        Returns:
            bool: 是否成功
        """
        handler = self._handlers.get(camera_id)
        if handler:
            return await handler.unregister_video_callback(channel)
        return False

    def get_recent_camera_img(self, camera_id: str, channel: int, count: int):
        """
        获取最近的摄像头图像

        Args:
            camera_id: 摄像头ID
            channel: 通道号
            count: 图像数量

        Returns:
            CameraImgSeq (miot_schema格式) or None
        """
        from miloco_server.schema.miot_schema import CameraImgSeq, CameraImgInfo, CameraInfo
        
        handler = self._handlers.get(camera_id)
        if not handler:
            logger.warning("RTSP摄像头处理器未找到: %s", camera_id)
            return None
        
        # 获取图像数据
        raw_img_seq = handler.get_recent_images(channel, count)
        if not raw_img_seq or not raw_img_seq.img_list:
            logger.warning("RTSP摄像头没有可用图像: %s, channel=%d", camera_id, channel)
            return None
        
        # 获取摄像头信息
        camera_data = self._dao.get_by_id(camera_id)
        if not camera_data:
            logger.warning("RTSP摄像头数据未找到: %s", camera_id)
            return None
        
        # 构建CameraInfo（与米家摄像头格式一致）
        camera_info = CameraInfo(
            did=camera_id,
            name=camera_data.get("name", "RTSP Camera"),
            model="rtsp.camera.custom",
            online=camera_data.get("online_main", False) or camera_data.get("online_sub", False),
            channel_count=2 if camera_data.get("rtsp_url_sub") else 1,
            camera_type="rtsp",
            home_name=camera_data.get("location", ""),
            room_name=camera_data.get("location", ""),
        )
        
        # 转换图像列表格式
        img_list = [
            CameraImgInfo(data=img.data, timestamp=img.timestamp)
            for img in raw_img_seq.img_list
        ]
        
        # 返回与米家摄像头相同格式的CameraImgSeq
        return CameraImgSeq(
            camera_info=camera_info,
            channel=channel,
            img_list=img_list
        )

    async def refresh_all_status(self) -> Dict[str, Dict[str, bool]]:
        """
        刷新所有摄像头状态

        Returns:
            Dict[str, Dict[str, bool]]: 所有摄像头的状态
        """
        result = {}
        for camera_id, handler in self._handlers.items():
            status = await handler.check_status()
            result[camera_id] = status
            self._dao.update_online_status(camera_id, 0, status.get("online_main", False))
            self._dao.update_online_status(camera_id, 1, status.get("online_sub", False))
        return result
