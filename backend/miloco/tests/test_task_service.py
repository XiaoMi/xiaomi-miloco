# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""TaskService 业务流测试 (v2)。

流程:
1. ``service.create_task(req)`` 仅占位
2. ``RuleRepo().create(rule)`` 只写 rule 表 (rule.task_id FK 挂载)
3. cron 引用直接 INSERT cron 表 (v2: cron.task_id FK CASCADE, dispatch_owner='external')

PendingOp 只含 cron kind; delete 触发 task_terminate_log。
"""

import pytest
from miloco.database.rule_repo import RuleRepo
from miloco.database.task_repo import TaskConflict
from miloco.rule.schema import (
    Rule,
    RuleCondition,
    RuleLifecycle,
    RuleMode,
)
from miloco.task.schema import TaskCreateRequest, TaskUpdateRequest


def _insert_external_cron(task_id: str, cron_id: str) -> None:
    """测试辅助: 直接往 cron 表塞一条 external 引用行 (模拟老 openclaw cron 挂钩)."""
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO cron (cron_id, task_id, dispatch_owner, enabled, "
            "created_at, updated_at) VALUES (?, ?, 'external', 1, 0, 0)",
            (cron_id, task_id),
        )
        conn.commit()


@pytest.fixture
def real_db(tmp_path, monkeypatch):
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
def service(real_db):
    from miloco.task.service import TaskService

    return TaskService(rule_repo=RuleRepo())


def _make_rule_obj(task_id="t1", name=None, query="客厅有人") -> Rule:
    return Rule(
        name=name or f"[{task_id}] r",
        task_id=task_id,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        condition=RuleCondition(perceive_device_ids=["d1"], query=query),
        actions=[],
        action_descriptions=["fire"],
    )


def _setup_task_with_rule(service, task_id="t1", description="d", query="客厅有人"):
    """方案 P 下的标准建 task 流程：先 task → 再 rule（自动 link）。"""
    service.create_task(TaskCreateRequest(task_id=task_id, description=description))
    rule_id = RuleRepo().create(_make_rule_obj(task_id=task_id, query=query))
    return rule_id


def test_create_task_then_rule_auto_links(service):
    """rule create 只写 rule 表; task view.links 从 rule.task_id backfill."""
    service.create_task(TaskCreateRequest(task_id="t1", description="客厅有人开灯"))
    rule_id = RuleRepo().create(_make_rule_obj(task_id="t1", query="客厅有人"))

    view = service.get_full_view("t1")
    assert view.task_id == "t1"
    assert view.description == "客厅有人开灯"
    assert view.status == "active"
    assert len(view.rule_briefs) == 1
    assert view.rule_briefs[0].rule_id == rule_id
    assert view.rule_briefs[0].query == "客厅有人"
    # links 字段兼容 backfill: rule.task_id → links[kind='rule']
    assert any(link.kind == "rule" and link.ref == rule_id for link in view.links)


def test_create_task_409_on_duplicate_id(service):
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    with pytest.raises(TaskConflict):
        service.create_task(TaskCreateRequest(task_id="t1", description="d2"))


def test_disable_task_marks_meta_paused_and_disables_rules(service):
    rid = _setup_task_with_rule(service)
    result = service.disable_task("t1")
    assert result.status == "paused"
    assert result.backend_synced.meta_status == "ok"
    assert result.backend_synced.rules[0].rule_id == rid
    assert RuleRepo().get_by_id(rid).enabled is False


def test_disable_pending_ops_for_cron_only(service):
    """disable 返回的 agent_pending 仅含 cron。"""
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    _insert_external_cron("t1", "job-001")
    result = service.disable_task("t1")
    kinds = {op.kind for op in result.agent_pending}
    assert kinds == {"cron"}
    assert all(op.action == "disable" for op in result.agent_pending)


def test_enable_pending_ops_cron_only(service):
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    _insert_external_cron("t1", "job-001")
    service.disable_task("t1")
    result = service.enable_task("t1")
    assert result.status == "active"
    actions = {op.action for op in result.agent_pending}
    assert actions == {"enable"}


def test_delete_task_writes_terminate_log_and_cascade(service, real_db):
    """delete 事务先写 task_terminate_log, FK CASCADE 清 rule / cron / task_record_*."""
    from miloco.database.connector import get_db_connector
    from miloco.task_record.schema import RecordKind
    from miloco.task_record.service import TaskRecordService

    rid = _setup_task_with_rule(service)
    _insert_external_cron("t1", "job-001")
    rec_svc = TaskRecordService()
    rec_svc.init_record(
        "t1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
    )
    rec_svc.progress_increment("t1", delta=3)

    result = service.delete_task("t1", reason="abandoned")
    assert result is not None
    assert result.backend_synced.rules_deleted == [rid]
    # agent_pending 仅 cron
    assert {op.kind for op in result.agent_pending} == {"cron"}

    with get_db_connector().get_connection() as conn:
        log_rows = list(
            conn.execute(
                "SELECT reason, kind, description FROM task_terminate_log WHERE task_id='t1'"
            )
        )
        assert len(log_rows) == 1
        assert log_rows[0]["reason"] == "abandoned"
        assert log_rows[0]["kind"] == "progress"
        # task / rule / cron / task_record_progress 全部清空 (FK CASCADE)
        for tbl in ("task", "rule", "cron", "task_record_progress"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE task_id='t1'"
            ).fetchone()[0]
            assert n == 0, f"{tbl} not cleaned"


def test_delete_task_default_reason_completed(service):
    """``reason`` 默认 completed，无 record 时不阻塞 delete。"""
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    result = service.delete_task("t1")
    assert result is not None


def test_delete_task_not_found_returns_none(service):
    assert service.delete_task("nope") is None


def test_update_description(service):
    service.create_task(TaskCreateRequest(task_id="t1", description="old"))
    ok = service.update_description("t1", TaskUpdateRequest(description="new"))
    assert ok is True
    view = service.get_full_view("t1")
    assert view.description == "new"


def test_list_for_dedupe(service):
    _setup_task_with_rule(service, task_id="t1", query="q1")
    service.create_task(TaskCreateRequest(task_id="t2", description="d2"))
    RuleRepo().create(_make_rule_obj(task_id="t2", name="[t2] r", query="q2"))

    items = service.list_for_dedupe()
    assert {v.task_id for v in items} == {"t1", "t2"}


def test_delete_task_is_atomic_on_mid_failure(service, real_db, monkeypatch):
    """B1 回归：delete_task 单事务化——中途异常时 terminate_log / rule / task 全部回滚。"""
    from miloco.database.connector import get_db_connector
    from miloco.database.task_repo import TaskRepo
    from miloco.task_record.schema import RecordKind
    from miloco.task_record.service import TaskRecordService

    rid = _setup_task_with_rule(service)
    rec_svc = TaskRecordService()
    rec_svc.init_record(
        "t1", RecordKind.PROGRESS, {"target": 8, "unit": "杯", "window": "day"}
    )

    # 在 TaskRepo.delete_task_in_tx 阶段制造异常
    original = TaskRepo.delete_task_in_tx

    def faulty(cursor, task_id):
        raise RuntimeError("simulated mid-transaction failure")

    monkeypatch.setattr(TaskRepo, "delete_task_in_tx", staticmethod(faulty))

    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        service.delete_task("t1", reason="abandoned")

    monkeypatch.setattr(TaskRepo, "delete_task_in_tx", original)

    # 全部回滚：terminate_log 未写、rule 还在、task 还在
    with get_db_connector().get_connection() as conn:
        log_count = conn.execute(
            "SELECT COUNT(*) FROM task_terminate_log WHERE task_id='t1'"
        ).fetchone()[0]
        rule_exists = conn.execute(
            "SELECT 1 FROM rule WHERE id=?", (rid,)
        ).fetchone()
        task_exists = conn.execute(
            "SELECT 1 FROM task WHERE task_id='t1'"
        ).fetchone()
    assert log_count == 0
    assert rule_exists is not None
    assert task_exists is not None


def test_dangling_rule_link_no_op_after_v2(service):
    """v2 后 rule.task_id 是权威源, rule 行删则 list_by_task 直接不返回 → rule_briefs 空。"""
    rid = _setup_task_with_rule(service)
    RuleRepo().delete(rid)
    view = service.get_full_view("t1")
    assert view.rule_briefs == []
