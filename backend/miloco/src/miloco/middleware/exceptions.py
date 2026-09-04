# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Exception definitions
Provides unified exception handling mechanism, distinguishing HTTP exceptions and business exceptions
"""

from fastapi import status


class BaseAPIException(Exception):
    """Base API exception class"""

    def __init__(self, message: str, code: int, http_status: int = status.HTTP_200_OK):
        self.message = message
        self.code = code
        self.http_status = http_status
        super().__init__(self.message)


class HTTPException(BaseAPIException):
    """Base HTTP exception class"""

    def __init__(self, message: str, status_code: int, code: int = 1000):
        super().__init__(message, code, http_status=status_code)


class BadRequestException(HTTPException):
    """Bad request exception - 400 Bad Request"""

    def __init__(self, message: str = "Bad request"):
        super().__init__(message, status.HTTP_400_BAD_REQUEST, code=1001)


class ValidationException(HTTPException):
    """Parameter validation exception - 422 Unprocessable Context"""

    def __init__(self, message: str = "Validation failed"):
        super().__init__(message, status.HTTP_422_UNPROCESSABLE_CONTENT, code=1002)


class AuthenticationException(HTTPException):
    """Authentication exception - 401 Unauthorized"""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, status.HTTP_401_UNAUTHORIZED, code=1003)


class AuthorizationException(HTTPException):
    """Authorization exception - 403 Forbidden"""

    def __init__(self, message: str = "Access denied"):
        super().__init__(message, status.HTTP_403_FORBIDDEN, code=1004)


class BusinessException(BaseAPIException):
    """Base business exception class - returns 200 + business error code"""

    def __init__(self, message: str, code: int = 2000):
        super().__init__(message, code, http_status=status.HTTP_200_OK)


class ResourceNotFoundException(BusinessException):
    """Resource not found exception - 2001"""

    def __init__(self, message: str):
        super().__init__(message, code=2001)



class ConflictException(BusinessException):
    """Resource conflict exception (such as name duplication, etc.) - 2002"""

    def __init__(self, message: str):
        super().__init__(message, code=2002)


class MiotAuthUnavailableError(BusinessException):
    """米家授权已被云端拒绝，走云端的下发一律停止 - 2003

    与「未绑定」不同：绑定关系还在，是凭据不能再续期了。住户需要重新授权，
    这不是重试能解决的——所以直接拒绝，而不是让请求发出去撞 401。
    """

    def __init__(self, message: str):
        super().__init__(message, code=2003)


class ExternalServiceException(BusinessException):
    """External service exception - 3000"""

    def __init__(self, message: str):
        super().__init__(message, code=3000)


class LLMServiceException(ExternalServiceException):
    """LLM service exception - 3100"""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = 3100


class MiotServiceException(ExternalServiceException):
    """Mi Home service exception - 3200"""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = 3200


class MiotOAuthException(MiotServiceException):
    """Mi Home OAuth authentication exception - 3201"""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = 3201


class HaServiceException(ExternalServiceException):
    """Home Assistant service exception - 3300"""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = 3300


class AgentWebhookException(ExternalServiceException):
    """Agent webhook service exception - 3400"""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = 3400
