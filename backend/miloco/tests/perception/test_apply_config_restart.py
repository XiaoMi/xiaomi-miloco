# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for PerceptionService.apply_config_restart / apply_omni_fps_live.

「应用设置」改感知参数后需真正生效，两条并列入口、各自最小代价：

- ``apply_config_restart``（window_size 变）：停 runner → 启 runner 让 start 重读窗口，
  不重建引擎、不重载模型。was_running 才需重启（未跑时下次 start 自然读新值）。
- ``apply_omni_fps_live``（omni_fps 变）：运行时热更，透传 pipeline 把新 omni_fps
  原地推给活跃引擎，不停 runner、不重建、不重载模型、不丢 track。

两者全程持 lifecycle 锁串行化，避免与并发 start/stop 交错。覆盖：

- apply_config_restart running：stop → start，返 True
- apply_config_restart not running：全 no-op（不误拉起 runner）
- apply_config_restart stop/start 抛异常 → 返 False 不冒泡（config 已写盘，调用方据
  restart_ok 区分「已保存但重启失败」，否则前端误报「保存失败」）
- apply_omni_fps_live：透传 pipeline.apply_omni_fps，返 True；抛异常 → 返 False 不冒泡
- lifecycle 锁串行化 apply_config_restart 与并发 start/stop，防交错状态错乱
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from miloco.perception.inference_admission import InferenceDrainDeferred
from miloco.perception.omni_probe_registry import _OMNI_PROBE_TASKS
from miloco.perception.processor import PipelineProcessor
from miloco.perception.runner import PerceptionRunner
from miloco.perception.service import PerceptionService
from miloco.perception.types import RealtimePerceptionResult


def _make_service(*, is_running: bool) -> PerceptionService:
    """Build a service bypassing __init__ with mocked engine/pipeline."""
    svc = PerceptionService.__new__(PerceptionService)
    svc._collector = MagicMock()
    svc._pipeline = MagicMock()
    svc._pipeline.apply_omni_fps = AsyncMock()
    svc._engine = MagicMock()
    svc._engine.is_running = is_running
    svc._engine.start = AsyncMock()
    svc._engine.stop = AsyncMock()
    svc._log_repo = MagicMock()
    svc._lifecycle_lock = asyncio.Lock()
    return svc


# ---- apply_config_restart（window_size：stop → start，不重建引擎） -------------


@pytest.mark.asyncio
async def test_apply_config_restart_running_does_stop_start():
    """引擎在跑:stop → start 重启 runner 重读窗口，返 True。不重建引擎、不重载模型。"""
    svc = _make_service(is_running=True)

    assert await svc.apply_config_restart() is True

    svc._engine.stop.assert_awaited_once()
    svc._engine.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_config_restart_not_running_is_noop():
    """引擎未运行:全 no-op——不误拉起一个没人配置意图的 runner。
    window_size 靠下次用户 start 时 runner 重读。"""
    svc = _make_service(is_running=False)

    assert await svc.apply_config_restart() is True

    svc._engine.stop.assert_not_awaited()
    svc._engine.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_config_restart_stop_failure_returns_false():
    """stop 抛异常 → 返 False，不冒泡成 500。

    config 已由调用方写盘(不可回滚)，返 False 让 PUT 端点带 restart_ok=False，
    前端提示「已保存但需手动重启」而非「保存失败」。
    """
    svc = _make_service(is_running=True)
    svc._engine.stop = AsyncMock(side_effect=InferenceDrainDeferred("busy"))

    assert await svc.apply_config_restart() is False

    svc._engine.start.assert_not_awaited()  # stop 抛错，start 未到达


@pytest.mark.asyncio
async def test_apply_config_restart_start_failure_returns_false():
    """start 阶段抛异常也返 False(不冒泡)。"""
    svc = _make_service(is_running=True)
    svc._engine.start = AsyncMock(side_effect=RuntimeError("sync devices failed"))

    assert await svc.apply_config_restart() is False


# ---- apply_omni_fps_live（omni_fps：运行时热更，不停 runner） ------------------


@pytest.mark.asyncio
async def test_apply_omni_fps_live_delegates_to_pipeline():
    """透传 pipeline.apply_omni_fps(新值)，返 True。不 stop/start runner（热更）。"""
    svc = _make_service(is_running=True)

    assert await svc.apply_omni_fps_live(2) is True

    svc._pipeline.apply_omni_fps.assert_awaited_once_with(2)
    svc._engine.stop.assert_not_awaited()
    svc._engine.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_omni_fps_live_failure_returns_false():
    """热更抛异常 → 返 False，不冒泡成 500（config 已写盘）。"""
    svc = _make_service(is_running=True)
    svc._pipeline.apply_omni_fps = AsyncMock(side_effect=RuntimeError("boom"))

    assert await svc.apply_omni_fps_live(2) is False


# ---- lifecycle 锁串行化 -------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_lock_serializes_restart_and_stop():
    """lifecycle 锁串行化:apply_config_restart 持锁期间,并发 stop_engine 必须等待,
    不会在 restart 的 stop→start 之间穿插执行(防 _is_running 交错错乱)。

    构造:restart(is_running=True)进入 start(其临界区最后一步、持锁、含让出点)后,
    并发发起 stop_engine;在 start 中途直接断言——锁仍 locked、且此刻 _engine.stop
    只被调过 1 次(restart 自己那次),证明并发 stop_engine 被锁挡在临界区外。
    """
    svc = _make_service(is_running=True)
    in_start = asyncio.Event()
    lock_states: list[bool] = []

    async def _start_probe():
        in_start.set()
        # 给并发 stop_engine() 充分的调度机会去尝试抢锁
        for _ in range(3):
            await asyncio.sleep(0)
        # 若锁生效:并发 stop_engine() 此刻仍卡在 async with 外 → 锁被 restart 持有
        lock_states.append(svc._lifecycle_lock.locked())
        # 且 restart 自己的 stop 已调 1 次、并发 stop_engine 的 stop 尚未进入
        lock_states.append(svc._engine.stop.call_count == 1)

    svc._engine.start = AsyncMock(side_effect=_start_probe)

    async def _wait_and_stop():
        await in_start.wait()  # restart 已进 start(持锁中)
        await svc.stop_engine()  # 真实走 service 锁，应阻塞到 restart 释放

    await asyncio.gather(svc.apply_config_restart(), _wait_and_stop())

    # start 中途:锁被 restart 持有 且 并发 stop_engine 临界区尚未进入 → 二者原子互斥
    assert lock_states == [True, True]
    # 最终 stop 被调 2 次(restart 自己 1 次 + 并发 stop_engine 释放后 1 次)
    assert svc._engine.stop.call_count == 2


async def test_lifecycle_writer_retains_lock_when_request_is_cancelled():
    svc = _make_service(is_running=True)
    closing = asyncio.Event()
    release = asyncio.Event()
    trying_to_start = asyncio.Event()

    async def close():
        closing.set()
        await release.wait()

    async def start():
        trying_to_start.set()
        await svc.start_engine()

    svc._engine.stop.side_effect = close
    stop_request = asyncio.create_task(svc.stop_engine())
    start_request = None
    try:
        await asyncio.wait_for(closing.wait(), 1)
        stop_request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_request

        start_request = asyncio.create_task(start())
        await asyncio.wait_for(trying_to_start.wait(), 1)
        assert svc._lifecycle_lock.locked()
        svc._engine.start.assert_not_awaited()
    finally:
        release.set()
        if start_request is not None:
            await asyncio.wait_for(start_request, 1)

    svc._engine.start.assert_awaited_once()


@pytest.mark.parametrize("entry", ["realtime", "on_demand"])
async def test_runner_defers_teardown_until_cancelled_inference_waiter_has_finished(
    monkeypatch, entry
):
    collector = MagicMock()
    proxy = MagicMock()
    pipeline = PipelineProcessor(collector, proxy, MagicMock())
    runner = PerceptionRunner(collector, pipeline, MagicMock())
    runner._is_running = True
    order = []
    entered = asyncio.Event()
    release = asyncio.Event()
    batch = MagicMock(empty=False, devices={}, start_timestamp=0, end_timestamp=0)
    collector.collect_batch.return_value = batch

    async def infer(*_args, **_kwargs):
        entered.set()
        await release.wait()
        order.append("inference")

    if entry == "realtime":
        proxy.realtime_perceive = AsyncMock(
            return_value=(RealtimePerceptionResult(skipped=True), set(), {}, set(), None)
        )
        # Use the full processor path and hold its post-inference work.
        proxy.handle_realtime_perception_result = AsyncMock(side_effect=infer)
        pipeline._perf_enabled = False
        pipeline._rule_shadow_enabled = False
        pipeline._flow_store = None
        operation = pipeline.process_realtime()
    else:
        proxy.on_demand_perceive = AsyncMock(side_effect=infer)
        operation = pipeline.process_on_demand(["camera"], "query")

    async def close():
        order.append("engine")

    async def disconnect():
        order.append("collector")

    proxy.close = AsyncMock(side_effect=close)
    runner._inference_worker.shutdown = Mock(side_effect=lambda **_: order.append("worker"))
    collector.shutdown = AsyncMock(side_effect=disconnect)
    request = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        with monkeypatch.context() as settings:
            settings.setattr("miloco.perception.processor._INFERENCE_DRAIN_TIMEOUT_S", 0)
            with pytest.raises(InferenceDrainDeferred, match="stop_deferred"):
                await runner.stop()

        assert runner.is_running
        assert pipeline.accepting_inference
        assert order == []
        proxy.close.assert_not_awaited()
        runner._inference_worker.shutdown.assert_not_called()
        collector.shutdown.assert_not_awaited()

        collection_count = collector.collect_batch.call_count
        async with pipeline._inference_admission.drain(0) as drained:
            assert drained is False
            assert await pipeline.process_realtime() is None
            assert await pipeline.process_on_demand(["camera"], "query") is None
        assert collector.collect_batch.call_count == collection_count
    finally:
        release.set()
        await asyncio.wait_for(runner.stop(), 2)

    assert not runner.is_running
    assert order == ["inference", "engine", "worker", "collector"]


async def test_deferred_stop_retires_old_realtime_output_and_allows_a_fresh_window(monkeypatch):
    collector = MagicMock()
    proxy = MagicMock()
    repo = MagicMock()
    pipeline = PipelineProcessor(collector, proxy, repo)
    runner = PerceptionRunner(collector, pipeline, repo)
    runner._is_running = True
    pipeline._perf_enabled = False
    pipeline._rule_shadow_enabled = False
    pipeline._flow_store = None
    collector.collect_batch.return_value = MagicMock(
        empty=False, devices={}, start_timestamp=0, end_timestamp=0, captured_at=0
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    result = RealtimePerceptionResult()

    async def infer(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return result, set(), {}, set(), None

    proxy.realtime_perceive = AsyncMock(side_effect=infer)
    proxy.handle_realtime_perception_result = AsyncMock()
    in_flight = asyncio.create_task(pipeline.process_realtime())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        monkeypatch.setattr("miloco.perception.processor._INFERENCE_DRAIN_TIMEOUT_S", 0)
        with pytest.raises(InferenceDrainDeferred):
            await runner.stop()
    finally:
        release.set()

    assert await asyncio.wait_for(in_flight, 1) is None
    proxy.handle_realtime_perception_result.assert_not_awaited()
    repo.append.assert_not_called()

    assert await pipeline.process_realtime() is result
    proxy.handle_realtime_perception_result.assert_awaited_once()
    repo.append.assert_called_once()


async def test_on_demand_answer_from_retired_generation_is_discarded():
    collector = MagicMock()
    collector.collect_batch.return_value = MagicMock(
        empty=False, start_timestamp=0, end_timestamp=0,
    )
    proxy = MagicMock()
    pipeline = PipelineProcessor(collector, proxy, MagicMock())
    entered = asyncio.Event()
    release = asyncio.Event()
    answer = MagicMock()

    async def infer(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return answer

    proxy.on_demand_perceive = AsyncMock(side_effect=infer)
    old_query = asyncio.create_task(pipeline.process_on_demand(["camera"], "query"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        pipeline.retire_results()
    finally:
        release.set()

    assert await asyncio.wait_for(old_query, 1) is None
    fresh_query = await pipeline.process_on_demand(["camera"], "query")
    assert fresh_query is not None
    assert fresh_query[0] is answer


# ─── runner.stop 清理 in-flight omni probe task (review #4 回归防护) ─────────


@pytest.mark.asyncio
async def test_runner_stop_cancels_inflight_omni_probe_tasks():
    """review #4 回归:runner.stop 必须 cancel _OMNI_PROBE_TASKS 里未完成的 probe
    task。若不清,event loop 销毁前 probe 未跑到 finally 的 clear_probe_in_flight,
    同进程再启 runner 时 _probe_in_flight 残留,try_arm_probe 永远返 False,自愈
    通道永久卡死。"""
    # 起真实 runner (bypass __init__),只 mock 必要属性
    runner = PerceptionRunner.__new__(PerceptionRunner)
    runner._collector = MagicMock()
    runner._collector.shutdown = AsyncMock()
    runner._pipeline = MagicMock()
    runner._pipeline.close = AsyncMock()
    runner._pipeline.drain_inference.return_value = AsyncMock()
    runner._log_repo = MagicMock()
    runner._is_running = True
    runner._perception_task = None
    runner._sync_devices_task = None
    runner._camera_scheduler = None
    runner._inference_worker = MagicMock()

    # 起一个真的 fire-and-forget task,注册进 _OMNI_PROBE_TASKS (与 processor 同款)
    cancelled_marker: list[str] = []

    async def _long_probe():
        try:
            await asyncio.sleep(30)  # 模拟 15s HTTP timeout 场景
        except asyncio.CancelledError:
            cancelled_marker.append("cancelled")
            raise

    probe_task = asyncio.create_task(_long_probe())
    _OMNI_PROBE_TASKS.add(probe_task)
    probe_task.add_done_callback(_OMNI_PROBE_TASKS.discard)

    # yield 一次让 probe_task 真正进入 asyncio.sleep(30) 的 await 点,否则从未 running
    # 的 task 被 cancel 时协程体不会执行到 except CancelledError 分支。
    await asyncio.sleep(0)

    # runner.stop 应 cancel probe_task 并 await 它退出
    await runner.stop()

    assert probe_task.done()
    assert cancelled_marker == ["cancelled"]
    # discard 回调:probe_task 应从 _OMNI_PROBE_TASKS 里被移除
    assert probe_task not in _OMNI_PROBE_TASKS
