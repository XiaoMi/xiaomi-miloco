"""可观测性 HTTP endpoint。

agent turn 元数据通过 ``observability.agent_meta_poller`` 主动 poll openclaw
``get_trace`` webhook 拉取,不再暴露 ``POST /api/trace/agent``(plugin 端不主动推)。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from miloco.middleware import verify_token
from miloco.observability.metrics_db import connect
from miloco.observability.stats import VIEWS as STATS_VIEWS

router = APIRouter(dependencies=[Depends(verify_token)])


@router.get("/api/trace/{trace_id}")
def get_trace(trace_id: str, request: Request):
    db_path = request.app.state.obs_db_path
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM traces_v WHERE trace_id=?", (trace_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "trace not found")
        cols = [d[0] for d in conn.execute("SELECT * FROM traces_v LIMIT 0").description]
        cycle = dict(zip(cols, row))

        d_rows = conn.execute(
            "SELECT * FROM traces_device WHERE cycle_id=? ORDER BY timestamp", (trace_id,)
        ).fetchall()
        d_cols = [d[0] for d in conn.execute("SELECT * FROM traces_device LIMIT 0").description]
        devices = [dict(zip(d_cols, r)) for r in d_rows]
        return {"cycle": cycle, "devices": devices}
    finally:
        conn.close()


@router.get("/api/traces")
def list_traces(
    request: Request,
    since: int | None = None,
    until: int | None = None,
    has_agent: int | None = None,
    limit: int = 100,
):
    """trace 列表(cycle 级)。agent 成功/失败筛选请走 /api/agent_runs?success=。

    拆 agent_runs 表后,同 trace 可能挂多个 agent_run(rule+interaction+suggestion),
    单一 success 标志已无法准确描述 trace 级状态。
    """
    db_path = request.app.state.obs_db_path
    conn = connect(db_path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(until)
        if has_agent is not None:
            clauses.append("has_agent_turn = ?")
            params.append(has_agent)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM traces_v {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM traces_v LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


@router.get("/api/stats")
def get_stats(
    request: Request,
    metric: str,
    bucket: str = "1h",
    since: int | None = None,
    until: int | None = None,
):
    view_fn = STATS_VIEWS.get(metric)
    if view_fn is None:
        raise HTTPException(400, f"unknown metric: {metric}")
    db_path = request.app.state.obs_db_path
    conn = connect(db_path)
    try:
        return view_fn(conn, bucket, since, until)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/api/agent_runs")
def list_agent_runs(
    request: Request,
    since: int | None = None,
    until: int | None = None,
    source: str | None = None,
    success: int | None = None,
    trace_id: str | None = None,
    limit: int = 100,
):
    db_path = request.app.state.obs_db_path
    conn = connect(db_path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(until)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if success is not None:
            clauses.append("success = ?")
            params.append(success)
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM agent_runs {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM agent_runs LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


@router.get("/api/actions")
def list_actions(
    request: Request,
    since_ms: int | None = None,
    until_ms: int | None = None,
    did: str | None = None,
    action_type: str | None = None,
    home_id: str | None = None,
    failed_only: int | None = None,
    limit: int = 50,
):
    """action_ledger 列表:agent 控制设备 / 播 TTS / 触发场景的持久审计,新到旧排序。

    action_ledger 表落在 observability.db(与本 router 其余 endpoint 同库),故放这里。
    limit 默认 50,上限 500(与 device/actions CLI 的分页节奏对齐,防一次拉全表)。
    """
    limit = max(1, min(limit, 500))
    db_path = request.app.state.obs_db_path
    conn = connect(db_path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if since_ms is not None:
            clauses.append("timestamp >= ?")
            params.append(since_ms)
        if until_ms is not None:
            clauses.append("timestamp <= ?")
            params.append(until_ms)
        if did:
            clauses.append("did = ?")
            params.append(did)
        if action_type:
            clauses.append("action_type = ?")
            params.append(action_type)
        if home_id:
            # NULL 放行:v4 迁移前的老行 / device cache 解析失败的行没有 home 标,
            # 严格等值会让历史台账在任何家的视图里都蒸发——宁可多显示不可丢审计。
            clauses.append("(home_id = ? OR home_id IS NULL)")
            params.append(home_id)
        if failed_only:
            clauses.append("success = 0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM action_ledger {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        cursor = conn.execute(sql, params)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]
    finally:
        conn.close()


@router.get("/api/events")
def list_events(
    request: Request,
    event_type: str | None = None,
    source: str | None = None,
    trace_id: str | None = None,
    since: int | None = None,
    until: int | None = None,
    limit: int = 100,
):
    db_path = request.app.state.obs_db_path
    conn = connect(db_path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM events {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM events LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()
