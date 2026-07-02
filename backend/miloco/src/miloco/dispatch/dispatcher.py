# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Backend 侧 agent turn 调度器（按会话单飞 + 同类批量合并）。

收口所有 producer（perception / rule / bind）→ agent 的投递：producer 仅投递
「结构化条目列表 + 该类型 builder 引用」，dispatcher 把 items 当 ``list[Any]``
透明存储，按会话维护队列、同类合并、单飞投递，平台侧同一会话在途 turn 恒 ≤1。

调度全部前置在 ``run_agent_turn``（openclaw ``agent`` webhook）之前完成——平台一旦
入队不可取消 / 改序，故合并 / 淘汰 / 排序必须在此层做。合并 / 丢弃 / 超时三类
「静默动作」均带 WARN 日志兜底。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from miloco.config import get_settings
from miloco.middleware.exceptions import AgentWebhookException
from miloco.observability.agent_meta_poller import AgentRunSource, track_agent_run
from miloco.utils.agent_client import run_agent_turn

logger = logging.getLogger(__name__)

EventType = Literal["interaction", "bind", "rule", "suggestion", "onboarding"]

# builder：把「合并后的同类条目列表」重构成一条 message（单一头、统一编号）。
# 返回 None/空 → drainer 跳过该批。dispatcher 不感知 items 的具体业务类型。
Builder = Callable[[list[Any]], "str | None"]

# 类型 → (sessionKey, lane, priority)。数字小 = 优先。
# bind / onboarding 与 interaction 共享会话/车道，但属不同合并类型，各自单飞、不混入同一 turn。
_ROUTE: dict[EventType, tuple[str, str, int]] = {
    "interaction": ("agent:main:miloco", "miloco-interactive", 0),
    "rule": ("agent:main:miloco-rule", "miloco-rule", 10),
    "suggestion": ("agent:main:miloco-suggest", "miloco-suggest", 20),
    "bind": ("agent:main:miloco", "miloco-interactive", 30),
    "onboarding": ("agent:main:miloco", "miloco-interactive", 30),
}

# 仅这三类（== AgentRunSource）写 agent_runs；bind / onboarding 不统计。
_TRACKED: frozenset[EventType] = frozenset({"interaction", "rule", "suggestion"})


@dataclass(eq=False)
class _QueuedEvent:
    """队列内单条待投递事件。eq=False → in/remove 走身份比较，避免同值条目误删。"""

    event_type: EventType
    items: list[Any]  # 结构化条目（list[Speech] / list[Suggestion] / [RuleTriggerCallback] / [str]）
    builder: Builder  # 该类型格式化函数引用；同一类型恒为同一 builder
    priority: int  # 类型级优先级（来自 _ROUTE，数字小=优先）
    enqueued_at: float  # time.monotonic()，用于同类合并排序 + 淘汰判旧
    intra_priority: int = 0  # 条目级优先级（数字小=优先）；无内层优先级的类型恒 0，仅参与淘汰、不改渲染序


class AgentDispatcher:
    """按 sessionKey 维护队列与单飞 drainer。"""

    # 传输级重试:仅对 AgentWebhookException(连接 / 5xx / HTTP 超时)做有限短退避重试,
    # 覆盖 webhook 瞬时不可达;exhausted 后 WARN 跳过该批。status=="timeout" 不在此列
    # (turn 已在平台侧运行,重试会重复触发)。
    _TRANSPORT_RETRIES = 2
    _TRANSPORT_BACKOFF_S = 0.5

    def __init__(self) -> None:
        self._queues: dict[str, list[_QueuedEvent]] = {}
        self._draining: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def start(self) -> None:
        self._closed = False

    async def stop(self) -> None:
        """置位 _closed、cancel 在途 drainer 并 gather（参照 poller 优雅停机）。"""
        self._closed = True
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._draining.clear()

    async def dispatch(
        self,
        event_type: EventType,
        items: list[Any],
        builder: Builder,
        intra_priority: int = 0,
    ) -> bool:
        """入队一条事件并触发单飞 drainer。返回该事件是否被接纳（未被超长淘汰）。

        intra_priority：同类型内的条目级优先级（数字小=优先），由 producer 按业务语义
        计算（如 suggestion 由 urgency 映射）；无内层优先级的类型用缺省 0。仅参与超长淘汰，
        不影响 drain / 渲染顺序（渲染恒按时序）。
        """
        if self._closed:
            logger.warning("dispatcher closed; dropping %s event", event_type)
            return False
        route = _ROUTE.get(event_type)
        if route is None:
            logger.error("unknown event_type=%s; dropping", event_type)
            return False
        session_key, _lane, priority = route
        ev = _QueuedEvent(
            event_type=event_type,
            items=list(items),
            builder=builder,
            priority=priority,
            enqueued_at=time.monotonic(),
            intra_priority=intra_priority,
        )
        q = self._queues.setdefault(session_key, [])
        q.append(ev)
        self._enforce_cap(session_key)
        accepted = any(e is ev for e in q)
        self._kick(session_key)
        return accepted

    def _enforce_cap(self, session_key: str) -> None:
        """超长淘汰，双层优先级 + 时间兜底（均「数字大 = 先淘汰」）：

        先淘汰类型最不紧急者（priority 数字最大），同类型再淘汰条目级最不紧急者
        （intra_priority 数字最大），仍并列则淘汰最旧（enqueued_at 最小）。
        """
        q = self._queues[session_key]
        cap = get_settings().dispatcher.max_queue
        evicted = 0
        while len(q) > cap:
            victim = max(q, key=lambda e: (e.priority, e.intra_priority, -e.enqueued_at))
            q.remove(victim)
            evicted += 1
        if evicted:
            logger.warning(
                "dispatcher queue over cap session=%s evicted=%d (max=%d)",
                session_key,
                evicted,
                cap,
            )

    def _kick(self, session_key: str) -> None:
        if self._closed or session_key in self._draining:
            return
        self._draining.add(session_key)
        task = asyncio.create_task(self._drain(session_key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, session_key: str) -> None:
        try:
            while True:
                batch = self._take_batch(session_key)
                if not batch:
                    break
                await self._send_batch(session_key, batch)
        finally:
            self._draining.discard(session_key)
            # 复位与最后取批之间可能有新事件到达 → 二次 kick 消除竞态。
            if self._queues.get(session_key) and not self._closed:
                self._kick(session_key)

    def _take_batch(self, session_key: str) -> list[_QueuedEvent]:
        """取会话内优先级最高的类型的全部事件，按入队时间升序，移出队列。

        每轮 turn 恒单一类型（同一 builder），保证「单头、统一编号」。
        """
        q = self._queues.get(session_key)
        if not q:
            return []
        etype = min(q, key=lambda e: e.priority).event_type
        batch = sorted(
            (e for e in q if e.event_type == etype), key=lambda e: e.enqueued_at
        )
        self._queues[session_key] = [e for e in q if e.event_type != etype]
        return batch

    async def _send_batch(self, session_key: str, batch: list[_QueuedEvent]) -> None:
        event_type = batch[0].event_type
        lane = _ROUTE[event_type][1]
        merged: list[Any] = [it for ev in batch for it in ev.items]
        try:
            msg = batch[0].builder(merged)
        except Exception:
            logger.exception("builder failed for %s batch; skipping", event_type)
            return
        if not msg:
            # builder 返回 None/空 → 无内容可发，跳过该批。
            return

        # drainer 不在感知 cycle 上下文，为每批次生成独立 trace_id（批次:run 1:1）。
        trace_id = str(uuid.uuid4())
        wait_ms = get_settings().dispatcher.turn_wait_timeout_ms
        run_id: str | None = None
        status: str = "error"
        rtt_ms: float = 0.0
        for attempt in range(self._TRANSPORT_RETRIES + 1):
            try:
                run_id, status, rtt_ms = await run_agent_turn(
                    msg,
                    session_key=session_key,
                    lane=lane,
                    trace_id=trace_id,
                    wait_timeout_ms=wait_ms,
                )
                break
            except AgentWebhookException as e:
                # 传输失败（连接 / 5xx / HTTP 超时）→ 有限短退避重试,
                # exhausted 后 WARN、跳过该批，继续下一批。
                if attempt == self._TRANSPORT_RETRIES:
                    logger.warning(
                        "agent turn transport failed session=%s type=%s err=%s; "
                        "skipping batch after %d attempts",
                        session_key,
                        event_type,
                        e,
                        attempt + 1,
                    )
                    return
                await asyncio.sleep(self._TRANSPORT_BACKOFF_S * (2**attempt))

        if status == "timeout":
            # 超时仅放行后续 turn、不终止平台在途 turn → WARN、跳过本批、继续下一批。
            logger.warning(
                "agent turn timed out session=%s type=%s wait_ms=%d; skip, continue",
                session_key,
                event_type,
                wait_ms,
            )
            return

        if run_id and event_type in _TRACKED:
            track_agent_run(
                trace_id, run_id, cast(AgentRunSource, event_type), rtt_ms
            )


_singleton: AgentDispatcher | None = None


def set_agent_dispatcher(dispatcher: AgentDispatcher | None) -> None:
    global _singleton
    _singleton = dispatcher


def get_agent_dispatcher() -> AgentDispatcher | None:
    return _singleton


async def dispatch_event(
    event_type: EventType,
    items: list[Any],
    builder: Builder,
    intra_priority: int = 0,
) -> bool:
    """模块级投递入口：转调单例 dispatcher。dispatcher 未就绪时丢弃并 WARN。"""
    dispatcher = get_agent_dispatcher()
    if dispatcher is None:
        logger.warning("no dispatcher set; dropping %s event", event_type)
        return False
    return await dispatcher.dispatch(event_type, items, builder, intra_priority)


def join_text_blocks(blocks: list[str]) -> str | None:
    """通用 builder：纯文本块以空行拼接。单块即原文，空则返回 None（drainer 跳过）。"""
    blocks = [b for b in blocks if b]
    if not blocks:
        return None
    return "\n\n".join(blocks)
