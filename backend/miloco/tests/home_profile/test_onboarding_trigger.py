# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""OnboardingTriggerService 单测 —— 全新安装主动邀请的触发排列组合。

覆盖：全新安装触发一次；person 非空 / 档案非空 / 米家未就绪 / KV 标记已置位
均静默；发送失败不置位（下次重试）；发送成功置位后二次调用静默；并发汇入只发
一次；dispatcher 路由包含 onboarding 且不入统计。约定同 test_welcome_service：
monkeypatch 模块级 ``dispatch_event``。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from miloco.database.kv_repo import OnboardingKeys
from miloco.dispatch.dispatcher import _ROUTE, _TRACKED
from miloco.home_profile import onboarding_trigger as ot
from miloco.home_profile.onboarding_trigger import OnboardingTriggerService


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
    mock = AsyncMock(return_value=sent)
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


def test_onboarding_route_registered_untracked():
    """onboarding 路由与 bind 同会话/车道/优先级档，且不入 agent_runs 统计。"""
    assert _ROUTE["onboarding"] == ("agent:main:miloco", "miloco-interactive", 30)
    assert _ROUTE["onboarding"] == _ROUTE["bind"]
    assert "onboarding" not in _TRACKED
