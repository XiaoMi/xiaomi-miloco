# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
Base OAuth2 client.
"""
import asyncio
import json
import logging
import time
from typing import Optional
from urllib.parse import urlencode
from uuid import uuid4
import aiohttp

from .types import BaseOAuthInfo

_LOGGER = logging.getLogger(__name__)


class BaseOAuth2Client:
    """Base OAuth2 client."""
    _AUTH_AUTHORIZATION_PATH: str = "/auth/authorize"
    _AUTH_TOKEN_PATH: str = "/auth/token"
    _AUTH_NAME_GRANT_TYPE: str = "grant_type"
    _AUTH_API_TIMEOUT: int = 30
    _AUTH_TOKEN_EXPIRES_TS_RATIO: float = 0.7

    _main_loop: asyncio.AbstractEventLoop
    _session: aiohttp.ClientSession
    _base_url: str
    _client_id: str
    _redirect_uri: str

    _client_secret: Optional[str]
    _state: Optional[str]

    def __init__(
            self, base_url: str, client_id: str, redirect_uri: str, client_secret: Optional[str] = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        """Initialize."""
        self._main_loop = loop or asyncio.get_running_loop()
        if not base_url or not client_id or not redirect_uri:
            raise ValueError("invalid prams")

        self._session = aiohttp.ClientSession(loop=self._main_loop)
        self._base_url = base_url
        self._client_id = client_id
        self._redirect_uri = redirect_uri

        self._client_secret = client_secret
        self._state = None

    @property
    def state(self) -> Optional[str]:
        """Get the current state."""
        return self._state

    def validate_state(self, state: str) -> bool:
        """Validate the state."""
        return self._state == state

    async def deinit_async(self) -> None:
        """Deinit the client."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def gen_auth_url_async(
        self, redirect_uri: Optional[str] = None, state: Optional[str] = None, **kwargs
    ) -> str:
        """Get auth url.

        Args:
            redirect_uri: Redirect URL.
            state: State parameter.

        Returns:
            str: OAuth2 url
        """

        if not state:
            self._state = uuid4().hex
        encoded_params = urlencode({
            "redirect_uri": redirect_uri or self._redirect_uri,
            "client_id": self._client_id,
            "response_type": "code",
            "state": state or self._state,
            **kwargs
        })

        return f"{self._base_url}{self._AUTH_AUTHORIZATION_PATH}?{encoded_params}"

    async def __get_token_async(self, params) -> BaseOAuthInfo:
        """Get access token."""
        data = {
            "client_id": self._client_id,
            **params
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        http_res = await self._session.post(
            url=f"{self._base_url}{self._AUTH_TOKEN_PATH}",
            data=data,
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "user-agent": "dueros.baidu.com"
            },
            timeout=self._AUTH_API_TIMEOUT
        )
        # invalid auth(400)
        # unauthorized(401)
        # user not active(403)
        if http_res.status != 200:
            _LOGGER.error(
                "invalid http code(%d), %s, %s -> %s",
                http_res.status, self._AUTH_TOKEN_PATH, data, await http_res.text(encoding="utf-8"))
            raise ValueError(
                f"invalid http code, {http_res.status}")

        res_obj = await http_res.json()
        if (
            not res_obj
            or "access_token" not in res_obj
            or "expires_in" not in res_obj
        ):
            raise ValueError(
                f"invalid http response, {json.dumps(res_obj)}")

        if "refresh_token" not in res_obj:
            if "refresh_token" in params:
                res_obj["refresh_token"] = params["refresh_token"]
            else:
                raise ValueError("invalid response, no refresh_token")

        return BaseOAuthInfo(
            access_token=res_obj["access_token"],
            refresh_token=res_obj["refresh_token"],
            expires_ts=int(
                time.time() + (res_obj.get("expires_in", 0)*self._AUTH_TOKEN_EXPIRES_TS_RATIO)))

    async def get_access_token_async(self, code: str) -> BaseOAuthInfo:
        """Get access token by authorization code.

        Args:
            code (str): OAuth2 redirect code.

        Returns:
            HAOAuthInfo: Home Assistant OAuth2 Info.
        """
        if not isinstance(code, str):
            raise ValueError("invalid code")

        return await self.__get_token_async(
            params={
                "code": code,
                self._AUTH_NAME_GRANT_TYPE: "authorization_code",
                "state": self._state,
                "redirect_uri": self._redirect_uri
            }
        )

    async def refresh_access_token_async(self, refresh_token: str) -> BaseOAuthInfo:
        """Get access token by refresh token.

        Args:
            refresh_token (str): Refresh token.

        Returns:
            HAOAuthInfo: Home Assistant OAuth2 Info.
        """
        if not isinstance(refresh_token, str):
            raise ValueError("invalid refresh_token")

        return await self.__get_token_async(
            params={
                "refresh_token": refresh_token,
                self._AUTH_NAME_GRANT_TYPE: "refresh_token"
            }
        )
