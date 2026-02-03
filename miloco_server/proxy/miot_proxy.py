# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""MIoT proxy module for handling Xiaomi IoT device related operations."""

import asyncio
import copy
import json
import logging
import time
from typing import Callable, Coroutine, Optional

from pydantic_core import to_jsonable_python
from miot.client import MIoTClient
from miot.types import MIoTOauthInfo, MIoTCameraInfo, MIoTCameraStatus, MIoTDeviceInfo, MIoTManualSceneInfo, MIoTUserInfo
from miot.camera import MIoTCameraInstance
from miot.rtsp_camera import RtspCameraInfo, RTSPCamera
from miot.rtsp_server import RtspServer

from miloco_server.config import MIOT_CACHE_DIR, CAMERA_CONFIG, RTSP_CAMERA_CONFIG, RTSP_SERVER_CONFIG
from miloco_server.dao.kv_dao import AuthConfigKeys, KVDao, DeviceInfoKeys
from miloco_server.schema.miot_schema import CameraImgSeq
from miloco_server.schema.rtsp_camera_schema import RtspCameraConfig
from miloco_server.utils.carmera_vision_handler import (
    CameraVisionHandler,
    RTSPEnabledCameraVisionHandler,
    RtspCameraVisionHandler,
    BaseCameraVisionHandler,
)


logger = logging.getLogger(__name__)

class MiotProxy:
    """Xiaomi IoT proxy class responsible for handling MIoT device related operations."""
    def __init__(self,
                 uuid: str,
                 redirect_uri: str,
                 kv_dao: KVDao,
                 cloud_server: Optional[str] = None,
                 rtsp_cameras: Optional[list[RtspCameraConfig | dict]] = None,
                 ):
        self._kv_dao = kv_dao
        self.init_miot_info_dict()
        self._token_refresh_task: Optional[asyncio.Task] = None
        self._rtsp_camera_info_dict: dict[str, RtspCameraInfo] = {}

        self._miot_client = MIoTClient(
            uuid=uuid,
            redirect_uri=redirect_uri,
            cache_path=str(MIOT_CACHE_DIR),
            oauth_info=self._oauth_info,
            cloud_server=cloud_server,
        )

        self._token_refresh_task = None
        self._frame_interval: int = CAMERA_CONFIG["frame_interval"]
        self._camera_img_cache_max_size: int = CAMERA_CONFIG["camera_img_cache_max_size"]
        self._rtsp_camera_configs: list[RtspCameraConfig] = [
            cfg if isinstance(cfg, RtspCameraConfig) else RtspCameraConfig.model_validate(cfg)
            for cfg in (rtsp_cameras or RTSP_CAMERA_CONFIG or [])
        ]

        # two times cache ttl, at least 1 second
        # frame_interval * cache_max_size / 1000 * 2 = seconds
        self._camera_img_cache_ttl: int = max(1, int(self._frame_interval * self._camera_img_cache_max_size / 1000 * 2))
        self._camera_img_managers: dict[str, BaseCameraVisionHandler] = {}
        self._rtsp_camera_client: Optional[RTSPCamera] = None

        # Initialize RTSP Server
        self._rtsp_server: Optional[RtspServer] = None
        if RTSP_SERVER_CONFIG.get("enabled", True):
            try:
                rtsp_port = RTSP_SERVER_CONFIG.get("port", 8554)
                self._rtsp_server = RtspServer(port=rtsp_port)
                self._rtsp_server.start()
                logger.info(
                    "RTSP Server started on port %s ",
                    rtsp_port
                )
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("Failed to start RTSP Server: %s", e)
        else:
            logger.info("RTSP Server is disabled in configuration")

        if self._rtsp_camera_configs:
            try:
                self._rtsp_camera_client = RTSPCamera(frame_interval=self._frame_interval)
                logger.info("RTSP camera client initialized")
            except FileNotFoundError as exc:
                logger.warning("RTSP library not found, will mark RTSP cameras offline: %s", exc)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("Failed to initialize RTSP camera client: %s", exc, exc_info=True)

    @property
    def miot_client(self) -> MIoTClient:
        return self._miot_client

    @classmethod
    async def create_miot_proxy(cls, uuid: str, redirect_uri: str, kv_dao: KVDao,
                              cloud_server: Optional[str] = None,
                              rtsp_cameras: Optional[list[RtspCameraConfig | dict]] = None) -> "MiotProxy":
        instance = cls(uuid, redirect_uri, kv_dao, cloud_server, rtsp_cameras)
        await instance.init_miot_info()
        instance._token_refresh_task = asyncio.create_task(instance._start_token_refresh_task())
        logger.info("MiotProxy initialization successful, oauth_info: %s", instance._oauth_info)
        return instance


    async def init_miot_info(self):
        await self._miot_client.init_async()

        if self._oauth_info:
            await self._check_and_refresh_token()
            await self.refresh_miot_info()


    async def refresh_miot_info(self) -> dict:
        """
        Refresh MiOT all information
        
        Returns:
            dict: Dictionary containing result of each refresh operation
        """
        result = {
            "cameras": False,
            "rtsp_cameras": False,
            "scenes": False,
            "user_info": False,
            "devices": False
        }

        camera_info_dict = await self.refresh_cameras()
        result["cameras"] = camera_info_dict is not None
        result["rtsp_cameras"] = bool(self._rtsp_camera_info_dict)

        scene_info_dict = await self.refresh_scenes()
        result["scenes"] = scene_info_dict is not None

        user_info = await self.refresh_user_info()
        result["user_info"] = user_info is not None

        device_info_dict = await self.refresh_devices()
        result["devices"] = device_info_dict is not None

        logger.info("MiOT info refresh completed: %s", result)
        return result


    def init_miot_info_dict(self):
        self._camera_info_dict: dict[str, MIoTCameraInfo] ={
            did: MIoTCameraInfo.model_validate(camera_info)
            for did, camera_info in json.loads(self._kv_dao.get(DeviceInfoKeys.CAMERA_INFO_KEY) or "{}").items()}
        self._device_info_dict: dict[str, MIoTDeviceInfo] ={
            did: MIoTDeviceInfo.model_validate(device_info)
            for did, device_info in json.loads(self._kv_dao.get(DeviceInfoKeys.DEVICE_INFO_KEY) or "{}").items()}
        self._scene_info_dict: dict[str, MIoTManualSceneInfo] = {
            scene_id: MIoTManualSceneInfo.model_validate(scene_info)
            for scene_id, scene_info in json.loads(self._kv_dao.get(DeviceInfoKeys.SCENE_INFO_KEY) or "{}").items()}

        user_info_str = self._kv_dao.get(DeviceInfoKeys.USER_INFO_KEY)
        if user_info_str:
            self._user_info: Optional[MIoTUserInfo] = MIoTUserInfo.model_validate_json(user_info_str)
        else:
            self._user_info = None

        oauth_info_str = self._kv_dao.get(AuthConfigKeys.MIOT_TOKEN_INFO_KEY)
        if oauth_info_str:
            self._oauth_info = MIoTOauthInfo.model_validate_json(oauth_info_str)
        else:
            self._oauth_info = None


    def get_recent_camera_img(self, camera_id: str, channel: int, recent_count: int) -> CameraImgSeq | None:
        manager = self._camera_img_managers.get(camera_id)
        if not manager:
            logger.warning("Camera %s not found in managers", camera_id)
            return None
        if recent_count > self._camera_img_cache_max_size or recent_count <= 0:
            logger.warning(
                "recent_count is out of range, camera_id: %s, channel: %s, "
                "recent_count: %s, camera_img_cache_max_size: %s",
                camera_id, channel, recent_count, self._camera_img_cache_max_size
            )
        return manager.get_recents_camera_img(channel, recent_count)


    async def start_camera_raw_stream(self, camera_id: str, channel: int,
                                    callback: Callable[[str, bytes, int, int, int], Coroutine]):
        manager = self._camera_img_managers.get(camera_id)
        if not manager:
            logger.warning("Camera %s not found in managers", camera_id)
            return
        await manager.register_raw_stream(callback, channel)
        logger.info("Successfully started camera raw stream, camera_id: %s, channel: %s", camera_id, channel)


    async def stop_camera_raw_stream(self, camera_id: str, channel: int):
        """
        Stop camera raw video stream

        Args:
            camera_id: Camera device ID
            channel: Channel number, default is 0
        """
        manager = self._camera_img_managers.get(camera_id)
        if not manager:
            logger.warning("Camera %s not found in managers", camera_id)
            return

        try:
            await manager.unregister_raw_stream(channel)
            logger.info("Successfully stopped camera raw video stream, camera_id: %s, channel: %s", camera_id, channel)
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to stop camera raw video stream: %s", e)
            raise


    async def _create_camera_img_manager(self, camera_info: MIoTCameraInfo) -> CameraVisionHandler | None:
        camera_instance = await self._get_camera_instance(camera_info)
        if camera_instance is not None:
            await camera_instance.start_async(enable_reconnect=True)

            # Use RTSP-enabled handler if RTSP server is running
            if self._rtsp_server:
                camera_img_manager = RTSPEnabledCameraVisionHandler(
                    camera_info, camera_instance, self._rtsp_server,
                    max_size=self._camera_img_cache_max_size, ttl=self._camera_img_cache_ttl
                )
            else:
                camera_img_manager = CameraVisionHandler(
                    camera_info,
                    camera_instance,
                    max_size=self._camera_img_cache_max_size,
                    ttl=self._camera_img_cache_ttl
                )

            self._camera_img_managers[camera_info.did] = camera_img_manager
            return camera_img_manager
        else:
            logger.error("Camera instance for %s is None, skipping", camera_info.did)
            return None


    async def _create_rtsp_camera_manager(self, camera_info: RtspCameraInfo) -> RtspCameraVisionHandler | None:
        if not self._rtsp_camera_client:
            camera_info.camera_status = MIoTCameraStatus.DISCONNECTED
            camera_info.online = False
            return None
        try:
            camera_instance = await self._rtsp_camera_client.create_camera_async(
                camera_info, frame_interval=self._frame_interval
            )
            # keep rtsp status synced into manager cache
            await camera_instance.register_status_changed_async(self._on_rtsp_status_changed, multi_reg=True)
            await camera_instance.start_async(enable_audio=camera_info.enable_audio, enable_reconnect=True)
            camera_img_manager = RtspCameraVisionHandler(
                camera_info, camera_instance, max_size=self._camera_img_cache_max_size, ttl=self._camera_img_cache_ttl
            )
            self._camera_img_managers[camera_info.did] = camera_img_manager
            return camera_img_manager
        except FileNotFoundError as exc:
            camera_info.camera_status = MIoTCameraStatus.DISCONNECTED
            camera_info.online = False
            logger.warning("RTSP lib not found for camera %s: %s", camera_info.did, exc)
            return None
        except Exception as e: # pylint: disable=broad-exception-caught
            camera_info.camera_status = MIoTCameraStatus.DISCONNECTED
            camera_info.online = False
            logger.error("Failed to create RTSP camera instance %s: %s", camera_info.did, e, exc_info=True)
            return None


    async def _get_camera_instance(self, camera_info: MIoTCameraInfo) -> Optional[MIoTCameraInstance]:
        try:
            return await self._miot_client.create_camera_instance_async(
                camera_info, frame_interval=self._frame_interval
            )
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to get camera instance: %s", e)
            return None


    async def get_cameras(self) -> dict[str, MIoTCameraInfo | RtspCameraInfo]:
        if not self._camera_info_dict:
            logger.warning("No camera info dict found, refreshing cameras")
            await self.refresh_cameras()
        if self._rtsp_camera_configs and not self._rtsp_camera_info_dict:
            await self._refresh_rtsp_cameras()
        return {**self._rtsp_camera_info_dict, **self._camera_info_dict}


    async def get_camera_dids(self) -> list[str]:
        """
        Get all available camera device ID list

        Returns:
            list[str]: Camera device ID list

        """
        camera_dict: Optional[dict[str, MIoTCameraInfo | RtspCameraInfo]] = await self.get_cameras()
        if not camera_dict:
            logger.warning("Unable to get camera list")
            return []

        camera_dids = list(camera_dict.keys())
        logger.debug("Retrieved %d camera device IDs", len(camera_dids))
        return camera_dids

    async def get_devices(self) -> dict[str, MIoTDeviceInfo]:
        if not self._device_info_dict:
            await self.refresh_devices()
        return self._device_info_dict


    async def refresh_cameras(self) -> dict[str, MIoTCameraInfo | RtspCameraInfo] | None:
        miot_ok = True
        rtsp_ok = True
        try:
            await self._refresh_miot_cameras()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            miot_ok = False
            logger.error("Failed to refresh MiOT cameras: %s", exc, exc_info=True)

        if self._rtsp_camera_configs:
            try:
                await self._refresh_rtsp_cameras()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                rtsp_ok = False
                logger.error("Failed to refresh RTSP cameras: %s", exc, exc_info=True)

        expected_dids = set(self._camera_info_dict.keys()) | set(self._rtsp_camera_info_dict.keys())
        await self._cleanup_removed_cameras(expected_dids)
        if not expected_dids:
            return None
        if not miot_ok:
            logger.warning("MiOT cameras refresh failed this round; returning cached data")
        if self._rtsp_camera_configs and not rtsp_ok:
            logger.warning("RTSP cameras refresh failed this round; returning cached data")
        return {**self._rtsp_camera_info_dict, **self._camera_info_dict}

    async def _refresh_miot_cameras(self) -> None:
        try:
            cameras = await self._miot_client.get_cameras_async()
            cameras = copy.deepcopy(cameras)
            for camera_did, camera_info in cameras.items():
                manager = self._camera_img_managers.get(camera_did)
                if isinstance(manager, CameraVisionHandler):
                    await manager.update_camera_info(camera_info)
                else:
                    await self._create_camera_img_manager(camera_info)

            for camera_did, manager in list(self._camera_img_managers.items()):
                if isinstance(manager, CameraVisionHandler) and camera_did not in cameras:
                    await manager.destroy()
                    del self._camera_img_managers[camera_did]
            self._camera_info_dict = cameras
            self._kv_dao.set(DeviceInfoKeys.CAMERA_INFO_KEY, json.dumps(to_jsonable_python(cameras)))
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to refresh cameras: %s", e)

    async def _refresh_rtsp_cameras(self) -> None:
        rtsp_info: dict[str, RtspCameraInfo] = {}
        for cfg in self._rtsp_camera_configs:
            cfg_info = cfg.to_rtsp_camera_info()
            manager = self._camera_img_managers.get(cfg_info.did)
            if not self._rtsp_camera_client:
                cfg_info.online = False
                cfg_info.camera_status = MIoTCameraStatus.DISCONNECTED
                rtsp_info[cfg_info.did] = cfg_info
                continue
            if isinstance(manager, RtspCameraVisionHandler):
                # Preserve runtime status/online while refreshing static fields from config.
                current = manager.camera_info
                merged = current.model_copy(update={
                    "name": cfg_info.name,
                    "rtsp_url": cfg_info.rtsp_url,
                    "model": cfg_info.model,
                    "vendor": cfg_info.vendor,
                    "home_name": cfg_info.home_name,
                    "room_name": cfg_info.room_name,
                    "icon": cfg_info.icon,
                    "use_tcp": cfg_info.use_tcp,
                })
                await manager.update_camera_info(merged)
                cfg_info = manager.camera_info
            else:
                manager = await self._create_rtsp_camera_manager(cfg_info)
                if manager:
                    cfg_info = manager.camera_info
                else:
                    cfg_info.online = False
                    cfg_info.camera_status = MIoTCameraStatus.DISCONNECTED

            rtsp_info[cfg_info.did] = cfg_info

        for camera_did, manager in list(self._camera_img_managers.items()):
            if isinstance(manager, RtspCameraVisionHandler) and camera_did not in rtsp_info:
                await manager.destroy()
                del self._camera_img_managers[camera_did]

        self._rtsp_camera_info_dict = rtsp_info

    async def _cleanup_removed_cameras(self, expected_dids: set[str]) -> None:
        for camera_did, manager in list(self._camera_img_managers.items()):
            if camera_did in expected_dids:
                continue
            await manager.destroy()
            del self._camera_img_managers[camera_did]
            # Remove from RTSP server if exists
            if self._rtsp_server:
                self._rtsp_server.remove_stream(camera_did)

    async def _on_rtsp_status_changed(self, did: str, status: MIoTCameraStatus) -> None:
        """Sync RTSP status into cached info and handler."""
        manager = self._camera_img_managers.get(did)
        if not isinstance(manager, RtspCameraVisionHandler):
            return
        manager.camera_info.camera_status = status
        manager.camera_info.online = status in (
            MIoTCameraStatus.CONNECTED,
            MIoTCameraStatus.CONNECTING,
            MIoTCameraStatus.RE_CONNECTING,
        )
        self._rtsp_camera_info_dict[did] = manager.camera_info

    async def refresh_devices(self) -> dict[str, MIoTDeviceInfo] | None:
        try:
            devices = await self._miot_client.get_devices_async()
            self._device_info_dict = devices
            self._kv_dao.set(DeviceInfoKeys.DEVICE_INFO_KEY, json.dumps(to_jsonable_python(devices)))
            return devices
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to refresh devices: %s", e)
            return None

    async def refresh_scenes(self) -> dict[str, MIoTManualSceneInfo] | None:
        try:
            scenes = await self._miot_client.get_manual_scenes_async()
            self._scene_info_dict = scenes
            self._kv_dao.set(DeviceInfoKeys.SCENE_INFO_KEY, json.dumps(to_jsonable_python(scenes)))
            return scenes
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to get all scenes: %s", e)
            return None

    async def get_all_scenes(self) -> dict[str, MIoTManualSceneInfo] | None:
        if not self._scene_info_dict:
            await self.refresh_scenes()
        return self._scene_info_dict

    async def execute_miot_scene(self, scene_id: str) -> bool:
        try:
            scene_info = self._scene_info_dict[scene_id]
            return await self._miot_client.run_manual_scene_async(scene_info=scene_info)
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to execute miot scene: %s", e)
            return False

    async def send_app_notify(self, app_notify_id: str) -> bool:
        try:
            return await self._miot_client.send_app_notify_async(app_notify_id)
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to send app notify: %s", e)
            return False

    async def check_token_valid(self) -> bool:
        try:
            return await self._miot_client.check_token_async()
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to check token valid: %s", e)
            raise

    async def refresh_user_info(self):
        try:
            user_info = await self._miot_client.get_user_info_async()
            self._user_info = user_info
            self._kv_dao.set(DeviceInfoKeys.USER_INFO_KEY, json.dumps(to_jsonable_python(user_info)))
            return user_info
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to refresh user info: %s", e)
            return None

    async def get_user_info(self) -> Optional[MIoTUserInfo]:
        if not self._user_info:
            await self.refresh_user_info()
        return self._user_info

    async def get_miot_login_url(self) -> str:
        url = await self._miot_client.gen_oauth_url_async()
        logger.info("Generated MIoT login URL: %s", url)
        return url

    async def get_miot_app_notify_id(self, content: str) -> str | None:
        try:
            app_notify_id = await self._miot_client.http_client.create_app_notify_async(content)
            logger.info("get_miot_app_notify_id app_notify_id: %s", app_notify_id)
            return app_notify_id
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to get miot app notify id: %s", e)
            return None


    async def get_miot_auth_info(self, code: str, state: str) -> MIoTOauthInfo:
        try:
            oauth_info = await self._miot_client.get_access_token_async(code=code, state=state)
            logger.info(
                "Retrieved MIoT auth info, code: %s, state: %s, token info: %s",
                code, state, oauth_info
            )
            self.reset_miot_token_info(oauth_info)
            asyncio.create_task(self.refresh_miot_info())
            return oauth_info
        except Exception as e: # pylint: disable=broad-exception-caught
            logger.error("Failed to get Xiaomi home token info, %s", e)
            raise e

    def reset_miot_token_info(self, miot_token_info: MIoTOauthInfo):
        """
        Reset persistent Mi Home token information
        """
        self._oauth_info = miot_token_info
        self._kv_dao.set(AuthConfigKeys.MIOT_TOKEN_INFO_KEY, miot_token_info.model_dump_json())
        logger.info("Token information updated, new expiration time: %s", miot_token_info.expires_ts)

    async def refresh_xiaomi_home_token_info(self) -> MIoTOauthInfo:
        try:
            if not self._oauth_info:
                raise ValueError("No oauth_info found")
            oauth_info = await self._miot_client.refresh_access_token_async(
                refresh_token=self._oauth_info.refresh_token
            )
            logger.info("Successfully refreshed Xiaomi home token info: %s", oauth_info)
            self.reset_miot_token_info(oauth_info)
            await asyncio.sleep(3)
            await self.refresh_miot_info()
            return oauth_info
        except Exception as e: # pylint: disable=broad-exception-caught
            self._oauth_info = None
            logger.error("Failed to refresh Xiaomi home token info: %s", e, exc_info=True)

    async def _start_token_refresh_task(self):
        """
        Start scheduled token refresh task
        """
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._check_and_refresh_token()
            except Exception as e: # pylint: disable=broad-exception-caught
                logger.error("Scheduled token refresh task exception: %s", e)
                await asyncio.sleep(60)  # Wait 1 minute after error before continuing

    async def _check_and_refresh_token(self):
        """
        Check if token is about to expire, refresh if needed
        """
        if not self._oauth_info:
            return

        current_time = int(time.time())
        expires_ts = self._oauth_info.expires_ts

        # Refresh token if it expires within 30 minutes
        if expires_ts - current_time <= 1800:  # 1800 seconds = 30 minutes
            logger.info(
                "Token is about to expire, starting refresh. Current time: %s, Expiration time: %s",
                current_time, expires_ts
            )
            await self.refresh_xiaomi_home_token_info()
            logger.info("Token refresh completed successfully")
