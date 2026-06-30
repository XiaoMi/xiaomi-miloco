# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT Type Definitions.
"""

from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class MIoTUserInfo(BaseModel):
    """MIoT User Info."""

    uid: str = Field(description="User id")
    nickname: str = Field(description="User nickname")
    icon: str = Field(description="User icon")

    union_id: str = Field(description="User OAuth2 union id")


class BaseOAuthInfo(BaseModel):
    """Base OAuth Info."""

    access_token: str = Field(description="OAuth2 access token")
    refresh_token: str = Field(description="OAuth2 refresh token")
    expires_ts: int = Field(description="OAuth2 access token expire time")


class MIoTOauthInfo(BaseOAuthInfo):
    """MIoT OAuth Info."""

    user_info: Optional[MIoTUserInfo] = Field(default=None, description="User info")


class MIoTRoomInfo(BaseModel):
    """MIoT Room Info."""

    room_id: str = Field(description="Room id")
    room_name: str = Field(description="Room name")
    # parent_id: str
    # Second
    create_ts: int = Field(description="Room create time")

    dids: List[str] = Field(description="Room device id list")


class MIoTHomeInfo(BaseModel):
    """MIoT Home Info."""

    home_id: str = Field(description="Home id")
    home_name: str = Field(description="Home name")
    # This is a share home
    share_home: bool = Field(description="Whether this is a share home")
    uid: str = Field(description="Home owner id")
    room_list: Dict[str, MIoTRoomInfo] = Field(description="Room list")
    # Second
    create_ts: int = Field(description="Home create time")
    dids: List[str] = Field(description="Home device id list")

    group_id: str = Field(description="Home group id")

    city_id: Optional[int] = Field(default=None, description="Home city id")
    longitude: Optional[float] = Field(default=None, description="Home longitude")
    latitude: Optional[float] = Field(default=None, description="Home latitude")
    address: Optional[str] = Field(default=None, description="Home address")


class MIoTCameraVideoQuality(int, Enum):
    """MIoT Camera Video Quality."""

    LOW = 1
    HIGH = 3


class MIoTCameraStatus(int, Enum):
    """MIoT Camera Video Status."""

    DISCONNECTED = 1
    # Connecting access MISS.
    CONNECTING = auto()
    RE_CONNECTING = auto()
    CONNECTED = auto()
    ERROR = auto()


class MIoTDeviceInfoCore(BaseModel):
    """MIoT Device Info Core."""

    did: str = Field(description="Device id")
    name: str = Field(description="Device name")


class MIoTDeviceInfo(BaseModel):
    """MIoT Device Info."""

    did: str = Field(description="Device id")
    name: str = Field(description="Device name")
    uid: str = Field(description="Device user id")
    urn: str = Field(description="Device urn")
    model: str = Field(description="Device model")
    manufacturer: str = Field(description="Device manufacturer")
    connect_type: int = Field(description="Device connect type")
    pid: int = Field(description="Device pid")
    token: str = Field(description="Device token")
    online: bool = Field(description="Device online status")
    voice_ctrl: int = Field(description="Device voice control status")
    # Device bind or share time.
    order_time: int = Field(description="Device bind or share time")
    sub_devices: Dict[str, "MIoTDeviceInfo"] = Field(
        default={}, description="Device sub devices"
    )
    is_set_pincode: int = Field(default=0, description="Device is set pincode")
    pincode_type: int = Field(default=0, description="Device pincode type")
    # Home information.
    home_id: Optional[str] = Field(default=None, description="Device home id")
    home_name: Optional[str] = Field(default=None, description="Device home name")
    room_id: Optional[str] = Field(default=None, description="Device room id")
    room_name: Optional[str] = Field(default=None, description="Device room name")

    rssi: Optional[int] = Field(default=None, description="Device rssi")
    lan_online: Optional[bool] = Field(
        default=None, description="LAN device online status"
    )
    local_ip: Optional[str] = Field(default=None, description="Device local ip")
    ssid: Optional[str] = Field(default=None, description="Device ssid")
    bssid: Optional[str] = Field(default=None, description="Device bssid")
    icon: Optional[str] = Field(default=None, description="Device icon")
    parent_id: Optional[str] = Field(default=None, description="Device parent id")
    # Owner information.
    owner_id: Optional[str] = Field(default=None, description="Device owner id")
    owner_nickname: Optional[str] = Field(
        default=None, description="Device owner nickname"
    )
    # Extra information.
    fw_version: Optional[str] = Field(
        default=None, description="Device firmware version"
    )
    mcu_version: Optional[str] = Field(default=None, description="Device mcu version")
    platform: Optional[str] = Field(default=None, description="Device platform")


class MIoTCameraInfo(MIoTDeviceInfo):
    """MIoT Camera Info, inherited from MIoTDeviceInfo."""

    channel_count: int = Field(description="Camera channel count")
    camera_status: MIoTCameraStatus = Field(description="Camera status")

    @property
    def connected(self) -> bool:
        """Whether the local camera stream is connected."""
        return self.camera_status == MIoTCameraStatus.CONNECTED


class MIoTLanDeviceInfo(BaseModel):
    """MIoT LAN Device Info."""

    did: str = Field(description="Device id")
    online: bool = Field(description="Device online status")
    ip: Optional[str] = Field(default=None, description="Device ip")


class MIoTManualSceneInfoCore(BaseModel):
    """MIoT Manual Scene Info Core."""

    scene_id: str = Field(description="Manual scene id")
    scene_name: str = Field(description="Manual scene name")


class MIoTManualSceneInfo(MIoTManualSceneInfoCore):
    """MIoT Manual Scene Info."""

    uid: str = Field(description="Manual scene user id")
    update_ts: int = Field(description="Manual scene update time")
    home_id: str = Field(description="Manual scene home id")

    room_id: Optional[str] = Field(default=None, description="Manual scene room id")
    icon: Optional[str] = Field(default=None, description="Manual scene icon")
    enable: Optional[bool] = Field(default=None, description="Manual scene status")
    dids: Optional[List[str]] = Field(
        default=None, description="Manual scene device id list"
    )
    pd_ids: Optional[List[int]] = Field(
        default=None, description="Manual scene pd id list"
    )


class MIoTAppNotify(BaseModel):
    """Xiaomi Home App Notify."""

    id_: str = Field(description="Notify id")
    text: str = Field(description="Notify content")
    create_ts: int = Field(description="Notify create time")


class InterfaceStatus(int, Enum):
    """Interface status."""

    ADD = 0
    UPDATE = auto()
    REMOVE = auto()


class NetworkInfo(BaseModel):
    """Network information."""

    name: str = Field(description="Network name")
    ip: str = Field(description="Network ip")
    netmask: str = Field(description="Network netmask")
    net_seg: str = Field(description="Network segment")


class MIoTSetPropertyParam(BaseModel):
    """MIoT Set Properties Params."""

    did: str = Field(description="Device id")
    siid: int = Field(description="Service instance id")
    piid: int = Field(description="Property instance id")
    value: Any = Field(description="Property value")


class MIoTGetPropertyParam(BaseModel):
    """MIoT Get Properties Params."""

    did: str = Field(description="Device id")
    siid: int = Field(description="Service instance id")
    piid: int = Field(description="Property instance id")


class MIoTEventParam(BaseModel):
    """MIoT Event Params."""

    did: str = Field(description="Device id")
    siid: int = Field(description="Service instance id")
    eiid: int = Field(description="Event instance id")


class MIoTActionParam(BaseModel):
    """MIoT Action Params."""

    did: str = Field(description="Device id")
    siid: int = Field(description="Service instance id")
    aiid: int = Field(description="Action instance id")
    # Dict[str, Any]
    in_: List[Any] = Field(serialization_alias="in", description="Action input")


class HAOAuthInfo(BaseOAuthInfo):
    """Home Assistant OAuth info."""


class HAStateInfo(BaseModel):
    """State info."""

    entity_id: str = Field(description="Entity id")
    domain: str = Field(description="Domain")
    state: str = Field(description="State")
    friendly_name: str = Field(description="Friendly name")
    last_changed: int = Field(default=0, description="Last changed time")
    last_reported: int = Field(default=0, description="Last reported time")
    last_updated: int = Field(default=0, description="Last updated time")

    attributes: Dict[str, Any] = Field(default={}, description="Attributes")
    context: Optional[Dict[str, Any]] = Field(default={}, description="Context")

    @field_validator("last_changed", "last_reported", "last_updated", mode="before")
    @classmethod
    def str_time2ts(cls, v):
        """Convert string time to timestamp."""
        if not isinstance(v, str):
            return 0
        try:
            return int(
                datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            return 0


class HADeviceInfo(HAStateInfo):
    """Home Assistant device info."""

    device_class: str = Field(description="Device class")


class HAAutomationInfo(HAStateInfo):
    """Automation info."""

    last_triggered: int = Field(default=0, description="Last triggered time")
    attr_id: str = Field(description="Attribute id")
    attr_mode: str = Field(description="Attribute mode")


class BaiduOAuthInfo(BaseOAuthInfo):
    """Baidu OAuth info."""


class MIoTCameraCodec(int, Enum):
    """MIoT Camera Codec ID."""

    VIDEO_H264 = 4
    VIDEO_HEVC = 5
    VIDEO_H265 = 5

    AUDIO_PCM = 1024
    AUDIO_G711U = 1026
    AUDIO_G711A = 1027
    AUDIO_OPUS = 1032


class MIoTCameraFrameType(int, Enum):
    """MIoT Camera Frame Type."""

    FRAME_P = 0  # P frame
    FRAME_I = 1  # I frame


class MIoTCameraFrameData(BaseModel):
    """MIoT Camera Frame.

    ``timestamp`` is the device-side PTS (ms) produced by the camera.
    ``recv_unix_ms`` is stamped at the host right when the raw frame is
    received from the network layer — used by downstream consumers to
    separate network latency (device PTS → host arrival) from decode
    latency (host arrival → decode completion).
    """

    codec_id: MIoTCameraCodec = Field(description="Codec id")
    length: int = Field(description="Frame length")
    timestamp: int = Field(description="Frame timestamp")
    sequence: int = Field(description="Frame sequence")
    frame_type: MIoTCameraFrameType = Field(description="Frame type")
    channel: int = Field(description="Frame channel")
    data: bytes = Field(description="Frame data")
    recv_unix_ms: int = Field(
        default=0,
        description="Host unix ms at the moment the raw frame arrived (pre-decode).",
    )


class MIoTCameraExtraItem(BaseModel):
    """MIoT Camera Extra Item."""

    channel_count: int = Field(description="Channel count")
    name: Optional[str] = Field(description="Extra item name")
    vendor: Optional[str] = Field(description="Vendor")


class MIoTCameraExtraInfo(BaseModel):
    """MIoT Camera Extra Info."""

    allow_classes: List[str] = Field(description="Allow classes")
    extra_info: Dict[str, MIoTCameraExtraItem] = Field(description="Extra info")
    allowlist: Dict[str, Dict[str, Dict]] = Field(description="Allowlist")
    denylist: Dict[str, Dict[str, Dict]] = Field(description="Denylist")


# ---------------------------------------------------------------------------
# MIoT MQTT cloud (mips_cloud) message types & errors
# ---------------------------------------------------------------------------


class MIoTDeviceBindEvent(BaseModel):
    """Decoded g_op push payload (account- or device-scoped).

    `event` is one of:
      * bind / unbind  — account-level `user/{uid}/g_op/{bind,unbind}`
        (device added / removed).
      * rename / hr_change — device-level `device/{did}/g_op/{rename,hr_change}`
        (name change / home+room reassignment).
    All share the same shape — `did` plus an undocumented remainder in `raw`.
    For device-level events `uid` may be empty (the topic carries did, not uid).
    """

    uid: str = Field(default="", description="Account uid (empty for device-level)")
    event: Literal["bind", "unbind", "rename", "hr_change"] = Field(
        description="g_op operation: bind / unbind / rename / hr_change"
    )
    did: Optional[str] = Field(default=None, description="Device id if present")
    raw: Dict[str, Any] = Field(
        default_factory=dict,
        description="Raw decoded payload (broker schema is undocumented)",
    )
    timestamp_ms: int = Field(default=0)


class MIoTSceneChangedEvent(BaseModel):
    """Decoded `home/{home_id}/scene/{rename,delete,edit}` payload.

    Home-scoped scene change. `home_id` and `event` come from the topic; the
    payload is undocumented and kept in `raw` (scene_id extracted if present).
    The new scene list is fetched authoritatively via refresh_scenes, so the
    handler typically just refreshes.
    """

    home_id: str = Field(description="Home id")
    event: Literal["rename", "delete", "edit"] = Field(
        description="Scene op: rename / delete / edit"
    )
    scene_id: Optional[str] = Field(default=None, description="Scene id if present")
    raw: Dict[str, Any] = Field(
        default_factory=dict,
        description="Raw decoded payload (broker schema is undocumented)",
    )
    timestamp_ms: int = Field(default=0)


class MIoTDeviceStateEvent(BaseModel):
    """Decoded `device/{did}/state/{online,offline}` payload.

    Device-level cloud online/offline state push. `did` and `event` come from
    the topic; the payload is undocumented and kept in `raw`. The handler
    updates the cached cloud ``online`` field directly from ``event`` rather
    than re-fetching the device list — this is the event-driven recovery path
    for cameras that went stale (online=false) across a backend restart.
    """

    did: str = Field(description="Device id")
    event: Literal["online", "offline"] = Field(
        description="Cloud state: online / offline"
    )
    raw: Dict[str, Any] = Field(
        default_factory=dict,
        description="Raw decoded payload (broker schema is undocumented)",
    )
    timestamp_ms: int = Field(default=0)


class MipsConnectionError(Exception):
    """MIPS cloud client failed to connect."""


class MipsSubscribeRejectedError(Exception):
    """Broker returned a non-success reason code in SUBACK.

    MQTT v5 reason codes that show up here include:
      0x87 — Not authorized (ACL refused the topic)
      0x97 — Quota exceeded
      0x8F — Topic filter invalid
      0xA1 — Subscription identifiers not supported
      0xA2 — Wildcard subscriptions not supported
    """

    def __init__(self, topic: str, reason_code: int, reason_string: str = "") -> None:
        self.topic = topic
        self.reason_code = reason_code
        self.reason_string = reason_string or f"reason_code=0x{reason_code:02x}"
        super().__init__(f"{topic}: {self.reason_string}")


class MipsSubscribeTimeoutError(Exception):
    """SUBACK was not received within MIHOME_MQTT_SUBSCRIBE_TIMEOUT."""

    def __init__(self, topic: str) -> None:
        self.topic = topic
        super().__init__(f"{topic}: SUBACK timeout")
