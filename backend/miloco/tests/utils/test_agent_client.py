# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for ``run_agent_turn`` — the synchronous投递+等待 wrapper used by the
dispatcher drainer.

Contract under test:
  * On a webhook dict reply, ``(runId, status, rtt_ms)`` is passed through;
    missing ``runId`` → None, missing ``status`` → "error".
  * The caller-supplied message / sessionKey / lane / traceId / timeoutMs land
    in the webhook payload verbatim.
  * HTTP timeout = ``wait_timeout_ms/1000 + _HTTP_BUFFER_S`` (must exceed the
    platform wait so HTTP never aborts a turn that is still running).
  * Transport failures are NOT swallowed — the exception propagates to the
    drainer, which decides whether to skip.
"""

from unittest.mock import AsyncMock, patch

import pytest
from miloco.middleware.exceptions import AgentWebhookException
from miloco.utils.agent_client import (
    _HTTP_BUFFER_S,
    reset_agent_sessions,
    run_agent_turn,
)

_WAIT_MS = 30_000


async def test_run_agent_turn_returns_runid_status_rtt():
    with patch(
        "miloco.utils.agent_client.call_agent_webhook",
        new=AsyncMock(return_value={"runId": "run-1", "status": "ok"}),
    ):
        run_id, status, rtt_ms = await run_agent_turn(
            "hello",
            session_key="agent:main:miloco",
            lane="miloco-interactive",
            trace_id="trace-abc",
            wait_timeout_ms=_WAIT_MS,
        )
    assert run_id == "run-1"
    assert status == "ok"
    assert rtt_ms >= 0


async def test_run_agent_turn_defaults_when_fields_missing():
    # No runId, no status → (None, "error").
    with patch(
        "miloco.utils.agent_client.call_agent_webhook",
        new=AsyncMock(return_value={}),
    ):
        run_id, status, _ = await run_agent_turn(
            "hi",
            session_key="s",
            lane="l",
            trace_id="t",
            wait_timeout_ms=_WAIT_MS,
        )
    assert run_id is None
    assert status == "error"


async def test_run_agent_turn_passes_params_and_timeout():
    captured: dict = {}

    async def fake(action, payload=None, *, timeout=30.0):
        captured["action"] = action
        captured["payload"] = payload or {}
        captured["timeout"] = timeout
        return {"runId": "r-1", "status": "ok"}

    with patch("miloco.utils.agent_client.call_agent_webhook", new=fake):
        await run_agent_turn(
            "hi",
            session_key="agent:main:miloco-rule",
            lane="miloco-rule",
            trace_id="trace-xyz",
            wait_timeout_ms=_WAIT_MS,
        )

    assert captured["action"] == "agent"
    p = captured["payload"]
    assert p["message"] == "hi"
    assert p["sessionKey"] == "agent:main:miloco-rule"
    assert p["lane"] == "miloco-rule"
    assert p["traceId"] == "trace-xyz"
    assert p["timeoutMs"] == _WAIT_MS
    # HTTP timeout must sit above the platform wait by exactly the buffer.
    assert captured["timeout"] == _WAIT_MS / 1000 + _HTTP_BUFFER_S


async def test_run_agent_turn_propagates_webhook_exception():
    with patch(
        "miloco.utils.agent_client.call_agent_webhook",
        new=AsyncMock(side_effect=AgentWebhookException("boom")),
    ):
        with pytest.raises(AgentWebhookException):
            await run_agent_turn(
                "hi",
                session_key="s",
                lane="l",
                trace_id="t",
                wait_timeout_ms=_WAIT_MS,
            )


async def test_run_agent_turn_omits_delivery_fields_by_default():
    # 不传 deliver/resolve_target → payload 不含对应字段，插件按原默认行为。
    mock = AsyncMock(return_value={"runId": "r", "status": "ok"})
    with patch("miloco.utils.agent_client.call_agent_webhook", new=mock):
        await run_agent_turn(
            "hi", session_key="s", lane="l", trace_id="t", wait_timeout_ms=_WAIT_MS,
        )
    payload = mock.await_args.args[1]
    assert "deliver" not in payload
    assert "resolveTarget" not in payload


async def test_run_agent_turn_passes_delivery_fields_when_set():
    # deliver/resolve_target 显式传入 → 原样落进 webhook payload（resolveTarget 驼峰）。
    mock = AsyncMock(return_value={"runId": "r", "status": "ok"})
    with patch("miloco.utils.agent_client.call_agent_webhook", new=mock):
        await run_agent_turn(
            "hi", session_key="s", lane="l", trace_id="t", wait_timeout_ms=_WAIT_MS,
            deliver=True, resolve_target="owner-channel",
        )
    payload = mock.await_args.args[1]
    assert payload["deliver"] is True
    assert payload["resolveTarget"] == "owner-channel"


async def test_run_agent_turn_passes_no_channel_status_through():
    # 插件结构化 no-channel（code 0 正常返回）→ status 原样透传，runId None，不抛异常。
    mock = AsyncMock(return_value={"runId": None, "status": "no-channel"})
    with patch("miloco.utils.agent_client.call_agent_webhook", new=mock):
        run_id, status, _ = await run_agent_turn(
            "hi", session_key="s", lane="l", trace_id="t", wait_timeout_ms=_WAIT_MS,
            deliver=True, resolve_target="owner-channel",
        )
    assert run_id is None
    assert status == "no-channel"


async def test_reset_agent_sessions_builds_payload():
    # session_keys / deleteTranscript 原样组包成 reset_sessions webhook payload。
    mock = AsyncMock(return_value={"reset": ["a", "b"], "failed": []})
    keys = ["agent:main:miloco", "agent:main:miloco-rule"]
    with patch("miloco.utils.agent_client.call_agent_webhook", new=mock):
        data = await reset_agent_sessions(keys)
    assert data == {"reset": ["a", "b"], "failed": []}
    action, payload = mock.await_args.args[0], mock.await_args.args[1]
    assert action == "reset_sessions"
    assert payload["sessionKeys"] == keys
    assert payload["deleteTranscript"] is True


async def test_reset_agent_sessions_short_circuits_on_empty():
    # 空列表直接短路：不发 webhook。
    mock = AsyncMock()
    with patch("miloco.utils.agent_client.call_agent_webhook", new=mock):
        assert await reset_agent_sessions([]) is None
    mock.assert_not_awaited()


async def test_reset_agent_sessions_propagates_webhook_exception():
    # 传输/结构化失败不吞：交由调用方（switch_home 后台任务）兜底降级。
    with patch(
        "miloco.utils.agent_client.call_agent_webhook",
        new=AsyncMock(side_effect=AgentWebhookException("boom")),
    ):
        with pytest.raises(AgentWebhookException):
            await reset_agent_sessions(["agent:main:miloco"])
