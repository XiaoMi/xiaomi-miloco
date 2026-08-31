"""窗口新鲜度门控（stale_window_sec）测试。

回归 2026-08-31 172.16.3.23 故障：网络中断 ~61 分钟后摄像头缓冲的旧窗口被
批量补处理，14:25 网络恢复瞬间用 13:24 的画面触发了扫地场景（in_delay ≈ 61
分钟，模型调用本身只要 ~2s）。门控后：窗口采集时间距当前超过 stale_window_sec
的窗口直接丢弃——不调 omni、不触发规则，只留 perf trace。
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from miloco.perception.processor import PipelineProcessor
from miloco.perception.schema import DecodedVideoFrame, DeviceData, PerceptionBatch


def _frame(ts_ms: int) -> DecodedVideoFrame:
    return DecodedVideoFrame(
        frame=b"\x00\x00\x00\x00",  # 假帧：门控在 omni 之前，不需要真实可解码
        stream_ts=0,
        wall_ms=ts_ms,
        unix_ms=ts_ms,
        recv_unix_ms=ts_ms,
    )


def _batch(end_unix_ms: int) -> PerceptionBatch:
    return PerceptionBatch(
        devices={
            "cam-1": DeviceData(
                meta=SimpleNamespace(did="cam-1", name="cam", room_name="Bedroom"),
                video=[_frame(end_unix_ms - 4000), _frame(end_unix_ms - 2000), _frame(end_unix_ms)],
                window_start_unix_ms=end_unix_ms - 5000,
                window_end_unix_ms=end_unix_ms,
            )
        }
    )


def _fake_settings(stale_window_sec: float) -> SimpleNamespace:
    """processor 只读 settings.perf.enabled + settings.perception.stale_window_sec。"""
    return SimpleNamespace(
        perf=SimpleNamespace(enabled=False),
        perception=SimpleNamespace(stale_window_sec=stale_window_sec),
    )


def _make_processor(batch: PerceptionBatch, monkeypatch, stale_window_sec: float = 30.0):
    collector = MagicMock()
    collector.collect_batch = MagicMock(return_value=batch)  # 同步方法（无 await）
    proxy = AsyncMock()
    proxy.realtime_perceive = AsyncMock(return_value=(None, [], [], []))
    proxy.handle_realtime_perception_result = AsyncMock()
    log_repo = MagicMock()
    log_repo.append = MagicMock()

    monkeypatch.setattr(
        "miloco.perception.processor.get_settings",
        lambda: _fake_settings(stale_window_sec),
    )
    proc = PipelineProcessor(collector, proxy, log_repo)
    return proc, proxy


@pytest.mark.asyncio
async def test_stale_window_dropped_without_omni(monkeypatch):
    """61 分钟前的积压窗口 → 直接丢弃：不调 omni、不触发规则、返回 False。"""
    end_ts = int(time.time() * 1000) - 61 * 60 * 1000
    proc, proxy = _make_processor(_batch(end_ts), monkeypatch)

    result = await proc.process_realtime()

    assert result is False
    proxy.realtime_perceive.assert_not_called()
    proxy.handle_realtime_perception_result.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_window_passes_gate(monkeypatch):
    """1 秒前的新鲜窗口 → 正常进 omni（门控不误伤）。"""
    end_ts = int(time.time() * 1000) - 1000
    proc, proxy = _make_processor(_batch(end_ts), monkeypatch)

    result = await proc.process_realtime()

    # 走正常路径：realtime_perceive 被调用（mock 返回 None → 消费但跳过）
    proxy.realtime_perceive.assert_awaited_once()
    assert result is False  # mock 返回 (None, ...) 的语义：consumed but skipped


@pytest.mark.asyncio
async def test_gate_disabled_when_zero(monkeypatch):
    """stale_window_sec=0 关闭门控：61 分钟前的窗口也照常处理。"""
    end_ts = int(time.time() * 1000) - 61 * 60 * 1000
    proc, proxy = _make_processor(_batch(end_ts), monkeypatch, stale_window_sec=0.0)

    result = await proc.process_realtime()

    proxy.realtime_perceive.assert_awaited_once()
    assert result is False


@pytest.mark.asyncio
async def test_stale_drop_publishes_trace(monkeypatch):
    """丢弃路径在 perf 开启时发布 stale trace（perf 页面可见 in_delay）。"""
    end_ts = int(time.time() * 1000) - 61 * 60 * 1000
    proc, proxy = _make_processor(_batch(end_ts), monkeypatch)
    proc._perf_enabled = True
    proc._publish_stale_trace = MagicMock()

    result = await proc.process_realtime()

    assert result is False
    proc._publish_stale_trace.assert_called_once()
    args = proc._publish_stale_trace.call_args.kwargs
    assert args["in_delay_s"] > 3000  # ~61 分钟
