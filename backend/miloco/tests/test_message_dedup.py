# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for MessageDeduper — the short-window notification dedup safety net.

Covers: duplicate within window → skip; outside window → allowed again;
boundary at exactly the window; only recorded keys dedup; distinct keys are
independent; window<=0 disables; and expired entries get pruned.
"""

from __future__ import annotations

from miloco.miot.message_dedup import MessageDeduper


class _Clock:
    """Manually-advanced monotonic clock for deterministic window tests."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_not_duplicate_before_record() -> None:
    dep = MessageDeduper(window_sec=60, clock=_Clock())
    assert dep.is_duplicate("a") is False


def test_duplicate_within_window() -> None:
    clock = _Clock()
    dep = MessageDeduper(window_sec=60, clock=clock)
    dep.record("a")
    clock.advance(10)
    assert dep.is_duplicate("a") is True


def test_allowed_again_after_window() -> None:
    clock = _Clock()
    dep = MessageDeduper(window_sec=60, clock=clock)
    dep.record("a")
    clock.advance(60)  # exactly the window → no longer within (uses strict <)
    assert dep.is_duplicate("a") is False


def test_still_dedup_just_before_window() -> None:
    clock = _Clock()
    dep = MessageDeduper(window_sec=60, clock=clock)
    dep.record("a")
    clock.advance(59.9)
    assert dep.is_duplicate("a") is True


def test_distinct_keys_are_independent() -> None:
    dep = MessageDeduper(window_sec=60, clock=_Clock())
    dep.record("a")
    assert dep.is_duplicate("b") is False


def test_window_zero_disables_dedup() -> None:
    dep = MessageDeduper(window_sec=0, clock=_Clock())
    dep.record("a")
    assert dep.is_duplicate("a") is False


def test_negative_window_disables_dedup() -> None:
    dep = MessageDeduper(window_sec=-5, clock=_Clock())
    dep.record("a")
    assert dep.is_duplicate("a") is False


def test_failed_send_not_recorded_is_retryable() -> None:
    # Simulate the service's "record only on success" contract: without a
    # record() call after a failed send, the same key is not deduped.
    dep = MessageDeduper(window_sec=60, clock=_Clock())
    assert dep.is_duplicate("a") is False
    # (no record — send failed)
    assert dep.is_duplicate("a") is False


def test_expired_entries_are_pruned() -> None:
    clock = _Clock()
    dep = MessageDeduper(window_sec=60, clock=clock)
    dep.record("a")
    dep.record("b")
    clock.advance(120)
    # A lookup past the window triggers prune of both stale entries.
    assert dep.is_duplicate("c") is False
    assert dep._recent == {}  # noqa: SLF001 — asserting the prune side effect


def test_clear_forgets_recorded_keys() -> None:
    """切号必须能清空：去重键是纯文案、不带账号维度，不清的话「A 刚发过同一句 →
    切到 B → B 再发」会命中去重，send_notify 打一条 skipped 日志后正常返回，
    调用方以为发成功了，用户永远收不到。"""
    clock = _Clock()
    d = MessageDeduper(window_sec=60, clock=clock)
    d.record("我出门了")
    assert d.is_duplicate("我出门了") is True

    d.clear()

    assert d.is_duplicate("我出门了") is False
