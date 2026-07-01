# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Integration tests for RuleRepo / RuleLogRepo against a real SQLite file.

跟 test_rule.py（mock 仓库）互补：mock 测不到字段映射、JSON 列序列化、
排序 / 过滤的 SQL 行为。这里每个 case 用 tmp_path 起一个全新的 DB。
"""

import time
import uuid

import pytest
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleActionExecuteResult,
    RuleCondition,
    RuleEvent,
    RuleExecuteResult,
    RuleLifecycle,
    RuleLog,
    RuleLogKind,
    RuleMode,
)

# ---- Fixtures: 真实 SQLite 隔离 ----


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """每个 case 起全新的 SQLite，自动创建表、自动清理。

    通过 MILOCO_DATABASE__PATH 环境变量覆盖 settings，再清两个缓存（settings
    单例 + db_connector 单例）让 init_database 落到 tmp 路径。
    """
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()

    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()

    # 方案 P：rule create 内部写 task_link(kind='rule')，FK 要求 task 已存在。
    # 本测试所有 rule 都挂在固定 task_id "test_task" 下，fixture 先建占位行。
    from miloco.utils.time_utils import now_iso

    with connector_module.get_db_connector().get_connection() as conn:
        conn.execute(
            "INSERT INTO task (task_id, description, status, created_at) "
            "VALUES ('test_task', 'rule repo test placeholder', 'active', ?)",
            (now_iso(),),
        )
        conn.commit()

    yield db_file

    # 清理：让 reset_settings 恢复默认，避免污染后续 test
    reset_settings()


@pytest.fixture
def rule_repo(real_db):
    from miloco.database.rule_repo import RuleRepo

    return RuleRepo()


@pytest.fixture
def log_repo(real_db):
    from miloco.database.rule_repo import RuleLogRepo

    return RuleLogRepo()


# ---- Helpers ----

TASK_ID = "test_task"


def _name(suffix="rule"):
    return f"[{TASK_ID}] {suffix}"


def _make_action(did="d1", iid="prop.2.1", value=True, idempotent=True, cooldown=None):
    return RuleAction(
        did=did, iid=iid, value=value, idempotent=idempotent, cooldown_minutes=cooldown
    )


def _make_condition(device_ids=None, query="有人经过"):
    return RuleCondition(
        perceive_device_ids=device_ids or ["cam-001"],
        query=query,
    )


def _make_static_rule(name=None, enabled=True, actions=None, condition=None):
    return Rule(
        id="",
        name=name if name is not None else _name("static"),
        task_id=TASK_ID,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=enabled,
        condition=condition or _make_condition(),
        actions=actions if actions is not None else [_make_action()],
    )


def _make_log(
    rule_id="r1",
    ts=None,
    kind=RuleLogKind.RULE_TRIGGER_SUCCESS,
    with_result=False,
):
    execute_result = None
    if with_result:
        execute_result = RuleExecuteResult(
            event=RuleEvent.ENTERED,
            action_results=[
                RuleActionExecuteResult(action=_make_action(), result=True)
            ],
            dynamic_rule_event_sent=False,
        )
    return RuleLog(
        id=str(uuid.uuid4()),
        timestamp=ts if ts is not None else int(time.time() * 1000),
        kind=kind,
        rule_id=rule_id,
        rule_name="test",
        rule_query="有人",
        trigger_context="ctx",
        execute_result=execute_result,
    )


# ============================================================
# RuleRepo: 字段往返 / V3 列 / JSON 序列化
# ============================================================


class TestRuleRepoRoundtrip:
    def test_create_then_get_by_id_round_trip_all_v3_fields(self, rule_repo):
        """create → get_by_id 必须保留所有 V3 字段（含 task_id/mode/lifecycle/on_*）。"""
        rule = Rule(
            id="",
            name=_name("round"),
            task_id=TASK_ID,
            mode=RuleMode.STATE,
            lifecycle=RuleLifecycle.TEMPORARY,
            enabled=True,
            condition=_make_condition(device_ids=["cam-001", "cam-002"], query="x"),
            actions=[],
            action_descriptions=[],
            on_enter_actions=[_make_action(did="enter-d", iid="prop.2.1", value=True)],
            on_enter_desc=None,
            on_exit_actions=[],
            on_exit_desc="关灯",
            terminate_when="主人回家",
            exit_debounce_seconds=120,
        )
        rid = rule_repo.create(rule)
        assert rid is not None

        got = rule_repo.get_by_id(rid)
        assert got is not None
        assert got.task_id == TASK_ID
        assert got.mode == RuleMode.STATE
        assert got.lifecycle == RuleLifecycle.TEMPORARY
        assert len(got.on_enter_actions) == 1
        assert got.on_enter_actions[0].did == "enter-d"
        assert got.on_enter_desc is None
        assert got.on_exit_actions == []
        assert got.on_exit_desc == "关灯"
        assert got.terminate_when == "主人回家"
        assert got.exit_debounce_seconds == 120
        assert got.condition.perceive_device_ids == ["cam-001", "cam-002"]

    def test_action_5_fields_preserved(self, rule_repo):
        """RuleAction 5 字段全 round-trip：did/iid/value/idempotent/cooldown_minutes。"""
        action = RuleAction(
            did="dev-x",
            iid="prop.5.7",
            value="hello",
            idempotent=False,
            cooldown_minutes=30,
        )
        rule = _make_static_rule(name=_name("act"), actions=[action])
        rid = rule_repo.create(rule)
        got = rule_repo.get_by_id(rid)
        assert got.actions[0].did == "dev-x"
        assert got.actions[0].iid == "prop.5.7"
        assert got.actions[0].value == "hello"
        assert got.actions[0].idempotent is False
        assert got.actions[0].cooldown_minutes == 30

    def test_action_with_params_for_action_iid(self, rule_repo):
        """iid=action.* 时 params（list）round-trip。"""
        action = RuleAction(did="d1", iid="action.3.1", params=[1, "hello", True])
        rule = _make_static_rule(name=_name("act-call"), actions=[action])
        rid = rule_repo.create(rule)
        got = rule_repo.get_by_id(rid)
        assert got.actions[0].iid == "action.3.1"
        assert got.actions[0].params == [1, "hello", True]
        assert got.actions[0].value is None

    def test_default_values_when_minimal_create(self, rule_repo):
        """最小 create：lifecycle/exit_debounce_seconds 应该用 schema 默认值。"""
        rule = _make_static_rule(name=_name("min"))
        rid = rule_repo.create(rule)
        got = rule_repo.get_by_id(rid)
        assert got.lifecycle == RuleLifecycle.PERMANENT
        assert got.exit_debounce_seconds == 60
        assert got.action_descriptions == []
        assert got.on_enter_actions == []
        assert got.on_exit_actions == []
        # 没设 duration 时 duration_seconds=None, duration_ratio 兜底 0.8
        assert got.duration_seconds is None
        assert got.duration_ratio == 0.8

    def test_duration_fields_round_trip(self, rule_repo):
        """duration_seconds + duration_ratio create → get_by_id round-trip。"""
        rule = Rule(
            id="",
            name=_name("dur"),
            task_id=TASK_ID,
            mode=RuleMode.EVENT,
            lifecycle=RuleLifecycle.PERMANENT,
            enabled=True,
            condition=_make_condition(),
            actions=[_make_action()],
            duration_seconds=1800,
            duration_ratio=0.65,
        )
        rid = rule_repo.create(rule)
        got = rule_repo.get_by_id(rid)
        assert got.duration_seconds == 1800
        assert got.duration_ratio == 0.65


# ============================================================
# RuleRepo: 查询 / 排序 / 唯一性 / 计数
# ============================================================


class TestRuleRepoQuery:
    def test_get_by_name(self, rule_repo):
        rid = rule_repo.create(_make_static_rule(name=_name("lookup")))
        got = rule_repo.get_by_name(_name("lookup"))
        assert got is not None
        assert got.id == rid

    def test_get_by_name_not_found(self, rule_repo):
        assert rule_repo.get_by_name("nonexistent") is None

    def test_exists_by_name_with_exclude_id(self, rule_repo):
        """exclude_id 应让规则查名时把自己排除（patch 同名时不算冲突）。"""
        rid = rule_repo.create(_make_static_rule(name=_name("self")))
        assert rule_repo.exists_by_name(_name("self")) is True
        assert rule_repo.exists_by_name(_name("self"), exclude_id=rid) is False

    def test_get_all_ordered_by_created_desc(self, rule_repo, monkeypatch):
        """get_all 按 created_at DESC,最后插入的排第一。

        v10 起 created_at 是 INTEGER ms,monkeypatch 注入递增 ms 避免 real sleep。"""
        counter = iter(range(100))

        def _fake_now_ms():
            return 1717286400000 + next(counter)

        monkeypatch.setattr(
            "miloco.database.rule_repo.now_ms", _fake_now_ms
        )
        for i in range(3):
            rule_repo.create(_make_static_rule(name=_name(f"r{i}")))
        rules = rule_repo.get_all()
        assert len(rules) == 3
        names = [r.name for r in rules]
        assert names[0] == _name("r2")

    def test_get_all_enabled_only_filter(self, rule_repo):
        rule_repo.create(_make_static_rule(name=_name("on"), enabled=True))
        rule_repo.create(_make_static_rule(name=_name("off"), enabled=False))
        assert len(rule_repo.get_all()) == 2
        enabled = rule_repo.get_all(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0].name == _name("on")

    def test_count_all_and_enabled(self, rule_repo):
        rule_repo.create(_make_static_rule(name=_name("1"), enabled=True))
        rule_repo.create(_make_static_rule(name=_name("2"), enabled=True))
        rule_repo.create(_make_static_rule(name=_name("3"), enabled=False))
        assert rule_repo.count_all() == 3
        assert rule_repo.count_enabled() == 2


# ============================================================
# RuleRepo: 写操作（update / delete）
# ============================================================


class TestRuleRepoMutation:
    def test_update_persists_changed_fields(self, rule_repo):
        rid = rule_repo.create(_make_static_rule(name=_name("before")))
        rule = rule_repo.get_by_id(rid)
        rule.name = _name("after")
        rule.enabled = False
        rule.exit_debounce_seconds = 999
        ok = rule_repo.update(rule)
        assert ok is True
        got = rule_repo.get_by_id(rid)
        assert got.name == _name("after")
        assert got.enabled is False
        assert got.exit_debounce_seconds == 999

    def test_update_nonexistent_returns_false(self, rule_repo):
        rule = _make_static_rule(name=_name("ghost"))
        rule.id = "nonexistent-id"
        ok = rule_repo.update(rule)
        assert ok is False

    def test_update_task_id_syncs_task_link(self, rule_repo, real_db):
        """rule.task_id 变更时 task_link(kind='rule') 行必须一笔事务跟着搬家，
        否则 `task get 旧` 仍显示挂着 rule、`task get 新` 里查不到 → SSOT 分裂。
        """
        import miloco.database.connector as connector_module
        from miloco.utils.time_utils import now_iso

        # 建第二个占位 task 作为迁移目标
        with connector_module.get_db_connector().get_connection() as conn:
            conn.execute(
                "INSERT INTO task (task_id, description, status, created_at) "
                "VALUES ('task_new', 'move target', 'active', ?)",
                (now_iso(),),
            )
            conn.commit()

        rid = rule_repo.create(_make_static_rule(name=_name("mover")))
        rule = rule_repo.get_by_id(rid)
        rule.task_id = "task_new"
        assert rule_repo.update(rule) is True

        with connector_module.get_db_connector().get_connection() as conn:
            row = conn.execute(
                "SELECT task_id FROM task_link "
                "WHERE link_kind='rule' AND link_ref=?",
                (rid,),
            ).fetchone()
        assert row is not None
        assert row["task_id"] == "task_new"

    def test_delete_then_get_returns_none(self, rule_repo):
        rid = rule_repo.create(_make_static_rule(name=_name("del")))
        assert rule_repo.exists(rid) is True
        ok = rule_repo.delete(rid)
        assert ok is True
        assert rule_repo.exists(rid) is False
        assert rule_repo.get_by_id(rid) is None

    def test_delete_nonexistent_returns_false(self, rule_repo):
        assert rule_repo.delete("nonexistent-id") is False


# ============================================================
# RuleLogRepo: kind/after_ts 过滤、execute_result JSON 往返
# ============================================================


class TestRuleLogRepoQuery:
    def test_create_then_get_by_rule_id(self, log_repo):
        log_repo.create(_make_log(rule_id="r1"))
        results = log_repo.get_by_rule_id("r1")
        assert len(results) == 1
        assert results[0].rule_id == "r1"

    def test_execute_result_json_round_trip(self, log_repo):
        log_repo.create(_make_log(rule_id="r-result", with_result=True))
        got = log_repo.get_by_rule_id("r-result")[0]
        assert got.execute_result is not None
        assert got.execute_result.event == RuleEvent.ENTERED
        assert len(got.execute_result.action_results) == 1  # STATIC dispatched
        assert got.execute_result.action_results[0].result is True

    def test_filter_by_kind(self, log_repo):
        log_repo.create(_make_log(rule_id="r", kind=RuleLogKind.RULE_TRIGGER_SUCCESS))
        log_repo.create(_make_log(rule_id="r", kind=RuleLogKind.RULE_TRIGGER_FAILURE))
        all_logs = log_repo.get_by_rule_id("r")
        success_only = log_repo.get_by_rule_id(
            "r", kind=RuleLogKind.RULE_TRIGGER_SUCCESS
        )
        assert len(all_logs) == 2
        assert len(success_only) == 1
        assert success_only[0].kind == RuleLogKind.RULE_TRIGGER_SUCCESS

    def test_filter_by_after_ts(self, log_repo):
        log_repo.create(_make_log(rule_id="r", ts=1000))
        log_repo.create(_make_log(rule_id="r", ts=2000))
        log_repo.create(_make_log(rule_id="r", ts=3000))
        recent = log_repo.get_by_rule_id("r", after_ts=1500)
        assert len(recent) == 2
        # DESC
        assert recent[0].timestamp == 3000
        assert recent[1].timestamp == 2000

    def test_get_all_with_limit_offset(self, log_repo):
        for i in range(5):
            log_repo.create(_make_log(rule_id="r", ts=1000 + i))
        page1 = log_repo.get_all(limit=2, offset=0)
        page2 = log_repo.get_all(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].timestamp == 1004  # DESC

    def test_count_filtered(self, log_repo):
        log_repo.create(
            _make_log(rule_id="r", kind=RuleLogKind.RULE_TRIGGER_SUCCESS, ts=1000)
        )
        log_repo.create(
            _make_log(rule_id="r", kind=RuleLogKind.RULE_TRIGGER_FAILURE, ts=2000)
        )
        log_repo.create(
            _make_log(rule_id="other", kind=RuleLogKind.RULE_TRIGGER_SUCCESS, ts=3000)
        )
        assert log_repo.count_all() == 3
        assert log_repo.count_all(kind=RuleLogKind.RULE_TRIGGER_FAILURE) == 1
        assert log_repo.count_by_rule_id("r") == 2
        assert log_repo.count_by_rule_id("r", after_ts=1500) == 1


# ============================================================
# RuleLogRepo: 删除路径
# ============================================================


class TestRuleLogRepoDeletion:
    def test_delete_by_rule_id_only_affects_target(self, log_repo):
        log_repo.create(_make_log(rule_id="r1"))
        log_repo.create(_make_log(rule_id="r1"))
        log_repo.create(_make_log(rule_id="r2"))
        ok = log_repo.delete_by_rule_id("r1")
        assert ok is True
        assert log_repo.count_by_rule_id("r1") == 0
        assert log_repo.count_by_rule_id("r2") == 1

    def test_delete_before_days(self, log_repo):
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 30 * 24 * 3600 * 1000
        recent_ms = now_ms - 1 * 24 * 3600 * 1000
        log_repo.create(_make_log(rule_id="r", ts=old_ms))
        log_repo.create(_make_log(rule_id="r", ts=recent_ms))
        deleted = log_repo.delete_before_days(keep_days=7)
        assert deleted == 1
        assert log_repo.count_all() == 1

    def test_update_execute_result(self, log_repo):
        log = _make_log(rule_id="r", with_result=False)
        log_repo.create(log)
        new_result = RuleExecuteResult(
            event=RuleEvent.EXITED,
            dynamic_rule_event_sent=True,
        )
        ok = log_repo.update_execute_result(log.id, new_result)
        assert ok is True
        got = log_repo.get_by_rule_id("r")[0]
        assert got.execute_result is not None
        assert got.execute_result.event == RuleEvent.EXITED
        assert got.execute_result.dynamic_rule_event_sent is True


# ============================================================
# Corrupted row tolerance：单条坏行不能阻塞 init_rule_service
# ============================================================


class TestRuleRepoCorruptedRow:
    def test_get_all_skips_invalid_enum_row(self, rule_repo, real_db):
        """mode 列写入未知 enum 值的 row 应被 _dict_to_rule 跳过，
        其余 row 正常加载；这是 backend 启动时调 get_all 的兜底，
        防止一条坏数据让整个进程起不来。"""
        good_id = rule_repo.create(_make_static_rule(name=_name("good")))

        # 绕开 schema 直接 INSERT 一条 mode=非法 enum 的 row
        from miloco.database.connector import get_db_connector
        from miloco.utils.time_utils import now_iso

        ts = now_iso()
        with get_db_connector().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO rule (
                    id, name, task_id, mode, lifecycle, enabled,
                    condition, actions, action_descriptions,
                    on_enter_actions, on_exit_actions, exit_debounce_seconds,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "corrupt-1",
                    _name("bad"),
                    "bad_task",
                    "INVALID_MODE",  # 不在 RuleMode enum 中
                    "permanent",
                    1,
                    '{"perceive_device_ids":["cam-001"],"query":"x"}',
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    60,
                    ts,
                    ts,
                ),
            )
            conn.commit()

        rules = rule_repo.get_all()
        assert len(rules) == 1
        assert rules[0].id == good_id

    def test_log_get_all_skips_invalid_enum_row(self, log_repo, real_db):
        """rule_log 同样要对 kind 非法值容错。"""
        good_log = _make_log(rule_id="r-good", ts=1000)
        log_repo.create(good_log)

        from miloco.database.connector import get_db_connector

        with get_db_connector().get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO rule_log (
                    id, timestamp, kind, rule_id, rule_name, rule_query,
                    trigger_context, execute_result, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "log-corrupt-1",
                    2000,
                    "INVALID_KIND",  # 不在 RuleLogKind enum
                    "r-good",
                    "n",
                    "q",
                    "",
                    None,
                    2000,
                ),
            )
            conn.commit()

        logs = log_repo.get_all()
        assert len(logs) == 1
        assert logs[0].id == good_log.id
