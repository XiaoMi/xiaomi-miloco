"""Hermes api_server 同步 chat 客户端：把 miloco turn 翻译成 Hermes /v1/chat/completions。

miloco 后端通过 ``run_agent_turn`` 期望 webhook 同步返回 ``{runId, status, error?, recovered?}``
（``status ∈ {ok, error, timeout}``，见
``backend/miloco/src/miloco/utils/agent_client.py``）。Hermes api_server 暴露同步
OpenAI 兼容端点 ``POST /v1/chat/completions``（见
``gateway/platforms/api_server.py::_handle_chat_completions``），返回标准
``{choices:[{message:{content}}]}``，并支持 ``X-Hermes-Session-Id`` 请求头做跨回合
会话连续（从 state.db 加载历史）。

本模块负责：
1. 用 (sessionKey, lane) 确定性映射出 Hermes session_id（见 ``session_map``），通过
   ``X-Hermes-Session-Id`` 头传入——同一 miloco 会话的多次回调落到同一 Hermes 会话，
   上下文连续。
2. 同步发起 chat，HTTP 超时 = ``timeoutMs/1000 + 15``（对齐 miloco 后端
   ``wait_timeout_ms + _HTTP_BUFFER_S``，避免 HTTP 先断而 turn 仍在跑）。
3. ``extraSystemPrompt`` 作为 system 角色消息前置。
4. 上下文溢出尽力自愈：识别溢出错误文案后，丢弃会话上下文用无 session 头的全新 turn
   重试一次（api_server 无 session 删除/重置路由，故用「无历史重试」近似 OpenClaw 的
   ``deleteSession + re-run``）。

best-effort：溢出信号识别依赖 Hermes 实例的实测错误文案；v0.10.0 的 api_server 路由
（/v1/chat/completions、X-Hermes-Session-Id）与 main 分支的 /api/sessions/{id}/chat
不同，本实现按已安装的 v0.10.0 适配。
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import httpx

from .session_map import map_session

logger = logging.getLogger(__name__)

# HTTP 超时在 miloco 后端 wait_timeout_ms 之上加的缓冲(秒)，对齐
# backend/miloco/src/miloco/utils/agent_client.py::_HTTP_BUFFER_S。
_HTTP_BUFFER_S = 15.0

# 上下文溢出关键词。Hermes api_server 没有标准化溢出错误码（_handle_chat_completions
# 把 agent 异常包成 OpenAI-style 5xx，文案来自 provider），只能按文案匹配。
# best-effort, 需 Hermes 实例验证：真实溢出文案可能是 "context length exceeded"、
# "maximum context length" 等，此处匹配范围偏宽以兜底。
_OVERFLOW_MARKERS = (
    "context overflow",
    "context length",
    "context window",
    "maximum context",
    "token limit",
    "context budget",
    # Anthropic-style（reviewer #3 补的）
    "prompt is too long",
    "too many tokens",
)


def _looks_like_overflow(text: str | None) -> bool:
    """best-effort 判定错误文案是否指示上下文溢出。无可靠信号时返回 False。"""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


def _new_run_id() -> str:
    """生成 miloco 期望的 runId（平台 turn id）。/v1/chat/completions 不返回 run id，故本地生成。"""
    return str(uuid.uuid4())


class HermesClient:
    """异步 Hermes api_server 客户端，封装同步 turn 语义。

    线程安全：每次 ``run_turn`` 创建独立 httpx.AsyncClient，无共享可变状态。
    """

    def __init__(self, api_url: str, api_key: str) -> None:
        # 去掉尾部斜杠，避免 ``http://host:port//v1/...`` 双斜杠。
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key

    def _headers(self, session_id: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id
        return headers

    def _build_messages(self, message: Any, system_message: str | None) -> list[dict[str, str]]:
        """组装 OpenAI 风格 messages：可选 system 前置 + 单条 user。"""
        msgs: list[dict[str, str]] = []
        if system_message:
            msgs.append({"role": "system", "content": system_message})
        msgs.append({"role": "user", "content": str(message)})
        return msgs

    async def _chat_once(
        self,
        client: httpx.AsyncClient,
        messages: list[dict[str, str]],
        session_id: str | None,
        timeout_s: float,
    ) -> httpx.Response:
        """单次同步 chat 调用。不捕获异常，由调用方按超时/溢出分支处理。"""
        body: dict[str, Any] = {"messages": messages}
        # 模型可由 HERMES_MODEL env 指定；为空则省略，api_server 用其配置的默认模型。
        model = os.environ.get("HERMES_MODEL", "").strip()
        if model:
            body["model"] = model
        return await client.post(
            f"{self._api_url}/v1/chat/completions",
            json=body,
            headers=self._headers(session_id),
            timeout=timeout_s,
        )

    async def run_turn(self, payload: dict[str, Any]) -> dict[str, Any]:
        """执行一次同步 turn，返回 miloco 期望的 ``{runId, status, error?, recovered?}``。

        参数 ``payload`` 字段（见 agent_client.py::run_agent_turn 与
        openclaw/.../webhooks/agent.ts::IRequestBody）：
        - ``message``: 用户消息（必填）
        - ``sessionKey``: 会话键，默认 "main"
        - ``lane``: 通道，默认 "default"
        - ``timeoutMs``: turn 等待上限（毫秒），HTTP 超时 = timeoutMs/1000 + 15
        - ``extraSystemPrompt``: 可选系统提示，作为 system 消息前置
        """
        run_id = _new_run_id()
        message = payload.get("message")
        if message is None:
            return {"runId": run_id, "status": "error", "error": "missing 'message' in payload"}
        session_key = payload.get("sessionKey") or "main"
        lane = payload.get("lane") or "default"
        session_id = map_session(session_key, lane)
        system_message = payload.get("extraSystemPrompt")
        try:
            timeout_ms = int(payload.get("timeoutMs") or 180_000)
        except (TypeError, ValueError):
            timeout_ms = 180_000
        timeout_s = max(timeout_ms / 1000.0, 1.0) + _HTTP_BUFFER_S

        messages = self._build_messages(message, system_message)

        async with httpx.AsyncClient() as client:
            # --- 首次 chat（带 session 头，上下文连续）---
            logger.info(
                "[adapter] → Hermes session_id=%s url=%s/v1/chat/completions timeout=%.1fs msg_len=%d",
                session_id, self._api_url, timeout_s, len(str(message)),
            )
            try:
                resp = await self._chat_once(client, messages, session_id, timeout_s)
            except httpx.TimeoutException:
                logger.warning(
                    "[adapter] ← Hermes TIMEOUT session=%s timeout=%.1fs", session_id, timeout_s
                )
                return {"runId": run_id, "status": "timeout"}
            except httpx.HTTPError as e:
                logger.warning(
                    "[adapter] ← Hermes transport error session=%s: %s", session_id, e
                )
                return {"runId": run_id, "status": "error", "error": str(e)}

            # 2xx → 成功
            if 200 <= resp.status_code < 300:
                logger.info(
                    "[adapter] ← Hermes HTTP %d session=%s OK", resp.status_code, session_id
                )
                return {"runId": run_id, "status": "ok"}

            # 非 2xx：尝试识别溢出并自愈（无 session 头重试一次）
            err_text = self._extract_error_text(resp)
            logger.warning(
                "[adapter] ← Hermes HTTP %d session=%s err=%s",
                resp.status_code, session_id, err_text[:200],
            )
            if _looks_like_overflow(err_text):
                logger.warning(
                    "[overflow-self-heal] context overflow suspected session=%s status=%s; "
                    "retrying once without session history",
                    session_id, resp.status_code,
                )
                return await self._heal_and_retry(
                    client, messages, timeout_s, run_id, err_text
                )

            return {
                "runId": run_id,
                "status": "error",
                "error": f"hermes chat HTTP {resp.status_code}: {err_text[:300]}",
            }

    async def _heal_and_retry(
        self,
        client: httpx.AsyncClient,
        messages: list[dict[str, str]],
        timeout_s: float,
        run_id: str,
        overflow_reason: str,
    ) -> dict[str, Any]:
        """best-effort 溢出自愈：丢弃会话历史，用无 X-Hermes-Session-Id 的全新 turn 重试一次。

        v0.10.0 的 api_server 无 session 删除/重置路由，无法精确复刻 OpenClaw 的
        ``deleteSession({deleteTranscript:true}) + re-run``。此处用「不带 session 头」
        起一个无历史的全新 turn 近似——能清掉累积的对话上下文，但丢掉该会话此前记忆。
        重建后仍溢出 = 系统提示自身超预算，不可恢复 → 停手返回。

        best-effort, 需 Hermes 实例验证。
        """
        try:
            resp = await self._chat_once(client, messages, None, timeout_s)
        except httpx.TimeoutException:
            return {
                "runId": run_id,
                "status": "timeout",
                "recovered": False,
                "error": overflow_reason,
            }
        except httpx.HTTPError as e:
            return {
                "runId": run_id,
                "status": "error",
                "recovered": False,
                "error": str(e),
            }

        if 200 <= resp.status_code < 300:
            logger.info("[overflow-self-heal] recovered via fresh stateless turn")
            return {"runId": run_id, "status": "ok", "recovered": True}

        retry_err = self._extract_error_text(resp)
        if _looks_like_overflow(retry_err):
            logger.error(
                "[overflow-self-heal] still overflow after fresh retry; unrecoverable"
            )
            return {
                "runId": run_id,
                "status": "error",
                "recovered": False,
                "error": retry_err or overflow_reason,
            }
        return {
            "runId": run_id,
            "status": "error",
            "recovered": False,
            "error": f"hermes chat HTTP {resp.status_code} after fresh retry: {retry_err[:300]}",
        }

    @staticmethod
    def _extract_error_text(resp: httpx.Response) -> str:
        """从非 2xx 响应里提取人类可读错误文案，兼容 OpenAI-style envelope。"""
        try:
            data = resp.json()
        except Exception:
            return resp.text or ""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
                if isinstance(msg, str):
                    return msg
            msg = data.get("message") or data.get("detail")
            if isinstance(msg, str):
                return msg
        return str(data)
