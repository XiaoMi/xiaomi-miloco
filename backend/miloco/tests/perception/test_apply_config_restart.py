# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for PerceptionService.apply_config_restart.

「应用设置」改感知参数后需真正重建引擎(omni_fps)+ runner 重读(window_size)才生效。
本方法:停 runner → 重建引擎 → 启 runner,全程持 lifecycle 锁串行化。覆盖:

- was_running=True:走 stop → rebuild → start,返 True
- was_running=False:只 rebuild,不 stop/start(未运行时不误拉起 runner)
- 重建抛异常(如磁盘满/模型加载失败)→ 返 False 不冒泡(config 已写盘,由调用方
  据 restart_ok 区分「已保存但重启失败」,否则前端误报「保存失败」)
- lifecycle 锁串行化 apply_config_restart 与并发 start/stop,防交错状态错乱
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
    svc._pipeline.rebuild = AsyncMock()
    svc._engine = MagicMock()
    svc._engine.is_running = is_running
    svc._engine.start = AsyncMock()
    svc._engine.stop = AsyncMock()
    svc._log_repo = MagicMock()
    svc._lifecycle_lock = asyncio.Lock()
    return svc


@pytest.mark.asyncio
async def test_apply_config_restart_running_does_stop_rebuild_start():
    """引擎在跑:stop → rebuild → start,返 True。"""
    svc = _make_service(is_running=True)

    assert await svc.apply_config_restart() is True

    svc._engine.stop.assert_awaited_once()
    svc._pipeline.rebuild.assert_awaited_once()
    svc._engine.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_config_restart_not_running_only_rebuilds():
    """引擎未运行:只 rebuild(重读 omni_fps),不 stop/start——不误拉起一个
    没人配置意图的 runner。window_size 靠下次用户 start 时 runner 重读。"""
    svc = _make_service(is_running=False)

    assert await svc.apply_config_restart() is True

    svc._pipeline.rebuild.assert_awaited_once()
    svc._engine.stop.assert_not_awaited()
    svc._engine.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_config_restart_rebuild_failure_returns_false():
    """重建抛异常(磁盘满/模型加载失败)→ 返 False,不冒泡成 500。

    config 已由调用方写盘(不可回滚),返 False 让 PUT 端点带 restart_ok=False,
    前端提示「已保存但需手动重启」而非「保存失败」。
    """
    svc = _make_service(is_running=True)
    svc._pipeline.rebuild = AsyncMock(side_effect=RuntimeError("disk full"))

    assert await svc.apply_config_restart() is False

    # stop 已执行(在 rebuild 之前),但 start 因 rebuild 抛错未到达
    svc._engine.stop.assert_awaited_once()
    svc._engine.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_config_restart_start_failure_returns_false():
    """start 阶段抛异常也返 False(不冒泡)。"""
    svc = _make_service(is_running=True)
    svc._engine.start = AsyncMock(side_effect=RuntimeError("sync devices failed"))

    assert await svc.apply_config_restart() is False


@pytest.mark.asyncio
async def test_lifecycle_lock_serializes_restart_and_stop():
    """lifecycle 锁串行化:apply_config_restart 持锁期间,并发 stop_engine 必须等待,
    不会在 restart 的 stop→rebuild→start 之间穿插执行(防 _is_running 交错错乱)。

    构造:restart 进入 rebuild(持锁、含让出点)后并发发起 stop_engine;在 rebuild
    中途直接断言 _lifecycle_lock.locked()——不依赖 AsyncMock 是否让出事件循环的
    调度细节,即便并发 stop 已被调度,它也应因抢不到锁而阻塞,锁仍处 locked。
    """
    # is_running=False:restart 只走 rebuild,不调 _engine.stop,这样 _engine.stop
    # 只会被并发的 stop_engine() 调用,可干净区分「谁进了临界区」。
    svc = _make_service(is_running=False)
    in_rebuild = asyncio.Event()
    stop_engine_ran: list[str] = []
    lock_states: list[bool] = []

    async def _rebuild():
        in_rebuild.set()
        # 给并发 stop_engine() 充分的调度机会去尝试抢锁
        for _ in range(3):
            await asyncio.sleep(0)
        # 若锁生效:并发 stop_engine() 此刻仍卡在 async with 外 → 锁被 restart 持有
        lock_states.append(svc._lifecycle_lock.locked())
        # 且 stop_engine() 的临界区(_engine.stop)还没跑到
        lock_states.append(len(stop_engine_ran) == 0)

    svc._pipeline.rebuild = AsyncMock(side_effect=_rebuild)
    svc._engine.stop = AsyncMock(side_effect=lambda: stop_engine_ran.append("stopped"))

    async def _wait_and_stop():
        await in_rebuild.wait()  # restart 已进 rebuild(持锁中)
        await svc.stop_engine()  # 真实走 service 锁,应阻塞到 restart 释放

    await asyncio.gather(svc.apply_config_restart(), _wait_and_stop())

    # rebuild 中途:锁被 restart 持有 且 stop_engine 临界区尚未进入 → 二者原子互斥
    assert lock_states == [True, True]
    # stop_engine() 最终仍执行(锁释放后)
    assert stop_engine_ran == ["stopped"]
