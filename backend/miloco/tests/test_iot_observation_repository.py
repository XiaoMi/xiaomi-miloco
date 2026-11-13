from __future__ import annotations

from miloco.iot_observation.models import ObservationAttribute, ObservationChange
from miloco.iot_observation.repository import ObservationRepository


def test_repository_deadline_interrupts_sql_and_restores_connection(tmp_path):
    import sqlite3

    import pytest

    repo = ObservationRepository(tmp_path / "deadline.db").open()
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            with repo.deadline(0):
                with repo.transaction() as conn:
                    conn.execute("WITH RECURSIVE numbers(value) AS (SELECT 1 UNION ALL SELECT value+1 FROM numbers WHERE value<1000000) SELECT sum(value) FROM numbers").fetchone()
        assert repo.change_count() == 0
        assert repo.record_change("home", change(at_ms=1000, old=False, new=True))
    finally:
        repo.close()


def attribute(version: str = "v1") -> ObservationAttribute:
    return ObservationAttribute("did-1", "iot/device/did-1/prop/2.1", "light", "on", version)


def change(*, at_ms: int, old: object, new: object, scope: int = 1) -> ObservationChange:
    return ObservationChange(attribute(), old, new, "cloud", at_ms, scope)


def test_repository_preserves_event_values_and_filters_same_value(tmp_path) -> None:
    repo = ObservationRepository(tmp_path / "iot-observation.sqlite3")
    repo.open()
    try:
        assert repo.record_change("home-1", change(at_ms=1000, old=False, new=True))
        assert not repo.record_change("home-1", change(at_ms=1001, old=True, new=True))
        rows = repo.recent_changes("home-1", since_ms=0, until_ms=2000)
        assert [(row.old, row.new, row.at_ms) for row in rows] == [(False, True, 1000)]
    finally:
        repo.close()


def test_repository_isolates_family_and_policy_epoch(tmp_path) -> None:
    repo = ObservationRepository(tmp_path / "iot-observation.sqlite3")
    repo.open()
    try:
        repo.record_change("home-1", change(at_ms=1000, old=False, new=True))
        other = ObservationChange(
            ObservationAttribute("did-1", "iot/device/did-1/prop/2.1", "light", "on", "v2"),
            True,
            False,
            "cloud",
            1100,
            2,
        )
        repo.record_change("home-2", other)
        assert len(repo.recent_changes("home-1", since_ms=0, until_ms=2000)) == 1
        assert len(repo.recent_changes("home-2", since_ms=0, until_ms=2000)) == 1
        assert repo.attribute_count() == 2
    finally:
        repo.close()


def test_successful_run_watermark_is_not_advanced_by_failed_run(tmp_path) -> None:
    repo = ObservationRepository(tmp_path / "iot-observation.sqlite3")
    repo.open()
    try:
        repo.begin_run("run-1", "home-1", started_at_ms=1000, source="live")
        repo.finish_run("run-1", status="failed", finished_at_ms=1100)
        assert repo.last_success_id("home-1") is None
        repo.begin_run("run-2", "home-1", started_at_ms=1200, source="live")
        repo.finish_run("run-2", status="ok", finished_at_ms=1300, watermark_id=1250)
        assert repo.last_success_id("home-1") == 1250
    finally:
        repo.close()


def test_metadata_capacity_failure_rolls_back(tmp_path):
    import pytest
    repo = ObservationRepository(tmp_path / "db.sqlite3", max_attributes=1).open()
    repo.record_change("home-1", change(at_ms=1000, old=False, new=True))
    with pytest.raises(RuntimeError, match="capacity"):
        repo.record_baseline("home-2", attribute(), False, scope=1, source="iot_align", at_ms=2000)
    assert repo.attribute_count() == 1
    repo.close()


def test_empty_success_advances_live_cursor_but_replay_does_not(tmp_path):
    repo = ObservationRepository(tmp_path / "db.sqlite3").open()
    repo.begin_run("empty", "home-1", started_at_ms=1000, source="live")
    repo.finish_run("empty", status="empty", finished_at_ms=1000, watermark_id=0)
    repo.begin_run("replay", "home-1", started_at_ms=1000, source="replay")
    repo.finish_run("replay", status="ok", finished_at_ms=1000, watermark_id=999)
    assert repo.last_success_id("home-1") == 0
    repo.close()
