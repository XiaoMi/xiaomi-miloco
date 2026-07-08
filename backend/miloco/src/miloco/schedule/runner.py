# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""ScheduleRunner — APScheduler (MemoryJobStore) + _fire + rebuild + listeners.

关键设计 (见 _local/schedule-migration-plan.md § 5.1):
- MemoryJobStore: cron 表是唯一持久化源, in-memory 每次进程启动 rebuild
- max_instances=1 + coalesce=True: 同 job 重叠触发跳过, 停机跨越期不追溯补跑
- message 前缀 [cron:{name}] 恒定加, 让 openclaw resolveProfile 走 minimal
- sessionKey miloco-schedule:{cron_id} 避让 miloco 老通路
- at 成功走 mark_fired_and_delete 单事务, 无中间态窄窗口
- status=error / transport 失败挂 :retry DateTrigger, 60s 后重试直到超 max_delay
- termination-at (max_delay_seconds=0) → misfire_grace_time=None, APScheduler 无限补跑
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from miloco.middleware.exceptions import AgentWebhookException
from miloco.schedule.repo import CronRepo
from miloco.schedule.schema import Cron
from miloco.utils.agent_client import run_agent_turn
from miloco.utils.time_utils import now_ms

logger = logging.getLogger(__name__)

_RETRY_DELAY_MS = 60 * 1000
_LANE = "schedule"


def _default_max_delay(kind: str) -> int:
    """cron/every 兜底 600s, at 兜底 300s (termination-at 显式传 0 才无限)."""
    if kind == "at":
        return 300
    return 600


def _resolve_max_delay(cron: Cron) -> int | None:
    """三态: NULL → default; 0 → None (无限补跑); 正整数 → 该秒数."""
    if cron.max_delay_seconds is None:
        return _default_max_delay(cron.kind or "cron")
    if cron.max_delay_seconds == 0:
        return None
    return cron.max_delay_seconds


def _build_trigger(cron: Cron) -> BaseTrigger:
    if cron.kind == "cron":
        return CronTrigger.from_crontab(cron.cron_expr, timezone=cron.tz)
    if cron.kind == "at":
        return DateTrigger(
            run_date=datetime.fromtimestamp(cron.at_ms / 1000, tz=timezone.utc)
        )
    if cron.kind == "every":
        start = (
            datetime.fromtimestamp(cron.anchor_ms / 1000, tz=timezone.utc)
            if cron.anchor_ms
            else None
        )
        return IntervalTrigger(seconds=cron.every_ms / 1000, start_date=start)
    raise ValueError(f"unknown cron kind: {cron.kind}")


class ScheduleRunner:
    """APScheduler + _fire 编排. 单例, 由 main.py 生命周期管理."""

    def __init__(self):
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            job_defaults={"max_instances": 1, "coalesce": True},
        )
        self._cron_repo = CronRepo()
        self._scheduler.add_listener(
            self._on_missed, EVENT_JOB_MISSED
        )
        self._scheduler.add_listener(self._on_error, EVENT_JOB_ERROR)
        self._scheduler.add_listener(
            self._on_max_instances, EVENT_JOB_MAX_INSTANCES
        )

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("ScheduleRunner started (in-memory jobstore)")

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("ScheduleRunner stopped")

    @property
    def running(self) -> bool:
        return self._scheduler.running

    # ── CRUD 联动 (供 REST router 调) ─────────────────────────────────

    def apply_enabled_state(self, cron: Cron) -> None:
        """按 cron.enabled 建 / 不建 in-memory job.

        external 行永不 add_job (rebuild 也过滤掉)。fired_at 已填 / at 过期
        场景由 rebuild 前置清理, 此处不判。
        """
        if cron.dispatch_owner != "internal":
            return
        if not cron.enabled:
            # disabled 不建 in-memory job, 靠 cron.enabled=0 记账
            self._remove_job(cron.cron_id)
            return
        try:
            trigger = _build_trigger(cron)
        except (ValueError, TypeError) as e:
            logger.error(
                "apply_enabled_state: failed to build trigger for %s: %s",
                cron.cron_id,
                e,
            )
            raise
        max_delay = _resolve_max_delay(cron)
        self._scheduler.add_job(
            self._fire,
            trigger=trigger,
            id=cron.cron_id,
            args=[cron.cron_id],
            replace_existing=True,
            misfire_grace_time=max_delay,
        )

    def remove_job(self, cron_id: str) -> None:
        """CRUD DELETE 后调, 清 in-memory job + :retry 影子."""
        self._remove_job(cron_id)
        self._remove_retry_job(cron_id)

    def _remove_job(self, cron_id: str) -> None:
        try:
            self._scheduler.remove_job(cron_id)
        except Exception:  # noqa: BLE001
            pass

    def _remove_retry_job(self, cron_id: str) -> None:
        try:
            self._scheduler.remove_job(f"{cron_id}:retry")
        except Exception:  # noqa: BLE001
            pass

    # ── rebuild ─────────────────────────────────────────────────────────

    def rebuild_from_db(self) -> None:
        """启动时从 cron 表全量重建 in-memory scheduler.

        fail-fast: 任一 build_trigger / add_job 抛异常直接终止 backend 启动
        (memory 方案下 rebuild 失败 = in-memory 空 = 所有 cron 静默停跑,
        不能 log error 继续跑降级)。
        """
        current_ms = now_ms()
        for cron in self._cron_repo.list_where("dispatch_owner='internal'"):
            cron_id = cron.cron_id

            # at + fired_at 已填 → 极端亚毫秒 crash 残留, defensive 清行
            if cron.kind == "at" and cron.fired_at is not None:
                logger.info(
                    "rebuild: at %s fired_at=%d (defensive cleanup)",
                    cron_id,
                    cron.fired_at,
                )
                self._cron_repo.delete(cron_id)
                continue

            # at 过 max_delay 窗口 → 放弃 (termination max_delay=0 → None 永不过期)
            if cron.kind == "at":
                max_delay = _resolve_max_delay(cron)
                if (
                    max_delay is not None
                    and current_ms > cron.at_ms + max_delay * 1000
                ):
                    logger.warning(
                        "rebuild: at %s overdue (at=%d max_delay=%ds), give up",
                        cron_id,
                        cron.at_ms,
                        max_delay,
                    )
                    self._cron_repo.delete(cron_id)
                    continue

            # at + retry_attempt > 0 → 只挂 :retry, 跳过主 job 避免并发
            if cron.kind == "at" and cron.enabled and cron.retry_attempt > 0:
                logger.info(
                    "rebuild: at %s in retry chain (attempt=%d), only :retry",
                    cron_id,
                    cron.retry_attempt,
                )
                self._schedule_at_retry(cron)
                continue

            self.apply_enabled_state(cron)
        logger.info("rebuild_scheduler_from_db done")

    # ── _fire (触发核心) ────────────────────────────────────────────────

    async def _fire(self, cron_id: str) -> None:
        """APScheduler 触发进入点. 重查 DB + 三 defensive 分支 + 调 agent."""
        cron = self._cron_repo.get(cron_id)

        # defensive 分支 1: cron 表无 = orphan job, 清 scheduler 侧
        if cron is None:
            logger.warning(
                "_fire: cron %s missing (orphan job), removing scheduler side",
                cron_id,
            )
            self._remove_job(cron_id)
            self._remove_retry_job(cron_id)
            return

        # defensive 分支 2: disabled → 不该被触发 (可能 disable 后 in-memory 未同步)
        if not cron.enabled:
            logger.warning(
                "_fire: cron %s disabled but triggered, removing scheduler side",
                cron_id,
            )
            self._remove_job(cron_id)
            self._remove_retry_job(cron_id)
            return

        # defensive 分支 3: at fired_at 已填 → 已成功但清行漏了, 补删
        if cron.kind == "at" and cron.fired_at is not None:
            logger.warning(
                "_fire: at %s fired_at already set, cleaning row", cron_id
            )
            self._cron_repo.delete(cron_id)
            self._remove_retry_job(cron_id)
            return

        # 触发 agent
        fire_ms = now_ms()
        message = f"[cron:{cron.name}] {cron.message}"
        session_key = f"miloco-schedule:{cron_id}"
        trace_id = uuid.uuid4().hex

        # idempotency key: at 稳定 key (retry 递增), cron/every 每次唯一
        if cron.kind == "at":
            if cron.retry_attempt > 0:
                idempotency_key = (
                    f"at:{cron_id}:{cron.at_ms}:retry:{cron.retry_attempt}"
                )
            else:
                idempotency_key = f"at:{cron_id}:{cron.at_ms}"
        else:
            idempotency_key = f"cron:{cron_id}:{fire_ms}"

        wait_timeout_ms = 180_000  # 与 dispatcher 默认一致

        try:
            _, status, _ = await run_agent_turn(
                text=message,
                session_key=session_key,
                lane=_LANE,
                trace_id=trace_id,
                wait_timeout_ms=wait_timeout_ms,
                light_context=cron.light_context,
                idempotency_key=idempotency_key,
            )
        except AgentWebhookException as e:
            logger.warning(
                "_fire: cron %s transport failed: %s", cron_id, e
            )
            if cron.kind == "at":
                self._handle_at_failure(cron_id, transport_error=True)
            # cron/every 传输失败不 retry (下周期自然重触)
            return

        if status == "ok":
            self._handle_success(cron)
        elif status == "timeout":
            # at: turn 可能仍在跑或已失败, 走 :retry 链兜底 (稳定 idempotency
            # + openclaw dedupe TTL 保证重投安全)。cron/every: 无副作用, 下周
            # 期自然重触, 不需要额外重试。
            if cron.kind == "at":
                self._handle_at_failure(cron_id, transport_error=True)
        elif status == "error":
            if cron.kind == "at":
                self._handle_at_failure(cron_id, transport_error=False)
            else:
                logger.warning(
                    "_fire: cron %s status=error (cron/every), skip retry, "
                    "next tick auto",
                    cron_id,
                )
        elif status == "no-channel":
            logger.error(
                "_fire: cron %s status=no-channel (env fault, not retryable), "
                "giving up",
                cron_id,
            )
            if cron.kind == "at":
                self._cron_repo.delete(cron_id)
                self._remove_retry_job(cron_id)
        else:
            logger.error("_fire: cron %s unknown status=%s", cron_id, status)

    def _handle_success(self, cron: Cron) -> None:
        """成功路径: at 走 mark_fired_and_delete 单事务; cron/every 无副作用."""
        if cron.kind == "at":
            self._cron_repo.mark_fired_and_delete(cron.cron_id, now_ms())
            self._remove_retry_job(cron.cron_id)

    def _handle_at_failure(self, cron_id: str, *, transport_error: bool) -> None:
        """at 失败路径: 挂 :retry DateTrigger 60s 后重试, 超 max_delay 放弃."""
        cron = self._cron_repo.get(cron_id)
        if cron is None:
            return

        if not transport_error:
            # status=error 走应用层重试: retry_attempt +1, 递增 idempotency key
            new_attempt = self._cron_repo.increment_retry_attempt(cron_id)
            if new_attempt == -1:
                return
            cron = self._cron_repo.get(cron_id)
            if cron is None:
                return

        # 检查是否已超 max_delay 窗口
        current_ms = now_ms()
        max_delay = _resolve_max_delay(cron)
        if (
            max_delay is not None
            and current_ms > cron.at_ms + max_delay * 1000
        ):
            logger.warning(
                "at %s exceeded max_delay window, giving up (attempt=%d)",
                cron_id,
                cron.retry_attempt,
            )
            self._cron_repo.delete(cron_id)
            self._remove_retry_job(cron_id)
            return

        self._schedule_at_retry(cron)

    def _schedule_at_retry(self, cron: Cron) -> None:
        """挂 :retry DateTrigger, 60s 后触发 _fire (再次走应用层重试链)."""
        run_at_ms = now_ms() + _RETRY_DELAY_MS
        max_delay = _resolve_max_delay(cron)
        self._scheduler.add_job(
            self._fire,
            trigger=DateTrigger(
                run_date=datetime.fromtimestamp(
                    run_at_ms / 1000, tz=timezone.utc
                )
            ),
            id=f"{cron.cron_id}:retry",
            args=[cron.cron_id],
            replace_existing=True,
            misfire_grace_time=max_delay,
        )

    # ── listeners ───────────────────────────────────────────────────────

    def _on_missed(self, event) -> None:
        raw_id = event.job_id
        cron_id = (
            raw_id[: -len(":retry")]
            if raw_id.endswith(":retry")
            else raw_id
        )
        cron = self._cron_repo.get(cron_id)
        if cron is None:
            logger.warning("job %s missed (already removed)", raw_id)
            return
        if cron.kind == "at":
            logger.warning(
                "at %s missed (past misfire_grace_time), deleting", cron_id
            )
            self._cron_repo.delete(cron_id)
            self._remove_retry_job(cron_id)
        else:
            logger.warning(
                "cron/every %s missed (will re-fire on next tick)", cron_id
            )

    def _on_error(self, event) -> None:
        logger.error(
            "APScheduler job %s raised: %s", event.job_id, event.exception,
            exc_info=event.exception,
        )

    def _on_max_instances(self, event) -> None:
        logger.warning(
            "cron %s max_instances=1 hit (previous run still active, skipping)",
            event.job_id,
        )


_runner: ScheduleRunner | None = None


def get_runner() -> ScheduleRunner:
    global _runner
    if _runner is None:
        _runner = ScheduleRunner()
    return _runner


def reset_runner_for_tests() -> None:
    """测试用: 清 runner 单例. 生产不该调用."""
    global _runner
    if _runner is not None and _runner.running:
        _runner.shutdown()
    _runner = None


async def _noop_ensure_event_loop() -> None:
    """占位: 保证异步事件循环存在 (AsyncIOScheduler 需要)."""
    await asyncio.sleep(0)
