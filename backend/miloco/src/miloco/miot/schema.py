# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MIoT schema module
Define MIoT device related data structures
"""

from __future__ import annotations

from typing import Any, Literal

from miot.types import MIoTCameraInfo, MIoTCameraStatus
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from miloco.utils.media import image_bytes_to_base64, image_manager


def normalize_sub_devices(
    raw: dict | None, parent_name: str | None
) -> dict[str, str]:
    """Normalize a MIoT sub-device container into {siid: user_alias}.

    Accepts both value shapes:
      - MIoTDeviceInfo objects (raw ``MIoTDeviceInfo.sub_devices``), or
      - plain dicts (after ``model_dump()``).
    The siid key drops its 's' prefix and must be numeric; the alias strips
    the parent device name suffix (e.g. "三楼书房-客厅多路开关" → "三楼书房")
    so callers consistently see the user-customized portion only.
    """
    if not raw:
        return {}
    suffix = f"-{parent_name}" if parent_name else ""
    result: dict[str, str] = {}
    for key, sub in raw.items():
        siid = key.lstrip("s")
        if not siid.isdigit():
            continue
        name = sub["name"] if isinstance(sub, dict) else sub.name
        if not isinstance(name, str):
            continue
        if suffix and name.endswith(suffix):
            name = name[: -len(suffix)]
        result[siid] = name
    return result


class DeviceInfo(BaseModel):
    did: str = Field(..., description="Device ID")
    # NOTE: keep ``name`` declared before ``sub_devices`` — the latter's
    # before-validator reads ``info.data["name"]`` to strip the parent-name
    # suffix from sub-device aliases. Pydantic only exposes already-validated
    # fields in ``info.data``, so reordering would silently make the strip a
    # no-op (aliases keep the redundant "-<device name>" suffix; no error).
    name: str = Field(..., description="Device name")
    online: bool = Field(False, description="Whether device is online")
    model: str | None = Field(None, description="Device model")
    icon: str | None = Field(None, description="Device icon URL")
    home_id: str | None = Field(None, description="Home id")
    home_name: str | None = Field(None, description="Home name")
    room_name: str | None = Field(None, description="Room name")
    is_set_pincode: int | None = Field(0, description="Whether PIN code is set")
    order_time: int | None = Field(None, description="Binding time")
    lan_online: bool | None = Field(None, description="Whether device is reachable on LAN")
    local_ip: str | None = Field(None, description="Device LAN IP address")
    sub_devices: dict[str, str] | None = Field(
        None, description="Sub-device custom names keyed by siid (e.g. {'3': '三楼书房'})"
    )

    @field_validator("sub_devices", mode="before")
    @classmethod
    def _coerce_sub_devices(cls, v: Any, info: ValidationInfo) -> Any:
        # None / empty → None (consistent with the `... or None` convention
        # used by build_sub_device_names callers, e.g. service.py).
        if not v:
            return None
        # Already {siid: str} (e.g. service.py pre-converts via
        # build_sub_device_names) → pass through; don't re-strip the suffix.
        if all(isinstance(x, str) for x in v.values()):
            return v
        # dict-of-dict from MIoT*.model_dump() → normalize. ``name`` is
        # declared before ``sub_devices`` so it's already validated here.
        return normalize_sub_devices(v, info.data.get("name")) or None


class CameraInfo(DeviceInfo):
    """Camera info"""

    channel_count: int | None = Field(None, description="Camera channel count", ge=0)
    camera_status: str | None = Field(None, description="Camera device status")

    @property
    def connected(self) -> bool:
        """Whether the local camera stream is connected."""
        return self.camera_status == str(MIoTCameraStatus.CONNECTED.value)


def choose_camera_list(
    camera_ids: list[str], camera_info_dict: dict[str, MIoTCameraInfo]
) -> list[CameraInfo]:
    """Choose camera list"""
    camera_list = []
    for camera_id in camera_ids:
        camera_info = camera_info_dict.get(camera_id)
        if camera_info:
            camera_list.append(CameraInfo.model_validate(camera_info.model_dump()))
        else:
            camera_list.append(
                CameraInfo(
                    did=camera_id,
                    name="Unknown Camera",
                    online=False,
                    channel_count=0,
                    camera_status=None,
                    icon=None,
                    home_name="Unknown Home",
                    room_name="Unknown Room",
                )
            )
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

    def to_base64(self) -> CameraImgBase64Seq:
        return CameraImgBase64Seq(
            camera_info=self.camera_info,
            channel=self.channel,
            img_list=[
                CameraImgInfoBase64(
                    data=image_bytes_to_base64(img.data), timestamp=img.timestamp
                )
                for img in self.img_list
            ],
        )

    async def store_to_path(self) -> CameraImgPathSeq:
        """Store images to file paths"""
        paths = await image_manager.save_image_list_async(
            self.camera_info.did, [img.data for img in self.img_list], self.channel
        )
        return CameraImgPathSeq(
            camera_info=self.camera_info,
            channel=self.channel,
            img_list=[
                CameraImgInfoPath(data=path, timestamp=img.timestamp)
                for path, img in zip(paths, self.img_list)
            ],
        )


class CameraImgBase64Seq(CameraImgSeq):
    img_list: list[CameraImgInfoBase64] = Field(
        ..., description="Base64 encoded image list"
    )


class CameraImgPathSeq(CameraImgSeq):
    img_list: list[CameraImgInfoPath] = Field(..., description="Image path list")

    async def delete_image_list_async(self) -> bool:
        image_name_list = [image.data for image in self.img_list]
        return await image_manager.delete_image_list_async(image_name_list)


class HAConfig(BaseModel):
    """Home Assistant configuration request"""

    base_url: str = Field(..., description="Home Assistant base URL", min_length=1)
    token: str = Field(..., description="Home Assistant access token", min_length=1)


class PropertyItem(BaseModel):
    iid: str = Field(..., description="Property IID, format: prop.{siid}.{piid}")
    value: Any = Field(..., description="Property value")


class DeviceControlRequest(BaseModel):
    type: Literal["set_property", "set_properties", "call_action"] = Field(
        ..., description="Control type"
    )
    iid: str | None = Field(
        None, description="IID for single set_property or call_action"
    )
    value: Any = Field(None, description="Value for set_property")
    properties: list[PropertyItem] | None = Field(
        None, description="Properties list for set_properties"
    )
    params: list[Any] | None = Field(None, description="Input params for call_action")


class SendNotifyRequest(BaseModel):
    notify: str = Field(..., description="Notification text", min_length=1)


class HomeSwitchRequest(BaseModel):
    """切换到指定家庭（唯一启用），其余自动停用。"""

    home_id: str = Field(..., min_length=1, description="要切换到的家庭 ID")


class CameraToggleItem(BaseModel):
    """单个相机的启用/停用操作。"""

    did: str = Field(..., min_length=1, description="相机 did")
    in_use: bool = Field(
        ..., description="true = 启用（恢复接入）；false = 停用（不接入）"
    )


class CameraToggleRequest(BaseModel):
    """批量切换相机启用状态。每项独立指定 did + in_use。"""

    items: list[CameraToggleItem] = Field(..., min_length=1)


class CameraVoiceToggleItem(BaseModel):
    """单个相机的拾音开/关操作（mic-off 语义）。"""

    did: str = Field(..., min_length=1, description="相机 did")
    voice_in_use: bool = Field(
        ...,
        description=(
            "true = 开启拾音；false = 关闭（该相机声音完全不被处理："
            "引擎入口剥离音频，不转写、不上云、语音指令不 dispatch）"
        ),
    )


class CameraVoiceToggleRequest(BaseModel):
    """批量切换相机拾音状态。每项独立指定 did + voice_in_use。

    拾音开关从属于感知开关：仅允许对感知已启用(in_use=True)的相机设置，否则整批拒绝。
    """

    items: list[CameraVoiceToggleItem] = Field(..., min_length=1)


class AuthorizeRequest(BaseModel):
    """User-pasted OAuth result from the Xiaomi redirect page."""

    code: str = Field(..., description="OAuth authorization code", min_length=1)
    state: str = Field(..., description="OAuth state token", min_length=1)


class MipsStatusResponse(BaseModel):
    """Cloud MQTT (mips_cloud) subscription status snapshot.

    Used by /api/miot/mips_status to verify whether real-time device-bind
    detection is active. When `user_bind_subscribed` is False, `last_error`
    explains why — typically broker ACL rejection of `user/{uid}/g_op/*`.
    """

    connected: bool = Field(
        ..., description="Whether mips_cloud MQTT client is currently connected"
    )
    user_bind_subscribed: bool = Field(
        ...,
        description=(
            "Whether the account-level bind/unbind topic subscription is "
            "currently believed to be active (connected AND no last_error)"
        ),
    )
    last_error: str | None = Field(
        None,
        description=(
            "Last user-level subscribe failure, e.g. broker ACL rejection. "
            "None means subscribe is healthy."
        ),
    )
