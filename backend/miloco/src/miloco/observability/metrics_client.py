"""异步队列 + 后台 worker。

业务路径只入队;worker 单线程串行写 SQLite,容忍弱一致。
对外暴露 module-level singleton ``get_metrics_client()``,启动期 ``set_metrics_client()`` 绑定。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miloco.observability.context import get_trace_id
from miloco.observability.metrics_db import connect, init_schema
from miloco.observability.types import (
    ActionLedgerRecord,
    AgentRunRecord,
    CycleTraceRecord,
    DeviceTraceRecord,
)

logger = logging.getLogger(__name__)

# 区分"调用者未传 trace_id"(走 ContextVar) 与 "显式传 None"(后台事件)
_SENTINEL = object()

# 周期性 commit 间隔。把每条独立 commit (autocommit) 换成固定周期的批量事务,
# fsync 频率从 ~117/s 锁到 ~1/_COMMIT_INTERVAL_SEC。
# 取值 15s = 4s nominal cycle × 3 + 3s jitter buffer:
# 即便 cycle 偶尔拉到 4.5-5s,也能稳定把 3 包合到同一事务。
# 故障最多丢一个周期内的 observability 数据(无业务影响)。
_COMMIT_INTERVAL_SEC = 15.0

# Buffer 上限,防御性:queue_maxsize 限制 in-flight 但不限 buffer,
# 突发流量(backlog flush / 后台 backfill 大量 enqueue)下 buffer 可能膨胀,
# 引发 OOM 或单事务过大(N 条 INSERT 在 _flush_buffer 里执行时间线性增长,
# 锁持有时长上升)。500 = 普通节奏 50x 余量(4s/cycle 几条 publish → 15s ~10 条),
# 突发场景下触发提前 flush,fsync 频率短时上升仍远低于 autocommit 模式。
_BUFFER_MAX = 500


class _FlushSentinel:
    """flush() 投递的特殊标记。worker 见到立即 commit 当前事务,
    使 flush() 的 ``await queue.join()`` 不必等 _COMMIT_INTERVAL_SEC 超时。
    """


_FLUSH_SENTINEL = _FlushSentinel()


@dataclass
class _PublishTraceJob:
    cycle: CycleTraceRecord
    devices: list[DeviceTraceRecord]


@dataclass
class _RecordAgentRunJob:
    record: AgentRunRecord


@dataclass
class _RecordActionJob:
    record: ActionLedgerRecord


@dataclass
class _PublishEventJob:
    event_id: str
    timestamp: int
    event_type: str
    trace_id: str | None
    source: str
    payload: str


class MetricsClient:
    def __init__(self, db_path: Path | str, queue_maxsize: int = 4096) -> None:
        self._db_path = Path(db_path)
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker: asyncio.Task | None = None
        self._conn: sqlite3.Connection | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._worker is not None:
            return
        self._conn = connect(self._db_path)
        init_schema(self._conn)
        self._stop.clear()
        self._worker = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._stop.set()
        await self._queue.put(None)
        try:
            await self._worker
        except Exception:
            logger.exception("metrics worker stop raised")
        self._worker = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def flush(self) -> None:
        """触发立即 commit + 等所有 in-flight job 完成(仅供测试与 shutdown 使用)。

        投递 _FLUSH_SENTINEL 让 worker 结束当前凑批并立即 commit,
        然后等 queue 排空 + 当前 batch 的 task_done 全部调用完毕。
        """
        if self._worker is None or self._worker.done():
            return
        await self._queue.put(_FLUSH_SENTINEL)
        await self._queue.join()

    def publish_trace(
        self,
        cycle: CycleTraceRecord,
        devices: list[DeviceTraceRecord],
    ) -> None:
        self._enqueue(_PublishTraceJob(cycle=cycle, devices=list(devices)))

    def record_agent_run(self, record: AgentRunRecord) -> None:
        self._enqueue(_RecordAgentRunJob(record=record))

    def record_action(self, record: ActionLedgerRecord) -> None:
        """入队一行 action_ledger(设备控制 / TTS / 场景触发的持久审计)。"""
        self._enqueue(_RecordActionJob(record=record))

    def publish_event(
        self,
        event_type: str,
        source: str,
        payload: dict[str, Any],
        trace_id: str | None | object = _SENTINEL,
    ) -> None:
        if trace_id is _SENTINEL:
            resolved: str | None = get_trace_id()
        else:
            resolved = trace_id  # type: ignore[assignment]
        self._enqueue(_PublishEventJob(
            event_id=str(uuid.uuid4()),
            timestamp=int(time.time() * 1000),
            event_type=event_type,
            trace_id=resolved,
            source=source,
            payload=json.dumps(payload, ensure_ascii=False),
        ))

    def _enqueue(self, job: Any) -> None:
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.warning("metrics queue full; dropping job %r", type(job).__name__)

    async def _run_worker(self) -> None:
        """内存 cache + 周期 flush:job 进内存 buffer,_COMMIT_INTERVAL_SEC 到点
        或收到 flush/stop sentinel 时,一次性 BEGIN+INSERT×n+COMMIT。

        关键:**事务期间只跑 INSERT 序列,锁持有 ~10ms**(而非长事务的 15s),
        cleanup / 其他 writer 不会被卡。崩溃丢失窗口同 15s(buffer 在内存里)。

        空闲期外层无超时阻塞在 queue.get(),**不产生任何 SQL 调用**。

        控制信号:
          - ``None``           (stop sentinel):flush buffer 后退出
          - ``_FlushSentinel`` (flush sentinel):立即 flush,回到外层等下一条
        """
        assert self._conn is not None

        stop_requested = False
        while not stop_requested and (
            not self._stop.is_set() or not self._queue.empty()
        ):
            # 外层:无超时等第一条 job(空闲期完全阻塞)
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                break

            if first is None:
                self._queue.task_done()
                if self._stop.is_set():
                    break
                continue
            if isinstance(first, _FlushSentinel):
                # buffer 为空,sentinel 是 no-op
                self._queue.task_done()
                continue

            # 真正的业务 job:进 buffer + 启动计时
            buffer: list[Any] = [first]
            deadline = time.monotonic() + _COMMIT_INTERVAL_SEC

            # 内层:凑批到内存 buffer + 等超时 / sentinel / buffer 满
            while len(buffer) < _BUFFER_MAX:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    job = await asyncio.wait_for(
                        self._queue.get(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    break
                except asyncio.CancelledError:
                    stop_requested = True
                    break

                if job is None:
                    self._queue.task_done()
                    if self._stop.is_set():
                        stop_requested = True
                    break
                if isinstance(job, _FlushSentinel):
                    self._queue.task_done()
                    break

                buffer.append(job)

            # flush buffer:单事务批量 INSERT,锁持有 ~10ms
            self._flush_buffer(buffer)
            for _ in buffer:
                self._queue.task_done()

    def _flush_buffer(self, buffer: list[Any]) -> None:
        """单事务批量 apply。BEGIN/COMMIT 包住 N 条 INSERT,
        单条 apply 异常只记日志不打断整批;commit 自身失败时 ROLLBACK 兜底。"""
        assert self._conn is not None
        if not buffer:
            return
        try:
            self._conn.execute("BEGIN")
        except sqlite3.Error:
            logger.exception(
                "metrics worker BEGIN failed; dropping %d buffered jobs",
                len(buffer),
            )
            return
        try:
            for job in buffer:
                try:
                    self._apply(job)
                except Exception:
                    logger.exception(
                        "metrics worker failed to apply %r",
                        type(job).__name__,
                    )
            self._conn.execute("COMMIT")
        except sqlite3.Error:
            logger.exception("metrics worker COMMIT failed; rolling back")
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                logger.exception("metrics worker ROLLBACK failed")

    def _apply(self, job: Any) -> None:
        assert self._conn is not None
        if isinstance(job, _PublishTraceJob):
            self._insert("traces", job.cycle.to_row())
            for d in job.devices:
                self._insert("traces_device", d.to_row())
        elif isinstance(job, _RecordAgentRunJob):
            self._insert("agent_runs", job.record.to_row())
        elif isinstance(job, _RecordActionJob):
            self._insert("action_ledger", job.record.to_row())
        elif isinstance(job, _PublishEventJob):
            self._conn.execute(
                "INSERT INTO events (event_id, timestamp, event_type, trace_id, source, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job.event_id, job.timestamp, job.event_type,
                 job.trace_id, job.source, job.payload),
            )

    def _insert(self, table: str, row: dict[str, Any]) -> None:
        assert self._conn is not None
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        self._conn.execute(
            f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})",
            tuple(row.values()),
        )


_singleton: MetricsClient | None = None


def set_metrics_client(client: MetricsClient | None) -> None:
    """启动期/关停期由 main.py lifespan 调用,绑定/解绑全局实例。"""
    global _singleton
    _singleton = client


def get_metrics_client() -> MetricsClient | None:
    """业务层取全局 MetricsClient;未绑定时返回 None(业务路径自行判空跳过 publish)。"""
    return _singleton
