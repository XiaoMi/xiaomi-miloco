from __future__ import annotations

import os
import time
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from miloco.middleware import verify_token
from miloco.node_monitor.monitor import get_monitor
from miloco.node_monitor.resource_monitor import ResourceMonitor

router = APIRouter(prefix="/monitor", tags=["Monitor"])

_resource_monitor: ResourceMonitor | None = None
_start_time: float | None = None


def set_resource_monitor(rm: ResourceMonitor, start_time: float) -> None:
    global _resource_monitor, _start_time
    _resource_monitor = rm
    _start_time = start_time


def _get_uname() -> str | None:
    try:
        u = os.uname()
    except (AttributeError, OSError):
        return None
    return f"{u.sysname} {u.nodename} {u.release} {u.version} {u.machine}"


@router.get("/", dependencies=[Depends(verify_token)])
async def get_monitor_meta():
    """Process-level meta: node_count / uptime_s / uname / resources."""
    mon = get_monitor()
    data: dict = {
        "node_count": len(mon.iter_states()),
    }
    if _start_time is not None:
        data["uptime_s"] = round(time.monotonic() - _start_time, 1)
    uname = _get_uname()
    if uname is not None:
        data["uname"] = uname
    if _resource_monitor is not None:
        data["resources"] = _resource_monitor.get_data()
    return data


@router.get("/resources", dependencies=[Depends(verify_token)])
async def get_resources():
    """System resource snapshot: CPU / memory / disk / etc."""
    if _resource_monitor is None:
        return JSONResponse(
            status_code=503, content={"error": "resource monitor not initialized"}
        )
    return _resource_monitor.get_data()


@router.get("/nodes", dependencies=[Depends(verify_token)])
async def get_all_nodes():
    """List business nodes."""
    mon = get_monitor()
    return {"ts": time.time(), "nodes": mon.snapshot()}


@router.get("/nodes/{name}", dependencies=[Depends(verify_token)])
async def get_node(name: str):
    """Get a single node snapshot by name."""
    mon = get_monitor()
    result = mon.snapshot_one(name)
    if result is None:
        return JSONResponse(
            status_code=404, content={"error": f"node '{name}' not found"}
        )
    return result


WindowKey = Literal["1h", "6h", "24h", "3d"]
BucketKey = Literal["1m", "5m", "1h"]
_WINDOW_SECONDS: dict[str, int] = {
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "3d": 259200,
}
_BUCKET_SECONDS: dict[str, int] = {"1m": 60, "5m": 300, "1h": 3600}


@router.get("/memory", dependencies=[Depends(verify_token)])
async def get_memory_snapshot():
    """最新一次 smaps + py_heap 完整快照。"""
    if _resource_monitor is None:
        return JSONResponse(
            status_code=503,
            content={"error": "resource monitor not initialized"},
        )
    snap = _resource_monitor.get_memory_latest()
    if snap is None:
        if not _resource_monitor.is_memory_available():
            return JSONResponse(
                status_code=503,
                content={"error": "memory monitor not available on this platform"},
            )
        return JSONResponse(
            status_code=503,
            content={"error": "memory snapshot not yet collected"},
        )
    return snap


@router.get("/memory/series", dependencies=[Depends(verify_token)])
async def get_memory_series(
    window: WindowKey = Query("1h"),
    bucket: BucketKey = Query("1m"),
):
    """3d 内 RSS/Py 时序，按 bucket 平均聚合。"""
    if _resource_monitor is None:
        return JSONResponse(
            status_code=503,
            content={"error": "resource monitor not initialized"},
        )
    return _resource_monitor.get_memory_series(
        window_seconds=_WINDOW_SECONDS[window],
        bucket_seconds=_BUCKET_SECONDS[bucket],
    )


@router.get("/cpu/series", dependencies=[Depends(verify_token)])
async def get_cpu_series(
    window: WindowKey = Query("1h"),
    bucket: BucketKey = Query("1m"),
):
    """3d 内进程 CPU 占用时序，按 bucket 平均聚合。"""
    if _resource_monitor is None:
        return JSONResponse(
            status_code=503,
            content={"error": "resource monitor not initialized"},
        )
    return _resource_monitor.get_cpu_series(
        window_seconds=_WINDOW_SECONDS[window],
        bucket_seconds=_BUCKET_SECONDS[bucket],
    )
