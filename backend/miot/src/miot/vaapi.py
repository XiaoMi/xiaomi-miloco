# Copyright (C) 2026 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""Opt-in persistent VA-API decoder. Only H.264 without frame reordering.

NUT carries device PTS across the process boundary; a bounded metadata table
recovers the corresponding host receive timestamp. No compressed packet is
sampled. Frames are selected on the device before downloading to system RAM.
"""

from __future__ import annotations

import io
import os
import platform
import re
import subprocess
import threading
import time
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path

import av


def _parameter_sets(payload):
    return {
        nal[0] & 31: nal
        for nal in re.split(b"\x00\x00(?:\x00)?\x01", payload)
        if nal and nal[0] & 31 in (7, 8)
    }


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
    ):
        if platform.system() != "Linux" or not Path(device).is_char_device():
            raise RuntimeError("VA-API render device unavailable")
        if not os.path.isabs(ffmpeg) or not os.access(ffmpeg, os.X_OK):
            raise RuntimeError("VA-API requires an executable absolute FFmpeg path")
        self._template = av.open(io.BytesIO(first.data), format="h264")
        codec = self._template.streams.video[0].codec_context
        if not codec.width or codec.has_b_frames:
            self._template.close()
            raise RuntimeError("VA-API bridge requires H.264 without B frames")
        self.extradata = codec.extradata
        self._parameter_sets = _parameter_sets(first.data)
        self._origin = first.timestamp
        self._last_pts = -1
        self._on_frame = on_frame
        self._timeout = timeout
        self._progress = time.monotonic()
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
            raise RuntimeError(f"VA-API worker unavailable: {self._error}")
        pts = data.timestamp - self._origin
        if pts <= self._last_pts:
            raise RuntimeError("non-monotonic PTS requires software decoding")
        with self._lock:
            if len(self._pending) >= 128:
                raise RuntimeError("VA-API metadata backlog exceeded")
            self._pending[pts] = data.model_copy(update={"data": b"", "length": 0})
        parameters = _parameter_sets(data.data)
        if any(
            self._parameter_sets.get(kind) != value
            for kind, value in parameters.items()
        ):
            self._parameter_sets.update(parameters)
            self.extradata = b"".join(
                b"\x00\x00\x00\x01" + value
                for kind, value in sorted(self._parameter_sets.items())
            )
            raise RuntimeError("H.264 parameters changed; software decoder required")
        packet = av.Packet(data.data)
        packet.stream = self._stream
        packet.time_base = Fraction(1, 1000)
        packet.pts = packet.dts = pts
        self._mux.mux(packet)
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
                    pts = round(frame.pts * frame.time_base * 1000)
                    with self._lock:
                        metadata = self._pending.pop(pts)
                        # Metadata belonging to frames filtered on the GPU is obsolete.
                        while self._pending and next(iter(self._pending)) < pts:
                            self._pending.popitem(last=False)
                    self._progress = time.monotonic()
                    self._on_frame(frame.reformat(format="yuv420p"), metadata)
            if not self._stop.is_set():
                self._error = RuntimeError("VA-API output ended")
        except Exception as exc:
            if not self._stop.is_set():
                self._error = exc

    def _watch(self):
        while not self._stop.wait(0.2):
            try:
                chunk = self._process.stderr.read(4096)
                if chunk:
                    self._stderr_tail = (self._stderr_tail + chunk)[-4096:]
            except OSError:
                pass
            with self._lock:
                waiting = bool(self._pending)
            if self._process.poll() is not None:
                self._error = RuntimeError(self._stderr_tail.decode(errors="replace"))
            elif waiting and time.monotonic() - self._progress > self._timeout:
                self._error = TimeoutError("VA-API frame deadline exceeded")
            if self._error is not None:
                # Killing the child unblocks a producer stuck writing into its pipe.
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
                return

    def abort(self):
        """Unblock pipe I/O; mux cleanup is deferred until its owner has stopped."""
        self._stop.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
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
        if self._mux is not None:
            try:
                self._mux.close()
            except (av.error.FFmpegError, OSError):
                pass
            self._mux = None
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        self._template.close()
        with self._lock:
            self._pending.clear()
