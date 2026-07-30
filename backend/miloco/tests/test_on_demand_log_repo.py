# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Integration tests for OnDemandLogRepo against a real SQLite file."""

import time
import uuid

import pytest


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
    from miloco.database.on_demand_log_repo import OnDemandLogRepo

    return OnDemandLogRepo()


def _make_entry(
    ts_ms: int,
    *,
    query: str = "谁在客厅？",
    answer: str = "没有人",
    sources: list[str] | None = None,
    clip_dids: list[str] | None = None,
    clip_kinds: dict[str, str] | None = None,
    has_trace: bool = False,
    entry_id: str | None = None,
):
    from miloco.perception.schema import OnDemandLogEntry

    return OnDemandLogEntry(
        id=entry_id or str(uuid.uuid4()),
        timestamp=ts_ms,
        query=query,
        answer=answer,
        sources=sources or ["lumi.camera.v1"],
        latency_ms=120,
        snapshot_count=len(clip_dids) if clip_dids else 0,
        clip_dids=clip_dids or [],
        clip_kinds=clip_kinds or {},
        has_trace=has_trace,
    )


class TestAppendAndQuery:
    def test_roundtrip(self, repo):
        now_ms = int(time.time() * 1000)
        entry = _make_entry(
            now_ms,
            sources=["dev_a", "dev_b"],
            clip_dids=["dev_a"],
            clip_kinds={"dev_a": "mp4"},
            has_trace=True,
        )
        assert repo.append(entry) is True

        logs = repo.query()
        assert len(logs) == 1
        row = logs[0]
        assert row["id"] == entry.id
        assert row["timestamp"] == now_ms
        assert row["query"] == "谁在客厅？"
        assert row["answer"] == "没有人"
        assert row["sources"] == ["dev_a", "dev_b"]
        assert row["clip_dids"] == ["dev_a"]
        assert row["clip_kinds"] == {"dev_a": "mp4"}
        assert row["has_trace"] is True

    def test_empty_query(self, repo):
        logs = repo.query()
        assert logs == []
        assert len(logs) == 0


class TestGetById:
    def test_hit(self, repo):
        now_ms = int(time.time() * 1000)
        entry = _make_entry(now_ms, entry_id="test-uuid-123")
        repo.append(entry)

        row = repo.get_by_id("test-uuid-123")
        assert row is not None
        assert row["id"] == "test-uuid-123"

    def test_miss(self, repo):
        assert repo.get_by_id("nonexistent") is None


class TestCompoundCursorPagination:
    """Compound cursor (timestamp, id) prevents data loss at page boundaries
    when multiple entries share the same millisecond timestamp."""

    def test_same_ms_no_duplicates_no_loss(self, repo):
        ts = int(time.time() * 1000)
        ids = [f"id-{chr(ord('a') + i)}" for i in range(5)]
        for eid in ids:
            repo.append(_make_entry(ts, entry_id=eid))

        page1 = repo.query(limit=3)
        assert len(page1) == 3

        oldest = page1[-1]
        page2 = repo.query(
            before_ms=oldest["timestamp"],
            before_id=oldest["id"],
            limit=3,
        )
        assert len(page2) == 2

        all_ids = {r["id"] for r in page1} | {r["id"] for r in page2}
        assert all_ids == set(ids)

    def test_cursor_without_before_id_falls_back_to_timestamp_only(self, repo):
        base = int(time.time() * 1000)
        repo.append(_make_entry(base, entry_id="older"))
        repo.append(_make_entry(base + 1000, entry_id="newer"))

        logs = repo.query(before_ms=base + 1000)
        assert len(logs) == 1
        assert logs[0]["id"] == "older"

    def test_pagination_order_is_desc(self, repo):
        base = int(time.time() * 1000)
        for i in range(5):
            repo.append(_make_entry(base + i * 1000, entry_id=f"entry-{i}"))

        logs = repo.query()
        timestamps = [r["timestamp"] for r in logs]
        assert timestamps == sorted(timestamps, reverse=True)


class TestQueryFilters:
    def test_after_ms_exclusive(self, repo):
        base = int(time.time() * 1000)
        repo.append(_make_entry(base, entry_id="at"))
        repo.append(_make_entry(base + 1000, entry_id="after"))

        logs = repo.query(after_ms=base)
        assert len(logs) == 1
        assert logs[0]["id"] == "after"

    def test_since_ms_inclusive(self, repo):
        base = int(time.time() * 1000)
        repo.append(_make_entry(base - 1000, entry_id="before"))
        repo.append(_make_entry(base, entry_id="at"))
        repo.append(_make_entry(base + 1000, entry_id="after"))

        logs = repo.query(since_ms=base)
        assert len(logs) == 2
        assert {r["id"] for r in logs} == {"at", "after"}


class TestDeleteBeforeDays:
    def test_deletes_old_keeps_recent(self, repo):
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 8 * 86400 * 1000  # 8 days ago
        repo.append(_make_entry(old_ms, entry_id="old"))
        repo.append(_make_entry(now_ms, entry_id="recent"))

        deleted = repo.delete_before_days(7)
        assert deleted == 1

        logs = repo.query()
        assert len(logs) == 1
        assert logs[0]["id"] == "recent"

    def test_within_window_not_deleted(self, repo):
        now_ms = int(time.time() * 1000)
        just_within = now_ms - 6 * 86400 * 1000  # 6 days ago (within 7-day window)
        repo.append(_make_entry(just_within, entry_id="within"))

        deleted = repo.delete_before_days(7)
        assert deleted == 0
        assert repo.count_all() == 1


class TestDuplicateId:
    def test_duplicate_id_returns_false(self, repo):
        entry = _make_entry(int(time.time() * 1000), entry_id="dup")
        assert repo.append(entry) is True
        assert repo.append(entry) is False


class TestRowToDict:
    """Verify JSON deserialization and defaults for optional columns."""

    def test_clip_kinds_dict_roundtrip(self, repo):
        now_ms = int(time.time() * 1000)
        repo.append(_make_entry(
            now_ms,
            clip_dids=["cam1", "cam2"],
            clip_kinds={"cam1": "mp4", "cam2": "m4a"},
        ))

        row = repo.query()[0]
        assert row["clip_kinds"] == {"cam1": "mp4", "cam2": "m4a"}
        assert row["clip_dids"] == ["cam1", "cam2"]

    def test_defaults_for_no_artifacts(self, repo):
        now_ms = int(time.time() * 1000)
        repo.append(_make_entry(now_ms))

        row = repo.query()[0]
        assert row["snapshot_count"] == 0
        assert row["clip_dids"] == []
        assert row["clip_kinds"] == {}
        assert row["has_trace"] is False
