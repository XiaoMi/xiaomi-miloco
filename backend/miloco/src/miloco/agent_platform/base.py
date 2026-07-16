# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""AgentPlatformAdapter 抽象基类与上下文数据类。

设计原则(``hermes-pr.md`` §五 #1+#2):
- Backend 不感知 Agent 平台(Hermes / OpenClaw / 其他),只持一个 Adapter 实例。
- Adapter 负责:组装 system prompt(从插件的 ``build_system(profile)`` 拿静态 +
  动态内容)、发起 HTTP 请求、解析响应、错误恢复(溢出自愈等)。
- 通用 Backend 侧调度: ``AgentDispatcher`` 按 session_key 调
  ``adapter.send_turn(TurnContext)``;异常分类与重试仍由 dispatcher 控制。

#11 trace 读盘约定:
- Adapter 提供 :meth:`read_trace_meta` 方法,backend 的 ``agent_meta_poller`` 轮询
  ``get_trace`` 时由 Adapter 实现读 ``MILOCO_HOME/trace/*.meta.json`` 返回。
- 这是文件 IPC 通道(plugin 写、Adapter 读),避免跨进程 webhook get_trace。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Context 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnContext:
    """Adapter.send_turn 的入参——Backend 给 Adapter 喂的事件快照。

    字段对齐 doc §五 #1+#2 + backend 现有 ``run_agent_turn``:
    - ``text``: 合并后的单条消息文本(perception 提示 / rule callback / 用户 IM)
    - ``session_key``: 全局 session id(``agent:main:miloco`` 等,dispatcher 生成)
    - ``lane``: 同 session 下的并行车道(``miloco-interactive`` / ``miloco-rule`` /
      ``miloco-suggest``)
    - ``trace_id``: backend 生成的本批次幂等键
    - ``wait_timeout_ms``: backend 期望平台同步等 turn 结束的最长等待时间
    - ``profile``: 注入分级,Adapter 决定 ``build_system`` 下哪些 block
    - ``extra``: Adapter 自定义的额外字段(可携带感知数据 / home-profile 等)
    """

    text: str
    session_key: str
    lane: str
    trace_id: str
    wait_timeout_ms: int
    profile: str = "full"  # "full" | "suggestion" | "rule" | "minimal"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentTurnResult:
    """Adapter.send_turn 的出参——对齐 backend 现有 ``run_agent_turn`` 返回值。"""

    run_id: Optional[str]
    status: str  # "ok" | "error" | "timeout"
    rtt_ms: float = 0.0
    # 溢出自愈观测(对齐 backend 现有 recovered/error 字段):
    recovered: Optional[bool] = None
    error: Optional[str] = None


@dataclass
class TraceMeta:
    """Adapter.read_trace_meta 的返回结构(对应 agent_runs 一行)。"""

    run_id: str
    query: str
    duration_ms: float
    llm_call_count: int
    tool_call_count: int
    llm_total_ms: float
    tool_total_ms: float
    tool_max_ms: float
    slowest_tool_name: Optional[str]
    success: bool
    error_count: int
    error_msg: Optional[str]
    jsonl_path: Optional[str]


# System prompt 构造器签名(供 plugin Adapter 实现使用):
# Adapter.send_turn 内部调用 ``builder(profile, extra)`` 拿到要放进 <system> 的文本。
SystemPromptBuilder = Callable[[str, dict[str, Any]], str]


# ---------------------------------------------------------------------------
# 异常类(对齐 backend AgentWebhookException)
# ---------------------------------------------------------------------------


class AdapterTransportError(Exception):
    """Adapter 传输失败(连接 / 5xx / HTTP 超时)。dispatcher 捕获后有限重试。"""


class AdapterTransientError(Exception):
    """Adapter 临时性错误(平台返回 status="timeout" 等),不重试,跳过该批。"""


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class AgentPlatformAdapter(ABC):
    """Backend 侧 Agent 平台适配器抽象基类。

    子类由 plugin 提供(Hermes 实现等),通过 :func:`miloco.agent_platform.loader.load_adapter`
    从 ``MILOCO_HOME/agent_platform/<name>/`` 动态加载。
    """

    #: 平台名(子类必设),例如 ``"hermes"`` / ``"openclaw"``。
    name: str = ""

    @abstractmethod
    async def send_turn(self, ctx: TurnContext) -> AgentTurnResult:
        """同步投递一条消息并等待该 turn 结束(或超时)。

        - 传输级异常(连接 / 5xx / HTTP 超时):抛 :class:`AdapterTransportError`
        - 平台侧 turn 跑超时:返回 ``status="timeout"``(不抛,允许后续 turn 继续)
        - 平台侧逻辑错误:返回 ``status="error"`` + ``error`` 文案
        - 成功:返回 ``status="ok"`` + ``run_id``
        """

    @abstractmethod
    async def read_trace_meta(self, run_id: str) -> Optional[TraceMeta]:
        """读 ``MILOCO_HOME/trace/<run_id>.meta.json``,返回 :class:`TraceMeta` 或 None。

        Backend ``agent_meta_poller`` 轮询 ``get_trace`` 时由本方法读盘返回。
        Plugin 端 trace.py 负责常写(meta.json),Adapter 不感知写入,只读取。
        """

    async def aclose(self) -> None:
        """Adapter 关闭钩子(释放 httpx client 等)。dispatcher.stop 时调。子类按需 override。"""
        return None


# ---------------------------------------------------------------------------
# WebhookAdapter — 旧 webhook 路径包装为 Adapter 接口
# ---------------------------------------------------------------------------


class WebhookAdapter(AgentPlatformAdapter):
    """内置兜底 Adapter：把旧 webhook 路径包装成与 HermesAdapter 平级的接口。

    ``get_adapter()`` 永远返回非 None — 要么是 plugin 提供的平台 Adapter，
    要么是这个内置的 WebhookAdapter。调用方不再需要 if/else 分支。
    """

    name = "webhook"

    _TRANSPORT_RETRIES = 2
    _TRANSPORT_BACKOFF_S = 0.5

    async def send_turn(self, ctx: TurnContext) -> AgentTurnResult:
        import asyncio

        from miloco.middleware.exceptions import AgentWebhookException
        from miloco.utils.agent_client import run_agent_turn

        delivery = ctx.extra.get("delivery") or {}
        for attempt in range(self._TRANSPORT_RETRIES + 1):
            try:
                run_id, status, rtt_ms = await run_agent_turn(
                    ctx.text,
                    session_key=ctx.session_key,
                    lane=ctx.lane,
                    trace_id=ctx.trace_id,
                    wait_timeout_ms=ctx.wait_timeout_ms,
                    **delivery,
                )
                return AgentTurnResult(run_id=run_id, status=status, rtt_ms=rtt_ms)
            except AgentWebhookException:
                if attempt == self._TRANSPORT_RETRIES:
                    raise AdapterTransportError(
                        f"agent turn transport exhausted after {attempt + 1} attempts "
                        f"session={ctx.session_key}"
                    )
                await asyncio.sleep(self._TRANSPORT_BACKOFF_S * (2 ** attempt))

    async def read_trace_meta(self, run_id: str) -> Optional[TraceMeta]:
        from miloco.middleware.exceptions import AgentWebhookException
        from miloco.utils.agent_client import call_agent_webhook

        try:
            data = await call_agent_webhook(
                "get_trace", {"runId": run_id}, timeout=5.0,
            )
        except Exception:
            return None
        status = (data or {}).get("status") if isinstance(data, dict) else None
        if status != "done":
            return None
        _C2S = {
            "runId": "run_id", "query": "query",
            "durationMs": "duration_ms", "llmCallCount": "llm_call_count",
            "toolCallCount": "tool_call_count",
            "llmTotalMs": "llm_total_ms", "toolTotalMs": "tool_total_ms",
            "toolMaxMs": "tool_max_ms", "slowestToolName": "slowest_tool_name",
            "errorCount": "error_count", "errorMsg": "error_msg",
            "jsonlPath": "jsonl_path",
        }
        translated = {_C2S.get(k, k): v for k, v in (data or {}).items() if isinstance(data, dict)}
        translated.setdefault("success", translated.get("success", True))
        return TraceMeta(
            run_id=translated.get("run_id", run_id),
            query=translated.get("query", ""),
            duration_ms=float(translated.get("duration_ms", 0.0) or 0.0),
            llm_call_count=int(translated.get("llm_call_count", 0) or 0),
            tool_call_count=int(translated.get("tool_call_count", 0) or 0),
            llm_total_ms=float(translated.get("llm_total_ms", 0.0) or 0.0),
            tool_total_ms=float(translated.get("tool_total_ms", 0.0) or 0.0),
            tool_max_ms=float(translated.get("tool_max_ms", 0.0) or 0.0),
            slowest_tool_name=translated.get("slowest_tool_name"),
            success=bool(translated.get("success", True)),
            error_count=int(translated.get("error_count", 0) or 0),
            error_msg=translated.get("error_msg"),
            jsonl_path=translated.get("jsonl_path"),
                )


