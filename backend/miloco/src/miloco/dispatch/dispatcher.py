# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Backend 侧 agent turn 调度器（按会话单飞 + 同类批量合并）。

收口所有 producer（perception / rule / bind）→ agent 的投递：producer 仅投递
「结构化条目列表 + 该类型 builder 引用」，dispatcher 把 items 当 ``list[Any]``
透明存储，按会话维护队列、同类合并、单飞投递，平台侧同一会话在途 turn 恒 ≤1。

调度全部前置在 ``run_agent_turn``（openclaw ``agent`` webhook）之前完成——平台一旦
入队不可取消 / 改序，故合并 / 淘汰 / 排序必须在此层做。合并 / 丢弃 / 超时 / 过期四类
「静默动作」均带 WARN 日志兜底。

过期（TTL）：每条事件记入队时刻 ``enqueued_at``，入队龄超过
``dispatcher.message_ttl_sec`` 即为过期。过期清理在两处发生——「新消息入队」时
（先腾出过期占用的空间，再判超长淘汰）与「打包发送前」（``_take_batch`` 取批前）。
收发在同一台机器实时进行，故仅 backend 发送前统一判过期，插件侧不重复校验。
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

# 去重后的 miloco session 全集（保持插入序），供切换家庭时批量 reset 用——
# 以 _ROUTE 为唯一事实源，避免在别处手抄 sessionKey 造成漂移。
MILOCO_SESSION_KEYS: list[str] = list(
    dict.fromkeys(session_key for session_key, _lane, _prio in _ROUTE.values())
)

# 仅这三类（== AgentRunSource）写 agent_runs；bind / onboarding 不统计。
_TRACKED: frozenset[EventType] = frozenset({"interaction", "rule", "suggestion"})

# 类型级投递参数（run_agent_turn 额外 kwargs）。onboarding 是交互式访谈，必须整个
# turn 直接跑在车主 IM 会话里且回复用户可见（deliver=True）——只把开场白 push 过去
# 而 turn 留在后台会话会割裂上下文（用户的 IM 回复落在 channel 会话，访谈状态却在
# 别处）。注意：队列仍按 _ROUTE 的 sessionKey 归并/单飞，实际 turn 会话由插件侧按
# resolveTarget 解析（owner-channel = 配置的 notifySessionKey，否则最近活跃的已绑定
# channel 会话）。其余类型不在表内 → 空 dict → 行为完全不变（后台 turn）。
_DELIVERY: dict[EventType, dict[str, Any]] = {
    "onboarding": {"resolve_target": "owner-channel", "deliver": True},
}


@dataclass(eq=False)
class _QueuedEvent:
    """队列内单条待投递事件。eq=False → in/remove 走身份比较，避免同值条目误删。"""

    event_type: EventType
    items: list[Any]  # 结构化条目（list[Speech] / list[Suggestion] / [RuleTriggerCallback] / [str]）
    builder: Builder  # 该类型格式化函数引用；同一类型恒为同一 builder
    priority: int  # 类型级优先级（来自 _ROUTE，数字小=优先）
    enqueued_at: float  # time.monotonic()，即消息创建时刻；用于同类合并排序 + 淘汰判旧 + 过期判龄
    intra_priority: int = 0  # 条目级优先级（数字小=优先）；无内层优先级的类型恒 0，仅参与淘汰、不改渲染序
    # 可选投递结果 future：需要区分「入队被接纳」与「真正送达平台」的 producer
    # （如 onboarding 终身一次性标记）传入；dispatcher 保证它在**每一条**丢弃/送达
    # 路径上都被 resolve，绝不悬空。True = turn 送达平台（成功或平台侧仍在途的
    # timeout）；False = 任何一种丢弃（淘汰 / closed / builder 失败 / 传输重试耗尽 /
    # turn 返回 error / stop 清队）。
    delivered: "asyncio.Future[bool] | None" = None


def _resolve_delivered(fut: "asyncio.Future[bool] | None", ok: bool) -> None:
    """resolve 投递结果 future；未传或已 done（如调用方守护超时后取消）则跳过。"""
    if fut is not None and not fut.done():
        fut.set_result(ok)


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
        # 队列里没排上的事件随停机丢弃：逐一 resolve False，投递 future 不悬空
        # （在途批次由 _send_batch 的 finally 兜底，含被 cancel 的场景）。
        for q in self._queues.values():
            for ev in q:
                _resolve_delivered(ev.delivered, False)
        self._queues.clear()

    async def dispatch(
        self,
        event_type: EventType,
        items: list[Any],
        builder: Builder,
        intra_priority: int = 0,
        delivered: "asyncio.Future[bool] | None" = None,
    ) -> bool:
        """入队一条事件并触发单飞 drainer。返回该事件是否被接纳（未被超长淘汰）。

        intra_priority：同类型内的条目级优先级（数字小=优先），由 producer 按业务语义
        计算（如 suggestion 由 urgency 映射）；无内层优先级的类型用缺省 0。仅参与超长淘汰，
        不影响 drain / 渲染顺序（渲染恒按时序）。

        delivered：可选投递结果 future（见 _QueuedEvent.delivered）。「接纳」只代表
        入队成功，真正送达与否由 drainer 异步 resolve 该 future——需要区分二者的
        producer 据此等待，不要把本方法返回值当"已送达"。
        """
        if self._closed:
            logger.warning("dispatcher closed; dropping %s event", event_type)
            _resolve_delivered(delivered, False)
            return False
        route = _ROUTE.get(event_type)
        if route is None:
            logger.error("unknown event_type=%s; dropping", event_type)
            _resolve_delivered(delivered, False)
            return False
        session_key, _lane, priority = route
        ev = _QueuedEvent(
            event_type=event_type,
            items=list(items),
            builder=builder,
            priority=priority,
            enqueued_at=time.monotonic(),
            intra_priority=intra_priority,
            delivered=delivered,
        )
        q = self._queues.setdefault(session_key, [])
        q.append(ev)
        # 新消息入队即先清理过期条目腾空间，再判超长淘汰——这样过期者优先让位，
        # 避免用「淘汰有效消息」去容纳一条本该被过期清掉的僵尸。
        self._drop_expired(session_key)
        self._enforce_cap(session_key)
        accepted = any(e is ev for e in self._queues.get(session_key, ()))
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
            _resolve_delivered(victim.delivered, False)  # 被淘汰即未送达
            evicted += 1
        if evicted:
            logger.warning(
                "dispatcher queue over cap session=%s evicted=%d (max=%d)",
                session_key,
                evicted,
                cap,
            )

    def _drop_expired(self, session_key: str) -> None:
        """清理入队龄超过 ``message_ttl_sec`` 的过期事件（被淘汰即未送达）。

        入队龄 = ``time.monotonic() - enqueued_at``。TTL<=0 视作关闭过期，直接返回。
        过期属「静默丢弃」→ 逐条 resolve delivered=False + WARN 兜底。
        """
        ttl_sec = get_settings().dispatcher.message_ttl_sec
        if ttl_sec <= 0:
            return
        q = self._queues.get(session_key)
        if not q:
            return
        now = time.monotonic()
        fresh: list[_QueuedEvent] = []
        expired: list[_QueuedEvent] = []
        for ev in q:
            (expired if now - ev.enqueued_at > ttl_sec else fresh).append(ev)
        if not expired:
            return
        self._queues[session_key] = fresh
        for ev in expired:
            _resolve_delivered(ev.delivered, False)
        # [DROPPED] 标记便于在 miloco-backend.log 里 grep；附最旧条目的延迟时长。
        max_age = max(now - ev.enqueued_at for ev in expired)
        logger.warning(
            "[DROPPED] dispatcher expired %d event(s) session=%s ttl_sec=%.1f max_delay_sec=%.1f",
            len(expired),
            session_key,
            ttl_sec,
            max_age,
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
        取批前先清理过期条目：僵尸消息绝不打包进本轮 turn。
        """
        self._drop_expired(session_key)
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
        # delivered 语义（finally 统一 resolve，任何提前 return / cancel 都不悬空）：
        # True = turn 送达平台（status ok，或 timeout——平台侧 turn 仍在途，视作已送达）；
        # False = builder 失败 / 无内容 / 传输重试耗尽 / turn 返回 error / 被 cancel。
        delivered = False
        try:
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
            delivery = _DELIVERY.get(event_type, {})
            for attempt in range(self._TRANSPORT_RETRIES + 1):
                try:
                    run_id, status, rtt_ms = await run_agent_turn(
                        msg,
                        session_key=session_key,
                        lane=lane,
                        trace_id=trace_id,
                        wait_timeout_ms=wait_ms,
                        **delivery,
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

            if status == "no-channel":
                # 插件侧结构化失败：车主从未私聊过 bot，解析不到 IM 会话。这不是
                # 传输故障（HTTP 200 / code 0 正常返回，不该烧重试），delivered=False
                # 让 producer（onboarding 一次性标记）不置位——等车主绑定 channel 后
                # 下次启动自然送达。
                logger.warning(
                    "agent turn undeliverable session=%s type=%s: no owner IM channel yet",
                    session_key,
                    event_type,
                )
                return

            if status == "timeout":
                # 超时仅放行后续 turn、不终止平台在途 turn → WARN、跳过本批、继续下一批。
                # 平台侧 turn 已在跑 → 对投递 future 而言算"已送达"。
                delivered = True
                logger.warning(
                    "agent turn timed out session=%s type=%s wait_ms=%d; skip, continue",
                    session_key,
                    event_type,
                    wait_ms,
                )
                return

            delivered = status == "ok"  # "error"：turn 执行失败，不算送达
            if run_id and event_type in _TRACKED:
                track_agent_run(
                    trace_id, run_id, cast(AgentRunSource, event_type), rtt_ms
                )
        finally:
            for ev in batch:
                _resolve_delivered(ev.delivered, delivered)


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
    delivered: "asyncio.Future[bool] | None" = None,
) -> bool:
    """模块级投递入口：转调单例 dispatcher。dispatcher 未就绪时丢弃并 WARN。

    delivered：可选投递结果 future（语义见 :class:`_QueuedEvent`）；本入口保证
    它在每条丢弃路径（含 dispatcher 未就绪）上也会被 resolve。
    """
    dispatcher = get_agent_dispatcher()
    if dispatcher is None:
        logger.warning("no dispatcher set; dropping %s event", event_type)
        _resolve_delivered(delivered, False)
        return False
    return await dispatcher.dispatch(event_type, items, builder, intra_priority, delivered)


def join_text_blocks(blocks: list[str]) -> str | None:
    """通用 builder：纯文本块以空行拼接。单块即原文，空则返回 None（drainer 跳过）。"""
    blocks = [b for b in blocks if b]
    if not blocks:
        return None
    return "\n\n".join(blocks)
