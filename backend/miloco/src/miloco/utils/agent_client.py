# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""HTTP client for sending requests to the agent webhook."""

import json
import logging
import time
from typing import Any

import httpx

from miloco.config import get_settings
from miloco.middleware.exceptions import AgentWebhookException

logger = logging.getLogger(__name__)

# run_agent_turn 的 HTTP 超时在 waitForRun 超时之上再加的缓冲(秒)。
# HTTP 超时必须 > wait_timeout_ms,否则 HTTP 先断而平台 turn 仍在跑、语义错乱。
_HTTP_BUFFER_S = 15.0


async def call_agent_webhook(
    action: str,
    payload: Any = None,
    *,
    timeout: float = 30.0,
) -> Any:
    """POST to agent webhook with ``{ action, payload }`` body.

    The webhook returns ``{ code, message, data }``.
    On ``code == 0`` the *data* field is returned.
    Otherwise raises :class:`AgentWebhookException`.
    """
    agent = get_settings().agent
    url = agent.webhook_url
    headers = (
        {"Authorization": f"Bearer {agent.auth_bearer}"} if agent.auth_bearer else {}
    )
    body = (
        {"action": action, "payload": payload or {}}
        if payload is not None
        else {"action": action}
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            from miloco.utils.bootstrap import BOOT_FROM

            if BOOT_FROM != "cli":
                logger.info(
                    "call_agent_webhook action=%s payload=%s",
                    action,
                    json.dumps(body, ensure_ascii=False),
                )
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise AgentWebhookException(f"Cannot connect to agent webhook: {e}") from e
        except httpx.TimeoutException as e:
            raise AgentWebhookException(f"Agent webhook request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            message_str = ""
            try:
                result = e.response.json()
                message_str = result.get(
                    "message", result.get("detail", "unknown error")
                )
            except Exception:
                message_str = e.response.text
            raise AgentWebhookException(
                f"Agent webhook returned HTTP {e.response.status_code}: {message_str}"
            ) from e
        except Exception as e:
            raise AgentWebhookException(f"Agent webhook request failed: {e}") from e

    try:
        result: dict[str, Any] = response.json()
    except Exception as e:
        raise AgentWebhookException(f"Agent webhook returned invalid JSON: {e}") from e

    code = result.get("code", -1)
    if code != 0:
        raise AgentWebhookException(
            f"Agent action '{action}' failed: [{code}] {result.get('message', 'unknown error')}"
        )
    return result.get("data")


async def reset_agent_sessions(
    session_keys: list[str],
    *,
    delete_transcript: bool = True,
    timeout: float = 10.0,
) -> Any:
    """批量重置（删除）指定 agent session：调插件 ``reset_sessions`` webhook。

    用于切换家庭时清掉旧家庭遗留的 miloco 会话上下文。``session_keys`` 空则直接短路
    （不发请求）。传输 / 结构化失败沿用 :func:`call_agent_webhook` 抛
    :class:`AgentWebhookException`——本函数不兜底，由调用方（switch_home 的后台任务）
    捕获并降级为 WARN，绝不影响切换本身。返回 webhook 的 ``data``
    （``{"reset": [...], "failed": [...]}``）。
    """
    if not session_keys:
        return None
    return await call_agent_webhook(
        "reset_sessions",
        {
            "sessionKeys": list(session_keys),
            "deleteTranscript": delete_transcript,
        },
        timeout=timeout,
    )


async def run_agent_turn(
    text: str,
    *,
    session_key: str,
    lane: str,
    trace_id: str,
    wait_timeout_ms: int,
    deliver: bool | None = None,
    resolve_target: str | None = None,
) -> tuple[str | None, str, float]:
    """投递一条消息并**同步等待**该 turn 结束(或超时),返回 ``(run_id, status, rtt_ms)``。

    - ``status`` ∈ ``{"ok", "error", "timeout", "no-channel"}``:webhook 结果透传;
      ``no-channel`` 仅 ``resolve_target="owner-channel"`` 时可能出现——插件侧解析
      不到任何车主 IM 会话(从未私聊过 bot),结构化返回、不算传输失败。
    - ``run_id``:平台 turn id;``data`` 缺 runId 时为 None。
    - ``rtt_ms``:HTTP 往返耗时(因 webhook 同步阻塞,已含 turn 执行时长)。
    - ``deliver`` / ``resolve_target``:可选透传给插件 webhook——deliver=True 让
      assistant 回复投递到会话绑定的 IM channel(用户可见);resolve_target=
      "owner-channel" 让插件忽略 sessionKey、把 turn 跑在车主 IM 会话里。
      不传(None)时不进 payload,插件按原默认行为(后台 turn,deliver=false)。

    HTTP 超时 = ``wait_timeout_ms/1000 + _HTTP_BUFFER_S``,**必须 > wait_timeout_ms**,
    否则 HTTP 先超时而平台 turn 仍在跑。webhook 传输失败(连接/5xx/HTTP 超时)直接
    抛 :class:`AgentWebhookException`,由调用方(drainer)捕获跳过,本函数不兜底。
    """
    started_at = time.monotonic()
    payload: dict[str, Any] = {
        "message": text,
        "sessionKey": session_key,
        "lane": lane,
        "traceId": trace_id,
        # 批次稳定幂等键:HTTP 真断(turn 已起但响应丢)后 dispatcher 重试会发新
        # 请求,平台按此键去重,避免同会话并发起第二个 turn 击穿 "在途 turn ≤ 1"。
        "idempotencyKey": trace_id,
        "timeoutMs": wait_timeout_ms,
    }
    if deliver is not None:
        payload["deliver"] = deliver
    if resolve_target is not None:
        payload["resolveTarget"] = resolve_target
    data = await call_agent_webhook(
        "agent",
        payload,
        timeout=wait_timeout_ms / 1000 + _HTTP_BUFFER_S,
    )
    rtt_ms = (time.monotonic() - started_at) * 1000
    run_id: str | None = None
    status = "error"
    if isinstance(data, dict):
        run_id = data.get("runId")
        status = data.get("status", "error")
        # 上下文溢出自愈观测：溢出 turn 的 give-up 分支返回 isError payload 而非抛错，
        # 平台据此把终态判成 status="ok"、waitForRun 不带 error；故后端识别溢出只能看 webhook
        # 透出的 recovered（true=已删会话重建恢复 / false=系统提示超预算不可恢复），error 则
        # 由 webhook 从 trace meta 取回具体溢出文案。仅在异常时打日志，不刷屏。
        recovered = data.get("recovered")
        error = data.get("error")
        if recovered is True:
            logger.warning(
                "agent session self-healed after context overflow session=%s; "
                "old session deleted & recreated (reason=%s)",
                session_key,
                error,
            )
        elif recovered is False:
            logger.warning(
                "agent session context overflow NOT recoverable by reset session=%s; "
                "system prompt likely exceeds context budget (reason=%s)",
                session_key,
                error,
            )
        elif status == "error" and error:
            logger.warning(
                "agent turn returned error session=%s error=%s",
                session_key,
                error,
            )
    return run_id, status, rtt_ms
