import gzip
import json
import threading
import time
from datetime import datetime

from .config import miloco_home

BUFFER_MAX = 500
DONE_TTL_S = 120.0
STUCK_TTL_S = 900.0
TURNS_HARD_CAP = 20
DAILY_DUMP_MAX = 300
QUERY_LEN_MAX = 30

_turns: dict[str, dict] = {}
_trace_links: dict[str, str] = {}
_lock = threading.Lock()


def _get_turn(run_id):
    return _turns.get(run_id)


def _get_or_init(run_id):
    state = _turns.get(run_id)
    if state is None:
        state = {
            "buffer": [],
            "query": "",
            "started_at": time.time(),
            "done": None,
            "done_at": None,
        }
        _turns[run_id] = state
    return state


def _now_timestamp():
    return datetime.now().astimezone().isoformat()


def _append_event(state, run_id, hook, payload, **extra):
    buf = state["buffer"]
    if len(buf) < BUFFER_MAX:
        buf.append(
            {
                "ts": _now_timestamp(),
                "hook": hook,
                "run_id": run_id,
                "payload": payload,
                **extra,
            }
        )
    elif len(buf) == BUFFER_MAX:
        buf.append(
            {
                "ts": _now_timestamp(),
                "hook": "_truncated",
                "run_id": run_id,
                "payload": {"dropped_after": BUFFER_MAX},
            }
        )


def _reduce_meta(buffer):
    llm_call_count = 0
    tool_call_count = 0
    llm_total_ms = 0
    tool_total_ms = 0
    tool_max_ms = 0
    slowest_tool_name = None
    error_count = 0
    error_msg = None
    for ev in buffer:
        hook = ev.get("hook")
        payload = ev.get("payload") or {}
        if hook == "llm_output":
            llm_call_count += 1
        if hook == "after_tool_call":
            tool_call_count += 1
            d = payload.get("duration_ms")
            if isinstance(d, (int, float)):
                tool_total_ms += d
                if d > tool_max_ms:
                    tool_max_ms = d
                    slowest_tool_name = payload.get("tool_name")
            if payload.get("error"):
                error_count += 1
                error_msg = str(payload["error"])[:1024]
    return {
        "llm_call_count": llm_call_count,
        "tool_call_count": tool_call_count,
        "llm_total_ms": llm_total_ms,
        "tool_total_ms": tool_total_ms,
        "tool_max_ms": tool_max_ms,
        "slowest_tool_name": slowest_tool_name,
        "error_count": error_count,
        "error_msg": error_msg,
    }


def register_trace_link(run_id, trace_id):
    with _lock:
        _trace_links[run_id] = trace_id
        _get_or_init(run_id)


def pop_trace_link(run_id):
    with _lock:
        return _trace_links.pop(run_id, None)


def record_event(run_id, hook, payload, **extra):
    with _lock:
        state = _get_or_init(run_id)
        _append_event(state, run_id, hook, payload, **extra)


def get_turn_status(run_id):
    with _lock:
        state = _turns.get(run_id)
        if state is None:
            return "unknown"
        return "done" if state.get("done") else "in_progress"


def peek_turn_meta(run_id):
    with _lock:
        state = _turns.get(run_id)
        if state is None:
            return None
        return state.get("done")


def pop_done_turn(run_id):
    with _lock:
        state = _turns.get(run_id)
        if not state or not state.get("done"):
            return None
        meta = state["done"]
        del _turns[run_id]
        return meta


def _gc_expired_turns():
    now = time.time()
    done_cutoff = now - DONE_TTL_S
    stuck_cutoff = now - STUCK_TTL_S
    for run_id in list(_turns.keys()):
        state = _turns[run_id]
        if state.get("done_at") is not None and state["done_at"] < done_cutoff:
            del _turns[run_id]
        elif not state.get("done") and state["started_at"] < stuck_cutoff:
            del _turns[run_id]
    if len(_turns) > TURNS_HARD_CAP:
        sorted_items = sorted(_turns.items(), key=lambda kv: kv[1]["started_at"])
        drop = sorted_items[: len(_turns) - TURNS_HARD_CAP]
        for run_id, _state in drop:
            del _turns[run_id]


def _is_debug_enabled():
    return (miloco_home() / ".debug_observability").exists()


def _today_dir():
    now = datetime.now().astimezone()
    return miloco_home() / "trace" / "agent" / now.strftime("%Y%m%d")


def _sanitize_query_for_filename(query):
    if not query:
        return "system"
    import re

    cleaned = re.sub(r"[\r\n\t]+", " ", query)
    cleaned = re.sub(r'[/\\:*?"<>|`]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:QUERY_LEN_MAX] or "system"


def finalize_turn(run_id, *, success, duration_ms=0, error=None, **extra):
    with _lock:
        state = _turns.get(run_id)
        if not state or state.get("done"):
            return
        trace_id = _trace_links.pop(run_id, None)
        _append_event(
            state,
            run_id,
            "finalize",
            {"success": success, "duration_ms": duration_ms, "error": error},
        )
        meta = _reduce_meta(state["buffer"])
        if not success and error and not meta.get("error_msg"):
            meta["error_count"] += 1
            meta["error_msg"] = str(error)[:1024]
        jsonl_path = None
        if trace_id and _is_debug_enabled():
            jsonl_path = _dump_jsonl(run_id, state, trace_id)
        if not trace_id:
            del _turns[run_id]
            _gc_expired_turns()
            return
        state["done"] = {
            "trace_id": trace_id,
            "run_id": run_id,
            "query": state.get("query", ""),
            "success": success,
            "duration_ms": duration_ms,
            "error": error,
            "jsonl_path": jsonl_path,
            **meta,
            **extra,
        }
        state["done_at"] = time.time()
        _gc_expired_turns()


def _dump_jsonl(run_id, state, trace_id):
    try:
        day_dir = _today_dir()
        day_dir.mkdir(parents=True, exist_ok=True)
        existing = len(list(day_dir.glob("*.jsonl.gz")))
        if existing >= DAILY_DUMP_MAX:
            return None
        filename = "{}__{}.jsonl.gz".format(
            run_id, _sanitize_query_for_filename(state.get("query"))
        )
        full_path = day_dir / filename
        text = "\n".join(json.dumps(e, ensure_ascii=False) for e in state["buffer"])
        full_path.write_bytes(gzip.compress(text.encode("utf-8")))
        return "trace/agent/{}/{}".format(day_dir.name, filename)
    except OSError:
        return None
