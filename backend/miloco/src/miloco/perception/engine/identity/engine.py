"""IdentityEngine —— 身份识别系统总编排。

把 SortTracker / IdentityLibrary / TrackIdentityState / EvidenceDispatcher 连成
完整管线。``run_identity`` 在每个窗口调
``IdentityEngine.process(tracking_results, frame, frame_idx, ts)`` 来：

  1. 维护每个 track 的 ``TrackIdentityState``（none/pending/confirmed/unknown）
  2. 决定哪些 track 需要派发 omni 识别（节流 + inflight + 重审周期）
  3. 调 dispatcher 派发（fused 模式缓存候选，等主调用 response 回流）
  4. dispatcher 回流时调 ``on_result`` 写回 state；commit 后异步累积 Tier C
  5. 给下游返回 ``track_id → face_id`` 映射（写到 ``IdentityTarget.face_id``）

启动时校验配置组合：
  - ``track_free`` × ``stranger.distinguish=true`` 是非法组合 → 启动直接抛错
    （track_free 无 track_id 持久性，无法稳定分配陌生人编号）
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Optional

import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.identity._image_utils import (
    compute_sharpness as _compute_sharpness,
)
from miloco.perception.engine.identity.dispatcher import (
    EvidenceDispatcher,
    FusedDispatcher,
    IdentityQueryItem,
    OmniIdentityResult,
)
from miloco.perception.engine.identity.library import (
    GallerySamples,
    IdentityLibrary,
    PersonRef,
)
from miloco.perception.engine.identity.tier_u import cam_id_from_device_id

if TYPE_CHECKING:
    from miloco.perception.engine.config import OmniConfig
    from miloco.perception.engine.identity.tier_u import TierUPool
from miloco.perception.engine.identity.state import (
    TrackIdentityState,
    apply_recheck_result,
    check_pending_timeout,
    clear_no_person_on_motion,
    get_face_id_value,
    mark_dispatched,
    needs_omni_call,
    promote_to_pending,
    record_no_person,
    update_evidence,
)

logger = logging.getLogger(__name__)


TrackingMode = Literal["track_based", "track_free"]
OmniCallMode = Literal["separate", "fused"]


# =============================================================================
# 配置
# =============================================================================


# IdentityEngineConfig 现在统一从 config.py 来（嵌套版，含 sort/stability/dispatch/
# gallery/stranger 子段）。engine.py 不再定义自己的配置类，避免双份定义混淆。
from miloco.perception.engine.config import IdentityEngineConfig  # noqa: E402

# =============================================================================
# tier_c 写入兜底阈值（hardcode；后续可上提到 IdentityEngineConfig）
# =============================================================================

# F · bbox 物理约束：归一化面积过小 / 长宽比异常 → 跳过 tier_c。
# 5% 是按 bench 实测定的（背景小目标 4.3% / 前景主体 65.7%，差距巨大）；
# 实际部署里若主体可能远到 <5%，再下调。
_TIER_C_MIN_BBOX_AREA_RATIO = 0.05
# 人体 bbox w/h 物理区间（1.8m 成年人）：
#   双臂紧贴身侧"立正" ~0.25 / 自然站手垂身侧 0.28–0.31 / 单手抬起 0.33–0.44 /
#   坐姿手抱腿 0.4–0.6 / 蹲 0.6–1.0 / 张开双臂 0.83–1.0 / 横躺 ~4.0
# 下界 0.20 让极端瘦长立正姿态也能通过（最瘦 ~0.22 留 10% 余量）；
# 上界 2.5 拦卧姿与 detector 漏检的细横条（卧姿 detector bbox 本就不准、不该入库）。
_TIER_C_MIN_BBOX_ASPECT = 0.20
_TIER_C_MAX_BBOX_ASPECT = 2.5

# pHash 距离仅作观测、**已不再作为 tier_c 拒绝阈值**(身份判定交给 omni 同人校验):
# tier_c 新 crop 与 tier_a body 的最小 hamming 距离仍会算并落 sidecar
# `phash_min_dist_vs_tier_a` 供观测/将来 calibrate,但不据此拒入(故不再保留裸阈值常量)。
# 历史 bench 标定阈值 = 28,仅留作 calibrate 参考。pHash 64-bit 汉明距离语义:
#   0-5 几乎相同；6-15 同人不同光照/姿势；16-25 同场景不同细节；25+ 明显不同。
#   bench:不同人 tier_a 互比 26–33 / 随机噪声 24–33——与"同场景"区间重叠,正是
#   pHash(刻画整图含背景的明暗布局,非身份)不适合做身份门的原因。

# 锐度(Laplacian 方差)地板:tier_c 候选画面过糊则不入库。复用工程既有"太糊"基线 50
# (与 extractor._GATE_SHARPNESS_MIN / tier_u sharpness_min 同口径)。实测库内 tier_a body
# 最低 88、tier_c body 最低 236,均远高于 50,故 50 仅兜底拦退化帧、不误杀正常样本;
# 后续可据写时 sidecar 的 sharpness 分布再共识调整。
_TIER_C_MIN_SHARPNESS = 50.0

# tier_c 防遮挡门: 单人 crop 因人体框相互遮挡会混入他人躯干, 污染在线累积样本库。
# 每窗算"当前 track 框 与他人 track 框的交集面积 / 当前框面积"(IoA, 非 IoU——分母是
# 当前框, 衡量"当前 crop 有多少被他人覆盖"), 取与他人最大值; ≥ 此比例则本窗不入 tier_c
# (也不走 omni 校验)。与人脸在场门 E5 同级, 计算落在 process() 人脸关联同一段。
_TIER_C_MAX_BODY_OVERLAP_RATIO = 0.05


# =============================================================================
# Dead track GC grace 窗口（真实世界秒数；构造时按 engine_fps 换算成帧数）
# =============================================================================

# 仅需覆盖 SortTracker max_age 内 IoU 再接同 track_id 的窗口（默认 max_age_sec=1s），
# 超过 max_age 后 SortTracker 不会复用 ID，再长的 grace 都是死内存。3s 是 3x max_age
# 的工程余量——盖住进程/网络抖动即可。inflight=True 的 state 由 _gc_dead_tracks 单独
# 保护，不靠 grace 兜底。
_DEAD_TRACK_GRACE_SEC = 3.0


# =============================================================================
# Helpers
# =============================================================================


def _max_overlap_ratio(
    box: tuple[int, int, int, int],
    others: list[tuple[int, int, int, int]],
) -> float:
    """``box`` 与 ``others`` 各框交集面积占 ``box`` 自身面积的最大比例 (IoA)。

    分母固定是当前框面积 (非并集), 衡量"当前 crop 有多少被他人覆盖"——这正是
    遮挡污染的判据。无 others / box 退化 (面积<=0) 时返 0.0。
    """
    x1, y1, x2, y2 = box
    area = max(0, x2 - x1) * max(0, y2 - y1)
    if area <= 0 or not others:
        return 0.0
    best = 0.0
    for ox1, oy1, ox2, oy2 in others:
        iw = min(x2, ox2) - max(x1, ox1)
        ih = min(y2, oy2) - max(y1, oy1)
        if iw <= 0 or ih <= 0:
            continue
        ratio = (iw * ih) / area
        if ratio > best:
            best = ratio
    return best


def _bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """两框 IoU（交集 / 并集）。退化框返 0.0。reject_region 覆盖判定用。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = min(ax2, bx2) - max(ax1, bx1)
    ih = min(ay2, by2) - max(ay1, by1)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _normalize_bbox_to_1000(
    bbox_xyxy: tuple[int, int, int, int] | None,
    frame: NDArray[np.uint8] | None,
) -> tuple[int, int, int, int] | None:
    """把像素坐标 bbox 归一化到 mimo 标准的 [0, 1000] 整数区间。

    送给 omni 的 video 被 ``_encode_video_mp4`` 按短边 512 等比缩放，原始像素
    bbox 在 prompt 里没有锚定意义。归一化后，omni 用相对位置
    就能把 track_id 正确挂到 video 里看到的人；同时与上游分辨率解耦。

    返回 None 的情况：bbox 缺失、frame 缺失、frame 维度异常。
    """
    if bbox_xyxy is None or frame is None:
        return None
    h, w = frame.shape[:2]
    if w <= 0 or h <= 0:
        return None
    x1, y1, x2, y2 = bbox_xyxy
    # 钳到 [0, 1000]，避免 bbox 越界（理论上 SortTracker 已 clip 过，多一层防御）
    def _scale(v: int, dim: int) -> int:
        return max(0, min(1000, int(round(v * 1000 / dim))))
    return (_scale(x1, w), _scale(y1, h), _scale(x2, w), _scale(y2, h))


def _bbox_center_norm(
    bbox_xyxy: tuple[int, int, int, int] | None,
    frame: NDArray[np.uint8] | None,
) -> tuple[float, float] | None:
    """track box 中心的归一化坐标 (cx, cy) ∈ [0,1];bbox/frame 缺失返 None。

    复用 ``_normalize_bbox_to_1000`` 的 [0,1000] 结果再 /1000,与送 omni 的 bbox
    同坐标系(供 [Identity] 状态 log 观察 track 在画面里的位置)。
    """
    nb = _normalize_bbox_to_1000(bbox_xyxy, frame)
    if nb is None:
        return None
    x1, y1, x2, y2 = nb
    return ((x1 + x2) / 2000.0, (y1 + y2) / 2000.0)


def _short_pid(person_id: str | None, name_lookup: dict[str, str]) -> str | None:
    """把 person_id 渲染成便于观察的短串：优先真实姓名（如"李四"），否则取
    uuid 第一段（``8278c2c9-4c28-...-349c`` → ``8278c2c9``）。空值（None/"")原样返回。"""
    if not person_id:
        return person_id
    name = name_lookup.get(person_id)
    if name:
        return name
    return person_id.split("-", 1)[0]


def _short_face(face_val: str, name_lookup: dict[str, str]) -> str:
    """缩短 get_face_id_value 渲染值里内嵌的 person_id，保留语义前缀。

    取值形态见 get_face_id_value：none / pending / pending:<id> / <id>(confirmed)
    / unknown* —— 仅后两种含 uuid，其余原样透传。
    """
    if face_val.startswith("pending:"):
        return "pending:" + (_short_pid(face_val[len("pending:") :], name_lookup) or "")
    if face_val in ("none", "pending", "unknown") or face_val.startswith(
        ("unknown-", "unknown_")
    ):
        return face_val
    # 其余即 confirmed 的裸 person_id
    return _short_pid(face_val, name_lookup) or face_val


@dataclass
class TierCCandidate:
    """异步写库队列项: commit 命中时入队的"待校验/待写 tier_c"候选(设计文档 E7)。

    body/face crop 均为入队时脱离 live frame 的深拷贝; 由后台 worker
    消费(观测 pHash/ReID/锐度 → omni 同人校验 → 写盘), 不阻塞感知主流程。
    """
    person_id: str
    track_id: int
    now_ts: float
    confidence: float
    reason: str
    body_crop: NDArray[np.uint8]
    face_crop: NDArray[np.uint8]
    reid_emb: "NDArray[np.float32] | None"
    bbox_area_ratio: float
    bbox_aspect: float
    # body_crop 的 Laplacian 方差(锐度);本阶段仅观测、不拦
    sharpness: float = 0.0


# =============================================================================
# IdentityEngine
# =============================================================================


class IdentityEngine:
    """身份识别系统编排器。"""

    def __init__(
        self,
        config: IdentityEngineConfig,
        library: IdentityLibrary,
        dispatcher: EvidenceDispatcher,
        scope_label: str = "",
        device_id: str = "",
        engine_fps: float = 3.0,
        period_sec: float = 4.0,
        tier_u_pool: "TierUPool | None" = None,
        omni_config: "OmniConfig | None" = None,
    ) -> None:
        """
        Args:
            scope_label: 该 engine 的人物 ID 渲染前缀字面量（如 ``"客厅-dev0"``）。
                         per-camera 多实例化场景下由 PerceptionEngine 分配，保证跨镜头
                         unknown 编号天然全局唯一（``unknown-{scope_label}-{idx}``）。
                         空字符串走老格式 ``unknown_{idx}``，向后兼容。
                         **仅用于人物 ID 渲染 / 日志, 不再用作 cam_id (v2 重构后)**。
            device_id:   米家 device_id (前端 ``device list`` 可见的稳定标识)。用作
                         ``self.cam_id`` 与 ``_deep_sort_trackers`` dict key, 跟 ``pool
                         fetch --cam`` 入参命名空间统一。空字符串走 fallback "default"
                         (向后兼容老调用 / 单测)。
            engine_fps:  engine 处理帧率（来自 ``config.input.fps``）。用于把 grace
                         秒数换算成帧数，跟 SortTracker 的 max_age_sec → frames 同源同步。
                         默认 3.0 仅供单测/向后兼容场景。
        """
        self._validate_config(config)
        self.config = config
        self.library = library
        self.dispatcher = dispatcher
        self.scope_label = scope_label
        # cam_id:陌生人池里给 push_crop 用的 camera 标识。**与 PerceptionEngine
        # 注入 _deep_sort_trackers 的 dict key 必须严格同源** —— provider 拿
        # CropEntry.cam_id 反查 tracker 取 emb,两边漂移会让 emb 取不到、池退化
        # 为"只累积、不去重"。v2 重构后两边都用米家 device_id 作 key, 命名空间跟
        # ``pool fetch --cam`` 入参统一; 老 scope_label 改名 + 仅留渲染用途。
        self.cam_id: str = cam_id_from_device_id(device_id)
        # 人类可读设备名(如"小米智能摄像机C700")。engine 自身只持 cam_id(=did),名字由
        # PerceptionEngine 每窗从 snapshot.device.name 挂上(见 pipeline._run_device),供
        # tier_c sidecar 记 camera_name。未挂时留空。
        self.device_name: str = ""
        # 陌生人池 (TierUPool):可选注入。unknown/pending track 的 body crop 累积到池里,
        # 给后续主动注册流程取数;commit confirmed 时调 close_write_gate 关闭该 cluster
        # 全部成员。未注入时本期所有"接池逻辑"安全 no-op,老路径完全不受影响。
        self.tier_u_pool = tier_u_pool

        # grace_frames 按真实秒数 × fps 换算；fps 改动后 grace 自动跟随。
        # 至少 1 帧防极端配置导致 0。
        self._dead_track_grace_frames: int = max(1, round(_DEAD_TRACK_GRACE_SEC * engine_fps))
        # 仅供冷却日志把 frame_index 余量换算成"窗 / 秒"展示: frame_index 每窗推进 fps×period_sec、
        # 每实际秒推进 fps(见 api.py _global_frame_index)。不参与任何阈值判定。
        self._engine_fps: float = max(engine_fps, 1e-6)
        self._frames_per_window: float = max(1.0, engine_fps * period_sec)

        # tier_c 写库冷却(秒) = mult × 写库门 × 快重审间隔(秒) = 2 × 6 × 10 = 120s; 在此按 engine_fps
        # 换算成 frame_index 帧数计冷却(用 frame_index 而非墙钟 now_ts: 确定性、按窗口走, now_ts
        # 是毫秒帧时间戳跟秒阈值比会错位)。秒标定、改 fps 墙钟时长不漂移。写一张后置
        # state.tier_c_cooldown_until_frame = 当前窗帧 + 本值, 冷却期内只验身份、不晋升
        # (process 冻结 write_eligible)。
        _stab = config.stability
        _cooldown_sec = (
            _stab.tier_c_cooldown_mult
            * _stab.write_eligible_min_count
            * _stab.recheck_interval_accumulating_sec
        )
        self._tier_c_cooldown_frames: int = max(1, round(_cooldown_sec * self._engine_fps))

        # per-track state
        self._states: dict[int, TrackIdentityState] = {}
        # 每个 track 上一次出现在 active 集合中的 frame_index，用于 dead track GC
        self._last_seen_frame: dict[int, int] = {}

        # per-engine 自增 unknown 编号（仅 track_based + distinguish=true 时启用）。
        # 配合 ``scope_label`` 渲染成 ``unknown-{scope_label}-{idx}``——scope_label 由
        # 上层（PerceptionEngine）分配，保证跨镜头唯一；本字段每个 engine 自己维护，
        # 无需跨 engine 协调或加锁。
        self._next_unknown_index: int = 1

        # 最近一帧（用于 commit 时 crop body 累积 Tier C）
        self._latest_frame: Optional[NDArray[np.uint8]] = None
        # 当前窗口 frame_index(每窗 process 早期更新)。tier_c 写库冷却用它(而非墙钟 now_ts)
        # 计数, 确定性、按窗口走; worker 写盘时读它作冷却锚点。
        self._cur_frame_index: int = 0
        self._latest_bbox: dict[int, tuple[int, int, int, int]] = {}  # track_id → xyxy
        # 每窗口同步：True ⟺ tracking_results 里这个 track 本帧真有检测命中
        # （非 coasting / 纯 Kalman 预测残留）。消费点：omni 候选收集 + tier_c
        # 写入入口；为 False 时这两处直接早退，避免残留框污染 tier_c 与 omni。
        self._detected_this_frame: dict[int, bool] = {}
        # face 在场写库门 + prompt face 标签 + 陌生人池 face_crop 三处共用源
        # (tier_c 污染修复): 每窗口 process() 早期 face 几何关联算一次, 存 track →
        # matched face_detection 对象 (没关联到的 track 不进 dict)。三处消费:
        #   1) _enqueue_tier_c_candidate 的 E5 face 在场闸 (查 key 存在)
        #   2) omni 候选填 IdentityQueryItem.face_visible (查 key 存在)
        #   3) _push_unknown_tracks_to_pool 取 matched face 做 face_crop (查 value)
        # 容忍度: 关联到任何 face_detection 即算在场 (不卡侧/正脸); 不验脸是不是对的人。
        self._face_match_this_window: dict[int, Any] = {}
        # tier_c 防遮挡门 (污染修复): 每窗 process() 人脸关联同段算一次, 存 track →
        # True ⟺ 本窗该 track 人体框被他人框遮挡 ≥ _TIER_C_MAX_BODY_OVERLAP_RATIO。
        # 消费点: _enqueue_tier_c_candidate 与 E5 人脸门同级早退 (不入库、不走 omni)。
        self._overlap_other_person: dict[int, bool] = {}
        # reject_region（默认关）：no_person 落定时登记的屏幕区域 (bbox_xyxy, 过期墙钟 ts)，
        # 区域内全新静止 track 直接预标 no_person、被 confirmed 真人覆盖或 TTL 到期解除。
        self._no_person_regions: list[tuple[tuple[int, int, int, int], float]] = []

        # 身份库变化监听：每窗口对比 library 的 person 快照，发现差异（成员新增/删除、
        # 或某成员 tier_a 样本指纹变化）时把所有 track 的身份字段清空回 pending，
        # 让下一窗口立即重派 omni 用新 gallery 重新判定。track_id / inflight 保留。
        # 只比 tier_a 指纹、不含 tier_c——tier_c 写入不触发全体重判，避免脏样本扩散。
        # None = 启动首窗口（仅记录、不触发重置）；list_persons 异常时保持上次值。
        # 快照元素 = (person_id, tier_a 指纹)，指纹是 ((文件名, mtime), ...)。
        self._last_library_snapshot: Optional[
            frozenset[tuple[str, tuple[tuple[str, float], ...]]]
        ] = None

        self._started = False
        self._closed = False

        # tier_c 异步写库(设计文档 E7): commit 命中只做便宜门 + 深拷贝 crop(O(1)), 把
        # 同人校验(~2s omni)+写盘 经 run_coroutine_threadsafe 丢到**持久 main_loop** 跑,
        # 不阻塞感知主流程。⚠️ 不能用 asyncio.create_task——感知每窗跑在 asyncio.run 起的
        # 临时 loop 上, 窗末收尾会把 create_task 起的协程 cancel(实测 CancelledError, 候选
        # 永远写不进)。main_loop 由 PerceptionEngine 经 set_main_loop 注入(client 层持久 loop)。
        self._omni_config: "OmniConfig | None" = omni_config
        self._main_loop: "asyncio.AbstractEventLoop | None" = None
        # 写库前同人校验是否**实际生效** = 开关开 且 注入了 omni_config。开关默认开,
        # 但若没接 omni_config(误用/漏传/部分单测)不崩溃, 降级为无异步身份门(上游
        # write_eligible 连续门仍兜底)并启动打 ERROR 告警(生产路径 api.py 恒传
        # self._config.omni, 不会触发降级)。
        self._tier_c_verify_active: bool = bool(
            self.config.tier_c_verify_enabled and omni_config is not None
        )
        if self.config.tier_c_verify_enabled and omni_config is None:
            logger.error(
                "tier_c_verify_enabled=True 但未注入 omni_config: 写库前同人校验降级为"
                "无异步身份门(上游 write_eligible 连续门仍兜底)。"
                "生产路径应在 build_identity_engine 传 omni_config。"
            )
        # 校验 omni 调用低并发, 不与感知主调用抢限流额度(信号量首次 acquire 时绑定 main_loop)。
        self._tier_c_verify_sem = asyncio.Semaphore(1)

    # =========================================================================
    # lifecycle
    # =========================================================================

    @staticmethod
    def _validate_config(config: IdentityEngineConfig) -> None:
        """启动校验：非法配置组合直接抛错。"""
        if config.tracking == "track_free" and config.stranger.distinguish:
            raise ValueError(
                "IdentityEngine 配置非法：tracking=track_free 不支持 stranger.distinguish=true "
                "（无 track_id 无法稳定区分陌生人编号）。请改为 track_based 或 distinguish=false。"
            )
        if config.tracking == "track_free":
            raise NotImplementedError(
                "IdentityEngine.tracking=track_free 暂未实施"
            )

    def start(self) -> None:
        """启动 dispatcher（fused 同步路径无 worker，留 hook 给 future separate 恢复）。"""
        if self._started:
            return
        self._started = True
        # 历史上 SeparateDispatcher.start() 在此调用；separate 模式归档后此处空操作。
        # FusedDispatcher 没有 worker queue，无需启动后台任务。

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # tier_c 写库协程跑在持久 main_loop 上(best-effort 富化), 不在此显式取消:
        # 在途的 ~2s 校验/写盘让它在 main_loop 上自然跑完, app 关停时随该 loop 收尾。
        await self.dispatcher.close()

    def set_main_loop(self, loop: "asyncio.AbstractEventLoop") -> None:
        """注入持久主 event loop(uvicorn app loop)。tier_c 写库协程经它用
        run_coroutine_threadsafe 调度, 从而脱离每窗临时 loop(asyncio.run)的生命周期、不被
        窗末 cancel。由 PerceptionEngine 在拿到/创建 engine 时调一次(幂等)。"""
        self._main_loop = loop

    # =========================================================================
    # process（每个窗口由 run_identity 调一次）
    # =========================================================================

    async def process(
        self,
        tracking_results: list[dict[str, Any]],
        latest_frame: NDArray[np.uint8] | None,
        frame_index: int,
        now_ts: float | None = None,
        face_detections: list[Any] | None = None,
    ) -> tuple[dict[int, str], dict[int, tuple[int, int, int, int]]]:
        """处理一个窗口的 tracking 结果。

        返回 ``({track_id: face_id_value}, {track_id: bbox_xyxy_norm})``：第一项是
        各 active track 的身份渲染值；第二项是本帧真实检测到的 track 的末帧归一化
        [0,1000] bbox（coasting 不含），供上层挂到 ``IdentityTarget`` 给名册注入位置。

        Args:
            tracking_results: ``SortTracker.get_tracking_results()`` 的输出
            latest_frame:     窗口最后一帧 BGR 图（用于 crop body）
            frame_index:      累计帧序号（重审周期判定用）
            now_ts:           当前时间戳；None 时 fallback 到 ``time.time()``
            face_detections:  本窗口最新一帧的 FACE 类检测列表（来自
                              ``tracking_service.tracker.last_detections``,
                              filter class_id == CLASS_FACE)。给 unknown crop
                              push 进陌生人池时关联同帧 face 用,不给默认走
                              face_crop=None(P0 旧行为)。
        """
        if not self.config.enabled:
            return {tr["id"]: "none" for tr in tracking_results}, {}

        if now_ts is None:
            now_ts = time.time()

        self._latest_frame = latest_frame
        self._cur_frame_index = frame_index  # tier_c 写库冷却锚点(帧计数, 非墙钟)

        # ----- 0. 监听 identity_lib 变化（新增 / 删除 / tier_a 或 tier_c 样本变化）-----
        # 一次 list_persons 同时供给 snapshot 比对和后面构造 name_lookup（避免双 IO）。
        # IO 异常时把 person_refs 标记为 None，监听层据此保留上次 snapshot 不误触发重置。
        person_refs: Optional[list[PersonRef]] = None
        try:
            person_refs = self.library.list_persons()
        except Exception:  # noqa: BLE001
            logger.warning("library.list_persons 异常，本窗口跳过身份库变化监听", exc_info=True)
        self._maybe_reset_on_library_change(person_refs, now_ts)

        # ----- 1. 维护 state（创建/promote/timeout）-----
        active_track_ids: set[int] = set()
        for tr in tracking_results:
            tid = int(tr["id"])
            active_track_ids.add(tid)

            # 缓存最新 bbox + 本帧检测命中标志（tier_c / omni 两处消费点用）
            self._latest_bbox[tid] = tuple(tr["xyxy"])  # type: ignore[arg-type]
            self._detected_this_frame[tid] = bool(tr.get("detected_this_frame", True))
            self._last_seen_frame[tid] = frame_index

            state = self._states.get(tid)
            if state is None:
                state = TrackIdentityState(track_id=tid)
                self._states[tid] = state

            # 写库连续计数冻结点(每窗口统一处理, 不依赖 omni 是否回流): confirmed track
            # 在以下两种情况清 0, 强制"写库资格"从干净的连续段重攒——
            #   1) 写库冷却期内: 出冷却后须从头连续攒 N 次才再写(实现写库样本时序间隔)。
            #   2) 已有候选在途(in_flight): 出结果前不让计数再涨, 防重复入队。
            # 注: 早先这里还会在 coasting(本帧无检测命中)时清 0, 防他人继承 track id 顶替。
            # 但低 fps 下真本人走动时瞬时漏检频繁, 而写库计数只在稀疏重审点累加、却被每个漏检窗
            # 打回 0 → 移动的真本人写库资格几乎永远攒不满(只有静止的人能写)。已移除该清零: 顶替
            # 由"重审矛盾清零(计数级)+ 写库前 omni 同人校验拿实际 crop 比对 tier_a 否决(crop 级)"
            # 双重兜底, coasting 清零是会误杀真本人的冗余代理; 入队检测门(D)仍不收 coasting 窗的
            # 帧、不会写 Kalman 幻影框。
            if state.status == "confirmed" and (
                frame_index < state.tier_c_cooldown_until_frame
                or state.in_flight_tier_c
            ):
                state.write_eligible_count = 0

            # no_person track 相对锚点明显移动 → 回 pending 重新识别（疑似被路过真人 ID-switch 带走）。
            # 静态误检框不动则维持 no_person（停派发、不导出），稳态下持续存活、不重注入 omni。
            if state.status == "no_person" and self.config.no_person.enabled:
                clear_no_person_on_motion(
                    state,
                    self._latest_bbox.get(tid),
                    self.config.no_person.motion_clear_displacement_ratio,
                    self.config.no_person.motion_clear_min_abs_px,
                )

            # SortTracker 给出该 track，本帧首次见 → promote 到 pending
            promote_to_pending(state, now_ts)

            # 检查 pending 超时（强制 unknown）；分配统一走 _allocate_unknown_index
            if check_pending_timeout(state, now_ts, self.config.stability):
                state.unknown_index = self._allocate_unknown_index()

        # ----- 1.5 计算"本窗每个 track 关联到的 face_detection" (三处共用源) -----
        # 几何关联 (IoA + 匈牙利, 纯计算, 不引入新模型)。一份结果共享给三处消费:
        # tier_c 写入闸 (查 key 存在) / omni 候选 face_visible 字段 (查 key 存在) /
        # _push_unknown_tracks_to_pool 拿 face_crop (查 value 取 Detection 对象)。
        self._face_match_this_window.clear()
        if face_detections:
            from miloco.perception.engine.identity.extractor import (
                _associate_face_to_body,
            )
            tr_by_id_for_face = {int(tr["id"]): tr for tr in tracking_results}
            for tid in active_track_ids:
                tr = tr_by_id_for_face.get(tid)
                if tr is None:
                    continue
                pseudo_body = type("PseudoBody", (), {
                    "xyxy": tuple(tr["xyxy"]),
                    "bbox": tuple(tr.get("bbox", tr["xyxy"])),
                })()
                matched = _associate_face_to_body(pseudo_body, face_detections, None)
                if matched is not None:
                    self._face_match_this_window[tid] = matched

        # ----- 1.6 计算"本窗每个 track 是否被他人框遮挡" (tier_c 防遮挡门, 与 E5 同级) -----
        # IoA = 交集面积 / 当前框面积; 取当前 track 与其他 active track 的最大值, ≥ 阈值标记。
        # 写入闸在 _enqueue_tier_c_candidate 消费 (查 True 早退, 不入库不走 omni)。
        # 只纳入本帧真有检测命中的框: 与 omni 候选收集 (step 2 的 detected_this_frame 跳过)
        # 同口径——coasting 纯 Kalman 残留框不对应本帧真人, 算进去会造成"假遮挡"误杀。
        self._overlap_other_person.clear()
        if len(active_track_ids) >= 2:
            boxes = {
                tid: self._latest_bbox[tid]
                for tid in active_track_ids
                if tid in self._latest_bbox
                and self._detected_this_frame.get(tid, False)
            }
            for tid, box in boxes.items():
                ratio = _max_overlap_ratio(box, [b for o, b in boxes.items() if o != tid])
                if ratio >= _TIER_C_MAX_BODY_OVERLAP_RATIO:
                    self._overlap_other_person[tid] = True

        # ----- 2. 收集需要派发 omni 的 track -----
        # 构造真名反查表（仅供下方 [Identity] 状态 log 把 person_id 渲染成姓名；
        # 身份先验不再进 prompt，candidates 不带姓名）。复用 step 0 已拿到的
        # person_refs，避免双 list_persons 调用。
        name_lookup: dict[str, str] = {}
        if person_refs is not None:
            for ref in person_refs:
                if ref.name:
                    name_lookup[ref.person_id] = ref.name

        # 身份漂移自检(commit 后无脸 body-only 误跟踪纠正): 在收集 candidate 前施加,
        # enforce 撤回的 track 本窗即作去先验 candidate 一并重判(与 library 变化重置同款
        # "重置→本窗重派"语义)。mode=off 时 O(1) 早退。
        self._run_drift_check(active_track_ids, now_ts, name_lookup)

        # reject_region（默认关）：区域级记忆，压同位置反复误检的 omni 重问。放在收集 candidate
        # 之前，使预标的 no_person track 本窗即不进派发。
        self._apply_reject_regions(active_track_ids, now_ts)

        candidates: list[IdentityQueryItem] = []
        for tid in active_track_ids:
            state = self._states[tid]
            if not needs_omni_call(
                state, frame_index, now_ts,
                self.config.dispatch.min_interval_sec,
                self.config.stability,
                self._engine_fps,
            ):
                continue
            # coasting（人离开/跟丢后纯 Kalman 预测残留）的 track 当窗口不进 omni
            # 候选：bbox 已不对应本帧真人，让 omni 看到只会催生背景误判。track
            # 本身仍在 _states 里存活，不影响 ID/face_id/tier_u 连续性。
            if not self._detected_this_frame.get(tid, False):
                continue
            body_crop = self._extract_body_crop(tid, latest_frame)
            # fused 模式下 body_crop 不发给 omni（fused 用 video 自身）；separate 必须有 crop
            if self.config.omni_call_mode == "separate" and body_crop is None:
                continue
            # 不向 prompt 注入该 track 的当前/疑似身份（去先验）：身份先验会锚定 omni
            # 复读旧答案，破坏 update_evidence「连续 N 次独立同答才 commit」计数器赖以
            # 成立的投票独立性。omni 每窗只凭 bbox 定位 + gallery 视觉独立识别；重审/
            # 确认全由 engine 侧状态机管理（confirmed/unknown 的当前身份不进 candidates）。
            # prompt 判置信引导: face_visible 是上游 face_detections 几何关联结果。
            # None 表示本窗 face_detections 未传 (engine 未填), prompt 不渲染该
            # 字段; True/False 是确定性事实, prompt 据此引导 omni 调置信。
            face_visible_val: Optional[bool]
            if face_detections is not None:
                face_visible_val = tid in self._face_match_this_window
            else:
                face_visible_val = None
            candidates.append(IdentityQueryItem(
                track_id=tid,
                body_crop=body_crop,
                bbox_xyxy_norm=_normalize_bbox_to_1000(
                    self._latest_bbox.get(tid), latest_frame,
                ),
                face_visible=face_visible_val,
                # 已落定身份(confirmed 成员 / unknown 陌生人)被再次派发 = 周期性重审;
                # 当前 prompt 不区分首次/重审(去先验),该字段已无消费方,仅填充、保留供将来差异化处理。
                is_recheck=state.status in ("confirmed", "unknown"),
            ))
            mark_dispatched(state, frame_index, now_ts)

        # ----- 3. 调 dispatcher 派发 -----
        if candidates:
            gallery_snapshot = self._build_gallery_snapshot()
            # 已 confirmed 的身份 → 占用的 track_id 映射；让 dispatcher 在
            # deliver_response 时给"挂到他人 track 上的同一身份"打 dup_id 标记。
            # 同一身份多 track 占位时（理论不该发生，但容错）取最早 commit 的那个。
            # 只看 active_track_ids：grace 内的死 track 已经不在本帧 omni 派发的
            # candidates 里，让它参与身份占位是多余的，且会导致同一人短暂离开-
            # 回来时新 track 被 dup_id 误标 unknown 直到旧 track 被 GC。
            confirmed_pid_owner: dict[str, int] = {}
            for tid_chk in active_track_ids:
                st_chk = self._states[tid_chk]
                if (
                    st_chk.status == "confirmed"
                    and st_chk.committed_person_id
                    and st_chk.committed_person_id not in confirmed_pid_owner
                ):
                    confirmed_pid_owner[st_chk.committed_person_id] = tid_chk
            await self.dispatcher.dispatch(
                candidates=candidates,
                gallery_snapshot=gallery_snapshot,
                on_result=self._make_on_result(now_ts=now_ts),
                confirmed_pid_owner=confirmed_pid_owner,
            )

        # ----- 3.5. 陌生人池累积:unknown / pending track 的 crop 推到 TierUPool -----
        # 仅在 pool 注入时启用;commit confirmed 后 cluster gate 自动关闭(见 _on_result)。
        # ⚠️ 零额外推理:ReID embedding 由 pool 自己问 ReIDProvider(读跟踪侧 deque 末尾),
        # 本函数从不调 HumanReID.extract_feature。
        if self.tier_u_pool is not None and latest_frame is not None:
            self._push_unknown_tracks_to_pool(
                tracking_results, active_track_ids, latest_frame, frame_index, now_ts,
                face_detections=face_detections,
            )
            self.tier_u_pool.flush_if_due()
            self.tier_u_pool.tick_ttl()
            self.tier_u_pool.gc_lru_if_over_budget()

        # ----- 4. 清理已死 track 的 state（保留 last_bbox 缓存到 GC）-----
        # SortTracker 已停止返回 + grace 帧未再现 + 无 inflight dispatcher 调用 → 视为彻底死亡
        # grace 取 dead_track_grace_frames（远大于 SortTracker.max_age 与 dispatch.stale_threshold）
        # 防止 long-running 实例 _states / _last_seen_frame / _latest_bbox 无限增长
        self._gc_dead_tracks(active_track_ids, frame_index)

        # ----- 5. 返回当前 face_id 映射 + 末帧归一化 bbox -----
        # bbox_norm 只对本帧真实检测到的 track 填（coasting 纯预测残留不填，避免给
        # 名册注入幻影位置）；与 candidates 的 bbox 同源同坐标系（_latest_bbox +
        # _normalize_bbox_to_1000），供上层挂到 IdentityTarget 给名册渲染 (名, bbox)。
        out: dict[int, str] = {}
        bbox_norm: dict[int, tuple[int, int, int, int]] = {}
        for tid in active_track_ids:
            state = self._states[tid]
            out[tid] = get_face_id_value(
                state,
                distinguish=self.config.stranger.distinguish,
                scope_label=self.scope_label,
            )
            if self._detected_this_frame.get(tid, False):
                nb = _normalize_bbox_to_1000(self._latest_bbox.get(tid), latest_frame)
                if nb is not None:
                    bbox_norm[tid] = nb

        # ----- 6. Identity 状态 log (观察"已知卡 unknown"类问题) -----
        # 每窗口输出一条紧凑 summary, 用户排查时配合 [Identity/omni] log 看
        # 状态机如何转移。无 active track 时不打, 避免噪音。
        # status / cand / comm 三个字段直接打出来 (排除 face_id_value 的歧义)。
        if active_track_ids:
            track_descs = []
            for tid in active_track_ids:
                st = self._states[tid]
                ctr = _bbox_center_norm(self._latest_bbox.get(tid), latest_frame)
                pos = f"|cx={ctr[0]:.2f},cy={ctr[1]:.2f}" if ctr is not None else ""
                track_descs.append(
                    f"t{tid}|face={_short_face(out[tid], name_lookup)}|status={st.status}"
                    f"|cand={_short_pid(st.candidate_person_id, name_lookup)}"
                    f"|comm={_short_pid(st.committed_person_id, name_lookup)}"
                    f"(stab={st.stability_count},conf={st.best_conf:.2f}){pos}"
                )
            logger.info(
                "[Identity] cam=%s active=%d | %s",
                self.cam_id, len(active_track_ids), " | ".join(track_descs),
            )
        return out, bbox_norm

    # =========================================================================
    # fused mode 桥接 API（给 omni.py 用）
    # =========================================================================

    def take_fused_pending(self):
        """fused 模式下从 dispatcher 取出本窗口缓存的 candidates + gallery。"""
        if not isinstance(self.dispatcher, FusedDispatcher):
            return None
        return self.dispatcher.take_pending()

    async def deliver_fused_response(self, assignments: list[dict]) -> None:
        if not isinstance(self.dispatcher, FusedDispatcher):
            logger.warning("deliver_fused_response 调用但当前 dispatcher 非 FusedDispatcher")
            return
        await self.dispatcher.deliver_response(assignments)

    async def deliver_fused_failure(self, reason: str) -> None:
        if not isinstance(self.dispatcher, FusedDispatcher):
            return
        await self.dispatcher.deliver_failure(reason)

    # =========================================================================
    # 公共查询
    # =========================================================================

    def get_face_id_value(self, track_id: int) -> str:
        """单 track 的 face_id 值（用于下游 IdentityTarget.face_id 渲染）。"""
        state = self._states.get(track_id)
        if state is None:
            return "none"
        return get_face_id_value(
            state,
            distinguish=self.config.stranger.distinguish,
            scope_label=self.scope_label,
        )

    def get_state(self, track_id: int) -> TrackIdentityState | None:
        return self._states.get(track_id)

    def reset(self) -> None:
        """清空所有 state（场景切换或测试用）。"""
        self._states.clear()
        self._last_seen_frame.clear()
        self._latest_bbox.clear()
        self._detected_this_frame.clear()
        self._face_match_this_window.clear()
        self._overlap_other_person.clear()
        self._latest_frame = None
        # 本 engine 的 unknown 编号归 1——scope_label 保证了跨镜头唯一性，
        # 单 engine 内部 reset 不影响别的 engine 的编号空间。
        self._next_unknown_index = 1
        # 监听层 snapshot 也回 None，让 reset 后的首窗口仅记录、不误触发重置
        # （reset 后 _states 是空的，重置也无害，但保持"启动语义"与首次启动一致）
        self._last_library_snapshot = None

    # =========================================================================
    # 身份库变化监听
    # =========================================================================

    def _maybe_reset_on_library_change(
        self, person_refs: Optional[list[PersonRef]], now_ts: float,
    ) -> None:
        """对比 library snapshot；发现差异时清空所有 track 的身份字段。

        snapshot 元素：``(person_id, tier_a 的 body+face (文件名,mtime) 指纹)``。
        只看 **tier_a**（身份的权威参考，body 与 face 同等对待）——其增 / 删 / 替换 /
        touch 都会改指纹 → 触发全体重判。
        **tier_c 不在触发面内**：tier_c 是抗外观漂移的累积层，自动写入会持续 churn，
        若纳入会形成"写库 → 重判 → 重攒 → 再写库"自喂环、并殃及无关 track（连同写库
        冷却一起被清）；tier_c 的增删/替换靠 gallery composite 自身的 (文件名,mtime)
        指纹缓存自然生效（下次 recheck 即用新参考图），无需强制全体重判。

        **故意不监听 name/role 变化**：snapshot 元素不含 name/role 字段。用户改真名
        或家庭角色只换了"展示名",身份(person_id) 没变、tier_a/tier_c 样本也没变,识别
        不需要重派, prompt 里的 ``name_lookup`` 每窗口实时从 ``list_persons``(读 meta.json)
        构造、下一轮自然用新名(改名时 update_person 已即时同步 meta.json,见 person/router)。
        把 name/role 纳入 snapshot 会让纯改名也触发全量
        promote_to_pending、丢掉所有 track 的 stability_count 累积——代价不值。

        三种边界保护：
          - person_refs is None（读库异常）→ 保留上次 snapshot 不更新，避免误触发
          - 启动首窗口（_last_library_snapshot is None）→ 仅记录、不触发重置
          - snapshot 一致 → no-op
        """
        if person_refs is None:
            return
        cur = frozenset(
            (ref.person_id, ref.tier_a_fingerprint)
            for ref in person_refs
        )
        if self._last_library_snapshot is None:
            self._last_library_snapshot = cur
            return
        if cur == self._last_library_snapshot:
            return
        added = cur - self._last_library_snapshot
        removed = self._last_library_snapshot - cur
        logger.info(
            "identity_lib 变化 → promote_all_to_pending (active_tracks=%d, "
            "added=%d, removed=%d)",
            len(self._states), len(added), len(removed),
        )
        self._promote_all_to_pending(now_ts)
        self._last_library_snapshot = cur

    def _promote_all_to_pending(self, now_ts: float) -> None:
        """library 变化时调：把所有 track 的身份字段清空、status 回 pending。

        保留：track_id、inflight、pending_started_ts 以外的运行时数据（last_seen_frame、
        latest_bbox 不在 state 里、不动）。inflight=True 的 track 让那笔 in-flight omni
        响应正常回流走 update_evidence——回流时 candidate/stability_count 已清零，
        相当于"从零开始累积证据"，不会污染。
        """
        for state in self._states.values():
            state.status = "pending"
            state.candidate_person_id = None
            state.stability_count = 0
            state.best_conf = 0.0
            state.committed_person_id = None
            state.unknown_index = None
            state.last_omni_call_frame = 0
            state.last_omni_call_ts = 0.0
            state.pending_started_ts = now_ts
            state.consecutive_recheck_unmatched = 0
            # 写库相关也清: 回 pending 后写库资格从头攒(确认后才再攒), 冷却作废
            state.write_eligible_count = 0
            state.tier_c_cooldown_until_frame = 0
            # 翻转态也清: library 变化是"去先验全员重识别", 非翻转——残留 reverted 会让重识别
            # 误用 flip 阈值 / 豁免 60s 超时。
            state.reverted_from_confirmed = False
            state.flip_recheck_count = 0
            # drift 态也清(与 _revoke_track_to_pending 对齐): 全员去先验后旧的低窗累计 /
            # 采信复认抑制都作废, 否则重新 confirmed 后会少 1 窗即撤 / 错误抑制一次撤回。
            state.drift_consec_low = 0
            state.drift_suppressed_pid = None
            # inflight 不动

    def _revoke_track_to_pending(
        self, state: TrackIdentityState, now_ts: float,
    ) -> None:
        """把**单个** track 的身份字段清空、status 回 pending(字段清单照搬
        ``_promote_all_to_pending``)。

        身份漂移自检 enforce 撤回用:走"新目标识别"语义(去先验、不走任何重审),下一
        fused 窗作去先验 candidate 重新识别。inflight / in_flight_tier_c 不动(让在途
        响应正常回流;回流时 candidate/stability 已清零,相当从零累积)。``drift_*``
        字段由调用方按采信复认护栏单独处理(此处不碰)。
        """
        state.status = "pending"
        state.candidate_person_id = None
        state.stability_count = 0
        state.best_conf = 0.0
        state.committed_person_id = None
        state.unknown_index = None
        state.last_omni_call_frame = 0
        state.last_omni_call_ts = 0.0
        state.pending_started_ts = now_ts
        state.consecutive_recheck_unmatched = 0
        state.write_eligible_count = 0
        state.tier_c_cooldown_until_frame = 0
        # 翻转态也清: drift 撤回是"该 track 已不是同一人, 去先验干净重判", 非翻转——不黏旧名
        # (committed 已清)、不用 flip 阈值。与 hysteresis 翻转(apply_recheck_result 置 reverted)
        # 正交: drift 只盯 confirmed, mid-flip track 是 pending → 运行期不互撞, 此处仅防残留。
        state.reverted_from_confirmed = False
        state.flip_recheck_count = 0

    def _run_drift_check(
        self,
        active_track_ids: set[int],
        now_ts: float,
        name_lookup: dict[str, str],
    ) -> None:
        """Track 身份漂移自检 —— 每窗对每个"已绑成员的 confirmed track"比对其当前外观
        质心与该 person 近期同摄 TierC 参考质心(cos),累计 ``consecutive_windows`` 个低窗
        (``sim < threshold``;sim 回升即清 0,无数据窗 emb 不足/无参考 不计不清)入嫌疑集。
        补 commit 后人物交叉/交互致 track 跟错人的盲区。

        三档(``drift_check.mode``):off 早退;observe 算 sim + 打日志、不撤;enforce
        在 observe 基础上批量撤嫌疑集回 pending(丢回 omni 重判)+ 采信复认护栏。

        全程 body-only、**零额外推理**(track 质心读 tracker deque、参考质心读库里已落盘
        .npy)。在 inference 单线程(process)内施加,无需加锁。
        """
        cfg = self.config.drift_check
        if cfg.mode == "off":
            return

        suspects: list[int] = []
        for tid in active_track_ids:
            st = self._states.get(tid)
            # 只盯"已绑成员"的 confirmed track(unknown/陌生人 status≠confirmed 天然出射程)
            if st is None or st.status != "confirmed" or not st.committed_person_id:
                continue
            pid = st.committed_person_id

            # 采信复认护栏: 撤过且 omni 复认回同一 person → 不再二次撤; committed 变了即重新武装。
            # 取舍(设计内,防撤→复认→又撤震荡): 武装后该 (track, person) 的 body 自纠正会一直
            # 停摆到 committed 换成新身份才解除——其间若真发生漂移且当事人持续背身(omni 看不到脸、
            # 翻转不触发)会漏纠。多为误撤+复认的"其实就是本人"场景, 漏纠成本低; 如实测漏纠偏多,
            # 可改"复认后连续若干窗 sim 正常再解除"(本次不做)。
            if st.drift_suppressed_pid is not None:
                if st.drift_suppressed_pid == pid:
                    # 打一条便于线上 grep suppress 发生频率(为"是否加过期机制"攒数据); debug
                    # 级避免每窗每条刷 info。
                    logger.debug(
                        "[Identity/drift] cam=%s track_id=%d suppress skip (pid=%s)",
                        self.cam_id, tid, _short_pid(pid, name_lookup),
                    )
                    continue
                st.drift_suppressed_pid = None

            # track 质心(零额外推理); emb 不足不拿噪声质心误判(M=2 连续要求本身抗噪, min 可小)
            centroid: NDArray[np.float32] | None = None
            n_emb = 0
            if self.tier_u_pool is not None:
                centroid, n_emb = self.tier_u_pool.get_track_centroid(self.cam_id, tid)
            if centroid is None or n_emb < cfg.min_track_emb:
                continue

            # 参考质心(近期同摄 TierC, 退近期 TierA); 无参考不判
            ref, n_ref, ref_kind = self.library.get_person_recent_tier_c_centroid(
                pid, self.cam_id, cfg.recency_sec, now_ts,
            )
            if ref is None:
                continue

            sim = float(np.dot(centroid, ref))
            if sim < cfg.threshold:
                st.drift_consec_low += 1
            else:
                st.drift_consec_low = 0

            # 单独一条 [Identity/drift] 便于 grep / milog 染色; 只打原始 sim, 不标阈线
            logger.info(
                "[Identity/drift] cam=%s track_id=%d person=%s sim=%.3f ref=%s "
                "n_ref=%d n_emb=%d consec_low=%d",
                self.cam_id, tid, _short_pid(pid, name_lookup), sim, ref_kind,
                n_ref, n_emb, st.drift_consec_low,
            )

            if st.drift_consec_low >= cfg.consecutive_windows:
                suspects.append(tid)

        if cfg.mode != "enforce" or not suspects:
            return

        # enforce: 批量撤嫌疑集 → pending(身份已释放, 彼此不 dup_id), 下窗去先验一并重判
        for tid in suspects:
            st = self._states[tid]
            old_pid = st.committed_person_id
            self._revoke_track_to_pending(st, now_ts)
            st.drift_suppressed_pid = old_pid   # 武装采信复认护栏(防撤→复认→又撤震荡)
            st.drift_consec_low = 0
            logger.warning(
                "[Identity/drift] cam=%s track_id=%d 撤回身份 %s(body 漂移连续 %d 窗)"
                " → pending 丢回 omni 重判",
                self.cam_id, tid, _short_pid(old_pid, name_lookup),
                cfg.consecutive_windows,
            )

    # =========================================================================
    # 内部工具
    # =========================================================================

    def get_confirmed_track_ids(self) -> list[int]:
        """返回当前 state.status==confirmed 的 track_id 列表。

        用途: TierU fetch 时跟 confirmed track 做去重(case b 兜底)需要拿到这些
        track 的实时 emb 跟 pool 里 cluster centroid 比对。
        """
        return [tid for tid, st in self._states.items() if st.status == "confirmed"]

    def _allocate_unknown_index(self) -> int:
        """分配下一个 unknown 编号（distinguish=true 时用）。

        per-engine 内部计数器自增。跨镜头全局唯一性由 ``scope_label`` 前缀保证
        （``unknown-{scope_label}-{idx}``），不需要跨 engine 协调。

        并发约束：本 engine 的 ``process`` 主循环与 dispatcher ``on_result`` 回流
        都跑在同一 event loop 单线程，互斥执行——无需加锁。如果未来恢复 separate
        异步 dispatcher（worker 在独立任务里回流 on_result），需在此处加锁
        （asyncio.Lock 即可）。
        """
        idx = self._next_unknown_index
        self._next_unknown_index += 1
        return idx

    def _apply_reject_regions(self, active_track_ids: set[int], now_ts: float) -> None:
        """reject_region（默认关）维护。

        no_person 落定时登记屏幕区域（见 ``_on_result``）。本步：
          ① TTL 过期清理；
          ② 被 confirmed 真人覆盖（IoU ≥ clear_iou）的区域解除（真人确在那）；
          ③ 区域内"全新（从未派发）且本帧检测到的 pending track"直接预标 no_person，
             跳过 omni 重问——压住同位置反复误检的调用。
        预标的 track 仍受移动解除保护（真人移动即回 pending 重识别）。
        ``reject_region_enabled=false``（默认）时整体跳过，零行为影响。
        """
        np_cfg = self.config.no_person
        if not (np_cfg.enabled and np_cfg.reject_region_enabled):
            return
        if not self._no_person_regions:
            return
        # ① TTL 过期
        self._no_person_regions = [
            (b, exp) for (b, exp) in self._no_person_regions if exp > now_ts
        ]
        # ② 被 confirmed 真人覆盖的区域解除
        confirmed_boxes = [
            self._latest_bbox[t] for t in active_track_ids
            if self._states[t].status == "confirmed"
            and self._detected_this_frame.get(t, False)
            and t in self._latest_bbox
        ]
        if confirmed_boxes:
            self._no_person_regions = [
                (b, exp) for (b, exp) in self._no_person_regions
                if max((_bbox_iou(b, cb) for cb in confirmed_boxes), default=0.0)
                < np_cfg.reject_region_clear_iou
            ]
        # ③ 区域内全新 track 预标 no_person（跳过 omni）
        for tid in active_track_ids:
            st = self._states[tid]
            if not (
                st.status == "pending"
                and st.last_omni_call_frame == 0      # 全新、从未派发过
                and st.no_person_vote_count == 0
                and self._detected_this_frame.get(tid, False)
            ):
                continue
            box = self._latest_bbox.get(tid)
            if box is not None and any(
                _bbox_iou(box, rb) >= np_cfg.reject_region_clear_iou
                for rb, _ in self._no_person_regions
            ):
                st.status = "no_person"
                st.no_person_anchor_bbox = box
                logger.info(
                    "[Identity] cam=%s track_id=%d 落在 no_person reject_region → 预标 no_person（跳过 omni）",
                    self.cam_id, tid,
                )

    def _gc_dead_tracks(self, active_ids: set[int], frame_index: int) -> None:
        """清理 SortTracker 已不再返回、且超过 grace 帧未再现的 track state。

        保留有 inflight dispatcher 调用的 track，防止回流时找不到目标 state。
        grace 帧数由 ``__init__`` 时按 ``engine_fps`` 换算 ``_DEAD_TRACK_GRACE_SEC`` 得到。
        """
        threshold = frame_index - self._dead_track_grace_frames
        dead: list[int] = []
        for tid, state in self._states.items():
            if tid in active_ids:
                continue
            if state.inflight:
                continue
            if self._last_seen_frame.get(tid, frame_index) > threshold:
                continue
            dead.append(tid)
        for tid in dead:
            self._states.pop(tid, None)
            self._last_seen_frame.pop(tid, None)
            self._latest_bbox.pop(tid, None)
            self._detected_this_frame.pop(tid, None)
            self._face_match_this_window.pop(tid, None)
            self._overlap_other_person.pop(tid, None)

    def _build_gallery_snapshot(self) -> dict[str, GallerySamples]:
        """从 IdentityLibrary 取当前所有 person 的 gallery 快照。

        走 ``get_gallery_composites_for_omni`` 带 L1+L2 缓存的出口：返回的
        ``GallerySamples`` 里 ``body_composite_jpeg`` / ``face_composite_jpeg``
        已填好可直接 base64 塞入 prompt；``body_crops`` / ``face_crops`` 留空，
        稳态命中下省掉每窗口的 imread + resize + hstack + jpeg encode。
        高度/质量参数与 ``FusedPromptConfig`` 默认值（256 / 128 / 85）一致，
        cfg 改非默认时缓存自动按 fingerprint 失效重建。
        """
        return self.library.get_gallery_composites_for_omni(
            person_ids=None,  # 取全部
            body_n=self.config.gallery.body_refs_per_person,
            face_n=self.config.gallery.face_refs_per_person,
            cam_id=self.cam_id,  # tier_c 只取本相机子目录(per-cam 隔离)
        )

    def _extract_body_crop(
        self, track_id: int, frame: NDArray[np.uint8] | None,
    ) -> NDArray[np.uint8] | None:
        """从 frame 中按 bbox + padding 裁出 body crop。"""
        if frame is None:
            return None
        bbox = self._latest_bbox.get(track_id)
        if bbox is None:
            return None
        x1, y1, x2, y2 = bbox
        h, w = frame.shape[:2]
        bw, bh = x2 - x1, y2 - y1
        if bw <= 0 or bh <= 0:
            return None
        pad = self.config.body_crop_padding_ratio
        px = int(round(bw * pad))
        py = int(round(bh * pad))
        cx1 = max(0, x1 - px)
        cy1 = max(0, y1 - py)
        cx2 = min(w, x2 + px)
        cy2 = min(h, y2 + py)
        if cx2 <= cx1 or cy2 <= cy1:
            return None
        return frame[cy1:cy2, cx1:cx2].copy()

    def _push_unknown_tracks_to_pool(
        self,
        tracking_results: list[dict[str, Any]],
        active_track_ids: set[int],
        latest_frame: NDArray[np.uint8],
        frame_index: int,
        now_ts: float,
        *,
        face_detections: list[Any] | None = None,
    ) -> None:
        """对当前 unknown/pending track 累积 crop 到陌生人池。

        confirmed track 不 push——它们已经识别成功,样本会走 tier_c 累积路径而非池。

        face_detections 不为 None 时,对每个 body track 关联本帧 face_dets(IoU
        最大且过阈值的那个),把 face crop 跟 body 一起塞进 CropEntry。后续
        C 路径(register from-cluster)在 extract_from_pool 里跨帧 top-K 分发
        face,让用户注册到的样本带 face 备料。
        """
        from miloco.perception.engine.identity.tier_u import CropEntry

        # 用 dict 加速查 track confidence / xyxy。face_detections 参数保留只为
        # 签名兼容/可读性, 实际 face 关联结果走 self._face_match_this_window 共用
        # cache (process() step 1.5 已算过一次, 避免本函数再做几何关联重复计算)。
        tr_by_id = {int(tr["id"]): tr for tr in tracking_results}

        for tid in active_track_ids:
            state = self._states.get(tid)
            if state is None:
                continue
            # 已 confirmed 的 track 不进池——它们走 tier_c 累积路径。
            # no_person（非人误检）也不进池——杂物 crop 进陌生人池会污染聚类。
            if state.status in ("confirmed", "no_person"):
                continue
            tr = tr_by_id.get(tid)
            if tr is None:
                continue
            body_crop = self._extract_body_crop(tid, latest_frame)
            if body_crop is None or body_crop.size == 0:
                continue
            # 同帧 face 关联结果从 process() step 1.5 共用 cache 读 (避免重复几何关联)。
            # 关联到的 face_det 直接 crop 出来塞 CropEntry.face_crop。
            face_crop = None
            matched_face = self._face_match_this_window.get(tid)
            if matched_face is not None:
                fh, fw = latest_frame.shape[:2]
                fx1, fy1, fx2, fy2 = matched_face.xyxy
                fx1 = max(0, int(fx1))
                fy1 = max(0, int(fy1))
                fx2 = min(fw, int(fx2))
                fy2 = min(fh, int(fy2))
                if fx2 > fx1 and fy2 > fy1:
                    face_crop = latest_frame[fy1:fy2, fx1:fx2].copy()
            crop_entry = CropEntry(
                cam_id=self.cam_id,
                track_id=tid,
                frame_index=frame_index,
                captured_at=now_ts,
                body_crop=body_crop,
                face_crop=face_crop,
                sharpness=_compute_sharpness(body_crop),
                bbox_xyxy=tuple(tr["xyxy"]),  # type: ignore[arg-type]
                detector_conf=float(tr.get("confidence", 0.0)),
            )
            self.tier_u_pool.push_crop(crop_entry)

    def _make_on_result(self, *, now_ts: float) -> Callable[[OmniIdentityResult], Awaitable[None]]:
        """构造 dispatcher 的 on_result 回调（闭包绑定 now_ts）。"""

        async def _on_result(result: OmniIdentityResult) -> None:
            # 每次 omni 回结果先打一条 log (含 dispatcher 解析后的字段),
            # 配合 [Identity] state log 看状态机如何被驱动。
            logger.info(
                "[Identity/omni] cam=%s track_id=%d → person_id=%r conf=%.2f "
                "reason=%r dup_id=%s batch_size=%d",
                self.cam_id, result.track_id, result.person_id, result.confidence,
                result.reason, result.dup_id, result.batch_size,
            )
            state = self._states.get(result.track_id)
            if state is None:
                logger.warning("on_result 收到 unknown track_id=%d 的结果（state 已 GC）",
                               result.track_id)
                return

            # no_person：omni 判该框内确无人（非人误检）。累计票数，连续达阈值落定 no_person
            # （停派发 / 身份不导出 / 不进陌生人池）；confirmed 成员不被翻（见 record_no_person）。
            # 非 no_person 结果到达时清票，保证"连续"语义。confirmed 成员永不被 no_person 干扰。
            if self.config.no_person.enabled and result.no_person:
                committed = record_no_person(
                    state,
                    anchor_bbox=self._latest_bbox.get(result.track_id),
                    vote_threshold=self.config.no_person.vote_threshold,
                )
                if committed:
                    logger.info(
                        "[Identity] cam=%s track_id=%d 落定 no_person（非人误检，停派发 / 身份不导出）",
                        self.cam_id, result.track_id,
                    )
                    # reject_region（默认关）：登记该屏幕区域，抑制同位置反复误检的 omni 重问
                    np_cfg = self.config.no_person
                    anchor = self._latest_bbox.get(result.track_id)
                    if np_cfg.reject_region_enabled and anchor is not None:
                        self._no_person_regions.append(
                            (anchor, now_ts + np_cfg.reject_region_ttl_sec)
                        )
                        # 软上限防无限增长（TTL 过期已每窗清理，此处兜底极端多误检场景）
                        if len(self._no_person_regions) > 64:
                            self._no_person_regions = self._no_person_regions[-64:]
                return
            if state.no_person_vote_count:
                # omni 本窗看到人了 → no_person 连续性中断，清票
                state.no_person_vote_count = 0

            # D · 跨身份冲突兜底：dispatcher 检测出"omni 把已锁定身份挂到了
            # 另一个 candidate"——同一身份只能对应一个 track，物理上不可能两个
            # track 都是同一人。强制视为 unknown 处理（不让此 person_id 进入证据
            # 累积,state 走 None 路径,不 commit、不写 tier_c）。下窗口 omni 还能重判。
            effective_person_id = result.person_id
            if result.dup_id and effective_person_id is not None:
                logger.warning(
                    "track_id=%d 收到 dup_id 标记 (omni 给 person_id=%s 但已被另一 track 锁定)，"
                    "本次视为 unknown 处理",
                    result.track_id, effective_person_id,
                )
                effective_person_id = None

            # 按 state 当前 status 分流：confirmed → 重审；其他 → 普通累积
            if state.status == "confirmed":
                pre_committed = state.committed_person_id  # 保留 fell_back 前的 committed 给 log
                fell_back = apply_recheck_result(
                    state, effective_person_id, result.confidence, self.config.stability,
                    # dup_id 被掩成的 None 是"判成了另一个已占位的人"(矛盾), 不是弃权——
                    # 透传让 apply_recheck_result 走矛盾分支(清写库 + 快阀), 不当背对放过。
                    is_dup_id=result.dup_id,
                    # 看到脸却判 None = 对当前身份的否定(走矛盾分支可翻转); 没看到脸 = 背对弃权。
                    face_visible=result.face_visible,
                )
                if fell_back:
                    logger.info("track_id=%d 重审退回 pending（committed=%s）",
                                result.track_id, pre_committed)
                elif (
                    effective_person_id is not None
                    and effective_person_id == state.committed_person_id
                    and result.batch_size < 3
                ):
                    # 时序一致性核心: confirmed 重审一致时也尝试累积 tier_c, 让
                    # write_eligible_count 累计到 N 才真写入。多目标批次阈值放宽到
                    # >=3 才跳过 (见下方 batch_size>=3 注释)。
                    try:
                        self._enqueue_tier_c_candidate(
                            person_id=state.committed_person_id,
                            track_id=result.track_id, now_ts=now_ts,
                            confidence=result.confidence, reason=result.reason,
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "Tier C 入队失败 (重审一致路径) track_id=%d person_id=%s",
                            result.track_id, state.committed_person_id, exc_info=True,
                        )
            else:
                committed = update_evidence(
                    state, effective_person_id, result.confidence, self.config.stability,
                )
                # 翻转黏滞计数: 退回后每次重审回流(未 commit 且仍 pending+reverted)+1;
                # 黏满 flip_sticky_max_recheck 仍未定 → 放手, 交还正常状态机(此后正常超时/
                # 累积, 显示回落 pending), 挡 omni 乱跳时旧名长期挂错人(ID-switch)。
                if (
                    not committed
                    and state.status == "pending"
                    and state.reverted_from_confirmed
                ):
                    state.flip_recheck_count += 1
                    if state.flip_recheck_count >= self.config.stability.flip_sticky_max_recheck:
                        state.reverted_from_confirmed = False
                        logger.info(
                            "[Identity] cam=%s track_id=%d 翻转黏滞超限放手(committed=%s)",
                            self.cam_id, result.track_id, state.committed_person_id,
                        )
                if committed:
                    # 落定到 unknown：分配 unknown_index（与主循环共用 _allocate_unknown_index）
                    if state.status == "unknown" and state.unknown_index is None:
                        state.unknown_index = self._allocate_unknown_index()
                    # 落定到 confirmed:关闭陌生人池里该 track 所属 cluster 全部成员的
                    # 写入 gate(决策 1.1 α 行为)。该 track 之后的 crop 走 tier_c 累积。
                    # 用 ``self.cam_id`` 跟 push_crop 路径同源,不内联 ``or "default"``——
                    # 后者只是 ``cam_id_from_device_id`` 当前的实现,helper 改了这里
                    # 会漂移导致 close 找不到 entry、池永不关闭。
                    if state.status == "confirmed" and self.tier_u_pool is not None:
                        try:
                            self.tier_u_pool.close_write_gate(
                                self.cam_id,
                                result.track_id,
                            )
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "陌生人池 close_write_gate 异常 track_id=%d",
                                result.track_id, exc_info=True,
                            )
                        # 主动扫池: 用刚 confirmed track 的实时 emb 扫池子里其他
                        # cluster, 命中的同人 residual cluster 一并 close (case b:
                        # 同人多 track 没合到一起时, 此刻立即清掉, 不等下次 fetch)。
                        try:
                            self.tier_u_pool.close_same_person_clusters_by_track(
                                self.cam_id, result.track_id,
                            )
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "confirmed 主动清池失败 track_id=%d",
                                result.track_id, exc_info=True,
                            )
                    # 落定到 confirmed：tier_c 写入由后续 confirmed 重审"连续 N 次一致"
                    # 驱动；commit 当刻 write_eligible_count 刚清 0（见 update_evidence），
                    # 不在此入队。（旧"commit 即写库"入队块已删——新规则下它 count=0 恒被
                    # 时序一致性门挡下、永不写库，徒增日志噪音与误读。）

        return _on_result

    def _extract_face_crop(
        self, track_id: int, frame: NDArray[np.uint8] | None,
    ) -> NDArray[np.uint8] | None:
        """从本窗几何关联到的 face Detection 裁出 face crop(深拷贝)。无关联返回 None。
        取法与 _push_unknown_tracks_to_pool 的 face crop 同源(self._face_match_this_window)。"""
        if frame is None:
            return None
        matched = self._face_match_this_window.get(track_id)
        if matched is None:
            return None
        fh, fw = frame.shape[:2]
        fx1, fy1, fx2, fy2 = matched.xyxy
        fx1 = max(0, int(fx1))
        fy1 = max(0, int(fy1))
        fx2 = min(fw, int(fx2))
        fy2 = min(fh, int(fy2))
        if fx2 <= fx1 or fy2 <= fy1:
            return None
        return frame[fy1:fy2, fx1:fx2].copy()

    def _enqueue_tier_c_candidate(
        self, *, person_id: str, track_id: int, now_ts: float,
        confidence: float = 0.0, reason: str = "",
    ) -> None:
        """commit 命中: 跑便宜门(依赖本窗状态)+ 深拷贝 body/face crop, 入异步写库队列。

        O(1) 返回, 不 await 任何 omni/IO(那些交后台 worker)。门顺序:
        冷却/in_flight → D 本帧检测 → E5 本窗有脸 → 写库连续门(连续 N 次一致) → F bbox → 锐度。
        过门后置 in_flight + 归零 write_eligible(设计文档 E7 的 H1: worker 出结果前不重复入队)。
        """
        if not self.config.tier_c_accumulate_on_commit:
            return
        state = self._states.get(track_id)
        # 冷却 / 在途(同步段便宜门; in_flight 防 worker 出结果前同一 track 重复入队)
        # 各门跳过都打 info(频率≈每次重审一条, 不刷屏): 线上一眼看出为何没写库。
        if state is not None and self._cur_frame_index < state.tier_c_cooldown_until_frame:
            _remain = state.tier_c_cooldown_until_frame - self._cur_frame_index
            # 用 ceil 而非 round: 冷却中(_remain≥1)窗/秒恒 ≥1, 不会出现"~0 个窗"与"还在冷却"矛盾
            logger.info("tier_c 入队跳过 cam=%s track_id=%d person_id=%s (冷却中, 距出冷却 ~%d 个窗 / ~%d 秒)",
                        self.cam_id, track_id, person_id,
                        math.ceil(_remain / self._frames_per_window),
                        math.ceil(_remain / self._engine_fps))
            return
        if state is not None and state.in_flight_tier_c:
            logger.info("tier_c 入队跳过 cam=%s track_id=%d person_id=%s (已有候选在途 in_flight, 待 worker 出结果)",
                        self.cam_id, track_id, person_id)
            return
        # D · 本帧检测命中(coasting 残留框不入)
        if not self._detected_this_frame.get(track_id, False):
            logger.info("tier_c 入队跳过 cam=%s track_id=%d person_id=%s (D · 本帧无检测命中/coasting)",
                        self.cam_id, track_id, person_id)
            return
        # E5 · 本窗关联到 face(无脸不入, 省后续校验 omni)
        if track_id not in self._face_match_this_window:
            logger.info("tier_c 入队跳过 cam=%s track_id=%d person_id=%s (E · 本窗未关联到 face)",
                        self.cam_id, track_id, person_id)
            return
        # E6 · 防遮挡: 本窗人体框被他人框覆盖 ≥ 阈值(疑混入他人躯干), 不入库也不走 omni
        if self._overlap_other_person.get(track_id):
            logger.info("tier_c 入队跳过 track_id=%d person_id=%s (E6 · 本窗人体框与他人重叠 ≥%.0f%%, 疑混入他人)",
                        track_id, person_id, _TIER_C_MAX_BODY_OVERLAP_RATIO * 100)
            return
        # 写库连续门: 同一身份连续 N 次 confirmed 重审一致才入
        min_n = self.config.stability.write_eligible_min_count
        if (state.write_eligible_count if state else 0) < min_n:
            logger.info("tier_c 入队跳过 cam=%s track_id=%d person_id=%s (时序一致性门 · write_eligible_count=%d < %d)",
                        self.cam_id, track_id, person_id, state.write_eligible_count if state else 0, min_n)
            return
        # F · bbox 物理约束
        bbox = self._latest_bbox.get(track_id)
        frame = self._latest_frame
        if bbox is None or frame is None:
            logger.info("tier_c 入队跳过 cam=%s track_id=%d person_id=%s (bbox/frame 缺失)",
                        self.cam_id, track_id, person_id)
            return
        fh, fw = frame.shape[:2]
        if fw <= 0 or fh <= 0:
            return
        x1, y1, x2, y2 = bbox
        bw = max(0, x2 - x1)
        bh = max(0, y2 - y1)
        if bw == 0 or bh == 0:
            return
        area_ratio = (bw * bh) / (fw * fh)
        aspect = bw / bh
        if area_ratio < _TIER_C_MIN_BBOX_AREA_RATIO:
            logger.info("tier_c 入队跳过 cam=%s track_id=%d person_id=%s (F · bbox 面积 %.1f%% 过小)",
                        self.cam_id, track_id, person_id, area_ratio * 100)
            return
        if aspect < _TIER_C_MIN_BBOX_ASPECT or aspect > _TIER_C_MAX_BBOX_ASPECT:
            logger.info("tier_c 入队跳过 cam=%s track_id=%d person_id=%s (F · bbox 长宽比 w/h=%.2f 越界)",
                        self.cam_id, track_id, person_id, aspect)
            return
        # 深拷贝 body + face crop(脱离 live frame, 下窗帧缓冲会复用)
        body_crop = self._extract_body_crop(track_id, frame)
        if body_crop is None or body_crop.size == 0:
            return
        sharpness = _compute_sharpness(body_crop)
        if sharpness < _TIER_C_MIN_SHARPNESS:
            logger.info("tier_c 入队跳过 track_id=%d person_id=%s (锐度 %.0f < %.0f, 画面过糊)",
                        track_id, person_id, sharpness, _TIER_C_MIN_SHARPNESS)
            return
        face_crop = self._extract_face_crop(track_id, frame)
        if face_crop is None or face_crop.size == 0:
            return
        # reid emb(零额外推理, 读 track deque 末尾)入队快照, 供 .npy 存档
        reid_emb: NDArray[np.float32] | None = None
        if self.tier_u_pool is not None:
            try:
                reid_emb = self.tier_u_pool.get_track_embedding(self.cam_id, track_id)
            except Exception:  # noqa: BLE001
                logger.warning("tier_c 取 reid emb 异常 cam=%s track_id=%d person_id=%s, .npy 跳过",
                               self.cam_id, track_id, person_id, exc_info=True)
        cand = TierCCandidate(
            person_id=person_id, track_id=track_id, now_ts=now_ts,
            confidence=confidence, reason=reason,
            body_crop=body_crop, face_crop=face_crop, reid_emb=reid_emb,
            bbox_area_ratio=area_ratio, bbox_aspect=aspect,
            sharpness=sharpness,
        )
        # 把同人校验(~2s omni)+写盘丢到**持久 main_loop** 上跑。
        # ⚠️ 本函数运行在每窗临时 loop(asyncio.run, inference 线程)里, 窗末临时 loop 会被
        # asyncio.run 收尾 cancel——若用 asyncio.create_task 起协程会随之被杀(实测
        # CancelledError, 候选永远写不进库)。改用 run_coroutine_threadsafe 调度到 client 层
        # 捕获的持久 app loop(main_loop, 主线程), 协程脱离窗口生命周期、稳定跑完。
        loop = self._main_loop
        if loop is None:
            logger.warning(
                "tier_c 入队跳过 track_id=%d person_id=%s (main_loop 未注入, 异步写库不可用)",
                track_id, person_id,
            )
            return
        def _on_tier_c_done(f) -> None:
            # 候选已消费(成功/否决/异常): 清 in_flight(允许该 track 后续重攒+入队)+ 记异常。
            # 回调在 main_loop 线程上跑, 清单字段原子, 与感知线程读 in_flight 不冲突。
            st_done = self._states.get(track_id)
            if st_done is not None:
                st_done.in_flight_tier_c = False
            try:
                exc = f.exception()
            except Exception:  # noqa: BLE001  (取消等)
                return
            if exc is not None:
                logger.warning(
                    "tier_c 写库协程异常 track_id=%d person_id=%s: %r",
                    track_id, person_id, exc,
                )

        # 先置 in_flight + 清写库计数, 再调度: 否则若协程秒回(将来有人在首个 await 前加同步
        # 早退就会发生), done_callback 可能先把 in_flight 清 False, 本行再置 True → 永久卡
        # True, 该 track 此后被入队门一直挡住、再不写库。置位先于调度则与协程是否秒回无关。
        # 出结果前不重复入队(H1); 计数从 0 重攒。
        if state is not None:
            state.in_flight_tier_c = True
            state.write_eligible_count = 0
        try:
            fut = asyncio.run_coroutine_threadsafe(self._process_tier_c_candidate(cand), loop)
        except RuntimeError:
            # loop 已关停(app 退出): 回滚 in_flight, 不留"卡死"残留。
            if state is not None:
                state.in_flight_tier_c = False
            logger.warning("tier_c 调度失败 cam=%s track_id=%d person_id=%s (loop 已关停), 跳过",
                           self.cam_id, track_id, person_id)
            return
        fut.add_done_callback(_on_tier_c_done)

    async def _process_tier_c_candidate(self, cand: "TierCCandidate") -> None:
        """worker 内单候选门链: 记录观测指标 → (开关开)omni 同人校验 → 写盘 → 置长冷却。
        跑在持久 main_loop 上(经 _enqueue 的 run_coroutine_threadsafe 调度); in_flight 由
        _enqueue 注册的 done_callback 在本协程结束时统一清。"""
        # —— 观测指标(pHash 距离 / ReID 余弦 / 锐度): 仅记录(debug 日志 + sidecar 字段), 不拦;
        #    身份判定交给下方 omni 同人校验 ——
        try:
            min_dist = await asyncio.to_thread(
                self.library.tier_c_phash_check, cand.person_id, cand.body_crop,
            )
        except Exception:  # noqa: BLE001
            logger.warning("tier_c pHash 观测异常 person_id=%s", cand.person_id, exc_info=True)
            min_dist = None
        try:
            reid_cos_a, reid_cos_c = await asyncio.to_thread(
                self.library.tier_c_reid_cos_observe, cand.person_id, cand.reid_emb,
            )
        except Exception:  # noqa: BLE001
            logger.warning("tier_c ReID 余弦观测异常 person_id=%s", cand.person_id, exc_info=True)
            reid_cos_a, reid_cos_c = None, None
        logger.debug(
            "tier_c 观测 track_id=%d person_id=%s phash_vs_a=%s reid_cos_a=%s reid_cos_c=%s sharp=%.1f",
            cand.track_id, cand.person_id, min_dist, reid_cos_a, reid_cos_c, cand.sharpness,
        )

        # —— omni 1v1 同人校验(设计文档 E7); 仅当开关开且已注入 omni_config 时生效 ——
        verify: dict | None = None
        if self._tier_c_verify_active:
            verify = await self._run_tier_c_verify(cand)
            if verify is None:
                logger.info("tier_c 跳过 cam=%s track_id=%d person_id=%s (同人校验未完成/失败, 保守不写)",
                            self.cam_id, cand.track_id, cand.person_id)
                return
            passed = (
                verify["same_person"]
                and verify["confidence"] >= self.config.tier_c_verify_conf_threshold
            )
            if not passed:
                logger.info(
                    "tier_c 否决 cam=%s track_id=%d person_id=%s (校验 same=%s conf=%.2f reason=%r)",
                    self.cam_id, cand.track_id, cand.person_id,
                    verify["same_person"], verify["confidence"], verify["reason"],
                )
                return

        # —— 写盘(IO 入线程)——
        extra_meta = {
            "track_id": int(cand.track_id),
            "confidence": float(cand.confidence),
            "reason": cand.reason,
            "trigger": "fused_commit",
            # 样本来源相机(多相机下同一 person 的 tier_c 样本会来自不同相机,记来源便于
            # review 哪台贡献的样本 / 排查跨摄误累积)。did 是稳定键,name 是可读名。
            "camera_id": self.cam_id,
            "camera_name": self.device_name,
            "bbox_area_ratio": round(cand.bbox_area_ratio, 4),
            "bbox_aspect": round(cand.bbox_aspect, 3),
            "phash_min_dist_vs_tier_a": min_dist if min_dist is not None else -1,
            "reid_cos_vs_tier_a": round(reid_cos_a, 4) if reid_cos_a is not None else -1,
            "reid_cos_vs_tier_c": round(reid_cos_c, 4) if reid_cos_c is not None else -1,
            "sharpness": round(cand.sharpness, 1),
        }
        if verify is not None:
            extra_meta["verify_same_person"] = verify["same_person"]
            extra_meta["verify_confidence"] = round(verify["confidence"], 3)
            extra_meta["verify_reason"] = verify["reason"]
        await asyncio.to_thread(
            self.library.add_tier_c_sample,
            cand.person_id, cand.body_crop, cand.now_ts, "auto_accumulate", extra_meta, cand.reid_emb,
            cam_id=self.cam_id,
        )
        # 写成功 → 回协程置长冷却(2N 次重审帧, 拉开写入间隔); track 可能已 GC → 判 None。
        # in_flight 由 _enqueue 注册的 done_callback 在本协程结束时清。
        # 锚点用 self._cur_frame_index(写盘时的当前窗帧): 与 process()/needs_omni_call 比较冷却
        # 用的是同一 frame_index 计数(确定性、按窗口走); 相对入队帧仅差 worker 周转的几帧, 对
        # 120 单位(≈120s @fps=1)冷却可忽略; 冷却期 + in_flight 双重冻结 write_eligible, 不会重复入队。
        st = self._states.get(cand.track_id)
        if st is not None:
            st.tier_c_cooldown_until_frame = self._cur_frame_index + self._tier_c_cooldown_frames

    async def _run_tier_c_verify(self, cand: "TierCCandidate") -> "dict | None":
        """omni 1v1 同人校验(QUERY=本帧 body+face, GALLERY=tier_a body+face)。

        返回 {same_person, confidence, reason}; 读库/构图/调用任一失败返回 None(上层保守不写)。
        omni 调用走独立低并发 semaphore, 不抢感知主调用限流。图像操作入 to_thread。
        """
        from miloco.perception.engine.omni.omni_client import (
            call_omni,
            resolve_live_omni_config,
        )
        from miloco.perception.engine.omni.prompt_builder import (
            build_tier_c_verify_payload,
        )
        from miloco.perception.engine.omni.response_parser import (
            parse_tier_c_verify_response,
        )

        try:
            gallery_body, gallery_face = await asyncio.to_thread(
                self.library.get_tier_a_verify_crops, cand.person_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("tier_c 校验读 tier_a crops 失败 cam=%s person_id=%s", self.cam_id, cand.person_id, exc_info=True)
            return None
        if not gallery_body and not gallery_face:
            logger.info("tier_c 校验跳过 cam=%s person_id=%s (无 tier_a 参考)", self.cam_id, cand.person_id)
            return None
        payload = await asyncio.to_thread(
            build_tier_c_verify_payload,
            cand.body_crop, cand.face_crop, gallery_body, gallery_face,
        )
        if payload is None:
            return None
        try:
            async with self._tier_c_verify_sem:
                omni_cfg = (
                    resolve_live_omni_config(self._omni_config)
                    if self._omni_config is not None
                    else self._omni_config
                )
                raw = await call_omni(payload, omni_cfg, type="on_demand")
        except Exception:  # noqa: BLE001
            logger.warning("tier_c 同人校验 omni 调用失败 cam=%s person_id=%s", self.cam_id, cand.person_id, exc_info=True)
            return None
        return parse_tier_c_verify_response(raw)


# =============================================================================
# 工厂
# =============================================================================


def build_identity_library(library_root: str | Path | None = None) -> IdentityLibrary:
    """实例化 IdentityLibrary 单例（per-camera 多 engine 化场景下由调用方共享给所有 engine）。

    library_root 走 single source of truth：默认调 ``resolve_library_root``，与
    person/router 必然一致。调用方传入 library_root 仅用于测试 / 显式覆盖场景。
    """
    if library_root is None:
        from miloco.perception.engine.identity.config_loader import resolve_library_root
        root = resolve_library_root()
    else:
        root = Path(library_root)
        if not root.is_absolute():
            from miloco.config import get_settings
            root = get_settings().directories.workspace_dir / root
    lib = IdentityLibrary(root)
    # 启动时把 SQL person(name/role) 同步进文件层 meta.json：让改造前注册、没有
    # meta.json 的历史 person 补齐真名 / 角色，否则 omni 渲染退化为 UUID。幂等 +
    # best-effort（DB 不可达不阻塞 library 构造）。
    try:
        from miloco.perception.engine.identity.meta_sync import (
            sync_person_meta_from_sql,
        )
        sync_person_meta_from_sql(lib)
    except Exception:  # noqa: BLE001
        logger.warning("build_identity_library: meta sync 失败（忽略）", exc_info=True)
    return lib


def build_identity_engine(
    config: IdentityEngineConfig,
    library_root: str | Path | None = None,
    library: IdentityLibrary | None = None,
    scope_label: str = "",
    device_id: str = "",
    engine_fps: float = 3.0,
    period_sec: float = 4.0,
    tier_u_pool: "TierUPool | None" = None,
    omni_config: "OmniConfig | None" = None,
) -> IdentityEngine:
    """按配置实例化 IdentityEngine（含正确 dispatcher 注入）。

    ``omni_call_mode`` 取值：
      - ``"fused"``     —— 当前默认 / 已实现：识别合并到主调用，省一次 omni 请求
      - ``"separate"``  —— 占位，未实施；选此值会抛 ``NotImplementedError``

    Args:
        config:         engine 配置
        library_root:   library 根目录；仅当 ``library=None`` 时使用
        library:        预构造好的 ``IdentityLibrary`` 实例。per-camera 多 engine 化
                        场景下，调用方应**一次性** ``build_identity_library`` 后把
                        同一份 library 注入到所有 per-camera engine，保证：
                          - composite L1/L2 cache 跨镜头共享（同人重复编码省掉）
                          - tier_a/tier_c 写入 single source of truth
                          - list_persons / get_name / get_role 等查询同源
                        不传则按 ``library_root`` 自行实例化（向后兼容老入口）。
        scope_label:    unknown id 的 scope 前缀（如 ``"客厅-dev0"``）。透传给
                        ``IdentityEngine``，用于跨镜头 unknown 编号唯一化。
        engine_fps:     engine 处理帧率（应等于 ``config.input.fps``，跟 SortTracker
                        同源）。透传给 ``IdentityEngine``，让 dead track grace 帧数
                        按真实秒数换算，fps 调整后自动跟随。默认 3.0 仅供单测兜底。
        period_sec:     单窗时长（秒，应等于 ``config.input.period_sec``）。仅用于把
                        冷却 frame_index 余量在日志里换算成"窗 / 秒"展示，不参与阈值判定。
                        默认 3.0 仅供单测兜底。
    """
    if library is None:
        library = build_identity_library(library_root)

    dispatcher: EvidenceDispatcher
    if config.omni_call_mode == "separate":
        raise NotImplementedError(
            "omni_call_mode='separate' 暂未实施；当前请使用 'fused'。"
        )
    elif config.omni_call_mode == "fused":
        dispatcher = FusedDispatcher(config=config.dispatch)
    else:
        raise ValueError(f"unknown omni_call_mode: {config.omni_call_mode}")

    return IdentityEngine(
        config=config,
        library=library,
        dispatcher=dispatcher,
        scope_label=scope_label,
        device_id=device_id,
        engine_fps=engine_fps,
        period_sec=period_sec,
        tier_u_pool=tier_u_pool,
        omni_config=omni_config,
    )
