# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""StateStore 并发：多线程写入、订阅表写时复制、投递顺序与提交顺序一致。"""

from __future__ import annotations

import asyncio
import itertools
import sys
import threading

import pytest
from miloco.state import StateStore


@pytest.fixture(autouse=True)
def frequent_thread_switches():
    """把 GIL 切换间隔压到最小。

    默认间隔足够让一个线程把整轮循环跑完，读改写的竞争窗口根本撞不上——测试会在有 bug 的
    实现上照样绿。
    """
    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(original)


def run_threads(target, count: int) -> None:
    threads = [threading.Thread(target=target, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_concurrent_writes_to_one_subtree():
    """所有线程写同一个父节点下的不同 key。

    分散到各自的子树上时真正共享的只有 root 那一次插入，去掉锁也大半跑得过去。
    """
    writers, writes = 8, 300
    store = StateStore()

    def writer(index: int) -> None:
        for step in range(writes):
            store.set(f"iot/dev/prop/{index}-{step}", step, source="t")

    run_threads(writer, writers)

    assert (
        len(store.snapshot("iot/dev/prop/*")["iot"]["dev"]["prop"]) == writers * writes
    )
    assert store._leaf_count == writers * writes


def test_concurrent_unsubscribe_removes_every_subscriber():
    """钉的是订阅表写侧上锁：写时复制是读改写三步，不加锁会丢更新。

    只测退订不测订阅：GIL 版本里 `self._subs = self._subs + (x,)` 中间没有让出点，去掉锁
    也丢不了；退订那步走生成器，让出点是真实存在的，去掉锁就红。
    """
    store = StateStore()
    unsubscribes = [store.subscribe("a/b", lambda change: None) for _ in range(800)]

    run_threads(lambda index: [item() for item in unsubscribes[index::4]], 4)

    assert store._subs == ()


async def test_event_timestamps_never_go_backwards(monkeypatch):
    """钉的是取时间在锁内：放锁外时先取到时间的线程可能后提交。

    事件按提交顺序投递，所以 `at` 必须单调不减。用自增的 `now_ms` 放大——真实的 `now_ms`
    同一毫秒内返回同一个数，大部分乱序被这个粒度盖住了。
    """
    ticks = itertools.count(1)
    monkeypatch.setattr("miloco.state.store.now_ms", lambda: next(ticks))
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("race/value", seen.append)

    def writer(index: int) -> None:
        for step in range(500):
            store.set("race/value", f"{index}-{step}", source="t")

    run_threads(writer, 8)
    for _ in range(500):
        await asyncio.sleep(0)

    stamps = [change.at for change in seen]
    assert stamps == sorted(stamps)


async def test_events_chain_without_gaps():
    """钉的是排队在锁内：挪到锁外时投递顺序会与树的提交顺序相反。

    断言相邻事件首尾相接（后一条的 `old` 等于前一条的 `new`）。只比对最后一条事件与树的
    最终值抓不住——顺序反了，最后一条往往还是对的。写入量按实测定：低于这个量级，排队挪到
    锁外也撞不出乱序，测试会在有 bug 的实现上变绿。
    """
    writers, writes = 16, 1000
    store = StateStore()
    store.start()
    seen: list = []
    store.subscribe("race/value", seen.append)

    def writer(index: int) -> None:
        for step in range(writes):
            store.set("race/value", f"{index}-{step}", source="t")

    run_threads(writer, writers)
    for _ in range(500):
        await asyncio.sleep(0)

    assert len(seen) == writers * writes
    assert seen[-1].new == store.get("race/value")
    assert [
        (previous.new, current.old)
        for previous, current in zip(seen, seen[1:])
        if previous.new != current.old
    ] == []
