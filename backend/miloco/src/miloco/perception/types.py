"""Shared perception data types — unified source data structures.

DeviceSnapshot is the canonical representation of a single device's
perception data within one time window. All downstream consumers (engine,
tracker, miloco) should work with this structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, Field


@dataclass
class PerceptionDevice:
    """Perception-capable device info."""

    did: str
    name: str
    device_type: str  # "camera" | "speaker"
    online: bool = True
    room_id: str | None = None  # 兼容字段，与 room_name 等价
    room_name: str | None = None


@dataclass
class VideoFrame:
    """Single video frame — raw data + timestamp only."""

    data: NDArray[np.uint8]  # BGR (H, W, 3)
    timestamp: float = 0.0  # ms


@dataclass
class AudioFrame:
    """Single audio chunk — raw data + timestamp only."""

    data: NDArray[np.int16]  # PCM mono
    timestamp: float = 0.0  # ms


@dataclass
class VideoStream:
    """Video stream: frame sequence + stream-level metadata."""

    frames: list[VideoFrame] = field(default_factory=list)
    width: int = 0
    height: int = 0

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def empty(self) -> bool:
        return not self.frames


@dataclass
class AudioStream:
    """Audio stream: chunk sequence + stream-level metadata."""

    frames: list[AudioFrame] = field(default_factory=list)
    sample_rate: int = 16000
    channels: int = 1

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def empty(self) -> bool:
        return not self.frames


@dataclass
class DeviceSnapshot:
    """Single device's perception data for one time window.

    This is the unified data interface consumed by all downstream
    perception tasks (engine pipeline, tracker, etc.).
    """

    device: PerceptionDevice
    start_timestamp: float  # ms, window start
    end_timestamp: float  # ms, window end

    video: VideoStream | None = None
    audio: AudioStream | None = None

    # --- Convenience properties ---

    @property
    def room_name(self) -> str:
        return self.device.room_name or self.device.did

    @property
    def frames(self) -> list[NDArray[np.uint8]]:
        """BGR image list (for engine compatibility)."""
        if not self.video:
            return []
        return [f.data for f in self.video.frames]

    def get_frames_at_fps(self, target_fps: float) -> list[NDArray[np.uint8]]:
        """Return frames resampled to *target_fps*.

        Down-sampling picks the nearest source frame for each target
        timestamp; up-sampling duplicates source frames to fill the
        higher rate.  Timestamps on individual ``VideoFrame`` objects
        are used when available; otherwise frames are assumed to be
        uniformly spaced across ``duration_ms``.
        """
        if not self.video or self.video.empty:
            return []

        src_frames = self.video.frames
        if target_fps <= 0:
            return []

        duration_s = self.duration_ms / 1000.0
        if duration_s <= 0:
            return [f.data for f in src_frames]

        # Build source timestamp list (seconds, relative to start)
        src_ts = self._source_timestamps_s(src_frames, duration_s)

        target_count = max(1, round(target_fps * duration_s))
        target_interval = duration_s / target_count

        result: list[NDArray[np.uint8]] = []
        src_idx = 0
        for i in range(target_count):
            t = (i + 0.5) * target_interval  # centre of each target slot
            # Advance src_idx to the nearest source frame
            while src_idx < len(src_ts) - 1 and abs(src_ts[src_idx + 1] - t) <= abs(src_ts[src_idx] - t):
                src_idx += 1
            result.append(src_frames[src_idx].data)
        return result

    @staticmethod
    def _source_timestamps_s(frames: list["VideoFrame"], duration_s: float) -> list[float]:
        """Return per-frame timestamps in seconds relative to window start."""
        if frames[0].timestamp > 0 or (len(frames) > 1 and frames[-1].timestamp > 0):
            t0 = frames[0].timestamp
            return [(f.timestamp - t0) / 1000.0 for f in frames]
        # Fallback: assume uniform spacing
        n = len(frames)
        if n == 1:
            return [duration_s / 2.0]
        return [i * duration_s / (n - 1) for i in range(n)]

    @property
    def audio_clip(self) -> NDArray[np.int16]:
        """Concatenated PCM samples (for engine compatibility)."""
        if not self.audio or not self.audio.frames:
            return np.array([], dtype=np.int16)
        if len(self.audio.frames) == 1:
            return self.audio.frames[0].data
        return np.concatenate([f.data for f in self.audio.frames])

    @property
    def sample_rate(self) -> int:
        return self.audio.sample_rate if self.audio else 16000

    @property
    def duration_ms(self) -> float:
        return self.end_timestamp - self.start_timestamp

    @property
    def fps(self) -> float:
        n = len(self.video.frames) if self.video else 0
        d = self.duration_ms
        if n > 0 and d > 0:
            return n / (d / 1000)
        return 0.0

    @property
    def frame_size(self) -> tuple[int, int]:
        """(width, height) of source frames."""
        if self.video:
            if self.video.width and self.video.height:
                return (self.video.width, self.video.height)
            if self.video.frames:
                h, w = self.video.frames[0].data.shape[:2]
                return (w, h)
        return (0, 0)

    @property
    def has_video(self) -> bool:
        return self.video is not None and not self.video.empty

    @property
    def has_audio(self) -> bool:
        return self.audio is not None and not self.audio.empty

    @property
    def has_data(self) -> bool:
        return self.has_video or self.has_audio


@dataclass
class BatchedSnapshot:
    """Multi-device batched perception data.

    Groups multiple DeviceSnapshot instances from a single
    collection cycle, supporting multi-room / multi-device queries.
    """

    snapshots: list[DeviceSnapshot] = field(default_factory=list)
    captured_at: float = 0.0  # ms, Unix epoch

    @property
    def empty(self) -> bool:
        return not any(s.has_data for s in self.snapshots)

    @property
    def device_count(self) -> int:
        return len(self.snapshots)

    def by_room(self) -> dict[str, list[DeviceSnapshot]]:
        result: dict[str, list[DeviceSnapshot]] = {}
        for s in self.snapshots:
            result.setdefault(s.room_name, []).append(s)
        return result

    def by_device(self) -> dict[str, DeviceSnapshot]:
        return {s.device.did: s for s in self.snapshots}

    def get_device(self, did: str) -> DeviceSnapshot | None:
        for s in self.snapshots:
            if s.device.did == did:
                return s
        return None


class CaptionEntry(BaseModel):
    """A single scene description for the current window (one per device in a batch)."""

    description: str = Field(..., description="Natural language description of the current scene")
    room_name: str = Field(default="", description="Real room name from device config (set by engine)")
    source_device_ids: list[str] = Field(
        default_factory=list,
        description="Device IDs that contributed to this judgment in the current cycle",
    )
    device_name: str = Field(default="", description="Source camera display name (engine-injected, human-readable; NOT sent as raw did)")
    time_window: str = Field(default="", description="Picture capture window [HH:MM:SS-HH:MM:SS] (engine-injected)")


class MatchedRule(BaseModel):
    """Matched rule."""

    rule_id: str = Field(..., description="Unique identifier of the matched rule (stable key for downstream)")
    rule_name: str = Field(default="", description="Display name copied from the model, e.g. '[task_id] 描述'")
    reason: str = Field(..., description="Explanation of why this rule was matched")
    room_name: str = Field(
        default="",
        description="Real room name from device config (set by engine; not from model)",
    )
    source_device_ids: list[str] = Field(
        default_factory=list,
        description="Device IDs that contributed to this match (set by engine; not from model)",
    )
    device_name: str = Field(default="", description="Human-readable camera name (set by engine; not from model)")
    time_window: str = Field(default="", description="Picture capture window [HH:MM:SS-HH:MM:SS] (engine-injected)")
    caption: str = Field(default="", description="Caption from same-window CaptionEntry (attached before text build)")


class Speech(BaseModel):
    """User voice speech/command."""

    needs_response: bool = Field(..., description="True if the speaker directed this at the assistant")
    speaker: str = Field(..., description="Speaker identity, e.g. '用户', '未知'")
    content: str = Field(..., description="Transcribed speech content")
    is_complete: bool = Field(default=True, description="Whether the utterance is semantically complete")
    room_name: str = Field(default="", description="Real room name from device config (set by engine)")
    source_device_ids: list[str] = Field(
        default_factory=list,
        description="Device IDs that contributed to this judgment in the current cycle",
    )
    device_name: str = Field(default="", description="Source camera display name (engine-injected, human-readable; NOT sent as raw did)")
    time_window: str = Field(default="", description="Picture capture window [HH:MM:SS-HH:MM:SS] (engine-injected)")
    caption: str = Field(default="", description="Caption from same-window CaptionEntry (attached before dispatch)")


class Suggestion(BaseModel):
    """Event suggestion — triggered by danger or anomaly detected in scene."""

    event: str = Field(..., description="Description of the detected anomaly or event")
    action: str = Field(..., description="Recommended action to take")
    urgency: str = Field(
        default="low",
        description="模型自评紧急程度：high | medium | low（缺省退化为 low）",
    )
    id: int | None = Field(
        default=None,
        description="Engine 回写的事件链 id（每房间内单增）；模型禁止输出",
    )
    room_name: str = Field(
        default="",
        description="Real room name from device config (set by engine; not from model)",
    )
    source_device_ids: list[str] = Field(
        default_factory=list,
        description="Device IDs that contributed to this suggestion (set by engine; not from model)",
    )
    device_name: str = Field(default="", description="Source camera display name (engine-injected, human-readable; NOT sent as raw did)")
    time_window: str = Field(default="", description="Picture capture window [HH:MM:SS-HH:MM:SS] (engine-injected)")
    caption: str = Field(default="", description="Caption from same-window CaptionEntry (attached before dispatch)")


# urgency 排名（数字大 = 更紧急）；engine 事件链与调度器条目级优先级共用此单一真值。
URGENCY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def suggestion_intra_priority(suggestions: list[Suggestion]) -> int:
    """一批 suggestion 的「条目级调度优先级」。

    遵循 dispatcher 约定「数字小 = 越优先（越该保留）」：取批内最高 urgency 的负 rank，
    high → -2（最该保留），low / 缺省 → 0（与无内层优先级的类型同默认）。
    """
    return -max((URGENCY_RANK.get(s.urgency, 0) for s in suggestions), default=0)


class RealtimePerceptionResult(BaseModel):
    """Perception engine's output structure for realtime perception."""

    time: str = Field(default="", description="System-injected wall-clock time (HH:MM:SS), echoed back by the engine for logging")
    caption: list[CaptionEntry] = Field(default=[], description="Scene descriptions for each area")
    matched_rules: list[MatchedRule] = Field(default=[], description="Rules matched against current perception data")
    speeches: list[Speech] = Field(default=[], description="Detected voice speeches/commands")
    env_sounds: list[str] = Field(default=[], description="Detected non-speech sound events (one description per device)")
    suggestions: list[Suggestion] = Field(
        default=[], description="Safety, environment, behavior, or anomaly suggestions"
    )
    skipped: bool = Field(default=False, description="True if all rooms were skipped by gate (no meaningful change)")
    error_code: str | None = Field(
        default=None,
        description="Pipeline 异常时填写的错误码;None 表示正常。失败时一同设 skipped=True",
    )
    timing: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Pipeline timing breakdown in ms。'_' 前缀 key 装 per-device 元数据"
            "(如 _device_trace_id_{did}),不参与耗时统计。"
        ),
    )
    usage: dict[str, int] | None = Field(
        default=None,
        description=(
            "Omni API token usage: input_tokens (=prompt_tokens) / output_tokens / "
            "cached_tokens / audio_tokens / video_tokens"
        ),
    )
    device_rule_map: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "本 batch per-device 实际下发的 rule_id 列表(did → rule_ids)。供 client.py "
            "精确推退未命中的 (rule_id, did) 状态机桶,避免 rule 绑 cam_A 时被 cam_B 帧 "
            "错误推退。空 dict 表示 OmniError 兜底/无下发。"
        ),
    )


class OnDemandPerceptionResult(BaseModel):
    """Perception engine's output structure for on-demand perception."""

    answer: str = Field(..., description="Answer to the on-demand query")
