# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MiOT service module
"""

import asyncio
import json
import logging
import time
import uuid

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
    MAX_CAMERA_PROMPT_LEN,
    MAX_ENABLED_CAMERAS,
    allowed_home_ids,
    camera_prompts,
    clear_camera_prompt,
    denied_audio_camera_dids,
    denied_camera_dids,
    denied_channels_of,
    denied_video_camera_dids,
    filter_by_home,
    is_home_allowed,
    physical_camera_did,
    select_active_camera_dids,
    set_camera_prompt,
    set_cameras_audio_in_use,
    set_cameras_channels_in_use,
    set_cameras_video_in_use,
    set_cameras_voice_in_use,
    set_homes_in_use,
    synthetic_camera_did,
    voice_allowed_camera_dids,
)
from miloco.miot.lru import LRUStore
from miloco.miot.message_dedup import MessageDeduper
from miloco.miot.result_codes import summarize_results
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


def _truncate_value_len(value_json: str | None) -> int:
    """日志用:value_json 长度(不记内容,防 TTS 全文 / secrets 落日志)。"""
    return len(value_json) if value_json else 0


def _request_value_json(request: "DeviceControlRequest") -> str | None:
    """把控制请求的尝试参数归一成 value_json(成功/异常路径共用)。

    失败动作是排查时最有价值的场景——异常路径的台账也必须能看到 agent 当时
    具体试图设置什么值 / 播什么 TTS / 调用什么参数,不能只落 error。
    """
    try:
        if request.type == "set_property":
            return json.dumps(request.value, ensure_ascii=False)
        if request.type == "set_properties":
            return json.dumps(
                {p.iid: p.value for p in (request.properties or [])},
                ensure_ascii=False,
            )
        return json.dumps(request.params or [], ensure_ascii=False)
    except Exception:
        return None  # 参数本身不可序列化时不反噬审计主体


def _request_iid(request: "DeviceControlRequest") -> str | None:
    """把控制请求归一成台账 iid 列(成功/异常路径共用)。

    set_properties 不填顶层 iid(iid 在 properties 各项里),其台账行落逗号
    拼接的复数 iid——异常路径若直接取 request.iid 会恒落 NULL,按 iid 检索
    失败动作时就会漏行,故与成功路径同构地按 type 重建。
    """
    try:
        if request.type == "set_properties":
            return ",".join(p.iid for p in (request.properties or [])) or None
        return request.iid
    except Exception:
        return None  # 与 value_json 同口径:归一失败不反噬审计主体


async def _write_action_ledger(
    miot_proxy: MiotProxy,
    *,
    action_type: str,
    did: str,
    iid: str | None,
    value_json: str | None,
    result_code: int | None,
    result_msg: str | None,
    success: bool,
    error: str | None,
    source: str = "cli",
    source_id: str | None = None,
    home_id: str | None = None,
) -> None:
    """落一行 action_ledger + 打一条 INFO 结果日志。**fail-open**:

    ``source`` 区分触发源:``cli``(control_device 路径,含 manual CLI 与 agent-via-CLI,
    后者由 trace_id 区分)/ ``rule``(RuleRunner 直控,``source_id`` 写 rule_id)。

    整体裹 try/except,任何异常只 warning,绝不影响调用方的控制结果。
    device_name / room 从内存 device cache 解析(便宜),解析失败留 None。
    日志行不含 secrets:TTS / set 值只记 value_json 长度,全文只进 DB。
    """
    try:
        from miloco.observability.metrics_client import get_metrics_client
        from miloco.observability.types import ActionLedgerRecord

        device_name: str | None = None
        room: str | None = None
        try:
            dev = (await miot_proxy.get_devices()).get(did)
            if dev is None and home_id is None:
                # 摄像头只在 camera cache(control_device 的家庭校验同样两级查):
                # 不回落会让摄像头动作 home_id=NULL,经查询侧 NULL 放行串到所有家。
                # MIoTCameraInfo 继承 MIoTDeviceInfo,name/room_name/home_id 同字段。
                # 仅在 home_id 未显式传入时才回落——get_cameras() cache miss 会触发
                # 网络刷新,scene_trigger(did=scene_id、home 已传)不该为此买单。
                dev = ((await miot_proxy.get_cameras()) or {}).get(did)
            if dev is not None:
                device_name = getattr(dev, "name", None)
                room = getattr(dev, "room_name", None)
                if home_id is None:
                    # 未显式传入才从 cache 补。scene_trigger 的 did 是
                    # scene_id,cache 必 miss——那条路径由调用方带场景所属家传入。
                    home_id = getattr(dev, "home_id", None)
        except Exception:
            pass  # cache 解析失败不影响审计主体

        client = get_metrics_client()
        if client is not None:
            client.record_action(
                ActionLedgerRecord(
                    id=str(uuid.uuid4()),
                    timestamp=int(time.time() * 1000),
                    action_type=action_type,
                    did=did,
                    device_name=device_name,
                    room=room,
                    iid=iid,
                    value_json=value_json,
                    result_code=result_code,
                    result_msg=result_msg,
                    success=success,
                    error=error,
                    source=source,
                    source_id=source_id,
                    home_id=home_id,
                )
            )

        logger.info(
            "action_ledger device=%s(did=%s room=%s) type=%s iid=%s success=%s "
            "reason=%s value_len=%d",
            device_name or "?", did, room or "?", action_type, iid, success,
            (result_msg or error or "ok"),
            _truncate_value_len(value_json),
        )
    except Exception as e:  # noqa: BLE001 —— 审计 fail-open,绝不拖垮控制调用
        logger.warning("action_ledger write failed (did=%s): %s", did, e)


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
        self._kv_repo.delete(ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY)
        self._kv_repo.delete(ScopeConfigKeys.CAMERA_VIDEO_BLACK_LIST_KEY)
        self._kv_repo.delete(ScopeConfigKeys.CAMERA_AUDIO_BLACK_LIST_KEY)
        self._kv_repo.delete(ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY)
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
            from miloco.perception.engine_state import is_perception_enabled

            perception_service = get_manager().perception_service
            logger.info("Restarting perception engine after auth callback")
            await perception_service.stop_engine()  # 未运行时是安全 no-op
            # 尊重用户「休息」意图：被手动暂停时重新授权不自动拉起引擎，
            # 否则重新授权会绕开开机门控、无视暂停继续烧 token。
            if is_perception_enabled(self._kv_repo):
                await perception_service.start_engine()
                logger.info("Perception engine restarted successfully")
            else:
                logger.info("感知被用户手动休息，重新授权后不自动拉起引擎")
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
        # 尝试参数先归一好,成功/异常路径共用——SDK/网络抛异常时台账也能看到
        # agent 当时试图设置什么值 / 播什么 TTS / 什么参数。
        attempted_value_json = _request_value_json(request)
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
                success, code, msg = summarize_results(results)
                await _write_action_ledger(
                    self._miot_proxy,
                    action_type="set_property",
                    did=did, iid=request.iid,
                    value_json=attempted_value_json,
                    result_code=code, result_msg=msg,
                    success=success, error=None,
                )
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
                success, code, msg = summarize_results(results)
                await _write_action_ledger(
                    self._miot_proxy,
                    action_type="set_properties",
                    # 复数 iid 逗号拼接;value_json 存 {iid: value} 全集
                    iid=_request_iid(request),
                    did=did,
                    value_json=attempted_value_json,
                    result_code=code, result_msg=msg,
                    success=success, error=None,
                )
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
            success, code, msg = summarize_results(result)
            # call_action 的 in_params 存 value_json —— speaker play-text 的 TTS 全文落这里
            await _write_action_ledger(
                self._miot_proxy,
                action_type="call_action",
                did=did, iid=request.iid,
                value_json=attempted_value_json,
                result_code=code, result_msg=msg,
                success=success, error=None,
            )
            return {"result": result}

        # 兜底：原写法 `except A, B:` 是 Python 2 语法，在 Python 3 上为 SyntaxError，
        # 会导致本模块在 3.x 解释器下整个无法加载。修正为 Python 3 规范的元组捕获语法。
        except (ValidationException, ResourceNotFoundException):
            raise
        except Exception as e:
            logger.error("Failed to control device %s: %s", did, e)
            # 异常路径也落一行:success=0 + error + 尝试参数(失败审计完整性)
            await _write_action_ledger(
                self._miot_proxy,
                action_type=getattr(request, "type", None) or "call_action",
                did=did, iid=_request_iid(request),
                value_json=attempted_value_json,
                result_code=None, result_msg=None,
                success=False, error=str(e),
            )
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
            # 兜底自动切换同样换掉了启用家庭 → 重置会话，消除旧家庭上下文泄漏
            # （与显式 switch_home 同一 bug class）。
            self._schedule_agent_session_reset()

        # 按 home_id 字典序排序——米家 SDK 返回顺序受设备活跃度等影响不稳定，
        # 不排 HomeSwitcher 列表会在两次 reload 之间跳。
        return sorted(seen.values(), key=lambda h: h["home_id"])

    def _schedule_agent_session_reset(self) -> None:
        """切换家庭后后台 best-effort 重置 openclaw 里的 miloco session，清掉旧家庭
        遗留的上下文（设备 / 房间 / 习惯），避免串入新家庭。

        显式 `switch_home` 与 `list_homes` 兜底自动切换共用此入口。fire-and-forget：
        openclaw 不可达 / 删除失败只 WARN、不上抛，绝不阻塞或打断切换本身。
        """
        async def _bg():
            from miloco.dispatch.dispatcher import MILOCO_SESSION_ROUTES
            from miloco.utils.agent_client import reset_agent_sessions

            try:
                await reset_agent_sessions(MILOCO_SESSION_ROUTES)
            except Exception as e:
                logger.warning("reset agent sessions failed: %s", e)

        reset_task = asyncio.create_task(_bg())
        # 防御性持有引用，避免 task 在 await 挂起期间被 GC 回收。
        _background_tasks.add(reset_task)
        reset_task.add_done_callback(_background_tasks.discard)

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
        # 切换前后的启用集：只有真的变了才 reset——切到"已是当前唯一启用"的家庭
        # （重复点选 / 重复提交同一 home_id）不该白删仍然有效的热上下文。
        prev_allow = allowed_home_ids(self._kv_repo)
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

        # KV 已写入，本地更新 in_use 标记后立即返回，不等待 refresh 完成。
        allow = allowed_home_ids(self._kv_repo)
        # 启用集真的变化了才重置会话（避免切到当前家庭白丢热上下文）。
        if allow != prev_allow:
            self._schedule_agent_session_reset()
        for h in homes:
            h["in_use"] = h["home_id"] in allow
        return homes

    async def list_cameras_with_state(self) -> list[dict]:
        """列出当前启用家庭下的相机，每项含三态可用性 + in_use / voice_in_use / connected。

        可用性拆成三个正交指标（替代旧的一把揉 is_online）：
          - ``cloud_online``：米家云端在线
          - ``lan_reachable``：局域网可达（能拉流的前提）
          - ``awake``：镜头开关。True=镜头开启 / False=镜头关闭(隐私·遮挡) /
            None=该机型无开关属性或读取失败（未知）。走 cache_only 只读 refresh_camera_online_status
            已填的缓存，不单独打云；缓存空时为 None（未知），刷新后自愈。
        ``in_use``=**当下真正开启**（= 该相机在 select_active 的活跃集里：默认开·未拉黑 +
        三态满足 + 上限≤4）——离线/不可达/镜头关的相机 in_use=false，不显示为开；超上限的
        也不算开。兼容字段 ``is_online`` = ``cloud_online and lan_reachable``（纯连通性）。
        ``voice_in_use`` 是**存储的拾音偏好**（在拾音白名单即 True，**默认 False**）；
        「生效态」= ``in_use and voice_in_use`` 由前端派生。通过 ``toggle_camera`` 的
        ``audio_enabled`` / ``in_use`` 别名操作时同步写入拾音白名单，保持两路一致。
        """
        self._migrate_v1_blacklist_if_needed()
        voice_allowed = voice_allowed_camera_dids(self._kv_repo)
        video_denied = denied_video_camera_dids(self._kv_repo)
        audio_denied = denied_audio_camera_dids(self._kv_repo)
        prompt_map = camera_prompts(self._kv_repo)
        connected = self._connected_camera_dids()
        cameras = filter_by_home(
            self._kv_repo, await self._miot_proxy.get_cameras() or {}
        )
        # 过滤已从账号删除的摄像头：_camera_info_dict 是内存缓存，
        # 设备删除后不会自动清除，需要用 _device_info_dict 做交集校验。
        devices = await self._miot_proxy.get_devices()
        cameras = {did: info for did, info in cameras.items() if did in devices}
        # awake：只读缓存（云读收在 refresh_camera_online_status，前端列表前必调）。
        awake_map = await self._miot_proxy.read_cameras_awake(
            list(cameras.keys()), cache_only=True
        )
        # in_use = 活跃集：与拉流/投喂同一口径（select_active：未拉黑 + home + 三态 + 上限）。
        active = set(
            select_active_camera_dids(self._kv_repo, cameras, awake_map=awake_map)
        )
        out: list[dict] = []
        for did, info in cameras.items():
            cloud_online = bool(getattr(info, "online", False))
            lan_reachable = bool(getattr(info, "lan_online", False))
            channel_count = getattr(info, "channel_count", None) or 1
            lens_awake = awake_map.get(did) or {}
            # 全拆后每路是独立一等相机：``did`` 仍是物理 did（会话/拾音按整台），``channel``
            # 区分通道；``awake`` / ``in_use`` / ``connected`` 逐通道给。单通道 channel=0、
            # 各字段按裸 did，与旧行为一致，仅多带 channel。
            base = {
                "did": did,
                "name": getattr(info, "name", None),
                # 透 room_name 让前端能在多摄像头家庭显示"客厅 / 卧室"区分——
                # 米家默认相机名常是"小米智能摄像机 2 代"等泛称，光看 name 难辨。
                "room_name": getattr(info, "room_name", None),
                # 通道总数：判「多通道相机」的权威信号（channel_count>1），前端/CLI 据此
                # 决定是否拼合成 did、显镜头标签——与后端 select_active 同口径，别再用「行数」代理。
                "channel_count": channel_count,
                "cloud_online": cloud_online,
                "lan_reachable": lan_reachable,
                # 兼容旧字段：纯连通性(云端+局域网)，不含镜头开关维度。
                "is_online": cloud_online and lan_reachable,
                # 存储偏好：在拾音白名单 = 拾音开启（**默认关闭**，opt-in）。拾音按整台存
                # （只球机/ch0 有 mic），前端在无 mic 的通道上隐藏该开关。
                "voice_in_use": did in voice_allowed,
                # v2 per-modality 感知开关（整台粒度，物理 did）：
                "video_enabled": did not in video_denied,
                "audio_enabled": did not in audio_denied,
            }
            for ch in range(channel_count):
                syn_did = synthetic_camera_did(did, ch, channel_count)
                out.append(
                    {
                        **base,
                        "channel": ch,
                        # per-lens 镜头态（None=未知/机型无开关）
                        "awake": lens_awake.get(ch),
                        # per-channel 启用（活跃集按合成 did）+ per-channel 真投喂
                        "in_use": syn_did in active,
                        "connected": syn_did in connected,
                        # 每摄像头的自定义「感知须知」prompt（无则 ""）。
                        # 按合成 did 存取，双摄每路可有独立须知。
                        "perception_prompt": prompt_map.get(syn_did, ""),
                    }
                )
        return out

    async def toggle_camera(self, items: list[dict]) -> list[dict]:
        """批量切换相机感知开关（全拆通道级 + v2 per-modality）。

        每项形如 ``{"did": str, "in_use"?: bool, "video_enabled"?: bool, "audio_enabled"?: bool}``。
        - ``in_use``：控制该通道（合成 did / 裸 did → 整台全通道）的连接激活态（写 CAMERA_BLACK_LIST_KEY）
        - ``video_enabled``：per-modality 视频感知开关（写 CAMERA_VIDEO_BLACK_LIST_KEY，整台粒度）
        - ``audio_enabled``：per-modality 音频感知开关（写 CAMERA_AUDIO_BLACK_LIST_KEY，整台粒度）
        三个字段都可选；omitted = 不改。全拆语义：启停按**通道**走（每路一台独立相机）。
        """
        cameras = await self._miot_proxy.get_cameras() or {}

        def _cc(pdid: str) -> int:
            return getattr(cameras.get(pdid), "channel_count", None) or 1

        # 解析每项 → 物理 did + 通道集；聚成 updates[physical_did][channel] = in_use。
        # 裸多通道 did 展成全通道；同一 (did,ch) 后到覆盖先到（矛盾输入以最后一项为准）。
        # **后端是唯一执法点**（前端只发合法 did，CLI/API 直连全靠这里挡）：畸形合成 did
        # （`:ch` 后为空/非数字）或**越界通道**（≥ channel_count）都当非法 did 拒——否则前者
        # int() 崩 500、后者会把死条目写进黑名单（读侧只遍历 range(cc) 永远清不掉）。
        updates: dict[str, dict[int, bool]] = {}
        video_updates: dict[str, bool] = {}
        audio_updates: dict[str, bool] = {}
        voice_updates: dict[str, bool] = {}
        unknown: list[str] = []
        bad_channel: list[str] = []
        for it in items:
            raw = it["did"]
            pdid = physical_camera_did(raw)
            if pdid not in cameras:
                unknown.append(raw)
                continue
            cc = _cc(pdid)
            if ":ch" in raw:
                _, ch_str = raw.rsplit(":ch", 1)
                try:
                    ch = int(ch_str)
                except ValueError:
                    bad_channel.append(raw)
                    continue
                if not (0 <= ch < cc):
                    bad_channel.append(raw)
                    continue
                chans = [ch]
            else:
                chans = list(range(cc))
            if "in_use" in it:
                in_use = bool(it["in_use"])
                for c in chans:
                    updates.setdefault(pdid, {})[c] = in_use
                if "video_enabled" not in it:
                    video_updates[pdid] = in_use
                if "audio_enabled" not in it:
                    audio_updates[pdid] = in_use
                    voice_updates[pdid] = in_use
            if "video_enabled" in it:
                video = bool(it["video_enabled"])
                video_updates[pdid] = video
                # 开启视频感知时自动激活通道（否则相机不连接、无实时画面）
                if video and "in_use" not in it:
                    for c in chans:
                        updates.setdefault(pdid, {})[c] = True
            if "audio_enabled" in it:
                audio = bool(it["audio_enabled"])
                audio_updates[pdid] = audio
                voice_updates[pdid] = audio
                if audio and "in_use" not in it:
                    for c in chans:
                        updates.setdefault(pdid, {})[c] = True
        if unknown:
            raise ValidationException(
                f"Unknown camera did(s) {unknown}; valid: {sorted(cameras.keys())}"
            )
        if bad_channel:
            raise ValidationException(
                f"非法通道号（格式错误或越界）: {bad_channel}"
            )

        def _in_scope(pdid: str) -> bool:
            return is_home_allowed(
                self._kv_repo, getattr(cameras[pdid], "home_id", None)
            )

        def _cloud(pdid: str) -> bool:
            return bool(getattr(cameras[pdid], "online", False))

        def _lan(pdid: str) -> bool:
            return bool(getattr(cameras[pdid], "lan_online", False))

        enabling = [
            (p, ch) for p, chs in updates.items() for ch, iu in chs.items() if iu
        ]
        if enabling:
            in_scope = {p for p in cameras if _in_scope(p)}
            # 三态门（后端唯一执法点）：开启的通道必须 云端在线 && 局域网可达 && **该路**镜头
            # 未关。云端/局域网是相机级；镜头是 per-lens。awake 走新鲜云读（非 cache_only）。
            awake_map = await self._miot_proxy.read_cameras_awake(sorted(in_scope))

            cloud_offline = sorted({p for p, _ in enabling if not _cloud(p)})
            if cloud_offline:
                raise ValidationException(
                    f"摄像头米家云端离线,无法开启（{cloud_offline}）;请待其上线后再启用"
                )
            lan_offline = sorted({p for p, _ in enabling if not _lan(p)})
            if lan_offline:
                raise ValidationException(
                    f"摄像头局域网不可达,无法开启（{lan_offline}）;"
                    "请确认主机与相机在同一局域网后再启用"
                )
            lens_off = [
                synthetic_camera_did(p, ch, _cc(p))
                for p, ch in enabling
                if (awake_map.get(p) or {}).get(ch) is False
            ]
            if lens_off:
                raise ValidationException(
                    f"摄像头镜头已关闭,无法开启感知（{lens_off}）;"
                    "请先在米家中打开该摄像头镜头后再启用"
                )

            # 上限检查：数「操作后可用启用通道数」——每路独占一个名额。逐 in_scope 相机模拟
            # 本批操作后的黑名单，统计 未拉黑 && 三态好(云端+局域网+该路镜头开) 的通道数。
            # 镜头关/离线/局域网不可达的路不占名额（与 select_active / 前端 in_use 同口径）。
            denied_now = denied_camera_dids(self._kv_repo)
            streams_after = 0
            for p in in_scope:
                if not (_cloud(p) and _lan(p)):
                    continue
                cc = _cc(p)
                lens = awake_map.get(p) or {}
                disabled = denied_channels_of(denied_now, p, cc)
                for ch, iu in updates.get(p, {}).items():
                    disabled.discard(ch) if iu else disabled.add(ch)
                for ch in range(cc):
                    if ch in disabled or lens.get(ch) is False:
                        continue
                    streams_after += 1
            if streams_after > MAX_ENABLED_CAMERAS:
                raise ValidationException(
                    f"最多同时启用 {MAX_ENABLED_CAMERAS} 条摄像头视频流"
                    f"（操作后将有 {streams_after} 条），"
                    f"请先禁用一路再启用新的"
                )

        channels_changed = False
        if updates:
            _, channels_changed = set_cameras_channels_in_use(
                self._kv_repo, updates, {p: _cc(p) for p in updates}
            )
        video_changed = False
        if video_updates:
            enable_v = [p for p, v in video_updates.items() if v]
            disable_v = [p for p, v in video_updates.items() if not v]
            if disable_v:
                _, video_changed = set_cameras_video_in_use(self._kv_repo, disable_v, False)
            if enable_v:
                _, c = set_cameras_video_in_use(self._kv_repo, enable_v, True)
                video_changed = video_changed or c
        audio_changed = False
        if audio_updates:
            enable_a = [p for p, v in audio_updates.items() if v]
            disable_a = [p for p, v in audio_updates.items() if not v]
            if disable_a:
                _, audio_changed = set_cameras_audio_in_use(self._kv_repo, disable_a, False)
            if enable_a:
                _, c = set_cameras_audio_in_use(self._kv_repo, enable_a, True)
                audio_changed = audio_changed or c
        if voice_updates:
            enable_voice = [p for p, v in voice_updates.items() if v]
            disable_voice = [p for p, v in voice_updates.items() if not v]
            if disable_voice:
                set_cameras_voice_in_use(self._kv_repo, disable_voice, False)
            if enable_voice:
                set_cameras_voice_in_use(self._kv_repo, enable_voice, True)
        # 如果某摄像头两路感知都关了，自动停用通道（释放连接资源、预览消失）
        all_affected = set(video_updates) | set(audio_updates)
        if all_affected:
            video_denied = denied_video_camera_dids(self._kv_repo)
            audio_denied = denied_audio_camera_dids(self._kv_repo)
            both_off = [p for p in all_affected if p in video_denied and p in audio_denied]
            if both_off:
                both_updates = {p: {c: False for c in range(_cc(p))} for p in both_off}
                _, c = set_cameras_channels_in_use(
                    self._kv_repo, both_updates, {p: _cc(p) for p in both_off}
                )
                channels_changed = channels_changed or c
        changed = channels_changed or video_changed or audio_changed
        if changed:
            if channels_changed:
                # 先 refresh_cameras：按新 KV(黑名单)建/销 camera manager——两路都关的相机
                # 销毁 manager，停掉 native PPCS 会话+解码线程；仍有活跃路的保留。
                await self._miot_proxy.refresh_cameras()
            # _sync_camera_adapter：perception 按新集连/断订阅 + resync per-modality 订阅。
            await self._sync_camera_adapter()
        # 返回受影响的相机（按物理 did），结构与 list_cameras_with_state 一致。
        all_cameras = await self.list_cameras_with_state()
        affected_dids = set(updates) | set(video_updates) | set(audio_updates)
        affected = [cam for cam in all_cameras if cam["did"] in affected_dids]
        return affected

    async def toggle_camera_voice(self, items: list[dict]) -> list[dict]:
        """批量切换相机「拾音」状态（mic-off 语义）。每项 {"did": str, "voice_in_use": bool}。

        关闭 = 该相机声音完全不被处理：引擎入口剥离音频（不进 gate/omni、不转写、
        不上云、语音指令不 dispatch），dispatch/落库闸门作第二道防线。

        拾音开关从属于感知开关：只能在相机感知启用(in_use=True)时设置；相机感知已关闭
        (在黑名单)时整批拒绝。与 ``toggle_camera`` 不同,**不**调 refresh_cameras /
        _sync_camera_adapter / _restart_perception_engine——拾音黑名单在引擎入口与
        client.py dispatch 阶段实时读取(KVRepo.set 已同步更新进程内缓存),下一感知窗
        即生效,无需重建 manager 或重启。本地拉流不变(音频仍解码进缓冲,只是不被处理)。

        拾音同样按**整台相机**走（拾音白名单存物理 did），合成 did（``did:ch{n}``）先归一。
        """
        all_dids = [physical_camera_did(i["did"]) for i in items]

        cameras = await self._miot_proxy.get_cameras() or {}
        unknown = [d for d in all_dids if d not in cameras]
        if unknown:
            raise ValidationException(
                f"Unknown camera did(s) {unknown}; valid: {sorted(cameras.keys())}"
            )

        # 拾音从属于感知：感知**整台关闭**(所有通道都拉黑)的相机不允许设置拾音。前端会把这类
        # 相机的拾音开关置灰,这里再兜一道防脏请求。关相机不改写拾音白名单——存储偏好保留,
        # 相机重新启用后旧拾音设置自动生效(「自动关」是派生生效态,不落库)。拾音是相机级
        # (mic 只在球机/ch0)，故按物理 did 判「整台是否全关」。
        denied = denied_camera_dids(self._kv_repo)

        def _cc(pdid: str) -> int:
            return getattr(cameras.get(pdid), "channel_count", None) or 1

        disabled = [
            d
            for d in all_dids
            if len(denied_channels_of(denied, d, _cc(d))) >= _cc(d)
        ]
        if disabled:
            raise ValidationException(
                f"摄像头感知已关闭，无法设置声音（{disabled}）；请先开启该摄像头感知"
            )

        enable_dids = [
            physical_camera_did(i["did"]) for i in items if i["voice_in_use"]
        ]
        disable_dids = [
            physical_camera_did(i["did"]) for i in items if not i["voice_in_use"]
        ]
        if disable_dids:
            set_cameras_voice_in_use(self._kv_repo, disable_dids, False)
        if enable_dids:
            set_cameras_voice_in_use(self._kv_repo, enable_dids, True)
        # 返回受影响的相机，结构与 list_cameras_with_state 一致
        all_cameras = await self.list_cameras_with_state()
        affected = [cam for cam in all_cameras if cam["did"] in set(all_dids)]
        return affected

    def _resolve_prompt_target_dids(self, raw: str, cameras: dict) -> list[str]:
        """把一个 raw did（合成通道 did ``cam:chN`` / 裸物理 did）解析成**存储用的合成
        did 列表**——与 ``list_cameras_with_state`` / 感知注入侧的 key 口径一致。

        - 未知物理 did → ValidationException（防 typo）。
        - ``:chN`` 越界或格式非法 → ValidationException（后端唯一执法点，同 toggle_camera）。
        - 裸多通道 did → 展成全部通道（该台各路设同一条须知）；单摄 → 裸 did（cc=1）。
        """
        pdid = physical_camera_did(raw)
        if pdid not in cameras:
            raise ValidationException(
                f"Unknown camera did(s) ['{raw}']; valid: {sorted(cameras.keys())}"
            )
        cc = getattr(cameras.get(pdid), "channel_count", None) or 1
        if ":ch" in raw:
            _, ch_str = raw.rsplit(":ch", 1)
            try:
                ch = int(ch_str)
            except ValueError:
                raise ValidationException(f"非法通道号（格式错误）: {raw}")
            if not (0 <= ch < cc):
                raise ValidationException(f"非法通道号（越界）: {raw}")
            chans = [ch]
        else:
            chans = list(range(cc))
        return [synthetic_camera_did(pdid, c, cc) for c in chans]

    async def set_camera_prompt(self, items: list[dict]) -> list[dict]:
        """批量设置相机自定义「感知须知」prompt。每项 {"did": str, "prompt": str}。

        ``did`` 可为合成通道 did（``cam:chN``，双摄逐路）或裸物理 did（多通道 = 全通道设
        同一条，单摄 = 它自己）。``prompt`` strip 后须非空（空串在 schema 层已拒）。校验：
        未知 did、越界通道、超 ``MAX_CAMERA_PROMPT_LEN`` 全部通过才写。

        **不**从属于感知 / 拾音开关——关着的相机也可预配 prompt，只在被感知时逐窗注入生效。
        不 refresh / _sync / _restart——引擎每感知窗按合成 did 实时读 KV。
        """
        cameras = await self._miot_proxy.get_cameras() or {}

        too_long = [
            i["did"] for i in items if len((i.get("prompt") or "").strip()) > MAX_CAMERA_PROMPT_LEN
        ]
        if too_long:
            raise ValidationException(
                f"感知须知过长（超过 {MAX_CAMERA_PROMPT_LEN} 字）：{too_long}"
            )

        # 先全解析 + 校验（任一非法整批不写），再统一落库。
        resolved = [
            (self._resolve_prompt_target_dids(i["did"], cameras), i.get("prompt") or "")
            for i in items
        ]
        touched_physical = {physical_camera_did(i["did"]) for i in items}
        for syn_dids, prompt in resolved:
            for syn in syn_dids:
                set_camera_prompt(self._kv_repo, syn, prompt)
        all_cameras = await self.list_cameras_with_state()
        affected = [cam for cam in all_cameras if cam["did"] in touched_physical]
        return affected

    async def clear_camera_prompt(self, dids: list[str]) -> list[dict]:
        """批量清除相机自定义「感知须知」prompt。参数只传 did 列表（合成 / 裸物理均可）。

        校验未知 did / 越界通道；裸多通道 did 清全部通道。不 refresh / _sync / _restart。
        """
        cameras = await self._miot_proxy.get_cameras() or {}
        resolved = [self._resolve_prompt_target_dids(d, cameras) for d in dids]
        touched_physical = {physical_camera_did(d) for d in dids}
        for syn_dids in resolved:
            for syn in syn_dids:
                clear_camera_prompt(self._kv_repo, syn)
        all_cameras = await self.list_cameras_with_state()
        affected = [cam for cam in all_cameras if cam["did"] in touched_physical]
        return affected

    def _migrate_v1_blacklist_if_needed(self) -> None:
        from miloco.miot.filter import migrate_v1_blacklist
        migrate_v1_blacklist(self._kv_repo)

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
        """Hot-sync camera connections after a scope change (channel-level + per-modality)."""
        self._migrate_v1_blacklist_if_needed()
        adapter = self._camera_adapter()
        if adapter is None:
            return
        try:
            await adapter.sync_devices()
            await adapter.resync_subscriptions()
        except Exception as e:
            logger.warning("Camera adapter sync after scope change failed: %s", e)

    async def trigger_scene(self, scene_id: str) -> bool:
        """Trigger a MIoT manual scene."""
        scenes: dict = {}
        # 异常路径也要能看到"当时想触发什么"(失败审计完整性)——scene_name
        # 在校验通过后、执行前就归一好,成功/异常两路复用。
        scene_value_json: str | None = None
        try:
            scenes = (await self._miot_proxy.get_all_scenes()) or {}
            if scene_id not in scenes:
                raise ResourceNotFoundException(f"Scene '{scene_id}' not found")
            if not is_home_allowed(self._kv_repo, getattr(scenes[scene_id], "home_id", None)):
                raise ValidationException(
                    f"Scene '{scene_id}' is not in an allowed home"
                )
            # 场景无 did:用 scene_id 占 did/iid。scene_name 落 value_json 便于回看。
            # home_id 显式传场景所属家——did 是 scene_id,device cache 解析必 miss,
            # 不传的话场景台账恒 NULL、经查询侧 NULL 放行会串入他家合流页。
            scene_name = getattr(scenes[scene_id], "scene_name", None)
            scene_value_json = json.dumps(
                {"scene_name": scene_name}, ensure_ascii=False
            )
            ok = await self._miot_proxy.execute_miot_scene(scene_id)
            await _write_action_ledger(
                self._miot_proxy,
                action_type="scene_trigger",
                did=scene_id, iid=scene_id,
                value_json=scene_value_json,
                result_code=None,
                result_msg=None if ok else "场景触发失败",
                success=bool(ok), error=None,
                home_id=getattr(scenes[scene_id], "home_id", None),
            )
            return ok
        except (ResourceNotFoundException, ValidationException):
            raise
        except Exception as e:
            logger.error("Failed to trigger scene %s: %s", scene_id, e)
            await _write_action_ledger(
                self._miot_proxy,
                action_type="scene_trigger",
                did=scene_id, iid=scene_id,
                # 执行前已归一(校验没过就是 None——那种失败本来无参可记)
                value_json=scene_value_json,
                result_code=None, result_msg=None,
                success=False, error=str(e),
                # scenes 取列表阶段就炸时为空 dict → .get 兜底 None
                home_id=getattr(scenes.get(scene_id), "home_id", None),
            )
            raise MiotServiceException(f"Failed to trigger scene: {str(e)}") from e
