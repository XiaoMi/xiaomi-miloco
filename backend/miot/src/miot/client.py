# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT Client.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional

from miot.error import MIoTClientError
from miot.spec import MIoTSpecParser
from miot.storage import MIoTStorage

from .camera import (
    MIoTCamera,
    MIoTCameraInstance,
    get_camera_extra_info,
    is_camera_model,
)
from .cloud import MIoTHttpClient, MIoTOAuth2Client
from .const import (
    CLOUD_SERVER_DEFAULT,
    OAUTH2_CLIENT_ID,
    SYSTEM_LANGUAGE_DEFAULT,
)
from .i18n import MIoTI18n
from .lan import MIoTLan
from .mips_cloud import MIoTMipsCloud
from .network import MIoTNetwork
from .types import (
    MIoTAppNotify,
    MIoTCameraExtraInfo,
    MIoTCameraInfo,
    MIoTCameraStatus,
    MIoTDeviceBindEvent,
    MIoTDeviceInfo,
    MIoTDeviceStateEvent,
    MIoTHomeInfo,
    MIoTLanDeviceInfo,
    MIoTManualSceneInfo,
    MIoTOauthInfo,
    MIoTSceneChangedEvent,
    MIoTUserInfo,
    MipsConnectionError,
    MipsSubscribeRejectedError,
    MipsSubscribeTimeoutError,
)

_LOGGER = logging.getLogger(__name__)

# 同轮刷新内重复骤降只累加一次,见 get_devices_async 的骤降守卫注释
_DEVICE_REPLY_SHRINK_MIN_GAP_SEC = 30.0
# 本轮返回设备数低于缓存这么多倍才视为"骤降"(单个 home 解析失败即触发)
_DEVICE_REPLY_SHRINK_RATIO = 0.5
# 重放并发上限。与上层对账的 _RECONCILE_CONCURRENCY 限制的是同一件事
# (同时在飞的 SUBSCRIBE 条数,压力落在同一个 broker 连接上),调整时两处一起改。
_REPLAY_CONCURRENCY = 16


def _now() -> float:
    """monotonic 时钟的模块级封装,便于测试只 patch 这一处而不冻结事件循环。"""
    return time.monotonic()


async def _replay_subscriptions(
    keys: list[str],
    subscribe: Callable[[str], Awaitable[None]],
    label: str,
) -> int:
    """并发重放一批 tracked key(上限 _REPLAY_CONCURRENCY),best-effort,返回成功数。"""
    sem = asyncio.Semaphore(_REPLAY_CONCURRENCY)

    async def _one(key: str) -> bool:
        async with sem:
            try:
                await subscribe(key)
                return True
            except Exception as e:
                _LOGGER.warning(
                    "mips_cloud re-subscribe %s FAILED %s: %s", label, key, e
                )
                return False

    results = await asyncio.gather(*(_one(k) for k in keys))
    return sum(results)


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
    _callbacks_lan_device_status_changed: Dict[
        str, Callable[[str, MIoTLanDeviceInfo], Coroutine]
    ]
    _device_buffer: Optional[Dict[str, MIoTDeviceInfo]]

    # mips_cloud (MQTT push) state. None until OAuth completes; rebuilt after
    # a fresh `get_access_token_async`. Token rotation goes through
    # `update_access_token` (no reinit).
    _mips_cloud: Optional[MIoTMipsCloud]
    # Last user-level subscribe failure (e.g. ACL rejection). None means
    # subscribe is currently believed to be active. Surfaced via the
    # `mips_user_sub_error` property for the HTTP status endpoint.
    _mips_user_sub_error: Optional[str]
    _callback_user_bind: Optional[Callable[[MIoTDeviceBindEvent], Any]]
    # Device-level meta-change handler (rename / hr_change). Separate from
    # _callback_user_bind: a meta change must NOT flow through the bind welcome
    # path — the did is still present after refresh and would be misreported
    # as a new device.
    _callback_device_meta_changed: Optional[Callable[[MIoTDeviceBindEvent], Any]]
    # Device-level cloud state-change handler (online / offline). Separate
    # from _callback_device_meta_changed: a state push updates the cached
    # `online` field directly (event-driven recovery), not the meta refresh
    # path.
    _callback_device_state_changed: Optional[Callable[[MIoTDeviceStateEvent], Any]]
    # Dids whose `device/{did}/g_op/#` meta topic is (intended to be)
    # subscribed. This client owns the per-device meta subs: it re-issues them
    # on _setup_mips_async (re-OAuth / fresh setup); plain reconnects are
    # handled by mips_cloud's own _subs replay.
    _meta_sub_dids: set
    # Dids whose `device/{did}/state/#` cloud online/offline topic is
    # (intended to be) subscribed. Same ownership/replay model as
    # _meta_sub_dids.
    _state_sub_dids: set
    # Home ids whose `home/{home_id}/scene/#` topic is (intended to be)
    # subscribed. Same ownership model as _meta_sub_dids, but per home.
    _scene_sub_home_ids: set
    _callback_scene_changed: Optional[Callable[[MIoTSceneChangedEvent], Any]]

    _init_done: bool
    _lifecycle_lock: asyncio.Lock

    def __init__(
        self,
        uuid: str,
        redirect_uri: str,
        cache_path: Optional[str] = None,
        lang: Optional[str] = None,
        oauth_info: Optional[MIoTOauthInfo | Dict] = None,
        cloud_server: Optional[str] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
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
            self._oauth_info = (
                MIoTOauthInfo(**oauth_info)
                if isinstance(oauth_info, Dict)
                else oauth_info
            )
        else:
            self._oauth_info = None
        # get_event_loop() 自 3.10 起弃用；与 SDK 内 cloud/storage/lan/mdns/oauth2/spec/camera 等其他客户端保持一致。
        self._main_loop = loop or asyncio.get_running_loop()
        self._cloud_server = cloud_server or CLOUD_SERVER_DEFAULT
        self._lang = lang or SYSTEM_LANGUAGE_DEFAULT

        self._cameras_buffer = None
        self._last_lan_ping_ts = 0
        self._callbacks_lan_device_status_changed = {}
        self._device_buffer = None
        self._device_shrink_streak = 0
        self._last_device_shrink_ts = 0.0

        # mips_cloud state
        self._mips_cloud = None
        self._mips_user_sub_error = None
        self._callback_user_bind = None
        self._callback_device_meta_changed = None
        self._callback_device_state_changed = None
        self._meta_sub_dids = set()
        self._state_sub_dids = set()
        self._scene_sub_home_ids = set()
        self._callback_scene_changed = None
        self._callback_mips_connect: Optional[Callable[[], Any]] = None
        self._callback_subscription_reset: Optional[
            Callable[[set[str], set[str], set[str]], Any]
        ] = None

        # Pre-declare sub-client slots so deinit_async can safely walk them
        # even if init_async aborted before creating the full set.
        self._storage = None
        self._spec_parser = None
        self._i18n = None  # type: ignore
        self._oauth_client = None  # type: ignore
        self._http_client = None  # type: ignore
        self._network_client = None  # type: ignore
        self._lan_client = None  # type: ignore
        self._camera_client = None  # type: ignore

        self._init_done = False
        # Serializes init/deinit so a shielded rollback blocks the next init.
        self._lifecycle_lock = asyncio.Lock()

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
            raise MIoTClientError(
                "storage is not initialized, maybe cache_path is None"
            )
        return self._storage

    @property
    def spec_parser(self) -> MIoTSpecParser:
        """Spec parser."""
        if not self._spec_parser:
            raise MIoTClientError(
                "spec_parser is not initialized, maybe cache_path is None"
            )
        return self._spec_parser

    @property
    def cameras_info(self) -> Dict[str, MIoTCameraInfo]:
        """Cameras info."""
        return self._cameras_buffer or {}

    @property
    def camera_client(self) -> MIoTCamera:
        """Camera client."""
        return self._camera_client

    @property
    def http_client(self) -> MIoTHttpClient:
        """HTTP client."""
        return self._http_client

    async def __on_lan_device_status_changed(
        self, did: str, info: MIoTLanDeviceInfo, ctx: Any = None
    ) -> None:

        if self._cameras_buffer and did in self._cameras_buffer:
            self._cameras_buffer[did].lan_online = info.online
            self._cameras_buffer[did].local_ip = info.ip
        if did in self._callbacks_lan_device_status_changed:
            await self._callbacks_lan_device_status_changed[did](did, info)

    async def init_async(self) -> None:
        """Init the client."""
        async with self._lifecycle_lock:
            if self._init_done:
                _LOGGER.warning("client already init")
                return
            success = False
            try:
                self._i18n = MIoTI18n(lang=self._lang, loop=self._main_loop)
                if self._cache_path:
                    self._storage = MIoTStorage(self._cache_path, loop=self._main_loop)
                    # await self._storage.init_async()
                    self._spec_parser = MIoTSpecParser(
                        storage=self._storage, lang=self._lang, loop=self._main_loop
                    )
                    # 只构造、不在启动时联网 init：spec 标准库 / 设备类型目录需拉 miot-spec.org
                    # （数百请求）。解析器改为在首次真正解析设备 spec 时惰性自初始化（见
                    # MIoTSpecParser.parse_async），因此未登录 / 无设备时一个 spec 请求都不会发。
                await self._i18n.init_async()
                self._oauth_client = MIoTOAuth2Client(
                    redirect_uri=self._redirect_uri,
                    cloud_server=self._cloud_server,
                    uuid=self._uuid,
                    loop=self._main_loop,
                )
                self._http_client = MIoTHttpClient(
                    cloud_server=self._cloud_server,
                    access_token=self._oauth_info.access_token
                    if self._oauth_info
                    else "",
                    loop=self._main_loop,
                )
                self._network_client = MIoTNetwork(loop=self._main_loop)
                await self._network_client.init_async()
                self._lan_client = MIoTLan(
                    net_ifs=list((await self._network_client.get_info_async()).keys()),
                    network=self._network_client,
                    loop=self._main_loop,
                )
                await self._lan_client.init_async()
                await self._lan_client.register_status_changed_async(
                    key="miot_client", handler=self.__on_lan_device_status_changed
                )
                self._camera_client = MIoTCamera(
                    cloud_server=self._cloud_server,
                    access_token=self._oauth_info.access_token
                    if self._oauth_info
                    else "",
                    loop=self._main_loop,
                )
                await self._camera_client.init_async()

                # mips_cloud requires a valid access_token; skip cleanly if
                # we have not been through OAuth yet. The caller will retry
                # via setup_mips_async() once OAuth completes.
                if self._oauth_info and self._oauth_info.access_token:
                    await self._setup_mips_async()

                self._init_done = True
                success = True
            finally:
                if not success:
                    # FIXME: If cancel is delivered inside _deinit_locked, remaining
                    # sub-client deinit_async() calls are skipped: aiohttp
                    # sessions may leak fds, the LAN worker thread/loop + UDP
                    # socket may survive as an orphan, and background timers
                    # may keep firing. The outer finally still nulls all refs
                    # so the next init gets a fresh instance, at the cost of
                    # the leaked resources above.
                    self._init_done = True
                    try:
                        await self._deinit_locked()
                    except asyncio.CancelledError:
                        _LOGGER.warning(
                            "init rollback interrupted by cancellation; "
                            "sub-client refs cleared, some deinit_async() may be skipped"
                        )
                        raise
                    except Exception:
                        _LOGGER.exception("deinit during init rollback failed")
                    finally:
                        self._init_done = False

    async def deinit_async(self) -> None:
        """Deinit the client."""
        async with self._lifecycle_lock:
            await self._deinit_locked()

    async def _deinit_locked(self) -> None:
        """Deinit body; caller must hold _lifecycle_lock."""
        if not self._init_done:
            _LOGGER.info("client not init")
            return

        async def _safe(name: str, coro_factory) -> None:
            try:
                coro = coro_factory()
            except Exception as err:
                _LOGGER.warning("%s.deinit skipped, factory error: %s", name, err)
                return
            if coro is None:
                return
            try:
                await coro
            except Exception as err:
                _LOGGER.warning("%s.deinit failed, proceeding: %s", name, err)

        # Tear down in reverse dependency order, then drop sub-client refs so
        # that the next init_async() creates fresh sub-clients on the same
        # MIoTClient instance. Each step is isolated so a single failure
        # cannot strand the instance in a half-torn-down state. A sub-client
        # may be None if init_async aborted before creating it.
        try:
            # mips_cloud must be deinit-ed before oauth_client / http_client
            # so any in-flight subscribe futures see MipsConnectionError
            # rather than hanging on a closed http session.
            await _safe(
                "mips_cloud",
                lambda: self._mips_cloud.deinit_async() if self._mips_cloud else None,
            )
            await _safe(
                "oauth_client",
                lambda: (
                    self._oauth_client.deinit_async() if self._oauth_client else None
                ),
            )
            await _safe(
                "http_client",
                lambda: self._http_client.deinit_async() if self._http_client else None,
            )
            await _safe(
                "camera_client",
                lambda: (
                    self._camera_client.deinit_async() if self._camera_client else None
                ),
            )
            await _safe(
                "lan_unregister",
                lambda: (
                    self._lan_client.unregister_status_changed_async("miot_client")
                    if self._lan_client
                    else None
                ),
            )
            await _safe(
                "lan_client",
                lambda: self._lan_client.deinit_async() if self._lan_client else None,
            )
            await _safe(
                "network_client",
                lambda: (
                    self._network_client.deinit_async()
                    if self._network_client
                    else None
                ),
            )
            await _safe(
                "spec_parser",
                lambda: self._spec_parser.deinit_async() if self._spec_parser else None,
            )
            await _safe(
                "i18n",
                lambda: self._i18n.deinit_async() if self._i18n else None,
            )
        finally:
            # Always drop references and clear the init flag so a subsequent
            # init_async() is not blocked by the "already init" guard.
            self._oauth_client = None  # type: ignore
            self._http_client = None  # type: ignore
            self._camera_client = None  # type: ignore
            self._lan_client = None  # type: ignore
            self._network_client = None  # type: ignore
            self._spec_parser = None
            self._storage = None
            self._i18n = None  # type: ignore
            self._cameras_buffer = None
            self._device_buffer = None
            self._device_shrink_streak = 0
            self._last_device_shrink_ts = 0.0
            self._last_lan_ping_ts = 0
            self._callbacks_lan_device_status_changed = {}
            self._mips_cloud = None
            self._mips_user_sub_error = None
            self._callback_user_bind = None
            self._callback_device_meta_changed = None
            self._callback_device_state_changed = None
            self._meta_sub_dids = set()
            self._state_sub_dids = set()
            self._scene_sub_home_ids = set()
            self._callback_scene_changed = None
            self._callback_mips_connect = None
            self._callback_subscription_reset = None
            self._init_done = False

    async def gen_oauth_url_async(self, redirect_uri: Optional[str] = None) -> str:
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
        self._http_client.update_http_header(access_token=self._oauth_info.access_token)
        await self._camera_client.update_access_token_async(
            access_token=self._oauth_info.access_token
        )
        await self.get_user_info_async()
        # First-time OAuth: build the mips_cloud client now that we have a
        # token and the user uid (loaded by get_user_info_async above). A
        # later token refresh path goes through refresh_access_token_async →
        # update_access_token instead of a fresh setup.
        await self._setup_mips_async()
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
        await self._camera_client.update_access_token_async(
            access_token=self._oauth_info.access_token
        )
        if self._mips_cloud is not None:
            await self._mips_cloud.update_access_token(self._oauth_info.access_token)
        else:
            # mips_cloud not yet set up (e.g. fresh process restored oauth
            # from KV cache and is doing its first refresh). Set up now.
            await self._setup_mips_async()
        return self._oauth_info

    async def check_token_async(self) -> bool:
        """Get user information to check if the token is valid.

        Returns:
            bool: Check result.
        """
        try:
            await self._http_client.get_user_info_async()
        except Exception:
            return False
        return True

    async def get_homes_async(
        self, fetch_share_home: bool = False
    ) -> Dict[str, MIoTHomeInfo]:
        """Get homes info.

        Args:
            fetch_share_home (bool, optional): Whether fetch share home. Defaults to False.

        Returns:
            Dict[str, MIoTHomeInfo]: Home info list
        """
        return await self._http_client.get_homes_async(
            fetch_share_home=fetch_share_home
        )

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
        self,
        home_list: Optional[List[MIoTHomeInfo]] = None,
        fetch_share_home: bool = False,
    ) -> Dict[str, MIoTDeviceInfo]:
        """Get devices info.

        Args:
            home_list (Optional[List[MIoTHomeInfo]], optional): Home list. Defaults to None.
            fetch_share_home (bool, optional): Whether fetch share home. Defaults to False.

        Returns:
            Dict[str, MIoTDeviceInfo]: Devices info.
        """
        devices: Dict[str, MIoTDeviceInfo] = await self._http_client.get_devices_async(
            home_infos=home_list, fetch_share_home=fetch_share_home
        )
        # 一次瞬时"骤降"(整份返空只是极端情形——单个 home 解析失败返回的是部分列表,
        # cloud.py 对缺字段的 home 静默 continue)不该被当成"账户真的缩了":否则合并分支
        # 把没返回的 did pop 光,对账会整批退订再重订,推送全断。连续两轮骤降才认缩;
        # 同轮并发调用(refresh_devices + refresh_cameras)只算一次,窗口锚定本轮第一次。
        buffered = len(self._device_buffer) if self._device_buffer else 0
        shrank = buffered > 0 and len(devices) < buffered * _DEVICE_REPLY_SHRINK_RATIO
        if not shrank:
            self._device_shrink_streak = 0
            self._last_device_shrink_ts = 0.0
        else:
            now = _now()
            if now - self._last_device_shrink_ts >= _DEVICE_REPLY_SHRINK_MIN_GAP_SEC:
                self._device_shrink_streak += 1
                # 锚点只在开新一轮时推进,否则窗口被同 streak 内的返空续期、永不收敛
                self._last_device_shrink_ts = now
            if self._device_shrink_streak < 2:
                _LOGGER.warning(
                    "get_devices_async: cloud returned %d devices while %d are "
                    "buffered; keeping the buffer this round (streak=%d)",
                    len(devices),
                    buffered,
                    self._device_shrink_streak,
                )
                await self._merge_lan_status()
                return self._device_buffer
            # 认账:buffer 即将与云端对齐,计数器和锚点一并复位——否则紧跟着的
            # 下一次瞬时骤降会因 streak 仍是 2 而直接穿透守卫。
            _LOGGER.warning(
                "get_devices_async: shrink confirmed over %d rounds, "
                "accepting %d devices (was %d)",
                self._device_shrink_streak,
                len(devices),
                buffered,
            )
            self._device_shrink_streak = 0
            self._last_device_shrink_ts = 0.0
        # 判 None 而非真值:合并会把未返回的 did pop 光,空 buffer 若被换成新对象,
        # 上层 _device_info_dict 还指着旧对象、别名断开,对账会拿空字典算差集。
        if self._device_buffer is None:
            self._device_buffer = devices
        else:
            # Merge cloud updates into existing buffer.
            for did in list(self._device_buffer.keys()):
                if did not in devices:
                    self._device_buffer.pop(did, None)
                    continue
                device = devices.pop(did, None)
                self._device_buffer[did].__dict__.update(device.__dict__)

            for did in list(devices.keys()):
                self._device_buffer[did] = devices.pop(did)

        await self._merge_lan_status()
        return self._device_buffer

    async def _merge_lan_status(self) -> None:
        """把 LAN 发现合并进 buffered 设备(lan_online / local_ip)。

        正常路径与骤降守卫的提前返回共用——被守卫拦下的那轮,刚上电的相机仍应可被
        require_lan 发现,lan_online 不能停在上一轮旧值。
        """
        lan_devices = await self._lan_client.get_devices_async()
        for did in self._device_buffer:
            if did in lan_devices:
                self._device_buffer[did].lan_online = lan_devices[did].online
                self._device_buffer[did].local_ip = lan_devices[did].ip
            else:
                self._device_buffer[did].lan_online = False
                self._device_buffer[did].local_ip = None

    async def get_manual_scenes_async(
        self,
        home_list: Optional[List[MIoTHomeInfo]] = None,
        fetch_share_home: bool = False,
    ) -> Dict[str, MIoTManualSceneInfo]:
        """Get manual scenes info.

        Args:
            home_list (Optional[List[MIoTHomeInfo]], optional): Home list. Defaults to None.
            fetch_share_home (bool, optional): Whether fetch share home. Defaults to False.

        Returns:
            Dict[str, MIoTManualSceneInfo]: Manual scenes info.
        """
        return await self._http_client.get_manual_scenes_async(
            home_infos=home_list, fetch_share_home=fetch_share_home
        )

    async def run_manual_scene_async(self, scene_info: MIoTManualSceneInfo) -> bool:
        """Run manual scene.

        Args:
            scene_info(MIoTManualSceneInfo): Scene info, MUST include uid, scene_id, home_id.

        Returns:
            bool: Run manual scene result.
        """
        return await self._http_client.run_manual_scene_async(scene_info=scene_info)

    async def get_cameras_async(
        self,
        home_list: Optional[List[MIoTHomeInfo]] = None,
        fetch_share_home: bool = False,
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
        devices = await self.get_devices_async(
            home_list=home_list, fetch_share_home=fetch_share_home
        )
        for did, device_info in devices.items():
            if not await is_camera_model(device_info.model, camera_extra_info):
                continue
            cameras[did] = MIoTCameraInfo(
                **device_info.model_dump(),
                channel_count=(
                    camera_extra_info.extra_info[device_info.model].channel_count
                    if device_info.model in camera_extra_info.extra_info
                    else 1
                ),
                camera_status=MIoTCameraStatus.DISCONNECTED,
            )
        self._cameras_buffer = cameras
        for did, camera_info in self._cameras_buffer.items():
            # Camera connect status
            if did in self._camera_client.camera_map:
                camera_info.camera_status = (
                    await self._camera_client.get_camera_status_async(did)
                )
            else:
                camera_info.camera_status = MIoTCameraStatus.DISCONNECTED

        # Merge LAN status into camera buffer.
        lan_devices = await self._lan_client.get_devices_async()
        for did, camera_info in self._cameras_buffer.items():
            if did in lan_devices:
                camera_info.lan_online = lan_devices[did].online
                camera_info.local_ip = lan_devices[did].ip

        return self._cameras_buffer

    async def refresh_cameras_status_async(self) -> None:
        """Refresh cameras status with lan ping."""
        ts_now = int(time.time())
        if ts_now - self._last_lan_ping_ts < MIoTLan.OT_PROBE_INTERVAL_MIN:
            return
        await self._lan_client.ping_async()
        self._last_lan_ping_ts = ts_now

    async def create_camera_instance_async(
        self,
        camera_info: MIoTCameraInfo,
        frame_interval: int = 500,
        enable_hw_accel: bool = True,
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
            enable_hw_accel=enable_hw_accel,
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
        return await self._camera_client.register_status_changed_async(
            did=did, callback=callback
        )

    async def unregister_camera_status_changed_async(self, did: str) -> None:
        """Unregister camera status changed callback.

        Args:
            did (str): Device id.

        Returns:
            bool: Unregister result.
        """
        return await self._camera_client.unregister_status_changed_async(did=did)

    # ------------------------------------------------------------------ mips

    @property
    def mips_connected(self) -> bool:
        """Whether the cloud MQTT (mips) client is currently connected."""
        return self._mips_cloud is not None and self._mips_cloud.is_connected

    @property
    def mips_user_sub_error(self) -> Optional[str]:
        """Last user-level (`user/{uid}/g_op/...`) subscribe error.

        None means subscribe is currently believed to be active. A non-None
        value persists until the next successful ``_setup_mips_async`` cycle
        or a successful reconnect resubscribe (``_on_mips_subscribe_success``),
        so the HTTP status endpoint can report the broker's rejection reason.
        """
        return self._mips_user_sub_error

    async def _fire_subscription_reset(self) -> None:
        """Hand the upper layer the current tracked sets so it can rebuild
        its intent mirrors as a faithful copy of what mips actually has."""
        if not self._callback_subscription_reset:
            return
        try:
            ret = self._callback_subscription_reset(
                self._meta_sub_dids,
                self._state_sub_dids,
                self._scene_sub_home_ids,
            )
            if asyncio.iscoroutine(ret):
                await ret
        except Exception as e:
            _LOGGER.error("subscription reset callback failed: %s", e)

    async def _setup_mips_async(self) -> None:
        """Build (or rebuild) the mips_cloud client and attempt user-level subs.

        Idempotent: a prior client is torn down first.

        User-level subscribe failures DO NOT raise — they are captured in
        `_mips_user_sub_error` and logged at ERROR. The user-level topics
        (`user/{uid}/g_op/{bind,unbind}`) are the only ones we subscribe;
        ACL rejection on either should be visible but should not kill the
        rest of the integration.
        """
        if not self._oauth_info or not self._oauth_info.access_token:
            _LOGGER.warning("mips setup skipped: no access_token")
            return
        if not self._oauth_info.user_info or not self._oauth_info.user_info.uid:
            # Should not happen — get_user_info_async populates user_info.
            _LOGGER.warning("mips setup skipped: no user uid")
            return

        # Tear down any pre-existing instance (re-OAuth scenario).
        if self._mips_cloud is not None:
            try:
                await self._mips_cloud.deinit_async()
            except Exception as e:
                _LOGGER.warning("mips_cloud deinit during re-setup failed: %s", e)
            self._mips_cloud = None

        mips = MIoTMipsCloud(
            uuid=self._uuid,
            # broker prefix = HTTP cloud_server：海外区直接对应（sg/i2/ru/us/de），
            # 国内 prod 用 "cn"。dev 切 preview/staging 时改 settings 里的 cloud_server。
            cloud_server=self._cloud_server,
            app_id=OAUTH2_CLIENT_ID,
            token=self._oauth_info.access_token,
            loop=self._main_loop,
        )
        try:
            await mips.init_async()
        except MipsConnectionError as e:
            _LOGGER.error(
                "mips_cloud connect FAILED: %s. Real-time push disabled; "
                "device list will refresh only via existing HTTP triggers.",
                e,
            )
            self._mips_cloud = None
            self._mips_user_sub_error = f"connect failed: {e}"
            # 旧实例已在上面拆掉,broker 上一个订阅都不剩。若把三个 tracked 集合
            # 原样留着,上层对账会算出 to_add = ∅、SDK 侧 `did in set` 短路也会
            # 吞掉重试,推送要等到下一次 token 刷新重跑本方法才可能恢复(小时级)。
            # 清空并通知上层重建镜像,下一轮 refresh_* 就能重新下发全量订阅
            # ——此时 mips 仍不可用,sub_*_async 会抛 MipsConnectionError,
            # 对账不记录、继续重试,直到连接恢复为止。
            self._meta_sub_dids = set()
            self._state_sub_dids = set()
            self._scene_sub_home_ids = set()
            await self._fire_subscription_reset()
            return
        self._mips_cloud = mips
        # Listen for unattended-subscribe failures (currently: subscribes
        # re-issued by mips_cloud after a paho reconnect). Without this,
        # an ACL revocation mid-session would silently disable push while
        # /mips_status kept claiming user_bind_subscribed=true. First-time
        # rejections still raise via MipsSubscribeRejectedError below.
        mips.register_subscribe_error_handler(self._on_mips_subscribe_error)
        mips.register_subscribe_success_handler(self._on_mips_subscribe_success)
        mips.register_mips_state_handler(self._on_mips_connect)

        uid = self._oauth_info.user_info.uid
        try:
            await mips.sub_user_bind_async(uid, self._on_user_bind_msg)
            await mips.sub_user_unbind_async(uid, self._on_user_bind_msg)
            self._mips_user_sub_error = None
            _LOGGER.info(
                "mips_cloud user-level subscribe OK (uid=%s); "
                "real-time device-bind enabled",
                uid,
            )
        except MipsSubscribeRejectedError as e:
            self._mips_user_sub_error = (
                f"topic={e.topic} {e.reason_string}(0x{e.reason_code:02x})"
            )
            _LOGGER.error(
                "mips_cloud user-level subscribe REJECTED by broker: %s. "
                "Real-time device-bind detection disabled. "
                "NOT falling back to reconnect-refresh — check broker ACL.",
                self._mips_user_sub_error,
            )
        except MipsSubscribeTimeoutError as e:
            self._mips_user_sub_error = f"topic={e.topic} SUBACK timeout"
            _LOGGER.error(
                "mips_cloud user-level subscribe TIMEOUT: %s. "
                "Real-time device-bind detection disabled.",
                self._mips_user_sub_error,
            )
        except Exception as e:
            self._mips_user_sub_error = f"unexpected error: {e}"
            _LOGGER.exception(
                "mips_cloud user-level subscribe failed unexpectedly: %s", e
            )

        # Re-issue per-device meta (rename/hr_change) subscriptions tracked
        # from a previous mips instance (re-OAuth / fresh setup wipes
        # mips._subs). Plain reconnects don't reach here — mips replays _subs
        # itself. Per-device failures are logged but never abort the rest.
        if self._meta_sub_dids:
            dids = sorted(self._meta_sub_dids)
            self._meta_sub_dids = set()
            ok = await _replay_subscriptions(
                dids, self.sub_device_meta_async, "device-meta"
            )
            _LOGGER.info(
                "mips_cloud re-subscribed device-meta for %d/%d devices",
                ok,
                len(dids),
            )

        # Same replay for per-home scene subscriptions.
        if self._scene_sub_home_ids:
            home_ids = sorted(self._scene_sub_home_ids)
            self._scene_sub_home_ids = set()
            ok = await _replay_subscriptions(
                home_ids, self.sub_home_scene_async, "home-scene"
            )
            _LOGGER.info(
                "mips_cloud re-subscribed home-scene for %d/%d homes",
                ok,
                len(home_ids),
            )

        # Same replay for per-device cloud state (online/offline) subs.
        if self._state_sub_dids:
            dids = sorted(self._state_sub_dids)
            self._state_sub_dids = set()
            ok = await _replay_subscriptions(
                dids, self.sub_device_state_async, "device-state"
            )
            _LOGGER.info(
                "mips_cloud re-subscribed device-state for %d/%d devices",
                ok,
                len(dids),
            )

        # mips 实例被重建 → 上层的订阅意图镜像必须重建为 SDK 重放后的真相:
        # 若镜像里留着"重放失败的 did"(SDK 集合已无它),下次对账算不出重订;
        # 若镜像丢了"重放重订上的已解绑 did",下次对账算不出退订。这里在三个
        # 重放块**之后**把 post-replay 集合交给上层,让镜像成为它们的忠实副本。
        await self._fire_subscription_reset()

        # 新实例的首次 CONNACK 发生在 register_mips_state_handler 之前
        # (_dispatch_state_handlers 当时读到的 handler 列表还是空的), _on_mips_connect
        # 不会被触发。若上一轮连接失败已把 tracked 集合清空、这里重放为空,不主动踢一次
        # 对账的话 SDK 记账与上层镜像会停在空集,推送要等下一次外部 refresh_* 才恢复
        # (小时级)。显式补一次,与 paho 重连路径复用同一个入口。
        self._on_mips_connect(True)

    def register_user_bind_callback(
        self, callback: Optional[Callable[[MIoTDeviceBindEvent], Any]]
    ) -> None:
        """Register the single account-level bind/unbind handler.

        Pass None to clear. The handler fires for both `bind` and `unbind`
        topics; differentiate via `event.event`.
        """
        self._callback_user_bind = callback

    def _on_user_bind_msg(self, msg: MIoTDeviceBindEvent) -> None:
        cb = self._callback_user_bind
        if cb is None:
            return
        ret = cb(msg)
        if asyncio.iscoroutine(ret):
            asyncio.ensure_future(ret)

    def register_device_meta_changed_callback(
        self, callback: Optional[Callable[[MIoTDeviceBindEvent], Any]]
    ) -> None:
        """Register the single device-meta-change handler.

        Fires for every subscribed device's rename / hr_change events
        (event.event distinguishes them). Pass None to clear. The new
        name/room/home is fetched authoritatively via a device-list refresh,
        so the handler typically just triggers refresh_devices.
        """
        self._callback_device_meta_changed = callback

    def _on_device_meta_changed_msg(self, msg: MIoTDeviceBindEvent) -> None:
        cb = self._callback_device_meta_changed
        if cb is None:
            return
        ret = cb(msg)
        if asyncio.iscoroutine(ret):
            asyncio.ensure_future(ret)

    async def sub_device_meta_async(self, did: str) -> None:
        """订阅一台设备的 g_op meta topic(幂等)。

        失败传播、不记入 `_meta_sub_dids`,让上层 diff 下次重试;mips 不可用时
        抛 MipsConnectionError(记意图当成功会让上层以为已订阅、重连重放也补不上)。
        """
        if did in self._meta_sub_dids:
            return
        mips = self._mips_cloud
        if mips is None or not mips.is_connected:
            raise MipsConnectionError(
                f"mips_cloud unavailable; device-meta subscribe not issued did={did}"
            )
        await mips.sub_device_meta_changed_async(did, self._on_device_meta_changed_msg)
        self._meta_sub_dids.add(did)

    async def unsub_device_meta_async(self, did: str) -> None:
        """Unsubscribe one device's meta topic."""
        self._meta_sub_dids.discard(did)
        mips = self._mips_cloud
        if mips is None:
            return
        await mips.unsub_device_meta_changed_async(did)

    def register_device_state_changed_callback(
        self, callback: Optional[Callable[[MIoTDeviceStateEvent], Any]]
    ) -> None:
        """Register the single device cloud state-change handler.

        Fires for every subscribed device's online / offline events
        (event.event distinguishes them). Pass None to clear. The handler
        updates the cached cloud ``online`` field directly from the event —
        this is the event-driven recovery path for cameras that went stale
        across a backend restart (no device-list re-fetch).
        """
        self._callback_device_state_changed = callback

    def _on_device_state_msg(self, msg: MIoTDeviceStateEvent) -> None:
        cb = self._callback_device_state_changed
        if cb is None:
            return
        ret = cb(msg)
        if asyncio.iscoroutine(ret):
            asyncio.ensure_future(ret)

    async def sub_device_state_async(self, did: str) -> None:
        """订阅一台设备的 state topic(幂等);契约同 sub_device_meta_async。"""
        if did in self._state_sub_dids:
            return
        mips = self._mips_cloud
        if mips is None or not mips.is_connected:
            raise MipsConnectionError(
                f"mips_cloud unavailable; device-state subscribe not issued did={did}"
            )
        await mips.sub_device_state_async(did, self._on_device_state_msg)
        self._state_sub_dids.add(did)

    async def unsub_device_state_async(self, did: str) -> None:
        """Unsubscribe one device's cloud state topic."""
        self._state_sub_dids.discard(did)
        mips = self._mips_cloud
        if mips is None:
            return
        await mips.unsub_device_state_async(did)

    def register_scene_changed_callback(
        self, callback: Optional[Callable[[MIoTSceneChangedEvent], Any]]
    ) -> None:
        """Register the single scene-change handler.

        Fires for every subscribed home's rename / delete / edit scene events.
        Pass None to clear. The new scene list is fetched authoritatively via
        a refresh, so the handler typically just triggers refresh_scenes.
        """
        self._callback_scene_changed = callback

    def _on_scene_changed_msg(self, msg: MIoTSceneChangedEvent) -> None:
        cb = self._callback_scene_changed
        if cb is None:
            return
        ret = cb(msg)
        if asyncio.iscoroutine(ret):
            asyncio.ensure_future(ret)

    async def sub_home_scene_async(self, home_id: str) -> None:
        """订阅一个家庭的 scene topic(幂等);契约同 sub_device_meta_async。"""
        if home_id in self._scene_sub_home_ids:
            return
        mips = self._mips_cloud
        if mips is None or not mips.is_connected:
            raise MipsConnectionError(
                f"mips_cloud unavailable; home-scene subscribe not issued "
                f"home={home_id}"
            )
        await mips.sub_home_scene_changed_async(home_id, self._on_scene_changed_msg)
        self._scene_sub_home_ids.add(home_id)

    async def unsub_home_scene_async(self, home_id: str) -> None:
        """Unsubscribe one home's scene topic."""
        self._scene_sub_home_ids.discard(home_id)
        mips = self._mips_cloud
        if mips is None:
            return
        await mips.unsub_home_scene_changed_async(home_id)

    def register_mips_connect_callback(
        self, callback: Optional[Callable[[], Any]]
    ) -> None:
        """Register a callback fired on every MQTT (re)connect.

        The intended consumer is MiotProxy.refresh_devices — after a
        reconnect the device list may have drifted during the disconnect
        window, so an unconditional refresh is the safe default.
        """
        self._callback_mips_connect = callback

    def register_subscription_reset_callback(
        self, callback: Optional[Callable[[set[str], set[str], set[str]], Any]]
    ) -> None:
        """mips 重放后回调上层,把三个重放后的 tracked 集合交回(meta/state/scene)。"""
        self._callback_subscription_reset = callback

    def _on_mips_connect(self, connected: bool) -> None:
        if not connected:
            return
        cb = self._callback_mips_connect
        if cb is None:
            return
        ret = cb()
        if asyncio.iscoroutine(ret):
            asyncio.ensure_future(ret)

    def _on_mips_subscribe_error(self, info: tuple[str, int, str]) -> None:
        """Update mips_user_sub_error when an unattended subscribe fails.

        Currently the only unattended subscribes happen after a paho
        reconnect; the "(after reconnect)" suffix in the error string makes
        the trigger context obvious. Acts ONLY on the user-level bind topics
        (`user/{uid}/g_op/{bind,unbind}`), which is what mips_user_sub_error /
        /mips_status track. Device-meta (`device/{did}/g_op/...`) and home-scene
        (`home/{home_id}/scene/...`) re-subscribe results must NOT touch this
        flag — they have their own per-entity retry path and a device-meta
        SUBACK has no bearing on whether bind detection is healthy.
        """
        topic, code, reason = info
        if not topic.startswith("user/"):
            return
        if code >= 0:
            self._mips_user_sub_error = (
                f"topic={topic} {reason}(0x{code:02x}) (after reconnect)"
            )
        else:
            self._mips_user_sub_error = f"topic={topic} {reason} (after reconnect)"
        _LOGGER.error(
            "mips_cloud subscribe (after reconnect) REJECTED: %s. "
            "Real-time device-bind detection disabled until next setup.",
            self._mips_user_sub_error,
        )

    def _on_mips_subscribe_success(self, topic: str) -> None:
        """Clear mips_user_sub_error when an unattended subscribe succeeds.

        Symmetric with _on_mips_subscribe_error. After a paho reconnect,
        if the re-issued subscribe is accepted by the broker (e.g. ACL was
        restored), the stale error is cleared so /mips_status accurately
        reports user_bind_subscribed=true. Acts ONLY on the user-level bind
        topics — a device-meta / home-scene re-subscribe success must not clear
        a genuine user-bind error (it would mask a real bind-detection outage).
        """
        if not topic.startswith("user/"):
            return
        if self._mips_user_sub_error is not None:
            _LOGGER.info(
                "mips_cloud subscribe (after reconnect) OK topic=%s; "
                "clearing previous error: %s",
                topic,
                self._mips_user_sub_error,
            )
            self._mips_user_sub_error = None

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
