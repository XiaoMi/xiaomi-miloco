"""
Time-range stream buffer for perception pipeline.

**MultiTrackSyncBuffer** — multi-track time-windowed aggregation buffer.
Groups fragments from named tracks (video, audio, …) into fixed-size time
windows. A window becomes *ready* when all registered tracks have data or
the window expires (newer data arrives beyond the window boundary).
Ready windows are queued for consumption.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class ReadyWindow:
    """A consumed window's tracks with its wall-clock time boundaries."""

    tracks: dict[str, list[StreamFragment]]
    start_ms: int  # wall-clock window start (inclusive)
    end_ms: int  # wall-clock window end (exclusive)


@dataclass
class StreamFragment(Generic[T]):
    """A single stream fragment with dual timestamps."""

    data: T
    stream_ts: int  # ms, device-relative timestamp (for intra-device A/V sync)
    wall_ms: int  # ms, monotonic wall-clock timestamp (for cross-device alignment)


@dataclass
class _TimeWindow:
    """A single time window holding fragments from multiple tracks."""

    window_start_ms: int  # inclusive, ms
    window_end_ms: int  # exclusive, ms
    tracks: dict[str, list[StreamFragment]] = field(default_factory=dict)
    tracks_seen: set[str] = field(default_factory=set)


class MultiTrackSyncBuffer:
    """Time-windowed multi-track aggregation buffer.

    All tracks of a device share a public time window. When the window is
    *ready* (all tracks have data, or the window expires because newer data
    arrives), the window's data is queued for consumption.

    Args:
        track_names: Names of tracks to synchronize (e.g. ``["video", "audio"]``).
        window_ms: Window size in milliseconds (= processing cycle).
        max_windows: Max ready windows before backpressure kicks in.
        on_window_ready: Optional callback invoked (outside the lock) when a
            new window becomes ready. Typically sets an ``asyncio.Event``.
        buffer_full_action: What to do when ready_queue > max_windows.
            ``"drop"`` — drop the oldest ready window (FIFO).
            ``"clear"`` — clear entire buffer, keep only current fragment.
            ``"keep"`` — no backpressure, accumulate until manual clear.
    """

    def __init__(
        self,
        track_names: list[str],
        window_ms: int = 3000,
        max_windows: int = 5,
        on_window_ready: Callable[[], None] | None = None,
        window_settle_ms: int = 500,
        buffer_full_action: str = "keep",
    ):
        if not track_names:
            raise ValueError("track_names must not be empty")
        if window_ms <= 0:
            raise ValueError("window_ms must be positive")

        self._track_names: frozenset[str] = frozenset(track_names)
        self._window_ms = window_ms
        self._window_settle_ms = window_settle_ms
        self._max_windows = max_windows
        self._on_window_ready = on_window_ready
        self._buffer_full_action = buffer_full_action

        # 丢包统计:put 路径累加,consume_drop_stats() 拉取增量后清零。
        # 全部在 self._lock 内读写。
        self._dropped_since_drain: int = 0
        self._overflow_count_since_drain: int = 0
        self._max_depth_since_drain: int = 0
        self._last_overflow_action: str | None = None

        # Active (not yet ready) windows, keyed by window window_start_ms
        self._windows: dict[int, _TimeWindow] = {}
        # Ready window window_start_ms, in chronological order
        self._ready_queue: deque[int] = deque()
        self._ready_keys: set[int] = set()
        # Recently drained windows kept for peek (active perception)
        self._drained: deque[_TimeWindow] = deque()
        # Per-track first window keys — each track's first window is partial
        # (stream starts mid-window) and must be skipped.  Tracking per-track
        # handles the case where tracks start at different times (e.g. audio
        # arrives hundreds of ms before video).
        self._first_window_keys: dict[str, int | None] = {t: None for t in track_names}
        # Tracks that have already recorded their first window key.
        # Prevents re-setting after the key is cleared in _expire_old_windows.
        self._tracks_initialized: set[str] = set()

        self._lock = threading.Lock()

    # ---- Window helpers ----

    def _window_key(self, stream_ts: int) -> int:
        """Compute the window window_start_ms for a given stream timestamp."""
        return (stream_ts // self._window_ms) * self._window_ms

    def _get_or_create_window(self, key: int) -> _TimeWindow:
        """Get or create a time window (caller holds lock)."""
        win = self._windows.get(key)
        if win is None:
            win = _TimeWindow(
                window_start_ms=key,
                window_end_ms=key + self._window_ms,
            )
            self._windows[key] = win
        return win

    def _mark_ready(self, key: int) -> None:
        """Move a window to the ready queue (caller holds lock)."""
        if key not in self._ready_keys:
            self._ready_keys.add(key)
            self._ready_queue.append(key)

    def _expire_old_windows(self, current_wall_ms: int) -> None:
        """Mark closed windows as ready, with a settle grace period.

        A window whose time period has ended becomes ready when EITHER:
        - All registered tracks have contributed data (early ready), OR
        - The settle grace period has elapsed (timeout ready), giving the
          slower track time to catch up.

        This ensures audio/video tracks are aligned within each window.
        """
        # Collect all per-track first-window keys that still need skipping
        skip_keys = {k for k in self._first_window_keys.values() if k is not None}

        for wkey in sorted(self._windows):
            window_end = wkey + self._window_ms
            if window_end > current_wall_ms:
                break  # window still active, and all later ones too
            if wkey in self._ready_keys:
                continue

            # Skip windows that are any track's first (partial) window.
            # This handles inter-track startup delay: if audio starts in
            # window N and video starts in window N+1, both N and N+1 are
            # skipped so the first consumed window has full data on all tracks.
            if wkey in skip_keys:
                for t in self._first_window_keys:
                    if self._first_window_keys[t] == wkey:
                        self._first_window_keys[t] = None
                self._windows.pop(wkey, None)
                continue

            win = self._windows[wkey]
            all_tracks_present = win.tracks_seen >= self._track_names
            settled = current_wall_ms >= window_end + self._window_settle_ms

            if all_tracks_present or settled:
                self._mark_ready(wkey)

    # ---- Write ----

    def put(self, track: str, data: T, stream_ts: int, wall_ms: int) -> None:
        """Append a fragment to a track within the appropriate time window.

        Args:
            track: Track name (must be one of ``track_names``).
            data: Raw data payload.
            stream_ts: Stream-side timestamp in milliseconds (device-relative).
            wall_ms: Monotonic wall-clock timestamp in milliseconds
                (calibrated from stream_ts, used for windowing).
        """
        if track not in self._track_names:
            raise ValueError(f"Unknown track: {track!r}")

        should_signal = False
        with self._lock:
            key = self._window_key(wall_ms)

            # Record each track's first window key (it will be partial)
            if track not in self._tracks_initialized:
                self._tracks_initialized.add(track)
                self._first_window_keys[track] = key

            win = self._get_or_create_window(key)

            win.tracks.setdefault(track, []).append(
                StreamFragment(data=data, stream_ts=stream_ts, wall_ms=wall_ms)
            )
            win.tracks_seen.add(track)

            # Expire older windows — uses actual wall_ms (not aligned key)
            # so the settle grace period is measured precisely.
            old_ready_count = len(self._ready_queue)
            self._expire_old_windows(wall_ms)
            should_signal = len(self._ready_queue) > old_ready_count

            # Backpressure when ready_queue exceeds max_windows
            if (
                self._buffer_full_action != "keep"
                and len(self._ready_queue) > self._max_windows
            ):
                ready_before = len(self._ready_queue)
                active_before = len(self._windows)
                if ready_before > self._max_depth_since_drain:
                    self._max_depth_since_drain = ready_before
                self._overflow_count_since_drain += 1

                if self._buffer_full_action == "clear":
                    dropped = ready_before + active_before
                    self._windows.clear()
                    self._ready_queue.clear()
                    self._ready_keys.clear()
                    self._drained.clear()
                    should_signal = False
                    win = self._get_or_create_window(key)
                    win.tracks.setdefault(track, []).append(
                        StreamFragment(data=data, stream_ts=stream_ts, wall_ms=wall_ms)
                    )
                    win.tracks_seen.add(track)
                    self._dropped_since_drain += dropped
                    self._last_overflow_action = "clear"
                    logger.warning(
                        "[stream_buffer] overflow → clear: ready=%d active=%d max=%d dropped=%d",
                        ready_before, active_before, self._max_windows, dropped,
                    )
                elif self._buffer_full_action == "drop":
                    dropped = 0
                    while len(self._ready_queue) > self._max_windows:
                        oldest_key = self._ready_queue.popleft()
                        self._ready_keys.discard(oldest_key)
                        self._windows.pop(oldest_key, None)
                        dropped += 1
                    self._dropped_since_drain += dropped
                    self._last_overflow_action = "drop"
                    logger.warning(
                        "[stream_buffer] overflow → drop: ready=%d active=%d max=%d dropped=%d",
                        ready_before, active_before, self._max_windows, dropped,
                    )

        if should_signal and self._on_window_ready:
            self._on_window_ready()

    # ---- Consume (drain ready windows) ----

    def drain_ready(self) -> ReadyWindow | None:
        """Drain all ready windows for realtime inference, returning only the newest.

        实时感知只关心"当前画面"。若把积压的旧窗口逐个送推理,结果会越追越旧
        (滞后随积压单调增长),还会在 full_action 触发时连续丢失一整段。因此每次
        drain 直接取**最新**窗口送推理,中间更旧的 ready 窗口跳过——但仍移进
        _drained 供 active-perception 的 peek 复用(数据不丢失,只是不进实时推理)。

        Returns:
            A ``ReadyWindow`` for the newest ready window, or ``None`` if no
            ready window is available.
        """
        with self._lock:
            if not self._ready_queue:
                return None

            ordered_keys = sorted(self._ready_queue)
            newest_key = ordered_keys[-1]
            newest_win: _TimeWindow | None = None
            drained_count = 0
            # 排空整个 ready_queue:全部移进 _drained 留给 peek,但只有最新一个
            # 作为返回值送实时推理。按 key(=window_start_ms)升序入队保证时序。
            for key in ordered_keys:
                win = self._windows.pop(key, None)
                if win is None:
                    continue
                self._drained.append(win)
                drained_count += 1
                if key == newest_key:
                    newest_win = win
            self._ready_queue.clear()
            self._ready_keys.clear()

            # 实时推理只取最新一个,其余更旧的窗口被"跳过"(数据仍在 _drained 可 peek,
            # 但未送推理)。计入丢弃统计并标 action="skip",让 dashboard 的背压指标
            # 反映真实积压——否则 drain 侧的跳过对监控完全不可见,背压会显得偏轻。
            if newest_win is not None and drained_count > 1:
                self._dropped_since_drain += drained_count - 1
                self._last_overflow_action = "skip"
                if drained_count > self._max_depth_since_drain:
                    self._max_depth_since_drain = drained_count

            # _drained 是「已 drain 供 peek 复用」的回看缓存,和 full_action 的 ready 队列
            # 背压语义无关——任何 action 都必须裁到 max_windows。此前只在 != "keep" 时裁,
            # 导致 keep 模式下 _drained 永不收敛、每 drain 一窗就永久堆一份整窗解码帧
            # (issue #429 §4.1:帧级无界泄漏,实测 120 窗→1.1GB)。peek_latest 只取最近
            # duration_ms,max_windows 窗足够,收窄不影响回看语义。
            while len(self._drained) > self._max_windows:
                self._drained.popleft()

            if newest_win is None:
                return None

            return ReadyWindow(
                tracks=newest_win.tracks,
                start_ms=newest_win.window_start_ms,
                end_ms=newest_win.window_end_ms,
            )

    # ---- Peek (non-consuming) ----

    def peek_latest(
        self, duration_ms: int | None = None
    ) -> dict[str, list[StreamFragment]] | None:
        """Peek the most recent ``duration_ms`` of data without consuming.

        Each track is sliced independently: its own newest timestamp anchors
        a ``[newest - duration_ms, newest]`` range.  This avoids A/V gaps
        caused by a single global cutoff biased toward the faster track.

        Args:
            duration_ms: Target time range in milliseconds. Defaults to
                ``window_ms`` if not provided.

        Returns:
            ``{track_name: [fragments]}`` from the recent time range, or ``None``
            if no windows exist.
        """
        target_ms = duration_ms if duration_ms is not None else self._window_ms

        with self._lock:
            if not self._windows:
                return None

            # Collect all windows: drained (oldest first) + active
            all_wins = list(self._drained) + [
                self._windows[k] for k in sorted(self._windows)
            ]
            if not all_wins:
                return None

            # Find newest wall_ms PER TRACK so each track gets its own
            # duration_ms slice.  A single global cutoff biases toward the
            # track with the latest timestamp, causing the other track to
            # be shorter and creating an A/V gap.
            track_newest: dict[str, int] = {}
            for win in all_wins:
                for track, frags in win.tracks.items():
                    if frags:
                        last_ts = frags[-1].wall_ms
                        if last_ts > track_newest.get(track, 0):
                            track_newest[track] = last_ts
            if not track_newest:
                return None

            track_cutoff = {t: newest - target_ms for t, newest in track_newest.items()}

            merged: dict[str, list[StreamFragment]] = {}
            for win in all_wins:
                for track, frags in win.tracks.items():
                    cutoff = track_cutoff.get(track, 0)
                    for f in frags:
                        if f.wall_ms >= cutoff:
                            merged.setdefault(track, []).append(f)
            return merged

    # ---- Introspection ----

    @property
    def ready_count(self) -> int:
        """Number of ready windows waiting to be consumed."""
        with self._lock:
            return len(self._ready_queue)

    @property
    def window_count(self) -> int:
        """Total number of active windows (ready + incomplete)."""
        with self._lock:
            return len(self._windows)

    def clear(self) -> None:
        """Remove all windows and reset state."""
        with self._lock:
            self._windows.clear()
            self._ready_queue.clear()
            self._ready_keys.clear()
            self._drained.clear()
            self._first_window_keys = {t: None for t in self._track_names}
            self._tracks_initialized.clear()

    def consume_drop_stats(self) -> tuple[int, int, int, str | None]:
        """拉取自上次调用以来累计的丢包统计并清零。

        返回 ``(dropped_windows, overflow_count, max_buffer_depth,
        last_overflow_action)``。供消费侧(collector)在每次 drain 后调用,
        增量塞进 DeviceData 跟 cycle 一起回传到 trace。

        max_buffer_depth 取 overflow 触发瞬间的 ready_queue 长度峰值,
        反映 omni 慢导致 backlog 堆积的最坏情况。
        """
        with self._lock:
            stats = (
                self._dropped_since_drain,
                self._overflow_count_since_drain,
                self._max_depth_since_drain,
                self._last_overflow_action,
            )
            self._dropped_since_drain = 0
            self._overflow_count_since_drain = 0
            self._max_depth_since_drain = 0
            self._last_overflow_action = None
            return stats
