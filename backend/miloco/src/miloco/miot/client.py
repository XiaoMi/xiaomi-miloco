# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""MIoT proxy module for handling Xiaomi IoT device related operations."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

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
    MIoTDeviceStateEvent,
    MIoTGetPropertyParam,
    MIoTLanDeviceInfo,
    MIoTManualSceneInfo,
    MIoTOauthInfo,
    MIoTSceneChangedEvent,
    MIoTSetPropertyParam,
    MIoTUserInfo,
    MipsConnectionError,
)
from pydantic_core import to_jsonable_python

from miloco.config import get_settings
from miloco.database.kv_repo import AuthConfigKeys, DeviceInfoKeys, KVRepo
from miloco.miot.camera_handler import CameraVisionHandler
from miloco.miot.filter import (
    allowed_home_ids,
    filter_by_home,
    is_home_allowed,
    physical_camera_did,
    select_active_camera_dids,
)
from miloco.miot.mips_listeners import (
    BindEventListener,
    CameraStateEventListener,
    DeviceMetaEventListener,
    SceneEventListener,
)
from miloco.miot.schema import CameraImgSeq, normalize_sub_devices
from miloco.miot.state_push import write_online
from miloco.miot.welcome_service import DeviceWelcomeService

logger = logging.getLogger(__name__)

# 容器里这一笔的来源标记。与对齐、推送分开记，dump 里一眼能看出是谁删的
STATE_RECONCILE_SOURCE = "iot_reconcile"


# 三类对账(meta / device-state / scene)共享的总在飞订阅并发上限——压力落在
# 同一条 broker 连接上,分闸会让上限变成 3×;SDK 重放侧 _REPLAY_CONCURRENCY
# 与它限制的是同一件事,调值时两处一起改。
_RECONCILE_CONCURRENCY = 16


def _is_subscribable_did(did: str) -> bool:
    """did 能否用于拼 MQTT topic:带 '/' 会打断 topic 路径与解码正则。"""
    return "/" not in did


async def _reconcile_subscriptions(
    target_fn: Callable[[], set[str]],
    subscribed: set[str],
    sub: Callable[[str], Awaitable[None]],
    unsub: Callable[[str], Awaitable[None]],
    *,
    lock: asyncio.Lock,
    semaphore: asyncio.Semaphore,
    generation: Callable[[], int],
    label: str,
    key_name: str = "did",
) -> None:
    """把某类订阅集合对账到 ``target_fn`` 算出的目标集。

    订 ``target - subscribed``、退 ``subscribed - target``(并发上限
    ``_RECONCILE_CONCURRENCY``);单键失败只记日志不中断。原地更新
    ``subscribed``。

    锁只保护意图集的读改写,不跨网络 I/O——整批 SUBACK 最坏要等
    数十秒到分钟级,跨 I/O 持锁会把 unbind 抹记账和 mips 重建镜像挡在锁外:
    - 差集在锁内计算并记下 ``generation``(纯内存,微秒级),锁随即释放;
    - 网络 sub/unsub 在锁外并发执行,但三类对账共用一个 ``semaphore``
      (``_RECONCILE_CONCURRENCY``),对同一条 broker 连接的总在飞 SUBSCRIBE
      数封顶,不因三类并发而翻倍;
    - 提交时回锁,仅当 ``generation`` 未变才把结果写回——期间若有 unbind 抹记账
      或重建镜像改写过意图集,放弃本轮提交。这不丢一致性:被放弃轮的
      ``to_add`` 仍在当前目标集里、``to_remove`` 仍在目标集补集里,改写方都
      保证会触发下一轮对账,按新目标集重算后把网络侧与镜像拉回一致。

    其余不变式:
    - ``target_fn`` 在锁内求值,避免等锁期间快照过期把新绑设备当"该退订";
    - 代次只由**作废他人在途提交**的改写方递增:unbind 抹记账、mips 重建后
      按 SDK 真相重建镜像。本函数的提交**不**递增——三类对账并发跑,
      任一类提交时递增会把另外两类在途的提交连带作废,一次刷新要多轮
      才收敛;提交只做"代次没变才写回"的检查,不制造新代次;
    - 生命周期边界的 _reset_subscription_mirrors 刻意不持锁,见该函数
      docstring 的收敛论证;
    - 意图集必须**原地**改集合对象(本函数在调用点捕获集合对象、之后才
      等锁,换新对象会让排队对账写进孤儿集);
    - 持锁期间不要调用任何 refresh_* 方法(否则可能成环)。
    """
    async with lock:
        target = target_fn()
        to_add = target - subscribed
        to_remove = subscribed - target
        if not to_add and not to_remove:
            return
        gen = generation()

    async def _sub(key: str) -> str | None:
        async with semaphore:
            try:
                await sub(key)
                return key
            except MipsConnectionError as e:
                # broker 不可达是整批共性问题,不按 did 刷 error。
                logger.debug("subscribe %s deferred %s=%s: %s", label, key_name, key, e)
                return None
            except Exception as e:
                logger.error("subscribe %s failed %s=%s: %s", label, key_name, key, e)
                return None

    async def _unsub(key: str) -> str | None:
        async with semaphore:
            try:
                await unsub(key)
            except Exception as e:
                logger.error("unsubscribe %s failed %s=%s: %s", label, key_name, key, e)
            return key

    added = await asyncio.gather(*(_sub(k) for k in to_add))
    removed = await asyncio.gather(*(_unsub(k) for k in to_remove))

    async with lock:
        if generation() != gen:
            # 意图集在飞行期间被 unbind 抹记账 / 重建镜像改写过,网络操作已按旧
            # 差集执行;写回会覆盖新真相,放弃本轮,由下一轮对账收敛。
            logger.info(
                "%s subscriptions commit skipped (intent changed mid-flight)",
                label,
            )
            return
        subscribed |= {k for k in added if k}
        subscribed -= {k for k in removed if k}
        added_ok = [k for k in added if k]
        removed_ok = [k for k in removed if k]
        failed = len(to_add) - len(added_ok)
        log = logger.warning if failed else logger.info
        log(
            "%s subscriptions synced: +%d -%d (total=%d, failed=%d)",
            label,
            len(added_ok),
            len(removed_ok),
            len(subscribed),
            failed,
        )


def _resolve_camera_switch_iids(spec: dict) -> list[tuple[int, int]]:
    """从设备 spec 里定位所有相机镜头开关属性（camera-control 服务的 on 属性）的 (siid, piid)。

    按 spec 类型名匹配（``service_type_name == "camera-control"`` 且 ``type_name == "on"``，
    均为语言无关的 URN 类型名），不硬编码 siid/piid，也**不看本地化的 service_description**；
    指示灯的 on 属于 ``indicator-light`` 服务，天然排除。双摄命中多个（主控 + 每镜头各一）。
    镜头→通道的归属由调用方按 **siid 序数**决定（见 ``read_cameras_awake``），不靠中文标签。
    找不到返回空列表。
    """
    iids: list[tuple[int, int]] = []
    for iid, entry in spec.items():
        if not iid.startswith("prop."):
            continue
        if (
            entry.get("service_type_name") == "camera-control"
            and entry.get("type_name") == "on"
        ):
            parts = iid.split(".")
            if len(parts) == 3:
                try:
                    iids.append((int(parts[1]), int(parts[2])))
                except ValueError:
                    continue
    return iids


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
        # 后台顺带对账任务(见 _spawn_subscription_sync),deinit 时取消
        self._background_syncs: set[asyncio.Task] = set()
        # coalescing 状态:循环在飞期间的再次触发折叠成"补一轮"而不是新建任务。
        # _sub_sync_running 在循环协程的 finally 里复位(与协程返回同步),不能用
        # _background_syncs 非空代替——done_callback 是 call_soon 排队的,循环已
        # 返回但 discard 未执行时,集合仍非空,会把新触发误折叠成没人读的 rerun。
        self._sub_sync_rerun_requested = False
        self._sub_sync_running = False
        # Serialize refresh_devices: multiple entries (MQTT reconnect,
        # bind-debounce, device refresh, lazy load) can fire concurrently
        # and would otherwise race on _device_info_dict / KV / diff log.
        self._refresh_devices_lock = asyncio.Lock()
        # 登录 / switch_home / unbind 可并发触发 refresh_cameras,加锁防
        # _camera_img_managers / SDK callback 状态竞争。
        self._refresh_cameras_lock = asyncio.Lock()
        # 订阅意图集的读改写锁（对账 / unbind 抹记账 / 重建镜像共用）
        self._sub_intent_lock = asyncio.Lock()
        # 意图集代次:只有作废他人在途提交的改写方(unbind 抹记账 / mips 重建镜像)
        # 在锁内递增;对账提交不递增、只校验代次,防止把飞行期间的改写覆盖掉
        # (见 _reconcile_subscriptions)。
        self._sub_intent_generation = 0
        # 三类对账共用一个并发闸:同时刻对同一条 broker 连接的在飞 SUBSCRIBE
        # 总数不超过 _RECONCILE_CONCURRENCY(每类自建会把上限放大成 3×)
        self._sub_semaphore = asyncio.Semaphore(_RECONCILE_CONCURRENCY)
        # 相机清单是否至少成功拉取过一次（区分"真没相机"与"还没加载"）
        self._cameras_loaded = False

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

        # did → {channel: 「镜头开关」态} 缓存（camera-control:on 最近一次云读结果，per-lens）。
        # 每路 True=镜头开启 / False=镜头关闭(隐私·物理遮挡) / None=未知(读失败)；channel 缺失
        # 亦视为未知。单摄只有 {0: …}；双摄 {0: 球机, 1: 枪机}。由 refresh_camera_online_status
        # 的云读路径填充；select_active / 列表只读它做门，不各自打云。属性无变更推送、只能按需
        # 云读，故新鲜度到「上次 refresh」为止。
        self._camera_awake_cache: dict[str, dict[int, bool | None]] = {}

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
        self._scene_listener = SceneEventListener(refresh_scenes=self.refresh_scenes)
        # Home ids whose home/{home_id}/scene/{rename,delete,edit} topics this
        # proxy intends to subscribe. Mirrors _subscribed_meta_dids but per home.
        self._subscribed_scene_home_ids: set[str] = set()

        # 上下线事件的 60s 对账防抖:事件批量落定后重拉相机状态。
        # 非相机事件不重新武装——见 _on_device_state_changed_event。
        self._camera_state_listener = CameraStateEventListener(
            refresh_camera_online_status=self.refresh_camera_online_status
        )
        # Dids whose device/{did}/state/{online,offline} topics this proxy
        # intends to subscribe. Mirrors _subscribed_meta_dids but for cloud
        # online/offline state; drives the diff in
        # _sync_device_state_subscriptions. ACCOUNT-WIDE (every device,
        # cameras included) so `device list` and `scope camera list` both
        # reflect the push.
        self._subscribed_device_state_dids: set[str] = set()

        # 上下线 / 属性推送的本层消费方。SDK 那两个回调是单槽、已被本类占着，
        # 别的消费方（状态容器、属性历史）挂在这里。存在 proxy 上而不是 client 上，
        # re-OAuth 重建 client 后不用重新注册
        self._state_listeners: list[Callable[[Any], Awaitable[None]]] = []
        self._props_listeners: list[Callable[[Any], Awaitable[None]]] = []

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

    def _reset_subscription_mirrors(self) -> None:
        """清空三个订阅意图镜像。

        必须原地清空(见 _reconcile_subscriptions 的"原地改写"要求)。
        不持锁——init/deinit 是生命周期边界,SDK 侧集合一并重置;此刻若有残留
        对账把 did 写回这个(同一个)集合,下一轮对账会因 SDK 侧已空而算出退订、
        自行收敛,不需要靠锁排除。
        """
        for mirror in (
            self._subscribed_meta_dids,
            self._subscribed_device_state_dids,
            self._subscribed_scene_home_ids,
        ):
            mirror.clear()

    def _spawn_subscription_sync(self) -> None:
        """把顺带对账派成后台任务,移出响应路径。

        锁已不跨网络 I/O(_reconcile_subscriptions 只拿微秒级临界区算差集/提交),
        三类对账可以并发跑,互不阻塞(总在飞 SUBSCRIBE 由共享 semaphore 封顶);
        与 refresh_devices 尾部口径一致,晚几秒完成没有语义影响。

        coalescing:同一窗口内 refresh_devices / refresh_cameras /
        refresh_camera_online_status 常各触发一次,若每次都新建任务,轮次会在同一
        镜像快照上重叠、重复发包。这里只保留一个循环任务,在飞期间的再次触发只置
        ``_sub_sync_rerun_requested``,由循环补一轮吸收——轮次串行后差集天然为空,
        重复 SUBSCRIBE 从源头消失。
        """

        if self._sub_sync_running:
            self._sub_sync_rerun_requested = True
            return
        self._sub_sync_running = True
        self._sub_sync_rerun_requested = False
        task = asyncio.create_task(self._subscription_sync_loop())
        self._background_syncs.add(task)
        task.add_done_callback(self._background_syncs.discard)

    async def _subscription_sync_loop(self) -> None:
        """对账循环:串行跑轮次,期间有新触发或代次被改写则补一轮再停。"""
        labels = ("meta", "device-state", "scene")
        try:
            while True:
                self._sub_sync_rerun_requested = False
                gen_before = self._sub_intent_generation
                results = await asyncio.gather(
                    self._sync_meta_subscriptions(),
                    self._sync_device_state_subscriptions(),
                    self._sync_scene_subscriptions(),
                    return_exceptions=True,
                )
                for label, r in zip(labels, results):
                    if isinstance(r, BaseException):
                        logger.error("%s subscription sync failed: %s", label, r)
                # 运行期间有新的对账请求(可能算差集之后设备清单又变了),或代次被
                # unbind/reset 改过(可能有提交被代次守卫放弃)——补一轮保证收敛,
                # 不必等外部刷新(如 bind/unbind 的 5s 防抖)才恢复。
                if (
                    not self._sub_sync_rerun_requested
                    and self._sub_intent_generation == gen_before
                ):
                    break
        finally:
            # 与协程返回同步,不留 add_done_callback 那种 call_soon 窗口。
            self._sub_sync_running = False

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
        self._scene_listener = SceneEventListener(refresh_scenes=self.refresh_scenes)
        self._camera_state_listener = CameraStateEventListener(
            refresh_camera_online_status=self.refresh_camera_online_status
        )
        self._reset_subscription_mirrors()
        self._miot_client.register_user_bind_callback(self._on_user_bind_event)
        # Device meta change (rename/hr_change): refresh the list so the new
        # name/room/home propagates. Kept off the bind welcome path.
        self._miot_client.register_device_meta_changed_callback(
            self._on_device_meta_changed_event
        )
        # Device cloud online/offline state: update the cached `online` field
        # directly (event-driven recovery for cameras that went stale across a
        # backend restart), plus a trailing reconciliation.
        self._miot_client.register_device_state_changed_callback(
            self._on_device_state_changed_event
        )
        # Home scene change (rename/delete/edit): refresh the scene list.
        self._miot_client.register_scene_changed_callback(self._on_scene_changed_event)
        # mips 实例重建后重建订阅意图镜像——见 _on_subscription_reset
        self._miot_client.register_subscription_reset_callback(
            self._on_subscription_reset
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

        # 1a. Cancel any in-flight background subscription syncs.
        for task in list(self._background_syncs):
            task.cancel()
        self._background_syncs.clear()
        self._sub_sync_rerun_requested = False
        # 取消会经 finally 复位,但取消是异步投递的,显式复位防 deinit 之后、
        # 取消生效之前又有刷新入口误以为循环在飞、把触发折叠掉。
        self._sub_sync_running = False

        # 1b. Cancel any pending bind/rename-event debounce timers — otherwise
        # they might fire during teardown and try to call refresh_devices on a
        # half-destroyed proxy.
        self._bind_listener.deinit()
        self._meta_listener.deinit()
        self._scene_listener.deinit()
        self._camera_state_listener.deinit()

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
        self._reset_subscription_mirrors()
        self._cameras_loaded = False
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
            logger.warning("MiOT info refresh completed with errors: %s", result)
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
        self,
        camera_info: MIoTCameraInfo,
    ) -> CameraVisionHandler | None:
        # 纯建原语:不含 scope gate(黑名单/home 白名单的判断在调用方 refresh_cameras)。
        # start_async 起 native PPCS 会话+解码;只对通过 refresh gate 的相机调用。
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

    async def devices_in_current_home(self) -> dict[str, MIoTDeviceInfo]:
        """只回当前启用家庭的设备。`get_devices` 是账号全量，过滤在各调用方自己做。"""
        return filter_by_home(self._kv_repo, await self.get_devices())

    def has_enabled_home(self) -> bool:
        """启用集非空。

        用来把「作用域是空的」和「作用域里没有设备」分开 —— 两者的
        `devices_in_current_home()` 都是空 dict，但前者说明还没选家庭。
        """
        return bool(allowed_home_ids(self._kv_repo))

    async def _on_lan_device_changed(self, did: str, info: MIoTLanDeviceInfo) -> None:
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
                # 相机列表已成功加载(放字典发布处,而非整段副作用全绿之后)
                self._cameras_loaded = True
                # 启动/新设备补读 awake：对当前家庭、awake 缓存里还没有的相机读一次并回填。
                # 重启后 refresh_cameras 在感知首轮 sync 就会跑 → 镜头态从启动即热，不依赖
                # 开面板/上下线推送；select_active 的镜头门随即可用。只补"缺失的"→ 启动读
                # 一遍、之后命中缓存不重复；后续新鲜度由 refresh_camera_online_status / 开启
                # 校验 / 相机上下线推送刷新（属性变更订阅待后续补）。
                try:
                    missing = [
                        did
                        for did, info in cameras.items()
                        if did not in self._camera_awake_cache
                        and is_home_allowed(
                            self._kv_repo, getattr(info, "home_id", None)
                        )
                    ]
                    if missing:
                        await self.read_cameras_awake(missing)
                except Exception as e:
                    logger.warning("refresh_cameras awake gap-fill failed: %s", e)
                # manager(native PPCS 会话+解码线程)的建/销与感知投喂**共用同一口径**
                # (select_active_camera_dids)：在启用家庭 + 未拉黑 + 在线 + 镜头未关、按 did
                # 截到上限。拉流集 = 投喂集，单一来源不漂移；关掉/移出家庭/离线/镜头关/超额的
                # 相机都不在 active 里 → 不建/已建则销，真正停掉 native 会话与解码。
                # select_active 返回**合成 did**（每路一台）；manager / native 会话是
                # **每物理相机一条**，故按物理 did 收敛做 ref-count：任一路在 active →
                # 该物理相机会话该在；两路都不在 → 拆。
                active_channels = set(
                    select_active_camera_dids(
                        self._kv_repo, cameras, awake_map=self._camera_awake_cache
                    )
                )
                active = {physical_camera_did(d) for d in active_channels}
                logger.debug(
                    "Camera streaming set: channels=%s physical=%s managers=%s",
                    sorted(active_channels),
                    sorted(active),
                    sorted(self._camera_img_managers),
                )
                for camera_did in cameras.keys():
                    if camera_did not in self._camera_img_managers:
                        if camera_did not in active:
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
                            # 起停相机 native 会话直接影响相机有限的并发流名额，
                            # 用 WARNING 便于运维一眼追踪拉流生命周期。
                            logger.warning(
                                "Camera native stream started: %s", camera_did
                            )
                    else:
                        await self._camera_img_managers[camera_did].update_camera_info(
                            cameras[camera_did]
                        )

                for camera_did in list(self._camera_img_managers.keys()):
                    # 不在 active 集（账号消失 / 移出家庭 / 被关 / 离线 / 超额）→ 销毁，
                    # 真正 miot_camera_stop + decoder.stop()，停掉 native 会话与解码。
                    if camera_did in active:
                        logger.debug(
                            "Manager %s kept alive (in_use & in scope)", camera_did
                        )
                        continue
                    # 每台单独兜异常：批量销（切家庭销旧家庭 N 台 / 超额收敛 N-M 台）时，
                    # 任一台 unregister/destroy 抛错不能拖垮其余——否则剩余 manager 留在
                    # dict 里继续白拉流，要等下次 refresh 才重试。失败的留在 dict 待重试。
                    try:
                        await self._miot_client.unregister_lan_device_changed_async(
                            did=camera_did
                        )
                        await self._camera_img_managers[camera_did].destroy()
                        del self._camera_img_managers[camera_did]
                        logger.warning("Camera native stream stopped: %s", camera_did)
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            "Failed to destroy camera manager %s, "
                            "leaving in dict for retry: %s",
                            camera_did,
                            e,
                        )
                # 独立走 refresh_cameras 也顺带对账订阅,派后台任务不挡相机路径
                self._spawn_subscription_sync()
                return cameras

            except Exception as e:
                logger.error("Failed to refresh cameras: %s", e)
                return None

    async def refresh_camera_online_status(self) -> dict[str, MIoTCameraInfo] | None:
        """轻量刷新:重拉 SDK 相机列表、更新 ``_camera_info_dict``(online / lan_online
        等元数据),**不调 update_camera_info、不动解码注册 / 帧队列 / manager**——故
        完全不扰动 watch 视频流。

        (别名副作用:``get_cameras_async`` 内部先 ``get_devices_async`` 重拉并原地合并
        SDK 设备 buffer,而 ``_device_info_dict`` 与 SDK buffer 是同一对象,所以本方法
        会**顺带**刷新 ``_device_info_dict`` 里全量设备的 online,不单是相机。)

        用途:``list_cameras_with_state`` 只读 ``_camera_info_dict`` 缓存,相机重新上线后
        该缓存不会自愈(云端 online 只有重拉 SDK 才更新),前端「此刻」页加载前调这个即可
        让在线状态真实,而不必走会过 update_camera_info(在共用 SDK 实例上重注册/注销
        解码 → 瞬时卡流)的重量级 refresh_cameras。

        与 refresh_cameras 共用 ``_refresh_cameras_lock``,防并发改 ``_camera_info_dict``。
        """
        from miloco.manager import get_manager

        # 与 refresh_devices 同口径：代号在打云端之前记
        scope = get_manager().current_scope()
        async with self._refresh_cameras_lock:
            try:
                cameras = await self._miot_client.get_cameras_async()
                self._camera_info_dict = copy.deepcopy(cameras)
                self._cameras_loaded = True
            except Exception as e:
                logger.error("Failed to refresh camera online status: %s", e)
                return None
        # 锁外顺带刷新 awake(镜头开关)缓存：select_active / list_cameras_with_state 只读缓存
        # 做门、不各自打云；awake 的云读收在这条"前端列表前必调、且本就在读云"的路径里。
        # 只读**当前启用家庭**的相机——awake 缓存的消费方(list filter_by_home / select_active
        # 镜头门)都只作用于当前家庭，对非当前家庭相机预读纯属浪费、还挤米家限频。
        try:
            dids = [
                did
                for did, info in self._camera_info_dict.items()
                if is_home_allowed(self._kv_repo, getattr(info, "home_id", None))
            ]
            if dids:
                await self.read_cameras_awake(dids)
        except Exception as e:
            logger.warning("refresh awake cache failed: %s", e)
        # 本方法经 get_cameras_async → get_devices_async 顺带增删了设备缓存(别名),
        # 与 refresh_cameras 尾部同理补对账,同样派后台任务不挡响应路径。
        # 那次别名副作用也刷新了全量设备的 online,容器要跟着走。
        try:
            self._sync_iot_online_flags(self._device_info_dict, scope)
        except Exception as e:
            logger.warning("同步在线标志到容器失败: %s", e)
        self._spawn_subscription_sync()
        return self._camera_info_dict

    async def refresh_devices(self) -> dict[str, MIoTDeviceInfo] | None:
        from miloco.manager import get_manager

        async with self._refresh_devices_lock:
            # 代号在打云端之前记：这一趟要往返，回来时可能已经不是当初那个作用域了
            scope = get_manager().current_scope()
            try:
                devices = await self._miot_client.get_devices_async()
                # 故意不 deepcopy:返回的就是 SDK buffer 本体。refresh_cameras 尾部的
                # 对账靠这个别名看到刚重拉的设备清单,改成拷贝会把新绑设备当"该退订"。
                self._device_info_dict = devices
                # 与 refresh_cameras / refresh_camera_online_status 同口径:对账派后台任务。
                # 目标集合在进锁后才求值(见 _reconcile_subscriptions),晚几秒不影响正确性;
                # 本方法挂在 /miot/home_info?refresh=true 与 /miot/refresh_miot_devices
                # 两个 HTTP handler 上,不能被整批 SUBACK 拖住。
                self._spawn_subscription_sync()
            except Exception as e:
                logger.error("Failed to refresh devices: %s", e)
                return None
            try:
                self._reconcile_iot_deletions(devices, scope)
            except Exception as e:
                # 调和失败不该让刷新看起来失败：设备缓存已换、订阅对账已派出
                logger.warning("清理已离开当前家庭的设备失败: %s", e)
            try:
                self._sync_iot_online_flags(devices, scope)
            except Exception as e:
                logger.warning("同步在线标志到容器失败: %s", e)
            return devices

    def _sync_iot_online_flags(
        self, devices: dict[str, MIoTDeviceInfo], scope: int
    ) -> None:
        """把当前家庭设备的在线标志重写一遍。

        推送会在 MQTT 断连期间丢，而重拉会把缓存修对 —— 不跟着写容器，容器就停在旧值
        上，且没有任何机制能看出它落后了。

        同值不产事件（容器按值比对），所以每次刷新都重写不会刷爆订阅方。
        """
        from miloco.manager import get_manager

        manager = get_manager()
        if scope != manager.current_scope():
            logger.info("作用域已变，跳过这次在线标志同步")
            return
        store = manager.state_store
        for did, device in filter_by_home(self._kv_repo, devices).items():
            write_online(store, did, bool(getattr(device, "online", True)))

    def _reconcile_iot_deletions(
        self, devices: dict[str, MIoTDeviceInfo], scope: int
    ) -> None:
        """删掉容器里已不属于当前家庭的设备。

        设备换家、单台解绑、设备被删，三种情况同一个形态，一次覆盖，不用各写一个钩子。

        方向是「容器里有、当前家庭没有」。反过来那批是「还没对齐到的」，归对齐管。

        `devices` 是**本次刷新成功返回的那份**，不是 `_device_info_dict`、不是上一次的
        缓存 —— 拿旧缓存做差集会把刚进来的设备算成多余的。
        """
        from miloco.manager import get_manager

        manager = get_manager()
        if scope != manager.current_scope():
            # refresh_devices 是 MIPS 重连回调，切家过程中会被触发。不挡的话，一个旧
            # 回调会拿旧家庭的设备集合去和刚清空重建的新树做差集，把新家庭的设备全删掉
            logger.info("作用域已变，跳过这次设备清理")
            return
        if not devices:
            # 「家里一台设备都没有」和「接口出问题」长得一样，宁可留一台幽灵设备。
            # 过滤后为空则照常清理 —— 那是这个家庭确实没有设备
            logger.info("云端没返回任何设备，跳过这次清理")
            return
        store = manager.state_store
        keep = set(filter_by_home(self._kv_repo, devices))
        # 只要第一层的键；`get` 落在中间节点会把整棵子树复制一遍，等出现第二个需要
        # 枚举实体 id 的调用方再给容器加一个只取子节点名的接口
        for did in set(store.get("iot/device", {})) - keep:
            store.delete(f"iot/device/{did}", source=STATE_RECONCILE_SOURCE)

    @staticmethod
    def _log_device_diff(action: str, dev: MIoTDeviceInfo | None, did: str) -> None:
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
        """转发 bind/unbind 推送;bind 直接补订该 did,unbind 退订并抹记账。

        bind = 设备回到账户,立即补订 state/meta(幂等:重绑时若 broker 订阅还
        活着,SDK 的 ``did in set`` 短路直接返回、不重复发包),镜像记在订阅
        成功之后——失败则不记录,防抖到点后的 refresh_devices 对账会照常把它
        当 to_add 重试。unbind = 设备离开账户,锁内抹记账并递增代次(作废
        可能把该 did 写回镜像的在途提交),锁外网络退订(best-effort)。
        不再需要"bind 时先退订再重订":unbind 事件自己就会把订阅退掉。
        """

        # 先转发:监听器武装 5s 尾沿防抖 / 欢迎播报,下面的记账与网络临界区
        # (锁不跨网络 I/O)不会让推送处理链排队。
        await self._bind_listener.on_event(msg)
        if not msg.did:
            return

        if msg.event == "bind":
            # 镜像记在 SDK 确认订阅之后:失败时镜像不记录,5s 防抖后的对账会
            # 重试;成功时 add(不递增代次——add 不会把已移除设备写回镜像,
            # 递增反而会把并发三类对账的在途提交全部作废、白白补一轮)。
            try:
                await self._miot_client.sub_device_state_async(msg.did)
            except Exception as e:
                logger.error("sub device-state on bind failed did=%s: %s", msg.did, e)
            else:
                async with self._sub_intent_lock:
                    self._subscribed_device_state_dids.add(msg.did)
            try:
                await self._miot_client.sub_device_meta_async(msg.did)
            except Exception as e:
                logger.error("sub device-meta on bind failed did=%s: %s", msg.did, e)
            else:
                async with self._sub_intent_lock:
                    self._subscribed_meta_dids.add(msg.did)
            return

        if msg.event == "unbind":
            # 抹记账在锁内(纯内存,与对账差集/提交互斥)、网络退订放锁外。
            # 锁外安全的前提:SDK 退订从 discard 到发包无任何 await,对账插不
            # 进来——若将来给退订加 UNSUBACK 等待,必须把这两次 unsub 挪回锁内。
            # 代次递增让正在飞行中的对账提交自失效(见 _reconcile_subscriptions),
            # 由防抖到点后的 refresh_devices 派出的下一轮对账按新设备清单收敛。
            async with self._sub_intent_lock:
                self._subscribed_device_state_dids.discard(msg.did)
                self._subscribed_meta_dids.discard(msg.did)
                self._sub_intent_generation += 1
            try:
                await self._miot_client.unsub_device_state_async(msg.did)
            except Exception as e:
                logger.error(
                    "unsub device-state on unbind failed did=%s: %s", msg.did, e
                )
            try:
                await self._miot_client.unsub_device_meta_async(msg.did)
            except Exception as e:
                logger.error(
                    "unsub device-meta on unbind failed did=%s: %s", msg.did, e
                )

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

    async def _on_device_state_changed_event(self, msg: MIoTDeviceStateEvent) -> None:
        """处理云端上下线推送:更新两份缓存的 `online`,再交给本层的消费方。

        相机对账防抖只在三种情况重新武装:命中相机、两份缓存都不认识、
        相机清单从未成功加载(注意不是"缓存为空"——零相机账户加载成功是空字典,
        按空判断会让每条灯事件永续重拉)。已知非相机设备不武装。
        """
        cam = self._camera_info_dict.get(msg.did)
        if cam is not None:
            cam.online = msg.event == "online"
            logger.info(
                "camera cloud state updated: did=%s online=%s (event=%s)",
                msg.did,
                cam.online,
                msg.event,
            )
        dev = self._device_info_dict.get(msg.did)
        if dev is not None:
            dev.online = msg.event == "online"
            # 相机上面已打过 INFO;非相机设备按账户规模可能有上百台在反复翻转,
            # 降到 DEBUG 免得刷掉运维日志。
            if cam is None:
                logger.debug(
                    "device cloud state updated: did=%s online=%s (event=%s)",
                    msg.did,
                    dev.online,
                    msg.event,
                )
        if cam is not None or dev is None or not self._cameras_loaded:
            await self._camera_state_listener.on_event(msg)
        await self._fan_out(self._state_listeners, msg, "device-state")

    def add_device_state_listener(
        self, callback: Callable[[Any], Awaitable[None]]
    ) -> None:
        """加一个上下线推送的消费方。多次调用按注册顺序全部收到。"""
        self._state_listeners.append(callback)

    def add_device_props_listener(
        self, callback: Callable[[Any], Awaitable[None]]
    ) -> None:
        """加一个属性推送的消费方。多次调用按注册顺序全部收到。"""
        self._props_listeners.append(callback)

    async def _fan_out(
        self, listeners: list[Callable[[Any], Awaitable[None]]], msg: Any, label: str
    ) -> None:
        """逐个投递。一个消费方抛异常不影响其余 —— 它们互不知情，也不该互相背锅。"""
        for callback in listeners:
            try:
                await callback(msg)
            except Exception as e:
                logger.error("%s listener failed: %s", label, e)

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

        在 _spawn_subscription_sync 派出的后台任务里跑(经 refresh_devices /
        refresh_cameras / refresh_camera_online_status 到达),不持任何刷新锁;
        并发安全来自 _sub_intent_lock + 目标集锁内求值(见 _reconcile_subscriptions),
        **别把 _sub_intent_lock 当多余的双重加锁删掉**。

        ACCOUNT-WIDE ON PURPOSE — do NOT scope-filter this by managed home.
        A device sitting in an out-of-scope home must already be subscribed so
        an hr_change moving it INTO a managed home is heard — that's how the
        move-in welcome works. The managed-home scope is applied only at the
        welcome step (_is_move_into_scope / DeviceWelcomeService), never to the
        subscription. (Scene subs differ — they ARE scoped, since a scene has
        no move-into-scope analogue; see _sync_scene_subscriptions.)

        Dids containing '/' (Huami/Zepp-bridged sub-devices) are skipped:
        the '/' breaks the topic path AND the decoder regex — see
        _is_subscribable_did. (An older note here claimed 0x87 rejection; that
        is UNVERIFIED, same origin as the disproven blt.* observation.)
        """
        skipped = [
            did for did in self._device_info_dict if not _is_subscribable_did(did)
        ]
        if skipped:
            logger.debug(
                "device-meta: skipping %d did(s) with '/': %s", len(skipped), skipped
            )
        await _reconcile_subscriptions(
            lambda: {
                did for did in self._device_info_dict if _is_subscribable_did(did)
            },
            self._subscribed_meta_dids,
            self._miot_client.sub_device_meta_async,
            self._miot_client.unsub_device_meta_async,
            lock=self._sub_intent_lock,
            semaphore=self._sub_semaphore,
            generation=lambda: self._sub_intent_generation,
            label="device-meta",
        )

    async def _sync_device_state_subscriptions(self) -> None:
        """Reconcile per-device cloud state (online/offline) subs to the
        device list.

        在 _spawn_subscription_sync 派出的后台任务里跑——经 refresh_devices、
        refresh_cameras 和 refresh_camera_online_status(60s 防抖的落点)到达,
        不持任何刷新锁;并发安全见 _sync_meta_subscriptions。ACCOUNT-WIDE:
        every device's online state surfaces in `device list` (cameras included).

        Dids containing '/' (Huami/Zepp-bridged sub-devices) are skipped:
        the '/' breaks the topic path AND the decoder regex. `blt.*` /
        `proxy.*` gateway children are NOT excluded — their state subscribes
        are SUBACK 0x00 (a live probe; the earlier 0x87 was a broken-instance
        artifact), so subscribe them.
        """
        skipped = [
            did for did in self._device_info_dict if not _is_subscribable_did(did)
        ]
        if skipped:
            logger.debug(
                "device-online-state: skipping %d did(s) with '/': %s",
                len(skipped),
                skipped,
            )
        await _reconcile_subscriptions(
            lambda: {
                did for did in self._device_info_dict if _is_subscribable_did(did)
            },
            self._subscribed_device_state_dids,
            self._miot_client.sub_device_state_async,
            self._miot_client.unsub_device_state_async,
            lock=self._sub_intent_lock,
            semaphore=self._sub_semaphore,
            generation=lambda: self._sub_intent_generation,
            label="device-online-state",
        )

    async def _on_scene_changed_event(self, msg: MIoTSceneChangedEvent) -> None:
        """Forward home scene-change push events to the dedicated listener.

        The debounce + refresh logic lives in
        ``miloco.miot.mips_listeners.SceneEventListener`` — this method is a
        thin shim, mirroring ``_on_user_bind_event``.
        """
        await self._scene_listener.on_event(msg)

    async def _on_subscription_reset(
        self, meta_dids: set, state_dids: set, scene_home_ids: set
    ) -> None:
        """mips 实例重建后,把本层意图镜像重建为 SDK 重放后的集合(原地改写,见
        _reconcile_subscriptions 的原地要求)。代次递增使飞行中对账的提交自失效,
        由重建后必然触发的下一轮对账重订。由 SDK 在三个重放块之后调用。"""
        async with self._sub_intent_lock:
            for mirror, truth in (
                (self._subscribed_meta_dids, meta_dids),
                (self._subscribed_device_state_dids, state_dids),
                (self._subscribed_scene_home_ids, scene_home_ids),
            ):
                mirror.clear()
                mirror.update(truth)
            self._sub_intent_generation += 1
            logger.info("subscription intent rebuilt from SDK post-replay state")

    def _collect_home_ids(self) -> set[str]:
        """Union of home_ids across cached devices / cameras / scenes.

        Reads each cache as of its last refresh. Called from the background
        reconcile loop, which is spawned by refresh_devices, refresh_cameras
        AND refresh_camera_online_status — so which cache is current depends
        on the entry point; the others reflect their previous refresh. A home
        appearing ONLY in a not-yet-refreshed cache is thus picked up one
        refresh late; in practice every home has devices, so the union covers
        them immediately, without an extra homes HTTP call.

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

        在 _spawn_subscription_sync 派出的后台任务里跑,不持刷新锁;与
        _subscribed_scene_home_ids 的差集由 _sub_intent_lock 串行化、目标集锁内
        求值(见 _reconcile_subscriptions)。

        Scoped to managed homes only: a scene in an out-of-scope home is
        irrelevant and has no move-into-scope analogue (unlike device-meta,
        which must stay account-wide so a device moving INTO scope is heard).
        A home leaving scope therefore drops out of ``target`` and gets
        unsubscribed on the next sync.
        """
        await _reconcile_subscriptions(
            lambda: {
                h for h in self._collect_home_ids() if is_home_allowed(self._kv_repo, h)
            },
            self._subscribed_scene_home_ids,
            self._miot_client.sub_home_scene_async,
            self._miot_client.unsub_home_scene_async,
            lock=self._sub_intent_lock,
            semaphore=self._sub_semaphore,
            generation=lambda: self._sub_intent_generation,
            label="home-scene",
            key_name="home",
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
            logger.info("Retrieved MIoT auth info, code: %s, state: %s", code, state)
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

    async def read_cameras_awake(
        self, dids: list[str], *, cache_only: bool = False
    ) -> dict[str, dict[int, bool | None]]:
        """读取相机「镜头开关」态（``camera-control:on``），**per-lens**，写入 ``_camera_awake_cache``。

        返回 ``{did: {channel: True | False | None}}``：每路 ``True``=镜头开着、``False``=镜头关
        （隐私/物理遮挡）、``None``=读失败/未知；channel 缺失亦视为未知。单摄只有 ``{0: …}``；
        双摄 ``{0: 球机, 1: 枪机}``。机型无 ``camera-control:on`` → ``{}``（整机未知）。

        镜头→通道映射**按 siid 序数**（不看本地化描述）：多通道且开关数 ≥ 通道数时，取 siid
        最高的 ``channel_count`` 个开关作「每路开关」（自动排除低 siid 的主控），按 siid 升序依次
        配 ch0/ch1/…；否则（开关数 < 通道数，或单摄）全部开关 OR 后归各路（单摄即归 ch0，与旧整机
        OR 等价）并打 warning。同一路多开关按 OR 合并（任一 True→True；否则任一 None→None；全
        False→False，不因个别读失败误判关闭）。未来某机型 siid 顺序不符时走 per-model 配置覆盖，
        不退整机 OR、不加启发（详见函数体注释）。

        ``cache_only=True``：只返回缓存里已有的值（没有→``{}``），**完全不发云端请求**——给
        ``list_cameras_with_state`` 用。默认走云端新鲜读、回填缓存。属性无变更推送、只能云端读，
        故新鲜度到「上次 refresh」为止。
        """
        result: dict[str, dict[int, bool | None]] = {}
        if cache_only:
            for did in dids:
                result[did] = dict(self._camera_awake_cache.get(did) or {})
            return result
        params: list[MIoTGetPropertyParam] = []
        # did → {channel: [(siid, piid), ...]}：每路由哪些开关 iid 供值。
        param_meta: dict[str, dict[int, list[tuple[int, int]]]] = {}
        for did in dids:
            device = self._device_info_dict.get(did)
            if device is None:
                result[did] = {}
                continue
            try:
                spec = await self._fetch_device_spec(device.urn)
            except Exception as e:
                logger.warning("read awake: fetch spec failed did=%s: %s", did, e)
                result[did] = {}
                continue
            switch_iids = _resolve_camera_switch_iids(spec)
            if not switch_iids:
                # 该机型 spec 无 camera-control:on → 无法判断镜头态，记 model 便于排查。
                logger.info(
                    "camera %s (model=%s) has no camera-control:on prop; awake=unknown",
                    did,
                    getattr(device, "model", "?"),
                )
                result[did] = {}
                self._camera_awake_cache[did] = {}
                continue
            channel_count = (
                getattr(self._camera_info_dict.get(did), "channel_count", None) or 1
            )
            # 镜头开关 → 通道**按 siid 序数**归属（不看本地化描述）：多通道且开关数 ≥ 通道数时，
            # 取 siid 最高的 channel_count 个作「每路开关」（自动排除低 siid 的主控），按 siid
            # 升序依次配 ch0/ch1/…（实测球机 siid 恒低于枪机、主控最低 → 升序=通道序）。
            #
            # 「siid 升序 = 通道序」是**序数假设**。**若未来某机型不符**（球/枪 siid 顺序反了、
            # 或主控不是最低 siid 等），**不要**退整机 OR、也**不要**再加别的猜测式启发——那只会
            # 把静默错位换个花样。**正确处置是给该 model 单独在配置里显式罗列 `{model: {siid:
            # channel}}` 覆盖**（届时补一个 per-model 映射表 + 实证一次即可）。
            #
            # 下面的**整机 OR** 只是「连每路开关都枚举不出」（开关数 < 通道数）的最后兜底——它能
            # 正确处理「整台全关→排除」，只是丢了 per-lens 精度；命中即打 warning 让降级可观测。
            # 它**不是**用来顶替错序映射的，错序请走上面的 per-model 配置路径。
            ch_iids: dict[int, list[tuple[int, int]]] = {}
            if channel_count > 1 and len(switch_iids) >= channel_count:
                per_lens = sorted(switch_iids)[
                    -channel_count:
                ]  # 最高 cc 个、已按 siid 升序
                for ch in range(channel_count):
                    ch_iids[ch] = [per_lens[ch]]
            else:
                if channel_count > 1:
                    logger.warning(
                        "camera %s (model=%s) channel_count=%d 但仅 %d 个 camera-control:on "
                        "开关，无法按 siid 分路，awake 退整机 OR",
                        did,
                        getattr(device, "model", "?"),
                        channel_count,
                        len(switch_iids),
                    )
                for ch in range(channel_count):
                    ch_iids[ch] = list(switch_iids)
            param_meta[did] = ch_iids

        # 从 param_meta 汇出**去重**的待读属性（整机 OR 兜底会让多路引用同一开关）。
        for did, ch_iids in param_meta.items():
            for siid, piid in {sp for iids in ch_iids.values() for sp in iids}:
                params.append(MIoTGetPropertyParam(did=did, siid=siid, piid=piid))
        if not params:
            return result
        try:
            rows = await self.get_device_properties(params)
        except Exception as e:
            logger.warning("read awake: get_props failed: %s", e)
            for did in param_meta:
                result[did] = {}
            return result

        by_key: dict[tuple, dict] = {}
        for row in rows or []:
            try:
                by_key[(row["did"], row["siid"], row["piid"])] = row
            except (TypeError, KeyError):
                continue
        for did, ch_iids in param_meta.items():
            per_ch: dict[int, bool | None] = {}
            for ch, iids in ch_iids.items():
                vals: list[bool | None] = []
                for siid, piid in iids:
                    row = by_key.get((did, siid, piid))
                    if not row or row.get("code", -1) != 0:
                        vals.append(None)
                    else:
                        vals.append(bool(row.get("value")))
                if any(v is True for v in vals):
                    per_ch[ch] = True
                elif any(v is None for v in vals):
                    per_ch[ch] = None
                else:
                    per_ch[ch] = False
            result[did] = per_ch
            self._camera_awake_cache[did] = per_ch
        return result

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
                logger.error(
                    "Token refresh failed, re-login required: miloco-cli account bind"
                )
