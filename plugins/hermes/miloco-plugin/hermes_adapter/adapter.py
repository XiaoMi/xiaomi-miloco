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
import shutil
import subprocess
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


def _resolve_owner_session() -> tuple[Optional[str], Optional[str]]:
    """解析车主 IM 会话 ID 和投递平台名。

    从 Hermes channel_directory 的 ``platforms.{name}`` 下找第一个已绑定 IM 频道。
    channel 对象的 ``id`` 字段即为 session_id。返回 (session_id, platform)。
    均为 None 表示还没绑过 IM。
    """
    channel_file = Path.home() / ".hermes" / "channel_directory.json"
    try:
        data = json.loads(channel_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        return None, None
    for plat_name, channels in platforms.items():
        if isinstance(channels, list) and channels:
            for ch in channels:
                if isinstance(ch, dict) and ch.get("id"):
                    return str(ch["id"]), plat_name
    return None, None


# ---------------------------------------------------------------------------
# Adapter 主类
# ---------------------------------------------------------------------------


# hermes CLI 删除会话子进程。CLI 不在 PATH 或未知输出时保守返回 False。
def _delete_hermes_session(session_id: str, timeout: float = 15.0) -> bool:
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        return False
    try:
        proc = subprocess.run(
            [hermes_bin, "sessions", "delete", session_id, "--yes"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return False
    out = proc.stdout + proc.stderr
    if "Deleted session" in out or "not found" in out:
        return True
    return False


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
        self._client: Optional[httpx.AsyncClient] = None
        # run_id → send_turn 发送原文。trace.py 用 Hermes session_id 落盘，
        # adapter 用 uuid 当 run_id——两边文件名对不上。此处存文本，
        # read_trace_meta 按文本前缀反查 meta.json 实现关联。
        self._pending_texts: dict[str, str] = {}

    _PENDING_TEXTS_MAX = 200

    # ---- build_system --------------------------------------------------

    def build_system(self, profile: str, extra: dict[str, Any]) -> str:
        """组装 OpenAI ``<system>`` 消息文本。

        对齐 doc §五 #2: 硬约束 + 工具索引 + 感知格式 + 数据源 + (按 profile)档案/目录。

        实现要点: 从 plugin 侧 ``context_injection`` 模块复用 ``_build_prepend`` /
        ``_build_append``(都是 module-private,这里直接 import 或 inline)。
        """
        from .context_injection import (
            _build_append,
            _build_prepend,
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
        # 存储文本供 read_trace_meta 反查（trace.py 用 session_id 落盘，不认这里的 uuid）
        self._pending_texts[run_id] = text
        if len(self._pending_texts) > self._PENDING_TEXTS_MAX:
            self._pending_texts.pop(next(iter(self._pending_texts)), None)
        session_key = getattr(ctx, "session_key", "main") or "main"
        lane = getattr(ctx, "lane", "default") or "default"
        wait_timeout_ms = int(getattr(ctx, "wait_timeout_ms", 180_000) or 180_000)
        profile = getattr(ctx, "profile", "full") or "full"
        extra = getattr(ctx, "extra", {}) or {}
        delivery = extra.get("delivery") or {}

        # 处理投递意图（对齐底座 WebhookAdapter，见 base.py:179）
        # dispatcher 为 onboarding 等交互型事件塞了 {"resolve_target": "owner-channel", "deliver": True}
        # turn 用新会话让 LLM 干净评估，投递才用车主 IM 会话
        owner_platform = None
        owner_chat = None
        if delivery.get("resolve_target") == "owner-channel":
            owner_session, owner_platform = _resolve_owner_session()
            if not owner_session:
                return _result(run_id="", status="no-channel")
            # LLM turn 用新会话（不污染车主 IM 历史），投递才用 IM 会话
            session_id = None
            owner_chat = owner_session
        else:
            # suggestion 每个事件独立评估，不用持久 session；
            # 但需保留 miloco: 前缀让 trace hook 正常落盘。
            import uuid as _uuid

            session_id = _map_session(session_key, lane) if lane != "miloco-suggest" else (
                f"{_map_session(session_key, lane)}:{_uuid.uuid4().hex[:8]}"
            )
        timeout_s = max(wait_timeout_ms / 1000.0, 1.0) + _HTTP_BUFFER_S

        # 组装 messages: <system>(可选) + <user>
        # build_system 内部可能走 subprocess（catalog CLI），丢线程池避免阻塞事件循环
        if profile != "minimal":
            import asyncio as _asyncio
            loop = _asyncio.get_running_loop()
            system_text = await loop.run_in_executor(None, self.build_system, profile, extra)
        else:
            system_text = ""
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
                # 对 deliver=True 的 turn，Hermes /v1/chat/completions 不会
                # 自动推回 IM（纯拉取端点），需从响应体读回复经 hermes send 投递。
                # 带 chat_id（`platform:chat_id`）而非裸平台名，定位车主具体的绑定会话
                # 而非依赖 PLATFORM_HOME_CHANNEL 环境变量（多数平台没配）。
                delivery_target = (
                    f"{owner_platform}:{owner_chat}" if owner_chat
                    else owner_platform
                )
                if delivery.get("deliver") and delivery_target:
                    if not _deliver_response(resp, delivery_target):
                        return _err_result(
                            run_id,
                            "deliver failed: hermes send returned error",
                            rtt_ms=rtt_ms,
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
        """按发送原文反查 trace.py 落盘的 meta.json。

        adapter 用 uuid 当 run_id，trace.py 用 Hermes session_id 落盘文件名——
        两边对不上，无法按文件名匹配。此处用 _pending_texts 存的发送原文，
        按 ``query`` 字段前缀匹配最近落盘的 meta.json。
        """
        text = self._pending_texts.pop(run_id, None)
        if not text:
            return None
        candidates = sorted(
            self._trace_dir.rglob("*.meta.json"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        data: dict[str, Any] | None = None
        for meta_path in candidates[:50]:
            try:
                candidate = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("[hermes adapter] read_trace_meta skip %s: %s", meta_path.name, exc)
                continue
            query = (candidate.get("query") or "").strip()
            if query and text.strip().startswith(query):
                data = candidate
                break
        if not data:
            return None
        from types import SimpleNamespace
        return SimpleNamespace(
            run_id=run_id,
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

    # ---- max_send_turn_latency_s (AgentPlatformAdapter 契约) -----------

    def max_send_turn_latency_s(self) -> float:
        """返回单次 send_turn 最长耗时估计（含溢出自愈重试一次）。

        onboarding_trigger._delivery_guard_timeout_s 靠此方法估算守护 timeo。
        WebhookAdapter 已实现（考虑重试+退避），Hermes Adapter 补齐。
        """
        timeout_s = 180.0 + _HTTP_BUFFER_S
        return 2 * timeout_s

    # ---- reset_sessions (AgentPlatformAdapter 可选契约) -------------------

    async def reset_sessions(
        self, routes: list[tuple[str, str]], *, delete_transcript: bool = True, timeout: float = 10.0,
    ) -> dict[str, Any]:
        """切家庭时批量清理 Hermes 会话。按 (session_key, lane) 映射为 session_id 后逐个删。

        返回 ``{"reset": [...], "failed": [...]}``。suggest 车道每次 turn
        用一次性会话（带 uuid 后缀），无跨轮状态，跳过重置。
        """
        reset, failed, seen = [], [], set()
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        for session_key, lane in routes:
            if lane == "miloco-suggest":
                continue
            session_id = _map_session(session_key, lane)
            if session_id in seen:
                continue
            seen.add(session_id)
            ok = await loop.run_in_executor(None, _delete_hermes_session, session_id, timeout)
            (reset if ok else failed).append(session_id)
        return {"reset": reset, "failed": failed}

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


def _deliver_response(resp: httpx.Response, platform: str) -> bool:
    """从 Hermes chat completions 响应里提取回复，经 hermes send 投递到 IM。

    Hermes /v1/chat/completions 不会自动推 IM，需显式投递。
    返回 True 表示投递成功（或内容为空跳过），False 表示投递失败。
    """
    import shutil
    import subprocess
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        logger.warning("[hermes adapter] hermes CLI not found, cannot deliver")
        return False
    try:
        body = resp.json()
        content = None
        choices = body.get("choices") or []
        if choices and choices[0].get("message"):
            content = choices[0]["message"].get("content")
        if not content:
            return True  # 无内容不是故障
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("[hermes adapter] parse response for deliver failed: %s", exc)
        return False
    try:
        proc = subprocess.run(
            [hermes_bin, "send", "--to", platform, "--json", "-q", content],
            capture_output=True, text=True, timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, Exception) as exc:
        logger.warning("[hermes adapter] hermes send failed: %s", exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "[hermes adapter] hermes send error rc=%d: %s",
            proc.returncode, (proc.stderr or "")[:200],
        )
        return False
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False
    # rc=0 不等于送达：hermes send 可能 rc=0 但 payload 表示软失败。
    return bool(payload.get("success") is True or payload.get("ok") is True)


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