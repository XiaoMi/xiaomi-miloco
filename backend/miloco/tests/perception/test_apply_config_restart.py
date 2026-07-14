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
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.perception.service import PerceptionService


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
    svc._engine.stop = AsyncMock(side_effect=RuntimeError("stop failed"))

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
