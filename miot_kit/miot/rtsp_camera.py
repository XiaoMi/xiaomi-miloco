# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
RTSP Camera support.
This module defines the Python-side interface expected from the native `libcamera_rtsp` library.
The native library should provide capabilities similar to `libmiot_camera_lite.so`, including
status callbacks and raw frame callbacks for decoding.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import time
from ctypes import (
    CDLL,
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    string_at,
    c_bool,
    c_char_p,
    c_int,
    c_uint8,
    c_uint32,
    c_uint64,
    c_void_p,
)
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from pydantic import BaseModel, Field

from .const import CAMERA_RECONNECT_TIME_MAX, CAMERA_RECONNECT_TIME_MIN
from .decoder import MIoTMediaDecoder
from .error import MIoTCameraError
from .types import MIoTCameraCodec, MIoTCameraFrameData, MIoTCameraFrameType, MIoTCameraStatus

_LOGGER = logging.getLogger(__name__)


# level, msg
_RTSP_CAMERA_LOG_HANDLER = CFUNCTYPE(None, c_int, c_char_p)
# camera pointer, status
_RTSP_CAMERA_ON_STATUS_CHANGED = CFUNCTYPE(None, c_int)

rtsp_lib_cache: Optional[CDLL] = None

class RtspCameraInfo(BaseModel):
    """RTSP camera information and runtime state."""

    did: str = Field(description="Camera id")
    name: str = Field(description="Camera name")
    rtsp_url: str = Field(description="RTSP url")
    codec: Optional[MIoTCameraCodec] = Field(default=None, description="Video codec (auto-detect when not specified)")
    channel_count: int = Field(default=1, description="Channel count")
    enable_audio: bool = Field(default=False, description="Enable audio decoding")
    use_tcp: bool = Field(default=False, description="Force RTSP SETUP over TCP instead of UDP")

    online: bool = Field(default=True, description="Online status")
    camera_status: MIoTCameraStatus = Field(default=MIoTCameraStatus.DISCONNECTED, description="Camera status")
    model: str = Field(default="rtsp_camera", description="Camera model for display")
    icon: Optional[str] = Field(default=None, description="Icon url/path")
    home_name: Optional[str] = Field(default=None, description="Home/area name")
    room_name: Optional[str] = Field(default=None, description="Room name")
    is_set_pincode: int = Field(default=0, description="Placeholder for compatibility")
    order_time: int = Field(default_factory=lambda: int(time.time()), description="Bind time for compatibility")
    vendor: Optional[str] = Field(default=None, description="Vendor/brand name")
    source: str = Field(default="rtsp", description="Camera source marker")


class _RTSPCameraFrameHeaderC(Structure):
    """Raw frame header from libcamera_rtsp."""

    _fields_ = [
        ("codec_id", c_uint32),
        ("length", c_uint32),
        ("timestamp", c_uint64),
        ("sequence", c_uint32),
        ("frame_type", c_uint32),
        ("channel", c_uint8),
    ]


_RTSP_CAMERA_ON_RAW_DATA = CFUNCTYPE(None, POINTER(_RTSPCameraFrameHeaderC), POINTER(c_uint8))


class _RTSPCameraInfoC(Structure):
    """RTSP camera info passed to native lib."""

    _fields_ = [
        ("did", c_char_p),
        ("name", c_char_p),
        ("url", c_char_p),
        ("codec_id", c_uint32),
        ("channel_count", c_uint8),
    ]


class _RTSPCameraConfigC(Structure):
    """RTSP camera start config passed to native lib."""

    _fields_ = [
        ("url", c_char_p),
        ("enable_audio", c_bool),
        ("force_tcp", c_bool),
    ]


class _RTSPCameraInstanceC(c_void_p):
    """RTSP camera native instance pointer."""


class RTSPCameraInstance:
    """RTSP Camera Instance wrapping libcamera_rtsp."""

    def __init__(
        self,
        manager: "RTSPCamera",
        frame_interval: int,
        enable_hw_accel: bool,
        camera_info: RtspCameraInfo,
        main_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._manager = manager
        self._main_loop = main_loop or asyncio.get_event_loop()
        self._lib_rtsp_camera = manager.lib_rtsp_camera
        self._camera_info = camera_info
        self._frame_interval = frame_interval
        self._enable_hw_accel = enable_hw_accel
        self._callback_refs: Dict[str, Callable] = {}

        self._rtsp_url: str = camera_info.rtsp_url
        self._enable_audio: bool = camera_info.enable_audio
        self._use_tcp: bool = camera_info.use_tcp
        self._enable_reconnect: bool = False
        self._callbacks: Dict[str, Dict[str, Callable[..., Coroutine]]] = {}

        self._reconnect_timer: Optional[asyncio.TimerHandle] = None
        self._reconnect_timeout: int = CAMERA_RECONNECT_TIME_MIN
        self._debug_raw_count: int = 0

        self._decoders: List[MIoTMediaDecoder] = []

        self._c_instance = self._lib_rtsp_camera.camera_rtsp_new(
            byref(
                _RTSPCameraInfoC(
                    camera_info.did.encode("utf-8"),
                    camera_info.name.encode("utf-8"),
                    self._rtsp_url.encode("utf-8"),
                    int(camera_info.codec) if camera_info.codec is not None else 0,
                    camera_info.channel_count,
                )
            )
        )
        if not self._c_instance:
            raise MIoTCameraError("create rtsp camera failed")

        _LOGGER.info("rtsp camera inited, %s, %s", camera_info.did, camera_info.name)

    @property
    def camera_info(self) -> RtspCameraInfo:
        """Camera info."""
        return self._camera_info

    async def destroy_async(self) -> None:
        """Destroy camera."""
        await self.stop_async()
        for key in list(self._callback_refs.keys()):
            if key == "status":
                self._lib_rtsp_camera.camera_rtsp_unregister_status_changed(self._c_instance)
            elif key.startswith("r"):
                self._lib_rtsp_camera.camera_rtsp_unregister_raw_data(self._c_instance, int(key.replace("r", "")))
        self._lib_rtsp_camera.camera_rtsp_free(self._c_instance)
        self._callback_refs.clear()
        self._callbacks.clear()

    async def start_async(
        self,
        enable_audio: bool = False,
        enable_reconnect: bool = False,
    ) -> None:
        """Start camera."""
        channel_count: int = self._camera_info.channel_count or 1
        self._enable_audio = enable_audio
        self._enable_reconnect = enable_reconnect
        # pessimistically mark offline until connected
        self._camera_info.camera_status = MIoTCameraStatus.DISCONNECTED
        self._camera_info.online = False

        # Init decoders
        for _ in range(channel_count):
            decoder = MIoTMediaDecoder(
                frame_interval=self._frame_interval,
                video_callback=self.__on_video_decode_callback,
                audio_callback=self.__on_audio_decode_callback,
                enable_hw_accel=self._enable_hw_accel,
                enable_audio=self._enable_audio,
                main_loop=self._main_loop,
            )
            self._decoders.append(decoder)
            decoder.daemon = True
            decoder.start()

        # Register status callback.
        c_callback = _RTSP_CAMERA_ON_STATUS_CHANGED(self.__on_status_changed)
        result: int = self._lib_rtsp_camera.camera_rtsp_register_status_changed(self._c_instance, c_callback)
        self._callback_refs["status"] = c_callback
        _LOGGER.info("register rtsp status changed, %s, %s", self._camera_info.did, result)

        self._reconnect_timer = self._main_loop.call_later(
            0, lambda: self._main_loop.create_task(self.__try_start_async())
        )

    async def stop_async(self) -> None:
        """Stop camera."""
        self._enable_reconnect = False
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
            self.__reset_try_start_timeout()

        result: int = await self._main_loop.run_in_executor(
            None, self._lib_rtsp_camera.camera_rtsp_stop, self._c_instance
        )
        for decoder in self._decoders:
            decoder.stop()
        self._decoders.clear()
        _LOGGER.info("rtsp camera stop, %s, %s", self._camera_info.did, result)

    async def get_status_async(self) -> MIoTCameraStatus:
        """Get camera status."""
        result: int = await self._main_loop.run_in_executor(
            None, self._lib_rtsp_camera.camera_rtsp_status, self._c_instance
        )
        _LOGGER.info("rtsp camera status, %s, %s", self._camera_info.did, result)
        return MIoTCameraStatus(result)

    async def register_status_changed_async(
        self, callback: Callable[[str, MIoTCameraStatus], Coroutine], multi_reg: bool = False
    ) -> int:
        """Register camera status changed callback."""
        self._callbacks.setdefault("status", {})
        reg_id: int = 0
        if multi_reg:
            reg_id = len(self._callbacks["status"])
        self._callbacks["status"][str(reg_id)] = callback
        return reg_id

    async def unregister_status_changed_async(self, reg_id: int = 0) -> None:
        """Unregister camera status changed callback."""
        if "status" not in self._callbacks:
            return
        self._callbacks["status"].pop(str(reg_id), None)

    async def register_raw_video_async(
        self, callback: Callable[[str, bytes, int, int, int], Coroutine], channel: int = 0, multi_reg: bool = False
    ) -> int:
        """Register camera raw stream callback."""
        await self.__update_raw_data_register_status_async(channel=channel)
        reg_key: str = f"raw_video.{channel}"
        self._callbacks.setdefault(reg_key, {})
        reg_id: int = 0
        if multi_reg:
            reg_id = len(self._callbacks[reg_key])
        self._callbacks[reg_key][str(reg_id)] = callback
        return reg_id

    async def unregister_raw_video_async(self, channel: int = 0, reg_id: int = 0) -> None:
        """Unregister camera raw stream callback."""
        reg_key: str = f"raw_video.{channel}"
        if reg_key not in self._callbacks:
            return
        self._callbacks[reg_key].pop(str(reg_id), None)
        await self.__update_raw_data_register_status_async(channel=channel, is_register=False)

    async def register_raw_audio_async(
        self, callback: Callable[[str, bytes, int, int, int], Coroutine], channel: int = 0, multi_reg: bool = False
    ) -> int:
        """Register camera raw audio callback."""
        await self.__update_raw_data_register_status_async(channel=channel)
        reg_key: str = f"raw_audio.{channel}"
        self._callbacks.setdefault(reg_key, {})
        reg_id: int = 0
        if multi_reg:
            reg_id = len(self._callbacks) + 1
        self._callbacks[reg_key][str(reg_id)] = callback
        return reg_id

    async def unregister_raw_audio_async(self, channel: int = 0, reg_id: int = 0) -> None:
        """Unregister camera raw audio callback."""
        reg_key: str = f"raw_audio.{channel}"
        if reg_key not in self._callbacks:
            return
        self._callbacks[reg_key].pop(str(reg_id), None)
        await self.__update_raw_data_register_status_async(channel=channel, is_register=False)

    async def register_decode_jpg_async(
        self, callback: Callable[[str, bytes, int, int], Coroutine], channel: int = 0, multi_reg: bool = False
    ) -> int:
        """Register camera decode jpg callback."""
        await self.__update_raw_data_register_status_async(channel=channel)
        reg_key: str = f"decode_jpg.{channel}"
        self._callbacks.setdefault(reg_key, {})
        reg_id: int = 0
        if multi_reg:
            reg_id = len(self._callbacks) + 1
        self._callbacks[reg_key][str(reg_id)] = callback
        return reg_id

    async def unregister_decode_jpg_async(self, channel: int = 0, reg_id: int = 0) -> None:
        """Unregister camera decode jpg callback."""
        await self.__update_raw_data_register_status_async(channel=channel, is_register=False)
        reg_key: str = f"decode_jpg.{channel}"
        if reg_key not in self._callbacks:
            return
        self._callbacks[reg_key].pop(str(reg_id), None)

    async def register_decode_pcm_async(
        self, callback: Callable[[str, bytes, int, int], Coroutine], channel: int = 0, multi_reg: bool = False
    ) -> int:
        """Register camera decode pcm callback."""
        await self.__update_raw_data_register_status_async(channel=channel)
        reg_key: str = f"decode_pcm.{channel}"
        self._callbacks.setdefault(reg_key, {})
        reg_id: int = 0
        if multi_reg:
            reg_id = len(self._callbacks) + 1
        self._callbacks[reg_key][str(reg_id)] = callback
        return reg_id

    async def unregister_decode_pcm_async(self, channel: int = 0, reg_id: int = 0) -> None:
        """Unregister camera decode pcm callback."""
        await self.__update_raw_data_register_status_async(channel=channel, is_register=False)
        reg_key: str = f"decode_pcm.{channel}"
        if reg_key not in self._callbacks:
            return
        self._callbacks[reg_key].pop(str(reg_id), None)

    async def __register_raw_data_async(self, channel: int = 0) -> None:
        """Register raw data callback."""
        if channel < 0 or channel >= self._camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", self._camera_info.did, channel)
            raise MIoTCameraError(f"invalid channel, {self._camera_info.did}, {channel}")

        c_callback = _RTSP_CAMERA_ON_RAW_DATA(self.__on_raw_data)
        result: int = self._lib_rtsp_camera.camera_rtsp_register_raw_data(self._c_instance, c_callback, channel)
        self._callback_refs[f"r{channel}"] = c_callback
        _LOGGER.info("register rtsp raw data, %s, %s, %s", self._camera_info.did, channel, result)

    async def __unregister_raw_data_async(self, channel: int = 0) -> None:
        """Unregister raw data callback."""
        if channel < 0 or channel >= self._camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", self._camera_info.did, channel)
            raise MIoTCameraError(f"invalid channel, {self._camera_info.did}, {channel}")

        result: int = self._lib_rtsp_camera.camera_rtsp_unregister_raw_data(self._c_instance, channel)
        self._callback_refs.pop(f"r{channel}", None)
        _LOGGER.info("unregister rtsp raw data, %s, %s, %s", self._camera_info.did, channel, result)

    async def __update_raw_data_register_status_async(self, channel: int, is_register: bool = True) -> None:
        """Update raw data register status."""
        reg_key: str = f"r{channel}"
        if is_register and reg_key not in self._callback_refs:
            await self.__register_raw_data_async(channel)
        elif not is_register:
            need_unreg: bool = True
            if len(self._callbacks.get(f"raw_video.{channel}", {})) > 0:
                need_unreg = False
            if len(self._callbacks.get(f"raw_audio.{channel}", {})) > 0:
                need_unreg = False
            if len(self._callbacks.get(f"decode_jpg.{channel}", {})) > 0:
                need_unreg = False
            if len(self._callbacks.get(f"decode_pcm.{channel}", {})) > 0:
                need_unreg = False
            if need_unreg:
                await self.__unregister_raw_data_async(channel)

    async def __try_start_async(self) -> None:
        _LOGGER.info("try start rtsp camera, %s", self._camera_info.did)
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

        result: int = await self._main_loop.run_in_executor(
            None,
            self._lib_rtsp_camera.camera_rtsp_start,
            self._c_instance,
            byref(_RTSPCameraConfigC(self._rtsp_url.encode("utf-8"), self._enable_audio, self._use_tcp)),
        )
        _LOGGER.info(
            "try start rtsp camera, result->%s, did->%s, enable_audio->%s, enable_reconnect->%s",
            result,
            self._camera_info.did,
            self._enable_audio,
            self._enable_reconnect,
        )
        if result == 0:
            # Mark connecting so UI reflects progressing state before first frame/status arrives.
            self._camera_info.camera_status = MIoTCameraStatus.CONNECTING
            self._camera_info.online = True
            s_callbacks = self._callbacks.get("status", {})
            for callback in s_callbacks.values():
                asyncio.run_coroutine_threadsafe(
                    callback(self._camera_info.did, MIoTCameraStatus.CONNECTING),
                    self._main_loop
                )
            self.__reset_try_start_timeout()
            return

        # Mark offline immediately on failure
        self._camera_info.camera_status = MIoTCameraStatus.DISCONNECTED
        self._camera_info.online = False
        s_callbacks = self._callbacks.get("status", {})
        for callback in s_callbacks.values():
            asyncio.run_coroutine_threadsafe(
                callback(self._camera_info.did, MIoTCameraStatus.DISCONNECTED),
                self._main_loop,
            )

        if self._enable_reconnect:
            self._reconnect_timer = self._main_loop.call_later(
                self.__get_try_start_timeout(), lambda: self._main_loop.create_task(self.__try_start_async())
            )
        else:
            _LOGGER.error("rtsp camera start failed, %s, %s", self._camera_info.did, result)
            raise MIoTCameraError(f"rtsp camera start failed, {self._camera_info.did}, {result}")

    def __get_try_start_timeout(self) -> int:
        self._reconnect_timeout = min(self._reconnect_timeout * 2, CAMERA_RECONNECT_TIME_MAX)
        _LOGGER.info("get rtsp reconnect timeout, %s, %s", self._camera_info.did, self._reconnect_timeout)
        return self._reconnect_timeout

    def __reset_try_start_timeout(self) -> None:
        self._reconnect_timeout = CAMERA_RECONNECT_TIME_MIN
        _LOGGER.info("reset rtsp reconnect timeout, %s, %s", self._camera_info.did, self._reconnect_timeout)

    def __on_status_changed(self, status: int) -> None:
        """Callback for status changed."""
        camera_status: MIoTCameraStatus = MIoTCameraStatus(status)
        self._camera_info.camera_status = camera_status
        self._camera_info.online = camera_status in (
            MIoTCameraStatus.CONNECTED,
            MIoTCameraStatus.CONNECTING,
            MIoTCameraStatus.RE_CONNECTING,
        )
        s_callbacks = self._callbacks.get("status", {})
        for callback in s_callbacks.values():
            asyncio.run_coroutine_threadsafe(callback(self._camera_info.did, camera_status), self._main_loop)
        if camera_status == MIoTCameraStatus.DISCONNECTED and self._enable_reconnect:
            self._reconnect_timer = self._main_loop.call_later(
                self.__get_try_start_timeout(), lambda: self._main_loop.create_task(self.__try_start_async())
            )

    def __on_raw_data(self, frame_header_ptr: Any, data: bytes) -> None:
        """Callback for raw data."""
        frame_header: _RTSPCameraFrameHeaderC = frame_header_ptr.contents
        codec_id: MIoTCameraCodec = MIoTCameraCodec(frame_header.codec_id)
        channel: int = frame_header.channel
        # Keep runtime codec synced with actual stream for downstream consumers.
        if self._camera_info.codec != codec_id:
            self._camera_info.codec = codec_id
        if self._debug_raw_count < 3:
            _LOGGER.info(
                "rtsp raw frame #%s, did=%s, codec=%s, len=%s, ts=%s, type=%s",
                self._debug_raw_count,
                self._camera_info.did,
                codec_id.name,
                frame_header.length,
                frame_header.timestamp,
                frame_header.frame_type,
            )
            self._debug_raw_count += 1
        # Mark connected on first frame to keep UI status accurate when native lib does not emit status.
        if self._camera_info.camera_status != MIoTCameraStatus.CONNECTED:
            self._camera_info.camera_status = MIoTCameraStatus.CONNECTED
            self._camera_info.online = True
            s_callbacks = self._callbacks.get("status", {})
            for callback in s_callbacks.values():
                asyncio.run_coroutine_threadsafe(
                    callback(self._camera_info.did, MIoTCameraStatus.CONNECTED),
                    self._main_loop
                )
        frame_data = MIoTCameraFrameData(
            codec_id=codec_id,
            length=frame_header.length,
            timestamp=frame_header.timestamp,
            sequence=frame_header.sequence,
            frame_type=MIoTCameraFrameType(frame_header.frame_type),
            channel=channel,
            data=string_at(data, frame_header.length),
        )
        if codec_id in [MIoTCameraCodec.VIDEO_H264, MIoTCameraCodec.VIDEO_H265]:
            if self._callbacks.get(f"decode_jpg.{channel}", None):
                self._decoders[channel].push_video_frame(frame_data)
            v_callbacks = self._callbacks.get(f"raw_video.{channel}", {})
            for v_callback in list(v_callbacks.values()):
                asyncio.run_coroutine_threadsafe(
                    v_callback(
                        self._camera_info.did,
                        frame_data.data,
                        frame_data.timestamp,
                        frame_data.sequence,
                        channel,
                    ),
                    self._main_loop,
                )
        elif codec_id in [MIoTCameraCodec.AUDIO_OPUS, MIoTCameraCodec.AUDIO_G711A, MIoTCameraCodec.AUDIO_G711U]:
            if self._callbacks.get(f"decode_pcm.{channel}", None):
                self._decoders[channel].push_audio_frame(frame_data)
            a_callbacks = self._callbacks.get(f"raw_audio.{channel}", {})
            for a_callback in list(a_callbacks.values()):
                asyncio.run_coroutine_threadsafe(
                    a_callback(
                        self._camera_info.did,
                        frame_data.data,
                        frame_data.timestamp,
                        frame_data.sequence,
                        channel,
                    ),
                    self._main_loop,
                )
        else:
            _LOGGER.error("unknown rtsp codec, %s, %s, %s", self._camera_info.did, codec_id, frame_header.timestamp)

    async def __on_video_decode_callback(self, data: bytes, timestamp: int, channel: int) -> None:
        """On video decode callback."""
        v_callbacks = self._callbacks.get(f"decode_jpg.{channel}", {})
        for callback in list(v_callbacks.values()):
            asyncio.run_coroutine_threadsafe(callback(self._camera_info.did, data, timestamp, channel), self._main_loop)

    async def __on_audio_decode_callback(self, data: bytes, timestamp: int, channel: int) -> None:
        """On audio decode callback."""
        a_callbacks = self._callbacks.get(f"decode_pcm.{channel}", {})
        for callback in list(a_callbacks.values()):
            asyncio.run_coroutine_threadsafe(callback(self._camera_info.did, data, timestamp, channel), self._main_loop)


def _load_rtsp_dynamic_lib() -> CDLL:
    global rtsp_lib_cache
    if rtsp_lib_cache is not None:
        return rtsp_lib_cache

    system = platform.system().lower()
    machine = platform.machine().lower()
    lib_path = Path(__file__).parent / "libs"

    if system == "linux":
        if machine in ("x86_64", "amd64"):
            lib_path = lib_path / system / "x86_64"
        elif machine in ("arm64", "aarch64"):
            lib_path = lib_path / system / "arm64"
        elif machine.startswith("arm"):
            lib_path = lib_path / "linux" / "arm"
        else:
            raise RuntimeError(f"unsupported Linux architecture: {machine}")
        lib_path = lib_path / "libcamera_rtsp.so"
    elif system == "darwin":
        if machine == "x86_64":
            lib_path = lib_path / system / "x86_64"
        elif machine in ("arm64", "aarch64"):
            lib_path = lib_path / system / "arm64"
        else:
            raise RuntimeError(f"unsupported macOS architecture: {machine}")
        lib_path = lib_path / "libcamera_rtsp.dylib"
    elif system == "windows":
        if machine in ("x86_64", "amd64"):
            lib_path = lib_path / system / "x86_64"
        elif machine in ("arm64", "aarch64"):
            lib_path = lib_path / system / "arm64"
        else:
            raise RuntimeError(f"Unsupported Windows architecture: {machine}")
        lib_path = lib_path / "camera_rtsp.dll"
    else:
        raise RuntimeError(f"unsupported system: {system}")

    if not lib_path.exists():
        raise FileNotFoundError(f"library not found: {lib_path}")
    _LOGGER.info("load rtsp dynamic lib: %s", lib_path)
    lib_rtsp_camera = CDLL(str(lib_path))
    lib_rtsp_camera.camera_rtsp_set_log_handler.argtypes = [_RTSP_CAMERA_LOG_HANDLER]
    lib_rtsp_camera.camera_rtsp_set_log_handler.restype = None
    lib_rtsp_camera.camera_rtsp_init.argtypes = []
    lib_rtsp_camera.camera_rtsp_init.restype = c_int
    lib_rtsp_camera.camera_rtsp_deinit.argtypes = []
    lib_rtsp_camera.camera_rtsp_deinit.restype = None
    lib_rtsp_camera.camera_rtsp_new.argtypes = [POINTER(_RTSPCameraInfoC)]
    lib_rtsp_camera.camera_rtsp_new.restype = _RTSPCameraInstanceC
    lib_rtsp_camera.camera_rtsp_free.argtypes = [_RTSPCameraInstanceC]
    lib_rtsp_camera.camera_rtsp_free.restype = None
    lib_rtsp_camera.camera_rtsp_start.argtypes = [_RTSPCameraInstanceC, POINTER(_RTSPCameraConfigC)]
    lib_rtsp_camera.camera_rtsp_start.restype = c_int
    lib_rtsp_camera.camera_rtsp_stop.argtypes = [_RTSPCameraInstanceC]
    lib_rtsp_camera.camera_rtsp_stop.restype = c_int
    lib_rtsp_camera.camera_rtsp_status.argtypes = [_RTSPCameraInstanceC]
    lib_rtsp_camera.camera_rtsp_status.restype = c_int
    lib_rtsp_camera.camera_rtsp_version.argtypes = []
    lib_rtsp_camera.camera_rtsp_version.restype = c_char_p
    lib_rtsp_camera.camera_rtsp_register_status_changed.argtypes = [
        _RTSPCameraInstanceC,
        _RTSP_CAMERA_ON_STATUS_CHANGED,
    ]
    lib_rtsp_camera.camera_rtsp_register_status_changed.restype = c_int
    lib_rtsp_camera.camera_rtsp_unregister_status_changed.argtypes = [_RTSPCameraInstanceC]
    lib_rtsp_camera.camera_rtsp_unregister_status_changed.restype = c_int
    lib_rtsp_camera.camera_rtsp_register_raw_data.argtypes = [_RTSPCameraInstanceC, _RTSP_CAMERA_ON_RAW_DATA, c_uint8]
    lib_rtsp_camera.camera_rtsp_register_raw_data.restype = c_int
    lib_rtsp_camera.camera_rtsp_unregister_raw_data.argtypes = [_RTSPCameraInstanceC, c_uint8]
    lib_rtsp_camera.camera_rtsp_unregister_raw_data.restype = c_int
    try:
        version_bytes: bytes = lib_rtsp_camera.camera_rtsp_version()
        _LOGGER.info("libcamera_rtsp version: %s", version_bytes.decode("utf-8"))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _LOGGER.warning("failed to read libcamera_rtsp version: %s", exc)

    rtsp_lib_cache = lib_rtsp_camera
    return lib_rtsp_camera


class RTSPCamera:
    """RTSP Camera manager."""

    def __init__(
        self,
        frame_interval: int = 500,
        enable_hw_accel: bool = True,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._main_loop = loop or asyncio.get_running_loop()
        self._frame_interval = frame_interval
        self._enable_hw_accel = enable_hw_accel
        self._camera_map: Dict[str, RTSPCameraInstance] = {}

        self._lib_rtsp_camera = _load_rtsp_dynamic_lib()
        self._log_handler = _RTSP_CAMERA_LOG_HANDLER(self._on_rtsp_camera_log)
        self._lib_rtsp_camera.camera_rtsp_set_log_handler(self._log_handler)
        self._lib_rtsp_camera.camera_rtsp_init()

    @property
    def lib_rtsp_camera(self) -> CDLL:
        """Lib rtsp camera."""
        return self._lib_rtsp_camera

    async def deinit_async(self) -> None:
        """Deinit."""
        for did in list(self._camera_map.keys()):
            await self.destroy_camera_async(did=did)
        self._camera_map.clear()
        self._lib_rtsp_camera.camera_rtsp_deinit()
        self._lib_rtsp_camera = None  # type: ignore

    async def create_camera_async(
        self,
        camera_info: RtspCameraInfo | Dict,
        frame_interval: Optional[int] = None,
        enable_hw_accel: Optional[bool] = None,
    ) -> RTSPCameraInstance:
        """Create camera."""
        camera: RtspCameraInfo = (
            RtspCameraInfo(**camera_info) if isinstance(camera_info, Dict) else camera_info.model_copy()
        )
        did: str = camera.did
        if did in self._camera_map:
            _LOGGER.info("rtsp camera already exists, %s", did)
            return self._camera_map[did]
        self._camera_map[did] = RTSPCameraInstance(
            manager=self,
            frame_interval=frame_interval or self._frame_interval,
            # respect explicit False; only fall back when None
            enable_hw_accel=self._enable_hw_accel if enable_hw_accel is None else enable_hw_accel,
            camera_info=camera,
            main_loop=self._main_loop,
        )
        return self._camera_map[did]

    async def destroy_camera_async(self, did: str) -> None:
        """Destroy camera."""
        if did not in self._camera_map:
            return
        camera = self._camera_map.pop(did)
        return await camera.destroy_async()

    async def start_camera_async(
        self,
        did: str,
        enable_audio: bool = False,
        enable_reconnect: bool = False,
    ) -> None:
        """Start camera."""
        if did not in self._camera_map:
            _LOGGER.error("rtsp camera not found, %s", did)
            raise MIoTCameraError(f"rtsp camera not found, {did}")
        return await self._camera_map[did].start_async(
            enable_audio=enable_audio,
            enable_reconnect=enable_reconnect,
        )

    async def stop_camera_async(self, did: str) -> None:
        """Stop camera."""
        if did not in self._camera_map:
            _LOGGER.error("rtsp camera not found, %s", did)
            raise MIoTCameraError(f"rtsp camera not found, {did}")
        return await self._camera_map[did].stop_async()

    async def get_camera_status_async(self, did: str) -> MIoTCameraStatus:
        """Get camera status."""
        if did not in self._camera_map:
            _LOGGER.error("rtsp camera not found, %s", did)
            raise MIoTCameraError(f"rtsp camera not found, {did}")
        return await self._camera_map[did].get_status_async()

    async def get_camera_version_async(self) -> str:
        """Get camera version."""
        result: bytes = await self._main_loop.run_in_executor(None, self._lib_rtsp_camera.camera_rtsp_version)
        return result.decode("utf-8")

    def _on_rtsp_camera_log(self, level: int, msg: bytes) -> None:
        """Native log handler."""
        _LOGGER.log(level, "libcamera_rtsp: %s", msg.decode("utf-8"))
