# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""软停(stop_to_unconfigured)实体逻辑 + 与在飞 perceive 的互斥(use-after-close 回归)。

删当前生效模型 → 关引擎实例 + 状态降回 no_omni_api_key,保留 tick 自愈循环。此前该链路
零真实覆盖(admin 层 delete-active 测试只过是因 manager 未初始化、软停 AttributeError 被
best-effort 吞掉)。本文件直接驱动 PerceptionEngineProxy.stop_to_unconfigured:

- 关掉在跑引擎 → perception_engine=None(ready=False),按空 key 重判落 no_omni_api_key
- 引擎未起时重入安全(不抛)
- 并发回归:perceive 持 _engine_lock 在飞时,stop_to_unconfigured 必须等其结束才 teardown
  —— 锁住 teardown 与在飞推理的次序,杜绝引擎被拔导致的 use-after-close
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from miloco.perception.client import PerceptionEngineProxy
from miloco.perception.engine.resource_validator import (
    EngineReadiness,
    ValidationResult,
)


def _make_proxy(engine=None) -> PerceptionEngineProxy:
    """绕过 __init__(会跑真 _init_engine)构造 proxy,只挂测试需要的字段 + _engine_lock。"""
    p = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
    p.perception_engine = engine
    p._status = "ready" if engine is not None else "no_omni_api_key"
    p._status_message = ""
    p._last_captions = {}
    p._inference_worker = None
    p._engine_lock = asyncio.Lock()
    return p


@contextmanager
def _patched_unconfigured():
    """让 _init_engine 走 validate→NOT_CONFIGURED(空 key),落 no_omni_api_key、不建真引擎。"""
    settings = MagicMock()
    settings.perception.engine = {}
    with patch(
        "miloco.perception.client.get_settings", return_value=settings
    ), patch(
        "miloco.perception.client.get_monitor", return_value=MagicMock()
    ), patch(
        "miloco.perception.client.resolve_omni_api_key", side_effect=lambda k="": k or ""
    ), patch(
        "miloco.perception.engine.resource_validator.validate_resources",
        return_value=ValidationResult(status=EngineReadiness.NOT_CONFIGURED, message="未配置"),
    ):
        yield


async def test_stop_to_unconfigured_closes_and_redegrades():
    """软停:关掉在跑引擎实例 → perception_engine=None(ready=False),按空 key 重判落 no_omni_api_key。"""
    engine = MagicMock()
    engine.close = AsyncMock()
    proxy = _make_proxy(engine=engine)

    with _patched_unconfigured():
        await proxy.stop_to_unconfigured()

    engine.close.assert_awaited_once()
    assert proxy.perception_engine is None
    assert proxy.ready is False
    assert proxy.status == "no_omni_api_key"


async def test_stop_to_unconfigured_reentrant_when_engine_none():
    """重入安全:引擎未起(perception_engine is None)时跳过 close,仅按当前配置重判,不抛。"""
    proxy = _make_proxy(engine=None)

    with _patched_unconfigured():
        await proxy.stop_to_unconfigured()

    assert proxy.perception_engine is None
    assert proxy.status == "no_omni_api_key"


async def test_soft_stop_waits_for_inflight_perceive():
    """并发回归:perceive 持 _engine_lock 在飞时,stop_to_unconfigured 阻塞在 acquire,
    必须等 perceive 结束才 teardown —— 引擎不会在推理途中被拔(use-after-close)。"""
    engine = MagicMock()
    engine.close = AsyncMock()
    proxy = _make_proxy(engine=engine)

    order: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def inflight_perceive():
        # 复刻 realtime_perceive 的持锁方式:持 _engine_lock 直到推理完成。
        async with proxy._engine_lock:
            order.append("perceive_start")
            entered.set()
            await release.wait()
            order.append("perceive_end")

    async def soft_stop():
        order.append("stop_call")
        with _patched_unconfigured():
            await proxy.stop_to_unconfigured()
        order.append("stop_done")

    t1 = asyncio.create_task(inflight_perceive())
    await entered.wait()  # 确保 perceive 已持锁
    t2 = asyncio.create_task(soft_stop())
    await asyncio.sleep(0.02)  # 给 soft_stop 机会尝试(并卡在)acquire

    # perceive 仍持锁未放:soft_stop 应被挡住,引擎尚未被关
    assert "perceive_start" in order
    assert "stop_done" not in order
    assert proxy.perception_engine is not None
    engine.close.assert_not_awaited()

    release.set()
    await asyncio.gather(t1, t2)

    # 次序:perceive 先结束,stop 才 teardown
    assert order.index("perceive_end") < order.index("stop_done")
    assert proxy.perception_engine is None
    engine.close.assert_awaited_once()
