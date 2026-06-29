"""
PerceptionEngine — real perception inference via perception-engine pipeline.

Bridges BatchedSnapshot to the perception-engine's full Gate → Edge → Omni
pipeline, with rule filtering per room and structured output.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.identity.tier_u import cam_id_from_device_id
from miloco.perception.engine_base import BasePerceptionEngine
from miloco.perception.types import (
    URGENCY_RANK,
    AudioFrame,
    AudioStream,
    BatchedSnapshot,
    MatchedRule,
    OnDemandPerceptionResult,
    RealtimePerceptionResult,
    Speech,
    Suggestion,
)

if TYPE_CHECKING:
    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.engine.identity.engine import IdentityEngine
    from miloco.perception.engine.identity.tracking_service import TrackingService
    from miloco.perception.engine.types import BatchPipelineResult, OmniContext

logger = logging.getLogger(__name__)

_PENDING_SPEECH_TIMEOUT_SEC = 30

# 启动失败重试间隔（秒）：last_fail_ts 超过此值后允许再尝试创建 engine
_ENGINE_RETRY_INTERVAL_SEC = 600.0

# === suggestion 事件链表参数 ===（URGENCY_RANK 已上移至 perception.types,单一真值）
# COOLDOWN_SEC 同时是「同一持续事件的再提醒间隔」与「链条无心跳后的淘汰 TTL」。
# urgency 只决定这个间隔的长短：越紧急催得越勤（high 1min / medium 2min / low 5min）。
# 复报只看「距上次上报是否过了当前 urgency 的冷却」——见 assign_id_and_update_link。
COOLDOWN_SEC: dict[str, float] = {"high": 60.0, "medium": 120.0, "low": 300.0}
MAX_EID: int = 999

# suggestion 事件链匹配阈值：本轮 event 与已有链 event 的句向量余弦 ≥ 此值即判为
# 同一桩持续事件（续接、心跳抑制）。0.70 在 6/9 真实 trace 上校准（精度优先，
# per-device 分桶下精度更高）。
SUGG_SIM_THRESHOLD: float = 0.70


def _ms_since(start: float) -> float:
    return (time.monotonic() - start) * 1000


class PerceptionEngine(BasePerceptionEngine):
    """Real perception proxy backed by perception-engine batch pipeline.

    Converts miloco PerceptionBatch → BatchedSnapshot, runs the batch
    Gate→Edge→Omni pipeline (per-room merging), and returns aggregated
    scene descriptions.
    """

    def __init__(
        self,
        config: PerceptionConfig | None = None,
    ):
        from miloco.perception.engine.config import PerceptionConfig
        from miloco.perception.engine.identity.engine import build_identity_library

        self._config = config or PerceptionConfig()

        # ============ tracking_service 构造参数缓存（懒加载 factory 复用）============
        self._tracking_mode = self._config.identity.tracking_service_mode
        # 把 IdentityEngineConfig.sort 转成 sort.py 的 SortConfig（duck typing：字段同名）
        # 让 SortTracker 拿到 yaml 里的 max_age_sec / n_init / 等用户层配置
        from miloco.perception.engine.identity.sort import (
            SortConfig as _RuntimeSortConfig,
        )
        sort_dc = self._config.identity_engine.sort
        self._runtime_sort_cfg = _RuntimeSortConfig(
            n_init=sort_dc.n_init,
            max_age_sec=sort_dc.max_age_sec,
            iou_threshold=sort_dc.iou_threshold,
            detector_conf_threshold=sort_dc.detector_conf_threshold,
            track_human_only=sort_dc.track_human_only,
        )
        # 构造 tracking_service kwargs:公共参数(model_dir / use_gpu / input_size / fps
        # 等)所有非 mock 模式都需要;mode-specific 参数(sort_config / deep_sort_config)
        # 各模式只接自己那一份。
        #
        # 历史 bug:原来用 ``mode in ("real", "fast", "detect_only")`` 的 if 条件,
        # deep_sort 落到 else 分支拿空 dict —— yaml 里 perception_model_dir /
        # use_gpu / fps + identity_engine.deep_sort 段 9 字段全部 silent drop,
        # DeepSortTrackingService 用默认参数构造,自定义部署直接失效。
        if self._tracking_mode == "mock":
            self._tracking_service_kwargs = {}
        else:
            common_kwargs = {
                "model_dir": self._config.identity.perception_model_dir or None,
                "use_gpu": self._config.identity.perception_use_gpu,
                "input_width": self._config.identity.perception_input_width,
                "input_height": self._config.identity.perception_input_height,
                "fps": self._config.input.fps,   # SortTracker 用 fps 算 max_age_sec → 帧数
            }
            if self._tracking_mode == "deep_sort":
                self._tracking_service_kwargs = {
                    **common_kwargs,
                    "deep_sort_config": self._config.identity_engine.deep_sort,
                }
            else:  # real / fast / detect_only
                self._tracking_service_kwargs = {
                    **common_kwargs,
                    "sort_config": self._runtime_sort_cfg,
                }

        # ============ per-camera 多实例化 ============
        # 设计原因：进程级单实例 SortTracker 时，所有镜头的 frame 喂同一个 tracker，
        # 跨镜头 detection 永远 IoU 不匹配 → 反复推高 time_since_update → track 寿命
        # 被压到 1/N（N=活跃镜头数）。单实例 IdentityEngine 也会把多镜头的 track 状态
        # 混在一个 _states 字典里。改成 per-camera 多实例化后，状态天然按镜头隔离。
        #
        # 懒加载：__init__ 时不知道有哪些 device，第一次见到新 did 时按需创建。
        # 不主动 GC：device 数量天然有限（< 10），单 engine state ~几十 KB，临时离线
        # 后会回来，清掉再建反而要重热 composite cache。
        self._tracking_services: dict[str, "TrackingService"] = {}
        self._identity_engines: dict[str, "IdentityEngine | None"] = {}
        # 持久 app event loop(由 client 层 set_main_loop 注入), 透传给各 identity engine,
        # 供 tier_c 写库协程 run_coroutine_threadsafe 调度(脱离每窗临时 loop, 防窗末被 cancel)。
        self._main_loop: "asyncio.AbstractEventLoop | None" = None
        # 启动失败兜底：value=None 的 device 上次失败的时刻；超过 _ENGINE_RETRY_INTERVAL
        # 后下次访问会重新尝试创建。避免临时性问题（磁盘满、文件锁竞争）必须重启进程才能恢复。
        self._engine_fail_ts: dict[str, float] = {}

        # 持有 fire-and-forget 的清理任务,防止被 GC 在执行完前回收(Python 文档
        # 推荐;event loop 自身只持弱引用)。任务完成后 done_callback 自动 discard。
        # 用途:start() 失败时的半启动清理(_get_or_create_identity_engine 异常路径)。
        self._pending_close_tasks: set[asyncio.Task] = set()

        # device 在所在 room 内的序号映射：用于拼 scope_label="<room>-dev<idx>"
        # 排序策略：首次见到的 device 拿 dev0，下一个拿 dev1，序号按出现顺序固定下来
        # （后续 reset_session 不打乱；只有进程重启才重新分配）。
        self._room_device_index: dict[str, list[str]] = {}

        # IdentityLibrary 全局一份，per-camera engine 共享：composite L1/L2 cache、
        # tier_a/tier_c 写盘、list_persons / get_name / get_role 同源。unknown 编号空间则
        # **不**共享——按 scope_label 拼前缀保证跨镜头唯一，engine 内部各自计数。
        ie_cfg = self._config.identity_engine
        self._identity_lib = None
        if ie_cfg.enabled:
            try:
                self._identity_lib = build_identity_library()
            except Exception as e:  # noqa: BLE001
                logger.error("IdentityLibrary 启动失败：%s（pipeline 退化为无 identity 模式）", e)
                self._identity_lib = None

        # TierUPool 全局一份(v1.2 主动注册改造,PR 7 接入):所有 per-camera engine
        # 共享一个池(陌生人池本身就跨 cam 维护 cluster)。
        #
        # ReIDProvider 注入策略:启动时先用一个空 dict 构造 DeepSortReIDProvider,
        # 把 dict 同时持给 self._deep_sort_trackers——后续 _get_or_create_tracking_service
        # 创建 deep_sort 服务时往 dict 里加一项(键 = device_id, 与 CropEntry.cam_id
        # 一致, v2 重构后 cam_id 统一改用米家 device_id),provider 通过 dict 引用透明
        # 看到新 tracker。
        #
        # tracking_service_mode 非 deep_sort(mock/real/fast/detect_only)时 dict 永远
        # 为空,provider.get_embedding 全 None,池退化为"只累积、不去重"——fetch
        # 仍能拿到近期 crop,适合演示场景但 cluster 永远 singleton。
        from miloco.perception.engine.identity.tier_u import (
            DeepSortReIDProvider,
            TierUConfig,
            TierUPool,
        )
        self._tier_u_pool = None
        # device_id -> DeepSortTracker(v2 重构后 cam_id == device_id);由
        # _get_or_create_tracking_service 填充
        self._deep_sort_trackers: dict[str, object] = {}
        # get_reid_extractor 兜底用:摄像头掉线时懒加载独立 HumanReID 实例,
        # 让注册流程(上传图/视频/池抽样)的 .npy 写盘不被"摄像头是否在线"绑死。
        self._fallback_human_reid: object | None = None
        if ie_cfg.enabled:
            try:
                self._tier_u_pool = TierUPool(
                    config=TierUConfig(),
                    reid_provider=DeepSortReIDProvider(self._deep_sort_trackers),
                )
            except Exception as e:  # noqa: BLE001
                logger.error("TierUPool 启动失败:%s(注册流程仍走 from-media 路径)", e)
                self._tier_u_pool = None

        # Per-device 状态：与 per-camera omni 调用粒度对齐。同 room 多镜头各自独立的
        # 上次描述 / 上次建议 / 未完成语音 —— 不能共享一份，否则 cam-B 看到的"上次场景"
        # 会是 cam-A 上次说的内容。audio_tail 同理：原 per-room 版在多 device 循环里
        # 反复覆写同一个 room key，最后只保留 last device 的 tail（隐性 bug），改 per-device
        # 后每个镜头自己 overlap 自己。
        #
        # TODO(continuity, cross-camera-context): per-device 隔离 last_caption 的代价是
        # 跨镜头场景**连续性丢失**——人从客厅走到卧室，卧室 cam-B 拿不到客厅 cam-A 的
        # "上次场景"。caption 会描述为"卧室出现一个人"而非"妈妈刚从客厅来"。若业务方
        # 反馈连续性需求，可加 room-level 上下文摘要（汇总同 room 所有 device 的最新
        # caption 作为额外参考段）。
        self._last_captions: dict[str, str] = {}                    # key: device_id
        # Per-device 事件链表：device_id -> {eid: {"event","action","urgency","last_ts","embedding"}}
        # urgency 单调不降级（only-up），故单字段即可，不再区分 current/max
        self._sugg_table: dict[str, dict[int, dict]] = {}           # key: device_id

        # suggestion 语义去重的句向量编码器；缺模型/依赖时降级为精确文本匹配，不影响主流程。
        self._embedder = None
        try:
            from miloco.config import get_settings
            from miloco.perception.engine.omni.dedup_embedder import EventEmbedder
            self._embedder = EventEmbedder(get_settings().directories.models_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "EventEmbedder 初始化失败，suggestion 去重降级为精确文本匹配：%s", e
            )
        self._next_sugg_id: dict[str, int] = {}                     # key: device_id
        self._pending_speech: dict[str, list[dict]] = {}            # key: device_id
        self._pending_speech_rounds: dict[str, int] = {}            # key: device_id
        self._max_pending_speech_rounds = max(1, _PENDING_SPEECH_TIMEOUT_SEC // self._config.input.period_sec)
        self._audio_tail: dict[str, NDArray[np.int16]] = {}         # key: device_id
        # 上一窗口末次被检帧（gate 预处理后的 448 灰度），visual gate 跨窗口比较用
        self._gate_prev_frames: dict[str, NDArray[np.uint8]] = {}   # key: device_id
        # visual / audio 最近通过的 monotonic ts,喂 gate hold 判定。
        # visual 用于 hold 资格(spec Section 3.1);audio 仅落 traces 不参与判定。
        self._gate_last_visual_pass_ts: dict[str, float] = {}       # key: device_id
        self._gate_last_audio_pass_ts: dict[str, float] = {}        # key: device_id
        # 上一窗 hold_pass + hold 进入时刻,用于检测状态转换打 HOLD_START /
        # HOLD_EXPIRED / HOLD_RECOVERED 日志 + events 表事件(参考 rule_runner)。
        self._gate_hold_active: dict[str, bool] = {}                # key: device_id
        self._gate_hold_started_at: dict[str, float] = {}           # key: device_id (monotonic)

        # tier_c 闲时定期清(见 .wsh_cc/TierC定期清-落地设计.md):周期协程挂 main_loop。
        # frame_provider 由持有 collector 的层注入(processor),供 live 检测取帧。
        self._tierc_frame_provider: "Callable[[str], NDArray[np.uint8] | None] | None" = None
        self._tierc_clear_task: "asyncio.Task | None" = None
        self._tierc_last_clear_date: dict[str, str] = {}            # key: device_id → "YYYY-MM-DD"
        # 累计帧序号（重审周期判定用）
        self._global_frame_index = 0

    def _scope_label_for(self, device_id: str, room_name: str) -> str:
        """计算（并记忆）一个 device 在所在 room 内的 scope_label，格式 ``"<room>-dev<idx>"``。

        分配规则：room 内首次见到此 did → append 到列表、idx = 最后位置；之后调用
        都返回同一序号。``reset_session`` 不打乱，进程内序号稳定。

        典型场景下 room_name 是中文友好名（"客厅"/"卧室"），渲染出来直接给人看：
        ``unknown-客厅-dev0-3``。
        """
        room_list = self._room_device_index.setdefault(room_name, [])
        if device_id not in room_list:
            room_list.append(device_id)
        idx = room_list.index(device_id)
        return f"{room_name}-dev{idx}"

    def get_tier_u_pool(self):
        """暴露 TierUPool(陌生人池)给上层 router 用。

        engine 禁用 / 池启动失败时返 None。封装内部字段访问,避免外部跨层
        直接读 ``_tier_u_pool`` 私有字段(改名 / 重构会 silent break)。
        """
        return self._tier_u_pool

    def get_deep_sort_config(self):
        """暴露 yaml-resolved ``DeepSortConfigDC`` 给上层 router 视频注册路径用。

        router 临时构造 DeepSortTracker 时,跟主流程 tracking_service 共享同一份
        yaml 配置(``max_age_sec`` 等),避免硬编码 ``DeepSortConfigDC()`` 默认值
        导致同 deep_sort 实例在主路径和注册路径行为不一致(典型坑:max_age_sec 默认
        1.0,yaml 改 3.0 后主路径用 3.0 注册路径仍 1.0,track 过早被杀分裂)。
        """
        return self._config.identity_engine.deep_sort

    def get_input_config(self):
        """暴露 ``InputConfig``(fps / omni_fps / period_sec)给上层做 perf 日志可视。

        封装 ``_config.input`` 私有字段访问;processor 据此在 ``[perf]`` 行打印各层帧率。
        """
        return self._config.input

    def get_reid_extractor(self):
        """供身份库写盘兜底用的 HumanReID 实例。

        优先复用活动 DeepSortTracker 持有的 ReID 实例(避免重复加载 ONNX);若
        无活动 tracker(例如摄像头不可达、感知 pipeline 没拉起),**懒加载一个
        独立 HumanReID instance** 作 fallback——注册流程(上传图/视频/池抽样)
        不该被"摄像头当前在线"绑死。独立实例缓存在 ``self._fallback_human_reid``,
        进程内只构造一次(ONNX session ~250 ms 加载,后续 extract_feature 3-5 ms)。

        多 device 场景下所有 tracker 共用同一份 ReID ONNX(同模型路径),取任一即可。
        """
        for tracker in self._deep_sort_trackers.values():
            if hasattr(tracker, "human_reid"):
                return tracker.human_reid
        # Fallback:摄像头掉线 / 感知 pipeline 没启,但注册流程仍要算 emb 写 .npy。
        # 单独 HumanReID 实例,只用于 add_tier_a_samples_batch / extract_from_image
        # 写盘兜底,跟跟踪侧零额外推理硬约束无关(那条约束只针对 TierU 池代码)。
        if self._fallback_human_reid is None:
            try:
                from miloco.perception.engine.identity.tracker.human_reid import (
                    HumanReID,
                )
                from miloco.perception.engine.identity.tracking_service import (
                    RealTrackingService,
                )
                # 必须用解析后的绝对路径: HumanReID 默认 model_path 是相对 "models/..." ,
                # supervisor 启动 cwd 不在该目录 → init() 静默失败(catch 返 False)、session
                # 留 None → 之后 extract_feature 报"模型未初始化"。复用 tracking_service 的同一
                # 解析口径(配了 perception_model_dir 走它, 否则回退包内 models/), 跟活动 tracker 一致。
                reid_path = RealTrackingService._resolve_model_path(
                    self._config.identity.perception_model_dir or None,
                    "human_body_reid_v2.onnx",
                )
                inst = HumanReID(model_path=reid_path, use_gpu=False)
                if inst.session is None:
                    # init() 内部已 catch+log 具体异常; 这里不缓存坏实例, 直接返 None
                    logger.warning(
                        "get_reid_extractor: 兜底 HumanReID 初始化失败 (model_path=%s)", reid_path,
                    )
                    return None
                self._fallback_human_reid = inst
                logger.info(
                    "get_reid_extractor: 摄像头侧无 ReID, 懒加载独立 HumanReID 兜底 (path=%s)", reid_path,
                )
            except Exception:  # noqa: BLE001
                logger.warning("get_reid_extractor: HumanReID 独立加载失败", exc_info=True)
                return None
        return self._fallback_human_reid

    def _get_or_create_tracking_service(self, device_id: str, room_name: str):
        """懒加载单镜头 SortTracker。每个 device_id 一份，跨调用复用。

        room_name 参数仅用于 ``_scope_label_for`` 一致性（虽然 tracker 自身不需要 scope）；
        让 factory 签名与 ``_get_or_create_identity_engine`` 对齐，pipeline 调用统一。

        deep_sort 模式额外:服务创建成功后,把内部 DeepSortTracker 按 device_id
        (v2 重构后 cam_id == device_id) 注入 ``self._deep_sort_trackers`` 共享
        dict——TierUPool 持有的 DeepSortReIDProvider 引用同一 dict,这样 push_crop
        时 provider 能用 device_id 直接 dispatch 到对应 tracker 取 emb。

        ``deep_sort`` 启动失败兜底:常见原因是 ``human_body_reid_v2.onnx`` 模型缺失
        (模型未随包分发 / 增量升级漏同步)。失败时降级到 ``real`` 模式 + log warning,
        业务能继续跑(陌生人池退化为"只累积、不去重",其余识别链路不受影响),
        部署侧 follow up 补模型即可。
        """
        from miloco.perception.engine.identity.tracking_service import (
            create_tracking_service,
        )
        self._scope_label_for(device_id, room_name)  # 注册 device 到 room 索引（副作用），label 此处不用
        svc = self._tracking_services.get(device_id)
        if svc is None:
            try:
                svc = create_tracking_service(self._tracking_mode, **self._tracking_service_kwargs)
                logger.info("created tracking_service for device=%s mode=%s", device_id, self._tracking_mode)
            except Exception as e:  # noqa: BLE001
                if self._tracking_mode == "deep_sort":
                    logger.warning(
                        "tracking_service mode=deep_sort 创建失败:%s。降级 mode=real 重试"
                        "(可能是 human_body_reid_v2.onnx 模型缺失;陌生人池将退化为"
                        "'只累积、不去重',部署侧请补模型后重启进程)",
                        e,
                    )
                    fallback_kwargs = dict(self._tracking_service_kwargs)
                    # 把 deep_sort 模式专属参数清掉(RealTrackingService 不接 deep_sort_config),
                    # 补 sort_config(real 模式必需)。其余 common_kwargs(model_dir/use_gpu/
                    # input_size/fps)两个模式共用,原样保留。
                    fallback_kwargs.pop("deep_sort_config", None)
                    if "sort_config" not in fallback_kwargs:
                        fallback_kwargs["sort_config"] = self._runtime_sort_cfg
                    svc = create_tracking_service("real", **fallback_kwargs)
                    logger.info("tracking_service for device=%s 降级到 mode=real 成功", device_id)
                else:
                    raise
            self._tracking_services[device_id] = svc
            # deep_sort 模式:登记 tracker 到 ReID 共享 dict。**dict key 必须等于
            # IdentityEngine.cam_id** —— provider 用 CropEntry.cam_id 反查取 emb,
            # 两边漂移会让 emb 取不到、池退化为"只累积、不去重"。
            # 两边都走 ``tier_u.cam_id_from_device_id`` 唯一 helper, key 即米家
            # device_id, 跟前端 ``pool fetch --cam`` 入参命名空间统一(v2 重构)。
            # 降级到 real 时 svc.tracker 是 SortTracker 不是 DeepSortTracker —
            # provider get_embedding 永远拿不到 emb,池自然退化(预期行为)。
            if self._tracking_mode == "deep_sort" and hasattr(svc, "tracker"):
                from miloco.perception.engine.identity.deep_sort import DeepSortTracker
                if isinstance(svc.tracker, DeepSortTracker):
                    self._deep_sort_trackers[cam_id_from_device_id(device_id)] = svc.tracker
        return svc

    def get_active_confirmed_track_keys(self) -> list[tuple[str, int]]:
        """返回所有 cam 上 status=confirmed 的 ``(cam_id, track_id)`` 列表。

        用途: TierU pool fetch 时, 跟当前 confirmed track 做去重 (case b 兜底)。
        router 拿到 keys 传给 ``pool.fetch(confirmed_track_keys=...)``, pool
        内部从 reid_provider 取实时 emb 比对。
        """
        out: list[tuple[str, int]] = []
        for device_id, engine in self._identity_engines.items():
            if engine is None:
                continue
            cam_id = cam_id_from_device_id(device_id)
            for tid in engine.get_confirmed_track_ids():
                out.append((cam_id, tid))
        return out

    def set_main_loop(self, loop: "asyncio.AbstractEventLoop") -> None:
        """记录持久 app event loop, 并透传给所有(含已缓存)identity engine。client 层每窗
        调一次(幂等); tier_c 写库协程靠它脱离每窗临时 loop(asyncio.run), 不被窗末 cancel。"""
        self._main_loop = loop
        for eng in self._identity_engines.values():
            if eng is not None:
                eng.set_main_loop(loop)
        # 首次注入 loop 时起 tier_c 闲时定期清协程(幂等;跑在持久 main_loop 上)。
        if (
            self._tierc_clear_task is None
            and self._identity_lib is not None
            and self._config.identity_engine.tierc_clear.enabled
        ):
            self._tierc_clear_task = loop.create_task(self._tierc_clear_loop())

    def set_tierc_frame_provider(
        self, provider: "Callable[[str], NDArray[np.uint8] | None]"
    ) -> None:
        """注入"按 did 取最近一帧解码图"的回调(由持有 collector 的层接上),供定期清 live 检测。"""
        self._tierc_frame_provider = provider

    async def _tierc_clear_loop(self) -> None:
        """tier_c 闲时定期清:窗内轮询,逐相机整池清空(默认无条件;require_absence 时确认无人才清)。"""
        cfg = self._config.identity_engine.tierc_clear
        logger.info(
            "[tierc-clear] 启动: 窗口 %02d-%02d, 轮询 %ds, 模式=%s",
            cfg.window_start_hour, cfg.window_end_hour, cfg.poll_interval_sec,
            "确认无人" if cfg.require_absence else "无条件",
        )
        while True:
            try:
                await self._tierc_clear_tick(cfg)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("[tierc-clear] tick 异常", exc_info=True)
            await asyncio.sleep(cfg.poll_interval_sec)

    async def _tierc_clear_tick(self, cfg) -> None:
        """单次轮询:时间窗内逐相机整池清空 tier_c。默认无条件清;``require_absence=True``
        时先走"确认无人"漏斗(mtime/gate 静默 + live 检测),任一不过则本轮跳过、等下次轮询。"""
        lib = self._identity_lib
        if lib is None:
            return
        now = datetime.now()
        # 支持跨午夜窗(start > end, 如 23-2);同日窗(3-5)走前一分支。
        start, end = cfg.window_start_hour, cfg.window_end_hour
        in_window = (start <= now.hour < end) if start < end else (now.hour >= start or now.hour < end)
        if not in_window:
            return
        today = now.strftime("%Y-%m-%d")
        mono = time.monotonic()
        for did in list(self._tracking_services.keys()):
            if self._tierc_last_clear_date.get(did) == today:
                continue  # 今晚已清过该相机(幂等)
            # 前置: 该相机 tier_c 池为空 → 无可清, 标记完成跳过(两模式共用)
            latest_mtime = lib.tier_c_pool_latest_mtime(did)
            if latest_mtime is None:
                self._tierc_last_clear_date[did] = today
                continue
            # 确认无人漏斗(仅 require_absence=True): mtime 静默 → gate 静默 → live 检测收口,
            # 任一不过则本轮跳过、等下次轮询。默认 False 跳过整段 → 无条件清。
            if cfg.require_absence:
                if (time.time() - latest_mtime) < cfg.pool_quiet_sec:
                    continue
                gate_ts = self._gate_last_visual_pass_ts.get(did)
                if gate_ts is not None and (mono - gate_ts) < cfg.gate_quiet_sec:
                    continue
                if await self._tierc_detect_has_person(did, cfg.detect_person_conf):
                    continue
            # 清空该相机所有 person 的 tier_c
            # 文件 I/O(list_person_ids 扫目录 + 逐文件 unlink)整段偏移到线程,与写路径
            # add_tier_c_sample 的 to_thread 契约对齐,不在 event loop 上阻塞 SSE/HTTP;
            # clear_tier_c 内部自带 _tier_c_write_lock,线程内与写 worker 天然互斥。
            def _clear_all_persons(cam_id: str = did) -> int:
                total = 0
                for pid in lib.list_person_ids():
                    total += lib.clear_tier_c(cam_id, pid)
                return total

            cleared = await asyncio.to_thread(_clear_all_persons)
            self._tierc_last_clear_date[did] = today
            logger.info("[tierc-clear] cam=%s 闲时清空 tier_c: 删除 %d 图", did, cleared)

    async def _tierc_detect_has_person(self, did: str, conf: float) -> bool:
        """拉该相机最近一帧跑 person 检测, 有 conf≥阈值的人返 True。

        取帧/检测器/帧任一不可用 → **保守返 True(视作有人, 不清)**, 避免误清。
        """
        provider = self._tierc_frame_provider
        if provider is None:
            return True
        try:
            frame = provider(did)
        except Exception:  # noqa: BLE001
            logger.warning("[tierc-clear] cam=%s 取帧失败", did, exc_info=True)
            return True
        if frame is None:
            return True
        svc = self._tracking_services.get(did)
        detector = getattr(svc, "_detector", None) if svc is not None else None
        if detector is None:
            return True
        try:
            from miloco.perception.engine.identity.tracker.detector import Detection
            dets = await asyncio.to_thread(detector.detect, frame)
        except Exception:  # noqa: BLE001
            logger.warning("[tierc-clear] cam=%s 检测失败", did, exc_info=True)
            return True
        return any(
            d.class_id == Detection.CLASS_HUMAN and d.confidence >= conf for d in dets
        )

    def _get_or_create_identity_engine(self, device_id: str, room_name: str):
        """懒加载单镜头 IdentityEngine。共享同一份 ``_identity_lib``。

        identity_engine 禁用 / library 启动失败 → 返回 None（pipeline 退化为无 identity）。
        scope_label 由 ``_scope_label_for`` 分配（``<room>-dev<idx>``），跨镜头 unknown
        编号据此天然唯一。

        启动失败重试：上次失败超过 ``_ENGINE_RETRY_INTERVAL_SEC`` 后允许再试一次，
        避免临时性问题（磁盘满 / 文件锁竞争）必须重启进程才能恢复。
        """
        if self._identity_lib is None:
            return None
        eng = self._identity_engines.get(device_id)
        if eng is not None:
            return eng
        if device_id in self._identity_engines:
            # 上次启动失败，检查是否到了重试时间窗
            last_fail = self._engine_fail_ts.get(device_id, 0.0)
            if time.monotonic() - last_fail < _ENGINE_RETRY_INTERVAL_SEC:
                return None
            logger.info(
                "IdentityEngine for device=%s 上次失败已超 %ds，重试创建",
                device_id, int(_ENGINE_RETRY_INTERVAL_SEC),
            )
        from miloco.perception.engine.identity.engine import build_identity_engine
        ie_cfg = self._config.identity_engine
        scope_label = self._scope_label_for(device_id, room_name)

        # 两段 try 分别捕获 build 与 start 失败:
        # - build 失败:eng 还没构造好,无资源需清理。
        # - start 失败:eng 已经构造(含 FusedDispatcher 实例),需调 close() 释放可能的
        #   半启动状态;但 close() 是 async,本方法是 sync 不能 await,用 ensure_future
        #   fire-and-forget 排队。close() 当前实现 idempotent + 只清 dispatcher 内部
        #   缓存,失败也无害(except 兜底 swallow)。
        # 当前 IdentityEngine.start() 实现是 sync no-op,实际不会到 start 失败分支,
        # 本写法是 future-proof:start() 引入资源初始化失败可能时,清理路径已就位。
        eng = None
        try:
            eng = build_identity_engine(
                ie_cfg, library=self._identity_lib, scope_label=scope_label,
                device_id=device_id,
                engine_fps=self._config.input.fps,
                period_sec=self._config.input.period_sec,
                tier_u_pool=self._tier_u_pool,
                omni_config=self._config.omni,   # 供 tier_c 写库前 omni 同人校验(E7)
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "IdentityEngine for device=%s 构造失败:%s(该镜头退化为无 identity 模式,%ds 后重试)",
                device_id, e, int(_ENGINE_RETRY_INTERVAL_SEC),
            )
            self._engine_fail_ts[device_id] = time.monotonic()
            self._identity_engines[device_id] = None
            return None

        try:
            eng.start()
            logger.info(
                "IdentityEngine started for device=%s scope=%s (tracking=%s, omni_call_mode=%s)",
                device_id, scope_label, ie_cfg.tracking, ie_cfg.omni_call_mode,
            )
            # 创建成功,清掉历史失败时间戳
            self._engine_fail_ts.pop(device_id, None)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "IdentityEngine for device=%s 启动失败:%s(该镜头退化为无 identity 模式,%ds 后重试)",
                device_id, e, int(_ENGINE_RETRY_INTERVAL_SEC),
            )
            # 半启动清理:在跑的 event loop 上 fire-and-forget close(),错误吞掉。
            # task 加入 self._pending_close_tasks 持有强引用,防止 event loop 之外
            # 没人引用导致 GC 提前回收(Python 文档建议)。done_callback 自动 discard。
            try:
                task = asyncio.get_running_loop().create_task(eng.close())
                self._pending_close_tasks.add(task)
                task.add_done_callback(self._pending_close_tasks.discard)
            except RuntimeError:
                pass  # 不在 event loop 里(理论上不可能 — 调用链全是 async),跳过
            except Exception:  # noqa: BLE001
                pass  # close() 内部抛错也吞掉,不阻塞主流程
            eng = None
            self._engine_fail_ts[device_id] = time.monotonic()

        if eng is not None:
            if self._main_loop is not None:
                eng.set_main_loop(self._main_loop)  # 新建 engine 立即拿到持久 loop
        self._identity_engines[device_id] = eng
        return eng

    async def close(self) -> None:
        """关闭引擎——释放所有 per-camera IdentityEngine 资源（dispatcher worker 等）。"""
        if self._tierc_clear_task is not None:
            self._tierc_clear_task.cancel()
            self._tierc_clear_task = None
        for did, eng in self._identity_engines.items():
            if eng is None:
                continue
            try:
                await eng.close()
            except Exception as e:  # noqa: BLE001
                logger.error("IdentityEngine.close for device=%s 失败：%s", did, e)

    # ------------------------------------------------------------------
    # Suggestion 事件链表 / cooldown 管理
    # ------------------------------------------------------------------

    def assign_id_and_update_link(self, room: str, sugg: Suggestion, now: float) -> bool:
        """给 suggestion 分配事件链 id 并维护 _sugg_table。

        Returns:
            True if linked to an existing chain (heartbeat, should NOT be reported).
            False if this is a new chain entry (should be reported).
        """
        if sugg.id is not None:
            return False  # impossible

        table = self._sugg_table.setdefault(room, {})
        urgency = sugg.urgency if sugg.urgency in URGENCY_RANK else "medium"
        sugg.urgency = urgency

        # 本轮 event 句向量（embedder 可用时），用于语义匹配，并在新链/升级时存入链条。
        emb = None
        if sugg.event and self._embedder is not None:
            try:
                emb = self._embedder.embed(sugg.event)
            except Exception as e:  # noqa: BLE001
                logger.warning("event 句向量编码失败，本条退化为精确匹配：%s", e)

        # 找已有链：语义相似度（embedder 可用）或精确文本（兜底）。不再用 prev_id——
        # 停注入历史后模型无从填，去重已全归代码：模型每窗对同一事件措辞会漂移，靠句向量
        # 余弦认出"同一桩持续事件"，避免反复开新链刷屏（旧版精确匹配的死穴）。
        eid_to_link: int | None = None
        if sugg.event:
            if emb is not None:
                best_sim, best_eid = -1.0, None
                for eid, entry in table.items():
                    e_emb = entry.get("embedding")
                    if e_emb is None:
                        continue
                    sim = float(np.dot(emb, e_emb))
                    if sim > best_sim:
                        best_sim, best_eid = sim, eid
                if best_eid is not None and best_sim >= SUGG_SIM_THRESHOLD:
                    eid_to_link = best_eid
                    logger.info(
                        "[sugg-link semantic] room=%s eid=%d sim=%.3f event=%r",
                        room, best_eid, best_sim, sugg.event,
                    )
            else:
                for eid, entry in table.items():
                    if entry["event"] == sugg.event:
                        eid_to_link = eid
                        break

        if eid_to_link is not None:
            entry = table[eid_to_link]
            eid = eid_to_link
            curr = URGENCY_RANK[urgency]
            is_upgrade = curr > URGENCY_RANK[entry["urgency"]]
            if is_upgrade:
                # 事态升级：接受模型本轮的新描述（反映升级后的严重度），刷新链条文案。
                # 模型留空时仍兜底沿用历史，避免升级却丢描述。
                if not sugg.event:
                    sugg.event = entry["event"]
                if not sugg.action:
                    sugg.action = entry["action"]
                logger.info(
                    "[sugg-link upgrade] room=%s eid=%d %s -> %s event=%r",
                    room, eid, entry["urgency"], urgency, sugg.event,
                )
                entry["urgency"] = urgency
                entry["event"] = sugg.event
                entry["action"] = sugg.action
                if emb is not None:  # 文案已刷新，向量同步刷新，后续匹配以新描述为准
                    entry["embedding"] = emb
            else:
                # 心跳（同级或降级）：
                #  · urgency 单调不降级——本轮报更低时直接忽略，沿用历史值；
                #  · 文本沿用链条规范描述，忽略模型本轮重写的文本（drift 防护：曾出现
                #    "揉眼睛"被刷成"操作电脑"）。模型在链接轮重写描述多为同义改写，
                #    按契约（填 id 时 event 应留空）一律丢弃规范描述沿用，仅记 info 备查。
                if sugg.event and sugg.event != entry["event"]:
                    logger.debug(
                        "[sugg-link relabel] room=%s eid=%d 模型链接轮重写描述 %r"
                        "（沿用链条规范描述 %r）",
                        room, eid, sugg.event, entry["event"],
                    )
                sugg.event = entry["event"]
                sugg.action = entry["action"]
            # urgency 单调不降级：对外回写链条当前（峰值）紧急度
            sugg.urgency = entry["urgency"]
            entry["last_ts"] = now  # refresh TTL — 事件仍在持续，延长链条生存期
            sugg.id = eid
            # 冷却节流：复报只看「距上次上报是否过了当前 urgency 的冷却」。urgency 只决定
            # 冷却长短——升级到 high 即采用更短冷却(60s)、因而更易够到门槛，但仍须过完该冷却
            # 才复报，没过就抑制。替代旧的「升级即复报 / 30s TTL 过期重建」两条刷屏路径。
            cooldown = COOLDOWN_SEC.get(entry["urgency"], COOLDOWN_SEC["medium"])
            if now - entry.get("last_report_ts", 0.0) >= cooldown:
                entry["last_report_ts"] = now
                return False  # 过了冷却 → 复报
            return True  # 冷却内 → 抑制
        else:
            if not sugg.event:
                # event 为空又无可匹配链：丢弃，不开空链、不外发（否则 agent 会收到一条
                # event="" 的空提醒）。停注入历史后模型基本不会再产空 event，留作兜底。
                logger.info("[sugg-link drop] room=%s event 为空，丢弃", room)
                return True  # 视为已处理（抑制上报），不创建新链
            eid = self._alloc_eid(room, table)
            table[eid] = {
                "event": sugg.event,
                "action": sugg.action,
                "urgency": urgency,
                "last_ts": now,         # 最后一次「被检测到」——驱动淘汰
                "last_report_ts": now,  # 最后一次「上报给 agent」——驱动冷却复报
                "embedding": emb,  # 可能为 None（embedder 不可用），匹配时跳过
            }
            sugg.id = eid
            return False  # new chain — report

    def _evict_expired_links(self, room: str, now: float) -> None:
        """淘汰超过 COOLDOWN_SEC 的事件链（TTL = 对应 urgency 的 cooldown）。"""
        table = self._sugg_table.get(room)
        if not table:
            return
        self._sugg_table[room] = {
            eid: v for eid, v in table.items()
            if (now - v["last_ts"]) < COOLDOWN_SEC.get(v["urgency"], COOLDOWN_SEC["medium"])
        }

    def _alloc_eid(self, room: str, table: dict[int, dict]) -> int:
        """分配新链 id。1..MAX_EID 单调累加；超过后从空位复用。"""
        next_id = self._next_sugg_id.get(room, 0) + 1
        if next_id <= MAX_EID:
            self._next_sugg_id[room] = next_id
            return next_id
        eid = 1
        while eid in table:
            eid += 1
        return eid

    def reset_session(self) -> None:
        """场景切换时联动重置所有 per-camera SortTracker 与 IdentityEngine。

        SortTracker.reset() 内部把 ``_next_track_id`` 重置为 0；如果不同步清空
        IdentityEngine._states，新 track 从 0 重新自增时会在 dead-track grace
        期内命中残留 state，继承已失效的 committed_person_id。两者必须联动。
        当前 pipeline / api 没有触发点；保留此方法作为未来场景切换的统一入口。
        """
        for svc in self._tracking_services.values():
            if hasattr(svc, "reset_session"):
                svc.reset_session()
        for eng in self._identity_engines.values():
            if eng is not None:
                eng.reset()
        self._global_frame_index = 0
        # 旧场景基准帧 vs 新场景首帧的 diff 不代表真实变化,清掉退化为冷启动语义
        self._gate_prev_frames.clear()
        # hold 状态机一并清,首通后 6min 倒计时重启
        self._gate_last_visual_pass_ts.clear()
        self._gate_last_audio_pass_ts.clear()
        self._gate_hold_active.clear()
        self._gate_hold_started_at.clear()

    async def realtime_perceive(
        self,
        batch: BatchedSnapshot,
        rules: list[dict] | None = None,
        on_early_speeches: Callable[[list[Speech]], Awaitable[None]] | None = None,
        on_early_matched_rules: Callable[[list[MatchedRule]], Awaitable[None]] | None = None,
        on_early_suggestions: Callable[[list[Suggestion]], Awaitable[None]] | None = None,
    ) -> RealtimePerceptionResult | None:
        """Run full engine batch pipeline with rule evaluation."""
        from miloco.perception.engine.pipeline import run_batch_pipeline
        from miloco.perception.engine.types import OmniContext, RuleCondition

        rules = rules or []

        if batch.empty:
            return None

        # 进入新一轮前先按 TTL 淘汰过期事件链
        now = time.monotonic()
        for did in list(self._sugg_table.keys()):
            self._evict_expired_links(did, now)

        # Build per-device contexts with filtered rules（rules 按 device 维度精确筛选——
        # rule.condition.perceive_device_ids 命中该 device 才下发；空列表表示全部感知
        # 设备广播。pending_speech 也是严格 per-device 的——同 room 多镜头各自独立的
        # "上次"语境）
        # device_rule_map 同步记录 did → 下发的 rule_id 列表,供 client.py EXITED 阶段
        # 精确推退状态机桶(不再用 enabled_rule_ids 全集喂 False)。
        contexts: dict[str, OmniContext] = {}
        device_rule_map: dict[str, list[str]] = {}
        for room_name, snapshots in batch.by_room().items():
            for snapshot in snapshots:
                did = snapshot.device.did
                dispatched = [
                    r for r in rules
                    if not r.get("condition", {}).get("perceive_device_ids")
                    or did in r["condition"]["perceive_device_ids"]
                ]
                device_rule_map[did] = [r["id"] for r in dispatched]
                device_rules = [
                    RuleCondition(
                        rule_id=r["id"],
                        rule_name=r.get("name", ""),
                        query=r.get("condition", {}).get("query", ""),
                    )
                    for r in dispatched
                ]
                # last_caption / last_suggestions 不再注入 prompt（回灌模型自己的上轮结论
                # 会形成回声室、强化幻觉）。caption 变化去重 + suggestion 事件链去重均下沉
                # 到代码（_last_captions 比对、assign_id_and_update_link 语义匹配）。
                contexts[did] = OmniContext(
                    rule_conditions=device_rules,
                    pending_speech=self._pending_speech.get(did),
                    current_time=datetime.fromtimestamp(snapshot.start_timestamp / 1000).strftime("%H:%M:%S"),
                    room_name=room_name,
                )

        # Prepend audio tail from previous window (overlap to reduce boundary truncation)
        # Per-device tail（不是 per-room）—— 修复旧版同 room 多 device 反复覆写同一 key
        # 只保留 last device tail 的隐性 bug
        overlap_samples = int(self._config.input.audio_overlap_ms / 1000 * 16000)
        if overlap_samples > 0:
            for snapshot in batch.snapshots:
                did = snapshot.device.did
                prev_tail = self._audio_tail.get(did)
                current_audio = snapshot.audio_clip
                # Save current tail before any modification
                if current_audio.size >= overlap_samples:
                    self._audio_tail[did] = current_audio[-overlap_samples:].copy()
                elif current_audio.size > 0:
                    self._audio_tail[did] = current_audio.copy()
                # Prepend previous tail to current audio
                if prev_tail is not None and prev_tail.size > 0 and current_audio.size > 0:
                    merged = np.concatenate([prev_tail, current_audio])
                    ts = snapshot.audio.frames[0].timestamp if snapshot.audio and snapshot.audio.frames else snapshot.start_timestamp
                    snapshot.audio = AudioStream(
                        frames=[AudioFrame(data=merged, timestamp=ts)],
                        sample_rate=snapshot.sample_rate,
                    )

        # Run batch pipeline（通过 factory 回调懒加载 per-device tracking_service /
        # identity_engine，让 fused 模式下回灌路径打通）
        try:
            result = await run_batch_pipeline(
                batch,
                contexts,
                self._config,
                get_tracking_service=self._get_or_create_tracking_service,
                get_identity_engine=self._get_or_create_identity_engine,
                on_early_speeches=on_early_speeches,
                on_early_matched_rules=on_early_matched_rules,
                on_early_suggestions=on_early_suggestions,
                # 流式早出的 suggestion 经此闸门解析事件链（与 _merge_results 同一方法、
                # 同一推理线程），心跳/重复抑制后才外发
                assign_suggestion_link=self.assign_id_and_update_link,
                frame_index_offset=self._global_frame_index,
                gate_prev_frames=self._gate_prev_frames,
                gate_last_visual_pass_ts=self._gate_last_visual_pass_ts,
                gate_last_audio_pass_ts=self._gate_last_audio_pass_ts,
                gate_hold_active=self._gate_hold_active,
                gate_hold_started_at=self._gate_hold_started_at,
            )
        except Exception as e:
            logger.error("Batch pipeline failed: %s", e, exc_info=True)
            raise  # 让上层 processor 按异常类型分类（OmniError → omni_error_count）

        # 推进全局帧序号——驱动 IdentityEngine recheck 周期与 dead-track GC
        # 增量按 downsample 后单窗口帧数估算（fps × period_sec），与 run_identity
        # 内部 ``frame_index_offset + len(gate_packet.frames)`` 语义对齐
        self._global_frame_index += self._config.input.fps * self._config.input.period_sec

        # Merge all rooms into a single RealtimePerceptionResult
        return self._merge_results(result, contexts, device_rule_map=device_rule_map)

    async def on_demand_perceive(
        self,
        batch: BatchedSnapshot,
        query: str,
    ) -> OnDemandPerceptionResult:
        """Active query — skip Gate, run Edge, query prompt to Omni."""
        from miloco.perception.engine.pipeline import run_query_pipeline

        if batch.empty:
            return OnDemandPerceptionResult(answer="")

        # query 路径 omni 仍是 per-room 一次（产品语义：用户问"房间"，答案单份），
        # 需要 per-room 的 last_caption 作为参考。``_last_captions`` 已改 per-device，
        # 这里合成 per-room：**取 room 内第一个 snapshot 对应 device 的 caption**——
        # 与 ``_encode_batch_video`` 的 "first device that has frames" 视频选取精准对齐
        # （query 路径不走 gate filter，identity_packets 顺序 = snapshots 顺序，
        # ``snapshots[0]`` 通常就是 ``_encode_batch_video`` 实际选中那个 device）。
        #
        # 为什么不拼接多 device caption：query 路径 omni 实际只看到首镜头视频（遗留
        # degenerate，见 run_query_pipeline 的 Note 段）。若 last_caption 拼成多视角
        # "设备0：xxx；设备1：yyy"，但视频里没有设备1的画面 → 文本与视觉信息不对称，
        # 容易触发模型混乱或幻觉。等 query 路径修好多视频问题后再升级。
        per_room_last_caption: dict[str, str] = {}
        for room_name, snapshots in batch.by_room().items():
            if not snapshots:
                continue
            first_did = snapshots[0].device.did
            cap = self._last_captions.get(first_did)
            if cap:
                per_room_last_caption[room_name] = cap

        try:
            results = await run_query_pipeline(
                batch,
                query,
                self._config,
                get_tracking_service=self._get_or_create_tracking_service,
                get_identity_engine=self._get_or_create_identity_engine,
                last_captions=per_room_last_caption,
                frame_index_offset=self._global_frame_index,
            )
        except Exception as e:
            logger.error("Query pipeline failed: %s", e, exc_info=True)
            return OnDemandPerceptionResult(answer="")

        # 与 realtime_perceive 保持一致地推进全局帧序号
        self._global_frame_index += self._config.input.fps * self._config.input.period_sec

        # Merge all room answers
        answers = [r.answer for r in results.values() if r.answer]
        return OnDemandPerceptionResult(answer="\n".join(answers) if answers else "")

    # ------------------------------------------------------------------
    # Result merging
    # ------------------------------------------------------------------

    def _merge_results(
        self,
        result: BatchPipelineResult,
        contexts: dict[str, OmniContext] | None = None,
        device_rule_map: dict[str, list[str]] | None = None,
    ) -> RealtimePerceptionResult:
        """把所有 room × device 的 OmniOutput 合并成一份 RealtimePerceptionResult。

        per-device 调用粒度下，每个 device 的 omni 输出挂在 ``device_results[did].
        omni_output``——遍历所有 room 的所有 device，把 caption / matched_rules /
        speeches / suggestions extend 到合并结果里（保留 device 的
        ``source_device_ids`` 元信息）。去重 / pending_speech 续接均按 device_id 维度做。

        ``device_rule_map`` 由 ``realtime_perceive`` 在 per-device 过滤循环里同步构建
        透传过来,挂到结果上供 client.py 精确推退未命中的 (rule_id, did) 桶。
        """

        all_skipped = all(r.skipped for r in result.rooms.values()) if result.rooms else False

        # Build merged timing: batch-level timing + per-room timing
        # "_" 前缀 key 装 per-device 元数据(如 device_trace_id),保持顶层不加
        # room 前缀,让下游 ``key.startswith("_")`` 过滤逻辑能识别。
        timing: dict[str, Any] = {}
        if result.timing:
            timing.update(result.timing)
        for room_name, room_result in result.rooms.items():
            if room_result.timing:
                for k, v in room_result.timing.items():
                    timing[k if k.startswith("_") else f"{room_name}/{k}"] = v

        merged = RealtimePerceptionResult(
            skipped=all_skipped,
            timing=timing or None,
            device_rule_map=device_rule_map or {},
        )

        for room_name, room_result in result.rooms.items():
            if room_result.skipped:
                continue

            for did, dr in room_result.device_results.items():
                out = dr.omni_output
                # out.skipped=True 来自 response_parser._fallback（JSON 解析失败
                # 等异常路径）。fallback 的 caption=[{description: "[解析失败] ..."}]
                # 含模型截断输出的原始片段，禁止写回 last_captions / 外发给 client：
                #   - 写回 → 下一轮 prompt "上次场景：[解析失败] ..." 注入，形成
                #     复读片段二次回流，可能让模型在历史参考上继续走偏；
                #   - 外发 → 下游消费方拿到一段无意义的解析失败字符串，污染日志和
                #     存储。
                if out is None or out.skipped:
                    # skipped 路径（含 ngram 流式复读熔断截断、JSON 解析失败等）
                    # 不能就这么 continue：stale pending_speech 仍会注入下轮 prompt，
                    # 模型可能继续复读 pending_speech 内容 → 又 abort → 又 skipped，
                    # 形成自我强化死锁。在 continue 前推进 rounds 并按阈值清理，
                    # 与 line 686 正常超时路径行为对齐。
                    if did in self._pending_speech:
                        rounds = self._pending_speech_rounds.get(did, 0) + 1
                        if rounds > self._max_pending_speech_rounds:
                            logger.warning(
                                "pending_speech exceeded %d rounds via skipped path for device %s (room %s), clearing buffer: %s",
                                self._max_pending_speech_rounds,
                                did,
                                room_name,
                                self._pending_speech.get(did),
                            )
                            self._pending_speech.pop(did, None)
                            self._pending_speech_rounds.pop(did, None)
                        else:
                            self._pending_speech_rounds[did] = rounds
                    continue

                # caption 每窗如实下发（不去重——去重会让"规则命中窗 caption 被吞"日志
                # 不直观）；仅描述非空时下发并刷新 _last_captions 基准（on_demand 路径读它当上下文）。
                has_caption = bool(out.caption and out.caption[0].description)
                if has_caption:
                    self._last_captions[did] = out.caption[0].description

                # 反馈环熔断：本轮 incomplete content 若与上轮 pending_speech 完全
                # 重合，视为模型复读 prompt 注入的"上轮未完成语音"明文。命中后：
                #   1. 不写回 pending_speech，断开续命链
                #   2. 该 incomplete interaction 也不外发给 client（needs_response=false，
                #      纯噪声，外发只会污染日志/下游）
                incomplete = [i for i in out.speeches if not i.is_complete]
                prev_contents = {p["content"] for p in self._pending_speech.get(did, [])}
                new_incomplete = [i for i in incomplete if i.content not in prev_contents]
                loopback_detected = bool(incomplete) and not new_incomplete
                if loopback_detected:
                    logger.info(
                        "loopback incomplete detected for device %s (room %s), suppressing interaction and skipping pending_speech writeback: %s",
                        did,
                        room_name,
                        [i.content for i in incomplete],
                    )

                if has_caption:
                    merged.caption.extend(out.caption)
                merged.matched_rules.extend(out.matched_rules)
                if loopback_detected:
                    merged.speeches.extend(i for i in out.speeches if i.is_complete)
                else:
                    merged.speeches.extend(out.speeches)
                merged.env_sounds.extend(out.env_sounds)
                # 给 suggestions 分配 id / 更新事件链表；链接到已有链的 heartbeat 不上报
                merge_now = time.monotonic()
                for s in out.suggestions:
                    if s.id is not None:
                        # per-omni 早送已打 id：_run_device 已把本设备 suggestions 裁成只剩
                        # 新链(心跳抑制)，此处直接保留进 result.suggestions 供 dump/上下文完整
                        # 展示；不再 assign_link(链表已在早送时更新)，防重发交 client 侧
                        # early_sent_sugg_ids(见 handle_realtime_perception_result)。
                        merged.suggestions.append(s)
                        continue
                    linked = self.assign_id_and_update_link(did, s, merge_now)
                    if not linked:
                        merged.suggestions.append(s)
                if not merged.time:
                    ctx = (contexts or {}).get(did)
                    if ctx and ctx.current_time:
                        merged.time = ctx.current_time
                # 多 device 场景下取最后一次调用的 usage（不累加）
                if out.usage:
                    merged.usage = out.usage

                # Update pending speech for cross-window continuation（per-device）
                if new_incomplete:
                    rounds = self._pending_speech_rounds.get(did, 0) + 1
                    if rounds > self._max_pending_speech_rounds:
                        logger.warning(
                            "pending_speech exceeded %d rounds for device %s (room %s), clearing buffer: %s",
                            self._max_pending_speech_rounds,
                            did,
                            room_name,
                            [i.content for i in new_incomplete],
                        )
                        self._pending_speech.pop(did, None)
                        self._pending_speech_rounds.pop(did, None)
                    else:
                        self._pending_speech[did] = [{"speaker": i.speaker, "content": i.content} for i in new_incomplete]
                        self._pending_speech_rounds[did] = rounds
                else:
                    self._pending_speech.pop(did, None)
                    self._pending_speech_rounds.pop(did, None)

        return merged
