# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""task summary 单元/集成测试。"""

from miloco.task.schema import (
    ActiveSession,
    RecordSummary,
    TaskSummaryView,
    WindowRemaining,
)


def test_window_remaining_schema_basic():
    wr = WindowRemaining(seconds=3600, display="1h 0m")
    assert wr.seconds == 3600
    assert wr.display == "1h 0m"


def test_active_session_schema_basic():
    s = ActiveSession(started_at="2026-06-11T14:05:00+08:00", elapsed_minutes=47)
    assert s.elapsed_minutes == 47


def test_record_summary_progress_minimal():
    rs = RecordSummary(
        kind="progress",
        completed=False,
        active_session=None,
        window_remaining=None,
        derived={"target": 20, "current": 12, "unit": "次", "remaining": 8, "progress_pct": 0.6},
    )
    assert rs.kind == "progress"
    assert rs.derived["current"] == 12


def test_task_summary_view_inherits_full_view_fields():
    view = TaskSummaryView(
        task_id="t1",
        description="d",
        status="active",
        paused_at=None,
        created_at="2026-06-01T00:00:00+08:00",
        rule_briefs=[],
        record=None,
    )
    assert view.record is None
    assert view.task_id == "t1"


# ── Task 2: _build_window_remaining / _build_active_session ───────────────────


from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from miloco.task_record.service import (  # noqa: E402
    _build_active_session,
    _build_window_remaining,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def test_window_remaining_day_at_morning():
    now = datetime(2026, 6, 11, 8, 0, 0, tzinfo=SHANGHAI_TZ)
    wr = _build_window_remaining("day", now)
    assert wr.seconds == 16 * 3600
    assert wr.display == "16h 0m"


def test_window_remaining_day_at_late_night():
    now = datetime(2026, 6, 11, 23, 58, 30, tzinfo=SHANGHAI_TZ)
    wr = _build_window_remaining("day", now)
    assert wr.seconds == 90
    assert wr.display == "1m"


def test_window_remaining_day_at_seconds():
    now = datetime(2026, 6, 11, 23, 59, 47, tzinfo=SHANGHAI_TZ)
    wr = _build_window_remaining("day", now)
    assert wr.seconds == 13
    assert wr.display == "13s"


def test_active_session_none_when_start_at_empty():
    row = {"active_session_start_at": None}
    now = datetime(2026, 6, 11, 14, 52, 0, tzinfo=SHANGHAI_TZ)
    assert _build_active_session(row, now) is None


def test_active_session_computes_elapsed_minutes():
    row = {"active_session_start_at": "2026-06-11T14:05:00+08:00"}
    now = datetime(2026, 6, 11, 14, 52, 0, tzinfo=SHANGHAI_TZ)
    sess = _build_active_session(row, now)
    assert sess is not None
    assert sess.started_at == "2026-06-11T14:05:00+08:00"
    assert sess.elapsed_minutes == 47


# ── Task 3: TaskRecordService.list_active_summaries ───────────────────────────


import pytest  # noqa: E402
from miloco.task_record.schema import RecordKind  # noqa: E402
from miloco.task_record.service import TaskRecordService  # noqa: E402


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """复制 test_task_router_e2e.py 的 fixture 思路:隔离 DB + 重置 manager。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    # API 出口字符串后缀依赖 deploy_timezone(),锁 Asia/Shanghai 让断言稳定。
    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module
    import miloco.manager as manager_module

    connector_module.db_connector = None
    connector_module.init_database()
    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    yield


def _insert_task(task_id: str, description: str = "d", status: str = "active") -> None:
    """直插 task 行,绕过 service。"""
    from miloco.database.connector import db_connector

    assert db_connector is not None
    with db_connector.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO task (task_id, description, status, created_at) "
            "VALUES (?, ?, ?, '2026-06-01T00:00:00+08:00')",
            (task_id, description, status),
        )


def test_list_active_summaries_empty(isolated_db):
    service = TaskRecordService()
    result = service.list_active_summaries(window="day")
    assert result == {}


def test_list_active_summaries_progress_kind_day_window(isolated_db):
    _insert_task("t_prog")
    service = TaskRecordService()
    service.init_record(
        "t_prog",
        RecordKind.PROGRESS,
        {"target": 20, "unit": "次", "window": "day"},
    )
    service.progress_increment("t_prog", delta=12)
    result = service.list_active_summaries(window="day")
    assert "t_prog" in result
    rs = result["t_prog"]
    assert rs.kind == "progress"
    assert rs.completed is False
    assert rs.active_session is None
    assert rs.window_remaining is not None
    assert rs.derived["target"] == 20
    assert rs.derived["current"] == 12
    assert rs.derived["unit"] == "次"
    assert rs.derived["remaining"] == 8
    assert 0 < rs.derived["progress_pct"] <= 1


def test_list_active_summaries_progress_kind_window_all_no_window_remaining(isolated_db):
    _insert_task("t_prog2")
    service = TaskRecordService()
    service.init_record(
        "t_prog2",
        RecordKind.PROGRESS,
        {"target": 10, "unit": "次", "window": "day"},
    )
    service.progress_increment("t_prog2", delta=3)
    result = service.list_active_summaries(window="all")
    rs = result["t_prog2"]
    assert rs.window_remaining is None
    assert rs.derived["target"] == 10
    assert rs.derived["current"] == 3


def test_list_active_summaries_duration_window_day_with_active_session(isolated_db):
    _insert_task("t_dur")
    service = TaskRecordService()
    service.init_record(
        "t_dur",
        RecordKind.DURATION,
        {"target_minutes": 120},
    )
    service.session_start("t_dur", at="2026-06-11T14:05:00+08:00")
    result = service.list_active_summaries(window="day")
    rs = result["t_dur"]
    assert rs.kind == "duration"
    assert rs.active_session is not None
    # API 出口走 deploy_timezone(测试 fixture 锁 Asia/Shanghai)。
    assert rs.active_session.started_at == "2026-06-11T14:05:00+08:00"
    assert "target_minutes" in rs.derived
    assert "accumulated_minutes_today" in rs.derived


def test_list_active_summaries_duration_window_all_no_accumulated(isolated_db):
    _insert_task("t_dur2")
    service = TaskRecordService()
    service.init_record(
        "t_dur2",
        RecordKind.DURATION,
        {"target_minutes": 60},
    )
    result = service.list_active_summaries(window="all")
    rs = result["t_dur2"]
    assert "target_minutes" in rs.derived
    assert "accumulated_minutes_today" not in rs.derived


def test_list_active_summaries_event_kind(isolated_db):
    _insert_task("t_evt")
    service = TaskRecordService()
    service.init_record("t_evt", RecordKind.EVENT, {})
    service.event_append(
        "t_evt", description="客人到访", at="2026-06-11T11:42:13+08:00"
    )
    result = service.list_active_summaries(window="day")
    rs = result["t_evt"]
    assert rs.kind == "event"
    assert rs.derived["count_total"] == 1
    assert rs.derived["count_today"] == 0 or rs.derived["count_today"] == 1
    assert rs.derived["last_at"] == "2026-06-11T11:42:13+08:00"


def test_list_active_summaries_event_window_all_no_count_today(isolated_db):
    _insert_task("t_evt2")
    service = TaskRecordService()
    service.init_record("t_evt2", RecordKind.EVENT, {})
    service.event_append("t_evt2", description="x", at="2026-06-11T10:00:00+08:00")
    result = service.list_active_summaries(window="all")
    rs = result["t_evt2"]
    assert "count_total" in rs.derived
    assert "count_today" not in rs.derived


def test_list_active_summaries_broken_progress_isolated(isolated_db, monkeypatch):
    _insert_task("t_bad")
    _insert_task("t_good")
    service = TaskRecordService()
    service.init_record(
        "t_bad",
        RecordKind.PROGRESS,
        {"target": 5, "unit": "次", "window": "day"},
    )
    service.init_record(
        "t_good",
        RecordKind.PROGRESS,
        {"target": 5, "unit": "次", "window": "day"},
    )

    import miloco.task_record.service as srv_module

    orig = srv_module._derive_progress

    def fake_derive_progress(row):
        if row["task_id"] == "t_bad":
            raise RuntimeError("forced derive failure")
        return orig(row)

    monkeypatch.setattr(srv_module, "_derive_progress", fake_derive_progress)
    result = service.list_active_summaries(window="day")
    assert "t_bad" not in result
    assert "t_good" in result


# ── Task 4: TaskService.list_summary 左连接 ───────────────────────────────────


def test_task_service_list_summary_left_join_with_and_without_record(isolated_db):
    _insert_task("t_with_record")
    _insert_task("t_no_record")

    rec_service = TaskRecordService()
    rec_service.init_record(
        "t_with_record",
        RecordKind.PROGRESS,
        {"target": 10, "unit": "次", "window": "day"},
    )
    rec_service.progress_increment("t_with_record", delta=3)

    from miloco.task.service import TaskService

    task_service = TaskService()
    views = task_service.list_summary(window="day")
    by_id = {v.task_id: v for v in views}
    assert by_id["t_with_record"].record is not None
    assert by_id["t_with_record"].record.derived["current"] == 3
    assert by_id["t_no_record"].record is None


def test_task_service_list_summary_paused_task_included(isolated_db):
    _insert_task("t_paused", status="paused")

    from miloco.task.service import TaskService

    views = TaskService().list_summary(window="day")
    assert any(v.task_id == "t_paused" and v.status == "paused" for v in views)
