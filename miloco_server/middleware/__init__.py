# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Middleware module
"""
from miloco_server.middleware.exceptions import (
    BaseAPIException,
    BusinessException,
    ConflictException,
    ResourceNotFoundException,
    ExternalServiceException,
    LLMServiceException,
    MiotServiceException,
    MiotOAuthException,
    HaServiceException,
    AuthenticationException,
    AuthorizationException,
    ValidationException,
    BadRequestException
)

from miloco_server.middleware.auth_middleware import (
    create_access_token,
    verify_jwt_token,
    verify_token,
    verify_websocket_token,
    set_auth_cookie,
    clear_auth_cookie,
    invalidate_all_tokens,
    is_token_valid,
    ADMIN_USERNAME
)

__all__ = [
    "BaseAPIException",
    "BusinessException",
    "ConflictException",
    "ResourceNotFoundException",
    "ExternalServiceException",
    "LLMServiceException",
    "MiotServiceException",
    "MiotOAuthException",
    "HaServiceException",
    "AuthenticationException",
    "AuthorizationException",
    "ValidationException",
    "BadRequestException",
    # Authentication middleware functions
    "create_access_token",
    "verify_jwt_token",
    "verify_token",
    "verify_websocket_token",
    "set_auth_cookie",
    "clear_auth_cookie",
    "invalidate_all_tokens",
    "is_token_valid",
    "ADMIN_USERNAME"
]
