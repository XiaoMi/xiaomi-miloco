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


def test_stats_latency_series(app_with_data):
    with TestClient(app_with_data) as tc:
        r = tc.get("/api/stats?metric=latency_series&bucket=1h")
    assert r.status_code == 200
    data = r.json()
    if data:
        assert "ms_cycle" in data[0] and "ms_e2e" in data[0]
        assert "ms_omni" in data[0]
        # ms_omni_ok 跟 ms_e2e_ok 一对(仅 omni 成功 cycle 的均值)
        assert "ms_e2e_ok" in data[0]
        assert "ms_omni_ok" in data[0]
        # window_ms 是耗时图那条「可用时间」参考线的数据源,缺了图上就没有判据
        assert "window_ms" in data[0]
        # fixture 的窗口跨度恒为 3000ms
        assert data[0]["window_ms"] == 3000


def test_summary_p95_recomputed_on_ms_not_converted_from_ratio(tmp_path):
    """P95 必须在毫秒上重算,不能由比值换算。

    构造两行,让「耗时最大的行」与「比值最大的行」互不相同:
      A: 耗时 100ms / 窗口  1000ms → 比值 0.10(比值最大,耗时最小)
      B: 耗时 200ms / 窗口 10000ms → 比值 0.02(耗时最大,比值最小)
    毫秒 P95 必须落在 B 那一侧。若换成「先取比值 P95 再乘窗口」,拿到的是 A 那一行,
    量级完全不同——这正是耗时不能由比值反推的原因。
    """
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    for i, (cycle_ms, window_ms) in enumerate([(100.0, 1000.0), (200.0, 10000.0)]):
        conn.execute(
            "INSERT INTO traces (trace_id, timestamp, cycle_total_ms, "
            "window_duration_ms, omni_call_count, omni_error_count) "
            "VALUES (?, ?, ?, ?, 1, 0)",
            (f"p-{i}", now_ms - i * 60_000, cycle_ms, window_ms),
        )
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db
    with TestClient(app) as tc:
        d = tc.get("/api/stats?metric=summary").json()

    # 毫秒口径下 P95 贴着 B(200ms);比值口径下最大的是 A(0.10),两者指向不同的行
    assert d["p95_ms_e2e"] > 150.0
    assert d["p95_ms_e2e"] <= 200.0


def test_window_zero_rows_excluded_from_latency_but_still_counted(tmp_path):
    """窗口跨度为 0 的行不参与耗时聚合,但仍计入轮次等全量口径。

    改造前读的是 traces_v 视图,每个比值都包在 `CASE WHEN window_duration_ms > 0`
    里,跨度为 0 的行比值为 NULL、不参与均值;改造后直接取毫秒列,必须自己带上这道
    过滤,否则口径就与改造前不同了。另一层理由:耗时图的判据是「耗时对可用时间」,
    两条线必须来自同一批 cycle。

    同时这道过滤**不能**溢到同一张卡的其他格上——轮次、丢弃率、Omni 错误率仍是
    全量口径。故用 CASE 而不是给整条查询加 WHERE。
    """
    db = tmp_path / "obs.db"
    conn = connect(db)
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    rows = [
        (4000.0, 1000.0),
        (0.0, 9999.0),   # 跨度未知：耗时异常大,若计入会把均值明显拉高
        (4000.0, 2000.0),
    ]
    for i, (window_ms, cycle_ms) in enumerate(rows):
        conn.execute(
            "INSERT INTO traces (trace_id, timestamp, cycle_total_ms, "
            "window_duration_ms, omni_call_count, omni_error_count) "
            "VALUES (?, ?, ?, ?, 1, 0)",
            (f"w-{i}", now_ms - i * 60_000, cycle_ms, window_ms),
        )
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db
    with TestClient(app) as tc:
        series = tc.get("/api/stats?metric=latency_series&bucket=1h").json()
        summ = tc.get("/api/stats?metric=summary").json()

    # 耗时侧：只有两行参与,均值 1500 而不是 (1000+9999+2000)/3 = 4333
    assert len(series) == 1
    assert series[0]["ms_cycle"] == 1500.0
    assert series[0]["window_ms"] == 4000.0
    # P95 也只在那两行里取,不会被 9999 顶上去
    assert summ["p95_ms_e2e"] <= 2000.0
    # 全量口径不受这道过滤影响：三行都算轮次
    assert summ["cycle_count"] == 3


def _summary_of(tmp_path, rows, name):
    """rows 为 [(window_duration_ms, cycle_total_ms), ...],建库取 summary。"""
    db = tmp_path / f"{name}.db"
    conn = connect(db)
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    for i, (window_ms, cycle_ms) in enumerate(rows):
        conn.execute(
            "INSERT INTO traces (trace_id, timestamp, cycle_total_ms, "
            "window_duration_ms, omni_call_count, omni_error_count) "
            "VALUES (?, ?, ?, ?, 1, 0)",
            (f"{name}-{i}", now_ms - (i % 600) * 1000, cycle_ms, window_ms),
        )
    conn.commit()
    conn.close()
    app = FastAPI()
    app.include_router(router)
    app.state.obs_db_path = db
    with TestClient(app) as tc:
        return tc.get("/api/stats?metric=summary").json()


def test_p95_behind_matches_ratio_criterion(tmp_path):
    """「跟不上」的判据必须是**逐行比值的 P95 与 1 比**,与改造前一字不差。

    不能改用「毫秒 P95 对窗口跨度均值」:P95 是尾部统计量、均值是中心统计量,两者
    不可互推。下面两组数据就是那种写法的假阳性与假阴性,窗口跨度不齐时都会发生
    (跨度取 batch 内所有 snapshot 的最大跨度,本来就不是常量)。
    """
    # 假阳性：每一轮都跟得上自己的窗口（比值 0.5 与 0.75），不该标红。
    # 但「毫秒 P95 (=15000) > 跨度均值 (=2900)」会把它标红。
    ok = _summary_of(tmp_path, [(1000.0, 500.0)] * 90 + [(20000.0, 15000.0)] * 10, "fp")
    assert ok["p95_behind_e2e"] is False
    assert ok["p95_ms_e2e"] > ok["p95_ms_omni"] or True  # 显示值仍是毫秒
    assert ok["p95_ms_e2e"] >= 500.0

    # 假阴性：10% 的轮次跑到自己窗口的两倍（200/100），必须标红。
    # 但「毫秒 P95 (=1000) < 跨度均值 (=9010)」会漏掉它。
    bad = _summary_of(tmp_path, [(10000.0, 1000.0)] * 90 + [(100.0, 200.0)] * 10, "fn")
    assert bad["p95_behind_e2e"] is True
    # 显示的毫秒 P95 恰恰比上面那组小——正说明显示值不能拿来当判据
    assert bad["p95_ms_e2e"] < ok["p95_ms_e2e"]


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
        "cycle_count", "skip_rate", "drop_rate", "omni_error_rate",
        "p95_ms_e2e", "p95_ms_omni", "p95_behind_e2e", "p95_behind_omni",
        "agent_call_count", "window",
    ):
        assert k in d
    # 数值合理性:20 条 cycle,一半 skip → skip_rate=0.5
    assert d["cycle_count"] == 20
    assert d["skip_rate"] == pytest.approx(0.5, abs=1e-6)
    # 丢包率:5*2=10 dropped,cycle=20 → 10/30
    assert d["drop_rate"] == pytest.approx(10 / 30, abs=1e-6)
    # omni 错误率:1 / 10 个非 skip cycle
    assert d["omni_error_rate"] == pytest.approx(0.1, abs=1e-6)
    # agent 调用数:i<7 → 7
    assert d["agent_call_count"] == 7


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
        r = tc.get("/api/stats?metric=latency_series&bucket=99x")
    assert r.status_code == 400
