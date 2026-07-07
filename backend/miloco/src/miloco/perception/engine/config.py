"""Perception Engine MVP — Configuration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InputConfig:
    # 帧率旋钮集中在此(+ settings.yaml 的 perception.engine.input 块),不在代码各处散落:
    #   fps      —— 下发/pipeline 帧率 = tracker 帧率(下采后 tracker 逐帧消费全部, 无独立节流)
    #   omni_fps —— 送给 omni 的视频帧率, 独立解耦; omni 已是瓶颈(~3900ms/窗), 不随 fps 涨
    # 关系: omni 视频在 pipeline 内由 fps 帧再下采到 omni_fps(见 pipeline._downsample_for_omni)。
    fps: int = 3
    omni_fps: int = 1
    period_sec: int = 4
    audio_overlap_ms: int = (
        100  # overlap window to reduce audio truncation at boundaries
    )


@dataclass
class GateConfig:
    check_fps: int = 1
    change_threshold: float = 0.005
    cooldown_ms: int = 1000
    audio_energy_threshold: float = 0.015
    # VAD(silero)门控 speeches 字段:音频过能量 gate 后,再用 VAD 判本窗有没有真人声。
    # 无人声 → 只从 schema 剥掉 speeches(env_sounds / 喂音频照旧),根除模型在键鼠敲击 /
    # 办公底噪上脑补"像指令的话"。纯能量分不开人声与机械瞬态噪声,故独立加 VAD 这道。
    speech_vad_enabled: bool = True
    # threshold=0.4 + min_chunks=3：在 82 幻觉 + 149 真实语音 clip 上实测 FP 4% / FN 5%
    # 的拐点(对话 peak 概率中位 0.998、键鼠噪声 0.10,分得很开)。残留 FP 由下游
    # needs_response 佐证兜底。speech_prob 已落 trace,后续可据线上分布再调。
    speech_vad_threshold: float = 0.4  # 单帧人声概率阈(silero)
    speech_vad_min_speech_chunks: int = 3  # 需 >= 此数的 512 样本帧过阈才判有人声(抗单次咔哒尖峰)
    # visual 滞回时长(秒)。visual 最近通过过、距今 <= 此值时,本窗 visual
    # 不通过也强制生成 packet 并打 hold 标志。0 = 关闭。
    # 默认 90s:过长(如 6min)在画面间歇有变化的场景会持续刷新 last_visual_pass_ts、
    # hold 窗反复续命,期间纯静默窗也被拉起跑下游,浪费 omni 调用。运维侧可在
    # settings.yaml 的 perception.engine.gate.hold_duration_sec 覆盖。
    hold_duration_sec: float = 90.0


@dataclass
class IdentityConfig:
    static_displacement_threshold: float = (
        0.05  # ratio of displacement to bbox diagonal
    )
    crop_padding_ratio: float = 0.2
    static_frame_resolution: str = "high"
    dynamic_frame_resolution: str = "medium"
    tracking_service_mode: str = "mock"  # "mock" | "real" | "deep_sort"（v1.2 加 deep_sort：含 ReID 给陌生人池复用）
    tracking_service_url: str = ""
    # Real tracking service config
    perception_model_dir: str = ""  # empty = auto-detect
    perception_use_gpu: bool = False
    perception_input_width: int = 1280
    perception_input_height: int = 720


# =============================================================================
# IdentityEngineConfig —— omni 身份识别系统总配置
# =============================================================================


@dataclass
class SortConfigDC:
    """SortTracker 调参。

    ``max_age_sec`` 是真实世界秒数；SortTracker 实例化时按 fps 换算成帧数：
        max_age_frames = max_age_sec × fps
    """

    n_init: int = 1
    max_age_sec: float = 1.0  # track 丢失多久（秒）后注销
    iou_threshold: float = 0.3
    detector_conf_threshold: float = 0.5
    track_human_only: bool = True


@dataclass
class DeepSortConfigDC:
    """DeepSORT 跟踪器调参。

    与 ``SortConfigDC`` 同名字段语义对齐;额外加 fast 模式开关与 ReID 抽取频率。
    yaml 仅暴露用户可能调的 7 字段:
        - 跟踪基本:n_init/max_age_sec/iou_threshold/detector_conf_threshold/track_human_only
        - fast 模式:mode/human_reid_skip_windows

    内部走代码默认值的:max_cosine_distance(DeepSORT 标准 0.2) / reid_model_path /
    use_gpu 等部署/接口级配置;static_displacement_ratio / static_min_abs_px 等
    几何固定阈值(位移/对角线比 0.05 + 绝对位移 10px,业务无关,在 TrackerConfig
    里有默认值)。
    """

    # 跟踪基本参数(与 SortConfigDC 同名同义)
    n_init: int = 1
    max_age_sec: float = 1.0
    iou_threshold: float = 0.3
    detector_conf_threshold: float = 0.5
    track_human_only: bool = True

    # fast 模式(静止 track 跳过 ReID 推理)
    mode: str = "fast"                       # "normal" / "fast"
    human_reid_skip_windows: int = 4         # 静止 track 每 N 个 window 才抽一次 ReID


@dataclass
class StabilityConfigDC:
    """Pending State 置信度感知 commit 阈值。

    omni 单次识别可能误判，按 ``best_conf`` 决定需要几次同答才能 commit：
    高置信 1 次即落定、中/低置信各 3 次（越不确定要越多票，阈值单调不降）。
    """

    high_conf_threshold: float = 0.85
    mid_conf_threshold: float = 0.65
    low_conf_threshold: float = 0.50
    commit_threshold_high: int = 1
    commit_threshold_mid: int = 3
    commit_threshold_low: int = 3
    pending_timeout_sec: float = 60.0
    # 重审周期用**秒**标定(墙钟语义), 由 needs_omni_call 入口按 engine_fps 运行时换算成
    # frame_index 帧数(round(sec × fps))——与 max_age_sec 同款"秒标定、运行时换算", 改 fps
    # 自动跟随、墙钟周期不漂移。
    recheck_interval_sec: float = (
        30.0  # confirmed / unknown 状态下重审周期
        # 30s 盲窗的取舍: unknown 状态下太长(如 60s)会"离开回来后卡 unknown 数分钟",
        # 太短则 omni 调用频率高。同时用作 confirmed track 写库**冷却期内**的慢重审间隔。
    )
    # confirmed track 处于"攒写库资格"阶段(非冷却)的快重审间隔, 比 recheck_interval_sec 短,
    # 让"连续 N 次一致"更快凑齐。代价: 攒库阶段 omni 调用更密(仅 confirmed 且非冷却时;
    # 攒满即写并进冷却转慢, 不会持续高频)。
    recheck_interval_accumulating_sec: float = 10.0
    hysteresis_unmatched_count: int = 2
    # 时序一致性写库门 (tier_c 污染修复): 同一 person_id **连续 N 次 confirmed 重审一致**
    # 才允许写入 tier_c。把"显示用 commit"(灵敏, conf-aware 1/2/3 次) 跟"写库资格"(严格)
    # 解耦, 挡偶发高置信误判 / ID-switch 残留。严格连续: 中途 None(弃权) / 矛盾 / coasting
    # 任一打断即清 0 重攒; commit(pending→confirmed) 时也清 0, 保证攒库发生在确认之后。
    write_eligible_min_count: int = 6
    # tier_c 写库冷却: 攒满 write_eligible_min_count 写一张后进入冷却期不再晋升,
    # 冷却时长(秒) = mult × write_eligible_min_count × 快重审间隔(秒) = 2×6×10 = 120s
    # (engine 内按 engine_fps 换算成帧计冷却; 秒标定、改 fps 不漂移)。
    # 冷却期内: 慢重审验身份(矛盾仍能退回)、不写、不攒(write_eligible 冻结在 0)。
    tier_c_cooldown_mult: int = 2

    # 翻转专用 commit 阈值: 仅"由 confirmed 退回的 pending"重确认用, 比首次严防误翻。
    # 首次识别仍用上面的 commit_threshold_*(高1/中3/低3), 保高置信单票快速上名。
    flip_commit_threshold_high: int = 2
    flip_commit_threshold_mid: int = 2
    flip_commit_threshold_low: int = 3
    # 翻转黏滞: 退回 pending 后显示保持旧名的最大重审窗数; 超了仍未定则放手交还正常状态机。
    flip_sticky_max_recheck: int = 2


@dataclass
class DispatchConfigDC:
    """识别派发节流参数（避免每帧调 omni 浪费 token）。"""

    min_interval_sec: float = 5.0
    max_queries_per_call: int = 4
    stale_threshold_sec: float = 30.0
    max_retries: int = 1


@dataclass
class GalleryConfigDC:
    """omni prompt 中 gallery snapshot 渲染参数。

    每人渲染 1 张 body composite + 1 张 face composite 给 omni 做识别参考；
    人脸用于精准匹配，全身用于体型 / 衣着辅助。
    """

    body_refs_per_person: int = 3
    face_refs_per_person: int = 3
    library_root: str = "data/identity_lib"


@dataclass
class StrangerConfigDC:
    """陌生人编号策略。

    默认 false：不维持陌生人身份编号；所有未识别目标归为 ``"unknown"``。
    设 true 时启用 ``unknown_<n>`` 自增编号；仅 ``tracking=track_based`` 时合法
    （track_free 没有 track_id 持久性，无法稳定分配编号，启动会报错）。
    """

    distinguish: bool = False


@dataclass
class TierCClearConfig:
    """tier_c 闲时定期清:每晚低活跃窗口整池清空该相机 tier_c(对错都清),把污染寿命压到
    ≤1 天 + 让 tier_c 只反映"今天"的外观。清空 = 退纯 tier_a(安全态)。

    默认 ``require_absence=False``:凌晨到点**无条件清**(不判在场)。代价:若有人在场,
    其当天好样本也一并清掉,需重新累积。置 True 恢复旧"确认无人"漏斗(mtime/gate 静默 +
    live 检测收口),见 ``api._tierc_clear_tick``。
    """

    enabled: bool = True
    # False(默认):到点无条件清。True:走"确认无人"漏斗(下方三项仅此模式生效)
    require_absence: bool = False
    # 清理时间窗(本地 24h):[start_hour, end_hour) 内才评估。无条件模式在窗起点(凌晨3点)首轮即清一次。
    window_start_hour: int = 3
    window_end_hour: int = 5
    poll_interval_sec: int = 3600        # 窗内轮询间隔(60min);幂等键=日历日,同日窗每相机
                                         # 每晚至多清一次(跨午夜窗按日历日各算一次,但二次必遇
                                         # 空池前置→标记跳过,详见 api._tierc_clear_tick)
    # —— 以下三项仅 require_absence=True(确认无人模式)时生效 ——
    # 前置过滤:该相机所有 person 的 tier_c 最新写入 mtime ≥ 此秒数(近期没在写)
    pool_quiet_sec: float = 1800.0
    # 前置过滤:该相机距上次 visual gate pass ≥ 此秒数(近期画面无变化)
    gate_quiet_sec: float = 600.0
    # 收口:主动拉一帧跑检测,无 conf≥此值的人才判"确无人"(唯一能抓静止人的闸)
    detect_person_conf: float = 0.8


@dataclass
class DriftCheckConfigDC:
    """Track 身份漂移自检参数（commit 后人物交叉/交互致 track 跟错人的纠正安全网）。

    每窗对每个"已绑成员的 confirmed track"，用该 person 近期同摄 TierC body 质心
    作参考，比对 track 当前外观质心(cos)；累计 ``consecutive_windows`` 个低窗
    （``sim < threshold``；无数据窗不计不清）入嫌疑集。三档（``mode``）：

      - ``enforce`` —— 撤嫌疑集回 pending 丢回 omni 重判 + 采信复认护栏（默认）。
      - ``observe`` —— 只算 sim + 打 ``[Identity/drift]`` 日志、不撤（调试 / 抓阈值分布）。
      - ``off``     —— 完全不算、不打、不撤。

    全程 body-only、零额外推理（复用 tracker 已有 ReID 特征 + 库里已落盘 .npy）。
    """

    mode: str = "enforce"              # "off" | "observe" | "enforce"
    recency_sec: float = 900.0         # 参考样本时间窗 15min（跨天旧外观对今天不可比）
    threshold: float = 0.55            # sim < 此值视为不同人；0.55 = 该 ReID 模型区分"绝对不同人"的既定经验阈值(误撤极少)
    consecutive_windows: int = 2       # 累计 M 个低窗才入嫌疑集（无数据窗不计不清）
    min_track_emb: int = 3             # track 质心要求最少 emb 数（不足跳过，不拿噪声质心误判）


@dataclass
class NoPersonConfigDC:
    """无人误检（no_person）抑制参数。

    检测模型偶尔把静态杂物 / 家具误报成人体框 → 形成 track → 注入 omni → caption 据框
    脑补"陌生人在做某事"。omni 在 identities 里把"框内确无人"判为 ``no_person``（区别于
    ``unknown``=有人但不认识，见 ``field_registry``），本配置控制引擎侧如何消费该判定：
    连续 ``vote_threshold`` 次 no_person 才落定 ``no_person`` 状态（停派发 omni、身份不导出、
    不进陌生人池）。

    召回保护偏"少误判无人"——落定后有三重解除通道，确保任何 track 都不会被永久静音：
      1) 落定需连续 ``vote_threshold`` 票（防单次误判掉背对 / 遮挡的真人）；
      2) 框相对落定锚点明显移动 → 立即回 pending（会动的真人即时自愈，走
         ``clear_no_person_on_motion``）；
      3) 每 ``no_person_recheck_sec`` 放行一次慢重审，重审判到人即回 pending（走
         ``clear_no_person_to_pending``）。这条是最后兜底的"不永久卡死"自愈不变式，非主恢复
         通道：只为极少数"未注册真人一动不动、被连判两票误压、又不触发移动解除"的残留兜底。
         注意急性摔倒 / 倒地动作由上游动作 / 事件检测捕获、不依赖本状态，故此周期可放得很长。
    """

    enabled: bool = True
    # 连续命中 no_person 多少次才落定状态（防单次误判把背对 / 遮挡的真人误当无人）
    vote_threshold: int = 2
    # 落定后"明显移动"判据：中心位移 ≥ min_abs_px **且** ≥ ratio×bbox 对角线，两者同时满足才算移动
    motion_clear_displacement_ratio: float = 0.15
    motion_clear_min_abs_px: float = 30.0
    # 慢重审自愈周期（秒）：落定后每此秒数放行一次 omni 重审，判到人即回 pending。这是"任何状态
    # 都不会被永久卡死"的最后兜底（主恢复靠上面的移动解除 + 上游动作检测），故可放长；
    # needs_omni_call 入口按 engine_fps 换算成帧。默认 1200s(20min)，现场想更懒可调 1800/3600。
    no_person_recheck_sec: float = 1200.0

    # reject_region（P3，默认关）：把 no_person 的屏幕区域登记为"禁新建 track 区"，
    # 直到真人 track 与之明显重叠或区域 TTL 到期才失效。默认关闭——需现场验证后再开
    # （误开会挡住门口 / 座位等真人常现位置的新 track）。
    reject_region_enabled: bool = False
    reject_region_ttl_sec: float = 300.0      # 区域登记后多久自动过期失效
    reject_region_clear_iou: float = 0.3      # 真人 track 与禁区 IoU ≥ 此值即解除该禁区


@dataclass
class IdentityEngineConfig:
    """omni 身份识别系统总配置。"""

    enabled: bool = True

    # 跟踪策略：track_based 维持跨帧身份连续性；track_free 暂未实施
    tracking: str = "track_based"  # "track_based" | "track_free"
    # omni 调用方式：
    #   "fused"    —— 主调用同时输出 environments / speeches / identity_assignments
    #   "separate" —— 占位，未实施
    omni_call_mode: str = "fused"

    # 子组件
    sort: SortConfigDC = field(default_factory=SortConfigDC)
    deep_sort: DeepSortConfigDC = field(default_factory=DeepSortConfigDC)   # v1.2 新增
    stability: StabilityConfigDC = field(default_factory=StabilityConfigDC)
    dispatch: DispatchConfigDC = field(default_factory=DispatchConfigDC)
    gallery: GalleryConfigDC = field(default_factory=GalleryConfigDC)
    stranger: StrangerConfigDC = field(default_factory=StrangerConfigDC)
    tierc_clear: TierCClearConfig = field(default_factory=TierCClearConfig)
    drift_check: DriftCheckConfigDC = field(default_factory=DriftCheckConfigDC)
    no_person: NoPersonConfigDC = field(default_factory=NoPersonConfigDC)   # 无人误检抑制

    # crop / 累积
    body_crop_padding_ratio: float = 0.05
    tier_c_accumulate_on_commit: bool = True
    # 写 tier_c 前用 omni 做一次"待入库样本 vs 该成员权威库(tier_a)"的同人校验(设计文档 E7),
    # 挡非本人误判落库。默认开;也是 tier_c 回喂 gallery(C3)的前提。需配合注入 omni_config,
    # 开了却没注入则降级为仅 pHash 把关(启动打 ERROR, 不崩溃)。
    tier_c_verify_enabled: bool = True
    # 上述校验的放行门:omni 判 same_person 且 confidence >= 此值才写库。
    tier_c_verify_conf_threshold: float = 0.8

    # response 中低于此置信度的 person_id 强制视作 unknown，防 omni 幻觉
    confidence_cutoff: float = 0.5

    # 暂未启用的开关（对应功能尚未实施）
    ensemble_grounding: bool = False  # ensemble grounding 兜底
    body_attr_text: bool = False  # 人体特征文本
    alias_frequency: bool = False  # AliasFrequencyTracker（track_free 专用）


@dataclass
class OmniConfig:
    model: str = "xiaomi/mimo-v2.5"
    api_key: str = ""  # Set via MILOCO_MODEL__OMNI__API_KEY env var or config
    base_url: str = "https://api.xiaomimimo.com/v1"
    max_completion_tokens: int = 512
    temperature: float = 0.1  # 低温更利于结构化感知判定：实测 grounded prompt 下温度越低误报/坏 JSON 越少
    top_p: float = 0.95
    timeout: float = 30.0
    stream: bool = False


@dataclass
class PerceptionConfig:
    input: InputConfig = field(default_factory=InputConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    omni: OmniConfig = field(default_factory=OmniConfig)
    identity_engine: IdentityEngineConfig = field(default_factory=IdentityEngineConfig)


# =============================================================================
# 配置加载 helper：dict（来自 yaml）→ 嵌套 dataclass
# =============================================================================


def identity_engine_config_from_dict(d: dict | None) -> IdentityEngineConfig:
    """把 settings.yaml 中 ``perception.engine.identity_engine`` 字典转为
    嵌套 dataclass 树。缺失字段走 dataclass 默认值，未知字段被忽略（向前兼容）。
    """
    if not d:
        return IdentityEngineConfig()

    def _filter(cls, sub: dict | None) -> dict:
        """只保留 cls 定义的字段；丢弃 yaml 里 unknown key。"""
        if not sub:
            return {}
        from dataclasses import fields

        valid_keys = {f.name for f in fields(cls)}
        return {k: v for k, v in sub.items() if k in valid_keys}

    sub_factories = {
        "sort": SortConfigDC,
        "deep_sort": DeepSortConfigDC,    # v1.2 主动注册改造 · 漏加导致 yaml 走 dict 不转 dataclass
        "stability": StabilityConfigDC,
        "dispatch": DispatchConfigDC,
        "gallery": GalleryConfigDC,
        "stranger": StrangerConfigDC,
        "tierc_clear": TierCClearConfig,
        "drift_check": DriftCheckConfigDC,
        "no_person": NoPersonConfigDC,
    }
    kwargs: dict = {}

    # 顶层 scalar 字段
    from dataclasses import fields

    top_level_keys = {f.name for f in fields(IdentityEngineConfig)}
    for k, v in d.items():
        if k in top_level_keys and k not in sub_factories:
            kwargs[k] = v

    # 嵌套子 dataclass
    for k, cls in sub_factories.items():
        kwargs[k] = cls(**_filter(cls, d.get(k)))

    return IdentityEngineConfig(**kwargs)
