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
    RuleLogKind,
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


async def _drain(runner) -> None:
    """fire 是 spawn 出去的 task(update_state 跑在感知热路径上,不能被它拖住)。
    等它们跑完再断言 —— 否则测的只是"调度了没有",而不是"做了什么"。"""
    import asyncio

    pending = [t for t in getattr(runner, "_fire_tasks", set()) if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    else:
        for _ in range(3):
            await asyncio.sleep(0)


def _backend(monkeypatch, kind: str) -> None:
    # 打在**定义处**:runner 里那是函数内导入(顶层导入会把 cv2/av/numpy 拖进
    # 规则引擎),所以 runner 模块上没有这个名字可打。
    monkeypatch.setattr(
        "miloco.perception.capabilities.perception_executes_device_actions",
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
    # 不是 None:再往下就是执行记录落库。提前返回会让这条规则在界面上的执行历史
    # 一片空白 —— 用户只看到自动化不响了,没有任何地方说得出为什么。
    assert res is not None
    assert res.action_results == []
    assert res.dynamic_rule_event_sent is False
    assert "不执行设备动作" in res.error


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
    # 被拒绝的执行不该同时再发一条 rule_fire —— 那会把它计进「触发了多少次」。
    assert [e[0] for e in events] == ["rule_action_refused"], (
        f"实际发出的事件: {[e[0] for e in events]}"
    )
    assert events[0][2]["reason"] == "perception_backend_no_device_actions"

    # 落库的必须是一条**失败**记录,而不是没有记录。
    kinds = [c.args[0].kind for c in runner._log_repo.create.call_args_list]
    assert kinds == [RuleLogKind.RULE_TRIGGER_FAILURE], kinds


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

    # 断言"两个值恰好相等"是同义反复:把 capabilities 写死成 return False 也照样过。
    # 要钉住的是**联动** —— 事实只写在引擎的类属性上,改它就必须改变结论。
    monkeypatch.setattr(LocalVisionEngine, "STATIC_RULE_EXECUTION", True)
    assert perception_executes_device_actions() is True, "结论没有跟着引擎的能力声明走"
    monkeypatch.setattr(LocalVisionEngine, "STATIC_RULE_EXECUTION", False)
    assert perception_executes_device_actions() is False

    cfg.perception.engine_backend = "cloud"
    assert perception_executes_device_actions() is True
    # 缺省就是云端 —— 存量部署不受这道门影响。
    assert PerceptionSettings().engine_backend == "cloud"


def test_unreadable_config_falls_back_to_refusing(monkeypatch):
    """判定本身失败时**按拒绝处理**。

    这条原来钉的是相反方向(回落到"执行"),理由是「读不到配置时不能把所有人的
    自动化都停掉」。那个顾虑是真的,但方向仍然该翻,两点:

    1. **代价不对称。** 误拒只是一次规则没响,而且会记成一次 RULE_TRIGGER_FAILURE
       —— 看得见、查得到。误放是让一个纯视觉模型去关燃气阀、开门锁。安全不变量的
       兜底只能朝拒绝的方向倒。
    2. **那个顾虑的前提基本不成立。** ``get_settings()`` 是带缓存的全局;它抛异常
       意味着进程已经坏得很深,此时感知本身也跑不起来 —— 不存在"配置读不到但自动化
       在正常工作"这个状态。

    触发窗口需要引擎属性链与 settings 同时读不到,极窄。但"窄"不是把方向定反的
    理由:属性链是五层私有属性,任何一次重构改了中间名字都会让第一层静默失效。
    """
    from miloco.perception.capabilities import perception_executes_device_actions

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("miloco.perception.capabilities.get_settings", _boom)
    monkeypatch.setattr("miloco.perception.capabilities._active_engine", lambda: None)
    assert perception_executes_device_actions() is False


@pytest.mark.asyncio
async def test_manual_trigger_is_not_blocked_by_the_gate(runner, monkeypatch):
    """人工 / agent 经 `POST /api/rules/{id}/trigger` 显式触发不受这道闸约束。

    闸约束的是**感知通路自己**去驱动设备(纯视觉模型没有音频佐证、没有身份识别)。
    这里发起的正是那个被授权做决定的角色 —— 挡住它是过度拦截,而且端点会把
    `None` 翻译成"规则不存在或未启用",一条彻头彻尾的错误信息。
    """
    _backend(monkeypatch, "local")
    executed: list = []

    async def _exec(rule_id, action):
        executed.append(action)
        return RuleActionExecuteResult(action=action, result=True)

    monkeypatch.setattr(runner, "_execute_action", _exec)
    monkeypatch.setattr(runner, "_sources_currently_true", lambda rid: ["manual"])

    res = await runner.trigger_rule("r-gas", "用户在界面上点了触发")
    assert res is not None
    assert len(executed) == 1, "人工触发被那道感知闸挡住了"


# ── 走真实入口:感知上报 → 状态机 → fire ──────────────────────────────────


@pytest.mark.asyncio
async def test_gate_holds_on_the_path_production_actually_takes(runner, monkeypatch):
    """经 ``update_state``(感知引擎每帧调的那个)驱动一次命中。

    此前所有闸测试都直接调 ``_fire``,于是依赖的是那个参数的**默认值**。把感知
    调用点改成 ``perception_driven=False``——燃气阀于是每次命中都会开——2885 条
    测试全绿。整条特性最要紧的安全属性,没有任何一条测试走过它真实的路径。
    """
    _backend(monkeypatch, "local")
    execute = AsyncMock()
    monkeypatch.setattr(runner, "_execute_action", execute)

    await runner.update_state("r-gas", "cam1", True, context="厨房检测到明火")
    await _drain(runner)

    execute.assert_not_called()
    kinds = [c.args[0].kind for c in runner._log_repo.create.call_args_list]
    assert kinds == [RuleLogKind.RULE_TRIGGER_FAILURE], kinds
    errors = [c.args[0].execute_result.error for c in runner._log_repo.create.call_args_list]
    assert "不执行设备动作" in errors[0]


@pytest.mark.asyncio
async def test_cloud_backend_still_drives_devices_on_the_same_path(runner, monkeypatch):
    """同一条真实路径上,云端通路必须照常执行 —— 否则这道闸就把所有人都拦了。"""
    _backend(monkeypatch, "cloud")
    executed: list = []

    async def _exec(rule_id, action):
        executed.append(action)
        return RuleActionExecuteResult(action=action, result=True)

    monkeypatch.setattr(runner, "_execute_action", _exec)

    await runner.update_state("r-gas", "cam1", True, context="厨房检测到明火")
    await _drain(runner)

    assert len(executed) == 1
    kinds = [c.args[0].kind for c in runner._log_repo.create.call_args_list]
    assert kinds == [RuleLogKind.RULE_TRIGGER_SUCCESS], kinds


# ── 能力判定优先问活引擎 ──────────────────────────────────────────────────


def test_capability_prefers_the_live_engine_over_config(monkeypatch):
    """只读配置有一个真实窗口:切回云端时配置先落盘,而随后的软停是 best-effort
    (失败仅告警)—— 那段时间里配置说 cloud、实际仍是本地引擎在感知,闸会放行
    设备动作。而这正是这道闸存在的理由。
    """
    from types import SimpleNamespace

    from miloco.config.settings import get_settings
    from miloco.perception import capabilities as cap

    cfg = get_settings().model_copy(deep=True)
    cfg.perception.engine_backend = "cloud"          # 配置已经切回云端
    monkeypatch.setattr(cap, "get_settings", lambda: cfg)

    local_engine = SimpleNamespace(STATIC_RULE_EXECUTION=False)  # 但引擎还是本地的
    monkeypatch.setattr(cap, "_active_engine", lambda: local_engine)
    assert cap.perception_executes_device_actions() is False, (
        "配置抢在引擎前面被信了 —— 软停失败的那段窗口里设备动作会被放行"
    )

    monkeypatch.setattr(cap, "_active_engine", lambda: SimpleNamespace())
    assert cap.perception_executes_device_actions() is True, "云端引擎没有该属性,应按执行处理"


def test_active_engine_attribute_chain_matches_the_real_types():
    """`_active_engine` 走的是一串私有属性。任何一环改名,它会静默返回 None 并
    回落到读配置 —— 也就是悄悄退回上面那个窗口。用真实类型钉住这条链路。"""
    # 直接对着 _active_engine 的源码取属性名,再逐个到真实类型上验证 —— 只列
    # 三个常量的话,改了 _active_engine 而没改这里,测试照样绿。
    import inspect
    import re as _re

    from miloco.perception.capabilities import _active_engine
    from miloco.perception.client import PerceptionEngineProxy
    from miloco.perception.processor import PipelineProcessor
    from miloco.perception.service import PerceptionService

    chain = _re.findall(r"\.(_[a-z_]+|perception_engine)\b",
                        inspect.getsource(_active_engine))
    assert "perception_engine" in chain, f"_active_engine 不再取引擎? {chain}"
    for attr, owner in (("_pipeline", PerceptionService),
                        ("_perception_engine_proxy", PipelineProcessor),
                        ("perception_engine", PerceptionEngineProxy)):
        assert attr in chain, f"_active_engine 不再走 {attr},这条测试已失效"
        assert attr in owner.__init__.__code__.co_names, f"{owner.__name__} 上没有 {attr}"


# ── 窗口跟着当前通路走 ────────────────────────────────────────────────────


def test_window_size_follows_the_active_backend(monkeypatch):
    """两条通路同一时刻只开一条,窗口就该跟着那条走。

    云端 4 秒是因为每帧都要付 token 钱;本地 12 秒是因为 codec 每 8 帧才产出
    1 张画布,窗口短了就喂不饱模型。各存一份再让用户手动对齐,迟早会忘。
    """
    from miloco.config.settings import get_settings
    from miloco.perception import capabilities as cap

    cfg = get_settings().model_copy(deep=True)
    cfg.perception.collect.window_size = 4
    cfg.perception.local_vision.window_size = 12
    monkeypatch.setattr(cap, "get_settings", lambda: cfg)

    cfg.perception.engine_backend = "cloud"
    assert cap.active_window_size_sec() == 4
    cfg.perception.engine_backend = "local"
    assert cap.active_window_size_sec() == 12, "切到本地窗口没跟着变"


def test_window_size_falls_back_when_config_is_unreadable(monkeypatch):
    """读不到配置时按云端既有默认,不能让采集停摆。"""
    from miloco.perception import capabilities as cap

    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(cap, "get_settings", _boom)
    assert cap.active_window_size_sec() == 4


def test_window_is_consistent_with_the_canvas_budget():
    """窗口、帧率、画布数是一条约束链,不是三个独立旋钮 —— 默认值必须自洽。

    画布数 = 源帧 / 8(group_size=32 产出 4 张),所以 N 张画布需要 8N 帧,
    而源帧 = 窗口 × 帧率。默认值若不自洽,模型会自己降档并每窗报一次警告。
    """
    from miloco.config.settings import LocalVisionSettings

    d = LocalVisionSettings()
    frames = d.window_size * d.container_fps
    assert frames >= d.codec_target_canvas * 8, (
        f"{d.window_size}s × {d.container_fps}fps = {frames} 帧,"
        f"喂不饱 {d.codec_target_canvas} 张画布(需 {d.codec_target_canvas * 8} 帧)"
    )
    assert d.codec_target_canvas % 4 == 0, "画布数必须能被 images_per_group=4 整除"
    assert d.max_frames >= frames, "max_frames 比一个窗口的帧数还小,等于主动丢画布"
