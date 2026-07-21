from unittest.mock import AsyncMock, MagicMock, patch

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


def _mock_adapter(read_trace_meta=None):
    """返回一个 mock adapter，read_trace_meta 返回指定值或 callable。"""
    adapter = MagicMock()
    adapter.name = "mock"
    if callable(read_trace_meta):
        adapter.read_trace_meta = AsyncMock(side_effect=read_trace_meta)
    elif read_trace_meta is not None:
        adapter.read_trace_meta = AsyncMock(return_value=read_trace_meta)
    else:
        adapter.read_trace_meta = AsyncMock(return_value=None)
    return adapter


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
        from miloco.agent_platform.base import TraceMeta
        fake_meta = TraceMeta(
            run_id="r-1", query="q", duration_ms=555.0,
            llm_call_count=1, tool_call_count=0,
            llm_total_ms=400.0, tool_total_ms=0.0,
            tool_max_ms=0.0, slowest_tool_name=None,
            success=True, error_count=0, error_msg=None,
            jsonl_path=None,
        )
        with patch(
            "miloco.observability.agent_meta_poller.get_adapter",
            return_value=_mock_adapter(fake_meta),
        ):
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
    """先返回 None 几次再返回 meta,验证 backoff retry。"""
    monkeypatch.setattr(poller_mod, "_POLL_INTERVAL_S", 0.01)

    db = tmp_path / "obs.db"
    _init_db(db).close()
    client = MetricsClient(db_path=db)
    await client.start()
    poller = AgentMetaPoller(metrics_client=client)
    await poller.start()
    try:
        _publish_seed_trace(client, "t-retry")
        from miloco.agent_platform.base import TraceMeta
        calls = {"n": 0}

        async def fake_read_trace_meta(run_id):
            calls["n"] += 1
            if calls["n"] < 3:
                return None
            return TraceMeta(
                run_id=run_id, query="q", duration_ms=100.0,
                llm_call_count=1, tool_call_count=0,
                llm_total_ms=90.0, tool_total_ms=0.0,
                tool_max_ms=0.0, slowest_tool_name=None,
                success=True, error_count=0, error_msg=None,
                jsonl_path=None,
            )

        with patch(
            "miloco.observability.agent_meta_poller.get_adapter",
            return_value=_mock_adapter(fake_read_trace_meta),
        ):
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


async def test_poller_never_done_gives_up(tmp_path, monkeypatch):
    """read_trace_meta 持续返回 None → 超时后不写 agent_runs。"""
    monkeypatch.setattr(poller_mod, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(poller_mod, "_MAX_DEADLINE_S", 0.05)
    db = tmp_path / "obs.db"
    _init_db(db).close()
    client = MetricsClient(db_path=db)
    await client.start()
    poller = AgentMetaPoller(metrics_client=client)
    await poller.start()
    try:
        _publish_seed_trace(client, "t-nope")
        with patch(
            "miloco.observability.agent_meta_poller.get_adapter",
            return_value=_mock_adapter(None),  # always returns None
        ):
            poller.enqueue("t-nope", "r-nope", "suggestion", webhook_rtt_ms=8.0)
            await poller._queue.join()
            await client.flush()

        conn = connect(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE trace_id=?",
                ("t-nope",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0
    finally:
        await poller.stop()
        await client.stop()
