from __future__ import annotations

import collections
import logging
import os
import sys
import threading
import time

import psutil

from miloco.node_monitor.mem_snapshot import MemSnapshot
from miloco.node_monitor.py_heap import PyHeapSnapshot, sample_py_heap
from miloco.node_monitor.smaps import parse_smaps
from miloco.node_monitor.vmmap import parse_vmmap

logger = logging.getLogger(__name__)

RESOURCE_MONITOR_INTERVAL = 60
MEMORY_RING_MAXLEN = 3 * 24 * 60  # 3d @ 60s
PROC_RING_MAXLEN = 3 * 24 * 60  # 3d @ 60s
SMAPS_PATH = "/proc/self/smaps"
TASK_DIR = "/proc/self/task"

# (ts, rss_kb, py_objects, py_size_kb)
MemoryPoint = tuple[float, int, int, int]
# (ts, cpu_pct, num_threads)  —— CPU 占用百分比(多核可 > 100) + 进程线程数
ProcPoint = tuple[float, float, int]


def _sample_mem() -> MemSnapshot:
    """按平台分发：darwin 走 proc_pidinfo，其他走 /proc/self/smaps。"""
    if sys.platform == "darwin":
        return parse_vmmap()
    return parse_smaps(SMAPS_PATH, task_dir=TASK_DIR)


class ResourceMonitor:
    """Daemon thread that collects process resource metrics every 60s."""

    def __init__(self, monitor, db_path: str, log_dir: str):
        self._monitor = monitor
        self._db_path = db_path
        self._log_dir = log_dir
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._data: dict = {}
        self._lock = threading.Lock()
        self._psutil_proc = psutil.Process()
        self._memory_ring: collections.deque[MemoryPoint] = collections.deque(
            maxlen=MEMORY_RING_MAXLEN
        )
        self._memory_latest: MemSnapshot | None = None
        self._py_heap_latest: PyHeapSnapshot | None = None
        self._memory_lock = threading.Lock()
        self._mem_available = True
        self._proc_ring: collections.deque[ProcPoint] = collections.deque(
            maxlen=PROC_RING_MAXLEN
        )
        self._proc_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="resource-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def get_data(self) -> dict:
        with self._lock:
            return dict(self._data)

    def _run(self) -> None:
        # psutil.cpu_percent(interval=0) returns 0.0 on the very first call
        # (it has no prior baseline). Discard that reading so the first
        # _collect() exposes a real percentage rather than a misleading zero.
        try:
            self._psutil_proc.cpu_percent(interval=0)
        except Exception:
            pass
        # 启动探测内存 region 采集：失败标记不可用，后续 _collect 跳过该段
        try:
            _sample_mem()
        except Exception as e:
            self._mem_available = False
            logger.warning(
                "memory regions not available, categorization disabled: %s", e
            )
        self._collect()
        while not self._stop_event.wait(timeout=RESOURCE_MONITOR_INTERVAL):
            self._collect()

    def _collect(self) -> None:
        snapshot: dict = {"ts": time.time()}

        proc = self._psutil_proc
        cpu_pct: float | None = None
        try:
            cpu_pct = proc.cpu_percent(interval=0)
            snapshot["cpu_pct"] = cpu_pct
        except Exception:
            pass
        try:
            snapshot["rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            pass
        try:
            snapshot["fd"] = proc.num_fds()
        except Exception:
            pass
        num_threads = 0
        try:
            num_threads = proc.num_threads()
            snapshot["num_threads"] = num_threads
        except Exception:
            pass

        try:
            if os.path.exists(self._db_path):
                snapshot["db_size_mb"] = round(
                    os.path.getsize(self._db_path) / (1024 * 1024), 2
                )
        except Exception:
            pass

        try:
            total_log = 0
            if os.path.isdir(self._log_dir):
                for f in os.listdir(self._log_dir):
                    fp = os.path.join(self._log_dir, f)
                    if os.path.isfile(fp):
                        total_log += os.path.getsize(fp)
            snapshot["log_size_mb"] = round(total_log / (1024 * 1024), 2)
        except Exception:
            pass

        with self._lock:
            self._data = snapshot

        # CPU 时序独立入环：不受下面内存 region 采集 early-return 影响
        if cpu_pct is not None:
            with self._proc_lock:
                self._proc_ring.append((snapshot["ts"], cpu_pct, num_threads))

        # 内存 region + py_heap 采集（两路独立 try，互不影响）
        mem_snap: MemSnapshot | None = None
        if self._mem_available:
            try:
                mem_snap = _sample_mem()
            except Exception:
                logger.exception("memory region sample failed; will retry next cycle")

        py_snap: PyHeapSnapshot | None = None
        try:
            py_snap = sample_py_heap()
        except Exception:
            logger.exception("py_heap sample failed; will retry next cycle")

        if mem_snap is None and py_snap is None:
            return

        # 用本周期成功值 + 上次 latest 兜底凑全 4 字段
        rss_kb = (
            mem_snap.total_rss_kb
            if mem_snap
            else (self._memory_latest.total_rss_kb if self._memory_latest else 0)
        )
        py_objs = (
            py_snap.total_objects
            if py_snap
            else (self._py_heap_latest.total_objects if self._py_heap_latest else 0)
        )
        py_size = (
            py_snap.total_size_kb
            if py_snap
            else (self._py_heap_latest.total_size_kb if self._py_heap_latest else 0)
        )
        # 上面已 early-return，到这里 mem_snap / py_snap 至少一个非 None
        if mem_snap is not None:
            ts_val = mem_snap.ts
        else:
            assert py_snap is not None
            ts_val = py_snap.ts
        point: MemoryPoint = (ts_val, rss_kb, py_objs, py_size)

        with self._memory_lock:
            self._memory_ring.append(point)
            if mem_snap is not None:
                self._memory_latest = mem_snap
            if py_snap is not None:
                self._py_heap_latest = py_snap

    def get_memory_latest(self) -> dict | None:
        """latest 内存 region + py_heap 拍平 dict；两者都为 None 返回 None。"""
        with self._memory_lock:
            mem_snap = self._memory_latest
            py_snap = self._py_heap_latest
        if mem_snap is None and py_snap is None:
            return None
        return _combine_to_dict(mem_snap, py_snap)

    def get_memory_series(self, window_seconds: int, bucket_seconds: int) -> dict:
        """时序按 bucket_seconds 墙钟对齐 + 平均聚合。"""
        cutoff = time.time() - window_seconds
        with self._memory_lock:
            raw = [
                (ts, rss, py_objs, py_size)
                for ts, rss, py_objs, py_size in self._memory_ring
                if ts >= cutoff
            ]
        if not raw:
            return {
                "ts_start": None,
                "ts_end": None,
                "interval_s": bucket_seconds,
                "points": [],
            }

        bucket_s = max(bucket_seconds, RESOURCE_MONITOR_INTERVAL)
        buckets: dict[int, list[tuple[int, int, int]]] = {}
        for ts, rss, py_objs, py_size in raw:
            key = int(ts // bucket_s) * bucket_s
            buckets.setdefault(key, []).append((rss, py_objs, py_size))

        points = [
            {
                "ts": float(key),
                "rss_kb": sum(v[0] for v in vs) // len(vs),
                "py_objects": sum(v[1] for v in vs) // len(vs),
                "py_size_kb": sum(v[2] for v in vs) // len(vs),
            }
            for key, vs in sorted(buckets.items())
        ]
        return {
            "ts_start": points[0]["ts"],
            "ts_end": points[-1]["ts"],
            "interval_s": bucket_s,
            "points": points,
        }

    def get_proc_series(self, window_seconds: int, bucket_seconds: int) -> dict:
        """进程 CPU 占用 + 线程数时序，按 bucket_seconds 墙钟对齐 + 平均聚合。

        core_count = os.cpu_count()，供前端把多核 cpu_pct 归一化到 0-100%。
        """
        cutoff = time.time() - window_seconds
        with self._proc_lock:
            raw = [
                (ts, pct, nthreads)
                for ts, pct, nthreads in self._proc_ring
                if ts >= cutoff
            ]
        if not raw:
            return {
                "ts_start": None,
                "ts_end": None,
                "interval_s": bucket_seconds,
                "points": [],
                "core_count": os.cpu_count() or 1,
            }

        bucket_s = max(bucket_seconds, RESOURCE_MONITOR_INTERVAL)
        buckets: dict[int, list[tuple[float, int]]] = {}
        for ts, pct, nthreads in raw:
            key = int(ts // bucket_s) * bucket_s
            buckets.setdefault(key, []).append((pct, nthreads))

        points = [
            {
                "ts": float(key),
                "cpu_pct": round(sum(v[0] for v in vs) / len(vs), 1),
                "num_threads": round(sum(v[1] for v in vs) / len(vs)),
            }
            for key, vs in sorted(buckets.items())
        ]
        return {
            "ts_start": points[0]["ts"],
            "ts_end": points[-1]["ts"],
            "interval_s": bucket_s,
            "points": points,
            "core_count": os.cpu_count() or 1,
        }

    def is_memory_available(self) -> bool:
        return self._mem_available


def _combine_to_dict(
    mem_snap: MemSnapshot | None,
    py_snap: PyHeapSnapshot | None,
) -> dict:
    """内存 region + py_heap dataclass → JSON-serializable dict。缺失段不写字段。"""
    result: dict = {}
    if mem_snap is not None:
        result["ts"] = mem_snap.ts
        result["total_rss_kb"] = mem_snap.total_rss_kb
        result["categories"] = [
            {"name": c.name, "rss_kb": c.rss_kb, "count": c.count}
            for c in mem_snap.categories
        ]
        result["other_rss_kb"] = mem_snap.other_rss_kb
        result["other_count"] = mem_snap.other_count
    if py_snap is not None:
        result.setdefault("ts", py_snap.ts)
        result["python_heap"] = {
            "total_objects": py_snap.total_objects,
            "total_size_kb": py_snap.total_size_kb,
            "types": [
                {"qualname": t.qualname, "count": t.count, "size_kb": t.size_kb}
                for t in py_snap.types
            ],
            "other_size_kb": py_snap.other_size_kb,
            "other_count": py_snap.other_count,
        }
    return result
