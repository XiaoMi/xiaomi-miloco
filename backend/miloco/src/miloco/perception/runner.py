"""
Realtime Perception Engine.

Scheduler that delegates perception to the pipeline processor.
Device sync runs on its own timer, decoupled from perception ticks.

The perception loop reacts to two triggers:
1. **Window-ready event** — fired by MultiTrackSyncBuffer when a time
   window has data from all tracks (early trigger).
2. **Capture interval timeout** — fallback timer that fires even when
   not all tracks have arrived within the window.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from miloco.config import get_settings
from miloco.database.perception_repo import PerceptionLogRepo
from miloco.perception import omni_probe_registry
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.processor import PipelineProcessor
from miloco.perception.schema import EngineState, PerceptionEngineStatus

logger = logging.getLogger(__name__)


class PerceptionRunner:
    """Background engine that schedules periodic perception cycles."""

    def __init__(
        self,
        collector: MultimodalCollector,
        pipeline: PipelineProcessor,
        log_repo: PerceptionLogRepo,
        window_ready_event: asyncio.Event | None = None,
    ):
        self._collector = collector
        self._pipeline = pipeline
        self._log_repo = log_repo

        self._collect_interval = get_settings().perception.collect.window_size
        self._is_running = False
        self._perception_task: asyncio.Task | None = None
        self._sync_devices_task: asyncio.Task | None = None
        self._window_ready = window_ready_event

        # Dedicated single-thread executor for inference — keeps the main
        # event loop free for stream frame callbacks.
        self._inference_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="perception-infer"
        )

    @property
    def is_running(self) -> bool:
        return self._is_running

    def status(self) -> PerceptionEngineStatus:
        sources = self._collector.get_all_active_sources()
        last_latency = self._pipeline.last_latency
        return PerceptionEngineStatus(
            running=self._is_running,
            engine=EngineState(
                ready=self._pipeline.engine_ready,
                status=self._pipeline.engine_status,
                message=self._pipeline.engine_status_message,
            ),
            interval_seconds=self._collect_interval,
            today_inference_count=self._log_repo.get_today_inference_count(),
            active_sources=[
                {
                    "did": s.did,
                    "name": s.name,
                    "device_type": s.device_type,
                    "room_name": s.room_name,
                }
                for s in sources.values()
            ],
            last_latency=last_latency.to_dict() if last_latency else None,
        )

    async def start(self) -> None:
        """Start the realtime perception loop and device sync loop."""
        if self._is_running:
            logger.warning("[engine] 引擎已在运行，忽略重复启动")
            return

        self._is_running = True

        # 重启时重读窗口时长（config 可能在停止期间被改）——__init__ 只读一次，
        # 不重读会导致「应用设置」改了 window_size 后引擎仍按旧值跑。
        self._collect_interval = get_settings().perception.collect.window_size

        # Recreate executor if it was shut down by a previous stop()
        if self._inference_executor._shutdown:
            self._inference_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="perception-infer"
            )

        # 显式启动/重启(含「重启感知」按钮):全可恢复态重建一次,含 engine_init_failed
        # ——引擎构造失败(如临时磁盘满)补救后靠这条恢复,不在 tick 每秒重试重型构造。
        # 必须在 set_inference_executor 之前,确保引擎已存在再挂 executor。
        self._pipeline.try_reinit_engine(include_failed=True)

        # Attach inference executor to engine proxy so perceive calls
        # run in the dedicated thread, not on the main event loop.
        self._pipeline.set_inference_executor(self._inference_executor)

        # Initial device sync before first tick
        await self._collector.sync_all_devices()

        self._perception_task = asyncio.create_task(self._perception_loop())
        self._sync_devices_task = asyncio.create_task(self._sync_devices_loop())

        logger.info("Perception engine started")

    async def stop(self) -> None:
        """Stop the realtime perception loop and shutdown collector."""
        if not self._is_running:
            logger.warning("[engine] 引擎未运行，忽略重复停止")
            return

        self._is_running = False

        for task in (
            self._perception_task,
            self._sync_devices_task,
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._perception_task = None
        self._sync_devices_task = None

        # 清理 in-flight probe task,防同进程再启 runner 时 _probe_in_flight 残留导致
        # 自愈通道永久卡死。registry 是独立 module,不进 runner↔processor 循环链。
        await omni_probe_registry.cancel_inflight()

        self._inference_executor.shutdown(wait=False)

        # 关闭 perception engine（含 IdentityEngine dispatcher worker 等）
        try:
            await self._pipeline.close()
        except Exception as e:  # noqa: BLE001
            logger.error("[engine] 关闭引擎失败 | %s", e)

        await self._collector.shutdown()
        logger.info("Perception engine stopped")

    async def _tick(self) -> None:
        """Drain all ready windows and infer sequentially."""
        # 每 tick 驱动一次 omni 熔断器自动探测:OPEN_RECOVERABLE + backoff 到期时 spawn
        # 一次后台 probe。无外部驱动时 probe_due 归零后状态永远不动,provider 恢复后感知
        # 也不会自愈,只能靠用户手动点「立即重试」。sync 判断 + 后台 spawn,tick 不阻塞。
        # 前置到 active_sources 判断之前:无摄像头联调 / 摄像头全部掉线场景下也要能自愈,
        # 否则 backoff 到期后 next_probe_at_monotonic 卡在过去、SSE 快照持续显示"0 秒后下次探测"。
        # 非 OPEN_RECOVERABLE 时 try_arm_probe 零开销直接返 False,前置安全。
        self._pipeline.drive_omni_probe()

        if not self._collector.get_all_active_sources():
            return

        # 每个 tick 自愈一次:出厂态配好 key / 补完模型后,下个推理周期(默认 4s)自动转
        # ready,与 omni_client.resolve_live_omni_config 注释承诺的"下个推理周期热生效"
        # 对齐。只放行廉价"等外部条件"态(缺 key/模型),engine_init_failed 不在此重试
        # (见 try_reinit);配合 STARTING 后移,未满足前置条件时零开销、零 event_log 噪声。
        self._pipeline.try_reinit_engine()

        result = await self._pipeline.process_realtime()
        # 缓冲区里可能积压了多个 ready 窗口，此处循环处理直到缓冲区清空
        while result is not None and self._is_running:
            result = await self._pipeline.process_realtime()

    async def _wait_for_trigger(self) -> None:
        """Wait for window-ready event OR capture interval timeout.

        If a window_ready_event is provided, we race it against the timer.
        The event is cleared after waking so the next cycle can wait again.
        """
        if self._window_ready is not None:
            try:
                await asyncio.wait_for(
                    self._window_ready.wait(),
                    timeout=self._collect_interval,
                )
            except TimeoutError:
                pass
            finally:
                self._window_ready.clear()
        else:
            await asyncio.sleep(self._collect_interval)

    async def _perception_loop(self) -> None:
        """Perception loop — wakes on window-ready or timeout.

        Each cycle: run one tick (drains all ready windows), then wait for
        the next trigger.
        """
        while self._is_running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[runner] 单次感知循环失败 | %s", e, exc_info=True)

            try:
                await self._wait_for_trigger()
            except asyncio.CancelledError:
                break

    async def _sync_devices_loop(self) -> None:
        """Device sync loop — runs independently from perception ticks."""
        while self._is_running:
            try:
                active_devices = self._collector.get_all_active_sources()
                await asyncio.sleep(10 if len(active_devices) > 0 else 1)
            except asyncio.CancelledError:
                break

            try:
                await self._collector.sync_all_devices()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[runner] 设备同步失败 | %s", e, exc_info=True)
