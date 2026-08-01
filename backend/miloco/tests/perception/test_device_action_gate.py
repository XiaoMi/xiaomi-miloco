"""本地感知通路下,规则的**直连设备动作一律不执行**。

切换后端时已经拦过一次带直连动作的规则,但那只是**转移**上的检查。这里守的是一条
**状态**不变量:切过去之后新建规则、把已有规则改成带动作、任务激活时批量启用、
直接改 config.json —— 每一条都能把系统带回被禁止的状态,而规则触发这条路上原本
没有任何一处知道感知后端是谁。

差别不是理论上的:纯视觉模型没有音频佐证、也没有身份识别,不该由它自己去关燃气阀。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import (
    Rule,
    RuleAction,
    RuleActionExecuteResult,
    RuleCondition,
    RuleEvent,
    RuleMode,
)


def _gas_valve_rule() -> Rule:
    """代码注释里一直拿来举例的那条规则:命中就去关燃气阀。"""
    return Rule(
        id="r-gas", task_id="t-1", name="厨房明火关阀", mode=RuleMode.EVENT, enabled=True,
        condition=RuleCondition(query="厨房有明火且无人", perceive_device_ids=[]),
        actions=[RuleAction(did="gasvalve", iid="prop.2.1", value=False)],
    )


def _dynamic_rule() -> Rule:
    return Rule(
        id="r-dyn", task_id="t-1", name="有人进门", mode=RuleMode.EVENT, enabled=True,
        condition=RuleCondition(query="有人进门", perceive_device_ids=[]),
        action_descriptions=["提醒我有人回来了"],
    )


@pytest.fixture
def runner():
    proxy = AsyncMock()
    proxy.set_device_properties = AsyncMock(return_value=[{"code": 0}])
    proxy.call_device_action = AsyncMock(return_value={"code": 0})
    log_repo = MagicMock()
    log_repo.create = MagicMock(return_value="log-1")
    return RuleRunner(
        rules=[_gas_valve_rule(), _dynamic_rule()],
        miot_proxy=proxy, rule_log_repo=log_repo,
    )


def _backend(monkeypatch, kind: str) -> None:
    monkeypatch.setattr(
        "miloco.rule.runner.perception_executes_device_actions",
        lambda: kind != "local",
    )


@pytest.mark.asyncio
async def test_cloud_backend_still_executes_device_actions(runner, monkeypatch):
    """既有行为不能被这道门改掉 —— 云端通路照常直连执行。"""
    _backend(monkeypatch, "cloud")
    executed: list = []

    async def _exec(rule_id, action):
        executed.append(action)
        return RuleActionExecuteResult(action=action, result=True)

    monkeypatch.setattr(runner, "_execute_action", _exec)
    res = await runner._fire(
        _gas_valve_rule(), RuleEvent.ENTERED, ["cam1"], "ctx", "exec-1",
    )
    assert res is not None
    assert len(executed) == 1


@pytest.mark.asyncio
async def test_local_backend_refuses_to_drive_the_device(runner, monkeypatch):
    """本地通路下,同一条规则必须一个动作都不发。

    这条如果失效,后果是一个纯视觉的 4B 模型自己决定关掉燃气阀 —— 而产品在四个
    地方(能力声明、卡片文案、切换日志、文档)都写着这条通路不执行设备动作。
    """
    _backend(monkeypatch, "local")
    execute = AsyncMock()
    monkeypatch.setattr(runner, "_execute_action", execute)

    res = await runner._fire(
        _gas_valve_rule(), RuleEvent.ENTERED, ["cam1"], "ctx", "exec-1",
    )
    execute.assert_not_called()
    assert res is None


@pytest.mark.asyncio
async def test_local_backend_still_dispatches_dynamic_rules(runner, monkeypatch):
    """只拦直连动作。动态规则(交给 agent 决策)正是本通路的**唯一**执行方式,
    连它一起拦掉的话,本地通路就完全不能用了。"""
    _backend(monkeypatch, "local")
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_execute_dynamic", sent)

    res = await runner._fire(
        _dynamic_rule(), RuleEvent.ENTERED, ["cam1"], "ctx", "exec-2",
    )
    sent.assert_awaited()
    assert res is not None


@pytest.mark.asyncio
async def test_refusal_is_announced_not_silent(runner, monkeypatch, caplog):
    """必须留下痕迹。静默拒绝就是"用户的自动化不响了,而没有任何地方说得清为什么"。"""
    _backend(monkeypatch, "local")
    monkeypatch.setattr(runner, "_execute_action", AsyncMock())
    events: list = []
    monkeypatch.setattr(
        runner, "_publish_rule_event",
        lambda kind, rid, payload: events.append((kind, rid, payload)),
    )
    with caplog.at_level("ERROR"):
        await runner._fire(_gas_valve_rule(), RuleEvent.ENTERED, ["cam1"], "c", "e")

    assert any("厨房明火关阀" in r.getMessage() for r in caplog.records), "拒绝没有写进日志"
    assert [e[0] for e in events] == ["rule_action_refused"]
    assert events[0][2]["reason"] == "perception_backend_no_device_actions"


# ── 能力声明与实际执行必须是同一个事实 ────────────────────────────────────


def test_capability_declaration_matches_what_the_rule_path_enforces(monkeypatch):
    """界面上那句"本通路不执行任何设备动作"来自引擎的类属性;规则执行侧读的是
    另一个函数。两者一旦漂移,就是界面说一套、执行做另一套。"""
    from miloco.config.settings import PerceptionSettings, get_settings
    from miloco.perception.capabilities import perception_executes_device_actions
    from miloco.perception.local_vision.engine import LocalVisionEngine

    cfg = get_settings().model_copy(deep=True)

    cfg.perception.engine_backend = "local"
    monkeypatch.setattr("miloco.perception.capabilities.get_settings", lambda: cfg)
    assert perception_executes_device_actions() is LocalVisionEngine.STATIC_RULE_EXECUTION

    cfg.perception.engine_backend = "cloud"
    assert perception_executes_device_actions() is True
    # 缺省就是云端 —— 存量部署不受这道门影响。
    assert PerceptionSettings().engine_backend == "cloud"


def test_unreadable_config_falls_back_to_existing_cloud_behaviour(monkeypatch):
    """读不到配置时不能把所有人的自动化都停掉。"""
    from miloco.perception.capabilities import perception_executes_device_actions

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("miloco.perception.capabilities.get_settings", _boom)
    assert perception_executes_device_actions() is True
