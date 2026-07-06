"""HermesAdapter —— OpenAI 兼容 ``/v1/chat/completions`` 客户端 + 系统消息构造。

设计:见 ``hermes-pr.md`` §五 #1+#2+#11。本文件由 backend importlib 动态加载,
不需要也不应该 import 后端 wheel 的任何符号(duck-typed)。

Hermes /v1/chat/completions 协议(OpenAI 兼容 Chat Completions API):
- 请求: ``{"messages": [{"role": "system", ...}, {"role": "user", ...}], "model"?: "..."}``
- 响应: 标准 OpenAI 风格 ``{"choices": [{"message": {"content": "..."}}]}``
- 头: ``Authorization: Bearer $API_SERVER_KEY``、``X-Hermes-Session-Id: <session_id>``
  (用于跨回合会话连续,从 hermes state.db 加载历史)
- 模型: 可由 ``HERMES_MODEL`` env 指定,空则用 hermes 自己的默认模型

溢出自愈(沿用 279): 识别错误文案含 ``_OVERFLOW_MARKERS`` 时,无 session 头重试一次
(最佳努力,v0.10.0 api_server 无 session 删除/重置路由)。

trace 文件 IPC(#11): plugin ``trace.py`` 写 ``$MILOCO_HOME/trace/<run_id>.meta.json``,
本 adapter ``read_trace_meta`` 读盘返回。本 session 暂未重构 ``trace.py``,所以读盘
通常返回 None —— backend meta_poller 走超时分支,不影响功能。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP 超时常量(对齐 backend utils.agent_client._HTTP_BUFFER_S)
# ---------------------------------------------------------------------------

_HTTP_BUFFER_S = 15.0

# 默认 Hermes api_server 地址(可在构造时 override + env:HERMES_API_URL)
# 注意:hermes gateway 主端口默认 8642,不是 18100(18100 是早期 v0.10.0 假设)。
# install-hermes.sh 不写 HERMES_API_URL(env 由 hermes supervisor 管理),
# 这里固定默认 8642。
_DEFAULT_HERMES_URL = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642")


def _load_api_key() -> str:
    """拿 API_SERVER_KEY:优先级 env > ~/.hermes/.env > 空。

    backend supervisor 一般不会 source ~/.hermes/.env,所以从这里 fallback 读。
    install-hermes.sh Step 6 已保证 ~/.hermes/.env::API_SERVER_KEY 存在。
    """
    key = os.environ.get("API_SERVER_KEY", "").strip()
    if key:
        return key
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.is_file():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("API_SERVER_KEY=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


_DEFAULT_API_KEY = _load_api_key()

# 上下文溢出关键词(best-effort 识别)。
# v0.10.0 api_server 无标准化溢出错误码,需按文案匹配。
_OVERFLOW_MARKERS = (
    "context overflow",
    "context length",
    "context window",
    "maximum context",
    "token limit",
    "context budget",
    "prompt is too long",
    "too many tokens",
)


def _looks_like_overflow(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(m in lowered for m in _OVERFLOW_MARKERS)


def _new_run_id() -> str:
    """miloco 期望 webhook 返回 ``runId``(平台 turn id);/v1/chat/completions 不返回,
    本地生成 uuid 兜底。"""
    return str(uuid.uuid4())


def _resolve_trace_dir() -> Path:
    """``$MILOCO_HOME/trace/agent/`` —— plugin trace.py 写盘位置。

    默认 settings.directories.miloco_home 解析失败时回退 ``~/.openclaw/miloco``。
    """
    try:
        from .paths import miloco_home
        return miloco_home() / "trace" / "agent"
    except Exception:
        return Path("~/.openclaw/miloco/trace/agent").expanduser()


# ---------------------------------------------------------------------------
# Session mapping(内联实现,原独立 adapter 已删)
# ---------------------------------------------------------------------------


def _map_session(session_key: str, lane: str) -> str:
    """(sessionKey, lane) → 稳定 Hermes session_id。

    简化版(对齐 279): 用 miloco sessionKey 作前缀,带 lane,确保同 (sessionKey, lane)
    落到同一 hermes 会话,跨回合上下文连续。
    """
    return f"miloco:{session_key}:{lane}"


# ---------------------------------------------------------------------------
# Adapter 主类
# ---------------------------------------------------------------------------


class Adapter:
    """Hermes adapter —— 满足 backend ``AgentPlatformAdapter`` 接口契约(duck-typed)。

    暴露 ``send_turn`` / ``read_trace_meta`` / ``build_system`` / ``aclose`` / ``name``。
    loader 用 ``hasattr`` 检查,不要求继承 ``miloco.agent_platform.base.AgentPlatformAdapter``。
    """

    name = "hermes"

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        trace_dir: Optional[Path] = None,
    ) -> None:
        self._api_url = (api_url or _DEFAULT_HERMES_URL).rstrip("/")
        self._api_key = api_key or _DEFAULT_API_KEY
        self._trace_dir = trace_dir or _resolve_trace_dir()
        # 每 turn 新建 httpx.AsyncClient(避免跨回合连接复用导致 cancellation 串扰)
        self._client: Optional[httpx.AsyncClient] = None

    # ---- build_system --------------------------------------------------

    def build_system(self, profile: str, extra: dict[str, Any]) -> str:
        """组装 OpenAI ``<system>`` 消息文本。

        对齐 doc §五 #2: 硬约束 + 工具索引 + 感知格式 + 数据源 + (按 profile)档案/目录。

        实现要点: 从 plugin 侧 ``context_injection`` 模块复用 ``_build_prepend`` /
        ``_build_append``(都是 module-private,这里直接 import 或 inline)。
        """
        from .context_injection import (
            _build_prepend,
            _build_append,
        )

        prepend = _build_prepend(profile)
        append = _build_append(profile)
        sections = [prepend] if prepend else []
        if append:
            sections.append(append)
        return "\n\n---\n\n".join(sections)

    # ---- send_turn -----------------------------------------------------

    async def send_turn(self, ctx: Any) -> Any:
        """同步投递一条 turn,返回 AgentTurnResult-like 对象。

        ``ctx`` 是 :class:`miloco.agent_platform.base.TurnContext`(duck-typed,只取 attr):
        - text / session_key / lane / trace_id / wait_timeout_ms / profile / extra

        返回值暴露 ``run_id`` / ``status`` / ``rtt_ms`` / ``recovered`` / ``error``
        (对齐 backend AgentTurnResult)。
        """
        run_id = _new_run_id()
        text = getattr(ctx, "text", "") or ""
        session_key = getattr(ctx, "session_key", "main") or "main"
        lane = getattr(ctx, "lane", "default") or "default"
        trace_id = getattr(ctx, "trace_id", "") or ""
        wait_timeout_ms = int(getattr(ctx, "wait_timeout_ms", 180_000) or 180_000)
        profile = getattr(ctx, "profile", "full") or "full"
        extra = getattr(ctx, "extra", {}) or {}

        session_id = _map_session(session_key, lane)
        timeout_s = max(wait_timeout_ms / 1000.0, 1.0) + _HTTP_BUFFER_S

        # 组装 messages: <system>(可选) + <user>
        system_text = self.build_system(profile, extra) if profile != "minimal" else ""
        messages: list[dict[str, str]] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": text})

        body: dict[str, Any] = {"messages": messages}
        model = os.environ.get("HERMES_MODEL", "").strip()
        if model:
            body["model"] = model

        headers = self._headers(session_id)

        started_at = time.monotonic()
        try:
            client = httpx.AsyncClient(timeout=timeout_s)
        except Exception as exc:
            logger.error("[hermes adapter] AsyncClient 创建失败: %s", exc)
            return _err_result(run_id, f"client init failed: {exc}")

        try:
            try:
                logger.info(
                    "[hermes adapter] → chat session=%s url=%s/v1/chat/completions "
                    "timeout=%.1fs sys_len=%d user_len=%d",
                    session_id, self._api_url, timeout_s,
                    len(system_text), len(text),
                )
                resp = await client.post(
                    f"{self._api_url}/v1/chat/completions",
                    json=body, headers=headers, timeout=timeout_s,
                )
            except httpx.TimeoutException:
                logger.warning(
                    "[hermes adapter] ← Hermes TIMEOUT session=%s timeout=%.1fs",
                    session_id, timeout_s,
                )
                return _result(run_id=run_id, status="timeout")
            except httpx.HTTPError as exc:
                logger.warning(
                    "[hermes adapter] ← Hermes transport error session=%s: %s",
                    session_id, exc,
                )
                return _err_result(run_id, str(exc))

            rtt_ms = (time.monotonic() - started_at) * 1000

            # 2xx → 成功
            if 200 <= resp.status_code < 300:
                logger.info(
                    "[hermes adapter] ← Hermes HTTP %d session=%s OK rtt=%.0fms",
                    resp.status_code, session_id, rtt_ms,
                )
                return _result(run_id=run_id, status="ok", rtt_ms=rtt_ms)

            # 非 2xx: 尝试溢出识别 + 自愈
            err_text = _extract_error_text(resp)
            logger.warning(
                "[hermes adapter] ← Hermes HTTP %d session=%s err=%s",
                resp.status_code, session_id, err_text[:200],
            )
            if _looks_like_overflow(err_text):
                logger.warning(
                    "[hermes adapter] overflow self-heal: session=%s, retry stateless",
                    session_id,
                )
                return await self._heal_and_retry(
                    messages, timeout_s, run_id, err_text, started_at,
                )

            return _err_result(
                run_id,
                f"hermes chat HTTP {resp.status_code}: {err_text[:300]}",
                rtt_ms=rtt_ms,
            )
        finally:
            await client.aclose()

    async def _heal_and_retry(
        self,
        messages: list[dict[str, str]],
        timeout_s: float,
        run_id: str,
        overflow_reason: str,
        started_at: float,
    ) -> Any:
        """溢出自愈:无 session 头重试一次(对齐 279 best-effort)。"""
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(
                    f"{self._api_url}/v1/chat/completions",
                    json={"messages": messages},
                    headers=self._headers(None),
                    timeout=timeout_s,
                )
        except httpx.TimeoutException:
            return _result(
                run_id=run_id, status="timeout",
                recovered=False, error=overflow_reason,
            )
        except httpx.HTTPError as exc:
            return _err_result(
                run_id, str(exc), recovered=False, error=overflow_reason,
            )

        rtt_ms = (time.monotonic() - started_at) * 1000
        if 200 <= resp.status_code < 300:
            logger.info("[hermes adapter] overflow self-heal: recovered via stateless retry")
            return _result(
                run_id=run_id, status="ok", rtt_ms=rtt_ms, recovered=True,
            )

        retry_err = _extract_error_text(resp)
        if _looks_like_overflow(retry_err):
            logger.error(
                "[hermes adapter] overflow self-heal: still overflow after fresh retry; "
                "unrecoverable (likely system prompt exceeds budget)"
            )
            return _err_result(
                run_id, retry_err or overflow_reason,
                rtt_ms=rtt_ms, recovered=False,
            )
        return _err_result(
            run_id,
            f"hermes chat HTTP {resp.status_code} after fresh retry: {retry_err[:300]}",
            rtt_ms=rtt_ms, recovered=False,
        )

    # ---- read_trace_meta ----------------------------------------------

    async def read_trace_meta(self, run_id: str) -> Optional[Any]:
        """读 ``$MILOCO_HOME/trace/agent/<YYYYMMDD>/<runId>__<query>.meta.json`` → TraceMeta-like。

        trace.py 写带日期子目录+query后缀,此处 glob 搜索匹配。
        """
        candidates = sorted(
            self._trace_dir.glob(f"*{run_id}*.meta.json"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not candidates:
            return None
        meta_path = candidates[0]
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[hermes adapter] read_trace_meta 解析失败 run=%s path=%s err=%s",
                run_id, meta_path, exc,
            )
            return None
        # 简单包成 SimpleNamespace,够 backend poller 字段读取
        from types import SimpleNamespace
        return SimpleNamespace(
            run_id=data.get("run_id") or run_id,
            query=data.get("query", ""),
            duration_ms=float(data.get("duration_ms") or 0.0),
            llm_call_count=int(data.get("llm_call_count") or 0),
            tool_call_count=int(data.get("tool_call_count") or 0),
            llm_total_ms=float(data.get("llm_total_ms") or 0.0),
            tool_total_ms=float(data.get("tool_total_ms") or 0.0),
            tool_max_ms=float(data.get("tool_max_ms") or 0.0),
            slowest_tool_name=data.get("slowest_tool_name"),
            success=bool(data.get("success")),
            error_count=int(data.get("error_count") or 0),
            error_msg=data.get("error_msg"),
            jsonl_path=data.get("jsonl_path"),
        )

    # ---- helpers -------------------------------------------------------

    def _headers(self, session_id: Optional[str]) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        if session_id:
            h["X-Hermes-Session-Id"] = session_id
        return h

    async def aclose(self) -> None:
        """释放资源(目前每 turn 新建 client,无需主动 close;保留接口兼容)。"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


# ---------------------------------------------------------------------------
# 结果构造 helpers(不引 backend 依赖,用 SimpleNamespace 让 backend 字段读取工作)
# ---------------------------------------------------------------------------


def _result(
    run_id: str,
    status: str,
    rtt_ms: float = 0.0,
    recovered: Optional[bool] = None,
    error: Optional[str] = None,
) -> Any:
    from types import SimpleNamespace
    return SimpleNamespace(
        run_id=run_id, status=status, rtt_ms=rtt_ms,
        recovered=recovered, error=error,
    )


def _err_result(
    run_id: str,
    error: str,
    rtt_ms: float = 0.0,
    recovered: Optional[bool] = None,
) -> Any:
    return _result(
        run_id=run_id, status="error",
        rtt_ms=rtt_ms, recovered=recovered, error=error,
    )


def _extract_error_text(resp: httpx.Response) -> str:
    """从非 2xx 响应里提取人类可读错误文案(兼容 OpenAI-style envelope)。"""
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