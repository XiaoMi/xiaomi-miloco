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
from unittest.mock import MagicMock, patch

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

    result = RealtimePerceptionResult(
        matched_rules=[
            MatchedRule(rule_id="r1", reason="x",
                        room_name="卧室", source_device_ids=["cam-002"])
        ],
    )
    with patch("miloco.manager.get_manager", return_value=fake_mgr):
        await proxy.handle_realtime_perception_result(result)

    assert seen == [
        {
            "trigger_room": "卧室",
            "trigger_dids": ["cam-002"],
            "caption": "",
            "device_name": "",
            "cycle_source_states": {"cam-002": True},
        }
    ]


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
    fake_mgr.rule_service.get_enabled_rule_ids = MagicMock(return_value=[])

    with patch("miloco.manager.get_manager", return_value=fake_mgr), \
         patch("miloco.perception.client.dispatch_event", new_callable=AsyncMock) as disp:
        await proxy.handle_realtime_perception_result(result)

    disp.assert_awaited_once()
    assert [s.id for s in disp.await_args.args[1]] == [1]


# test_unmatched_enabled_rules_get_false_each_cycle / test_unmatched_skips_early_sent_rules
# 已迁移到 test_perception_client_rule_dispatch.py(per-device 状态机重构后,
# false 广播改为按 device_rule_map 精确推退,旧 case 的全集 enabled rule 模型不再适用)。


# ─── 按摄像头语音开关闸门（_filter_voice_enabled + dispatch gate）───────────────


def _voice_mgr(voice_allowed: set[str]) -> MagicMock:
    """构造一个 get_manager() 返回值,其 kv_repo 让 voice_allowed_camera_dids 返回给定集合。"""
    import json as _json

    from miloco.database.kv_repo import ScopeConfigKeys

    store = {ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY: _json.dumps(list(voice_allowed))}
    kv = MagicMock()
    kv.get = lambda key, default=None: store.get(key, default)
    mgr = MagicMock()
    mgr.kv_repo = kv
    return mgr


def test_filter_voice_enabled_keeps_only_allowlisted_did():
    """source_device_ids[0] 在语音白名单 → 保留；不在（未开启拾音）→ 丢弃。"""
    from miloco.perception.client import _filter_voice_enabled
    from miloco.perception.types import Speech

    s_off = Speech(needs_response=True, speaker="爸爸", content="开灯",
                   source_device_ids=["cam-off"], device_name="客厅相机")
    s_on = Speech(needs_response=True, speaker="妈妈", content="关灯",
                  source_device_ids=["cam-on"], device_name="卧室相机")

    with patch("miloco.manager.get_manager", return_value=_voice_mgr({"cam-on"})):
        kept = _filter_voice_enabled([s_off, s_on])

    assert [s.content for s in kept] == ["关灯"]


def test_filter_voice_enabled_empty_allowlist_drops_all():
    """**默认关**:白名单为空 = 无相机开启拾音 → 丢弃全部 speech。"""
    from miloco.perception.client import _filter_voice_enabled
    from miloco.perception.types import Speech

    s = Speech(needs_response=True, speaker="x", content="c", source_device_ids=["d"])
    with patch("miloco.manager.get_manager", return_value=_voice_mgr(set())):
        assert _filter_voice_enabled([s]) == []


def test_filter_voice_enabled_fail_closed_on_lookup_error():
    """读 KV/manager 失败 → fail-closed,丢弃全部（默认关语义下不处理未授权音频）。"""
    from miloco.perception.client import _filter_voice_enabled
    from miloco.perception.types import Speech

    s = Speech(needs_response=True, speaker="x", content="c", source_device_ids=["d"])
    with patch("miloco.manager.get_manager", side_effect=RuntimeError("boom")):
        assert _filter_voice_enabled([s]) == []


async def test_handle_realtime_drops_voice_disabled_speech(proxy):
    """终态 dispatch 路径:未在语音白名单(未开启拾音)相机的 speech 指令不 dispatch,开启相机的照常发。"""
    from unittest.mock import AsyncMock

    from miloco.perception.types import RealtimePerceptionResult, Speech

    result = RealtimePerceptionResult(
        speeches=[
            Speech(needs_response=True, speaker="爸爸", content="开灯",
                   is_complete=True, source_device_ids=["cam-off"]),
            Speech(needs_response=True, speaker="妈妈", content="关灯",
                   is_complete=True, source_device_ids=["cam-on"]),
        ],
    )

    fake_mgr = _voice_mgr({"cam-on"})

    async def _noop_update(*a, **k):
        ...

    fake_mgr.rule_service.update_state = _noop_update
    fake_mgr.rule_service.get_enabled_rule_ids = MagicMock(return_value=[])

    with patch("miloco.manager.get_manager", return_value=fake_mgr), \
         patch("miloco.perception.client.dispatch_event", new_callable=AsyncMock) as disp:
        await proxy.handle_realtime_perception_result(result)

    # 只发开启拾音相机(cam-on)的「关灯」；未开启相机(cam-off)的「开灯」丢弃
    disp.assert_awaited_once()
    dispatched = disp.await_args.args[1]
    assert [s.content for s in dispatched] == ["关灯"]


async def test_handle_realtime_dispatches_when_voice_enabled(proxy):
    """对照组:相机已开启拾音(在白名单) → speech 指令照常 dispatch。"""
    from unittest.mock import AsyncMock

    from miloco.perception.types import RealtimePerceptionResult, Speech

    result = RealtimePerceptionResult(
        speeches=[
            Speech(needs_response=True, speaker="爸爸", content="开灯",
                   is_complete=True, source_device_ids=["cam-on"]),
        ],
    )
    fake_mgr = _voice_mgr({"cam-on"})

    async def _noop_update(*a, **k):
        ...

    fake_mgr.rule_service.update_state = _noop_update
    fake_mgr.rule_service.get_enabled_rule_ids = MagicMock(return_value=[])

    with patch("miloco.manager.get_manager", return_value=fake_mgr), \
         patch("miloco.perception.client.dispatch_event", new_callable=AsyncMock) as disp:
        await proxy.handle_realtime_perception_result(result)

    disp.assert_awaited_once()
    assert [s.content for s in disp.await_args.args[1]] == ["开灯"]


async def test_early_speeches_voice_disabled_not_dispatched(proxy):
    """早出路径:_on_early_speeches 内的语音闸门必须拦下未开启拾音相机的指令。

    防回归钉:早出闸门被删时,终态闸门救不回来——早出泄漏的指令已 dispatch 且进
    early_sent_contents,终态路径按内容去重直接跳过。此测试直打
    _realtime_perceive_impl 的 on_early_speeches 回调,钉住早出闸门本身。
    """
    from unittest.mock import AsyncMock

    from miloco.perception.types import Speech

    main_loop = asyncio.get_running_loop()

    async def engine_realtime(*args, **kwargs):
        await kwargs["on_early_speeches"]([
            Speech(needs_response=True, speaker="爸爸", content="开灯",
                   is_complete=True, source_device_ids=["cam-off"]),
        ])
        return _empty_result()

    proxy.perception_engine.realtime_perceive = engine_realtime

    with patch("miloco.manager.get_manager", return_value=_voice_mgr(set())), \
         patch("miloco.perception.client.dispatch_event", new_callable=AsyncMock) as disp:
        await proxy._realtime_perceive_impl(
            _stub_snapshot(), [], 0, 0.0, main_loop, [],
        )

    disp.assert_not_awaited()


async def test_early_speeches_voice_enabled_dispatched(proxy):
    """对照组:相机在白名单内(已开启拾音) → 早出指令照常 dispatch（闸门有选择性）。"""
    from unittest.mock import AsyncMock

    from miloco.perception.types import Speech

    main_loop = asyncio.get_running_loop()

    async def engine_realtime(*args, **kwargs):
        await kwargs["on_early_speeches"]([
            Speech(needs_response=True, speaker="妈妈", content="关灯",
                   is_complete=True, source_device_ids=["cam-on"]),
        ])
        return _empty_result()

    proxy.perception_engine.realtime_perceive = engine_realtime

    with patch("miloco.manager.get_manager", return_value=_voice_mgr({"cam-on"})), \
         patch("miloco.perception.client.dispatch_event", new_callable=AsyncMock) as disp:
        await proxy._realtime_perceive_impl(
            _stub_snapshot(), [], 0, 0.0, main_loop, [],
        )

    disp.assert_awaited_once()
    assert [s.content for s in disp.await_args.args[1]] == ["关灯"]


# ─── 语音闸门在 meaningful_events 落库路径（_persist_meaningful_event）─────────


async def test_persist_skips_asr_only_window_from_voice_disabled_cam():
    """纯 ASR 窗口 + 相机未开启拾音 → 过滤后无 speech,不入 meaningful_events。

    语音开关 = 不执行也不记录:未在白名单相机的定向指令转写不落库、不推 SSE。
    """
    from miloco.perception.client import _persist_meaningful_event
    from miloco.perception.snapshot_context import OmniEventArtifacts
    from miloco.perception.types import RealtimePerceptionResult, Speech

    result = RealtimePerceptionResult(
        speeches=[
            Speech(needs_response=True, speaker="爸爸", content="开灯",
                   is_complete=True, source_device_ids=["cam-off"]),
        ],
    )
    fake_mgr = _voice_mgr(set())

    with patch("miloco.manager.get_manager", return_value=fake_mgr):
        await _persist_meaningful_event(
            result=result, device_ids=["cam-off"], artifacts=OmniEventArtifacts(),
        )

    fake_mgr.meaningful_events_dao.insert.assert_not_called()
    # persist 用 model_copy 过滤,不原地改与主路径(规则匹配/dispatch)共享的 result
    assert len(result.speeches) == 1


async def test_persist_keeps_asr_window_from_voice_enabled_cam():
    """对照组:相机在语音白名单内(已开启拾音) → ASR 窗口照常入表,has_asr=True。"""
    from miloco.perception.client import _persist_meaningful_event
    from miloco.perception.snapshot_context import OmniEventArtifacts
    from miloco.perception.types import RealtimePerceptionResult, Speech

    result = RealtimePerceptionResult(
        speeches=[
            Speech(needs_response=True, speaker="妈妈", content="关灯",
                   is_complete=True, source_device_ids=["cam-on"]),
        ],
    )
    fake_mgr = _voice_mgr({"cam-on"})

    with patch("miloco.manager.get_manager", return_value=fake_mgr):
        await _persist_meaningful_event(
            result=result, device_ids=["cam-on"], artifacts=OmniEventArtifacts(),
        )

    fake_mgr.meaningful_events_dao.insert.assert_called_once()
    kwargs = fake_mgr.meaningful_events_dao.insert.call_args.kwargs
    assert kwargs["has_asr"] is True
    assert "关灯" in kwargs["text"]


async def test_persist_mixed_window_excludes_voice_disabled_transcript():
    """混合窗口:视觉建议 + 语音关闭相机的 speech → 事件仍入表(视觉产物不受影响),
    但落库内容(text / payload_json / has_asr)不含被拦截的转写。"""
    from miloco.perception.client import _persist_meaningful_event
    from miloco.perception.snapshot_context import OmniEventArtifacts
    from miloco.perception.types import (
        RealtimePerceptionResult,
        Speech,
        Suggestion,
    )

    result = RealtimePerceptionResult(
        suggestions=[Suggestion(event="水龙头没关", action="提醒", urgency="low")],
        speeches=[
            Speech(needs_response=True, speaker="爸爸", content="开灯",
                   is_complete=True, source_device_ids=["cam-off"]),
        ],
    )
    fake_mgr = _voice_mgr(set())

    with patch("miloco.manager.get_manager", return_value=fake_mgr):
        await _persist_meaningful_event(
            result=result, device_ids=["cam-off"], artifacts=OmniEventArtifacts(),
        )

    fake_mgr.meaningful_events_dao.insert.assert_called_once()
    kwargs = fake_mgr.meaningful_events_dao.insert.call_args.kwargs
    assert kwargs["has_suggestion"] is True
    assert kwargs["has_asr"] is False  # speech 已被闸门滤掉
    assert "水龙头没关" in kwargs["text"]
    assert "开灯" not in kwargs["text"]
    assert "开灯" not in kwargs["payload_json"]
    # 原 result 不被原地改
    assert len(result.speeches) == 1
