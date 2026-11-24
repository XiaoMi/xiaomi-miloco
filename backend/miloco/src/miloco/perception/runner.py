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
from collections.abc import Callable

from miloco.config import get_settings
from miloco.config.settings import PerceptionScheduleMode
from miloco.database.perception_repo import PerceptionLogRepo
from miloco.perception import omni_probe_registry
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.inference_worker import InferenceWorker
from miloco.perception.per_camera_scheduler import PerCameraScheduler
from miloco.perception.processor import PipelineProcessor
from miloco.perception.schema import (
    CameraWindowSource,
    EngineState,
    PerceptionEngineStatus,
)

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
        self._window_ready = window_ready_event if window_ready_event is not None else asyncio.Event()
        self._camera_scheduler: PerCameraScheduler | None = None
        self._schedule_mode: PerceptionScheduleMode = "batch"
        if get_settings().perception.schedule.mode == "per_camera" and self.schedule_available:
            self._set_schedule_mode("per_camera")

        # 复用专用推理线程的事件循环，避免每轮推理反复创建默认 executor。
        self._inference_worker = InferenceWorker(thread_name="perception-infer")

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def window_size_effective(self) -> int | None:
        return self._collect_interval if self._is_running else None

    @property
    def schedule_available(self) -> bool:
        return isinstance(self._collector.get_adapter("camera"), CameraWindowSource)

    @property
    def schedule_mode(self) -> PerceptionScheduleMode:
        return self._schedule_mode

    def _set_schedule_mode(self, mode: PerceptionScheduleMode) -> None:
        adapter = None
        if mode == "per_camera":
            adapter = self._collector.get_adapter("camera")
            if not isinstance(adapter, CameraWindowSource):
                raise ValueError("camera scheduling requires a camera adapter")
        if self._camera_scheduler is not None:
            self._camera_scheduler.retire_results()
        self._camera_scheduler = self._pipeline.set_camera_scheduler(adapter, self._window_ready.set)
        self._schedule_mode = mode
        self._window_ready.set()

    async def apply_schedule_config(
        self,
        mode: PerceptionScheduleMode,
        save: Callable[[], None],
        *,
        restart_window: bool,
        omni_fps: int | None,
    ) -> None:
        async with self._pipeline.drain_inference():
            save()
            if restart_window and self._is_running:
                self._pipeline.retire_results()
                if self._camera_scheduler is not None:
                    self._camera_scheduler.retire_results()
                await self._finish_stop()
                await self._finish_start()
            if omni_fps is not None:
                await self._pipeline.apply_omni_fps(omni_fps)
            if mode != self._schedule_mode or (
                mode == "per_camera" and (restart_window or omni_fps is not None)
            ):
                self._set_schedule_mode(mode)

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

        async with self._pipeline.drain_inference():
            await self._finish_start()

    async def _finish_start(self) -> None:
        self._pipeline.clear_flow_snapshots()
        self._is_running = True

        # 重启时重读窗口时长（config 可能在停止期间被改）——__init__ 只读一次，
        # 不重读会导致「应用设置」改了 window_size 后引擎仍按旧值跑。
        self._collect_interval = get_settings().perception.collect.window_size

        # worker 的每次启动使用独立线程和事件循环，旧线程尚在退出也不阻塞新一代。
        if not self._inference_worker.is_running:
            self._inference_worker.start()

        # 显式启动/重启(含「重启感知」按钮):全可恢复态重建一次,含 engine_init_failed
        # ——引擎构造失败(如临时磁盘满)补救后靠这条恢复,不在 tick 每秒重试重型构造。
        # 必须在 set_inference_worker 之前,确保引擎已存在再挂 worker。
        self._pipeline.try_reinit_engine(include_failed=True)

        # Attach inference worker to engine proxy so perceive calls
        # run in the dedicated thread, not on the main event loop.
        self._pipeline.set_inference_worker(self._inference_worker)

        # Initial device sync before first tick
        await self._sync_devices()

        self._perception_task = asyncio.create_task(self._perception_loop())
        self._sync_devices_task = asyncio.create_task(self._sync_devices_loop())

        logger.info("Perception engine started")

    async def stop(self) -> None:
        """Stop the runner once accepted inference has finished."""
        self._retire_results()
        async with self._pipeline.drain_inference(allow_sealed=True):
            await self._finish_stop()

    async def restart(
        self, should_start: Callable[[], bool], *,
        wait_seconds: float | None = None, discard_inflight_results: bool = False,
    ) -> bool:
        """在同一次排空内完成重启。授权刷新后，旧连接的在途结果先失效。"""
        if discard_inflight_results:
            self._retire_results()
        async with self._pipeline.drain_inference(wait_seconds=wait_seconds):
            self._retire_results()
            await self._finish_stop()
            # 排空和停止期间用户可能点了「让它休息」，也可能开始关机。
            if not self._pipeline.inference_sealed and should_start():
                await self._finish_start()
                return True
            return False

    def _retire_results(self) -> None:
        self._pipeline.retire_results()
        if self._camera_scheduler is not None:
            self._camera_scheduler.retire_results()

    async def _finish_stop(self) -> None:
        if not self._is_running:
            self._pipeline.clear_flow_snapshots()
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

        # 关闭 perception engine（含 IdentityEngine dispatcher worker 等）
        try:
            await self._pipeline.close()
        except Exception as e:  # noqa: BLE001
            logger.error("[engine] 关闭引擎失败 | %s", e)
        finally:
            self._inference_worker.shutdown(wait=False)
            self._pipeline.clear_flow_snapshots()

        await self._collector.shutdown()
        logger.info("Perception engine stopped")

    async def _tick(self) -> None:
        """Drain all ready windows and infer sequentially."""
        if not self._pipeline.accepting_inference:
            return
        # 每 tick 驱动一次 omni 熔断器自动探测:OPEN_RECOVERABLE(backoff 到期)或
        # OPEN_CONFIG(慢周期到期,防分类误判黑洞)时 spawn 一次后台 probe。无外部驱动时
        # probe_due 归零后状态永远不动,provider 恢复后感知也不会自愈,只能靠用户手动点
        # “立即重试”。sync 判断 + 后台 spawn,tick 不阻塞。
        # 前置到 active_sources 判断之前:无摄像头联调 / 摄像头全部掉线场景下也要能自愈,
        # 否则 backoff 到期后 next_probe_at_monotonic 卡在过去、SSE 快照持续显示"0 秒后下次探测"。
        # CLOSED / HALF_OPEN 时 try_arm_probe 零开销直接返 False,前置安全。
        self._pipeline.drive_omni_probe()

        active_device_ids = set(self._collector.get_all_active_sources())
        self._pipeline.retain_flow_devices(active_device_ids)
        if not active_device_ids:
            return

        # 每个 tick 自愈一次:出厂态配好 key / 补完模型后,下个推理周期(默认 4s)自动转
        # ready,与 omni_client.resolve_live_omni_config 注释承诺的"下个推理周期热生效"
        # 对齐。只放行廉价"等外部条件"态(缺 key/模型),engine_init_failed 不在此重试
        # (见 try_reinit);配合 STARTING 后移,未满足前置条件时零开销、零 event_log 噪声。
        self._pipeline.try_reinit_engine()

        if (scheduler := self._camera_scheduler) is not None:
            rules = await self._pipeline.get_realtime_rules()
            if self._camera_scheduler is scheduler:
                scheduler.schedule_ready(rules)
            return
        result = await self._pipeline.process_realtime()
        # 缓冲区里可能积压了多个 ready 窗口，此处循环处理直到缓冲区清空
        while result is not None and self._is_running and self._camera_scheduler is None:
            result = await self._pipeline.process_realtime()

    async def _wait_for_trigger(self) -> None:
        """Wait for window-ready event OR capture interval timeout.

        If a window_ready_event is provided, we race it against the timer.
        The event is cleared after waking so the next cycle can wait again.
        """
        try:
            await asyncio.wait_for(
                self._window_ready.wait(),
                timeout=self._collect_interval,
            )
        except TimeoutError:
            # With or without a signal, the next tick checks for ready windows.
            pass
        finally:
            self._window_ready.clear()

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
                await self._sync_devices()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[runner] 设备同步失败 | %s", e, exc_info=True)

    async def _sync_devices(self) -> None:
        await self._collector.sync_all_devices()
        self._pipeline.retain_flow_devices(
            set(self._collector.get_all_active_sources())
        )
