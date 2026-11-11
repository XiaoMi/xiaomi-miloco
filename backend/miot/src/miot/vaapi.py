# Copyright (C) 2026 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""Opt-in persistent VA-API decoder for H.264/HEVC without frame reordering.

NUT carries device PTS across the process boundary; a bounded metadata table
recovers the corresponding host receive timestamp. No compressed packet is
sampled. Frames are selected on the device before downloading to system RAM.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
from collections import Counter, OrderedDict
from fractions import Fraction
from pathlib import Path

import av

from .flow import LatestFrameDelivery
from .types import MIoTCameraCodec


def _parameter_sets(payload, hevc=False):
    result = {}
    for nal in re.split(b"\x00\x00(?:\x00)?\x01", payload):
        if not nal:
            continue
        kind = (nal[0] >> 1) & 63 if hevc else nal[0] & 31
        if kind in ((32, 33, 34) if hevc else (7, 8)):
            result[kind] = nal
    return result


class TimestampDiscontinuity(RuntimeError):
    """Input timeline reset; recreate the bridge at a random-access point."""


class HardwareWorkerError(RuntimeError):
    """The hardware worker exited or stopped producing decoded output."""


class FFmpegVAAPIDecoder:
    """One worker per stream; the owner must close it before using software."""

    def __init__(
        self,
        first,
        *,
        ffmpeg,
        on_frame,
        interval_ms=0,
        device="/dev/dri/renderD128",
        timeout=5.0,
        label="unknown",
    ):
        if platform.system() != "Linux" or not Path(device).is_char_device():
            raise RuntimeError("VA-API render device unavailable")
        if not os.path.isabs(ffmpeg) or not os.access(ffmpeg, os.X_OK):
            raise RuntimeError("VA-API requires an executable absolute FFmpeg path")
        self._hevc = getattr(first, "codec_id", None) == MIoTCameraCodec.VIDEO_H265
        self._template = av.open(
            io.BytesIO(first.data), format="hevc" if self._hevc else "h264"
        )
        codec = self._template.streams.video[0].codec_context
        if not codec.width or codec.has_b_frames:
            self._template.close()
            raise RuntimeError("VA-API bridge requires video without B frames")
        self.extradata = codec.extradata
        self._parameter_sets = _parameter_sets(first.data, hevc=self._hevc)
        self._origin = first.timestamp
        self._last_pts = -1
        self._on_frame = on_frame
        self._timeout = max(timeout, interval_ms / 1000 * 3 + 1)
        self._progress = time.monotonic()
        self._last_input = self._progress
        self._started = self._progress
        self._last_report = self._progress
        self._stats = Counter()
        self._delivery = None
        self._label = label
        self._pending = OrderedDict()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._error = None
        self._stderr_tail = b""
        self._process = None
        self._mux = None
        self._reader = None
        self._watcher = None
        filters = []
        if interval_ms > 0:
            filters.append(
                f"select=isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval_ms / 1000})"
            )
        filters += ["hwdownload", "format=nv12"]
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-hwaccel",
            "vaapi",
            "-hwaccel_device",
            device,
            "-hwaccel_output_format",
            "vaapi",
            "-probesize",
            "32768",
            "-analyzeduration",
            "0",
            "-f",
            "nut",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            ",".join(filters),
            "-fps_mode",
            "passthrough",
            "-c:v",
            "rawvideo",
            "-threads",
            "1",
            "-enc_time_base",
            "demux",
            "-flush_packets",
            "1",
            "-f",
            "nut",
            "pipe:1",
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            os.set_blocking(self._process.stderr.fileno(), False)
            self._mux = av.open(
                self._process.stdin,
                mode="w",
                format="nut",
                options={"flush_packets": "1"},
            )
            self._stream = self._mux.add_stream_from_template(
                self._template.streams.video[0]
            )
            self._stream.time_base = Fraction(1, 1000)
            self._delivery = LatestFrameDelivery(self._deliver)
            self._reader = threading.Thread(target=self._read, name="vaapi-frames")
            self._watcher = threading.Thread(target=self._watch, name="vaapi-watch")
            self._reader.start()
            self._watcher.start()
        except Exception:
            self.close()
            raise

    def push(self, data):
        """Called serially by the existing decoder thread; never drop packets."""
        if self._error is not None or self._stop.is_set():
            raise HardwareWorkerError(f"VA-API worker unavailable: {self._error}")
        pts = data.timestamp - self._origin
        if pts <= self._last_pts:
            raise TimestampDiscontinuity("non-monotonic input PTS")
        now = time.monotonic()
        if now - self._last_input > self._timeout / 2:
            # A resumed input gets a fresh output deadline.
            self._progress = now
        self._last_input = now
        with self._lock:
            if len(self._pending) >= 128:
                # Only the timestamp index expires. Every compressed packet is muxed.
                self._pending.popitem(last=False)
                self._stats["metadata_evicted"] += 1
            self._pending[pts] = (
                data.model_copy(update={"data": b"", "length": 0}),
                now,
            )
            self._stats["metadata_peak"] = max(
                self._stats["metadata_peak"], len(self._pending)
            )
        parameters = _parameter_sets(data.data, hevc=self._hevc)
        if any(
            self._parameter_sets.get(kind) != value
            for kind, value in parameters.items()
        ):
            self._parameter_sets.update(parameters)
            self.extradata = b"".join(
                b"\x00\x00\x00\x01" + value
                for kind, value in sorted(self._parameter_sets.items())
            )
            raise RuntimeError("Video parameters changed; software decoder required")
        packet = av.Packet(data.data)
        packet.stream = self._stream
        packet.time_base = Fraction(1, 1000)
        packet.pts = packet.dts = pts
        begin = time.monotonic()
        try:
            self._mux.mux(packet)
        except (OSError, av.error.FFmpegError) as exc:
            if self._process.poll() is not None:
                raise HardwareWorkerError("VA-API worker exited during input") from exc
            raise
        elapsed = (time.monotonic() - begin) * 1000
        self._stats["input_packets"] += 1
        self._stats["mux_wall_ms"] += elapsed
        self._stats["mux_max_ms"] = max(self._stats["mux_max_ms"], elapsed)
        self._last_pts = pts

    def _read(self):
        try:
            with av.open(
                self._process.stdout,
                format="nut",
                options={"probesize": "32", "analyzeduration": "0"},
            ) as output:
                for frame in output.decode(video=0):
                    if self._stop.is_set():
                        break
                    self._progress = time.monotonic()
                    self._stats["output_frames"] += 1
                    pts = round(frame.pts * frame.time_base * 1000)
                    with self._lock:
                        entry = self._pending.pop(pts, None)
                        while self._pending and next(iter(self._pending)) < pts:
                            self._pending.popitem(last=False)
                            self._stats["metadata_filtered"] += 1
                    if entry is None:
                        # This decoded frame is obsolete; codec references stay intact.
                        self._stats["output_without_metadata"] += 1
                        continue
                    metadata, received_at = entry
                    self._delivery.offer(frame, metadata, received_at=received_at)
            if not self._stop.is_set():
                self._error = RuntimeError("VA-API output ended")
        except Exception as exc:
            if not self._stop.is_set():
                self._error = exc

    def _deliver(self, frame, metadata):
        begin = time.monotonic()
        converted = frame.reformat(format="yuv420p")
        self._stats["format_wall_ms"] += (time.monotonic() - begin) * 1000
        self._on_frame(converted, metadata)
        if not getattr(self, "_reported_first", False):
            logging.getLogger(__name__).warning(
                "VAAPI first frame delivered: stream=%s %sx%s",
                self._label,
                frame.width,
                frame.height,
            )
            self._reported_first = True

    def metrics(self):
        with self._lock:
            pending = len(self._pending)
        now = time.monotonic()
        return {
            "stream": self._label,
            "pid": self._process.pid,
            "uptime_s": round(now - self._started, 2),
            "metadata_pending": pending,
            "input_age_s": round(now - self._last_input, 3),
            "output_age_s": round(now - self._progress, 3),
            **dict(self._stats),
            "delivery": self._delivery.snapshot() if self._delivery else None,
        }

    def _report(self):
        logging.getLogger(__name__).warning(
            "VAAPI pipeline metrics: %s", json.dumps(self.metrics(), sort_keys=True)
        )

    def _watch(self):
        while not self._stop.wait(0.2):
            try:
                chunk = self._process.stderr.read(4096)
                if chunk:
                    self._stderr_tail = (self._stderr_tail + chunk)[-4096:]
            except OSError:
                # Stderr is diagnostic only; process/deadline checks still run.
                pass
            now = time.monotonic()
            if now - getattr(self, "_last_report", now) >= 30:
                self._report()
                self._last_report = now
            with self._lock:
                waiting = bool(self._pending)
            if self._process.poll() is not None:
                self._error = RuntimeError(self._stderr_tail.decode(errors="replace"))
            elif (
                waiting
                and now - self._progress > self._timeout
                and now - getattr(self, "_last_input", now) < min(1, self._timeout / 2)
            ):
                self._error = TimeoutError("VA-API frame deadline exceeded")
            if self._error is not None:
                # Killing the child unblocks a producer stuck writing into its pipe.
                try:
                    self._process.kill()
                except ProcessLookupError:
                    # The worker exited between poll() and kill().
                    pass
                return

    def abort(self):
        """Unblock pipe I/O; mux cleanup is deferred until its owner has stopped."""
        self._stop.set()
        if self._delivery is not None:
            self._delivery.abort()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                # The worker exited between poll() and terminate().
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def close(self):
        """Called by the writer owner after push ends; idempotently reap resources."""
        self.abort()
        process = self._process
        for thread in (self._reader, self._watcher):
            if (
                thread is not None
                and thread.ident is not None
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=2)
                if thread.is_alive():
                    raise RuntimeError("VA-API worker did not stop")
        if self._delivery is not None:
            self._delivery.close()
        if self._mux is not None:
            try:
                self._mux.close()
            except (av.error.FFmpegError, OSError):
                # The child is already reaped; its closed pipe cannot be flushed.
                pass
            self._mux = None
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        self._template.close()
        with self._lock:
            self._pending.clear()
