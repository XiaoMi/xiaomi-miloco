import pytest
from miloco.observability.metrics_db import SCHEMA_VERSION, connect, init_schema


def test_init_schema_creates_required_tables(tmp_path):
    db_path = tmp_path / "obs.db"
    conn = connect(db_path)
    init_schema(conn)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "traces" in tables
    assert "traces_device" in tables
    assert "events" in tables
    assert "agent_runs" in tables


def test_init_schema_creates_traces_v_view(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    views = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    ).fetchall()}
    assert "traces_v" in views


def test_init_schema_is_idempotent(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    init_schema(conn)


def test_traces_table_has_required_columns(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(traces)").fetchall()}
    for required in (
        "trace_id", "timestamp", "device_count", "skipped",
        "in_delay_ms", "cycle_total_ms",
        "window_first_frame_recv_ms", "stream_lag_ms",
        "gate_ms", "gate_video_pass", "gate_audio_pass",
        "omni_call_count", "omni_error_count",
        "timing_detail", "cycle_error_msg",
    ):
        assert required in cols, f"missing column: {required}"
    # agent_* 列已搬到 agent_runs 表,traces 表不再持有
    for removed in (
        "has_agent_turn", "run_id", "agent_query",
        "agent_webhook_rtt_ms", "agent_duration_ms",
        "llm_call_count", "tool_call_count",
        "llm_total_ms", "tool_total_ms", "tool_max_ms",
        "slowest_tool_name", "agent_success",
        "agent_error_count", "agent_error_msg", "jsonl_path",
    ):
        assert removed not in cols, f"column should be removed: {removed}"


def test_agent_runs_table_has_required_columns(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(agent_runs)").fetchall()}
    expected = {
        "run_id", "trace_id", "timestamp", "source",
        "query", "webhook_rtt_ms", "duration_ms",
        "llm_call_count", "tool_call_count",
        "llm_total_ms", "tool_total_ms", "tool_max_ms",
        "slowest_tool_name", "success",
        "error_count", "error_msg", "jsonl_path",
    }
    assert expected == cols


def test_traces_v_view_derives_has_agent_turn(tmp_path):
    """traces_v.has_agent_turn 应来自 EXISTS(agent_runs WHERE trace_id=...)。"""
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    conn.execute(
        "INSERT INTO traces (trace_id, timestamp) VALUES (?, ?)", ("c-1", 1000),
    )
    conn.execute(
        "INSERT INTO traces (trace_id, timestamp) VALUES (?, ?)", ("c-2", 2000),
    )
    conn.execute(
        "INSERT INTO agent_runs (run_id, trace_id, timestamp, source) "
        "VALUES (?, ?, ?, ?)",
        ("r-1", "c-1", 1100, "interaction"),
    )
    rows = dict(conn.execute(
        "SELECT trace_id, has_agent_turn FROM traces_v"
    ).fetchall())
    assert rows == {"c-1": 1, "c-2": 0}


def test_auto_vacuum_incremental(tmp_path):
    """新建 db connect 后应是 auto_vacuum=INCREMENTAL(=2)。"""
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    assert mode == 2


def test_traces_device_table_has_required_columns(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(traces_device)").fetchall()}
    for required in (
        "device_trace_id", "cycle_id", "timestamp",
        "device_id", "room_name",
        "decode_video_avg_ms", "decode_audio_avg_ms",
        "video_frame_count", "audio_frame_count",
        "gate_ms", "gate_video_ms", "gate_audio_ms",
        "gate_video_pass", "gate_audio_pass", "gate_skipped",
        "identity_ms", "omni_ms",
        "omni_error_code", "omni_retry_count",
        "dropped_windows_count", "overflow_count",
        "max_buffer_depth", "last_overflow_action",
    ):
        assert required in cols, f"missing column: {required}"


def test_traces_overflow_columns_exist(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(traces)").fetchall()}
    assert "dropped_windows_total" in cols
    assert "overflow_count_total" in cols


def test_user_version_set_after_init(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    cur = conn.execute("PRAGMA user_version").fetchone()[0]
    assert cur == SCHEMA_VERSION


def test_init_schema_refuses_legacy_db_without_version(tmp_path):
    """老 db: 表已经在但 user_version=0,应拒启动并提示删 db。"""
    db = tmp_path / "obs.db"
    conn = connect(db)
    conn.execute("CREATE TABLE traces (trace_id TEXT)")  # 模拟无版本号老库
    with pytest.raises(RuntimeError, match="无 schema 版本号"):
        init_schema(conn)


def test_init_schema_refuses_mismatched_version(tmp_path):
    """user_version 不为 0 且 ≠ SCHEMA_VERSION,直接报错。"""
    conn = connect(tmp_path / "obs.db")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="不匹配"):
        init_schema(conn)


def test_events_table_columns(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(events)").fetchall()}
    assert cols == {
        "event_id", "timestamp", "event_type", "trace_id", "source", "payload"
    }


def test_wal_mode_enabled(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# =============================================================================
# gate_hold_pass 列 schema + init_schema 重复调用幂等
# =============================================================================

import sqlite3  # noqa: E402


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def test_fresh_db_has_gate_hold_pass(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_schema(conn)
    assert _has_column(conn, "traces_device", "gate_hold_pass")
    assert _has_column(conn, "traces", "gate_hold_pass")


def test_init_schema_idempotent_repeat_init(tmp_path):
    conn = connect(tmp_path / "idem.db")
    init_schema(conn)
    init_schema(conn)
    init_schema(conn)
    assert _has_column(conn, "traces_device", "gate_hold_pass")


# =============================================================================
# action_ledger 表 (v2) schema + v1 → v2 migration
# =============================================================================


def test_fresh_db_has_action_ledger_table(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "action_ledger" in tables


def test_action_ledger_columns(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(action_ledger)").fetchall()}
    assert cols == {
        "id", "timestamp", "action_type", "did", "device_name", "room",
        "iid", "value_json", "result_code", "result_msg", "success",
        "error", "trace_id",
    }


def test_action_ledger_has_timestamp_index(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    idx = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_action_ledger_ts" in idx


def test_user_version_is_2(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_v1_to_v2_migration_adds_action_ledger(tmp_path):
    """模拟一个 v1 库(有全部旧表 + user_version=1,无 action_ledger),
    init_schema 应 additive 补表并把 user_version 推到 2,不删库。"""
    db = tmp_path / "obs.db"
    conn = connect(db)
    # 先建一个真 v1 库:临时把 SCHEMA_VERSION 回退没法直接做,故手工搭 v1 骨架。
    conn.execute("CREATE TABLE traces (trace_id TEXT PRIMARY KEY, timestamp INTEGER)")
    conn.execute("CREATE TABLE traces_device (device_trace_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY)")
    conn.execute("PRAGMA user_version = 1")
    # v1 库里插一行,验证 migration 不丢数据
    conn.execute("INSERT INTO traces (trace_id, timestamp) VALUES ('t1', 111)")

    init_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "action_ledger" in tables
    # 旧数据仍在
    assert conn.execute(
        "SELECT trace_id FROM traces"
    ).fetchone()[0] == "t1"
