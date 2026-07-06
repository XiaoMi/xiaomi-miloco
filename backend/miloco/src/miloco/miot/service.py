# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MiOT service module
"""

import asyncio
import logging

from miot.types import (
    MIoTActionParam,
    MIoTCameraInfo,
    MIoTDeviceInfo,
    MIoTGetPropertyParam,
    MIoTManualSceneInfo,
    MIoTSetPropertyParam,
    MIoTUserInfo,
)

from miloco.config import get_settings
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.database.person_repo import PersonRepo
from miloco.middleware.exceptions import (
    BusinessException,
    MiotOAuthException,
    MiotServiceException,
    ResourceNotFoundException,
    ValidationException,
)
from miloco.miot.client import MiotProxy, build_sub_device_names
from miloco.miot.filter import (
    MAX_ENABLED_CAMERAS,
    allowed_home_ids,
    denied_camera_dids,
    filter_by_home,
    is_home_allowed,
    set_cameras_in_use,
    set_homes_in_use,
)
from miloco.miot.lru import LRUStore
from miloco.miot.message_dedup import MessageDeduper
from miloco.miot.schema import (
    CameraChannel,
    CameraImgSeq,
    CameraInfo,
    DeviceControlRequest,
    DeviceInfo,
    SceneInfo,
)

logger = logging.getLogger(__name__)

# 持有后台 task 引用，避免 CPython GC 回收 fire-and-forget task。
_background_tasks: set[asyncio.Task] = set()


def _parse_prop_iid(iid: str) -> tuple[int, int]:
    """Parse 'prop.{siid}.{piid}' → (siid, piid)."""
    parts = iid.split(".")
    if len(parts) != 3 or parts[0] != "prop":
        raise ValidationException(
            f"Invalid property iid format: '{iid}', expected prop.{{siid}}.{{piid}}"
        )
    try:
        return int(parts[1]), int(parts[2])
    except ValueError as e:
        raise ValidationException(f"Invalid iid numbers in '{iid}'") from e


def _parse_action_iid(iid: str) -> tuple[int, int]:
    """Parse 'action.{siid}.{aiid}' → (siid, aiid)."""
    parts = iid.split(".")
    if len(parts) != 3 or parts[0] != "action":
        raise ValidationException(
            f"Invalid action iid format: '{iid}', expected action.{{siid}}.{{aiid}}"
        )
    try:
        return int(parts[1]), int(parts[2])
    except ValueError as e:
        raise ValidationException(f"Invalid iid numbers in '{iid}'") from e


class MiotService:
    """MiOT service class"""

    def __init__(
        self,
        miot_proxy: MiotProxy,
        person_repo: PersonRepo | None = None,
    ):
        self._miot_proxy = miot_proxy
        self._person_repo = person_repo
        self._lru = LRUStore(miot_proxy._kv_repo.db_connector)
        # 相同通知文案的短窗去重兜底（窗口来自 config.json / settings.yaml
        # notify.dedup_window_sec）。防住 agent 顺序循环里同一条文案被反复重发、
        # 1:1 透传成一串重复推送（真实事故：单会话内顺序调用）。check→await→record
        # 结构不覆盖真正的并发双发，兜底不为此加锁。
        self._notify_deduper = MessageDeduper(
            window_sec=get_settings().notify.dedup_window_sec
        )

    async def lru_snapshot(self) -> dict:
        return self._lru.load()

    @property
    def _kv_repo(self):
        """Shortcut to the shared KVRepo (for filter / scope reads & writes)."""
        return self._miot_proxy._kv_repo

    async def _assert_did_in_allowed_home(self, did: str) -> None:
        """Raise ValidationException if did belongs to a home outside the allowed set.

        Checks both ``_device_info_dict`` and ``_camera_info_dict`` because cameras
        live in a separate dict in :class:`MiotProxy`.

        """
        allow = allowed_home_ids(self._kv_repo)
        if not allow:
            # list_homes 兜底会自动选第一个家庭，这里再调一次确保 KV 已更新
            await self.list_homes()
            allow = allowed_home_ids(self._kv_repo)
        devices = await self._miot_proxy.get_devices()
        info = devices.get(did)
        if info is None:
            cameras = await self._miot_proxy.get_cameras()
            info = cameras.get(did) if cameras else None
        if info is None:
            raise ResourceNotFoundException(f"Device '{did}' not found")
        if not is_home_allowed(self._kv_repo, getattr(info, "home_id", None)):
            raise ValidationException(
                f"Device '{did}' is not in an allowed home"
            )

    def _safe_lru_touch(self, did: str, iids: list[str]) -> None:
        """Best-effort LRU 写入；任何异常只 warning，不让控制返回受影响。

        语义是「用户意图」而非「操作成功」——上游 set_device_properties 即使
        云端返回 code != 0（设备离线 / 只读 / 限流）也不抛异常，调用方仍会触发
        本函数。这是有意为之：用户表达过的关注点应进入 LRU 占据展示槽位，
        以便下次目录注入时优先呈现，与控制是否真正生效无关。
        """
        try:
            for iid in iids:
                self._lru.touch(did, iid)
        except Exception as e:
            logger.warning("LRU touch failed for did=%s iids=%s: %s", did, iids, e)

    def _clear_account_scope_state(self) -> None:
        """Clear service-layer scope residue (called on account switch)."""
        self._kv_repo.delete(ScopeConfigKeys.HOME_WHITE_LIST_KEY)
        self._kv_repo.delete(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY)
        self._lru.clear()

    @property
    def miot_client(self):
        """Get the MIoTClient instance."""
        return self._miot_proxy.miot_client

    async def authorize_with_code(self, code: str, state: str):
        """
        Exchange the OAuth authorization code (provided by user after redirect)
        for an access token, then refresh runtime state.
        """
        try:
            logger.info("authorize_with_code state=%s code=%s…", state, code[:8])

            self._clear_account_scope_state()
            await self._miot_proxy.get_miot_auth_info(code=code, state=state)

            # 登录后 list_homes 兜底会自动选第一个家庭（如果启用集为空）。
            await self.list_homes()
            # list_homes 已确保 HOME_WHITE_LIST_KEY 非空（空集时自动选首个家庭）；
            # get_miot_auth_info 内部的初次 refresh_cameras 在白名单还是空集时运行，
            # is_home_allowed 对空集返回 False → 所有摄像头被 continue 跳过 →
            # _camera_img_managers 为空。这里补一次确保 managers 正确创建。
            await self._miot_proxy.refresh_cameras()
            # _sync_camera_adapter 的结果会被下面 restart 里的 sync_all_devices 覆盖,
            # 保留是为了在 perception engine 未运行时也能让感知订阅状态收敛。
            await self._sync_camera_adapter()

            # Restart perception engine so camera adapters can re-register
            # frame callbacks now that camera_img_managers exist.
            await self._restart_perception_engine()

            # 授权 + list_homes 自动选家已完成 → 首次安装在这里就能触发主动
            # onboarding 邀请，无需等下次重启。fire-and-forget：邀请失败/条件
            # 不满足都不影响授权主流程（幂等判定收在 maybe_trigger 内）。
            self._kick_onboarding_trigger()

        except Exception as e:
            logger.error("Failed to process Xiaomi MiOT authorization code: %s", e)
            raise MiotServiceException(
                f"Failed to process Xiaomi MiOT authorization code: {str(e)}"
            ) from e

    def _kick_onboarding_trigger(self) -> None:
        """授权成功后异步触发一次 onboarding 主动邀请检查（不阻塞、不抛错）。"""
        try:
            from miloco.manager import get_manager

            task = asyncio.create_task(get_manager().onboarding_trigger.maybe_trigger())
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        except Exception:  # noqa: BLE001
            logger.warning("onboarding 主动邀请触发失败(忽略)", exc_info=True)

    async def _restart_perception_engine(self):
        """Restart perception engine after auth to pick up newly available cameras."""
        try:
            from miloco.manager import get_manager

            perception_service = get_manager().perception_service
            logger.info("Restarting perception engine after auth callback")
            await perception_service.stop_engine()
            await perception_service.start_engine()
            logger.info("Perception engine restarted successfully")
        except Exception as e:
            # 有意不 re-raise：感知引擎重启失败不应导致授权本身失败，
            # token 已持久化，用户可手动重启服务恢复摄像头。
            logger.error("Failed to restart perception engine: %s", e)

    async def refresh_miot_all_info(self) -> dict:
        """
        Refresh MiOT all information

        Returns:
            dict: Dictionary containing result of each refresh operation
        """
        try:
            return await self._miot_proxy.refresh_miot_info()
        except Exception as e:
            logger.error("Failed to refresh MiOT all information: %s", e)
            raise MiotServiceException(
                f"Failed to refresh MiOT all information: {str(e)}"
            ) from e

    async def refresh_camera_online(self) -> bool:
        """轻量刷新相机在线状态(只更新缓存元数据,不扰 watch 流)。

        见 client.refresh_camera_online_status——专给前端「列相机」前调,解决相机重新
        上线后 is_online 不自愈,而又不像 refresh_miot_cameras 那样会瞬时卡流。
        """
        result = await self._miot_proxy.refresh_camera_online_status()
        return result is not None

    async def refresh_miot_cameras(self):
        """
        Refresh MiOT camera information
        """
        try:
            result = await self._miot_proxy.refresh_cameras()
            if not result:
                raise MiotServiceException("Failed to refresh MiOT cameras")
            return True
        except Exception as e:
            logger.error("Failed to refresh MiOT cameras: %s", e)
            raise MiotServiceException(
                f"Failed to refresh MiOT cameras: {str(e)}"
            ) from e

    async def refresh_miot_scenes(self):
        """
        Refresh MiOT scene information
        """
        try:
            result = await self._miot_proxy.refresh_scenes()
            # None means call failed; an empty dict just means no scenes available and should not be treated as an error
            if result is None:
                raise MiotServiceException("Failed to refresh MiOT scenes")
            return True
        except Exception as e:
            logger.error("Failed to refresh MiOT scenes: %s", e)
            raise MiotServiceException(
                f"Failed to refresh MiOT scenes: {str(e)}"
            ) from e

    async def refresh_miot_user_info(self):
        """
        Refresh MiOT user information
        """
        try:
            result = await self._miot_proxy.refresh_user_info()
            if not result:
                raise MiotServiceException("Failed to refresh MiOT user info")
            return True
        except Exception as e:
            logger.error("Failed to refresh MiOT user info: %s", e)
            raise MiotServiceException(
                f"Failed to refresh MiOT user info: {str(e)}"
            ) from e

    async def refresh_miot_devices(self):
        """
        Refresh MiOT device information
        """
        try:
            result = await self._miot_proxy.refresh_devices()
            if not result:
                raise MiotServiceException("Failed to refresh MiOT devices")
            return True
        except Exception as e:
            logger.error("Failed to refresh MiOT devices: %s", e)
            raise MiotServiceException(
                f"Failed to refresh MiOT devices: {str(e)}"
            ) from e

    def get_mips_status(self) -> dict:
        """Cloud MQTT (mips_cloud) subscription status snapshot.

        Used by /api/miot/mips_status to check whether real-time device-bind
        detection is currently working — see MipsStatusResponse for fields.
        """
        return self._miot_proxy.get_mips_status()

    async def get_miot_bind_status(self) -> dict:
        """
        Get MIoT bind status

        Returns:
            dict: Dictionary containing is_bound and user_info (if bound)
        """
        try:
            is_token_valid = await self._miot_proxy.check_token_valid()
            # max_enabled_cameras 随状态一并下发，作为前端「最多投喂几路」的唯一来源
            # （front 不再各自硬编码上限）。绑定与否都带，未绑时前端也能拿到上限。
            if not is_token_valid:
                return {
                    "is_bound": False,
                    "max_enabled_cameras": MAX_ENABLED_CAMERAS,
                }
            user_info = await self._miot_proxy.get_user_info()
            result: dict = {
                "is_bound": True,
                "max_enabled_cameras": MAX_ENABLED_CAMERAS,
            }
            if user_info:
                result["user_info"] = user_info
            return result
        except Exception as e:
            logger.error("Failed to check MIoT bind status: %s", e)
            raise MiotServiceException(
                f"Failed to check MIoT bind status: {str(e)}"
            ) from e

    async def bind_miot(self) -> dict:
        """
        Bind MIoT: Create a new OAuth URL for user authorization.

        Returns:
            dict: Dictionary containing oauth_url
        """
        try:
            oauth_url = await self._miot_proxy.get_miot_login_url()
            return {"oauth_url": oauth_url}
        except Exception as e:
            logger.error("Failed to bind MIoT: %s", e)
            raise MiotServiceException(f"Failed to bind MIoT: {str(e)}") from e

    async def unbind_miot(self) -> None:
        """
        Unbind MIoT: fully clean up MIoT state and reinitialize to a clean state.
        """
        try:
            self._clear_account_scope_state()
            await self._miot_proxy.deinit()
            # deinit 已清空 _camera_info_dict 和 token；init 重建 client 但无
            # 有效 token，refresh_cameras 大概率静默失败（返回 None）。
            # 仍调用一次：若 token 残留则清掉旧摄像机 managers；失败无副作用。
            await self._miot_proxy.init()
            await self._miot_proxy.refresh_cameras()
            await self._sync_camera_adapter()
        except Exception as e:
            logger.error("Failed to unbind MIoT: %s", e)
            raise MiotServiceException(f"Failed to unbind MIoT: {str(e)}") from e

    async def get_miot_login_status(self) -> dict:
        """
        Get MiOT login status

        Returns:
            dict: Dictionary containing status and message (if not logged in)

        Raises:
            MiotOAuthException: When user is not logged in or login status check fails
        """
        try:
            is_token_valid = await self._miot_proxy.check_token_valid()
            if not is_token_valid:
                return {
                    "is_logged_in": False,
                    "message": "请调用 miloco-cli account bind 进行登录",
                }
            return {"is_logged_in": True}

        except Exception as e:
            logger.error("Failed to check MiOT login status: %s", e)
            raise MiotOAuthException(
                f"Failed to check MiOT login status: {str(e)}"
            ) from e

    async def get_miot_user_info(self) -> MIoTUserInfo:
        """
        Get MiOT user information

        Returns:
            dict: User information dictionary

        Raises:
            ResourceNotFoundException: When unable to get user information
            ExternalServiceException: When external service call fails
        """
        try:
            user_info = await self._miot_proxy.get_user_info()

            if not user_info:
                raise ResourceNotFoundException("No logged in user information found")

            return user_info
        except Exception as e:
            logger.error("Failed to get MiOT user info: %s", e)
            raise MiotServiceException(f"Failed to get MiOT user info: {str(e)}") from e

    async def get_miot_camera_list(self) -> list[CameraInfo]:
        """
        Get MiOT camera list

        Returns:
            List[CameraInfo]: Camera information list

        Raises:
            MiotServiceException: When getting camera list fails
        """
        try:
            camera_dict: (
                dict[str, MIoTCameraInfo] | None
            ) = await self._miot_proxy.get_cameras()
            if not camera_dict:
                raise MiotServiceException("Failed to get MiOT camera list")

            camera_dict = filter_by_home(self._kv_repo, camera_dict)

            camera_list = [
                CameraInfo.model_validate(camera_info.model_dump())
                for camera_info in camera_dict.values()
            ]

            return camera_list
        except MiotServiceException:
            raise
        except Exception as e:
            logger.error("Failed to get MiOT camera list: %s", e)
            raise MiotServiceException(
                f"Failed to get MiOT camera list: {str(e)}"
            ) from e

    async def get_miot_device_list(self) -> list[DeviceInfo]:
        try:
            device_dict: dict[
                str, MIoTDeviceInfo
            ] = await self._miot_proxy.get_devices()
            if not device_dict:
                raise MiotServiceException("Failed to get MiOT device list")
            device_dict = filter_by_home(self._kv_repo, device_dict)
            device_list = []
            for device_info in device_dict.values():
                data = device_info.model_dump()
                sub_names = build_sub_device_names(device_info)
                data["sub_devices"] = sub_names or None
                device_list.append(DeviceInfo.model_validate(data))
            return device_list
        except MiotServiceException:
            raise
        except Exception as e:
            logger.error("Failed to get MiOT device list: %s", e)
            raise MiotServiceException(
                f"Failed to get MiOT device list: {str(e)}"
            ) from e

    async def get_miot_cameras_img(
        self, camera_dids: list[str], vision_use_img_count: int
    ) -> list[CameraImgSeq]:
        logger.info("get_miot_cameras_img, camera_dids: %s", ", ".join(camera_dids))
        try:
            all_camera_info: dict[
                str, MIoTCameraInfo
            ] = await self._miot_proxy.get_cameras()
            if not all_camera_info:
                return []

            selected_camera_info: list[MIoTCameraInfo] = [
                info for info in all_camera_info.values() if (info.did in camera_dids)
            ]

            camera_channels: list[CameraChannel] = []
            for camera_info in selected_camera_info:
                for channel in range(camera_info.channel_count or 1):
                    camera_channels.append(
                        CameraChannel(did=camera_info.did, channel=channel)
                    )

            camera_img_seqs = []
            for camera_channel in camera_channels:
                camera_img_seq = self._miot_proxy.get_recent_camera_img(
                    camera_channel.did, camera_channel.channel, vision_use_img_count
                )
                if not camera_img_seq:
                    logger.error(
                        "get_miot_cameras_img, get recent camera img failed, did: %s, channel: %s",
                        camera_channel.did,
                        camera_channel.channel,
                    )
                    continue

                camera_img_seqs.append(camera_img_seq)
            return camera_img_seqs
        except Exception as e:
            logger.error("Failed to get MiOT camera images: %s", e)
            raise MiotServiceException(
                f"Failed to get MiOT camera images: {str(e)}"
            ) from e

    async def get_miot_scene_list(self) -> list[SceneInfo]:
        """
        Get all MiOT scenes

        Returns:
            dict: Scene information dictionary

        Raises:
            MiotServiceException: When getting scenes fails
        """
        try:
            scenes: (
                dict[str, MIoTManualSceneInfo] | None
            ) = await self._miot_proxy.get_all_scenes()

            if scenes is None:
                raise MiotServiceException("Failed to get MiOT scene list")

            scenes = filter_by_home(self._kv_repo, scenes)

            scene_info_list = [
                SceneInfo(
                    scene_id=scene_info.scene_id, scene_name=scene_info.scene_name
                )
                for scene_info in scenes.values()
            ]

            return scene_info_list
        except MiotServiceException:
            raise
        except Exception as e:
            logger.error("Failed to get MiOT scene list: %s", e)
            raise MiotServiceException(
                f"Failed to get MiOT scene list: {str(e)}"
            ) from e

    async def send_notify(self, notify: str) -> None:
        """Send notification.

        Identical text seen again within ``notify.dedup_window_sec`` is skipped
        (returns ok without hitting MiHome) — a safety net so an agent loop can't
        turn into a burst of duplicate pushes. The window is recorded only on a
        successful send, so a failed attempt stays retryable.
        """
        key = notify.strip()
        if self._notify_deduper.is_duplicate(key):
            logger.info(
                "send_notify skipped: identical message within dedup window"
            )
            return
        try:
            notify_id = await self._miot_proxy.get_miot_app_notify_id(notify)
            if not notify_id:
                raise ValidationException(
                    "MiOT app notification content is inappropriate, please re-enter"
                )
            result = await self._miot_proxy.send_app_notify(notify_id)
            if not result:
                raise BusinessException("Failed to send notification")
        except Exception as e:
            logger.error("Failed to send notification: %s", str(e))
            raise BusinessException(f"Failed to send notification: {str(e)}") from e
        self._notify_deduper.record(key)

    async def start_audio_stream(self, camera_id: str, channel: int, callback):
        """Start audio stream."""
        try:
            logger.info(
                "Starting audio stream: camera_id=%s, channel=%s", camera_id, channel
            )
            await self._miot_proxy.start_camera_raw_audio_stream(
                camera_id, channel, callback
            )
        except Exception as e:
            logger.error("Failed to start audio stream: %s", e)
            raise MiotServiceException(f"Failed to start audio stream: {str(e)}") from e

    async def stop_audio_stream(self, camera_id: str, channel: int):
        """Stop audio stream."""
        try:
            logger.info("Stopping audio stream: camera_id=%s", camera_id)
            await self._miot_proxy.stop_camera_raw_audio_stream(camera_id, channel)
        except Exception as e:
            logger.error("Failed to stop audio stream: %s", e)
            raise MiotServiceException(f"Failed to stop audio stream: {str(e)}") from e

    def get_audio_codec(self, camera_id: str, channel: int) -> str:
        """Get detected audio codec for a camera channel."""
        return self._miot_proxy.get_audio_codec(camera_id, channel)

    async def start_video_stream(
        self, camera_id: str, channel: int, callback
    ) -> int:
        """Subscribe to *decoded* video frames for live transcode.

        Returns the SDK ``reg_id`` (needed by :meth:`stop_video_stream`).
        The callback receives BGR ndarrays produced by the SDK's PyAV
        decoder, shared with perception via ``multi_reg=True`` — decode
        happens once per camera regardless of how many subscribers.
        """
        try:
            logger.info(
                "Starting decoded video stream: camera_id=%s, channel=%s",
                camera_id, channel,
            )
            if callback is None:
                logger.info(
                    "No callback function, skipping registration: camera_id=%s",
                    camera_id,
                )
                return -1
            return await self._miot_proxy.start_camera_decode_video_stream(
                camera_id, channel, callback
            )
        except Exception as e:
            logger.error("Failed to start video stream: %s", e)
            raise MiotServiceException(f"Failed to start video stream: {str(e)}") from e

    async def stop_video_stream(
        self, camera_id: str, channel: int, reg_id: int
    ):
        """Unsubscribe from the decoded video stream (paired with start)."""
        try:
            logger.info(
                "Stopping decoded video stream: camera_id=%s, reg_id=%d",
                camera_id, reg_id,
            )
            await self._miot_proxy.stop_camera_decode_video_stream(
                camera_id, channel, reg_id
            )
        except Exception as e:
            logger.error("Failed to stop video stream: %s", e)
            raise MiotServiceException(f"Failed to stop video stream: {str(e)}") from e

    async def get_home_info(self, *, refresh: bool = False) -> dict:
        """Get home info。refresh=True 时先刷新云端数据。"""
        try:
            if refresh:
                await asyncio.gather(
                    self._miot_proxy.refresh_devices(),
                    self._miot_proxy.refresh_scenes(),
                    self._miot_proxy.refresh_cameras(),
                )
            data = await self._miot_proxy.get_home_info_data()

            # 家庭过滤：data 内的 devices/scenes 不带 home_id，借助原始 dict 反查
            allow = allowed_home_ids(self._kv_repo)
            if allow:
                allowed_dids = set(
                    filter_by_home(self._kv_repo, await self._miot_proxy.get_devices()).keys()
                )
                allowed_scene_ids = set(
                    filter_by_home(self._kv_repo,
                        await self._miot_proxy.get_all_scenes() or {}
                    ).keys()
                )
                data["devices"] = [
                    d for d in data.get("devices", []) if d.get("did") in allowed_dids
                ]
                data["scenes"] = [
                    s
                    for s in data.get("scenes", [])
                    if s.get("scene_id") in allowed_scene_ids
                ]
                data["areas"] = [
                    {"name": a}
                    for a in sorted({d.get("room") for d in data["devices"] if d.get("room")})
                ]
            else:
                # 未选择家庭：清空 devices/scenes/areas
                data["devices"] = []
                data["scenes"] = []
                data["areas"] = []
            # home_name 选举:仅在 allow 非空(住户已选家庭)时挑唯一家;
            # allow 为空表示未选择家庭,此时 data["devices"]/scenes
            # 为空集,home_name 显式置 None。
            home_id_to_name = data.get("home_id_to_name") or {}
            if not allow:
                data["home_name"] = None
            else:
                # 优先 cache,cache 空 *或* cache 跟 allow 无交集时 fallback list_homes
                # (家里所有摄像头都离线导致 device cache 不含启用集 hid 的 case)。
                home_name: str | None = None
                if home_id_to_name:
                    sorted_hids = sorted(home_id_to_name.keys())
                    pick_hids = [h for h in sorted_hids if h in allow]
                    if pick_hids:
                        home_name = home_id_to_name[pick_hids[0]]
                if home_name is None:
                    try:
                        homes = await self.list_homes()
                    except Exception as e:
                        logger.warning("list_homes failed in get_home_info: %s", e)
                        homes = []
                    sorted_homes = sorted(homes, key=lambda h: h["home_id"])
                    pick = [h for h in sorted_homes if h["home_id"] in allow]
                    if pick:
                        home_name = pick[0].get("home_name")
                data["home_name"] = home_name
            # home_id_to_name 是 backend 内部用的中转，前端不需要。
            # client.py::get_home_info_data 每次 build 新 dict（dict literal 现构造），
            # 这里 pop 不会污染上游 cache。
            data.pop("home_id_to_name", None)

            if self._person_repo:
                persons = self._person_repo.get_all()
                data["persons"] = [p.model_dump() for p in persons]
            return data
        except Exception as e:
            logger.error("Failed to get home info: %s", e)
            raise MiotServiceException(f"Failed to get home info: {str(e)}") from e

    async def get_device_spec(self, did: str) -> dict:
        """Get single device spec (轻量，不刷新云端数据)。"""
        dev = (await self._miot_proxy.get_devices()).get(did)
        if dev is None:
            raise ValidationException(f"did '{did}' not found")
        sub_names = build_sub_device_names(dev)
        spec = await self._miot_proxy._fetch_device_spec(dev.urn, sub_names) or {}
        return {
            "did": dev.did,
            "name": dev.name,
            "home": dev.home_name,
            "model": dev.model,
            "room": dev.room_name,
            "online": dev.online,
            "category": dev.urn.split(":")[3] if ":" in dev.urn else None,
            "spec": spec,
        }

    async def control_device(self, did: str, request: DeviceControlRequest) -> dict:
        """Control device: set_property / set_properties / call_action."""
        try:
            await self._assert_did_in_allowed_home(did)

            if request.type == "set_property":
                if not request.iid:
                    raise ValidationException("iid is required for set_property")
                siid, piid = _parse_prop_iid(request.iid)
                params = [
                    MIoTSetPropertyParam(
                        did=did, siid=siid, piid=piid, value=request.value
                    )
                ]
                results = await self._miot_proxy.set_device_properties(params)
                self._safe_lru_touch(did, [request.iid])
                return {"results": results}

            if request.type == "set_properties":
                if not request.properties:
                    raise ValidationException(
                        "properties is required for set_properties"
                    )
                params = []
                for prop in request.properties:
                    siid, piid = _parse_prop_iid(prop.iid)
                    params.append(
                        MIoTSetPropertyParam(
                            did=did, siid=siid, piid=piid, value=prop.value
                        )
                    )
                results = await self._miot_proxy.set_device_properties(params)
                self._safe_lru_touch(did, [p.iid for p in request.properties])
                return {"results": results}

            # call_action
            if not request.iid:
                raise ValidationException("iid is required for call_action")
            siid, aiid = _parse_action_iid(request.iid)
            param = MIoTActionParam(
                did=did, siid=siid, aiid=aiid, in_=request.params or []
            )
            result = await self._miot_proxy.call_device_action(param)
            self._safe_lru_touch(did, [request.iid])
            return {"result": result}

        # 兜底：原写法 `except A, B:` 是 Python 2 语法，在 Python 3 上为 SyntaxError，
        # 会导致本模块在 3.x 解释器下整个无法加载。修正为 Python 3 规范的元组捕获语法。
        except (ValidationException, ResourceNotFoundException):
            raise
        except Exception as e:
            logger.error("Failed to control device %s: %s", did, e)
            raise MiotServiceException(f"Failed to control device: {str(e)}") from e

    async def get_device_status(self, did: str, iids: list[str] | None) -> dict:
        """Get device property values. iids is list of 'prop.{siid}.{piid}' strings."""
        try:
            devices = await self._miot_proxy.get_devices()
            if did not in devices:
                raise ResourceNotFoundException(f"Device '{did}' not found")
            if not is_home_allowed(self._kv_repo, getattr(devices[did], "home_id", None)):
                raise ValidationException(
                    f"Device '{did}' is not in an allowed home"
                )

            # 用户主动指定 iids = 「这次确实关心这些 prop」→ 写 LRU；
            # 不传 iids 走全量可读冷查询，不算"用过"，不写。
            user_specified = bool(iids)
            if not iids:
                iids = await self._miot_proxy.get_readable_prop_iids(did)
                if not iids:
                    return {"properties": []}

            params = [
                MIoTGetPropertyParam(did=did, siid=siid, piid=piid)
                for siid, piid in (_parse_prop_iid(iid) for iid in iids)
            ]
            results = await self._miot_proxy.get_device_properties(params)
            properties = [
                {
                    "iid": f"prop.{r['siid']}.{r['piid']}",
                    "value": r.get("value"),
                    "code": r.get("code", 0),
                }
                for r in results
            ]
            if user_specified:
                self._safe_lru_touch(did, iids)
            return {"properties": properties}

        # 兜底：同上，Python 2 的 `except A, B:` 语法在 Python 3 下是 SyntaxError。
        except (ValidationException, ResourceNotFoundException):
            raise
        except Exception as e:
            logger.error("Failed to get device status %s: %s", did, e)
            raise MiotServiceException(f"Failed to get device status: {str(e)}") from e

    # ─── scope: 家庭 / 相机接入范围 ──────────────────────────────────────────

    async def list_homes(self) -> list[dict]:
        """列出账号下全部家庭（绕过过滤），每项含 in_use 标记。

        优先调米家 SDK ``get_homes_async()`` 拿用户真全集（含没设备 / 设备全离线
        的家），失败兜底到从 cached devices/cameras 反推。Union devices 与 cameras
        两个 dict 的 home_id —— 「家里只装了一台摄像头、无其他设备」这种单看
        device dict 会漏。

        兜底：启用集为空时自动选第一个家庭。
        """
        allow = allowed_home_ids(self._kv_repo)
        seen: dict[str, dict] = {}

        # 主路径：米家 user-level API 拿全集
        try:
            home_infos = await self._miot_proxy.miot_client.get_homes_async(
                fetch_share_home=True,
            )
            for hid, info in home_infos.items():
                seen[hid] = {
                    "home_id": hid,
                    "home_name": info.home_name,
                    "in_use": hid in allow,
                }
        except Exception as e:
            logger.warning("get_homes_async failed, fallback to device cache: %s", e)

        # 兜底 / 补集：device + camera cache 推断(防 SDK 漏 / SDK 调用失败)。
        # 如果主路径返了某 hid 但 home_name=None(米家偶尔会这样),fallback 路径
        # 这边的设备 home_name 可能有真值,补上去——不能简单 continue 把 hid 跳过。
        # 包 except:主路径 get_homes_async 失败时 fallback 大概率也是同一个 SDK 异常
        # (token 过期 / 网络断 / SDK rate limit),不包就把整个 list_homes 干 500 →
        # 前端 HomeSwitcher 不渲染住户连切家入口都没,得重启 backend。
        try:
            devices = await self._miot_proxy.get_devices() or {}
            cameras = await self._miot_proxy.get_cameras() or {}
        except Exception as e:
            logger.warning("list_homes fallback get_devices/cameras failed: %s", e)
            devices, cameras = {}, {}
        for info in list(devices.values()) + list(cameras.values()):
            hid = getattr(info, "home_id", None)
            if not hid:
                continue
            n = getattr(info, "home_name", None)
            if hid in seen:
                # falsy 判断兜空串——米家 SDK 偶尔返空字符串而非 None。
                if not seen[hid]["home_name"] and n:
                    seen[hid]["home_name"] = n
                continue
            seen[hid] = {
                "home_id": hid,
                "home_name": n,
                "in_use": hid in allow,
            }
        # 兜底：启用集为空，或启用集与可见家庭无交集（选中的家已失效）
        # → 自动选第一个家庭并清掉失效旧 id，避免 UI 伪装「已选」而感知全黑。
        visible = set(seen.keys())
        if seen and not (allow & visible):
            first = sorted(visible)[0]
            set_homes_in_use(self._kv_repo, [first], True)
            stale = [h for h in allow if h not in visible]
            if stale:
                set_homes_in_use(self._kv_repo, stale, False)
            allow = {first}
            logger.info("启用集与可见家庭无交集，自动启用首个家庭 %s（兜底）", first)
            for h in seen.values():
                h["in_use"] = h["home_id"] in allow

        # 按 home_id 字典序排序——米家 SDK 返回顺序受设备活跃度等影响不稳定，
        # 不排 HomeSwitcher 列表会在两次 reload 之间跳。
        return sorted(seen.values(), key=lambda h: h["home_id"])

    async def switch_home(self, home_id: str) -> list[dict]:
        """切换到指定家庭（唯一启用），其余自动停用。

        原子操作：先 add target 再 remove others，单事务完成无半态。
        返回切换后的全量家庭列表。刷新设备/摄像头/场景放到后台异步完成，
        避免让 HTTP 响应等待云端 API 调用。
        """
        homes = await self.list_homes()
        known = {h["home_id"] for h in homes}
        if home_id not in known:
            raise ValidationException(
                f"Unknown home_id {home_id!r}; valid: {sorted(known)}"
            )
        # 先把目标加进在用集合,再把其余移出。
        target_list, _ = set_homes_in_use(self._kv_repo, [home_id], True)
        others = [h for h in target_list if h != home_id]
        if others:
            target_list, _ = set_homes_in_use(self._kv_repo, others, False)

        # 后台异步刷新：设备/摄像头/场景列表需随家庭切换更新，但不必
        # 让 HTTP 响应等它们完成。设备列表/摄像头列表请求时兜底触发刷新。
        async def _background_refresh():
            results = await asyncio.gather(
                self._miot_proxy.refresh_devices(),
                self._miot_proxy.refresh_cameras(),
                self._miot_proxy.refresh_scenes(),
                return_exceptions=True,
            )
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                logger.warning("switch_home background refresh partial failure: %s",
                               errors)
            try:
                await self._sync_camera_adapter()
            except Exception as e:
                logger.warning("switch_home _sync_camera_adapter failed: %s", e)

        task = asyncio.create_task(_background_refresh())
        # 防御性持有引用，避免 task 在 await 挂起期间被 GC 回收。
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        # 切换家庭后重置 openclaw 里的 miloco session，清掉旧家庭遗留的上下文
        # （设备 / 房间 / 习惯），避免串入新家庭。best-effort 后台执行：openclaw
        # 不可达 / 删除失败只 WARN，绝不阻塞或打断切换本身。
        async def _reset_agent_sessions_bg():
            from miloco.dispatch.dispatcher import MILOCO_SESSION_KEYS
            from miloco.utils.agent_client import reset_agent_sessions

            try:
                await reset_agent_sessions(MILOCO_SESSION_KEYS)
            except Exception as e:
                logger.warning("switch_home reset agent sessions failed: %s", e)

        reset_task = asyncio.create_task(_reset_agent_sessions_bg())
        _background_tasks.add(reset_task)
        reset_task.add_done_callback(_background_tasks.discard)

        # KV 已写入，本地更新 in_use 标记后立即返回，不等待 refresh 完成。
        allow = allowed_home_ids(self._kv_repo)
        for h in homes:
            h["in_use"] = h["home_id"] in allow
        return homes

    async def list_cameras_with_state(self) -> list[dict]:
        """列出当前启用家庭下的相机，每项含 is_online / in_use / connected。"""
        denied = denied_camera_dids(self._kv_repo)
        connected = self._connected_camera_dids()
        cameras = filter_by_home(
            self._kv_repo, await self._miot_proxy.get_cameras() or {}
        )
        # 过滤已从账号删除的摄像头：_camera_info_dict 是内存缓存，
        # 设备删除后不会自动清除，需要用 _device_info_dict 做交集校验。
        devices = await self._miot_proxy.get_devices()
        cameras = {did: info for did, info in cameras.items() if did in devices}
        out: list[dict] = []
        for did, info in cameras.items():
            online = bool(getattr(info, "online", False)) and bool(
                getattr(info, "lan_online", False)
            )
            out.append(
                {
                    "did": did,
                    "name": getattr(info, "name", None),
                    # 透 room_name 让前端能在多摄像头家庭显示"客厅 / 卧室"区分——
                    # 米家默认相机名常是"小米智能摄像机 2 代"等泛称，光看 name 难辨。
                    "room_name": getattr(info, "room_name", None),
                    "is_online": online,
                    "in_use": did not in denied,
                    "connected": did in connected,
                }
            )
        return out

    async def toggle_camera(self, items: list[dict]) -> list[dict]:
        """批量切换相机启用状态。每项 {"did": str, "in_use": bool}。

        全部校验通过后才一起写入。双向均校验未知 did 防 typo。
        """
        enable_dids = [i["did"] for i in items if i["in_use"]]
        disable_dids = [i["did"] for i in items if not i["in_use"]]
        all_dids = enable_dids + disable_dids

        cameras = await self._miot_proxy.get_cameras() or {}
        unknown = [d for d in all_dids if d not in cameras]
        if unknown:
            raise ValidationException(
                f"Unknown camera did(s) {unknown}; valid: {sorted(cameras.keys())}"
            )

        if enable_dids:
            # 离线设备禁止「开启」投喂:它被感知接入层 online_only 过滤、永远连不上,
            # 开了也不出画面、徒占上限名额。只拦「开启」——已启用的设备掉线后仍保留
            # inUse=true(允许态不被强制改),且可正常被「关闭」(disable 不走这条校验)。
            # 在线口径 = online && lan_online,与 list_cameras_with_state 的 is_online 一致。
            def _online(did: str) -> bool:
                info = cameras[did]
                return bool(getattr(info, "online", False)) and bool(
                    getattr(info, "lan_online", False)
                )

            offline_enable = [d for d in enable_dids if not _online(d)]
            if offline_enable:
                raise ValidationException(
                    f"摄像头当前离线,无法开启投喂（{offline_enable}）;请待其上线后再启用"
                )

            # 上限检查：用户主动 enable 超限时直接报错，不做自动禁用。计数口径与
            # list_cameras_with_state / refresh_cameras 一致——只数当前启用家庭内、
            # 未拉黑的相机（get_cameras 返回全部家庭，须按 home 过滤）。
            denied = denied_camera_dids(self._kv_repo)

            def _in_scope(did: str) -> bool:
                return is_home_allowed(
                    self._kv_repo, getattr(cameras[did], "home_id", None)
                )

            in_scope = {d for d in cameras if _in_scope(d)}
            # 模拟本批操作后的启用集：现状未拉黑的，先去掉本批 disable，再并入
            # 本批 enable。enable 最后并入 → 与写库顺序一致（disable 先写、
            # enable 后写，矛盾输入 enable 胜出）。单向 enable / 单向 disable /
            # 混合换机都按净结果校验。
            final_enabled = (
                (in_scope - denied) - set(disable_dids)
            ) | (set(enable_dids) & in_scope)
            if len(final_enabled) > MAX_ENABLED_CAMERAS:
                raise ValidationException(
                    f"最多同时启用 {MAX_ENABLED_CAMERAS} 台摄像头"
                    f"（操作后将有 {len(final_enabled)} 台），"
                    f"请先禁用一台再启用新摄像头"
                )

        changed = False
        if disable_dids:
            _, c = set_cameras_in_use(self._kv_repo, disable_dids, False)
            changed = changed or c
        if enable_dids:
            _, c = set_cameras_in_use(self._kv_repo, enable_dids, True)
            changed = changed or c
        if changed:
            # 先 refresh_cameras：按新 KV(黑名单)建/销 camera manager——关掉的相机
            # 销毁 manager，停掉 native PPCS 会话+解码线程，不再拉流。
            # 再 _sync_camera_adapter：perception 按新 manager 集连/断订阅。顺序不可换,
            # 否则 sync 先连上随后 manager 被销,会留 stale reg_id。
            await self._miot_proxy.refresh_cameras()
            await self._sync_camera_adapter()
        # 返回受影响的相机，结构与 list_cameras_with_state 一致
        all_cameras = await self.list_cameras_with_state()
        affected = [cam for cam in all_cameras if cam["did"] in set(all_dids)]
        return affected

    def _camera_adapter(self):
        """Lazily fetch the perception camera adapter; returns None if unavailable."""
        try:
            from miloco.manager import get_manager

            return get_manager().perception_service._collector.get_adapter("camera")
        except Exception as e:
            logger.warning("Camera adapter lookup failed: %s", e)
            return None

    def _connected_camera_dids(self) -> set[str]:
        adapter = self._camera_adapter()
        return set(adapter.get_connected_devices().keys()) if adapter else set()

    async def _sync_camera_adapter(self) -> None:
        """Hot-sync camera connections after a scope change."""
        adapter = self._camera_adapter()
        if adapter is None:
            return
        try:
            await adapter.sync_devices()
        except Exception as e:
            logger.warning("Camera adapter sync after scope change failed: %s", e)

    async def trigger_scene(self, scene_id: str) -> bool:
        """Trigger a MIoT manual scene."""
        try:
            scenes = await self._miot_proxy.get_all_scenes()
            if not scenes or scene_id not in scenes:
                raise ResourceNotFoundException(f"Scene '{scene_id}' not found")
            if not is_home_allowed(self._kv_repo, getattr(scenes[scene_id], "home_id", None)):
                raise ValidationException(
                    f"Scene '{scene_id}' is not in an allowed home"
                )
            return await self._miot_proxy.execute_miot_scene(scene_id)
        except (ResourceNotFoundException, ValidationException):
            raise
        except Exception as e:
            logger.error("Failed to trigger scene %s: %s", scene_id, e)
            raise MiotServiceException(f"Failed to trigger scene: {str(e)}") from e
