# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""OnboardingTriggerService 单测 —— 全新安装主动邀请的触发排列组合。

覆盖：全新安装触发一次；person 非空 / 档案非空 / 米家未就绪 / KV 标记已置位
均静默；发送失败不置位（下次重试）；发送成功置位后二次调用静默；并发汇入只发
一次；dispatcher 路由包含 onboarding 且不入统计。约定同 test_welcome_service：
monkeypatch 模块级 ``dispatch_event``。

另含**真 dispatcher 回归组**：驱动真实 AgentDispatcher + 假 run_agent_turn，
守住「终身标记只认真送达」——入队接纳 ≠ 送达，传输耗尽 / turn error 时标记必须
不置位（stub dispatch_event 的测试盖不住这条链路）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.database.kv_repo import OnboardingKeys
from miloco.dispatch import AgentDispatcher, set_agent_dispatcher
from miloco.dispatch import dispatcher as disp_mod
from miloco.dispatch.dispatcher import _ROUTE, _TRACKED
from miloco.home_profile import onboarding_trigger as ot
from miloco.home_profile.onboarding_trigger import OnboardingTriggerService
from miloco.middleware.exceptions import AgentWebhookException


class _FakeKV:
    """dict 版 KVRepo 替身：只实现 trigger 用到的 get/set。"""

    def __init__(self, initial: dict[str, str] | None = None):
        self.data = dict(initial or {})

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value
        return True


def _service(
    kv=None, *, miot_ready=True, persons=False, profile_entries=False,
) -> tuple[OnboardingTriggerService, _FakeKV]:
    kv = kv or _FakeKV()
    svc = OnboardingTriggerService(
        kv_repo=kv,
        is_miot_ready=lambda: miot_ready,
        has_persons=lambda: persons,
        has_profile_entries=lambda: profile_entries,
    )
    return svc, kv


def _patch_dispatch(monkeypatch, *, sent=True):
    """stub dispatch_event：入队恒接纳（返 True），送达结果经 delivered future 给出。

    与真实契约对齐：返回值是「接纳」，送达与否由 dispatcher resolve future——
    stub 必须 resolve，否则 maybe_trigger 会等到守护超时。
    """

    def _side_effect(event_type, items, builder, intra_priority=0, delivered=None):
        if delivered is not None and not delivered.done():
            delivered.set_result(sent)
        return True

    mock = AsyncMock(side_effect=_side_effect)
    monkeypatch.setattr(ot, "dispatch_event", mock)
    return mock


@pytest.mark.asyncio
async def test_fresh_install_fires_once_and_sets_flag(monkeypatch):
    mock = _patch_dispatch(monkeypatch, sent=True)
    svc, kv = _service()

    assert await svc.maybe_trigger() is True
    mock.assert_awaited_once()
    assert mock.await_args.args[0] == "onboarding"  # event type
    msg = mock.await_args.args[1][0]
    assert "[系统事件]" in msg and "miloco-onboarding" in msg and "初始化家庭" in msg
    # 标记已置位（存时间戳）
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY)


@pytest.mark.asyncio
async def test_persons_nonempty_stays_silent(monkeypatch):
    mock = _patch_dispatch(monkeypatch)
    svc, kv = _service(persons=True)
    assert await svc.maybe_trigger() is False
    mock.assert_not_awaited()
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY) is None


@pytest.mark.asyncio
async def test_profile_nonempty_stays_silent(monkeypatch):
    mock = _patch_dispatch(monkeypatch)
    svc, _ = _service(profile_entries=True)
    assert await svc.maybe_trigger() is False
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_miot_not_ready_stays_silent(monkeypatch):
    mock = _patch_dispatch(monkeypatch)
    svc, _ = _service(miot_ready=False)
    assert await svc.maybe_trigger() is False
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_already_set_stays_silent(monkeypatch):
    mock = _patch_dispatch(monkeypatch)
    kv = _FakeKV({OnboardingKeys.ONBOARDING_PROMPTED_KEY: "2026-07-01T00:00:00"})
    svc, _ = _service(kv)
    assert await svc.maybe_trigger() is False
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_failure_keeps_flag_unset_and_retries(monkeypatch):
    # sent=False → 不置位 → 下一次调用（如下次启动）重试。
    mock = _patch_dispatch(monkeypatch, sent=False)
    svc, kv = _service()

    assert await svc.maybe_trigger() is False
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY) is None
    assert await svc.maybe_trigger() is False
    assert mock.await_count == 2  # 重试而非静默


@pytest.mark.asyncio
async def test_success_then_second_call_silent(monkeypatch):
    mock = _patch_dispatch(monkeypatch, sent=True)
    svc, kv = _service()

    assert await svc.maybe_trigger() is True
    assert await svc.maybe_trigger() is False
    assert mock.await_count == 1
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY)


@pytest.mark.asyncio
async def test_concurrent_calls_fire_once(monkeypatch):
    # 启动调用点与授权回调可能并发汇入：lock 串行化后只发一次。
    mock = _patch_dispatch(monkeypatch, sent=True)
    svc, _ = _service()

    results = await asyncio.gather(svc.maybe_trigger(), svc.maybe_trigger())
    assert sorted(results) == [False, True]
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_condition_callback_error_treated_as_not_met(monkeypatch):
    # 条件回调抛异常 → 按不满足处理，不发、不置位、不抛给调用方。
    mock = _patch_dispatch(monkeypatch)

    def _boom() -> bool:
        raise RuntimeError("db down")

    kv = _FakeKV()
    svc = OnboardingTriggerService(
        kv_repo=kv,
        is_miot_ready=_boom,
        has_persons=lambda: False,
        has_profile_entries=lambda: False,
    )
    assert await svc.maybe_trigger() is False
    mock.assert_not_awaited()
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY) is None


@pytest.mark.asyncio
async def test_not_accepted_keeps_flag_unset_and_retries(monkeypatch):
    # 入队未被接纳（调度器未就绪/淘汰）→ 不置位、可重试。
    def _reject(event_type, items, builder, intra_priority=0, delivered=None):
        if delivered is not None and not delivered.done():
            delivered.set_result(False)
        return False

    mock = AsyncMock(side_effect=_reject)
    monkeypatch.setattr(ot, "dispatch_event", mock)
    svc, kv = _service()

    assert await svc.maybe_trigger() is False
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY) is None
    assert await svc.maybe_trigger() is False
    assert mock.await_count == 2  # 重试而非静默


@pytest.mark.asyncio
async def test_guard_timeout_no_flag_but_no_infire_repeat(monkeypatch):
    # 送达 future 一直不 resolve（结果未知）→ 守护超时：KV 不置位（下次启动重试），
    # 但 _fired 置位防本进程内重发。
    monkeypatch.setattr(ot, "_delivery_guard_timeout_s", lambda: 0.05)

    def _accept_never_resolve(event_type, items, builder, intra_priority=0, delivered=None):
        return True  # 接纳但不 resolve

    mock = AsyncMock(side_effect=_accept_never_resolve)
    monkeypatch.setattr(ot, "dispatch_event", mock)
    svc, kv = _service()

    assert await svc.maybe_trigger() is False
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY) is None
    # 本进程内不再重发
    assert await svc.maybe_trigger() is False
    assert mock.await_count == 1


def test_onboarding_route_registered_untracked():
    """onboarding 路由与 bind 同会话/车道/优先级档，且不入 agent_runs 统计。"""
    assert _ROUTE["onboarding"] == ("agent:main:miloco", "miloco-interactive", 30)
    assert _ROUTE["onboarding"] == _ROUTE["bind"]
    assert "onboarding" not in _TRACKED


def test_guard_timeout_upper_bounds_dispatcher_worst_case():
    """不变量：守护超时 > dispatcher 最坏 resolve 耗时（按当前 settings/常量独立计算）。

    曾经的真实 bug：守护硬编码 120s < turn_wait 默认 180s —— 一次 120~180s 的
    "慢但成功"送达会被误判未送达 → 下次启动双邀请。这里用真实常量独立复算
    worst case，任何一侧改动导致守护跌破上界即变红。
    """
    from miloco.config import get_settings
    from miloco.utils.agent_client import _HTTP_BUFFER_S

    wait_s = get_settings().dispatcher.turn_wait_timeout_ms / 1000
    retries = AgentDispatcher._TRANSPORT_RETRIES
    worst = (retries + 1) * (wait_s + _HTTP_BUFFER_S) + sum(
        AgentDispatcher._TRANSPORT_BACKOFF_S * (2**a) for a in range(retries)
    )
    guard = ot._delivery_guard_timeout_s()
    assert guard > worst, f"守护超时 {guard}s 未覆盖 dispatcher 最坏 resolve 耗时 {worst}s"
    # 最起码要盖过单次 turn 等待（120s < 180s 的旧 bug 形态）
    assert guard > wait_s


# ─── 真 dispatcher 回归组 ─────────────────────────────────────────────────────
# stub dispatch_event 的测试无法暴露「入队接纳 ≠ 送达」：这里驱动真实
# AgentDispatcher（drainer / 重试 / delivered future 全真），只假 run_agent_turn。


@pytest.fixture
async def real_dispatcher(monkeypatch):
    monkeypatch.setattr(
        disp_mod,
        "get_settings",
        lambda: SimpleNamespace(
            dispatcher=SimpleNamespace(turn_wait_timeout_ms=1_000, max_queue=16)
        ),
    )
    monkeypatch.setattr(AgentDispatcher, "_TRANSPORT_BACKOFF_S", 0.001)
    d = AgentDispatcher()
    await d.start()
    set_agent_dispatcher(d)
    yield d
    await d.stop()
    set_agent_dispatcher(None)


@pytest.mark.asyncio
async def test_real_dispatcher_transport_failure_keeps_flag_unset(real_dispatcher, monkeypatch):
    """回归：drainer 传输重试耗尽静默丢批 → 标记必须不置位、下次可重试。"""

    async def failing_turn(msg, *, session_key, lane, trace_id, wait_timeout_ms, **kw):
        raise AgentWebhookException("agent webhook still booting")

    monkeypatch.setattr(disp_mod, "run_agent_turn", failing_turn)
    svc, kv = _service()

    assert await svc.maybe_trigger() is False
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY) is None
    # 未置位 + 未 _fired → 下一次调用（模拟下次启动）重新尝试
    assert await svc.maybe_trigger() is False
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY) is None


@pytest.mark.asyncio
async def test_real_dispatcher_success_sets_flag(real_dispatcher, monkeypatch):
    """镜像：真 dispatcher 送达成功 → 标记置位、二次静默。"""

    async def ok_turn(msg, *, session_key, lane, trace_id, wait_timeout_ms, **kw):
        assert "[系统事件]" in msg
        return "run-1", "ok", 1.0

    monkeypatch.setattr(disp_mod, "run_agent_turn", ok_turn)
    svc, kv = _service()

    assert await svc.maybe_trigger() is True
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY)
    assert await svc.maybe_trigger() is False  # 一次性


@pytest.mark.asyncio
async def test_real_dispatcher_timeout_status_counts_as_delivered(real_dispatcher, monkeypatch):
    """status=timeout：平台侧 turn 仍在途 → 视作送达，置位（避免双邀请）。"""

    async def timeout_turn(msg, *, session_key, lane, trace_id, wait_timeout_ms, **kw):
        return None, "timeout", 1.0

    monkeypatch.setattr(disp_mod, "run_agent_turn", timeout_turn)
    svc, kv = _service()

    assert await svc.maybe_trigger() is True
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY)


@pytest.mark.asyncio
async def test_real_dispatcher_error_status_keeps_flag_unset(real_dispatcher, monkeypatch):
    """status=error：turn 执行失败 → 不算送达，不置位（下次启动重试）。"""

    async def error_turn(msg, *, session_key, lane, trace_id, wait_timeout_ms, **kw):
        return "run-1", "error", 1.0

    monkeypatch.setattr(disp_mod, "run_agent_turn", error_turn)
    svc, kv = _service()

    assert await svc.maybe_trigger() is False
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY) is None


@pytest.mark.asyncio
async def test_real_dispatcher_no_channel_keeps_flag_unset(real_dispatcher, monkeypatch):
    """no-channel（车主从未私聊过 bot）→ 未送达不置位；绑定 channel 后重试即送达。

    全链路走真 dispatcher：断言 onboarding 批次带 owner-channel 投递参数、
    结构化 no-channel 只调一次（不烧传输重试）、标记不置位；随后"车主绑定了
    channel"（turn 转 ok）→ 下一次 maybe_trigger 送达并置位。
    """
    calls: list[dict] = []
    channel_bound = False

    async def turn(msg, *, session_key, lane, trace_id, wait_timeout_ms, **kw):
        calls.append(kw)
        if not channel_bound:
            return None, "no-channel", 1.0
        return "run-1", "ok", 1.0

    monkeypatch.setattr(disp_mod, "run_agent_turn", turn)
    svc, kv = _service()

    # 未绑 channel：未送达、不置位、只调一次
    assert await svc.maybe_trigger() is False
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY) is None
    assert calls == [{"resolve_target": "owner-channel", "deliver": True}]

    # 车主绑定 channel 后（模拟下次启动重试）：送达并置位
    channel_bound = True
    assert await svc.maybe_trigger() is True
    assert kv.get(OnboardingKeys.ONBOARDING_PROMPTED_KEY)
