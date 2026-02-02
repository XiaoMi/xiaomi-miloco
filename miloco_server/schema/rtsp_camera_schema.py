# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
RTSP摄像头相关的数据模型定义
"""

from typing import Optional, List
from pydantic import BaseModel, Field


class RTSPCameraBase(BaseModel):
    """RTSP摄像头基础模型"""
    name: str = Field(..., description="摄像头名称")
    location: str = Field(default="", description="摄像头位置")
    rtsp_url_main: str = Field(..., description="主码流RTSP地址")
    rtsp_url_sub: Optional[str] = Field(default="", description="子码流RTSP地址（可选）")
    enabled: bool = Field(default=True, description="是否启用")


class RTSPCameraCreate(RTSPCameraBase):
    """创建RTSP摄像头请求模型"""
    pass


class RTSPCameraUpdate(BaseModel):
    """更新RTSP摄像头请求模型"""
    name: Optional[str] = Field(default=None, description="摄像头名称")
    location: Optional[str] = Field(default=None, description="摄像头位置")
    rtsp_url_main: Optional[str] = Field(default=None, description="主码流RTSP地址")
    rtsp_url_sub: Optional[str] = Field(default=None, description="子码流RTSP地址")
    enabled: Optional[bool] = Field(default=None, description="是否启用")


class RTSPCameraInfo(RTSPCameraBase):
    """RTSP摄像头信息响应模型"""
    id: str = Field(..., description="摄像头ID")
    online_main: bool = Field(default=False, description="主码流在线状态")
    online_sub: bool = Field(default=False, description="子码流在线状态")
    created_at: Optional[str] = Field(default=None, description="创建时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")
    
    # 兼容米家摄像头的字段
    did: Optional[str] = Field(default=None, description="设备ID（兼容米家）")
    model: str = Field(default="rtsp.camera.custom", description="设备型号")
    channel_count: int = Field(default=2, description="通道数量（主码流+子码流）")
    online: bool = Field(default=False, description="是否在线")
    camera_type: str = Field(default="rtsp", description="摄像头类型")

    class Config:
        from_attributes = True

    def __init__(self, **data):
        super().__init__(**data)
        # 设置did为id，兼容米家摄像头
        if self.did is None:
            self.did = self.id
        # 计算总体在线状态
        self.online = self.online_main or self.online_sub
        # 计算通道数量
        if self.rtsp_url_sub:
            self.channel_count = 2
        else:
            self.channel_count = 1


class RTSPCameraListResponse(BaseModel):
    """RTSP摄像头列表响应模型"""
    cameras: List[RTSPCameraInfo] = Field(default=[], description="摄像头列表")
    total: int = Field(default=0, description="总数量")


class RTSPCameraStatusCheck(BaseModel):
    """RTSP摄像头状态检查结果"""
    camera_id: str = Field(..., description="摄像头ID")
    channel: int = Field(..., description="通道号（0=主码流，1=子码流）")
    online: bool = Field(..., description="是否在线")
    error: Optional[str] = Field(default=None, description="错误信息")
