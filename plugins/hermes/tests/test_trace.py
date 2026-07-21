"""trace.py 单测：buffer 累积、reduce_meta、debug 落盘、daily cap、GC。"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest
from miloco_plugin_pkg import trace as tr


@pytest.fixture(autouse=True)
def _clean_state(tmp_path: Path, monkeypatch):
    """每个测试都用独立 miloco_home + 清空 _turns/_trace_links。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_TRACE_DEBUG", raising=False)
    with tr._lock:
        tr._turns.clear()
        tr._trace_links.clear()
    yield
    with tr._lock:
        tr._turns.clear()
        tr._trace_links.clear()


# ── 注册 ──────────────────────────────────────────────────────────────────

def test_register_trace_hooks_returns_count():
    """register_trace_hooks 用 mock ctx 返回成功数。"""
    class _MockCtx:
        def __init__(self):
            self.calls = []
        def register_hook(self, name, fn):
            self.calls.append((name, fn))

    ctx = _MockCtx()
    n = tr.register_trace_hooks(ctx)
    assert n == 6
    assert len(ctx.calls) == 6
    names = [c[0] for c in ctx.calls]
    assert "pre_llm_call" in names
    assert "post_llm_call" in names
    assert "pre_tool_call" in names
    assert "post_tool_call" in names
    assert "on_session_start" in names
    assert "on_session_end" in names


def test_register_trace_hooks_partial_failure():
    """单个 register 失败不影响其他。"""
    class _MockCtx:
        def __init__(self):
            self.fail = {"pre_tool_call"}
        def register_hook(self, name, fn):
            if name in self.fail:
                raise RuntimeError("simulated")
    ctx = _MockCtx()
    n = tr.register_trace_hooks(ctx)
    assert n == 5  # 5 succeeded


# ── run_id 推导 ───────────────────────────────────────────────────────────

def test_run_id_prefers_task_id():
    rid = tr._run_id_from_args(session_id="sess-abc", task_id="task-xyz")
    assert rid == "task-xyz"


def test_run_id_falls_back_to_session_id():
    rid = tr._run_id_from_args(session_id="sess-abc")
    assert rid == "sess-abc"


def test_run_id_unknown_when_both_missing():
    rid = tr._run_id_from_args()
    assert rid == "unknown"


# ── user query 提取 ───────────────────────────────────────────────────────

def test_extract_user_query_strips_date_prefix():
    raw = "[Mon Jun 18 14:32:11 2026] 你好世界"
    assert tr._extract_user_query(raw) == "你好世界"


def test_extract_user_query_keeps_plain():
    assert tr._extract_user_query("hello world") == "hello world"


def test_extract_user_query_empty():
    assert tr._extract_user_query("") == ""
    assert tr._extract_user_query(None) == ""


def test_sanitize_filename_safe_chars():
    s = tr._sanitize_filename('hello/world\\name:with*chars?')
    assert "/" not in s and "\\" not in s and ":" not in s and "*" not in s and "?" not in s


def test_sanitize_filename_truncates():
    s = tr._sanitize_filename("x" * 500)
    assert len(s) <= tr.QUERY_LEN_MAX


def test_sanitize_filename_empty_fallback():
    assert tr._sanitize_filename("") == "system"
    assert tr._sanitize_filename(None) == "system"


# ── record + reduce ───────────────────────────────────────────────────────

def test_pre_llm_call_records_event_and_query():
    tr._hk_pre_llm_call("sess-1", "[Mon Jun 18 14:32:11 2026] 你好", [], True, "claude-sonnet", "test")
    state = tr._turns["sess-1"]
    assert state.query == "你好"
    assert len(state.buffer) == 1
    assert state.buffer[0]["hook"] == "pre_llm_call"


def test_post_tool_call_extracts_error():
    """post_tool_call 能从 result JSON 提 error 字段。"""
    tr._hk_post_tool_call("sess-1", {"x": 1}, json.dumps({"error": "boom"}), "sess-1")
    state = tr._turns["sess-1"]
    assert state.buffer[-1]["payload"]["error"] == "boom"


def test_post_tool_call_no_error():
    tr._hk_post_tool_call("sess-1", {}, json.dumps({"ok": True}), "sess-1")
    state = tr._turns["sess-1"]
    assert state.buffer[-1]["payload"].get("error") is None


def test_reduce_meta_counts_llm_and_tools():
    """reduce_meta 聚合 llm_call_count / tool_call_count / 错误 / 最慢 tool。"""
    # pre_llm_call + post_llm_call x 2 + pre/post_tool_call x 2
    tr._hk_pre_llm_call("sess-1", "hi", [], True, "m", "p")
    tr._hk_post_llm_call("sess-1", "hi", "ans", [], "m", "p", duration_ms=1000)
    tr._hk_post_llm_call("sess-1", "hi2", "ans2", [], "m", "p", duration_ms=2000)
    tr._hk_pre_tool_call("miloco_im_push", {"m": "x"}, "sess-1")
    tr._hk_post_tool_call("miloco_im_push", {"m": "x"}, "ok", "sess-1", duration_ms=300)
    tr._hk_pre_tool_call("bad_tool", {}, "sess-1")
    tr._hk_post_tool_call("bad_tool", {}, json.dumps({"error": "fail"}), "sess-1", duration_ms=500)

    state = tr._turns["sess-1"]
    meta = tr._reduce_meta(state.buffer)
    assert meta["llm_call_count"] == 2
    assert meta["tool_call_count"] == 2
    assert meta["llm_total_ms"] == 3000
    assert meta["tool_total_ms"] == 800
    assert meta["tool_max_ms"] == 500
    assert meta["slowest_tool_name"] == "bad_tool"
    assert meta["error_count"] == 1
    assert "fail" in (meta["error_msg"] or "")


# ── traceLink ─────────────────────────────────────────────────────────────

def test_register_and_pop_trace_link():
    tr.register_trace_link("sess-1", "trace-abc")
    assert "sess-1" in tr._trace_links
    assert "sess-1" in tr._turns  # 同时 init turn entry
    v = tr.pop_trace_link("sess-1")
    assert v == "trace-abc"
    assert "sess-1" not in tr._trace_links


def test_pop_trace_link_missing_returns_none():
    assert tr.pop_trace_link("nonexistent") is None


# ── on_session_end finalize ───────────────────────────────────────────────

MILOCO_SESSION = "miloco:agent:main:miloco-suggest:miloco-suggest"


def test_session_end_without_trace_id_drops():
    """非 miloco: 前缀的 session → 直接 GC，不留 meta、不落盘。"""
    tr._hk_pre_llm_call("sess-1", "hi", [], True, "m", "p")
    tr._hk_on_session_end("sess-1", True, False, "m", "p")
    assert "sess-1" not in tr._turns


def test_session_end_with_trace_id_keeps_done_meta():
    """miloco: 前缀的 session → finalize，留 done meta 给 backend 拉。"""
    tr.register_trace_link(MILOCO_SESSION, "trace-abc")
    tr._hk_pre_llm_call(MILOCO_SESSION, "hi", [], True, "m", "p")
    tr._hk_post_llm_call(MILOCO_SESSION, "hi", "ans", [], "m", "p", duration_ms=500)
    tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")
    state = tr._turns[MILOCO_SESSION]
    assert state.done is not None
    assert state.done["trace_id"] == "trace-abc"
    assert state.done["success"] is True
    assert state.done["llm_call_count"] == 1
    assert MILOCO_SESSION not in tr._trace_links


def test_session_end_idempotent():
    """同一 session end 调两次，第二次是 no-op。"""
    tr.register_trace_link(MILOCO_SESSION, "trace-abc")
    tr._hk_pre_llm_call(MILOCO_SESSION, "hi", [], True, "m", "p")
    tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")
    tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")
    state = tr._turns[MILOCO_SESSION]
    end_events = [e for e in state.buffer if e["hook"] == "on_session_end"]
    assert len(end_events) == 1


# ── pop_done_turn（adapter get_trace 用） ─────────────────────────────────

def test_pop_done_turn_specific_run_id():
    tr.register_trace_link(MILOCO_SESSION, "trace-abc")
    tr._hk_pre_llm_call(MILOCO_SESSION, "hi", [], True, "m", "p")
    tr._hk_on_session_end(MILOCO_SESSION, True, False, "m", "p")
    meta = tr.pop_done_turn(MILOCO_SESSION)
    assert meta is not None
    assert meta["trace_id"] == "trace-abc"
    assert tr.pop_done_turn(MILOCO_SESSION) is None


def test_pop_done_turn_latest():
    """run_id=None 返最新一个 done turn。"""
    sids = ["miloco:sess-1", "miloco:sess-2", "miloco:sess-3"]
    for sid in sids:
        tr.register_trace_link(sid, f"trace-{sid}")
        tr._hk_pre_llm_call(sid, "hi", [], True, "m", "p")
        tr._hk_on_session_end(sid, True, False, "m", "p")
    meta = tr.pop_done_turn(None)
    assert meta is not None
    assert meta["run_id"] in sids


def test_pop_done_turn_empty():
    assert tr.pop_done_turn("nonexistent") is None
    assert tr.pop_done_turn(None) is None


# ── debug 落盘 ────────────────────────────────────────────────────────────

def test_flush_writes_even_without_debug():
    """debug 默认关时也常写 trace（已去 debug 门槛，对齐 hermes-pr.md §五 #11）。"""
    sess = "miloco:test-flush"
    tr.register_trace_link(sess, "trace-abc")
    tr._hk_pre_llm_call(sess, "hi", [], True, "m", "p")
    tr._hk_on_session_end(sess, True, False, "m", "p")
    state = tr._turns[sess]
    # 去 debug 门槛后常写，jsonl_path 不应为 None
    assert state.done["jsonl_path"] is not None
    # meta.json 应出现
    today = Path(os.environ["MILOCO_HOME"]) / "trace" / "agent"
    assert any(today.rglob("*.meta.json"))


def test_flush_enabled_writes_jsonl_and_meta(monkeypatch):
    monkeypatch.setenv("MILOCO_TRACE_DEBUG", "1")
    sess = "miloco:test-flush-enabled"
    tr.register_trace_link(sess, "trace-abc")
    tr._hk_pre_llm_call(sess, "[Mon Jun 18 14:32:11 2026] 你好", [], True, "m", "p")
    tr._hk_post_tool_call("miloco_im_push", {}, "ok", sess, duration_ms=42)
    tr._hk_on_session_end(sess, True, False, "m", "p")

    state = tr._turns[sess]
    assert state.done["jsonl_path"] is not None

    today = Path(os.environ["MILOCO_HOME"]) / "trace" / "agent"
    jsonl_files = list(today.rglob("*.jsonl.gz"))
    meta_files = list(today.rglob("*.meta.json"))
    assert len(jsonl_files) == 1
    assert len(meta_files) == 1

    # jsonl 能解开
    with gzip.open(jsonl_files[0], "rt", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert any(e["hook"] == "pre_llm_call" for e in lines)
    assert any(e["hook"] == "post_tool_call" for e in lines)
    assert any(e["hook"] == "on_session_end" for e in lines)

    # meta 内容齐
    meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert meta["trace_id"] == "trace-abc"
    assert meta["tool_call_count"] == 1
    assert meta["slowest_tool_name"] == "miloco_im_push"
    assert meta["jsonl_path"].endswith(".jsonl.gz")


def test_daily_cap_skips_dump(monkeypatch):
    """cap = 300，超出 warn 跳过（不抛错，jsonl_path=None）。"""
    monkeypatch.setenv("MILOCO_TRACE_DEBUG", "1")
    # 预先建 300 个 .gz 文件
    today = Path(os.environ["MILOCO_HOME"]) / "trace" / "agent" / "20991231"
    today.mkdir(parents=True, exist_ok=True)
    for i in range(tr.DAILY_DUMP_MAX):
        (today / f"old_{i}.jsonl.gz").write_bytes(b"")

    # 把系统时间推到 2099-12-31 让 _today_dir() 用这个
    monkeypatch.setattr(tr, "_today_dir", lambda: today)

    sess = "miloco:test-cap"
    tr.register_trace_link(sess, "trace-abc")
    tr._hk_pre_llm_call(sess, "hi", [], True, "m", "p")
    tr._hk_on_session_end(sess, True, False, "m", "p")
    state = tr._turns[sess]
    assert state.done["jsonl_path"] is None  # 跳过落盘


# ── GC ────────────────────────────────────────────────────────────────────

def test_gc_removes_old_done_turns():
    """done_at 超过 TTL 的 turn 被 GC。"""
    sess_old = "miloco:old-1"
    sess_new = "miloco:new-1"
    tr.register_trace_link(sess_old, "trace-old")
    tr._hk_pre_llm_call(sess_old, "hi", [], True, "m", "p")
    tr._hk_on_session_end(sess_old, True, False, "m", "p")
    # 把 done_at 改到很久以前
    tr._turns[sess_old].done_at = 0  # epoch
    # 加一个新鲜的
    tr.register_trace_link(sess_new, "trace-new")
    tr._hk_pre_llm_call(sess_new, "hi", [], True, "m", "p")
    tr._hk_on_session_end(sess_new, True, False, "m", "p")
    tr._gc_expired_turns()
    assert sess_old not in tr._turns
    assert sess_new in tr._turns