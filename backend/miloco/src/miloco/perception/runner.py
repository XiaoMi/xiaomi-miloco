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
import time

from miloco.config import get_settings
from miloco.database.perception_repo import PerceptionLogRepo
from miloco.perception import omni_probe_registry
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.inference_worker import InferenceWorker
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

        # 自动停止/恢复状态：omni 熔断器累计 OPEN 时间超过阈值时自动停引擎，
        # 恢复后自动重启。_auto_stopped=True 时 tick 只跑 probe 自愈，不跑 pipeline。
        # 用累计时间而非连续时间，防止 OPEN→probe→CLOSED 快速振荡时计时器反复被清零。
        self._cb_open_accumulated: float = 0.0  # 累计 OPEN 时间（秒）
        self._cb_last_open_tick: float | None = None  # 上一次 tick 时 OPEN 的时间点
        self._auto_stopped: bool = False
        self._auto_stopped_at: float | None = None  # 自动停止时刻，用于冷却期
        self._auto_restart_cooldown_sec: float = 120.0  # 自动停止后冷却期（秒）
        self._recovery_probe_task: asyncio.Task | None = None

        # Persistent worker thread with a durable event loop for inference.
        # Replaces the old ThreadPoolExecutor + asyncio.run() pattern that
        # leaked threads via repeated default executor creation. Safe to
        # stop() then start() again on this same instance — see its
        # docstring for how it isolates each restart's thread/loop so a
        # still-draining previous generation never blocks or races the new
        # one.
        self._inference_worker = InferenceWorker(thread_name="perception-infer")

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
        self._auto_stopped = False  # 用户手动启动时清除自动停止标记
        self._cb_open_accumulated = 0.0
        self._cb_last_open_tick = None

        # 重启时重读窗口时长（config 可能在停止期间被改）——__init__ 只读一次，
        # 不重读会导致「应用设置」改了 window_size 后引擎仍按旧值跑。
        self._collect_interval = get_settings().perception.collect.window_size

        # Restart worker if it was stopped by a previous stop(). Safe even
        # if that previous generation's thread hasn't exited yet (it may
        # still be draining a non-preemptible ONNX call) — start() spins up
        # a new generation on this same instance without waiting on the old
        # one; see InferenceWorker's docstring.
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
            self._recovery_probe_task,
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._perception_task = None
        self._sync_devices_task = None
        self._recovery_probe_task = None
        self._auto_stopped = False
        self._cb_open_accumulated = 0.0
        self._cb_last_open_tick = None

        # 清理 in-flight probe task,防同进程再启 runner 时 _probe_in_flight 残留导致
        # 自愈通道永久卡死。registry 是独立 module,不进 runner↔processor 循环链。
        await omni_probe_registry.cancel_inflight()

        self._inference_worker.shutdown(wait=False)

        # 关闭 perception engine（含 IdentityEngine dispatcher worker 等）
        try:
            await self._pipeline.close()
        except Exception as e:  # noqa: BLE001
            logger.error("[engine] 关闭引擎失败 | %s", e)

        await self._collector.shutdown()
        logger.info("Perception engine stopped")

    async def _tick(self) -> None:
        """Drain all ready windows and infer sequentially."""
        # 自动生命周期管理：检查 omni 熔断器状态，决定是否自动停止/恢复引擎
        await self._auto_manage_lifecycle()

        # 自动停止后：只跑 probe 自愈，不跑 pipeline（节省 CPU + ONNX 内存）
        if self._auto_stopped:
            return

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

    # ---- 自动停止/恢复 ----------------------------------------------------------

    async def _auto_manage_lifecycle(self) -> None:
        """每 tick 检查 omni 熔断器状态，驱动自动停止/恢复。

        自动停止条件：熔断器持续 OPEN_RECOVERABLE / OPEN_CONFIG 超过阈值秒数。
        自动恢复条件：自动停止后，Runner 驱动 probe 探测成功（熔断器回到 CLOSED）。
        """
        settings = get_settings().perception.collect
        if not settings.auto_stop_on_omni_failure:
            return

        from miloco.perception.engine.omni.circuit_breaker import (
            CircuitState,
            get_omni_circuit_breaker,
        )

        cb = get_omni_circuit_breaker()
        state = cb.current_state

        if self._auto_stopped:
            # 已自动停止 → 检查是否该恢复
            if state == CircuitState.CLOSED:
                # 冷却期内不重启，防止 stop→restart 振荡
                elapsed = time.monotonic() - self._auto_stopped_at if self._auto_stopped_at else 0
                if elapsed < self._auto_restart_cooldown_sec:
                    logger.debug(
                        "[auto-lifecycle] CLOSED but in cooldown (%.0fs/%.0fs)",
                        elapsed, self._auto_restart_cooldown_sec,
                    )
                else:
                    logger.info("[runner] omni 恢复（熔断器 CLOSED），自动重启感知引擎")
                    await self._auto_restart_engine()
            else:
                # 继续驱动 probe（内部有退避节流，不会高频调用）
                self._drive_recovery_probe()
            return

        # 未自动停止 → 累计 OPEN 时间，检查是否该停
        # OPEN_CONFIG（API key 错误/模型不存在）也会触发自动停止；
        # 但恢复时 _drive_recovery_probe 的 try_arm_probe 只认 OPEN_RECOVERABLE，
        # 所以 OPEN_CONFIG 停机后不会自动 probe，需要用户修正配置后手动重试。
        now = time.monotonic()
        is_open = state in (CircuitState.OPEN_RECOVERABLE, CircuitState.OPEN_CONFIG)

        if is_open:
            # 累计 OPEN 时间
            if self._cb_last_open_tick is not None:
                self._cb_open_accumulated += now - self._cb_last_open_tick
            self._cb_last_open_tick = now

            if self._cb_open_accumulated > settings.auto_stop_threshold_sec:
                logger.warning(
                    "[runner] omni 熔断器累计 OPEN %.0fs（阈值 %.0fs），自动停止感知引擎",
                    self._cb_open_accumulated,
                    settings.auto_stop_threshold_sec,
                )
                self._cb_last_open_tick = None
                await self._auto_stop_engine()
        else:
            # CLOSED 或 HALF_OPEN → 停止累计，但不清零（保留已累计的时间）
            self._cb_last_open_tick = None

    async def _auto_stop_engine(self) -> None:
        """停止引擎但保留 tick 循环，为 probe 自愈留通道。

        与 stop_engine 的区别：只暂停解码器 + 关引擎实例，**不关采集器**。
        设备保持连接（避免 _sync_devices_loop 反悔重连 churn），解码器暂停后
        不产出解码帧，sync_buffer 自然不进数据，CPU 一样省。
        """
        self._auto_stopped = True
        self._auto_stopped_at = time.monotonic()
        self._cb_open_accumulated = 0.0
        self._cb_last_open_tick = None
        try:
            # 只暂停解码器 + 关引擎；不调 collector.shutdown()，避免被
            # _sync_devices_loop 在 ~1s 内 reconnect 所有设备（白做一轮 disconnect→reconnect）。
            self._collector.pause_streams()
            await self._pipeline.close()
            logger.info("[runner] 感知引擎已自动停止（gate+identity+omni 全停，解码器已暂停），等待 omni 恢复")
        except Exception as e:
            logger.error("[runner] 自动停止引擎失败 | %s", e, exc_info=True)

    async def _auto_restart_engine(self) -> None:
        """probe 成功后自动重启引擎。"""
        self._auto_stopped = False
        self._recovery_probe_task = None
        try:
            # 先恢复解码器，再重建引擎和采集
            self._collector.resume_streams()
            await self._collector.switch_to_decode_mode()
            await self._collector.sync_all_devices()
            self._pipeline.try_reinit_engine(include_failed=True)
            if not self._inference_worker.is_running:
                self._inference_worker.start()
            self._pipeline.set_inference_worker(self._inference_worker)
            logger.info("[runner] 感知引擎自动重启成功")
        except Exception as e:
            logger.error("[runner] 自动重启引擎失败 | %s", e, exc_info=True)
            self._auto_stopped = True  # 重启失败，保持停止态

    def _drive_recovery_probe(self) -> None:
        """引擎停止后，由 Runner 直接驱动熔断器 probe（复用 processor._run_omni_probe）。

        熔断器内部的 try_arm_probe() 有退避节流：只有 OPEN_RECOVERABLE + backoff 到期
        + 无 in-flight 时才放行，避免高频探测。
        """
        from miloco.perception.engine.omni.circuit_breaker import (
            get_omni_circuit_breaker,
        )
        from miloco.perception.processor import _run_omni_probe

        cb = get_omni_circuit_breaker()
        if not cb.try_arm_probe():
            return
        self._recovery_probe_task = asyncio.create_task(_run_omni_probe())

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
