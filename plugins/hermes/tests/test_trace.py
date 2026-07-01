import threading

import pytest

from hermes import trace


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "miloco"
    home.mkdir()
    monkeypatch.setenv("MILOCO_HOME", str(home))
    trace._turns.clear()
    trace._trace_links.clear()
    yield
    trace._turns.clear()
    trace._trace_links.clear()


def test_register_trace_link_creates_placeholder_in_progress():
    trace.register_trace_link("run-1", "trace-A")
    state = trace._get_turn("run-1")
    assert state is not None
    assert state["buffer"] == []
    assert trace.get_turn_status("run-1") == "in_progress"


def test_register_trace_link_sets_trace_link():
    trace.register_trace_link("run-1", "trace-A")
    assert trace._trace_links["run-1"] == "trace-A"


def test_pop_trace_link_returns_and_removes():
    trace.register_trace_link("run-1", "trace-A")
    assert trace.pop_trace_link("run-1") == "trace-A"
    assert "run-1" not in trace._trace_links


def test_pop_trace_link_missing_returns_none():
    assert trace.pop_trace_link("missing") is None


def test_record_event_accumulates_to_buffer():
    trace.record_event("run-2", "llm_input", {"prompt": "hi"})
    trace.record_event("run-2", "llm_output", {"text": "hello"})
    state = trace._get_turn("run-2")
    assert len(state["buffer"]) == 2
    assert state["buffer"][0]["hook"] == "llm_input"
    assert state["buffer"][1]["hook"] == "llm_output"


def test_get_turn_status_unknown_for_missing():
    assert trace.get_turn_status("nope") == "unknown"


def test_finalize_turn_sets_done():
    trace.register_trace_link("run-3", "trace-C")
    trace.record_event("run-3", "llm_output", {"text": "x"})
    trace.record_event("run-3", "after_tool_call", {"toolName": "search"})
    trace.finalize_turn("run-3", success=True, duration_ms=42)
    assert trace.get_turn_status("run-3") == "done"


def test_pop_done_turn_returns_meta_and_clears():
    trace.register_trace_link("run-4", "trace-D")
    trace.record_event("run-4", "llm_output", {"text": "x"})
    trace.record_event("run-4", "after_tool_call", {"toolName": "search"})
    trace.finalize_turn("run-4", success=True, duration_ms=10)
    meta = trace.pop_done_turn("run-4")
    assert meta is not None
    assert meta["run_id"] == "run-4"
    assert meta["trace_id"] == "trace-D"
    assert meta["success"] is True
    assert meta["duration_ms"] == 10
    assert meta["llm_call_count"] == 1
    assert meta["tool_call_count"] == 1
    assert trace._get_turn("run-4") is None
    assert trace.get_turn_status("run-4") == "unknown"


def test_pop_done_turn_returns_none_when_not_done():
    trace.register_trace_link("run-5", "trace-E")
    assert trace.pop_done_turn("run-5") is None


def test_peek_turn_meta_non_destructive():
    trace.register_trace_link("run-6", "trace-F")
    trace.finalize_turn("run-6", success=True, duration_ms=1)
    meta = trace.peek_turn_meta("run-6")
    assert meta is not None
    assert meta["success"] is True
    assert trace._get_turn("run-6") is not None


def test_record_event_truncates_at_buffer_max():
    run_id = "run-buffer"
    for i in range(trace.BUFFER_MAX + 50):
        trace.record_event(run_id, "llm_output", {"i": i})
    state = trace._get_turn(run_id)
    assert len(state["buffer"]) == trace.BUFFER_MAX + 1
    assert state["buffer"][-1]["hook"] == "_truncated"


def test_gc_expires_done_turn_after_ttl(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(trace.time, "time", lambda: clock[0])
    trace.register_trace_link("run-gc", "trace-G")
    trace.finalize_turn("run-gc", success=True, duration_ms=5)
    assert trace.get_turn_status("run-gc") == "done"
    clock[0] = 1000.0 + trace.DONE_TTL_S + 1
    trace._gc_expired_turns()
    assert trace.get_turn_status("run-gc") == "unknown"


def test_gc_expires_stuck_in_progress_turn(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(trace.time, "time", lambda: clock[0])
    trace.register_trace_link("run-stuck", "trace-S")
    clock[0] = 1000.0 + trace.STUCK_TTL_S + 1
    trace._gc_expired_turns()
    assert trace.get_turn_status("run-stuck") == "unknown"


def test_lock_is_a_lock():
    assert isinstance(trace._lock, type(threading.Lock()))


def test_constants_match_spec():
    assert trace.BUFFER_MAX == 500
    assert trace.DONE_TTL_S == 120.0
    assert trace.STUCK_TTL_S == 900.0
    assert trace.TURNS_HARD_CAP == 20
    assert trace.DAILY_DUMP_MAX == 300
