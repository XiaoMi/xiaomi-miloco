import time
from datetime import datetime, timedelta

from miloco.observability.cleanup import (
    cleanup_agent_runs_table,
    cleanup_events_table,
    cleanup_trace_jsonl,
    cleanup_traces_device_table,
    cleanup_traces_table,
)
from miloco.observability.metrics_db import connect, init_schema


def test_cleanup_traces_deletes_old_rows(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    old = now_ms - 8 * 86400 * 1000
    new = now_ms - 1 * 86400 * 1000
    for ts, tid in [(old, "old-1"), (new, "new-1")]:
        conn.execute(
            "INSERT INTO traces (trace_id, timestamp) VALUES (?, ?)", (tid, ts)
        )
    deleted = cleanup_traces_table(conn, retention_days=7)
    assert deleted == 1
    survivors = [r[0] for r in conn.execute("SELECT trace_id FROM traces").fetchall()]
    assert survivors == ["new-1"]


def test_cleanup_traces_device_independent(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    old = now_ms - 8 * 86400 * 1000
    new = now_ms - 1 * 86400 * 1000
    conn.execute(
        "INSERT INTO traces_device (device_trace_id, cycle_id, timestamp, device_id) "
        "VALUES (?, ?, ?, ?)", ("dt-old", "c-old", old, "did-1")
    )
    conn.execute(
        "INSERT INTO traces_device (device_trace_id, cycle_id, timestamp, device_id) "
        "VALUES (?, ?, ?, ?)", ("dt-new", "c-new", new, "did-1")
    )
    deleted = cleanup_traces_device_table(conn, retention_days=7)
    assert deleted == 1


def test_cleanup_events_independent(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    old = now_ms - 8 * 86400 * 1000
    new = now_ms - 1 * 86400 * 1000
    conn.execute(
        "INSERT INTO events (event_id, timestamp, event_type, source) "
        "VALUES (?, ?, ?, ?)", ("e-old", old, "rule_match", "r-1")
    )
    conn.execute(
        "INSERT INTO events (event_id, timestamp, event_type, source) "
        "VALUES (?, ?, ?, ?)", ("e-new", new, "rule_match", "r-1")
    )
    deleted = cleanup_events_table(conn, retention_days=7)
    assert deleted == 1


def test_cleanup_trace_jsonl_removes_old_dir(tmp_path):
    root = tmp_path / "trace" / "rule"
    old_dir = root / (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    new_dir = root / (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    (old_dir / "run.jsonl.gz").write_text("x")
    (new_dir / "run.jsonl.gz").write_text("y")

    deleted = cleanup_trace_jsonl(root=root, retention_days=7)
    assert deleted == 1
    assert not old_dir.exists()
    assert new_dir.exists()


def test_cleanup_agent_runs_table(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    now_ms = int(time.time() * 1000)
    old = now_ms - 8 * 86400 * 1000
    new = now_ms - 1 * 86400 * 1000
    for ts, run_id in [(old, "r-old"), (new, "r-new")]:
        conn.execute(
            "INSERT INTO agent_runs (run_id, trace_id, timestamp, source) "
            "VALUES (?, ?, ?, ?)", (run_id, "c-1", ts, "interaction")
        )
    deleted = cleanup_agent_runs_table(conn, retention_days=7)
    assert deleted == 1
    survivors = [r[0] for r in conn.execute(
        "SELECT run_id FROM agent_runs"
    ).fetchall()]
    assert survivors == ["r-new"]
