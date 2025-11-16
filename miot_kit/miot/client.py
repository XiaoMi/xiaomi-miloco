# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT Client.
"""
# pylint: disable=too-many-arguments, too-many-positional-arguments
# pylint: disable=too-many-instance-attributes
import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

from miot.error import MIoTClientError
from miot.spec import MIoTSpecParser
from miot.storage import MIoTStorage

from .const import CLOUD_SERVER_DEFAULT, SYSTEM_LANGUAGE_DEFAULT
from .types import (
    MIoTAppNotify,
    MIoTCameraExtraInfo,
    MIoTCameraInfo,
    MIoTCameraStatus,
    MIoTDeviceInfo,
    MIoTHomeInfo,
    MIoTLanDeviceInfo,
    MIoTManualSceneInfo,
    MIoTOauthInfo,
    MIoTUserInfo
)
from .i18n import MIoTI18n
from .cloud import MIoTOAuth2Client, MIoTHttpClient
from .lan import MIoTLan
from .network import MIoTNetwork
from .camera import MIoTCamera, MIoTCameraInstance, get_camera_extra_info

_LOGGER = logging.getLogger(__name__)


class MIoTClient:
    """MIoT Client."""
    _main_loop: asyncio.AbstractEventLoop
    _cloud_server: str
    _lang: str

    _uuid: str
    _redirect_uri: str
    _oauth_info: Optional[MIoTOauthInfo]

    _cache_path: Optional[str]

    _storage: Optional[MIoTStorage]
    _spec_parser: Optional[MIoTSpecParser]
    _i18n: MIoTI18n
    _oauth_client: MIoTOAuth2Client
    _http_client: MIoTHttpClient
    _network_client: MIoTNetwork
    _lan_client: MIoTLan
    _camera_client: MIoTCamera

    _cameras_buffer: Optional[Dict[str, MIoTCameraInfo]]
    _last_lan_ping_ts: int
    _callbacks_lan_device_status_changed: Dict[str, Callable[[str, MIoTLanDeviceInfo], Coroutine]]
    _device_buffer: Optional[Dict[str, MIoTDeviceInfo]]

    _init_done: bool

    def __init__(
        self,
        uuid: str,
        redirect_uri: str,
        cache_path: Optional[str] = None,
        lang: Optional[str] = None,
        oauth_info: Optional[MIoTOauthInfo | Dict] = None,
        cloud_server: Optional[str] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        """MIoT Client init.
        **MUST call `init_async` after initialization.**
        Args:
            uuid (str): random uuid, it can be generated using the `uuid.uuid4().hex` command.
            redirect_uri (str): redirect url, http://127.0.0.1 or ...
            oauth_info (Optional[MIoTOauthInfo], optional): OAuth2 info, call the interface
                (`get_access_token_async` or `refresh_access_token_async`) to generate. 
                Defaults to None.
            cloud_server (Optional[str], optional): The area where the server is located, 
                Such as `cn`, `ru`. Defaults to None.
            loop (Optional[asyncio.AbstractEventLoop], optional): Main loop. Defaults to None.

        """
        if not uuid:
            raise ValueError("uuid is required")
        if not redirect_uri:
            raise ValueError("redirect_uri is required")
        self._uuid = uuid
        self._redirect_uri = redirect_uri
        self._cache_path = cache_path
        if oauth_info:
            self._oauth_info = MIoTOauthInfo(**oauth_info) if isinstance(oauth_info, Dict) else oauth_info
        else:
            self._oauth_info = None
        self._main_loop = loop or asyncio.get_event_loop()
        self._cloud_server = cloud_server or CLOUD_SERVER_DEFAULT
        self._lang = lang or SYSTEM_LANGUAGE_DEFAULT

        self._cameras_buffer = None
        self._last_lan_ping_ts = 0
        self._callbacks_lan_device_status_changed = {}
        self._device_buffer = None

        self._init_done = False

    async def __aexit__(self, exc_type, exc, tb):
        await self.deinit_async()

    @property
    def i18n(self) -> MIoTI18n:
        """I18n translate."""
        return self._i18n

    @property
    def storage(self) -> MIoTStorage:
        """Storage."""
        if not self._storage:
            raise MIoTClientError("storage is not initialized, maybe cache_path is None")
        return self._storage

    @property
    def spec_parser(self) -> MIoTSpecParser:
        """Spec parser."""
        if not self._spec_parser:
            raise MIoTClientError("spec_parser is not initialized, maybe cache_path is None")
        return self._spec_parser

    @property
    def cameras_info(self) -> Dict[str, MIoTCameraInfo]:
        """Cameras info."""
        return self._cameras_buffer or {}

    @property
    def camera_client(self) -> MIoTCamera:
        """Camera client.
        """
        return self._camera_client

    @property
    def http_client(self) -> MIoTHttpClient:
        """HTTP client."""
        return self._http_client

    async def __on_lan_device_status_changed(
        self, did: str, info: MIoTLanDeviceInfo, ctx: Any = None
    ) -> None:
        # pylint: disable=unused-argument
        if self._cameras_buffer and did in self._cameras_buffer:
            self._cameras_buffer[did].lan_status = info.online
            self._cameras_buffer[did].local_ip = info.ip
        if did in self._callbacks_lan_device_status_changed:
            await self._callbacks_lan_device_status_changed[did](did, info)

    async def init_async(self) -> None:
        """Init the client."""
        if self._init_done:
            _LOGGER.warning("client already init")
            return
        self._i18n = MIoTI18n(lang=self._lang, loop=self._main_loop)
        if self._cache_path:
            self._storage = MIoTStorage(self._cache_path, loop=self._main_loop)
            # await self._storage.init_async()
            self._spec_parser = MIoTSpecParser(storage=self._storage, lang=self._lang, loop=self._main_loop)
            await self._spec_parser.init_async()
        await self._i18n.init_async()
        self._oauth_client = MIoTOAuth2Client(
            redirect_uri=self._redirect_uri,
            cloud_server=self._cloud_server,
            uuid=self._uuid,
            loop=self._main_loop)
        self._http_client = MIoTHttpClient(
            cloud_server=self._cloud_server,
            access_token=self._oauth_info.access_token if self._oauth_info else "",
            loop=self._main_loop)
        self._network_client = MIoTNetwork(loop=self._main_loop)
        await self._network_client.init_async()
        self._lan_client = MIoTLan(
            net_ifs=list((await self._network_client.get_info_async()).keys()),
            network=self._network_client,
            loop=self._main_loop)
        await self._lan_client.init_async()
        await self._lan_client.register_status_changed_async(
            key="miot_client", handler=self.__on_lan_device_status_changed)
        self._camera_client = MIoTCamera(
            cloud_server=self._cloud_server,
            access_token=self._oauth_info.access_token if self._oauth_info else "",
            loop=self._main_loop)
        await self._camera_client.init_async()
        self._init_done = True

    async def deinit_async(self) -> None:
        """Deinit the client."""
        if not self._init_done:
            _LOGGER.info("client not init")
            return
        await self._oauth_client.deinit_async()
        await self._http_client.deinit_async()
        await self._camera_client.deinit_async()
        await self._lan_client.unregister_status_changed_async("miot_client")
        await self._lan_client.deinit_async()
        await self._network_client.deinit_async()
        self._init_done = False

    async def gen_oauth_url_async(
            self,
            redirect_uri: Optional[str] = None
    ) -> str:
        """Generate OAuth2 URL.

        Args:
            redirect_uri (Optional[str]): redirect url, 
                Such as `http://127.0.0.1`, `https://xxxx.api.io.mi.com`

        Returns:
            str: OAuth2 URL.
        """
        return self._oauth_client.gen_auth_url(redirect_uri=redirect_uri)

    async def get_access_token_async(self, code: str, state: str) -> MIoTOauthInfo:
        """Get access token by authorization code.

        Args:
            code (str): OAuth2 redirect code.
            state (str): Redirect state.

        Returns:
            MIoTOauthInfo: MIoT OAuth2 Info.
        """
        if not await self._oauth_client.check_state_async(redirect_state=state):
            raise ValueError("state is invalid")
        self._oauth_info = await self._oauth_client.get_access_token_async(code=code)
        self._http_client.update_http_header(
            access_token=self._oauth_info.access_token)
        await self._camera_client.update_access_token_async(
            access_token=self._oauth_info.access_token)
        await self.get_user_info_async()
        return self._oauth_info

    async def refresh_access_token_async(self, refresh_token: str) -> MIoTOauthInfo:
        """Refresh access token.

        Args:
            refresh_token (str): Refresh token.

        Returns:
            MIoTOauthInfo: MIoT OAuth2 Info.
        """
        oauth_info = await self._oauth_client.refresh_access_token_async(refresh_token)
        if self._oauth_info:
            oauth_info.user_info = self._oauth_info.user_info
            self._oauth_info = oauth_info
        else:
            self._oauth_info = oauth_info
            await self.get_user_info_async()
        self._http_client.update_http_header(access_token=self._oauth_info.access_token)
        await self._camera_client.update_access_token_async(access_token=self._oauth_info.access_token)
        return self._oauth_info

    async def check_token_async(self) -> bool:
        """Get user information to check if the token is valid.

        Returns:
            bool: Check result.
        """
        try:
            await self._http_client.get_user_info_async()
        except Exception:  # pylint: disable=broad-exception-caught
            return False
        return True

    async def get_homes_async(self, fetch_share_home: bool = False) -> Dict[str, MIoTHomeInfo]:
        """Get homes info.

        Args:
            fetch_share_home (bool, optional): Whether fetch share home. Defaults to False.

        Returns:
            Dict[str, MIoTHomeInfo]: Home info list
        """
        return await self._http_client.get_homes_async(fetch_share_home=fetch_share_home)

    async def get_user_info_async(self) -> MIoTUserInfo:
        """Get user info.

        Returns:
            MIoTUserInfo: User info, include uid, nickname, icon, union_id, etc.
        """
        user_info: MIoTUserInfo = await self._http_client.get_user_info_async()
        if self._oauth_info:
            self._oauth_info.user_info = user_info
        return user_info

    async def get_devices_async(
        self, home_list: Optional[List[MIoTHomeInfo]] = None,
        fetch_share_home: bool = False
    ) -> Dict[str, MIoTDeviceInfo]:
        """Get devices info.

        Args:
            home_list (Optional[List[MIoTHomeInfo]], optional): Home list. Defaults to None.
            fetch_share_home (bool, optional): Whether fetch share home. Defaults to False.

        Returns:
            Dict[str, MIoTDeviceInfo]: Devices info.
        """
        devices: Dict[str, MIoTDeviceInfo] = await self._http_client.get_devices_async(
            home_infos=home_list, fetch_share_home=fetch_share_home)
        if not self._device_buffer:
            self._device_buffer = devices
            return self._device_buffer
        if not self._device_buffer:
            # Empty buffer
            return self._device_buffer

        # Update lan information.
        lan_devices = await self._lan_client.get_devices_async()
        for did in self._device_buffer.keys():
            if did not in devices:
                self._device_buffer.pop(did, None)
                continue
            device = devices.pop(did, None)
            self._device_buffer[did].__dict__.update(device.__dict__)
            if did in lan_devices:
                self._device_buffer[did].lan_status = lan_devices[did].online
                self._device_buffer[did].local_ip = lan_devices[did].ip
            else:
                self._device_buffer[did].lan_status = False
                self._device_buffer[did].local_ip = None

        for did in devices.keys():
            self._device_buffer[did] = devices.pop(did)
            if did in lan_devices:
                self._device_buffer[did].lan_status = lan_devices[did].online
                self._device_buffer[did].local_ip = lan_devices[did].ip
            else:
                self._device_buffer[did].lan_status = False
                self._device_buffer[did].local_ip = None

        return self._device_buffer

    async def get_manual_scenes_async(
        self, home_list: Optional[List[MIoTHomeInfo]] = None,
        fetch_share_home: bool = False
    ) -> Dict[str, MIoTManualSceneInfo]:
        """Get manual scenes info.

        Args:
            home_list (Optional[List[MIoTHomeInfo]], optional): Home list. Defaults to None.
            fetch_share_home (bool, optional): Whether fetch share home. Defaults to False.

        Returns:
            Dict[str, MIoTManualSceneInfo]: Manual scenes info.
        """
        return await self._http_client.get_manual_scenes_async(home_infos=home_list, fetch_share_home=fetch_share_home)

    async def run_manual_scene_async(self, scene_info: MIoTManualSceneInfo) -> bool:
        """Run manual scene.

        Args:
            scene_info(MIoTManualSceneInfo): Scene info, MUST include uid, scene_id, home_id.

        Returns:
            bool: Run manual scene result.
        """
        return await self._http_client.run_manual_scene_async(scene_info=scene_info)

    async def get_cameras_async(
        self, home_list: Optional[List[MIoTHomeInfo]] = None, fetch_share_home: bool = False
    ) -> Dict[str, MIoTCameraInfo]:
        """Get cameras info.

        Args:
            home_list (Optional[List[MIoTHomeInfo]], optional): Home list. Defaults to None.
            fetch_share_home (bool, optional): Whether fetch share home. Defaults to False.
            skip_cloud (bool, optional): Whether skip cloud. Defaults to False.
                NOTICE: If there is no local cache, a direct request will be sent to the cloud.

        Returns:
            Dict[str, MIoTDeviceInfo]: Camera info.
        """
        camera_extra_info: MIoTCameraExtraInfo = await get_camera_extra_info()
        cameras: Dict[str, MIoTCameraInfo] = {}
        devices = await self.get_devices_async(home_list=home_list, fetch_share_home=fetch_share_home)
        for did, device_info in devices.items():
            if device_info.model in camera_extra_info.blacklist:
                continue
            if device_info.model.split('.')[1] not in camera_extra_info.support_classes:
                continue
            cameras[did] = MIoTCameraInfo(
                **device_info.model_dump(),
                channel_count=(
                    camera_extra_info.extra_info[device_info.model].channel_count
                    if device_info.model in camera_extra_info.extra_info else 1
                ),
                camera_status=MIoTCameraStatus.DISCONNECTED
            )
            cameras[did].online = False
            cameras[did].local_ip = None
        self._cameras_buffer = cameras
        for did, camera_info in self._cameras_buffer.items():
            # Camera connect status
            if did in self._camera_client.camera_map:
                camera_info.camera_status = await self._camera_client.get_camera_status_async(did)
            else:
                camera_info.camera_status = MIoTCameraStatus.DISCONNECTED
            # TODO: Dirty logic, Need to optimize upper-level business judgment logic
            camera_info.online = camera_info.camera_status == MIoTCameraStatus.CONNECTED

        return self._cameras_buffer

    async def refresh_cameras_status_async(self) -> None:
        """Refresh cameras status with lan ping."""
        ts_now = int(time.time())
        if ts_now - self._last_lan_ping_ts < MIoTLan.OT_PROBE_INTERVAL_MIN:
            return
        await self._lan_client.ping_async()
        self._last_lan_ping_ts = ts_now

    async def create_camera_instance_async(
        self, camera_info: MIoTCameraInfo,
        frame_interval: int = 500,
        enable_hw_accel: bool = True
    ) -> MIoTCameraInstance:
        """Create camera instance.

        Args:
            camera_info (MIoTCameraInfo): Camera info.

        Returns:
            MIoTCameraInstance: MIoT camera instance.
        """
        return await self._camera_client.create_camera_async(
            camera_info=camera_info,
            frame_interval=frame_interval,
            enable_hw_accel=enable_hw_accel
        )

    async def get_camera_instance_async(self, did: str) -> Optional[MIoTCameraInstance]:
        """Get camera instance by did.

        Args:
            did (str): Device id.

        Returns:
            Optional[MIoTCameraInstance]: MIoT camera instance.
        """
        return await self._camera_client.get_camera_instance_async(did)

    async def register_lan_device_changed_async(
        self, did: str, callback: Callable[[str, MIoTLanDeviceInfo], Coroutine]
    ) -> bool:
        """Register lan device changed callback.

        Args:
            did (str): Device id.
            callback (Callable[[str, MIoTLanDeviceInfo], Coroutine]): Callback.

        Returns:
            bool: Register result. 
        """
        self._callbacks_lan_device_status_changed[did] = callback
        return True

    async def unregister_lan_device_changed_async(self, did: str) -> bool:
        """Unregister lan device changed callback.

        Args:
            did (str): Device id.

        Returns:
            bool: Unregister result.
        """
        self._callbacks_lan_device_status_changed.pop(did, None)
        return True

    async def register_camera_status_changed_async(
        self, did: str, callback: Callable[[str, MIoTCameraStatus], Coroutine]
    ) -> int:
        """Register camera status changed callback.

        Args:
            did (str): Device id.
            callback (Callable[[str, MIoTCameraStatus], Coroutine]): Callback.

        Returns:
            bool: Register result.
        """
        return await self._camera_client.register_status_changed_async(did=did, callback=callback)

    async def unregister_camera_status_changed_async(self, did: str) -> None:
        """Unregister camera status changed callback.

        Args:
            did (str): Device id.

        Returns:
            bool: Unregister result.
        """
        return await self._camera_client.unregister_status_changed_async(did=did)

    async def send_app_notify_async(self, notify_id: str) -> bool:
        """Send app notify.

        Args:
            notify_id (str): Notify id, get from `create_app_notify_async`.

        Returns:
            bool: Send result.
        """
        return await self._http_client.send_app_notify_async(notify_id=notify_id)

    async def create_app_notify_async(self, text: str) -> str:
        """Create app notify.

        Args:
            text (str): Notify text.

        Returns:
            str: Notify id.
        """
        return await self._http_client.create_app_notify_async(text=text)

    async def get_app_notifies_async(
        self, notify_ids: str | List[str] | None = None
    ) -> Dict[str, MIoTAppNotify]:
        """Get app notifies.

        Args:
            notify_ids (str | List[str] | None, optional): Notify ids. Defaults to None.

        Returns:
            Dict[str, MIoTAppNotify]: App notifies.
        """
        return await self._http_client.get_app_notifies_async(notify_ids=notify_ids)

    async def delete_app_notifies_async(self, notify_ids: str | List[str]) -> bool:
        """Delete app notifies.

        Args:
            notify_ids (str | List[str]): Notify ids.

        Returns:
            bool: Delete result.
        """
        return await self._http_client.delete_app_notifies_async(notify_ids=notify_ids)

    async def send_app_notify_once_async(self, content: str) -> bool:
        """Send app notify once.

        Args:
            content (str): Notify content. This interface will automatically create a notify message and 
            then automatically delete it after the sending is completed.

        Returns:
            bool: Send result.
        """
        notify_id: str = await self._http_client.create_app_notify_async(text=content)
        if not notify_id:
            _LOGGER.error("create app notify failed")
            return False
        result = await self._http_client.send_app_notify_async(notify_id=notify_id)
        # delete app notify.
        if not await self._http_client.delete_app_notifies_async(notify_ids=notify_id):
            _LOGGER.warning("delete app notify failed, %s", notify_id)
        return result
