# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Integration tests for PerceptionLogRepo against a real SQLite file."""

import time
import uuid
from dataclasses import dataclass, field

import pytest


@dataclass
class _LogEntry:
    """Minimal stand-in for PerceptionLogEntry (avoids importing heavy perception pkg)."""

    timestamp: int
    descriptions: dict
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """Each test case gets a fresh SQLite DB."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()

    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    yield db_file

    reset_settings()


@pytest.fixture
def repo(real_db):
    from miloco.database.perception_repo import PerceptionLogRepo

    return PerceptionLogRepo()


def _make_entry(ts_ms: int, desc: dict[str, str] | None = None) -> _LogEntry:
    return _LogEntry(
        timestamp=ts_ms,
        descriptions=desc or {"cam1": f"scene at {ts_ms}"},
    )


class TestPerceptionRepoQuery:
    def test_basic_insert_and_query(self, repo):
        now_ms = int(time.time() * 1000)
        repo.append(_make_entry(now_ms, {"cam1": "hello"}))

        logs, count = repo.query()
        assert count == 1
        assert logs[0]["d"] == {"cam1": "hello"}
        assert "T" in logs[0]["t"]

    def test_limit_none_returns_all(self, repo):
        base = int(time.time() * 1000)
        for i in range(5):
            repo.append(_make_entry(base + i * 1000, {"cam1": f"scene {i}"}))

        logs, count = repo.query(limit=None)
        assert count == 5

    def test_limit_restricts_count(self, repo):
        base = int(time.time() * 1000)
        for i in range(5):
            repo.append(_make_entry(base + i * 1000, {"cam1": f"scene {i}"}))

        logs, count = repo.query(limit=2)
        assert count == 2

    def test_limit_returns_latest_rows_in_ascending_order(self, repo):
        base = int(time.time() * 1000)
        for i in range(5):
            repo.append(_make_entry(base + i * 1000, {"cam1": f"scene {i}"}))

        logs, count = repo.query(limit=2)

        assert count == 2
        assert [log["d"]["cam1"] for log in logs] == ["scene 3", "scene 4"]

    def test_after_ms_filter(self, repo):
        base = int(time.time() * 1000)
        for i in range(5):
            repo.append(_make_entry(base + i * 1000, {"cam1": f"scene {i}"}))

        logs, count = repo.query(after_ms=base + 2000)
        assert count == 2
        assert "scene 3" in logs[0]["d"]["cam1"]

    def test_before_ms_filter(self, repo):
        base = int(time.time() * 1000)
        for i in range(5):
            repo.append(_make_entry(base + i * 1000, {"cam1": f"scene {i}"}))

        logs, count = repo.query(before_ms=base + 2000)
        assert count == 2
        assert "scene 0" in logs[0]["d"]["cam1"]

    def test_after_and_before_window(self, repo):
        base = int(time.time() * 1000)
        for i in range(5):
            repo.append(_make_entry(base + i * 1000, {"cam1": f"scene {i}"}))

        logs, count = repo.query(after_ms=base + 1000, before_ms=base + 4000)
        assert count == 2

    def test_since_ms_filter(self, repo):
        old = int(time.time() * 1000) - 7200_000
        recent = int(time.time() * 1000) - 1000
        repo.append(_make_entry(old, {"cam1": "old"}))
        repo.append(_make_entry(recent, {"cam1": "recent"}))

        since_ms = int(time.time() * 1000) - 3600_000
        logs, count = repo.query(since_ms=since_ms)
        assert count == 1
        assert logs[0]["d"]["cam1"] == "recent"

    def test_t_field_is_iso8601(self, repo):
        now_ms = int(time.time() * 1000)
        repo.append(_make_entry(now_ms, {"cam1": "test"}))

        logs, _ = repo.query()
        t_val = logs[0]["t"]
        assert "T" in t_val
        assert "+" in t_val or "Z" in t_val

    def test_adjacent_dedup(self, repo):
        now_ms = int(time.time() * 1000)
        desc = {"cam1": "same scene"}
        assert repo.append(_make_entry(now_ms, desc)) is True
        assert repo.append(_make_entry(now_ms + 1000, desc)) is False

        logs, count = repo.query()
        assert count == 1

    def test_delete_before_days(self, repo):
        old_ms = int(time.time() * 1000) - 40 * 86400_000
        recent_ms = int(time.time() * 1000)
        repo.append(_make_entry(old_ms, {"cam1": "old"}))
        repo.append(_make_entry(recent_ms, {"cam1": "new"}))

        deleted = repo.delete_before_days(30)
        assert deleted == 1

        logs, count = repo.query()
        assert count == 1
        assert logs[0]["d"]["cam1"] == "new"
