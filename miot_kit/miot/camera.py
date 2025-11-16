# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
""""
MIoT Camera.
"""
import asyncio
import logging
from ctypes import (
    CDLL, CFUNCTYPE, POINTER, Structure, byref, string_at,
    c_bool, c_char_p, c_int, c_uint8, c_uint32, c_uint64, c_void_p
)
from pathlib import Path
import platform
from typing import Any, Callable, Coroutine, Dict, List, Optional
from aiocache import Cache, cached
import aiofiles
import yaml

from .decoder import MIoTMediaDecoder
from .error import MIoTCameraError
from .const import CAMERA_RECONNECT_TIME_MIN, CAMERA_RECONNECT_TIME_MAX, OAUTH2_API_HOST_DEFAULT, OAUTH2_CLIENT_ID
from .types import (
    MIoTCameraCodec,
    MIoTCameraExtraInfo,
    MIoTCameraFrameData,
    MIoTCameraFrameType,
    MIoTCameraStatus,
    MIoTCameraInfo,
    MIoTCameraVideoQuality
)

_LOGGER = logging.getLogger(__name__)


#  level, msg
_MIOT_CAMERA_LOG_HANDLER = CFUNCTYPE(None, c_int, c_char_p)
# camera pointer, status
_MIOT_CAMERA_ON_STATUS_CHANGED = CFUNCTYPE(None, c_int)


class _MIoTCameraFrameHeaderC(Structure):
    """MIoT Camera Raw Data C."""
    _fields_ = [
        ("codec_id", c_uint32),
        ("length", c_uint32),
        ("timestamp", c_uint64),
        ("sequence", c_uint32),
        ("frame_type", c_uint32),
        ("channel", c_uint8)
    ]


_MIOT_CAMERA_ON_RAW_DATA = CFUNCTYPE(None, POINTER(_MIoTCameraFrameHeaderC), POINTER(c_uint8))


class _MIoTCameraInfoC(Structure):
    """MIoT Camera Info C."""
    _fields_ = [
        ("did", c_char_p),
        ("model", c_char_p),
        ("channel_count", c_uint8)
    ]


class _MIoTCameraConfigC(Structure):
    """MIoT Camera Config C."""
    _fields_ = [
        ("video_qualities", POINTER(c_uint8)),
        ("enable_audio", c_bool),
        ("pin_code", c_char_p),
    ]


class _MIoTCameraInstanceC(c_void_p):
    """MIoT Camera Clang Instance."""


class MIoTCameraInstance:
    """MIoT Camera Instance."""
    _manager: "MIoTCamera"
    _main_loop: asyncio.AbstractEventLoop
    _lib_miot_camera: CDLL

    _did: str
    _frame_interval: int
    _enable_hw_accel: bool

    _camera_info: MIoTCameraInfo
    _callback_refs: Dict[str, Callable]

    _c_instance: _MIoTCameraInstanceC
    _video_qualities: List
    _pin_code: Optional[str]
    _enable_audio: bool
    _enable_reconnect: bool
    _enable_record: bool
    _callbacks: Dict[str, Dict[str, Callable[..., Coroutine]]]

    _reconnect_timer: Optional[asyncio.TimerHandle]
    _reconnect_timeout: int

    _decoders: List[MIoTMediaDecoder]

    def __init__(
        self,
        manager: "MIoTCamera",
        frame_interval: int,
        enable_hw_accel: bool,
        camera_info: MIoTCameraInfo,
        main_loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._manager = manager
        self._main_loop = main_loop or asyncio.get_event_loop()
        self._lib_miot_camera = manager.lib_miot_camera
        self._camera_info = camera_info
        self._did = camera_info.did
        self._frame_interval = frame_interval
        self._enable_hw_accel = enable_hw_accel
        self._callback_refs = {}

        self._video_qualities = [MIoTCameraVideoQuality.LOW]
        self._pin_code = None
        self._enable_audio = False
        self._enable_reconnect = False
        self._enable_record = False

        self._callbacks = {}
        self._reconnect_timer = None
        self._reconnect_timeout = CAMERA_RECONNECT_TIME_MIN
        self._decoders = []

        model: str = camera_info.model
        channel_count: int = camera_info.channel_count
        self._c_instance = self._lib_miot_camera.miot_camera_new(
            byref(_MIoTCameraInfoC(
                camera_info.did.encode("utf-8"),
                model.encode("utf-8"),
                channel_count
            ))
        )
        if not self._c_instance:
            raise MIoTCameraError("create camera failed")

        _LOGGER.info("camera inited, %s, %s", camera_info.did, model)

    @property
    def camera_info(self) -> MIoTCameraInfo:
        """Camera info."""
        return self._camera_info

    async def destroy_async(self) -> None:
        """Destroy camera."""
        await self.stop_async()
        for key in list(self._callback_refs.keys()):
            if key == "status":
                self._lib_miot_camera.miot_camera_unregister_status_changed(self._c_instance)
            elif key.startswith("r"):
                self._lib_miot_camera.miot_camera_unregister_raw_data(self._c_instance, int(key.replace("r", "")))
        self._lib_miot_camera.miot_camera_free(self._c_instance)
        self._callback_refs.clear()
        self._callbacks.clear()

    async def start_async(
        self,
        qualities: MIoTCameraVideoQuality | List[MIoTCameraVideoQuality] = MIoTCameraVideoQuality.LOW,
        pin_code: Optional[str] = None,
        enable_audio: bool = False,
        enable_reconnect: bool = False,
        enable_record: bool = False,
    ) -> None:
        """Start camera."""
        channel_count: int = self._camera_info.channel_count or 1
        video_qualities: List
        if isinstance(qualities, MIoTCameraVideoQuality):
            # channel count > 1, use default quality foreach channel
            video_qualities = [qualities.value for _ in range(channel_count)]
        elif isinstance(qualities, List):
            video_qualities = [quality.value for quality in qualities]
        else:
            _LOGGER.error("invalid camera video qualities, %s", qualities)
            raise MIoTCameraError(f"invalid camera video qualities, {qualities}")
        video_qualities.append(0)
        self._pin_code = pin_code
        self._video_qualities = video_qualities
        self._enable_audio = enable_audio
        self._enable_reconnect = enable_reconnect
        self._enable_record = enable_record

        # Init decoders
        for _ in range(channel_count):
            decoder = MIoTMediaDecoder(
                frame_interval=self._frame_interval,
                video_callback=self.__on_video_decode_callback,
                audio_callback=self.__on_audio_decode_callback,
                enable_hw_accel=self._enable_hw_accel,
                enable_audio=self._enable_audio,
                main_loop=self._main_loop
            )
            self._decoders.append(decoder)
            decoder.daemon = True
            decoder.start()

        # Register status callback.
        c_callback = _MIOT_CAMERA_ON_STATUS_CHANGED(self.__on_status_changed)
        result: int = self._lib_miot_camera.miot_camera_register_status_changed(self._c_instance, c_callback)
        # MUST add to callback refs, otherwise it will be freed.
        self._callback_refs["status"] = c_callback
        _LOGGER.info("register status changed, %s, %s", self._did, result)

        self._reconnect_timer = self._main_loop.call_later(
            0, lambda: self._main_loop.create_task(self.__try_start_async())
        )

    async def stop_async(self) -> None:
        """Stop camera."""
        # Cancel reconnect task if exists.
        self._enable_reconnect = False
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
            self.__reset_try_start_timeout()

        result: int = await self._main_loop.run_in_executor(
            None, self._lib_miot_camera.miot_camera_stop, self._c_instance
        )
        # Stop decoders
        for decoder in self._decoders:
            decoder.stop()
        self._decoders.clear()

        _LOGGER.info("camera stop, %s, %s", self._did, result)

    async def get_status_async(self) -> MIoTCameraStatus:
        """Get camera status."""
        result: int = await self._main_loop.run_in_executor(
            None, self._lib_miot_camera.miot_camera_status, self._c_instance
        )
        _LOGGER.info("camera status, %s, %s", self._did, result)
        return MIoTCameraStatus(result)

    async def register_status_changed_async(
        self, callback: Callable[[str, MIoTCameraStatus], Coroutine], multi_reg: bool = False
    ) -> int:
        """Register camera status changed callback.
        async def on_status_changed_async(did: str, status: int)
        """
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
        """Register camera raw stream callback.
        async def on_raw_video_async(did: str, data: bytes, ts: int, seq: int, channel: int)
        """
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
        """Register camera raw audio callback.
        async def on_raw_audio_async(did: str, data: bytes, ts: int, seq: int, channel: int)
        """
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
        """Register camera decode jpg callback.
        async def on_decode_jpg_async(did: str, data: bytes, ts: int, channel: int)
        """
        await self.__update_raw_data_register_status_async(channel=channel)
        reg_key: str = f"decode_jpg.{channel}"
        self._callbacks.setdefault(reg_key, {})
        reg_id: int = 0
        if multi_reg:
            reg_id = len(self._callbacks) + 1
        self._callbacks[reg_key][str(reg_id)] = callback
        return reg_id

    async def unregister_decode_jpg_async(self,  channel: int = 0, reg_id: int = 0) -> None:
        """Unregister camera decode jpg callback."""
        await self.__update_raw_data_register_status_async(channel=channel, is_register=False)
        reg_key: str = f"decode_jpg.{channel}"
        if reg_key not in self._callbacks:
            return
        self._callbacks[reg_key].pop(str(reg_id), None)

    async def register_decode_pcm_async(
        self, callback: Callable[[str, bytes, int, int], Coroutine], channel: int = 0, multi_reg: bool = False
    ) -> int:
        """Register camera decode pcm callback.
        async def on_decode_pcm_async(did: str, data: bytes, ts: int, channel: int)
        """
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
            _LOGGER.error("invalid channel, %s, %s", self._did, channel)
            raise MIoTCameraError(f"invalid channel, {self._did}, {channel}")

        c_callback = _MIOT_CAMERA_ON_RAW_DATA(self.__on_raw_data)
        result: int = self._lib_miot_camera.miot_camera_register_raw_data(self._c_instance, c_callback, channel)
        # MUST add to callback refs, otherwise it will be freed.
        self._callback_refs[f"r{channel}"] = c_callback
        _LOGGER.info("register raw data, %s, %s, %s", self._did, channel, result)

    async def __unregister_raw_data_async(self, channel: int = 0) -> None:
        """Unregister raw data callback."""
        if channel < 0 or channel >= self._camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", self._did, channel)
            raise MIoTCameraError(f"invalid channel, {self._did}, {channel}")

        result: int = self._lib_miot_camera.miot_camera_unregister_raw_data(self._c_instance, channel)
        self._callback_refs.pop(f"r{channel}", None)
        _LOGGER.info("unregister raw data, %s, %s, %s", self._did, channel, result)

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
        _LOGGER.info("try start camera, %s", self._did)
        # Cancel reconnect task if exists.
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

        result: int = await self._main_loop.run_in_executor(
            None, self._lib_miot_camera.miot_camera_start, self._c_instance,
            byref(_MIoTCameraConfigC(
                (c_uint8 * ((self.camera_info.channel_count or 1)+1))(*self._video_qualities),
                self._enable_audio,
                self._pin_code.encode("utf-8") if self._pin_code else None)
            )
        )
        _LOGGER.info(
            "try start camera, result->%s, did->%s, enable_audio->%s, enable_reconnect->%s, pin_code->%.2s**",
            result, self.camera_info.did, self._enable_audio, self._enable_reconnect, self._pin_code)
        if result == 0:
            self.__reset_try_start_timeout()
            return
        # Reconnect.
        if self._enable_reconnect:
            self._reconnect_timer = self._main_loop.call_later(
                self.__get_try_start_timeout(), lambda: self._main_loop.create_task(self.__try_start_async())
            )
        else:
            _LOGGER.error("camera start failed, %s, %s", self._did, result)
            raise MIoTCameraError(f"camera start failed, {self.camera_info.did}, {result}")

    def __get_try_start_timeout(self) -> int:
        self._reconnect_timeout = min(self._reconnect_timeout * 2, CAMERA_RECONNECT_TIME_MAX)
        _LOGGER.info("get reconnect timeout, %s, %s", self._did, self._reconnect_timeout)
        return self._reconnect_timeout

    def __reset_try_start_timeout(self) -> None:
        self._reconnect_timeout = CAMERA_RECONNECT_TIME_MIN
        _LOGGER.info("reset reconnect timeout, %s, %s", self._did, self._reconnect_timeout)

    def __on_status_changed(self, status: int) -> None:
        """Callback for status changed."""
        camera_status: MIoTCameraStatus = MIoTCameraStatus(status)
        self._camera_info.camera_status = camera_status
        # TODO: Dirty logic, Need to optimize upper-level business judgment logic
        self._camera_info.online = self._camera_info.camera_status == MIoTCameraStatus.CONNECTED
        s_callbacks = self._callbacks.get("status", {})
        for callback in s_callbacks.values():
            asyncio.run_coroutine_threadsafe(callback(self._did, camera_status), self._main_loop)
        if camera_status == MIoTCameraStatus.DISCONNECTED and self._enable_reconnect:
            self._reconnect_timer = self._main_loop.call_later(
                self.__get_try_start_timeout(), lambda: self._main_loop.create_task(self.__try_start_async())
            )

    def __on_raw_data(self, frame_header_ptr: Any, data: bytes) -> None:
        """Callback for raw data."""
        frame_header: _MIoTCameraFrameHeaderC = frame_header_ptr.contents
        codec_id: MIoTCameraCodec = MIoTCameraCodec(frame_header.codec_id)
        channel: int = frame_header.channel
        frame_data = MIoTCameraFrameData(
            codec_id=codec_id,
            length=frame_header.length,
            timestamp=frame_header.timestamp,
            sequence=frame_header.sequence,
            frame_type=MIoTCameraFrameType(frame_header.frame_type),
            channel=channel,
            data=string_at(data, frame_header.length)
        )
        if codec_id in [MIoTCameraCodec.VIDEO_H264, MIoTCameraCodec.VIDEO_H265]:
            # raw video
            if self._callbacks.get(f"decode_jpg.{channel}", None):
                self._decoders[channel].push_video_frame(frame_data)
            v_callbacks = self._callbacks.get(f"raw_video.{channel}", {})
            for v_callback in list(v_callbacks.values()):
                asyncio.run_coroutine_threadsafe(
                    v_callback(self._did, frame_data.data, frame_data.timestamp, frame_data.sequence, channel),
                    self._main_loop
                )
        elif codec_id in [MIoTCameraCodec.AUDIO_OPUS, MIoTCameraCodec.AUDIO_G711A, MIoTCameraCodec.AUDIO_G711U]:
            # raw audio
            if self._callbacks.get(f"decode_pcm.{channel}", None):
                self._decoders[channel].push_audio_frame(frame_data)
            self._decoders[channel].push_audio_frame(frame_data)
            a_callbacks = self._callbacks.get(f"raw_audio.{channel}", {})
            for a_callback in list(a_callbacks.values()):
                asyncio.run_coroutine_threadsafe(
                    a_callback(self._did, frame_data.data, frame_data.timestamp, frame_data.sequence, channel),
                    self._main_loop
                )
        else:
            _LOGGER.error("unknown codec, %s, %s, %s", self._did, codec_id, frame_header.timestamp)
        # _LOGGER.info("raw, %s, %s, %s, %s", self._did, channel, frame_header.timestamp, frame_header.sequence)

    async def __on_video_decode_callback(self, data: bytes, timestamp: int, channel: int) -> None:
        """On video decode callback."""
        # _LOGGER.info("decode jpg, %s, %s, %s, %s", self._did, len(data), timestamp, channel)
        v_callbacks = self._callbacks.get(f"decode_jpg.{channel}", {})
        for callback in list(v_callbacks.values()):
            asyncio.run_coroutine_threadsafe(
                callback(self._did, data, timestamp, channel),
                self._main_loop
            )

    async def __on_audio_decode_callback(self, data: bytes, timestamp: int, channel: int) -> None:
        """On audio decode callback."""
        # _LOGGER.info("decode audio, %s, %s, %s, %s", self._did, len(data), timestamp, channel)
        a_callbacks = self._callbacks.get(f"decode_pcm.{channel}", {})
        for callback in list(a_callbacks.values()):
            asyncio.run_coroutine_threadsafe(
                callback(self._did, data, timestamp, channel),
                self._main_loop
            )


def _load_dynamic_lib():
    system = platform.system().lower()   # 'linux', 'darwin', 'windows'
    machine = platform.machine().lower()  # 'x86_64', 'arm64', 'aarch64', 'i386'
    lib_path = Path(__file__).parent / "libs"
    if system == "linux":
        # linux
        if machine in ("x86_64", "amd64"):
            # x86_64
            lib_path = lib_path / "linux" / "x86_64"
        elif machine in ("aarch64", "arm64"):
            # arm 64 bit
            lib_path = lib_path / "linux" / "arm64"
        elif machine.startswith("arm"):
            # arm 32 bit
            lib_path = lib_path / "linux" / "arm"
        else:
            raise RuntimeError(f"unsupported Linux architecture: {machine}")
        lib_path = lib_path / "libmiot_camera_lite.so"

    elif system == "darwin":
        # macOS
        if machine == "x86_64":
            # Intel Mac
            lib_path = lib_path / "macos" / "x86_64"
        elif machine in ("arm64",):
            # Apple M series
            lib_path = lib_path / "macos" / "arm64"
        else:
            raise RuntimeError(f"unsupported macOS architecture: {machine}")
        lib_path = lib_path / "libmiot_camera_lite.dylib"

    elif system == "windows":
        if machine in ("x86_64", "amd64"):
            # x86_64
            lib_path = lib_path / "windows" / "x86_64"
        elif machine in ("arm64",):
            # ARM64
            lib_path = lib_path / "windows" / "arm64"
        else:
            raise RuntimeError(f"Unsupported Windows architecture: {machine}")
        lib_path = lib_path / "miot_camera_lite.dll"
    else:
        raise RuntimeError(f"unsupported system: {system}")

    if not lib_path.exists():
        raise FileNotFoundError(f"library not found: {lib_path}")
    _LOGGER.info("load dynamic lib: %s", lib_path)
    lib_miot_camera = CDLL(str(lib_path))
    # set log handler
    lib_miot_camera.miot_camera_set_log_handler.argtypes = [_MIOT_CAMERA_LOG_HANDLER]
    lib_miot_camera.miot_camera_set_log_handler.restype = None
    # miot_camera_init
    lib_miot_camera.miot_camera_init.argtypes = [c_char_p, c_char_p, c_char_p]
    lib_miot_camera.miot_camera_init.restype = c_int
    # miot_camera_deinit
    lib_miot_camera.miot_camera_deinit.argtypes = []
    lib_miot_camera.miot_camera_deinit.restype = None
    # miot_camera_update_access_token
    lib_miot_camera.miot_camera_update_access_token.argtypes = [c_char_p]
    lib_miot_camera.miot_camera_update_access_token.restype = c_int
    # miot_camera_new
    lib_miot_camera.miot_camera_new.argtypes = [POINTER(_MIoTCameraInfoC)]
    lib_miot_camera.miot_camera_new.restype = _MIoTCameraInstanceC
    # miot_camera_free
    lib_miot_camera.miot_camera_free.argtypes = [_MIoTCameraInstanceC]
    lib_miot_camera.miot_camera_free.restype = None
    # miot_camera_start
    lib_miot_camera.miot_camera_start.argtypes = [_MIoTCameraInstanceC, POINTER(_MIoTCameraConfigC)]
    lib_miot_camera.miot_camera_start.restype = c_int
    # miot_camera_stop
    lib_miot_camera.miot_camera_stop.argtypes = [_MIoTCameraInstanceC]
    lib_miot_camera.miot_camera_stop.restype = c_int
    # miot_camera_status
    lib_miot_camera.miot_camera_status.argtypes = [_MIoTCameraInstanceC]
    lib_miot_camera.miot_camera_status.restype = c_int
    # miot_camera_version
    lib_miot_camera.miot_camera_version.argtypes = []
    lib_miot_camera.miot_camera_version.restype = c_char_p
    # miot_camera_register_status_changed
    lib_miot_camera.miot_camera_register_status_changed.argtypes = [
        _MIoTCameraInstanceC, _MIOT_CAMERA_ON_STATUS_CHANGED]
    lib_miot_camera.miot_camera_register_status_changed.restype = c_int
    # miot_camera_unregister_status_changed
    lib_miot_camera.miot_camera_unregister_status_changed.argtypes = [_MIoTCameraInstanceC]
    lib_miot_camera.miot_camera_unregister_status_changed.restype = c_int
    # miot camera raw data
    lib_miot_camera.miot_camera_register_raw_data.argtypes = [_MIoTCameraInstanceC, _MIOT_CAMERA_ON_RAW_DATA, c_uint8]
    lib_miot_camera.miot_camera_register_raw_data.restype = c_int
    lib_miot_camera.miot_camera_unregister_raw_data.argtypes = [_MIoTCameraInstanceC, c_uint8]
    lib_miot_camera.miot_camera_unregister_raw_data.restype = c_int

    return lib_miot_camera


class MIoTCamera:
    """MIoT Camera."""
    _main_loop: asyncio.AbstractEventLoop
    _lib_miot_camera: CDLL

    _host: str
    _access_token: str
    _frame_interval: int
    _enable_hw_accel: bool
    # key: did, value: MIoTCameraInstance
    _camera_map: Dict[str, MIoTCameraInstance]
    # logger handler
    _log_handler: Callable

    @property
    def lib_miot_camera(self) -> CDLL:
        """Lib miot camera."""
        return self._lib_miot_camera

    def __init__(
            self, cloud_server: str, access_token: str, frame_interval: int = 500, enable_hw_accel: bool = True,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        """Init."""
        if not isinstance(cloud_server, str) or not isinstance(access_token, str):
            raise MIoTCameraError("invalid parameter")
        self._main_loop = loop or asyncio.get_running_loop()
        self._host = OAUTH2_API_HOST_DEFAULT
        if cloud_server != "cn":
            self._host = f"{cloud_server}.{OAUTH2_API_HOST_DEFAULT}"
        self._access_token = access_token
        self._frame_interval = frame_interval
        self._enable_hw_accel = enable_hw_accel
        self._camera_map = {}

        # lib init
        self._lib_miot_camera = _load_dynamic_lib()
        # MUST add to refs, otherwise it will be freed.
        self._log_handler = _MIOT_CAMERA_LOG_HANDLER(self._on_miot_camera_log)
        self._lib_miot_camera.miot_camera_set_log_handler(self._log_handler)

        self._lib_miot_camera.miot_camera_init(
            self._host.encode("utf-8"), OAUTH2_CLIENT_ID.encode("utf-8"), self._access_token.encode("utf-8")
        )

    def __del__(self):
        """Del."""
        if self._lib_miot_camera:
            self._lib_miot_camera.miot_camera_set_log_handler(None)
            self._lib_miot_camera.miot_camera_deinit()
            self._lib_miot_camera = None  # type: ignore

    def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async exit."""

    @property
    def camera_map(self) -> Dict[str, MIoTCameraInstance]:
        """Camera map."""
        return self._camera_map

    async def init_async(self, frame_interval: int = 500, enable_hw_accel: bool = False) -> None:
        """Init."""
        self._frame_interval = frame_interval
        self._enable_hw_accel = enable_hw_accel
        _LOGGER.info("miot camera lib version: %s", await self.get_camera_version_async())

    async def deinit_async(self) -> None:
        """Deinit."""
        for did in list(self._camera_map.keys()):
            await self.destroy_camera_async(did=did)
        self._camera_map.clear()
        self._lib_miot_camera.miot_camera_deinit()
        self._lib_miot_camera = None  # type: ignore

    async def update_access_token_async(self, access_token: str) -> None:
        """Update access token."""
        self._access_token = access_token
        self._lib_miot_camera.miot_camera_update_access_token(self._access_token.encode("utf-8"))

    async def create_camera_async(
        self,
        camera_info: MIoTCameraInfo | Dict,
        frame_interval: Optional[int] = None,
        enable_hw_accel: Optional[bool] = None,
    ) -> MIoTCameraInstance:
        """Create camera."""
        camera: MIoTCameraInfo = (
            MIoTCameraInfo(**camera_info) if isinstance(camera_info, Dict) else camera_info.model_copy()
        )
        did: str = camera.did
        if did in self._camera_map:
            _LOGGER.info("camera already exists")
            return self._camera_map[did]
        self._camera_map[did] = MIoTCameraInstance(
            manager=self,
            frame_interval=frame_interval or self._frame_interval,
            enable_hw_accel=enable_hw_accel or self._enable_hw_accel,
            camera_info=camera,
            main_loop=self._main_loop
        )
        return self._camera_map[did]

    async def get_camera_instance_async(self, did: str) -> Optional[MIoTCameraInstance]:
        """Get camera instance."""
        if did not in self._camera_map:
            return None
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
        pin_code: Optional[str] = None,
        qualities: MIoTCameraVideoQuality | List[MIoTCameraVideoQuality] = MIoTCameraVideoQuality.LOW,
        enable_audio: bool = False,
        enable_reconnect: bool = False,
    ) -> None:
        """Start camera."""
        # Check.
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        if pin_code and len(pin_code) != 4:
            _LOGGER.error("invalid pin code, %s", pin_code)
            raise MIoTCameraError("invalid pin code")
        return await self._camera_map[did].start_async(
            pin_code=pin_code,
            qualities=qualities,
            enable_audio=enable_audio,
            enable_reconnect=enable_reconnect
        )

    async def stop_camera_async(self, did: str) -> None:
        """Stop camera."""
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        return await self._camera_map[did].stop_async()

    async def get_camera_status_async(self, did: str) -> MIoTCameraStatus:
        """Get camera status."""
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        return await self._camera_map[did].get_status_async()

    async def get_camera_version_async(self) -> str:
        """Get camera version."""
        result: bytes = await self._main_loop.run_in_executor(None, self._lib_miot_camera.miot_camera_version)
        return result.decode("utf-8")

    async def register_status_changed_async(
        self, did: str, callback: Callable[[str, MIoTCameraStatus], Coroutine], multi_reg: bool = False
    ) -> int:
        """Register camera status changed.
        async def on_status_changed_async(did: str, status: int)
        """
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        return await self._camera_map[did].register_status_changed_async(callback=callback, multi_reg=multi_reg)

    async def unregister_status_changed_async(self, did: str, reg_id: int = 0) -> None:
        """Unregister status changed."""
        if did not in self._camera_map:
            _LOGGER.info("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        return await self._camera_map[did].unregister_status_changed_async(reg_id=reg_id)

    async def register_raw_video_async(
        self,
        did: str,
        callback: Callable[[str, bytes, int, int, int], Coroutine],
        channel: int = 0,
        multi_reg: bool = False
    ) -> int:
        """Register raw video.
        async def on_raw_video_async(did: str, data: bytes, ts: int, seq: int, channel: int)
        """
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        if channel < 0 or channel >= self._camera_map[did].camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", did, channel)
            raise MIoTCameraError(f"invalid channel, {did}, {channel}")

        return await self._camera_map[did].register_raw_video_async(
            callback=callback, channel=channel, multi_reg=multi_reg
        )

    async def unregister_raw_video_async(self, did: str, channel: int = 0, reg_id: int = 0) -> None:
        """Unregister raw video."""
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        if channel < 0 or channel >= self._camera_map[did].camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", did, channel)
            raise MIoTCameraError(f"invalid channel, {did}, {channel}")

        return await self._camera_map[did].unregister_raw_video_async(channel=channel, reg_id=reg_id)

    async def register_raw_audio_async(
        self,
        did: str,
        callback: Callable[[str, bytes, int, int, int], Coroutine],
        channel: int = 0,
        multi_reg: bool = False
    ) -> int:
        """Register raw audio.
        async def on_raw_audio_async(did: str, data: bytes, ts: int, seq: int, channel: int)
        """
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        if channel < 0 or channel >= self._camera_map[did].camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", did, channel)
            raise MIoTCameraError(f"invalid channel, {did}, {channel}")

        return await self._camera_map[did].register_raw_audio_async(
            callback=callback, channel=channel, multi_reg=multi_reg
        )

    async def unregister_raw_audio_async(self, did: str, channel: int = 0, reg_id: int = 0) -> None:
        """Unregister raw audio."""
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        if channel < 0 or channel >= self._camera_map[did].camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", did, channel)
            raise MIoTCameraError(f"invalid channel, {did}, {channel}")

        return await self._camera_map[did].unregister_raw_audio_async(channel=channel, reg_id=reg_id)

    async def register_decode_jpg_async(
        self, did: str, callback: Callable[[str, bytes, int, int], Coroutine], channel: int = 0, multi_reg: bool = False
    ) -> int:
        """Register decode jpg.
        async def on_decode_jpg_async(did: str, data: bytes, ts: int, channel: int)
        """
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        if channel < 0 or channel >= self._camera_map[did].camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", did, channel)
            raise MIoTCameraError(f"invalid channel, {did}, {channel}")
        return await self._camera_map[did].register_decode_jpg_async(
            callback=callback, channel=channel, multi_reg=multi_reg
        )

    async def unregister_decode_jpg_async(self, did: str, channel: int = 0, reg_id: int = 0) -> None:
        """Unregister decode jpg."""
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        if channel < 0 or channel >= self._camera_map[did].camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", did, channel)
            raise MIoTCameraError(f"invalid channel, {did}, {channel}")
        return await self._camera_map[did].unregister_decode_jpg_async(channel=channel, reg_id=reg_id)

    async def register_decode_pcm_async(
        self, did: str, callback: Callable[[str, bytes, int, int], Coroutine], channel: int = 0, multi_reg: bool = False
    ) -> int:
        """Register decode pcm.
        async def on_decode_pcm_async(did: str, data: bytes, ts: int, channel: int)
        """
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        if channel < 0 or channel >= self._camera_map[did].camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", did, channel)
            raise MIoTCameraError(f"invalid channel, {did}, {channel}")
        return await self._camera_map[did].register_decode_pcm_async(
            callback=callback, channel=channel, multi_reg=multi_reg
        )

    async def unregister_decode_pcm_async(self, did: str, channel: int = 0, reg_id: int = 0) -> None:
        """Unregister decode pcm."""
        if did not in self._camera_map:
            _LOGGER.error("camera not found, %s", did)
            raise MIoTCameraError(f"camera not found, {did}")
        if channel < 0 or channel >= self._camera_map[did].camera_info.channel_count:
            _LOGGER.error("invalid channel, %s, %s", did, channel)
            raise MIoTCameraError(f"invalid channel, {did}, {channel}")
        return await self._camera_map[did].unregister_decode_pcm_async(channel=channel, reg_id=reg_id)

    def _on_miot_camera_log(self, level: int, msg: bytes) -> None:
        """Log handler for MIoT Camera."""
        # pylint: disable=unused-argument
        _LOGGER.info(msg.decode("utf-8"))


@cached(ttl=600, cache=Cache.MEMORY)
async def get_camera_extra_info() -> MIoTCameraExtraInfo:
    """Get cameras extra info."""
    # TODO: Get from cloud.
    file_path = Path(__file__).parent / "configs" / "camera_extra_info.yaml"
    if not file_path.exists():
        raise MIoTCameraError(f"camera_extra_info.yaml file not exists, {file_path}")
    result: Optional[MIoTCameraExtraInfo] = None
    async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
        _LOGGER.info("load camera_extra_info.yaml file: %s", file_path)
        result = MIoTCameraExtraInfo.model_validate(yaml.safe_load(await f.read()))
    if not result:
        raise MIoTCameraError(f"camera_extra_info.yaml file is empty, {file_path}")
    return result
