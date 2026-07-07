# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Short-window message dedup — a safety net against repeated notifications.

An agent stuck in a loop can ask to send the *same* notification many times in
a few seconds. This tiny helper suppresses an identical message seen again
within a time window, so the amplifier layer never forwards a 1:1 burst of
duplicates to the user.

Mirrors the ``_recent`` dedup pattern in :mod:`miloco.miot.welcome_service`:
monotonic timestamps, **recorded only on a successful send** so a failed
attempt can still be retried immediately. Kept intentionally small and pure
(injectable ``clock``) so it can be unit-tested without the owning service.

Callers check-then-record around the actual (awaited) send, so this guards
*serial* repeats — the real failure mode, a single session looping — not truly
concurrent double-sends. That's an accepted trade-off for a safety net: keeping
"record only on success" (retryable) is worth more than closing the race.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class MessageDeduper:
    """Suppress an identical ``key`` seen again within ``window_sec``.

    Usage is a check-then-record pair around the actual send::

        if deduper.is_duplicate(key):
            return          # skip: sent an identical message recently
        ...do the send...
        deduper.record(key) # only on success → a failed send is retryable

    ``window_sec <= 0`` disables dedup entirely (``is_duplicate`` always False).
    """

    def __init__(
        self,
        window_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_sec = window_sec
        self._clock = clock
        # key -> monotonic ts of last successful send.
        self._recent: dict[str, float] = {}

    def is_duplicate(self, key: str) -> bool:
        """Return True if ``key`` was recorded within the window (→ skip send).

        Also prunes entries older than the window so the map stays bounded —
        unlike welcome_service (keyed by a finite device set), notification
        text is an unbounded key space.
        """
        if self._window_sec <= 0:
            return False
        now = self._clock()
        self._prune(now)
        last = self._recent.get(key)
        return last is not None and now - last < self._window_sec

    def record(self, key: str) -> None:
        """Record ``key`` as sent at the current time."""
        if self._window_sec <= 0:
            return
        self._recent[key] = self._clock()

    def _prune(self, now: float) -> None:
        expired = [k for k, ts in self._recent.items() if now - ts >= self._window_sec]
        for k in expired:
            del self._recent[k]
