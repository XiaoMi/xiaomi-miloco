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
        result: dict[str, PerceptionDevice] = {}
        for did in active:
            camera_info = CameraInfo.model_validate(cams[did].model_dump())
            online = camera_info.online and camera_info.lan_online
            # 多通道相机（双摄等）：每条通道展成独立感知单元，合成 did = ``did:ch{n}``。
            # 单通道保留裸 did（零回归）。相机名不带通道标签，通道标签是前端关注点。
            channel_count = camera_info.channel_count or 1
            channel_dids = (
                [f"{did}{_CHANNEL_SEP}{ch}" for ch in range(channel_count)]
                if channel_count > 1
                else [did]
            )
            for channel_did in channel_dids:
                result[channel_did] = PerceptionDevice(
                    did=channel_did,
                    name=camera_info.name,
                    device_type="camera",
                    room_id=camera_info.room_name,
                    room_name=camera_info.room_name,
                    online=online,
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
            try:
                expected = await self.discover_devices(
                    online_only=True, require_lan=False
                )
                now_ms = _monotonic_ms()
                if len(expected) > len(self._devices) and (
                    now_ms - self._last_ondemand_refresh_ms
                    >= _ONDEMAND_REFRESH_MIN_INTERVAL_MS
                ):
                    self._last_ondemand_refresh_ms = now_ms
                    await self._miot_proxy.refresh_cameras()
            except Exception as e:  # noqa: BLE001
                logger.warning("On-demand camera manager refresh failed: %s", e)
        await super().sync_devices(all_devices)

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
                physical_did, channel, self._make_decoded_video_callback(did)
            )
            state.decoded_video_reg_id = reg_id
        except Exception as e:
            logger.error("Failed to subscribe decoded video for %s: %s", did, e)

        # Subscribe decoded audio frame stream (multi-reg)
        try:
            reg_id = await self._miot_proxy.start_camera_decode_audio_stream(
                physical_did, channel, self._make_decoded_audio_callback(did)
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
            online=camera.online and camera.lan_online,
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

    def _make_decoded_video_callback(self, did: str):
        """Decoded video frame callback: feeds decoded_video track in sync buffer.

        Receives BGR numpy arrays (already converted from PyAV in decoder thread).
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
                state = self._devices.get(did)
                if not state:
                    # 设备已断开但回调仍在排队的 race: 不计入 fps_60s,
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
                state.sync_buffer.put(
                    "decoded_video", decoded, stream_ts=ts, wall_ms=wall_ms
                )

        return _on_decoded_video

    def _make_decoded_audio_callback(self, did: str):
        """Decoded audio frame callback: feeds decoded_audio track in sync buffer.

        Receives PCM numpy arrays (already resampled from PyAV in decoder thread).
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
                state = self._devices.get(did)
                if not state:
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
