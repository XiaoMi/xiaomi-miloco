# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""端到端测试(层 4):perception client.handle_realtime_perception_result 串到
真 RuleRunner。多周期模拟摄像头进出,锁定 per-device 状态机的实际部署行为。

层 1-3 mock 出 update_state 调用观察参数;本层用真 RuleRunner 让状态机跑起
来,catch 多函数组合后的边界 bug——比如 cam_A 离线 N 个周期后回归,rule
状态是否真的保持;duration 跨摄像头 OR 是否真的累计;pending_exit 是否真
的不被旁观摄像头污染。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from miloco.perception.client import PerceptionEngineProxy
from miloco.perception.types import MatchedRule, RealtimePerceptionResult
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleCondition,
    RuleLifecycle,
    RuleMode,
)

TASK_ID = "e2e-task"


def _make_state_rule(rule_id: str, device_ids: list[str]) -> Rule:
    """STATE mode rule:on_enter / on_exit 各执行一个 prop 写操作,便于通过
    miot_proxy.set_device_properties 计数 fire 次数。"""
    return Rule(
        id=rule_id,
        name=f"[{TASK_ID}] {rule_id}",
        task_id=TASK_ID,
        mode=RuleMode.STATE,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=RuleCondition(perceive_device_ids=device_ids, query="人出现"),
        on_enter_actions=[
            RuleAction(did=f"enter-{rule_id}", iid="prop.2.1", value=True, idempotent=True)
        ],
        on_exit_actions=[
            RuleAction(did=f"exit-{rule_id}", iid="prop.2.1", value=True, idempotent=True)
        ],
        exit_debounce_seconds=0,  # 加速 EXIT fire
    )


@pytest.fixture
def mock_miot_proxy():
    proxy = AsyncMock()
    proxy.get_camera_dids = AsyncMock(return_value=["cam_A", "cam_B"])
    proxy.get_device_properties = AsyncMock(return_value=[{"code": 0, "value": False}])
    proxy.set_device_properties = AsyncMock(return_value=[{"code": 0}])
    proxy.call_device_action = AsyncMock(return_value={"code": 0})
    return proxy


@pytest.fixture
def mock_log_repo():
    repo = MagicMock()
    repo.create = MagicMock(return_value="log-id")
    repo.get_all = MagicMock(return_value=[])
    repo.get_by_rule_id = MagicMock(return_value=[])
    repo.count_all = MagicMock(return_value=0)
    repo.count_by_rule_id = MagicMock(return_value=0)
    repo.delete_by_rule_id = MagicMock(return_value=True)
    repo.delete_before_days = MagicMock(return_value=0)
    return repo


@pytest.fixture
def proxy_with_runner(mock_miot_proxy, mock_log_repo):
    """PerceptionEngineProxy + 真 RuleRunner。返回 (proxy, runner, manager_patcher_factory)。

    manager_patcher_factory:context manager,patch get_manager 返回包了 runner 的
    fake mgr。test 内部用 `with mgr_ctx()` 即可,内部 cycle 共享同一 runner。
    """
    p = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
    p.perception_engine = MagicMock()
    p._last_captions = {}
    p._executor = None

    mock_task_record_service = MagicMock()
    mock_task_record_service.read_duration_target_state = MagicMock(return_value=None)
    mock_task_record_service.detect_record_kind = MagicMock(return_value=None)

    runner = RuleRunner(
        rules=[],
        miot_proxy=mock_miot_proxy,
        rule_log_repo=mock_log_repo,
        sample_interval_seconds=0.5,
        task_record_service=mock_task_record_service,
    )

    fake_mgr = MagicMock()
    fake_mgr.rule_service.update_state = runner.update_state
    fake_mgr.rule_service.get_enabled_rule_ids = lambda: [
    fake_mgr.rule_service.get_all_rules = AsyncMock(return_value=[])
        r.id for r in runner.get_enabled_rules()
    ]

    def mgr_ctx():
        return patch("miloco.manager.get_manager", return_value=fake_mgr)

    return p, runner, mgr_ctx


def _result(
    matched: list[tuple[str, list[str], str]] | None = None,
    device_rule_map: dict[str, list[str]] | None = None,
) -> RealtimePerceptionResult:
    """构造 RealtimePerceptionResult:matched 每项 (rule_id, source_dids, reason)。"""
    matched_rules = [
        MatchedRule(rule_id=rid, confidence=1.0, reason=reason, source_device_ids=dids)
        for rid, dids, reason in (matched or [])
    ]
    return RealtimePerceptionResult(
        skipped=False,
        matched_rules=matched_rules,
        device_rule_map=device_rule_map or {},
    )


# ============================================================
# E2E case
# ============================================================


@pytest.mark.asyncio
async def test_e2e_single_cam_unchanged(proxy_with_runner, mock_miot_proxy):
    """单摄像头部署:rule 绑 [cam_A],ENTER → 持续命中 → 退出。锁定回归——
    改动后单摄像头行为等价(状态机第二维从 'perception' 变 'cam_A',但桶语义不变)。"""
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_X", ["cam_A"]))

    with mgr_ctx():
        # cycle 1: 命中 → ENTERED
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_X", ["cam_A"], "人来")],
                    device_rule_map={"cam_A": ["rule_X"]}),
            clips_by_device=None,
        )
        # cycle 2: 未命中,pending_exit 1st
        await proxy.handle_realtime_perception_result(
            _result(device_rule_map={"cam_A": ["rule_X"]}),
            clips_by_device=None,
        )
        # cycle 3: 未命中,确认 EXIT → schedule debounce(seconds=0 立即 fire)
        await proxy.handle_realtime_perception_result(
            _result(device_rule_map={"cam_A": ["rule_X"]}),
            clips_by_device=None,
        )

    await asyncio.sleep(0.05)
    await runner.drain()

    dids = [
        call[0][0][0].did
        for call in mock_miot_proxy.set_device_properties.call_args_list
    ]
    assert dids == ["enter-rule_X", "exit-rule_X"], (
        f"单摄像头 enter→exit 完整周期失败,实际 fire 列表 {dids}"
    )


@pytest.mark.asyncio
async def test_e2e_cam_a_offline_rule_state_preserved(
    proxy_with_runner, mock_miot_proxy
):
    """核心修复验证:rule 绑 [cam_A],cycle 中 cam_A 离线只来 cam_B 时,rule
    状态应保持(device_rule_map[B] 不含 rule_X → 没有 update_state 推退)。
    旧行为会无差别广播 False 把 rule 错误推退。"""
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_X", ["cam_A"]))

    with mgr_ctx():
        # cycle 1: cam_A 在线 + 命中 → ENTERED
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_X", ["cam_A"], "命中")],
                    device_rule_map={"cam_A": ["rule_X"]}),
            clips_by_device=None,
        )
        # cycle 2-4: cam_A 离线,只有 cam_B 上线(rule 没绑 B,device_rule_map[B]=[])
        for _ in range(3):
            await proxy.handle_realtime_perception_result(
                _result(device_rule_map={"cam_B": []}),
                clips_by_device=None,
            )

    await runner.drain()

    # 只有 1 次 fire(enter),无 exit
    dids = [
        call[0][0][0].did
        for call in mock_miot_proxy.set_device_properties.call_args_list
    ]
    assert dids == ["enter-rule_X"], (
        f"cam_A 离线期间 rule 不应被推退,实际 fire 列表 {dids}"
    )
    # rule 状态仍为 True
    assert runner._last_rule_state.get("rule_X") is True
    # cam_A 桶仍为 True,cam_B 桶根本不存在(从未下发)
    assert runner._last_source_state.get(("rule_X", "cam_A")) is True
    assert ("rule_X", "cam_B") not in runner._last_source_state


@pytest.mark.asyncio
async def test_e2e_multi_cam_or_aggregation(proxy_with_runner, mock_miot_proxy):
    """rule 绑 [A,B](广播):cam_A 先命中,cam_B 后命中接力,rule 保持 True;
    两 cam 都不命中 ×2 才进 EXITED。"""
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_or", ["cam_A", "cam_B"]))

    dm = {"cam_A": ["rule_or"], "cam_B": ["rule_or"]}

    with mgr_ctx():
        # cycle 1: cam_A 命中 → ENTERED via A
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_or", ["cam_A"], "A 命中")], device_rule_map=dm),
            clips_by_device=None,
        )
        # cycle 2: cam_B 命中,cam_A 未命中 → A 桶 1st pending,B 桶 ENTER 但 rule 已 True
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_or", ["cam_B"], "B 命中")], device_rule_map=dm),
            clips_by_device=None,
        )
        # cycle 3: cam_B 继续命中,cam_A 未命中 → A 桶第二帧 false 确认,B 桶继续 true → rule 仍 True
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_or", ["cam_B"], "B 持续")], device_rule_map=dm),
            clips_by_device=None,
        )
        # cycle 4-5: 都不命中 → 两桶都翻 false → EXITED
        for _ in range(2):
            await proxy.handle_realtime_perception_result(
                _result(device_rule_map=dm),
                clips_by_device=None,
            )

    await asyncio.sleep(0.05)
    await runner.drain()

    dids = [
        call[0][0][0].did
        for call in mock_miot_proxy.set_device_properties.call_args_list
    ]
    assert dids == ["enter-rule_or", "exit-rule_or"], (
        f"多 cam 接力期间 rule 应稳定 True,失败 fire 列表 {dids}"
    )


@pytest.mark.asyncio
async def test_e2e_pending_exit_no_cross_pollution(
    proxy_with_runner, mock_miot_proxy
):
    """cam_A 进 pending_exit 时 cam_B 帧不应清掉 cam_A 的 pending(per-did 桶隔离)。
    本 case 走完整 perception client 入口 + 多 source matched_rules。"""
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_pex", ["cam_A", "cam_B"]))

    dm = {"cam_A": ["rule_pex"], "cam_B": ["rule_pex"]}

    with mgr_ctx():
        # cycle 1: A 和 B 都命中 → ENTERED(由 A 或 B 触发,rule 翻 True)
        await proxy.handle_realtime_perception_result(
            _result(
                matched=[
                    ("rule_pex", ["cam_A"], "A"),
                    ("rule_pex", ["cam_B"], "B"),
                ],
                device_rule_map=dm,
            ),
            clips_by_device=None,
        )
        assert runner._last_source_state[("rule_pex", "cam_A")] is True
        assert runner._last_source_state[("rule_pex", "cam_B")] is True

        # cycle 2: 只有 B 命中 → A 桶喂 false(false 广播路径),1st pending
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_pex", ["cam_B"], "B")], device_rule_map=dm),
            clips_by_device=None,
        )
        # A 应在 pending_source_exit 里(prev=True, this=False, 1st frame)
        assert ("rule_pex", "cam_A") in runner._pending_source_exit, (
            "A 的 1st false 未进 pending"
        )
        # B 不应在 pending(B 这一帧仍 True,update_state 路径不进 pending_exit)
        assert ("rule_pex", "cam_B") not in runner._pending_source_exit

    await runner.drain()
    # rule 仍 True(B 桶为真)
    assert runner._last_rule_state.get("rule_pex") is True
    # 只有 1 次 enter fire,无 exit
    dids = [
        call[0][0][0].did
        for call in mock_miot_proxy.set_device_properties.call_args_list
    ]
    assert dids == ["enter-rule_pex"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known limitation: duration_seconds + 多 source rule 在 cycle 内同步翻 "
        "True→False 时,update_state 入口处 effective_state 用'本帧 current_bool ⊔ "
        "其他桶上一帧' OR 聚合;先来的 source 看到其他桶上一帧 True → append 1 + "
        "round_id 消化,后来的 source 跳过 → deque 虚高 1 格,可能触发误 fire。"
        "修复需重构 duration fire 时序(从 update_state 入口同步 fire 改为 cycle 末延迟 "
        "fire),改动 N 个 duration 测试,ROI 不划算。当前 1 摄像头部署不暴露;多摄像头 + "
        "duration rule + 同步翻转极端场景才暴露,误差 ≤ 1 个 sample。等业务真翻车再做。"
    ),
)
@pytest.mark.asyncio
async def test_e2e_duration_multi_source_sync_transition_no_overcount(
    proxy_with_runner, mock_miot_proxy, mock_log_repo
):
    """xfail 锁定 bug:duration_seconds 配置 rule,多 source 同步翻 True→False 时
    deque 虚高 1 格 → 可能触发误 fire。

    场景:rule 绑 [cam_A, cam_B] duration_seconds=1, ratio=1.0, sample_interval=0.5
    (maxlen=2 → 满 ratio 即 fire)。
    - cycle 1 (round=200): A=True, B=True → 累计 [1]
    - cycle 2 (round=201): A=False, B=False → 实际应累计 [1, 0]，不 fire;
      但当前实现下 false 广播 cam_A 先到时 effective_state 看 cam_B 上一帧=True →
      append 1 → win=[1, 1] → sum/maxlen=1.0 ≥ ratio → **误 fire**。

    本 case xfail (strict=True),修复后该 case 应自然通过 → strict 让 unexpected
    pass 也报错,提醒移除 xfail mark。
    """
    from miloco.rule.runner import RuleRunner
    from miloco.rule.schema import (
        Rule,
        RuleAction,
        RuleCondition,
        RuleLifecycle,
        RuleMode,
    )

    proxy, _runner, _mgr_ctx = proxy_with_runner
    runner_dur = RuleRunner(
        rules=[],
        miot_proxy=mock_miot_proxy,
        rule_log_repo=mock_log_repo,
        sample_interval_seconds=0.5,
    )
    rule = Rule(
        id="rule_dur_or",
        name=f"[{TASK_ID}] dur-or",
        task_id=TASK_ID,
        mode=RuleMode.EVENT,
        lifecycle=RuleLifecycle.PERMANENT,
        enabled=True,
        condition=RuleCondition(perceive_device_ids=["cam_A", "cam_B"], query="人"),
        actions=[
            RuleAction(did="fire-d", iid="prop.2.1", value=True, idempotent=True)
        ],
        duration_seconds=1,
        duration_ratio=1.0,
    )
    runner_dur.add_rule(rule)

    fake_mgr = MagicMock()
    fake_mgr.rule_service.update_state = runner_dur.update_state
    fake_mgr.rule_service.get_enabled_rule_ids = lambda: [
    fake_mgr.rule_service.get_all_rules = AsyncMock(return_value=[])
        r.id for r in runner_dur.get_enabled_rules()
    ]

    dm = {"cam_A": ["rule_dur_or"], "cam_B": ["rule_dur_or"]}

    with patch("miloco.manager.get_manager", return_value=fake_mgr), \
         patch("miloco.rule.runner.time.time") as mt:
        mt.return_value = 100.0
        await proxy.handle_realtime_perception_result(
            _result(
                matched=[
                    ("rule_dur_or", ["cam_A"], "A"),
                    ("rule_dur_or", ["cam_B"], "B"),
                ],
                device_rule_map=dm,
            ),
            clips_by_device=None,
        )
        mt.return_value = 100.5
        await proxy.handle_realtime_perception_result(
            _result(device_rule_map=dm),
            clips_by_device=None,
        )

    await runner_dur.drain()

    # 期望(修复后):不触发 fire。当前(bug 存在):"fire-d" 出现在 fire 列表 → xfail。
    dids = [
        call[0][0][0].did
        for call in mock_miot_proxy.set_device_properties.call_args_list
    ]
    assert "fire-d" not in dids, (
        f"两 source 同步翻 False 不应触发 duration fire,实际 fire 列表 {dids}"
    )


@pytest.mark.asyncio
async def test_e2e_disabled_rule_during_cycle(proxy_with_runner, mock_miot_proxy):
    """rule 下发到 omni 后 cycle 内被 disable:false 广播路径应跳过该 rule,
    不报错且不再 update_state 推进状态机。"""
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_dis", ["cam_A"]))

    with mgr_ctx():
        # cycle 1: 命中 → ENTERED
        await proxy.handle_realtime_perception_result(
            _result(
                matched=[("rule_dis", ["cam_A"], "命中")],
                device_rule_map={"cam_A": ["rule_dis"]},
            ),
            clips_by_device=None,
        )
        await runner.drain()
        assert runner._last_rule_state.get("rule_dis") is True

        # cycle 中 disable
        runner._rules["rule_dis"].enabled = False

        # cycle 2: 未命中,device_rule_map 还包含 rule_dis(omni 已收到下发)
        # → false 广播路径检查 enabled_set 跳过该 rule
        await proxy.handle_realtime_perception_result(
            _result(device_rule_map={"cam_A": ["rule_dis"]}),
            clips_by_device=None,
        )

    await runner.drain()
    # 没有触发任何 exit fire(disable 前已 ENTERED,enter fire 是 1 次)
    dids = [
        call[0][0][0].did
        for call in mock_miot_proxy.set_device_properties.call_args_list
    ]
    assert dids == ["enter-rule_dis"]
