"""``/api/stats`` 聚合视图实现。

bucket 粒度:5m / 1h / 1d。SQLite 无 percentile_cont,用 Python 端 sort 实现。
"""
from __future__ import annotations

import statistics
import time
from typing import Any

_BUCKET_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "1h": 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def _bucket_ms(bucket: str) -> int:
    if bucket not in _BUCKET_MS:
        raise ValueError(f"invalid bucket: {bucket}")
    return _BUCKET_MS[bucket]


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def _window(since: int | None, until: int | None) -> tuple[int, int]:
    now_ms = int(time.time() * 1000)
    return (since or now_ms - 86_400_000), (until or now_ms)


def latency_percentiles(conn, bucket, since, until):
    """处理耗时分布。只算成功 cycle (omni_error_count = 0),避免限流/超时拖偏分布。"""
    bms = _bucket_ms(bucket)
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT (timestamp / ?) * ? AS ts, cycle_total_ms FROM traces "
        "WHERE timestamp BETWEEN ? AND ? AND cycle_total_ms IS NOT NULL "
        "AND omni_error_count = 0",
        (bms, bms, s, u),
    ).fetchall()
    groups: dict[int, list[float]] = {}
    for ts, v in rows:
        groups.setdefault(ts, []).append(v)
    return [
        {
            "ts": ts,
            "p50": _percentile(vs, 0.5),
            "p75": _percentile(vs, 0.75),
            "p95": _percentile(vs, 0.95),
            "p99": _percentile(vs, 0.99),
        }
        for ts, vs in sorted(groups.items())
    ]


def rtf_series(conn, bucket, since, until):
    """RTF 时序。返回的 rtf*  字段是"全部 cycle"的平均(原语义);

    rtf_e2e_ok / rtf_omni_ok 是"成功 cycle (omni_error_count = 0)"的平均——前端拿来
    跟对应的全部 cycle 均值对比,差值反映"omni 失败拖累 rtf"的程度。
    """
    bms = _bucket_ms(bucket)
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT (timestamp/?)*? AS ts, AVG(rtf), AVG(rtf_e2e), "
        "AVG(rtf_stream_e2e), AVG(rtf_pipeline), AVG(rtf_omni), "
        "AVG(CASE WHEN omni_error_count = 0 THEN rtf_e2e END) AS rtf_e2e_ok, "
        "AVG(CASE WHEN omni_error_count = 0 THEN rtf_omni END) AS rtf_omni_ok "
        "FROM traces_v WHERE timestamp BETWEEN ? AND ? "
        "GROUP BY ts ORDER BY ts",
        (bms, bms, s, u),
    ).fetchall()
    return [
        {
            "ts": r[0],
            "rtf": r[1],
            "rtf_e2e": r[2],
            "rtf_stream_e2e": r[3],
            "rtf_pipeline": r[4],
            "rtf_omni": r[5],
            "rtf_e2e_ok": r[6],
            "rtf_omni_ok": r[7],
        }
        for r in rows
    ]


def gate_pass_rate(conn, bucket, since, until):
    bms = _bucket_ms(bucket)
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT (timestamp/?)*? AS ts, "
        "AVG(gate_passed)*1.0, AVG(gate_video_pass)*1.0, AVG(gate_audio_pass)*1.0 "
        "FROM traces_v WHERE timestamp BETWEEN ? AND ? "
        "GROUP BY ts ORDER BY ts",
        (bms, bms, s, u),
    ).fetchall()
    return [{"ts": r[0], "overall": r[1], "video": r[2], "audio": r[3]} for r in rows]


def agent_success_rate(conn, bucket, since, until):
    bms = _bucket_ms(bucket)
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT (timestamp/?)*? AS ts, AVG(success)*1.0, COUNT(*) "
        "FROM agent_runs WHERE timestamp BETWEEN ? AND ? "
        "GROUP BY ts ORDER BY ts",
        (bms, bms, s, u),
    ).fetchall()
    return [{"ts": r[0], "success_rate": r[1], "sample_size": r[2]} for r in rows]


def agent_latency_breakdown(conn, bucket, since, until):
    bms = _bucket_ms(bucket)
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT (timestamp/?)*? AS ts, "
        "AVG(duration_ms), AVG(llm_total_ms), AVG(tool_total_ms), "
        "AVG(duration_ms - COALESCE(llm_total_ms,0) - COALESCE(tool_total_ms,0)) "
        "FROM agent_runs WHERE timestamp BETWEEN ? AND ? "
        "GROUP BY ts ORDER BY ts",
        (bms, bms, s, u),
    ).fetchall()
    return [
        {"ts": r[0], "total": r[1], "llm": r[2], "tool": r[3], "framework": r[4]}
        for r in rows
    ]


def slowest_tool_top_n(conn, bucket, since, until):
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT slowest_tool_name, COUNT(*), AVG(tool_max_ms), MAX(tool_max_ms) "
        "FROM agent_runs WHERE tool_call_count > 0 "
        "AND timestamp BETWEEN ? AND ? AND slowest_tool_name IS NOT NULL "
        "GROUP BY slowest_tool_name ORDER BY COUNT(*) DESC LIMIT 10",
        (s, u),
    ).fetchall()
    return [
        {"tool_name": r[0], "count": r[1], "avg_max_ms": r[2], "peak_ms": r[3]}
        for r in rows
    ]


def decode_breakdown(conn, bucket, since, until):
    bms = _bucket_ms(bucket)
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT (timestamp/?)*? AS ts, AVG(decode_video_avg_ms), "
        "AVG(decode_audio_avg_ms), AVG(video_frame_count), AVG(audio_frame_count) "
        "FROM traces_device WHERE (video_frame_count>0 OR audio_frame_count>0) "
        "AND timestamp BETWEEN ? AND ? GROUP BY ts ORDER BY ts",
        (bms, bms, s, u),
    ).fetchall()
    return [
        {
            "ts": r[0],
            "v_avg": r[1],
            "a_avg": r[2],
            "v_frames_per_cycle": r[3],
            "a_frames_per_cycle": r[4],
        }
        for r in rows
    ]


def agent_webhook_health(conn, bucket, since, until):
    """从 agent_runs 表统计 webhook rtt 分布。

    failed_count 已无法从 webhook rtt 直接判定(transport 失败时根本不写 agent_runs),
    保留 0 占位以兼容前端,但语义改为「agent_runs 表里 success=0 的数量」。
    """
    bms = _bucket_ms(bucket)
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT (timestamp/?)*? AS ts, webhook_rtt_ms, success "
        "FROM agent_runs WHERE timestamp BETWEEN ? AND ?",
        (bms, bms, s, u),
    ).fetchall()
    groups: dict[int, dict[str, Any]] = {}
    for ts, rtt, success in rows:
        g = groups.setdefault(ts, {"rtts": [], "failed": 0})
        if rtt is not None:
            g["rtts"].append(rtt)
        if success == 0:
            g["failed"] += 1
    return [
        {
            "ts": ts,
            "avg_rtt": statistics.mean(g["rtts"]) if g["rtts"] else 0.0,
            "p95_rtt": _percentile(g["rtts"], 0.95),
            "failed_count": g["failed"],
        }
        for ts, g in sorted(groups.items())
    ]


def drop_series(conn, bucket, since, until):
    """按 bucket 聚合丢包数 / overflow 触发次数,看绝对量 + 时间分布。

    返回每个 bucket 的:
      dropped — 该时段累计丢的窗口数
      overflow_count — 该时段触发 buffer overflow 的次数
      cycle_count — 该时段处理的 cycle 数(参考用)
    """
    bms = _bucket_ms(bucket)
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT (timestamp/?)*? AS ts, "
        "SUM(dropped_windows_total), SUM(overflow_count_total), COUNT(*) "
        "FROM traces WHERE timestamp BETWEEN ? AND ? "
        "GROUP BY ts ORDER BY ts",
        (bms, bms, s, u),
    ).fetchall()
    return [
        {
            "ts": r[0],
            "dropped": r[1] or 0,
            "overflow_count": r[2] or 0,
            "cycle_count": r[3],
        }
        for r in rows
    ]


def omni_error_series(conn, bucket, since, until):
    """omni 错误按时间桶 + 类型聚合,堆叠柱状图数据源。

    分三类:
      rate_limit — HTTPStatusError:429 (限流)
      timeout    — code 含 "Timeout" (httpx 各种超时类)
      other      — 其他 (5xx / ConnectError / 解析失败等)

    按 cycle 去重(GROUP BY cycle_id):batch 推理失败时所有 device 行都会
    打同一 error_code,这里要的是"cycle 失败次数"而不是"受影响 device 行数",
    否则 N 镜头部署下柱子高度会被虚高 N 倍。

    X 轴用 traces 表所有 cycle 的 bucket 当 anchor,LEFT JOIN 错误聚合,
    保证没错误的 bucket 也返回 0 计数。这样切窗口时 X 轴始终跟 drop/rtf
    等同源 chart 对齐,不会因为"近期 omni 无错误"卡在最后一根错误柱。
    """
    bms = _bucket_ms(bucket)
    s, u = _window(since, until)
    rows = conn.execute(
        "WITH cycle_err AS ("
        "  SELECT cycle_id, MIN(timestamp) AS ts_first, MIN(omni_error_code) AS code "
        "  FROM traces_device "
        "  WHERE timestamp BETWEEN ? AND ? AND omni_error_code IS NOT NULL "
        "  GROUP BY cycle_id "
        "), "
        "err_per_bucket AS ("
        "  SELECT (ts_first/?)*? AS ts, "
        "  SUM(CASE WHEN code = 'HTTPStatusError:429' THEN 1 ELSE 0 END) AS rate_limit, "
        "  SUM(CASE WHEN code LIKE '%Timeout%' THEN 1 ELSE 0 END) AS timeout, "
        "  SUM(CASE WHEN code IS NOT NULL "
        "            AND code != 'HTTPStatusError:429' "
        "            AND code NOT LIKE '%Timeout%' THEN 1 ELSE 0 END) AS other "
        "  FROM cycle_err GROUP BY ts "
        "), "
        "all_buckets AS ("
        "  SELECT DISTINCT (timestamp/?)*? AS ts FROM traces "
        "  WHERE timestamp BETWEEN ? AND ? "
        ") "
        "SELECT b.ts, "
        "COALESCE(e.rate_limit, 0), "
        "COALESCE(e.timeout, 0), "
        "COALESCE(e.other, 0) "
        "FROM all_buckets b "
        "LEFT JOIN err_per_bucket e ON b.ts = e.ts "
        "ORDER BY b.ts",
        (s, u, bms, bms, bms, bms, s, u),
    ).fetchall()
    return [
        {"ts": r[0], "rate_limit": r[1], "timeout": r[2], "other": r[3]}
        for r in rows
    ]


def summary(conn, bucket, since, until):
    """窗口内总览,单点聚合(不分时间桶)。bucket 入参保留但忽略,只为统一签名。

    agent_call_count 从 agent_runs 表来——拆表后语义是"agent 调用次数"
    (含同 trace 多 turn),前端的"窗口内 agent 活跃度"看这个更准。
    """
    s, u = _window(since, until)
    row = conn.execute(
        "SELECT COUNT(*), AVG(skipped), "
        "SUM(dropped_windows_total), "
        "SUM(omni_error_count), SUM(omni_call_count) "
        "FROM traces WHERE timestamp BETWEEN ? AND ?",
        (s, u),
    ).fetchone()
    cycle_count, skip_avg, dropped_sum, omni_err, omni_call = row
    processed_device_window_count = conn.execute(
        "SELECT COUNT(*) FROM traces_device td "
        "JOIN traces t ON t.trace_id = td.cycle_id "
        "WHERE t.timestamp BETWEEN ? AND ?",
        (s, u),
    ).fetchone()[0]
    agent_count = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE timestamp BETWEEN ? AND ?",
        (s, u),
    ).fetchone()[0]
    if not cycle_count:
        return {
            "cycle_count": 0,
            "dropped_count": 0,
            "processed_device_window_count": 0,
            "skip_rate": 0.0,
            "drop_rate": 0.0,
            "omni_error_rate": 0.0,
            "p95_rtf_e2e": 0.0,
            "p95_rtf_omni": 0.0,
            "agent_call_count": 0,
            "window": {"since": s, "until": u},
        }
    dropped_sum = dropped_sum or 0
    drop_denominator = dropped_sum + processed_device_window_count
    drop_rate = dropped_sum / drop_denominator if drop_denominator else 0.0
    omni_call = omni_call or 0
    omni_err = omni_err or 0
    omni_error_rate = (omni_err / omni_call) if omni_call > 0 else 0.0
    rtf_rows = conn.execute(
        "SELECT rtf_e2e, rtf_omni FROM traces_v "
        "WHERE timestamp BETWEEN ? AND ? AND omni_error_count = 0",
        (s, u),
    ).fetchall()
    rtf_e2e_vals = [r[0] for r in rtf_rows if r[0] is not None]
    rtf_omni_vals = [r[1] for r in rtf_rows if r[1] is not None]
    return {
        "cycle_count": cycle_count,
        "dropped_count": dropped_sum,
        "processed_device_window_count": processed_device_window_count,
        "skip_rate": skip_avg or 0.0,
        "drop_rate": drop_rate,
        "omni_error_rate": omni_error_rate,
        "p95_rtf_e2e": _percentile(rtf_e2e_vals, 0.95) if rtf_e2e_vals else 0.0,
        "p95_rtf_omni": _percentile(rtf_omni_vals, 0.95) if rtf_omni_vals else 0.0,
        "agent_call_count": agent_count or 0,
        "window": {"since": s, "until": u},
    }


_STAGE_FIELDS = (
    "decode_ms",
    "collect_ms",
    "convert_ms",
    "gate_ms",
    "identity_ms",
    "omni_ms",
    "log_ms",
)


def stage_percentiles(conn, bucket, since, until):
    """8 阶段耗时 × AVG/P50/P75/P95/P99。bucket 入参保留但忽略。

    每个字段独立过滤 v > 0,避免 skip cycle 的 0 值污染分布
    (identity_ms / omni_ms 在 skip 时会落到 0)。
    另外排除 omni 错误 cycle (omni_error_count > 0)——超时/限流下 omni_ms 不反映
    正常处理耗时,污染各阶段分布。
    """
    s, u = _window(since, until)
    rows = conn.execute(
        f"SELECT {','.join(_STAGE_FIELDS)} FROM traces "
        "WHERE timestamp BETWEEN ? AND ? AND omni_error_count = 0",
        (s, u),
    ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for idx, name in enumerate(_STAGE_FIELDS):
        vals = [r[idx] for r in rows if r[idx] is not None and r[idx] > 0]
        if vals:
            result[name] = {
                "avg": statistics.mean(vals),
                "p50": _percentile(vals, 0.5),
                "p75": _percentile(vals, 0.75),
                "p95": _percentile(vals, 0.95),
                "p99": _percentile(vals, 0.99),
                "sample_size": len(vals),
            }
        else:
            result[name] = {"avg": 0.0, "p50": 0.0, "p75": 0.0, "p95": 0.0, "p99": 0.0, "sample_size": 0}
    return result


def gate_score_percentiles(conn, bucket, since, until):
    """gate 真实打分(visual_change_score / audio_energy_level) × per-device P50/P75/P90/P99。

    bucket 入参保留但忽略——分布按整段窗口聚合,不分桶。
    NULL 行(on-demand bypass / 系统异常 fallback)直接过滤,不进 percentile。
    """
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT device_id, room_name, gate_video_score, gate_audio_energy "
        "FROM traces_device WHERE timestamp BETWEEN ? AND ?",
        (s, u),
    ).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    for did, room, vs, ae in rows:
        g = groups.setdefault(
            did, {"room_name": room, "video": [], "audio": []}
        )
        # 同 device 跨多 room 不预期出现;若出现以首次 room_name 为准,与既有
        # traces_device 查询风格一致。
        if vs is not None:
            g["video"].append(vs)
        if ae is not None:
            g["audio"].append(ae)

    def _pcts(vals: list[float]) -> dict[str, Any]:
        if not vals:
            return {"p50": None, "p75": None, "p90": None, "p99": None, "count": 0}
        return {
            "p50": _percentile(vals, 0.5),
            "p75": _percentile(vals, 0.75),
            "p90": _percentile(vals, 0.9),
            "p99": _percentile(vals, 0.99),
            "count": len(vals),
        }

    return [
        {
            "device_id": did,
            "room_name": g["room_name"],
            "video": _pcts(g["video"]),
            "audio": _pcts(g["audio"]),
        }
        for did, g in sorted(groups.items())
    ]


def error_top_n(conn, bucket, since, until):
    s, u = _window(since, until)
    rows = conn.execute(
        "SELECT error_msg FROM agent_runs "
        "WHERE error_msg IS NOT NULL AND timestamp BETWEEN ? AND ?",
        (s, u),
    ).fetchall()
    counts: dict[str, int] = {}
    for (msg,) in rows:
        prefix = msg.split(":")[0][:64]
        counts[prefix] = counts.get(prefix, 0) + 1
    return [
        {"error_prefix": p, "count": c}
        for p, c in sorted(counts.items(), key=lambda x: -x[1])[:10]
    ]


VIEWS = {
    "latency_percentiles": latency_percentiles,
    "rtf_series": rtf_series,
    "gate_pass_rate": gate_pass_rate,
    "agent_success_rate": agent_success_rate,
    "agent_latency_breakdown": agent_latency_breakdown,
    "slowest_tool_top_n": slowest_tool_top_n,
    "decode_breakdown": decode_breakdown,
    "agent_webhook_health": agent_webhook_health,
    "error_top_n": error_top_n,
    "summary": summary,
    "stage_percentiles": stage_percentiles,
    "drop_series": drop_series,
    "omni_error_series": omni_error_series,
    "gate_score_percentiles": gate_score_percentiles,
}
