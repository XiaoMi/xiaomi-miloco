"""Hermes turn trace —— 对齐 OpenClaw ``plugins/openclaw/src/hooks/trace.ts``。

OpenClaw 7 个 trace 事件 → Hermes 6 个 hook（一一对应 / 合并）：
- ``llm_input``           → ``pre_llm_call``
- ``llm_output``          → ``post_llm_call``
- ``before_tool_call``    → ``pre_tool_call``
- ``after_tool_call``     → ``post_tool_call``
- ``model_call_ended``    → 合并到 ``post_llm_call`` 的 durationMs
- agent turn end          → ``on_session_end``
- session start           → ``on_session_start``

Hermes 没有 ``runId`` 概念——直接用 ``session_id`` 作为 turn id。traceId 用
``miloco:<sessionKey>:<lane>`` 前缀的 session_id 推导（context_injection 时识别）。

落盘：``$MILOCO_HOME/trace/agent/<YYYYMMDD>/<runId>__<query>.jsonl.gz`` + 同名
``.meta.json``（adapter 的 read_trace_meta 读这个）。

每日 cap 300（超出 warn 跳过，防撑爆磁盘）—— 与 openclaw 完全一致。
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import miloco_home

logger = logging.getLogger(__name__)

# ── 常量（与 openclaw trace.ts 对齐） ─────────────────────────────────────
BUFFER_MAX = 5000  # 单 turn buffer 上限（events 数）；超出记 _truncated
DAILY_DUMP_MAX = 300  # 每日 jsonl.gz 文件数 cap
QUERY_LEN_MAX = 80  # 文件名里 query 部分最大长度
TRACE_DONE_TTL_S = 3600.0  # done turns 在内存保留 1h（adapter get_trace 轮询窗口）


# ── 状态 ────────────────────────────────────────────────────────────────

class _TurnState:
    __slots__ = ("buffer", "started_at", "query", "done", "done_at")

    def __init__(self, started_at: int) -> None:
        self.buffer: List[Dict[str, Any]] = []
        self.started_at = started_at
        self.query: str = ""
        self.done: Optional[Dict[str, Any]] = None
        self.done_at: int = 0


# 进程内 registry：run_id → _TurnState（与 openclaw turns Map 等价）
_turns: Dict[str, _TurnState] = {}
# run_id → trace_id（webhook adapter 启动 turn 时注入；openclaw traceLinks 等价）
_trace_links: Dict[str, str] = {}
_lock = threading.Lock()


# ── 工具 ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _today_dir() -> Path:
    """``$MILOCO_HOME/trace/agent/YYYYMMDD``。"""
    p = miloco_home() / "trace" / "agent" / datetime.now().strftime("%Y%m%d")
    return p


def _extract_user_query(prompt: Optional[str]) -> str:
    """从 prompt 提取"用户实际消息"部分，去掉 OpenClaw 风格的星期前缀。"""
    if not prompt:
        return ""
    # 去掉 [Mon Jun 18 14:32:11 2026] 之类的 date prefix
    cleaned = re.sub(r"\[\w{3}\s[^\]]*\]\s*", "", prompt)
    return cleaned.strip()


def _sanitize_filename(q: Optional[str]) -> str:
    """与 openclaw sanitizeQueryForFilename 等价。"""
    if not q:
        return "system"
    s = re.sub(r"[\r\n\t]+", " ", q)
    s = re.sub(r"[/\\:*?\"<>|`]", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s[:QUERY_LEN_MAX]
    return s or "system"


def _gc_expired_turns() -> None:
    """清理 done 超 1h 的 turn（释放内存）。"""
    cutoff = _now_ms() - int(TRACE_DONE_TTL_S * 1000)
    expired = [k for k, v in _turns.items() if v.done and v.done_at < cutoff]
    for k in expired:
        _turns.pop(k, None)


# ── 公共 API ────────────────────────────────────────────────────────────

def is_debug_enabled() -> bool:
    """debug 开关：``$MILOCO_HOME/config.json::trace.debug`` 或环境变量。"""
    import os
    if os.environ.get("MILOCO_TRACE_DEBUG", "").lower() in ("1", "true", "yes"):
        return True
    try:
        cfg_path = miloco_home() / "config.json"
        if cfg_path.is_file():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            return bool((cfg.get("trace") or {}).get("debug", False))
    except (OSError, json.JSONDecodeError):
        pass
    return False


def register_trace_link(run_id: str, trace_id: str) -> None:
    """外部注入 run_id ↔ trace_id 映射（adapter 启动 turn 时调用）。"""
    with _lock:
        _trace_links[run_id] = trace_id
        # 同时 init turn entry，避免 backend 第一次 poll 在 turn end 之前到达时找不到
        _get_or_init(run_id)


def pop_trace_link(run_id: str) -> Optional[str]:
    with _lock:
        v = _trace_links.pop(run_id, None)
        return v


def pop_done_turn(run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """adapter 的 get_trace 调：取 done meta（原子，poll 完即清）。

    ``run_id=None`` 时返回最新一个 done turn 的 meta（兜底，对应 backend 用
    ``get_trace`` 不带 runId 的轮询）。
    """
    with _lock:
        if run_id is None:
            done = [(k, v.done, v.done_at) for k, v in _turns.items() if v.done]
            if not done:
                return None
            done.sort(key=lambda x: x[2], reverse=True)
            run_id, meta, _ = done[0]
        else:
            state = _turns.get(run_id)
            meta = state.done if state else None
        if meta is None:
            return None
        # pop（与 openclaw popDoneTurn 等价——meta 给 backend 后即清，避免重复消费）
        _turns.pop(run_id, None)
        return meta


def _get_or_init(run_id: str) -> _TurnState:
    state = _turns.get(run_id)
    if state is None:
        state = _TurnState(_now_ms())
        _turns[run_id] = state
    return state


def _push_event(state: _TurnState, ev: Dict[str, Any]) -> None:
    if len(state.buffer) < BUFFER_MAX:
        state.buffer.append(ev)
    elif len(state.buffer) == BUFFER_MAX:
        state.buffer.append({"ts": _now_iso(), "hook": "_truncated", "runId": ev.get("runId"), "payload": {"droppedAfter": BUFFER_MAX}})


def _record(run_id: str, hook_name: str, payload: Dict[str, Any]) -> None:
    """hook handler 调：append 一条 event 到 buffer。"""
    with _lock:
        state = _get_or_init(run_id)
        _push_event(state, {
            "ts": _now_iso(),
            "hook": hook_name,
            "runId": run_id,
            "traceId": _trace_links.get(run_id),
            "payload": payload,
        })


def _reduce_meta(buffer: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 buffer 聚合 meta（对齐 openclaw reduceMeta）。"""
    llm_call_count = 0
    tool_call_count = 0
    llm_total_ms = 0
    tool_total_ms = 0
    tool_max_ms = 0
    slowest_tool_name: Optional[str] = None
    error_count = 0
    error_msg: Optional[str] = None
    for ev in buffer:
        hook = ev.get("hook", "")
        pl = ev.get("payload") or {}
        if hook == "post_llm_call":
            llm_call_count += 1
            # 落盘字段名是 camelCase durationMs（保持和 OpenClaw trace.ts schema 一致）
            d = pl.get("durationMs")
            if isinstance(d, (int, float)):
                llm_total_ms += int(d)
        if hook == "post_tool_call":
            tool_call_count += 1
            d = pl.get("durationMs")
            if isinstance(d, (int, float)):
                tool_total_ms += int(d)
                if d > tool_max_ms:
                    tool_max_ms = int(d)
                    slowest_tool_name = pl.get("toolName")
            if pl.get("error"):
                error_count += 1
                if error_msg is None:
                    error_msg = str(pl.get("error"))[:1024]
    return {
        "llmCallCount": llm_call_count,
        "toolCallCount": tool_call_count,
        "llmTotalMs": llm_total_ms,
        "toolTotalMs": tool_total_ms,
        "toolMaxMs": tool_max_ms,
        "slowestToolName": slowest_tool_name,
        "errorCount": error_count,
        "errorMsg": error_msg,
    }


def _flush_to_disk(run_id: str, state: _TurnState, final_success: bool) -> Optional[str]:
    """落盘：``trace/agent/<YYYYMMDD>/<runId>__<query>.jsonl.gz`` + 同名 ``.meta.json``。

    返回 jsonl 相对路径（写进 meta 让 backend / 调试看到）；超出每日 cap 时 warn 跳过。
    """
    try:
        dir_path = _today_dir()
        dir_path.mkdir(parents=True, exist_ok=True)
        existing = [p for p in dir_path.iterdir() if p.suffix == ".gz"]
        if len(existing) >= DAILY_DUMP_MAX:
            logger.warning(
                "[miloco-trace] daily cap reached: %d/%d, skip dump runId=%s",
                len(existing), DAILY_DUMP_MAX, run_id,
            )
            return None
        filename = f"{run_id}__{_sanitize_filename(state.query)}.jsonl.gz"
        full_path = dir_path / filename
        text = "\n".join(json.dumps(e, ensure_ascii=False) for e in state.buffer) + "\n"
        with gzip.open(full_path, "wt", encoding="utf-8") as f:
            f.write(text)
        # meta 文件（adapter get_trace 读这个）——小 JSON 包含聚合统计 + 路径
        meta = _reduce_meta(state.buffer)
        meta["runId"] = run_id
        meta["traceId"] = _trace_links.get(run_id)
        meta["query"] = state.query
        meta["success"] = final_success
        meta["jsonlPath"] = f"trace/agent/{dir_path.name}/{filename}"
        meta["startedAt"] = state.started_at
        meta["doneAt"] = _now_ms()
        meta_path = dir_path / f"{run_id}__{_sanitize_filename(state.query)}.meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta["jsonlPath"]
    except OSError as exc:
        logger.warning("[miloco-trace] flush failed runId=%s: %s", run_id, exc)
        return None


# ── hook handlers ───────────────────────────────────────────────────────

def _run_id_from_args(*, session_id: str | None = None, task_id: str | None = None, **_: Any) -> str:
    """决定 runId：task_id 优先（Hermes 子任务），否则 session_id。"""
    return (task_id or session_id or "unknown")


def _hk_pre_llm_call(session_id, user_message, conversation_history, is_first_turn, model, platform, **kwargs):
    """对齐 OpenClaw llm_input hook——记录 turn 起点 + user query。"""
    run_id = _run_id_from_args(session_id=session_id, task_id=kwargs.get("task_id"))
    query = _extract_user_query(user_message)
    with _lock:
        state = _get_or_init(run_id)
        if not state.query:
            state.query = query[:QUERY_LEN_MAX]
    _record(run_id, "pre_llm_call", {
        "model": model,
        "platform": platform,
        "isFirstTurn": bool(is_first_turn),
        "messageCount": len(conversation_history or []),
        "queryLen": len(query),
    })


def _hk_post_llm_call(session_id, user_message, assistant_response, conversation_history, model, platform, **kwargs):
    """对齐 llm_output + model_call_ended——同一 hook 记完整体 turn。"""
    run_id = _run_id_from_args(session_id=session_id, task_id=kwargs.get("task_id"))
    # Hermes hook emit 用 snake_case duration_ms（不是 camelCase durationMs）
    _record(run_id, "post_llm_call", {
        "model": model,
        "platform": platform,
        "assistantLen": len(assistant_response or ""),
        "durationMs": kwargs.get("duration_ms", 0),  # 落盘字段名保留 camelCase 兼容老 reader
    })


def _hk_pre_tool_call(tool_name, args, task_id, **kwargs):
    run_id = _run_id_from_args(task_id=task_id)
    _record(run_id, "pre_tool_call", {
        "toolName": tool_name,
        "argsKeys": sorted((args or {}).keys()) if isinstance(args, dict) else [],
    })


def _hk_post_tool_call(tool_name, args, result, task_id, **kwargs):
    run_id = _run_id_from_args(task_id=task_id)
    # 尝试解析 result 里的 error 字段
    error: Optional[str] = None
    try:
        if isinstance(result, str) and result.strip():
            parsed = json.loads(result)
            if isinstance(parsed, dict) and parsed.get("error"):
                error = str(parsed["error"])[:512]
    except (json.JSONDecodeError, TypeError):
        pass
    _record(run_id, "post_tool_call", {
        "toolName": tool_name,
        "resultLen": len(result or ""),
        "durationMs": kwargs.get("duration_ms", 0),  # Hermes 用 snake_case emit
        "error": error,
    })


def _hk_on_session_start(session_id, model, platform, **kwargs):
    run_id = _run_id_from_args(session_id=session_id, task_id=kwargs.get("task_id"))
    with _lock:
        # 新 session 重置 buffer（与 openclaw 一致——session 切换就清旧）
        _turns.pop(run_id, None)
        _get_or_init(run_id)
    _record(run_id, "on_session_start", {"model": model, "platform": platform})


def _hk_on_session_end(session_id, completed, interrupted, model, platform, **kwargs):
    """对齐 OpenClaw agent_end——finalize turn，写盘 + 留 meta 给 backend 拉。"""
    run_id = _run_id_from_args(session_id=session_id, task_id=kwargs.get("task_id"))
    with _lock:
        state = _turns.get(run_id)
        if not state or state.done:
            return
        # record end event
        _push_event(state, {
            "ts": _now_iso(),
            "hook": "on_session_end",
            "runId": run_id,
            "traceId": _trace_links.get(run_id),
            "payload": {
                "completed": bool(completed),
                "interrupted": bool(interrupted),
                "model": model,
                "platform": platform,
                "durationMs": _now_ms() - state.started_at,
            },
        })
        # traceId 为空 = 非 miloco 触发的 turn（普通 chat）→ GC，不落盘
        if not _trace_links.get(run_id):
            _turns.pop(run_id, None)
            _gc_expired_turns()
            return
        # 落盘
        jsonl_path = _flush_to_disk(run_id, state, bool(completed))
        meta = _reduce_meta(state.buffer)
        meta["runId"] = run_id
        meta["traceId"] = _trace_links.get(run_id)
        meta["query"] = state.query
        meta["durationMs"] = _now_ms() - state.started_at
        meta["success"] = bool(completed)
        meta["jsonlPath"] = jsonl_path
        state.done = meta
        state.done_at = _now_ms()
        _gc_expired_turns()
    # 清掉 traceLink（与 openclaw popTraceLink 等价）
    pop_trace_link(run_id)


# ── 注册 ────────────────────────────────────────────────────────────────

HOOK_REGISTRATIONS: List[tuple] = [
    ("pre_llm_call", _hk_pre_llm_call),
    ("post_llm_call", _hk_post_llm_call),
    ("pre_tool_call", _hk_pre_tool_call),
    ("post_tool_call", _hk_post_tool_call),
    ("on_session_start", _hk_on_session_start),
    ("on_session_end", _hk_on_session_end),
]


def register_trace_hooks(ctx) -> int:
    """在 ``register(ctx)`` 里调用：注册全部 trace hook。返回成功数。"""
    n = 0
    for event, handler in HOOK_REGISTRATIONS:
        try:
            ctx.register_hook(event, handler)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("[miloco-trace] register %s failed: %s", event, exc)
    return n