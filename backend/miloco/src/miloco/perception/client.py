"""
PerceptionEngineProxy — real perception inference via perception-engine pipeline.

Bridges miloco's PerceptionBatch (PyAV decoded frames) to the perception-engine's
full Gate → Edge (Tracker) → Omni pipeline, converting data formats efficiently
and mapping PipelineResult back to the dict[str, str] interface.

CPU-bound inference (frame convert, Gate, Edge) and async I/O (Omni HTTP) run
in a dedicated inference thread so the main event loop stays free for stream
frame ingestion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from miloco.config import get_settings
from miloco.dispatch import dispatch_event
from miloco.node_monitor import Lifecycle, NodeName, get_monitor
from miloco.observability.context import (
    get_trace_id,
    reset_trace_id,
    set_trace_id,
)
from miloco.observability.metrics_client import get_metrics_client
from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.config import InputConfig
from miloco.perception.engine.omni.omni_client import OmniError, resolve_omni_api_key
from miloco.perception.event_text_builder import (
    build_speeches_text,
    build_suggestions_text,
    caption_for_dids,
)
from miloco.perception.schema import PerceptionBatch
from miloco.perception.snapshot_context import (
    ClipKind,
    OmniEventArtifacts,
    event_artifacts_scope,
)
from miloco.perception.types import (
    CaptionEntry,
    MatchedRule,
    OnDemandPerceptionResult,
    RealtimePerceptionResult,
    Speech,
    Suggestion,
    suggestion_intra_priority,
)


def _attach_caption(
    items: list[Suggestion] | list[Speech],
    captions: list[CaptionEntry],
) -> None:
    for item in items:
        if not item.caption and item.source_device_ids:
            item.caption = caption_for_dids(captions, item.source_device_ids)


def _publish_perception_event(event_type: str, source: str, payload: dict) -> None:
    client = get_metrics_client()
    if client is None:
        return
    client.publish_event(event_type=event_type, source=source, payload=payload)


if TYPE_CHECKING:
    from miloco.perception.types import BatchedSnapshot

logger = logging.getLogger(__name__)

# 模块级强引用持有 _persist_meaningful_event 后台任务,防 asyncio 只持弱引用导致
# 任务运行中被 GC 回收(CPython 文档明确警告).done_callback 在任务结束时自动 discard.
_PERSIST_BG_TASKS: set[asyncio.Task] = set()


def _filter_voice_enabled(speeches: list[Speech]) -> list[Speech]:
    """按摄像头「拾音白名单」过滤 speech：``source_device_ids[0]``(相机 did)在
    白名单里(拾音已开启)的放行,其余丢弃。**默认关**:不在白名单 = 拾音关闭。
    实时读 KV(进程内缓存),改开关即时生效、无需重启感知引擎。

    分层防线:**第一道**在引擎入口——``engine/api.py::_strip_unauthorized_voice_audio``
    对未开启拾音的相机整批剥离音频(不进 gate/omni,不转写、无语音派生 suggestion、
    不烧音频 token),正常情况下这些相机的 speech 根本不会产生。本函数是**第二道**
    (引擎入口剥离失效 / 旧窗口残留时兜底),两个执法点:① 语音指令 dispatch(早出
    _on_early_speeches + 终态 handle_realtime_perception_result);
    ② meaningful_events 落库/SSE(_persist_meaningful_event 在 classify 前过滤)
    ——拾音关闭 = 不执行也不记录转写。
    规则匹配及 caption / suggestion 等视觉产物不经此函数,不受影响。读 KV 失败时
    **fail-closed**(丢弃全部语音):默认关语义下,宁可漏掉一次语音,也不处理用户
    未授权相机的音频。
    """
    from miloco.manager import get_manager
    from miloco.miot.filter import voice_allowed_camera_dids

    try:
        voice_allowed = voice_allowed_camera_dids(get_manager().kv_repo)
    except Exception as e:
        logger.warning("voice allow-list lookup failed, dropping all speeches (fail-closed): %s", e)
        return []
    kept: list[Speech] = []
    for s in speeches:
        did = s.source_device_ids[0] if s.source_device_ids else None
        if did is None or did not in voice_allowed:
            logger.info(
                "speech 被摄像头声音开关拦截丢弃(未开启拾音,不下发/不落库): did=%s device_name=%s content_len=%d",
                did, s.device_name, len(s.content),
            )
            continue
        kept.append(s)
    return kept


def _ms_since(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _filter_completed_event_rules(
    rules: list[dict],
) -> tuple[list[dict], list[str]]:
    """剔除 event mode 中关联 task 当前活跃期 record 已「本周期达标」的 rule。

    判据（任一）：

    - record.status == 'completed'（oneshot 终态）
    - progress recurring + current >= target（如每日 N 杯水当天喝够后静默）
    - duration recurring + accumulated >= target_minutes * 60

    state mode 不过滤（剔除会让 ENTERED→EXITED 翻转、取消 on_exit 设备动作；
    state 路径靠 rule engine ``_target_fired`` runtime 做周期达标静默）。
    无 record 的 event rule 保留（维持现状）。

    返回 (kept_rules, skipped_task_ids)。skipped_task_ids 按 task 去重后排序，
    供调用方做去重打印。
    """
    event_task_ids = {
        r["task_id"] for r in rules if r.get("mode") == "event" and r.get("task_id")
    }
    if not event_task_ids:
        return rules, []

    from miloco.database.connector import get_db_connector
    from miloco.task_record.repo import (
        fetch_active_record_satisfaction_by_task_ids,
    )

    with get_db_connector().get_connection() as conn:
        satisfaction_map = fetch_active_record_satisfaction_by_task_ids(
            conn.cursor(), list(event_task_ids)
        )

    kept: list[dict] = []
    skipped: set[str] = set()
    for r in rules:
        tid = r.get("task_id")
        if r.get("mode") == "event" and satisfaction_map.get(tid):
            skipped.add(tid)
            continue
        kept.append(r)
    return kept, sorted(skipped)


async def _run_with_trace_id(
    trace_id: str | None,
    coro,
    artifacts: OmniEventArtifacts | None = None,
):
    """新 event loop 入口:把主线程的 trace_id / artifacts set 回当前 Context.

    ContextVar 不跨线程边界 — run_in_executor 在新线程里 asyncio.run() 起新 loop,
    主线程的 ContextVar 值全部 reset 成 default.必须显式抓主线程值,在新 loop 入口
    重新 set,omni 内部才能拿到.
    """
    if artifacts is not None:
        cm = event_artifacts_scope(artifacts)
    else:
        from contextlib import nullcontext
        cm = nullcontext()

    if trace_id is None:
        with cm:
            return await coro
    token = set_trace_id(trace_id)
    try:
        with cm:
            return await coro
    finally:
        reset_trace_id(token)


class PerceptionEngineProxy:
    """Real perception proxy backed by perception-engine pipeline.

    Converts miloco DeviceData (PyAV frames) → engine InputSlice (numpy),
    runs the full Gate→Edge→Omni pipeline per device, and returns aggregated
    scene descriptions.
    """

    def __init__(self):
        # 基础状态初始化
        self.perception_engine: PerceptionEngine | None = None
        self._status: str = "not_initialized"
        self._status_message: str = ""
        self._last_captions: dict[str, str] = {}
        self._executor: ThreadPoolExecutor | None = None
        # 软停(stop_to_unconfigured)与在飞 perceive 互斥:teardown 必等当前推理完成,
        # 持锁期间进来的 perceive 在 if not ready 守卫处安全跳过 → 杜绝 use-after-close。
        self._engine_lock = asyncio.Lock()

        self._init_engine()

    def _init_engine(self) -> None:
        """校验资源(key / 模型) + 创建引擎。``__init__`` 与 ``try_reinit()`` 共用。

        缺前置条件时置对应 ``_status`` + lifecycle ``PREREQ_MISSING`` 并提前返回;
        成功时置 ``_status='ready'`` + lifecycle ``READY``。重入安全(reinit 复用)。
        """
        from miloco.perception.engine.resource_validator import (
            EngineReadiness,
            validate_resources,
        )

        settings = get_settings()
        engine_cfg = settings.perception.engine

        omni_kwargs = dict(engine_cfg.get("omni", {}))
        omni_api_key = resolve_omni_api_key(omni_kwargs.get("api_key", ""))

        identity_kwargs = dict(engine_cfg.get("identity", {}))
        models_dir = identity_kwargs.get("perception_model_dir") or str(
            settings.directories.models_dir
        )

        mon = get_monitor()
        validation = validate_resources(omni_api_key, models_dir)

        if validation.status == EngineReadiness.MODELS_MISSING:
            self._status = "models_missing"
            self._status_message = validation.message
            logger.warning("感知引擎不可用: %s", self._status_message)
            mon.set_lifecycle(NodeName.ENGINE, Lifecycle.PREREQ_MISSING, error=self._status_message)
            return

        if validation.status == EngineReadiness.NOT_CONFIGURED:
            self._status = "no_omni_api_key"
            self._status_message = "多模态大模型 API Key 未配置"
            logger.warning("感知引擎不可用: %s", self._status_message)
            mon.set_lifecycle(NodeName.ENGINE, Lifecycle.PREREQ_MISSING, error=self._status_message)
            return

        # READY — 正常创建引擎。STARTING 仅在确认要构造引擎(validate 通过)时才标:
        # tick-driven reinit 在等外部条件态(缺 key / 模型未下完)走不到这里,失败回到
        # 同一 PREREQ_MISSING,set_lifecycle 对同态(old==life)不 emit,故每 tick 零
        # event_log 噪声——无需在 try_reinit 再写一份与 validate_resources 重复的 cheap check。
        mon.set_lifecycle(NodeName.ENGINE, Lifecycle.STARTING)
        try:
            self.perception_engine = self._create_engine(
                engine_cfg, omni_kwargs, identity_kwargs, models_dir
            )
            self._status = "ready"
            self._status_message = ""  # reinit 成功时清掉上一轮的 "未配置" 残留消息
            mon.set_lifecycle(NodeName.ENGINE, Lifecycle.READY)
        except Exception as e:
            self._status = "engine_init_failed"
            self._status_message = f"引擎创建异常: {e}"
            logger.error("感知引擎创建失败: %s", e)
            mon.set_lifecycle(NodeName.ENGINE, Lifecycle.FAILED, error=str(e))

    # tick-driven 自愈放行的"等外部条件"态:validate 廉价(缺 key 零 IO、缺模型仅
    # stat),失败回到同一 PREREQ_MISSING、_init_engine 不翻 lifecycle → 每 tick 零
    # event_log 噪声,可安全地每个推理 tick 轮询。
    _TICK_RECOVERABLE = ("no_omni_api_key", "models_missing")
    # 显式重启(runner.start)额外放行 engine_init_failed:构造失败原因不可 cheap 判定,
    # validate 会通过而每 tick 重跑重型 _create_engine 会阻塞 event loop,故不纳入 tick
    # 自愈,只靠「重启感知」按钮重建一次。
    _RESTART_RECOVERABLE = _TICK_RECOVERABLE + ("engine_init_failed",)

    def try_reinit(self, *, include_failed: bool = False) -> bool:
        """补完前置条件后无需重启进程即可重建引擎。

        默认(``include_failed=False``,tick-driven 自愈,见 ``runner._tick``)只放行
        廉价"等外部条件"态:缺 key(``no_omni_api_key``)、模型未下完(``models_missing``)
        ——validate 廉价且失败不翻 lifecycle,可每 tick 轮询,配好 key / 下完模型后下个
        推理周期自动转 ready。

        ``include_failed=True``(「重启感知」经 ``runner.start`` 调)额外放行
        ``engine_init_failed``:引擎构造失败(如临时磁盘满)补救后靠按钮重建一次,不每
        tick 自动重试——重型 ``_create_engine`` 每 tick 跑会阻塞 event loop。

        已 ``ready`` / ``not_initialized`` 直接返回 ``False``(no-op,不碰已有引擎实例)。
        成功重建时 ``_init_engine`` 已把 lifecycle 翻到 ``READY``——``set_executor`` 守卫
        只认 ``STOPPED`` 不会帮翻,故必须在创建路径里显式置(``_init_engine`` 已做)。
        返回是否「本次转入 ready」。
        """
        allowed = self._RESTART_RECOVERABLE if include_failed else self._TICK_RECOVERABLE
        if self._status not in allowed:
            return False
        self._init_engine()
        return self._status == "ready"

    @property
    def ready(self) -> bool:
        return self.perception_engine is not None

    @property
    def status(self) -> str:
        return self._status

    @property
    def status_message(self) -> str:
        return self._status_message

    def _create_engine(
        self, engine_cfg: dict, omni_kwargs: dict, identity_kwargs: dict, models_dir: str
    ) -> PerceptionEngine:
        """构建 PerceptionConfig 并创建引擎实例。"""
        from miloco.perception.engine.config import (
            GateConfig,
            IdentityConfig,
            OmniConfig,
            PerceptionConfig,
        )
        from miloco.perception.engine.identity.config_loader import (
            load_identity_engine_config,
        )

        if not identity_kwargs.get("perception_model_dir"):
            identity_kwargs["perception_model_dir"] = models_dir

        identity_engine_cfg = load_identity_engine_config(
            override=engine_cfg.get("identity_engine"),
        )

        config = PerceptionConfig(
            input=InputConfig(**engine_cfg.get("input", {})),
            gate=GateConfig(**engine_cfg.get("gate", {})),
            identity=IdentityConfig(**identity_kwargs),
            omni=OmniConfig(**omni_kwargs),
            identity_engine=identity_engine_cfg,
        )

        return PerceptionEngine(config=config)

    def set_executor(self, executor: ThreadPoolExecutor) -> None:
        """Attach inference thread executor (called by engine at startup).

        Lifecycle: 仅 STOPPED → READY (stop_engine 后的热重启场景)。
        __init__ 已把 ENGINE 设过 READY/FAILED;FAILED 通常是永久性的
        (模型缺失/API key 没配),不应被 set_executor 误唤醒回 READY。
        """
        self._executor = executor
        mon = get_monitor()
        state = mon.get_state(NodeName.ENGINE)
        if state and state.lifecycle == Lifecycle.STOPPED and self.perception_engine is not None:
            mon.set_lifecycle(NodeName.ENGINE, Lifecycle.READY)

    def set_tierc_frame_provider(self, provider) -> None:
        """透传"按 did 取最近一帧"回调给底层引擎(tier_c 定期清 live 检测用)。"""
        if self.perception_engine is not None:
            self.perception_engine.set_tierc_frame_provider(provider)

    async def close(self) -> None:
        """Close engine resources (e.g., IdentityEngine dispatcher worker)."""
        if self.perception_engine is None:
            return  # PREREQ_MISSING / FAILED — nothing to stop, preserve lifecycle
        get_monitor().set_lifecycle(NodeName.ENGINE, Lifecycle.STOPPED)
        try:
            await self.perception_engine.close()
        except AttributeError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error("[engine] 关闭引擎 proxy 失败 | %s", e)

    async def rebuild(self) -> None:
        """无条件销毁当前引擎实例并按最新配置重建（运行时改感知参数后生效用）。

        与 ``stop_to_unconfigured`` 不同：不依赖 key 被删的降级语义；与 ``try_reinit``
        不同：不受 ``ready`` 守卫限制，``ready`` 态也强制重造。调用方须保证 settings
        缓存已刷新（PUT config 已 ``reset_settings()``），使 ``_init_engine`` 读到新值
        （omni_fps 等 cache 在 ``_create_engine`` 的配置）。
        """
        async with self._engine_lock:
            if self.perception_engine is not None:
                await self.close()
                self.perception_engine = None
            self._init_engine()

    async def stop_to_unconfigured(self) -> None:
        """软停引擎,回到「未配模型」态——与「启用→tick 自愈拉起」对称的反向操作。

        删除当前生效模型后调:关掉正在跑的引擎实例并把状态降回 ``no_omni_api_key``,
        但**不碰** runner 的 tick 循环。后续配好新模型并启用时,下个推理周期
        ``try_reinit`` 会自动重建(与初始未配模型态完全一致)。``realtime_perceive``
        入口的 ``if not self.ready`` 守卫保证降级后 tick 安全跳过、不崩。

        重入安全:引擎未起(``perception_engine is None``)时跳过 close,仅按当前配置重判。
        """
        async with self._engine_lock:  # 与在飞 perceive 互斥,teardown 必等其完成
            if self.perception_engine is not None:
                await self.close()
                self.perception_engine = None  # ready→False,tick 的 realtime_perceive 立即跳过
            # 按当前(删后已清空 key 的)配置重判:落 no_omni_api_key;万一 key 仍在则重建为 ready。
            self._init_engine()

    # ---- Internal impls (run in inference thread) ----

    async def _realtime_perceive_impl(
        self,
        batched_snapshot: BatchedSnapshot,
        rules: list[dict],
        device_count: int,
        convert_ms: float,
        main_loop: asyncio.AbstractEventLoop,
        skipped_task_ids: list[str],
    ) -> tuple[RealtimePerceptionResult | None, set[str], set[tuple[str, str]], set[int]]:
        """Actual realtime perceive logic — runs in the inference thread.

        Receives an already-converted BatchedSnapshot (numpy-only) so this
        thread never touches PyAV frame objects owned by the main thread.

        Returns (result, early_sent_contents, early_sent_rule_ids, early_sent_sugg_ids)
        where each set tracks items already dispatched via streaming callbacks.
        early_sent_rule_ids 装 (rule_id, did) 对——per-device 状态机粒度,同一 rule 在
        cam_A early 命中后,cam_B 终态又命中应当照常打 True(不同桶),所以去重必须带 did。
        early_sent_sugg_ids 记 per-omni 早送过的 suggestion 事件链 id：merge 已把这些新链
        保留进 result.suggestions（供 dump/上下文），发送侧据此跳过、防对 Agent 重发。
        """
        assert self.perception_engine is not None
        early_sent_contents: set[str] = set()
        early_sent_rule_ids: set[tuple[str, str]] = set()
        early_sent_sugg_ids: set[int] = set()

        # 当 self._executor is not None 时，本协程跑在 inference 线程的临时
        # loop 上（asyncio.run 创建的）。engine 在此处 await callback 后，
        # callback 内部任何 asyncio.create_task(...) 都会挂在临时 loop 上，
        # asyncio.run 退出时会被 cancel —— 即使 caller 持有强引用也救不回来
        # （问题不是 GC 是 loop 关闭）。把 callback 派发回主 loop 后，副作用
        # （如 RuleRunner._spawn_fire）创建的 task 才有稳定的执行环境。
        def _on_main_loop(coro_fn):
            async def wrapped(*args, **kwargs):
                if asyncio.get_running_loop() is main_loop:
                    return await coro_fn(*args, **kwargs)
                fut = asyncio.run_coroutine_threadsafe(
                    coro_fn(*args, **kwargs), main_loop
                )
                return await asyncio.wrap_future(fut)

            return wrapped

        @_on_main_loop
        async def _on_early_speeches(speeches: list[Speech]) -> None:
            commands = [
                i for i in speeches if i.needs_response and i.is_complete
            ]
            # 按摄像头语音开关闸门:被拉黑的相机语音指令不 dispatch(实时读 KV)。
            commands = _filter_voice_enabled(commands)
            if not commands:
                return
            for c in commands:
                early_sent_contents.add(c.content)
                _publish_perception_event(
                    "interaction", c.speaker,
                    {"content": c.content, "room_name": c.room_name},
                )
            # B2 单源真值:文本构造延迟到 drainer,producer 投递条目 + builder 引用
            await dispatch_event("interaction", commands, build_speeches_text)

        @_on_main_loop
        async def _on_early_matched_rules(rules: list[MatchedRule]) -> None:
            from miloco.manager import get_manager

            svc = get_manager().rule_service
            for r in rules:
                # source_did 取真 did(pipeline.py:321 注入,单元素列表);异常态空列表
                # 兜底 "perception",保留 fallback 行为不抛 IndexError。
                did = r.source_device_ids[0] if r.source_device_ids else "perception"
                early_sent_rule_ids.add((r.rule_id, did))
                _publish_perception_event(
                    "rule_match", r.rule_id, {"reason": r.reason},
                )
                await svc.update_state(
                    r.rule_id, did, True, r.reason,
                    trigger_room=r.room_name,
                    trigger_dids=r.source_device_ids,
                    caption="", device_name=r.device_name,
                )

        @_on_main_loop
        async def _on_early_suggestions(suggestions: list[Suggestion]) -> None:
            # 这里收到的已是经事件链闸门过滤后的「新链」suggestion——心跳/重复在
            # pipeline 层（_wrap_suggestions_cb → assign_id_and_update_link）已抑制。
            # 剔除 engine 内部字段（id）后外发。
            for s in suggestions:
                if s.id is not None:
                    early_sent_sugg_ids.add(s.id)  # 终态 merge 会把同一新链保留进 result，发送侧据此跳过
                _publish_perception_event(
                    "suggestion", s.event, {"action": s.action},
                )
            # B2 单源真值:文本构造延迟到 drainer；urgency 仅作淘汰用的条目级优先级
            await dispatch_event(
                "suggestion", suggestions, build_suggestions_text,
                intra_priority=suggestion_intra_priority(suggestions),
            )

        # --- Pipeline timing ---
        t = time.monotonic()
        try:
            result = await self.perception_engine.realtime_perceive(
                batched_snapshot,
                rules,
                on_early_speeches=_on_early_speeches,
                on_early_matched_rules=_on_early_matched_rules,
                on_early_suggestions=_on_early_suggestions,
            )
        except OmniError as e:
            # 兜底分支:主路径 run_batch_pipeline 已在 _run_device 内逐相机吞掉 OmniError
            # (partial 模式、返回 skipped、不上抛,见 pipeline._run_device),故此处通常不触发,
            # 仅防 merge / 其它阶段意外抛 OmniError。返回带 error_code 的占位 result,让
            # processor._publish_trace 把 omni_error_count +1;skipped=True 阻止 log/sse/postprocess
            # 跑空数据;e.code 保留具体异常类型(ReadTimeout / ConnectError 等)。
            # 注意:partial_timing 在 batch 并发路径已不再填充(恒 None),仅兼容旧抛出方。
            logger.error("[omni] omni 阶段失败 | %s", e, exc_info=True)
            result = RealtimePerceptionResult(
                skipped=True,
                error_code=e.code,
                timing=e.partial_timing,
            )
        # 其他 pipeline 阶段失败(gate / identity / convert / postprocess 等)不在此处接,
        # 让异常往上冒到 processor 的 except Exception,只 log 不算进 omni_error_count,
        # 避免虚高 omni 错误率。将来需要按阶段细分错误率时再加 cycle_error_count 等指标。

        pipeline_ms = _ms_since(t)

        if result:
            # Inject proxy-level timing into result.timing (prefixed with _ to
            # distinguish from engine-internal keys).
            timing = result.timing or {}
            timing["_convert_ms"] = convert_ms
            timing["_pipeline_total_ms"] = pipeline_ms
            timing["_device_count"] = device_count
            # Window duration = max span across all snapshots
            timing["_window_duration_ms"] = max(
                (
                    s.end_timestamp - s.start_timestamp
                    for s in batched_snapshot.snapshots
                ),
                default=0.0,
            )
            result.timing = timing

            if not result.skipped:
                logger.info(
                    "✅ realtime_perceive: %s | skipped_task_ids=%s",
                    result.model_dump_json(ensure_ascii=False),
                    skipped_task_ids,
                )

        return (
            result,
            early_sent_contents,
            early_sent_rule_ids,
            early_sent_sugg_ids,
        )

    async def _on_demand_perceive_impl(
        self, batched_snapshot: BatchedSnapshot, query: str
    ) -> OnDemandPerceptionResult | None:
        """Actual on-demand perceive logic — runs in the inference thread."""
        assert self.perception_engine is not None
        try:
            result = await self.perception_engine.on_demand_perceive(
                batched_snapshot, query
            )
        except Exception as e:
            logger.error("[pipeline] 引擎管线失败 | %s", e, exc_info=True)
            result = None

        if result:
            logger.info(
                "🔥 on_demand_perceive: %s", result.model_dump_json(ensure_ascii=False)
            )

        return result

    # ---- Public interface (dispatches to inference thread) ----

    async def realtime_perceive(
        self, batch: PerceptionBatch,
        artifacts: OmniEventArtifacts | None = None,
    ) -> tuple[RealtimePerceptionResult | None, set[str], set[tuple[str, str]], set[int]]:
        """Run full engine pipeline — offloaded to inference thread.

        Returns (result, early_sent_contents, early_sent_rule_ids, early_sent_sugg_ids)
        for dedup in post-processing.

        artifacts: 可选;若非 None,inference 线程 omni 内部产出的 clip 字节
        会按 device_id 写入 artifacts.clips,omni HTTP 调用 trace 会累积到
        artifacts.trace.调用方负责创建 OmniEventArtifacts() 传入(ContextVar 不跨
        executor 线程,只能显式透传 reference).
        """
        # _engine_lock:与 stop_to_unconfigured 互斥,持锁期间引擎不会被 teardown 拔掉。
        async with get_monitor().track_async(NodeName.ENGINE, "perceive") as _eng_h, self._engine_lock:
            if not self.ready:
                _eng_h.skip_rolling()
                return None, set(), set(), set()

            from miloco.manager import get_manager

            rules = await get_manager().rule_service.get_all_rules(enabled_only=True)
            rules = [rule.model_dump() for rule in rules]
            rules, skipped_task_ids = _filter_completed_event_rules(rules)

            device_count = sum(1 for d in batch.devices.values() if d.has_data)

            # Convert PyAV frames → numpy ON THE MAIN THREAD so the inference
            # thread never touches PyAV objects created by the decoder thread.
            # This avoids cross-thread FFmpeg access that causes EAGAIN / libx264 errors.
            t = time.monotonic()
            batched_snapshot = batch.to_batched_snapshot()
            convert_ms = _ms_since(t)

            if batched_snapshot is None:
                _eng_h.skip_rolling()
                return None, set(), set(), set()

            if batch.end_timestamp and batch.start_timestamp:
                _eng_h.add_window_ms(batch.end_timestamp - batch.start_timestamp)

            main_loop = asyncio.get_running_loop()
            # 把持久 app loop 注入 PerceptionEngine→各 identity engine, 供 tier_c 写库协程
            # run_coroutine_threadsafe 调度(脱离下方 asyncio.run 起的每窗临时 loop, 否则
            # 写库协程会在窗末被 cancel, 候选永远写不进)。
            if self.perception_engine is not None:
                self.perception_engine.set_main_loop(main_loop)
            # inference 线程通过 asyncio.run() 起新 loop,ContextVar 不跨 loop。
            # 显式抓主线程的 trace_id / artifacts,在新 loop 入口 set 回去,
            # 保证 omni / publish_event / push_clip_bytes / push_omni_trace 能拿到。
            trace_id = get_trace_id()
            if self._executor is not None:
                return await main_loop.run_in_executor(
                    self._executor,
                    lambda: asyncio.run(
                        _run_with_trace_id(
                            trace_id,
                            self._realtime_perceive_impl(
                                batched_snapshot,
                                rules,
                                device_count,
                                convert_ms,
                                main_loop,
                                skipped_task_ids,
                            ),
                            artifacts=artifacts,
                        )
                    ),
                )
            # 单线程路径(无 executor,测试 / runner 启动前的短窗口):processor 只传
            # artifacts 不开 scope,这里手动开,保证 omni 内部 push_clip_bytes /
            # push_omni_trace 能命中.executor 路径由上面 _run_with_trace_id 开,两条路径都覆盖.
            if artifacts is not None:
                with event_artifacts_scope(artifacts):
                    return await self._realtime_perceive_impl(
                        batched_snapshot,
                        rules,
                        device_count,
                        convert_ms,
                        main_loop,
                        skipped_task_ids,
                    )
            return await self._realtime_perceive_impl(
                batched_snapshot,
                rules,
                device_count,
                convert_ms,
                main_loop,
                skipped_task_ids,
            )

    async def on_demand_perceive(
        self, batch: PerceptionBatch, query: str
    ) -> OnDemandPerceptionResult | None:
        """Run on-demand query pipeline — offloaded to inference thread."""
        async with get_monitor().track_async(NodeName.ENGINE, "on_demand") as _eng_h, self._engine_lock:
            if not self.ready:
                _eng_h.skip_rolling()
                return None

            # Convert PyAV frames → numpy on main thread (same reason as realtime).
            batched_snapshot = batch.to_batched_snapshot()

            if batched_snapshot is None:
                _eng_h.skip_rolling()
                return None

            if batch.end_timestamp and batch.start_timestamp:
                _eng_h.add_window_ms(batch.end_timestamp - batch.start_timestamp)

            if self._executor is not None:
                loop = asyncio.get_running_loop()
                trace_id = get_trace_id()
                return await loop.run_in_executor(
                    self._executor,
                    lambda: asyncio.run(
                        _run_with_trace_id(
                            trace_id,
                            self._on_demand_perceive_impl(batched_snapshot, query),
                        )
                    ),
                )

            return await self._on_demand_perceive_impl(batched_snapshot, query)

    async def handle_realtime_perception_result(
        self,
        result: RealtimePerceptionResult,
        early_sent_contents: set[str] | None = None,
        early_sent_rule_ids: set[tuple[str, str]] | None = None,
        early_sent_sugg_ids: set[int] | None = None,
        device_ids: list[str] | None = None,
        artifacts: OmniEventArtifacts | None = None,
    ):
        """Handle realtime perception result — runs on main loop.

        device_ids / artifacts 由 processor 透传;给 _persist_meaningful_event
        入 meaningful_events 表 + 落 clip + omni_trace 用.artifacts=None 时跳过
        persist(单元测试早期路径 / runner 未启动 等场景).

        artifacts.clips value 形态为 `(bytes, ClipKind)`,kind ∈ {"mp4","m4a"} 决定
        落盘扩展名 + SSE 推 kind.artifacts.trace 由 omni HTTP 调用 finally 填入,
        随 clip 一起落到 event_dir.
        """
        if result.skipped:
            return

        # T6: meaningful_events 后台异步持久化 — 不阻塞下面 webhook 主路径(B4 / B11).
        # 失败仅 log,不抛.classify / device_ids 空 / artifacts 空等所有
        # 降级路径都在 _persist 内自处理.
        # 任务必须挂 _PERSIST_BG_TASKS 强引用,否则 asyncio 弱引用模型下 GC 可能在
        # 任务完成前回收 → 偶发"INSERT 没落库 / SSE 不推" 难复现.
        if artifacts is not None:
            task = asyncio.create_task(
                _persist_meaningful_event(
                    result=result,
                    device_ids=device_ids or [],
                    artifacts=artifacts,
                )
            )
            _PERSIST_BG_TASKS.add(task)
            task.add_done_callback(_PERSIST_BG_TASKS.discard)

        from miloco.manager import get_manager

        # handle matched rules via update_state (skip early-sent ones)
        # 去重粒度从 rule_id 改为 (rule_id, did):同 rule 在 cam_A early 命中后,cam_B
        # 终态又命中应当照常打 True(不同桶),不能被 early 误吃。
        svc = get_manager().rule_service
        cycle_source_states_by_rule: dict[str, dict[str, bool]] = {}
        for did, rule_ids in result.device_rule_map.items():
            for rule_id in rule_ids:
                cycle_source_states_by_rule.setdefault(rule_id, {})[did] = False
        for matched_rule in result.matched_rules:
            did = (
                matched_rule.source_device_ids[0]
                if matched_rule.source_device_ids
                else "perception"
            )
            cycle_source_states_by_rule.setdefault(matched_rule.rule_id, {})[did] = True
        if early_sent_rule_ids:
            for rule_id, did in early_sent_rule_ids:
                cycle_source_states_by_rule.setdefault(rule_id, {})[did] = True

        for matched_rule in result.matched_rules:
            did = matched_rule.source_device_ids[0] if matched_rule.source_device_ids else "perception"
            if early_sent_rule_ids and (matched_rule.rule_id, did) in early_sent_rule_ids:
                continue
            _publish_perception_event(
                "rule_match", matched_rule.rule_id, {"reason": matched_rule.reason},
            )
            await svc.update_state(
                matched_rule.rule_id, did, True, matched_rule.reason,
                trigger_room=matched_rule.room_name,
                trigger_dids=matched_rule.source_device_ids,
                caption=caption_for_dids(result.caption, matched_rule.source_device_ids),
                device_name=matched_rule.device_name,
                cycle_source_states=cycle_source_states_by_rule.get(
                    matched_rule.rule_id
                ),
            )

        # 对本 batch 实际下发过、但未命中的 (rule_id, did) 喂 update_state(False)。
        # frame-driven 模式:runner 帧级抗抖(_pending_source_exit)需要"持续 F"才能完成
        # 第二帧确认,所以未命中也要每 cycle 喂 F,不能 edge-driven 只在 matched→unmatched
        # 翻转时调一次。
        # per-device 精确广播:device_rule_map[did] 就是该 device 实际进过 omni prompt 的
        # rule 列表 — 只对这些组合喂 False。rule 绑 cam_A 时若本 batch 只有 cam_B,
        # rule 根本没下发 → 不会出现在 device_rule_map 任何 did 的列表里 → 状态保持上一帧。
        # device_rule_map 空(OmniError 兜底)→ 本 cycle 不做任何状态机推退。
        matched_pairs: set[tuple[str, str]] = {
            (r.rule_id, r.source_device_ids[0] if r.source_device_ids else "perception")
            for r in result.matched_rules
        }
        if early_sent_rule_ids:
            matched_pairs |= early_sent_rule_ids

        enabled_set = set(svc.get_enabled_rule_ids())
        for did, rule_ids in result.device_rule_map.items():
            for rule_id in rule_ids:
                if (rule_id, did) in matched_pairs:
                    continue
                # 防 race:下发后 rule 在 cycle 内被 disable
                if rule_id not in enabled_set:
                    continue
                await svc.update_state(
                    rule_id,
                    did,
                    False,
                    cycle_source_states=cycle_source_states_by_rule.get(rule_id),
                )

        # result.suggestions 含本窗全部「新链」（dump/上下文已完整）。per-omni 下这些新链
        # 已在 _on_early_suggestions 逐相机早送过（id 记入 early_sent_sugg_ids）——此处据此
        # 跳过、避免对 Agent 重发；batch 模式无早送（集合为空）→ 全量上报。
        pending_suggestions = [
            s for s in result.suggestions
            if not (early_sent_sugg_ids and s.id in early_sent_sugg_ids)
        ]
        if pending_suggestions:
            _attach_caption(pending_suggestions, result.caption)
            for s in pending_suggestions:
                _publish_perception_event(
                    "suggestion", s.event, {"action": s.action},
                )
            # B2 单源真值:文本构造延迟到 drainer；urgency 仅作淘汰用的条目级优先级
            await dispatch_event(
                "suggestion", pending_suggestions, build_suggestions_text,
                intra_priority=suggestion_intra_priority(pending_suggestions),
            )

        # handle speeches (skip those already sent via streaming early callback)
        speeches: list[Speech] = []
        for interaction in result.speeches:
            if interaction.needs_response and interaction.is_complete:
                if early_sent_contents and interaction.content in early_sent_contents:
                    continue
                speeches.append(interaction)
        # 按摄像头语音开关闸门:被拉黑的相机语音指令不 dispatch(实时读 KV)。
        speeches = _filter_voice_enabled(speeches)
        if speeches:
            _attach_caption(speeches, result.caption)
            for it in speeches:
                _publish_perception_event(
                    "interaction", it.speaker,
                    {"content": it.content, "room_name": it.room_name},
                )
            # B2 单源真值:文本构造延迟到 drainer(builder 二次过滤对已过滤列表 idempotent)
            await dispatch_event("interaction", speeches, build_speeches_text)


# ─── meaningful_events 后台持久化(异步,不阻塞 webhook 主路径)───────────


async def _persist_meaningful_event(
    *,
    result: RealtimePerceptionResult,
    device_ids: list[str],
    artifacts: OmniEventArtifacts,
) -> None:
    """后台异步入 meaningful_events 表 + 落 event artifacts + 推 SSE.

    流程:
      0. 语音黑名单过滤 speech(与 dispatch 同一闸门)→ 语音关闭相机的转写既不
         入分类判定也不落库/推 SSE;caption / suggestion / 规则命中照常
      1. classify(result) → 任一 has_* 为真才入表(纯 caption / 仅闲聊不入表)
      2. 反查 rule_names(rule_service 查 name;rule 已删 / 异常跳过该条)
      3. INSERT meaningful_events(snapshot_count=0)
      4. 落盘 artifacts(clip + omni_trace,写前预检磁盘 < snapshot_min_free_disk_mb 跳过)→
         update_snapshot_count(成功 device 数,trace 不计入)
      5. _publish_meaningful_event(B13:metadata-only 也推 SSE)

    clip 字节是 omni 内部 push 出来的字节级 mp4(零重编),video 路径 H264+AAC,
    audio-only 路径 m4a.omni trace 含 prompt / response / latency / usage / error,
    用于复盘 LLM 决策.snapshot_count 字段语义复用为"成功落盘 clip 的 device 数".

    任何异常仅 error log,不抛(B4 / B11 非阻塞约束).
    """
    from miloco.database.meaningful_events_dao import MeaningfulEventDao  # noqa: F401
    from miloco.manager import get_manager
    from miloco.perception.event_classifier import classify
    from miloco.perception.event_text_builder import build_agent_text
    from miloco.perception.snapshot_writer import (
        check_disk_space,
        get_snapshot_root,
        save_event_artifacts,
    )

    try:
        # 语音关闭相机的转写不落库:与 dispatch 同一闸门先滤 speech,classify /
        # payload / text / SSE 全部基于过滤后的视图——语音开关 = 不执行也不记录。
        # 只滤 speech,同相机的 caption / suggestion / 规则命中照常(开关只管语音,
        # 不管相机感知)。result 与主路径(规则匹配 / speech dispatch)共享,不可原地
        # 改 → model_copy 浅拷贝换 speeches 列表。
        result = result.model_copy(
            update={"speeches": _filter_voice_enabled(result.speeches)}
        )
        cls = classify(result)
        if not cls["is_meaningful"]:
            return

        mgr = get_manager()
        dao = mgr.meaningful_events_dao
        event_id = str(uuid.uuid4())
        timestamp_ms = int(time.time() * 1000)
        # timing 已被 observability traces 消费,DB 里这份是冗余副本
        payload_dict = result.model_dump()
        payload_dict.pop("timing", None)
        payload_json = json.dumps(payload_dict, ensure_ascii=False)

        # 反查 rule_names:让 DB.text 与 webhook 文本里 rule 段渲染为
        # {"rule_name":<name>, "reason":...} 跟 suggestions JSON 风格统一
        # (没找到 rule_name 时 fallback 用 rule_id).
        rule_names: dict[str, str] = {}
        rule_queries: dict[str, str] = {}
        if result.matched_rules:
            for mr in result.matched_rules:
                try:
                    rule = await mgr.rule_service.get_rule(mr.rule_id)
                    if rule:
                        if rule.name:
                            rule_names[mr.rule_id] = rule.name
                        rule_queries[mr.rule_id] = rule.condition.query
                except Exception:  # noqa: BLE001
                    pass

        text = build_agent_text(result, rule_names=rule_names, rule_queries=rule_queries)

        insert_ok = dao.insert(
            event_id=event_id,
            timestamp=timestamp_ms,
            text=text,
            payload_json=payload_json,
            has_rule_hit=cls["has_rule_hit"],
            has_suggestion=cls["has_suggestion"],
            has_asr=cls["has_asr"],
            device_ids=device_ids,
            snapshot_count=0,
            rule_names=rule_names,
        )
        if not insert_ok:
            logger.error("meaningful_events insert failed for %s", event_id)
            return  # INSERT 失败不继续

        # 落盘 event artifacts — 可能因 clips/trace 都缺失 / 磁盘紧张提前 return,
        # 此时 count 保持 0;不论哪种降级,row 都已 INSERT,SSE 应该推(否则前端
        # 实时收不到 metadata-only 事件).
        count = 0
        if artifacts.clips or artifacts.trace is not None or artifacts.gallery:
            settings = get_settings()
            snapshot_root = get_snapshot_root()
            if not check_disk_space(
                snapshot_root, settings.perception.snapshot_min_free_disk_mb
            ):
                logger.error(
                    "snapshot disk low (< %d MB free), skip save for event %s",
                    settings.perception.snapshot_min_free_disk_mb,
                    event_id,
                )
                # count 留 0,继续走 publish
            else:
                count = save_event_artifacts(event_id, artifacts)
                if count > 0:
                    dao.update_snapshot_count(event_id, count)
        else:
            logger.debug("no artifacts for event %s, snapshot_count stays 0", event_id)

        # 从 artifacts.clips 取 clip_kind:同 batch 要么全 video 要么全 audio-only
        # (_is_audio_only 是 batch 级共识,见 prompt_builder._is_audio_only),
        # 取第一个 device 的 kind 即代表整批.count == 0 时 kind 留 None
        # (metadata-only / 磁盘紧张 → 没落盘).
        clip_kind: ClipKind | None = None
        if count > 0 and artifacts.clips:
            clip_kind = next(iter(artifacts.clips.values()))[1]

        # B13 SSE 推送:只要 row 入表了就推,不论 count==0 还是 >0.
        # 落盘完成后 publish,snapshot_count 是真实值,clip_kind 帮 UI 区分 🎬/🎤.
        has_trace = (get_snapshot_root() / event_id / "omni_trace.json.gz").exists()

        try:
            _publish_meaningful_event(
                event_id=event_id,
                timestamp=timestamp_ms,
                text=text,
                has_rule_hit=cls["has_rule_hit"],
                has_suggestion=cls["has_suggestion"],
                has_asr=cls["has_asr"],
                snapshot_count=count,
                device_ids=device_ids,
                rule_names=rule_names,
                clip_kind=clip_kind,
                has_trace=has_trace,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("SSE publish failed for event %s: %s", event_id, e)

    except Exception as e:  # noqa: BLE001
        logger.error("_persist_meaningful_event failed: %s", e, exc_info=True)


def _publish_meaningful_event(
    *,
    event_id: str,
    timestamp: int,
    text: str,
    has_rule_hit: bool,
    has_suggestion: bool,
    has_asr: bool,
    snapshot_count: int,
    device_ids: list[str],
    rule_names: dict[str, str] | None = None,
    clip_kind: str | None = None,
    has_trace: bool = False,
) -> None:
    """通过 processor._publish 推送 meaningful_event SSE 帧.

    payload 字段与 /api/events list 元素同形,前端 EventSource 收到后直接拼到列表顶部.
    pipeline 不可用时(测试 / 引擎未起)静默跳过.

    clip_kind ∈ {"mp4","m4a",None}:UI 区分 🎬 视频 / 🎤 音频事件 / 无回放占位.
    """
    from miloco.manager import get_manager

    try:
        processor = get_manager().perception_service._pipeline
    except AttributeError:
        return

    payload = {
        "event_id": event_id,
        "timestamp": timestamp,
        "text": text,
        "has_rule_hit": has_rule_hit,
        "has_suggestion": has_suggestion,
        "has_asr": has_asr,
        "snapshot_count": snapshot_count,
        "device_ids": device_ids,
        "rule_names": rule_names or {},
        "clip_kind": clip_kind,
        "has_trace": has_trace,
    }
    processor._publish("meaningful_event", payload)
