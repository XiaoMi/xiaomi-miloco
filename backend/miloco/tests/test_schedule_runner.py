# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""ScheduleRunner._fire / rebuild_from_db 各分支的行为测试.

APScheduler 不真跑, 靠手工调 runner._fire(cron_id) 模拟触发; agent webhook
用 monkeypatch stub 掉, 断言 cron 表状态 (mark_fired_and_delete / retry_attempt / DELETE)。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def runner_env(tmp_path, monkeypatch):
    db_file = tmp_path / "sched.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task (task_id, description, created_at) "
            "VALUES ('t1', 'x', 0)"
        )
        conn.commit()

    import miloco.schedule.runner as runner_module

    runner_module._runner = None  # type: ignore[attr-defined]
    runner = runner_module.ScheduleRunner()

    yield runner, runner_module, monkeypatch

    if runner.running:
        runner.shutdown()
    runner_module._runner = None  # type: ignore[attr-defined]
    reset_settings()


def _insert_internal_at(cron_id: str, at_ms: int, task_id: str = "t1"):
    from miloco.schedule.repo import CronRepo
    from miloco.schedule.schema import Cron

    CronRepo().insert(
        Cron(
            cron_id=cron_id,
            task_id=task_id,
            dispatch_owner="internal",
            name=f"at-{cron_id}",
            kind="at",
            at_ms=at_ms,
            message="ping",
            created_at=0,
            updated_at=0,
        )
    )


def _insert_internal_cron(cron_id: str, expr: str = "* * * * *", task_id: str = "t1"):
    from miloco.schedule.repo import CronRepo
    from miloco.schedule.schema import Cron

    CronRepo().insert(
        Cron(
            cron_id=cron_id,
            task_id=task_id,
            dispatch_owner="internal",
            name=f"cron-{cron_id}",
            kind="cron",
            cron_expr=expr,
            message="msg",
            created_at=0,
            updated_at=0,
        )
    )


# ── _fire 分支 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_orphan_cron_removes_scheduler_side(runner_env):
    """cron 表无对应行 (orphan job) → 清 scheduler 侧, 不 raise."""
    runner, _, _ = runner_env
    # 直接调 _fire, 表里根本没这个 cron_id
    await runner._fire("ghost-cron-id")
    # 无 exception 说明 defensive 分支命中


@pytest.mark.asyncio
async def test_fire_disabled_skips_agent_call(runner_env, monkeypatch):
    """disabled cron 触发 → 清 scheduler 侧, 不调 agent."""
    runner, runner_module, _ = runner_env
    _insert_internal_at("at-1", at_ms=1_000_000)

    from miloco.schedule.repo import CronRepo

    CronRepo().set_enabled("at-1", False)

    agent_mock = AsyncMock(return_value=(None, "ok", 0.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    await runner._fire("at-1")

    agent_mock.assert_not_called()


@pytest.mark.asyncio
async def test_fire_at_success_marks_fired_and_deletes(runner_env, monkeypatch):
    """at 成功 → mark_fired_and_delete 单事务, 表行清空."""
    runner, runner_module, _ = runner_env
    _insert_internal_at("at-1", at_ms=1_000_000)

    agent_mock = AsyncMock(return_value=("run-1", "ok", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    await runner._fire("at-1")

    from miloco.schedule.repo import CronRepo

    assert CronRepo().get("at-1") is None
    agent_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_fire_at_status_error_increments_retry(runner_env, monkeypatch):
    """at status=error → retry_attempt +1, 挂 :retry, 表行仍在."""
    runner, runner_module, _ = runner_env
    at_ms = 1_000_000
    _insert_internal_at("at-1", at_ms=at_ms)

    agent_mock = AsyncMock(return_value=("run-1", "error", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    # monkeypatch now_ms 保证不超 max_delay 窗口 (default 300s)
    monkeypatch.setattr(runner_module, "now_ms", lambda: at_ms + 60_000)

    # stub scheduler.add_job 避免真启动
    added = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(kwargs.get("id"))

    await runner._fire("at-1")

    from miloco.schedule.repo import CronRepo

    cron = CronRepo().get("at-1")
    assert cron is not None
    assert cron.retry_attempt == 1
    assert "at-1:retry" in added


@pytest.mark.asyncio
async def test_fire_at_transport_error_schedules_retry(runner_env, monkeypatch):
    """AgentWebhookException → 挂 :retry (transport 失败不算应用层重试, retry_attempt 不 +1)."""
    runner, runner_module, _ = runner_env
    at_ms = 1_000_000
    _insert_internal_at("at-1", at_ms=at_ms)

    from miloco.middleware.exceptions import AgentWebhookException

    agent_mock = AsyncMock(side_effect=AgentWebhookException("transport down"))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)
    monkeypatch.setattr(runner_module, "now_ms", lambda: at_ms + 60_000)

    added = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(kwargs.get("id"))

    await runner._fire("at-1")

    from miloco.schedule.repo import CronRepo

    cron = CronRepo().get("at-1")
    assert cron is not None
    assert cron.retry_attempt == 0  # transport 失败不 +1
    assert "at-1:retry" in added


@pytest.mark.asyncio
async def test_fire_at_no_channel_gives_up(runner_env, monkeypatch):
    """at status=no-channel → 直接 DELETE 放弃, 不重试."""
    runner, runner_module, _ = runner_env
    _insert_internal_at("at-1", at_ms=1_000_000)

    agent_mock = AsyncMock(return_value=(None, "no-channel", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    await runner._fire("at-1")

    from miloco.schedule.repo import CronRepo

    assert CronRepo().get("at-1") is None


@pytest.mark.asyncio
async def test_fire_at_overdue_gives_up_on_retry_path(runner_env, monkeypatch):
    """at status=error 但已过 max_delay → DELETE 放弃, 不再挂 retry."""
    runner, runner_module, _ = runner_env
    at_ms = 1_000_000
    _insert_internal_at("at-1", at_ms=at_ms)

    agent_mock = AsyncMock(return_value=("run-1", "error", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    # now = at_ms + 400s, 超过 default max_delay 300s
    monkeypatch.setattr(runner_module, "now_ms", lambda: at_ms + 400 * 1000)

    added = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(kwargs.get("id"))

    await runner._fire("at-1")

    from miloco.schedule.repo import CronRepo

    assert CronRepo().get("at-1") is None
    assert added == []  # 不挂 :retry


@pytest.mark.asyncio
async def test_fire_at_status_timeout_schedules_retry(runner_env, monkeypatch):
    """at status=timeout → 走 :retry (turn 可能仍在跑或已失败, 稳定 idempotency 防重投)."""
    runner, runner_module, _ = runner_env
    at_ms = 1_000_000
    _insert_internal_at("at-1", at_ms=at_ms)

    agent_mock = AsyncMock(return_value=("run-1", "timeout", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)
    monkeypatch.setattr(runner_module, "now_ms", lambda: at_ms + 60_000)

    added = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(kwargs.get("id"))

    await runner._fire("at-1")

    from miloco.schedule.repo import CronRepo

    cron = CronRepo().get("at-1")
    assert cron is not None  # 行还在, 未被当成功删掉
    assert cron.retry_attempt == 0  # transport_error 路径不 +1
    assert "at-1:retry" in added


@pytest.mark.asyncio
async def test_fire_cron_kind_timeout_leaves_row_no_retry(runner_env, monkeypatch):
    """cron kind status=timeout → 无副作用, 下周期自然重触, 不挂 :retry."""
    runner, runner_module, _ = runner_env
    _insert_internal_cron("c1")

    agent_mock = AsyncMock(return_value=("run-1", "timeout", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    added = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(kwargs.get("id"))

    await runner._fire("c1")

    from miloco.schedule.repo import CronRepo

    assert CronRepo().get("c1") is not None
    assert added == []  # cron/every 无 :retry


@pytest.mark.asyncio
async def test_fire_cron_kind_success_leaves_row(runner_env, monkeypatch):
    """cron kind 成功 → 保留行等下次 fire (无 mark_fired_and_delete)."""
    runner, runner_module, _ = runner_env
    _insert_internal_cron("c1")

    agent_mock = AsyncMock(return_value=("run-1", "ok", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    await runner._fire("c1")

    from miloco.schedule.repo import CronRepo

    assert CronRepo().get("c1") is not None


@pytest.mark.asyncio
async def test_fire_passes_light_context_to_agent(runner_env, monkeypatch):
    """cron.light_context=True → run_agent_turn 收到 light_context=True."""
    runner, runner_module, _ = runner_env
    from miloco.schedule.repo import CronRepo
    from miloco.schedule.schema import Cron

    CronRepo().insert(
        Cron(
            cron_id="c1",
            task_id="t1",
            dispatch_owner="internal",
            name="light",
            kind="cron",
            cron_expr="* * * * *",
            message="msg",
            light_context=True,
            created_at=0,
            updated_at=0,
        )
    )

    agent_mock = AsyncMock(return_value=("run-1", "ok", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    await runner._fire("c1")

    kwargs = agent_mock.call_args.kwargs
    assert kwargs["light_context"] is True


@pytest.mark.asyncio
async def test_fire_message_prefix_and_session_key(runner_env, monkeypatch):
    """_fire 无条件给 message 加 [cron:{name}] 前缀 + sessionKey miloco-schedule:{id}."""
    runner, runner_module, _ = runner_env
    _insert_internal_cron("c1")

    agent_mock = AsyncMock(return_value=("run-1", "ok", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    await runner._fire("c1")

    kwargs = agent_mock.call_args.kwargs
    assert kwargs["text"].startswith("[cron:cron-c1] ")
    assert kwargs["session_key"] == "miloco-schedule:c1"


@pytest.mark.asyncio
async def test_fire_at_idempotency_key_stable_for_first_attempt(
    runner_env, monkeypatch
):
    """at retry_attempt=0 → idempotency_key = at:{cron_id}:{at_ms}."""
    runner, runner_module, _ = runner_env
    at_ms = 1_000_000
    _insert_internal_at("at-1", at_ms=at_ms)

    agent_mock = AsyncMock(return_value=("run-1", "ok", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    await runner._fire("at-1")

    kwargs = agent_mock.call_args.kwargs
    assert kwargs["idempotency_key"] == f"at:at-1:{at_ms}"


@pytest.mark.asyncio
async def test_fire_at_idempotency_key_incremented_on_retry(
    runner_env, monkeypatch
):
    """at retry_attempt=N (N>0) → idempotency_key = at:{id}:{at_ms}:retry:{N}."""
    runner, runner_module, _ = runner_env
    at_ms = 1_000_000
    _insert_internal_at("at-1", at_ms=at_ms)

    from miloco.schedule.repo import CronRepo

    CronRepo().increment_retry_attempt("at-1")  # → 1
    CronRepo().increment_retry_attempt("at-1")  # → 2

    agent_mock = AsyncMock(return_value=("run-1", "ok", 100.0))
    monkeypatch.setattr(runner_module, "run_agent_turn", agent_mock)

    await runner._fire("at-1")

    kwargs = agent_mock.call_args.kwargs
    assert kwargs["idempotency_key"] == f"at:at-1:{at_ms}:retry:2"


# ── rebuild_from_db 分支 ────────────────────────────────────────────────


def test_rebuild_at_overdue_deletes_row(runner_env, monkeypatch):
    """rebuild 时 at 已过 max_delay 窗口 → DELETE 放弃."""
    runner, runner_module, _ = runner_env
    at_ms = 1_000_000
    _insert_internal_at("at-1", at_ms=at_ms)

    monkeypatch.setattr(runner_module, "now_ms", lambda: at_ms + 400 * 1000)

    runner._scheduler.add_job = lambda *args, **kwargs: None  # stub

    runner.rebuild_from_db()

    from miloco.schedule.repo import CronRepo

    assert CronRepo().get("at-1") is None


def test_rebuild_at_fired_at_defensive_cleanup(runner_env):
    """rebuild 时 at 已 fired_at (crash 残留) → 直接 DELETE."""
    runner, _, _ = runner_env
    _insert_internal_at("at-1", at_ms=1_000_000)

    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "UPDATE cron SET fired_at=? WHERE cron_id=?", (999, "at-1")
        )
        conn.commit()

    runner._scheduler.add_job = lambda *args, **kwargs: None
    runner.rebuild_from_db()

    from miloco.schedule.repo import CronRepo

    assert CronRepo().get("at-1") is None


def test_rebuild_at_retry_chain_only_reschedules_retry(
    runner_env, monkeypatch
):
    """rebuild 时 at retry_attempt>0 → 只挂 :retry, 跳过主 job."""
    runner, runner_module, _ = runner_env
    at_ms = 1_000_000
    _insert_internal_at("at-1", at_ms=at_ms)

    from miloco.schedule.repo import CronRepo

    CronRepo().increment_retry_attempt("at-1")

    monkeypatch.setattr(runner_module, "now_ms", lambda: at_ms + 60_000)

    added: list[str] = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(
        kwargs.get("id")
    )
    runner.rebuild_from_db()

    assert added == ["at-1:retry"]  # 主 job 不建, 只 :retry


def test_rebuild_skips_external(runner_env):
    """rebuild 只处理 dispatch_owner='internal', external 不建 in-memory job."""
    runner, _, _ = runner_env
    from miloco.database.connector import get_db_connector

    with get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO cron (cron_id, task_id, dispatch_owner, enabled, "
            "created_at, updated_at) VALUES ('ext-1', 't1', 'external', 1, 0, 0)"
        )
        conn.commit()

    added: list[str] = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(
        kwargs.get("id")
    )
    runner.rebuild_from_db()

    assert "ext-1" not in added


def test_rebuild_enabled_cron_adds_job(runner_env):
    """rebuild 正常 cron/every 走 apply_enabled_state → add_job."""
    runner, _, _ = runner_env
    _insert_internal_cron("c1")

    added: list[str] = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(
        kwargs.get("id")
    )
    runner.rebuild_from_db()

    assert "c1" in added


def test_rebuild_disabled_cron_skips_add_job(runner_env):
    """rebuild disabled cron → 不建 in-memory job (靠 enabled=0 记账)."""
    runner, _, _ = runner_env
    _insert_internal_cron("c1")

    from miloco.schedule.repo import CronRepo

    CronRepo().set_enabled("c1", False)

    added: list[str] = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(
        kwargs.get("id")
    )
    runner.rebuild_from_db()

    assert "c1" not in added


# ── apply_enabled_state 分支 ─────────────────────────────────────────────


def test_apply_external_is_noop(runner_env):
    """apply_enabled_state 遇 external → 不动 scheduler."""
    runner, _, _ = runner_env
    from miloco.schedule.schema import Cron

    external = Cron(
        cron_id="ext-1",
        task_id="t1",
        dispatch_owner="external",
        created_at=0,
        updated_at=0,
    )

    added = []
    runner._scheduler.add_job = lambda *args, **kwargs: added.append(
        kwargs.get("id")
    )
    runner.apply_enabled_state(external)

    assert added == []
