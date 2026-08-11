# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for DeviceWelcomeService — the shared welcome action.

Covers: present + in-scope → dispatch; absent → skip; out-of-scope → skip;
dedup within the window → second skipped; a failed dispatch is not recorded
(retry allowed); and the message template fields.

The greeting is routed through the module-level ``dispatch_event`` (patched
here per test); ``dispatch_event("bind", [msg], builder)`` puts the message at
call ``args[1][0]``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.miot import welcome_service as ws
from miloco.miot.welcome_service import DeviceWelcomeService


def _device(did="d1", name="测试设备", room="卧室", home="测试家", home_id="H1",
            model="test.model.x1"):
    return SimpleNamespace(
        did=did, name=name, home_id=home_id, home_name=home, room_name=room,
        model=model,
    )


def _service(devices, allowed):
    return DeviceWelcomeService(
        get_device=lambda did: devices.get(did),
        is_home_allowed=lambda hid: hid in allowed,
        log_device_diff=lambda *a: None,
    )


def _patch_dispatch(monkeypatch, *, sent=True):
    mock = AsyncMock(return_value=sent)
    monkeypatch.setattr(ws, "dispatch_event", mock)
    return mock


@pytest.mark.asyncio
async def test_welcome_present_in_scope_dispatches(monkeypatch):
    mock = _patch_dispatch(monkeypatch, sent=True)
    svc = _service({"d1": _device()}, {"H1"})

    assert await svc.welcome("d1") is True
    mock.assert_awaited_once()
    assert mock.await_args.args[0] == "bind"  # event type
    msg = mock.await_args.args[1][0]
    assert "[新设备接入]" in msg and "测试设备" in msg and "卧室" in msg


def test_welcome_message_does_not_ask_agent_to_self_name_miloco():
    """播报模板不得要求 agent 对用户自称 miloco。

    本文本与插件 B_IDENTITY 同一轮进上下文，而该块明令"不要改口自称 Miloco"；
    两边给相反的硬指令，结果要么顶掉宿主人设、要么让本模板的要求静默落空。
    设备被纳管、成为"能力延伸"的语义仍要在，只是换成不指名的第一人称。
    """
    msg = ws.DeviceWelcomeService._format_message(_device())
    assert "miloco发现" not in msg
    assert "miloco 的能力延伸" not in msg
    assert "不要自称 miloco" in msg
    assert "你自身能力的延伸" in msg
    # 交付渠道仍走 skill：这里的 miloco-notify 是工具名，不是 agent 自称
    assert "miloco-notify" in msg


@pytest.mark.asyncio
async def test_welcome_absent_skips(monkeypatch):
    mock = _patch_dispatch(monkeypatch)
    svc = _service({}, {"H1"})
    assert await svc.welcome("d1") is False
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_welcome_out_of_scope_skips(monkeypatch):
    mock = _patch_dispatch(monkeypatch)
    svc = _service({"d1": _device(home_id="H_other")}, {"H1"})
    assert await svc.welcome("d1") is False
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_welcome_dedup_within_window(monkeypatch):
    mock = _patch_dispatch(monkeypatch, sent=True)
    svc = _service({"d1": _device()}, {"H1"})

    assert await svc.welcome("d1") is True
    # Immediate second call for the same did → deduped.
    assert await svc.welcome("d1") is False
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_failed_dispatch_not_recorded_for_dedup(monkeypatch):
    # dispatch returns False → not sent → not recorded → retry allowed.
    mock = _patch_dispatch(monkeypatch, sent=False)
    svc = _service({"d1": _device()}, {"H1"})

    assert await svc.welcome("d1") is False
    assert await svc.welcome("d1") is False
    assert mock.await_count == 2  # retried, not deduped


def test_format_message_fields():
    msg = DeviceWelcomeService._format_message(
        _device(did="12345", name="床头灯", room="主卧", home="我的家",
                model="xiaomi.light.x1")
    )
    assert "[新设备接入]" in msg
    assert all(s in msg for s in ("床头灯", "12345", "主卧", "我的家", "xiaomi.light.x1"))


def test_format_message_fallbacks():
    dev = _device(did="99999", name=None, room=None, home=None, model="")
    msg = DeviceWelcomeService._format_message(dev)
    assert all(s in msg for s in ("未知设备", "未知房间", "未知家庭", "未知型号", "99999"))
