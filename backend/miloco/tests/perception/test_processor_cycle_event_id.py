# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""处理器这一跳把本 cycle 的事件 id 从 realtime_perceive 交给落库侧。

感知周期的取数与落库被处理器分在两次调用里:realtime_perceive 返回 5 元组,
第 5 个是本 cycle 事件行的 id,再作为 cycle_event_id 交给
handle_realtime_perception_result。这一跳是生产路径上唯一把两端接起来的地方,
它断了以后两端各自的单测都还是绿的——id 照旧被 mint、照旧被消费,只是中途丢了。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
from miloco.perception.processor import PipelineProcessor
from miloco.perception.schema import (
    DecodedVideoFrame,
    DeviceData,
    PerceptionBatch,
)
from miloco.perception.types import PerceptionDevice, RealtimePerceptionResult


def _batch_with_one_device() -> PerceptionBatch:
    """一个非空 batch:empty 为假才会走到推理与落库那一段。"""
    batch = PerceptionBatch()
    dd = DeviceData(meta=PerceptionDevice(
        did="cam_A", name="cam_A", device_type="camera", room_name="客厅",
    ))
    dd.window_start_unix_ms = 100_000
    dd.window_end_unix_ms = 103_000
    dd.video.append(DecodedVideoFrame(
        frame=np.zeros((4, 4, 3), dtype=np.uint8),
        stream_ts=0, recv_unix_ms=99_500,
    ))
    batch.devices["cam_A"] = dd
    return batch


async def test_processor_forwards_cycle_event_id_to_persist_side():
    """realtime_perceive 交回的那个 id,必须原样进 handle_realtime_perception_result。

    变异:把这里的 cycle_event_id 改成 None(或另 mint 一个),本用例变红。
    """
    result = RealtimePerceptionResult(skipped=False)

    proc = PipelineProcessor(
        collector=MagicMock(),
        perception_engine_proxy=MagicMock(),
        log_repo=MagicMock(),
    )
    proc._collector.collect_batch = MagicMock(return_value=_batch_with_one_device())
    proc._perf_enabled = False
    proc._perception_engine_proxy.realtime_perceive = AsyncMock(
        return_value=(result, {}, {}, set(), "ev-proc-1")
    )
    proc._perception_engine_proxy.handle_realtime_perception_result = AsyncMock()

    await proc._process_realtime_inner()

    kwargs = proc._perception_engine_proxy.handle_realtime_perception_result.await_args.kwargs
    assert kwargs["cycle_event_id"] == "ev-proc-1"


async def test_processor_empty_batch_never_calls_persist_side():
    """空 batch 直接返回,两次调用都不发生——对照组,证明上面那条断言确实走了落库那一跳。"""
    proc = PipelineProcessor(
        collector=MagicMock(),
        perception_engine_proxy=MagicMock(),
        log_repo=MagicMock(),
    )
    proc._collector.collect_batch = MagicMock(return_value=PerceptionBatch())
    proc._perf_enabled = False
    proc._perception_engine_proxy.realtime_perceive = AsyncMock()
    proc._perception_engine_proxy.handle_realtime_perception_result = AsyncMock()

    assert await proc._process_realtime_inner() is None
    proc._perception_engine_proxy.realtime_perceive.assert_not_awaited()
    proc._perception_engine_proxy.handle_realtime_perception_result.assert_not_awaited()
