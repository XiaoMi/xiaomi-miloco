# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for PerceptionEngineProxy early-callback main-loop dispatch.

Production path: realtime_perceive() offloads _realtime_perceive_impl to an
inference thread via run_in_executor + asyncio.run, so the impl coroutine
runs on a temporary event loop. The engine awaits early callbacks from that
temp loop. Without dispatching back, any task spawned inside (e.g.
RuleRunner._spawn_fire's create_task) ends up on the temp loop and gets
cancelled when asyncio.run() exits — even when held in a strong-reference
set, because the issue is loop closure, not GC.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.perception.client import PerceptionEngineProxy
from miloco.perception.types import (
    BatchedSnapshot,
    MatchedRule,
    RealtimePerceptionResult,
)


@pytest.fixture
def proxy():
    """Build a PerceptionEngineProxy without invoking the real engine __init__
    (which loads model configs). Wires only what the tests under test rely on."""
    p = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
    p.perception_engine = MagicMock()
    p._last_captions = {}
    p._executor = None
    return p


def _empty_result() -> RealtimePerceptionResult:
    return RealtimePerceptionResult(skipped=True)


def _stub_snapshot() -> BatchedSnapshot:
    return BatchedSnapshot(snapshots=[], captured_at=0.0)


async def test_matched_rules_callback_runs_on_main_loop(proxy):
    """When impl runs on a temp loop in the inference thread, the matched-rules
    callback body must execute on the main loop. Otherwise update_state →
    _spawn_fire would create_task on the temp loop and lose it on close."""

    main_loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    seen: list[tuple[int, int]] = []

    async def engine_realtime(*args, **kwargs):
        await kwargs["on_early_matched_rules"]([
            MatchedRule(rule_id="r1", confidence=1.0, reason="x")
        ])
        return _empty_result()

    proxy.perception_engine.realtime_perceive = engine_realtime

    async def capture(rule_id, source, value, reason=None, **kwargs):
        seen.append((id(asyncio.get_running_loop()), threading.get_ident()))

    fake_mgr = MagicMock()
    fake_mgr.rule_service.update_state = capture
    fake_mgr.rule_service.get_all_rules = AsyncMock(return_value=[])

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-infer")
    try:
        with patch("miloco.manager.get_manager", return_value=fake_mgr):
            await main_loop.run_in_executor(
                executor,
                lambda: asyncio.run(
                    proxy._realtime_perceive_impl(
                        _stub_snapshot(), [], 0, 0.0, main_loop, [],
                    )
                ),
            )
    finally:
        executor.shutdown(wait=True)

    assert len(seen) == 1
    seen_loop_id, seen_thread_id = seen[0]
    assert seen_loop_id == id(main_loop), (
        "callback ran on temp loop; loop closure would cancel any task it spawns"
    )
    assert seen_thread_id == main_thread


async def test_early_matched_rules_meta_passed_to_update_state(proxy):
    """早出路径：MatchedRule 上的 room_name / source_device_ids 透传给 update_state。"""

    main_loop = asyncio.get_running_loop()
    seen: list[dict] = []

    async def engine_realtime(*args, **kwargs):
        await kwargs["on_early_matched_rules"]([
            MatchedRule(rule_id="r1", reason="x",
                        room_name="客厅", source_device_ids=["cam-001"],
                        device_name="小米摄像机")
        ])
        return _empty_result()

    proxy.perception_engine.realtime_perceive = engine_realtime

    async def capture(rule_id, source, value, reason=None, **kwargs):
        seen.append(kwargs)

    fake_mgr = MagicMock()
    fake_mgr.rule_service.update_state = capture
    fake_mgr.rule_service.get_all_rules = AsyncMock(return_value=[])

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-infer")
    try:
        with patch("miloco.manager.get_manager", return_value=fake_mgr):
            await main_loop.run_in_executor(
                executor,
                lambda: asyncio.run(
                    proxy._realtime_perceive_impl(
                        _stub_snapshot(), [], 0, 0.0, main_loop, [],
                    )
                ),
            )
    finally:
        executor.shutdown(wait=True)

    assert seen == [{"trigger_room": "客厅", "trigger_dids": ["cam-001"], "caption": "", "device_name": "小米摄像机"}]


async def test_final_matched_rules_meta_passed_to_update_state(proxy):
    """全量路径（handle_realtime_perception_result）：meta 同样透传。"""
    seen: list[dict] = []

    async def capture(rule_id, source, value, reason=None, **kwargs):
        seen.append(kwargs)

    fake_mgr = MagicMock()
    fake_mgr.rule_service.update_state = capture
    fake_mgr.rule_service.get_all_rules = AsyncMock(return_value=[])

    result = RealtimePerceptionResult(
        matched_rules=[
            MatchedRule(rule_id="r1", reason="x",
                        room_name="卧室", source_device_ids=["cam-002"])
        ],
    )
    with patch("miloco.manager.get_manager", return_value=fake_mgr):
        await proxy.handle_realtime_perception_result(result)

    assert seen == [{"trigger_room": "卧室", "trigger_dids": ["cam-002"], "caption": "", "device_name": ""}]


async def test_spawn_in_callback_survives_temp_loop_close(proxy):
    """Tasks created inside the callback (mimicking _spawn_fire) must run on
    the main loop and outlive the temp loop. Holding a strong reference is
    not enough — the loop itself must remain open. We verify by asserting the
    spawned task completes successfully after realtime_perceive returns."""

    main_loop = asyncio.get_running_loop()
    spawned_done = asyncio.Event()
    spawned_task_holder: list[asyncio.Task] = []

    async def engine_realtime(*args, **kwargs):
        await kwargs["on_early_matched_rules"]([
            MatchedRule(rule_id="r1", confidence=1.0, reason="x")
        ])
        return _empty_result()

    proxy.perception_engine.realtime_perceive = engine_realtime

    async def background_work():
        await asyncio.sleep(0.05)
        spawned_done.set()

    async def fake_update_state(*args, **kwargs):
        # Mimics RuleRunner._spawn_fire: fire-and-forget create_task.
        # If this runs on the temp loop, the task dies when asyncio.run() exits.
        spawned_task_holder.append(asyncio.create_task(background_work()))

    fake_mgr = MagicMock()
    fake_mgr.rule_service.update_state = fake_update_state
    fake_mgr.rule_service.get_all_rules = AsyncMock(return_value=[])

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-infer")
    try:
        with patch("miloco.manager.get_manager", return_value=fake_mgr):
            await main_loop.run_in_executor(
                executor,
                lambda: asyncio.run(
                    proxy._realtime_perceive_impl(
                        _stub_snapshot(), [], 0, 0.0, main_loop, [],
                    )
                ),
            )
    finally:
        executor.shutdown(wait=True)

    assert spawned_task_holder, "callback should have spawned a task"
    task = spawned_task_holder[0]
    assert task.get_loop() is main_loop, "spawned task is on the wrong loop"

    await asyncio.wait_for(spawned_done.wait(), timeout=1.0)
    assert task.done() and not task.cancelled()


async def test_no_executor_fallback_runs_callback_inline(proxy):
    """When self._executor is None, _realtime_perceive_impl runs on the main
    loop directly. The wrapper must short-circuit (current is main_loop) and
    not introduce cross-thread overhead."""

    main_loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    seen: list[tuple[int, int]] = []

    async def engine_realtime(*args, **kwargs):
        await kwargs["on_early_matched_rules"]([
            MatchedRule(rule_id="r1", confidence=1.0, reason="x")
        ])
        return _empty_result()

    proxy.perception_engine.realtime_perceive = engine_realtime

    async def capture(rule_id, source, value, reason=None, **kwargs):
        seen.append((id(asyncio.get_running_loop()), threading.get_ident()))

    fake_mgr = MagicMock()
    fake_mgr.rule_service.update_state = capture
    fake_mgr.rule_service.get_all_rules = AsyncMock(return_value=[])

    with patch("miloco.manager.get_manager", return_value=fake_mgr):
        await proxy._realtime_perceive_impl(
            _stub_snapshot(), [], 0, 0.0, main_loop, [],
        )

    assert seen == [(id(main_loop), main_thread)]


async def test_handle_realtime_skips_early_sent_suggestions(proxy):
    """per-omni:result.suggestions 含本窗全部新链(供 dump/上下文完整),但已早送的
    (id ∈ early_sent_sugg_ids)在发送侧跳过、不对 Agent 重发;未早送的(batch 新链)照常发。
    投递走 main 的 dispatch_event("suggestion", items, builder, intra_priority)。"""
    from unittest.mock import AsyncMock

    from miloco.perception.types import Suggestion

    s_sent = Suggestion(event="老人摔倒", action="查看", urgency="high", id=1)
    s_fresh = Suggestion(event="水龙头没关", action="提醒", urgency="low", id=2)
    result = RealtimePerceptionResult(suggestions=[s_sent, s_fresh])

    fake_mgr = MagicMock()
    async def _noop_update(*a, **k):
        ...

    fake_mgr.rule_service.update_state = _noop_update
    fake_mgr.rule_service.get_all_rules = AsyncMock(return_value=[])
    fake_mgr.rule_service.get_enabled_rule_ids = MagicMock(return_value=[])

    with patch("miloco.manager.get_manager", return_value=fake_mgr), \
         patch("miloco.perception.client.dispatch_event", new_callable=AsyncMock) as disp:
        await proxy.handle_realtime_perception_result(
            result, early_sent_sugg_ids={1},
        )

    # 只发未早送的 s_fresh(id=2);已早送的 s_sent(id=1)跳过(防双发)
    disp.assert_awaited_once()
    assert [s.id for s in disp.await_args.args[1]] == [2]
    # dump 完整:result.suggestions 两条都还在(本方法不改 result)
    assert [s.id for s in result.suggestions] == [1, 2]


async def test_handle_realtime_sends_all_when_no_early_sent(proxy):
    """batch 模式无早送(early_sent_sugg_ids 为空)→ result.suggestions 全量上报。"""
    from unittest.mock import AsyncMock

    from miloco.perception.types import Suggestion

    result = RealtimePerceptionResult(
        suggestions=[Suggestion(event="有人敲门", action="查看", urgency="medium", id=1)],
    )

    fake_mgr = MagicMock()
    async def _noop_update(*a, **k):
        ...

    fake_mgr.rule_service.update_state = _noop_update
    fake_mgr.rule_service.get_all_rules = AsyncMock(return_value=[])
    fake_mgr.rule_service.get_enabled_rule_ids = MagicMock(return_value=[])

    with patch("miloco.manager.get_manager", return_value=fake_mgr), \
         patch("miloco.perception.client.dispatch_event", new_callable=AsyncMock) as disp:
        await proxy.handle_realtime_perception_result(result)

    disp.assert_awaited_once()
    assert [s.id for s in disp.await_args.args[1]] == [1]


# test_unmatched_enabled_rules_get_false_each_cycle / test_unmatched_skips_early_sent_rules
# 已迁移到 test_perception_client_rule_dispatch.py(per-device 状态机重构后,
# false 广播改为按 device_rule_map 精确推退,旧 case 的全集 enabled rule 模型不再适用)。
