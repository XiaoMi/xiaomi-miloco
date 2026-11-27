# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""验证感知设置应用前后的生命周期顺序。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.perception.omni_probe_registry import _OMNI_PROBE_TASKS
from miloco.perception.runner import PerceptionRunner
from miloco.perception.service import PerceptionService


def _make_runtime(*, is_running):
    pipeline = MagicMock()
    pipeline.apply_omni_fps = AsyncMock()
    runner = PerceptionRunner.__new__(PerceptionRunner)
    runner._pipeline = pipeline
    runner._is_running = is_running
    runner._schedule_mode = "batch"
    runner._camera_scheduler = None
    runner._finish_start = AsyncMock()
    runner._finish_stop = AsyncMock()
    service = PerceptionService(MagicMock(), pipeline, runner, MagicMock(), MagicMock())
    return service, runner


@pytest.mark.parametrize("is_running", [False, True])
async def test_window_change_restarts_only_running_engine(is_running):
    service, runner = _make_runtime(is_running=is_running)
    events = []
    runner._finish_stop.side_effect = lambda: events.append("stop")
    runner._finish_start.side_effect = lambda: events.append("start")

    await service.apply_schedule_config(
        "batch", lambda: events.append("save"), restart_window=True, omni_fps=None,
    )

    assert events == (["save", "stop", "start"] if is_running else ["save"])
    runner._pipeline.apply_omni_fps.assert_not_awaited()


async def test_failed_window_restart_leaves_failure_visible_to_caller():
    service, runner = _make_runtime(is_running=True)
    runner._finish_stop.side_effect = RuntimeError("stop failed")
    save = MagicMock()

    with pytest.raises(RuntimeError, match="stop failed"):
        await service.apply_schedule_config("batch", save, restart_window=True, omni_fps=None)

    save.assert_called_once()
    runner._finish_start.assert_not_awaited()


async def test_lifecycle_lock_serializes_restart_and_stop():
    service, runner = _make_runtime(is_running=True)
    in_start = asyncio.Event()

    async def check_start():
        in_start.set()
        await asyncio.sleep(0)
        assert service._lifecycle_lock.locked()
        runner._finish_stop.assert_awaited_once()

    runner._finish_start.side_effect = check_start

    async def stop_after_start_begins():
        await in_start.wait()
        await service.stop_engine()

    async with asyncio.timeout(1):
        await asyncio.gather(
            service.apply_schedule_config("batch", lambda: None, restart_window=True, omni_fps=None),
            stop_after_start_begins(),
        )

    assert runner._finish_stop.await_count == 2


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
    runner._camera_scheduler = None
    runner._log_repo = MagicMock()
    runner._is_running = True
    runner._perception_task = None
    runner._sync_devices_task = None
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
