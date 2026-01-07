# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MIoT schema module
Define MIoT device related data structures
"""

from typing import Optional

from pydantic import BaseModel, Field

from miloco_server.utils.media import image_manager
from miloco_server.utils.normal_util import bytes_to_base64
from miot.types import MIoTCameraInfo

class DeviceInfo(BaseModel):
    did: str = Field(..., description="Device ID")
    name: str = Field(..., description="Device name")
    online:bool = Field(False, description="Whether device is online")
    model: Optional[str] = Field(None, description="Device model")
    icon: Optional[str] = Field(None, description="Device icon URL")
    home_name: Optional[str] = Field(None, description="Home name")
    room_name: Optional[str] = Field(None, description="Room name")
    is_set_pincode: Optional[int] = Field(0, description="Whether PIN code is set")
    order_time: Optional[int] = Field(None, description="Binding time")

class CameraInfo(DeviceInfo):
    """Camera info"""
    channel_count: Optional[int] = Field(None, description="Camera channel count", ge=0)
    camera_status: Optional[str] = Field(None, description="Camera device status")
    source: Optional[str] = Field(None, description="Camera source type, e.g. miot or rtsp")

def choose_camera_list(camera_ids: list[str], camera_info_dict: dict[str, MIoTCameraInfo]) -> list[CameraInfo]:
    """Choose camera list"""
    camera_list = []
    for camera_id in camera_ids:
        camera_info = camera_info_dict.get(camera_id)
        if camera_info:
            camera_list.append(CameraInfo.model_validate(camera_info.model_dump()))
        else:
            camera_list.append(CameraInfo(
                did=camera_id, name="Unknown Camera", online=False,
                channel_count=0, camera_status=None, icon=None,
                home_name="Unknown Home", room_name="Unknown Room"))
    return camera_list

class CameraChannel(BaseModel):
    did: str = Field(..., description="Camera ID")
    channel: int = Field(..., description="Channel number", ge=0)


class SceneInfo(BaseModel):
    scene_id: str = Field(..., description="Scene ID", min_length=1)
    scene_name: str = Field(..., description="Scene name", min_length=1)


class CameraImgInfo(BaseModel):
    data: bytes = Field(..., description="Image byte stream")
    timestamp: int = Field(..., description="Timestamp (millisecond Unix timestamp)")

class CameraImgInfoBase64(CameraImgInfo):
    data: str = Field(..., description="Base64 encoded image")

class CameraImgInfoPath(CameraImgInfo):
    data: str = Field(..., description="Image path")

class CameraImgSeq(BaseModel):
    """Camera image sequence model"""
    camera_info: CameraInfo
    channel: int = Field(..., description="Channel number", ge=0)
    img_list: list[CameraImgInfo] = Field(..., description="Image list")

    def to_base64(self) -> "CameraImgBase64Seq":
        return CameraImgBase64Seq(
            camera_info=self.camera_info,
            channel=self.channel,
            img_list=[CameraImgInfoBase64(
                data=bytes_to_base64(img.data),
                timestamp=img.timestamp
            ) for img in self.img_list]
        )

    async def store_to_path(self) -> "CameraImgPathSeq":
        """Store images to file paths"""
        paths = await image_manager.save_image_list_async(
            self.camera_info.did,
            [img.data for img in self.img_list],
            self.channel
        )
        return CameraImgPathSeq(
            camera_info=self.camera_info,
            channel=self.channel,
            img_list=[CameraImgInfoPath(
                data=path,
                timestamp=img.timestamp
            ) for path, img in zip(paths, self.img_list)]
        )


class CameraImgBase64Seq(CameraImgSeq):
    img_list: list[CameraImgInfoBase64] = Field(...,
                                                description="Base64 encoded image list")


class CameraImgPathSeq(CameraImgSeq):
    img_list: list[CameraImgInfoPath] = Field(..., description="Image path list")

    async def delete_image_list_async(self) -> bool:
        image_name_list = [image.data for image in self.img_list]
        return await image_manager.delete_image_list_async(image_name_list)


class HAConfig(BaseModel):
    """Home Assistant configuration request"""
    base_url: str = Field(..., description="Home Assistant base URL", min_length=1)
    token: str = Field(..., description="Home Assistant access token", min_length=1)
