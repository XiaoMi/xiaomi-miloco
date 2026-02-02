# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
RTSP摄像头控制器
处理RTSP摄像头的HTTP API请求
"""

import logging
from collections import OrderedDict
from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends, WebSocket
from fastapi.websockets import WebSocketDisconnect, WebSocketState

from miloco_server.middleware import verify_token, verify_websocket_token
from miloco_server.middleware.exceptions import ResourceNotFoundException
from miloco_server.schema.common_schema import NormalResponse
from miloco_server.schema.rtsp_camera_schema import (
    RTSPCameraCreate,
    RTSPCameraInfo,
    RTSPCameraUpdate,
)
from miloco_server.service.manager import get_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rtsp_camera", tags=["RTSP摄像头"])

manager = get_manager()


@router.get("", summary="获取所有RTSP摄像头", response_model=NormalResponse)
async def get_all_rtsp_cameras(
    enabled_only: bool = False,
    current_user: str = Depends(verify_token)
):
    """获取所有RTSP摄像头列表"""
    logger.info("获取RTSP摄像头列表, user: %s, enabled_only: %s", current_user, enabled_only)
    
    cameras = manager.rtsp_camera_service.get_all_cameras(enabled_only=enabled_only)
    
    logger.info("成功获取RTSP摄像头列表，数量: %d", len(cameras))
    return NormalResponse(
        code=0,
        message="获取RTSP摄像头列表成功",
        data=[camera.model_dump() for camera in cameras]
    )


@router.post("", summary="创建RTSP摄像头", response_model=NormalResponse)
async def create_rtsp_camera(
    camera_create: RTSPCameraCreate,
    current_user: str = Depends(verify_token)
):
    """创建新的RTSP摄像头"""
    logger.info("创建RTSP摄像头, user: %s, name: %s", current_user, camera_create.name)
    
    camera = manager.rtsp_camera_service.create_camera(camera_create)
    
    if camera:
        logger.info("RTSP摄像头创建成功: id=%s", camera.id)
        return NormalResponse(
            code=0,
            message="创建RTSP摄像头成功",
            data=camera.model_dump()
        )
    else:
        logger.error("RTSP摄像头创建失败")
        return NormalResponse(
            code=-1,
            message="创建RTSP摄像头失败",
            data=None
        )


@router.get("/{camera_id}", summary="获取单个RTSP摄像头", response_model=NormalResponse)
async def get_rtsp_camera(
    camera_id: str,
    current_user: str = Depends(verify_token)
):
    """获取单个RTSP摄像头信息"""
    logger.info("获取RTSP摄像头, user: %s, camera_id: %s", current_user, camera_id)
    
    camera = manager.rtsp_camera_service.get_camera(camera_id)
    
    if camera:
        return NormalResponse(
            code=0,
            message="获取RTSP摄像头成功",
            data=camera.model_dump()
        )
    else:
        raise ResourceNotFoundException(f"RTSP摄像头不存在: {camera_id}")


@router.put("/{camera_id}", summary="更新RTSP摄像头", response_model=NormalResponse)
async def update_rtsp_camera(
    camera_id: str,
    camera_update: RTSPCameraUpdate,
    current_user: str = Depends(verify_token)
):
    """更新RTSP摄像头信息"""
    logger.info("更新RTSP摄像头, user: %s, camera_id: %s", current_user, camera_id)
    
    camera = await manager.rtsp_camera_service.update_camera(camera_id, camera_update)
    
    if camera:
        logger.info("RTSP摄像头更新成功: id=%s", camera_id)
        return NormalResponse(
            code=0,
            message="更新RTSP摄像头成功",
            data=camera.model_dump()
        )
    else:
        raise ResourceNotFoundException(f"RTSP摄像头不存在或更新失败: {camera_id}")


@router.delete("/{camera_id}", summary="删除RTSP摄像头", response_model=NormalResponse)
async def delete_rtsp_camera(
    camera_id: str,
    current_user: str = Depends(verify_token)
):
    """删除RTSP摄像头"""
    logger.info("删除RTSP摄像头, user: %s, camera_id: %s", current_user, camera_id)
    
    success = await manager.rtsp_camera_service.delete_camera(camera_id)
    
    if success:
        logger.info("RTSP摄像头删除成功: id=%s", camera_id)
        return NormalResponse(
            code=0,
            message="删除RTSP摄像头成功",
            data=None
        )
    else:
        raise ResourceNotFoundException(f"RTSP摄像头不存在: {camera_id}")


@router.get("/{camera_id}/status", summary="检查RTSP摄像头状态", response_model=NormalResponse)
async def check_rtsp_camera_status(
    camera_id: str,
    current_user: str = Depends(verify_token)
):
    """检查RTSP摄像头在线状态"""
    logger.info("检查RTSP摄像头状态, user: %s, camera_id: %s", current_user, camera_id)
    
    status = await manager.rtsp_camera_service.check_camera_status(camera_id)
    
    return NormalResponse(
        code=0,
        message="检查RTSP摄像头状态成功",
        data=status
    )


@router.post("/refresh_status", summary="刷新所有RTSP摄像头状态", response_model=NormalResponse)
async def refresh_all_rtsp_camera_status(
    current_user: str = Depends(verify_token)
):
    """刷新所有RTSP摄像头的在线状态"""
    logger.info("刷新所有RTSP摄像头状态, user: %s", current_user)
    
    status = await manager.rtsp_camera_service.refresh_all_status()
    
    return NormalResponse(
        code=0,
        message="刷新RTSP摄像头状态成功",
        data=status
    )


# RTSP视频流WebSocket管理器
class RTSPVideoStreamManager:
    """RTSP视频流WebSocket管理器"""
    _CAMERA_CONNECT_COUNT_MAX: int = 4
    # key=camera_id.channel, value={user_name: {user_tag: Dict[id, websocket]}}
    _camera_connect_map: Dict[str, Dict[str, OrderedDict[str, WebSocket]]]
    _camera_connect_id: int

    def __init__(self):
        self._camera_connect_map = {}
        self._camera_connect_id = 0
        logger.info("初始化RTSP视频流WebSocket管理器")

    async def new_connection(
        self, websocket: WebSocket, user_name: str, token_hash: str, camera_id: str, channel: int
    ) -> str:
        """新建视频流连接"""
        camera_tag = f"{camera_id}.{channel}"
        if camera_tag not in self._camera_connect_map or not self._camera_connect_map[camera_tag]:
            self._camera_connect_map[camera_tag] = {}
            await manager.rtsp_camera_service.start_video_stream(
                camera_id=camera_id, channel=channel, callback=self.__video_stream_callback)
            logger.info("启动RTSP视频流, %s.%d", camera_id, channel)
        
        user_tag = f"{user_name}.{token_hash}"
        self._camera_connect_map[camera_tag].setdefault(user_tag, OrderedDict())
        connection_id = str(self._camera_connect_id)
        self._camera_connect_id += 1
        self._camera_connect_map[camera_tag][user_tag][connection_id] = websocket
        logger.info("新建RTSP视频流连接, %s, %s, %s", camera_tag, user_tag, connection_id)
        
        if len(self._camera_connect_map[camera_tag][user_tag]) > self._CAMERA_CONNECT_COUNT_MAX:
            logger.warning("连接数过多, %s.%d, %s, 移除最早的连接", camera_id, channel, user_tag)
            _, ws = self._camera_connect_map[camera_tag][user_tag].popitem(last=False)
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.close()
            except Exception as err:
                logger.error("关闭WebSocket出错: %s", err)
        
        return connection_id

    async def close_connection(
        self, user_name: str, token_hash: str, camera_id: str, channel: int, cid: str
    ):
        """关闭视频流连接"""
        camera_tag = f"{camera_id}.{channel}"
        user_tag = f"{user_name}.{token_hash}"
        if (
            camera_tag not in self._camera_connect_map
            or user_tag not in self._camera_connect_map[camera_tag]
            or cid not in self._camera_connect_map[camera_tag][user_tag]
        ):
            return
        
        logger.info("关闭RTSP视频流连接, %s, %s, %s", camera_tag, user_tag, cid)
        
        try:
            ws = self._camera_connect_map[camera_tag][user_tag].pop(cid)
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.close()
        except Exception as err:
            logger.error("关闭WebSocket出错: %s", err)
        
        if len(self._camera_connect_map[camera_tag][user_tag]) == 0:
            self._camera_connect_map[camera_tag].pop(user_tag, None)
        if len(self._camera_connect_map[camera_tag]) == 0:
            await manager.rtsp_camera_service.stop_video_stream(camera_id, channel)
            self._camera_connect_map.pop(camera_tag)
            logger.info("无连接，停止RTSP视频流, %s.%d", camera_id, channel)

    async def __video_stream_callback(
        self, did: str, data: bytes, ts: int, seq: int, channel: int
    ) -> None:
        """视频流回调"""
        camera_tag = f"{did}.{channel}"
        if camera_tag not in self._camera_connect_map:
            logger.error("无连接, %s.%d", did, channel)
            await manager.rtsp_camera_service.stop_video_stream(did, channel)
            return
        
        for conn in self._camera_connect_map[camera_tag].values():
            for ws in conn.values():
                try:
                    await ws.send_bytes(data)
                except Exception as err:
                    logger.error("WebSocket发送出错: %s", err)


rtsp_video_stream_manager = RTSPVideoStreamManager()


@router.websocket("/ws/video_stream")
async def rtsp_video_stream_websocket(
    websocket: WebSocket,
    camera_id: str,
    channel: int,
    current_user: str = Depends(verify_websocket_token)
):
    """RTSP视频流WebSocket端点"""
    logger.info("WebSocket连接请求, %s, %s.%d", current_user, camera_id, channel)
    start_time: datetime = datetime.now()
    token_hash: str = str(hash(websocket.cookies.get("access_token")))
    cid: Optional[str] = None
    
    try:
        await websocket.accept()
        cid = await rtsp_video_stream_manager.new_connection(
            websocket=websocket,
            user_name=current_user,
            token_hash=token_hash,
            camera_id=camera_id,
            channel=channel)
        
        while True:
            try:
                message = await websocket.receive_text()
                logger.info("收到客户端消息, %s", message)
            except Exception as err:
                logger.error("WebSocket错误: %s", err)
                break
    except WebSocketDisconnect:
        logger.warning("客户端断开连接, %s.%d", camera_id, channel)
    except Exception as err:
        logger.error("WebSocket错误, %s", err)
        await websocket.close(code=1011, reason=f"服务器错误: {str(err)}")
    finally:
        logger.info(
            "Websocket连接时长[%.2fs], %s.%d",
            (datetime.now() - start_time).total_seconds(), camera_id, channel)
        if cid:
            await rtsp_video_stream_manager.close_connection(
                user_name=current_user, token_hash=token_hash, camera_id=camera_id, channel=channel, cid=cid)
