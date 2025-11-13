# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT http client.
"""
# pylint: disable=too-many-arguments, too-many-positional-arguments
# pylint: disable=too-many-instance-attributes
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode
import aiohttp

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .common import calc_group_id
from .const import (
    MIHOME_HTTP_API_PUBKEY, MIHOME_HTTP_X_CLIENT_BIZID, MIHOME_HTTP_API_TIMEOUT,
    MIHOME_HTTP_X_ENCRYPT_TYPE, MIHOME_HTTP_USER_AGENT, OAUTH2_API_HOST_DEFAULT,
    OAUTH2_AUTH_URL, OAUTH2_CLIENT_ID, PROJECT_CODE
)
from .error import MIoTErrorCode, MIoTHttpError, MIoTOAuth2Error
from .types import (
    MIoTAppNotify, MIoTDeviceInfo, MIoTGetPropertyParam, MIoTHomeInfo, MIoTManualSceneInfo,
    MIoTOauthInfo, MIoTRoomInfo, MIoTUserInfo, MIoTSetPropertyParam, MIoTActionParam
)

_LOGGER = logging.getLogger(__name__)

TOKEN_EXPIRES_TS_RATIO = 0.7


class MIoTOAuth2Client:
    """OAuth2 agent url, default: product env."""
    _main_loop: asyncio.AbstractEventLoop
    _session: aiohttp.ClientSession
    _oauth_host: str
    _redirect_uri: str
    _device_id: str
    _state: str

    def __init__(
            self, redirect_uri: str, cloud_server: str, uuid: str, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        """Initialize."""
        self._main_loop = loop or asyncio.get_running_loop()
        if not redirect_uri:
            raise MIoTOAuth2Error("invalid redirect_uri")
        if not cloud_server:
            raise MIoTOAuth2Error("invalid cloud_server")
        if not uuid:
            raise MIoTOAuth2Error("invalid uuid")

        self._redirect_uri = redirect_uri
        if cloud_server == "cn":
            self._oauth_host = OAUTH2_API_HOST_DEFAULT
        else:
            self._oauth_host = f"{cloud_server}.{OAUTH2_API_HOST_DEFAULT}"
        self._device_id = f"{PROJECT_CODE}.{uuid}"
        self._state = hashlib.sha1(f"d={self._device_id}".encode("utf-8")).hexdigest()
        self._session = aiohttp.ClientSession(loop=self._main_loop)

    @property
    def state(self) -> str:
        """Get the current state."""
        return self._state

    async def deinit_async(self) -> None:
        """Deinit the client."""
        if self._session and not self._session.closed:
            await self._session.close()

    def set_redirect_uri(self, redirect_uri: str) -> None:
        """Set the redirect url."""
        if not isinstance(redirect_uri, str) or redirect_uri.strip() == "":
            raise MIoTOAuth2Error("invalid redirect_uri")
        self._redirect_uri = redirect_uri

    def gen_auth_url(
        self,
        redirect_uri: Optional[str] = None,
        scope: Optional[List] = None,
        skip_confirm: Optional[bool] = False,
    ) -> str:
        """Get auth url.

        Args:
            redirect_uri
            scope (list, optional):
                开放数据接口权限 ID，可以传递多个，用空格分隔，具体值可以参考开放
                [数据接口权限列表](https://dev.mi.com/distribute/doc/details?pId=1518).
                Defaults to None.\n
            skip_confirm (bool, optional):
                默认值为true，授权有效期内的用户在已登录情况下，不显示授权页面，直接通过。
                如果需要用户每次手动授权，设置为false. Defaults to True.\n

        Returns:
            str: OAuth2 url
        """
        params: Dict = {
            "redirect_uri": redirect_uri or self._redirect_uri,
            "client_id": OAUTH2_CLIENT_ID,
            "response_type": "code",
            "device_id": self._device_id,
            "state": self._state
        }
        if scope:
            params["scope"] = " ".join(scope).strip()
        params["skip_confirm"] = skip_confirm
        encoded_params = urlencode(params)

        return f"{OAUTH2_AUTH_URL}?{encoded_params}"

    async def check_state_async(self, redirect_state: str) -> bool:
        """Check the redirect state."""
        return self._state == redirect_state

    async def __get_token_async(self, data) -> MIoTOauthInfo:
        http_res = await self._session.get(
            url=f"https://{self._oauth_host}/app/v2/{PROJECT_CODE}/oauth/get_token",
            params={"data": json.dumps(data)},
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=MIHOME_HTTP_API_TIMEOUT
        )
        if http_res.status == 401:
            _LOGGER.error(
                "unauthorized(401), oauth/get_token, %s -> %s", data, await http_res.text(encoding="utf-8"))
            raise MIoTOAuth2Error("unauthorized(401)", MIoTErrorCode.CODE_OAUTH_UNAUTHORIZED)
        if http_res.status != 200:
            _LOGGER.error(
                "invalid http code %d, oauth/get_token, %s -> %s",
                http_res.status, data, await http_res.text(encoding="utf-8"))
            raise MIoTOAuth2Error(f"invalid http status code, {http_res.status}")

        res_str = await http_res.text()
        res_obj = json.loads(res_str)
        if (
            not res_obj
            or res_obj.get("code", None) != 0
            or "result" not in res_obj
            or not all(key in res_obj["result"] for key in ["access_token", "refresh_token", "expires_in"])
        ):
            raise MIoTOAuth2Error(f"invalid http response, {res_str}, {json.dumps(data)}")

        return MIoTOauthInfo(
            access_token=res_obj["result"]["access_token"],
            refresh_token=res_obj["result"]["refresh_token"],
            expires_ts=int(time.time() + (res_obj["result"].get("expires_in", 0)*TOKEN_EXPIRES_TS_RATIO))
        )

    async def get_access_token_async(self, code: str) -> MIoTOauthInfo:
        """Get access token by authorization code.

        Args:
            code (str): OAuth2 redirect code.

        Returns:
            MIoTOauthInfo: MIoT OAuth2 Info.
        """
        if not isinstance(code, str):
            raise MIoTOAuth2Error("invalid code")

        return await self.__get_token_async(data={
            "client_id": OAUTH2_CLIENT_ID,
            "redirect_uri": self._redirect_uri,
            "code": code,
            "device_id": self._device_id
        })

    async def refresh_access_token_async(self, refresh_token: str) -> MIoTOauthInfo:
        """Get access token by refresh token.

        Args:
            refresh_token (str): Refresh token.

        Returns:
            MIoTOauthInfo: MIoT OAuth2 Info.
        """
        if not isinstance(refresh_token, str):
            raise MIoTOAuth2Error("invalid refresh_token")

        return await self.__get_token_async(data={
            "client_id": OAUTH2_CLIENT_ID,
            "redirect_uri": self._redirect_uri,
            "refresh_token": refresh_token,
        })


class MIoTHttpClient:
    """MIoT http client."""
    # pylint: disable=inconsistent-quotes
    _GET_PROP_AGGREGATE_INTERVAL: float = 0.2
    _GET_PROP_MAX_REQ_COUNT = 150
    _main_loop: asyncio.AbstractEventLoop
    _session: aiohttp.ClientSession
    _random_aes_key: bytes
    _cipher: Cipher
    _client_secret_b64: str
    _host: str
    _base_url: str
    _access_token: str

    _get_prop_timer: Optional[asyncio.TimerHandle]
    _get_prop_list: Dict[str, Dict]
    # icon is persisted in the cache.
    _icon_map: Dict[str, str]

    def __init__(
            self, cloud_server: str,  access_token: str,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        """Initialize."""
        self._main_loop = loop or asyncio.get_running_loop()
        self._host = OAUTH2_API_HOST_DEFAULT
        self._base_url = ""
        self._access_token = ""

        self._get_prop_timer = None
        self._get_prop_list = {}
        self._icon_map = {}

        if (
            not isinstance(cloud_server, str)
            or not isinstance(access_token, str)
        ):
            raise MIoTHttpError("invalid params")

        self.update_http_header(cloud_server=cloud_server, access_token=access_token)

        self._session = aiohttp.ClientSession(loop=self._main_loop)
        self._random_aes_key = os.urandom(16)
        self._cipher = Cipher(
            algorithms.AES(self._random_aes_key),
            modes.CBC(self._random_aes_key),
            backend=default_backend())
        self._client_secret_b64 = base64.b64encode(
            load_pem_public_key(
                MIHOME_HTTP_API_PUBKEY.encode("utf-8"), default_backend()
            ).encrypt(plaintext=self._random_aes_key, padding=asym_padding.PKCS1v15())).decode("utf-8")  # type: ignore

    async def deinit_async(self) -> None:
        """Deinit the client."""
        if self._get_prop_timer:
            self._get_prop_timer.cancel()
            self._get_prop_timer = None
        for item in self._get_prop_list.values():
            fut: Optional[asyncio.Future] = item.get("fut", None)
            if fut:
                fut.cancel()
        self._get_prop_list.clear()
        if self._session and not self._session.closed:
            await self._session.close()

    def update_http_header(
        self, cloud_server: Optional[str] = None,
        access_token: Optional[str] = None
    ) -> None:
        """Update http header."""
        if isinstance(cloud_server, str):
            if cloud_server != "cn":
                self._host = f"{cloud_server}.{OAUTH2_API_HOST_DEFAULT}"
            self._base_url = f"https://{self._host}"
        if isinstance(access_token, str):
            self._access_token = access_token

    @property
    def __api_request_headers(self) -> Dict:
        return {
            "Content-Type": "text/plain",
            "User-Agent": MIHOME_HTTP_USER_AGENT,
            "X-Client-BizId": MIHOME_HTTP_X_CLIENT_BIZID,
            "X-Encrypt-Type": MIHOME_HTTP_X_ENCRYPT_TYPE,
            "X-Client-AppId": OAUTH2_CLIENT_ID,
            "X-Client-Secret": self._client_secret_b64,
            "Host": self._host,
            "Authorization": f"Bearer{self._access_token}",
        }

    def aes_encrypt_with_b64(self, data: Dict) -> str:
        """AES encrypt."""
        encryptor = self._cipher.encryptor()
        padder = sym_padding.PKCS7(128).padder()
        padded_data = padder.update(json.dumps(data).encode("utf-8")) + padder.finalize()
        encrypted = encryptor.update(padded_data) + encryptor.finalize()
        result = base64.b64encode(encrypted).decode("utf-8")
        # _LOGGER.info("aes encrypt, %s", result)
        return result

    def aes_decrypt_with_b64(self, data: str) -> Dict:
        """AES decrypt."""
        decryptor = self._cipher.decryptor()
        unpadder = sym_padding.PKCS7(128).unpadder()
        decrypted = decryptor.update(base64.b64decode(data)) + decryptor.finalize()
        unpadded_data = unpadder.update(decrypted) + unpadder.finalize()
        result = json.loads(unpadded_data.decode("utf-8"))
        # _LOGGER.info("aes decrypt, %s", result)
        return result

    async def __mihome_api_get_async(
        self, url_path: str, params: Dict,
        timeout: int = MIHOME_HTTP_API_TIMEOUT
    ) -> Dict:
        """Get data from mihome api with http get."""
        # pylint: disable=unused-private-member
        http_res = await self._session.get(
            url=f"{self._base_url}{url_path}",
            params=params,
            headers=self.__api_request_headers,
            timeout=timeout)
        if http_res.status == 401:
            _LOGGER.error(
                "mihome api get unauthorized(401), %s, %s -> %s",
                url_path, params, await http_res.text(encoding="utf-8"))
            raise MIoTHttpError(
                "mihome api get failed, unauthorized(401)", MIoTErrorCode.CODE_HTTP_INVALID_ACCESS_TOKEN)
        if http_res.status != 200:
            _LOGGER.error(
                "mihome api get failed, %s, %s, %s -> %s",
                http_res.status, url_path, params, await http_res.text(encoding="utf-8"))
            raise MIoTHttpError(f"mihome api get failed, {http_res.status}, {url_path}, {params}")
        res_str = await http_res.text()
        res_obj: Dict = self.aes_decrypt_with_b64(res_str)
        if res_obj.get("code", None) != 0:
            raise MIoTHttpError(f"invalid response code, {res_obj.get('code', None)}, {res_obj.get('message', '')}")
        # _LOGGER.debug("mihome api get, %s%s, %s -> %s", self._base_url, url_path, params, res_obj)
        return res_obj

    async def __mihome_api_post_async(
        self, url_path: str, data: Dict,
        timeout: int = MIHOME_HTTP_API_TIMEOUT
    ) -> Dict:
        """Get data from mihome api with http post."""
        http_res = await self._session.post(
            url=f"{self._base_url}{url_path}",
            data=self.aes_encrypt_with_b64(data),
            headers=self.__api_request_headers,
            timeout=timeout)
        if http_res.status == 401:
            _LOGGER.error(
                "mihome api post unauthorized(401), %s, %s, %s -> %s",
                url_path, data, self.__api_request_headers, await http_res.text(encoding="utf-8"))
            raise MIoTHttpError(
                "mihome api get failed, unauthorized(401)", MIoTErrorCode.CODE_HTTP_INVALID_ACCESS_TOKEN)
        if http_res.status != 200:
            _LOGGER.error(
                "mihome api post failed, %s, %s, %s, %s -> %s",
                http_res.status, url_path, data, self.__api_request_headers, await http_res.text(encoding="utf-8"))
            raise MIoTHttpError(f"mihome api post failed, {http_res.status}, {url_path}, {data}")
        res_str = await http_res.text()
        res_obj: Dict = self.aes_decrypt_with_b64(res_str)
        if res_obj.get("code", None) != 0:
            raise MIoTHttpError(f"invalid response code, {res_obj.get('code', None)}, {res_obj.get('message', '')}")
        # _LOGGER.debug("mihome api post, %s%s, %s -> %s", self._base_url, url_path, data, res_obj)
        return res_obj

    async def get_user_info_async(self) -> MIoTUserInfo:
        """Get user info."""
        http_res = await self._session.get(
            url="https://open.account.xiaomi.com/user/profile",
            params={
                "clientId": OAUTH2_CLIENT_ID,
                "token": self._access_token
            },
            headers={
                "content-type": "application/x-www-form-urlencoded"
            },
            timeout=MIHOME_HTTP_API_TIMEOUT
        )

        res_str = await http_res.text()
        res_obj = json.loads(res_str)
        if (
            not res_obj
            or res_obj.get("code", None) != 0
            or "data" not in res_obj
            or "unionId" not in res_obj["data"]
            or "miliaoNick" not in res_obj["data"]
        ):
            raise MIoTHttpError(f"invalid http response(user), {http_res.text}")

        res_api = await self.__mihome_api_post_async(
            url_path="/app/v2/oauth/get_uid_by_unionid",
            data={
                "union_id": res_obj["data"]["unionId"]
            },
        )
        if "result" not in res_api or not isinstance(res_api["result"], int):
            raise MIoTHttpError(f"invalid response result, {res_api}")

        return MIoTUserInfo(
            union_id=res_obj["data"]["unionId"],
            nickname=res_obj["data"]["miliaoNick"],
            icon=res_obj["data"].get("miliaoIcon", ""),
            uid=str(res_api["result"])
        )

    async def __get_dev_room_page_async(
        self, max_id: Optional[str] = None
    ) -> Dict:
        """Get dev room page."""
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/homeroom/get_dev_room_page",
            data={
                "start_id": max_id,
                "limit": 150,
            },
        )
        if "result" not in res_obj and "info" not in res_obj["result"]:
            raise MIoTHttpError(f"invalid response result, {res_obj}")
        home_list: Dict = {}
        for home in res_obj["result"]["info"]:
            if "id" not in home:
                _LOGGER.error("get dev room page error, invalid home, %s", home)
                continue
            home_list[str(home["id"])] = {"dids": home.get("dids", None) or [], "room_list": {}}
            for room in home.get("roomlist", []):
                if "id" not in room:
                    _LOGGER.error("get dev room page error, invalid room, %s", room)
                    continue
                home_list[str(home["id"])]["room_list"][str(room["id"])] = {"dids": room.get("dids", None) or []}
        if (
            res_obj["result"].get("has_more", False)
            and isinstance(res_obj["result"].get("max_id", None), str)
        ):
            next_list = await self.__get_dev_room_page_async(max_id=res_obj["result"]["max_id"])
            for home_id, info in next_list.items():
                home_list.setdefault(home_id, {"dids": [], "room_list": {}})
                home_list[home_id]["dids"].extend(info["dids"])
                for room_id, info in info["room_list"].items():
                    home_list[home_id]["room_list"].setdefault(room_id, {"dids": []})
                    home_list[home_id]["room_list"][room_id]["dids"].extend(info["dids"])

        return home_list

    async def get_homes_async(self, fetch_share_home: bool = False) -> Dict[str, MIoTHomeInfo]:
        """Get home infos."""
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/homeroom/gethome",
            data={
                "limit": 150,
                "fetch_share": fetch_share_home,
                "fetch_share_dev": fetch_share_home,
                "plat_form": 0,
                "app_ver": 9,
            },
        )
        if "result" not in res_obj:
            raise MIoTHttpError(f"invalid response result, {res_obj}")
        if "homelist" not in res_obj["result"]:
            raise MIoTHttpError(f"invalid response result.homelist, {res_obj}")

        home_infos: Dict[str, MIoTHomeInfo] = {}
        for home in [*res_obj["result"]["homelist"], *res_obj["result"].get("share_home_list", [])]:
            if "id" not in home or "name" not in home or "roomlist" not in home or "uid" not in home:
                continue
            uid = str(home["uid"])
            home_id = home["id"]
            home_infos[home_id] = MIoTHomeInfo(
                home_id=home_id,
                home_name=home["name"],
                share_home=home.get("shareflag", 0) == 1,
                dids=home.get("dids", []),
                room_list={
                    room["id"]: MIoTRoomInfo(
                        room_id=room["id"],
                        room_name=room["name"],
                        dids=room.get("dids", []),
                        create_ts=room.get("create_time", 0))
                    for room in home.get("roomlist", []) if "id" in room
                },
                create_ts=home.get("create_time", 0),
                uid=uid,
                group_id=calc_group_id(uid=uid, home_id=home_id),
                city_id=home.get("city_id", None),
                longitude=home.get("longitude", None),
                latitude=home.get("latitude", None),
                address=home.get("address", None))
        if (
            res_obj["result"].get("has_more", False)
            and isinstance(res_obj["result"].get("max_id", None), str)
        ):
            more_list = await self.__get_dev_room_page_async(max_id=res_obj["result"]["max_id"])
            for home_id, home_info in more_list.items():
                if home_id not in home_infos:
                    _LOGGER.info("unknown home, %s, %s", home_id, home_info)
                    continue
                home_infos[home_id].dids.extend(home_info.get("dids", []))
                for room_id, room_info in home_info.get("room_list", {}).items():
                    home_infos[home_id].room_list.setdefault(
                        room_id, MIoTRoomInfo(
                            room_id=room_id,
                            room_name="",
                            create_ts=room_info.get("create_time", 0),
                            dids=[]))
                    home_infos[home_id].room_list[room_id].dids.extend(room_info.get("dids", []))

        return home_infos

    async def __get_device_icon_async(self, model: str) -> Tuple[str, str]:
        """Get device icon."""
        http_res = await self._session.post(
            url=f"{self._base_url}/app/v2/productconfig/get_icon",
            data=self.aes_encrypt_with_b64({
                "icon_name": "icon_real",
                "model": model,
            }),
            headers=self.__api_request_headers,
            timeout=MIHOME_HTTP_API_TIMEOUT)
        if http_res.status not in [200, 302, 403]:
            raise MIoTHttpError(f"get icon failed, code={http_res.status}, model={model}")

        icon_url = str(http_res.url)
        self._icon_map[model] = icon_url
        return model, icon_url

    async def __get_device_icon_batch_async(self, models: Set[str]) -> Dict[str, str]:
        """Get device icon batch."""
        icons: Dict[str, str] = {}
        task_list = []
        for model in models:
            if model in self._icon_map:
                # _LOGGER.info(
                #     "get device icon from cache, %s, %s", model, self._icon_map[model])
                icons[model] = self._icon_map[model]
                continue
            task_list.append(self.__get_device_icon_async(model=model))
        if task_list:
            results = await asyncio.gather(*task_list, return_exceptions=True)
            for result in results:
                if isinstance(result, Tuple):
                    icons[result[0]] = result[1]
                    continue
                _LOGGER.error("get device icon failed, %s", result)
        return icons

    async def __get_urn_by_model_async(
        self, model: str, version: int = 0
    ) -> Optional[str]:
        """Get urn by model."""
        http_res = await self._session.get(
            url="https://miot-spec.org/internal/urn-by-model-version",
            params={
                "model": model,
                "version": 0
            },
            timeout=10)
        if http_res.status != 200:
            _LOGGER.info("get urn by model failed, %s, %s, %s", http_res.status, model, version)
            return None
        res_obj: Dict = await http_res.json()
        if not isinstance(res_obj, Dict):
            return None
        _LOGGER.debug("get urn by model, %s, %s", model, res_obj)
        return res_obj.get("urn", None)

    async def __get_device_list_page_async(
        self, dids: List[str], start_did: Optional[str] = None
    ) -> Dict[str, MIoTDeviceInfo]:
        """Get device list page."""
        req_data: Dict = {
            "limit": 200,
            "get_split_device": True,
            "dids": dids
        }
        if start_did:
            req_data["start_did"] = start_did
        device_infos: Dict[str, MIoTDeviceInfo] = {}
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/home/device_list_page",
            data=req_data
        )
        if "result" not in res_obj:
            raise MIoTHttpError(f"invalid response result, {res_obj}")
        res_obj = res_obj["result"]

        models: Set[str] = set()
        urn_buffer = {}
        for device in res_obj.get("list", []) or []:
            did = device.get("did", None)
            name = device.get("name", None)
            urn = device.get("spec_type", None)
            model = device.get("model", None)
            if model:
                models.add(model)
            if did is None or name is None:
                _LOGGER.info("invalid device, cloud, %s", device)
                continue
            if model is None:
                _LOGGER.info("missing the model field, cloud, %s", device)
                continue
            if urn is None:
                if model in urn_buffer:
                    urn = urn_buffer[model]
                else:
                    urn = await self.__get_urn_by_model_async(model=model)
                    urn_buffer[model] = urn
                if not urn:
                    _LOGGER.info("missing the urn field, cloud, %s", device)
                    continue

            device_infos[did] = MIoTDeviceInfo(
                did=did,
                name=name,
                urn=urn,
                model=model,
                uid=str(device["uid"]),
                connect_type=device.get("pid", -1),
                token=device.get("token", None),
                online=device.get("isOnline", False),
                icon=device.get("icon", None),
                parent_id=device.get("parent_id", None),
                manufacturer=model.split(".")[0],
                # 2: xiao-ai, 1: general speaker
                voice_ctrl=device.get("voice_ctrl", 0),
                rssi=device.get("rssi", None),
                pid=device.get("pid", None),
                local_ip=device.get("local_ip", None),
                ssid=device.get("ssid", None),
                bssid=device.get("bssid", None),
                order_time=device.get("orderTime", 0)
            )
            if isinstance(device.get("owner", None), Dict) and device["owner"]:
                device_infos[did].owner_id = str(device["owner"]["userid"])
                device_infos[did].owner_nickname = device["owner"]["nickname"]
            if isinstance(device.get("extra", None), Dict) and device["extra"]:
                device_infos[did].fw_version = device["extra"].get("fw_version", None)
                device_infos[did].mcu_version = device["extra"].get("mcu_version", None)
                device_infos[did].platform = device["extra"].get("platform", None)
                device_infos[did].is_set_pincode = device["extra"].get("isSetPincode", None)
                device_infos[did].pincode_type = device["extra"].get("pincodeType", None)

        # get device icon
        if models:
            icons = await self.__get_device_icon_batch_async(models=models)
            for device in device_infos.values():
                device.icon = icons.get(device.model, None)

        next_start_did = res_obj.get("next_start_did", None)
        if res_obj.get("has_more", False) and next_start_did:
            device_infos.update(await self.__get_device_list_page_async(
                dids=dids, start_did=next_start_did))
        return device_infos

    async def get_devices_with_dids_async(
        self, dids: List[str]
    ) -> Optional[Dict[str, MIoTDeviceInfo]]:
        """Get devices with dids.
        NOTICE: The obtained device information does not include household information."""
        results: List[Dict[str, MIoTDeviceInfo]] = await asyncio.gather(
            *[self.__get_device_list_page_async(dids=dids[index:index+150]) for index in range(0, len(dids), 150)])
        devices: Dict[str, MIoTDeviceInfo] = {}
        for result in results:
            if result is None:
                return None
            devices.update(result)
        return devices

    async def get_devices_async(
        self, home_infos: Optional[List[MIoTHomeInfo]] = None,
        fetch_share_home: bool = False
    ) -> Dict[str, MIoTDeviceInfo]:
        """Get devices."""
        local_homes = home_infos
        if not local_homes:
            local_homes = (await self.get_homes_async(fetch_share_home=fetch_share_home)).values()
        # Set device home info.
        devices: Dict = {}
        for home_info in local_homes:
            home_id: str = home_info.home_id
            home_name: str = home_info.home_name
            group_id: str = home_info.group_id
            devices.update({did: {
                "home_id": home_id,
                "home_name": home_name,
                "room_id": home_id,
                "room_name": home_name,
                "group_id": group_id
            } for did in home_info.dids or []})
            for room_id, room_info in home_info.room_list.items():
                devices.update({
                    did: {
                        "home_id": home_id,
                        "home_name": home_name,
                        "room_id": room_id,
                        "room_name": room_info.room_name,
                        "group_id": group_id
                    } for did in room_info.dids})

        dids = sorted(list(devices.keys()))
        results = await self.get_devices_with_dids_async(dids=dids)
        if results is None:
            raise MIoTHttpError("get devices failed")

        for did in list(results.keys()):
            home_info = devices.pop(did, None)
            if home_info:
                results[did] = results[did].model_copy(update=home_info)
            # Whether sub devices
            match_str = re.search(r"\.s\d+$", did)
            if not match_str:
                continue

            device = results.pop(did)
            parent_did = did.replace(match_str.group(), "")
            if parent_did in results:
                results[parent_did].sub_devices[match_str.group()[1:]] = device
            else:
                _LOGGER.error("unknown sub devices, %s, %s", did, parent_did)
        return results

    async def get_props_async(self, params: List[MIoTGetPropertyParam]) -> List:
        """Get props."""
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/miotspec/prop/get",
            data={
                "datasource": 1,
                "params": [param.model_dump() for param in params]
            },
        )
        if "result" not in res_obj:
            raise MIoTHttpError("invalid response result")
        return res_obj["result"]

    async def __get_prop_async(self, param: MIoTGetPropertyParam) -> Any:
        """Get prop."""
        results = await self.get_props_async(params=[param])
        if not results:
            return None
        result = results[0]
        if "value" not in result:
            return None
        return result["value"]

    async def __get_prop_handler(self) -> bool:
        """Get prop handler."""
        props_req: Set[str] = set()
        props_buffer: List[MIoTGetPropertyParam] = []

        for key, item in self._get_prop_list.items():
            if item.get("tag", False):
                continue
            # NOTICE: max req prop
            if len(props_req) >= self._GET_PROP_MAX_REQ_COUNT:
                break
            item["tag"] = True
            props_buffer.append(item["param"])
            props_req.add(key)

        if not props_buffer:
            _LOGGER.error("get prop error, empty request list")
            return False
        results = await self.get_props_async(props_buffer)

        for result in results:
            if not all(key in result for key in ["did", "siid", "piid", "value"]):
                continue
            key = f"{result['did']}.{result['siid']}.{result['piid']}"
            prop_obj = self._get_prop_list.pop(key, None)
            if prop_obj is None:
                _LOGGER.info("get prop error, key not exists, %s", result)
                continue
            prop_obj["fut"].set_result(result["value"])
            props_req.remove(key)

        for key in props_req:
            prop_obj = self._get_prop_list.pop(key, None)
            if prop_obj is None:
                continue
            prop_obj["fut"].set_result(None)
        if props_req:
            _LOGGER.info("get prop from cloud failed, %s", props_req)

        if self._get_prop_list:
            self._get_prop_timer = self._main_loop.call_later(
                self._GET_PROP_AGGREGATE_INTERVAL,
                lambda: self._main_loop.create_task(self.__get_prop_handler()))
        else:
            self._get_prop_timer = None
        return True

    async def get_prop_async(
        self, param: MIoTGetPropertyParam, immediately: bool = False
    ) -> Any:
        """Get prop."""
        if immediately:
            return await self.__get_prop_async(param=param)
        key: str = f"{param.did}.{param.siid}.{param.piid}"
        prop_obj = self._get_prop_list.get(key, None)
        if prop_obj:
            return await prop_obj["fut"]
        fut = self._main_loop.create_future()
        self._get_prop_list[key] = {
            "param": param,
            "fut": fut
        }
        if self._get_prop_timer is None:
            self._get_prop_timer = self._main_loop.call_later(
                delay=self._GET_PROP_AGGREGATE_INTERVAL,
                callback=lambda: self._main_loop.create_task(self.__get_prop_handler())
            )

        return await fut

    async def set_prop_async(
        self, param: MIoTSetPropertyParam
    ) -> Dict:
        """Set prop."""
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/miotspec/prop/set",
            data={
                "params": [param.model_dump()]
            },
            timeout=15
        )
        if "result" not in res_obj and len(res_obj["result"]) != 1:
            raise MIoTHttpError(f"invalid response result, {res_obj}", MIoTErrorCode.CODE_MIPS_INVALID_RESULT)

        return res_obj["result"][0]

    async def set_props_async(self, params: List[MIoTSetPropertyParam]) -> List:
        """Set props.
        params = [{"did": "xxxx", "siid": 2, "piid": 1, "value": False}]
        """
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/miotspec/prop/set",
            data={
                "params": [param.model_dump() for param in params]
            },
            timeout=15
        )
        if "result" not in res_obj:
            raise MIoTHttpError("invalid response result")

        return res_obj["result"]

    async def action_async(self, param: MIoTActionParam) -> Dict:
        """Action.
        params = {"did": "xxxx", "siid": 2, "aiid": 1, "in": []}
        "in": in_list  # [item["value"] for item in in_list]
        """
        # NOTICE: Non-standard action param
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/miotspec/action",
            data={
                "params": param.model_dump(by_alias=True)
            },
            timeout=15
        )
        if "result" not in res_obj:
            raise MIoTHttpError("invalid response result")

        return res_obj["result"]

    async def __get_manual_scenes_with_home_id_async(
        self, uid: str, home_id: str, scene_ids: Optional[List[int]] = None
    ) -> Dict[str, MIoTManualSceneInfo]:
        """Get manual scene list."""
        req_data: Dict = {
            "home_id": home_id,
            "owner_uid": uid,
            "source": "zkp",
            "get_type": 2
        }
        if isinstance(scene_ids, List) and scene_ids:
            req_data["scene_ids"] = scene_ids
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/appgateway/miot/appsceneservice/AppSceneService/GetManualSceneList",
            data=req_data
        )
        if "result" not in res_obj:
            raise MIoTHttpError(f"invalid response result, {res_obj}")

        return {
            scene["scene_id"]: MIoTManualSceneInfo(
                scene_id=scene["scene_id"],
                scene_name=scene["scene_name"],
                uid=uid,
                update_ts=scene["update_time"],
                home_id=home_id,
                room_id=scene.get("room_id", None),
                icon=scene.get("icon", None),
                enable=scene.get("enable", True),
                dids=scene.get("dids", []),
                pd_ids=scene.get("pd_ids", [])

            ) for scene in res_obj["result"]}

    async def get_manual_scenes_async(
        self, home_infos: Optional[List[MIoTHomeInfo]] = None,
        fetch_share_home: bool = False
    ) -> Dict[str, MIoTManualSceneInfo]:
        """Get manual scene list."""
        local_homes = home_infos
        if not local_homes:
            local_homes = (await self.get_homes_async(fetch_share_home=fetch_share_home)).values()

        manual_scenes: Dict[str, MIoTManualSceneInfo] = {}
        for home_info in local_homes:
            manual_scenes.update(
                await self.__get_manual_scenes_with_home_id_async(uid=home_info.uid, home_id=home_info.home_id))
        return manual_scenes

    async def run_manual_scene_async(
        self, scene_info: MIoTManualSceneInfo
    ) -> bool:
        """Run manual scene."""
        req_data: Dict = {
            "owner_uid": scene_info.uid,
            "scene_id": scene_info.scene_id,
            "scene_type": 2
        }
        if scene_info.home_id:
            req_data["home_id"] = scene_info.home_id
        if scene_info.room_id:
            req_data["room_id"] = scene_info.room_id

        res_obj = await self.__mihome_api_post_async(
            url_path="/app/appgateway/miot/appsceneservice/AppSceneService/NewRunScene",
            data=req_data
        )
        if "result" not in res_obj:
            raise MIoTHttpError(f"invalid response result, {res_obj}")

        return res_obj["result"]

    async def send_app_notify_async(self, notify_id: str) -> bool:
        """Send app notify."""
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/oauth/send_push",
            data={
                "key": notify_id
            }
        )
        if "result" not in res_obj:
            raise MIoTHttpError(f"invalid response result, {res_obj}")

        return res_obj["result"]

    async def create_app_notify_async(self, text: str) -> str:
        """Create Xiaomi Home app notify."""
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/oauth/save_text",
            data={
                "text": text
            }
        )
        if "result" not in res_obj and isinstance(res_obj["result"], str):
            raise MIoTHttpError(f"invalid response result, {res_obj}")
        return res_obj["result"]

    async def get_app_notifies_async(
        self, notify_ids: str | List[str] | None = None
    ) -> Dict[str, MIoTAppNotify]:
        """Get Xiaomi Home app notify."""
        keys = None
        if notify_ids is None:
            keys = []
        elif isinstance(notify_ids, str):
            keys = [notify_ids]
        elif isinstance(notify_ids, List):
            keys = notify_ids
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/oauth/get_texts",
            data={
                "keys": keys
            }
        )
        if "result" not in res_obj or isinstance(res_obj, List):
            raise MIoTHttpError(f"invalid response result, {res_obj}")
        notifies = {}
        for notify in res_obj["result"]:
            notifies[notify["key"]] = MIoTAppNotify(
                id_=notify["key"],
                text=notify["text"],
                create_ts=notify["create_time"])
        return notifies

    async def delete_app_notifies_async(self, notify_ids: str | List[str]) -> bool:
        """Delete Xiaomi Home app notify."""
        res_obj = await self.__mihome_api_post_async(
            url_path="/app/v2/oauth/del_texts",
            data={
                "keys": notify_ids if isinstance(notify_ids, List) else [notify_ids]
            }
        )
        if "result" not in res_obj:
            raise MIoTHttpError(f"invalid response result, {res_obj}")
        return res_obj["result"]
