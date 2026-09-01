# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""TaskRecordService 单测：业务规则 + 跨表事务 + derived 装配 + 异常路径。

覆盖 spec §11.1 列出的关键 case：
- record init 事务边界（FK 兜底 / 重复 init 409 / task 不存在 404）
- progress_increment 三分支 + 负 delta 撤销 + completed 回退
- session_start/end reentry / order 校验
- event_append 基本 / 时序
- record update 白名单 / 禁字段 / 不命中
- task 无活跃 record → no_active_record
- terminate_log 写入 + 30 天滚动 + final_snapshot 按 kind 装配
- rollover_one（progress + duration 跨 active session 切两段）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_task_record_service.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    # API 出口字符串后缀依赖 deploy_timezone(),锁 Asia/Shanghai 让断言稳定。
    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()
    yield db_file
    reset_settings()


@pytest.fixture
def service(real_db):
    from miloco.task_record.service import TaskRecordService

    return TaskRecordService()


@pytest.fixture
def db(real_db):
    from miloco.database.connector import get_db_connector

    return get_db_connector()


def _insert_task(db, task_id: str, description: str = "x"):
    from miloco.utils.time_utils import now_iso

    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO task (task_id, description, status, created_at) "
            "VALUES (?, ?, 'active', ?)",
            (task_id, description, now_iso()),
        )
        conn.commit()


# ── init_record ──────────────────────────────────────────────────────────────


class TestInitRecord:
    def test_init_progress_ok(self, service, db):
        _insert_task(db, "p1")
        view = service.init_record(
            "p1",
            __import__("miloco.task_record.schema", fromlist=["RecordKind"]).RecordKind.PROGRESS,
            {"target": 8, "unit": "杯", "window": "day"},
        )
        assert view["kind"] == "progress"
        assert view["record"]["current"] == 0
        assert view["record"]["target"] == 8
        assert view["derived"]["remaining"] == 8
        assert view["derived"]["progress_pct"] == 0.0

    def test_init_duplicate_returns_already_exists(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordAlreadyExistsError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        with pytest.raises(RecordAlreadyExistsError):
            service.init_record(
                "p1",
                RecordKind.PROGRESS,
                {"target": 10, "unit": "杯", "window": "day"},
            )

    def test_init_task_not_found(self, service):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            service.init_record(
                "nope",
                RecordKind.PROGRESS,
                {"target": 8, "unit": "杯", "window": "day"},
            )

    def test_init_schema_invalid(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "p1")
        with pytest.raises(RecordSchemaError):
            service.init_record(
                "p1",
                RecordKind.PROGRESS,
                {"target": -1, "unit": "杯", "window": "day"},
            )

    def test_init_event(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "e1")
        view = service.init_record("e1", RecordKind.EVENT, {})
        assert view["kind"] == "event"
        assert view["derived"]["count_total"] == 0
        assert view["derived"]["last_at"] is None

    def test_init_duration_without_target_minutes(self, service, db):
        """无阈值时长追踪场景:省略 target_minutes 应允许,derived.remaining_minutes=None。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d_no_target")
        view = service.init_record("d_no_target", RecordKind.DURATION, {})
        assert view["kind"] == "duration"
        assert view["record"]["target_minutes"] is None
        assert view["derived"]["accumulated_minutes_today"] == 0
        assert view["derived"]["remaining_minutes"] is None
        assert view["derived"]["active_session_start_at"] is None


# ── progress_increment ───────────────────────────────────────────────────────


class TestProgressIncrement:
    def test_increment_basic(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        r = service.progress_increment("p1", delta=1)
        assert r["current"] == 1
        assert r["status"] == "active"
        assert r["derived"]["remaining"] == 7

    def test_increment_caps_at_target_and_flips_status(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 3, "unit": "杯", "window": "day"}
        )
        r = service.progress_increment("p1", delta=10)
        assert r["current"] == 3
        assert r["status"] == "completed"
        assert r["derived"]["remaining"] == 0
        assert r["derived"]["progress_pct"] == 1.0

    def test_increment_noop_when_completed(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 2, "unit": "杯", "window": "day"}
        )
        service.progress_increment("p1", delta=5)
        r = service.progress_increment("p1", delta=1)
        assert r["noop"] is True
        assert r["reason"] == "inactive"
        assert r["current"] == 2

    def test_negative_delta_floors_at_zero(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        service.progress_increment("p1", delta=2)
        r = service.progress_increment("p1", delta=-5)
        assert r["current"] == 0
        assert r["status"] == "active"

    def test_negative_delta_keeps_completed(self, service, db):
        """spec §11.1 'completed 状态不回退'：负 delta 减 current 但 status 锁定。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 3, "unit": "杯", "window": "day"}
        )
        service.progress_increment("p1", delta=3)  # completed
        r = service.progress_increment("p1", delta=-1)
        assert r["current"] == 2
        assert r["status"] == "completed"  # status 保持，不回退到 active

    def test_no_active_record(self, service, db):
        from miloco.task_record.service import RecordNotFoundError

        _insert_task(db, "p1")
        with pytest.raises(RecordNotFoundError):
            service.progress_increment("p1", delta=1)

    def test_wrong_kind_progress_inc_on_duration(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordWrongKindError

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        with pytest.raises(RecordWrongKindError):
            service.progress_increment("d1", delta=1)

    def test_wrong_kind_progress_inc_on_event(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordWrongKindError

        _insert_task(db, "e1")
        service.init_record("e1", RecordKind.EVENT, {})
        with pytest.raises(RecordWrongKindError):
            service.progress_increment("e1", delta=1)


# ── session_start / session_end ──────────────────────────────────────────────


class TestSession:
    def test_start_then_end(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record(
            "d1", RecordKind.DURATION, {"target_minutes": 60}
        )
        service.session_start("d1", at="2026-06-10T09:00:00")
        r = service.session_end("d1", at="2026-06-10T09:25:00")
        assert r["this_session_minutes"] == 25
        assert r["derived"]["accumulated_minutes_today"] == 25
        assert r["derived"]["active_session_start_at"] is None

    def test_session_start_reentry(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        # 入参显式 aware,出口走 deploy_timezone(测试 fixture 锁 Asia/Shanghai)。
        service.session_start("d1", at="2026-06-10T09:00:00+08:00")
        r = service.session_start("d1", at="2026-06-10T09:10:00+08:00")
        assert r["already_active"] is True
        assert r["start_at"] == "2026-06-10T09:00:00+08:00"

    def test_session_end_before_start_raises(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        service.session_start("d1", at="2026-06-10T09:00:00")
        with pytest.raises(RecordSchemaError):
            service.session_end("d1", at="2026-06-10T08:00:00")

    def test_session_end_without_start_raises(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        with pytest.raises(RecordSchemaError):
            service.session_end("d1", at="2026-06-10T09:00:00")

    def test_session_accepts_aware_iso_with_offset(self, service, db):
        """CLI 传 +08:00 aware ISO 不能让相减抛 TypeError（R1 回归）。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        # start aware + end aware
        service.session_start("d1", at="2026-06-10T09:00:00+08:00")
        r = service.session_end("d1", at="2026-06-10T09:25:00+08:00")
        assert r["this_session_minutes"] == 25
        # 全 aware,环境无关
        service.session_start("d1", at="2026-06-10T10:00:00+08:00")
        r = service.session_end("d1", at="2026-06-10T10:15:00+08:00")
        assert r["this_session_minutes"] == 15

    def test_wrong_kind_session_on_progress(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordWrongKindError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        with pytest.raises(RecordWrongKindError):
            service.session_start("p1", at="2026-06-10T09:00:00")
        with pytest.raises(RecordWrongKindError):
            service.session_end("p1", at="2026-06-10T09:30:00")

    def test_duration_completed_flip_on_target_reach(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 30})
        service.session_start("d1", at="2026-06-10T09:00:00")
        service.session_end("d1", at="2026-06-10T09:35:00")
        view = service.get_active_record("d1")
        assert view["record"]["status"] == "completed"

    def test_duration_no_target_session_end_does_not_flip(self, service, db):
        """无 target_minutes 时,session-end 累计再多也不能 flip status=completed。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d_open")
        service.init_record("d_open", RecordKind.DURATION, {})
        service.session_start("d_open", at="2026-06-10T09:00:00")
        result = service.session_end("d_open", at="2026-06-10T11:00:00")
        assert result["this_session_minutes"] == 120
        assert result["derived"]["remaining_minutes"] is None
        assert result["derived"]["accumulated_minutes_today"] == 120
        view = service.get_active_record("d_open")
        assert view["record"]["status"] == "active"
        assert view["record"]["target_minutes"] is None


# ── event_append ─────────────────────────────────────────────────────────────


class TestEventAppend:
    def test_append_and_count(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "e1")
        service.init_record("e1", RecordKind.EVENT, {})
        # 入参显式 aware,出口走 deploy_timezone(锁 Asia/Shanghai)。
        service.event_append("e1", "第一次", at="2026-06-10T09:00:00+08:00")
        r = service.event_append("e1", "第二次", at="2026-06-10T11:00:00+08:00")
        assert r["entry_id"] > 0
        assert r["derived"]["count_total"] == 2
        assert r["derived"]["last_at"] == "2026-06-10T11:00:00+08:00"

    def test_empty_description_rejected(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "e1")
        service.init_record("e1", RecordKind.EVENT, {})
        with pytest.raises(RecordSchemaError):
            service.event_append("e1", "", at="2026-06-10T09:00:00")

    def test_wrong_kind_event_append_on_progress(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordWrongKindError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        with pytest.raises(RecordWrongKindError):
            service.event_append("p1", "x", at="2026-06-10T09:00:00")


# ── patch / get / no_active_record ───────────────────────────────────────────


class TestPatch:
    def test_patch_whitelist_field_ok(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        view = service.patch_active_record("p1", {"target": 10, "unit": "次"})
        assert view["record"]["target"] == 10
        assert view["record"]["unit"] == "次"

    def test_patch_forbidden_field_raises(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        for forbidden in ("status", "current", "archived_at", "id", "kind"):
            with pytest.raises(RecordSchemaError):
                service.patch_active_record("p1", {forbidden: "anything"})

    def test_patch_no_active_record(self, service, db):
        from miloco.task_record.service import RecordNotFoundError

        _insert_task(db, "p1")
        with pytest.raises(RecordNotFoundError):
            service.patch_active_record("p1", {"target": 10})

    def test_patch_reject_target_zero(self, service, db):
        """target=0 应被值校验拒。"""
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS,
            {"target": 8, "unit": "杯", "window": "day"},
        )
        with pytest.raises(RecordSchemaError):
            service.patch_active_record("p1", {"target": 0})
        with pytest.raises(RecordSchemaError):
            service.patch_active_record("p1", {"target": -5})

    def test_patch_reject_invalid_window(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS,
            {"target": 8, "unit": "杯", "window": "day"},
        )
        with pytest.raises(RecordSchemaError):
            service.patch_active_record("p1", {"window": "invalid"})

    def test_patch_reject_negative_target_minutes(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "d1")
        service.init_record(
            "d1", RecordKind.DURATION, {"target_minutes": 60}
        )
        with pytest.raises(RecordSchemaError):
            service.patch_active_record("d1", {"target_minutes": -5})
        with pytest.raises(RecordSchemaError):
            service.patch_active_record("d1", {"target_minutes": 0})

    def test_patch_target_lowered_flips_completed(self, service, db):
        """progress target 调低到 ≤ current 时,status 应 flip 为 completed。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS,
            {"target": 8, "unit": "杯", "window": "day"},
        )
        service.progress_increment("p1", delta=5)
        # current=5,target=8,status=active
        view = service.patch_active_record("p1", {"target": 5})
        assert view["record"]["status"] == "completed"

    def test_patch_target_raised_unflips_completed(self, service, db):
        """progress target 调高到 > current 时,status 应 flip 回 active。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS,
            {"target": 3, "unit": "杯", "window": "day"},
        )
        service.progress_increment("p1", delta=3)
        # 达标 → completed
        with db.get_connection() as conn:
            status_before = conn.execute(
                "SELECT status FROM task_record_progress WHERE task_id='p1'"
            ).fetchone()[0]
        assert status_before == "completed"

        view = service.patch_active_record("p1", {"target": 10})
        assert view["record"]["status"] == "active"

    def test_patch_duration_target_lowered_flips_completed(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record(
            "d1", RecordKind.DURATION, {"target_minutes": 60}
        )
        service.session_start("d1", at="2026-06-10T09:00:00")
        service.session_end("d1", at="2026-06-10T09:30:00")
        # accumulated=30, target=60, status=active
        view = service.patch_active_record("d1", {"target_minutes": 20})
        assert view["record"]["status"] == "completed"

    def test_patch_duration_target_null_keeps_active(self, service, db):
        """duration target_minutes 改 null 时 status 永远 active(无达标语义)。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record(
            "d1", RecordKind.DURATION, {"target_minutes": 30}
        )
        service.session_start("d1", at="2026-06-10T09:00:00")
        service.session_end("d1", at="2026-06-10T09:35:00")
        # completed
        with db.get_connection() as conn:
            status_before = conn.execute(
                "SELECT status FROM task_record_duration WHERE task_id='d1'"
            ).fetchone()[0]
        assert status_before == "completed"

        view = service.patch_active_record("d1", {"target_minutes": None})
        assert view["record"]["status"] == "active"
        assert view["record"]["target_minutes"] is None

    def test_get_no_active_record(self, service, db):
        from miloco.task_record.service import RecordNotFoundError

        _insert_task(db, "p1")
        with pytest.raises(RecordNotFoundError):
            service.get_active_record("p1")


# ── terminate_log ────────────────────────────────────────────────────────────


class TestTerminateLog:
    def test_write_progress_snapshot(self, service, db):
        from miloco.task_record.schema import RecordKind, TerminateReason

        _insert_task(db, "p1", description="喝水")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        service.progress_increment("p1", delta=5)
        service.write_terminate_log("p1", TerminateReason.ABANDONED)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_terminate_log WHERE task_id = ?", ("p1",)
            ).fetchone()
        assert row is not None
        assert row["kind"] == "progress"
        assert row["reason"] == "abandoned"
        assert row["description"] == "喝水"
        snapshot = json.loads(row["final_snapshot"])
        assert snapshot == {"target": 8, "current": 5, "unit": "杯", "window": "day"}

    def test_write_duration_snapshot(self, service, db):
        from miloco.task_record.schema import RecordKind, TerminateReason

        _insert_task(db, "d1", description="看书")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        service.session_start("d1", at="2026-06-10T09:00:00")
        service.session_end("d1", at="2026-06-10T09:30:00")
        service.write_terminate_log("d1", TerminateReason.COMPLETED)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT final_snapshot FROM task_terminate_log WHERE task_id = ?",
                ("d1",),
            ).fetchone()
        snapshot = json.loads(row["final_snapshot"])
        # session 已 end(无 in-flight),accumulated=30,in_flight=0
        assert snapshot["target_minutes"] == 60
        assert snapshot["accumulated_minutes"] == 30
        assert snapshot["in_flight_minutes"] == 0
        assert snapshot["active_session_start_at"] is None

    def test_write_duration_snapshot_with_in_flight_session(self, service, db):
        """有未 end 的 active session 时,snapshot 应含 in_flight 时长,
        否则 delete 级联清后这段时长丢失。"""
        from miloco.task_record.schema import RecordKind, TerminateReason

        _insert_task(db, "d_inflight", description="看电视")
        service.init_record(
            "d_inflight", RecordKind.DURATION, {"target_minutes": 120}
        )
        service.session_start("d_inflight", at="2026-06-10T09:00:00")
        # 不 end,直接 terminate
        service.write_terminate_log("d_inflight", TerminateReason.ABANDONED)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT final_snapshot FROM task_terminate_log WHERE task_id = ?",
                ("d_inflight",),
            ).fetchone()
        snapshot = json.loads(row["final_snapshot"])
        # in_flight_minutes 应该 > 0(从 9:00 算到 now)
        assert snapshot["target_minutes"] == 120
        assert snapshot["in_flight_minutes"] > 0
        assert snapshot["accumulated_minutes"] == snapshot["in_flight_minutes"]
        assert snapshot["active_session_start_at"] is not None

    def test_write_duration_snapshot_no_target(self, service, db):
        """无 target_minutes 时,final_snapshot.target_minutes 应为 None。"""
        from miloco.task_record.schema import RecordKind, TerminateReason

        _insert_task(db, "d_open", description="健身追踪")
        service.init_record("d_open", RecordKind.DURATION, {})
        service.session_start("d_open", at="2026-06-10T09:00:00")
        service.session_end("d_open", at="2026-06-10T10:30:00")
        service.write_terminate_log("d_open", TerminateReason.ABANDONED)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT final_snapshot FROM task_terminate_log WHERE task_id = ?",
                ("d_open",),
            ).fetchone()
        snapshot = json.loads(row["final_snapshot"])
        assert snapshot["target_minutes"] is None
        assert snapshot["accumulated_minutes"] == 90
        assert snapshot["in_flight_minutes"] == 0
        assert snapshot["active_session_start_at"] is None

    def test_write_event_snapshot(self, service, db):
        from miloco.task_record.schema import RecordKind, TerminateReason

        _insert_task(db, "e1", description="打卡")
        service.init_record("e1", RecordKind.EVENT, {})
        # 入参显式 aware,出口走 deploy_timezone(锁 Asia/Shanghai)。
        service.event_append("e1", "一", at="2026-06-10T09:00:00+08:00")
        service.event_append("e1", "二", at="2026-06-10T11:00:00+08:00")
        service.write_terminate_log("e1", TerminateReason.EXPIRED)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT final_snapshot FROM task_terminate_log WHERE task_id = ?",
                ("e1",),
            ).fetchone()
        snapshot = json.loads(row["final_snapshot"])
        assert snapshot["event_count"] == 2
        assert snapshot["first_at"] == "2026-06-10T09:00:00+08:00"
        assert snapshot["last_at"] == "2026-06-10T11:00:00+08:00"

    def test_prune_30_days(self, service, db):
        from miloco.task_record.repo import TerminateLogRepo
        from miloco.task_record.schema import RecordKind

        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            for at in (
                "2026-05-01T09:00:00",  # 40 天前（相对 2026-06-10）
                "2026-05-20T09:00:00",  # 21 天前
                "2026-06-09T09:00:00",  # 1 天前
            ):
                TerminateLogRepo.insert(
                    cursor,
                    task_id="x",
                    kind=RecordKind.PROGRESS,
                    reason="completed",
                    description="x",
                    final_snapshot={},
                    terminated_at=at,
                )
            conn.commit()

        # 模拟 now = 2026-06-10 → cutoff = 2026-05-11
        deleted = service.prune_terminate_log(now=datetime(2026, 6, 10, 12, 0, 0))
        assert deleted == 1

        with db.get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM task_terminate_log"
            ).fetchone()["n"]
        assert n == 2


# ── rollover_one ─────────────────────────────────────────────────────────────


class TestTaskPausedNoop:
    """B5 回归：task.status='paused' 时所有 mutate op 返 noop，不写脏数据。"""

    def _pause(self, db, task_id):
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task SET status='paused', paused_at='2026-06-10T09:00:00+08:00' "
                "WHERE task_id=?",
                (task_id,),
            )
            conn.commit()

    def test_progress_inc_noop_when_paused(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        service.progress_increment("p1", delta=3)
        self._pause(db, "p1")
        r = service.progress_increment("p1", delta=1)
        assert r["noop"] is True
        assert r["reason"] == "task_paused"
        assert r["current"] == 3  # 没累加
        view = service.get_active_record("p1")
        assert view["record"]["current"] == 3  # DB 没改

    def test_event_append_noop_when_paused(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "e1")
        service.init_record("e1", RecordKind.EVENT, {})
        service.event_append("e1", "a", at="2026-06-10T09:00:00+08:00")
        self._pause(db, "e1")
        r = service.event_append("e1", "b", at="2026-06-10T10:00:00+08:00")
        assert r["noop"] is True
        assert r["reason"] == "task_paused"
        assert r["derived"]["count_total"] == 1  # b 没写入

    def test_session_start_noop_when_paused(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        self._pause(db, "d1")
        r = service.session_start("d1", at="2026-06-10T09:00:00+08:00")
        assert r["noop"] is True
        assert r["reason"] == "task_paused"
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT active_session_start_at FROM task_record_duration "
                "WHERE task_id='d1' AND archived_at IS NULL"
            ).fetchone()
        assert row["active_session_start_at"] is None  # 没写入

    def test_session_end_noop_when_paused(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        service.session_start("d1", at="2026-06-10T09:00:00+08:00")
        self._pause(db, "d1")
        r = service.session_end("d1", at="2026-06-10T09:30:00+08:00")
        assert r["noop"] is True
        assert r["reason"] == "task_paused"
        # active_session_start_at 仍在（没被清）；duration_session 子表没新行
        with db.get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM task_record_duration_session WHERE task_id='d1'"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT active_session_start_at FROM task_record_duration "
                "WHERE task_id='d1' AND archived_at IS NULL"
            ).fetchone()
        assert n == 0
        # v10 起 DB 内是 INTEGER ms (UTC 绝对时刻),直接比 ms 值
        from miloco.utils.time_utils import iso_to_ms

        assert row["active_session_start_at"] == iso_to_ms(
            "2026-06-10T09:00:00+08:00"
        )

    def test_active_task_still_works(self, service, db):
        """non-regression：未 pause 时正常 mutate。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        r = service.progress_increment("p1", delta=1)
        assert r.get("noop") is None or r.get("noop") is False
        assert r["current"] == 1


class TestComputeStrict:
    def test_progress_with_date_returns_archived_snapshot(self, service, db):
        from miloco.task_record.repo import ProgressRepo
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1",
            RecordKind.PROGRESS,
            {
                "target": 8,
                "unit": "杯",
                "window": "day",
                "recurring_pattern": {"window": "day"},
            },
        )
        service.progress_increment("p1", delta=5)
        # 模拟昨日归档:archived_at 与 date 用 UTC 视角同步,测试与 deploy_timezone 解耦。
        # date='2026-06-09' 走 _local_date_to_ms_range 按 deploy_timezone 解读,这里
        # 把 archived_at 设到 deploy 视角下 2026-06-09 的中点(中午 12:00 当地)以兼容
        # 任意 deploy_timezone (UTC / Asia/Shanghai 等都落在该日内)。
        from datetime import datetime, timedelta

        from miloco.utils.time_utils import deploy_timezone

        local_noon = datetime(
            2026, 6, 9, 12, 0, 0, tzinfo=deploy_timezone()
        )
        archived_ms = int(local_noon.timestamp() * 1000)
        next_local_noon = local_noon + timedelta(days=1)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task_record_progress SET archived_at = ? "
                "WHERE task_id = 'p1'",
                (archived_ms,),
            )
            cursor = conn.cursor()
            ProgressRepo.insert_active(
                cursor,
                task_id="p1",
                target=8,
                unit="杯",
                window="day",
                recurring_pattern={"window": "day"},
                expires_at=None,
                now=next_local_noon.strftime("%Y-%m-%dT%H:%M:%S%z"),
            )
            conn.commit()

        # date='2026-06-09' 命中(deploy_timezone 视角某天)
        r = service.compute_derived("p1", window="all", date="2026-06-09")
        assert r["kind"] == "progress"
        assert r["derived"]["remaining"] == 3  # 8 - 5

    def test_progress_with_invalid_window_raises(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        with pytest.raises(RecordSchemaError):
            service.compute_derived("p1", window="week")
        with pytest.raises(RecordSchemaError):
            service.compute_derived("p1", window="month")

    def test_compute_rejects_unknown_window(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        with pytest.raises(RecordSchemaError):
            service.compute_derived("p1", window="garbage")

    def test_compute_rejects_date_with_non_all_window(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        with pytest.raises(RecordSchemaError):
            service.compute_derived("p1", window="day", date="2026-06-09")

    def test_event_with_date_filters_entries(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "e1")
        service.init_record("e1", RecordKind.EVENT, {})
        service.event_append("e1", "yesterday-a", at="2026-06-09T08:00:00+08:00")
        service.event_append("e1", "yesterday-b", at="2026-06-09T20:00:00+08:00")
        service.event_append("e1", "today", at="2026-06-10T09:00:00+08:00")

        r = service.compute_derived("e1", date="2026-06-09")
        assert r["derived"]["count_total"] == 2
        assert r["derived"]["last_at"] == "2026-06-09T20:00:00+08:00"

    def test_duration_rejects_unsupported_window(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        with pytest.raises(RecordSchemaError):
            service.compute_derived("d1", window="week")

    def test_duration_no_target_compute_returns_null_remaining(self, service, db):
        """无 target_minutes 时 compute 当前活跃行,remaining_minutes 应为 None。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d_open")
        service.init_record("d_open", RecordKind.DURATION, {})
        service.session_start("d_open", at="2026-06-10T09:00:00")
        service.session_end("d_open", at="2026-06-10T09:45:00")
        r = service.compute_derived("d_open", window="all")
        assert r["derived"]["accumulated_minutes_today"] == 45
        assert r["derived"]["remaining_minutes"] is None


class TestComputeRange:
    """G1：区间 compute --from --to 聚合。"""

    def test_progress_range_sums_archive_currents(self, service, db):
        from miloco.task_record.repo import ProgressRepo
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        # 模拟 7 天的 archive 数据（rollover 每天归档一次，current 不同）
        with db.get_connection() as conn:
            cursor = conn.cursor()
            for day, current in [
                ("2026-06-01", 6),
                ("2026-06-02", 8),
                ("2026-06-03", 5),
                ("2026-06-04", 7),
                ("2026-06-05", 8),
                ("2026-06-06", 4),
                ("2026-06-07", 8),
            ]:
                cursor.execute("BEGIN")
                ProgressRepo.insert_active(
                    cursor,
                    task_id="p1",
                    target=8,
                    unit="杯",
                    window="day",
                    recurring_pattern={"window": "day"},
                    expires_at=None,
                    now=f"{day}T00:00:00+08:00",
                )
                ProgressRepo.update_progress(
                    cursor,
                    task_id="p1",
                    new_current=current,
                    new_status="active",
                    now=f"{day}T23:00:00+08:00",
                )
                ProgressRepo.archive_active(
                    cursor, task_id="p1", archived_at=f"{day}T23:59:00+08:00"
                )
                conn.commit()
        # 当前活跃行（今日）
        service.init_record(
            "p1",
            RecordKind.PROGRESS,
            {"target": 10, "unit": "杯", "window": "day"},
        )

        # 区间 06-01 ~ 06-07：sum=6+8+5+7+8+4+8=46，7 天有数据，target_recent=8（最近一日归档）
        r = service.compute_range("p1", from_date="2026-06-01", to_date="2026-06-07")
        assert r["kind"] == "progress"
        assert r["derived"]["days_with_data"] == 7
        assert r["derived"]["total_current"] == 46
        assert r["derived"]["target_recent"] == 8

    def test_event_range_counts_entries(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "e1")
        service.init_record("e1", RecordKind.EVENT, {})
        # 跨 3 天的 entry
        service.event_append("e1", "a", at="2026-06-05T09:00:00+08:00")
        service.event_append("e1", "b", at="2026-06-05T15:00:00+08:00")
        service.event_append("e1", "c", at="2026-06-06T10:00:00+08:00")
        service.event_append("e1", "d", at="2026-06-08T11:00:00+08:00")

        r = service.compute_range("e1", from_date="2026-06-05", to_date="2026-06-06")
        assert r["derived"]["total_count"] == 3
        assert r["derived"]["days_with_data"] == 2

        # 单日
        r = service.compute_range("e1", from_date="2026-06-08", to_date="2026-06-08")
        assert r["derived"]["total_count"] == 1
        assert r["derived"]["days_with_data"] == 1

    def test_duration_range_sums_session_minutes(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        # 2 个 session 跨 2 天
        service.session_start("d1", at="2026-06-05T09:00:00+08:00")
        service.session_end("d1", at="2026-06-05T09:30:00+08:00")  # 30 min
        service.session_start("d1", at="2026-06-06T14:00:00+08:00")
        service.session_end("d1", at="2026-06-06T15:15:00+08:00")  # 75 min

        r = service.compute_range("d1", from_date="2026-06-05", to_date="2026-06-06")
        assert r["derived"]["total_minutes"] == 105
        assert r["derived"]["days_with_data"] == 2
        assert r["derived"]["target_minutes_recent"] == 60

    def test_range_invalid_from_gt_to(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        with pytest.raises(RecordSchemaError):
            service.compute_range("p1", from_date="2026-06-10", to_date="2026-06-01")

    def test_range_invalid_date_format(self, service, db):
        from miloco.task_record.schema import RecordKind
        from miloco.task_record.service import RecordSchemaError

        _insert_task(db, "p1")
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )
        with pytest.raises(RecordSchemaError):
            service.compute_range("p1", from_date="yesterday", to_date="2026-06-10")


class TestListArchives:
    """G2：archive list 接口。"""

    def test_list_progress_archives(self, service, db):
        from miloco.task_record.repo import ProgressRepo
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        with db.get_connection() as conn:
            cursor = conn.cursor()
            for day, current in [("2026-06-08", 5), ("2026-06-09", 8)]:
                cursor.execute("BEGIN")
                ProgressRepo.insert_active(
                    cursor,
                    task_id="p1",
                    target=8,
                    unit="杯",
                    window="day",
                    recurring_pattern={"window": "day"},
                    expires_at=None,
                    now=f"{day}T00:00:00+08:00",
                )
                ProgressRepo.update_progress(
                    cursor,
                    task_id="p1",
                    new_current=current,
                    new_status="active" if current < 8 else "completed",
                    now=f"{day}T23:00:00+08:00",
                )
                ProgressRepo.archive_active(
                    cursor, task_id="p1", archived_at=f"{day}T23:59:00+08:00"
                )
                conn.commit()
        # 当前活跃行
        service.init_record(
            "p1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
        )

        r = service.list_archives("p1")
        assert r["kind"] == "progress"
        assert len(r["archives"]) == 2
        # 按 DESC 排序
        assert r["archives"][0]["date"] == "2026-06-09"
        assert r["archives"][0]["current"] == 8
        assert r["archives"][0]["status"] == "completed"
        assert r["archives"][1]["date"] == "2026-06-08"
        assert r["archives"][1]["current"] == 5

    def test_list_event_archives_grouped_by_day(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "e1")
        service.init_record("e1", RecordKind.EVENT, {})
        for at in (
            "2026-06-05T09:00:00+08:00",
            "2026-06-05T15:00:00+08:00",
            "2026-06-06T10:00:00+08:00",
        ):
            service.event_append("e1", "x", at=at)

        r = service.list_archives("e1")
        assert r["kind"] == "event"
        assert len(r["archives"]) == 2
        # DESC
        assert r["archives"][0] == {"date": "2026-06-06", "count": 1}
        assert r["archives"][1] == {"date": "2026-06-05", "count": 2}

    def test_list_duration_archives_grouped_by_day(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record("d1", RecordKind.DURATION, {"target_minutes": 60})
        service.session_start("d1", at="2026-06-05T09:00:00+08:00")
        service.session_end("d1", at="2026-06-05T09:30:00+08:00")
        service.session_start("d1", at="2026-06-06T14:00:00+08:00")
        service.session_end("d1", at="2026-06-06T14:45:00+08:00")

        r = service.list_archives("d1")
        assert r["kind"] == "duration"
        assert len(r["archives"]) == 2
        assert r["archives"][0]["date"] == "2026-06-06"
        assert r["archives"][0]["accumulated_minutes"] == 45
        assert r["archives"][1]["accumulated_minutes"] == 30


class TestRolloverOne:
    def test_rollover_progress_resets_current(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1",
            RecordKind.PROGRESS,
            {
                "target": 8,
                "unit": "杯",
                "window": "day",
                "recurring_pattern": {"day": "*"},
            },
        )
        service.progress_increment("p1", delta=3)

        service.rollover_one(
            "p1", RecordKind.PROGRESS, now=datetime(2026, 6, 11, 0, 5, 0)
        )

        with db.get_connection() as conn:
            rows = list(
                conn.execute(
                    "SELECT current, archived_at FROM task_record_progress "
                    "WHERE task_id = ? ORDER BY id",
                    ("p1",),
                )
            )
        assert len(rows) == 2
        assert rows[0]["archived_at"] is not None
        assert rows[1]["archived_at"] is None
        assert rows[1]["current"] == 0

    def test_rollover_duration_with_active_session_splits_two(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d1")
        service.init_record(
            "d1",
            RecordKind.DURATION,
            {"target_minutes": 60, "recurring_pattern": {"day": "*"}},
        )
        service.session_start("d1", at="2026-06-10T23:30:00")

        service.rollover_one(
            "d1", RecordKind.DURATION, now=datetime(2026, 6, 11, 0, 5, 0)
        )

        with db.get_connection() as conn:
            sessions = list(
                conn.execute(
                    "SELECT * FROM task_record_duration_session "
                    "WHERE task_id = ? ORDER BY id",
                    ("d1",),
                )
            )
            new_main = conn.execute(
                "SELECT active_session_start_at FROM task_record_duration "
                "WHERE task_id = ? AND archived_at IS NULL",
                ("d1",),
            ).fetchone()

        assert len(sessions) == 1
        # v10 起 raw DB 是 INTEGER ms
        from miloco.utils.time_utils import iso_to_ms, ms_to_aware_dt

        assert sessions[0]["start_at"] == iso_to_ms("2026-06-10T23:30:00")
        end_local = ms_to_aware_dt(sessions[0]["end_at"]).strftime(
            "%Y-%m-%dT%H:%M"
        )
        assert end_local == "2026-06-11T00:05"
        assert sessions[0]["duration_seconds"] == 35 * 60
        assert sessions[0]["archived_at"] is not None
        active_local = ms_to_aware_dt(
            new_main["active_session_start_at"]
        ).strftime("%Y-%m-%dT%H:%M")
        assert active_local == "2026-06-11T00:05"

    def test_rollover_event_is_noop(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "e1")
        service.init_record("e1", RecordKind.EVENT, {})
        service.event_append("e1", "x", at="2026-06-10T09:00:00")

        service.rollover_one(
            "e1", RecordKind.EVENT, now=datetime(2026, 6, 11, 0, 5, 0)
        )

        with db.get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM task_record_event WHERE task_id = ?",
                ("e1",),
            ).fetchone()["n"]
        assert n == 1

    def test_rollover_skips_non_recurring(self, service, db):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p1")
        service.init_record(
            "p1",
            RecordKind.PROGRESS,
            {"target": 8, "unit": "杯", "window": "longterm"},  # 无 recurring_pattern
        )
        service.progress_increment("p1", delta=3)

        service.rollover_one(
            "p1", RecordKind.PROGRESS, now=datetime(2026, 6, 11, 0, 5, 0)
        )

        with db.get_connection() as conn:
            rows = list(
                conn.execute(
                    "SELECT current, archived_at FROM task_record_progress "
                    "WHERE task_id = ?",
                    ("p1",),
                )
            )
        assert len(rows) == 1
        assert rows[0]["current"] == 3


class TestDeriveDurationPrecisionRegression:
    """_derive_duration 应返回 10min（651s // 60）而非 5min。

    memory project_task_record_duration_floor_precision 复现：9 段全活跃
    session 总秒数 651。旧实现 SUM(duration_minutes)=5min（每段 floor 丢秒
    累积），新实现 SUM(duration_seconds)=651 后 // 60 = 10min。
    """

    def test_compute_derived_returns_floor_of_total_seconds(
        self, service, db
    ):
        from datetime import datetime, timezone

        from miloco.task_record.repo import DurationRepo
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "phone_usage_precision")
        service.init_record(
            "phone_usage_precision", RecordKind.DURATION, {}
        )

        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            base = datetime(
                2026, 6, 13, 10, 0, 0, tzinfo=timezone(timedelta(hours=8))
            )
            t = base
            for sec in [60, 73, 65, 80, 70, 78, 67, 75, 83]:
                end_t = t + timedelta(seconds=sec)
                DurationRepo.insert_session(
                    cursor,
                    task_id="phone_usage_precision",
                    start_at=t.isoformat(timespec="seconds"),
                    end_at=end_t.isoformat(timespec="seconds"),
                    duration_seconds=sec,
                )
                t = end_t
            conn.commit()

        result = service.compute_derived(task_id="phone_usage_precision")
        # 651s // 60 = 10min
        assert result["derived"]["accumulated_minutes_today"] == 10


# ============================================================
# recurring task 永不翻 completed
# ============================================================


class TestRecurringNeverCompleted:
    """recurring task 的 status 永远保持 active，达 target 不翻 completed。

    recurring 的语义是循环（每天/每周/每月重置），没有"完成"终点。
    本周期内"已达标，不重复通知" 由达标条件项自身的值承担（已经是真就产不出新
    边沿），跨周期 rollover 把它翻假；不污染 DB status 字段。
    """

    def test_progress_recurring_keeps_active_when_target_reached(
        self, service, db
    ):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p_recur")
        service.init_record(
            "p_recur",
            RecordKind.PROGRESS,
            {
                "target": 3, "unit": "杯", "window": "day",
                "recurring_pattern": {"window": "day"},
            },
        )
        r = service.progress_increment("p_recur", delta=10)
        assert r["current"] == 3
        # recurring → 永远 active
        assert r["status"] == "active"

    def test_progress_oneshot_still_flips_to_completed(self, service, db):
        """对照：oneshot（无 recurring_pattern）仍按旧逻辑翻 completed。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p_once")
        service.init_record(
            "p_once",
            RecordKind.PROGRESS,
            {"target": 3, "unit": "杯", "window": "day"},
        )
        r = service.progress_increment("p_once", delta=10)
        assert r["status"] == "completed"

    def test_duration_recurring_keeps_active_when_target_reached(
        self, service, db
    ):
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d_recur")
        service.init_record(
            "d_recur",
            RecordKind.DURATION,
            {
                "target_minutes": 30,
                "recurring_pattern": {"window": "day"},
            },
        )
        service.session_start("d_recur", at="2026-06-10T09:00:00")
        service.session_end("d_recur", at="2026-06-10T09:35:00")
        view = service.get_active_record("d_recur")
        # recurring → 永远 active
        assert view["record"]["status"] == "active"
        # 累计仍正确
        assert view["derived"]["accumulated_minutes_today"] == 35

    def test_duration_oneshot_still_flips_to_completed(self, service, db):
        """对照：oneshot duration（无 recurring_pattern）仍按旧逻辑翻 completed。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d_once")
        service.init_record(
            "d_once", RecordKind.DURATION, {"target_minutes": 30}
        )
        service.session_start("d_once", at="2026-06-10T09:00:00")
        service.session_end("d_once", at="2026-06-10T09:35:00")
        view = service.get_active_record("d_once")
        assert view["record"]["status"] == "completed"

    def test_progress_recurring_patch_target_keeps_active(self, service, db):
        """recurring + patch target 调低到 ≤ current 也不该翻 completed。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "p_patch")
        service.init_record(
            "p_patch",
            RecordKind.PROGRESS,
            {
                "target": 10, "unit": "杯", "window": "day",
                "recurring_pattern": {"window": "day"},
            },
        )
        service.progress_increment("p_patch", delta=5)
        # patch target 调低到 3（current=5 > target=3）
        view = service.patch_active_record("p_patch", {"target": 3})
        assert view["record"]["status"] == "active"

    def test_duration_recurring_patch_target_keeps_active(self, service, db):
        """recurring duration + patch target_minutes 调低也不翻 completed。"""
        from miloco.task_record.schema import RecordKind

        _insert_task(db, "d_patch")
        service.init_record(
            "d_patch",
            RecordKind.DURATION,
            {
                "target_minutes": 120,
                "recurring_pattern": {"window": "day"},
            },
        )
        service.session_start("d_patch", at="2026-06-10T09:00:00")
        service.session_end("d_patch", at="2026-06-10T10:00:00")  # 60min
        # patch target 调低到 30（accumulated=60 > target=30）
        view = service.patch_active_record("d_patch", {"target_minutes": 30})
        assert view["record"]["status"] == "active"
