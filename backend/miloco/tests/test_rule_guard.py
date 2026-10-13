# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""前提规则在 rule 侧的约束: 不是进出路径、不配动作、不配累计窗口。"""

from __future__ import annotations

import pytest
from miloco.middleware.exceptions import ValidationException
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleCondition,
    RuleDirection,
    RuleMode,
    task_rule_set_error,
)
from miloco.rule.service import _rule_action_slots, _validate_rule_consistency


def _guard(**kw) -> Rule:
    return Rule(
        id="g1",
        name="[t1] 空调开着",
        task_id="t1",
        direction=RuleDirection.GUARD,
        condition=RuleCondition(perceive_device_ids=["cam-001"], query="空调 开关 = 开"),
        **kw,
    )


def _action() -> RuleAction:
    return RuleAction(did="device-001", iid="prop.2.1", value=True, idempotent=True)


# ── mode 占位 ─────────────────────────────────────────────────────


def test_guard_coerces_mode_to_event():
    """guard 在 mode 里没有对应项, 存一个自洽的占位值。"""
    assert _guard().mode is RuleMode.EVENT


# ── 不是进出路径 (§9) ──────────────────────────────────────────────


def test_guard_alone_is_not_an_entry_path():
    """只挂前提的 task 还没有进路径, 但装配是分步的, 不拦。"""
    assert task_rule_set_error([RuleDirection.GUARD]) is None


def test_guard_does_not_break_session_exclusivity():
    """session 必须独占 task, 前提不算占位 —— 算了的话这个合法配置会被拒。"""
    assert task_rule_set_error([RuleDirection.SESSION, RuleDirection.GUARD]) is None


def test_guard_does_not_count_as_entry_path():
    """exit + guard 仍然没有进路径。"""
    error = task_rule_set_error([RuleDirection.EXIT, RuleDirection.GUARD])
    assert error is not None and "没有进路径" in error


def test_guard_does_not_count_as_exit_path():
    """配了达标动作要有出路径, 前提不顶替。"""
    error = task_rule_set_error(
        [RuleDirection.ENTER, RuleDirection.GUARD], has_target_action=True
    )
    assert error is not None and "exit" in error


# ── 不配动作 ──────────────────────────────────────────────────────


def test_guard_rejects_actions():
    with pytest.raises(ValidationException, match="前提规则"):
        _validate_rule_consistency(_guard(actions=[_action()]))


def test_guard_rejects_action_descriptions():
    with pytest.raises(ValidationException, match="前提规则"):
        _validate_rule_consistency(_guard(action_descriptions=["开灯"]))


def test_guard_without_actions_passes():
    _validate_rule_consistency(_guard())


# ── 不配累计窗口 ──────────────────────────────────────────────────


def test_guard_rejects_duration_seconds():
    """前提读的是条件层的当前值, 滑窗只影响 fire 路径 —— 配了静默无效。"""
    with pytest.raises(ValidationException, match="前提规则"):
        _validate_rule_consistency(_guard(duration_seconds=600))


# ── 不碰 task 的动作槽 ────────────────────────────────────────────


def test_guard_owns_no_slot():
    assert _rule_action_slots(_guard()) == {}


def test_guard_clearing_its_own_actions_touches_no_slot():
    """前提自己的动作字段被显式改空时也不许写 task 槽 —— 写了就把同 task 那条
    真正的进入规则的动作清掉。"""
    assert _rule_action_slots(_guard(), changed_fields={"actions"}) == {}
