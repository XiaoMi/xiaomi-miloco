"""
Pipeline processor — orchestrates collect → omni → log → postprocess.

Provides two processing paths, both using collector.collect_batch():
- process_realtime(): batch collection + inference for continuous monitoring
- process_on_demand(): batch collection + on-demand query (multi-device fusion)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from miloco.config import get_settings
from miloco.database.perception_repo import PerceptionLogRepo
from miloco.node_monitor import Lifecycle, NodeName, get_monitor
from miloco.observability.aggregate import aggregate_cycle
from miloco.observability.context import (
    reset_trace_id,
    set_trace_id,
)
from miloco.observability.metrics_client import get_metrics_client
from miloco.observability.types import (
    DecodeTrace,
    DeviceTraceRecord,
    GateTrace,
    IdentityTrace,
    OmniTrace,
)
from miloco.perception.client import PerceptionEngineProxy
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.engine.types import RealtimePerceptionResult
from miloco.perception.schema import (
    PerceptionBatch,
    PerceptionLatency,
    PerceptionLogEntry,
)
from miloco.perception.types import OnDemandPerceptionResult

logger = logging.getLogger("perf")


def _ms_since(start: float) -> float:
    return (time.monotonic() - start) * 1000


# gate 阶段 pipeline 一个 device 写 5 个 key:gate_{did}_ms(总) +
# gate_video/audio_{did}_ms(子模态拆分) + gate_video/audio_{did}_pass(0/1 标志)。
# 直接用 startswith("gate_") 会把 5 个全加进 gate_ms,造成 ~2-3 倍虚高。
# regex 只匹配 device 级总耗时:gate_<single segment>_ms,排除子模态拆分和 pass 标志。
#
# 限制:[^_]+ 要求 device ID 不含下划线。当前命名规范(d1/did-a/纯字母数字+连字符)
# 不触发,如果未来 did 加下划线,这条只影响 [perf] 日志行的 gate_ms 聚合;SQLite
# per-device trace 走 f"{room}/gate_{did}_ms" 精确 key,不走正则,不受影响。
# did 改名时同步看一眼这条。
_GATE_TOTAL_RE = re.compile(r"^gate_[^_]+_ms$")


def _aggregate_stage_ms(timing: dict) -> tuple[float, float, float]:
    """从 result.timing 聚合 gate / identity / omni 每 cycle 耗时。

    key 形如 ``{room}/{stage}_{did}_ms``;以 ``/`` 拆出 suffix 再分类。
    gate 用 regex 排除子模态和 pass 标志,其余阶段单一 key 直接前缀匹配。

    **omni 取 max(不求和)**：omni 改并发(gather)后各相机并行,墙钟=最慢一路而非总和;
    取 max 与 cycle ``total`` / RTF(均墙钟)口径一致,避免 [perf] 出现 ``omni(Σ) > total``
    的误导。gate/identity 也并行,但 ms 级、sum≈max,保持 sum 不额外改。
    """
    gate_ms = identity_ms = omni_ms = 0.0
    for key, val in timing.items():
        if key.startswith("_"):
            continue  # proxy-injected,跳过
        suffix = key.rsplit("/", 1)[-1] if "/" in key else key
        if _GATE_TOTAL_RE.match(suffix):
            gate_ms += val
        elif suffix.startswith("identity_") and suffix.endswith("_ms"):
            identity_ms += val
        elif suffix.startswith("omni_") and suffix.endswith("_ms"):
            omni_ms = max(omni_ms, val)  # 并发:omni 墙钟取最慢一路
    return gate_ms, identity_ms, omni_ms


class PipelineProcessor:
    """Data flow pipeline: collect_batch → omni inference → log → postprocess."""

    def __init__(
        self,
        collector: MultimodalCollector,
        perception_engine_proxy: PerceptionEngineProxy,
        log_repo: PerceptionLogRepo,
    ):
        self._collector = collector
        self._perception_engine_proxy = perception_engine_proxy
        self._log_repo = log_repo
        self._last_latency: PerceptionLatency | None = None
        self._last_batch: PerceptionBatch | None = None
        # SSE 订阅者队列;由 events_router / metric stream 等通过 subscribe_sse() 注册
        self._sse_subscribers: list[asyncio.Queue] = []

        # tier_c 闲时定期清:把 collector 的"按 did 取最近一帧"接到引擎(gate 关停时 live 检测取帧)。
        self._perception_engine_proxy.set_tierc_frame_provider(self._collector.peek_latest_frame)

        settings = get_settings()
        self._perf_enabled: bool = settings.perf.enabled

    def try_reinit_engine(self, *, include_failed: bool = False) -> None:
        """补完前置条件后热重建引擎;非可恢复态幂等 no-op。

        每个 tick 入口调一次(默认 ``include_failed=False``,见 runner._tick):配好 key
        / 补完模型后下个推理周期自动转 ready。``runner.start`` 显式重启时传
        ``include_failed=True``,额外恢复 engine_init_failed。若本次刚转入 ready,必须
        重挂 tier_c frame provider:__init__ 时 engine 还是 None,当时
        set_tierc_frame_provider 是 no-op,不重挂则 gate 关停时的 live 检测取帧会丢。
        """
        if self._perception_engine_proxy.try_reinit(include_failed=include_failed):
            self._perception_engine_proxy.set_tierc_frame_provider(
                self._collector.peek_latest_frame
            )

    def set_inference_executor(self, executor: ThreadPoolExecutor) -> None:
        """Forward the inference executor to the engine proxy.

        Lifecycle: STARTING → READY (success) / FAILED (异常)。
        """
        mon = get_monitor()
        mon.set_lifecycle(NodeName.PROCESSOR, Lifecycle.STARTING)

        try:
            self._perception_engine_proxy.set_executor(executor)
        except Exception as e:
            state = mon.get_state(NodeName.PROCESSOR)
            if state and state.lifecycle == Lifecycle.STARTING:
                mon.set_lifecycle(NodeName.PROCESSOR, Lifecycle.FAILED, error=repr(e))
            raise

        state = mon.get_state(NodeName.PROCESSOR)
        if state and state.lifecycle == Lifecycle.STARTING:
            mon.set_lifecycle(NodeName.PROCESSOR, Lifecycle.READY)

    @property
    def engine_ready(self) -> bool:
        return self._perception_engine_proxy.ready

    @property
    def engine_status(self) -> str:
        return self._perception_engine_proxy.status

    @property
    def engine_status_message(self) -> str:
        return self._perception_engine_proxy.status_message

    @property
    def last_latency(self) -> PerceptionLatency | None:
        return self._last_latency

    @property
    def tier_u_pool(self):
        """暴露陌生人池给上层 (service / router) 用,封装 ``_perception_engine_proxy``
        私有字段,让外部访问全程走公开接口。

        链路:Pipeline.tier_u_pool → proxy.perception_engine (public field) →
        PerceptionEngine.get_tier_u_pool()。任一层启动失败返 None。
        """
        try:
            return self._perception_engine_proxy.perception_engine.get_tier_u_pool()
        except AttributeError:
            return None

    @property
    def deep_sort_config(self):
        """暴露 yaml-resolved DeepSortConfigDC 给 router 视频注册路径用。

        访问失败(engine 未初始化等)返代码默认值 ``DeepSortConfigDC()``——保证调用方
        总能拿到合法 dataclass,不用处理 None。
        """
        try:
            return self._perception_engine_proxy.perception_engine.get_deep_sort_config()
        except AttributeError:
            from miloco.perception.engine.config import DeepSortConfigDC
            return DeepSortConfigDC()

    def get_active_confirmed_track_keys(self) -> list[tuple[str, int]]:
        """所有 cam 上当前 confirmed track 的 ``(cam_id, track_id)`` 列表。

        给 router pool_fetch 用: 跟 confirmed track 实时 emb 做去重 (case b)。
        engine 未初始化时返空列表。
        """
        try:
            return self._perception_engine_proxy.perception_engine.get_active_confirmed_track_keys()
        except AttributeError:
            return []

    def get_reid_extractor(self):
        """从 PerceptionEngine 借 HumanReID 实例,给 IdentityLibrary 写盘兜底用。
        链路同 tier_u_pool;任一层启动失败 / 无活动 tracker 返 None。"""
        try:
            return self._perception_engine_proxy.perception_engine.get_reid_extractor()
        except AttributeError:
            return None

    async def close(self) -> None:
        """关闭底层 perception engine proxy(含 IdentityEngine dispatcher 等资源)。

        封装 ``_perception_engine_proxy.close()`` 私有访问,给 runner / service 等
        外部调用方提供 public close 入口。异常不在此层 swallow——由调用方决定是否吞掉
        (runner.shutdown 已有 try/except logger.error 包装)。
        """
        get_monitor().set_lifecycle(NodeName.PROCESSOR, Lifecycle.STOPPED)
        await self._perception_engine_proxy.close()

    async def stop_to_unconfigured(self) -> None:
        """软停底层引擎(删当前生效模型→回未配态),保留 tick 自愈循环。透传 proxy。"""
        await self._perception_engine_proxy.stop_to_unconfigured()

    @property
    def last_batch(self) -> PerceptionBatch | None:
        return self._last_batch

    def subscribe_sse(self) -> asyncio.Queue:
        """注册 SSE 订阅者;调用方负责 await q.get() 并在 finally 调 unsubscribe_sse 清理.

        events_router / 未来 metric stream 共用同一组订阅者,_publish 时全广播,
        消费端按 event_type 字段过滤(避免 fan-out 时多个队列各做一份).

        maxsize=256 上限防 OOM:慢消费(浏览器 tab 后台 throttle / 网络僵死)时,
        _publish 会撞 QueueFull 走 drop + log,而非无界增长.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._sse_subscribers.append(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue) -> None:
        self._sse_subscribers = [s for s in self._sse_subscribers if s is not q]

    def _publish(self, event_type: str, data: dict) -> None:
        """非阻塞广播给所有 SSE 订阅者;队列满时跳过该订阅(B11 非阻塞约束).

        与 _publish_trace / _publish_failed_trace 是两套不同的发布通道:
        - _publish_trace:写 observability traces SQLite
        - _publish:走 in-process asyncio.Queue 给 SSE 客户端
        """
        for q in self._sse_subscribers:
            try:
                q.put_nowait((event_type, data))
            except asyncio.QueueFull:
                logger.warning("SSE subscriber queue full, dropping %s", event_type)

    async def process_realtime(self) -> RealtimePerceptionResult | None | bool:
        """Realtime perception pipeline.

        1. Batch-collect all active devices via collector.collect_batch()
        2. Single batch inference via perception_engine_proxy.realtime_perceive()
        3. Store fused result to DB (with auto-dedup)
        4. Call postprocess hooks

        Returns:
            PerceptionLogEntry — successfully processed and logged.
            False — data was consumed but inference skipped/empty (caller
                    should continue draining remaining windows).
            None — no data available to process (buffer empty).
        """
        async with get_monitor().track_async(NodeName.PROCESSOR, "realtime") as _proc_h:
            return await self._process_realtime_inner(_proc_h)

    async def _process_realtime_inner(self, _proc_h=None) -> RealtimePerceptionResult | None | bool:
        t_cycle = time.monotonic()

        # 1. Drain all active devices into a batch (consuming from buffers)
        t = time.monotonic()
        batch = self._collector.collect_batch(drain=True)
        collect_ms = _ms_since(t)

        if batch.empty:
            # drain 循环退出的常态:每个 _tick 至少 1 次 empty 收尾。
            # skip() 让本次 track 不进 fps/p95/RTF 等滚动指标,否则空轮询会把
            # fps 推到与 _tick 频率挂钩、p95_latency 拉到 collect_batch 耗时量级,
            # 完全掩盖真实推理速率与延迟。
            if _proc_h is not None:
                _proc_h.skip_rolling()
            return None  # no ready windows — caller should stop

        self._last_batch = batch

        # 给整个 cycle 绑定 trace_id,cycle 内所有 publish_event / push_omni_trace 自动关联
        trace_id = str(uuid.uuid4())
        cycle_start_unix_ms = int(time.time() * 1000)
        trace_token = set_trace_id(trace_id)

        try:
            start_dt = datetime.fromtimestamp(
                batch.start_timestamp / 1000, tz=timezone.utc
            ).astimezone()
            end_dt = datetime.fromtimestamp(
                batch.end_timestamp / 1000, tz=timezone.utc
            ).astimezone()

            in_delay_s = (datetime.now().astimezone() - end_dt).total_seconds()

            # 2. Single batch inference call (convert + gate + edge + omni)
            # 旁路收集 omni 拿到的 clip 字节 + omni HTTP trace 给 meaningful_events 复用.
            # 因为 omni 推理跑在 inference thread executor 里(asyncio.run 起新 loop,
            # ContextVar 不跨线程),artifacts 必须**显式作为入参透传**给
            # realtime_perceive,让它在 inference 线程的新 loop 入口重新 set ContextVar.
            # artifacts.clips value 是 (bytes, kind) — kind ∈ {"mp4","m4a"} 区分视频/
            # audio-only 路径,持久化层据此选 clip.mp4 / clip.m4a 扩展名 + Content-Type.
            # artifacts.trace 由 omni HTTP 调用 finally 填入,随 clip 一起落到 event_dir.
            from miloco.perception.snapshot_context import OmniEventArtifacts

            artifacts = OmniEventArtifacts()
            try:
                result, early_sent_contents, early_sent_rule_ids, early_sent_sugg_ids = await self._perception_engine_proxy.realtime_perceive(
                    batch, artifacts=artifacts
                )
            except Exception as e:
                logger.error("[processor] 实时感知失败 | %s", e, exc_info=True)
                # 系统级失败也要留 trace,否则 dashboard 上 cycle 数缺失,问题不可见。
                # cycle_error_msg 标记"这是异常 cycle",前端列表可红字提示。
                if self._perf_enabled:
                    self._publish_failed_trace(
                        trace_id=trace_id,
                        cycle_start_unix_ms=cycle_start_unix_ms,
                        batch=batch,
                        in_delay_s=in_delay_s,
                        collect_ms=collect_ms,
                        t_cycle=t_cycle,
                        exc=e,
                    )
                return False  # data consumed but failed — caller should continue

            if not result:
                return False  # data consumed but skipped — caller should continue

            # 3. Build log entry and store (skip if all rooms were gated)
            log_ms = 0.0
            if not result.skipped:
                t = time.monotonic()
                entry = PerceptionLogEntry(
                    id=str(uuid.uuid4()),
                    timestamp=batch.captured_at,
                    descriptions={c.room_name: c.description for c in result.caption},
                )
                self._log_repo.append(entry)

                log_ms = _ms_since(t)

            # 4. Postprocess (handle_realtime_perception_result checks skipped internally)
            # clip 复用 omni 产出:artifacts.clips 已由 omni 内部
            # (_encode_video_mp4 / _encode_audio_only_mp4)字节级 push 填好,**零重编**.
            # 视频路径 mp4 = H264+AAC;audio-only 路径 mp4 = 纯 AAC m4a 容器.
            # engine 异常 / 全 device gate skipped → artifacts.clips 为空,device_ids=[] →
            # _persist 仍入表(metadata-only)给 UI 显示语义提示,但不落盘.
            # 同 batch 内空字节的 device 被过滤掉(payload[0] 真值判).
            if result.skipped:
                artifacts.clips.clear()
            else:
                artifacts.clips = {
                    did: payload
                    for did, payload in artifacts.clips.items()
                    if payload[0]
                }
            device_ids = list(artifacts.clips.keys())

            await self._perception_engine_proxy.handle_realtime_perception_result(
                result,
                early_sent_contents=early_sent_contents,
                early_sent_rule_ids=early_sent_rule_ids,
                early_sent_sugg_ids=early_sent_sugg_ids,
                device_ids=device_ids,
                artifacts=artifacts,
            )

            # --- Assemble latency report from result.timing ---
            if self._perf_enabled:
                cycle_total_ms = _ms_since(t_cycle)

                timing = result.timing or {}
                gate_ms, identity_ms, omni_ms = _aggregate_stage_ms(timing)

                out_delay_s = (datetime.now().astimezone() - end_dt).total_seconds()

                stream_lag_ms = 0.0
                if batch.window_first_frame_recv_ms is not None:
                    stream_lag_ms = max(
                        0.0,
                        float(batch.end_timestamp - batch.window_first_frame_recv_ms),
                    )

                timing_detail = {k: v for k, v in timing.items() if not k.startswith("_")}
                if batch.video_frame_count:
                    timing_detail["decode_video_ms"] = batch.decode_video_avg_ms
                if batch.audio_frame_count:
                    timing_detail["decode_audio_ms"] = batch.decode_audio_avg_ms

                latency = PerceptionLatency(
                    in_delay_ms=in_delay_s * 1000,
                    out_delay_ms=out_delay_s * 1000,
                    decode_ms=batch.decode_avg_ms,
                    collect_ms=collect_ms,
                    log_ms=log_ms,
                    cycle_total_ms=cycle_total_ms,
                    convert_ms=timing.get("_convert_ms", 0.0),
                    gate_ms=gate_ms,
                    identity_ms=identity_ms,
                    omni_ms=omni_ms,
                    pipeline_total_ms=timing.get("_pipeline_total_ms", 0.0),
                    window_duration_ms=timing.get("_window_duration_ms", 0.0),
                    stream_lag_ms=stream_lag_ms,
                    device_count=int(timing.get("_device_count", batch.device_count)),
                    skipped=result.skipped,
                    timestamp=time.time() * 1000,
                    timing_detail=timing_detail or None,
                )
                self._last_latency = latency

                # 各层帧率(pipeline=下发=tracker 帧率, omni 解耦后独立);engine 未就绪时显 0
                pipeline_fps = identity_fps = omni_fps = 0
                try:
                    _in_cfg = self._perception_engine_proxy.perception_engine.get_input_config()
                    pipeline_fps = identity_fps = _in_cfg.fps
                    omni_fps = _in_cfg.omni_fps
                except Exception:
                    pass

                logger.info(
                    (
                        "[perf] window=[%s-%s] in_delay=%.1fs out_delay=%.1fs | "
                        "devices=%d total=%.1fms RTF=%.3f | "
                        "pipeline=%dfps identity=%dfps omni=%dfps | "
                        "gate=%.1fms identity=%.1fms omni=%.1fms | "
                        "decode=%.1fms collect=%.1fms convert=%.1fms log=%.1fms"
                    ),
                    start_dt.strftime("%H:%M:%S"),
                    end_dt.strftime("%H:%M:%S"),
                    in_delay_s,
                    out_delay_s,
                    latency.device_count,
                    latency.cycle_total_ms,
                    latency.rtf,
                    pipeline_fps,
                    identity_fps,
                    omni_fps,
                    latency.gate_ms,
                    latency.identity_ms,
                    latency.omni_ms,
                    latency.decode_ms,
                    latency.collect_ms,
                    latency.convert_ms,
                    latency.log_ms,
                )

                self._publish_trace(
                    trace_id=trace_id,
                    cycle_start_unix_ms=cycle_start_unix_ms,
                    batch=batch,
                    latency=latency,
                    timing=timing,
                    stream_lag_ms=stream_lag_ms,
                    error_code=result.error_code,
                )

            # Report window duration for node monitor RTF calculation
            if _proc_h is not None and batch.end_timestamp and batch.start_timestamp:
                _proc_h.add_window_ms(batch.end_timestamp - batch.start_timestamp)
            # Set engine sub-stage detail for diagnosis
            if result and result.timing:
                _g, _i, _o = _aggregate_stage_ms(result.timing)
                get_monitor().set_detail(
                    NodeName.ENGINE, gate_ms=_g, identity_ms=_i, omni_ms=_o,
                )

            return result
        finally:
            reset_trace_id(trace_token)

    def _publish_trace(
        self,
        *,
        trace_id: str,
        cycle_start_unix_ms: int,
        batch: PerceptionBatch,
        latency: PerceptionLatency,
        timing: dict,
        stream_lag_ms: float,
        error_code: str | None = None,
        cycle_error_msg: str | None = None,
    ) -> None:
        """从 timing dict 还原 per-device records,聚合后入异步队列。

        cycle_error_msg 非空(系统异常路径):显式置 cycle.skipped=False,区分
        "gate 正常跳过(全部 device 没通过 gate)" 和 "后端炸了 timing 全空"。
        否则 stats.AVG(skipped) 的 skip_rate 会被系统异常 trace 污染。
        """
        client = get_metrics_client()
        if client is None:
            return

        device_records: list[DeviceTraceRecord] = []
        for did, dd in batch.devices.items():
            # api._merge_results 把每个 room 的 timing 加上 "{room_name}/" 前缀避免同名冲突。
            room = dd.meta.room_name or did
            gv = float(timing.get(f"{room}/gate_video_{did}_ms", 0.0))
            ga = float(timing.get(f"{room}/gate_audio_{did}_ms", 0.0))
            gvp = bool(timing.get(f"{room}/gate_video_{did}_pass", 0))
            gap = bool(timing.get(f"{room}/gate_audio_{did}_pass", 0))
            ghp = bool(timing.get(f"{room}/gate_hold_{did}_pass", 0))
            # hold-only 窗口(gvp=False, gap=False, ghp=True)实际跑过 identity + omni,
            # 不能误判成 skip 否则 identity_ms/omni_ms/has_agent 等观测全部置 None。
            gate_skipped = not (gvp or gap or ghp)

            identity_ms = float(timing.get(f"{room}/identity_{did}_ms", 0.0))
            omni_ms = float(timing.get(f"{room}/omni_{did}_ms", 0.0))
            # partial:单相机 omni 失败时 _run_device 把 OmniError.code(形如
            # "HTTPStatusError:429" / "ReadTimeout")存进 _omni_error_{did}(顶层 _ 前缀,
            # 不加 room)。cycle 级 error_code(整 batch 早死)优先;否则用它给该相机 trace
            # 记 omni error,使 omni_error_count +1。错误详情在 pipeline 的 logger.warning 里。
            per_dev_omni_err = timing.get(f"_omni_error_{did}")

            # 复用 pipeline 在 set_device_context 之前生成的同一把 UUID,
            # 让 traces_device 行有稳定 device_trace_id。gate 全失败导致 timing
            # 整体缺失时 fallback 新 UUID 保证 PRIMARY KEY 非空。
            dt_raw = timing.get(f"_device_trace_id_{did}")
            dt_id = dt_raw if isinstance(dt_raw, str) and dt_raw else str(uuid.uuid4())

            # gate 真实评估的打分。pipeline 正常路径下两个 key 都有值;
            # on-demand bypass / 系统异常 fallback 路径 timing 缺这两个 key,
            # 这里读到 None 后写库落 NULL,P50-P99 视图过滤掉。
            gv_raw = timing.get(f"_gate_video_score_{did}")
            ga_raw = timing.get(f"_gate_audio_energy_{did}")
            gate_video_score = float(gv_raw) if isinstance(gv_raw, (int, float)) else None
            gate_audio_energy = float(ga_raw) if isinstance(ga_raw, (int, float)) else None

            device_records.append(DeviceTraceRecord(
                device_trace_id=dt_id,
                cycle_id=trace_id,
                timestamp=int(dd.window_start_unix_ms or cycle_start_unix_ms),
                device_id=did,
                room_name=room,
                decode=DecodeTrace(
                    video_avg_ms=dd.decode_video_avg_ms,
                    audio_avg_ms=dd.decode_audio_avg_ms,
                    video_frame_count=len(dd.video),
                    audio_frame_count=len(dd.audio),
                ),
                gate=GateTrace(
                    ms=gv + ga, video_ms=gv, audio_ms=ga,
                    video_pass=gvp, audio_pass=gap, skipped=gate_skipped,
                    video_score=gate_video_score, audio_energy=gate_audio_energy,
                    hold_pass=ghp,
                ),
                identity=None if gate_skipped else IdentityTrace(ms=identity_ms),
                # error_code 时,即使 gate timing 缺失(整 batch 早死)也算"omni 被尝试且失败",
                # 让 omni_error_count 在 aggregate 里 +1。
                omni=(
                    OmniTrace(ms=omni_ms, error_code=error_code)
                    if error_code is not None
                    else (
                        OmniTrace(ms=omni_ms, error_code=str(per_dev_omni_err))
                        if per_dev_omni_err
                        else (None if gate_skipped else OmniTrace(ms=omni_ms))
                    )
                ),
                dropped_windows_count=dd.dropped_windows,
                overflow_count=dd.overflow_count,
                max_buffer_depth=dd.max_buffer_depth,
                last_overflow_action=dd.last_overflow_action,
            ))

        cycle_meta = dict(
            trace_id=trace_id,
            timestamp=cycle_start_unix_ms,
            in_delay_ms=latency.in_delay_ms,
            out_delay_ms=latency.out_delay_ms,
            decode_ms=latency.decode_ms,
            collect_ms=latency.collect_ms,
            convert_ms=latency.convert_ms,
            log_ms=latency.log_ms,
            cycle_total_ms=latency.cycle_total_ms,
            pipeline_total_ms=latency.pipeline_total_ms,
            window_duration_ms=latency.window_duration_ms,
            window_first_frame_recv_ms=batch.window_first_frame_recv_ms,
            stream_lag_ms=stream_lag_ms,
            timing_detail=latency.timing_detail,
            cycle_error_msg=cycle_error_msg,
        )
        cycle = aggregate_cycle(device_records, cycle_meta)
        if cycle_error_msg:
            cycle.skipped = False
        client.publish_trace(cycle, device_records)

    def _publish_failed_trace(
        self,
        *,
        trace_id: str,
        cycle_start_unix_ms: int,
        batch: PerceptionBatch,
        in_delay_s: float,
        collect_ms: float,
        t_cycle: float,
        exc: BaseException,
    ) -> None:
        """非 OmniError 异常路径下的最小 trace,只填能算出的字段。

        gate/identity/omni/convert 等没跑到的阶段都是 0;skipped=False
        以便区分"gate skip 正常跳过"和"系统异常"。
        cycle_error_msg 只存"类型 + 首行 + 截断到 160 字符"的短摘要让 dashboard
        一眼能认出哪条挂了;完整 traceback 已通过 exc_info 写 log,db 不当 log 用。
        """
        _MAX_LEN = 160
        head = f"{type(exc).__name__}: {exc}".splitlines()[0]
        error_msg = head[:_MAX_LEN - 3] + "..." if len(head) > _MAX_LEN else head
        stream_lag_ms = 0.0
        if batch.window_first_frame_recv_ms is not None:
            stream_lag_ms = max(
                0.0,
                float(batch.end_timestamp - batch.window_first_frame_recv_ms),
            )
        window_duration_ms = float(batch.end_timestamp - batch.start_timestamp)
        latency = PerceptionLatency(
            in_delay_ms=in_delay_s * 1000,
            decode_ms=batch.decode_avg_ms,
            collect_ms=collect_ms,
            cycle_total_ms=_ms_since(t_cycle),
            window_duration_ms=window_duration_ms,
            stream_lag_ms=stream_lag_ms,
            device_count=batch.device_count,
            skipped=False,
            timestamp=time.time() * 1000,
        )
        self._publish_trace(
            trace_id=trace_id,
            cycle_start_unix_ms=cycle_start_unix_ms,
            batch=batch,
            latency=latency,
            timing={},
            stream_lag_ms=stream_lag_ms,
            cycle_error_msg=error_msg,
        )

    async def process_on_demand(
        self, dids: list[str] | None, query: str
    ) -> OnDemandPerceptionResult | None:
        """Active perception pipeline — multi-device batch query.

        1. Batch-collect specified devices via collector.collect_batch(dids)
        2. Run perception_engine_proxy.on_demand_perceive(batch, query) for fusion inference
        3. Return answer dict mapping source key to answer string
        """
        async with get_monitor().track_async(NodeName.PROCESSOR, "on_demand") as _proc_h:
            # Peek without consuming — realtime pipeline still needs this data
            batch = self._collector.collect_batch(dids, drain=False)

            if batch.empty:
                logger.warning("[collect](device=%s) 无可用数据源(skipped)", dids)
                _proc_h.skip_rolling()
                return None

            if batch.end_timestamp and batch.start_timestamp:
                _proc_h.add_window_ms(batch.end_timestamp - batch.start_timestamp)

            try:
                return await self._perception_engine_proxy.on_demand_perceive(batch, query)
            except Exception as e:
                logger.error("[processor] 主动查询感知失败 | %s", e, exc_info=True)
                return None
