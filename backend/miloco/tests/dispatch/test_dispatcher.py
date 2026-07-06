# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for the backend AgentDispatcher (per-session single-flight + same-type merge).

Behaviors under test:
  * 同类合并：单飞期间到达的同类事件，合并进下一轮 turn 的一条 message。
  * builder 契约：builder 收到 *扁平合并后* 的 items；返回 None/空 → 跳过该批。
  * 单飞：同一 session 平台在途 turn 恒 ≤1。
  * 优先级 + 时序：_take_batch 取最高优先级类型、同类按入队时间升序。
  * 双层淘汰：超长时按 (类型优先级, 条目级 intra_priority, -时间) 淘汰最不紧急者；
    条目级仅参与淘汰、不改 _take_batch 渲染序；被淘汰的 dispatch 返回 False。
  * 超时 / 传输失败：均跳过该批、不写 agent_runs，drainer 存活继续。
  * 可观测：成功且类型 ∈ {interaction,rule,suggestion} 才 track_agent_run；bind 不统计。
  * 生命周期：stop() 取消在途 drainer；closed 后 dispatch 丢弃。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest
from miloco.agent_platform.base import AdapterTransportError
from miloco.config import get_settings
from miloco.dispatch import (
    AgentDispatcher,
    dispatch_event,
    get_agent_dispatcher,
    join_text_blocks,
    set_agent_dispatcher,
)
from miloco.dispatch import dispatcher as disp_mod
from miloco.dispatch.dispatcher import _QueuedEvent

# 队列上限的唯一真源现为 settings；测试读取它，与 dispatcher._enforce_cap 同源。
MAX_QUEUE = get_settings().dispatcher.max_queue


def _patch_with_turn(monkeypatch, turn):
    """【hermes-pr.md §五 #1+#1 完成】把旧 `run_agent_turn(msg, ...)` mock 转 mock adapter。

    旧 API: ``async turn(msg, *, session_key, lane, trace_id, wait_timeout_ms)`` → `(run_id, status, rtt_ms)`
    新 API: ``async adapter.send_turn(ctx)`` → AgentTurnResult
    关键差异:**不 mock _send_via_adapter**,而是 mock `get_adapter` 返 mock adapter。
    这样的好处:_send_via_adapter 的 retry 逻辑(AdapterTransportError → 重试)真实跑,
    `turn` 抛的异常会通过 adapter.send_turn 传播到 _send_via_adapter 的 except 块,
    模拟真实场景(adapter 报 transport error,dispatcher 重试)。

    不然如果 mock 整个 _send_via_adapter → retry 逻辑也被 mock 走,无法测 dispatcher 的重试。
    """
    adapter_send_turn_calls = []

    class _MockAdapter:
        name = "mock"

        async def send_turn(self, ctx):
            adapter_send_turn_calls.append(ctx)
            return await turn(
                ctx.text,
                session_key=ctx.session_key,
                lane=ctx.lane,
                trace_id=ctx.trace_id,
                wait_timeout_ms=ctx.wait_timeout_ms,
            )

        async def read_trace_meta(self, run_id):
            return None

        def build_system(self, profile, extra):
            return ""

    mock_adapter = _MockAdapter()
    monkeypatch.setattr(disp_mod, "get_adapter", lambda: mock_adapter)
    return mock_adapter, adapter_send_turn_calls


def _join(items: list) -> str | None:
    """Trivial builder: space-join string items; None when empty."""
    return " ".join(str(i) for i in items) if items else None


async def _settle(d: AgentDispatcher, timeout: float = 2.0) -> None:
    """Wait until all queues are empty and no drainer is in flight."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(0.01)
        if not d._draining and not any(d._queues.values()):
            return
    raise AssertionError("dispatcher did not settle within timeout")


@pytest.fixture
def patched(monkeypatch):
    """Patch the dispatcher module's collaborators; return a call recorder.

    【hermes-pr.md §五 #1+#2+#4+#11+#1 完成】dispatcher 改走 adapter-only(原 run_agent_turn
    webhook 路径已删)。测试 mock 改用 mock adapter 替换 get_adapter,默认 send_turn
    立即返 ok(模拟正常调通)。需要 slow/timeout/error 行为的测试自己再
    monkeypatch.setattr 替换 _send_via_adapter。
    """
    rec = SimpleNamespace(turns=[], tracks=[])

    class _MockAdapter:
        name = "mock"

        async def send_turn(self, ctx):
            rec.turns.append(
                SimpleNamespace(
                    msg=ctx.text,
                    session_key=ctx.session_key,
                    lane=ctx.lane,
                    trace_id=ctx.trace_id,
                    wait_timeout_ms=ctx.wait_timeout_ms,
                    profile=ctx.profile,
                    extra=ctx.extra,
                )
            )
            from types import SimpleNamespace as _NS
            return _NS(run_id=f"run-{len(rec.turns)}", status="ok", rtt_ms=1.0)

        async def read_trace_meta(self, run_id):
            return None

        def build_system(self, profile, extra):
            return ""

    mock_adapter = _MockAdapter()
    # patch get_adapter 返 mock,这样 dispatcher._send_via_adapter 能找到它
    monkeypatch.setattr(disp_mod, "get_adapter", lambda: mock_adapter)

    def fake_track(trace_id, run_id, source, rtt_ms):
        rec.tracks.append(
            SimpleNamespace(
                trace_id=trace_id, run_id=run_id, source=source, rtt_ms=rtt_ms
            )
        )

    monkeypatch.setattr(disp_mod, "track_agent_run", fake_track)

    # get_settings mock:dispatcher._enforce_cap 读 .dispatcher.max_queue
    monkeypatch.setattr(
        disp_mod,
        "get_settings",
        lambda: SimpleNamespace(
            dispatcher=SimpleNamespace(
                turn_wait_timeout_ms=30_000,
                max_queue=MAX_QUEUE,
                message_ttl_sec=1e9,
            )
        ),
    )

    return rec


# --------------------------------------------------------------- pure logic (no async)


def test_enforce_cap_evicts_least_urgent_first():
    """priority 数字大者(最不紧急)优先淘汰，即使它是最新入队的。"""
    d = AgentDispatcher()
    sk = "agent:main:miloco"
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    for i in range(MAX_QUEUE):
        q.append(_QueuedEvent("interaction", [f"i{i}"], _join, 0, now + i))
    # bind: least urgent (30) but newest — must still be the eviction victim.
    q.append(_QueuedEvent("bind", ["b"], _join, 30, now + 1000))

    d._enforce_cap(sk)

    assert len(q) == MAX_QUEUE
    assert all(e.event_type == "interaction" for e in q)


def test_enforce_cap_same_priority_evicts_oldest():
    """同优先级时淘汰最旧者。"""
    d = AgentDispatcher()
    sk = "agent:main:miloco"
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    for i in range(MAX_QUEUE + 3):
        q.append(_QueuedEvent("interaction", [f"i{i}"], _join, 0, now + i))

    d._enforce_cap(sk)

    assert len(q) == MAX_QUEUE
    kept = {e.items[0] for e in q}
    assert {"i0", "i1", "i2"}.isdisjoint(kept)  # 3 oldest evicted
    assert "i3" in kept


def test_enforce_cap_intra_priority_evicts_least_urgent_within_type():
    """同类型内,先淘汰条目级最不紧急(intra_priority 数字最大)者,即便它最新。"""
    d = AgentDispatcher()
    sk = "agent:main:miloco-suggest"
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    for i in range(MAX_QUEUE):  # 满队 high(intra=-2),最旧
        q.append(_QueuedEvent("suggestion", [f"h{i}"], _join, 20, now + i, -2))
    # low(intra=0)最新 → 因条目级最不紧急被淘汰。
    q.append(_QueuedEvent("suggestion", ["low_new"], _join, 20, now + 1000, 0))

    d._enforce_cap(sk)

    assert len(q) == MAX_QUEUE
    assert all(e.items[0] != "low_new" for e in q)


def test_enforce_cap_type_priority_dominates_intra():
    """类型优先级是第一层:更不紧急的类型即便条目级极紧急也先被淘汰。"""
    d = AgentDispatcher()
    sk = "agent:main:miloco"  # interaction(0) & bind(30) 共用
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    for i in range(MAX_QUEUE):
        q.append(_QueuedEvent("interaction", [f"i{i}"], _join, 0, now + i, 0))
    # bind 给一个极端紧急的 intra=-100,仍因类型最不紧急(30)而被淘汰。
    q.append(_QueuedEvent("bind", ["b"], _join, 30, now + 1, -100))

    d._enforce_cap(sk)

    assert len(q) == MAX_QUEUE
    assert all(e.event_type == "interaction" for e in q)


def _patch_ttl(monkeypatch, ttl_sec: float, max_queue: int = MAX_QUEUE) -> None:
    """Point disp_mod.get_settings at a stub with the given message_ttl_sec."""
    monkeypatch.setattr(
        disp_mod,
        "get_settings",
        lambda: SimpleNamespace(
            dispatcher=SimpleNamespace(
                turn_wait_timeout_ms=30_000,
                max_queue=max_queue,
                message_ttl_sec=ttl_sec,
            )
        ),
    )


def test_drop_expired_evicts_aged_keeps_fresh(monkeypatch):
    """入队龄超过 TTL 的清掉，龄内的保留。"""
    _patch_ttl(monkeypatch, ttl_sec=10.0)
    d = AgentDispatcher()
    sk = "agent:main:miloco"
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    q.append(_QueuedEvent("interaction", ["old"], _join, 0, now - 60))  # 60s 前，过期
    q.append(_QueuedEvent("interaction", ["fresh"], _join, 0, now - 1))  # 1s 前，未过期

    d._drop_expired(sk)

    assert [e.items[0] for e in d._queues[sk]] == ["fresh"]


def test_drop_expired_disabled_when_ttl_non_positive(monkeypatch):
    """TTL<=0 关闭过期：再旧也不清。"""
    _patch_ttl(monkeypatch, ttl_sec=0.0)
    d = AgentDispatcher()
    sk = "agent:main:miloco"
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    q.append(_QueuedEvent("interaction", ["ancient"], _join, 0, now - 10_000))

    d._drop_expired(sk)

    assert [e.items[0] for e in d._queues[sk]] == ["ancient"]


@pytest.mark.asyncio
async def test_drop_expired_resolves_delivered_false(monkeypatch):
    """过期即丢弃 → delivered future resolve False（与淘汰同语义）。"""
    _patch_ttl(monkeypatch, ttl_sec=10.0)
    d = AgentDispatcher()
    sk = "agent:main:miloco"
    now = time.monotonic()
    fut = asyncio.get_event_loop().create_future()
    ev = _QueuedEvent("interaction", ["old"], _join, 0, now - 60)
    ev.delivered = fut
    d._queues.setdefault(sk, []).append(ev)

    d._drop_expired(sk)

    assert fut.result() is False


def test_take_batch_drops_expired_before_packing(monkeypatch):
    """打包发送前先清过期：僵尸消息绝不进本轮 turn。"""
    _patch_ttl(monkeypatch, ttl_sec=10.0)
    d = AgentDispatcher()
    sk = "agent:main:miloco"
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    q.append(_QueuedEvent("interaction", ["stale"], _join, 0, now - 60))
    q.append(_QueuedEvent("interaction", ["live"], _join, 0, now - 1))

    batch = d._take_batch(sk)

    assert [e.items[0] for e in batch] == ["live"]


def test_take_batch_render_order_ignores_intra_priority():
    """渲染/出批仍按时序:_take_batch 不因 intra_priority 重排(后到的 high 仍排后)。"""
    d = AgentDispatcher()
    sk = "agent:main:miloco-suggest"
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    q.append(_QueuedEvent("suggestion", ["low_early"], _join, 20, now + 1, 0))
    q.append(_QueuedEvent("suggestion", ["high_late"], _join, 20, now + 5, -2))

    batch = d._take_batch(sk)

    assert [e.items[0] for e in batch] == ["low_early", "high_late"]


def test_take_batch_picks_highest_priority_and_sorts_by_time():
    d = AgentDispatcher()
    sk = "agent:main:miloco"  # shared by interaction(0) & bind(30)
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    q.append(_QueuedEvent("bind", ["b1"], _join, 30, now + 5))
    q.append(_QueuedEvent("interaction", ["i_late"], _join, 0, now + 9))
    q.append(_QueuedEvent("interaction", ["i_early"], _join, 0, now + 1))

    batch = d._take_batch(sk)

    # interaction (priority 0) wins over bind (30); time-sorted ascending.
    assert [e.event_type for e in batch] == ["interaction", "interaction"]
    assert [e.items[0] for e in batch] == ["i_early", "i_late"]
    # the un-chosen type stays queued.
    assert [e.event_type for e in d._queues[sk]] == ["bind"]


def test_join_text_blocks():
    assert join_text_blocks(["a", "b"]) == "a\n\nb"
    assert join_text_blocks(["only"]) == "only"
    assert join_text_blocks(["a", "", "b"]) == "a\n\nb"  # empties filtered
    assert join_text_blocks([""]) is None
    assert join_text_blocks([]) is None


# --------------------------------------------------------------- async behavior


@pytest.mark.asyncio
async def test_same_type_merge_into_one_turn(patched, monkeypatch):
    """单飞期间到达的同类事件合并到下一轮：builder 收到扁平合并列表。"""
    gate = asyncio.Event()
    builder_calls: list[list] = []

    def rec_builder(items):
        builder_calls.append(list(items))
        return "MSG:" + ",".join(items)

    n = {"i": 0}

    async def turn(msg, *, session_key, lane, trace_id, wait_timeout_ms):
        n["i"] += 1
        if n["i"] == 1:
            await gate.wait()  # hold turn-1 so b & c pile up behind it
        return f"run-{n['i']}", "ok", 1.0

    _patch_with_turn(monkeypatch, turn)

    d = AgentDispatcher()
    await d.start()
    try:
        await d.dispatch("interaction", ["a"], rec_builder)
        await asyncio.sleep(0.03)  # let drainer take [a] and block in turn-1
        await d.dispatch("interaction", ["b"], rec_builder)
        await d.dispatch("interaction", ["c"], rec_builder)
        gate.set()
        await _settle(d)
    finally:
        await d.stop()

    # turn-1 = [a] alone; turn-2 = [b, c] merged (single builder call, both items).
    assert ["a"] in builder_calls
    assert ["b", "c"] in builder_calls
    assert len(builder_calls) == 2


@pytest.mark.asyncio
async def test_builder_none_skips_turn(patched):
    d = AgentDispatcher()
    await d.start()
    try:
        accepted = await d.dispatch("interaction", [], _join)  # _join([]) -> None
        await _settle(d)
    finally:
        await d.stop()

    assert accepted is True  # enqueued fine
    assert patched.turns == []  # but nothing sent — builder produced no message


@pytest.mark.asyncio
async def test_single_flight_per_session(patched, monkeypatch):
    """同一 session 永不并发 turn(平台在途恒 ≤1)。"""
    state = {"inflight": 0, "max": 0}

    async def turn(msg, *, session_key, lane, trace_id, wait_timeout_ms):
        state["inflight"] += 1
        state["max"] = max(state["max"], state["inflight"])
        await asyncio.sleep(0.02)
        state["inflight"] -= 1
        return "run-x", "ok", 1.0

    _patch_with_turn(monkeypatch, turn)

    d = AgentDispatcher()
    await d.start()
    try:
        for i in range(6):
            await d.dispatch("interaction", [f"m{i}"], _join)
        await _settle(d)
    finally:
        await d.stop()

    assert state["max"] == 1


@pytest.mark.asyncio
async def test_tracks_on_success_with_source(patched):
    d = AgentDispatcher()
    await d.start()
    try:
        await d.dispatch("interaction", ["x"], _join)
        await d.dispatch("rule", ["y"], _join)  # different session — runs in parallel
        await _settle(d)
    finally:
        await d.stop()

    sources = {t.source for t in patched.tracks}
    assert sources == {"interaction", "rule"}


@pytest.mark.asyncio
async def test_bind_not_tracked(patched):
    d = AgentDispatcher()
    await d.start()
    try:
        await d.dispatch("bind", ["new device"], _join)
        await _settle(d)
    finally:
        await d.stop()

    assert len(patched.turns) == 1  # turn still sent
    assert patched.tracks == []  # but not recorded to agent_runs


@pytest.mark.asyncio
async def test_missing_run_id_not_tracked(patched, monkeypatch):
    async def turn(msg, *, session_key, lane, trace_id, wait_timeout_ms):
        return None, "ok", 1.0  # ok status but no runId

    _patch_with_turn(monkeypatch, turn)

    d = AgentDispatcher()
    await d.start()
    try:
        await d.dispatch("interaction", ["x"], _join)
        await _settle(d)
    finally:
        await d.stop()

    assert patched.tracks == []


@pytest.mark.asyncio
async def test_timeout_skips_and_does_not_track(patched, monkeypatch):
    async def turn(msg, *, session_key, lane, trace_id, wait_timeout_ms):
        return "run-x", "timeout", 1.0

    _patch_with_turn(monkeypatch, turn)

    d = AgentDispatcher()
    await d.start()
    try:
        await d.dispatch("interaction", ["x"], _join)
        await _settle(d)  # must not hang
    finally:
        await d.stop()

    assert patched.tracks == []


@pytest.mark.asyncio
async def test_transport_exception_retries_then_skips_and_survives(patched, monkeypatch):
    calls = 0

    async def turn(msg, *, session_key, lane, trace_id, wait_timeout_ms):
        nonlocal calls
        calls += 1
        raise AdapterTransportError("boom")  # 【hermes-pr.md §五 #1+#1 完成】新架构用 AdapterTransportError(替代 AgentWebhookException)

    _patch_with_turn(monkeypatch, turn)

    d = AgentDispatcher()
    d._TRANSPORT_BACKOFF_S = 0.0  # neutralize backoff sleeps for a fast test
    await d.start()
    try:
        await d.dispatch("interaction", ["x"], _join)
        await _settle(d)  # drainer retries transport, then swallows and finishes
    finally:
        await d.stop()

    # 传输失败被重试 _TRANSPORT_RETRIES+1 次后跳过该批,drainer 存活、不写 agent_runs。
    assert calls == d._TRANSPORT_RETRIES + 1
    assert patched.tracks == []


@pytest.mark.asyncio
async def test_fresh_trace_id_per_batch(patched):
    d = AgentDispatcher()
    await d.start()
    try:
        await d.dispatch("interaction", ["x"], _join)
        await _settle(d)
        await d.dispatch("interaction", ["y"], _join)
        await _settle(d)
    finally:
        await d.stop()

    assert len(patched.turns) == 2
    t0, t1 = patched.turns[0].trace_id, patched.turns[1].trace_id
    assert t0 != t1
    uuid.UUID(t0)  # parses as a valid uuid
    uuid.UUID(t1)


@pytest.mark.asyncio
async def test_dispatch_returns_false_when_new_event_evicted(patched):
    d = AgentDispatcher()
    await d.start()
    sk = "agent:main:miloco"
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    for i in range(MAX_QUEUE):  # full of urgent interactions
        q.append(_QueuedEvent("interaction", [f"i{i}"], _join, 0, now + i))
    try:
        # bind is least urgent → it is the victim of its own over-cap append.
        accepted = await d.dispatch("bind", ["late"], _join)
        await _settle(d)
    finally:
        await d.stop()

    assert accepted is False


@pytest.mark.asyncio
async def test_dispatch_expired_freed_space_admits_new_event(patched, monkeypatch):
    """新消息入队时清理过期腾空间：满队全过期 → 新消息不被超长淘汰而被接纳。"""
    _patch_ttl(monkeypatch, ttl_sec=10.0)
    d = AgentDispatcher()
    await d.start()
    monkeypatch.setattr(d, "_kick", lambda sk: None)  # 冻结 drainer，留住队列供检查
    sk = "agent:main:miloco"
    now = time.monotonic()
    q = d._queues.setdefault(sk, [])
    for i in range(MAX_QUEUE):  # 满队，且全部过期
        q.append(_QueuedEvent("interaction", [f"i{i}"], _join, 0, now - 60))
    try:
        # 若无过期清理，bind 最不紧急会被自己的超额 append 淘汰（False）；
        # 过期清理先腾空 → bind 被接纳。
        accepted = await d.dispatch("bind", ["late"], _join)
        assert accepted is True
        assert [e.items[0] for e in d._queues[sk]] == ["late"]
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_expired_event_dropped_before_send(patched, monkeypatch):
    """过期条目在发送前被清：turn 只收到新鲜内容，过期者 delivered=False。"""
    _patch_ttl(monkeypatch, ttl_sec=10.0)
    d = AgentDispatcher()
    await d.start()
    sk = "agent:main:miloco"
    now = time.monotonic()
    stale_fut = asyncio.get_event_loop().create_future()
    stale = _QueuedEvent("interaction", ["stale"], _join, 0, now - 60)
    stale.delivered = stale_fut
    try:
        d._queues.setdefault(sk, []).append(stale)  # 预置一条过期
        await d.dispatch("interaction", ["fresh"], _join)  # 新鲜的触发 drain
        await _settle(d)
    finally:
        await d.stop()

    assert len(patched.turns) == 1
    assert patched.turns[0].msg == "fresh"  # 仅新鲜条目送出
    assert stale_fut.result() is False  # 过期条目未送达


@pytest.mark.asyncio
async def test_ttl_disabled_keeps_aged_events(patched, monkeypatch):
    """TTL<=0 关闭过期：陈旧条目照常送出。"""
    _patch_ttl(monkeypatch, ttl_sec=0.0)
    d = AgentDispatcher()
    await d.start()
    sk = "agent:main:miloco"
    now = time.monotonic()
    try:
        d._queues.setdefault(sk, []).append(
            _QueuedEvent("interaction", ["ancient"], _join, 0, now - 10_000)
        )
        d._kick(sk)
        await _settle(d)
    finally:
        await d.stop()

    assert [t.msg for t in patched.turns] == ["ancient"]


@pytest.mark.asyncio
async def test_unknown_event_type_dropped(patched):
    d = AgentDispatcher()
    await d.start()
    try:
        accepted = await d.dispatch("nope", ["x"], _join)  # type: ignore[arg-type]
    finally:
        await d.stop()

    assert accepted is False
    assert patched.turns == []


@pytest.mark.asyncio
async def test_closed_dispatcher_drops(patched):
    d = AgentDispatcher()
    await d.start()
    await d.stop()

    assert await d.dispatch("interaction", ["x"], _join) is False
    assert patched.turns == []


@pytest.mark.asyncio
async def test_stop_cancels_inflight(patched, monkeypatch):
    async def turn(msg, *, session_key, lane, trace_id, wait_timeout_ms):
        await asyncio.sleep(3600)  # park forever; stop() must cancel it
        return "run-x", "ok", 1.0

    _patch_with_turn(monkeypatch, turn)

    d = AgentDispatcher()
    await d.start()
    await d.dispatch("interaction", ["x"], _join)
    await asyncio.sleep(0.03)  # let the drainer enter the parked turn
    assert d._tasks  # a drainer is in flight

    await d.stop()

    assert d._tasks == set()
    assert d._closed is True
    assert await d.dispatch("interaction", ["y"], _join) is False


@pytest.mark.asyncio
async def test_dispatch_event_routes_to_singleton(patched):
    d = AgentDispatcher()
    await d.start()
    set_agent_dispatcher(d)
    try:
        ok = await dispatch_event("interaction", ["hi"], _join)
        await _settle(d)
        assert ok is True
        assert get_agent_dispatcher() is d
        assert len(patched.turns) == 1
    finally:
        set_agent_dispatcher(None)
        await d.stop()


@pytest.mark.asyncio
async def test_dispatch_event_without_dispatcher_returns_false():
    set_agent_dispatcher(None)
    assert await dispatch_event("interaction", ["hi"], _join) is False


@pytest.mark.asyncio
async def test_dispatch_threads_intra_priority(patched, monkeypatch):
    """dispatch 把 intra_priority 落到队列事件上(冻结 drainer 以同步断言队列)。"""
    d = AgentDispatcher()
    await d.start()
    monkeypatch.setattr(d, "_kick", lambda sk: None)  # 冻结 drainer,留住事件供检查
    try:
        await d.dispatch("suggestion", ["s"], _join, intra_priority=-2)
        q = d._queues["agent:main:miloco-suggest"]
        assert len(q) == 1
        assert q[0].intra_priority == -2
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_dispatch_intra_priority_defaults_zero(patched, monkeypatch):
    """不传 intra_priority 时缺省 0(无内层优先级的类型行为不变)。"""
    d = AgentDispatcher()
    await d.start()
    monkeypatch.setattr(d, "_kick", lambda sk: None)
    try:
        await d.dispatch("interaction", ["x"], _join)
        assert d._queues["agent:main:miloco"][0].intra_priority == 0
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_dispatch_event_threads_intra_priority(patched, monkeypatch):
    """模块级 dispatch_event 透传 intra_priority 到单例。"""
    d = AgentDispatcher()
    await d.start()
    set_agent_dispatcher(d)
    monkeypatch.setattr(d, "_kick", lambda sk: None)
    try:
        await dispatch_event("suggestion", ["s"], _join, intra_priority=-1)
        assert d._queues["agent:main:miloco-suggest"][0].intra_priority == -1
    finally:
        set_agent_dispatcher(None)
        await d.stop()
