import time
from unittest.mock import patch

import pytest
from miloco.node_monitor.mem_snapshot import CategoryStats, MemSnapshot
from miloco.node_monitor.monitor import NodeMonitor, get_monitor
from miloco.node_monitor.py_heap import PyHeapSnapshot, PyTypeStats
from miloco.node_monitor.resource_monitor import (
    MEMORY_RING_MAXLEN,
    PROC_RING_MAXLEN,
    ResourceMonitor,
)


def _make_smaps(ts=1000.0, rss=612000) -> MemSnapshot:
    return MemSnapshot(
        ts=ts,
        total_rss_kb=rss,
        categories=[CategoryStats("[heap]", rss, 1)],
        other_rss_kb=0,
        other_count=0,
    )


def _make_py(ts=1000.0, objs=10000, size=8000) -> PyHeapSnapshot:
    return PyHeapSnapshot(
        ts=ts,
        total_objects=objs,
        total_size_kb=size,
        types=[PyTypeStats("builtins.dict", 500, 4000)],
        other_size_kb=0,
        other_count=0,
    )


@pytest.fixture(autouse=True)
def _reset_monitor():
    NodeMonitor._reset()
    yield
    NodeMonitor._reset()


@pytest.fixture
def tmp_db(tmp_path):
    p = tmp_path / "miloco.db"
    p.write_bytes(b"x" * 4096)
    return str(p)


@pytest.fixture
def tmp_log_dir(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    (d / "miloco-backend.log").write_bytes(b"a" * 8192)
    (d / "node_events.log").write_bytes(b"b" * 2048)
    return str(d)


class TestCollect:
    def test_collect_populates_process_fields(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm._collect()
        d = rm.get_data()
        assert "ts" in d
        assert "rss_mb" in d and d["rss_mb"] > 0
        assert "fd" in d and d["fd"] > 0
        assert "cpu_pct" in d

    def test_collect_db_size_from_existing_file(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm._collect()
        d = rm.get_data()
        assert d["db_size_mb"] == round(4096 / (1024 * 1024), 2)

    def test_collect_log_size_sums_files(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm._collect()
        d = rm.get_data()
        assert d["log_size_mb"] == round((8192 + 2048) / (1024 * 1024), 2)

    def test_collect_omits_db_size_when_missing(self, tmp_path, tmp_log_dir):
        missing = str(tmp_path / "nonexistent.db")
        rm = ResourceMonitor(get_monitor(), db_path=missing, log_dir=tmp_log_dir)
        rm._collect()
        d = rm.get_data()
        assert "db_size_mb" not in d

    def test_collect_log_size_zero_when_dir_missing(self, tmp_db, tmp_path):
        missing = str(tmp_path / "no_such_log_dir")
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=missing)
        rm._collect()
        d = rm.get_data()
        assert d["log_size_mb"] == 0.0


class TestGetData:
    def test_returns_copy(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm._collect()
        d = rm.get_data()
        d["rss_mb"] = -1
        assert rm.get_data()["rss_mb"] != -1

    def test_empty_before_collect(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        assert rm.get_data() == {}


class TestThread:
    def test_start_stop(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not rm.get_data():
            time.sleep(0.05)
        assert rm._thread is not None and rm._thread.is_alive()
        assert "ts" in rm.get_data()
        rm.stop()
        assert not rm._thread.is_alive()


class TestMemoryCollect:
    def test_collect_appends_point_and_updates_both_latests(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
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
        assert len(rm._memory_ring) == 1
        assert rm._memory_latest is not None
        assert rm._py_heap_latest is not None
        latest = rm.get_memory_latest()
        assert latest is not None
        assert "total_rss_kb" in latest
        assert "python_heap" in latest

    def test_ring_caps_at_maxlen(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
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
            for _ in range(MEMORY_RING_MAXLEN + 2):
                rm._collect()
        assert len(rm._memory_ring) == MEMORY_RING_MAXLEN

    def test_series_window_filter(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        # 让 3 个 ts 落在不同的 60s 桶里：用对齐到 60s 整数倍的基准
        bucket_aligned = int(time.time() // 60) * 60
        rm._memory_ring.append((bucket_aligned - 180, 200, 1000, 50))  # 窗口外
        rm._memory_ring.append((bucket_aligned - 60, 210, 1100, 55))  # 窗口内
        rm._memory_ring.append((bucket_aligned, 220, 1200, 60))  # 窗口内
        # window=time.time() - bucket_aligned + 120 保证只保留后两个
        cutoff_offset = int(time.time() - bucket_aligned) + 120
        series = rm.get_memory_series(window_seconds=cutoff_offset, bucket_seconds=60)
        # 后两点 ts 差 60s，落在不同 60s 桶
        assert len(series["points"]) == 2

    def test_series_bucket_aggregation(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        # 选 180s 对齐的基准（- 540 = - 3*180 保持对齐），6 个点分成两个清晰的桶
        base = int(time.time() // 180) * 180 - 540
        for i in range(6):
            rm._memory_ring.append(
                (float(base + i * 60), 200 + i * 10, 1000, 50)
            )
        # 用足够大的 window（覆盖 base）但避免 999999 这种巨值的不必要计算
        window = int(time.time() - base + 100)
        series = rm.get_memory_series(window_seconds=window, bucket_seconds=180)
        assert len(series["points"]) == 2
        assert series["points"][0]["rss_kb"] == 210  # (200+210+220)/3
        assert series["points"][1]["rss_kb"] == 240  # (230+240+250)/3

    def test_series_includes_py_fields(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm._memory_ring.append((time.time(), 200, 1000, 50))
        series = rm.get_memory_series(window_seconds=60, bucket_seconds=60)
        assert "py_objects" in series["points"][0]
        assert "py_size_kb" in series["points"][0]

    def test_smaps_unavailable_keeps_py_heap(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm._mem_available = False
        with patch(
            "miloco.node_monitor.resource_monitor.sample_py_heap",
            return_value=_make_py(),
        ):
            rm._collect()
        latest = rm.get_memory_latest()
        assert latest is not None
        assert "total_rss_kb" not in latest
        assert "python_heap" in latest

    def test_smaps_parse_failure_keeps_old_latest(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        with (
            patch(
                "miloco.node_monitor.resource_monitor.parse_smaps",
                return_value=_make_smaps(rss=111),
            ),
            patch(
                "miloco.node_monitor.resource_monitor.sample_py_heap",
                return_value=_make_py(),
            ),
        ):
            rm._collect()
        first_rss = rm._memory_latest.total_rss_kb
        with (
            patch(
                "miloco.node_monitor.resource_monitor.parse_smaps",
                side_effect=OSError("boom"),
            ),
            patch(
                "miloco.node_monitor.resource_monitor.sample_py_heap",
                return_value=_make_py(),
            ),
        ):
            rm._collect()
        assert rm._memory_latest.total_rss_kb == first_rss
        assert rm._mem_available is True

    def test_py_heap_failure_keeps_smaps(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        with (
            patch(
                "miloco.node_monitor.resource_monitor.parse_smaps",
                return_value=_make_smaps(),
            ),
            patch(
                "miloco.node_monitor.resource_monitor.sample_py_heap",
                side_effect=RuntimeError("boom"),
            ),
        ):
            rm._collect()
        assert len(rm._memory_ring) == 1
        assert rm._memory_latest is not None
        assert rm._py_heap_latest is None

    def test_both_failed_skips_ring(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        with (
            patch(
                "miloco.node_monitor.resource_monitor.parse_smaps",
                side_effect=OSError(),
            ),
            patch(
                "miloco.node_monitor.resource_monitor.sample_py_heap",
                side_effect=RuntimeError(),
            ),
        ):
            rm._collect()
        assert len(rm._memory_ring) == 0

    def test_resources_data_unaffected(self, tmp_db, tmp_log_dir):
        """回归保护：现有 get_data() 字段不被新增内存监控影响。"""
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
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
        data = rm.get_data()
        assert "rss_mb" in data and "cpu_pct" in data and "fd" in data
        assert "db_size_mb" in data and "log_size_mb" in data
        assert "categories" not in data
        assert "python_heap" not in data
        assert "total_rss_kb" not in data


class TestProcCollect:
    def test_first_sample_skips_ring(self, tmp_db, tmp_log_dir):
        """首次采样测量窗口仅启动探测那几十毫秒，读数虚高，不入环。"""
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm._collect()
        assert len(rm._proc_ring) == 0
        rm._collect()
        assert len(rm._proc_ring) == 1
        ts, pct, nthreads = rm._proc_ring[0]
        assert isinstance(pct, float)
        assert nthreads > 0

    def test_proc_ring_survives_memory_failure(self, tmp_db, tmp_log_dir):
        """内存两路全挂时 _collect 会 early-return，CPU 仍须已入环。"""
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        with (
            patch(
                "miloco.node_monitor.resource_monitor.parse_smaps",
                side_effect=OSError(),
            ),
            patch(
                "miloco.node_monitor.resource_monitor.sample_py_heap",
                side_effect=RuntimeError(),
            ),
        ):
            rm._collect()  # 首次：CPU 跳过入环，内存两路失败
            rm._collect()  # 第二次：CPU 入环，内存 early-return 不入 memory_ring
        assert len(rm._memory_ring) == 0
        assert len(rm._proc_ring) == 1

    def test_proc_ring_caps_at_maxlen(self, tmp_db, tmp_log_dir):
        # patch 掉内存采集(smaps 全读 + 遍历 Python 堆)，否则跑满环长要几千次真实
        # 采集，慢到超时。CPU 段仍真实调用，本测试只验证 deque maxlen 截断。
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
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
            for _ in range(PROC_RING_MAXLEN + 2):
                rm._collect()
        assert len(rm._proc_ring) == PROC_RING_MAXLEN

    def test_num_threads_fallback_keeps_last_value(self, tmp_db, tmp_log_dir):
        """线程数取不到时沿用上一个已知值，不写 0（避免曲线假性归零）。"""
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm._collect()  # 首次跳过
        rm._collect()  # 入环一个正常值
        prev = rm._proc_ring[-1][2]
        assert prev > 0
        with patch.object(
            rm._psutil_proc, "num_threads", side_effect=OSError("boom")
        ):
            rm._collect()
        assert rm._proc_ring[-1][2] == prev

    def test_num_threads_fallback_on_first_ringed_sample(self, tmp_db, tmp_log_dir):
        """首采样已观测到线程数;紧接着的第 2 次失败时应沿用它,而非写 0。"""
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        rm._collect()  # 首次:成功采到线程数,但跳过入环
        observed = rm.get_data()["num_threads"]
        assert observed > 0
        with patch.object(
            rm._psutil_proc, "num_threads", side_effect=OSError("boom")
        ):
            rm._collect()  # 第二次:入环,但线程数取不到
        assert rm._proc_ring[-1][2] == observed

    def test_proc_series_bucket_aggregation(self, tmp_db, tmp_log_dir):
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        base = int(time.time() // 180) * 180 - 540
        for i in range(6):
            rm._proc_ring.append((float(base + i * 60), 10.0 * (i + 1), 20 + i))
        window = int(time.time() - base + 100)
        series = rm.get_proc_series(window_seconds=window, bucket_seconds=180)
        assert len(series["points"]) == 2
        assert series["points"][0]["cpu_pct"] == 20.0  # (10+20+30)/3
        assert series["points"][0]["cpu_pct_max"] == 30.0  # max(10,20,30)
        assert series["points"][1]["cpu_pct"] == 50.0  # (40+50+60)/3
        assert series["points"][1]["cpu_pct_max"] == 60.0
        assert series["points"][0]["num_threads"] == 21  # round((20+21+22)/3)

    def test_proc_series_empty_still_returns_core_count(self, tmp_db, tmp_log_dir):
        """空环也必须带 core_count：前端拿它当除数，缺了就 NaN。"""
        rm = ResourceMonitor(get_monitor(), db_path=tmp_db, log_dir=tmp_log_dir)
        series = rm.get_proc_series(window_seconds=3600, bucket_seconds=60)
        assert series["points"] == []
        assert series["core_count"] >= 1
