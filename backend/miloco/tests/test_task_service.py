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
    RuleDirection,
    RuleLifecycle,
    RuleMode,
    TriggerOutcome,
)
from miloco.task.schema import CronRef, TaskCreateRequest, TaskUpdateRequest


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


def _insert_internal_cron(task_id: str, cron_id: str) -> None:
    """测试辅助: 直接往 cron 表塞一条 internal cron 行 (由 backend APScheduler 管)."""
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO cron (cron_id, task_id, dispatch_owner, name, kind, "
            "cron_expr, message, enabled, created_at, updated_at) VALUES "
            "(?, ?, 'internal', 'test', 'cron', '0 * * * *', 'msg', 1, 0, 0)",
            (cron_id, task_id),
        )
        conn.commit()


class _StubRunner:
    """替代 ScheduleRunner: 记录 apply/remove 调用不实际启动 APScheduler."""

    def __init__(self):
        self.apply_calls: list = []
        self.remove_calls: list = []

    def apply_enabled_state(self, cron):
        self.apply_calls.append((cron.cron_id, cron.enabled))

    def remove_job(self, cron_id):
        self.remove_calls.append(cron_id)


@pytest.fixture
def stub_runner(monkeypatch):
    """把 miloco.schedule.runner.get_runner 换成 stub."""
    from miloco.schedule import runner as runner_module

    stub = _StubRunner()
    monkeypatch.setattr(runner_module, "get_runner", lambda: stub)
    return stub


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
    """rule create 只写 rule 表; task view.rule_briefs 从 rule.task_id backfill."""
    service.create_task(TaskCreateRequest(task_id="t1", description="客厅有人开灯"))
    rule_id = RuleRepo().create(_make_rule_obj(task_id="t1", query="客厅有人"))

    view = service.get_full_view("t1")
    assert view.task_id == "t1"
    assert view.description == "客厅有人开灯"
    assert view.status == "active"
    assert len(view.rule_briefs) == 1
    assert view.rule_briefs[0].rule_id == rule_id
    assert view.rule_briefs[0].query == "客厅有人"


def test_create_task_defaults_to_permanent(service):
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    assert service.get_full_view("t1").lifecycle == "permanent"


def test_create_task_stores_temporary_lifecycle(service):
    """建行时不写这一列的话, 限时 task 在库里与长期 task 无从区分。"""
    service.create_task(
        TaskCreateRequest(task_id="t1", description="d", lifecycle="temporary")
    )
    assert service.get_full_view("t1").lifecycle == "temporary"


def test_create_task_stores_expiry_as_iso_roundtrip(service):
    """入库是 ms、出口是同一时刻的 ISO —— 存原样字符串的话与 record 那份对不上。

    断绝对时刻而不是字符串: 出口走 ``ms_to_iso_local``, 偏移后缀跟着部署时区走,
    写死 ``+08:00`` 会让这条测试在 UTC 的 CI 上红而本机绿。带不带偏移单独断 ——
    那部分是对外契约 (前端 ``new Date(value)`` 靠它), 与环境时区无关。
    """
    from datetime import datetime

    service.create_task(
        TaskCreateRequest(
            task_id="t1",
            description="d",
            lifecycle="temporary",
            expires_at="2026-06-10T23:59:59+08:00",
        )
    )
    got = service.get_full_view("t1").expires_at
    assert datetime.fromisoformat(got) == datetime.fromisoformat(
        "2026-06-10T23:59:59+08:00"
    )
    assert datetime.fromisoformat(got).tzinfo is not None


def test_create_task_stores_expiry_as_integer_ms(real_db):
    """库里必须是 ms 整数。

    存原样 ISO 字符串一样能往返回来同一个 ISO（INTEGER 亲和性对转不成数字的值
    原样存 TEXT），所以只断往返分不开对错 —— 而扫过期要 ``WHERE expires_at < ?``
    做数值比较，存 TEXT 就是逐字符比。
    """
    import sqlite3

    from miloco.task.service import TaskService

    service = TaskService(rule_repo=RuleRepo())
    service.create_task(
        TaskCreateRequest(
            task_id="t1",
            description="d",
            lifecycle="temporary",
            expires_at="2026-06-10T23:59:59+08:00",
        )
    )
    conn = sqlite3.connect(real_db)
    try:
        stored = conn.execute(
            "SELECT expires_at FROM task WHERE task_id='t1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert isinstance(stored, int)
    # 2026-06-10T23:59:59+08:00 == 2026-06-10T15:59:59Z
    assert stored == 1781107199000


def test_create_task_without_expiry_reads_back_none(service):
    service.create_task(
        TaskCreateRequest(task_id="t1", description="d", lifecycle="temporary")
    )
    assert service.get_full_view("t1").expires_at is None


def test_create_task_rejects_unparseable_expiry():
    """非法 ISO 要在入参层被拒。

    入库前那步用 ``iso_to_ms``, 它抛裸 ValueError、router 不捕, 一路冒到全局兜底
    变成 500 —— 写命令的是 agent, 500 里没有能让它自己纠正的信息。
    """
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError, match="不是合法的 ISO8601"):
        TaskCreateRequest(
            task_id="t1",
            description="d",
            lifecycle="temporary",
            expires_at="明天下午三点",
        )


def test_create_task_accepts_zulu_expiry():
    """Z 后缀与 iso_to_ms 同口径, 别把合法值拦了。"""
    req = TaskCreateRequest(
        task_id="t1",
        description="d",
        lifecycle="temporary",
        expires_at="2026-06-10T15:59:59Z",
    )
    assert req.expires_at == "2026-06-10T15:59:59Z"


def test_create_task_rejects_expiry_on_permanent():
    """permanent 带到期时刻是自相矛盾的配置, 放行就没人知道该信哪一个。"""
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError, match="expires_at 只能配 lifecycle=temporary"):
        TaskCreateRequest(
            task_id="t1", description="d", expires_at="2026-06-10T23:59:59+08:00"
        )


def test_create_task_409_on_duplicate_id(service):
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    with pytest.raises(TaskConflict):
        service.create_task(TaskCreateRequest(task_id="t1", description="d2"))


def _real_rule_service():
    """真的 RuleService + RuleRunner —— 停用生效与否要看 runner 内存那份。

    用 stub 顶替会变成「测了实现、没测接线」: 现状那个洞恰恰是 task 只写了 DB、
    没通知 runner (§19.9)。
    """
    from miloco.database.rule_repo import RuleLogRepo
    from miloco.rule.runner import RuleRunner
    from miloco.rule.service import RuleService

    rule_repo = RuleRepo()
    runner = RuleRunner(
        rules=rule_repo.get_all(enabled_only=False),
        miot_proxy=None,
        rule_log_repo=RuleLogRepo(),
    )
    return RuleService(rule_repo, RuleLogRepo(), runner, None), runner


def test_disable_task_marks_meta_paused(service):
    rid = _setup_task_with_rule(service)
    result = service.disable_task("t1")
    assert result.status == "paused"
    assert result.backend_synced.meta_status == "ok"
    assert result.backend_synced.rules[0].rule_id == rid


def test_disable_task_does_not_overwrite_rule_enabled(real_db):
    """rule.enabled 是用户意图, task 停用不再覆写它 (§19.9)。

    生效与否看派生量「有效启用」—— runner 内存里那条 rule 不再进判定。
    """
    from miloco.task.service import TaskService

    rule_service, runner = _real_rule_service()
    svc = TaskService(rule_repo=RuleRepo(), rule_service=rule_service)
    rid = _setup_task_with_rule(svc)
    runner.add_rule(RuleRepo().get_by_id(rid))
    assert len(runner.get_enabled_rules()) == 1

    result = svc.disable_task("t1")

    assert result.backend_synced.rules[0].result == "ok"
    assert RuleRepo().get_by_id(rid).enabled is True
    assert runner.get_enabled_rules() == []
    assert runner.is_task_paused("t1") is True


@pytest.mark.asyncio
async def test_effectively_enabled_rules_drops_paused_task(real_db):
    """感知侧每 cycle 下发取的那份, 停用的 task 不该进去。

    DB 里 ``enabled`` 仍是 1, 所以只按 enabled 过滤的旧口径会把它放出去 ——
    规则继续下发、继续推理、继续触发。
    """
    from miloco.task.service import TaskService

    rule_service, runner = _real_rule_service()
    svc = TaskService(rule_repo=RuleRepo(), rule_service=rule_service)
    rid = _setup_task_with_rule(svc)
    runner.add_rule(RuleRepo().get_by_id(rid))
    assert len(await rule_service.get_effectively_enabled_rules()) == 1

    svc.disable_task("t1")

    assert RuleRepo().get_by_id(rid).enabled is True
    assert await rule_service.get_effectively_enabled_rules() == []
    assert len(await rule_service.get_all_rules(enabled_only=True)) == 1


def test_disable_task_cancels_target_timers(real_db, monkeypatch):
    """停用要连带取消已起的达标 timer, 否则它到点仍会走一遍 fire 路径。

    enable 不该再调 —— 那时没有遗留 timer 要清。
    """
    from miloco.task.service import TaskService

    rule_service, runner = _real_rule_service()
    cancelled: list[str] = []
    monkeypatch.setattr(runner.record_source, "disarm", cancelled.append)
    svc = TaskService(rule_repo=RuleRepo(), rule_service=rule_service)
    _setup_task_with_rule(svc)

    svc.disable_task("t1")
    assert cancelled == ["t1"]

    svc.enable_task("t1")
    assert cancelled == ["t1"]


def test_enable_task_restores_effective_enabled(real_db):
    from miloco.task.service import TaskService

    rule_service, runner = _real_rule_service()
    svc = TaskService(rule_repo=RuleRepo(), rule_service=rule_service)
    rid = _setup_task_with_rule(svc)
    runner.add_rule(RuleRepo().get_by_id(rid))
    svc.disable_task("t1")
    # 中间态必须断言：不断言的话「停用从没生效过」与「停用后又恢复了」终态一样
    assert runner.get_enabled_rules() == []

    svc.enable_task("t1")

    assert len(runner.get_enabled_rules()) == 1
    assert runner.is_task_paused("t1") is False


def test_user_disabled_rule_stays_disabled_across_task_toggle(real_db):
    """现状 bug 的回归测试: 用户手动关掉的那条, task 停用再启用后不该被打开。"""
    from miloco.task.service import TaskService

    rule_service, runner = _real_rule_service()
    svc = TaskService(rule_repo=RuleRepo(), rule_service=rule_service)
    svc.create_task(TaskCreateRequest(task_id="t1", description="d"))
    keep = RuleRepo().create(_make_rule_obj(task_id="t1", name="[t1] keep"))
    off = RuleRepo().create(_make_rule_obj(task_id="t1", name="[t1] off"))
    off_rule = RuleRepo().get_by_id(off)
    off_rule.enabled = False
    RuleRepo().update(off_rule)
    for rid in (keep, off):
        runner.add_rule(RuleRepo().get_by_id(rid))

    svc.disable_task("t1")
    svc.enable_task("t1")

    assert RuleRepo().get_by_id(off).enabled is False
    assert [r.id for r in runner.get_enabled_rules()] == [keep]


def test_disable_task_reports_fail_without_rule_service(service):
    """没注入 rule_service → 内存态没人刷, 停用实际没生效, 不该报 ok。"""
    rid = _setup_task_with_rule(service)
    result = service.disable_task("t1")
    assert result.backend_synced.rules[0].rule_id == rid
    assert result.backend_synced.rules[0].result == "fail"


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
    ok = service.update_meta("t1", TaskUpdateRequest(description="new"))
    assert ok is True
    view = service.get_full_view("t1")
    assert view.description == "new"


def test_update_only_touches_the_fields_that_were_sent(service):
    """partial: 没传的字段保持原样。"""
    service.create_task(
        TaskCreateRequest(
            task_id="t1",
            description="old",
            lifecycle="temporary",
            expires_at="2026-06-10T23:59:59+08:00",
        )
    )
    service.update_meta("t1", TaskUpdateRequest(description="new"))
    view = service.get_full_view("t1")
    assert view.description == "new"
    assert view.lifecycle == "temporary"
    assert view.expires_at is not None


def test_update_rejects_unparseable_expiry():
    """update 与 create 共用同一份校验器, 但共用不等于两条路都被走过。"""
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError, match="不是合法的 ISO8601"):
        TaskUpdateRequest(expires_at="下周三")


def test_update_can_move_the_expiry(service):
    """「今天改成明天」—— record 与 cron 那两份之外, task 这份也得跟着改。"""
    from datetime import datetime

    service.create_task(
        TaskCreateRequest(
            task_id="t1",
            description="d",
            lifecycle="temporary",
            expires_at="2026-06-10T23:59:59+08:00",
        )
    )
    service.update_meta("t1", TaskUpdateRequest(expires_at="2026-06-11T23:59:59+08:00"))
    assert datetime.fromisoformat(
        service.get_full_view("t1").expires_at
    ) == datetime.fromisoformat("2026-06-11T23:59:59+08:00")


def test_update_stores_the_expiry_as_integer_ms(real_db):
    """改完之后库里仍是 ms 整数。

    与 create 那条同一个理由: 存原样 ISO 也能往返回同一个时刻 (ms_to_iso_local
    对字符串原样透传), 只断往返分不开对错 —— 而扫过期要拿这一列做数值比较。
    """
    import sqlite3

    from miloco.database.task_repo import TaskRepo
    from miloco.task.service import TaskService

    service = TaskService(rule_repo=RuleRepo())
    service.create_task(
        TaskCreateRequest(
            task_id="t1",
            description="d",
            lifecycle="temporary",
            expires_at="2026-06-10T23:59:59+08:00",
        )
    )
    service.update_meta("t1", TaskUpdateRequest(expires_at="2026-06-11T23:59:59+08:00"))
    conn = sqlite3.connect(real_db)
    try:
        stored = conn.execute(
            "SELECT expires_at FROM task WHERE task_id='t1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert isinstance(stored, int)
    # 2026-06-11T23:59:59+08:00 == 2026-06-11T15:59:59Z
    assert stored == 1781193599000
    assert TaskRepo().get_full_view("t1")["expires_at"] is not None


def test_update_can_clear_the_expiry_back_to_permanent(service):
    """限时改回长期: 两个字段一次请求里一起改, 中间态不落库。"""
    service.create_task(
        TaskCreateRequest(
            task_id="t1",
            description="d",
            lifecycle="temporary",
            expires_at="2026-06-10T23:59:59+08:00",
        )
    )
    service.update_meta(
        "t1", TaskUpdateRequest(lifecycle="permanent", expires_at=None)
    )
    view = service.get_full_view("t1")
    assert view.lifecycle == "permanent"
    assert view.expires_at is None


def test_update_rejects_permanent_while_the_expiry_stays(service):
    """只把 lifecycle 改成 permanent, 到期时刻还留在库里 —— 校验看的是写后组合。"""
    from miloco.middleware.exceptions import ValidationException

    service.create_task(
        TaskCreateRequest(
            task_id="t1",
            description="d",
            lifecycle="temporary",
            expires_at="2026-06-10T23:59:59+08:00",
        )
    )
    with pytest.raises(ValidationException, match="只能配 lifecycle=temporary"):
        service.update_meta("t1", TaskUpdateRequest(lifecycle="permanent"))


def test_update_rejects_expiry_on_a_permanent_task(service):
    """反方向: task 是 permanent, 只塞到期时刻。"""
    from miloco.middleware.exceptions import ValidationException

    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    with pytest.raises(ValidationException, match="只能配 lifecycle=temporary"):
        service.update_meta(
            "t1", TaskUpdateRequest(expires_at="2026-06-10T23:59:59+08:00")
        )


@pytest.mark.parametrize("column", ["description", "lifecycle"])
def test_update_rejects_clearing_a_not_null_column(service, column):
    """可清空的只有到期时刻。

    点名 NOT NULL 的列会漏掉下一列 —— 这里两列一起参数化, 加第三列时照抄一行。
    """
    from miloco.middleware.exceptions import ValidationException

    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    with pytest.raises(ValidationException, match="不能清空"):
        service.update_meta("t1", TaskUpdateRequest(**{column: None}))


def test_update_rejects_clearing_the_description(service):
    """那一列 NOT NULL —— 放行会在 UPDATE 时撞约束落成 500。"""
    from miloco.middleware.exceptions import ValidationException

    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    with pytest.raises(ValidationException, match="description 不能清空"):
        service.update_meta("t1", TaskUpdateRequest(description=None))


def test_update_rejects_an_empty_body(service):
    from miloco.middleware.exceptions import ValidationException

    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    with pytest.raises(ValidationException, match="至少要传一个可改字段"):
        service.update_meta("t1", TaskUpdateRequest())


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


# ── internal cron 联动分支 ─────────────────────────────────────────────────


def test_disable_task_internal_cron_no_agent_pending(service, stub_runner):
    """disable: internal cron 不进 agent_pending, cron.enabled=0, runner 收到 apply."""
    from miloco.schedule.repo import CronRepo

    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    _insert_internal_cron("t1", "job-internal")

    result = service.disable_task("t1")

    assert result.agent_pending == []
    assert CronRepo().get("job-internal").enabled is False
    assert stub_runner.apply_calls == [("job-internal", False)]


def test_enable_task_internal_cron_apply(service, stub_runner):
    """enable: cron.enabled=1, runner 收到 apply (enabled=True)."""
    from miloco.schedule.repo import CronRepo

    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    _insert_internal_cron("t1", "job-internal")
    service.disable_task("t1")
    stub_runner.apply_calls.clear()

    result = service.enable_task("t1")

    assert result.agent_pending == []
    assert CronRepo().get("job-internal").enabled is True
    assert stub_runner.apply_calls == [("job-internal", True)]


def test_toggle_task_mixed_cron_only_external_in_pending(service, stub_runner):
    """混合 internal + external: agent_pending 只含 external, internal 走 apply."""
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    _insert_internal_cron("t1", "job-int")
    _insert_external_cron("t1", "job-ext")

    result = service.disable_task("t1")

    refs = {op.ref for op in result.agent_pending}
    assert refs == {"job-ext"}
    assert [c[0] for c in stub_runner.apply_calls] == ["job-int"]


def test_delete_task_internal_cron_calls_remove_job(service, stub_runner):
    """delete: internal cron 不进 agent_pending, runner.remove_job 被调."""
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    _insert_internal_cron("t1", "job-internal")

    result = service.delete_task("t1", reason="completed")

    assert result is not None
    assert result.agent_pending == []
    assert stub_runner.remove_calls == ["job-internal"]


def test_delete_task_mixed_cron_only_external_in_pending(service, stub_runner):
    """delete 混合: agent_pending 只含 external, internal 走 remove_job."""
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    _insert_internal_cron("t1", "job-int")
    _insert_external_cron("t1", "job-ext")

    result = service.delete_task("t1", reason="completed")

    assert result is not None
    refs = {op.ref for op in result.agent_pending}
    assert refs == {"job-ext"}
    assert stub_runner.remove_calls == ["job-int"]


def test_get_full_view_returns_cron_refs_with_dispatch_owner(service):
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    _insert_internal_cron("t1", "job-int")
    _insert_external_cron("t1", "job-ext")

    view = service.get_full_view("t1")

    assert view is not None
    refs = {(c.ref, c.dispatch_owner) for c in view.cron_refs}
    assert refs == {("job-int", "internal"), ("job-ext", "external")}


def test_list_for_dedupe_returns_cron_refs(service):
    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    _insert_internal_cron("t1", "job-int")

    views = service.list_for_dedupe()

    by_id = {v.task_id: v for v in views}
    assert by_id["t1"].cron_refs == [CronRef(ref="job-int", dispatch_owner="internal")]


def test_action_desc_renders_scene_id_not_none():
    """场景没有 value/params;只按 iid 渲染会给用户看到 `scene=None`,
    且同一条规则装两个场景时两行完全一样(前端直接展示这个串)。"""
    from miloco.rule.schema import RuleAction
    from miloco.task.service import _action_desc

    a = RuleAction(did="scene-A", iid="scene", idempotent=False, cooldown_minutes=5)
    b = RuleAction(did="scene-B", iid="scene", idempotent=False, cooldown_minutes=5)

    assert _action_desc(a) == "scene:scene-A"
    assert _action_desc(a) != _action_desc(b)


def test_action_desc_keeps_prop_and_action_shape():
    """既有两种形态的展示串不能被改动带偏。"""
    from miloco.rule.schema import RuleAction
    from miloco.task.service import _action_desc

    prop = RuleAction(did="d1", iid="prop.2.1", value=True)
    tts = RuleAction(
        did="d2", iid="action.7.3", params=["你好"],
        idempotent=False, cooldown_minutes=5,
    )
    assert _action_desc(prop) == "prop.2.1=True"
    assert _action_desc(tts) == "action.7.3=['你好']"


def test_state_action_desc_keeps_payload_out():
    """state 摘要只带形态。前端按「；」切分摘要串(TasksPage.splitActions),
    TTS 文案里的分号会把一条动作切成几行残句。"""
    from miloco.rule.schema import RuleAction
    from miloco.task.service import _action_desc_short

    tts = RuleAction(
        did="speaker", iid="action.7.3", params=["灯已调暗；投影已开启"],
        idempotent=False, cooldown_minutes=5,
    )
    scene = RuleAction(
        did="scene-A", iid="scene", idempotent=False, cooldown_minutes=5
    )
    assert _action_desc_short(tts) == "action.7.3"
    assert "；" not in _action_desc_short(tts)
    assert _action_desc_short(scene) == "scene:scene-A"


# ── task 侧动作入口 ────────────────────────────────────────────────────


def _mk_task(service, task_id="t1"):
    service.create_task(TaskCreateRequest(task_id=task_id, description="d"))


def test_list_all_carries_the_expiry_too(real_db):
    """列表接口也要带到期时刻。

    ``GET /tasks`` 与 ``GET /tasks/summary`` 都走 ``list_all``, 那条 SELECT 漏一列
    的话每个 task 都回 null —— 库里有值、详情页看得到、列表页看不到。
    断 repo 的原始 dict: service 层拿 ``raw["expires_at"]``, 漏列在那里是 KeyError,
    但那只在有 task 时才走到, 形状本身要在这里钉住。
    """
    from datetime import datetime

    from miloco.database.task_repo import TaskRepo
    from miloco.task.service import TaskService

    service = TaskService(rule_repo=RuleRepo())
    service.create_task(
        TaskCreateRequest(
            task_id="t1",
            description="d",
            lifecycle="temporary",
            expires_at="2026-06-10T23:59:59+08:00",
        )
    )
    rows = TaskRepo().list_all()
    assert len(rows) == 1
    assert "expires_at" in rows[0]
    assert datetime.fromisoformat(rows[0]["expires_at"]) == datetime.fromisoformat(
        "2026-06-10T23:59:59+08:00"
    )
    assert service.list_summary("day")[0].expires_at is not None


def test_full_view_exposes_every_action_slot(service):
    """六个槽必须全出现在 repo 拼出来的 actions 里。

    断的是 repo 的原始 dict 而不是 ``TaskFullView.actions``: 后者是 pydantic
    模型, 缺的键会被字段默认值补齐, 拿它断形状分不开"repo 少拼了一个槽"和
    "repo 拼全了"。
    ``task get`` 与 ``task list`` 走同一个拼装函数, 所以不写"两边一致"那种
    断言 —— 同一份实现的一致性测试永远绿。
    """
    from miloco.database.task_repo import TaskRepo

    _mk_task(service)
    assert set(TaskRepo().get_full_view("t1")["actions"]) == {
        "on_enter_actions",
        "on_enter_desc",
        "on_exit_actions",
        "on_exit_desc",
        "on_target_actions",
        "on_target_desc",
    }


def test_set_actions_only_touches_the_slots_that_were_sent(service):
    """partial 语义: 没传的槽保持原样。

    多 rule 的 task 只能从这里改动作, 一次改进入动作就把退出动作冲掉的话,
    这个入口本身就不可用。
    """
    from miloco.task.schema import TaskActionsUpdateRequest

    _mk_task(service)
    service.set_boundary_actions(
        "t1", TaskActionsUpdateRequest(on_enter_desc="进", on_exit_desc="出")
    )
    service.set_boundary_actions("t1", TaskActionsUpdateRequest(on_enter_desc="新进"))

    view = service.get_full_view("t1")
    assert view.actions.on_enter_desc == "新进"
    assert view.actions.on_exit_desc == "出"


def test_set_actions_clears_a_slot_when_explicitly_null(service):
    """传 null 才清空 —— 与"没传"必须能分开。"""
    from miloco.task.schema import TaskActionsUpdateRequest

    _mk_task(service)
    service.set_boundary_actions(
        "t1", TaskActionsUpdateRequest(on_enter_desc="进", on_exit_desc="出")
    )
    service.set_boundary_actions("t1", TaskActionsUpdateRequest(on_exit_desc=None))

    view = service.get_full_view("t1")
    assert view.actions.on_enter_desc == "进"
    assert view.actions.on_exit_desc is None


def test_set_actions_reports_missing_task(service):
    from miloco.task.schema import TaskActionsUpdateRequest

    assert (
        service.set_boundary_actions(
            "nope", TaskActionsUpdateRequest(on_enter_desc="进")
        )
        is False
    )


def test_set_actions_refreshes_the_state_machine_snapshot(service, monkeypatch):
    """runner 手里的动作是内存副本, 不刷新就是改了不生效而 CLI 报成功。"""
    from miloco.task.schema import TaskActionsUpdateRequest

    _mk_task(service)
    reconfigured: list[str] = []

    class _Stub:
        def reconfigure_task(self, task_id):
            reconfigured.append(task_id)

    service._rule_service = _Stub()
    service.set_boundary_actions("t1", TaskActionsUpdateRequest(on_enter_desc="进"))

    assert reconfigured == ["t1"]


def test_full_view_exposes_rule_direction(service):
    """enter 与 exit 的 mode 都是 event, 不带 direction 就分不出一条 rule 的方向。"""
    from miloco.rule.schema import RuleDirection

    _mk_task(service)
    rule = Rule(
        name="[t1] 出",
        task_id="t1",
        mode=RuleMode.EVENT,
        condition=RuleCondition(perceive_device_ids=["cam1"], query="走了"),
        action_descriptions=["该退了"],
    )
    rule.direction = RuleDirection.EXIT
    RuleRepo().create(rule)

    view = service.get_full_view("t1")
    assert [b.direction for b in view.rule_briefs] == ["exit"]


def test_set_actions_clearing_a_static_slot_writes_empty_list(real_db):
    """``*_actions`` 三列是 NOT NULL —— 清空它们只能写空列表, 写 null 会直接崩。"""
    from miloco.task.schema import TaskActionsUpdateRequest

    service = _service_with_rule_stub(None)
    _mk_task(service)
    service.set_boundary_actions(
        "t1",
        TaskActionsUpdateRequest(
            on_enter_actions=[
                {"did": "1", "iid": "prop.2.1", "value": 1, "idempotent": True}
            ]
        ),
    )
    service.set_boundary_actions("t1", TaskActionsUpdateRequest(on_enter_actions=None))

    assert service.get_full_view("t1").actions.on_enter_actions == []


def _mk_enter_rule(task_id="t1", name=None, with_action=True):
    rule = Rule(
        name=name or f"[{task_id}] 进",
        task_id=task_id,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        condition=RuleCondition(perceive_device_ids=["cam1"], query="有人"),
        action_descriptions=["开灯"] if with_action else [],
    )
    rule.direction = RuleDirection.ENTER
    RuleRepo().create(rule)


def test_set_actions_can_clear_the_enter_slot(real_db):
    """清进入动作不再被拦。

    拦过一版, 但那道闸只守得住这一个入口 —— 换方向、挪 task、给没接管的 task 补
    任意一个别的槽, 都能造成同样的哑规则; 而拦住了也给不出出路 (兄弟自带动作在
    task 已接管时同样选不到)。现在由 report_muted_enter_rules 在 reconfigure 时
    按真实状态报出, 诊断用例见 test_reconfigure_path。
    """
    from miloco.task.schema import TaskActionsUpdateRequest

    service = _service_with_rule_stub(None)
    _mk_task(service)
    service.set_boundary_actions("t1", TaskActionsUpdateRequest(on_enter_desc="开灯"))
    _mk_enter_rule(with_action=False)

    assert service.set_boundary_actions(
        "t1", TaskActionsUpdateRequest(on_enter_desc=None)
    )
    assert service.get_full_view("t1").actions.on_enter_desc is None


# ── 配达标动作要先有 duration record + 阈值 ──────────────────────────────


def _service_with_rule_stub(record_state):
    """带一个只回答"有没有阈值"的 rule service 替身。"""
    from unittest.mock import MagicMock

    from miloco.database.task_repo import TaskRepo
    from miloco.rule.service import RuleService
    from miloco.task.service import TaskService

    record_svc = MagicMock()
    record_svc.detect_record_kind = MagicMock(
        return_value="duration" if record_state is not None else None
    )
    record_svc.read_duration_target_state = MagicMock(return_value=record_state)
    rule_svc = RuleService.__new__(RuleService)
    rule_svc._task_record_service = record_svc
    # 配达标动作还要查这个 task 有没有出路径, 那道闸读的是 rule 表
    rule_svc._repo = RuleRepo()
    # 清进入动作那道闸要看清完之后 task 还剩哪些槽 (读侧是否仍被接管)
    rule_svc._task_repo = TaskRepo()
    rule_svc.reconfigure_task = MagicMock()
    return TaskService(rule_repo=RuleRepo(), rule_service=rule_svc)


def test_set_actions_rejects_target_desc_without_a_record(real_db):
    """配了达标通知却没有 record —— 永远不会响, 且从配置上看不出缺什么。"""
    from miloco.middleware.exceptions import ValidationException
    from miloco.task.schema import TaskActionsUpdateRequest

    service = _service_with_rule_stub(None)
    _mk_task(service)
    with pytest.raises(ValidationException, match="无活跃 record"):
        service.set_boundary_actions(
            "t1", TaskActionsUpdateRequest(on_target_desc="达标推送")
        )


def test_set_actions_rejects_target_desc_without_a_threshold(real_db):
    from miloco.middleware.exceptions import ValidationException
    from miloco.task.schema import TaskActionsUpdateRequest

    service = _service_with_rule_stub((None, 0))
    _mk_task(service)
    with pytest.raises(ValidationException, match="target_minutes"):
        service.set_boundary_actions(
            "t1", TaskActionsUpdateRequest(on_target_desc="达标推送")
        )


def test_set_actions_allows_target_desc_when_the_record_is_ready(real_db):
    from miloco.task.schema import TaskActionsUpdateRequest

    service = _service_with_rule_stub((30, 0))
    _mk_task(service)
    assert service.set_boundary_actions(
        "t1", TaskActionsUpdateRequest(on_target_desc="达标推送")
    )


def test_set_actions_does_not_check_the_record_for_other_slots(service):
    """只有达标那个槽有这个前置 —— 顺手校验会让改进入动作也被 record 卡住。"""
    from miloco.task.schema import TaskActionsUpdateRequest

    _mk_task(service)
    assert service.set_boundary_actions(
        "t1", TaskActionsUpdateRequest(on_enter_desc="进")
    )


def test_list_summary_carries_task_boundary_actions(service, real_db):
    """列表路径也要带 task 级动作。

    多条 rule 的 task 动作只存在 task 行上, rule_briefs 的 actions_desc 是空的 ——
    列表不带 actions 的话住户界面把配好推送的 task 显示成「无动作」。
    """
    from miloco.database.task_repo import TaskRepo

    service.create_task(TaskCreateRequest(task_id="t1", description="d"))
    TaskRepo().set_boundary_actions(
        "t1", on_enter_desc="进来推一条", on_exit_desc="出去推一条"
    )

    view = next(v for v in service.list_summary("day") if v.task_id == "t1")

    assert view.actions is not None
    assert view.actions.on_enter_desc == "进来推一条"
    assert view.actions.on_exit_desc == "出去推一条"


def test_set_actions_replaces_the_whole_slot(service):
    """同槽两列一起写。

    两列互斥而选槽时静态优先, 只写传进来的那一列的话, 用户把动作从设备直控改成
    Agent 文案时残留的静态列会继续赢 —— 请求返回成功、task get 显示新文案、实际
    下发的还是旧的设备动作。
    """
    from miloco.task.schema import TaskActionsUpdateRequest

    _mk_task(service)
    service.set_boundary_actions(
        "t1",
        TaskActionsUpdateRequest(
            on_enter_actions=[{"did": "lamp", "iid": "prop.2.1", "value": True}]
        ),
    )
    service.set_boundary_actions(
        "t1", TaskActionsUpdateRequest(on_enter_desc="提醒孩子先喝口水")
    )

    view = service.get_full_view("t1")
    assert view.actions.on_enter_desc == "提醒孩子先喝口水"
    assert view.actions.on_enter_actions == []
    # 别的槽照旧不碰
    assert view.actions.on_exit_desc is None


@pytest.mark.asyncio
async def test_resume_can_reenter_while_the_condition_is_still_true(real_db):
    """停用要连带清掉条件层, 否则恢复后回不到 on。

    停用期间 update_state 在入口就 return, 条件层冻在停用那一刻的值; 而状态机那边
    运行态已归 off。恢复后条件若仍成立, 与冻住的旧值一比是 old==new、产不出边沿,
    task 就再也进不去 —— 要等条件先假一次才有救。
    """
    from miloco.task.service import TaskService

    rule_service, runner = _real_rule_service()
    svc = TaskService(rule_repo=RuleRepo(), rule_service=rule_service)
    rid = _setup_task_with_rule(svc)
    runner.add_rule(RuleRepo().get_by_id(rid))
    await runner.update_state(rid, "cam-001", True, "")
    await runner.drain()
    assert runner._state[rid].last_rule_state is True

    svc.disable_task("t1")
    svc.enable_task("t1")

    # 恢复后第一帧条件仍成立 —— 必须重新产出进入边沿
    outcome = await runner.update_state(rid, "cam-001", True, "")
    await runner.drain()
    assert outcome is not TriggerOutcome.STILL_IN, (
        "恢复后条件仍成立却产不出边沿, task 永远回不到 on"
    )


def test_set_actions_rejects_target_desc_on_a_task_without_an_exit_path(real_db):
    """事件型 task 配达标 = 配了个永远不响的通知。

    运行态恒 off, 达标信号到状态机被判成"不在会话中"、一次都不派; 更根上的原因是
    累计时长靠 session-start / session-end 配对, 没有出路径就永远发不出 session-end。
    """
    from miloco.middleware.exceptions import ValidationException
    from miloco.task.schema import TaskActionsUpdateRequest

    service = _service_with_rule_stub((30, 0))
    _mk_task(service)
    RuleRepo().create(
        Rule(
            name="[t1] 进",
            task_id="t1",
            mode=RuleMode.EVENT,
            direction=RuleDirection.ENTER,
            condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人"),
            action_descriptions=["开灯"],
        )
    )

    with pytest.raises(ValidationException, match="需要一条 direction=exit 或 session"):
        service.set_boundary_actions(
            "t1", TaskActionsUpdateRequest(on_target_desc="达标推送")
        )


def test_set_actions_allows_target_desc_before_any_rule_exists(real_db):
    """装配是分步的 —— 先配动作后建规则是正常顺序, 那一刻判不出形态。"""
    from miloco.task.schema import TaskActionsUpdateRequest

    service = _service_with_rule_stub((30, 0))
    _mk_task(service)

    assert service.set_boundary_actions(
        "t1", TaskActionsUpdateRequest(on_target_desc="达标推送")
    )
