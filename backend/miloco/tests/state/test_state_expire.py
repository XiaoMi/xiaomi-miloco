# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""StateStore 的过期状态与过期通知。"""

from __future__ import annotations

import asyncio

from miloco.state import MISSING, StateStore


async def wait_for_expiration() -> None:
    await asyncio.sleep(0.05)
    await asyncio.sleep(0)


async def test_expiration_deletes_value_and_notifies_subscribers():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("event/**", seen.append)

    store.set("event/door", "open", source="sensor", expires_in_ms=10)
    await wait_for_expiration()

    assert store.get("event/door") is MISSING
    assert [(change.old, change.new, change.reason) for change in seen] == [
        (MISSING, "open", "update"),
        ("open", MISSING, "expire"),
    ]
    assert seen[-1].source == "state_expire"

    store.stop()


async def test_retained_expiration_is_visible_in_metadata_but_not_normal_reads():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("event/**", seen.append)

    store.set(
        "event/door",
        "open",
        source="sensor",
        expires_in_ms=10,
        on_expire="retain",
    )
    await wait_for_expiration()

    assert store.get("event/door") is MISSING
    entry = store.get_entry("event/door")
    assert entry is not None
    assert entry.expired is True
    assert entry.expires_at is not None
    assert seen[-1].old == seen[-1].new == "open"
    assert seen[-1].reason == "expire"

    store.stop()


async def test_refreshing_value_makes_old_expiration_noop():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("event/**", seen.append)

    store.set("event/door", "open", source="sensor", expires_in_ms=100)
    await asyncio.sleep(0.02)
    store.set("event/door", "closed", source="sensor", expires_in_ms=1000)
    await asyncio.sleep(0)

    assert store.get("event/door") == "closed"
    assert [change.reason for change in seen] == ["update", "update"]

    store.stop()


def test_refreshing_value_keeps_expiration_heap_bounded():
    store = StateStore()

    for _ in range(300):
        store.set("event/door", "open", source="sensor", expires_in_ms=60_000)

    assert len(store._expiration_heap) <= 64


def test_restart_on_another_event_loop_ignores_old_reschedule_callback():
    store = StateStore()
    old_errors: list[BaseException | None] = []
    old_loop = asyncio.new_event_loop()
    old_loop.set_debug(True)
    old_loop.set_exception_handler(
        lambda _loop, context: old_errors.append(context.get("exception"))
    )
    old_loop.run_until_complete(_start(store))
    store.set("event/door", "open", source="sensor", expires_in_ms=30)
    store.stop()

    new_loop = asyncio.new_event_loop()
    new_loop.set_debug(True)
    try:
        new_loop.run_until_complete(_start(store))
        old_loop.run_until_complete(asyncio.sleep(0))
        new_loop.run_until_complete(asyncio.sleep(0.06))
        assert store.get("event/door") is MISSING
        store.stop()
    finally:
        old_loop.close()
        new_loop.close()

    assert old_errors == []


async def _start(store: StateStore) -> None:
    store.start()


def test_value_without_expiration_has_no_expiration_metadata():
    store = StateStore()

    store.set("event/door", "open", source="sensor")

    entry = store.get_entry("event/door")
    assert entry is not None
    assert entry.expires_at is None
    assert entry.expired is False
