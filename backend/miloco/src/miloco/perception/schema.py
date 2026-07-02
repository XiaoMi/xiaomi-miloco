"""
Perception module data models.

Includes both:
- dataclasses for internal pipeline data flow (multimodal packets with source metadata)
- Pydantic models for API request/response
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, Field

from miloco.perception.types import BatchedSnapshot, DeviceSnapshot, PerceptionDevice
from miloco.perception.utils import snapshot_from_arrays

logger = logging.getLogger(__name__)

# ---- Internal pipeline data models (dataclass) ----


@dataclass
class DecodedVideoFrame:
    """Decoded video frame — carries BGR numpy array.

    Frames are converted from PyAV VideoFrame to numpy in the decoder thread
    to avoid cross-thread FFmpeg access.

    ``decode_latency_ms = decoded_unix_ms - recv_unix_ms`` — the time the
    MIoT decoder thread spent turning the encoded packet into a VideoFrame
    (FFmpeg decode + any queueing in the decoder ring buffer between
    arrival and processing).  Measured host-locally by the MIoT SDK:
    ``recv_unix_ms`` is stamped in ``miot.camera.__on_raw_data`` before
    enqueue, ``decoded_unix_ms`` is stamped right after ``av.decode()``
    returns in ``miot.decoder``.

    Defaults to 0.0 when the frame pre-dates the instrumented path (tests
    / legacy callers) or when the computed delta is negative (clamped).
    """

    frame: NDArray[np.uint8]  # BGR uint8 numpy array (H, W, 3)
    stream_ts: int  # ms, device-relative stream timestamp
    wall_ms: int = 0  # ms, monotonic wall-clock timestamp (cross-device alignment)
    unix_ms: int = 0  # ms, Unix epoch timestamp (for downstream display/storage)
    # Host unix ms at the moment the raw encoded packet arrived at the host.
    recv_unix_ms: int = 0
    # Host unix ms at the moment the decoder finished turning the packet
    # into a VideoFrame (right after av.decode() returns).
    decoded_unix_ms: int = 0
    decode_latency_ms: float = 0.0


@dataclass
class DecodedAudioFrame:
    """Decoded audio frame — carries PCM numpy array.

    Audio is resampled from PyAV AudioFrame to s16/mono/16kHz numpy in the
    decoder thread to avoid cross-thread FFmpeg access.
    """

    frame: NDArray[np.int16]  # PCM int16 numpy array (mono, 16kHz)
    stream_ts: int  # ms, device-relative stream timestamp
    wall_ms: int = 0  # ms, monotonic wall-clock timestamp (cross-device alignment)
    unix_ms: int = 0  # ms, Unix epoch timestamp (for downstream display/storage)
    recv_unix_ms: int = 0
    decoded_unix_ms: int = 0
    decode_latency_ms: float = 0.0


@dataclass
class DeviceData:
    """Single device's multimodal data for one time window.

    Contains decoded PyAV frames for Omni inference / image processing / ASR.

    ``decode_avg_ms`` / ``decode_video_avg_ms`` / ``decode_audio_avg_ms``
    carry the per-window average decode latency (see
    :class:`DecodedVideoFrame.decode_latency_ms`).  ``decode_avg_ms`` is the
    frame-count-weighted mean across video and audio; the two side channels
    retain the per-modality mean for diagnostics.
    """

    meta: PerceptionDevice

    # Decoded frames
    video: list[DecodedVideoFrame] = field(default_factory=list)
    audio: list[DecodedAudioFrame] = field(default_factory=list)

    # Wall-clock time range of the collection window (ms, monotonic)
    window_start_ms: int = 0
    window_end_ms: int = 0
    # Unix epoch time range of the collection window (ms)
    window_start_unix_ms: int = 0
    window_end_unix_ms: int = 0

    # Per-window decode-latency aggregates (ms). All 0.0 when no frames.
    decode_avg_ms: float = 0.0
    decode_video_avg_ms: float = 0.0
    decode_audio_avg_ms: float = 0.0

    # 背压统计:本 cycle 期间(自上次 drain 起)该设备 stream_buffer 的累计结果。
    # dropped_windows 含两类:put 侧 full_action 硬丢 + drain 侧取最新时跳过的旧窗口
    # (后者数据仍在 _drained 可 peek,action 标 "skip")。
    dropped_windows: int = 0
    overflow_count: int = 0  # 仅 put 侧 full_action 触发次数,不含 drain skip
    max_buffer_depth: int = 0
    last_overflow_action: str | None = None  # "clear" | "drop" | "skip" | None

    @property
    def has_data(self) -> bool:
        return bool(self.video or self.audio)

    # ---- Convenience methods for downstream consumers ----

    def to_snapshot(
        self,
    ) -> DeviceSnapshot | None:
        frames = self.get_bgr_frames()
        audio_clip = self.get_pcm_ndarray(sample_rate=16000)

        if not frames and len(audio_clip) == 0:
            return None

        start_ts = (
            float(self.window_start_unix_ms)
            if self.window_start_unix_ms
            else float(self.window_start_ms)
        )

        end_ts = (
            float(self.window_end_unix_ms)
            if self.window_end_unix_ms
            else float(self.window_end_ms)
        )

        return snapshot_from_arrays(
            self.meta,
            frames=frames,
            audio=audio_clip,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
        )

    def select_representative_frames(
        self, max_frames: int | None = None
    ) -> list[DecodedVideoFrame]:
        """Pick representative frames for preview/encoding. Currently 均匀采样。

        集中策略，避免 ``get_jpeg_frames`` 等下游路径各自实现一遍。
        """
        frames = self.video
        if not frames or not max_frames or len(frames) <= max_frames:
            return list(frames)
        step = len(frames) / max_frames
        return [frames[int(i * step)] for i in range(max_frames)]

    def get_jpeg_frames(
        self, *, max_frames: int | None = None, quality: int = 90
    ) -> list[bytes]:
        """Convert BGR numpy frames to JPEG bytes on demand.

        Args:
            max_frames: If set, uniformly sample this many frames.
            quality: JPEG compression quality (1-100).

        Returns:
            List of JPEG image bytes.
        """
        import cv2

        frames = self.select_representative_frames(max_frames)
        if not frames:
            return []

        jpeg_list: list[bytes] = []
        for df in frames:
            try:
                ok, buf = cv2.imencode(
                    ".jpg", df.frame, [cv2.IMWRITE_JPEG_QUALITY, quality]
                )
                if ok:
                    jpeg_list.append(buf.tobytes())
            except Exception as e:
                logger.warning("Failed to convert frame to JPEG: %s", e)
        return jpeg_list

    def get_bgr_frames(
        self, *, max_frames: int | None = None
    ) -> list[NDArray[np.uint8]]:
        """Return BGR numpy arrays (H, W, 3).

        Frames are already BGR numpy arrays (converted in the decoder thread),
        so this is just a passthrough with optional downsampling.

        Args:
            max_frames: If set, uniformly sample this many frames.

        Returns:
            List of BGR uint8 numpy arrays, each shaped (H, W, 3).
        """
        frames = self.video
        if not frames:
            return []

        if max_frames and len(frames) > max_frames:
            step = len(frames) / max_frames
            frames = [frames[int(i * step)] for i in range(max_frames)]

        return [df.frame for df in frames]

    def get_pcm_ndarray(self, *, sample_rate: int = 16000) -> NDArray[np.int16]:
        """Return PCM int16 numpy array (mono).

        Audio frames are already resampled to s16/mono/16kHz in the decoder
        thread, so this just concatenates them.

        Args:
            sample_rate: Target sample rate in Hz (ignored — decoder already
                resamples to 16kHz).

        Returns:
            1-D int16 numpy array. Empty array if no audio data.
        """
        if not self.audio:
            return np.array([], dtype=np.int16)

        pcm_parts: list[NDArray[np.int16]] = []
        for da in self.audio:
            pcm_parts.append(da.frame.view(np.int16))

        if not pcm_parts:
            return np.array([], dtype=np.int16)
        return np.concatenate(pcm_parts)

    def get_pcm_audio(self, *, sample_rate: int = 16000) -> bytes:
        """Return raw PCM bytes (signed 16-bit, mono).

        Args:
            sample_rate: Target sample rate in Hz (ignored — decoder already
                resamples to 16kHz).

        Returns:
            Raw PCM bytes (signed 16-bit, mono).
        """
        return self.get_pcm_ndarray(sample_rate=sample_rate).tobytes()


@dataclass
class PerceptionBatch:
    """All devices' multimodal data collected in a single perception cycle.

    Structure:
        devices[did] = DeviceData(meta, video, audio)
    """

    devices: dict[str, DeviceData] = field(default_factory=dict)
    captured_at: int = field(default_factory=lambda: int(time.time() * 1000))

    # Batch-level decode-latency aggregates, computed once at pack time by
    # the collector (see MultimodalCollector.collect_batch).  Frame-count-
    # weighted means across all devices.  Kept here so the pipeline does
    # not need to re-walk per-device frames.
    decode_avg_ms: float = 0.0
    decode_video_avg_ms: float = 0.0
    decode_audio_avg_ms: float = 0.0
    # Frame counts that produced the video/audio splits above — lets the
    # pipeline drop keys from timing_detail when the modality was absent.
    video_frame_count: int = 0
    audio_frame_count: int = 0
    # 窗口内最早一帧到达 host 的 unix ms(端到端起点)。无 recv_unix_ms 数据时为 None。
    window_first_frame_recv_ms: int | None = None

    @property
    def empty(self) -> bool:
        return not any(d.has_data for d in self.devices.values())

    @property
    def device_count(self) -> int:
        return len(self.devices)

    @property
    def start_timestamp(self) -> int:
        return min(d.window_start_unix_ms for d in self.devices.values())

    @property
    def end_timestamp(self) -> int:
        return max(d.window_end_unix_ms for d in self.devices.values())

    def get_device(self, did: str) -> DeviceData | None:
        return self.devices.get(did)

    def to_batched_snapshot(self) -> BatchedSnapshot | None:
        snapshots = list(
            filter(
                lambda x: x is not None,
                [device.to_snapshot() for device in self.devices.values()],
            )
        )
        if not snapshots:
            return None
        return BatchedSnapshot(
            snapshots=snapshots,
            captured_at=self.captured_at,
        )


@dataclass
class PerceptionLatency:
    """Full perception cycle latency breakdown (all values in ms).

    Timing chain:
        frame source → decode → collect → pipeline(= gate + identity + omni)
                     → log → overhead

    RTF (Real-Time Factor) = processing_time / media_duration.
    RTF < 1 means the system processes faster than real-time.
    """

    # Delay — time from window end to processing start / finish
    in_delay_ms: float = 0.0
    out_delay_ms: float = 0.0

    # Media decode layer — per-frame average across the current window.
    # Frame-count-weighted mean of video + audio latency; see
    # DecodedVideoFrame for the per-frame definition.
    decode_ms: float = 0.0

    # Processor layer — cycle-level
    collect_ms: float = 0.0
    log_ms: float = 0.0
    cycle_total_ms: float = 0.0

    # Engine pipeline layer — aggregated across all devices/rooms
    convert_ms: float = 0.0  # PyAV → numpy conversion
    gate_ms: float = 0.0  # sum of all devices' gate time
    identity_ms: float = 0.0  # sum of all devices' identity (tracking + recognition) time
    omni_ms: float = 0.0  # sum of all rooms' omni (LLM) time
    pipeline_total_ms: float = 0.0  # total engine pipeline wall-clock time

    # Media window duration (ms) — max span across all devices
    window_duration_ms: float = 0.0

    # Stream-level lag: window_end - earliest frame recv (ms). 0 when unknown.
    stream_lag_ms: float = 0.0

    # Meta
    device_count: int = 0
    skipped: bool = False
    timestamp: float = 0.0

    # Raw per-room/per-device timing from engine (e.g. "room/gate_cam1_ms")
    timing_detail: dict[str, float] | None = None

    @property
    def rtf(self) -> float:
        """Overall RTF: cycle_total / window_duration. <1 = faster than real-time."""
        if self.window_duration_ms <= 0:
            return 0.0
        return self.cycle_total_ms / self.window_duration_ms

    @property
    def rtf_pipeline(self) -> float:
        """Pipeline-only RTF (convert+gate+identity+omni) / window_duration."""
        if self.window_duration_ms <= 0:
            return 0.0
        return self.pipeline_total_ms / self.window_duration_ms

    @property
    def rtf_e2e(self) -> float:
        """(cycle_total + in_delay) / window — 包括等窗口攒齐的等待。"""
        if self.window_duration_ms <= 0:
            return 0.0
        return (self.cycle_total_ms + self.in_delay_ms) / self.window_duration_ms

    @property
    def rtf_stream_e2e(self) -> float:
        """(cycle_total + in_delay + stream_lag) / window — 端到端含拉流。"""
        if self.window_duration_ms <= 0:
            return 0.0
        return (
            self.cycle_total_ms + self.in_delay_ms + self.stream_lag_ms
        ) / self.window_duration_ms

    @property
    def rtf_omni(self) -> float:
        """omni_ms / window — LLM 主调用单独的 RTF 切片。"""
        if self.window_duration_ms <= 0:
            return 0.0
        return self.omni_ms / self.window_duration_ms

    def to_dict(self) -> dict:
        d = {
            "in_delay_ms": round(self.in_delay_ms, 1),
            "out_delay_ms": round(self.out_delay_ms, 1),
            "decode_ms": round(self.decode_ms, 1),
            "collect_ms": round(self.collect_ms, 1),
            "log_ms": round(self.log_ms, 1),
            "cycle_total_ms": round(self.cycle_total_ms, 1),
            "convert_ms": round(self.convert_ms, 1),
            "gate_ms": round(self.gate_ms, 1),
            "identity_ms": round(self.identity_ms, 1),
            "omni_ms": round(self.omni_ms, 1),
            "pipeline_total_ms": round(self.pipeline_total_ms, 1),
            "window_duration_ms": round(self.window_duration_ms, 1),
            "stream_lag_ms": round(self.stream_lag_ms, 1),
            "rtf": round(self.rtf, 3),
            "rtf_pipeline": round(self.rtf_pipeline, 3),
            "rtf_e2e": round(self.rtf_e2e, 3),
            "rtf_stream_e2e": round(self.rtf_stream_e2e, 3),
            "rtf_omni": round(self.rtf_omni, 3),
            "device_count": self.device_count,
            "skipped": self.skipped,
            "timestamp": self.timestamp,
        }
        if self.timing_detail:
            d["timing_detail"] = {k: round(v, 1) for k, v in self.timing_detail.items()}
        return d


# ---- API models (Pydantic) ----


class EngineState(BaseModel):
    """Perception engine readiness state."""

    ready: bool = False
    status: str = "not_initialized"
    message: str = ""


class PerceptionEngineStatus(BaseModel):
    """Realtime perception engine status."""

    running: bool = False
    engine: EngineState = Field(default_factory=EngineState)
    interval_seconds: int = 3
    today_inference_count: int = 0
    active_sources: list[dict] = Field(
        default_factory=list,
        description="Active devices: [{did, name, device_type, modalities}]",
    )
    last_latency: dict | None = Field(
        default=None,
        description="Last cycle latency breakdown in ms",
    )


class OnDemandPerceptionRequest(BaseModel):
    """On-demand perception query request."""

    sources: list[str] = Field(..., description="Device did list", min_length=1)
    query: str = Field(..., description="Natural language question", min_length=1)


class OnDemandPerceptionResultItem(BaseModel):
    """Single source result for active perception."""

    answer: str = Field(..., description="Perception answer")
    timestamp: str = Field(..., description="ISO 8601 timestamp")


class PerceptionLogEntry(BaseModel):
    """Perception log entry stored in DB."""

    id: str = Field(..., description="UUID")
    timestamp: int = Field(..., description="Millisecond Unix timestamp")
    descriptions: dict = Field(
        ..., description="Fused multi-source descriptions keyed by source name"
    )


class MeaningfulEvent(BaseModel):
    """有意义事件(family-ui Activity tab 展示用).

    一次推理 = 一行 event;同窗口 N 摄像头合并 1 行,device_ids JSON 记录参与摄像头.
    `text` 字段与 agent webhook 收到的同一段聚合文本(B2 单源真值).
    响应不含 payload_json / schema_version / created_at(后端复盘用,API 不返).
    """

    event_id: str = Field(..., description="UUID,DB 主键")
    timestamp: int = Field(..., description="Millisecond Unix timestamp,感知窗口 start_ms")
    text: str = Field(..., description="聚合 agent 视图文本(与 agent webhook 一字不差)")
    has_rule_hit: bool = Field(default=False, description="是否含规则命中")
    has_suggestion: bool = Field(default=False, description="是否含主动建议")
    has_asr: bool = Field(default=False, description="是否含 needs_response ASR")
    snapshot_count: int = Field(
        default=0,
        description="成功落 clip 的 device 数(0 ~ len(device_ids));字段名沿用历史,语义现已是 device 数而非帧数",
    )
    device_ids: list[str] = Field(
        default_factory=list,
        description="参与本次推理的 device_id 列表(对齐实际可落盘 frames)",
    )
    rule_names: dict[str, str] = Field(
        default_factory=dict,
        description="rule_id → rule_name 映射;UI 渲染规则提醒文本时把 [rule_id] 替换成 rule_name",
    )
    clip_kind: Literal["mp4", "m4a"] | None = Field(
        default=None,
        description=(
            "Container of the persisted clip,服务端 stat 落盘文件后缀计算:"
            "'mp4' = H264+AAC video container(omni video 路径产物);"
            "'m4a' = AAC-only audio container(omni audio-only 路径产物);"
            "None = no clip on disk(metadata-only / cleanup 已清).多 device 共识下"
            "全 device kind 一致(见 prompt_builder._is_audio_only),取第一个 device 即可."
        ),
    )


class EventListResponse(BaseModel):
    """GET /api/events 响应 data 字段."""

    events: list[MeaningfulEvent] = Field(
        default_factory=list,
        description="按 timestamp DESC 排,前端 length < limit 时停止翻页",
    )
