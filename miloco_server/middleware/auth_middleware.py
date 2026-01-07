# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Authentication middleware
Provides JWT token creation, verification and management functionality
"""
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

import jwt
from fastapi import Request, Response, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocket

from miloco_server.config import JWT_CONFIG
from miloco_server.middleware.exceptions import AuthenticationException

logger = logging.getLogger(__name__)

# Global token invalidation timestamp
token_invalidation_time = int(time.time())

ADMIN_USERNAME = "admin"

class JWTConfig:
    """
    JWT configuration management class
    """
    _instance: Optional["JWTConfig"] = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Prevent re-initialization if already initialized
        if self.__class__._initialized:
            return

        # Initialize secret key
        config_secret_key = JWT_CONFIG["secret_key"]
        if config_secret_key == "your-super-secret-key-change-this":
            self._secret_key = secrets.token_urlsafe(32)
        else:
            self._secret_key = config_secret_key

        self._algorithm = JWT_CONFIG["algorithm"]
        self._access_token_expire_minutes = JWT_CONFIG["access_token_expire_minutes"]

        # Mark as initialized
        self.__class__._initialized = True

        # Log initialization (logging system should be ready at this point)
        logger.info("JWT configuration initialized - algorithm: %s, expire_minutes: %s",
                   self._algorithm, self._access_token_expire_minutes)

    @property
    def secret_key(self) -> str:
        """Get JWT secret key"""
        return self._secret_key

    @property
    def algorithm(self) -> str:
        """Get JWT algorithm"""
        return self._algorithm

    @property
    def access_token_expire_minutes(self) -> int:
        """Get access token expiration time in minutes"""
        return self._access_token_expire_minutes


# Global JWT configuration instance (lazy-initialized)
_jwt_config: Optional[JWTConfig] = None

def get_jwt_config() -> JWTConfig:
    """
    Get JWT configuration instance (lazy-initialized singleton)
    
    Returns:
        JWTConfig: JWT configuration instance
    """
    global _jwt_config
    if _jwt_config is None:
        _jwt_config = JWTConfig()
    return _jwt_config

def invalidate_all_tokens():
    """
    Invalidate all JWT tokens
    Implemented by setting global invalidation timestamp
    """
    global token_invalidation_time
    current_time = int(time.time())
    token_invalidation_time = current_time
    logger.info("All JWT tokens invalidated, invalidation timestamp: %s", current_time)

def is_token_valid(token_issued_at: int) -> bool:
    """
    Check if JWT token is valid

    Args:
        token_issued_at: JWT token issued timestamp (iat field)

    Returns:
        bool: True if token is valid, False if token is invalidated
    """
    # If token issued time is earlier than global invalidation time, token is invalid
    is_valid = token_issued_at > token_invalidation_time

    if not is_valid:
        logger.info("Token invalidated - issued at: %s, invalidation time: %s",
                    token_issued_at, token_invalidation_time)

    return is_valid

def create_access_token(username: str) -> str:
    """Create JWT access token"""
    jwt_config = get_jwt_config()
    expire = datetime.utcnow() + timedelta(minutes=jwt_config.access_token_expire_minutes)
    to_encode = {
        "sub": username,
        "exp": expire,
        "iat": int(time.time()),
    }
    encoded_jwt = jwt.encode(to_encode, jwt_config.secret_key, algorithm=jwt_config.algorithm)
    return encoded_jwt

def _verify_jwt_token_internal(token: Optional[str]) -> str:
    """
    Internal JWT verification logic, raises AuthenticationException

    Args:
        token: JWT token string, can be None

    Returns:
        str: Username

    Raises:
        AuthenticationException: Raised when authentication fails
    """
    if not token:
        raise AuthenticationException("Authentication token not found, please login first")

    try:
        jwt_config = get_jwt_config()
        payload = jwt.decode(token, jwt_config.secret_key, algorithms=[jwt_config.algorithm])
        username: str = payload.get("sub")
        # Get token issued time
        token_issued_at: int = payload.get("iat")

        if username is None or username != ADMIN_USERNAME:
            raise AuthenticationException("Invalid authentication token")

        # Check if token has been invalidated
        if not is_token_valid(token_issued_at):
            raise AuthenticationException("Authentication token has been invalidated, please login again")

        return username
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationException("Authentication token has expired, please login again") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationException("Invalid authentication token") from exc

def verify_jwt_token(token: Optional[str]) -> str:
    """
    Verify JWT token and return username (general verification function)

    Args:
        token: JWT token string, can be None

    Returns:
        str: Username

    Raises:
        HTTPException: Raised when authentication fails
    """
    try:
        # Internal verification logic, raises AuthenticationException
        return _verify_jwt_token_internal(token)
    except AuthenticationException as exc:
        # Catch AuthenticationException and convert to HTTPException
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message
        ) from exc

def verify_token(request: Request) -> str:
    """Verify JWT token and return username (from Cookie)"""
    # Get token from Cookie
    token = request.cookies.get("access_token")
    return verify_jwt_token(token)

def verify_websocket_token(websocket: WebSocket) -> str:
    """Verify JWT token for WebSocket connection"""
    # Get token from query parameters
    token = websocket.cookies.get("access_token")
    return verify_jwt_token(token)

def set_auth_cookie(response: Response, access_token: str) -> None:
    """
    Set authentication cookie

    Args:
        response: FastAPI Response object
        access_token: JWT access token
    """
    jwt_config = get_jwt_config()
    response.set_cookie(
        key="access_token",
        value=access_token,
        max_age=jwt_config.access_token_expire_minutes * 60,  # Convert to seconds
        httponly=True,  # Prevent XSS attacks
        secure=False,   # Set to False for development, True for production
        samesite="lax"  # CSRF protection
    )

def clear_auth_cookie(response: Response) -> None:
    """
    Clear authentication cookie

    Args:
        response: FastAPI Response object
    """
    response.delete_cookie(key="access_token")


class AuthStaticFiles(StaticFiles):
    """StaticFiles with authentication middleware"""
    async def __call__(self, scope, receive, send):
        request = Request(scope, receive=receive)
        verify_token(request)
        await super().__call__(scope, receive, send)
