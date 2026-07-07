"""端到端:cycle publish → poller 拉 openclaw get_trace → INSERT agent_runs → 查询。"""
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.observability.agent_meta_poller import AgentMetaPoller
from miloco.observability.aggregate import aggregate_cycle
from miloco.observability.metrics_client import MetricsClient
from miloco.observability.metrics_db import connect, init_schema
from miloco.observability.router import router
from miloco.observability.types import (
    DecodeTrace,
    DeviceTraceRecord,
    GateTrace,
    IdentityTrace,
    OmniTrace,
)


async def test_cycle_publish_then_poller_fetches_meta_then_query(tmp_path):
    db = tmp_path / "obs.db"
    # 预初始化 schema,/api/trace/{id} 端点能直接打开
    conn = connect(db)
    init_schema(conn)
    conn.close()

    client = MetricsClient(db_path=db)
    await client.start()
    app = FastAPI()
    app.include_router(router)
    app.state.metrics_client = client
    app.state.obs_db_path = db

    poller = AgentMetaPoller(metrics_client=client)
    await poller.start()

    try:
        # 1. cycle publish_trace
        meta = dict(
            trace_id="e2e-1", timestamp=1000,
            in_delay_ms=0, out_delay_ms=0,
            decode_ms=0, collect_ms=0, convert_ms=0, log_ms=0,
            cycle_total_ms=200, pipeline_total_ms=180,
            window_duration_ms=3000,
            window_first_frame_recv_ms=None, stream_lag_ms=500.0,
        )
        devices = [DeviceTraceRecord(
            device_trace_id="dt-e", cycle_id="e2e-1", timestamp=1000,
            device_id="d", room_name="客厅",
            decode=DecodeTrace(1.0, 1.0, 30, 0),
            gate=GateTrace(1.0, 0.5, 0.5, True, False, False),
            identity=IdentityTrace(ms=2.0),
            omni=OmniTrace(ms=100.0),
        )]
        client.publish_trace(aggregate_cycle(devices, meta), devices)

        # 2. cycle 内业务事件
        client.publish_event("rule_match", "rule-001", {"reason": "x"}, trace_id="e2e-1")
        client.publish_event("interaction", "用户", {"content": "开灯"}, trace_id="e2e-1")
        await client.flush()

        # 3. mock adapter.read_trace_meta 返回 done meta,
        #    poller 拿到后调 record_agent_run 写入 agent_runs
        from miloco.agent_platform.base import TraceMeta
        fake_meta = TraceMeta(
            run_id="r-1", query="开客厅灯",
            duration_ms=1200.0,
            llm_call_count=1, tool_call_count=1,
            llm_total_ms=900.0, tool_total_ms=100.0,
            tool_max_ms=100.0, slowest_tool_name="miot_call",
            success=True, error_count=0, error_msg=None,
            jsonl_path=None,
        )
        from unittest.mock import Mock
        mock_adapter = Mock()
        mock_adapter.read_trace_meta = AsyncMock(return_value=fake_meta)
        with patch(
            "miloco.observability.agent_meta_poller.get_adapter",
            return_value=mock_adapter,
        ):
            poller.enqueue("e2e-1", "r-1", "interaction", webhook_rtt_ms=15.5)
            await poller._queue.join()
            await client.flush()

        # 4. GET /api/trace/{id} 拿回整 cycle + devices,traces_v 派生 has_agent_turn=1
        with TestClient(app) as tc:
            r = tc.get("/api/trace/e2e-1")
            assert r.status_code == 200
            data = r.json()
            assert data["cycle"]["trace_id"] == "e2e-1"
            assert data["cycle"]["has_agent_turn"] == 1
            assert len(data["devices"]) == 1

        # 5. GET /api/agent_runs?trace_id=e2e-1
        with TestClient(app) as tc:
            r = tc.get("/api/agent_runs?trace_id=e2e-1")
            assert r.status_code == 200
            runs = r.json()
            assert len(runs) == 1
            assert runs[0]["run_id"] == "r-1"
            assert runs[0]["source"] == "interaction"
            assert runs[0]["slowest_tool_name"] == "miot_call"
            assert runs[0]["webhook_rtt_ms"] == 15.5
            assert runs[0]["llm_total_ms"] == 900.0

        # 6. GET /api/events?trace_id=e2e-1
        with TestClient(app) as tc:
            r = tc.get("/api/events?trace_id=e2e-1")
            evts = r.json()
            assert len(evts) == 2
            types = sorted(e["event_type"] for e in evts)
            assert types == ["interaction", "rule_match"]
    finally:
        await poller.stop()
        await client.stop()
