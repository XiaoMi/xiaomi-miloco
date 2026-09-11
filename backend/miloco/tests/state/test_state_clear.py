# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""StateStore 清空整棵树。"""

from __future__ import annotations

import asyncio
import logging

from miloco.state import MISSING, StateStore


async def settle(rounds: int = 200) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def make_store() -> StateStore:
    store = StateStore()
    store._commit("iot/device/d1/status/online", True, source="align")
    store._commit("iot/device/d1/prop/2.1", 26, source="align")
    store._commit("omni/device/d1/caption", "有人", source="omni")
    return store


def test_clear_drops_every_leaf():
    store = make_store()

    store._commit_clear(source="switch")

    assert store.stats()["leaves"] == 0
    assert store.snapshot("**") == {}
    assert store.get("iot/device/d1/status/online") is MISSING


def test_clear_reports_one_delete_per_leaf():
    store = make_store()

    changes = store._commit_clear(source="switch")

    assert {change.path for change in changes} == {
        "iot/device/d1/status/online",
        "iot/device/d1/prop/2.1",
        "omni/device/d1/caption",
    }
    assert all(change.new is MISSING for change in changes)
    assert all(change.source == "switch" for change in changes)


def test_clear_lets_the_tree_grow_back():
    store = make_store()
    store._commit_clear(source="switch")

    store._commit("iot/device/d9/status/online", True, source="align")

    assert store.stats()["leaves"] == 1
    assert store.get("iot/device/d9/status/online") is True


def test_clear_zeroes_the_count_even_if_it_had_drifted():
    """归零的全部价值就在计数已经不对的时候；计数正确时归零和减法结果相同，钉不出区别。"""
    store = make_store()
    store._leaf_count = 99

    store._commit_clear(source="switch")

    assert store.stats()["leaves"] == 0


def test_clear_of_empty_store_changes_nothing():
    store = StateStore()

    assert store._commit_clear(source="switch") == []
    assert store.stats()["leaves"] == 0


def test_clear_resets_the_two_content_warnings():
    """两个标志说的是树的内容；内容清了，旧内容压过的告警不该继续压新内容的第一条。"""
    store = make_store()
    store._warned_leaf_limit = True
    store._warned_shape_flip = True

    store._commit_clear(source="switch")

    assert store._warned_leaf_limit is False
    assert store._warned_shape_flip is False


def test_clear_keeps_the_lifetime_counters():
    """这三个数记的是这条生命里漏过、丢过、翻过多少，被一次 clear 抹掉就没意义了。"""
    store = make_store()
    store._dropped = 3
    store._discarded = 5
    store._shape_flips = 7

    store._commit_clear(source="switch")

    stats = store.stats()
    assert stats["dropped"] == 3
    assert stats["discarded"] == 5
    assert stats["shape_flips"] == 7


async def test_clear_delivers_a_delete_to_subscribers():
    store = make_store()
    store.start()
    seen: list = []
    store.subscribe("iot/**", seen.append)

    store.clear(source="switch")
    await settle()

    assert sorted(change.path for change in seen) == [
        "iot/device/d1/prop/2.1",
        "iot/device/d1/status/online",
    ]
    assert store.stats()["pending"] == 0
    store.stop()


async def test_clear_from_a_subscriber_callback_obeys_the_cascade_limit(caplog):
    """回调里再 clear 与 set / delete 同口径，超限被拒而不是无限递归。

    断言那条拒绝日志，不断言最终叶子数：回调最后一步就是 clear，级联有没有被拦住，
    最终叶子数都是 0，那个断言分不出两种情况。
    """
    store = StateStore()
    store.start()
    store._commit("iot/device/d1/status/online", True, source="align")

    def clear_then_rewrite(change):
        # clear 放前面：告警只报第一条，被 set 占了就看不出 clear 走没走这道闸
        store.clear(source="cb")
        store.set("iot/device/d2/status/online", True, source="cb")

    store.subscribe("iot/**", clear_then_rewrite)
    with caplog.at_level(logging.ERROR, logger="miloco.state.store"):
        store.set("iot/device/d1/status/online", False, source="align")
        await settle()

    rejected = [
        r.getMessage() for r in caplog.records if "cascade depth" in r.getMessage()
    ]
    assert any("rejecting clear" in m for m in rejected)
    store.stop()
