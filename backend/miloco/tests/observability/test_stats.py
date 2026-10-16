import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.observability.metrics_db import connect, init_schema
from miloco.observability.router import router


@pytest.fixture
def app_with_data(tmp_path):
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    for i in range(10):
        ts = now_ms - i * 60_000
        conn.execute(
            "INSERT INTO traces (trace_id, timestamp, cycle_total_ms, "
            "window_duration_ms, gate_video_pass, gate_audio_pass, omni_call_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"c-{i}", ts, 100 + i * 10, 3000,
             1 if i % 2 == 0 else 0, 1 if i % 3 == 0 else 0, 1),
        )
        # 前 5 个 cycle 各挂一行 agent_run;前 4 个成功,第 5 个失败
        if i < 5:
            conn.execute(
                "INSERT INTO agent_runs (run_id, trace_id, timestamp, source, "
                "query, webhook_rtt_ms, duration_ms, llm_call_count, tool_call_count, "
                "llm_total_ms, tool_total_ms, tool_max_ms, slowest_tool_name, success) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"r-{i}", f"c-{i}", ts, "interaction",
                 f"q-{i}", 10.0 + i, 1000.0, 1, 2,
                 600.0, 300.0, 250.0, "miot_call", 1 if i < 4 else 0),
            )
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db
    return app


def test_stats_latency_percentiles(app_with_data):
    with TestClient(app_with_data) as tc:
        r = tc.get("/api/stats?metric=latency_percentiles&bucket=1h")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    if data:
        assert "p50" in data[0] and "p95" in data[0]


def test_stats_rtf_series(app_with_data):
    with TestClient(app_with_data) as tc:
        r = tc.get("/api/stats?metric=rtf_series&bucket=1h")
    assert r.status_code == 200
    data = r.json()
    if data:
        assert "rtf" in data[0] and "rtf_e2e" in data[0]
        # rtf_omni 是 2026-05 补充字段 — 必须出现在返回里
        assert "rtf_omni" in data[0]
        # rtf_omni_ok 跟 rtf_e2e_ok 一对(仅 omni 成功 cycle 的均值)
        assert "rtf_e2e_ok" in data[0]
        assert "rtf_omni_ok" in data[0]


def test_stats_gate_pass_rate(app_with_data):
    with TestClient(app_with_data) as tc:
        r = tc.get("/api/stats?metric=gate_pass_rate&bucket=1h")
    assert r.status_code == 200


def test_stats_agent_latency_breakdown(app_with_data):
    with TestClient(app_with_data) as tc:
        r = tc.get("/api/stats?metric=agent_latency_breakdown&bucket=1h")
    assert r.status_code == 200
    data = r.json()
    if data:
        assert "llm" in data[0] and "tool" in data[0]


def test_stats_slowest_tool(app_with_data):
    with TestClient(app_with_data) as tc:
        r = tc.get("/api/stats?metric=slowest_tool_top_n")
    assert r.status_code == 200
    data = r.json()
    assert any(d["tool_name"] == "miot_call" for d in data)


def test_stats_agent_webhook_health(app_with_data):
    with TestClient(app_with_data) as tc:
        r = tc.get("/api/stats?metric=agent_webhook_health&bucket=1h")
    assert r.status_code == 200


@pytest.fixture
def app_with_full_data(tmp_path):
    """更完整的 fixture:含 skip、丢包、阶段耗时,供 summary / stage_percentiles 用。"""
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    # 20 条 cycle:前 10 条非 skip,有完整阶段耗时;后 10 条 skip,只有 decode/collect
    for i in range(20):
        skipped = 0 if i < 10 else 1
        identity_ms = 20.0 + i if not skipped else 0.0
        omni_ms = 800.0 + i * 50 if not skipped else 0.0
        # 前 5 条带丢包记录(dropped_windows_total=2),后面不丢
        dropped = 2 if i < 5 else 0
        overflow = 1 if i < 5 else 0
        # omni 错误:i==3 时一次,其余 0
        omni_err = 1 if i == 3 else 0
        omni_call = 1 if not skipped else 0
        ts = now_ms - i * 60_000
        conn.execute(
            "INSERT INTO traces (trace_id, timestamp, skipped, "
            "decode_ms, collect_ms, convert_ms, gate_ms, identity_ms, omni_ms, log_ms, "
            "cycle_total_ms, pipeline_total_ms, window_duration_ms, "
            "in_delay_ms, stream_lag_ms, "
            "gate_video_pass, gate_audio_pass, "
            "omni_call_count, omni_error_count, "
            "dropped_windows_total, overflow_count_total) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"c-{i}", ts, skipped,
             10.0, 5.0, 3.0, 2.0, identity_ms, omni_ms, 1.0,
             100.0 + i * 10, 90.0 + i * 10, 3000.0,
             50.0, 20.0,
             1 if i % 2 == 0 else 0, 1 if i % 3 == 0 else 0,
             omni_call, omni_err,
             dropped, overflow),
        )
        conn.execute(
            "INSERT INTO traces_device "
            "(device_trace_id, cycle_id, timestamp, device_id) "
            "VALUES (?, ?, ?, ?)",
            (f"d-{i}", f"c-{i}", ts, "camera-1"),
        )
        # 前 7 条 cycle 各挂 1 个 agent_run
        if i < 7:
            conn.execute(
                "INSERT INTO agent_runs (run_id, trace_id, timestamp, source, "
                "duration_ms, success) VALUES (?, ?, ?, ?, ?, ?)",
                (f"r-{i}", f"c-{i}", ts, "interaction", 500.0, 1),
            )
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db
    return app


def test_stats_summary_returns_aggregate_object(app_with_full_data):
    with TestClient(app_with_full_data) as tc:
        r = tc.get("/api/stats?metric=summary")
    assert r.status_code == 200
    d = r.json()
    # 必备字段都在
    for k in (
        "cycle_count", "processed_device_window_count", "skip_rate",
        "drop_rate", "omni_error_rate",
        "p95_rtf_e2e", "p95_rtf_omni", "agent_call_count", "window",
    ):
        assert k in d
    # 数值合理性:20 条 cycle,一半 skip → skip_rate=0.5
    assert d["cycle_count"] == 20
    assert d["processed_device_window_count"] == 20
    assert d["skip_rate"] == pytest.approx(0.5, abs=1e-6)
    # 丢包率:10 dropped,20 processed device windows → 10/30
    assert d["drop_rate"] == pytest.approx(10 / 30, abs=1e-6)
    # omni 错误率:1 / 10 个非 skip cycle
    assert d["omni_error_rate"] == pytest.approx(0.1, abs=1e-6)
    # agent 调用数:i<7 → 7
    assert d["agent_call_count"] == 7


def test_stats_summary_drop_rate_uses_device_windows(tmp_path):
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO traces (trace_id, timestamp, dropped_windows_total) "
        "VALUES (?, ?, ?)",
        ("c-1", now_ms, 2),
    )
    for i in range(2):
        conn.execute(
            "INSERT INTO traces_device "
            "(device_trace_id, cycle_id, timestamp, device_id) "
            "VALUES (?, ?, ?, ?)",
            (f"d-{i}", "c-1", now_ms, f"camera-{i}"),
        )
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db

    with TestClient(app) as tc:
        response = tc.get("/api/stats?metric=summary")

    assert response.status_code == 200
    data = response.json()
    assert data["cycle_count"] == 1
    assert data["processed_device_window_count"] == 2
    assert data["drop_rate"] == pytest.approx(2 / 4, abs=1e-6)


def test_stats_summary_empty_window(app_with_full_data):
    """指定一个空窗口,返回结构完整且为零。"""
    with TestClient(app_with_full_data) as tc:
        r = tc.get("/api/stats?metric=summary&since=1&until=2")
    assert r.status_code == 200
    d = r.json()
    assert d["cycle_count"] == 0
    assert d["skip_rate"] == 0.0
    assert d["agent_call_count"] == 0


def test_stats_drop_series(app_with_full_data):
    with TestClient(app_with_full_data) as tc:
        r = tc.get("/api/stats?metric=drop_series&bucket=1h")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d, list)
    if d:
        for k in ("ts", "dropped", "overflow_count", "cycle_count"):
            assert k in d[0]
    # fixture 里前 5 条 cycle 各丢 2 个,共 10 个;后 15 条不丢
    total_dropped = sum(b["dropped"] for b in d)
    assert total_dropped == 10
    total_overflow = sum(b["overflow_count"] for b in d)
    assert total_overflow == 5


def test_stats_stage_percentiles(app_with_full_data):
    with TestClient(app_with_full_data) as tc:
        r = tc.get("/api/stats?metric=stage_percentiles")
    assert r.status_code == 200
    d = r.json()
    for f in ("decode_ms", "collect_ms", "convert_ms", "gate_ms",
              "identity_ms", "omni_ms", "log_ms"):
        assert f in d
        for k in ("avg", "p50", "p75", "p95", "p99", "sample_size"):
            assert k in d[f]
    # decode 在所有 20 条 cycle 都跑了(>0);i==3 因 omni_error_count>0 被过滤,剩 19
    assert d["decode_ms"]["sample_size"] == 19
    # identity/omni 只在前 10 条非 skip cycle 才 >0;i==3 omni 错误过滤后剩 9
    assert d["identity_ms"]["sample_size"] == 9
    assert d["omni_ms"]["sample_size"] == 9


def test_stats_gate_score_percentiles_per_device(tmp_path):
    """gate_video_score / gate_audio_energy 按 device 分组算 P50/P75/P90/P99。
    NULL 行被过滤,跨 device 排序按 device_id 字典序。
    """
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    now_ms = int(time.time() * 1000)

    # d1: 10 个 video_score 均匀 [0.001..0.010],10 个 audio_energy [0.005..0.050]
    # d2: 10 个 video_score 均匀 [0.020..0.029],只 5 个有 audio_energy(其余 NULL)
    for i in range(10):
        conn.execute(
            "INSERT INTO traces_device "
            "(device_trace_id, cycle_id, timestamp, device_id, room_name, "
            " gate_video_score, gate_audio_energy) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"d1-{i}", f"c-{i}", now_ms - i * 60_000, "d1", "客厅",
                0.001 + i * 0.001, 0.005 + i * 0.005,
            ),
        )
        conn.execute(
            "INSERT INTO traces_device "
            "(device_trace_id, cycle_id, timestamp, device_id, room_name, "
            " gate_video_score, gate_audio_energy) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"d2-{i}", f"c-{i}", now_ms - i * 60_000, "d2", "书房",
                0.020 + i * 0.001,
                0.010 + i * 0.002 if i < 5 else None,
            ),
        )
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db
    with TestClient(app) as tc:
        r = tc.get("/api/stats?metric=gate_score_percentiles&bucket=1h")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert [row["device_id"] for row in data] == ["d1", "d2"]

    d1 = next(row for row in data if row["device_id"] == "d1")
    assert d1["room_name"] == "客厅"
    # 10 个值 [0.001..0.010] 的 P50 ≈ 0.0055,P99 = 线性插值落在 [0.009, 0.010]
    assert d1["video"]["count"] == 10
    assert d1["video"]["p50"] == pytest.approx(0.0055, abs=1e-6)
    assert 0.009 <= d1["video"]["p99"] <= 0.010
    assert d1["audio"]["count"] == 10

    d2 = next(row for row in data if row["device_id"] == "d2")
    # audio 只有 5 个非 NULL
    assert d2["audio"]["count"] == 5
    assert d2["video"]["count"] == 10


def test_stats_gate_score_percentiles_empty(tmp_path):
    """没有数据时返回空 list,不报错。"""
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db
    with TestClient(app) as tc:
        r = tc.get("/api/stats?metric=gate_score_percentiles&bucket=1h")
    assert r.status_code == 200
    assert r.json() == []


def test_stats_gate_score_percentiles_all_null_returns_zero_count(tmp_path):
    """device 行存在但 score 全 NULL → 该 device 视频/音频 count=0、percentile=None。"""
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    for i in range(3):
        conn.execute(
            "INSERT INTO traces_device "
            "(device_trace_id, cycle_id, timestamp, device_id, room_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"d-{i}", f"c-{i}", now_ms - i * 60_000, "d", "厨房"),
        )
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db
    with TestClient(app) as tc:
        r = tc.get("/api/stats?metric=gate_score_percentiles&bucket=1h")
    data = r.json()
    assert len(data) == 1
    assert data[0]["device_id"] == "d"
    assert data[0]["video"] == {
        "p50": None, "p75": None, "p90": None, "p99": None, "count": 0,
    }


def test_stats_omni_error_series_includes_buckets_without_errors(tmp_path):
    """无 omni 错误的 bucket 也返回(填 0),X 轴跟 drop_series 对齐。
    回归: 之前 SQL 仅 GROUP BY cycle_err,X 轴会卡在最后一根错误柱。
    """
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    # 对齐到 1m bucket 起点,避免 bucket 边界抖动
    base = (now_ms // 60_000) * 60_000
    for i in range(5):
        ts = base - i * 60_000
        conn.execute(
            "INSERT INTO traces (trace_id, timestamp) VALUES (?, ?)",
            (f"c-{i}", ts),
        )
        # 只有 i==0 那个 cycle 记一条 omni 错误,其余 cycle 的 device 行都没 omni_error_code
        omni_err_code = "HTTPStatusError:429" if i == 0 else None
        conn.execute(
            "INSERT INTO traces_device "
            "(device_trace_id, cycle_id, timestamp, device_id, room_name, omni_error_code) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"d-{i}", f"c-{i}", ts, "d", "客厅", omni_err_code),
        )
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db
    since = base - 10 * 60_000
    with TestClient(app) as tc:
        r = tc.get(f"/api/stats?metric=omni_error_series&bucket=1m&since={since}")
    assert r.status_code == 200
    data = r.json()
    # 5 个 cycle 落 5 个 1m bucket,即使 4 个没错误也要返回
    assert len(data) == 5
    # 总错误数仍然是 1(限流),其他类型都 0
    assert sum(b["rate_limit"] for b in data) == 1
    assert sum(b["timeout"] for b in data) == 0
    assert sum(b["other"] for b in data) == 0
    # 4 个 bucket 全 0
    zero_buckets = [b for b in data if b["rate_limit"] == 0 and b["timeout"] == 0 and b["other"] == 0]
    assert len(zero_buckets) == 4


def test_stats_invalid_metric_returns_400(app_with_data):
    with TestClient(app_with_data) as tc:
        r = tc.get("/api/stats?metric=non_existent")
    assert r.status_code == 400


def test_stats_invalid_bucket_returns_400(app_with_data):
    with TestClient(app_with_data) as tc:
        r = tc.get("/api/stats?metric=rtf_series&bucket=99x")
    assert r.status_code == 400
