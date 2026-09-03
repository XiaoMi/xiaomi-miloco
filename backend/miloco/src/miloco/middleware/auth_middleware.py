# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Authentication middleware
Provides service token verification for HTTP and WebSocket connections
"""

import logging

from fastapi import Request
from fastapi.websockets import WebSocket

from miloco.config import get_settings
from miloco.middleware.exceptions import AuthenticationException

logger = logging.getLogger(__name__)

BEARER_PREFIX = "Bearer "


def _extract_bearer_token(authorization: str | None) -> str | None:
    """Extract token from 'Bearer <token>' format."""
    if authorization and authorization.startswith(BEARER_PREFIX):
        return authorization[len(BEARER_PREFIX) :]
    return None


def verify_token(request: Request) -> None:
    r"""Verify service token from Authorization header (Bearer format).

    HTTP 鉴权**只**接受标准 ``Authorization: Bearer <token>`` 头部。
    不支持 ``?token=…`` URL query 参数——webUI 同源部署后 fetch 会自动从
    ``window.__MILOCO_TOKEN__`` 读 token 加到 header，没有走 URL 的需要；
    URL token 历史上是 vite proxy 时代的 dev 兼容路径，已废弃。

    （WebSocket 鉴权另行用 ``verify_websocket_token``，因为浏览器原生 WS API
    不能设 header，必须走 ``?token=…``——这是浏览器约束不是设计选择。）

    **返回值约束**：本依赖恒返回 ``None``——鉴权通过就 return、不通过就抛。若将来
    改成返回真实的调用者标识，**返回前必须剥掉 ``\r`` / ``\n``**：多处接口把注入
    进来的这个值直接当 ``logger`` 的 ``%s`` 参数记「接口被调用」，带换行即可在日志
    里伪造出整行。改这里的人不一定会去翻那些调用点，所以约束写在源头。
    """
    service_token = get_settings().server.token
    if not service_token:
        return

    token = _extract_bearer_token(request.headers.get("Authorization"))
    if token != service_token:
        # 不写 received / expected token 全文 / 前缀 —— 攻击者构造 brute force 时
        # log 给出 8 字符前缀 = 大幅缩小搜索空间。仅记 path + 是否带 token，
        # 排查靠 client_ip + 时间戳即可。
        logger.warning(
            "Auth failed: path=%s has_token=%s",
            request.url.path,
            token is not None,
        )
        raise AuthenticationException("Invalid or missing service token")


def verify_token_query_fallback(request: Request) -> None:
    """与 verify_token 同语义,但额外接受 `?token=...` query 参数.

    使用场景:
    - `<img src>` 拉 snapshot JPEG 时浏览器无法附带 Authorization header
    - EventSource (SSE) 同理不能设 header

    其它 HTTP endpoint 仍用 `verify_token`(只 header,安全分级).
    """
    service_token = get_settings().server.token
    if not service_token:
        return

    token = (
        _extract_bearer_token(request.headers.get("Authorization"))
        or request.query_params.get("token")
    )
    if token != service_token:
        logger.warning(
            "Auth failed (query fallback): path=%s has_token=%s",
            request.url.path,
            token is not None,
        )
        raise AuthenticationException("Invalid or missing service token")


def verify_websocket_token(websocket: WebSocket) -> None:
    r"""Verify service token for WebSocket connection.

    Browsers cannot set custom headers on the native ``WebSocket`` API, so we
    accept the token from either the standard ``Authorization: Bearer …``
    header (used by CLI / server-to-server callers) or the ``?token=…`` query
    parameter (used by browser pages like ``static/watch.html``).
    
    **返回值约束**：与 ``verify_token`` 同款——本依赖恒返回 ``None``。若将来改为
    返回真实的调用者标识，返回前必须剥掉 ``\r`` / ``\n``：注入进来的这个值会被
    接口直接当 ``logger`` 的 ``%s`` 参数记「接口被调用」，带换行即可在日志里伪造
    出整行。两条依赖注入的是同名参数、被同样地打进日志，约束因此两边都写。
    """
    service_token = get_settings().server.token
    if not service_token:
        return

    token = (
        _extract_bearer_token(websocket.headers.get("Authorization"))
        or websocket.query_params.get("token")
    )
    if token != service_token:
        raise AuthenticationException("Invalid or missing service token")
