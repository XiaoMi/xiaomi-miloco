"""SQLite 表 + jsonl 目录的过期清理。"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _cutoff_ms(retention_days: int) -> int:
    return int(time.time() * 1000) - retention_days * 86400 * 1000


def cleanup_traces_table(conn: sqlite3.Connection, retention_days: int) -> int:
    cur = conn.execute(
        "DELETE FROM traces WHERE timestamp < ?", (_cutoff_ms(retention_days),)
    )
    return cur.rowcount


def cleanup_traces_device_table(conn: sqlite3.Connection, retention_days: int) -> int:
    cur = conn.execute(
        "DELETE FROM traces_device WHERE timestamp < ?",
        (_cutoff_ms(retention_days),),
    )
    return cur.rowcount


def cleanup_events_table(conn: sqlite3.Connection, retention_days: int) -> int:
    cur = conn.execute(
        "DELETE FROM events WHERE timestamp < ?", (_cutoff_ms(retention_days),)
    )
    return cur.rowcount


def cleanup_agent_runs_table(conn: sqlite3.Connection, retention_days: int) -> int:
    cur = conn.execute(
        "DELETE FROM agent_runs WHERE timestamp < ?",
        (_cutoff_ms(retention_days),),
    )
    return cur.rowcount


_DIR_RE = re.compile(r"^\d{8}$")


def cleanup_trace_jsonl(root: Path, retention_days: int) -> int:
    if not root.exists():
        return 0
    deleted = 0
    cutoff_ord = datetime.now().toordinal() - retention_days
    for entry in root.iterdir():
        if not entry.is_dir() or not _DIR_RE.match(entry.name):
            continue
        try:
            ord_day = datetime.strptime(entry.name, "%Y%m%d").toordinal()
        except ValueError:
            continue
        if ord_day < cutoff_ord:
            shutil.rmtree(entry, ignore_errors=True)
            deleted += 1
    return deleted
