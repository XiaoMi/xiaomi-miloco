"""adapter 侧：读 plugin 写的 trace meta 文件，喂给 backend 的 get_trace 轮询。

plugin 端（``trace.py``）在 ``on_session_end`` 时把 turn meta 写成
``$MILOCO_HOME/trace/agent/<YYYYMMDD>/<runId>__<query>.meta.json``（Phase 2.1 引入）。
adapter 是独立进程，通过读盘拿到 meta（IPC over filesystem）。

backend ``dispatch/dispatcher.py`` 的 ``get_trace`` 轮询期望 ``{status, ...meta}`` 形状。
找不到对应 runId → fallback ``{status:"done"}``（向后兼容，老 adapter 行为）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _trace_root() -> Path:
    """``$MILOCO_HOME/trace/agent``（对齐 plugin 端 trace.py::_today_dir）。"""
    home = os.environ.get("MILOCO_HOME", "")
    base = Path(home) if home else Path.home() / ".openclaw" / "miloco"
    return base / "trace" / "agent"


def _today_yyyymmdd() -> str:
    return time.strftime("%Y%m%d")


def _read_meta_file(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[adapter-trace] meta read failed %s: %s", path, exc)
        return None


def get_done_meta(run_id: Optional[str] = None) -> Optional[dict]:
    """读最新或指定 runId 的 done meta。返回 dict 或 None。

    - ``run_id=None`` → 返最近 done 的（按文件 mtime 倒序），与 plugin 端
      ``pop_done_turn(None)`` 等价。
    - ``run_id=具体值`` → 返第一个 runId 匹配的 meta（glob 找）。
    """
    today_dir = _trace_root() / _today_yyyymmdd()
    if not today_dir.is_dir():
        return None
    try:
        metas = list(today_dir.glob("*.meta.json"))
    except OSError as exc:
        logger.warning("[adapter-trace] glob failed %s: %s", today_dir, exc)
        return None
    if not metas:
        return None
    if run_id:
        for p in metas:
            meta = _read_meta_file(p)
            if meta and meta.get("runId") == run_id:
                return meta
        return None
    # 最新一个：按 mtime 倒序
    metas.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in metas:
        meta = _read_meta_file(p)
        if meta is not None:
            return meta
    return None


def get_trace_response(run_id: Optional[str] = None) -> dict:
    """``action:get_trace`` 的响应——回真实 meta，找不到时降级 done。

    backend ``dispatch/dispatcher.py`` 期望 ``{status, ...meta}`` 形状：
    - done + llmCallCount/toolCallCount/durationMs/...（对齐 OpenClaw 行为）
    - 降级：``{status:"done"}``（老 adapter 行为）
    """
    meta = get_done_meta(run_id)
    if not meta:
        return {"status": "done"}
    return {
        "status": "done" if meta.get("success", True) else "error",
        "runId": meta.get("runId"),
        "traceId": meta.get("traceId"),
        "query": meta.get("query", ""),
        "durationMs": meta.get("durationMs", 0),
        "llmCallCount": meta.get("llmCallCount", 0),
        "toolCallCount": meta.get("toolCallCount", 0),
        "llmTotalMs": meta.get("llmTotalMs", 0),
        "toolTotalMs": meta.get("toolTotalMs", 0),
        "toolMaxMs": meta.get("toolMaxMs", 0),
        "slowestToolName": meta.get("slowestToolName"),
        "errorCount": meta.get("errorCount", 0),
        "errorMsg": meta.get("errorMsg"),
        "jsonlPath": meta.get("jsonlPath"),
    }