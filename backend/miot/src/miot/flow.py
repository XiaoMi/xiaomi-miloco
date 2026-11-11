# Copyright (C) 2026 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""Bounded latest-frame delivery for real-time decoded video."""

import logging
import threading
import time

_LOGGER = logging.getLogger(__name__)


class LatestFrameDelivery:
    """Keep one frame in flight and at most one pending frame.

    Ownership of each offered VideoFrame moves to this object. The producer
    must not mutate it after offer(); replacement releases the old reference.
    No compressed packet enters this queue.
    """

    def __init__(self, callback, *, max_age=1.0):
        self._callback = callback
        self._max_age = max_age
        self._condition = threading.Condition()
        self._pending = None
        self._stopping = False
        self._inflight = False
        self._last_error_log = 0.0
        self._stats = {
            "offered": 0,
            "replaced": 0,
            "expired": 0,
            "processed": 0,
            "errors": 0,
            "callback_wall_ms": 0.0,
            "callback_cpu_ms": 0.0,
            "callback_max_ms": 0.0,
            "frame_age_max_ms": 0.0,
        }
        self._thread = threading.Thread(
            target=self._run, name="vaapi-delivery", daemon=True
        )
        self._thread.start()

    def offer(self, frame, metadata, *, received_at=None):
        if received_at is None:
            received_at = time.monotonic()
        with self._condition:
            if self._stopping:
                return False
            self._stats["offered"] += 1
            if self._pending is not None:
                self._stats["replaced"] += 1
            self._pending = (frame, metadata, received_at)
            self._condition.notify()
            return True

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                frame, metadata, received_at = self._pending
                self._pending = None
                self._inflight = True
            age = time.monotonic() - received_at
            if age > self._max_age:
                with self._condition:
                    self._stats["expired"] += 1
                    self._stats["frame_age_max_ms"] = max(
                        self._stats["frame_age_max_ms"], age * 1000
                    )
                    self._inflight = False
                del frame, metadata
                continue
            started = time.monotonic()
            cpu_started = time.thread_time()
            success = False
            try:
                self._callback(frame, metadata)
                success = True
            except Exception as exc:
                with self._condition:
                    self._stats["errors"] += 1
                if started - self._last_error_log >= 30:
                    _LOGGER.warning("Decoded-frame consumer failed: %s", exc)
                    self._last_error_log = started
            finally:
                elapsed = (time.monotonic() - started) * 1000
                cpu = (time.thread_time() - cpu_started) * 1000
                with self._condition:
                    self._stats["processed"] += int(success)
                    self._stats["callback_wall_ms"] += elapsed
                    self._stats["callback_cpu_ms"] += cpu
                    self._stats["callback_max_ms"] = max(
                        self._stats["callback_max_ms"], elapsed
                    )
                    self._stats["frame_age_max_ms"] = max(
                        self._stats["frame_age_max_ms"], age * 1000
                    )
                    self._inflight = False
                del frame, metadata

    def snapshot(self):
        with self._condition:
            return {
                **self._stats,
                "pending": int(self._pending is not None),
                "inflight": int(self._inflight),
                "alive": self._thread.is_alive(),
            }

    def abort(self):
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify_all()

    def close(self, timeout=2):
        self.abort()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("Decoded-frame consumer did not stop")
        self._callback = None
