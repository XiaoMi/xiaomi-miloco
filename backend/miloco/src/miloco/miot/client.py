# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""MIoT proxy module for handling Xiaomi IoT device related operations."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Callable, Coroutine

from av.audio.frame import AudioFrame
from av.video.frame import VideoFrame
from miot.camera import MIoTCameraInstance
from miot.client import MIoTClient
from miot.spec import MIoTSpecTypeLevel
from miot.types import (
    MIoTActionParam,
    MIoTCameraInfo,
    MIoTDeviceBindEvent,
    MIoTDeviceInfo,
    MIoTGetPropertyParam,
    MIoTLanDeviceInfo,
    MIoTManualSceneInfo,
    MIoTOauthInfo,
    MIoTSceneChangedEvent,
    MIoTSetPropertyParam,
    MIoTUserInfo,
)
from pydantic_core import to_jsonable_python

from miloco.config import get_settings
from miloco.database.kv_repo import AuthConfigKeys, DeviceInfoKeys, KVRepo
from miloco.miot.camera_handler import CameraVisionHandler
from miloco.miot.filter import is_home_allowed
from miloco.miot.mips_listeners import (
    BindEventListener,
    DeviceMetaEventListener,
    SceneEventListener,
)
from miloco.miot.schema import CameraImgSeq, normalize_sub_devices
from miloco.miot.welcome_service import DeviceWelcomeService

logger = logging.getLogger(__name__)


def build_sub_device_names(device: MIoTDeviceInfo) -> dict[str, str]:
    """Convert MIoTDeviceInfo.sub_devices to {siid: user_alias}.

    Strips the parent device name suffix (e.g. "三楼书房-客厅多路开关" → "三楼书房")
    so callers consistently see the user-customized portion only.
    """
    return normalize_sub_devices(device.sub_devices, device.name)


class MiotProxy:
    """Xiaomi IoT proxy class responsible for handling MIoT device related operations."""

    def __init__(
        self,
        uuid: str,
        redirect_uri: str,
        kv_repo: KVRepo,
        cloud_server: str | None = None,
    ):
        self._kv_repo = kv_repo
        self.init_miot_info_dict()
        self._camera_img_managers: dict[str, CameraVisionHandler] = {}
        self._token_refresh_task: asyncio.Task | None = None
        # Serialize refresh_devices: multiple entries (MQTT reconnect,
        # bind-debounce, device refresh, lazy load) can fire concurrently
        # and would otherwise race on _device_info_dict / KV / diff log.
        self._refresh_devices_lock = asyncio.Lock()
        # 登录 / switch_home / unbind 可并发触发 refresh_cameras,加锁防
        # _camera_img_managers / SDK callback 状态竞争。
        self._refresh_cameras_lock = asyncio.Lock()

        # Save params for creating new MIoTClient instances
        self._uuid = uuid
        self._redirect_uri = redirect_uri
        self._cloud_server = cloud_server

        self._miot_client: MIoTClient = None  # type: ignore

        _settings = get_settings()
        self._frame_interval: int = _settings.camera.frame_interval
        self._max_cache_images: int = _settings.camera.max_cache_images

        # two times cache ttl, at least 1 second
        # frame_interval * cache_max_size / 1000 * 2 = seconds
        self._camera_img_cache_ttl: int = max(
            1, int(self._frame_interval * self._max_cache_images / 1000 * 2)
        )

        # URN → spec dict cache (no TTL / no capacity limit): device specs are
        # immutable per model — once fetched they never change. Typical home has
        # < 100 device models so memory footprint is negligible.
        self._spec_cache: dict[str, dict] = {}

        # Welcome action shared by the bind path and the home-move path:
        # given a refreshed did, greet it if present + in a managed home.
        self._welcome_service = DeviceWelcomeService(
            get_device=lambda did: self._device_info_dict.get(did),
            is_home_allowed=lambda home_id: is_home_allowed(self._kv_repo, home_id),
            log_device_diff=self._log_device_diff,
        )

        # Listener for account-level bind/unbind events from MIPS cloud.
        # Owns its own debounce timer state; receives MIoTDeviceBindEvent
        # via on_event() and delegates confirmed binds to the welcome service.
        self._bind_listener = self._build_bind_listener()

        # Listener for device-level meta changes (rename / hr_change).
        # Debounces then refreshes the device list so the new name/room/home
        # propagates. A move INTO a managed home additionally welcomes the
        # device (welcome flag set by _on_device_meta_changed_event).
        self._meta_listener = DeviceMetaEventListener(
            refresh_devices=self.refresh_devices,
            refresh_cameras=self.refresh_cameras,
            refresh_scenes=self.refresh_scenes,
            welcome=self._welcome_service.welcome,
        )
        # Dids whose device/{did}/g_op/{rename,hr_change} meta topics this proxy
        # intends to subscribe. Drives the diff in _sync_meta_subscriptions; the
        # authoritative broker-side state lives in MIoTClient._meta_sub_dids.
        self._subscribed_meta_dids: set[str] = set()

        # Listener for home-level scene changes (rename/delete/edit). Debounces
        # then refreshes the scene list.
        self._scene_listener = SceneEventListener(
            refresh_scenes=self.refresh_scenes
        )
        # Home ids whose home/{home_id}/scene/{rename,delete,edit} topics this
        # proxy intends to subscribe. Mirrors _subscribed_meta_dids but per home.
        self._subscribed_scene_home_ids: set[str] = set()

    def _build_bind_listener(self) -> BindEventListener:
        """Build a fresh BindEventListener.

        Re-invoked from init() after deinit(): deinit() permanently fences
        the previous listener via _closed=True, so unbind_miot (which is
        deinit+init) would otherwise leave bind/unbind push silently dropped.
        """
        return BindEventListener(
            refresh_devices=self.refresh_devices,
            get_device=lambda did: self._device_info_dict.get(did),
            welcome=self._welcome_service.welcome,
            refresh_cameras=self.refresh_cameras,
            refresh_scenes=self.refresh_scenes,
        )

    def _create_miot_client(self) -> MIoTClient:
        """Create a new MIoTClient instance."""
        return MIoTClient(
            uuid=self._uuid,
            redirect_uri=self._redirect_uri,
            cache_path=str(get_settings().directories.miot_cache_dir),
            oauth_info=self._oauth_info,
            cloud_server=self._cloud_server,
        )

    @property
    def miot_client(self) -> MIoTClient:
        if self._miot_client is None:
            raise RuntimeError("MIoTClient is not initialized. Call init() first.")
        return self._miot_client

    @property
    def is_authenticated(self) -> bool:
        """Whether MIoT OAuth has been completed and an access token is usable."""
        return self._oauth_info is not None

    @classmethod
    async def create_miot_proxy(
        cls,
        uuid: str,
        redirect_uri: str,
        kv_repo: KVRepo,
        cloud_server: str | None = None,
    ) -> MiotProxy:
        instance = cls(uuid, redirect_uri, kv_repo, cloud_server)
        await instance.init()
        logger.info(
            "MiotProxy initialization successful, authenticated: %s",
            instance.is_authenticated,
        )
        return instance

    async def init(self):
        """Initialize MIoT proxy: create new client, init it, refresh info, start token refresh."""
        self._miot_client = self._create_miot_client()
        # Rebuild listeners + register the push callbacks BEFORE init_async().
        # init_async runs _setup_mips_async, which (re)subscribes mips topics —
        # the broker may push the moment a SUBSCRIBE is acked. Wiring the
        # handlers (and live listeners) up first means such a push lands on a
        # listener instead of being dropped by the SDK's `cb is None` guard.
        # The rebuild is also mandatory after a prior deinit(): it fences the
        # old listeners via _closed=True, so a stale one would drop every push.
        # A fresh MIoTClient starts with empty meta/scene sub sets — reset our
        # intent views to match.
        self._bind_listener = self._build_bind_listener()
        self._meta_listener = DeviceMetaEventListener(
            refresh_devices=self.refresh_devices,
            refresh_cameras=self.refresh_cameras,
            refresh_scenes=self.refresh_scenes,
            welcome=self._welcome_service.welcome,
        )
        self._subscribed_meta_dids = set()
        self._scene_listener = SceneEventListener(
            refresh_scenes=self.refresh_scenes
        )
        self._subscribed_scene_home_ids = set()
        self._miot_client.register_user_bind_callback(self._on_user_bind_event)
        # Device meta change (rename/hr_change): refresh the list so the new
        # name/room/home propagates. Kept off the bind welcome path.
        self._miot_client.register_device_meta_changed_callback(
            self._on_device_meta_changed_event
        )
        # Home scene change (rename/delete/edit): refresh the scene list.
        self._miot_client.register_scene_changed_callback(
            self._on_scene_changed_event
        )

        await self._miot_client.init_async()

        # After MQTT (re)connect, unconditionally refresh the device list — the
        # disconnect window may have caused us to miss events. Registered AFTER
        # init_async on purpose: the first connect during setup should not
        # pre-empt the initial full refresh done by refresh_miot_info below.
        self._miot_client.register_mips_connect_callback(self.refresh_devices)
        await self.refresh_miot_info()

        if self._token_refresh_task:
            self._token_refresh_task.cancel()
            self._token_refresh_task = None

        self._token_refresh_task = asyncio.create_task(self._start_token_refresh_task())

    async def deinit(self):
        """Deinit MIoT proxy: cancel tasks, destroy cameras, close client, clear all state."""
        # 1. Cancel token refresh background task
        if self._token_refresh_task:
            self._token_refresh_task.cancel()
            self._token_refresh_task = None

        # 1b. Cancel any pending bind/rename-event debounce timers — otherwise
        # they might fire during teardown and try to call refresh_devices on a
        # half-destroyed proxy.
        self._bind_listener.deinit()
        self._meta_listener.deinit()
        self._scene_listener.deinit()

        # 2. Destroy all camera_img_managers
        for mgr in self._camera_img_managers.values():
            await mgr.destroy()
        self._camera_img_managers.clear()

        # 3. Deinit MIoTClient and invalidate reference
        if self._miot_client:
            try:
                await self._miot_client.deinit_async()
            except Exception as e:
                # Keep going: leaking a sub-client is still better than
                # leaving the whole client half-torn-down on the next init.
                logger.warning("miot_client.deinit_async failed, proceeding: %s", e)
            self._miot_client = None  # type: ignore

        # 4. Clear auth/user data from KV store (device/camera/scene are
        #    in-memory only, no KV persistence to clean up).
        for key in [
            AuthConfigKeys.MIOT_TOKEN_INFO_KEY,
            DeviceInfoKeys.USER_INFO_KEY,
        ]:
            self._kv_repo.delete(key)

        # 5. Clear in-memory state
        self._oauth_info = None
        self._camera_info_dict = {}
        self._device_info_dict = {}
        self._scene_info_dict = {}
        self._user_info = None
        self._subscribed_meta_dids = set()
        self._subscribed_scene_home_ids = set()
        # Welcome service survives deinit (rebuilt only in __init__), but its
        # dedup window state must reset alongside the other in-memory caches —
        # otherwise a re-bind of the same did within WELCOME_DEDUP_SEC after an
        # unbind_miot would be wrongly skipped.
        self._welcome_service._recent.clear()

    async def refresh_miot_info(self) -> dict:
        """
        Refresh MiOT all information

        Returns:
            dict: Dictionary containing result of each refresh operation
        """
        result: dict = {
            "cameras": False,
            "scenes": False,
            "user_info": False,
            "devices": False,
            "errors": [],
        }

        if not self._oauth_info:
            return result

        for label, fn in [
            ("cameras", self.refresh_cameras),
            ("scenes", self.refresh_scenes),
            ("user_info", self.refresh_user_info),
            ("devices", self.refresh_devices),
        ]:
            try:
                r = await fn()
                result[label] = r is not None
            except Exception as e:
                result["errors"].append(f"{label}: {e}")

        if result["errors"]:
            logger.warning(
                "MiOT info refresh completed with errors: %s", result
            )
        else:
            logger.info("MiOT info refresh completed: %s", result)
        return result

    def init_miot_info_dict(self):
        # device/camera/scene 不持久化，启动时为空，由 refresh_miot_info() 填充。
        self._camera_info_dict: dict[str, MIoTCameraInfo] = {}
        self._device_info_dict: dict[str, MIoTDeviceInfo] = {}
        self._scene_info_dict: dict[str, MIoTManualSceneInfo] = {}

        user_info_str = self._kv_repo.get(DeviceInfoKeys.USER_INFO_KEY)
        if user_info_str:
            self._user_info: MIoTUserInfo | None = MIoTUserInfo.model_validate_json(
                user_info_str
            )
        else:
            self._user_info = None

        oauth_info_str = self._kv_repo.get(AuthConfigKeys.MIOT_TOKEN_INFO_KEY)
        if oauth_info_str:
            self._oauth_info = MIoTOauthInfo.model_validate_json(oauth_info_str)
        else:
            self._oauth_info = None

    def get_recent_camera_img(
        self, camera_id: str, channel: int, recent_count: int
    ) -> CameraImgSeq | None:
        if camera_id not in self._camera_img_managers:
            logger.warning("Camera %s not found in managers", camera_id)
            return None
        if recent_count > self._max_cache_images or recent_count <= 0:
            logger.warning(
                "recent_count is out of range, camera_id: %s, channel: %s, "
                "recent_count: %s, max_cache_images: %s",
                camera_id,
                channel,
                recent_count,
                self._max_cache_images,
            )
        return self._camera_img_managers[camera_id].get_recent_camera_img(
            channel, recent_count
        )

    async def start_camera_raw_audio_stream(
        self,
        camera_id: str,
        channel: int,
        callback: Callable[[str, bytes, int, int, int], Coroutine],
    ):
        if camera_id not in self._camera_img_managers:
            logger.warning("Camera %s not found in managers", camera_id)
            return
        instance = self._camera_img_managers[camera_id]
        await instance.register_raw_audio_stream(callback, channel)
        logger.info(
            "Successfully started camera audio stream, camera_id: %s, channel: %s",
            camera_id,
            channel,
        )

    async def stop_camera_raw_audio_stream(self, camera_id: str, channel: int):
        if camera_id not in self._camera_img_managers:
            logger.warning("Camera %s not found in managers", camera_id)
            return
        instance = self._camera_img_managers[camera_id]
        try:
            await instance.unregister_raw_audio_stream(channel)
            logger.info(
                "Successfully stopped camera audio stream, camera_id: %s, channel: %s",
                camera_id,
                channel,
            )
        except Exception as e:
            logger.error("Failed to stop camera audio stream: %s", e)
            raise

    def get_audio_codec(self, camera_id: str, channel: int) -> str:
        if camera_id not in self._camera_img_managers:
            logger.warning(
                "Camera %s not found in managers, defaulting to opus", camera_id
            )
            return "opus"
        codec = self._camera_img_managers[camera_id].get_audio_codec(channel)
        return codec or "opus"

    async def start_camera_raw_stream(
        self,
        camera_id: str,
        channel: int,
        callback: Callable[[str, bytes, int, int, int], Coroutine],
    ):
        if camera_id not in self._camera_img_managers:
            logger.warning("Camera %s not found in managers", camera_id)
            return
        instance = self._camera_img_managers[camera_id]
        await instance.register_raw_stream(callback, channel)
        logger.info(
            "Successfully started camera raw stream, camera_id: %s, channel: %s",
            camera_id,
            channel,
        )

    async def stop_camera_raw_stream(self, camera_id: str, channel: int):
        """
        Stop camera raw video stream

        Args:
            camera_id: Camera device ID
            channel: Channel number, default is 0
        """
        if camera_id not in self._camera_img_managers:
            logger.warning("Camera %s not found in managers", camera_id)
            return

        instance = self._camera_img_managers[camera_id]
        try:
            await instance.unregister_raw_stream(channel)
            logger.info(
                "Successfully stopped camera raw video stream, camera_id: %s, channel: %s",
                camera_id,
                channel,
            )
        except Exception as e:
            logger.error("Failed to stop camera raw video stream: %s", e)
            raise

    async def start_camera_decode_video_stream(
        self,
        camera_id: str,
        channel: int,
        callback: Callable[[str, VideoFrame, int, int], Coroutine],
    ) -> int:
        if camera_id not in self._camera_img_managers:
            logger.warning("Camera %s not found in managers", camera_id)
            return -1
        instance = self._camera_img_managers[camera_id]
        reg_id = await instance.register_decode_video_frame_stream(callback, channel)
        logger.info(
            "Started decode video frame stream, camera_id: %s, channel: %s, reg_id: %d",
            camera_id,
            channel,
            reg_id,
        )
        return reg_id

    async def stop_camera_decode_video_stream(
        self, camera_id: str, channel: int, reg_id: int
    ):
        if camera_id not in self._camera_img_managers:
            logger.warning("Camera %s not found in managers", camera_id)
            return
        instance = self._camera_img_managers[camera_id]
        try:
            await instance.unregister_decode_video_frame_stream(channel, reg_id)
            logger.info(
                "Stopped decode video frame stream, camera_id: %s, channel: %s, reg_id: %d",
                camera_id,
                channel,
                reg_id,
            )
        except Exception as e:
            logger.error("Failed to stop decode video frame stream: %s", e)
            raise

    async def start_camera_decode_audio_stream(
        self,
        camera_id: str,
        channel: int,
        callback: Callable[[str, AudioFrame, int, int], Coroutine],
    ) -> int:
        if camera_id not in self._camera_img_managers:
            logger.warning("Camera %s not found in managers", camera_id)
            return -1
        instance = self._camera_img_managers[camera_id]
        reg_id = await instance.register_decode_audio_frame_stream(callback, channel)
        logger.info(
            "Started decode audio frame stream, camera_id: %s, channel: %s, reg_id: %d",
            camera_id,
            channel,
            reg_id,
        )
        return reg_id

    async def stop_camera_decode_audio_stream(
        self, camera_id: str, channel: int, reg_id: int
    ):
        if camera_id not in self._camera_img_managers:
            logger.warning("Camera %s not found in managers", camera_id)
            return
        instance = self._camera_img_managers[camera_id]
        try:
            await instance.unregister_decode_audio_frame_stream(channel, reg_id)
            logger.info(
                "Stopped decode audio frame stream, camera_id: %s, channel: %s, reg_id: %d",
                camera_id,
                channel,
                reg_id,
            )
        except Exception as e:
            logger.error("Failed to stop decode audio frame stream: %s", e)
            raise

    async def _create_camera_img_manager(
        self, camera_info: MIoTCameraInfo,
    ) -> CameraVisionHandler | None:
        # scope 不影响 manager 的建立——watch 视频流需要 camera instance 无论 inUse 状态。
        # toggle_scope 只改 KV,不触发 refresh_cameras,所以这里只在启动/摄像头首次发现时调用,
        # 此时 start_async 是正常的初始化,不会干扰已有连接。
        camera_instance = await self._get_camera_instance(camera_info)
        if camera_instance is not None:
            await camera_instance.start_async(enable_reconnect=True, enable_audio=True)
            camera_img_manager = CameraVisionHandler(
                camera_info,
                camera_instance,
                # 传 manager，让 handler.destroy() 走 manager.destroy_camera_async(did)
                # 清 SDK _camera_map cache。SDK 隐藏 access 在 backend 多处已有先例
                # (如 client.py:599 self._camera_client.camera_map)。
                self._miot_client._camera_client,
                max_size=self._max_cache_images,
                ttl=self._camera_img_cache_ttl,
            )
            self._camera_img_managers[camera_info.did] = camera_img_manager
            return camera_img_manager
        else:
            logger.error("Camera instance for %s is None, skipping", camera_info.did)
            return None

    async def _get_camera_instance(
        self, camera_info: MIoTCameraInfo
    ) -> MIoTCameraInstance | None:
        try:
            return await self._miot_client.create_camera_instance_async(
                camera_info, frame_interval=self._frame_interval
            )
        except Exception as e:
            logger.error("Failed to get camera instance: %s", e)
            return None

    async def get_cameras(self) -> dict[str, MIoTCameraInfo]:
        if not self._camera_info_dict:
            logger.warning("No camera info dict found, refreshing cameras")
            await self.refresh_cameras()
        return self._camera_info_dict

    def get_cached_camera(self, did: str) -> MIoTCameraInfo | None:
        """Return camera metadata from the in-memory cache without refreshing."""
        return self._camera_info_dict.get(did)

    async def get_camera_dids(self) -> list[str]:
        """
        Get all available camera device ID list

        Returns:
            list[str]: Camera device ID list

        """
        camera_dict: dict[str, MIoTCameraInfo] | None = await self.get_cameras()
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

    async def _on_lan_device_changed(
        self, did: str, info: MIoTLanDeviceInfo
    ) -> None:
        # refresh_cameras deep-copies SDK state, so post-init lan_online
        # changes only reach _camera_info_dict via this hook.
        cam = self._camera_info_dict.get(did)
        if cam is None:
            return
        cam.lan_online = info.online
        cam.local_ip = info.ip
        logger.debug(
            "Camera LAN status synced: did=%s, online=%s, ip=%s",
            did,
            info.online,
            info.ip,
        )

    async def refresh_cameras(self) -> dict[str, MIoTCameraInfo] | None:
        async with self._refresh_cameras_lock:
            try:
                cameras = await self._miot_client.get_cameras_async()
                cameras = copy.deepcopy(cameras)
                # Publish before registering so callbacks resolve against the new dict.
                self._camera_info_dict = cameras
                for camera_did in cameras.keys():
                    if camera_did not in self._camera_img_managers:
                        if not is_home_allowed(self._kv_repo, cameras[camera_did].home_id):
                            continue
                        manager = await self._create_camera_img_manager(
                            cameras[camera_did]
                        )
                        # Only register when the manager exists, so register/unregister
                        # stay paired with _camera_img_managers.
                        if manager is not None:
                            await self._miot_client.register_lan_device_changed_async(
                                did=camera_did, callback=self._on_lan_device_changed
                            )
                    else:
                        await self._camera_img_managers[camera_did].update_camera_info(
                            cameras[camera_did]
                        )

                for camera_did in list(self._camera_img_managers.keys()):
                    cam = cameras.get(camera_did)
                    # scope=false 时不 destroy,只有摄像头真正从账号消失才 destroy。
                    if cam is None:
                        await self._miot_client.unregister_lan_device_changed_async(
                            did=camera_did
                        )
                        await self._camera_img_managers[camera_did].destroy()
                        del self._camera_img_managers[camera_did]
                    else:
                        # cam 仍在账号里,manager 保活(无论 scope 状态)。
                        logger.debug("Manager %s kept alive for watch stream", camera_did)
                return cameras

            except Exception as e:
                logger.error("Failed to refresh cameras: %s", e)
                return None

    async def refresh_camera_online_status(self) -> dict[str, MIoTCameraInfo] | None:
        """轻量刷新:重拉 SDK 相机列表、只更新 ``_camera_info_dict``(online / lan_online
        等元数据),**不调 update_camera_info、不动解码注册 / 帧队列 / manager**——故
        完全不扰动 watch 视频流。

        用途:``list_cameras_with_state`` 只读 ``_camera_info_dict`` 缓存,相机重新上线后
        该缓存不会自愈(云端 online 只有重拉 SDK 才更新),前端「此刻」页加载前调这个即可
        让在线状态真实,而不必走会过 update_camera_info(在共用 SDK 实例上重注册/注销
        解码 → 瞬时卡流)的重量级 refresh_cameras。

        与 refresh_cameras 共用 ``_refresh_cameras_lock``,防并发改 ``_camera_info_dict``。
        """
        async with self._refresh_cameras_lock:
            try:
                cameras = await self._miot_client.get_cameras_async()
                self._camera_info_dict = copy.deepcopy(cameras)
                return self._camera_info_dict
            except Exception as e:
                logger.error("Failed to refresh camera online status: %s", e)
                return None

    async def refresh_devices(self) -> dict[str, MIoTDeviceInfo] | None:
        async with self._refresh_devices_lock:
            try:
                devices = await self._miot_client.get_devices_async()
                self._device_info_dict = devices
                await self._sync_meta_subscriptions()
                await self._sync_scene_subscriptions()
                return devices
            except Exception as e:
                logger.error("Failed to refresh devices: %s", e)
                return None

    @staticmethod
    def _log_device_diff(
        action: str, dev: MIoTDeviceInfo | None, did: str
    ) -> None:
        """Pretty-print one ADDED/REMOVED device line with all relevant
        identity fields (name, home, room, model, online, sub-devices, etc.)
        so the operator can tell *which* physical device was bound/unbound
        without having to look up the did separately."""
        if dev is None:
            logger.info("  %s did=%s (no cached info)", action, did)
            return
        sub = build_sub_device_names(dev)
        parts = [
            f"  {action} did={dev.did}",
            f"name={dev.name!r}",
            f"model={dev.model}",
            f"home={dev.home_name!r}(id={dev.home_id})",
            f"room={dev.room_name!r}(id={dev.room_id})",
            f"online={dev.online}",
            f"lan_online={dev.lan_online}",
            f"manufacturer={dev.manufacturer}",
            f"urn={dev.urn}",
            f"order_time={dev.order_time}",
        ]
        if dev.parent_id:
            parts.append(f"parent={dev.parent_id}")
        if dev.owner_nickname:
            parts.append(f"owner={dev.owner_nickname!r}")
        if dev.fw_version:
            parts.append(f"fw={dev.fw_version}")
        if sub:
            parts.append(f"sub_devices={sub}")
        logger.info(" ".join(parts))

    # ---------------------------------------------------------------- mips

    async def _on_user_bind_event(self, msg: MIoTDeviceBindEvent) -> None:
        """Forward bind/unbind push events to the dedicated listener.

        The actual debounce + report logic lives in
        ``miloco.miot.mips_listeners.BindEventListener`` — this method is a
        thin shim so MiotProxy stays the user-bind callback target without
        carrying the implementation.
        """
        await self._bind_listener.on_event(msg)

    async def _on_device_meta_changed_event(self, msg: MIoTDeviceBindEvent) -> None:
        """Forward device-meta change push events to the meta listener.

        All events refresh the device list. An ``hr_change`` that moves a
        device from an out-of-scope home INTO a managed (whitelisted) home
        additionally welcomes it (welcome=True) — it newly appeared in the
        user's home. Rename, intra-home room change and moves not entering
        scope just refresh (welcome=False). The move-into-scope decision lives
        here (it needs the scope whitelist); the listener defers the greeting
        until after the refresh and delegates it to the welcome service.
        """
        welcome = msg.event == "hr_change" and self._is_move_into_scope(msg)
        await self._meta_listener.on_event(msg, welcome=welcome)

    def _is_move_into_scope(self, msg: MIoTDeviceBindEvent) -> bool:
        """True if an hr_change moved a device into a managed home from an
        unmanaged one.

        Uses the payload's ``homeid`` (new) / ``origin_homeid`` (old) — see the
        dev_bind_room_change schema. A pure room change keeps homeid==origin
        (same allowed-status) and a move between two managed homes keeps the old
        home allowed, so both correctly return False. A payload missing EITHER
        home id returns False too: without the old home we cannot distinguish a
        genuine move-in from an intra-home change whose payload happens to omit
        origin_homeid, and a spurious "new device" welcome is worse than a
        missed one.
        """
        raw = msg.raw or {}
        new_home = raw.get("homeid")
        old_home = raw.get("origin_homeid")
        if new_home is None or old_home is None:
            return False
        return is_home_allowed(self._kv_repo, str(new_home)) and not is_home_allowed(
            self._kv_repo, str(old_home)
        )

    async def _sync_meta_subscriptions(self) -> None:
        """Reconcile per-device meta (rename/hr_change) subs to the device list.

        Called at the tail of refresh_devices (under _refresh_devices_lock, so
        the diff against _subscribed_meta_dids is race-free). New dids are
        subscribed, removed dids unsubscribed; both run concurrently and
        per-did failures only log — they never abort the refresh.

        ACCOUNT-WIDE ON PURPOSE — do NOT scope-filter this by managed home.
        A device sitting in an out-of-scope home must already be subscribed so
        an hr_change moving it INTO a managed home is heard — that's how the
        move-in welcome works. The managed-home scope is applied only at the
        welcome step (_is_move_into_scope / DeviceWelcomeService), never to the
        subscription. (Scene subs differ — they ARE scoped, since a scene has
        no move-into-scope analogue; see _sync_scene_subscriptions.)

        Dids containing '/' (Huami/Zepp-bridged sub-devices, e.g.
        ``huami.32098/12264203``) are skipped: the '/' breaks the topic path
        AND the decoder regex, and the broker has no pub/sub ACL for them
        anyway — every such subscribe is rejected with 0x87 Not authorized.
        """
        target = {did for did in self._device_info_dict if "/" not in did}
        skipped = [did for did in self._device_info_dict if "/" in did]
        if skipped:
            logger.debug("device-meta: skipping %d did(s) with '/': %s", len(skipped), skipped)
        to_add = target - self._subscribed_meta_dids
        to_remove = self._subscribed_meta_dids - target
        if not to_add and not to_remove:
            return

        async def _sub(did: str) -> str | None:
            try:
                await self._miot_client.sub_device_meta_async(did)
                return did
            except Exception as e:
                logger.error("subscribe device-meta failed did=%s: %s", did, e)
                return None

        async def _unsub(did: str) -> str | None:
            try:
                await self._miot_client.unsub_device_meta_async(did)
            except Exception as e:
                logger.error("unsubscribe device-meta failed did=%s: %s", did, e)
            return did

        added = await asyncio.gather(*(_sub(d) for d in to_add))
        removed = await asyncio.gather(*(_unsub(d) for d in to_remove))
        self._subscribed_meta_dids |= {d for d in added if d}
        self._subscribed_meta_dids -= {d for d in removed if d}
        logger.info(
            "device-meta subscriptions synced: +%d -%d (total=%d)",
            len([d for d in added if d]),
            len([d for d in removed if d]),
            len(self._subscribed_meta_dids),
        )

    async def _on_scene_changed_event(self, msg: MIoTSceneChangedEvent) -> None:
        """Forward home scene-change push events to the dedicated listener.

        The debounce + refresh logic lives in
        ``miloco.miot.mips_listeners.SceneEventListener`` — this method is a
        thin shim, mirroring ``_on_user_bind_event``.
        """
        await self._scene_listener.on_event(msg)

    def _collect_home_ids(self) -> set[str]:
        """Union of home_ids across cached devices / cameras / scenes.

        Reads each cache as of its last refresh, and is only called from
        refresh_devices — so the device cache is always current while the
        camera / scene caches reflect their previous refresh. A home appearing
        ONLY in a not-yet-refreshed camera/scene cache is thus picked up one
        device refresh late; in practice every home has devices, so the union
        covers them immediately, without an extra homes HTTP call.

        Returns the FULL set (no scope filter); the managed-home scoping is the
        caller's job — _sync_scene_subscriptions applies the whitelist.
        """
        home_ids: set[str] = set()
        for coll in (
            self._device_info_dict.values(),
            self._camera_info_dict.values(),
            self._scene_info_dict.values(),
        ):
            for item in coll:
                hid = getattr(item, "home_id", None)
                if hid:
                    home_ids.add(str(hid))
        return home_ids

    async def _sync_scene_subscriptions(self) -> None:
        """Reconcile per-home scene subs to the current home set.

        Called at the tail of refresh_devices (under _refresh_devices_lock, so
        the diff against _subscribed_scene_home_ids is race-free). New homes
        are subscribed, removed homes unsubscribed; both run concurrently and
        per-home failures only log — they never abort the refresh.

        Scoped to managed homes only: a scene in an out-of-scope home is
        irrelevant and has no move-into-scope analogue (unlike device-meta,
        which must stay account-wide so a device moving INTO scope is heard).
        A home leaving scope therefore drops out of ``target`` and gets
        unsubscribed on the next sync.
        """
        target = {
            h for h in self._collect_home_ids()
            if is_home_allowed(self._kv_repo, h)
        }
        to_add = target - self._subscribed_scene_home_ids
        to_remove = self._subscribed_scene_home_ids - target
        if not to_add and not to_remove:
            return

        async def _sub(home_id: str) -> str | None:
            try:
                await self._miot_client.sub_home_scene_async(home_id)
                return home_id
            except Exception as e:
                logger.error("subscribe home-scene failed home=%s: %s", home_id, e)
                return None

        async def _unsub(home_id: str) -> str | None:
            try:
                await self._miot_client.unsub_home_scene_async(home_id)
            except Exception as e:
                logger.error("unsubscribe home-scene failed home=%s: %s", home_id, e)
            return home_id

        added = await asyncio.gather(*(_sub(h) for h in to_add))
        removed = await asyncio.gather(*(_unsub(h) for h in to_remove))
        self._subscribed_scene_home_ids |= {h for h in added if h}
        self._subscribed_scene_home_ids -= {h for h in removed if h}
        logger.info(
            "home-scene subscriptions synced: +%d -%d (total=%d)",
            len([h for h in added if h]),
            len([h for h in removed if h]),
            len(self._subscribed_scene_home_ids),
        )

    def get_mips_status(self) -> dict:
        """Snapshot of cloud-MQTT connection and user-level subscribe status.

        Sole consumer is the /api/miot/mips_status endpoint, used to verify
        whether real-time device-bind detection is currently working.
        """
        client = self._miot_client
        if client is None:
            return {
                "connected": False,
                "user_bind_subscribed": False,
                "last_error": "miot_client not initialized",
            }
        last_error = client.mips_user_sub_error
        return {
            "connected": client.mips_connected,
            "user_bind_subscribed": client.mips_connected and last_error is None,
            "last_error": last_error,
        }

    async def refresh_scenes(self) -> dict[str, MIoTManualSceneInfo] | None:
        try:
            scenes = await self._miot_client.get_manual_scenes_async()
            self._scene_info_dict = scenes
            return scenes
        except Exception as e:
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
        except Exception as e:
            logger.error("Failed to execute miot scene: %s", e)
            return False

    async def send_app_notify(self, app_notify_id: str) -> bool:
        try:
            return await self._miot_client.send_app_notify_async(app_notify_id)
        except Exception as e:
            logger.error("Failed to send app notify: %s", e)
            return False

    async def check_token_valid(self) -> bool:
        try:
            return await self._miot_client.check_token_async()
        except Exception as e:
            logger.error("Failed to check token valid: %s", e)
            raise

    async def refresh_user_info(self):
        try:
            user_info = await self._miot_client.get_user_info_async()
            self._user_info = user_info
            self._kv_repo.set(
                DeviceInfoKeys.USER_INFO_KEY, json.dumps(to_jsonable_python(user_info))
            )
            return user_info
        except Exception as e:
            logger.error("Failed to refresh user info: %s", e)
            return None

    async def get_user_info(self) -> MIoTUserInfo | None:
        if not self._user_info:
            await self.refresh_user_info()
        return self._user_info

    async def get_miot_login_url(self) -> str:
        url = await self._miot_client.gen_oauth_url_async(self._redirect_uri)
        logger.info("Generated MIoT login URL: %s", url)
        return url

    async def get_miot_app_notify_id(self, content: str) -> str | None:
        try:
            app_notify_id = await self._miot_client.http_client.create_app_notify_async(
                content
            )
            logger.info("get_miot_app_notify_id app_notify_id: %s", app_notify_id)
            return app_notify_id
        except Exception as e:
            logger.error("Failed to get miot app notify id: %s", e)
            return None

    async def get_miot_auth_info(self, code: str, state: str) -> MIoTOauthInfo:
        try:
            oauth_info = await self._miot_client.get_access_token_async(
                code=code, state=state
            )
            logger.info(
                "Retrieved MIoT auth info, code: %s, state: %s", code, state
            )
            self.reset_miot_token_info(oauth_info)
            await self.refresh_miot_info()
            return oauth_info
        except Exception as e:
            logger.error("Failed to get Xiaomi home token info, %s", e)
            raise e

    def reset_miot_token_info(self, miot_token_info: MIoTOauthInfo):
        """
        Reset persistent Mi Home token information
        """
        self._oauth_info = miot_token_info
        self._kv_repo.set(
            AuthConfigKeys.MIOT_TOKEN_INFO_KEY, miot_token_info.model_dump_json()
        )
        logger.info(
            "Token information updated, new expiration time: %s",
            miot_token_info.expires_ts,
        )

    async def refresh_xiaomi_home_token_info(self) -> MIoTOauthInfo | None:
        try:
            if not self._oauth_info:
                raise ValueError("No oauth_info found")
            oauth_info = await self._miot_client.refresh_access_token_async(
                refresh_token=self._oauth_info.refresh_token
            )
            logger.info("Successfully refreshed Xiaomi home token info")
            self.reset_miot_token_info(oauth_info)
            await asyncio.sleep(3)
            await self.refresh_miot_info()
            return oauth_info
        except Exception as e:
            self._oauth_info = None
            logger.error(
                "Failed to refresh Xiaomi home token info: %s", e, exc_info=True
            )

    async def _start_token_refresh_task(self):
        """
        Start scheduled token refresh task
        """
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._check_and_refresh_token()
            except Exception as e:
                logger.error("Scheduled token refresh task exception: %s", e)
                await asyncio.sleep(60)  # Wait 1 minute after error before continuing

    async def set_device_properties(self, params: list[MIoTSetPropertyParam]) -> list:
        """Set device properties via MIoT cloud API."""
        try:
            return await self.miot_client.http_client.set_props_async(params)
        except Exception as e:
            logger.error("Failed to set device properties: %s", e)
            raise

    async def get_device_properties(self, params: list[MIoTGetPropertyParam]) -> list:
        """Get device properties via MIoT cloud API."""
        try:
            return await self.miot_client.http_client.get_props_async(params)
        except Exception as e:
            logger.error("Failed to get device properties: %s", e)
            raise

    async def get_readable_prop_iids(self, did: str) -> list[str]:
        """Return all readable prop iids for a device, derived from its spec."""
        device = self._device_info_dict.get(did)
        if not device:
            return []
        spec = await self._fetch_device_spec(device.urn)
        return [
            iid
            for iid, entry in spec.items()
            if iid.startswith("prop.") and entry.get("readable", False)
        ]

    async def call_device_action(self, param: MIoTActionParam) -> dict:
        """Call device action via MIoT cloud API."""
        try:
            return await self.miot_client.http_client.action_async(param)
        except Exception as e:
            logger.error("Failed to call device action: %s", e)
            raise

    async def get_home_info_data(self) -> dict:
        """Build home info dict for CLI cache, including spec data fetched via spec_parser."""
        devices = []
        for device in self._device_info_dict.values():
            category = None
            try:
                parts = device.urn.split(":")
                # urn:miot-spec-v2:device:{category}:{code}:...
                if len(parts) >= 4 and parts[2] == "device":
                    category = parts[3]
            except Exception:
                pass

            sub_device_names = build_sub_device_names(device)
            spec = await self._fetch_device_spec(device.urn, sub_device_names)
            devices.append(
                {
                    "did": device.did,
                    "name": device.name,
                    "home": device.home_name,
                    "online": device.online,
                    "model": device.model,
                    "room": device.room_name,
                    "category": category,
                    "spec": spec,
                    "sub_devices": sub_device_names or None,
                }
            )

        areas = sorted(
            {d.room_name for d in self._device_info_dict.values() if d.room_name}
        )
        scenes = [
            {"scene_id": s.scene_id, "scene_name": s.scene_name}
            for s in self._scene_info_dict.values()
        ]
        # 米家家庭名：每台 MIoTDeviceInfo / MIoTCameraInfo 自带 home_name + home_id
        # 字段（米家云在 list_homes 时把家庭信息分发到了下属每个设备）。
        # 单家庭账号 home_name 天然唯一；多家庭账号下需要 service 层按接入范围挑，
        # 这里把 home_id→home_name 完整映射也透出去（home_name 默认取第一个非空，
        # service 层若有接入范围配置会覆盖）。
        # 遍历顺序 cameras 优先于 devices（setdefault 让先到的赢）—— 大部分账号下
        # 同一 home 的 cameras / devices home_name 一致；极少数不一致场景以 cameras
        # 版本胜出（cameras 通常是住户主关心入口，名字更准）。
        home_id_to_name: dict[str, str] = {}
        home_name: str | None = None
        for d in (
            *self._camera_info_dict.values(),
            *self._device_info_dict.values(),
        ):
            hid = getattr(d, "home_id", None)
            n = getattr(d, "home_name", None)
            if hid and n:
                home_id_to_name.setdefault(str(hid), n)
            if n and home_name is None:
                home_name = n
        return {
            "home_name": home_name,
            "home_id_to_name": home_id_to_name,
            "devices": devices,
            "areas": [{"name": a} for a in areas],
            "scenes": scenes,
            "persons": [],
        }

    async def _fetch_device_spec(
        self, urn: str, sub_device_names: dict[str, str] | None = None
    ) -> dict:
        """Fetch spec for a device URN and return CLI-compatible spec dict.

        iid format conversion: parse_lite_async returns 'prop.0.{siid}.{piid}',
        we strip the device instance id (always 0) to get 'prop.{siid}.{piid}'.

        If sub_device_names is provided (siid -> custom name), override
        service_description for entries whose siid matches a sub-device.

        Base spec (without sub_device_names overrides) is cached in memory
        by URN. The override is applied on a shallow copy each call.
        """
        # Check in-memory cache for the base spec.
        cache_key = urn
        if cache_key in self._spec_cache:
            spec = {k: dict(v) for k, v in self._spec_cache[cache_key].items()}
            if sub_device_names:
                for iid, entry in spec.items():
                    siid = iid.split(".")[1] if "." in iid else None
                    if siid and siid in sub_device_names:
                        entry["service_description"] = sub_device_names[siid]
            return spec

        try:
            # service 级 + 属性级都降到 UNKNOWN：
            # - 某些设备（屏显开关）把 temperature/humidity 挂在
            #   type_level=UNKNOWN 的 environment 服务下；service 不放宽就过滤掉整服务。
            # - 某些蓝牙温湿度计（miaomiaoce-t9）把 temperature/relative-humidity
            #   放在非标准 piid（1001/1002）上，属性级不放宽就被过滤掉核心读数。
            # vendor 自定义类型（custom-environment / power-waste 等）走 CLI 端
            # whitelist.json 的 (service_type, kind, type_name) 三元组过滤，不会
            # 污染 catalog。
            # action 级同样降到 UNKNOWN：action 的 type_level 由 std-lib 服务模板的
            # required-/optional-actions 决定，模板拉取失败 / 缓存缺失时 get_action_type
            # 退化为 UNKNOWN，默认 OPTIONAL 阈值会把全部 action 过滤掉（音箱 play-text /
            # execute-text-directive 等被整组丢弃，催生 "key 'play-text' not found"）。
            # 与 service/property 一致放宽即可；非标 vendor action 仍由 proprietary +
            # whitelist 过滤，不会污染 catalog。
            spec_lite = await self.miot_client.spec_parser.parse_lite_async(
                urn=urn,
                spec_service_level=MIoTSpecTypeLevel.UNKNOWN,
                spec_property_level=MIoTSpecTypeLevel.UNKNOWN,
                spec_action_level=MIoTSpecTypeLevel.UNKNOWN,
            )
            if not spec_lite:
                return {}
            spec = {}
            for full_iid, s in spec_lite.items():
                # "prop.0.2.1" or "action.0.5.1" → "prop.2.1" / "action.5.1"
                parts = full_iid.split(".")
                if len(parts) != 4:
                    continue
                short_iid = f"{parts[0]}.{parts[2]}.{parts[3]}"
                entry: dict = {
                    "description": s.description,
                    "format": s.format,
                    "writeable": s.writeable,
                    "readable": s.readable,
                }
                if s.unit:
                    entry["unit"] = s.unit
                if s.value_range:
                    entry["value_range"] = [
                        s.value_range.min_,
                        s.value_range.max_,
                        s.value_range.step,
                    ]
                if s.value_list:
                    entry["value_list"] = [
                        {"name": v.name, "value": v.value} for v in s.value_list
                    ]
                if s.type_name:
                    entry["type_name"] = s.type_name
                if s.service_type_name:
                    entry["service_type_name"] = s.service_type_name
                if s.service_description:
                    entry["service_description"] = s.service_description
                if s.in_params:
                    entry["in_params"] = [
                        {"name": p.name, "format": p.format} for p in s.in_params
                    ]
                if s.prop_description:
                    entry["prop_description"] = s.prop_description
                spec[short_iid] = entry

            # Cache base spec (without sub_device_names overrides).
            self._spec_cache[cache_key] = {k: dict(v) for k, v in spec.items()}

            # Apply sub_device_names overrides on a copy.
            if sub_device_names:
                for iid, entry in spec.items():
                    siid = iid.split(".")[1] if "." in iid else None
                    if siid and siid in sub_device_names:
                        entry["service_description"] = sub_device_names[siid]

            return spec
        except RuntimeError:
            # miot_client not initialized (no OAuth yet)
            return {}
        except Exception as e:
            logger.warning("Failed to fetch spec for urn %s: %s", urn, e)
            return {}

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
                current_time,
                expires_ts,
            )
            result = await self.refresh_xiaomi_home_token_info()
            if result:
                logger.info("Token refresh completed successfully")
            else:
                logger.error("Token refresh failed, re-login required: miloco-cli account bind")
