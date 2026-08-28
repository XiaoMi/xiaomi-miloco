# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""StateStore 投递层：start() 之后的事件投递、启停与级联深度。"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading

import pytest
from miloco.state import MISSING, StateStore
from miloco.state.store import MAX_CASCADE_DEPTH


async def settle(rounds: int = 200) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def test_events_follow_write_order():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("iot/**", seen.append)

    for value in range(5):
        store.set("iot/dev1/prop", value, source="miot")
    await settle()

    assert [change.new for change in seen] == [0, 1, 2, 3, 4]


async def test_cross_thread_write_is_delivered():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("iot/**", seen.append)

    thread = threading.Thread(
        target=lambda: store.set("iot/dev1/online", True, source="mqtt")
    )
    thread.start()
    thread.join()
    await settle()

    assert [change.path for change in seen] == ["iot/dev1/online"]


async def test_delete_delivers_events_with_the_deleting_source():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("omni/**", seen.append)

    store.set("omni/rule/r1", {"dev1": True, "dev2": False}, source="omni")
    await settle()
    seen.clear()
    store.delete("omni/rule/r1", source="rule_admin")
    await settle()

    assert sorted(change.path for change in seen) == [
        "omni/rule/r1/dev1",
        "omni/rule/r1/dev2",
    ]
    assert {change.source for change in seen} == {"rule_admin"}
    assert store.get("omni/rule/r1") is MISSING


async def test_same_value_write_is_not_delivered():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    store.set("a/b", 1, source="t")
    await settle()

    assert len(seen) == 1


async def test_failing_subscriber_does_not_block_others():
    store = StateStore()
    store.start()
    seen: list = []

    def boom(change):
        raise RuntimeError("subscriber is broken")

    store.subscribe("a/**", boom)
    store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    await settle()

    assert len(seen) == 1


async def test_unsubscribe_stops_delivery():
    store = StateStore()
    store.start()
    seen: list = []
    unsubscribe = store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    await settle()
    unsubscribe()
    store.set("a/b", 2, source="t")
    await settle()

    assert len(seen) == 1


async def test_pattern_filters_delivery():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("iot/*/prop/2.1", seen.append)

    store.set("iot/dev1/prop/2.1", 26, source="miot")
    store.set("iot/dev1/prop/2.2", 50, source="miot")
    store.set("iot/dev2/prop/2.1", 24, source="miot")
    await settle()

    assert [change.path for change in seen] == [
        "iot/dev1/prop/2.1",
        "iot/dev2/prop/2.1",
    ]


async def test_pattern_longer_than_path_does_not_match():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("a/b/c", seen.append)

    store.set("a/b", 1, source="t")
    await settle()

    assert seen == []


# ---- 级联深度 ----


async def test_cascade_depth_is_capped():
    store = StateStore()
    store.start()

    def relay(change):
        step = int(change.path.split("/")[1])
        store.set(f"chain/{step + 1}", step + 1, source="relay")

    store.subscribe("chain/*", relay)
    store.set("chain/0", 0, source="t")
    await settle()

    assert len(store.snapshot("chain/*")["chain"]) == MAX_CASCADE_DEPTH


async def test_cascaded_delete_is_capped():
    """delete 与 set 共用同一个级联计数。"""
    store = StateStore()
    store.start()
    for step in range(MAX_CASCADE_DEPTH + 2):
        store._commit(f"chain/{step}", step, source="seed")

    def relay(change):
        step = int(change.path.split("/")[1])
        store.delete(f"chain/{step + 1}", source="relay")

    store.subscribe("chain/*", relay)
    store.delete("chain/0", source="t")
    await settle()

    assert len(store.snapshot("chain/*")["chain"]) == 2


async def test_deferred_write_inherits_cascade_depth():
    store = StateStore()
    store.start()

    async def later(step: int) -> None:
        await asyncio.sleep(0)
        store.set(f"chain/{step + 1}", step + 1, source="relay")

    def relay(change):
        step = int(change.path.split("/")[1])
        asyncio.create_task(later(step))

    store.subscribe("chain/*", relay)
    store.set("chain/0", 0, source="t")
    await settle()

    assert len(store.snapshot("chain/*")["chain"]) == MAX_CASCADE_DEPTH


# ---- 启停 ----


async def test_writes_before_start_are_dropped():
    store = StateStore()
    seen: list = []
    store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    store.start()
    await settle()

    assert seen == []
    assert store.get("a/b") == 1
    assert store._dropped == 1


async def test_stop_ends_delivery():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    store.stop()
    await settle()

    assert seen == []
    assert store._discarded == 1


async def test_restart_discards_events_queued_before_stop():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    store.stop()
    store.start()
    store.set("a/b", 2, source="t")
    await settle()

    assert [change.new for change in seen] == [2]


async def test_repeated_start_keeps_queued_events():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    store.start()
    await settle()

    assert [change.new for change in seen] == [1]


async def test_start_must_run_on_the_loop_thread():
    store = StateStore()
    failures: list = []

    def boot() -> None:
        try:
            store.start()
        except RuntimeError as exc:
            failures.append(exc)

    thread = threading.Thread(target=boot)
    thread.start()
    thread.join()

    assert len(failures) == 1
    assert store._loop is None


def test_start_outside_a_loop_raises():
    store = StateStore()
    with pytest.raises(RuntimeError):
        store.start()


def test_start_rejects_a_loop_that_is_not_running():
    """绑到一个建好但没在跑的 loop 上，事件会全丢且没有任何日志。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        store = StateStore()
        with pytest.raises(RuntimeError):
            store.start()
        assert store._loop is None
    finally:
        asyncio.set_event_loop(None)
        loop.close()


async def test_repeated_stop_is_idempotent():
    store = StateStore()
    store.start()
    generation = store._generation

    store.stop()
    store.stop()

    assert store._generation == generation + 1


def test_write_after_the_loop_is_closed_is_dropped():
    """loop 被外部关掉后写入不能炸——`call_soon_threadsafe` 抛的 RuntimeError 要当未启动处理。"""
    store = StateStore()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=lambda: loop.run_until_complete(_boot(store)))
    thread.start()
    thread.join()
    loop.close()

    store.set("a/b", 1, source="t")

    assert store.get("a/b") == 1
    assert store._dropped == 1


async def _boot(store: StateStore) -> None:
    store.start()


async def test_subscriptions_survive_restart():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("a/**", seen.append)

    store.stop()
    store.start()
    store.set("a/b", 1, source="t")
    await settle()

    assert len(seen) == 1


async def test_stats_tracks_dropped_events():
    store = StateStore()
    store.set("a/b", 1, source="t")

    assert store.stats()["dropped"] == 1
    assert store.stats()["pending"] == 0


async def test_not_started_warning_repeats_after_a_restart(caplog):
    """告警标志不复位的话，start / stop / start 之后再丢事件就彻底没声了。"""
    store = StateStore()
    store.set("a/b", 1, source="t")
    store.start()
    store.stop()
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="miloco.state.store"):
        store.set("a/c", 1, source="t")

    assert "not started" in caplog.text


async def test_change_committed_before_subscribe_is_not_delivered():
    """订阅只收订阅之后发生的变更。

    投递是异步的，所以「提交 → 订阅 → 事件循环转一圈」这个顺序真实存在，而且正好落在启动
    时段（对齐批量写入 + 消费方接线同一轮）。按边沿判定的消费方会为一个它上线前就发生完的
    跳变触发一次。
    """
    store = StateStore()
    store.start()
    store.set("a/b", 1, source="t")

    seen: list = []
    store.subscribe("a/**", seen.append)
    await settle()

    assert seen == []


async def test_change_committed_after_subscribe_is_delivered():
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    await settle()

    assert [change.path for change in seen] == ["a/b"]


async def test_subscriber_added_inside_a_callback_misses_the_current_batch():
    """回调里新订阅的一方不该收到正在投递的这一批 —— 那批在它订阅之前就提交了。"""
    store = StateStore()
    store.start()
    late: list = []

    def add_late_subscriber(_change) -> None:
        store.subscribe("a/**", late.append)

    store.subscribe("a/**", add_late_subscriber)
    store.set("a/b", 1, source="t")
    await settle()

    assert late == []
    # 它订阅之后的变更照收
    store.set("a/b", 2, source="t")
    await settle()
    assert [change.path for change in late] == ["a/b"]


async def test_unsubscribe_before_delivery_cancels_it():
    """退订能拦住已排队的通知：订阅表是投递时现读的，不是提交时定死的。

    加上「只收订阅之后的变更」那条之后，两端就都收紧了：提交时的订阅序号挡掉后来者，
    现读订阅表挡掉已退订的。
    """
    store = StateStore()
    store.start()
    seen: list = []
    unsubscribe = store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    unsubscribe()
    await settle()

    assert seen == []


async def test_unsubscribe_inside_a_callback_does_not_stop_the_current_batch():
    """唯一剩下的窗口：`_dispatch` 已经取过订阅表快照，这一批还是会投给它。

    消费方在退订之后立刻拆自己的状态，就可能被这最后一次回调撞上 —— 这是 `subscribe`
    文档里那条警告的**唯一**成立场景。
    """
    store = StateStore()
    store.start()
    seen: list = []

    def drop_the_other(_change) -> None:
        unsubscribe_other()

    store.subscribe("a/**", drop_the_other)
    unsubscribe_other = store.subscribe("a/**", seen.append)

    store.set("a/b", 1, source="t")
    await settle()

    # 第一个回调里已经把第二个退订了，但这一批的快照里还有它
    assert len(seen) == 1
    store.set("a/b", 2, source="t")
    await settle()
    assert len(seen) == 1


async def test_cascade_rejection_is_counted_and_only_logged_once(caplog):
    """闸按外部写入频率打日志会自己变成负担；次数从 stats() 读，与另外几道同口径。"""
    store = StateStore()
    store.start()

    counter = itertools.count()

    def keep_deriving(change):
        store.set("iot/device/d1/seq", next(counter), source="cb")

    store.subscribe("iot/**", keep_deriving)
    with caplog.at_level(logging.ERROR, logger="miloco.state.store"):
        for step in range(5):
            store.set("iot/device/d1/prop/2.1", step, source="align")
            await settle()

    rejections = [r for r in caplog.records if "cascade depth" in r.getMessage()]
    assert len(rejections) == 1
    assert store.stats()["rejected_cascade"] >= 5
    store.stop()
