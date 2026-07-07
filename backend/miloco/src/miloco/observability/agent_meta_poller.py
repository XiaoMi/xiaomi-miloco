"""Agent turn meta 反向 poller。

backend 发完 run_agent_turn 拿到 runId 后,enqueue 给 poller;
poller 周期性读 trace meta,拿到 meta 后调
``MetricsClient.record_agent_run`` 写入 agent_runs 表。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from miloco.agent_platform import get_adapter
from miloco.config import get_settings
from miloco.observability.metrics_client import MetricsClient
from miloco.observability.types import AgentRunRecord

logger = logging.getLogger(__name__)

# 轮询参数:每 3s 一次,最长 90s 等 agent 完成
_POLL_INTERVAL_S = 3.0
_MAX_DEADLINE_S = 90.0
# 限并发 poll 数 — openclaw get_trace 是同步 in-memory Map lookup,无 I/O 压力,
# 这里限并发主要是控 task 暴涨。每个 in-flight job 内每 3s 一次 webhook:
# 8 并发 → 峰值 ≈ 2.67 req/s,openclaw 完全扛得住。
_MAX_CONCURRENT_POLLS = 8

AgentRunSource = Literal["rule", "interaction", "suggestion"]


@dataclass
class _Job:
    trace_id: str
    run_id: str
    source: str
    webhook_rtt_ms: float | None
    # deadline 故意不在 enqueue 时算 — 排队/sem 等待时间不计入 90s 预算,
    # _poll_one 真正开跑时才起算。否则 backlog 下后面 job 的预算被前面吃光,
    # 极端情况零 poll 就超时,meta 静默丢。


class AgentMetaPoller:
    def __init__(self, metrics_client: MetricsClient, queue_maxsize: int = 256) -> None:
        self._client = metrics_client
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # 每 job 起独立 task,Semaphore 限同时 poll 数;消除 head-of-line:
        # 一个卡 90s 的 turn 不再阻塞后续 turn 的 meta 采集。
        self._sem = asyncio.Semaphore(_MAX_CONCURRENT_POLLS)
        self._pending: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._stop.set()
        try:
            self._worker.cancel()
            await self._worker
        except (asyncio.CancelledError, Exception):
            pass
        self._worker = None
        # worker 的 finally 已经 cancel + gather pending,这里兜底再清一次,
        # 防 worker 被异常路径绕过 finally。
        if self._pending:
            for t in self._pending:
                t.cancel()
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()

    def enqueue(
        self,
        trace_id: str,
        run_id: str,
        source: str,
        webhook_rtt_ms: float | None,
    ) -> None:
        try:
            self._queue.put_nowait(_Job(
                trace_id=trace_id,
                run_id=run_id,
                source=source,
                webhook_rtt_ms=webhook_rtt_ms,
            ))
        except asyncio.QueueFull:
            logger.warning(
                "agent_meta_poller queue full; dropping trace_id=%s run_id=%s",
                trace_id, run_id,
            )

    async def _run_worker(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    job = await self._queue.get()
                except asyncio.CancelledError:
                    break
                task = asyncio.create_task(self._poll_one_task(job))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        finally:
            # shutdown:cancel 所有 in-flight poll,避免 stop_engine 后还在打 webhook。
            for t in self._pending:
                t.cancel()
            await asyncio.gather(*self._pending, return_exceptions=True)

    async def _poll_one_task(self, job: _Job) -> None:
        async with self._sem:
            try:
                await self._poll_one(job)
            except Exception:
                logger.exception(
                    "agent_meta_poller failed for trace_id=%s run_id=%s",
                    job.trace_id, job.run_id,
                )
            finally:
                self._queue.task_done()

    async def _poll_one(self, job: _Job) -> None:
        """用 Adapter.read_trace_meta 读 trace meta（adapter 内部处理 webhook fallback）。
        """
        deadline = time.monotonic() + _MAX_DEADLINE_S
        adapter = get_adapter()
        while time.monotonic() < deadline and not self._stop.is_set():
            status, meta = await self._poll_once(job, adapter)
            if status == "done":
                record = AgentRunRecord(
                    run_id=getattr(meta, "run_id", None) or job.run_id,
                    trace_id=job.trace_id,
                    timestamp=int(time.time() * 1000),
                    source=job.source,
                    webhook_rtt_ms=job.webhook_rtt_ms,
                    query=getattr(meta, "query", "") or "",
                    duration_ms=float(getattr(meta, "duration_ms", 0.0) or 0.0),
                    llm_call_count=int(getattr(meta, "llm_call_count", 0) or 0),
                    tool_call_count=int(getattr(meta, "tool_call_count", 0) or 0),
                    llm_total_ms=float(getattr(meta, "llm_total_ms", 0.0) or 0.0),
                    tool_total_ms=float(getattr(meta, "tool_total_ms", 0.0) or 0.0),
                    tool_max_ms=float(getattr(meta, "tool_max_ms", 0.0) or 0.0),
                    slowest_tool_name=getattr(meta, "slowest_tool_name", None),
                    success=bool(getattr(meta, "success", False)),
                    error_count=int(getattr(meta, "error_count", 0) or 0),
                    error_msg=getattr(meta, "error_msg", None),
                    jsonl_path=getattr(meta, "jsonl_path", None),
                )
                self._client.record_agent_run(record)
                return
            if status == "unknown":
                logger.info(
                    "trace returned unknown for run_id=%s; giving up", job.run_id,
                )
                return
            if status == "error":
                logger.warning(
                    "trace read error run_id=%s err=%s", job.run_id, meta,
                )
                await asyncio.sleep(_POLL_INTERVAL_S)
                continue
            # in_progress → 继续轮询
            await asyncio.sleep(_POLL_INTERVAL_S)

        logger.warning(
            "agent_meta_poller timed out: trace_id=%s run_id=%s",
            job.trace_id, job.run_id,
        )

    async def _poll_once(self, job: _Job, adapter: Any) -> tuple[str, Any]:
        """单次读 trace meta。返回 ``(status, meta_or_data)``。

        status 含义:
        - ``"done"``: meta 读到
        - ``"in_progress"``: meta 不存在(turn 还没 flush),等下次轮询
        - ``"unknown"``: meta 不存在但超时
        - ``"error"``: 出错
        """
        try:
            meta = await adapter.read_trace_meta(job.run_id)
            if meta is not None:
                return ("done", meta)
            return ("in_progress", None)
        except Exception as exc:
            logger.warning(
                "adapter.read_trace_meta failed run_id=%s err=%s",
                job.run_id, exc,
            )
            return ("error", str(exc))


_singleton: AgentMetaPoller | None = None


def set_agent_meta_poller(poller: AgentMetaPoller | None) -> None:
    global _singleton
    _singleton = poller


def get_agent_meta_poller() -> AgentMetaPoller | None:
    return _singleton


def track_agent_run(
    trace_id: str | None,
    run_id: str | None,
    source: AgentRunSource,
    webhook_rtt_ms: float | None = None,
) -> None:
    """所有 fire agent 的调用方拿到 (trace_id, run_id) 后必须调一次。

    perf.enabled=false 时整套 observability 在 startup 不启动,这里单点短路;
    避免每个 5 处调用点都包 if perf.enabled。

    入参任一为空(transport 失败 / run_id 缺失)时静默跳过——agent_meta_poller
    需要 run_id 调 openclaw get_trace,缺失时无法工作;trace_id 缺失时无 cycle 关联。
    两者都齐全才 enqueue 给 poller 异步取 meta 写入 agent_runs 表。
    """
    if not get_settings().perf.enabled:
        return
    if not trace_id or not run_id:
        return
    poller = get_agent_meta_poller()
    if poller is None:
        return
    poller.enqueue(trace_id, run_id, source, webhook_rtt_ms)
