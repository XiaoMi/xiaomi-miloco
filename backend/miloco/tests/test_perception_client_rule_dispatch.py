# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for PerceptionEngineProxy.handle_realtime_perception_result rule dispatch.

层 3:client.py 调用 RuleService.update_state 的真 did 化 + EXITED 精确推退路径。
跟 test_perception_client.py 的 main-loop dispatch 测试职责分开,本文件专注:
- update_state 第二参 source_did 用 matched_rule.source_device_ids[0],不再硬编码 "perception"
- EXITED false 广播按 result.device_rule_map 精确推退,不再用 get_enabled_rule_ids 全集
- early_sent_rule_ids 改为 set[tuple[str, str]],去重粒度从 rule_id 改为 (rule_id, did)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from miloco.perception.client import PerceptionEngineProxy
from miloco.perception.types import MatchedRule, RealtimePerceptionResult


@pytest.fixture
def proxy():
    p = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
    p.perception_engine = MagicMock()
    p._last_captions = {}
    p._executor = None
    return p


def _capture_calls():
    """工厂:返回 (capture coroutine, calls list)。

    capture 兼容 update_state(rule_id, source_did, current_bool, context="", **kwargs)。
    """
    calls: list[tuple] = []

    async def capture(rule_id, source_did, current_bool, context="", **kwargs):
        calls.append((rule_id, source_did, current_bool, context))

    return capture, calls


def _fake_mgr(enabled_rule_ids: list[str], capture_fn) -> MagicMock:
    """Mock manager.rule_service:暴露 update_state + get_enabled_rule_ids。"""
    mgr = MagicMock()
    mgr.rule_service.update_state = capture_fn
    mgr.rule_service.get_enabled_rule_ids = MagicMock(return_value=enabled_rule_ids)
    return mgr


async def test_entered_uses_real_did_not_perception_string(proxy):
    """matched_rule 携带 source_device_ids=["cam_A"] → update_state 用真 did,
    不再是字符串 "perception"。"""
    result = RealtimePerceptionResult(
        skipped=False,
        matched_rules=[
            MatchedRule(
                rule_id="rule_X",
                confidence=1.0,
                reason="人来了",
                source_device_ids=["cam_A"],
            )
        ],
        device_rule_map={"cam_A": ["rule_X"]},
    )
    capture, calls = _capture_calls()
    with patch("miloco.manager.get_manager", return_value=_fake_mgr(["rule_X"], capture)):
        await proxy.handle_realtime_perception_result(result, artifacts=None)

    assert ("rule_X", "cam_A", True, "人来了") in calls
    assert not any(c[1] == "perception" for c in calls), (
        "update_state 不应再用字符串 'perception' 作 source_did"
    )


async def test_exited_only_dispatched_pairs(proxy):
    """device_rule_map={A:[X], B:[]}, 0 matched → 只 update_state ("X","A",False)。
    cam_B 没下发过 rule_X,**不应**出现 ("X","B",False)。"""
    result = RealtimePerceptionResult(
        skipped=False,
        matched_rules=[],
        device_rule_map={"cam_A": ["rule_X"], "cam_B": []},
    )
    capture, calls = _capture_calls()
    with patch("miloco.manager.get_manager", return_value=_fake_mgr(["rule_X"], capture)):
        await proxy.handle_realtime_perception_result(result, artifacts=None)

    assert ("rule_X", "cam_A", False, "") in calls
    assert not any(c[1] == "cam_B" for c in calls), (
        "cam_B 未下发该 rule,不应被推退"
    )
    assert len(calls) == 1


async def test_exited_skips_matched_pair(proxy):
    """rule_X 下发给 A 和 B,A 命中 → 只 A 走 True,B 走 False。"""
    result = RealtimePerceptionResult(
        skipped=False,
        matched_rules=[
            MatchedRule(
                rule_id="rule_X",
                confidence=1.0,
                reason="r",
                source_device_ids=["cam_A"],
            )
        ],
        device_rule_map={"cam_A": ["rule_X"], "cam_B": ["rule_X"]},
    )
    capture, calls = _capture_calls()
    with patch("miloco.manager.get_manager", return_value=_fake_mgr(["rule_X"], capture)):
        await proxy.handle_realtime_perception_result(result, artifacts=None)

    assert ("rule_X", "cam_A", True, "r") in calls
    assert ("rule_X", "cam_B", False, "") in calls
    assert len(calls) == 2


async def test_cycle_source_states_include_matched_and_false_pairs(proxy):
    """同一 cycle 内 True / False 调用都携带完整 source 快照。

    duration_seconds 依赖这个快照避免先处理的 source 读到后处理 source 的上一帧状态。
    """
    result = RealtimePerceptionResult(
        skipped=False,
        matched_rules=[
            MatchedRule(
                rule_id="rule_X",
                confidence=1.0,
                reason="A 命中",
                source_device_ids=["cam_A"],
            )
        ],
        device_rule_map={"cam_A": ["rule_X"], "cam_B": ["rule_X"]},
    )
    calls: list[tuple[str, str, bool, dict[str, bool] | None]] = []

    async def capture(rule_id, source_did, current_bool, context="", **kwargs):
        calls.append(
            (
                rule_id,
                source_did,
                current_bool,
                kwargs.get("cycle_source_states"),
            )
        )

    with patch("miloco.manager.get_manager", return_value=_fake_mgr(["rule_X"], capture)):
        await proxy.handle_realtime_perception_result(result, artifacts=None)

    assert calls == [
        ("rule_X", "cam_A", True, {"cam_A": True, "cam_B": False}),
        ("rule_X", "cam_B", False, {"cam_A": True, "cam_B": False}),
    ]


async def test_early_sent_dedup_per_rule_did_pair(proxy):
    """early 阶段 cam_A 已上报 → set 含 ("rule_X","cam_A");终态 result 也含
    rule_X@cam_A + rule_X@cam_B → A 不重打 True,B 照常打。"""
    result = RealtimePerceptionResult(
        skipped=False,
        matched_rules=[
            MatchedRule(
                rule_id="rule_X",
                confidence=1.0,
                reason="A 命中",
                source_device_ids=["cam_A"],
            ),
            MatchedRule(
                rule_id="rule_X",
                confidence=1.0,
                reason="B 命中",
                source_device_ids=["cam_B"],
            ),
        ],
        device_rule_map={"cam_A": ["rule_X"], "cam_B": ["rule_X"]},
    )
    capture, calls = _capture_calls()
    with patch("miloco.manager.get_manager", return_value=_fake_mgr(["rule_X"], capture)):
        await proxy.handle_realtime_perception_result(
            result,
            early_sent_rule_ids={("rule_X", "cam_A")},  # cam_A 已 early 上报
            artifacts=None,
        )

    # cam_A 不重打;cam_B 应当照常打 True;无任何 False 广播(都已命中)
    a_true = [c for c in calls if c == ("rule_X", "cam_A", True, "A 命中")]
    b_true = [c for c in calls if c == ("rule_X", "cam_B", True, "B 命中")]
    assert len(a_true) == 0, "cam_A early 已上报,终态应去重不重打"
    assert len(b_true) == 1, "cam_B 终态应正常 update_state(True)"
    assert not any(c[2] is False for c in calls)


async def test_omni_error_empty_map_no_state_change(proxy):
    """OmniError 兜底:device_rule_map={} → update_state 完全不被调用,状态保持。"""
    result = RealtimePerceptionResult(
        skipped=True,
        error_code="ReadTimeout",
        matched_rules=[],
        device_rule_map={},  # 空 dict = 本 cycle 无下发
    )
    capture, calls = _capture_calls()
    with patch(
        "miloco.manager.get_manager",
        return_value=_fake_mgr(["rule_X", "rule_Y"], capture),
    ):
        await proxy.handle_realtime_perception_result(result, artifacts=None)

    assert calls == [], "OmniError 兜底不应推退任何 (rule_id, did) 桶"


async def test_source_device_ids_empty_fallback_to_perception(proxy):
    """matched_rule.source_device_ids=[] 异常态(pipeline 未注入)→ 兜底 "perception",
    不抛 IndexError;保留 fallback 行为以防上游异常。"""
    result = RealtimePerceptionResult(
        skipped=False,
        matched_rules=[
            MatchedRule(
                rule_id="rule_X",
                confidence=1.0,
                reason="anomaly",
                source_device_ids=[],  # 异常态
            )
        ],
        device_rule_map={"cam_A": ["rule_X"]},
    )
    capture, calls = _capture_calls()
    with patch("miloco.manager.get_manager", return_value=_fake_mgr(["rule_X"], capture)):
        await proxy.handle_realtime_perception_result(result, artifacts=None)

    # 兜底 source_did = "perception";cam_A 仍走 False 广播(没在 matched_pairs)
    assert ("rule_X", "perception", True, "anomaly") in calls
    assert ("rule_X", "cam_A", False, "") in calls


async def test_disabled_rule_during_cycle_skipped(proxy):
    """rule 下发后在 cycle 内被 disable:false 广播应跳过该 rule,不报错。"""
    result = RealtimePerceptionResult(
        skipped=False,
        matched_rules=[],
        device_rule_map={"cam_A": ["rule_X", "rule_Y"]},
    )
    capture, calls = _capture_calls()
    # rule_Y 已 disable(不在 enabled list)
    with patch(
        "miloco.manager.get_manager",
        return_value=_fake_mgr(["rule_X"], capture),
    ):
        await proxy.handle_realtime_perception_result(result, artifacts=None)

    assert ("rule_X", "cam_A", False, "") in calls
    assert not any(c[0] == "rule_Y" for c in calls), (
        "rule_Y 已 disable,false 广播应跳过"
    )
    assert len(calls) == 1
