"""
Perception service layer.

Orchestrates the realtime engine, active perception queries,
perception log retrieval, and device management.

Active perception uses the same pipeline as realtime — data is collected
from the realtime stream buffers via collector.collect_batch(),
ensuring a unified data path.
"""

import asyncio
import logging

from miloco.database.on_demand_log_repo import OnDemandLogRepo
from miloco.database.perception_repo import PerceptionLogRepo
from miloco.middleware.exceptions import BusinessException
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.processor import PipelineProcessor
from miloco.perception.runner import PerceptionRunner
from miloco.perception.schema import (
    OnDemandPerceptionRequest,
    OnDemandPerceptionResultItem,
    PerceptionEngineStatus,
)
from miloco.perception.types import PerceptionDevice
from miloco.utils.time_utils import ms_to_iso_local, now_ms

logger = logging.getLogger(__name__)


class PerceptionService:
    """Service for all perception operations."""

    def __init__(
        self,
        collector: MultimodalCollector,
        pipeline: PipelineProcessor,
        perception_runner: PerceptionRunner,
        log_repo: PerceptionLogRepo,
        on_demand_log_repo: OnDemandLogRepo | None = None,
    ):
        self._collector = collector
        self._pipeline = pipeline
        self._engine = perception_runner
        self._log_repo = log_repo
        self._od_log_repo = on_demand_log_repo or OnDemandLogRepo()
        # 串行化引擎生命周期操作(start/stop/重建/降级)。这些操作都含多个 await
        # 让出点且改 runner._is_running,不加锁会在「应用设置重启」与用户手动
        # 启停/删模型交错时出现 executor 未重挂、孤儿 task 等状态错乱。
        self._lifecycle_lock = asyncio.Lock()

    # ---- Realtime engine lifecycle ----

    async def start_engine(self) -> None:
        async with self._lifecycle_lock:
            await self._engine.start()

    async def stop_engine(self) -> None:
        async with self._lifecycle_lock:
            await self._engine.stop()

    async def stop_to_unconfigured(self) -> None:
        """软停引擎回到「未配模型」态(删当前生效模型用),保留 tick 自愈循环。

        与 stop_engine 的区别:stop_engine 停整个 realtime 循环(含采集/设备同步);
        本方法只关引擎实例 + 降级状态,采集与 tick 继续,后续配好新模型自动自愈拉起。
        """
        async with self._lifecycle_lock:
            await self._pipeline.stop_to_unconfigured()

    async def apply_config_restart(self) -> bool:
        """window_size 变更后重启 runner 使新值生效：停 runner → 启 runner。

        window_size 靠 runner.start() 重读（见 runner.start），只需 stop→start，
        不重建引擎、不重载模型。was_running 时才需重启（未跑时下次 start 自然读新值）。
        全程持 lifecycle 锁,避免与并发的 start_engine/stop_engine 交错。

        （omni_fps 变更不再走这里——改走 ``apply_omni_fps_live`` 运行时热更，见其注释。）

        返回重启是否成功。config 已由调用方写盘(不可回滚),重启失败时返 False 让调用方
        区分「已保存但重启失败」,不冒泡成 500——否则前端会把「写盘成功+重启失败」误报
        成「保存失败」。
        """
        async with self._lifecycle_lock:
            try:
                was_running = self._engine.is_running
                if was_running:
                    await self._engine.stop()
                    await self._engine.start()
                return True
            except Exception as e:  # noqa: BLE001
                logger.error("[service] 感知参数变更后重启失败(config 已写盘) | %s", e, exc_info=True)
                return False

    async def apply_omni_fps_live(self, omni_fps: int) -> bool:
        """运行时热更 omni_fps（含其顶起的 tracker fps）：不停 runner、不重建引擎、
        不重载模型、不丢在途 track 状态。

        与 ``apply_config_restart`` 并列的轻量入口：只把新 omni_fps 原地推给活跃引擎
        （PipelineProcessor.apply_omni_fps → proxy → engine.apply_omni_fps）。持 lifecycle
        锁避免与 start/stop/restart 交错。引擎未起时 proxy 层 no-op（settings 已写盘）。

        返回是否成功。config 已写盘(不可回滚),失败返 False 不冒泡 500（同 restart 语义）。
        """
        async with self._lifecycle_lock:
            try:
                await self._pipeline.apply_omni_fps(omni_fps)
                return True
            except Exception as e:  # noqa: BLE001
                logger.error("[service] omni_fps 热更失败(config 已写盘) | %s", e, exc_info=True)
                return False

    def engine_status(self) -> PerceptionEngineStatus:
        return self._engine.status()

    @property
    def tier_u_pool(self):
        """暴露 PerceptionEngine 内部的 TierUPool(陌生人池)给 router 用。

        实际穿层封装放在 ``PipelineProcessor.tier_u_pool`` property,本层只透传。
        engine 禁用 / 池启动失败时返 None。
        """
        return self._pipeline.tier_u_pool

    @property
    def deep_sort_config(self):
        """暴露 yaml-resolved DeepSortConfigDC 给 router 视频注册路径用。

        穿层封装放在 ``PipelineProcessor.deep_sort_config``,本层透传。
        engine 未初始化时返代码默认值(``DeepSortConfigDC()``)。
        """
        return self._pipeline.deep_sort_config

    def get_active_confirmed_track_keys(self) -> list[tuple[str, int]]:
        """暴露当前所有 cam 上 confirmed track 的 ``(cam_id, track_id)`` 列表。

        给 router pool_fetch 用: 跟 confirmed track 实时 emb 做去重 (case b)。
        engine 未初始化时返空列表。
        """
        return self._pipeline.get_active_confirmed_track_keys()

    def get_reid_extractor(self):
        """从任一活动的 DeepSortTracker 借 HumanReID 实例,给身份库注册时
        ``add_tier_a_samples_batch`` 做 .npy 兜底抽取用。
        所有 device 的 tracker 共用同一份 ReID ONNX 模型,任选一个即可;
        无活动 tracker → None,库就跳过兜底(行为退回旧版,不报错)。
        """
        return self._pipeline.get_reid_extractor()

    # ---- Buffer management ----

    def clear_buffers(self) -> None:
        """Clear all device stream buffers.

        Discards all buffered data without disconnecting devices.
        New frames arriving after this call start from a clean state,
        allowing the realtime pipeline to process only fresh data.
        """
        self._collector.clear_all_buffers()
        logger.info("All perception buffers cleared")

    # ---- Active perception ----

    async def on_demand_perceive(
        self, request: OnDemandPerceptionRequest
    ) -> OnDemandPerceptionResultItem | None:
        """On-demand perception: batch-collects requested devices and runs
        a single fusion inference via pipeline.

        If the realtime engine is running, data comes from its existing stream
        subscriptions. If not running, the collector may have no data.
        """
        import uuid

        from miloco.perception.schema import OnDemandLogEntry

        active_sources = self._collector.get_all_active_sources()

        valid_dids: list[str] = []
        for did in request.sources:
            if did not in active_sources:
                logger.warning(
                    "[service](device=%s) 未激活感知(skipped)", did
                )
                continue
            valid_dids.append(did)

        if not valid_dids:
            raise BusinessException(
                "No valid active perception sources found. "
                "Ensure the perception engine is running and devices are online.",
                code=2011,
            )

        t_start = now_ms()

        # Single batch inference call — collector assembles batch, processor infers
        pipeline_result = await self._pipeline.process_on_demand(valid_dids, request.query)

        if not pipeline_result:
            raise BusinessException(
                "Failed to perform on-demand perception.",
                code=2012,
            )

        result, artifacts = pipeline_result
        t_end = now_ms()
        log_id = str(uuid.uuid4())

        # Save artifacts (clips + trace) to disk
        clip_dids: list[str] = []
        clip_kinds: dict[str, str] = {}
        has_trace = False

        if artifacts.clips or artifacts.trace:
            from miloco.config.settings import get_settings
            from miloco.perception.snapshot_writer import (
                check_disk_space,
                get_snapshot_root,
                save_event_artifacts,
            )

            settings = get_settings()
            snapshot_root = get_snapshot_root()
            if check_disk_space(snapshot_root, settings.perception.snapshot_min_free_disk_mb):
                clip_dids = save_event_artifacts(log_id, artifacts)
                clip_kinds = {
                    did: artifacts.clips[did][1]
                    for did in clip_dids
                    if did in artifacts.clips
                }
                has_trace = (snapshot_root / log_id / "omni_trace.json.gz").exists()

        # Persist on-demand query log (with artifact metadata)
        self._od_log_repo.append(
            OnDemandLogEntry(
                id=log_id,
                timestamp=t_start,
                query=request.query,
                answer=result.answer,
                sources=valid_dids,
                latency_ms=t_end - t_start,
                snapshot_count=len(clip_dids),
                clip_dids=clip_dids,
                clip_kinds=clip_kinds,
                has_trace=has_trace,
            )
        )

        # Map inference results back to API response items
        return OnDemandPerceptionResultItem(
            answer=result.answer,
            timestamp=ms_to_iso_local(t_end),
        )

    # ---- Perception logs ----

    def query_logs(
        self,
        after: str | None = None,
        before: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """Query perception logs.

        Args:
            after: ISO 8601 timestamp cursor — return entries after this time.
            before: ISO 8601 upper bound — return entries before this time.
            since: Relative time string like "1h", "30m", "2h30m".
            limit: Max entries to return. None means no limit.

        Returns:
            Dict with logs, count, and total_inferences.
        """
        from miloco.utils.time_utils import parse_iso_ms, since_to_ms

        after_ms: int | None = None
        before_ms: int | None = None
        since_ms: int | None = None

        if after:
            after_ms = parse_iso_ms(after, "after")

        if before:
            before_ms = parse_iso_ms(before, "before")

        if since and after_ms is None:
            since_ms = since_to_ms(since)

        logs, count = self._log_repo.query(
            after_ms=after_ms, before_ms=before_ms, since_ms=since_ms, limit=limit
        )

        return {
            "logs": logs,
            "count": count,
            "total_inferences": self._log_repo.get_today_inference_count(),
        }

    def cleanup_logs(self, keep_days: int) -> int:
        """清理过期感知日志。"""
        return self._log_repo.delete_before_days(keep_days)

    # ---- On-demand logs ----

    def query_on_demand_logs(
        self,
        since_ms: int | None = None,
        before_ms: int | None = None,
        before_id: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """Query on-demand perception query logs.

        Args:
            since_ms: Unix ms lower bound (inclusive).
            before_ms: Unix ms upper bound (exclusive).
            before_id: Compound cursor tiebreaker (used with before_ms).
            limit: Max entries to return.
        """
        logs, count = self._od_log_repo.query(
            since_ms=since_ms, before_ms=before_ms, before_id=before_id, limit=limit
        )

        return {"logs": logs, "count": count}

    def get_on_demand_log(self, log_id: str) -> dict | None:
        """Get a single on-demand log entry by ID."""
        return self._od_log_repo.get_by_id(log_id)

    def cleanup_on_demand_logs(self, keep_days: int) -> int:
        """清理过期主动查询日志。"""
        return self._od_log_repo.delete_before_days(keep_days)

    # ---- Device management ----

    async def get_devices(self, online_only: bool = True) -> list[PerceptionDevice]:
        """List all perception-capable devices across all adapter types.

        Args:
            online_only: If True (default), only return online devices.
                         If False, return all discovered devices.
        """
        devices: list[PerceptionDevice] = []

        for adapter in self._collector._adapters.values():
            try:
                # cap=False：列设备全集（含超出投喂上限的相机），用于 rule target
                # 校验等「枚举可选设备」语义，不受 MAX_ENABLED_CAMERAS 投喂上限收窄。
                discovered = await adapter.discover_devices(
                    online_only=online_only, cap=False
                )
                for did, source in discovered.items():
                    devices.append(source)
            except Exception as e:
                logger.error(
                    "[collect](adapter=%s) 发现设备失败 | %s",
                    adapter.device_type,
                    e,
                )

        return devices
