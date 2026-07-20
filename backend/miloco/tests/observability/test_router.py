import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.observability.aggregate import aggregate_cycle
from miloco.observability.metrics_client import MetricsClient
from miloco.observability.metrics_db import connect, init_schema
from miloco.observability.router import router
from miloco.observability.types import (
    DecodeTrace,
    DeviceTraceRecord,
    GateTrace,
)


@pytest.fixture
def app_with_db(tmp_path):
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    conn.close()
    client = MetricsClient(db_path=db)
    app = FastAPI()
    app.include_router(router)
    app.state.metrics_client = client
    app.state.obs_db_path = db
    return app, db, client


async def test_get_trace_returns_cycle_and_devices(app_with_db):
    app, _db, client = app_with_db
    await client.start()
    try:
        meta = dict(
            trace_id="c-2", timestamp=200,
            in_delay_ms=0, out_delay_ms=0,
            decode_ms=0, collect_ms=0, convert_ms=0, log_ms=0,
            cycle_total_ms=10, pipeline_total_ms=5,
            window_duration_ms=3000,
            window_first_frame_recv_ms=None, stream_lag_ms=None,
        )
        devices = [DeviceTraceRecord(
            device_trace_id="dt-2", cycle_id="c-2", timestamp=200,
            device_id="d2", room_name="r2",
            decode=DecodeTrace(1.0, 1.0, 10, 10),
            gate=GateTrace(1.0, 0.5, 0.5, True, False, False),
        )]
        client.publish_trace(aggregate_cycle(devices, meta), devices)
        await client.flush()

        with TestClient(app) as tc:
            r = tc.get("/api/trace/c-2")
        assert r.status_code == 200
        data = r.json()
        assert data["cycle"]["trace_id"] == "c-2"
        assert len(data["devices"]) == 1
        assert data["devices"][0]["device_id"] == "d2"
    finally:
        await client.stop()


async def test_list_traces_filters(app_with_db):
    app, _db, client = app_with_db
    await client.start()
    try:
        for i, tid in enumerate(["x-1", "x-2", "x-3"]):
            meta = dict(
                trace_id=tid, timestamp=1000 + i,
                in_delay_ms=0, out_delay_ms=0,
                decode_ms=0, collect_ms=0, convert_ms=0, log_ms=0,
                cycle_total_ms=10, pipeline_total_ms=5,
                window_duration_ms=3000,
                window_first_frame_recv_ms=None, stream_lag_ms=None,
            )
            devs = [DeviceTraceRecord(
                device_trace_id=f"dt-{i}", cycle_id=tid, timestamp=1000 + i,
                device_id="d", room_name="r",
                decode=DecodeTrace(1, 1, 1, 1),
                gate=GateTrace(0, 0, 0, False, False, True),
            )]
            client.publish_trace(aggregate_cycle(devs, meta), devs)
        await client.flush()

        with TestClient(app) as tc:
            r = tc.get("/api/traces?limit=2")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 2
        assert rows[0]["trace_id"] == "x-3"
    finally:
        await client.stop()


async def test_list_actions_filters_and_orders(app_with_db):
    from miloco.config import get_settings
    from miloco.observability.types import ActionLedgerRecord

    # router 挂 verify_token 依赖:测试环境 settings 有 token,需带 Bearer 头。
    token = get_settings().server.token
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    app, _db, client = app_with_db
    await client.start()
    try:
        client.record_action(ActionLedgerRecord(
            id="a1", timestamp=1000, action_type="set_property", did="d1",
            device_name="台灯", room="客厅", iid="prop.2.1", value_json="true",
            result_code=None, result_msg=None, success=True, error=None,
        ))
        client.record_action(ActionLedgerRecord(
            id="a2", timestamp=2000, action_type="call_action", did="d2",
            device_name=None, room=None, iid="action.5.1", value_json="[]",
            result_code=-704042011, result_msg="设备离线", success=False, error=None,
        ))
        await client.flush()

        with TestClient(app) as tc:
            # 默认:新到旧
            r = tc.get("/api/actions", headers=headers)
            assert r.status_code == 200
            rows = r.json()
            assert [x["id"] for x in rows] == ["a2", "a1"]

            # failed_only
            r = tc.get("/api/actions?failed_only=1", headers=headers)
            rows = r.json()
            assert [x["id"] for x in rows] == ["a2"]

            # did 过滤
            r = tc.get("/api/actions?did=d1", headers=headers)
            rows = r.json()
            assert [x["id"] for x in rows] == ["a1"]

            # since_ms 过滤
            r = tc.get("/api/actions?since_ms=1500", headers=headers)
            rows = r.json()
            assert [x["id"] for x in rows] == ["a2"]

            # until_ms 过滤(上界)——选定时间段时不混入范围外历史动作
            r = tc.get("/api/actions?until_ms=1500", headers=headers)
            rows = r.json()
            assert [x["id"] for x in rows] == ["a1"]

            # since_ms + until_ms 组合窗口
            r = tc.get("/api/actions?since_ms=500&until_ms=1500", headers=headers)
            rows = r.json()
            assert [x["id"] for x in rows] == ["a1"]

            # action_type 过滤
            r = tc.get("/api/actions?action_type=call_action", headers=headers)
            rows = r.json()
            assert [x["id"] for x in rows] == ["a2"]
    finally:
        await client.stop()


async def test_list_actions_home_filter(app_with_db):
    """home_id 过滤(v4):等值命中 + NULL 放行(老行/未解析行不蒸发),他家行排除。"""
    from miloco.config import get_settings
    from miloco.observability.types import ActionLedgerRecord

    token = get_settings().server.token
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    app, _db, client = app_with_db
    await client.start()
    try:
        def rec(rid: str, ts: int, home: str | None) -> ActionLedgerRecord:
            return ActionLedgerRecord(
                id=rid, timestamp=ts, action_type="set_property", did="d1",
                device_name=None, room=None, iid="prop.2.1", value_json="true",
                result_code=None, result_msg=None, success=True, error=None,
                home_id=home,
            )

        client.record_action(rec("h1-row", 1000, "H1"))
        client.record_action(rec("h2-row", 2000, "H2"))
        client.record_action(rec("legacy", 3000, None))
        await client.flush()

        with TestClient(app) as tc:
            # 不带 home_id:全部返回(向后兼容)
            r = tc.get("/api/actions", headers=headers)
            assert r.status_code == 200
            assert [x["id"] for x in r.json()] == ["legacy", "h2-row", "h1-row"]

            # home_id=H1:H1 行 + NULL 老行保留,H2 行排除,仍新到旧
            r = tc.get("/api/actions?home_id=H1", headers=headers)
            assert [x["id"] for x in r.json()] == ["legacy", "h1-row"]
    finally:
        await client.stop()
