# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""record 源（spec §6.4 ①层）。

这里只测源层：给定 record 上的累计与目标，喂出去的 bool 对不对、timer 排得对不对。
喂进条件层之后的事（边沿、方向映射、动作槽）在 test_rule.py 里测。
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from miloco.rule.record_source import (
    RecordRef,
    RecordSource,
    record_ref_of,
)
from miloco.rule.schema import (
    ConditionItem,
    Rule,
    RuleCondition,
    RuleConditionDNF,
    RuleDirection,
    RuleMode,
)

TASK_ID = "watch_tv"


def _make_rule(rule_id="rule-ms", source_type="record", spec=None, task_id=TASK_ID):
    item = ConditionItem(
        source_type=source_type,
        spec=spec if spec is not None else {
            "task_id": task_id, "kind": "duration", "op": ">=", "value": 120,
        },
    )
    return Rule(
        id=rule_id,
        name=f"[{task_id}] 累计达标",
        task_id=task_id,
        mode=RuleMode.EVENT,
        direction=RuleDirection.MILESTONE,
        condition=RuleCondition(perceive_device_ids=["__milestone_no_camera__"],
                                query="累计达标"),
        condition_dnf=RuleConditionDNF(any_of=[[item]]),
    )


async def _settle_arm(src):
    """等 arm 起的后台协程真的跑完。

    靠 ``sleep(0)`` 的话，断言"没喂值"在「等得不够」和「代码正确」两种情况下给
    同样的绿 —— 往 feed 里加一个 await 就静默变成不检查任何东西。
    """
    while src._arming:
        await asyncio.gather(*list(src._arming), return_exceptions=True)


def _make_source(record_state, refs=None):
    """返回 ``(source, fed)``；``fed`` 累积每次喂出去的 ``(rule_id, value, meta)``。"""
    svc = MagicMock()
    svc.read_duration_target_state = MagicMock(return_value=record_state)
    fed: list[tuple[str, bool, dict | None]] = []

    async def feed(rule_id, value, metadata=None):
        fed.append((rule_id, value, metadata))

    if refs is None:
        refs = [RecordRef(rule_id="rule-ms", task_id=TASK_ID)]
    return RecordSource(svc, feed, lambda _tid: list(refs)), fed, svc


# ---- 条件项识别 ----


class TestRecordRefOf:
    def test_recognizes_record_item(self):
        ref = record_ref_of(_make_rule())
        assert ref == RecordRef(rule_id="rule-ms", task_id=TASK_ID)

    def test_omni_item_is_not_a_record_ref(self):
        assert record_ref_of(_make_rule(source_type="omni")) is None

    def test_no_dnf_is_not_a_record_ref(self):
        rule = _make_rule()
        rule.condition_dnf = None
        assert record_ref_of(rule) is None

    @pytest.mark.parametrize(
        "spec",
        [
            {"task_id": TASK_ID, "kind": "progress", "op": ">=", "value": 8},
            {"task_id": TASK_ID, "kind": "duration", "op": ">", "value": 120},
            {"kind": "duration", "op": ">=", "value": 120},
        ],
        ids=["unsupported_kind", "unsupported_op", "missing_task_id"],
    )
    def test_unsupported_spec_does_not_trigger(self, spec):
        """不认识的形态返 None 而不是抛 —— 存量脏数据不该把感知链带崩。"""
        assert record_ref_of(_make_rule(spec=spec)) is None


# ---- arm ----


class TestArm:
    async def test_already_reached_feeds_true_immediately(self):
        src, fed, _ = _make_source((60, 60))
        src.arm(TASK_ID)
        await _settle_arm(src)
        assert [(r, v) for r, v, _ in fed] == [("rule-ms", True)]

    async def test_reached_feed_carries_the_numbers(self):
        """达标文案要用真实数字，不能只说"达标了"。"""
        src, fed, _ = _make_source((60, 75))
        src.arm(TASK_ID)
        await _settle_arm(src)
        _, _, meta = fed[0]
        assert meta["target_minutes"] == 60
        assert meta["accumulated_at_fire"] == 75
        assert meta["actual_target_at"]

    async def test_not_reached_schedules_timer_without_feeding(self):
        src, fed, _ = _make_source((60, 10))
        src.arm(TASK_ID)
        await _settle_arm(src)
        assert fed == []
        assert "rule-ms" in src._timers
        src.disarm(TASK_ID)

    async def test_no_target_does_not_feed(self):
        """没设目标 = 没有达标这件事。硬喂假会把已发出的达标在配上目标后重发。"""
        src, fed, _ = _make_source((None, 10))
        src.arm(TASK_ID)
        await _settle_arm(src)
        assert fed == []
        assert "rule-ms" not in src._timers

    async def test_no_duration_record_does_not_feed(self):
        src, fed, _ = _make_source(None)
        src.arm(TASK_ID)
        await _settle_arm(src)
        assert fed == []

    async def test_no_record_refs_no_timer(self):
        src, fed, _ = _make_source((60, 0), refs=[])
        src.arm(TASK_ID)
        await _settle_arm(src)
        assert fed == []
        assert src._timers == {}

    async def test_db_read_failure_does_not_raise(self):
        src, fed, svc = _make_source((60, 0))
        svc.read_duration_target_state = MagicMock(side_effect=RuntimeError("db down"))
        src.arm(TASK_ID)
        await _settle_arm(src)
        assert fed == []


# ---- timer 到点 ----


class TestTimerFire:
    async def test_fires_when_accumulated_catches_up(self):
        src, fed, _ = _make_source((60, 60))
        await src._feed_after(RecordRef("rule-ms", TASK_ID), 0, target_at_arm=60)
        assert [(r, v) for r, v, _ in fed] == [("rule-ms", True)]

    async def test_rearms_when_accumulated_fell_short(self):
        """睡的这段时间 session 断过 / 目标被调高 —— 重排，别把这天的达标丢掉。"""
        src, fed, _ = _make_source((60, 30))
        await src._feed_after(RecordRef("rule-ms", TASK_ID), 0, target_at_arm=60)
        assert fed == []
        assert "rule-ms" in src._timers
        src.disarm(TASK_ID)

    async def test_reads_current_target_at_fire_not_the_one_from_arm(self):
        """到点重算，不信 arm 时那笔账 —— 中间可能改过目标。"""
        src, fed, _ = _make_source((30, 40))
        await src._feed_after(RecordRef("rule-ms", TASK_ID), 0, target_at_arm=120)
        assert [(r, v) for r, v, _ in fed] == [("rule-ms", True)]
        assert fed[0][2]["target_minutes"] == 30


# ---- settle / disarm / reset ----


class TestSessionBoundary:
    async def test_settle_feeds_true_when_reached(self):
        """存在的理由是时间跳变：timer 按挂起前的时钟排，睡过头醒来时早该达标。"""
        src, fed, _ = _make_source((60, 61))
        await src.settle(TASK_ID)
        assert [(r, v) for r, v, _ in fed] == [("rule-ms", True)]

    async def test_settle_silent_when_not_reached(self):
        src, fed, _ = _make_source((60, 10))
        await src.settle(TASK_ID)
        assert fed == []

    async def test_settle_keeps_the_timer(self):
        """撤 timer 归 disarm —— 这次退出可能被别的条件挡住，session 还在。"""
        src, _, _ = _make_source((60, 10))
        src.arm(TASK_ID)
        await _settle_arm(src)
        await src.settle(TASK_ID)
        assert "rule-ms" in src._timers
        src.disarm(TASK_ID)

    async def test_disarm_drops_pending_timer(self):
        src, fed, _ = _make_source((60, 10))
        src.arm(TASK_ID)
        await _settle_arm(src)
        handle = src._timers["rule-ms"]
        src.disarm(TASK_ID)
        assert src._timers == {}
        await asyncio.sleep(0)
        # 看 done 而不是 cancelled: ``_feed_after`` 自己吞掉 CancelledError 正常返回,
        # 所以取消过的 timer 是 done 而非 cancelled。没被取消的话它还在 sleep, 不 done。
        assert handle.done()
        assert fed == []

    async def test_reset_feeds_false(self):
        """第二天能重新通知的全部机制。少了它条件停在真，达标不再产生新边沿。"""
        src, fed, _ = _make_source((60, 0))
        await src.reset(TASK_ID)
        assert [(r, v) for r, v, _ in fed] == [("rule-ms", False)]

    async def test_cancel_rule_drops_timer_by_rule_id(self):
        """rule 被删时它已不在 rule 表里，按 task 遍历找不到它。"""
        src, _, _ = _make_source((60, 10))
        src.arm(TASK_ID)
        await _settle_arm(src)
        src.cancel_rule("rule-ms")
        assert src._timers == {}


# ---- 跨日 ----


class TestRollOver:
    async def test_settles_old_day_then_zeroes_then_rearms(self):
        src, fed, _ = _make_source((60, 0))
        await src.roll_over(TASK_ID, pre_rollover_state=(60, 90))
        await _settle_arm(src)
        assert [v for _, v, _ in fed] == [True, False]
        assert "rule-ms" in src._timers
        src.disarm(TASK_ID)

    async def test_zeroes_without_settling_when_old_day_fell_short(self):
        src, fed, _ = _make_source((60, 0))
        await src.roll_over(TASK_ID, pre_rollover_state=(60, 30))
        await _settle_arm(src)
        assert [v for _, v, _ in fed] == [False]
        src.disarm(TASK_ID)

    async def test_zeroes_when_no_snapshot(self):
        src, fed, _ = _make_source((60, 0))
        await src.roll_over(TASK_ID, pre_rollover_state=None)
        await _settle_arm(src)
        assert [v for _, v, _ in fed] == [False]
        src.disarm(TASK_ID)


# ---- 达标规则是派生物 (spec §6.4 / §9) ----


def _legacy_rule(rule_id="rule-legacy", task_id=TASK_ID, on_target_desc="达标了"):
    """用户在 rule 上填 on_target_desc 的旧路径 —— skill 现在还走这条。"""
    rule = Rule(
        id=rule_id,
        name=f"[{task_id}] 计时",
        task_id=task_id,
        mode=RuleMode.STATE,
        condition=RuleCondition(perceive_device_ids=["cam-001"], query="有人在弹琴"),
    )
    rule.on_target_desc = on_target_desc
    return rule


def _rule_service(record_kind, duration_state, task_view=None, rules=None):
    from unittest.mock import AsyncMock

    from miloco.rule.runner import RuleRunner
    from miloco.rule.service import RuleService

    record_svc = MagicMock()
    record_svc.detect_record_kind = MagicMock(return_value=record_kind)
    record_svc.read_duration_target_state = MagicMock(return_value=duration_state)
    runner = RuleRunner(
        rules=[], miot_proxy=AsyncMock(), rule_log_repo=MagicMock(),
        task_record_service=record_svc,
    )
    task_repo = MagicMock()
    task_repo.get_full_view = MagicMock(
        return_value=task_view
        if task_view is not None
        else {"actions": {"on_target_desc": "达标了"}}
    )
    rule_repo = MagicMock()
    rule_repo.list_by_task = MagicMock(return_value=list(rules or []))
    rule_repo.exists_by_name = MagicMock(return_value=False)
    rule_repo.create = MagicMock(return_value="new-milestone-id")
    rule_repo.delete = MagicMock(return_value=True)
    svc = RuleService(
        rule_repo, MagicMock(), runner, AsyncMock(),
        task_repo=task_repo, task_record_service=record_svc,
    )
    return svc, rule_repo


class TestManualMilestoneIsRejected:
    def test_creating_a_milestone_rule_by_hand_is_rejected(self):
        """它是派生物 —— 两条产出路径就要回答"哪条说了算"。"""
        from miloco.middleware.exceptions import ValidationException
        from miloco.rule.service import _validate_rule_consistency

        with pytest.raises(ValidationException, match="自动维护"):
            _validate_rule_consistency(_make_rule())


class TestMilestoneReconcile:
    """三样齐备就该有一条, 任一缺失就该没有。"""

    def test_creates_when_everything_is_in_place(self):
        svc, repo = _rule_service("duration", (60, 0))
        svc.reconcile_milestone_rule(TASK_ID)

        assert repo.create.called
        created = repo.create.call_args[0][0]
        assert created.resolved_direction is RuleDirection.MILESTONE
        assert created.task_id == TASK_ID

    def test_created_rule_carries_a_record_condition_item(self):
        """没有 record 条件项的话 record 源认不出它, timer 永远不排。"""
        svc, repo = _rule_service("duration", (60, 0))
        svc.reconcile_milestone_rule(TASK_ID)

        item = repo.create.call_args[0][0].condition_dnf.any_of[0][0]
        assert item.source_type == "record"
        assert item.spec["task_id"] == TASK_ID
        # 阈值现读, 不存副本 —— 存了用户改目标后两份就分叉
        assert "value" not in item.spec

    def test_does_not_create_without_a_target_action(self):
        svc, repo = _rule_service("duration", (60, 0), task_view={"actions": {}})
        svc.reconcile_milestone_rule(TASK_ID)

        assert not repo.create.called

    def test_does_not_create_without_a_threshold(self):
        svc, repo = _rule_service("duration", (None, 0))
        svc.reconcile_milestone_rule(TASK_ID)

        assert not repo.create.called

    def test_does_not_create_without_a_duration_record(self):
        svc, repo = _rule_service(None, None)
        svc.reconcile_milestone_rule(TASK_ID)

        assert not repo.create.called

    def test_is_idempotent_when_one_already_exists(self):
        svc, repo = _rule_service("duration", (60, 0), rules=[_make_rule()])
        svc.reconcile_milestone_rule(TASK_ID)

        assert not repo.create.called
        assert not repo.delete.called

    def test_deletes_when_the_target_action_is_cleared(self):
        svc, repo = _rule_service(
            "duration", (60, 0), task_view={"actions": {}}, rules=[_make_rule()]
        )
        svc.reconcile_milestone_rule(TASK_ID)

        repo.delete.assert_called_once_with("rule-ms")

    def test_deletes_when_the_threshold_is_cleared(self):
        svc, repo = _rule_service("duration", (None, 0), rules=[_make_rule()])
        svc.reconcile_milestone_rule(TASK_ID)

        repo.delete.assert_called_once_with("rule-ms")

    def test_keeps_one_and_drops_the_duplicates(self):
        svc, repo = _rule_service(
            "duration", (60, 0),
            rules=[_make_rule("ms-1"), _make_rule("ms-2"), _make_rule("ms-3")],
        )
        svc.reconcile_milestone_rule(TASK_ID)

        assert not repo.create.called
        assert [c[0][0] for c in repo.delete.call_args_list] == ["ms-2", "ms-3"]

    def test_leaves_other_rules_alone(self):
        """只管达标那条 —— 误删会话规则会让整个 task 停摆。"""
        svc, repo = _rule_service(
            "duration", (60, 0), task_view={"actions": {}},
            rules=[_legacy_rule("r-session")],
        )
        svc.reconcile_milestone_rule(TASK_ID)

        assert not repo.delete.called

    def test_does_nothing_when_the_record_read_fails(self):
        """读不出来时按"别动"处理 —— 当成"不该有"会在一次抖动里删掉真规则。"""
        svc, repo = _rule_service("duration", (60, 0), rules=[_make_rule()])
        svc._task_record_service.read_duration_target_state = MagicMock(
            side_effect=RuntimeError("db down")
        )
        svc.reconcile_milestone_rule(TASK_ID)

        assert not repo.delete.called
        assert not repo.create.called


class TestLegacyTargetRecordValidation:
    """用户在 rule 上填 on_target_desc 那条旧路径的校验。"""

    def test_rule_without_target_desc_is_skipped(self):
        svc, _ = _rule_service(None, None)
        assert svc._target_record_task_id(_legacy_rule(on_target_desc=None)) is None

    def test_rejects_when_the_task_has_no_record(self):
        from miloco.middleware.exceptions import ValidationException

        svc, _ = _rule_service(None, None)
        with pytest.raises(ValidationException, match="无活跃 record"):
            svc._validate_target_record(_legacy_rule())

    def test_rejects_when_target_minutes_is_unset(self):
        from miloco.middleware.exceptions import ValidationException

        svc, _ = _rule_service("duration", (None, 0))
        with pytest.raises(ValidationException, match="target_minutes"):
            svc._validate_target_record(_legacy_rule())

    def test_reconfigure_recomputes_before_reading_the_topology(self):
        """重新配置必须先重算 —— 晚一步的话代建的那条进不了这次的拓扑。"""
        svc, repo = _rule_service("duration", (60, 0))
        svc.reconfigure_task(TASK_ID)

        assert repo.create.called
