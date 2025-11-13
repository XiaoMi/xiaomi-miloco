# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT error code and exception.
"""
from enum import Enum
from typing import Any


class MIoTErrorCode(Enum):
    """MIoT error code."""
    # Base error code
    CODE_UNKNOWN = -10000
    CODE_UNAVAILABLE = -10001
    CODE_INVALID_PARAMS = -10002
    CODE_RESOURCE_ERROR = -10003
    CODE_INTERNAL_ERROR = -10004
    CODE_UNAUTHORIZED_ACCESS = -10005
    CODE_TIMEOUT = -10006
    # OAuth error code
    CODE_OAUTH_UNAUTHORIZED = -10020
    # Http error code
    CODE_HTTP_INVALID_ACCESS_TOKEN = -10030
    # MIoT mips error code
    CODE_MIPS_INVALID_RESULT = -10040
    # MIoT cert error code
    CODE_CERT_INVALID_CERT = -10050
    # MIoT spec error code, [-10060, -10069]
    CODE_SPEC_DEFAULT = -10060
    # MIoT storage error code, [-10070, -10079]
    # MIPS service error code, [-10080, -10089]
    # MIoT lan error code, [-10090, -10099]
    CODE_LAN_UNAVAILABLE = -10100


class MIoTError(Exception):
    """MIoT error."""
    code: MIoTErrorCode
    message: Any

    def __init__(
        self,  message: Any, code: MIoTErrorCode = MIoTErrorCode.CODE_UNKNOWN
    ) -> None:
        self.message = message
        self.code = code
        super().__init__(self.message)

    def to_json_str(self) -> str:
        """To str."""
        return f"{{\"code\":{self.code.value},\"message\":\"{self.message}\"}}"

    def to_dict(self) -> dict:
        """To dict."""
        return {"code": self.code.value, "message": self.message}


class MIoTOAuth2Error(MIoTError):
    ...


class MIoTHttpError(MIoTError):
    ...


class MIoTMipsError(MIoTError):
    ...


class MIoTDeviceError(MIoTError):
    ...


class MIoTCameraError(MIoTError):
    ...


class MIoTSpecError(MIoTError):
    def __init__(self, message: Any, code: MIoTErrorCode = MIoTErrorCode.CODE_SPEC_DEFAULT) -> None:
        super().__init__(message, code)


class MIoTStorageError(MIoTError):
    ...


class MIoTCertError(MIoTError):
    ...


class MIoTClientError(MIoTError):
    ...


class MIoTLanError(MIoTError):
    ...


class MIoTMediaDecoderError(MIoTError):
    ...
