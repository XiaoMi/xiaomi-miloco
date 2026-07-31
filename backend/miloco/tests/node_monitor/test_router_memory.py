"""Integration tests for /monitor/memory + /monitor/memory/series + /monitor/proc/series.

用 TestClient 直接挂 monitor router，不拉起完整 lifespan / database / 感知。
verify_token 在 settings.server.token="" 时 bypass（默认值）。
"""

import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.node_monitor.mem_snapshot import CategoryStats, MemSnapshot
from miloco.node_monitor.monitor import NodeMonitor, get_monitor
from miloco.node_monitor.py_heap import PyHeapSnapshot, PyTypeStats
from miloco.node_monitor.resource_monitor import ResourceMonitor
from miloco.node_monitor.router import router as monitor_router
from miloco.node_monitor.router import set_resource_monitor


def _make_smaps(ts: float = 12345.0) -> MemSnapshot:
    return MemSnapshot(
        ts=ts,
        total_rss_kb=600_000,
        categories=[
            CategoryStats("[heap]", 250_000, 1),
            CategoryStats("libfoo.so", 100_000, 1),
        ],
        other_rss_kb=250_000,
        other_count=20,
    )


def _make_py(ts: float = 12345.0) -> PyHeapSnapshot:
    return PyHeapSnapshot(
        ts=ts,
        total_objects=100_000,
        total_size_kb=50_000,
        types=[PyTypeStats("builtins.dict", 30_000, 25_000)],
        other_size_kb=25_000,
        other_count=70_000,
    )


@pytest.fixture(autouse=True)
def _reset_monitor():
    NodeMonitor._reset()
    # 清掉 router 内 global，避免 case 间污染
    import miloco.node_monitor.router as router_module

    router_module._resource_monitor = None
    router_module._start_time = None
    yield
    router_module._resource_monitor = None
    router_module._start_time = None
    NodeMonitor._reset()


@pytest.fixture
def app():
    a = FastAPI()
    # 与 main.py 注册路径一致：prefix="/api"（router 自己再带 "/monitor"）
    a.include_router(monitor_router, prefix="/api")
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def rm(tmp_path):
    db = tmp_path / "miloco.db"
    db.write_bytes(b"x" * 1024)
    log = tmp_path / "log"
    log.mkdir()
    return ResourceMonitor(get_monitor(), db_path=str(db), log_dir=str(log))


class TestMemoryEndpoint:
    def test_503_when_monitor_not_initialized(self, client):
        resp = client.get("/api/monitor/memory")
        assert resp.status_code == 503
        assert "not initialized" in resp.json()["error"]

    def test_503_when_smaps_unavailable_and_no_py_heap(self, client, rm):
        rm._mem_available = False
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/memory")
        assert resp.status_code == 503
        assert "not available" in resp.json()["error"]

    def test_503_when_not_yet_collected(self, client, rm):
        # smaps available but ring/latest 都为空
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/memory")
        assert resp.status_code == 503
        assert "not yet collected" in resp.json()["error"]

    def test_200_full_snapshot(self, client, rm):
        with (
            patch(
                "miloco.node_monitor.resource_monitor.parse_smaps",
                return_value=_make_smaps(),
            ),
            patch(
                "miloco.node_monitor.resource_monitor.sample_py_heap",
                return_value=_make_py(),
            ),
        ):
            rm._collect()
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/memory")
        assert resp.status_code == 200
        data = resp.json()
        # smaps 段
        assert data["total_rss_kb"] == 600_000
        assert len(data["categories"]) == 2
        assert data["categories"][0]["name"] == "[heap]"
        assert data["other_count"] == 20
        # python_heap 段
        assert data["python_heap"]["total_objects"] == 100_000
        assert data["python_heap"]["types"][0]["qualname"] == "builtins.dict"

    def test_200_py_heap_only_when_smaps_unavailable(self, client, rm):
        rm._mem_available = False
        with patch(
            "miloco.node_monitor.resource_monitor.sample_py_heap",
            return_value=_make_py(),
        ):
            rm._collect()
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_rss_kb" not in data
        assert "categories" not in data
        assert "python_heap" in data


class TestMemorySeriesEndpoint:
    def test_503_when_monitor_not_initialized(self, client):
        resp = client.get("/api/monitor/memory/series")
        assert resp.status_code == 503

    def test_200_default_window_and_bucket(self, client, rm):
        # 入环 ts 取自快照(见 resource_monitor 的 ts_val),必须给真实时间:
        # get_memory_series 按 cutoff=now-window 过滤,默认的 12345.0 会被整点滤掉,
        # points 恒为空、下面的字段断言就永远跑不到。
        now = time.time()
        with (
            patch(
                "miloco.node_monitor.resource_monitor.parse_smaps",
                return_value=_make_smaps(ts=now),
            ),
            patch(
                "miloco.node_monitor.resource_monitor.sample_py_heap",
                return_value=_make_py(ts=now),
            ),
        ):
            rm._collect()
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/memory/series")
        assert resp.status_code == 200
        data = resp.json()
        # bucket=1m default → interval_s 至少 60（路由有 max(bucket, 60) 钳制）
        assert data["interval_s"] >= 60
        assert data["points"], "采了一次窗口内的快照后必须有点,空数组说明入环坏了"
        p = data["points"][0]
        assert "rss_kb" in p
        assert "py_objects" in p
        assert "py_size_kb" in p

    def test_200_various_window_bucket_combinations(self, client, rm):
        set_resource_monitor(rm, 0.0)
        for window, bucket in [
            ("1h", "1m"),
            ("6h", "5m"),
            ("24h", "1h"),
            ("3d", "1h"),
        ]:
            resp = client.get(
                f"/api/monitor/memory/series?window={window}&bucket={bucket}"
            )
            assert resp.status_code == 200, f"failed for {window}/{bucket}"

    def test_invalid_window_returns_422(self, client, rm):
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/memory/series?window=999h")
        assert resp.status_code == 422

    def test_invalid_bucket_returns_422(self, client, rm):
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/memory/series?window=1h&bucket=999s")
        assert resp.status_code == 422


class TestProcSeriesEndpoint:
    def test_503_when_monitor_not_initialized(self, client):
        resp = client.get("/api/monitor/proc/series")
        assert resp.status_code == 503

    def test_200_shape(self, client, rm):
        rm._collect()
        rm._collect()  # 首次采样跳过入环，第二次才有点
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/proc/series")
        assert resp.status_code == 200
        data = resp.json()
        assert data["core_count"] >= 1
        assert data["interval_s"] >= 60
        assert data["points"], "跑了两次 _collect 后必须有点,空数组说明入环坏了"
        p = data["points"][0]
        assert "cpu_pct" in p
        assert "cpu_pct_max" in p
        assert "num_threads" in p

    def test_200_various_window_bucket_combinations(self, client, rm):
        set_resource_monitor(rm, 0.0)
        for window, bucket in [
            ("1h", "1m"),
            ("6h", "5m"),
            ("24h", "1h"),
            ("3d", "1h"),
        ]:
            resp = client.get(
                f"/api/monitor/proc/series?window={window}&bucket={bucket}"
            )
            assert resp.status_code == 200, f"failed for {window}/{bucket}"

    def test_invalid_window_returns_422(self, client, rm):
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/proc/series?window=999h")
        assert resp.status_code == 422

    def test_invalid_bucket_returns_422(self, client, rm):
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/proc/series?window=1h&bucket=999s")
        assert resp.status_code == 422


class TestResourcesEndpointUnaffected:
    """回归保护：/monitor/resources 仍只返原 5 字段。"""

    def test_resources_endpoint_no_memory_fields(self, client, rm):
        with (
            patch(
                "miloco.node_monitor.resource_monitor.parse_smaps",
                return_value=_make_smaps(),
            ),
            patch(
                "miloco.node_monitor.resource_monitor.sample_py_heap",
                return_value=_make_py(),
            ),
        ):
            rm._collect()
        set_resource_monitor(rm, 0.0)
        resp = client.get("/api/monitor/resources")
        assert resp.status_code == 200
        data = resp.json()
        assert "rss_mb" in data
        assert "cpu_pct" in data
        # 不该有 memory monitor 新增字段
        assert "categories" not in data
        assert "python_heap" not in data
        assert "total_rss_kb" not in data
