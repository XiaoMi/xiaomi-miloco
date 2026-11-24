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
import json
import time
from threading import Event
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from miloco.config import get_settings
from miloco.observability.context import get_device_context
from miloco.perception.client import PerceptionEngineProxy
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.collect.stream_buffer import MultiTrackSyncBuffer
from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.config import (
    GateConfig,
    IdentityEngineConfig,
    PerceptionConfig,
)
from miloco.perception.engine.omni import omni, prompt_builder
from miloco.perception.inference_admission import InferenceDrainDeferred
from miloco.perception.processor import PipelineProcessor
from miloco.perception.runner import PerceptionRunner
from miloco.perception.service import PerceptionService
from miloco.perception.snapshot_context import OmniEventArtifacts, get_event_artifacts
from miloco.perception.types import (
    MatchedRule,
    PerceptionDevice,
    RealtimePerceptionResult,
)
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleCondition,
    RuleDirection,
    RuleLifecycle,
    RuleMode,
)
from miloco.rule.service import RuleService
from miloco.task.state_machine import TaskStateMachine, derive_directions

TASK_ID = "e2e-task"


def _make_state_rule(rule_id: str, device_ids: list[str]) -> Rule:
    """STATE mode rule:on_enter / on_exit 各执行一个 prop 写操作,便于通过
    miot_proxy.set_device_properties 计数 fire 次数。"""
    return Rule(
        id=rule_id,
        name=f"[{TASK_ID}-{rule_id}] {rule_id}",
        task_id=f"{TASK_ID}-{rule_id}",
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
    p._inference_worker = None

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
    runner.set_task_actions(f"{TASK_ID}-rule_X", {'on_enter_actions': [{'did': 'enter-rule_X', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_X', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

    with mgr_ctx():
        # cycle 1: 命中 → ENTERED
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_X", ["cam_A"], "人来")],
                    device_rule_map={"cam_A": ["rule_X"]}),
            artifacts=None,
        )
        # cycle 2: 未命中,pending_exit 1st
        await proxy.handle_realtime_perception_result(
            _result(device_rule_map={"cam_A": ["rule_X"]}),
            artifacts=None,
        )
        # cycle 3: 未命中,确认 EXIT → schedule debounce(seconds=0 立即 fire)
        await proxy.handle_realtime_perception_result(
            _result(device_rule_map={"cam_A": ["rule_X"]}),
            artifacts=None,
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
async def test_e2e_rule_statuses_snapshot_to_persist(proxy_with_runner, monkeypatch):
    """胶水层集成:命中 → update_state 返回结论 → client 循环里就地累积并聚合 → rule_statuses
    透传给 persist。patch _persist_meaningful_event 只截获 rule_statuses,不拉起 DB/落盘。"""
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_X", ["cam_A"]))
    runner.set_task_actions(f"{TASK_ID}-rule_X", {'on_enter_actions': [{'did': 'enter-rule_X', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_X', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

    captured: dict = {}

    async def _fake_persist(
        *, result, device_ids, artifacts, rule_statuses=None,
        incomplete_rule_ids=None, event_id=None,
    ):
        captured["rule_statuses"] = rule_statuses
        captured["event_id"] = event_id

    monkeypatch.setattr(
        "miloco.perception.client._persist_meaningful_event", _fake_persist
    )

    with mgr_ctx():
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_X", ["cam_A"], "人来")],
                    device_rule_map={"cam_A": ["rule_X"]}),
            artifacts=OmniEventArtifacts(),  # 非 None → 进 persist 分支（patch 后不真落库）
        )
        await asyncio.sleep(0.05)  # 让后台 persist task 跑起来

    await runner.drain()
    # ENTER fire → 该 rule 本周期结论 FIRED（client 快照现传中性枚举，中文标签由展示层映射）
    from miloco.rule.schema import TriggerOutcome

    assert captured.get("rule_statuses") == {"rule_X": TriggerOutcome.FIRED}


@pytest.mark.asyncio
async def test_e2e_rule_statuses_multi_cam_aggregates_to_fired(
    proxy_with_runner, monkeypatch
):
    """多摄像头聚合：同 rule 绑 [cam_A, cam_B]，同周期 cam_A ENTER→FIRED、cam_B 已在态→STILL_IN，
    就地累积后 aggregate 取最强 → 快照该 rule = FIRED。守住 setdefault().append() 累积半边
    （防退化成「取最后一个结论」→ 明明触发却写「未触发（持续中）」）。"""
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_or", ["cam_A", "cam_B"]))
    runner.set_task_actions(f"{TASK_ID}-rule_or", {'on_enter_actions': [{'did': 'enter-rule_or', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_or', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

    captured: dict = {}

    async def _fake_persist(
        *, result, device_ids, artifacts, rule_statuses=None,
        incomplete_rule_ids=None, event_id=None,
    ):
        captured["rule_statuses"] = rule_statuses
        captured["event_id"] = event_id

    monkeypatch.setattr(
        "miloco.perception.client._persist_meaningful_event", _fake_persist
    )

    with mgr_ctx():
        await proxy.handle_realtime_perception_result(
            _result(
                matched=[("rule_or", ["cam_A"], "A 命中"),
                         ("rule_or", ["cam_B"], "B 命中")],
                device_rule_map={"cam_A": ["rule_or"], "cam_B": ["rule_or"]},
            ),
            artifacts=OmniEventArtifacts(),
        )
        await asyncio.sleep(0.05)
    await runner.drain()

    from miloco.rule.schema import TriggerOutcome

    # cam_A=FIRED + cam_B=STILL_IN → 聚合取最强 → FIRED（非「取最后一个」的 STILL_IN）
    assert captured.get("rule_statuses") == {"rule_or": TriggerOutcome.FIRED}


@pytest.mark.asyncio
async def test_main_loop_update_state_raise_marks_rule_incomplete(
    proxy_with_runner, monkeypatch
):
    """终态主循环 update_state 抛异常 → 该 rule 记残缺、展示层标「未知」，不能整行消失。

    与早送侧同口径:同一份证据缺口两条路结果必须一致。整行消失的既有语义是「本周期完全没
    处理到」,而这里规则处理过、还可能真派发过一次执行(裸 DB 读排在 _spawn_fire 之后),
    静默降级成「没这回事」会让住户和排障的人都无法区分。
    """
    proxy, runner, _ = proxy_with_runner
    captured: dict = {}

    async def _fake_persist(
        *, result, device_ids, artifacts, rule_statuses=None,
        incomplete_rule_ids=None, event_id=None,
    ):
        captured["incomplete"] = incomplete_rule_ids

    async def _boom(*a, **k):
        raise RuntimeError("read_duration_target_state boom (post-dispatch)")

    monkeypatch.setattr(
        "miloco.perception.client._persist_meaningful_event", _fake_persist
    )
    fake = MagicMock()
    fake.rule_service.update_state = _boom
    fake.rule_service.get_enabled_rule_ids = lambda: ["rule_X"]
    with patch("miloco.manager.get_manager", return_value=fake):
        with pytest.raises(RuntimeError):
            await proxy.handle_realtime_perception_result(
                _result(matched=[("rule_X", ["cam_A"], "有人进门")],
                        device_rule_map={"cam_A": ["rule_X"]}),
                artifacts=OmniEventArtifacts(),
            )
        await asyncio.sleep(0.05)

    assert captured.get("incomplete") == {"rule_X"}, (
        "主循环 update_state 抛异常后该 rule 未记残缺 → 展示层整行消失,"
        "把可能已 fire 的规则降级成「本周期没处理到」"
    )


@pytest.mark.asyncio
async def test_e2e_early_send_failure_marks_rule_incomplete(
    proxy_with_runner, monkeypatch
):
    """早送某路判定未完成(value 留 None) → 该 rule 进 incomplete_rule_ids。

    钉住 client 侧「None ⇒ 证据残缺」的收集:不能只拿兄弟相机的结论聚合了事——同 rule 本周期
    最多一路返回 FIRED,若偏偏那一路抛异常,聚合结果就是确定但偏弱的假阴性。
    (展示层拿到 incomplete 后标「未知」,但聚合已是 FIRED 时不降级——见
    test_incomplete_does_not_downgrade_confirmed_fired;本用例只钉 client 侧的收集契约。)
    """
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_or", ["cam_A", "cam_B"]))
    runner.set_task_actions(f"{TASK_ID}-rule_or", {'on_enter_actions': [{'did': 'enter-rule_or', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_or', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

    captured: dict = {}

    async def _fake_persist(
        *, result, device_ids, artifacts, rule_statuses=None,
        incomplete_rule_ids=None, event_id=None,
    ):
        captured["rule_statuses"] = rule_statuses
        captured["event_id"] = event_id
        captured["incomplete"] = incomplete_rule_ids

    monkeypatch.setattr(
        "miloco.perception.client._persist_meaningful_event", _fake_persist
    )

    with mgr_ctx():
        await proxy.handle_realtime_perception_result(
            _result(
                matched=[("rule_or", ["cam_B"], "B 命中")],
                device_rule_map={"cam_A": ["rule_or"], "cam_B": ["rule_or"]},
            ),
            # 早送 cam_A 判定未完成:pair 已登记(去重/抑制推退照旧),但 value 留 None
            early_sent_rule_ids={("rule_or", "cam_A"): None},
            artifacts=OmniEventArtifacts(),
        )
        await asyncio.sleep(0.05)
    await runner.drain()

    from miloco.rule.schema import TriggerOutcome

    assert captured.get("incomplete") == {"rule_or"}, "value=None 未被收成证据残缺"
    # 聚合值确实非空(cam_B 走主循环正常返回) → 证明「未知」是在覆盖一个确定值,
    # 而非「本来就没值可覆盖」——否则本用例分不清 precedence 是否真生效。
    assert captured.get("rule_statuses") == {"rule_or": TriggerOutcome.FIRED}


@pytest.mark.asyncio
async def test_persist_spawned_even_if_update_state_raises(proxy_with_runner, monkeypatch):
    """韧性：update_state 循环抛异常时，finally 里仍 spawn persist（本 cycle 日志不整行丢失），
    且异常照常上抛（与「persist 领先循环」时的传播语义一致）。"""
    proxy, runner, mgr_ctx = proxy_with_runner

    persisted = {"called": False}

    async def _fake_persist(
        *, result, device_ids, artifacts, rule_statuses=None,
        incomplete_rule_ids=None, event_id=None,
    ):
        persisted["called"] = True

    async def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "miloco.perception.client._persist_meaningful_event", _fake_persist
    )

    fake = MagicMock()
    fake.rule_service.update_state = _boom
    fake.rule_service.get_enabled_rule_ids = lambda: []

    with patch("miloco.manager.get_manager", return_value=fake):
        with pytest.raises(RuntimeError):
            await proxy.handle_realtime_perception_result(
                _result(matched=[("rule_X", ["cam_A"], "x")],
                        device_rule_map={"cam_A": ["rule_X"]}),
                artifacts=OmniEventArtifacts(),  # 非 None → 进 persist 分支
            )
        await asyncio.sleep(0.05)  # 让 finally 里 spawn 的 persist task 跑起来

    assert persisted["called"] is True


@pytest.mark.asyncio
async def test_stale_outcome_not_leaked_on_exception(proxy_with_runner, monkeypatch):
    """异常路径下，本 cycle 未处理到的规则不会把上一 cycle 的旧结论泄进快照。
    （就地累积拿返回值的关键：引擎已不留跨 cycle 记账表，主循环命中侧靠 update_state 返回值，
    本 cycle 处理到某规则之前抛异常时它自然缺席，而非回落到旧结论。）"""
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_2", ["cam_A"]))
    runner.set_task_actions(f"{TASK_ID}-rule_2", {'on_enter_actions': [{'did': 'enter-rule_2', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_2', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

    # 上一 cycle：rule_2 ENTER 真 fire（rule_2 的状态机进入 ENTERED，模拟"上轮真触发过"）
    with mgr_ctx():
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_2", ["cam_A"], "x")],
                    device_rule_map={"cam_A": ["rule_2"]}),
            artifacts=None,
        )
    await runner.drain()

    # 本 cycle：处理到 rule_2 之前抛异常（_publish_perception_event 在 update_state 之前）
    captured: dict = {}

    async def _fake_persist(
        *, result, device_ids, artifacts, rule_statuses=None,
        incomplete_rule_ids=None, event_id=None,
    ):
        captured["rule_statuses"] = rule_statuses
        captured["event_id"] = event_id

    def _boom_publish(*a, **k):
        raise RuntimeError("boom before update_state")

    monkeypatch.setattr(
        "miloco.perception.client._persist_meaningful_event", _fake_persist
    )
    monkeypatch.setattr(
        "miloco.perception.client._publish_perception_event", _boom_publish
    )

    with mgr_ctx():
        with pytest.raises(RuntimeError):
            await proxy.handle_realtime_perception_result(
                _result(matched=[("rule_2", ["cam_A"], "x")],
                        device_rule_map={"cam_A": ["rule_2"]}),
                artifacts=OmniEventArtifacts(),
            )
        await asyncio.sleep(0.05)

    # ① persist 仍被发起（韧性）——正面断言，避免「根本没发起」时空字典让断言恒真
    assert "rule_statuses" in captured, "finally 未发起 persist（韧性回退）"
    # ② 快照为空——rule_2 本 cycle 在处理它之前就抛了，它就该缺席（不把旧 FIRED 当本 cycle）
    assert captured["rule_statuses"] == {}


@pytest.mark.asyncio
async def test_e2e_cam_a_offline_rule_state_preserved(
    proxy_with_runner, mock_miot_proxy
):
    """核心修复验证:rule 绑 [cam_A],cycle 中 cam_A 离线只来 cam_B 时,rule
    状态应保持(device_rule_map[B] 不含 rule_X → 没有 update_state 推退)。
    旧行为会无差别广播 False 把 rule 错误推退。"""
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_X", ["cam_A"]))
    runner.set_task_actions(f"{TASK_ID}-rule_X", {'on_enter_actions': [{'did': 'enter-rule_X', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_X', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

    with mgr_ctx():
        # cycle 1: cam_A 在线 + 命中 → ENTERED
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_X", ["cam_A"], "命中")],
                    device_rule_map={"cam_A": ["rule_X"]}),
            artifacts=None,
        )
        # cycle 2-4: cam_A 离线,只有 cam_B 上线(rule 没绑 B,device_rule_map[B]=[])
        for _ in range(3):
            await proxy.handle_realtime_perception_result(
                _result(device_rule_map={"cam_B": []}),
                artifacts=None,
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
    runner.set_task_actions(f"{TASK_ID}-rule_or", {'on_enter_actions': [{'did': 'enter-rule_or', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_or', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

    dm = {"cam_A": ["rule_or"], "cam_B": ["rule_or"]}

    with mgr_ctx():
        # cycle 1: cam_A 命中 → ENTERED via A
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_or", ["cam_A"], "A 命中")], device_rule_map=dm),
            artifacts=None,
        )
        # cycle 2: cam_B 命中,cam_A 未命中 → A 桶 1st pending,B 桶 ENTER 但 rule 已 True
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_or", ["cam_B"], "B 命中")], device_rule_map=dm),
            artifacts=None,
        )
        # cycle 3: cam_B 继续命中,cam_A 未命中 → A 桶第二帧 false 确认,B 桶继续 true → rule 仍 True
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_or", ["cam_B"], "B 持续")], device_rule_map=dm),
            artifacts=None,
        )
        # cycle 4-5: 都不命中 → 两桶都翻 false → EXITED
        for _ in range(2):
            await proxy.handle_realtime_perception_result(
                _result(device_rule_map=dm),
                artifacts=None,
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
    runner.set_task_actions(f"{TASK_ID}-rule_pex", {'on_enter_actions': [{'did': 'enter-rule_pex', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_pex', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

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
            artifacts=None,
        )
        assert runner._last_source_state[("rule_pex", "cam_A")] is True
        assert runner._last_source_state[("rule_pex", "cam_B")] is True

        # cycle 2: 只有 B 命中 → A 桶喂 false(false 广播路径),1st pending
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_pex", ["cam_B"], "B")], device_rule_map=dm),
            artifacts=None,
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


@pytest.mark.asyncio
async def test_e2e_duration_multi_source_sync_transition_no_overcount(
    proxy_with_runner, mock_miot_proxy, mock_log_repo
):
    """duration_seconds 配置 rule,多 source 同步翻 True→False 时不应过计数。

    场景:rule 绑 [cam_A, cam_B] duration_seconds=1, ratio=1.0, sample_interval=0.5
    (maxlen=2 → 满 ratio 即 fire)。
    - cycle 1 (round=200): A=True, B=True → 累计 [1]
    - cycle 2 (round=201): A=False, B=False → 实际应累计 [1, 0]，不 fire;
      false 广播 cam_A 先到时也不能把 cam_B 的上一帧 True 当成本轮 True。
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
            artifacts=None,
        )
        mt.return_value = 100.5
        await proxy.handle_realtime_perception_result(
            _result(device_rule_map=dm),
            artifacts=None,
        )

    await runner_dur.drain()

    # 两 source 同步退出时，本轮状态应按 cycle 快照聚合为 False，不能沿用任一
    # source 的上一帧 True 过计入 duration 窗口。
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
    runner.set_task_actions(f"{TASK_ID}-rule_dis", {'on_enter_actions': [{'did': 'enter-rule_dis', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_dis', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

    with mgr_ctx():
        # cycle 1: 命中 → ENTERED
        await proxy.handle_realtime_perception_result(
            _result(
                matched=[("rule_dis", ["cam_A"], "命中")],
                device_rule_map={"cam_A": ["rule_dis"]},
            ),
            artifacts=None,
        )
        await runner.drain()
        assert runner._last_rule_state.get("rule_dis") is True

        # cycle 中 disable
        runner._rules["rule_dis"].enabled = False

        # cycle 2: 未命中,device_rule_map 还包含 rule_dis(omni 已收到下发)
        # → false 广播路径检查 enabled_set 跳过该 rule
        await proxy.handle_realtime_perception_result(
            _result(device_rule_map={"cam_A": ["rule_dis"]}),
            artifacts=None,
        )

    await runner.drain()
    # 没有触发任何 exit fire(disable 前已 ENTERED,enter fire 是 1 次)
    dids = [
        call[0][0][0].did
        for call in mock_miot_proxy.set_device_properties.call_args_list
    ]
    assert dids == ["enter-rule_dis"]


@pytest.mark.asyncio
async def test_e2e_ledger_link_matches_persisted_event_id(
    proxy_with_runner, monkeypatch
):
    """一个 cycle 里,动作台账挂的那个 id 必须就是事件行拿到的那一个。

    这是整条链的收口断言:进入支与退出支的台账行都要指回**同一条**事件行,
    而那条事件行的 id 是感知周期在引擎调用前 mint 的。任一处另 mint 一个 uuid,
    折叠页就会把动作挂到一条不存在的父上——而「找不到父」与「真的没有父」
    在展示上是两种不同的东西,查起来只能靠猜。
    """
    proxy, runner, mgr_ctx = proxy_with_runner
    runner.add_rule(_make_state_rule("rule_L", ["cam_A"]))
    runner.set_task_actions(f"{TASK_ID}-rule_L", {'on_enter_actions': [{'did': 'enter-rule_L', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_enter_desc': None, 'on_exit_actions': [{'did': 'exit-rule_L', 'iid': 'prop.2.1', 'value': True, 'params': None, 'idempotent': True, 'cooldown_minutes': None}], 'on_exit_desc': None})

    persisted: dict = {}
    ledger: list[dict] = []

    async def _fake_persist(
        *, result, device_ids, artifacts, rule_statuses=None,
        incomplete_rule_ids=None, event_id=None,
    ):
        persisted["event_id"] = event_id

    async def _fake_ledger(_proxy, **kwargs):
        ledger.append(kwargs)

    monkeypatch.setattr(
        "miloco.perception.client._persist_meaningful_event", _fake_persist
    )
    # _execute_action 体内 import,故打模块属性即可被取到。
    monkeypatch.setattr("miloco.miot.service._write_action_ledger", _fake_ledger)

    with mgr_ctx():
        # cycle 1:命中 → ENTERED(带本 cycle 的事件 id)
        await proxy.handle_realtime_perception_result(
            _result(matched=[("rule_L", ["cam_A"], "人来")],
                    device_rule_map={"cam_A": ["rule_L"]}),
            artifacts=OmniEventArtifacts(),  # 非 None → 真进 persist 分支
            cycle_event_id="ev-cycle-1",
        )
        await asyncio.sleep(0.05)  # 让后台 persist task 跑起来
        # cycle 2/3:未命中 → 第一帧观察窗 → 第二帧确认 EXIT(debounce=0 立即 fire)。
        # 这两个周期不写事件行,故 id 传 None——正是要验的场景:退出支没有本周期
        # 事件可挂,只能挂进入时那条。
        for _ in range(2):
            await proxy.handle_realtime_perception_result(
                _result(device_rule_map={"cam_A": ["rule_L"]}),
                artifacts=None,
            )

    await asyncio.sleep(0.05)
    await runner.drain()

    assert persisted.get("event_id") == "ev-cycle-1"
    links = [(r["phase"], r["trigger_event_id"]) for r in ledger]
    assert links == [("enter", "ev-cycle-1"), ("exit", "ev-cycle-1")], (
        f"台账链路两列未指向同一条事件行,实际 {links}"
    )


WINDOW_MS = 80
FIRST_CAMERA = "cam_A"
SECOND_CAMERA = "cam_B"
CAMERAS = (FIRST_CAMERA, SECOND_CAMERA)


def elapsed_ms():
    return time.monotonic_ns() // 1_000_000


async def wait_until(stamp_ms):
    await asyncio.sleep(max(0, stamp_ms - elapsed_ms()) / 1000)


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize(("mode", "case"), [
    ("batch", "independent"),
    ("per_camera", "independent"),
    ("per_camera", "or_handoff"),
    ("per_camera", "or_broadcast"),
    ("per_camera", "or_exit"),
    ("per_camera", "or_new_job"),
    ("per_camera", "or_wait_limit"),
    ("per_camera", "or_timeout"),
    ("per_camera", "or_reconnect"),
    ("per_camera", "enter_first"),
    ("per_camera", "guard_first"),
    ("per_camera", "stale_guard"),
    ("per_camera", "unrelated_camera"),
])
async def test_camera_inference_scheduling(
    mode, case, proxy_with_runner, mock_miot_proxy, mock_log_repo, monkeypatch,
):
    _, rule_runner, manager_context = proxy_with_runner
    slow_camera = case != "guard_first"
    is_or = case.startswith("or_")
    or_handoff = case in ("or_handoff", "or_broadcast")
    is_guard = case in ("enter_first", "guard_first", "stale_guard")
    scheduled_mode = mode == "per_camera"
    selectors = list(CAMERAS) if is_or else [FIRST_CAMERA]
    if case == "or_broadcast":
        selectors = []
    rule = _make_state_rule("simulated-entry", selectors)
    rules = [rule]
    independent_rule = None
    if or_handoff:
        independent_rule = _make_state_rule("simulated-independent", [FIRST_CAMERA])
        rules.append(independent_rule)
        rule_runner.set_task_actions(independent_rule.task_id, {
            "on_enter_actions": [action.model_dump() for action in independent_rule.on_enter_actions],
        })
    guard = None
    if is_guard:
        guard = Rule(
            id="simulated-guard", name="[guard] 前提", task_id=rule.task_id,
            direction=RuleDirection.GUARD,
            condition=RuleCondition(perceive_device_ids=[SECOND_CAMERA], query="有人"),
        )
        rules.append(guard)
        state_machine = TaskStateMachine(
            is_condition_satisfied=rule_runner.is_condition_satisfied,
            dispatch_action=MagicMock(),
        )
        state_machine.register_task(rule.task_id, derive_directions(
            (item.id, item.resolved_direction.value) for item in rules
        ))
        rule_runner.attach_state_machine(state_machine)
    for item in rules:
        rule_runner.add_rule(item)
    rule_runner.set_task_actions(
        rule.task_id, {
            "on_enter_actions": [action.model_dump() for action in rule.on_enter_actions],
            "on_exit_actions": [action.model_dump() for action in rule.on_exit_actions],
        }
    )

    monkeypatch.setattr(get_settings().perception.schedule, "mode", "batch")
    monkeypatch.setattr(PerceptionEngineProxy, "_init_engine", lambda _: None)
    monkeypatch.setattr("miloco.perception.engine.omni.dedup_embedder.EventEmbedder", MagicMock)
    monkeypatch.setattr("miloco.perception.engine.api._camera_prompt_map", dict)
    monkeypatch.setattr("miloco.perception.engine.api._crop_denied_dids", set)
    monkeypatch.setattr("miloco.perception.engine.api._voice_allowed_dids", set)
    monkeypatch.setattr("miloco.miot.filter.voice_allowed_camera_dids", lambda _: set())
    monkeypatch.setattr("miloco.perception.client._persist_meaningful_event", AsyncMock())
    monkeypatch.setattr("miloco.miot.service._write_action_ledger", AsyncMock())
    rule_service = RuleService.__new__(RuleService)
    rule_service._runner = rule_runner
    svc = MagicMock(
        get_effectively_enabled_rules=AsyncMock(return_value=rules),
        get_enabled_rule_ids=lambda: [item.id for item in rules],
        update_state=AsyncMock(wraps=rule_service.update_state),
    )
    proxy = PerceptionEngineProxy()
    proxy.perception_engine = PerceptionEngine(PerceptionConfig(
        gate=GateConfig(cooldown_ms=0, speech_vad_enabled=False),
        identity_engine=IdentityEngineConfig(enabled=False),
    ))
    proxy._status = "ready"

    camera_api = MagicMock()
    camera_api.get_cached_camera.return_value = None
    camera_api.start_camera_decode_video_stream = AsyncMock(return_value=1)
    camera_api.start_camera_decode_audio_stream = AsyncMock(return_value=-1)
    camera_api.stop_camera_decode_video_stream = AsyncMock()
    ready = asyncio.Event()
    adapter = CameraDeviceAdapter(camera_api, on_window_ready=ready.set)
    collector = MultimodalCollector([adapter])
    processor = PipelineProcessor(collector, proxy, mock_log_repo)
    monkeypatch.setattr(processor, "drive_omni_probe", MagicMock())
    runner = PerceptionRunner(collector, processor, mock_log_repo, ready)
    monkeypatch.setattr(runner, "_sync_devices", AsyncMock())
    service = PerceptionService(collector, processor, runner, mock_log_repo, MagicMock())

    wall_offset = time.time_ns() // 1_000_000 - elapsed_ms()
    monkeypatch.setattr(adapter, "_calibrate", lambda _state, stamp: (stamp, stamp + wall_offset))
    callbacks = {}
    for did in CAMERAS:
        await adapter.connect_device(
            did, source=PerceptionDevice(did=did, name=did, room_name=did, device_type="camera"),
        )
        buffer = MultiTrackSyncBuffer(
            ["decoded_video"], window_ms=WINDOW_MS, max_windows=12, on_window_ready=ready.set,
        )
        adapter._devices[did].sync_buffer = buffer
        _, _, callbacks[did] = camera_api.start_camera_decode_video_stream.call_args.args

    async def put_frame(did, marker, stamp):
        pixels = np.full((48, 48, 3), marker * 25, dtype=np.uint8)
        await callbacks[did](did, pixels, stamp, 0)

    def encode_for_simulation(packets, **_kwargs):
        marker = int(packets[0].all_frames[0][0, 0, 0]) // 25
        return "v" * 1100, {"marker": marker}

    loop = asyncio.get_running_loop()
    release = Event()
    release_next = Event()
    started = []
    finished = []
    actions = []

    async def model_response(payload, _config):
        did = get_device_context().device_id
        marker = payload["video_media_info"]["marker"]
        scheduled = get_event_artifacts().scheduled_camera
        loop.call_soon_threadsafe(started.append, (did, marker, scheduled))
        if slow_camera and did == SECOND_CAMERA and marker == 1:
            assert await asyncio.to_thread(release.wait, 5)
        elif case == "or_new_job" and did == SECOND_CAMERA and marker == 2:
            assert await asyncio.to_thread(release_next.wait, 5)
        else:
            await asyncio.sleep(0.008)
        hit = None
        if is_or:
            if (did, marker) == (FIRST_CAMERA, 1) or (
                did == SECOND_CAMERA and marker == 1
                and case not in ("or_exit", "or_new_job")
            ):
                hit = rule
            elif (did, marker) == (FIRST_CAMERA, 4):
                hit = independent_rule
        elif did == FIRST_CAMERA and (marker == 1 if case == "unrelated_camera" else marker >= 2):
            hit = rule
        elif did == SECOND_CAMERA and guard is not None and marker >= 1:
            hit = guard
        hits = [{"rule_name": hit.name, "reason": f"window {marker}", "hit": True}] if hit else []
        loop.call_soon_threadsafe(finished.append, (did, marker))
        return {"choices": [{
            "message": {"content": json.dumps({"matched_rules": hits})},
            "finish_reason": "stop",
        }]}

    async def record_action(properties):
        actions.extend(prop.did for prop in properties)
        return [{"code": 0}]

    monkeypatch.setattr(prompt_builder, "_encode_batch_video", encode_for_simulation)
    monkeypatch.setattr(omni, "call_omni", model_response)
    monkeypatch.setattr(mock_miot_proxy, "set_device_properties", record_action)
    if case == "or_wait_limit":
        monkeypatch.setattr("miloco.perception.per_camera_scheduler._OR_EXIT_WAIT_S", 0.5)

    with manager_context() as manager:
        manager.return_value.rule_service = svc
        await service.start_engine()
        try:
            if mode == "per_camera":
                await service.apply_schedule_config(
                    mode, lambda: None, restart_window=False, omni_fps=None,
                )
            assert service.schedule_mode == ("per_camera" if scheduled_mode else "batch")
            if case == "or_timeout":
                runner._camera_scheduler._timeout_s = 0.65
            base = (elapsed_ms() // WINDOW_MS + 2) * WINDOW_MS
            for did in CAMERAS:
                await put_frame(did, 0, base - WINDOW_MS)
            await wait_until(base)
            for did in CAMERAS:
                await put_frame(did, 1, base)
            for marker in range(2, 6):
                stamp = base + (marker - 1) * WINDOW_MS
                await wait_until(stamp)
                await put_frame(FIRST_CAMERA, marker, stamp)
                if marker == 2 or (case == "or_new_job" and marker == 3):
                    await put_frame(SECOND_CAMERA, marker, stamp)

            await wait_until(base + 6 * WINDOW_MS)
            assert (FIRST_CAMERA, 1) in finished
            assert (SECOND_CAMERA, 1, scheduled_mode) in started
            if slow_camera:
                assert (SECOND_CAMERA, 1) not in finished
            before_release = [tag for did, tag in finished if did == FIRST_CAMERA]
            action_before_release = bool(actions)

            if scheduled_mode:
                assert before_release == [1, 2, 3, 4]
                first_match = next(
                    call for call in svc.update_state.await_args_list if call.args[:3] == (rule.id, FIRST_CAMERA, True)
                )
                first_positive_window = 1 if is_or or case == "unrelated_camera" else 2
                assert first_match.args[3] == f"window {first_positive_window}"
                assert action_before_release is (case not in ("enter_first", "stale_guard"))
                if is_or:
                    assert rule_runner.is_condition_satisfied(rule.id) is None
                    assert actions == (
                        ["enter-simulated-entry", "enter-simulated-independent"]
                        if or_handoff else ["enter-simulated-entry"]
                    )
                if case == "unrelated_camera":
                    assert actions == ["enter-simulated-entry", "exit-simulated-entry"]
                if case in ("enter_first", "stale_guard"):
                    assert rule_runner.is_condition_satisfied(rule.id) is True
                    assert rule_runner.is_condition_satisfied(guard.id) is not True
                if case == "guard_first":
                    hits = [call.args[0] for call in svc.update_state.await_args_list if call.args[2]]
                    assert hits[0] == guard.id
            else:
                assert before_release == [1]
                assert not action_before_release

            if mode == "per_camera" and slow_camera:
                with patch("miloco.perception.processor._INFERENCE_DRAIN_TIMEOUT_S", 0.01):
                    save = MagicMock()
                    with pytest.raises(InferenceDrainDeferred):
                        await service.apply_schedule_config("batch", save, restart_window=False, omni_fps=None)
                    save.assert_not_called()

            if case in ("stale_guard", "or_reconnect"):
                await adapter.disconnect_device(SECOND_CAMERA)
                await adapter.connect_device(
                    SECOND_CAMERA, source=PerceptionDevice(
                        did=SECOND_CAMERA, name=SECOND_CAMERA, device_type="camera",
                    ),
                )
            if case in ("or_wait_limit", "or_timeout", "or_reconnect"):
                async with asyncio.timeout(1):
                    await wait_for(lambda: "exit-simulated-entry" in actions)
                assert (SECOND_CAMERA, 1) not in finished

            release.set()
            await wait_for(lambda: (SECOND_CAMERA, 1) in finished)
            if case == "or_new_job":
                await wait_for(lambda: (SECOND_CAMERA, 2, True) in started)
                async with asyncio.timeout(0.5):
                    await wait_for(lambda: "exit-simulated-entry" in actions)
                assert (SECOND_CAMERA, 2) not in finished
                release_next.set()
            if case == "stale_guard":
                await wait_for(lambda: processor.inference_idle)
                await rule_runner.drain()
                assert rule_runner.is_condition_satisfied(guard.id) is not True
                assert not actions
                return
            await wait_for(lambda: (FIRST_CAMERA, 4) in finished and bool(actions))
            await wait_for(lambda: processor.inference_idle)
            if is_or:
                if or_handoff:
                    assert rule_runner.is_condition_satisfied(rule.id) is True
                    expected = ["enter-simulated-entry", "enter-simulated-independent"]
                elif case == "or_wait_limit":
                    assert rule_runner.is_condition_satisfied(rule.id) is True
                    expected = ["enter-simulated-entry", "exit-simulated-entry", "enter-simulated-entry"]
                else:
                    await wait_for(lambda: "exit-simulated-entry" in actions)
                    assert rule_runner.is_condition_satisfied(rule.id) is False
                    expected = ["enter-simulated-entry", "exit-simulated-entry"]
                if case in ("or_timeout", "or_reconnect"):
                    assert (rule.id, SECOND_CAMERA) not in rule_runner._last_source_state
                await rule_runner.drain()
                assert actions == expected
                assert service.schedule_mode == "per_camera"
                return
            await rule_runner.drain()
            if case == "unrelated_camera":
                assert actions == ["enter-simulated-entry", "exit-simulated-entry"]
                return
            assert actions == ["enter-simulated-entry"]

            if scheduled_mode:
                await service.apply_schedule_config("batch", lambda: None, restart_window=False, omni_fps=None)
                assert service.schedule_mode == "batch"
                rollback_at = (elapsed_ms() // WINDOW_MS + 2) * WINDOW_MS
                await wait_until(rollback_at)
                for did in CAMERAS:
                    await put_frame(did, 6, rollback_at)
                await wait_until(rollback_at + WINDOW_MS)
                for did in CAMERAS:
                    await put_frame(did, 7, rollback_at + WINDOW_MS)
                await wait_for(lambda: all((did, 6) in finished for did in CAMERAS))
                assert all((did, 6, False) in started for did in CAMERAS)
                await wait_for(lambda: processor.inference_idle)
                await rule_runner.drain()
                assert actions == ["enter-simulated-entry"]
        finally:
            release.set()
            release_next.set()
            await service.stop_engine()
