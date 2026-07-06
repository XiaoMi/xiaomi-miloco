from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from miloco.observability import agent_meta_poller as poller_mod
from miloco.observability.agent_meta_poller import AgentMetaPoller
from miloco.observability.aggregate import aggregate_cycle
from miloco.observability.metrics_client import MetricsClient
from miloco.observability.metrics_db import connect, init_schema
from miloco.observability.types import (
    DecodeTrace,
    DeviceTraceRecord,
    GateTrace,
)


def _init_db(db_path):
    conn = connect(db_path)
    init_schema(conn)
    return conn


# 【hermes-pr.md §五 #11 完成】test 上下文管理器:同时 patch get_adapter 返 None
# (强制走 webhook fallback 路径) + 允许传入额外的 patch 对象。
# 否则 _poll_once 优先调 adapter.read_trace_meta → 返 None(文件不存在) →
# 无限 in_progress 重试,测试卡死。
@contextmanager
def _no_adapter(**extra_patches):
    """Patch get_adapter 返 None + 应用其他 patches。

    用法:
        with _no_adapter():
            poller.enqueue(...)
        with _no_adapter(call=patch(...)):
            poller.enqueue(...)
    """
    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(patch("miloco.observability.agent_meta_poller.get_adapter", return_value=None))
        for ctx in extra_patches.values():
            stack.enter_context(ctx)
        yield


def _publish_seed_trace(client, trace_id="t-1"):
    """先 publish 一行 cycle,让 traces_v.has_agent_turn 派生有依赖目标。"""
    meta = dict(
        trace_id=trace_id, timestamp=100,
        in_delay_ms=0, out_delay_ms=0,
        decode_ms=0, collect_ms=0, convert_ms=0, log_ms=0,
        cycle_total_ms=10, pipeline_total_ms=5,
        window_duration_ms=3000,
        window_first_frame_recv_ms=None, stream_lag_ms=None,
    )
    devices = [DeviceTraceRecord(
        device_trace_id="dt-1", cycle_id=trace_id, timestamp=100,
        device_id="d", room_name="r",
        decode=DecodeTrace(1, 1, 1, 1),
        gate=GateTrace(0, 0, 0, False, False, True),
    )]
    client.publish_trace(aggregate_cycle(devices, meta), devices)


async def test_poller_done_writes_agent_run(tmp_path):
    db = tmp_path / "obs.db"
    _init_db(db).close()
    client = MetricsClient(db_path=db)
    await client.start()
    poller = AgentMetaPoller(metrics_client=client)
    await poller.start()
    try:
        _publish_seed_trace(client, "t-done")
        fake = {
            "status": "done",
            "runId": "r-1", "query": "q",
            "durationMs": 555.0, "success": True,
            "llmCallCount": 1, "toolCallCount": 0,
            "llmTotalMs": 400.0, "toolTotalMs": 0.0,
            "toolMaxMs": 0.0, "slowestToolName": None,
            "errorCount": 0, "errorMsg": None, "jsonlPath": None,
        }
        # 【hermes-pr.md §五 #11 完成】test 必须 mock get_adapter 返 None
        # 否则 _poll_once 优先调 adapter.read_trace_meta → 返 None → 无限 in_progress 重试。
        # 让 poller 走 fallback call_agent_webhook(webhook_rtt_ms 也是 fallback 路径写)。
        with _no_adapter(call=patch(
            "miloco.observability.agent_meta_poller.call_agent_webhook",
            new=AsyncMock(return_value=fake),
        )):
            poller.enqueue("t-done", "r-1", "interaction", webhook_rtt_ms=12.0)
            await poller._queue.join()
            await client.flush()

        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT run_id, trace_id, source, llm_total_ms, success, webhook_rtt_ms "
                "FROM agent_runs WHERE run_id=?", ("r-1",)
            ).fetchone()
            ha = conn.execute(
                "SELECT has_agent_turn FROM traces_v WHERE trace_id=?", ("t-done",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert row == ("r-1", "t-done", "interaction", 400.0, 1, 12.0)
        assert ha == 1
    finally:
        await poller.stop()
        await client.stop()


async def test_poller_in_progress_then_done(tmp_path, monkeypatch):
    """先返回 in_progress 几次再返回 done,验证 backoff retry。"""
    monkeypatch.setattr(poller_mod, "_POLL_INTERVAL_S", 0.01)

    db = tmp_path / "obs.db"
    _init_db(db).close()
    client = MetricsClient(db_path=db)
    await client.start()
    poller = AgentMetaPoller(metrics_client=client)
    await poller.start()
    try:
        _publish_seed_trace(client, "t-retry")
        calls = {"n": 0}

        async def fake_call(action, payload=None, *, timeout=5.0):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"status": "in_progress"}
            return {
                "status": "done",
                "runId": "r-retry", "query": "q",
                "durationMs": 100.0, "success": True,
                "llmCallCount": 1, "toolCallCount": 0,
                "llmTotalMs": 90.0, "toolTotalMs": 0.0,
                "toolMaxMs": 0.0, "slowestToolName": None,
                "errorCount": 0, "errorMsg": None, "jsonlPath": None,
            }

        with _no_adapter(call=patch(
            "miloco.observability.agent_meta_poller.call_agent_webhook",
            new=fake_call,
        )):
            poller.enqueue("t-retry", "r-retry", "rule", webhook_rtt_ms=None)
            await poller._queue.join()
            await client.flush()

        assert calls["n"] >= 3
        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT run_id, source FROM agent_runs WHERE run_id=?",
                ("r-retry",),
            ).fetchone()
        finally:
            conn.close()
        assert row == ("r-retry", "rule")
    finally:
        await poller.stop()
        await client.stop()


async def test_poller_unknown_gives_up(tmp_path):
    db = tmp_path / "obs.db"
    _init_db(db).close()
    client = MetricsClient(db_path=db)
    await client.start()
    poller = AgentMetaPoller(metrics_client=client)
    await poller.start()
    try:
        _publish_seed_trace(client, "t-unknown")
        with _no_adapter(unknown=patch(
            "miloco.observability.agent_meta_poller.call_agent_webhook",
            new=AsyncMock(return_value={"status": "unknown"}),
        )):
            poller.enqueue("t-unknown", "r-nope", "suggestion", webhook_rtt_ms=8.0)
            await poller._queue.join()
            await client.flush()

        conn = connect(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE trace_id=?",
                ("t-unknown",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0
    finally:
        await poller.stop()
        await client.stop()
