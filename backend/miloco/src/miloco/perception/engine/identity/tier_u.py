"""陌生人池(TierU) — unknown 阶段的轨迹 crop 池。

主流程 detector + 跟踪输出的 unknown track,在被 omni 确认身份前,所有 crop 累积
到本池;用户来注册时按 (cam, track_id) 或全局取数,让用户从已沉淀的候选中指认。
注册完成后 close_write_gate 把该 cluster 关闭。**全内存 / 重启即清 / 单文件无依赖**。

核心数据结构(详见各 dataclass docstring):
    CropEntry    : 一帧 body+face crop + 元数据
    TierUEntry   : 一个 (cam, track_id) 的累积条目;L1 原始帧池 + L2 精华 deque
    EquivClass   : 等价类索引,把同人不同 track 的 entry 挂到一个 cluster_id
    TierUPool    : 主类,持有所有 entry + cluster 注册表 + match_cache

设计三个不变量(必须时时满足,否则功能错):
    (I1) 写入 gate 关闭后 push_crop 静默丢弃(不抛、不累积),允许调用方无脑塞。
    (I2) L1 累计满 l1_capacity 才触发 flush;不到容量不晋级——保证"挑最优"有
         足够候选,而不是早期被随便一张占位。
    (I3) intra_cam 去重不物理合并 entry——两个 entry 都保留,通过 cluster_id 挂
         "等价"关系。这样各 entry 的 L1/L2/TTL 独立累积,逻辑解耦。

⚠️ 零额外推理硬约束:任何 ReID embedding **都从 ReIDProvider 取**(读跟踪侧
Track.features deque 末尾元素),严禁本文件调 HumanReID.extract_feature。
单测 ``test_tier_u_no_extra_extract`` 是护栏。
"""

from __future__ import annotations

import functools
import itertools
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import cv2
import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.identity._image_utils import (
    hamming as _hamming,
)
from miloco.perception.engine.identity._image_utils import (
    phash as _phash,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 公共 helper
# =============================================================================


def cam_id_from_device_id(device_id: str) -> str:
    """陌生人池 ``CropEntry.cam_id`` 的统一来源。

    多处用到:
      - ``IdentityEngine.cam_id``(push_crop 时 CropEntry.cam_id 用本值)
      - ``PerceptionEngine._deep_sort_trackers`` 的 dict key(provider 反查取 emb)

    **v2 重构**: 之前用 scope_label (``<room>-dev<idx>``) 作 cam_id, 跟前端 ``device
    list`` 拿到的米家 device_id 命名空间不一致, 导致 ``pool fetch --cam`` 用 device_id
    永远 0 候选。现在统一用米家 device_id 作 cam_id, 命名空间打通。日志 / 人物 ID 渲染
    场景继续用 scope_label (仍在 ``IdentityEngine.scope_label`` 字段)。

    两边漂移会让 provider get_embedding 拿不到 emb、池退化为"只累积、不去重",
    所以统一到本 helper 唯一来源。``device_id`` 非空时直接用, 否则 fallback "default"
    (允许 device_id="" 的单测 / 老路径不崩)。
    """
    return device_id or "default"


# =============================================================================
# 质量评分 (TierU 三处排序统一用)
# =============================================================================

# face_bonus: has_face 时给 quality_score 的乘法加成。
# 1.5 让 no_face 候选必须 quality_score 比 has_face 候选高 50% 才能反超,
# 比 TierA 的 1.2 更激进 — TierU 是给用户挑号,自然捕获场景下抓到人脸更重要。
# 不走 strict (has_face, q) 字典序的理由: 整 cluster 高质量 no_face 会被
# 低质量 has_face 永远压死, 反直觉。配合 cluster 之间排序后 post-sort 把
# 最高分 has_face 提到 #1, 头位体验仍有保障, 中后位允许"高质 no_face 上位"。
_FACE_BONUS_TIER_U: float = 1.5

# quality_score 三因子权重 (det > aspect > sharp):
# - det 0.5: 最直接的"YOLO 觉得这是标准 person"信号,贴近"易辨认"
# - aspect 0.3: bbox 比例接近站立人形 (0.4),跟"易辨认"强相关
# - sharp 0.2: 局部像素锐度,易被 motion artifact 误判,权重最低
_W_DET: float = 0.5
_W_ASPECT: float = 0.3
_W_SHARP: float = 0.2

# aspect 评分用的 "sweet spot": w/h = 1:2.5 = 0.4 = 站立人形标准比例。
# 偏离 sweet spot 越远 → aspect_score 越低。
_ASPECT_SWEET: float = 0.4

# sharpness 归一化参考值 (Laplacian variance), 沿用 extractor._SHARPNESS_NORM_REF。
# 跟 v4 §2.2.3 TierA 评分公式同口径。
_SHARPNESS_NORM_REF: float = 300.0


def _aspect_dist_normalized(
    aspect: float,
    aspect_min: float,
    aspect_max: float,
) -> float:
    """aspect 到 sweet spot 的归一化距离, ∈ [0, 1]。

    分段线性: 窄侧 [aspect_min, 0.4] 范围小、坡度陡(极瘦极易辨别且差);
    宽侧 [0.4, aspect_max] 范围大、坡度缓(横躺/侧坐 aspect 偏大但仍可辨)。
    天然不对称,符合"对宽的数据更宽容"的直觉。

    log 距离不行 — aspect=0.2 和 aspect=0.8 在 log 空间下距 0.4 相等(都是
    |log(2)|≈0.69),但前者是 1:5 极瘦差、后者接近方形更好辨认,直觉不符。

    边界值 aspect_min / aspect_max 处 → 1.0; sweet spot 处 → 0.0。
    """
    if aspect <= _ASPECT_SWEET:
        # 窄侧:范围窄 → 同样的 (sweet - aspect) 比例距离更大,坡度自然陡
        denom = _ASPECT_SWEET - aspect_min
        if denom <= 0:
            return 0.0
        return min(1.0, max(0.0, (_ASPECT_SWEET - aspect) / denom))
    else:
        # 宽侧:范围宽 → 同样的 (aspect - sweet) 比例距离更小,坡度自然缓
        denom = aspect_max - _ASPECT_SWEET
        if denom <= 0:
            return 0.0
        return min(1.0, max(0.0, (aspect - _ASPECT_SWEET) / denom))


def _crop_aspect(c: "CropEntry") -> float:
    """从 body_crop 形状算 w/h aspect。crop 缺失时 fallback sweet spot (不扣分)。"""
    if c.body_crop is None or c.body_crop.size == 0:
        return _ASPECT_SWEET
    h, w = c.body_crop.shape[:2]
    if h <= 0:
        return _ASPECT_SWEET
    return w / h


def quality_score(
    c: "CropEntry",
    *,
    aspect_min: float = 0.25,
    aspect_max: float = 2.0,
) -> float:
    """单 crop 的综合质量分, ∈ [0, 1.0] (无 face) / [0, 1.5] (有 face)。

    TierU 三处排序统一用: L1→L2 晋级、cluster 内 representative 选择、
    cluster 之间号码图排序。沿用 v4 §2.2.3 TierA 评分的"乘 face_bonus"风格,
    但 base 用加权和不是连乘 — 加法对单维度极低值更宽容 (任一为 0 不会让总分崩盘),
    且权重显式 (det > aspect > sharp) 更容易调。

    aspect_min / aspect_max 应与所在 TierUPool 的 config 同步 (默认值仅给单测 /
    pure 函数调用方便)。
    """
    det_norm = max(0.0, min(1.0, c.detector_conf))
    sharp_norm = max(0.0, min(1.0, c.sharpness / _SHARPNESS_NORM_REF))
    aspect_score = 1.0 - _aspect_dist_normalized(_crop_aspect(c), aspect_min, aspect_max)
    base = _W_DET * det_norm + _W_ASPECT * aspect_score + _W_SHARP * sharp_norm
    face_bonus = _FACE_BONUS_TIER_U if c.face_crop is not None else 1.0
    return base * face_bonus


# =============================================================================
# 配置
# =============================================================================


@dataclass
class TierUConfig:
    """陌生人池调参。

    **当前实施阶段**:本 dataclass 字段全部走代码默认值。``config.py`` 的
    ``identity_engine_config_from_dict`` sub_factories 还**没接** tier_u 段,
    yaml 里写 ``identity_engine.tier_u: ...`` 会被丢弃。如要 yaml 可调:
      1. ``config.py:IdentityEngineConfig`` 加 ``tier_u: TierUConfig`` 字段
      2. ``identity_engine_config_from_dict`` sub_factories 加 ``"tier_u":
         TierUConfig``(参考 ``"deep_sort": DeepSortConfigDC`` 同款)
      3. ``api.py:PerceptionEngine.__init__`` 把 ``self._config.identity_engine.
         tier_u`` 传给 ``TierUPool(config=...)``

    在 #1-#3 都补上之前,这里改默认值是唯一调整入口。
    """

    enabled: bool = True
    l1_capacity: int = 20                    # per (cam, track),累计满才晋级
                                             # (30→20:fast mode 大部分 L1 帧拉不到 emb,
                                             # 选优实际只看 sharpness,20 张已足够覆盖
                                             # 候选,直接降单 entry 内存天花板 ~33%)
    l2_capacity: int = 10                    # per (cam, track),FIFO deque
    fetch_window_sec: float = 300.0          # list 模式默认窗口(用户没给时间 hint
                                             # 时用)。by-id register / track 查询不
                                             # 走 window,真实兜底是
                                             # ttl_inactive_sec=72h。SKILL 话术里时间
                                             # 表述按 agent 实际传的 --window 复述,
                                             # 不再硬编码 5 min。
    ttl_inactive_sec: float = 259200.0       # 72 hr —— 用户偶尔翻池子的频率是 2-3 天,
                                             # 给"周末/短假回家发现陌生人"留 UX 余地
                                             # (36 hr 在工作日通勤 OK,周末场景容易丢)
    memory_budget_mb: int = 128              # LRU 兜底上限。l1_capacity=20 + l2=10 =
                                             # 30 crops/entry, body 上限 256 高 + face
                                             # 上限 128 高 → ~3-4 MB/entry。
                                             # 家用稳态 5-10 entries ≈ 15-40 MB,远未到
                                             # 128 MB;极端场景 ~36 entries 才接近上界,
                                             # 触发 LRU 弹最旧 entry(本就是兜底机制)。
                                             # 改 l1/l2_capacity 或 *_crop_max_height
                                             # 后需同步该估算(避免预算与实际容量脱钩)。
    reid_threshold_intra_cam: float = 0.85   # 入库去重(同 cam)余弦阈值
                                             # 0.9→0.85:body ReID 同人不同姿势/光照
                                             # 经验范围 0.85-0.95,0.9 卡边缘易把同人
                                             # 早期 track(emb 跟后期 mean centroid 偏远)
                                             # 当 singleton。0.85 给同人合并留足余量,
                                             # 家用场景成员少误合风险可控
    reid_threshold_cross_cam: float = 0.85   # 用时去重(跨 cam)余弦阈值(与 intra 同口径)
    # fetch 末尾跟 tier_c 逐张样本比对的余弦阈值(严于 cross_cam 0.85)。
    # tier_c 内部样本质量参差(累积了不同瞬间外观,衣着/姿态差异大),用统一 0.85
    # 容易"看起来像但不是同人"误隐藏真陌生人。拉到 0.90 宁愿漏判少几个,不要
    # 误隐藏。逐张比对(不 mean): tier_c 样本差异大, mean 后反而模糊化。
    reid_threshold_tier_c_dedup: float = 0.90
    # quality gate(crop 进 L2 前过滤)
    area_ratio_min: float = 0.05
    aspect_min: float = 0.25                 # w/h ≥ 0.25 = 不接受比 1:4 更瘦的 bbox
                                             # (0.20→0.25:1:5 极瘦杆即使 sharpness 高
                                             # 也不利辨认,直接源头丢)
    aspect_max: float = 2.0                  # w/h ≤ 2.0 = 横向不超过 2:1
                                             # (2.5→2.0:横躺/侧坐自然到 1.5-1.8 已够,
                                             # >2.0 多半是非人形误检 / 部分遮挡)
    sharpness_min: float = 50.0
    detector_conf_min: float = 0.4
    # crop 入池前的等比缩放上限。对齐 omni gallery composite 设计(body 高 256 / face
    # 高 128);源图比目标小不动(不 upscale 避免无意义放大)。
    # 收益:大 body(原始 400~800 高)→ 96~256 高,内存 -60~90%。
    # 下游影响评估:
    #   - HumanReID 抽 emb 输入 (96,192):本来就 resize 降采样,256 高完全够
    #   - omni gallery composite (body 高 256 / face 高 128):对齐设计值,无 resize 损失
    #   - 注册前端号码图 (_resize_h(body, 256)):对齐设计值
    #   - omni 单 crop encoding (512, 512):正方形强扭曲,本就在损失,该路径不当底线
    # 设 0 或负值视为关闭 resize(回到存原分辨率)。
    body_crop_max_height: int = 256
    face_crop_max_height: int = 128
    # 内部参数(故意不暴露 yaml)
    match_cache_capacity: int = 1000
    l1_first_dedup_ratio: float = 0.20       # L1 累积达到 capacity × 该比例时触发首次 dedup
                                             # (G 方案折中:延迟 ~6 帧让 DeepSORT 累几个 emb 更稳,
                                             #  避免首帧单 emb noise 高)
    g_retry_interval_frames: int = 5         # G 方案 emb 拉取失败后的重试间隔(帧)
                                             # mode=fast + skip_windows=4 下,静止 track 的 ReID
                                             # 可能 6 秒才出产,首次拉不到不能"一次就死"。
                                             # 5 帧 ≈ 5 s @ 1 fps,够 DeepSORT 累出第一条 emb。


# =============================================================================
# 数据结构
# =============================================================================


@dataclass
class CropEntry:
    """单帧的 body+face crop + 元数据。push_crop 接受的最小单元。

    ``reid_embedding`` 字段在 crop 入 L2 那一刻从 ReIDProvider 拉快照(零额外推理,
    读跟踪侧 Track.features deque 末尾)。这让 cluster 代表特征能 mean 到
    "所有 cluster member 的所有 L2 crop 的 emb",样本数比"每 entry 单 emb"多
    一个数量级,centroid 更稳。
    """

    cam_id: str
    track_id: int
    frame_index: int
    captured_at: float
    body_crop: NDArray[np.uint8]
    face_crop: NDArray[np.uint8] | None
    sharpness: float
    bbox_xyxy: tuple[int, int, int, int]
    detector_conf: float
    reid_embedding: NDArray[np.float32] | None = None  # 入 L2 时拉的 emb 快照,128-dim L2-normalized


@dataclass
class TierUEntry:
    """单个 (cam_id, track_id) 的累积条目。

    L1 是"原始帧池",所有 push_crop 进的 CropEntry 都先在这里堆;累计满
    ``l1_capacity`` 后批量评估(quality_gate + sharpness 排序),挑最优 1 张推 L2,
    然后 L1 整体清空。L2 是 FIFO deque,容量 ``l2_capacity``。

    ReID 字段在 L1 晋级后由 ``flush_if_due`` 调 ReIDProvider 快照填入(零额外
    推理——provider 内部读跟踪侧 deque)。``embedding_dirty`` 是脏标记驱动入库
    去重的关键:只在 emb 更新时才参与下一轮 intra_cam 比对。
    """

    cam_id: str
    track_id: int
    crops_l1: list[CropEntry] = field(default_factory=list)
    crops_l2: deque[CropEntry] = field(default_factory=deque)
    last_l1_push_ts: float = 0.0             # TTL 判定基准

    # ReID
    reid_embedding: NDArray[np.float32] | None = None  # 128-dim,来自 ReIDProvider 快照
    embedding_snapshot_ts: float = 0.0
    embedding_sharpness: float = 0.0
    cluster_id: str | None = None            # 入库去重后挂到的等价类
    embedding_dirty: bool = False            # 脏标记,intra_cam dedup 驱动

    # 写入 gate(commit 后关)
    write_open: bool = True

    # G 方案触发状态:L1 累积达 capacity × l1_first_dedup_ratio 时尝试拉 emb +
    # 跑 dedup。emb 拉到才置 did_first_dedup=True;拉不到时只更新
    # last_g_attempt_frame 限频,下窗口接着试。覆盖短命 track(< L1 容量帧消失)
    # 的去重需求,同时避免 mode=fast + skip_windows=4 下"6 秒时 ReID 还没产出
    # 就标记 done"导致 singleton 永不合并。
    did_first_dedup: bool = False
    last_g_attempt_frame: int = -1                # 上次 G 方案尝试拉 emb 的 frame_index

    @property
    def key(self) -> tuple[str, int]:
        return (self.cam_id, self.track_id)

    def memory_bytes(self) -> int:
        """粗算占用字节(用于 LRU eviction 判断,不要求精确)。"""
        n = 0
        for c in itertools.chain(self.crops_l1, self.crops_l2):
            if c.body_crop is not None:
                n += c.body_crop.nbytes
            if c.face_crop is not None:
                n += c.face_crop.nbytes
        if self.reid_embedding is not None:
            n += self.reid_embedding.nbytes
        return n


@dataclass
class EquivClass:
    """同人的多 (cam, track_id) 等价类。

    极简定义:仅持有 ``cluster_id`` + ``members``;representative 与 is_cross_cam
    都是 members 的派生属性,按需 lazy 计算,不存。
    """

    cluster_id: str
    members: set[tuple[str, int]] = field(default_factory=set)


@dataclass
class ClusterCandidate:
    """fetch 返回的注册候选,聚合了一个 cluster 内的所有 entry 信息。"""

    cluster_id: str
    members: list[tuple[str, int]]            # 跨 cam 时多个
    representative_crop: CropEntry            # 簇内 sharpness 最高
    total_crops: int                          # 簇内全部 L2 crop 数
    span_cam_count: int
    earliest_ts: float
    latest_ts: float
    # 决策 α 用:展开每 cam 的代表 crop 让用户视觉复核
    per_cam_representative: dict[str, CropEntry] = field(default_factory=dict)
    # cluster 内全部 L2 crops(extract_from_pool 注册时用,挑差异化 top-K)
    # 单 cam 多帧累积时 L2 多张;之前的 representative + per_cam 只暴露 1~N 张,
    # 单 cam 场景退化成 1 张样本入库——加 all_l2_crops 暴露全集让下游用全。
    all_l2_crops: list[CropEntry] = field(default_factory=list)
    # cluster 内全部 L1 crops(raw,尚未过 quality gate 晋级到 L2)。
    # 用作 L2 数不足(< 3)时的补足池——extractor 再跑一次 quality gate 过滤后用。
    # 防止"用户在摄像头前刚出现,L1 还没满 l1_capacity 帧 flush,L2 只 0~1 张"导致注册 candidate 不够。
    all_l1_crops: list[CropEntry] = field(default_factory=list)


# =============================================================================
# ReIDProvider Protocol
# =============================================================================


class ReIDProvider:
    """跟踪侧给陌生人池提供 ReID embedding 快照的协议。

    实现方(``DeepSortTracker``)读 ``Track.features`` deque 末尾元素返回,
    **零额外推理**——本接口不允许在内部触发 ``HumanReID.extract_feature``。
    """

    def get_embedding(self, cam_id: str, track_id: int) -> NDArray[np.float32] | None:
        """取 (cam, track) 当前的 ReID embedding 快照;不存在或未抽取过返回 None。"""
        raise NotImplementedError

    def get_centroid(
        self, cam_id: str, track_id: int,
    ) -> tuple[NDArray[np.float32] | None, int]:
        """取 (cam, track) 的历史特征质心 + emb 数;同样零额外推理。

        默认实现退化为"无质心"——只读快照的旧 provider 无需实现本接口即可继续
        工作(身份漂移自检在该 provider 下自动 no-op)。
        """
        return None, 0


# =============================================================================
# 主类
# =============================================================================


def _synchronized(method):
    """给 TierUPool 公开读写入口加 per-pool RLock。

    陌生人池被【推理线程】(每窗 ``asyncio.run`` 临时 loop 上 push/flush/close/gc)与
    【API 主线程持久 loop】(``/pool/fetch|status|cluster-split`` 直调)同时访问——这是
    真正的跨 OS 线程并发(非单线程协程交错),零锁下 fetch 遍历 _entries/clusters 时
    推理线程并发改同一 dict → ``RuntimeError: dictionary changed size`` 或读到半更新。
    池方法全是同步段(无 await),锁只包同步段、绝不跨 await → **不串行化 omni**(与 §1.4
    M2 "同线程协程交错不加锁" 不矛盾:那是同线程,这里是跨线程必须加)。用 ``RLock`` 允许
    公开方法内部互调(如 fetch 内触发 close/merge)同线程重入;``threading`` 锁跨线程有效
    (``asyncio.Lock`` 跨 loop 失效,不可用)。
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class TierUPool:
    """陌生人池主类。

    职责:
    - ``push_crop``       : 接 IdentityEngine 每窗口送来的 unknown crop,写 L1
    - ``flush_if_due``    : L1 满 → 挑最优入 L2 → 拉 ReID 快照 → 触发入库去重
    - ``fetch``           : 注册取数,可选 (cam, track) 精确锁定或全局扫
    - ``close_write_gate``: commit 后关该 cluster 所有成员的写入 gate + 清残留
    - ``tick_ttl``        : 每窗口跑 TTL 清理
    - ``gc_lru_if_over_budget``: 内存超额时 LRU 兜底
    """

    def __init__(
        self,
        config: TierUConfig | None = None,
        reid_provider: ReIDProvider | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or TierUConfig()
        self._reid_provider = reid_provider
        self._now = now_fn

        # 跨线程访问锁(推理线程 vs API 主线程,见 _synchronized)。RLock 允许公开方法
        # 内部互调重入;threading 锁跨线程有效(asyncio.Lock 跨 loop 失效)。
        self._lock = threading.RLock()

        # 主存储
        self._entries: dict[tuple[str, int], TierUEntry] = {}
        self._clusters: dict[str, EquivClass] = {}

        # 跨 cam 用时去重的 match_cache(避免同对簇重复算余弦)
        # key = frozenset({cluster_id_a, cluster_id_b}),value = cosine sim
        self._match_cache: dict[frozenset[str], float] = {}
        # 所有成员 write_open=False 的 cluster_id 集合 (close_write_gate 是单次原子
        # 关全 cluster, write_open 单向 True→False 不可逆 → set 单调增长直到 cluster
        # 接新成员/被弹时移除)。给 close_same_person_clusters_by_track +
        # _cluster_pairwise_union 跳过已 close cluster 用, 避免重复算 centroid。
        self._fully_closed_clusters: set[str] = set()
        # centroid (cluster mean emb) 缓存: cluster_id → (signature, centroid)。
        # 省 _cluster_pairwise_union 跨 fetch 重复算 np.mean(K×crops×128)。
        # **正确性**靠内容指纹: signature = (member frozenset, 参与 mean 的 emb 数),
        # 簇内容一变 signature 对不上 → 自动重算, 无需在 push_crop / flush / merge /
        # close 等路径手动 invalidate(漏维护最坏"多算一次", 不会用 stale centroid 误合)。
        # **但内存回收指纹管不了**: cluster_id 被彻底弹掉(evict 最后一个成员 / merge 旧簇
        # 被吸收)后永不再被查 → 其 cache 项既不会被指纹重算也不会被删 → 单调泄漏
        # (issue #429 §4.2)。故 _evict_entry / _merge_into_cluster 这两条"簇消失"路径
        # 显式 pop 掉对应 cluster_id(只 pop 已消失的 id, 对存活簇零影响, 无误合风险)。
        self._centroid_cache: dict[str, tuple[tuple, NDArray[np.float32]]] = {}

    @_synchronized
    def get_track_embedding(
        self, cam_id: str, track_id: int,
    ) -> "NDArray[np.float32] | None":
        """从 reid_provider 取 ``(cam_id, track_id)`` 的实时 emb (DeepSort Track.features
        deque 末尾)。

        Public wrapper 给 IdentityEngine tier_c 累积路径用 (避免破封装直接访
        ``self._reid_provider``)。``self._reid_provider is None`` (tier_u 禁用或
        provider 未注入) 时返 None,调用方降级。
        """
        if self._reid_provider is None:
            return None
        return self._reid_provider.get_embedding(cam_id, track_id)

    @_synchronized
    def get_track_centroid(
        self, cam_id: str, track_id: int,
    ) -> "tuple[NDArray[np.float32] | None, int]":
        """从 reid_provider 取 ``(cam_id, track_id)`` 的历史特征质心 + emb 数(零额外推理)。

        Public wrapper 给 IdentityEngine 身份漂移自检用(同 get_track_embedding,避免
        破封装直接访 ``self._reid_provider``)。provider 未注入时返 ``(None, 0)``,调用方降级。
        """
        if self._reid_provider is None:
            return None, 0
        return self._reid_provider.get_centroid(cam_id, track_id)

    # ----- 写入:push_crop / flush_if_due -----

    @_synchronized
    def push_crop(self, entry: CropEntry) -> None:
        """累积一帧 crop 到对应 (cam, track) 的 L1。

        - 池关闭(enabled=False)或该 entry 写入 gate 关闭 → 静默丢弃(I1)。
        - 同 (cam, track) 第一次出现 → 自动创建 TierUEntry。
        - **G 方案触发时机**:L1 累积到 ``l1_capacity × l1_first_dedup_ratio``
          (默认 20% = 4 张)时,尝试拉 emb 快照 + 跑 dedup tick,覆盖短命 track
          的去重需求。延迟 ~6 帧让 DeepSORT 累几个 emb 比首帧单 emb noise 更低。
        - **G 方案重试**:emb 拉到才设 ``did_first_dedup=True`` 收手;拉不到时
          只更新 ``last_g_attempt_frame``,按 ``g_retry_interval_frames`` 节流重试。
          原因:``mode=fast`` + ``human_reid_skip_windows=4`` 下,静止 track 的
          DeepSORT ``Track.features`` deque 经常要 4-8 秒才有第一条 emb,L1 累到
          4 张那一刻很可能 ``provider.get_embedding`` 返 None。这种 entry 会变成
          singleton cluster 永远合并不上同人——所以失败必须重试。
        - 稳态仍走 C 方案(L1 满 flush 时再跑一次更稳的 dedup)。
        """
        if not self.config.enabled:
            return
        key = (entry.cam_id, entry.track_id)
        te = self._entries.get(key)
        if te is None:
            te = TierUEntry(cam_id=entry.cam_id, track_id=entry.track_id)
            self._entries[key] = te
        if not te.write_open:
            return  # I1: gate 关闭后静默丢弃

        # 入池前等比缩放(只 downscale,不 upscale)——L1/L2 全程持有小图,memory_bytes
        # 立即受益。对齐 omni gallery composite 设计尺寸,见 TierUConfig 注释。
        entry.body_crop = _shrink_to_max_height(
            entry.body_crop, self.config.body_crop_max_height,
        )
        if entry.face_crop is not None:
            entry.face_crop = _shrink_to_max_height(
                entry.face_crop, self.config.face_crop_max_height,
            )

        te.crops_l1.append(entry)
        te.last_l1_push_ts = self._now()

        # 每次 push 给本 L1 crop 拉一次 emb 快照,让 L1 crop 也持有 reid_embedding
        # (内存代价可忽略 = 128 × float32 × L1_cap = 15 KB/entry)。
        # 关键意义:track 没活到 L1 满的"短命场景",L2 全空时注册走 L1 fallback,
        # 这些 L1 crop 直接带 emb 给下游 BodySample → 落 .npy。
        # 计算代价:provider.get_embedding 是 Track.features deque[-1] 读取 + clone
        # (零额外推理硬约束,见类 docstring),mode=fast 下静止 track 返回上次 emb 即可,
        # 失败返 None 不影响 push 流程。
        if self._reid_provider is not None and entry.reid_embedding is None:
            emb = self._reid_provider.get_embedding(entry.cam_id, entry.track_id)
            if emb is not None:
                entry.reid_embedding = emb

        # G 方案触发:L1 累计到 capacity × ratio 时尝试拉 emb + dedup tick
        first_dedup_threshold = max(
            1, int(self.config.l1_capacity * self.config.l1_first_dedup_ratio),
        )
        retry_gap = max(1, self.config.g_retry_interval_frames)
        if (not te.did_first_dedup
                and self._reid_provider is not None
                and len(te.crops_l1) >= first_dedup_threshold
                and entry.frame_index - te.last_g_attempt_frame >= retry_gap):
            emb = self._reid_provider.get_embedding(entry.cam_id, entry.track_id)
            te.last_g_attempt_frame = entry.frame_index
            if emb is not None:
                te.reid_embedding = emb
                te.embedding_snapshot_ts = te.last_l1_push_ts
                te.embedding_sharpness = entry.sharpness
                te.embedding_dirty = True
                self._intra_cam_dedup_tick()
                # emb 拉到才收手——拉不到下窗口按 retry_gap 节流重试
                te.did_first_dedup = True

    @_synchronized
    def flush_if_due(self) -> list[str]:
        """对所有 entry 检查 L1 是否累积到 l1_capacity,达标则晋级。

        返回本轮新建/更新的 cluster_id 列表(给上层埋点 / 日志用)。
        """
        if not self.config.enabled:
            return []

        promoted_clusters: list[str] = []
        now = self._now()

        for te in list(self._entries.values()):
            if not te.write_open:
                continue
            if len(te.crops_l1) < self.config.l1_capacity:
                continue  # I2: 不到容量不晋级

            # 1) 整批扫 L1,挑 sharpness 最高且过 quality_gate 的
            best = self._pick_best_in_l1(te.crops_l1)
            if best is None:
                # 全部不合格 → 清 L1 重新累积(不强行写)
                te.crops_l1.clear()
                continue

            # 2) 给入 L2 的 crop 拉 per-crop emb 快照(零额外推理,读跟踪侧 deque
            #    末尾)。失败留 None,cluster mean 时会跳过;不阻断 L2 写入。
            if self._reid_provider is not None and best.reid_embedding is None:
                emb_for_crop = self._reid_provider.get_embedding(te.cam_id, te.track_id)
                if emb_for_crop is not None:
                    best.reid_embedding = emb_for_crop

            # 3) 推入 L2(FIFO 满则弹最旧)
            while len(te.crops_l2) >= self.config.l2_capacity:
                te.crops_l2.popleft()
                # 顺手清匹配 cache(该 cluster 的代表帧可能变了——保守清)
                if te.cluster_id is not None:
                    self._invalidate_match_cache_for_cluster(te.cluster_id)
            te.crops_l2.append(best)
            te.crops_l1.clear()
            te.last_l1_push_ts = now

            # 4) entry 级 emb 快照:作 L2 为空时的 fallback。仅当 best 比已快照
            #    更清晰时刷新,避免老 sharpness 被压低。
            if self._reid_provider is not None and (
                te.reid_embedding is None or best.sharpness > te.embedding_sharpness
            ):
                # 复用 best.reid_embedding 节省一次 provider 调用
                emb = best.reid_embedding
                if emb is not None:
                    te.reid_embedding = emb
                    te.embedding_snapshot_ts = now
                    te.embedding_sharpness = best.sharpness
                    te.embedding_dirty = True

        # 4) 跑入库去重(脏标记驱动,intra_cam 内)
        promoted_clusters = self._intra_cam_dedup_tick()
        return promoted_clusters

    def _quality_score(self, c: CropEntry) -> float:
        """实例方法封装 module-level ``quality_score``, 自动注入 config 的 aspect 范围。

        TierU 三处排序统一调本方法:
          - ``_pick_best_in_l1`` (L1→L2 晋级)
          - ``_cluster_candidate_for`` (cluster 内 representative 选择)
          - ``_build_cluster_candidates`` (cluster 之间号码图排序)
        """
        return quality_score(
            c,
            aspect_min=self.config.aspect_min,
            aspect_max=self.config.aspect_max,
        )

    def _pick_best_in_l1(self, crops: Iterable[CropEntry]) -> CropEntry | None:
        """整批扫 L1,过 quality_gate 后按 quality_score 选最优 1 张;全不合格返回 None。

        排序键从单 ``sharpness`` 升级为综合 ``quality_score`` (det/aspect/sharp 加权 +
        face_bonus 1.5)。详见 ``quality_score`` docstring。
        """
        ok = [c for c in crops if self._pass_quality_gate(c)]
        if not ok:
            return None
        return max(ok, key=lambda c: self._quality_score(c))

    def _pass_quality_gate(self, c: CropEntry) -> bool:
        if c.body_crop is None or c.body_crop.size == 0:
            return False
        cfg = self.config
        h, w = c.body_crop.shape[:2]
        # bbox 物理:area_ratio + aspect。注意 bbox 是 xyxy,frame size 拿不到
        # 在这里——我们改为以 crop 自身 size 算 aspect;area_ratio 由上层在
        # 调 push_crop 前算好的 detector_conf 隐含信息间接保障(crop 太小 detector
        # 通常 conf 也低)。这里只校 aspect + sharpness + conf。
        if h <= 0 or w <= 0:
            return False
        aspect = w / h
        if aspect < cfg.aspect_min or aspect > cfg.aspect_max:
            return False
        if c.sharpness < cfg.sharpness_min:
            return False
        if c.detector_conf < cfg.detector_conf_min:
            return False
        return True

    # ----- 入库去重(intra_cam,脏标记驱动) -----

    def _entry_dedup_embedding(
        self, entry: TierUEntry,
    ) -> "NDArray[np.float32] | None":
        """供 dedup 路径用的 entry 代表 emb。

        优先 entry-level snapshot(``entry.reid_embedding``,G 方案首次 dedup 拉的);
        fallback 用该 entry 所有 L1 crops 的 reid_embedding mean(L2-normalized)。

        fallback 专治"短命 track":L1 累计帧数 < ``l1_capacity × l1_first_dedup_ratio``,
        G 方案触发不到 → 永远没拉到 entry-level emb → ``_intra_cam_dedup_tick`` 把它
        当 ``reid_embedding is None`` 跳过 → 同人多 short track 各占一份 LRU 槽。
        L1 crops 自己每次 push 都拉了 emb(见 ``push_crop``),mean 后做代表 emb
        足够稳。
        """
        if entry.reid_embedding is not None:
            return entry.reid_embedding
        embs = [c.reid_embedding for c in entry.crops_l1 if c.reid_embedding is not None]
        if not embs:
            return None
        mean = np.mean(embs, axis=0).astype(np.float32)
        norm = float(np.linalg.norm(mean))
        if norm <= 0:
            return None
        return (mean / norm).astype(np.float32)

    def _intra_cam_dedup_tick(self) -> list[str]:
        """O(dirty × N_clusters_same_cam) 复杂度。已 cluster 的旧片段跳过。

        ⚠️ 严禁本函数内调 HumanReID.extract_feature——两侧 embedding 都已经
        通过 ReIDProvider 快照过来。本函数仅做余弦计算。

        代表特征选择策略:**centroid linkage**——每个候选 cluster 取它所有
        member entry 的 reid_embedding 的 mean(L2-normalized)作为代表,
        而非随机挑一个 entry。比 single-link 稳健,不会因 cluster 里有一个
        "边界 emb"就被新进的 entry 桥接进来误合并。

        Entry 代表 emb 走 ``_entry_dedup_embedding`` 三级 fallback(entry-level
        snapshot → L1 mean),让没拿到 G 方案 emb 的短命 track 也能参与合并。

        返回本轮新建/合并的 cluster_id 列表。
        """
        by_cam: dict[str, list[TierUEntry]] = {}
        for te in self._entries.values():
            by_cam.setdefault(te.cam_id, []).append(te)

        touched: list[str] = []
        for entries in by_cam.values():
            # 每个 entry 算"代表 emb"(entry-level 优先,L1 mean fallback)
            emb_of: dict[tuple[str, int], NDArray[np.float32]] = {}
            for te in entries:
                emb = self._entry_dedup_embedding(te)
                if emb is not None:
                    emb_of[te.key] = emb

            # 候选 dirty 集 = 有可用 emb 且 (embedding_dirty 或 未 cluster)。
            # "未 cluster" 分支专治 L1 < G_thr 的短命 track:它们永远拿不到
            # entry-level emb → 从来不被标 dirty → 同人多 singleton。只要 L1
            # 已拉到 emb,就该让它每次 tick 都尝试合一次(命中即合,未命中保持
            # singleton 状态,下次 tick 还会再试)。
            dirty = [
                e for e in entries
                if e.key in emb_of and (e.embedding_dirty or e.cluster_id is None)
            ]
            for e in dirty:
                e_emb = emb_of[e.key]
                # 候选 cluster:同 cam 内、cluster_id 不同于 e、至少 1 个成员有 emb
                candidate_cids: set[str] = set()
                for c in entries:
                    if c is e:
                        continue
                    if c.cluster_id is None or c.cluster_id == e.cluster_id:
                        continue
                    if c.key not in emb_of:
                        continue
                    candidate_cids.add(c.cluster_id)

                best_sim, best_cid = -1.0, None
                for cid in candidate_cids:
                    rep = self._cluster_mean_embedding(cid)
                    if rep is None:
                        continue
                    sim = _cosine(e_emb, rep)
                    if sim > best_sim:
                        best_sim, best_cid = sim, cid

                if best_cid is not None and best_sim >= self.config.reid_threshold_intra_cam:
                    self._merge_into_cluster(e, best_cid)
                    touched.append(best_cid)
                else:
                    if e.cluster_id is None:
                        cid = _new_cluster_id()
                        self._clusters[cid] = EquivClass(cluster_id=cid, members={e.key})
                        e.cluster_id = cid
                        touched.append(cid)
                e.embedding_dirty = False
        return touched

    def _cluster_mean_embedding(self, cluster_id: str) -> NDArray[np.float32] | None:
        """计算 cluster 内所有 L2 crop 的 reid_embedding 的 mean,L2-normalized。

        ReID gallery aggregation 标准做法:多张照片求 mean 后归一化作为一个身份
        的代表特征。聚合粒度,三级 fallback:
            优先 = 所有 cluster member 的所有 L2 crop 的 per-crop emb(最细)
            其次 = L2 全空时该 member 的所有 L1 crop emb(短命 track 没机会晋级 L2)
            兜底 = L1 也空时用 entry 级 emb 快照(首次 push 拉的)

        样本数量级:K 个成员 × 平均 L2_size 张 crop,通常 K × 10 张 emb。
        比"每 entry 单 emb mean"多一个数量级,centroid 更稳。

        Returns None:cluster 不存在 / 所有 member 都没 emb / mean 模长 = 0。
        """
        cluster = self._clusters.get(cluster_id)
        if cluster is None:
            self._centroid_cache.pop(cluster_id, None)
            return None
        embs: list[NDArray[np.float32]] = []
        for key in cluster.members:
            entry = self._entries.get(key)
            if entry is None:
                continue
            # 优先收所有 L2 crop emb
            before = len(embs)
            for crop in entry.crops_l2:
                if crop.reid_embedding is not None:
                    embs.append(crop.reid_embedding)
            if len(embs) > before:
                continue
            # L2 全为空 → 退到 L1 (短命 track 没机会晋级 L2 时)
            for crop in entry.crops_l1:
                if crop.reid_embedding is not None:
                    embs.append(crop.reid_embedding)
            if len(embs) > before:
                continue
            # L1 也空 → 退到 entry 级 emb 快照(首次 push 拉的)
            if entry.reid_embedding is not None:
                embs.append(entry.reid_embedding)
        if not embs:
            self._centroid_cache.pop(cluster_id, None)
            return None
        # 内容指纹: (member frozenset, 参与 mean 的 emb 数)。收集 embs 引用是
        # O(crops) 廉价指针操作, 真正贵的是下面 np.mean(K×crops×128)。指纹命中
        # 就跳过 np.mean。crop emb append-only + flush 只移动不替换 → 同一批 emb
        # 指纹不变 centroid 也不变 (安全)。member 增删 / push / close 都会改指纹
        # → 自动重算。唯一窄碰撞: 某 member L1/L2 全空时 entry 级兜底 emb 被
        # embedding_dirty 重算替换 (emb 数不变但值变) → 慢一拍, 但仍是同 entry 的
        # emb 旧值, 非跨身份, 可接受。
        signature = (frozenset(cluster.members), len(embs))
        cached = self._centroid_cache.get(cluster_id)
        if cached is not None and cached[0] == signature:
            return cached[1]
        mean = np.mean(embs, axis=0).astype(np.float32)
        norm = float(np.linalg.norm(mean))
        if norm <= 0:
            self._centroid_cache.pop(cluster_id, None)
            return None
        centroid = mean / norm
        self._centroid_cache[cluster_id] = (signature, centroid)
        return centroid

    def _merge_into_cluster(self, entry: TierUEntry, target_cluster_id: str) -> None:
        """把 entry 挂到目标 cluster;若 entry 原本属于另一 cluster,把整个旧 cluster
        的成员一并迁过去(等价类合并,绝对不分裂)。

        合并后内部自动 invalidate ``_match_cache`` 里涉及 target / 被弹旧 cluster
        的项 — centroid 变了, _cluster_pairwise_union 若读到 stale sim 会误判
        (旧 sim 可能假高把不同人 cluster 错合, 跨身份污染不可逆)。所有调用方
        (_intra_cam_dedup_tick / _cluster_pairwise_union / close_same_person_*)
        无需再手动清缓存, 不变量收敛到一处, 新增 merge 调用方零成本继承。
        """
        target = self._clusters.get(target_cluster_id)
        if target is None:
            # 目标 cluster 已被清(竞态),直接新建
            target = EquivClass(cluster_id=target_cluster_id, members=set())
            self._clusters[target_cluster_id] = target

        if entry.cluster_id is None:
            target.members.add(entry.key)
            entry.cluster_id = target_cluster_id
            # 新成员加入 → target centroid 变了, 清涉及 target 的 cache
            self._invalidate_match_cache_for_cluster(target_cluster_id)
            # 新 entry 的 write_open 默认 True → target cluster 不再 fully closed
            self._fully_closed_clusters.discard(target_cluster_id)
            return

        if entry.cluster_id == target_cluster_id:
            return  # 已属同 cluster, 真 no-op, 不需要清

        # 跨 cluster 合并: 迁旧 cluster 全部成员, 旧 cluster 整个被弹
        old_cluster_id = entry.cluster_id
        old = self._clusters.pop(entry.cluster_id, None)
        if old is not None:
            for member_key in old.members:
                target.members.add(member_key)
                me = self._entries.get(member_key)
                if me is not None:
                    me.cluster_id = target_cluster_id
        else:
            target.members.add(entry.key)
            entry.cluster_id = target_cluster_id
        # 两侧 cache 都失效:
        # - target_cluster_id: centroid 变了 (吸收新成员)
        # - old_cluster_id: 已被弹 (dangling), cache 里 (old, x) 项全无效
        self._invalidate_match_cache_for_cluster(target_cluster_id)
        self._invalidate_match_cache_for_cluster(old_cluster_id)
        # fully_closed set 也要两侧清: target 接收新成员可能引入 write_open=True
        # 的 entry, old 已被弹 (set 里残留就是 stale)。
        self._fully_closed_clusters.discard(target_cluster_id)
        self._fully_closed_clusters.discard(old_cluster_id)
        # old_cluster_id 已 dangling,其 _centroid_cache 项永不再被查(不会自愈)→
        # 显式清,否则每次跨 cluster 合并泄漏一条 (issue #429)。target 的
        # centroid 虽也变了但 member 指纹随之变 → _cluster_mean_embedding 自动重算,不用清。
        self._centroid_cache.pop(old_cluster_id, None)

    # ----- 注册取数:fetch -----

    @_synchronized
    def fetch(
        self,
        *,
        cam_id: str | None = None,
        target_track_id: int | None = None,
        target_cluster_id: str | None = None,
        window_sec: float | None = None,
        reid_extractor: "Any | None" = None,
        tier_a_emb_lookup: dict[str, NDArray[np.float32]] | None = None,
        confirmed_track_keys: list[tuple[str, int]] | None = None,
        tier_c_emb_lookup: dict[str, list[NDArray[np.float32]]] | None = None,
    ) -> list[ClusterCandidate]:
        """取注册候选。

        - ``target_cluster_id`` 命中 → by-id 锁定 cluster, 返回其全量成员
          (**不套 window**, cluster_id 唯一, by-id 查询不受时间过滤约束;真实
          兜底是 ``ttl_inactive_sec``)。优先级最高。
        - ``target_track_id`` 命中 → 锁定该 entry 所属 cluster,返回 cluster 全
          量成员(含跨 cam 兄弟,如果 intra_cam 去重已经把它们挂在一起)。
        - ``cam_id`` 单独传 → 该 cam 内最近 window 的全部 cluster。
        - 都不传 → 全局最近 window 的全部 cluster(再跑一次 cross_cam 去重)。

        Args:
            reid_extractor: 兜底 emb 抽取器(任何提供 ``extract_feature(crop) ->
                ndarray[128]`` 的实例,典型 ``HumanReID``)。对 ``e.reid_embedding``
                仍为 None 的 entry,从 L1 最锐 crop 现场抽一张,**回写到 entry**
                (同时 ``embedding_dirty=True``),然后**重跑一次 intra_cam dedup**
                让刚补 emb 的 entry 也参与合并——专门解决"短命 track + DeepSORT
                fast 模式跳过 ReID 导致 entry 终身无 emb,intra_cam_dedup_tick
                因 ``reid_embedding is None`` 把它过滤,最终在 fetch 时变成
                singleton 跟同人其它 track 不合"的边界 case。
                None 时跳过兜底,行为退回旧版。
            tier_a_emb_lookup: ``{person_id: mean_emb}``,IdentityLibrary 里所有已注册
                person 的 tier_a 样本 mean emb。fetch 末尾跟 cluster centroid 比对,
                命中阈值的 cluster **物理 close_write_gate + 不返回给挑号**(case c
                兜底:已入库人在 TierU 池里的残留 cluster)。
                None 时跳过这层去重,行为退回旧版。
                注:``target_track_id`` 或 ``target_cluster_id`` 明确锁定时, 本层
                以及下面两层 (confirmed_track / tier_c) 都跳过 —— 调用方明确就要看
                那个 cluster, 误过滤会让用户拿到空结果, 体验差。
            confirmed_track_keys: 当前 active 且 status=confirmed 的 track 的
                ``(cam_id, track_id)`` 列表。fetch 末尾从 ``self._reid_provider``
                取这些 track 的实时 emb (DeepSort Track.features deque 末尾),
                跟 cluster centroid 比对, 命中的同上处理(case b 兜底:同人多 track
                没合到一起时,确保 confirmed 那一刻镜头里的人不会在挑号拼图重复出现)。
                None / 空列表 时跳过这层。
            tier_c_emb_lookup: ``{person_id: [emb1, emb2, ...]}``, 各 person 的
                tier_c 样本 emb 列表(不 mean, 逐张)。第三层去重: cluster centroid
                跟每个 person 的每张 tier_c emb 比对, 任一命中 ``reid_threshold_tier_c_dedup``
                阈值(0.90, 比 cross_cam 0.85 严) → 视为同人 close。
                解决 case "用户近期换衣服/换姿势, tier_c 累积了新外观, 但 TierA
                mean 还是旧外观差异较大" 的边界场景。
                None 时跳过这层。
        """
        window = window_sec if window_sec is not None else self.config.fetch_window_sec
        now = self._now()
        threshold_ts = now - window

        # 1) 锁定候选 entry
        if target_cluster_id is not None:
            cluster = self._clusters.get(target_cluster_id)
            if cluster is None:
                return []
            candidate_entries = [
                self._entries[k] for k in cluster.members if k in self._entries
            ]
        elif target_track_id is not None:
            key = (cam_id or "", target_track_id) if cam_id else None
            entry = self._entries.get(key) if key else None
            if entry is None:
                # 兜底:跨 cam 找该 track_id(实际不太会用但容错)
                entry = next(
                    (e for e in self._entries.values()
                     if e.track_id == target_track_id),
                    None,
                )
            if entry is None:
                return []
            if entry.cluster_id:
                members = list(self._clusters[entry.cluster_id].members)
            else:
                members = [entry.key]
            candidate_entries = [self._entries[k] for k in members if k in self._entries]
        else:
            candidate_entries = [
                e for e in self._entries.values()
                if (cam_id is None or e.cam_id == cam_id)
                and e.last_l1_push_ts >= threshold_ts
            ]

        if not candidate_entries:
            return []

        # 2) 第 4 道防线:给 reid_embedding 仍为 None 的 entry 现场抽 emb 回写
        # 触发条件:provider 整条 track 生命周期返 None(常见于 fast mode 静止
        # 短命 track),前三层(push 拉/G 重试/flush 快照)都没接住。
        if reid_extractor is not None:
            for e in candidate_entries:
                if e.reid_embedding is not None:
                    continue
                best = self._pick_best_in_l1(e.crops_l1)
                if best is None or best.body_crop is None or best.body_crop.size == 0:
                    continue
                try:
                    emb = reid_extractor.extract_feature(best.body_crop)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "fetch: 现场抽 ReID emb 失败 entry=(%s,%s)",
                        e.cam_id, e.track_id, exc_info=True,
                    )
                    continue
                if emb is None:
                    continue
                # 回写:下次 fetch 再来,这条 entry 已经有 emb 不再走兜底路径
                e.reid_embedding = emb
                e.embedding_snapshot_ts = now
                e.embedding_sharpness = best.sharpness
                e.embedding_dirty = True
                # 顺手给那张 best crop 也带上 emb,下游 BodySample → .npy 链路省一次抽取
                if best.reid_embedding is None:
                    best.reid_embedding = emb
            # 跑一次 dedup tick,让刚补 emb 的 entry 跟现有 cluster 比对合并
            self._intra_cam_dedup_tick()

        # 3) 全局 fetch → 跑一次 cluster 两两 centroid 合并 (含同 cam + 跨 cam)。
        # 解决 online clustering 的"早期阈值边界拒判 → 终身分裂"问题: dedup tick
        # 是 dirty entry 驱动的,entry 进 cluster 后 dirty=False, 永不重新评估。
        # 但 cluster 演化后 centroid 可能漂移到能合的程度 (实测 case: A 8 entries
        # 跟 B 3 entries 的 centroid sim=0.91 > 0.85 阈值, 早期边界拒判形成的分裂)。
        # fetch 时跑一次, lazy 化, 主循环零开销。
        if target_track_id is None and cam_id is None:
            self._cluster_pairwise_union(candidate_entries)

        # 4) 按 cluster 聚合 → ClusterCandidate
        candidates = self._build_cluster_candidates(candidate_entries)

        # 5) 跟 TierA + 当前 confirmed track + tier_c 三层去重 (case b/c 兜底)。
        # 命中的 cluster → close_write_gate (物理清池) + 不返回给挑号。
        # 注意: ``target_track_id`` 或 ``target_cluster_id`` 明确锁定单 cluster
        # 时跳过去重 —— 调用方 (推送响应路径 "这是我自己" / register from cluster)
        # 明确就要看这个 cluster, 误过滤会让用户拿到空结果, 体验差。
        if target_track_id is None and target_cluster_id is None and (
            tier_a_emb_lookup or confirmed_track_keys or tier_c_emb_lookup
        ):
            # confirmed_track_keys 转 embs (从 reid_provider 取实时值)
            confirmed_track_embs: list[NDArray[np.float32]] = []
            if confirmed_track_keys and self._reid_provider is not None:
                for ck_cam, ck_tid in confirmed_track_keys:
                    emb = self._reid_provider.get_embedding(ck_cam, ck_tid)
                    if emb is not None:
                        confirmed_track_embs.append(emb)
            candidates = self._filter_known_persons(
                candidates,
                tier_a_emb_lookup=tier_a_emb_lookup or {},
                confirmed_track_embs=confirmed_track_embs,
                tier_c_emb_lookup=tier_c_emb_lookup or {},
            )
        return candidates

    def _candidate_centroid_emb(self, c: ClusterCandidate) -> NDArray[np.float32] | None:
        """从 ClusterCandidate 算 centroid emb。

        正式 cluster (cluster_id 在 self._clusters 里) → 走 ``_cluster_mean_embedding``
        (覆盖所有 member 的 L2 emb, 更稳)。
        singleton 临时 cluster (``"singleton:cam:tid"`` 前缀, 不在 self._clusters 里)
        → 用 candidate 自带的 ``all_l2_crops`` / ``all_l1_crops`` 的 emb mean,
        再 fallback 到 ``representative_crop`` 的 emb。
        """
        if not c.cluster_id.startswith("singleton:"):
            rep = self._cluster_mean_embedding(c.cluster_id)
            if rep is not None:
                return rep
        # singleton 兜底: 从 candidate 自带 crops 算
        embs: list[NDArray[np.float32]] = []
        for crop in c.all_l2_crops:
            if crop.reid_embedding is not None:
                embs.append(crop.reid_embedding)
        if not embs:
            for crop in c.all_l1_crops:
                if crop.reid_embedding is not None:
                    embs.append(crop.reid_embedding)
        if not embs and c.representative_crop is not None \
                and c.representative_crop.reid_embedding is not None:
            embs.append(c.representative_crop.reid_embedding)
        if not embs:
            return None
        mean = np.mean(embs, axis=0).astype(np.float32)
        norm = float(np.linalg.norm(mean))
        if norm <= 0:
            return None
        return (mean / norm).astype(np.float32)

    def _filter_known_persons(
        self,
        candidates: list[ClusterCandidate],
        *,
        tier_a_emb_lookup: dict[str, NDArray[np.float32]],
        confirmed_track_embs: list[NDArray[np.float32]],
        tier_c_emb_lookup: dict[str, list[NDArray[np.float32]]] | None = None,
    ) -> list[ClusterCandidate]:
        """fetch 末尾的三层去重: cluster centroid 跟 TierA mean emb / 当前 confirmed
        track 实时 emb / tier_c 逐张 emb 比对, 命中阈值的 cluster 被 close_write_gate
        物理清池 + 从返回列表过滤掉。

        三层互补:
          - TierA 层 (静态, 阈值 0.85): 兜底 "用户已注册的人在 TierU 池子有残留 cluster"
          - confirmed track 层 (实时, 阈值 0.85): 兜底 "镜头里某人刚被识别成已知,
            但 TierU 里同人的其他 cluster 没被 close_write_gate 关掉" (case b)
          - tier_c 层 (近期累积, 阈值 0.90): 兜底 "用户近期换衣服/换姿势, tier_c
            累积了新外观, 但 TierA mean 还是旧外观差异较大" 边界场景

        阈值: 前两层用 ``reid_threshold_cross_cam`` (0.85); tier_c 层用
        ``reid_threshold_tier_c_dedup`` (0.90, 严一档), 因 tier_c 单样本质量参差
        (累积了不同瞬间外观), 严点避免误隐藏真陌生人。
        """
        if not candidates:
            return candidates
        threshold = self.config.reid_threshold_cross_cam
        tier_c_threshold = self.config.reid_threshold_tier_c_dedup
        tier_c_emb_lookup = tier_c_emb_lookup or {}
        kept: list[ClusterCandidate] = []
        for c in candidates:
            rep = self._candidate_centroid_emb(c)
            if rep is None:
                kept.append(c)  # 没 emb 不删, 保守: 留给用户判断
                continue
            hit_reason: str | None = None
            hit_sim: float = 0.0  # 命中 log 用, 排查误命中 (假阳性同身材误杀) 时看数值
            # 层 1: 跟 TierA mean emb 比对
            for pid, person_emb in tier_a_emb_lookup.items():
                if person_emb is None:
                    continue
                sim = _cosine(rep, person_emb)
                if sim >= threshold:
                    hit_reason = f"tier_a:{pid}"
                    hit_sim = sim
                    break
            # 层 2: 跟 confirmed track 实时 emb 比对。
            # confirmed_track_embs 上游 (router.pool_fetch 构造时) 已过滤 None,
            # 这里不重复 None check, 让 list 不含 None 的契约显式。
            if hit_reason is None:
                for track_emb in confirmed_track_embs:
                    sim = _cosine(rep, track_emb)
                    if sim >= threshold:
                        hit_reason = "confirmed_track"
                        hit_sim = sim
                        break
            # 层 3: 跟各 person 的 tier_c 逐张 emb 比对 (严一档阈值 0.90)
            if hit_reason is None:
                for pid, tc_embs in tier_c_emb_lookup.items():
                    if not tc_embs:
                        continue
                    matched_sim: float | None = None
                    for tc_emb in tc_embs:
                        if tc_emb is None:
                            continue
                        sim = _cosine(rep, tc_emb)
                        if sim >= tier_c_threshold:
                            matched_sim = sim
                            break
                    if matched_sim is not None:
                        hit_reason = f"tier_c:{pid}"
                        hit_sim = matched_sim
                        break
            if hit_reason is None:
                kept.append(c)
                continue
            # 命中: close_write_gate 物理清池 (取 cluster 任一成员触发)。
            # log 带 sim 值便于排查"跨身份误命中" — 假阳性同身材接近 cluster 被误关
            # 时, 看 sim 是否擦边 (~0.85-0.87) 决定要不要调阈值。
            logger.info(
                "TierU fetch 去重命中: cluster_id=%s 跟 %s (sim=%.3f) 同人, close_write_gate",
                c.cluster_id, hit_reason, hit_sim,
            )
            if c.members:
                any_cam, any_tid = c.members[0]
                try:
                    self.close_write_gate(any_cam, any_tid)
                except Exception:
                    logger.warning(
                        "fetch 去重 close_write_gate 失败 cluster=%s",
                        c.cluster_id, exc_info=True,
                    )
        return kept

    def _cluster_pairwise_union(self, entries: list[TierUEntry]) -> None:
        """对所有 cluster 两两比对 centroid 余弦, 超阈值合并 (含同 cam + 跨 cam)。

        代表特征策略: **centroid linkage** — 每个 cluster 取所有 member entry 的
        ``reid_embedding`` 的 mean L2-normalized 作为代表(同 _intra_cam_dedup_tick)。

        v2 升级 (2026-05-21): 之前叫 ``_cross_cam_union``, 只对跨 cam 的 cluster 跑;
        同 cam 内 cluster 即使 centroid 漂移到能合也永远分裂 (实测 case: 单 cam 内
        cluster A 8 entries vs B 3 entries, centroid sim=0.91 远超 0.85 阈值, 但
        因为 t=60s 时 track_3 vs 当时 cluster A (只有 track_1) sim=0.8429 边界拒判,
        从此分裂永久化)。改名 + 去掉 cam 限制后, fetch 时一律跑, lazy 化 catch-up
        合并机会。

        **保守边界 case** (避免链式合并 / stale rep):
        - 单次调用**只合最高分一对**, 不做 while loop 直到 converge
        - 链式合并 (A-B-C 都该合) 通过**多次 fetch** 自然 converge — fetch 频率
          ~1-2 次/天, 单 trace 上 1-2 天内合到稳定可接受
        - 合并后清掉所有涉及被弹 cluster_id 的 match_cache 项 (防 cache 指向已删 cluster)

        阈值用 ``reid_threshold_cross_cam`` (语义: cluster-vs-cluster centroid,
        跟前身 ``_cross_cam_union`` 一致)。
        """
        # cluster_ids: 当前有可用 dedup emb (含 L1 fallback) 的 cluster 集合,
        # 进 pairwise centroid 比对池。前身 _cross_cam_union 同时记录每 cluster
        # 的成员 cam 集合用于"跳过同 cam 对",改 _cluster_pairwise_union 后 cam 限制
        # 去掉, cam 集合无人读, 退化为单纯 set[cluster_id]。
        cluster_ids_set: set[str] = set()
        for e in entries:
            if e.cluster_id is None:
                continue
            # 已 fully_closed cluster 不再参与 pairwise union (commit 后 close 的
            # cluster 不会再用, 没必要算 centroid 进 pair 池; 合进去也只是给 close
            # cluster 加成员而 close_write_gate 已经把成员清了)。
            if e.cluster_id in self._fully_closed_clusters:
                continue
            # 用 helper 而非直接 e.reid_embedding,让 L1 fallback 也能让 cluster
            # 参与 pairwise union(否则全短命 cluster 永远不进 cluster_ids_set 里)
            if self._entry_dedup_embedding(e) is None:
                continue
            cluster_ids_set.add(e.cluster_id)

        cluster_ids = list(cluster_ids_set)
        if len(cluster_ids) < 2:
            return  # 单 cluster (含 0) 无需 pairwise
        # 预算每个 cluster 的 mean emb,避免 O(K²) 中重复计算
        cluster_reps: dict[str, NDArray[np.float32] | None] = {
            cid: self._cluster_mean_embedding(cid) for cid in cluster_ids
        }

        # 扫所有 pair, 记录最高分超阈值对; 不立即合, 避免 stale rep
        threshold = self.config.reid_threshold_cross_cam
        best_sim = -1.0
        best_pair: tuple[str, str] | None = None
        for i, j in itertools.combinations(range(len(cluster_ids)), 2):
            cid_a, cid_b = cluster_ids[i], cluster_ids[j]
            rep_a, rep_b = cluster_reps[cid_a], cluster_reps[cid_b]
            if rep_a is None or rep_b is None:
                continue
            cache_key = frozenset((cid_a, cid_b))
            sim = self._match_cache.get(cache_key)
            if sim is None:
                sim = _cosine(rep_a, rep_b)
                if len(self._match_cache) >= self.config.match_cache_capacity:
                    self._match_cache.pop(next(iter(self._match_cache)))
                self._match_cache[cache_key] = sim
            if sim >= threshold and sim > best_sim:
                best_sim = sim
                best_pair = (cid_a, cid_b)

        if best_pair is None:
            return
        cid_a, cid_b = best_pair
        # 合并 cid_b 整个等价类 → cid_a (任选成员触发, _merge_into_cluster 迁全部)。
        # cache invalidate 由 _merge_into_cluster 内部统一接手 (target + old 两侧),
        # 这里不再显式清, 防"调用方各自清, 漏一处就漏 stale sim 污染"。
        any_b_key = next(iter(self._clusters[cid_b].members))
        self._merge_into_cluster(self._entries[any_b_key], cid_a)

    def _build_cluster_candidates(
        self, entries: list[TierUEntry],
    ) -> list[ClusterCandidate]:
        """按 cluster 聚合 entries → ClusterCandidate 列表,按"sharpness 最高的
        representative"排序。"""
        grouped: dict[str, list[TierUEntry]] = {}
        unclustered: list[TierUEntry] = []
        for e in entries:
            if e.cluster_id is None:
                unclustered.append(e)
            else:
                grouped.setdefault(e.cluster_id, []).append(e)

        out: list[ClusterCandidate] = []
        # 已 cluster 的
        for cid, group_entries in grouped.items():
            cand = self._cluster_candidate_for(cid, group_entries)
            if cand is not None:
                out.append(cand)
        # 未 cluster 的每条单独成一个候选(临时 id,不进 self._clusters)
        for e in unclustered:
            cand = self._cluster_candidate_for(f"singleton:{e.cam_id}:{e.track_id}", [e])
            if cand is not None:
                out.append(cand)

        # 号码图展示顺序: 按 representative 的 quality_score 降序 (含 face_bonus 软加成)。
        # 软判断让高质 no_face 也能上位,避免低质 has_face 永远压死高质 no_face。
        out.sort(
            key=lambda c: self._quality_score(c.representative_crop),
            reverse=True,
        )

        # post-sort: 把最高分 has_face cluster 提前到 #1 位 (头位体验保障)。
        # 软排序后 #1 可能是 no_face 高分 cluster,但号码图首位用户对"有脸"的期望
        # 最强烈,这里做硬性补偿。整列表都无 face 时本步骤 no-op。
        # 注意只动 #1, 不重排其余 — 中后位仍保留软排序的"高质无脸上位"语义。
        if out and out[0].representative_crop.face_crop is None:
            face_idx = next(
                (i for i, c in enumerate(out)
                 if c.representative_crop.face_crop is not None),
                None,
            )
            if face_idx is not None:
                out.insert(0, out.pop(face_idx))
        return out

    def _cluster_candidate_for(
        self, cluster_id: str, group_entries: list[TierUEntry],
    ) -> ClusterCandidate | None:
        all_l2: list[CropEntry] = []
        all_l1: list[CropEntry] = []
        per_cam_rep: dict[str, CropEntry] = {}
        for e in group_entries:
            for c in e.crops_l2:
                # 兜底:L2 crop 在 flush 时通常会拉 emb,但若 flush 那刻 provider 暂时
                # 拿不到(mode=fast/skip_windows 偶发空 deque),crop.reid_embedding 留
                # None。用 entry 级 emb 快照兜底,让下游 extract_from_pool → BodySample
                # → _write_embedding 能落 .npy。覆盖"短命 track L1 满 l1_capacity 但 flush 那帧
                # ReID 没出产"边缘场景。
                if c.reid_embedding is None and e.reid_embedding is not None:
                    c.reid_embedding = e.reid_embedding
                all_l2.append(c)
                cur = per_cam_rep.get(e.cam_id)
                if cur is None or c.sharpness > cur.sharpness:
                    per_cam_rep[e.cam_id] = c
            # L1 也收集,给 extract_from_pool 在 L2 不足时按 sharpness 补足
            for c in e.crops_l1:
                # 关键兜底:L1 raw crop 从不在 push_crop / flush 路径上拉 emb(只有
                # 晋级 L2 的 best 才拉)。track 没活到 L1 满时 L2 全空,注册走 L1
                # fallback,这些 crop 的 reid_embedding 全 None → 入库后没 .npy。
                # 这里用 entry 级 emb(G 方案 / flush 任一拉到过的快照)兜底,让 L1
                # fallback 写出来的 tier_a 样本也带 ReID emb。
                if c.reid_embedding is None and e.reid_embedding is not None:
                    c.reid_embedding = e.reid_embedding
                all_l1.append(c)
        # L2 + L1 都为空才真的没数据;只有 L1 时 representative 取 L1 最锐(用户
        # 刚出现在镜头前,L1 还没满 flush 时也能让 fetch 看到候选)
        if not all_l2 and not all_l1:
            return None

        # crop 级去重(ReID + pHash 联合,详见 _dedup_crops_for_fetch):
        # cluster 内不同 entry 的 crop 可能拍到同姿势近副本,这里按 sharpness 降序
        # 跑一次双维度去重,把"像素级近副本 + ReID 余弦也接近"的丢掉。entry 级
        # 合并(同人多 track 挂同 cluster_id)由 _intra_cam_dedup_tick 在入库时做,
        # 本步骤只解决 entry 内 / cluster 内 crop 重复占样本名额的问题。
        all_l2.sort(key=lambda c: c.sharpness, reverse=True)
        all_l1.sort(key=lambda c: c.sharpness, reverse=True)
        all_l2 = _dedup_crops_for_fetch(all_l2)
        # L1 与 L2 合并去重:L1 fallback 不要和已留下的 L2 近副本;先把 L2 作种子,
        # 再让 L1 按 sharpness 顺序加入。
        if all_l2 and all_l1:
            seeded = list(all_l2)
            seeded.extend(all_l1)
            merged = _dedup_crops_for_fetch(seeded)
            # 拆回 L1 / L2 两段:同对象 id 在 L2 集合里的归 L2,其余归 L1。
            l2_id_set = {id(c) for c in all_l2}
            new_l2 = [c for c in merged if id(c) in l2_id_set]
            new_l1 = [c for c in merged if id(c) not in l2_id_set]
            all_l2 = new_l2
            all_l1 = new_l1
        elif all_l1:
            all_l1 = _dedup_crops_for_fetch(all_l1)

        # 去重可能把 per_cam_rep 里的代表淘汰掉——重算一遍,保持一致
        if all_l2:
            per_cam_rep = {}
            for c in all_l2:
                cur = per_cam_rep.get(c.cam_id)
                if cur is None or c.sharpness > cur.sharpness:
                    per_cam_rep[c.cam_id] = c

        # 去重后 L2 + L1 都为空(极端:全是近副本被收敛到 0)兜底返回 None
        if not all_l2 and not all_l1:
            return None
        # 代表(号码图封面)选择: 按 quality_score (含 face_bonus 1.5 软加成) 选最高。
        # face_bonus 软判断让"高质量无脸"也能压过"低质量有脸",避免 strict
        # (face, sharp) 字典序的反直觉。整 cluster 全无 face 时退化为纯 quality_score。
        if all_l2:
            rep = max(all_l2, key=lambda c: self._quality_score(c))
            earliest = min(c.captured_at for c in all_l2)
            latest = max(c.captured_at for c in all_l2)
        else:
            # 极端 fallback:L2 全空,用 L1(同上,统一走 quality_score)
            rep = max(all_l1, key=lambda c: self._quality_score(c))
            earliest = min(c.captured_at for c in all_l1)
            latest = max(c.captured_at for c in all_l1)
        return ClusterCandidate(
            cluster_id=cluster_id,
            members=[e.key for e in group_entries],
            representative_crop=rep,
            total_crops=len(all_l2),
            span_cam_count=len({e.cam_id for e in group_entries}),
            earliest_ts=earliest,
            latest_ts=latest,
            per_cam_representative=per_cam_rep,
            all_l2_crops=list(all_l2),
            all_l1_crops=list(all_l1),
        )

    # ----- commit / 残留清理 -----

    @_synchronized
    def close_same_person_clusters_by_track(
        self,
        cam_id: str,
        track_id: int,
    ) -> int:
        """用 ``(cam_id, track_id)`` 的实时 emb 扫池子里其他 cluster,
        余弦超阈值的同人 cluster 触发 ``close_write_gate``。

        触发时机: omni 把某 track commit 成 confirmed 时, engine 调本方法把池子里
        "同人 residual cluster" (case b: 同人多 track 没合到一起) 主动清掉,
        物理释放内存 + 防止用户挑号时看到。

        实现要点:
          - emb 从 ``self._reid_provider`` 取实时值 (DeepSort Track.features deque
            末尾), 这是 confirmed 那一刻最新的特征
          - 跳过 ``(cam_id, track_id)`` 自己所属 cluster (``close_write_gate``
            已经处理过, 避免重复)
          - 阈值 ``reid_threshold_cross_cam`` (默认 0.85)

        Returns:
            被 close 的 cluster 数 (不含 ``(cam_id, track_id)`` 自己所属那个)。
        """
        if self._reid_provider is None:
            return 0
        query_emb = self._reid_provider.get_embedding(cam_id, track_id)
        if query_emb is None:
            return 0
        # 跳过自己所属 cluster
        self_entry = self._entries.get((cam_id, track_id))
        exclude_cluster_id = self_entry.cluster_id if self_entry else None
        threshold = self.config.reid_threshold_cross_cam
        closed = 0
        for cid in list(self._clusters.keys()):
            if cid == exclude_cluster_id:
                continue
            # 已 fully_closed 的 cluster 跳过 (write_open 单向 True→False 不可逆,
            # 进 set 后不会再回到 open 除非 _merge_into_cluster 接新成员 / 整 cluster
            # 被弹, 那两个路径已经 discard 维护)。原先 O(N_members) 内循环判 all
            # closed 退化为 O(1) set 查; close_write_gate 不 pop cluster, 不跳的话
            # 每次 confirmed 都会重新扫 + 算 centroid。
            if cid in self._fully_closed_clusters:
                continue
            cluster = self._clusters.get(cid)
            if cluster is None or not cluster.members:
                continue
            cluster_rep = self._cluster_mean_embedding(cid)
            if cluster_rep is None:
                continue
            if _cosine(query_emb, cluster_rep) < threshold:
                continue
            any_cam, any_tid = next(iter(cluster.members))
            try:
                if self.close_write_gate(any_cam, any_tid) > 0:
                    closed += 1
                    logger.info(
                        "confirmed 主动清池: cluster_id=%s 跟刚 confirmed "
                        "(cam=%s, track=%d) 同人, closed",
                        cid, cam_id, track_id,
                    )
            except Exception:
                logger.warning(
                    "confirmed 主动清池 close_write_gate 失败 cluster=%s",
                    cid, exc_info=True,
                )
        return closed

    @_synchronized
    def close_write_gate(self, cam_id: str, track_id: int) -> int:
        """commit 后调:把 (cam, track) 所属 cluster **全部成员**的写入 gate 关掉,
        并清掉 L1/L2 残留 crops。返回被关闭的 entry 数(决策 1.1 α 行为)。
        """
        key = (cam_id, track_id)
        entry = self._entries.get(key)
        if entry is None:
            return 0
        if entry.cluster_id is None:
            members_keys = {key}
            cluster_id = None
        else:
            cluster_id = entry.cluster_id
            members_keys = set(self._clusters[cluster_id].members)

        closed = 0
        for mk in members_keys:
            me = self._entries.get(mk)
            if me is None:
                continue
            me.write_open = False
            me.crops_l1.clear()
            me.crops_l2.clear()
            closed += 1

        if cluster_id is not None:
            self._invalidate_match_cache_for_cluster(cluster_id)
            # 整 cluster 一次关完, 加入快查 set 让 close_same_person_clusters_by_track
            # / _cluster_pairwise_union 跳过已 close cluster (省 centroid 计算)。
            # write_open 单向 True→False 不可逆, 此处加入后, cluster 后续接新成员
            # (_merge_into_cluster) 时再 discard。
            self._fully_closed_clusters.add(cluster_id)
        return closed

    def _invalidate_match_cache_for_cluster(self, cluster_id: str) -> None:
        stale = [k for k in self._match_cache if cluster_id in k]
        for k in stale:
            self._match_cache.pop(k, None)

    # ----- M10:cluster 拆分(commit 前的"误合并修正"接口) -----

    @_synchronized
    def split_cluster(
        self,
        cluster_id: str,
        *,
        remove_members: list[tuple[str, int]] | None = None,
        remove_cams: list[str] | None = None,
    ) -> tuple[str, str] | None:
        """从指定 cluster 剥离一批成员到新 cluster_id;原 cluster 保留剩余成员。

        Args:
            remove_members: 直接指定要剥离的 (cam_id, track_id) 列表(精确控制)。
            remove_cams: 按 cam_id 批量剥离该相机下所有成员(快速路径,如"仅保留
                cam-A,把 cam-B 全拆出去")。
            两者 OR 关系;都为 None / 空 时 noop 返 None。

        Returns:
            (kept_cluster_id, new_cluster_id) — 保留方与拆出方的 cluster_id;
            若 cluster 不存在 / selector 不命中任何成员 / 命中后剩余成员为 0 → None。
        """
        cluster = self._clusters.get(cluster_id)
        if cluster is None:
            return None
        members = set(cluster.members)
        to_remove: set[tuple[str, int]] = set()
        if remove_members:
            to_remove |= {tuple(m) for m in remove_members if tuple(m) in members}
        if remove_cams:
            for m in members:
                if m[0] in remove_cams:
                    to_remove.add(m)
        if not to_remove:
            return None
        kept_members = members - to_remove
        # 不允许全拆光(那等于没拆,且把原 cluster 弄空)
        if not kept_members:
            logger.info("split_cluster: selector 命中所有成员,无意义拆分,跳过")
            return None

        # 新 cluster:to_remove
        new_cid = _new_cluster_id()
        self._clusters[new_cid] = EquivClass(cluster_id=new_cid, members=set(to_remove))
        for key in to_remove:
            entry = self._entries.get(key)
            if entry is not None:
                entry.cluster_id = new_cid

        # 原 cluster:留下 kept_members
        cluster.members = kept_members

        # match_cache 兜底清:两个 cluster 涉及的 pair 全部 invalidate
        self._invalidate_match_cache_for_cluster(cluster_id)
        self._invalidate_match_cache_for_cluster(new_cid)

        # _fully_closed_clusters 同步: 先 discard 原 cluster_id (成员组成变了, 旧
        # set 状态 stale), 再按拆分后两侧成员的 write_open 实际情况重新 add。
        # 不维护的话: split 一个 fully_closed cluster, new_cid 不进 set, 后续
        # close_same_person_clusters_by_track 走慢路径无效 close + 错误的
        # "confirmed 主动清池" log。
        self._fully_closed_clusters.discard(cluster_id)

        def _all_closed(member_keys: "set[tuple[str, int]]") -> bool:
            for mk in member_keys:
                me = self._entries.get(mk)
                if me is not None and me.write_open:
                    return False
            return True

        if _all_closed(kept_members):
            self._fully_closed_clusters.add(cluster_id)
        if _all_closed(to_remove):
            self._fully_closed_clusters.add(new_cid)

        logger.info("split_cluster %s → keep=%s,split=%s (new cid=%s)",
                    cluster_id, kept_members, to_remove, new_cid)
        return (cluster_id, new_cid)

    # ----- TTL / LRU -----

    @_synchronized
    def tick_ttl(self) -> int:
        """清理 ``ttl_inactive_sec`` 内无 L1 push 的 entry。返回清掉数。"""
        if not self.config.enabled:
            return 0
        now = self._now()
        threshold = now - self.config.ttl_inactive_sec
        dead = [
            key for key, e in self._entries.items()
            if e.last_l1_push_ts < threshold
            and e.last_l1_push_ts > 0  # 排除从未 push 的(刚 new 的 entry)
        ]
        for k in dead:
            self._evict_entry(k)
        return len(dead)

    @_synchronized
    def gc_lru_if_over_budget(self) -> int:
        """超过 ``memory_budget_mb`` 时按 last_l1_push_ts 升序弹最旧。返回清掉数。"""
        budget_bytes = self.config.memory_budget_mb * 1024 * 1024
        total = sum(e.memory_bytes() for e in self._entries.values())
        if total <= budget_bytes:
            return 0
        # 升序弹最旧(最早 push 的)
        ordered = sorted(self._entries.items(), key=lambda kv: kv[1].last_l1_push_ts)
        evicted = 0
        for key, entry in ordered:
            if total <= budget_bytes:
                break
            total -= entry.memory_bytes()
            self._evict_entry(key)
            evicted += 1
        if evicted > 0:
            logger.info("陌生人池 LRU 兜底清掉 %d 个 entry", evicted)
        return evicted

    def _evict_entry(self, key: tuple[str, int]) -> None:
        """物理删 entry + 同步清等价类 + match_cache。"""
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        if entry.cluster_id is not None:
            cluster = self._clusters.get(entry.cluster_id)
            if cluster is not None:
                cluster.members.discard(key)
                if not cluster.members:
                    self._clusters.pop(entry.cluster_id, None)
                    self._invalidate_match_cache_for_cluster(entry.cluster_id)
                    # cluster 整个被弹 (TTL/LRU 清掉最后一个成员) → set 里残留就是
                    # stale, 跟 _merge_into_cluster 跨 cluster 合并 old 被弹同语义。
                    # 不清的话 set 单调泄漏 (UUID 36 字节/条) + 跟 __init__ 注释
                    # "接新成员/被弹时移除"承诺不一致。
                    self._fully_closed_clusters.discard(entry.cluster_id)
                    # 同理清 _centroid_cache:cluster 已弹、永不再被查 → 不清则每淘汰
                    # 一簇泄漏一条 (issue #429 高 track churn 长跑最坏几百 MB)。
                    self._centroid_cache.pop(entry.cluster_id, None)

    # ----- 状态查询(给 status 端点用) -----

    @_synchronized
    def status(self) -> dict:
        total_bytes = sum(e.memory_bytes() for e in self._entries.values())
        active_entries = sum(1 for e in self._entries.values() if e.write_open)
        return {
            "entries_total": len(self._entries),
            "entries_active": active_entries,
            "clusters": len(self._clusters),
            "memory_mb": round(total_bytes / 1024 / 1024, 2),
            "memory_budget_mb": self.config.memory_budget_mb,
            "match_cache_size": len(self._match_cache),
        }

    # ----- 快照 / 复原:dump_to / load_from -----
    #
    # 目的:线上抓到的"难合并"现场可以原样落盘,本地 REPL 用 load_from 复原后
    # 反复调阈值 / dedup 逻辑做离线优化,不用每次都等线上再现。
    #
    # 格式选型(不用 pickle 的理由:跨 Python / 跨包版本不稳,且不可读):
    #   {path}/manifest.json  — 元数据(config / entries 结构 / clusters / match_cache)
    #   {path}/arrays.npz     — 所有 ndarray(crop 像素 + emb),按 manifest 引用的 key 索引
    #
    # load_from 只做"还原 self.state",**不接 reid_provider**(快照里的 cam/track
    # 跟线上 tracker 没对应关系);如果需要离线 fetch + 4 道防线兜底抽 emb,自己
    # 注一个 HumanReID 实例作为 reid_extractor 传 fetch。

    # v2 (2026-05-21): entry.cam_id 由 scope_label 改成米家 device_id。格式没变,
    # 字段语义变 → 必须 bump,让 load_from 直接拒掉旧快照,避免离线 fetch
    # 静默错位。
    _SNAPSHOT_VERSION = 2

    @_synchronized
    def dump_to(self, path: str) -> dict:
        """把池子完整状态写到目录 ``path``。返回 summary {entries, clusters, bytes}。

        @_synchronized:dump 在 API 主线程顺序遍历 _entries/_clusters/crops_l1/l2,
        推理线程并发 push/flush/evict/merge 时会"迭代中改大小"。RLock 同线程可重入、
        与池其它入口互斥,抓到一致快照。tut_dump_enable 默认关、仅排障开,但一开就在
        活池上跑,故加锁。"""
        import json
        import os

        os.makedirs(path, exist_ok=True)
        arrays: dict[str, NDArray] = {}

        def _crop_to_dict(prefix: str, c: CropEntry) -> dict:
            body_key = f"{prefix}_body"
            arrays[body_key] = c.body_crop
            d: dict = {
                "cam_id": c.cam_id,
                "track_id": c.track_id,
                "frame_index": c.frame_index,
                "captured_at": c.captured_at,
                "sharpness": c.sharpness,
                "bbox_xyxy": list(c.bbox_xyxy),
                "detector_conf": c.detector_conf,
                "body_key": body_key,
            }
            if c.face_crop is not None:
                face_key = f"{prefix}_face"
                arrays[face_key] = c.face_crop
                d["face_key"] = face_key
            if c.reid_embedding is not None:
                emb_key = f"{prefix}_emb"
                arrays[emb_key] = c.reid_embedding
                d["emb_key"] = emb_key
            return d

        entries_data: list[dict] = []
        for (cam, tid), e in self._entries.items():
            base = f"e_{cam}_{tid}"
            e_dict: dict = {
                "cam_id": e.cam_id,
                "track_id": e.track_id,
                "last_l1_push_ts": e.last_l1_push_ts,
                "embedding_snapshot_ts": e.embedding_snapshot_ts,
                "embedding_sharpness": e.embedding_sharpness,
                "cluster_id": e.cluster_id,
                "embedding_dirty": e.embedding_dirty,
                "write_open": e.write_open,
                "did_first_dedup": e.did_first_dedup,
                "last_g_attempt_frame": e.last_g_attempt_frame,
                "crops_l1": [_crop_to_dict(f"{base}_l1_{i}", c)
                             for i, c in enumerate(e.crops_l1)],
                "crops_l2": [_crop_to_dict(f"{base}_l2_{i}", c)
                             for i, c in enumerate(e.crops_l2)],
            }
            if e.reid_embedding is not None:
                emb_key = f"{base}_entry_emb"
                arrays[emb_key] = e.reid_embedding
                e_dict["reid_embedding_key"] = emb_key
            entries_data.append(e_dict)

        clusters_data = [
            {"cluster_id": c.cluster_id, "members": [list(m) for m in c.members]}
            for c in self._clusters.values()
        ]
        match_cache_data = [
            {"key": list(k), "sim": v} for k, v in self._match_cache.items()
        ]

        # 把 TierUConfig 转 dict;dataclass 没继承自 BaseModel,手动 vars 即可
        config_dict = {k: v for k, v in vars(self.config).items()}

        manifest = {
            "version": self._SNAPSHOT_VERSION,
            "dumped_at": self._now(),
            "config": config_dict,
            "entries": entries_data,
            "clusters": clusters_data,
            "match_cache": match_cache_data,
        }

        with open(os.path.join(path, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        np.savez_compressed(os.path.join(path, "arrays.npz"), **arrays)

        return {
            "entries": len(entries_data),
            "clusters": len(clusters_data),
            "arrays": len(arrays),
            "manifest_bytes": os.path.getsize(os.path.join(path, "manifest.json")),
            "arrays_bytes": os.path.getsize(os.path.join(path, "arrays.npz")),
        }

    @classmethod
    def load_from(
        cls,
        path: str,
        *,
        config: TierUConfig | None = None,
        reid_provider: ReIDProvider | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> "TierUPool":
        """从 ``dump_to`` 生成的目录还原一个 TierUPool(只还原状态,不接线上 provider)。

        ``config`` 给 None → 用快照里的 config 字典构造一个新 TierUConfig;离线调
        参时显式传入新 config 即可(老快照 + 新阈值的对比实验)。

        **快照版本**: 当前 ``_SNAPSHOT_VERSION = 2``。manifest.version 与之不符直接
        ``ValueError``,不做"软兼容"——v1 → v2 是 ``entry.cam_id`` 命名空间从
        scope_label (``<room>-dev<idx>``) 切到米家 device_id(纯数字),格式没变但
        字段语义变,放任旧快照 load 进来会埋两类坑:

          - **接 reid_provider 拿不到 emb** —— v2 ``_deep_sort_trackers`` 用 device_id
            作 key,跟 v1 快照里的 scope_label cam_id 不匹配,``get_embedding`` 返 None
          - **跟线上 fetch 不能混用** —— 离线 load 的 pool 与线上 PerceptionEngine 管的
            pool 是两份独立内存,命名空间也不同,别走线上的 push_crop / fetch 路径

        v1 快照真需要离线分析时,显式手改 manifest.json 里 ``"version": 1 → 2`` +
        把 entries[*].cam_id 重写到 device_id 再 load(没工具脚本,自己 grep 改)。
        """
        import json
        import os

        with open(os.path.join(path, "manifest.json")) as f:
            manifest = json.load(f)
        if manifest.get("version") != cls._SNAPSHOT_VERSION:
            raise ValueError(
                f"snapshot version mismatch: got {manifest.get('version')}, "
                f"expected {cls._SNAPSHOT_VERSION}",
            )
        arrays = np.load(os.path.join(path, "arrays.npz"))

        pool_config = config if config is not None else TierUConfig(**manifest["config"])
        pool = cls(config=pool_config, reid_provider=reid_provider, now_fn=now_fn)

        def _dict_to_crop(d: dict) -> CropEntry:
            return CropEntry(
                cam_id=d["cam_id"],
                track_id=d["track_id"],
                frame_index=d["frame_index"],
                captured_at=d["captured_at"],
                body_crop=arrays[d["body_key"]],
                face_crop=arrays[d["face_key"]] if "face_key" in d else None,
                sharpness=d["sharpness"],
                bbox_xyxy=tuple(d["bbox_xyxy"]),  # type: ignore[arg-type]
                detector_conf=d["detector_conf"],
                reid_embedding=arrays[d["emb_key"]] if "emb_key" in d else None,
            )

        for e_data in manifest["entries"]:
            entry = TierUEntry(
                cam_id=e_data["cam_id"],
                track_id=e_data["track_id"],
                crops_l1=[_dict_to_crop(c) for c in e_data["crops_l1"]],
                crops_l2=deque(_dict_to_crop(c) for c in e_data["crops_l2"]),
                last_l1_push_ts=e_data["last_l1_push_ts"],
                embedding_snapshot_ts=e_data["embedding_snapshot_ts"],
                embedding_sharpness=e_data["embedding_sharpness"],
                cluster_id=e_data["cluster_id"],
                embedding_dirty=e_data["embedding_dirty"],
                write_open=e_data["write_open"],
                did_first_dedup=e_data["did_first_dedup"],
                last_g_attempt_frame=e_data["last_g_attempt_frame"],
            )
            if "reid_embedding_key" in e_data:
                entry.reid_embedding = arrays[e_data["reid_embedding_key"]]
            pool._entries[(entry.cam_id, entry.track_id)] = entry

        for c_data in manifest["clusters"]:
            pool._clusters[c_data["cluster_id"]] = EquivClass(
                cluster_id=c_data["cluster_id"],
                members={tuple(m) for m in c_data["members"]},  # type: ignore[misc]
            )

        for mc in manifest["match_cache"]:
            pool._match_cache[frozenset(mc["key"])] = mc["sim"]

        return pool


# =============================================================================
# 工具
# =============================================================================


def _cosine(a: NDArray[np.float32], b: NDArray[np.float32]) -> float:
    """余弦相似度。两侧默认已 L2-normalized(来自 HumanReID),直接点积即可。

    兜底:若上游未归一化,fallback 走完整公式。
    """
    dot = float(np.dot(a, b))
    # 上游归一化的话 |a|=|b|=1,dot 就是 cosine
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if abs(na - 1.0) < 1e-3 and abs(nb - 1.0) < 1e-3:
        return dot
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


# fetch 端 crop 级去重阈值(常量,不暴露 yaml)。
#   FETCH_DEDUP_PHASH_LOOSE — 双维度联合判定时的 pHash 阈值;复用 registration_filter
#                              的 DEFAULT_PHASH_DISTANCE_MIN=28,符号统一。
#   FETCH_DEDUP_REID        — 双维度联合判定时的 ReID 余弦阈值。比 entry 级合并阈值
#                              0.9 更严——这里是"crop 级近重复",同人不同姿势的 emb
#                              余弦经验上 0.85-0.93,设 0.95 让不同姿势活下来。
#   FETCH_DEDUP_PHASH_STRICT — 一边无 ReID emb 时的 fallback;只能靠 pHash,宁严
#                              勿松,避免误删差异化样本。
_FETCH_DEDUP_PHASH_LOOSE: int = 28
_FETCH_DEDUP_REID: float = 0.95
_FETCH_DEDUP_PHASH_STRICT: int = 22


def _is_crop_duplicate(
    cand_phash: int,
    cand_emb: NDArray[np.float32] | None,
    selected: list[tuple[int, NDArray[np.float32] | None]],
) -> bool:
    """判 cand 是否跟 selected 里某张 crop 视觉 + 语义都接近(重复)。

    联合判定:**pHash 近 + ReID 近** 两个维度同时满足才视作重复。这样:
      - 同姿势同视角的近副本(pHash 接近,ReID 接近) → 判重复
      - 同人不同姿势(pHash 远,但 ReID 接近) → 不判重复(保留,丰富注册样本)
      - 不同人(ReID 远) → 不判重复

    ``selected`` 是 ``(phash, reid_embedding)`` 元组的列表;ReID 缺失退化为
    更严格的纯 pHash 判定(hamming < 22),宁严勿松。
    """
    for s_phash, s_emb in selected:
        phash_close = _hamming(cand_phash, s_phash) < _FETCH_DEDUP_PHASH_LOOSE
        if cand_emb is not None and s_emb is not None:
            # 双维度联合判定
            reid_close = _cosine(cand_emb, s_emb) >= _FETCH_DEDUP_REID
            if phash_close and reid_close:
                return True
        else:
            # ReID 缺失 → 纯 pHash 用更严阈值
            if _hamming(cand_phash, s_phash) < _FETCH_DEDUP_PHASH_STRICT:
                return True
    return False


def _dedup_crops_for_fetch(
    crops: list[CropEntry],
) -> list[CropEntry]:
    """fetch 端 cluster 内 crop 级去重(ReID + pHash 联合)。

    输入按 sharpness 降序(调用方负责),输出保留差异化的子集——最锐的优先,
    跟已选 crop 任一维度差异显著就保留,两维度都接近就丢。

    输入空 / 全 None body_crop 返回空列表;quality gate 已经在 push_crop / flush_if_due
    上游做过,这里不再过滤(避免拿掉用户可能选的边缘样本)。
    """
    if not crops:
        return []

    selected: list[CropEntry] = []
    selected_meta: list[tuple[int, NDArray[np.float32] | None]] = []
    for c in crops:
        if c.body_crop is None or c.body_crop.size == 0:
            continue
        c_phash = _phash(c.body_crop)
        if _is_crop_duplicate(c_phash, c.reid_embedding, selected_meta):
            continue
        selected.append(c)
        selected_meta.append((c_phash, c.reid_embedding))
    return selected


def _new_cluster_id() -> str:
    return uuid.uuid4().hex


def _shrink_to_max_height(
    img: NDArray[np.uint8] | None, max_height: int,
) -> NDArray[np.uint8] | None:
    """等比缩放到 ≤ max_height 高度;不 upscale,不损坏宽高比。

    - ``img`` 为 None / 空 / 非 2D~3D → 原样返回(防御,push_crop 上游可能传任何东西)
    - ``max_height`` ≤ 0 → 视为关闭 resize,原样返回(yaml 显式关闭路径)
    - 原高度 ≤ max_height → 原样返回(不放大避免无意义内存增加)
    - 否则 cv2.resize INTER_AREA 降采样,宽按比例算
    """
    if img is None or img.size == 0:
        return img
    if max_height <= 0:
        return img
    if img.ndim not in (2, 3):
        return img
    h = img.shape[0]
    w = img.shape[1]
    if h <= max_height or h <= 0 or w <= 0:
        return img
    new_w = max(1, int(round(w * max_height / h)))
    return cv2.resize(img, (new_w, max_height), interpolation=cv2.INTER_AREA)


# =============================================================================
# DeepSortReIDProvider:跟踪侧适配器,给 TierUPool 注入用
# =============================================================================


class DeepSortReIDProvider(ReIDProvider):
    """把 ``DeepSortTracker`` 包成 ReIDProvider。

    ``DeepSortTracker.get_track_embedding`` 已经实现"零额外推理"(从
    ``Track.features`` deque 取末尾),本类只做一层薄转发。

    多 cam 场景下,如果上层为每个 cam 各持一个 DeepSortTracker,这个 provider
    应该按 cam_id dispatch 到对应 tracker。当前主流程是"每房间一个 tracker"
    (cam_id 与 tracker 一一对应),本 provider 接 dict[cam_id, tracker]。
    """

    def __init__(self, trackers: dict[str, object] | object) -> None:
        # 兼容两种用法:dict[cam_id, tracker] / 单 tracker
        self._trackers = trackers

    def get_embedding(self, cam_id: str, track_id: int) -> NDArray[np.float32] | None:
        tracker = (
            self._trackers.get(cam_id) if isinstance(self._trackers, dict)
            else self._trackers
        )
        if tracker is None or not hasattr(tracker, "get_track_embedding"):
            return None
        return tracker.get_track_embedding(track_id)

    def get_centroid(
        self, cam_id: str, track_id: int,
    ) -> tuple[NDArray[np.float32] | None, int]:
        tracker = (
            self._trackers.get(cam_id) if isinstance(self._trackers, dict)
            else self._trackers
        )
        if tracker is None or not hasattr(tracker, "get_track_centroid"):
            return None, 0
        return tracker.get_track_centroid(track_id)
