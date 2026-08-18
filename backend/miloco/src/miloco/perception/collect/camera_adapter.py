"""
Camera device adapter — manages decoded video/audio frame streams from cameras.

Subscribes to 2 decoded stream types per device via MiotProxy:
  1. decoded_video — decoded PyAV VideoFrame
  2. decoded_audio — decoded PyAV AudioFrame

Buffers fragments in a 2-track MultiTrackSyncBuffer per device. The sync
buffer handles time-windowed A/V alignment automatically.

Multi-channel cameras (dual-lens / NVR) expose each lens as a separate
perception unit. A single-lens camera keeps its bare did; each extra channel
gets a synthetic did ``{did}:ch{n}`` so downstream keying (device_results,
tracking, identity) never collides across lenses. The synthetic did is the key
that flows through discover / connect / disconnect / collect; the bare physical
did is only used for the underlying SDK stream (sub/unsub) calls.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from miot.types import MIoTCameraInfo

from miloco.config import get_settings
from miloco.miot.client import MiotProxy
from miloco.miot.schema import CameraInfo
from miloco.node_monitor import NodeName, get_monitor
from miloco.perception.collect.adapter_base import BaseDeviceAdapter
from miloco.perception.collect.stream_buffer import (
    MultiTrackSyncBuffer,
    StreamFragment,
)
from miloco.perception.schema import (
    DecodedAudioFrame,
    DecodedVideoFrame,
    DeviceData,
)
from miloco.perception.types import PerceptionDevice

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)


def _monotonic_ms() -> int:
    """Monotonic wall-clock time in milliseconds."""
    return time.monotonic_ns() // 1_000_000


def _unix_ms() -> int:
    """Unix epoch time in milliseconds."""
    return int(time.time() * 1000)


_CAMERA_TRACKS = ["decoded_video", "decoded_audio"]

# 按需补建 refresh_cameras 的最小间隔：无设备态下 sync 循环 1s 一轮，
# 不节流会变成每秒一次重 SDK 调用 + 建连尝试。10s 足够让相机就绪后及时恢复。
_ONDEMAND_REFRESH_MIN_INTERVAL_MS = 10_000

# 静默检测：感知视频流 N 秒无帧 → 判僵尸连接。miss 层对「连接在但不出帧」的静默
# 无解（keepalive 只探连接活性、不探数据流），只能上层检测 + destroy/create 重拉。
# 正常流 ~1fps，30s 无帧基本确定是断；再短会误伤正常低帧。
_SILENCE_THRESHOLD_MS = 30_000

# 首帧专用上界：原生建连 + 首个 IDR 最慢约 15s，留足余量。超过它仍一帧未到 →
# 与「出过帧后静默」同等对待，走 destroy+create 自愈。没有这个上界，
# last_video_frame_ms 恒为 0 的通道（原生会话建起来了但媒体流一帧不来，正是跨网段 /
# 严格 NAT 最典型的僵尸态）会被「等首帧」分支无条件跳过 → 故障越彻底越救不回来。
_FIRST_FRAME_THRESHOLD_MS = 90_000

# 重连防抖：同台相机重连后 N 秒内不再重连，避免真坏相机 30s 一轮空转。
_RECONNECT_COOLDOWN_MS = 5 * 60_000

# 单通道相机的默认通道号（也是多通道相机 ch0）。
DEFAULT_VIDEO_CHANNEL = 0
DEFAULT_AUDIO_CHANNEL = 0

# 合成 did 的通道后缀分隔符：``{physical_did}:ch{n}``。
_CHANNEL_SEP = ":ch"


def split_channel_did(did: str) -> tuple[str, int]:
    """拆合成 did → (物理 did, 通道号)。

    ``'cam1:ch1'`` → ``('cam1', 1)``；``'cam1'`` → ``('cam1', 0)``（单通道直通）。
    """
    if _CHANNEL_SEP in did:
        physical, ch = did.rsplit(_CHANNEL_SEP, 1)
        return physical, int(ch)
    return did, DEFAULT_VIDEO_CHANNEL


@dataclass
class _CameraDeviceState:
    """Per-channel stream state — one entry per camera lens.

    Keyed by the synthetic did (``did``). For single-lens cameras that is the
    bare did (channel 0); for multi-channel cameras it carries the ``:ch{n}``
    suffix. The physical did / channel for SDK stream calls are derived from
    ``did`` via :func:`split_channel_did` at the (dis)connect call sites.
    """

    did: str
    sync_buffer: MultiTrackSyncBuffer = field(
        default_factory=lambda: MultiTrackSyncBuffer(_CAMERA_TRACKS)
    )
    # Registration IDs for multi-reg decoded frame callbacks
    decoded_video_reg_id: int = -1
    decoded_audio_reg_id: int = -1
    # Clock calibration: epoch_delta = unix_ms - monotonic_ms (locked on first frame)
    # Used to convert monotonic wall_ms to unix timestamps for display.
    epoch_delta: int | None = None
    # 最近一帧视频的 monotonic wall_ms，静默检测用。
    last_video_frame_ms: int = 0
    # 订阅完成时刻的 monotonic wall_ms。首帧未到（last_video_frame_ms == 0）时
    # 替代它参与静默判定，给「等首帧」一个上界，见 _FIRST_FRAME_THRESHOLD_MS。
    connected_at_ms: int = 0


class CameraDeviceAdapter(BaseDeviceAdapter):
    """Camera device type adapter — decoded video/audio frame streams."""

    device_type = "camera"
    _node_name = NodeName.CAMERA

    def __init__(
        self,
        miot_proxy: MiotProxy,
        on_window_ready: Callable[[], None] | None = None,
    ):
        self._miot_proxy = miot_proxy
        self._on_window_ready = on_window_ready
        self._devices: dict[str, _CameraDeviceState] = {}
        self._last_ondemand_refresh_ms = 0
        # 静默重连防抖标记：did -> 最近一次重连的 monotonic ms。
        self._last_reconnect_ms: dict[str, int] = {}

    async def discover_devices(
        self,
        all_devices: dict | None = None,
        online_only: bool = True,
        require_lan: bool = True,
        cap: bool = True,
    ) -> dict[str, PerceptionDevice]:
        if not self._miot_proxy.is_authenticated:
            return {}
        return self._filter_cameras_from_all(
            all_devices if all_devices else await self._miot_proxy.get_cameras(),
            online_only=online_only,
            require_lan=require_lan,
            cap=cap,
        )

    def _filter_cameras_from_all(
        self,
        all_devices: dict,
        *,
        online_only: bool = True,
        require_lan: bool = True,
        cap: bool = True,
    ) -> dict[str, PerceptionDevice]:
        """Filter camera-type devices from a full device dict.

        Drops cameras that are either:
        - 不在启用的家庭范围内（启用集为空时全部阻断——用户需先 switch_home），或
        - did 在停用的相机集合里。

        ``cap=True``（默认，连接/投喂路径）时最后按**流路数**（多通道相机一台算
        ``channel_count`` 路）升序确定性截断到 ``MAX_ENABLED_CAMERAS``：被动路径
        （登录/绑定后黑名单为空 → 家庭内全部相机均通过 home filter）下，这是投喂上限的
        唯一兜底，与 ``service.toggle_camera`` 的主动 enable 校验互补。不写 KV、不碰黑
        名单——只是少返回（从而少连接）超出上限的相机；口径与 toggle_camera 自洽（同样
        只数通过 home filter + 未拉黑的相机的流路数）。
        ``cap=False`` 用于「列全集」语义（如 rule target 校验），不受投喂上限影响。
        """
        from miloco.miot.filter import select_active_camera_dids

        kv = self._miot_proxy._kv_repo
        # 选择口径与 refresh_cameras 的 manager 建销共用同一函数，避免投喂集与拉流集
        # 漂移：在启用家庭 + 未拉黑 + 在线 + 镜头未关、按 did 截到 MAX_ENABLED_CAMERAS。
        cams = {
            did: info
            for did, info in all_devices.items()
            if isinstance(info, MIoTCameraInfo)
        }
        active = select_active_camera_dids(
            kv,
            cams,
            online_only=online_only,
            require_lan=require_lan,
            cap=cap,
            awake_map=getattr(self._miot_proxy, "_camera_awake_cache", None),
        )
        # ``select_active_camera_dids`` 已按通道展开、返回**合成 did**（单摄裸 did、多摄
        # ``{did}:ch{n}``），并已做过 per-channel 黑名单 / per-lens 镜头门 / 上限截断。这里
        # 只需按合成 did 建 PerceptionDevice;相机元数据按物理 did 从 cams 取。
        result: dict[str, PerceptionDevice] = {}
        for syn_did in active:
            physical_did, _ = split_channel_did(syn_did)
            camera_info = CameraInfo.model_validate(cams[physical_did].model_dump())
            result[syn_did] = PerceptionDevice(
                did=syn_did,
                name=camera_info.name,
                device_type="camera",
                room_id=camera_info.room_name,
                room_name=camera_info.room_name,
                # 已连上的相机即视为可达：直连掐死同网段 OTU 保活令 lan_online 掉 False，
                # 但连都连上了、可达是显然的，不能因此把在拉流的相机标成 offline 停投喂。
                online=camera_info.online
                and (camera_info.lan_online or camera_info.connected),
            )
        return result

    async def sync_devices(self, all_devices: dict | None = None) -> None:
        """周期 sync 入口：先做「按需补建」，再走基类热插拔同步。

        登录瞬间相机 LAN 未就绪时 `refresh_cameras` 建不成 camera_img_manager，
        之后无任何机制补建 → 永久不拉流（需重启进程）。这里在周期 sync 路径
        （`all_devices is None`）检测到「scope 内应连相机数 > 已连数」时，先触发
        一次 `refresh_cameras` 补建 manager 再交基类连接。应连数用
        `online_only=True, require_lan=False`：放过 lan_online 陈旧成 false 的卡死态
        相机（要救），但排除云端就离线的相机（救不活，避免它让判据永真致 refresh
        空转）。scope 内相机要么已连、要么云端离线时不触发，零额外开销。
        """
        if all_devices is None and self._miot_proxy.is_authenticated:
            await self._check_stalled_cameras()
            try:
                # 判据用**严格门**（require_lan 默认 True，与 refresh_cameras 建销
                # manager 的 select_active_camera_dids 完全同口径）。曾用
                # require_lan=False 想「放过 lan_online 陈旧成 false 的卡死态相机」，
                # 但那条路走不通也没必要：
                #   - 真正「卡死但还连着」的相机走 is_camera_connected 这条支路，本来
                #     就在严格集里（filter.py: lan_online or is_camera_connected）；
                #   - 既不 LAN 可达、原生也没连上的相机，refresh_cameras 用的是同一道
                #     严格门 ⇒ 它压根不会为这台建 manager，触发 refresh 是**必然空转**。
                # 而 missing 恒非空 + 节流窗(10s) ≈ sync 周期(10s) ⇒ 每轮都打一次云端
                # get_cameras_async。原注释已经在防「云端离线相机让判据永真致 refresh
                # 空转」，LAN 这一半是同一个坑的另一面。
                expected = await self.discover_devices(online_only=True)
                now_ms = _monotonic_ms()
                # 判据必须是**集合差**而非数量比较:数量看不见「数量相等、成员不同」。
                # 5+ 台相机的家庭里低字典序相机上线顶位时,应连集 {100,200,300,400} 与
                # 已连集 {200,300,400,500} 数量恰好都等于上限 ⇒ 数量判据恒 False ⇒
                # refresh_cameras 永不触发 ⇒ 顶位的 100 因 manager 缺失每轮订阅失败被
                # 剔除(日志反复刷 "will retry on next sync",而那个 retry 依赖的补建
                # 永不成立),500 则继续占着原生会话白拉流。旧的带截断保留集会把 500
                # 断掉使数量下降、间接触发补建;超集化拿走了那条自愈路径,而
                # _converge_feed_cap 只处理「数量 > 上限」,看不见成员漂移。
                missing = set(expected) - self._devices.keys()
                if missing and (
                    now_ms - self._last_ondemand_refresh_ms
                    >= _ONDEMAND_REFRESH_MIN_INTERVAL_MS
                ):
                    self._last_ondemand_refresh_ms = now_ms
                    await self._miot_proxy.refresh_cameras()
            except Exception as e:  # noqa: BLE001
                logger.warning("On-demand camera manager refresh failed: %s", e)
        # 断开判断与补建探测同口径 (require_lan=False):只按云端在线判定,保留
        # lan_online 偶发假 False 的已连相机,防止拉流中的相机被误断。
        await super().sync_devices(all_devices, disconnect_require_lan=False)
        await self._converge_feed_cap(all_devices)

    async def _converge_feed_cap(self, all_devices: dict | None = None) -> None:
        """把超出投喂上限的通道断掉,口径与 select_active_camera_dids 完全一致。

        为什么上限收敛不能寄生在基类的断开判据里:那个「保留集」为了满足
        「保留集 ⊇ 发现集」的不变量必须 ``cap=False``（截断按合成 did 升序取前 N,
        ``sorted(超集)[:N]`` 不含 ``sorted(子集)[:N]``），于是它再也收不住已连集的
        规模;而连接侧走的是带截断的发现集、``connect_device`` 自己不认上限。两边
        一叠加:低字典序相机上线被发现集纳入并新连,先前占位的高字典序相机仍在保留
        集里不被断开 ⇒ 已连路数单调越过上限,且被挤出活跃集的那路会在
        ``refresh_cameras``(销 manager) 与静默检测(建 manager) 之间无限震荡,白占
        相机有限的并发流名额。所以上限收敛独立收在这里,基类对投喂上限保持无感知。

        淘汰顺序先看「本轮还通过严格门吗」、再看字典序。只按字典序排会把优先级反转:
        保留集刻意放宽了 LAN 门（正是本 PR 要救的那类:云端在线但 LAN 已探不到、原生也
        没连上），这种僵尸通道若字典序靠前,就会挤掉字典序靠后、刚刚真连上的健康通道 ——
        日志每轮刷一条 over feed cap、那一路的投喂反复中断,而占着名额的僵尸一帧不出。
        （该链有兜底:僵尸静默满 30s / 首帧满 90s 后被静默检测连带断开、名额释放,所以
        表现是「另一台掉线后的 30~90s 窗口内健康相机被反复断连 3~9 次」而非永不自愈,
        但优先级反转本身与本方法要消灭的抖动是同一类失败模式。）
        """
        from miloco.miot.filter import MAX_ENABLED_CAMERAS

        if len(self._devices) <= MAX_ENABLED_CAMERAS:
            # 未超限直接退,省掉下面那次 discover(常态路径零开销)。
            return
        try:
            # 严格门 + 截断,与 refresh_cameras 的 manager 建销同口径;传入本轮的
            # all_devices 快照,热插拔路径不另取一份可能已漂移的相机表。
            preferred = set(await self.discover_devices(all_devices))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Feed-cap converge discover failed (%s); falling back to did order", e
            )
            preferred = set()
        ordered = sorted(self._devices, key=lambda d: (d not in preferred, d))
        for did in ordered[MAX_ENABLED_CAMERAS:]:
            logger.warning(
                "Camera %s over feed cap (%d), disconnecting overflow channel",
                did,
                MAX_ENABLED_CAMERAS,
            )
            try:
                await self.disconnect_device(did)
            except Exception as e:  # noqa: BLE001
                logger.error("Overflow disconnect failed %s: %s", did, e)

    async def _check_stalled_cameras(self) -> None:
        """静默检测：感知视频流超阈值无帧 → 判僵尸连接并触发重连。

        miss 层对「连接在但不出帧」的静默无解（keepalive 只探连接活性、不探数据流），
        只能上层检测 + 主动 destroy/create 重拉。只在周期 sync 路径跑，避免热插拔
        语义被静默检测打断。
        """
        now_ms = _monotonic_ms()
        # 先按物理 did 归并本轮所有静默通道：多镜头相机的 ch0/ch1 共用同一个 native
        # 会话，一次 destroy+create 就够；按通道 did 各触发一次等于重复重建（重建的
        # 还是同一个物理会话，几毫秒内 destroy 两次，四镜头就是四次）。
        stalled_by_physical: dict[str, list[str]] = {}
        for did, state in list(self._devices.items()):
            # 首帧未到 → 用「订阅时刻」判，阈值放宽到 _FIRST_FRAME_THRESHOLD_MS
            # （连接刚建立确实要等十几秒）；首帧已到 → 用「最后一帧时刻」判，
            # 阈值 _SILENCE_THRESHOLD_MS。
            # 这里必须有上界:last_video_frame_ms 只有帧到达才脱离 0，若无条件跳过，
            # 「一帧都没出」这个最坏的僵尸态会永久免疫检测（连日志都不留）。
            if state.last_video_frame_ms == 0:
                if (
                    state.connected_at_ms == 0
                    or now_ms - state.connected_at_ms < _FIRST_FRAME_THRESHOLD_MS
                ):
                    continue
            elif now_ms - state.last_video_frame_ms < _SILENCE_THRESHOLD_MS:
                continue
            physical_did, _ = split_channel_did(did)
            cam = self._miot_proxy.get_cached_camera(physical_did)
            # 云端已离线 → 救不活，交给基类按在线态断开，别白重连。
            if cam is not None and not cam.online:
                continue
            # 带上判定依据:「从未出帧」多半是路由/NAT 建不起媒体流,「出过帧后静默」
            # 多半是相机侧打嗝——两者运维处置不同,日志里要能一眼分开。
            stalled_by_physical.setdefault(physical_did, []).append(
                f"{did}(no-first-frame in {now_ms - state.connected_at_ms}ms)"
                if state.last_video_frame_ms == 0
                else f"{did}(silent {now_ms - state.last_video_frame_ms}ms)"
            )

        for physical_did, stalled_dids in stalled_by_physical.items():
            # 防抖按物理 did 计：同一台相机 5min 内只重建一次。
            if (
                now_ms - self._last_reconnect_ms.get(physical_did, 0)
                < _RECONNECT_COOLDOWN_MS
            ):
                continue
            logger.warning(
                "Camera %s stalled (channels=%s), reconnecting",
                physical_did,
                stalled_dids,
            )
            await self._reconnect_stalled(physical_did)

    async def _reconnect_stalled(self, physical_did: str) -> None:
        """重建一台静默相机：停该相机全部通道的解码订阅 → 重建 native 会话。

        三层重建缺一不可：disconnect_device 只 unregister 解码回调（不动 native miss
        会话）；reconnect_camera 才 destroy+create manager 真正断掉僵尸 MTP/PPCS 会话、
        重走 miss_client_connect 的建连重试；解码订阅由**同一轮** sync 紧随其后的
        connect_device 补齐（``sync_devices`` 先跑静默检测再跑基类同步，本方法已把这些
        通道从 ``_devices`` 摘掉，它们当轮就落进「发现集 − 已连集」）。
        必须把同一物理相机的所有已连通道一起断开：destroy 会连带作废兄弟通道在旧
        实例上的 reg_id，而 connect_device 对已在 _devices 里的 did 直接 early-return，
        不先断开就永远补不回订阅。
        """
        self._last_reconnect_ms[physical_did] = _monotonic_ms()
        siblings = [
            d for d in list(self._devices) if split_channel_did(d)[0] == physical_did
        ]
        for d in siblings:
            try:
                await self.disconnect_device(d)
            except Exception as e:  # noqa: BLE001
                logger.error("Stalled camera disconnect failed %s: %s", d, e)
        try:
            await self._miot_proxy.reconnect_camera(physical_did)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Stalled camera reconnect failed %s: %s", physical_did, e
            )
            return
        # 感知侧的解码订阅由同一轮 sync 的基类同步补齐，但 watch 直播 / record_clip /
        # 播放页音频的订阅也一样死在被 destroy 的旧实例上，且它们没有 sync 这条兜底
        # （见两个 manager 的 resubscribe_camera 说明）。局部 import 防循环依赖，也避免
        # client → ws 的反向依赖。视频与音频各自独立 try：一条失败不该挡住另一条。
        try:
            from miloco.miot.ws import miot_video_stream_manager

            await miot_video_stream_manager.resubscribe_camera(physical_did)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Resubscribe live/record streams failed %s: %s", physical_did, e
            )
        try:
            from miloco.miot.ws import miot_audio_stream_manager

            await miot_audio_stream_manager.resubscribe_camera(physical_did)
        except Exception as e:  # noqa: BLE001
            logger.error("Resubscribe audio streams failed %s: %s", physical_did, e)

    async def connect_device(
        self, did: str, source: PerceptionDevice | None = None
    ) -> None:
        if did in self._devices:
            return

        # source 只表示上游 sync_devices 已完成 discover/filter；相机元数据不从
        # source 读取，统一在打包窗口/status 时按 did 从 MiotProxy cache 现取。
        if source is None:
            discovered = await self.discover_devices()
            if did not in discovered:
                logger.warning("Camera %s not found or offline, cannot connect", did)
                return

        collect_cfg = get_settings().perception.collect

        # did 是合成 did（多通道带 ``:ch{n}`` 后缀）；SDK 建流用物理 did + 通道号。
        physical_did, channel = split_channel_did(did)

        state = _CameraDeviceState(
            did=did,
            sync_buffer=MultiTrackSyncBuffer(
                track_names=_CAMERA_TRACKS,
                window_ms=collect_cfg.window_size * 1000,
                max_windows=collect_cfg.max_windows,
                on_window_ready=self._on_window_ready,
                window_settle_ms=collect_cfg.settle_ms,
                buffer_full_action=collect_cfg.full_action,
            ),
        )
        self._devices[did] = state

        # Subscribe decoded video frame stream (multi-reg)
        try:
            reg_id = await self._miot_proxy.start_camera_decode_video_stream(
                physical_did, channel,
                self._make_decoded_video_callback(did, state),
            )
            state.decoded_video_reg_id = reg_id
        except Exception as e:
            logger.error("Failed to subscribe decoded video for %s: %s", did, e)

        # Subscribe decoded audio frame stream (multi-reg)
        try:
            reg_id = await self._miot_proxy.start_camera_decode_audio_stream(
                physical_did, channel,
                self._make_decoded_audio_callback(did, state),
            )
            state.decoded_audio_reg_id = reg_id
        except Exception as e:
            logger.error("Failed to subscribe decoded audio for %s: %s", did, e)

        # 两路流都没订上 = camera_img_manager 缺失（典型：登录时相机 LAN 未就绪，
        # refresh_cameras 没建成 manager，start_*_stream 返回 -1 静默失败）。保留该
        # device 只会让 active_sources 报「已连」假象，且 did 留在 _devices 使后续
        # sync 早退、永不重试。剔除它，交给 sync_devices 的按需补建在下轮重连。
        if state.decoded_video_reg_id < 0 and state.decoded_audio_reg_id < 0:
            self._devices.pop(did, None)
            logger.warning(
                "Camera %s stream subscribe failed (manager missing?), "
                "will retry on next sync",
                did,
            )
            return

        # 订阅成功且确认留在册：记下时刻，让静默检测能给「等首帧」设上界。
        # 只有视频订上、音频没订上（或反之）的通道也走到这里——视频恒无帧时靠
        # 这个时刻在 _FIRST_FRAME_THRESHOLD_MS 后被判僵尸并重建，而不是永久静默。
        state.connected_at_ms = _monotonic_ms()

    async def disconnect_device(self, did: str) -> None:
        state = self._devices.pop(did, None)
        if not state:
            return

        # did 是合成 did（多通道带 ``:ch{n}``）；SDK 停流用物理 did + 通道号。
        physical_did, channel = split_channel_did(did)

        if state.decoded_video_reg_id >= 0:
            try:
                await self._miot_proxy.stop_camera_decode_video_stream(
                    physical_did, channel, state.decoded_video_reg_id
                )
            except Exception as e:
                logger.error("Failed to unsubscribe decoded video for %s: %s", did, e)

        if state.decoded_audio_reg_id >= 0:
            try:
                await self._miot_proxy.stop_camera_decode_audio_stream(
                    physical_did, channel, state.decoded_audio_reg_id
                )
            except Exception as e:
                logger.error("Failed to unsubscribe decoded audio for %s: %s", did, e)

        state.sync_buffer.clear()

    def collect(self, did: str, *, drain: bool = True) -> DeviceData | None:
        """Collect multimodal data from the device's sync buffer.

        Args:
            did: Device ID to collect from.
            drain: If True (realtime), pop the oldest ready window.
                   If False (active query), peek all buffered data.
        """
        state = self._devices.get(did)
        if not state:
            return None

        if drain:
            ready = state.sync_buffer.drain_ready()
            if ready is None or not any(ready.tracks.values()):
                return None
            # drain 后立刻拉丢包增量,clear 后给下一 cycle 重新累。
            dropped, ovf_cnt, max_depth, last_action = (
                state.sync_buffer.consume_drop_stats()
            )
            return self._build_device_data(
                state,
                ready.tracks,
                window_start_ms=ready.start_ms,
                window_end_ms=ready.end_ms,
                dropped_windows=dropped,
                overflow_count=ovf_cnt,
                max_buffer_depth=max_depth,
                last_overflow_action=last_action,
            )
        else:
            collect_ms = get_settings().perception.collect.window_size * 1000
            tracks = state.sync_buffer.peek_latest(duration_ms=collect_ms)
            if tracks is None or not any(tracks.values()):
                return None
            return self._build_device_data(state, tracks)

    def peek_latest_frame(self, did: str, *, window_ms: int = 2000) -> "NDArray[np.uint8] | None":
        """非破坏性取该相机最近一帧解码图(numpy BGR);无缓存返 None。

        供 tier_c 闲时定期清的 live 检测用——gate 关停时正常 pipeline 不取帧,
        这里直接读 collector 已填充的 ``decoded_video`` 缓存(独立于 gate)。
        """
        state = self._devices.get(did)
        if state is None:
            return None
        tracks = state.sync_buffer.peek_latest(duration_ms=window_ms)
        if not tracks:
            return None
        dv_frags = tracks.get("decoded_video", [])
        if not dv_frags:
            return None
        return getattr(dv_frags[-1].data, "frame", None)

    @staticmethod
    def _wall_to_unix(state: _CameraDeviceState, wall_ms: int) -> int:
        """Convert monotonic wall_ms to unix_ms: unix = wall + epoch_delta."""
        if state.epoch_delta is not None:
            return wall_ms + state.epoch_delta
        return 0

    def _current_source(self, did: str) -> PerceptionDevice:
        """Build source metadata from MiotProxy's in-memory camera cache.

        ``did`` may be a synthetic channel did (``physical:ch{n}``); camera info
        is looked up by the physical did while the synthetic did is kept as the
        device identity (so downstream keying stays per-channel).
        """
        physical_did, _ = split_channel_did(did)
        get_cached_camera = getattr(self._miot_proxy, "get_cached_camera", None)
        camera_info = (
            get_cached_camera(physical_did) if get_cached_camera is not None else None
        )
        if camera_info is None:
            return PerceptionDevice(
                did=did, name=did, device_type="camera", room_name=did
            )
        camera = CameraInfo.model_validate(camera_info.model_dump())
        return PerceptionDevice(
            did=did,
            name=camera.name,
            device_type="camera",
            room_id=camera.room_name,
            room_name=camera.room_name,
            # 已连上的相机即视为可达（直连掐死 OTU 保活令 lan_online 掉 False）。
            online=camera.online and (camera.lan_online or camera.connected),
        )

    def _build_device_data(
        self,
        state: _CameraDeviceState,
        tracks: dict[str, list[StreamFragment]],
        window_start_ms: int = 0,
        window_end_ms: int = 0,
        *,
        dropped_windows: int = 0,
        overflow_count: int = 0,
        max_buffer_depth: int = 0,
        last_overflow_action: str | None = None,
    ) -> DeviceData | None:
        """Build DeviceData from decoded frame track fragments.

        Additionally aggregates per-frame ``decode_latency_ms`` into
        per-window averages (video / audio / combined).  This is the
        packaging point — downstream consumers (collector, pipeline)
        read the precomputed aggregates rather than re-walking frames.
        """
        dv_frags = tracks.get("decoded_video", [])
        da_frags = tracks.get("decoded_audio", [])

        if not dv_frags and not da_frags:
            return None

        video = [f.data for f in dv_frags]
        audio = [f.data for f in da_frags]

        v_count = len(video)
        a_count = len(audio)
        total_frames = v_count + a_count

        def _avg(sum_: float, count: int) -> float:
            return (sum_ / count) if count else 0.0

        # Decode-latency aggregates.
        v_decode_sum = sum(f.decode_latency_ms for f in video)
        a_decode_sum = sum(f.decode_latency_ms for f in audio)
        decode_video_avg = _avg(v_decode_sum, v_count)
        decode_audio_avg = _avg(a_decode_sum, a_count)
        decode_combined = _avg(v_decode_sum + a_decode_sum, total_frames)

        return DeviceData(
            meta=self._current_source(state.did),
            video=video,
            audio=audio,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            window_start_unix_ms=self._wall_to_unix(state, window_start_ms),
            window_end_unix_ms=self._wall_to_unix(state, window_end_ms),
            decode_avg_ms=decode_combined,
            decode_video_avg_ms=decode_video_avg,
            decode_audio_avg_ms=decode_audio_avg,
            dropped_windows=dropped_windows,
            overflow_count=overflow_count,
            max_buffer_depth=max_buffer_depth,
            last_overflow_action=last_overflow_action,
        )

    def get_connected_devices(self) -> dict[str, PerceptionDevice]:
        return {did: self._current_source(did) for did in self._devices}

    def clear_buffers(self) -> None:
        """Clear all camera sync buffers without disconnecting devices."""
        for did, state in self._devices.items():
            state.sync_buffer.clear()
            logger.info("Cleared sync buffer for camera %s", did)

    # ---- Callback factories ----

    @staticmethod
    def _calibrate(state: _CameraDeviceState, stream_ts: int) -> tuple[int, int]:
        """Return (wall_ms, unix_ms) for a frame.

        wall_ms is the actual system monotonic time (immune to stream clock
        drift).  epoch_delta (unix - mono) is locked on first call and used
        to derive unix_ms for display.
        """
        wall_ms = _monotonic_ms()
        if state.epoch_delta is None:
            state.epoch_delta = _unix_ms() - wall_ms
            logger.debug(
                "Clock calibrated for %s: epoch_delta=%d ms",
                state.did,
                state.epoch_delta,
            )
        unix_ms = wall_ms + state.epoch_delta
        return wall_ms, unix_ms

    @staticmethod
    def _compute_decode_latency(
        recv_unix_ms: int,
        decoded_unix_ms: int,
    ) -> float:
        """Compute per-frame ``decode_latency_ms = decoded - recv``.

        Both timestamps are stamped host-locally inside the MIoT SDK
        (``recv_unix_ms`` in ``miot.camera.__on_raw_data`` before
        enqueue, ``decoded_unix_ms`` right after ``av.decode()`` returns
        in ``miot.decoder``), so the delta is a clean host-local measure
        of "queue + FFmpeg decode" with no cross-clock assumptions.

        Guards:
        * ``recv_unix_ms == 0`` means the frame pre-dates the
          instrumented path (e.g. tests or legacy callbacks) — returns
          ``0.0`` to signal "unknown".
        * Negative values (clock skew, reconnect artifacts) are clamped
          to ``0.0``.
        """
        if recv_unix_ms == 0:
            return 0.0
        decode_ms = float(decoded_unix_ms - recv_unix_ms)
        if decode_ms < 0:
            decode_ms = 0.0
        return decode_ms

    def _make_decoded_video_callback(self, did: str, state: _CameraDeviceState):
        """Decoded video frame callback: feeds decoded_video track in sync buffer.

        Receives BGR numpy arrays (already converted from PyAV in decoder thread).

        ``state`` 是回调订阅时刻绑定的设备状态对象。回调只向**这个** state 的
        buffer 写帧：若 ``self._devices[did]`` 已不是它（静默自愈重连换了新
        状态），说明帧来自已失效的流，直接丢弃。这是对 disconnect→reconnect
        竞态的根本防护——unregister 后原生解码线程仍可能有在途帧 dispatch，
        若只按「did 有无 state」判活，旧流的在途帧会混进新 buffer。
        """

        async def _on_decoded_video(
            did_: str,
            frame: NDArray[np.uint8],
            ts: int,
            ch: int,
            recv_unix_ms: int = 0,
            decoded_unix_ms: int = 0,
        ):
            async with get_monitor().track_async(NodeName.CAMERA, "decode_video") as h:
                current = self._devices.get(did)
                if current is not state:
                    # state 已被替换/移除: 帧来自已失效的流。丢弃且不计入 fps_60s,
                    # 避免 stale 回调虚高 SOURCE 节点的处理速率指标。
                    h.skip_rolling()
                    return
                wall_ms, unix_ms = self._calibrate(state, ts)
                decode_latency_ms = self._compute_decode_latency(
                    recv_unix_ms, decoded_unix_ms
                )
                decoded = DecodedVideoFrame(
                    frame=frame,
                    stream_ts=ts,
                    wall_ms=wall_ms,
                    unix_ms=unix_ms,
                    recv_unix_ms=recv_unix_ms,
                    decoded_unix_ms=decoded_unix_ms,
                    decode_latency_ms=decode_latency_ms,
                )
                state.last_video_frame_ms = wall_ms
                state.sync_buffer.put(
                    "decoded_video", decoded, stream_ts=ts, wall_ms=wall_ms
                )

        return _on_decoded_video

    def _make_decoded_audio_callback(self, did: str, state: _CameraDeviceState):
        """Decoded audio frame callback: feeds decoded_audio track in sync buffer.

        Receives PCM numpy arrays (already resampled from PyAV in decoder thread).

        ``state`` 语义同 video 回调: 只向订阅时刻绑定的 state 写帧,state 被替换
        (disconnect→重连) 后丢弃,防 stale 音频帧混入新 buffer。
        """

        async def _on_decoded_audio(
            did_: str,
            frame: NDArray[np.int16],
            ts: int,
            ch: int,
            recv_unix_ms: int = 0,
            decoded_unix_ms: int = 0,
        ):
            async with get_monitor().track_async(NodeName.CAMERA, "decode_audio") as h:
                current = self._devices.get(did)
                if current is not state:
                    # 设备已断开但回调仍在排队的 race: 不计入 fps_60s,
                    # 避免 stale 回调虚高 SOURCE 节点的处理速率指标。
                    h.skip_rolling()
                    return
                wall_ms, unix_ms = self._calibrate(state, ts)
                decode_latency_ms = self._compute_decode_latency(
                    recv_unix_ms, decoded_unix_ms
                )
                decoded = DecodedAudioFrame(
                    frame=frame,
                    stream_ts=ts,
                    wall_ms=wall_ms,
                    unix_ms=unix_ms,
                    recv_unix_ms=recv_unix_ms,
                    decoded_unix_ms=decoded_unix_ms,
                    decode_latency_ms=decode_latency_ms,
                )
                state.sync_buffer.put(
                    "decoded_audio", decoded, stream_ts=ts, wall_ms=wall_ms
                )

        return _on_decoded_audio
