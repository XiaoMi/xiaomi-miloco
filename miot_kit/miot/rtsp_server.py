# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
RTSP Server support.
Wraps the libcamera_rtsp library to provide an RTSP server.
"""
from __future__ import annotations

import logging
from ctypes import (
    CDLL,
    CFUNCTYPE,
    POINTER,
    Structure,
    c_char_p,
    c_int,
    c_uint8,
    c_uint32,
    c_uint64,
    c_void_p,
    byref,
)

from .rtsp_camera import _load_rtsp_dynamic_lib as _load_lib_base

# Handle import error for standalone testing or relative import issues
try:
    from .error import MIoTCameraError
except ImportError:
    class MIoTCameraError(Exception):
        pass

_LOGGER = logging.getLogger(__name__)

# level, msg
_RTSP_SERVER_LOG_HANDLER = CFUNCTYPE(None, c_int, c_char_p)

class _RTSPServerFrameHeaderC(Structure):
    """Frame header matching rtsp_frame_header in C."""
    _fields_ = [
        ("codec_id", c_uint32),
        ("length", c_uint32),
        ("timestamp", c_uint64),
        ("sequence", c_uint32),
        ("frame_type", c_uint32),
        ("channel", c_uint8),
    ]

class _RTSPServerInstanceC(c_void_p):
    """RTSP server native instance pointer."""

def _load_rtsp_dynamic_lib() -> CDLL:
    # Reuse the loader from rtsp_camera to get the CDLL instance
    # It will have client-side argtypes set, which is fine.
    # We will add server-side argtypes to it.
    try:
        lib = _load_lib_base()
    except Exception as e:
        _LOGGER.error("Failed to load RTSP library via rtsp_camera loader: %s", e)
        raise

    # void rtsp_server_init(void);
    try:
        lib.rtsp_server_init.argtypes = []
        lib.rtsp_server_init.restype = None

        # rtsp_server_handle rtsp_server_new(int port);
        lib.rtsp_server_new.argtypes = [c_int]
        lib.rtsp_server_new.restype = _RTSPServerInstanceC

        # void rtsp_server_free(rtsp_server_handle handle);
        lib.rtsp_server_free.argtypes = [_RTSPServerInstanceC]
        lib.rtsp_server_free.restype = None

        # int rtsp_server_start(rtsp_server_handle handle);
        lib.rtsp_server_start.argtypes = [_RTSPServerInstanceC]
        lib.rtsp_server_start.restype = c_int

        # void rtsp_server_stop(rtsp_server_handle handle);
        lib.rtsp_server_stop.argtypes = [_RTSPServerInstanceC]
        lib.rtsp_server_stop.restype = None

        # int rtsp_server_add_stream(rtsp_server_handle handle, const char* url_suffix,
        #                            uint32_t video_codec_id, uint32_t audio_codec_id);
        lib.rtsp_server_add_stream.argtypes = [_RTSPServerInstanceC, c_char_p, c_uint32, c_uint32]
        lib.rtsp_server_add_stream.restype = c_int

        # int rtsp_server_remove_stream(rtsp_server_handle handle, const char* url_suffix);
        lib.rtsp_server_remove_stream.argtypes = [_RTSPServerInstanceC, c_char_p]
        lib.rtsp_server_remove_stream.restype = c_int

        # int rtsp_server_push_frame(rtsp_server_handle handle, const char* url_suffix,
        #                            const struct rtsp_frame_header* header, const uint8_t* data);
        lib.rtsp_server_push_frame.argtypes = [
            _RTSPServerInstanceC,
            c_char_p,
            POINTER(_RTSPServerFrameHeaderC),
            POINTER(c_uint8)
        ]
        lib.rtsp_server_push_frame.restype = c_int

        # void rtsp_server_set_log_handler(rtsp_log_handler_t handler);
        lib.rtsp_server_set_log_handler.argtypes = [_RTSP_SERVER_LOG_HANDLER]
        lib.rtsp_server_set_log_handler.restype = None
    except AttributeError as e:
        _LOGGER.error("Failed to load RTSP server functions: %s", e)
        raise

    return lib

class RtspServer:
    """RTSP Server."""

    _lib: CDLL
    _handle: _RTSPServerInstanceC
    _log_handler: _RTSP_SERVER_LOG_HANDLER

    def __init__(self, port: int = 8554):
        self._lib = _load_rtsp_dynamic_lib()
        self._log_handler = _RTSP_SERVER_LOG_HANDLER(self._on_log)
        self._lib.rtsp_server_set_log_handler(self._log_handler)
        self._lib.rtsp_server_init()

        self._handle = self._lib.rtsp_server_new(port)
        if not self._handle:
            raise MIoTCameraError("Failed to create RTSP server")

        self._port = port

        _LOGGER.info("RTSP Server created on port %s", port)

    def start(self):
        """Start the server loop."""
        ret = self._lib.rtsp_server_start(self._handle)
        if ret != 0:
            raise MIoTCameraError(f"Failed to start RTSP server: {ret}")
        _LOGGER.info("RTSP Server started")

    def stop(self):
        """Stop the server loop."""
        self._lib.rtsp_server_stop(self._handle)
        _LOGGER.info("RTSP Server stopped")

    def destroy(self):
        """Destroy the server instance."""
        if self._handle:
            self._lib.rtsp_server_free(self._handle)
            self._handle = None

    def add_stream(self, url_suffix: str, video_codec_id: int, audio_codec_id: int) -> bool:
        """Add a stream to the server.
        video_codec_id: 4=H264, 5=H265, 0=None
        audio_codec_id: 1026=G711U, 1027=G711A, 1032=OPUS, 0=None
        """
        ret = self._lib.rtsp_server_add_stream(
            self._handle,
            url_suffix.encode("utf-8"),
            video_codec_id,
            audio_codec_id
        )
        if ret == 0:
            # _LOGGER.info("Added RTSP stream: rtsp://*:%s/%s", self._port, url_suffix)
            return True
        _LOGGER.error("Failed to add RTSP stream: %s", url_suffix)
        return False

    def remove_stream(self, url_suffix: str) -> bool:
        """Remove a stream."""
        ret = self._lib.rtsp_server_remove_stream(
            self._handle,
            url_suffix.encode("utf-8")
        )
        return ret == 0

    def push_frame(
        self,
        url_suffix: str,
        codec_id: int,
        data: bytes,
        timestamp: int,
        sequence: int,
        frame_type: int
    ) -> bool:
        """Push a frame to the stream."""
        if not self._handle:
            return False

        header = _RTSPServerFrameHeaderC()
        header.codec_id = codec_id
        header.length = len(data)
        header.timestamp = timestamp
        header.sequence = sequence
        header.frame_type = frame_type
        header.channel = 0

        # ctypes.cast to void pointer or similar might be safer for bytes
        # but from_buffer_copy is standard.
        c_data = (c_uint8 * len(data)).from_buffer_copy(data)

        ret = self._lib.rtsp_server_push_frame(
            self._handle,
            url_suffix.encode("utf-8"),
            byref(header),
            c_data
        )
        return ret == 0

    def _on_log(self, level: int, msg: bytes):  # pylint: disable=unused-argument
        _LOGGER.info("[Native RTSP Server] %s", msg.decode("utf-8"))
