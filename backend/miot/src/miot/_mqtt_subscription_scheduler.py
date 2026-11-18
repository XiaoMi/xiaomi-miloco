"""Internal FIFO scheduling and shared rate limiting for MQTT subscriptions."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable, Optional

from .types import MipsConnectionError


class _MqttOperationRateLimiter:
    """Limit cloud MQTT subscribe and unsubscribe operations together."""

    def __init__(self, per_sec: int, window: float) -> None:
        if per_sec <= 0 or window <= 0:
            raise ValueError("MQTT operation rate limit must be positive")
        self._limit = per_sec
        self._window = window
        self._timestamps: deque[float] = deque()

    async def acquire(self) -> None:
        """Wait for one shared subscribe/unsubscribe operation slot."""
        while True:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= self._window:
                self._timestamps.popleft()
            if len(self._timestamps) < self._limit:
                self._timestamps.append(now)
                return
            await asyncio.sleep(max(self._timestamps[0] + self._window - now, 0.001))


class _MqttOperationDispatcher:
    def __init__(self, limiter: _MqttOperationRateLimiter) -> None:
        self._limiter = limiter
        self._slots = asyncio.Semaphore(32)
        self._queue: deque = deque()
        self._worker: Optional[asyncio.Task] = None

    async def submit(self, send: Callable, ack: Optional[asyncio.Future] = None):
        sent = asyncio.get_running_loop().create_future()
        self._queue.append((send, ack, sent))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        try:
            return await sent
        except asyncio.CancelledError:
            if ack is not None:
                ack.cancel()
            raise

    async def _run(self) -> None:
        while self._queue:
            send, ack, sent = self._queue.popleft()
            acquired = False
            try:
                if sent.cancelled():
                    continue
                if ack is not None:
                    await self._slots.acquire()
                    acquired = True
                await self._limiter.acquire()
                if sent.cancelled():
                    continue
                result = send()
                if ack is not None:
                    ack.add_done_callback(lambda _: self._slots.release())
                    acquired = False
                sent.set_result(result)
            except asyncio.CancelledError:
                if not sent.done():
                    sent.set_exception(MipsConnectionError("MQTT connection closed"))
                raise
            except Exception as exc:
                if not sent.done():
                    sent.set_exception(exc)
            finally:
                if acquired:
                    self._slots.release()
            await asyncio.sleep(0)

    def reset(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        while self._queue:
            _, _, sent = self._queue.popleft()
            if not sent.done():
                sent.set_exception(MipsConnectionError("MQTT connection closed"))
