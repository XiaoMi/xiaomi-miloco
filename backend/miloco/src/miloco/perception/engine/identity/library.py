"""IdentityLibrary — 身份库（gallery）持久化。

存储结构：

    data/identity_lib/
    ├── persons/
    │   └── <person_id>/
    │       ├── tier_a/                    # 用户登记，永久
    │       │   ├── body_001.png           # 新写入为 .png 无损；历史库 .jpg/.jpeg 仍可读
    │       │   ├── body_001.json          # sidecar：含 source / captured_at / 可选
    │       │   │                          #   register_session_id / cluster_id / track_id /
    │       │   │                          #   camera_id / score / phash
    │       │   ├── face_001.png           # 当前不送入识别管线，作为 face_recog 备料保存
    │       │   └── face_001.json
    │       ├── tier_c/                    # 系统在线累积，FIFO 容量 10
    │       │   ├── body_<ts>.png          # 同上：新写 .png，兼容历史 .jpg
    │       │   └── body_<ts>.json
    │       └── meta.json                  # {name, role?, last_seen_ts}
    ├── identity_bindings.json             # 跨模态绑定占位（声纹接入用）
    └── README.md

person_id 与 ``Person.id``（UUID）保持一致。

sidecar schema 扩展字段（v1.2 主动注册系列加入；旧 sidecar 缺字段时按缺省值处理）：
    - register_session_id : 触发本次写入的注册批次 ID（rollback 关键索引）
    - cluster_id          : 来自陌生人池去重的等价类（附件路径写 None）
    - camera_id / track_id: 来源信息
    - score               : 候选样本的代表性评分（§7.2.3）
    - phash               : 64-bit pHash hex 字符串

当前未实施：
- ``body_attr_text`` 文本特征（``GallerySamples`` 字段保留但永远 None）
- ``Tier B``（中期晋升策略；目前只有用户登记的 A + 系统累积的 C）
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import cv2
import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.identity import _avatar
from miloco.perception.engine.identity._image_utils import (
    hamming as _hamming,
)
from miloco.perception.engine.identity._image_utils import (
    phash as _phash,
)

logger = logging.getLogger(__name__)


# =============================================================================
# gallery 选样模式（tier_c pollution 修复 C1 / E6 系列）
# =============================================================================
#
# tier_c 在 gallery 参考图中的取样模式。背景：tier_c 写入存在污染（详见
# `.wsh_cc/身份识别问题诊断_系统现状与根因.md`），且 gallery 默认取"最近一张
# tier_c"会形成正反馈（错样本 → 进 gallery → 强化误判）。C1 期默认关闭
# tier_c 回喂，C3 引入信任过滤后切到 trusted。A/B 改这一行 + 重启 backend。
#
#   off     - 不回喂 tier_c, gallery body 列只用 tier_a (E6a, 斩正反馈)
#   recent  - 旧行为, 取 tier_c mtime 最新一张（留作对照 / 临时回退）
#   trusted - 取最新且 sidecar verify_same_person==True 的 tier_c(写库前 omni 同人校验判过的);
#             排除校验上线前/未校验样本防脏回喂; 无合格样本则回退纯 tier_a。当前默认。
#
_GALLERY_TIER_C_MODE: Literal["off", "recent", "trusted"] = "trusted"


# =============================================================================
# 数据结构
# =============================================================================


@dataclass
class PersonRef:
    """轻量 person 信息，用于列表展示。

    name 是人物真名(显示主键，来自 SQL，经 meta.json 缓存)，role 是可空的家庭角色。
    """

    person_id: str
    name: str | None
    role: str | None
    has_tier_a: bool
    num_tier_c: int
    # tier_a 下 body_* 图像数量（展示 / 调试用）。
    num_tier_a_body: int = 0
    # tier_a (body+face) 的 (文件名, mtime) 指纹。IdentityEngine 监听身份库变化用——
    # 精确捕获 tier_a 的增 / 删 / 替换 / touch（faces 与 body 同等对待，都是权威参考）。
    # tier_c 不进此指纹：tier_c 写入/编辑不强制全体重判，其变化靠 gallery composite
    # 自身的 (文件名,mtime) 指纹缓存自然生效（下次 recheck 即用新参考图）。
    tier_a_fingerprint: tuple[tuple[str, float], ...] = ()


@dataclass
class MergeResult:
    """merge_persons 返回结果。"""
    target_id: str
    merged_sources: list[str]
    written_tier_a: int
    written_tier_c: int


@dataclass
class SplitResult:
    """split_person 返回结果。``moved`` 是新 person tier_a 下的 body 文件名列表。"""
    new_person_id: str
    moved: list[str]


@dataclass
class BodySample:
    """注册流程 commit 批量入库的单条样本（v1.2 新增）。

    与 ``add_tier_a_sample`` 的散参数签名相比，``add_tier_a_samples_batch`` 用本
    dataclass 表达每张照片——便于把抽取算法 / 筛选算法产出的 metadata 一次性带进 sidecar。

    metadata 字典的标准 keys（按需填，缺省全部 None；不会覆盖 sidecar 系统字段）：
        - cluster_id        : 来自陌生人池去重的等价类
        - camera_id         : 来源相机
        - track_id          : 来源 track 编号
        - score             : §7.2.3 候选打分
        - phash             : pHash 64-bit hex 字符串
        - bbox              : (x1, y1, x2, y2)
        - detector_conf     : 检测置信
        - sharpness         : 清晰度

    ``reid_embedding``: 该样本对应的 ReID 特征向量(128-dim, L2-normalized)。
    陌生人池升级到身份库时直接把 per-crop emb 带进来即可,身份库内同名 .npy
    落盘(``body_001.npy`` 配同名 ``body_001.png``),后续做"未识别 track 跟已注册
    成员快速比对"等场景能直接用。可选,缺省 None 不落盘。
    """

    body_crop: NDArray[np.uint8]
    face_crop: NDArray[np.uint8] | None = None
    source: str = "register_session"
    captured_at: float | None = None
    metadata: dict | None = None
    reid_embedding: NDArray[np.float32] | None = None


@dataclass
class GallerySamples:
    """单个 person 的 gallery 样本，喂给 omni prompt 用。

    两条出口路径（``IdentityLibrary`` 提供）：

    - ``get_gallery_for_omni``：旧出口，填 ``body_crops`` / ``face_crops``（ndarray），
      每窗口现读现拼，没有缓存。保留向后兼容。
    - ``get_gallery_composites_for_omni``：带 L1+L2 缓存的新出口，填
      ``body_composite_jpeg`` / ``face_composite_jpeg``，``body_crops`` / ``face_crops``
      留空（省下 imread）。``prompt_builder`` 优先用这两个字段，命中后零 imread / 零 encode。

    任何一条出口都只填其中一组字段，不会两组都填。
    """

    person_id: str
    name: str | None
    role: str | None
    body_crops: list[NDArray[np.uint8]] = field(default_factory=list)  # ≤ N (默认 3)
    face_crops: list[NDArray[np.uint8]] = field(default_factory=list)  # ≤ N，face_recog 回归备料
    body_attr_text: str | None = None  # 当前未实施 body_attr_text，永远为 None
    body_composite_jpeg: Optional[bytes] = None
    face_composite_jpeg: Optional[bytes] = None


# =============================================================================
# person 目录级元信息 meta.json：{name, role?, last_seen_ts}
# =============================================================================
#
# name = 人物真名(显示主键，SQL 单一事实源的文件层缓存)；role = 可空家庭角色。
# 写入走 set_meta(可一次写 name+role)/ set_name(单字段便捷封装)。SQL 才是权威,文件层只是缓存。
# 不兼容旧 alias.json 版本身份库——旧库已弃用、重建即可(同迁移移除决策)。

_META_UNSET = object()  # set 时区分"未提供该字段"与"显式设为 None"


def _read_person_meta(person_dir: Path) -> dict:
    """读 meta.json；缺失或解析失败返回 {}。"""
    mf = person_dir / "meta.json"
    if mf.exists():
        try:
            return json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to parse meta.json for %s", person_dir.name)
    return {}


def _write_person_meta(person_dir: Path, *, name=_META_UNSET, role=_META_UNSET) -> None:
    """合并写 meta.json：只覆盖显式传入的字段，另一字段原样保留。

    始终写出 ``role`` 键(即便 None)，让文件自描述、且旧代码回滚后读取不崩。
    """
    data = _read_person_meta(person_dir)
    if name is not _META_UNSET:
        data["name"] = name
    if role is not _META_UNSET:
        data["role"] = role
    data["last_seen_ts"] = time.time()
    data.setdefault("role", None)
    person_dir.mkdir(parents=True, exist_ok=True)
    (person_dir / "meta.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# =============================================================================
# L1 / L2 缓存结构（内部使用）
# =============================================================================


# tier_a / tier_c 各取多少张的"选样指纹"——
# 用 (filename, mtime) 元组列表表达，能精确检出文件增删 / 替换 / touch。
# 单纯目录 mtime 不行：touch 文件不会更新 dir mtime，FIFO 替换才会。
_TierFingerprint = tuple[tuple[str, float], ...]


@dataclass
class _PersonTierCache:
    """L2 缓存：tier_a / tier_c 各自 hstack 完的 ndarray + 指纹。

    选样规则：拼接顺序固定为 ``[tier_a_picked, tier_c_picked]``（旧实现里
    "回填多余 tier_a 到 tier_c 之后" 的混插，在 omni 视角下顺序无意义，
    改为分段独立后能让 L2 两段缓存清晰，互不污染）。

    ndarray 高度统一（body=256 / face=128 default），宽度 = 各源图按比例缩放
    后总宽。**不** 在此缓存中做 ``max_total_width`` 兜底（合并 a/c 后再统一兜底，
    避免缓存被二次压缩失真）。

    L3 设计（未实施，预留位）：
        在 ``tier_a_*_nd`` 的基础上额外维护一个
        ``tier_a_slots: dict[filename, (x_start, x_end)]`` 索引，记录每张
        tier_a 图在拼接 ndarray 上的横坐标区间。这样当 tier_a 内部仅
        1 张图被增删（典型场景：用户在 register UI 上替换某张参考图），
        可以只对新文件做 imread + resize，其余 slot 直接从现存 ndarray
        切片复用，避开整段 tier_a 重读。
        节省预估：≈ 6 ms/人（tier_a 全 imread）；触发频率：仅在线增删 tier_a 单张时。
        实施门槛：删除 slot 后右侧 slot 整体左移、不同 height/quality 配置失效、
        ``max_total_width`` 兜底时 slot 偏移失真——综合收益/成本比偏低，
        L1+L2 已覆盖 99% 场景，暂不实施。
    """

    body_tier_a_nd: Optional[NDArray[np.uint8]] = None
    body_tier_c_nd: Optional[NDArray[np.uint8]] = None
    face_tier_a_nd: Optional[NDArray[np.uint8]] = None

    # fp 字段存 (文件指纹元组, target_height) 复合 key,把 height 内联进 per-tier
    # 命中判定。避免共享 height 字段被另一 tier 改写后引起本 tier 误命中——比如
    # body_tier_a 以新高度重建后改写共享 height,body_tier_c 用旧高度构建的 ndarray
    # 仍可能错误命中导致 np.hstack 高度不一致崩溃。
    body_tier_a_fp: tuple = ()
    body_tier_c_fp: tuple = ()
    face_tier_a_fp: tuple = ()


@dataclass
class _PersonCompositeCache:
    """L1 缓存：最终 jpeg bytes + 整体指纹。

    命中条件：选样后的所有 (filename, mtime) 元组 + 所有拼接/编码参数都不变。
    命中 → 直接返回 jpeg bytes，跳过全部 imread / resize / hstack / encode。
    """

    body_jpeg: Optional[bytes] = None
    face_jpeg: Optional[bytes] = None
    body_fp: tuple = ()
    face_fp: tuple = ()


# =============================================================================
# pHash / Hamming helpers — 实现在 _image_utils.py(已在顶部 import)
# =============================================================================


def _sanitize_cam_did(cam_did: str) -> str:
    """把 cam_did(米家 device_id)转成合法子目录名。posix 下数字 / lumi.xxx 基本合法,
    仅替换路径分隔 / 盘符 / 通配等非法字符与空白;空串兜底 ``_unknown``。"""
    if not cam_did:
        return "_unknown"
    safe = re.sub(r'[/\\:<>"|?*\s]', "_", cam_did)
    if safe in (".", ".."):  # 防路径穿越:纯 . / .. 会逃出 tier_c 目录
        return "_unknown"
    return safe or "_unknown"


def _next_index(existing: list[Path], prefix: str) -> int:
    """从已有 ``<prefix><NNN>`` 文件名(任意图像扩展名)中找最大编号 + 1。

    删除中间样本后用 ``len + 1`` 会覆盖剩余文件；用 max + 1 安全。
    注:``existing`` 必须含同 prefix 下**所有**扩展名的图(走 ``_list_crop_files``),
    否则新 png 可能与老 jpg 撞号。
    """
    max_idx = 0
    for p in existing:
        stem = p.stem  # e.g., "body_003"
        if not stem.startswith(prefix):
            continue
        try:
            idx = int(stem[len(prefix):])
        except ValueError:
            continue
        if idx > max_idx:
            max_idx = idx
    return max_idx + 1


# 落盘 crop 兼容的图像扩展名:新写入统一 .png, 读取兼容历史库的 .jpg/.jpeg。
CROP_IMG_EXTS: tuple[str, ...] = (".jpg", ".jpeg", ".png")


def _list_crop_files(directory: Path, prefix: str, *, recursive: bool = False) -> list[Path]:
    """列 ``directory`` 下 ``<prefix>_*`` 的图像文件(兼容 .jpg/.jpeg/.png)。

    排除 ``.npy``/``.json`` sidecar(它们与图同 stem)。顺序不保证, 调用方按需 ``sorted()``。
    ``recursive=True`` 用 rglob(tier_c 按相机子目录隔离时用)。
    """
    globber = directory.rglob if recursive else directory.glob
    return [p for p in globber(f"{prefix}_*") if p.suffix.lower() in CROP_IMG_EXTS]


# =============================================================================
# IdentityLibrary
# =============================================================================


class IdentityLibrary:
    """身份库。文件系统持久化，不依赖数据库（除了 ``Person`` 表外键由上层维护）。"""

    DEFAULT_TIER_A_MAX = 10                  # 每人 ≤ 10 张（body 5 + face 5）
    DEFAULT_TIER_C_MAX = 10                  # 每人 ≤ 10 张 body crop（FIFO）
    DEFAULT_PHASH_DISTANCE = 8               # 冗余过滤汉明距离阈值

    def __init__(
        self,
        root_dir: Path | str,
        tier_a_max_per_person: int = DEFAULT_TIER_A_MAX,
        tier_c_max_per_person: int = DEFAULT_TIER_C_MAX,
        phash_distance: int = DEFAULT_PHASH_DISTANCE,
    ) -> None:
        self.root = Path(root_dir)
        self.tier_a_max = tier_a_max_per_person
        self.tier_c_max = tier_c_max_per_person
        self.phash_distance = phash_distance

        self.persons_dir = self.root / "persons"
        self.bindings_file = self.root / "identity_bindings.json"
        self._ensure_dirs()

        # composite 缓存：per-engine 内存生命周期；进程重启自动重建，无需持久化。
        # delete_person / add_*_sample 写盘后由调用方触发的下一窗口 fingerprint 比对会失效旧条目
        # （fingerprint 含文件 mtime + 路径列表）；delete_person 时显式 pop 该 pid 防内存泄漏。
        # gallery 缓存键 = (person_id, cam_id):tier_c 按相机子目录隔离后,同一 person
        # 在不同相机的 gallery composite 不同(tier_c 段各异),故按 cam 分条目;tier_a 段
        # 在每个 (pid,cam) 条目各缓存一份(冗余极小、被 L1 吸收,且消除跨相机共享对象的并发顾虑)。
        self._tier_nd_cache: dict[tuple[str, str], _PersonTierCache] = {}
        self._composite_cache: dict[tuple[str, str], _PersonCompositeCache] = {}

        # tier_a body pHash 缓存：tier_c 写入前的"vs tier_a 距离校验"用，per-file 粒度
        # ``{pid: {filename: (mtime, phash)}}``。tier_a 写盘极少变（用户登记一次基本不动），
        # 稳态下 lookup 即返回，不需要重读 tier_a 图。
        # delete_person 时显式 pop；mtime 比对自动失效失败条目。
        self._tier_a_phash_cache: dict[str, dict[str, tuple[float, int]]] = {}

        # gallery 正脸优选缓存:``{pid: (face 文件指纹, 正脸 Path|None)}``。_pick_face_files 在
        # L1 命中判断**之前**被无条件调用(其结果参与 face 缓存键), 故正脸优选必须 memo 化——
        # 否则每窗(含 L1 命中)都对每张 face imread 找正脸, 把 imread 重引回 L1 命中热路径。
        # 指纹 = face 文件的 ((名, mtime), ...); face 样本集没变就复用、零解码。受 _cache_lock 护。
        self._frontal_face_cache: dict[str, tuple["_TierFingerprint", "Path | None"]] = {}

        # 身份漂移自检"近期同摄 tier_c/tier_a 参考质心"缓存:``{(pid,cam): (在窗指纹, 结果)}``。
        # 与正脸缓存同为"输入没变就别重算", 但**不对称**: 参考质心是 (样本集, now_ts) 的函数——
        # now_ts 每窗推进, 旧样本会滑出 recency 窗, 即便目录没变质心也变。故指纹取 **now_ts
        # 过滤后的在窗集** (名,mtime), 不是整目录; 命中省 np.load(mean+L2), miss 才付。_cache_lock 护。
        self._drift_ref_cache: dict[
            tuple[str, str],
            tuple[
                tuple["_TierFingerprint", "_TierFingerprint"],
                tuple["NDArray[np.float32] | None", int, str],
            ],
        ] = {}

        # 跨 OS 线程保护上面这些内存缓存。推理线程(gallery GC)、tier_c worker 的
        # to_thread(tier_c_phash_check 算 pHash)、API 线程(delete/merge/split →
        # _invalidate_person_cache)会并发读/写/迭代同一 dict,无锁会 RuntimeError
        # (dict changed size during iteration)或读到撕裂的 (mtime,phash)。锁只护
        # O(1) 的 dict 读写/迭代段;重活(cv2.imread/_phash)留锁外(见 tier_c_phash_check),
        # 不串行化推理或写库(与 TierUPool 的 RLock 同一精神)。
        self._cache_lock = threading.Lock()

        # tier_c 写入 per-person 锁:library 全局一份、多镜头(多 engine)共享,各自的写库
        # worker 在不同线程 to_thread(add_tier_c_sample) 同一 person 时,串行化整段
        # 读改写(glob→去重→FIFO→imwrite→sidecar),防互删/同毫秒撞名。元锁护字典本身。
        # 锁键 = (person_id, cam_id):tier_c 子目录隔离后,同一 person 不同相机写不同子目录,
        # 可并发;同 person 同相机仍串行(防同毫秒撞名 / 互删)。
        self._tier_c_write_locks: dict[tuple[str, str], threading.Lock] = {}
        self._tier_c_write_locks_guard = threading.Lock()

    def _tier_c_write_lock(self, person_id: str, cam_id: str) -> threading.Lock:
        """取/建该 (person, cam) 的 tier_c 写锁(进程内、跨线程)。"""
        key = (person_id, cam_id)
        with self._tier_c_write_locks_guard:
            lk = self._tier_c_write_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._tier_c_write_locks[key] = lk
            return lk

    def _invalidate_person_cache(self, person_id: str) -> None:
        """清掉某 person 在所有相机下的 gallery 缓存条目(L1/L2 键含 cam_id)+ tier_a phash
        缓存(按 person)。delete / merge / split / 写盘后调用,防陈旧条目与内存泄漏。"""
        with self._cache_lock:
            for k in [k for k in self._composite_cache if k[0] == person_id]:
                self._composite_cache.pop(k, None)
            for k in [k for k in self._tier_nd_cache if k[0] == person_id]:
                self._tier_nd_cache.pop(k, None)
            self._tier_a_phash_cache.pop(person_id, None)
            self._frontal_face_cache.pop(person_id, None)
            for k in [k for k in self._drift_ref_cache if k[0] == person_id]:
                self._drift_ref_cache.pop(k, None)

    # -------------------------------------------------------------------------
    # tier_c 闲时定期清(per 相机):见 .wsh_cc/TierC定期清-落地设计.md
    # -------------------------------------------------------------------------

    def list_person_ids(self) -> list[str]:
        """列出已注册 person 目录名(UUID4)。"""
        if not self.persons_dir.is_dir():
            return []
        return [
            p.name for p in self.persons_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ]

    def tier_c_pool_latest_mtime(self, cam_id: str) -> "float | None":
        """该相机下**所有 person** 的 tier_c 最新写入 mtime(秒);全空返 None。

        供闲时判定"该相机 tier_c 近期没在写"。读文件 mtime,零 pipeline 改动。
        """
        cam_sub = _sanitize_cam_did(cam_id)
        latest: float | None = None
        for pid in self.list_person_ids():
            cam_dir = self.persons_dir / pid / "tier_c" / cam_sub
            if not cam_dir.is_dir():
                continue
            for img in _list_crop_files(cam_dir, "body") + _list_crop_files(cam_dir, "face"):
                try:
                    m = img.stat().st_mtime
                except OSError:
                    continue
                if latest is None or m > latest:
                    latest = m
        return latest

    def clear_tier_c(self, cam_id: str, person_id: str) -> int:
        """清空某 person 在某相机的 tier_c(body/face 图 + 同 stem 的 .json/.npy sidecar)。

        返回删除的图数。走 ``_tier_c_write_lock`` 串行(不与写库 worker 抢同一池),
        删后 ``_invalidate_person_cache`` 让 gallery L1/L2 重建。清空 = 该 (person,cam)
        gallery 退纯 tier_a(安全态)。定期清专用。
        """
        cam_dir = self.persons_dir / person_id / "tier_c" / _sanitize_cam_did(cam_id)
        if not cam_dir.is_dir():
            return 0
        deleted = 0
        with self._tier_c_write_lock(person_id, cam_id):
            for img in _list_crop_files(cam_dir, "body") + _list_crop_files(cam_dir, "face"):
                img.with_suffix(".json").unlink(missing_ok=True)
                img.with_suffix(".npy").unlink(missing_ok=True)
                img.unlink(missing_ok=True)
                deleted += 1
        if deleted:
            self._invalidate_person_cache(person_id)
        return deleted

    def _ensure_dirs(self) -> None:
        self.persons_dir.mkdir(parents=True, exist_ok=True)
        if not self.bindings_file.exists():
            self.bindings_file.write_text("[]", encoding="utf-8")

    # -------------------------------------------------------------------------
    # 查询
    # -------------------------------------------------------------------------

    def list_persons(self) -> list[PersonRef]:
        """列出所有 person。"""
        out: list[PersonRef] = []
        if not self.persons_dir.is_dir():
            return out
        for p in sorted(self.persons_dir.iterdir()):
            if not p.is_dir():
                continue
            # 过滤系统/隐藏目录（如 .backup_<ts>、.DS_Store 等）
            if p.name.startswith("."):
                continue
            person_id = p.name
            tier_a = p / "tier_a"
            tier_c = p / "tier_c"
            a_body_files = _list_crop_files(tier_a, "body") if tier_a.is_dir() else []
            a_face_files = _list_crop_files(tier_a, "face") if tier_a.is_dir() else []
            has_a = bool(a_body_files)
            num_a_body = len(a_body_files)
            # tier_c 按相机子目录隔离:计数是跨摄用途,rglob 含全相机子目录 + 根下 legacy。
            num_c = len(_list_crop_files(tier_c, "body", recursive=True)) if tier_c.is_dir() else 0
            # tier_a (body+face) 指纹: 库变化监听据此判"权威参考是否变了"; tier_c 不计入。
            tier_a_fp = self._fingerprint(sorted(a_body_files + a_face_files))
            name, role = self.get_name_role(person_id)
            out.append(PersonRef(
                person_id=person_id,
                name=name,
                role=role,
                has_tier_a=has_a,
                num_tier_c=num_c, num_tier_a_body=num_a_body,
                tier_a_fingerprint=tier_a_fp,
            ))
        return out

    def get_name(self, person_id: str) -> str | None:
        """从 meta.json 读真名(显示主键)。"""
        return _read_person_meta(self.persons_dir / person_id).get("name")

    def get_role(self, person_id: str) -> str | None:
        """从 meta.json 读家庭角色(可空)。"""
        return _read_person_meta(self.persons_dir / person_id).get("role")

    def get_name_role(self, person_id: str) -> tuple[str | None, str | None]:
        """一次读 meta.json 取 (name, role)——避免 get_name + get_role 读两遍同一文件。"""
        meta = _read_person_meta(self.persons_dir / person_id)
        return meta.get("name"), meta.get("role")

    def set_meta(self, person_id: str, *, name=_META_UNSET, role=_META_UNSET) -> None:
        """一次 read+merge+write meta.json 写 name/role；omit 的字段(默认 _META_UNSET)保持原值。
        合并写出口——改名 / backfill 同时改两字段时只一次 IO。单字段写可用 set_name。"""
        _write_person_meta(self.persons_dir / person_id, name=name, role=role)

    def set_name(self, person_id: str, name: str | None) -> None:
        """写真名到 meta.json，不动 role（set_meta 的单字段便捷封装）。"""
        self.set_meta(person_id, name=name)

    def has_person_dir(self, person_id: str) -> bool:
        """该 person 在文件层是否已有目录(已注册样本/meta)。封装 persons_dir/<id> 布局,
        供调用方做"无样本 person 不建目录"之类的守卫, 不必自己拼内部路径。"""
        return (self.persons_dir / person_id).is_dir()

    # ── 显式头像（展示层）────────────────────────────────────────────────────
    # 落点 <root>/avatars/persons/<id>.<ext>，与 tier_a/tier_c 识别数据分离；**不建
    # persons/<id>/ 目录**——给没录入的人设头像也不会让空目录进 list_persons、
    # 从而不扰动 IdentityEngine 的 person 快照。展示优先级由调用方决定（显式头像 >
    # 否则回落 tier_a face[0]），见 person/router 的 GET /avatar。

    def person_avatar_path(self, person_id: str) -> Path | None:
        """显式头像文件路径（无则 None）。"""
        return _avatar.avatar_path(self.root, "persons", person_id)

    def set_person_avatar(self, person_id: str, data: bytes, ext: str) -> str:
        """写显式头像，返回规范化后的 ext。"""
        return _avatar.set_avatar(self.root, "persons", person_id, data, ext)

    def clear_person_avatar(self, person_id: str) -> None:
        """清显式头像（"恢复默认"→下次读取回落 face[0]）。"""
        _avatar.remove_avatar(self.root, "persons", person_id)

    def list_person_avatar_exts(self) -> dict[str, str]:
        """一次扫描返回 ``{person_id: ext}``，供 list_persons 批量填 avatar_ext。"""
        return _avatar.list_avatar_exts(self.root, "persons")

    def add_face_only_sample(self, person_id: str, face_crop, source: str = "user_upload") -> bool:
        """只写一张 face_*.png + sidecar, 绕过 add_tier_a_sample 的"必须有 body"约束。

        web 批量注册里用户可能只勾 face(纯人脸); add_tier_a_sample 以 body_crop 必填, 直接调
        会被迫多写一张冗余 body。本方法保持与正常 face 样本同格式(omni gallery 仍能识别)。
        face 容量(tier_a_max // 2)已满返 False。
        """
        tier_a_dir = self.persons_dir / person_id / "tier_a"
        tier_a_dir.mkdir(parents=True, exist_ok=True)
        existing_face = sorted(_list_crop_files(tier_a_dir, "face"))
        if len(existing_face) >= self.tier_a_max // 2:
            return False
        face_idx = _next_index(existing_face, "face_")
        face_path = tier_a_dir / f"face_{face_idx:03d}.png"
        cv2.imwrite(str(face_path), face_crop)
        self._write_sidecar(
            face_path, tier="a", kind="face", source=source,
            captured_at=time.time(), extra_meta=None,
        )
        return True

    def get_gallery_for_omni(
        self,
        person_ids: list[str] | None = None,
        body_refs_per_person: int = 3,
        face_refs_per_person: int = 3,
    ) -> dict[str, GallerySamples]:
        """喂给 omni prompt 用的 gallery 样本快照（旧出口，无缓存，每窗口现读现拼）。

        策略：
          - Tier A 优先；不足时补 Tier C 最近样本
          - body_attr_text 字段当前永远 None（功能未实施）

        ⚠️  ``prompt_builder._build_fused_user_content`` 已切到 ``get_gallery_composites_for_omni``
        新出口（带缓存）。本方法仅保留给那些显式需要 ``body_crops`` / ``face_crops``
        ndarray 的调用方（例如离线分析脚本）；正常 omni 派发路径**不再走**这里。
        """
        target_ids = person_ids if person_ids is not None else [
            p.person_id for p in self.list_persons()
        ]
        out: dict[str, GallerySamples] = {}
        for pid in target_ids:
            gs = self._build_gallery_one(pid, body_refs_per_person, face_refs_per_person)
            if gs is not None:
                out[pid] = gs
        return out

    def get_gallery_composites_for_omni(
        self,
        person_ids: list[str] | None = None,
        body_n: int = 3,
        face_n: int = 3,
        body_height: int = 256,
        face_height: int = 128,
        jpeg_quality: int = 85,
        cam_id: str = "",
    ) -> dict[str, GallerySamples]:
        """带 L1+L2 缓存的 composite jpeg 出口（fused omni 派发路径专用）。

        返回 ``GallerySamples``：``body_composite_jpeg`` / ``face_composite_jpeg``
        填好可直接 base64 塞入 prompt；``body_crops`` / ``face_crops`` 留空，
        节省 imread。

        缓存语义：
          - L1（per-person 最终 jpeg）：选样后所有 ``(filename, mtime)`` + 全部
            高度/质量参数都不变 → 直接返回 jpeg bytes，零 imread / 零 encode。
          - L2（per-tier ndarray）：tier_a 段和 tier_c 段各自缓存为已 hstack 完
            的 ndarray（未做 ``max_total_width`` 兜底）。当 tier_c FIFO 变化时
            tier_a 段命中可直接复用，省掉 tier_a 那部分 imread。
          - L3（slot 级，未实施）：见 ``_PersonTierCache`` 文档；当前评估收益/复杂度比
            偏低，暂留设计文档。

        拼接顺序固定 ``[tier_a, tier_c]`` —— 与旧 ``_build_gallery_one`` 的混插
        顺序不同（旧实现里 "回填多余 tier_a 到 tier_c 之后" 实际上是 dead branch，
        永远不会触发；新实现简化为分段独立，让 L2 缓存语义清晰）。
        omni 看到的是一张拼好的 banner，前后顺序无识别影响。
        """
        target_ids = person_ids if person_ids is not None else [
            p.person_id for p in self.list_persons()
        ]
        # GC：把已经不存在的 person_id 从缓存里清掉，防长跑实例内存泄漏。L1/L2 键是
        # (pid, cam)，按 key[0] 的 person 存活性清；phash 缓存仍按 pid。
        live_set = set(target_ids)
        with self._cache_lock:
            for key in list(self._composite_cache.keys()):
                if key[0] not in live_set:
                    self._composite_cache.pop(key, None)
            for key in list(self._tier_nd_cache.keys()):
                if key[0] not in live_set:
                    self._tier_nd_cache.pop(key, None)
            for pid in list(self._tier_a_phash_cache.keys()):
                if pid not in live_set:
                    self._tier_a_phash_cache.pop(pid, None)
            for pid in list(self._frontal_face_cache.keys()):
                if pid not in live_set:
                    self._frontal_face_cache.pop(pid, None)
            for key in list(self._drift_ref_cache.keys()):
                if key[0] not in live_set:
                    self._drift_ref_cache.pop(key, None)

        out: dict[str, GallerySamples] = {}
        for pid in target_ids:
            gs = self._build_composite_one(
                pid,
                body_n=body_n, face_n=face_n,
                body_height=body_height, face_height=face_height,
                jpeg_quality=jpeg_quality,
                cam_id=cam_id,
            )
            # 过滤无 body composite 的 person:对齐旧 get_gallery_for_omni 的
            # ``if not samples.body_crops: continue`` 行为。无 body 样本(轻量注册 /
            # tier_a 文件被误删)的 person 直接跳过,避免触发 prompt_builder 的
            # 全或无 pre-flight 整段放弃 —— 这种 person 本来就不该污染他人渲染。
            # 副作用:"有 body 但 imread 全失败"的边缘场景也会被过滤、不再触发
            # 全或无保护;但该场景概率极低(需文件被破坏),且即使被过滤也只是
            # 该 person 不出现在 prompt 里、omni 把他识别为 unknown,不会贴错到他人。
            if gs is not None and gs.body_composite_jpeg is not None:
                out[pid] = gs
        return out

    # ----- composite 缓存内部 -----

    def _build_composite_one(
        self, person_id: str, *,
        body_n: int, face_n: int,
        body_height: int, face_height: int,
        jpeg_quality: int,
        cam_id: str = "",
    ) -> Optional[GallerySamples]:
        from miloco.perception.engine.identity.gallery_composite import (
            DEFAULT_MAX_WIDTH,
            encode_png_bytes,
        )

        person_dir = self.persons_dir / person_id
        if not person_dir.is_dir():
            self._invalidate_person_cache(person_id)
            return None

        a_body_picked, c_body_picked = self._pick_body_files(person_dir, body_n, cam_id=cam_id)
        face_picked = self._pick_face_files(person_dir, face_n)

        body_a_fp = self._fingerprint(a_body_picked)
        body_c_fp = self._fingerprint(c_body_picked)
        face_fp = self._fingerprint(face_picked)

        # —— L1 命中检查 ——
        # 注:composite 现走 PNG 无损(encode_png_bytes 忽略 quality), jpeg_quality 在此
        # 仅作缓存键判别项, 不再影响输出画质;留它是为兼容 encode_fn 签名 + 缓存键稳定。
        body_full_fp = (body_a_fp, body_c_fp, body_height, jpeg_quality)
        face_full_fp = (face_fp, face_height, jpeg_quality)

        l1 = self._composite_cache.get((person_id, cam_id))
        if l1 and l1.body_fp == body_full_fp and l1.face_fp == face_full_fp:
            name, role = self.get_name_role(person_id)
            return GallerySamples(
                person_id=person_id,
                name=name,
                role=role,
                body_composite_jpeg=l1.body_jpeg,
                face_composite_jpeg=l1.face_jpeg,
            )

        # —— L1 miss → 走 L2 / 重建 ——
        # 锁内 setdefault:与 _invalidate / GC 对 _tier_nd_cache 的迭代串行,防"迭代中改大小"
        # 崩溃(_build 跑在推理线程、_invalidate 跑在 API 线程)。重活(imread/hstack)在锁外。
        with self._cache_lock:
            tier_cache = self._tier_nd_cache.setdefault((person_id, cam_id), _PersonTierCache())

        # body：tier_a / tier_c 段分别走 L2，最后合并
        a_nd = self._get_or_build_tier_nd(
            tier_cache, "body", "a",
            picked=a_body_picked, fp=body_a_fp,
            target_height=body_height,
        )
        c_nd = self._get_or_build_tier_nd(
            tier_cache, "body", "c",
            picked=c_body_picked, fp=body_c_fp,
            target_height=body_height,
        )
        body_jpeg = self._merge_and_encode(
            [x for x in (a_nd, c_nd) if x is not None],
            target_height=body_height,
            max_total_width=DEFAULT_MAX_WIDTH,
            jpeg_quality=jpeg_quality,
            encode_fn=encode_png_bytes,
        )

        # face：只有 tier_a 段
        face_nd = self._get_or_build_tier_nd(
            tier_cache, "face", "a",
            picked=face_picked, fp=face_fp,
            target_height=face_height,
        )
        face_jpeg = self._merge_and_encode(
            [face_nd] if face_nd is not None else [],
            target_height=face_height,
            max_total_width=DEFAULT_MAX_WIDTH,
            jpeg_quality=jpeg_quality,
            encode_fn=encode_png_bytes,
        )

        # 更新 L1(锁内写:与 _invalidate / GC 对 _composite_cache 的迭代串行,防崩溃)
        with self._cache_lock:
            self._composite_cache[(person_id, cam_id)] = _PersonCompositeCache(
                body_jpeg=body_jpeg, face_jpeg=face_jpeg,
                body_fp=body_full_fp, face_fp=face_full_fp,
            )

        name, role = self.get_name_role(person_id)
        return GallerySamples(
            person_id=person_id,
            name=name,
            role=role,
            body_composite_jpeg=body_jpeg,
            face_composite_jpeg=face_jpeg,
        )

    def _get_or_build_tier_nd(
        self,
        tier_cache: "_PersonTierCache",
        kind: str,        # "body" | "face"
        tier: str,        # "a" | "c"
        *,
        picked: list[Path],
        fp: "_TierFingerprint",
        target_height: int,
    ) -> Optional[NDArray[np.uint8]]:
        """L2 入口：返回 (kind, tier) 段的 hstack ndarray；命中跳过 imread + resize + hstack。

        (kind, tier) 必须是 ``_PersonTierCache`` 显式声明的槽位组合,否则下面 getattr
        会 AttributeError。当前白名单 = body→(a, c) + face→(a);face_tier_c 暂不需要
        (face 仅累积 tier_a)。未来要加新槽位时同步给 ``_PersonTierCache`` 加字段。
        """
        from miloco.perception.engine.identity.gallery_composite import hstack_to_height

        assert (kind, tier) in {("body", "a"), ("body", "c"), ("face", "a")}, (
            f"_get_or_build_tier_nd: 不支持的槽位 (kind={kind!r}, tier={tier!r}); "
            f"若新增槽位需先给 _PersonTierCache 加对应字段"
        )

        # 取该 (kind, tier) 槽位的 ndarray / fp 字段名
        nd_attr = f"{kind}_tier_{tier}_nd"
        fp_attr = f"{kind}_tier_{tier}_fp"

        # 复合 key: (文件指纹, target_height)。height 内联进 fp 是为了让命中判定
        # 严格落到 per-tier 维度,避免共享 height 字段被另一 tier 改写后引起本 tier
        # 误命中(返回旧高度 ndarray, 后续 hstack 崩溃)。
        full_key = (fp, target_height)

        cached_key = getattr(tier_cache, fp_attr)
        if cached_key == full_key:
            return getattr(tier_cache, nd_attr)

        # miss → 重读重拼
        if not picked:
            nd: Optional[NDArray[np.uint8]] = None
        else:
            crops = [self._load_image(f) for f in picked]
            crops = [c for c in crops if c is not None]
            # 不在 L2 做 max_total_width 兜底，留给合并阶段统一处理
            nd = hstack_to_height(crops, target_height, max_total_width=None) if crops else None

        setattr(tier_cache, nd_attr, nd)
        setattr(tier_cache, fp_attr, full_key)
        return nd

    @staticmethod
    def _merge_and_encode(
        parts: list[NDArray[np.uint8]],
        *,
        target_height: int,
        max_total_width: int,
        jpeg_quality: int,
        encode_fn,
    ) -> Optional[bytes]:
        """把 (tier_a_nd, tier_c_nd 可选) 合并 + max_width 兜底 + jpeg encode。

        parts 已由 L2 cache 统一过 height,直接 np.hstack 即可,
        无需走 hstack_to_height 的 height 归一化路径。
        """
        if not parts:
            return None
        full = parts[0] if len(parts) == 1 else np.hstack(parts)
        # 总宽度兜底：fused omni 主路径在这里走，跟 gallery_composite.hstack_to_height 镜像。
        # 这里一定是降采样，用 INTER_AREA 抗混叠（hstack 引入的新边界，LINEAR 降采样易出伪影）
        if full.shape[1] > max_total_width:
            scale = max_total_width / full.shape[1]
            new_w = int(full.shape[1] * scale)
            new_h = int(target_height * scale)
            full = cv2.resize(full, (new_w, new_h), interpolation=cv2.INTER_AREA)
        # encode_fn 现为 encode_png_bytes(PNG 无损), quality 入参按 encode_fn 协议透传但被忽略;
        # 仍传 jpeg_quality 是为保持 encode_fn 签名统一(改回 jpeg 编码器时即生效)。
        return encode_fn(full, quality=jpeg_quality)

    def _pick_body_files(
        self, person_dir: Path, body_n: int, cam_id: str | None = None,
    ) -> tuple[list[Path], list[Path]]:
        """选样 body：返回 (tier_a_picked, tier_c_picked)，各自有序，合计 ≤ body_n。

        策略：
          - tier_a 按 filename 升序，tier_c 按 mtime 倒序（取最近）
          - 有 tier_c 时给它留 1 槽，剩余给 tier_a；tier_c 实际不够则槽位留空
            （**不**回填 tier_a；旧 `_build_gallery_one` 的回填分支算上 reserved_c
            的代数关系实际永远 dead，省下来 L2 缓存能干净地分两段）

        ``cam_id``：tier_c 按相机子目录隔离,识别 cam-X 时只取 ``tier_c/<X>/``(本相机空→
        退纯 tier_a,不借其他相机)。根下 legacy 扁平样本不在任何 cam 子目录里→自然冻结、
        不进 gallery。``cam_id None``(如 get_tier_a_verify_crops 只要 tier_a)→ 不取 tier_c。
        """
        tier_a_dir = person_dir / "tier_a"
        tier_c_dir = (
            person_dir / "tier_c" / _sanitize_cam_did(cam_id) if cam_id else None
        )

        a_files = sorted(_list_crop_files(tier_a_dir, "body")) if tier_a_dir.is_dir() else []
        c_files: list[Path] = []
        if tier_c_dir is None or _GALLERY_TIER_C_MODE == "off":
            # cam_id 缺省 / E6a：tier_c 不回喂，gallery body 列自然回退到 3 张 tier_a
            pass
        elif _GALLERY_TIER_C_MODE == "recent":
            if tier_c_dir.is_dir():
                c_files = sorted(
                    _list_crop_files(tier_c_dir, "body"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
        elif _GALLERY_TIER_C_MODE == "trusted":
            # C3: 只回喂"写库时 omni 同人校验判通过"的 tier_c(sidecar verify_same_person==True)。
            # 该标记仅在写库前校验生效时写入, 故天然排除校验上线前/未校验的样本, 不把脏样本喂回
            # gallery。取最新(mtime 倒序)、留 1 槽; 无合格样本则 c_files 空 → 自然回退纯 tier_a。
            if tier_c_dir.is_dir():
                c_files = sorted(
                    (f for f in _list_crop_files(tier_c_dir, "body")
                     if self._tier_c_sample_verified(f)),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )

        reserved_c = 1 if c_files else 0
        a_take = min(len(a_files), max(0, body_n - reserved_c))
        a_picked = list(a_files[:a_take])
        remaining = body_n - a_take
        c_picked = list(c_files[:remaining]) if remaining > 0 and c_files else []
        return a_picked, c_picked

    @staticmethod
    def _face_wh_ratio_of(face_path: Path) -> float | None:
        """读 face crop(.jpg/.png) 尺寸算 宽/高 比; 读失败返 None。

        face crop 落盘是 cv2.imwrite 直存不 resize/pad, 故文件 w/h == 注册判正脸时
        ``_face_wh_ratio`` 看的 crop w/h, 口径一致。仅在 face 样本集变化(正脸缓存 miss)时
        调——稳态(含 composite L1 命中)走 ``_frontal_face_cache`` 复用、不解码; 每人 face
        ≤ tier_a_max//2(=5) 张、文件微小, miss 时一次性扫描成本可忽略。
        """
        img = cv2.imread(str(face_path))
        if img is None:
            return None
        h, w = img.shape[:2]
        return float(w) / max(1, h)

    def _pick_frontal_face(self, person_id: str, files: list[Path]) -> "Path | None":
        """从 ``files`` 里挑一张正脸(w/h ∈ 正脸区间)置首用; 无正脸返 None。

        **按 face 文件指纹 memo 化**: _pick_face_files 在 composite L1 命中判断之前被无条件
        调用, 不缓存就每窗对每张 face imread。指纹(名+mtime)没变即复用上次结果、零解码;
        face 样本一写/删指纹即变、自然失效。
        """
        fp = self._fingerprint(files)
        with self._cache_lock:
            cached = self._frontal_face_cache.get(person_id)
            if cached is not None and cached[0] == fp:
                return cached[1]
        # miss(首次 / face 样本集变化): imread 扫一遍找正脸。复用注册正脸口径(函数内 import
        # 规避循环依赖, 同本文件其他函数内 import 范式)。
        from miloco.perception.engine.identity.registration_filter import (
            DEFAULT_FRONTAL_WH_MAX,
            DEFAULT_FRONTAL_WH_MIN,
        )
        frontal = next(
            (
                f for f in files
                if (r := self._face_wh_ratio_of(f)) is not None
                and DEFAULT_FRONTAL_WH_MIN <= r < DEFAULT_FRONTAL_WH_MAX
            ),
            None,
        )
        with self._cache_lock:
            self._frontal_face_cache[person_id] = (fp, frontal)
        return frontal

    def _pick_face_files(self, person_dir: Path, face_n: int) -> list[Path]:
        """选 gallery 人脸样本; **正脸优先**: 把一张正脸排到第一(= composite 最左)。

        正脸判据复用注册口径: face crop 宽高比 w/h ∈ [DEFAULT_FRONTAL_WH_MIN,
        DEFAULT_FRONTAL_WH_MAX)(侧脸更窄、抬头转头更宽)。取文件名序最早的正脸(face_001
        通常即注册 seed), 即使它本在 ``[:face_n]`` 之外也拉进结果并置首。无任何样本落正脸带
        → 回退文件名序(该人没正脸样本只能尽力, 不凭空造)。端上 v1.2 已无 landmark/位姿,
        wh 比是唯一可用正脸代理。正脸优选经 ``_pick_frontal_face`` 按指纹 memo 化, 稳态零 imread。
        """
        if face_n <= 0:                                  # 不要人脸: 不被正脸前插误返 1 张
            return []
        tier_a_dir = person_dir / "tier_a"
        if not tier_a_dir.is_dir():
            return []
        # _list_crop_files 兼容 .jpg/.jpeg/.png(72a8233 PNG 无损化后新写为 .png)
        files = sorted(_list_crop_files(tier_a_dir, "face"))
        if not files:
            return []
        frontal = self._pick_frontal_face(person_dir.name, files)
        if frontal is None:
            return files[:face_n]                        # 无正脸 → 保持现序(best-effort)
        rest = [f for f in files if f != frontal]
        return [frontal] + rest[: max(0, face_n - 1)]    # 正脸排首 + 其余按原序补满 face_n

    @staticmethod
    def _tier_c_sample_verified(jpg_path: Path) -> bool:
        """trusted 模式信任过滤: 读 tier_c body 的 sidecar, 判写库前 omni 同人校验是否判过
        (verify_same_person==True)。无 sidecar / 无该字段 / 读失败 → False(保守: 不回喂未
        确认样本, 也天然排除校验上线前的老样本)。"""
        sidecar = jpg_path.with_suffix(".json")
        if not sidecar.exists():
            return False
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        return meta.get("verify_same_person") is True

    def get_tier_a_verify_crops(
        self, person_id: str, *, body_n: int = 3, face_n: int = 3,
    ) -> tuple[list[NDArray[np.uint8]], list[NDArray[np.uint8]]]:
        """读该 person 的 tier_a body + face crop(BGR ndarray), 供"写 tier_c 前同人
        校验"(设计文档 E7)合成 GALLERY 用。只取 tier_a(权威参考), 不含 tier_c。
        返回 (body_crops, face_crops); 读不出的文件跳过。"""
        person_dir = self.persons_dir / person_id
        a_body_files, _ = self._pick_body_files(person_dir, body_n)
        a_face_files = self._pick_face_files(person_dir, face_n)
        body_crops = [
            img for f in a_body_files if (img := cv2.imread(str(f))) is not None
        ]
        face_crops = [
            img for f in a_face_files if (img := cv2.imread(str(f))) is not None
        ]
        return body_crops, face_crops

    @staticmethod
    def _fingerprint(paths: list[Path]) -> "_TierFingerprint":
        """把一组文件 → ``((name, mtime), ...)``，作 L1/L2 命中比对。"""
        out: list[tuple[str, float]] = []
        for f in paths:
            try:
                out.append((f.name, f.stat().st_mtime))
            except OSError:
                # 文件刚被删（race）—— 用 0 占位，下窗口 glob 时自然不会再选它
                out.append((f.name, 0.0))
        return tuple(out)

    # -------------------------------------------------------------------------
    # tier_c 写入前的 pHash 距离校验（G · 物理特征兜底）
    # -------------------------------------------------------------------------

    def tier_c_phash_check(
        self,
        person_id: str,
        new_crop: NDArray[np.uint8],
    ) -> int | None:
        """算 ``new_crop`` 与该 person 所有 tier_a body 的最小汉明距离。

        调用方（``IdentityEngine`` 的 tier_c 写库 worker）拿到返回值后自己决策阈值：
        距离过大视为"跟权威样本差距过大、是脏样本"，拒绝写入 tier_c。

        Returns:
            int: 最小汉明距离（0-64，越小越像）
            None: 该 person 没有 tier_a body 样本可比对，调用方应当放行（避免误拦冷启）

        实现：tier_a body 的 pHash 用 ``self._tier_a_phash_cache`` 按 (filename, mtime)
        粒度缓存，稳态命中下 0 次 imread；只有该 tier_a 文件 mtime 变化才重读重算。
        """
        person_dir = self.persons_dir / person_id
        tier_a_dir = person_dir / "tier_a"
        if not tier_a_dir.is_dir():
            return None

        files = sorted(_list_crop_files(tier_a_dir, "body"))
        if not files:
            return None

        # 第一段(锁内)：读 cache 命中 + 收集 miss + GC stale。只动 dict、O(1),
        # 不含 imread/_phash。护住与推理线程 gallery GC / 其它 to_thread phash 校验
        # 并发改同一 dict(RuntimeError / 撕裂)。
        live_names: set[str] = set()
        tier_a_hashes: list[int] = []
        to_compute: list[tuple[str, Path, float]] = []
        with self._cache_lock:
            cache = self._tier_a_phash_cache.setdefault(person_id, {})
            for f in files:
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                live_names.add(f.name)
                entry = cache.get(f.name)
                if entry is not None and entry[0] == mtime:
                    tier_a_hashes.append(entry[1])
                else:
                    # cache miss / mtime 变了 → 留到锁外重算
                    to_compute.append((f.name, f, mtime))
            # GC：该 person 缓存里有但当前 tier_a 不存在的 filename 删掉
            for stale in [n for n in cache if n not in live_names]:
                cache.pop(stale, None)

        # 第二段(锁外)：cache miss 的重读重算(cv2.imread + _phash,数十 ms),
        # 绝不占锁——否则推理 GC 会偶发等一次 imread,违背"锁只护同步段"。
        computed: list[tuple[str, float, int]] = []
        for name, f, mtime in to_compute:
            img = cv2.imread(str(f))
            if img is None:
                logger.warning("tier_c_phash_check: 读 tier_a body 失败 %s", f)
                continue
            h = _phash(img)
            computed.append((name, mtime, h))
            tier_a_hashes.append(h)

        # 第三段(锁内)：把重算结果写回 cache。单 key 赋值、O(1);setdefault 兜
        # 两段之间被 _invalidate pop 掉 inner dict 的极端情形。
        if computed:
            with self._cache_lock:
                cache = self._tier_a_phash_cache.setdefault(person_id, {})
                for name, mtime, h in computed:
                    cache[name] = (mtime, h)

        if not tier_a_hashes:
            return None

        new_hash = _phash(new_crop)
        return min(_hamming(new_hash, h) for h in tier_a_hashes)

    # -------------------------------------------------------------------------
    # tier_c 写入前的 ReID 余弦观测(纯观测, 不做拦截决策)
    # -------------------------------------------------------------------------

    def tier_c_reid_cos_observe(
        self,
        person_id: str,
        new_emb: "NDArray[np.float32] | None",
    ) -> tuple[float | None, float | None]:
        """观测用:``new_emb`` 与 (tier_a 质心, tier_c 逐张最大) 的 ReID 余弦。

        返回 ``(cos_vs_tier_a, cos_vs_tier_c)``:
          - cos_vs_tier_a: 与该 person tier_a body 质心的余弦;无可用质心 → None
          - cos_vs_tier_c: 与该 person 各 tier_c body emb 的最大余弦;无样本 → None
        ``new_emb`` 为 None 时两项均 None。

        口径沿用库内既有约定:tier_a 用质心(``get_person_mean_emb``)、tier_c 逐张比
        (``get_person_tier_c_embs``,其文档说明 tier_c 内部差异大、不宜 mean)。emb 均
        128-dim L2-normalized,点积即余弦。**本方法只算数、不决定是否入库。**
        """
        if new_emb is None:
            return None, None
        mean = self.get_person_mean_emb(person_id)
        cos_a = float(np.dot(new_emb, mean)) if mean is not None else None
        tier_c_embs = self.get_person_tier_c_embs(person_id)
        cos_c = max((float(np.dot(new_emb, e)) for e in tier_c_embs), default=None)
        return cos_a, cos_c

    def _build_gallery_one(
        self,
        person_id: str,
        body_n: int,
        face_n: int,
    ) -> Optional[GallerySamples]:
        # ⚠️ DEAD 路径(仅旧 get_gallery_for_omni 出口用,主路径走 get_gallery_composites_for_omni)。
        # 其 tier_c glob 未做 per-cam 子目录适配:子目录布局下只会读到根下 legacy 扁平样本
        # (安全降级),不会读新写入的 tier_c/<cam>/。复活前需按 _pick_body_files 同样改 cam_id。
        person_dir = self.persons_dir / person_id
        if not person_dir.is_dir():
            return None

        # Tier A body
        a_body_files = sorted(_list_crop_files(person_dir / "tier_a", "body")) if (person_dir / "tier_a").is_dir() else []
        a_face_files = sorted(_list_crop_files(person_dir / "tier_a", "face")) if (person_dir / "tier_a").is_dir() else []
        # Tier C body（按时间戳取最近）
        c_body_files = []
        if (person_dir / "tier_c").is_dir():
            c_body_files = sorted(
                _list_crop_files(person_dir / "tier_c", "body"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )

        # 选样策略：A 优先 + 至少 1 张 C（让在线累积的最近样本始终参与匹配）
        # - 有 C 时：留 1 槽给 C 最近，A 取 body_n-1 张
        # - 无 C 时：A 取 body_n 张
        # - A 不够时：剩余槽全给 C
        reserved_c = 1 if c_body_files else 0
        a_take = min(len(a_body_files), max(0, body_n - reserved_c))
        body_files = list(a_body_files[:a_take])
        remaining = body_n - len(body_files)
        if remaining > 0 and c_body_files:
            body_files.extend(c_body_files[:remaining])
        # C 也不足时，回填多余的 A
        if len(body_files) < body_n and len(a_body_files) > a_take:
            extra = a_body_files[a_take : a_take + (body_n - len(body_files))]
            body_files.extend(extra)

        face_files = a_face_files[:face_n]

        body_crops = [self._load_image(f) for f in body_files]
        face_crops = [self._load_image(f) for f in face_files]
        body_crops = [c for c in body_crops if c is not None]
        face_crops = [c for c in face_crops if c is not None]

        name, role = self.get_name_role(person_id)
        return GallerySamples(
            person_id=person_id,
            name=name,
            role=role,
            body_crops=body_crops,
            face_crops=face_crops,
            body_attr_text=None,  # 当前未实施 body_attr_text，永远为 None
        )

    @staticmethod
    def _load_image(path: Path) -> Optional[NDArray[np.uint8]]:
        img = cv2.imread(str(path))
        if img is None:
            logger.warning("Failed to load image %s", path)
        return img

    # -------------------------------------------------------------------------
    # 写入
    # -------------------------------------------------------------------------

    def add_tier_a_sample(
        self,
        person_id: str,
        body_crop: NDArray[np.uint8],
        face_crop: NDArray[np.uint8] | None = None,
        source: str = "user_upload",
        captured_at: float | None = None,
        name: str | None = None,
        role: str | None = None,
        extra_meta: dict | None = None,
        reid_embedding: NDArray[np.float32] | None = None,
    ) -> bool:
        """添加 Tier A 样本（用户登记）。

        每张 crop 写入时同步生成 ``<filename>.json`` sidecar 元信息：
            - tier: "a"
            - kind: "body" | "face"
            - source: 来源标签（"user_upload" / "extract_ui" / "register_cli" 等）
            - captured_at / captured_at_iso: 采集时间
            - extra_meta: 调用方传入的扩展元信息（可含 conf / reason / track_id 等）

        签名说明：**不接受 ``body_attr_text`` 形参**（该字段对应的特征文本功能尚未实施）。

        Returns:
            True 写入成功；False 容量已满或图像无效。
        """
        if body_crop is None or body_crop.size == 0:
            return False

        person_dir = self.persons_dir / person_id
        tier_a_dir = person_dir / "tier_a"
        tier_a_dir.mkdir(parents=True, exist_ok=True)

        # 容量检查
        existing_body = sorted(_list_crop_files(tier_a_dir, "body"))
        if len(existing_body) >= self.tier_a_max // 2:
            logger.info("Tier A body 容量已满 person_id=%s", person_id)
            return False

        # 写入 body —— 用 max(existing_idx) + 1 而不是 len + 1，
        # 删除中间样本后再加新样本不会覆盖剩余文件
        next_idx = _next_index(existing_body, "body_")
        body_path = tier_a_dir / f"body_{next_idx:03d}.png"
        cv2.imwrite(str(body_path), body_crop)

        ts = captured_at if captured_at is not None else time.time()
        self._write_sidecar(body_path, tier="a", kind="body", source=source,
                             captured_at=ts, extra_meta=extra_meta)
        # 同名 .npy 落 reid_embedding(可选)。后续注册流程把陌生人池里 per-crop
        # emb 直接传进来,不必重抽。读时调 get_sample_embedding。
        if reid_embedding is not None:
            self._write_embedding(body_path, reid_embedding)

        # 写入 face（可选）
        if face_crop is not None and face_crop.size > 0:
            existing_face = sorted(_list_crop_files(tier_a_dir, "face"))
            if len(existing_face) < self.tier_a_max // 2:
                face_idx = _next_index(existing_face, "face_")
                face_path = tier_a_dir / f"face_{face_idx:03d}.png"
                cv2.imwrite(str(face_path), face_crop)
                self._write_sidecar(face_path, tier="a", kind="face", source=source,
                                     captured_at=ts, extra_meta=extra_meta)

        # 写入目录级 meta（name / role，与 sidecar 共存）
        if name is not None or role is not None:
            _write_person_meta(
                person_dir,
                name=name if name is not None else _META_UNSET,
                role=role if role is not None else _META_UNSET,
            )

        logger.info("Tier A 样本写入: person_id=%s body=%s face=%s",
                    person_id, body_path.name, face_crop is not None)
        return True

    def add_tier_c_sample(
        self,
        person_id: str,
        body_crop: NDArray[np.uint8],
        captured_at: float | None = None,
        source: str = "auto_accumulate",
        extra_meta: dict | None = None,
        reid_embedding: NDArray[np.float32] | None = None,
        *,
        cam_id: str,
    ) -> bool:
        """添加 Tier C 样本（系统在线累积，FIFO + pHash 冗余过滤）。

        每张 crop 写入时同步生成 ``<filename>.json`` sidecar 元信息，
        ``extra_meta`` 可携带触发本次累积的 ``track_id`` / ``confidence`` / ``reason``，
        便于后期 review 哪条 omni response 把样本入了库。

        ``cam_id``：样本来源相机。tier_c 按 ``tier_c/<cam>/`` 子目录隔离——pHash 冗余、
        FIFO、写入都限本相机子目录(每 (person,cam) 各自 FIFO ``tier_c_max``)。

        Returns:
            True 写入成功；False 冗余过滤拒绝或图像无效。
        """
        if body_crop is None or body_crop.size == 0:
            return False

        person_dir = self.persons_dir / person_id
        cam_dir = person_dir / "tier_c" / _sanitize_cam_did(cam_id)
        if not (person_dir / "tier_a").is_dir():
            # 没有 Tier A 不允许累积 Tier C（防野生 person_id 被错误累积）
            logger.warning("Tier A 不存在，不累积 Tier C: person_id=%s", person_id)
            return False
        cam_dir.mkdir(parents=True, exist_ok=True)

        # 整段"读改写"在 per-(person,cam) 锁内串行(多镜头共享 library, worker 在不同线程并发
        # 写时:同 person 同相机串行防互删/撞名,不同相机写不同子目录可并发)。
        with self._tier_c_write_lock(person_id, cam_id):
            # pHash 冗余过滤：与本相机最近 5 张比较
            new_hash = _phash(body_crop)
            recent = sorted(_list_crop_files(cam_dir, "body"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
            for old in recent:
                old_img = cv2.imread(str(old))
                if old_img is None:
                    continue
                old_hash = _phash(old_img)
                if _hamming(new_hash, old_hash) < self.phash_distance:
                    logger.debug("pHash 冗余过滤拒绝: person_id=%s cam=%s vs %s", person_id, cam_id, old.name)
                    return False

            # FIFO 容量管理（本相机子目录内，同步删除其 sidecar + .npy emb 文件）
            all_c = sorted(_list_crop_files(cam_dir, "body"), key=lambda p: p.stat().st_mtime)
            while len(all_c) >= self.tier_c_max:
                oldest = all_c.pop(0)
                oldest.unlink(missing_ok=True)
                oldest.with_suffix(".json").unlink(missing_ok=True)
                oldest.with_suffix(".npy").unlink(missing_ok=True)

            ts = captured_at if captured_at is not None else time.time()
            ts_ms = int(ts * 1000)  # ms 时间戳作文件名
            body_path = cam_dir / f"body_{ts_ms}.png"
            # 防同毫秒撞名(多镜头 / 快速连写): 已存在则 +1ms 直到空位。整段在 per-(person,cam)
            # 锁内, 文件名保持纯整数 body_<ms>.png 格式(下游按 mtime 排序 + 多扩展名 glob, 不受影响)。
            while body_path.exists():
                ts_ms += 1
                body_path = cam_dir / f"body_{ts_ms}.png"
            cv2.imwrite(str(body_path), body_crop)
            self._write_sidecar(body_path, tier="c", kind="body", source=source,
                                 captured_at=ts, extra_meta=extra_meta)
            if reid_embedding is not None:
                self._write_embedding(body_path, reid_embedding)

            logger.info("Tier C 样本写入: person_id=%s file=%s", person_id, body_path.name)
            return True

    def delete_person(self, person_id: str) -> bool:
        """级联删 person 的所有样本（用于 PersonService.delete_person 调用）。"""
        person_dir = self.persons_dir / person_id
        if not person_dir.is_dir():
            return False
        shutil.rmtree(person_dir)
        # 同步清掉缓存，防长跑实例内存泄漏；下一窗口的 GC 也会兜底
        self._invalidate_person_cache(person_id)
        logger.info("级联删除 identity_lib/persons/%s", person_id)
        return True

    # -------------------------------------------------------------------------
    # 身份库管理(M10,v1.2 新增):合并 / 拆分
    # -------------------------------------------------------------------------

    def merge_persons(
        self, target_id: str, source_ids: list[str],
    ) -> "MergeResult":
        """把 source_ids 的样本并入 target_id;删除 source 目录。

        语义("先复制再删源",中途失败留半成品而不是丢数据):
        - 对每个 source:
          1) tier_a/* 重命名后并入 target/tier_a/(同 stem .png/.jpg/.json/.npy 一组)
             超过 tier_a_max // 2 时按 sidecar score(若有)降序截断,保留高分。
          2) tier_c/* 全部并入 target/tier_c/(FIFO 自然淘汰,跑 phash 冗余过滤)
        - 全部 source 处理完后,删 source 目录(shutil.rmtree)。
        - DB 行(person 表)删除由调用方负责;本方法只动文件系统。

        Returns: MergeResult(target_id, merged_sources, written_tier_a, written_tier_c)。
        """
        target_dir = self.persons_dir / target_id
        if not target_dir.is_dir():
            logger.warning("merge_persons: target_id=%s 不存在", target_id)
            return MergeResult(target_id=target_id, merged_sources=[],
                                written_tier_a=0, written_tier_c=0)

        target_tier_a = target_dir / "tier_a"
        target_tier_c = target_dir / "tier_c"
        target_tier_a.mkdir(parents=True, exist_ok=True)
        target_tier_c.mkdir(parents=True, exist_ok=True)

        merged: list[str] = []
        written_a, written_c = 0, 0
        for sid in source_ids:
            if sid == target_id:
                continue  # 自己合自己无意义
            src_dir = self.persons_dir / sid
            if not src_dir.is_dir():
                logger.warning("merge_persons: source %s 不存在,跳过", sid)
                continue

            # 1) tier_a 合并(按 sidecar score 降序选,容量上限 tier_a_max // 2)
            src_tier_a = src_dir / "tier_a"
            if src_tier_a.is_dir():
                # 拿 (jpg_path, score) 对,按 score 降序选 body
                body_scored: list[tuple[Path, float]] = []
                for j in _list_crop_files(src_tier_a, "body"):
                    sc = self._read_sidecar_score(j)
                    body_scored.append((j, sc))
                body_scored.sort(key=lambda x: x[1], reverse=True)

                slots_left = max(0, (self.tier_a_max // 2)
                                  - len(_list_crop_files(target_tier_a, "body")))
                for jpg, _sc in body_scored[:slots_left]:
                    next_idx = _next_index(
                        sorted(_list_crop_files(target_tier_a, "body")), "body_",
                    )
                    # move 是纯重命名(不重编码), 保留源后缀避免造出"扩展名 png/字节 jpeg"的骗子文件
                    new_jpg = target_tier_a / f"body_{next_idx:03d}{jpg.suffix}"
                    self._move_sample_files(jpg, new_jpg, merge_session_extra=sid)
                    written_a += 1

                # face 同样规则(face_* 上限也是 tier_a_max // 2)
                face_files = sorted(_list_crop_files(src_tier_a, "face"))
                face_slots_left = max(0, (self.tier_a_max // 2)
                                       - len(_list_crop_files(target_tier_a, "face")))
                for jpg in face_files[:face_slots_left]:
                    next_idx = _next_index(
                        sorted(_list_crop_files(target_tier_a, "face")), "face_",
                    )
                    new_jpg = target_tier_a / f"face_{next_idx:03d}{jpg.suffix}"
                    self._move_sample_files(jpg, new_jpg, merge_session_extra=sid)

            # 2) tier_c 全合(FIFO 自然淘汰)。tier_c 按相机子目录隔离:递归读 src 全相机
            # 子目录 + 根下 legacy,保留来源相机相对路径迁到 target(tier_c/<cam>/ → 同名
            # cam 子目录;legacy 根下 → target 根下、保持冻结)。
            src_tier_c = src_dir / "tier_c"
            if src_tier_c.is_dir():
                touched_cam_dirs: set[Path] = set()
                for jpg in sorted(_list_crop_files(src_tier_c, "body", recursive=True),
                                   key=lambda p: p.stat().st_mtime):
                    # tier_c 用 ts_ms 命名,迁过来保留原 stem + 相机子目录相对路径
                    rel = jpg.relative_to(src_tier_c)
                    new_jpg = target_tier_c / rel
                    if new_jpg.exists():
                        # 同名(罕见)就跳,避免覆盖
                        continue
                    new_jpg.parent.mkdir(parents=True, exist_ok=True)
                    self._move_sample_files(jpg, new_jpg, merge_session_extra=sid)
                    written_c += 1
                    if new_jpg.parent != target_tier_c:
                        touched_cam_dirs.add(new_jpg.parent)
                # 每个写入的 cam 子目录各自 FIFO 裁剪(给 FIFO 一个机会;legacy 根下不裁、冻结)
                for cam_dir in touched_cam_dirs:
                    self._enforce_tier_c_capacity(cam_dir)

            # 3) 删 source 目录 + 清缓存
            shutil.rmtree(src_dir)
            self._invalidate_person_cache(sid)
            merged.append(sid)

        # 目标 cache 失效让下窗口重建
        self._invalidate_person_cache(target_id)
        logger.info("merge_persons target=%s sources=%s written_a=%d written_c=%d",
                    target_id, merged, written_a, written_c)
        return MergeResult(
            target_id=target_id, merged_sources=merged,
            written_tier_a=written_a, written_tier_c=written_c,
        )

    def split_person(
        self,
        source_id: str,
        new_person_id: str,
        new_name: str,
        *,
        new_role: str | None = None,
        selector_filenames: list[str] | None = None,
        selector_cluster_ids: list[str] | None = None,
        selector_cam_ids: list[str] | None = None,
        selector_session_ids: list[str] | None = None,
    ) -> "SplitResult":
        """从 source_id 中按 selector 筛出一批 tier_a sample 移到 new_person_id。

        selector 之间是 OR 关系(任一匹配即拆出);全部 None 视为空 selector,无操作。

        ⚠️ tier_c 不在拆分范围:tier_c 是系统自动累积,不带 register_session_id /
        cluster_id 等"用户可控"维度,拆分语义不清。仅拆 tier_a。
        """
        src_tier_a = self.persons_dir / source_id / "tier_a"
        if not src_tier_a.is_dir():
            return SplitResult(new_person_id=new_person_id, moved=[])

        # 选中文件 = match selector
        fnames = set(selector_filenames or [])
        cluster_set = set(selector_cluster_ids or [])
        cam_set = set(selector_cam_ids or [])
        session_set = set(selector_session_ids or [])
        if not (fnames or cluster_set or cam_set or session_set):
            return SplitResult(new_person_id=new_person_id, moved=[])

        # 准备 new person 目录
        new_dir = self.persons_dir / new_person_id
        new_tier_a = new_dir / "tier_a"
        new_tier_a.mkdir(parents=True, exist_ok=True)

        moved: list[str] = []
        for jpg in list(_list_crop_files(src_tier_a, "body")):
            if not self._sample_matches_selector(
                jpg, fnames=fnames, clusters=cluster_set,
                cams=cam_set, sessions=session_set,
            ):
                continue
            # 同名直接迁(避免编号冲突时按 _next_index 重命名)
            next_idx = _next_index(
                sorted(_list_crop_files(new_tier_a, "body")), "body_",
            )
            # move 是纯重命名, 保留源后缀(兼容历史 jpg)
            new_jpg = new_tier_a / f"body_{next_idx:03d}{jpg.suffix}"
            self._move_sample_files(jpg, new_jpg, split_session_extra=source_id)
            moved.append(new_jpg.name)
            # 配对 face(同 sidecar register_session_id 的 face_*)
            self._move_paired_face(src_tier_a, new_tier_a, jpg,
                                    selector_session_ids=session_set)

        # 写 meta（name / role）
        _write_person_meta(new_dir, name=new_name, role=new_role)

        # cache 清
        for pid in (source_id, new_person_id):
            self._invalidate_person_cache(pid)

        logger.info("split_person source=%s new=%s moved=%d",
                    source_id, new_person_id, len(moved))
        return SplitResult(new_person_id=new_person_id, moved=moved)

    # ----- merge / split 内部 helpers -----

    @staticmethod
    def _read_sidecar_score(jpg_path: Path) -> float:
        """从 sidecar JSON 读 score 字段;不存在或读失败返 0(排序时垫底)。"""
        sidecar = jpg_path.with_suffix(".json")
        if not sidecar.exists():
            return 0.0
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            return float(meta.get("score", 0.0))
        except Exception:
            return 0.0

    @staticmethod
    def _move_sample_files(
        old_jpg: Path, new_jpg: Path,
        *,
        merge_session_extra: str | None = None,
        split_session_extra: str | None = None,
    ) -> None:
        """把 (jpg, .json, .npy) 三件套一起 move,sidecar 加 audit 字段。"""
        old_sidecar = old_jpg.with_suffix(".json")
        old_npy = old_jpg.with_suffix(".npy")
        new_sidecar = new_jpg.with_suffix(".json")
        new_npy = new_jpg.with_suffix(".npy")

        old_jpg.rename(new_jpg)
        if old_sidecar.exists():
            # sidecar 可能要加 merge/split audit 字段
            try:
                meta = json.loads(old_sidecar.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            if merge_session_extra:
                meta.setdefault("merged_from", merge_session_extra)
            if split_session_extra:
                meta.setdefault("split_from", split_session_extra)
            new_sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            old_sidecar.unlink(missing_ok=True)
        if old_npy.exists():
            old_npy.rename(new_npy)

    def _enforce_tier_c_capacity(self, tier_c_dir: Path) -> None:
        """tier_c 超过 self.tier_c_max 时按 mtime 升序弹最旧。"""
        all_c = sorted(_list_crop_files(tier_c_dir, "body"),
                        key=lambda p: p.stat().st_mtime)
        while len(all_c) > self.tier_c_max:
            oldest = all_c.pop(0)
            oldest.unlink(missing_ok=True)
            oldest.with_suffix(".json").unlink(missing_ok=True)
            oldest.with_suffix(".npy").unlink(missing_ok=True)

    @staticmethod
    def _sample_matches_selector(
        jpg_path: Path,
        *,
        fnames: set[str],
        clusters: set[str],
        cams: set[str],
        sessions: set[str],
    ) -> bool:
        """sample 是否匹配 selector(任一维度命中即匹配)。"""
        if jpg_path.name in fnames:
            return True
        if not (clusters or cams or sessions):
            return False
        sidecar = jpg_path.with_suffix(".json")
        if not sidecar.exists():
            return False
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            return False
        if clusters and meta.get("cluster_id") in clusters:
            return True
        if cams and meta.get("camera_id") in cams:
            return True
        if sessions and meta.get("register_session_id") in sessions:
            return True
        return False

    @staticmethod
    def _move_paired_face(
        src_tier_a: Path, new_tier_a: Path, body_jpg: Path,
        *,
        selector_session_ids: set[str],
    ) -> None:
        """body 拆出时配对的 face_*(同 register_session_id)也跟着拆。

        sidecar 没 register_session_id 时不拆 face(保守:避免错误带走 face)。
        """
        if not selector_session_ids:
            return
        body_sidecar = body_jpg.with_suffix(".json")
        body_session: str | None = None
        if body_sidecar.exists():
            try:
                body_session = json.loads(body_sidecar.read_text(encoding="utf-8"))\
                    .get("register_session_id")
            except Exception:
                body_session = None
        if body_session is None or body_session not in selector_session_ids:
            return
        # 找同 session 的 face,移过去
        for face_jpg in list(_list_crop_files(src_tier_a, "face")):
            face_sidecar = face_jpg.with_suffix(".json")
            if not face_sidecar.exists():
                continue
            try:
                meta = json.loads(face_sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("register_session_id") != body_session:
                continue
            next_idx = _next_index(
                sorted(_list_crop_files(new_tier_a, "face")), "face_",
            )
            # move 是纯重命名, 保留源后缀(兼容历史 jpg)
            new_face = new_tier_a / f"face_{next_idx:03d}{face_jpg.suffix}"
            IdentityLibrary._move_sample_files(face_jpg, new_face)
            break  # 一对一配对,移一个就够

    # -------------------------------------------------------------------------
    # 主动注册系列：batch 入库 / rollback（v1.2 新增）
    # -------------------------------------------------------------------------

    def add_tier_a_samples_batch(
        self,
        person_id: str,
        bodies: list["BodySample"],
        register_session_id: str,
        name: str | None = None,
        role: str | None = None,
        reid_extractor: "Any | None" = None,
    ) -> list[str]:
        """批量写入 Tier A 样本（注册流程 commit 使用）。

        与 ``add_tier_a_sample`` 的区别：
        - 一次写多张 body（+ 可选 face），每张 sidecar 自动带 ``register_session_id``
          + 调用方在 ``BodySample.metadata`` 里补的额外字段（cluster_id / score / phash 等）
        - 容量满时按"先到先得"截断，返回已写入文件名列表（不返回 False）
        - 共用一份 name / role 写入

        Args:
            reid_extractor: 兜底现场抽取 ReID emb 用。``sample.reid_embedding`` 已经
                填好时直接落盘,不调用 extractor;反之(陌生人池 L1/L2 emb 都 None 的
                极端 race)用本对象 ``extract_feature(body_crop)`` 现场补一张,确保
                tier_a body_NNN 旁边都有 .npy。None 时关闭兜底,保留旧行为(无 emb 跳过)。

        Returns:
            写入的文件名列表（仅 body；face 不计入；空列表 = 写入失败）。
        """
        if not bodies:
            return []

        person_dir = self.persons_dir / person_id
        tier_a_dir = person_dir / "tier_a"
        tier_a_dir.mkdir(parents=True, exist_ok=True)

        written: list[str] = []
        for sample in bodies:
            if sample.body_crop is None or sample.body_crop.size == 0:
                continue
            existing_body = sorted(_list_crop_files(tier_a_dir, "body"))
            if len(existing_body) >= self.tier_a_max // 2:
                logger.info(
                    "batch 写入到达 tier_a body 容量上限 person_id=%s 已写=%d",
                    person_id, len(written),
                )
                break

            ts = sample.captured_at if sample.captured_at is not None else time.time()
            next_idx = _next_index(existing_body, "body_")
            body_path = tier_a_dir / f"body_{next_idx:03d}.png"
            cv2.imwrite(str(body_path), sample.body_crop)

            extra = dict(sample.metadata or {})
            extra["register_session_id"] = register_session_id
            self._write_sidecar(body_path, tier="a", kind="body",
                                source=sample.source, captured_at=ts,
                                extra_meta=extra)
            emb_to_write = sample.reid_embedding
            if emb_to_write is None and reid_extractor is not None:
                # 兜底:陌生人池 L1/L2 都没拉到 emb 时,登记落盘这一刻现场抽一次
                # (这层是 IdentityLibrary 而非 TierUPool,允许调 extract_feature;
                #  零额外推理硬约束只针对 TierU pool 的代码,见 tier_u.py docstring)。
                try:
                    emb_to_write = reid_extractor.extract_feature(sample.body_crop)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "add_tier_a_samples_batch: 现场抽 ReID emb 失败 person_id=%s",
                        person_id, exc_info=True,
                    )
                    emb_to_write = None
            if emb_to_write is not None:
                self._write_embedding(body_path, emb_to_write)

            # 配对 face（如果有）
            if sample.face_crop is not None and sample.face_crop.size > 0:
                existing_face = sorted(_list_crop_files(tier_a_dir, "face"))
                if len(existing_face) < self.tier_a_max // 2:
                    face_idx = _next_index(existing_face, "face_")
                    face_path = tier_a_dir / f"face_{face_idx:03d}.png"
                    cv2.imwrite(str(face_path), sample.face_crop)
                    self._write_sidecar(face_path, tier="a", kind="face",
                                        source=sample.source, captured_at=ts,
                                        extra_meta=extra)

            written.append(body_path.name)

        if name is not None or role is not None:
            _write_person_meta(
                person_dir,
                name=name if name is not None else _META_UNSET,
                role=role if role is not None else _META_UNSET,
            )

        logger.info("batch 写入 tier_a person_id=%s session=%s count=%d",
                    person_id, register_session_id, len(written))
        return written

    def delete_by_register_session(
        self, person_id: str, register_session_id: str,
    ) -> int:
        """删除该 person 下、由指定注册批次写入的所有 tier_a 文件（+ sidecar）。

        用于 ``register rollback`` 端点。扫描 tier_a/ 下所有 sidecar，匹配
        ``register_session_id`` 字段，删除对应 jpg + json 对。

        Returns:
            删除的文件对数（body / face 各算一对；返回的是 sidecar 数）。
        """
        person_dir = self.persons_dir / person_id
        tier_a_dir = person_dir / "tier_a"
        if not tier_a_dir.is_dir():
            return 0

        deleted = 0
        for sidecar in list(tier_a_dir.glob("*.json")):
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("register_session_id") != register_session_id:
                continue
            # 反查图像: 落盘可能是 .png(新) 或 .jpg/.jpeg(历史), 逐扩展名清, 避免孤儿图
            for ext in CROP_IMG_EXTS:
                sidecar.with_suffix(ext).unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            # 同名 .npy(若存在)一并清,避免孤儿 emb
            sidecar.with_suffix(".npy").unlink(missing_ok=True)
            deleted += 1

        if deleted > 0:
            # 该 person 的样本数变了，清缓存让下窗口重建
            self._invalidate_person_cache(person_id)
            logger.info("rollback 删除 person_id=%s session=%s files=%d",
                        person_id, register_session_id, deleted)
        return deleted

    # -------------------------------------------------------------------------
    # 内部工具
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # ReID emb 读写(v1.2 主动注册;.npy 与 jpg 同名同目录)
    # -------------------------------------------------------------------------

    def backfill_reid_embeddings(
        self,
        reid_extractor: Any,
        *,
        force: bool = False,
        person_ids: list[str] | None = None,
        tiers: tuple[str, ...] = ("a", "c"),
    ) -> dict:
        """老库适配:扫缺 .npy 的 body_*.jpg,现场抽 emb 落盘。

        历史身份库(PR 3 之前写入的样本)没有同名 .npy。后续 M10 合并/拆分按 emb
        验证 / 未识别 vs 已注册快速比对等场景需要 emb 齐全;本方法做一次性回填。

        Args:
            reid_extractor: 任何提供 ``extract_feature(crop) -> ndarray[128]`` 的实例
                (典型:``HumanReID``)。库层不持有 ONNX 模型实例,调用方决定何时构造。
            force: True 时即便 .npy 已存在也重新生成(用于切换 ReID 模型后批量重抽)。
            person_ids: None 扫全库;指定列表只扫这几个 person。
            tiers: 扫哪几个 tier(``("a", "c")`` / ``("a",)`` / ``("c",)``)。

        Returns:
            ``{"scanned": N, "generated": M, "skipped": S, "failed": F}``——
            scanned=访问过的 jpg 数 / generated=新写 .npy 数 / skipped=已有 .npy
            且未 force 跳过 / failed=读图或抽 emb 失败。
        """
        scanned = generated = skipped = failed = 0
        if not self.persons_dir.is_dir():
            return {"scanned": 0, "generated": 0, "skipped": 0, "failed": 0}

        if person_ids is None:
            pids = [p.name for p in self.persons_dir.iterdir()
                    if p.is_dir() and not p.name.startswith(".")]
        else:
            pids = list(person_ids)

        for pid in pids:
            person_dir = self.persons_dir / pid
            if not person_dir.is_dir():
                continue
            for tier in tiers:
                tier_dir = person_dir / f"tier_{tier}"
                if not tier_dir.is_dir():
                    continue
                # tier_c 按相机子目录隔离 → 递归扫(含根下 legacy);tier_a 仍扁平。
                for jpg in _list_crop_files(tier_dir, "body", recursive=(tier == "c")):
                    scanned += 1
                    npy_path = jpg.with_suffix(".npy")
                    if npy_path.exists() and not force:
                        skipped += 1
                        continue
                    img = cv2.imread(str(jpg))
                    if img is None:
                        logger.warning("backfill: 读图失败 %s", jpg)
                        failed += 1
                        continue
                    try:
                        emb = reid_extractor.extract_feature(img)
                    except Exception:  # noqa: BLE001
                        logger.warning("backfill: extract_feature 失败 %s", jpg,
                                        exc_info=True)
                        failed += 1
                        continue
                    self._write_embedding(jpg, emb)
                    generated += 1

        logger.info(
            "backfill_reid_embeddings done: scanned=%d generated=%d skipped=%d failed=%d",
            scanned, generated, skipped, failed,
        )
        return {"scanned": scanned, "generated": generated,
                "skipped": skipped, "failed": failed}

    @staticmethod
    def _write_embedding(image_path: Path, emb: NDArray[np.float32]) -> None:
        """落盘 ReID embedding 到 ``image_path.with_suffix(".npy")``。

        ⚠️ 写入前不重新归一化:调用方传进来的 emb 应当**已 L2-normalized**
        (DeepSORT 关联阶段产出的就是)。万一传入未归一化的,写盘时静默规整
        防止下游读出来用前还得自己 normalize。
        """
        npy_path = image_path.with_suffix(".npy")
        arr = np.asarray(emb, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(arr))
        if norm > 0 and abs(norm - 1.0) > 1e-3:
            arr = arr / norm
        np.save(str(npy_path), arr)

    def get_person_mean_emb(
        self, person_id: str,
    ) -> NDArray[np.float32] | None:
        """计算 person 的 tier_a body 样本 ReID emb 均值, L2-normalized; 无可用 emb 返 None。

        用途: TierU pool fetch 时跟池子里 cluster centroid 比对, 去重已注册的人
        (case c: 同一人 unknown 阶段累积的 cluster 残留)。

        实现: 扫 ``persons/{id}/tier_a/body_*.npy``, 全部加载 → mean → L2 归一化。
        没 .npy 或加载全失败 → None。mean 退化为零向量 (极端: 方向完全相反的 emb
        相互抵消, 如注册删后重建残留) 也返 None —— 零向量对 ``_cosine`` 无意义
        (0/0 → nan 污染 dedup log), 跟"无可用 emb"等价。
        """
        tier_a_dir = self.persons_dir / person_id / "tier_a"
        if not tier_a_dir.is_dir():
            return None
        embs: list[NDArray[np.float32]] = []
        for npy_path in tier_a_dir.glob("body_*.npy"):
            try:
                arr = np.load(str(npy_path)).astype(np.float32)
                embs.append(arr)
            except Exception:
                logger.warning("读 ReID emb 失败 %s", npy_path, exc_info=True)
                continue
        if not embs:
            return None
        mean = np.mean(np.stack(embs, axis=0), axis=0)
        norm = float(np.linalg.norm(mean))
        if norm <= 0:
            # 零向量对 _cosine 比对无意义 (0/0 → nan), 跟"无可用 emb"等价 → None。
            # 调用方 (pool_fetch _build_emb_lookups) 已有 None 过滤直接跳过该 person。
            return None
        return (mean / norm).astype(np.float32)

    def get_person_tier_c_embs(
        self, person_id: str,
    ) -> list[NDArray[np.float32]]:
        """加载 person 的 tier_c 所有 body 样本 ReID emb 列表 (不 mean, 逐张返回)。

        用途: TierU pool fetch 时跟池子里 cluster centroid 做第三层去重 (case b/c
        的"近期外观变化"加固)。**不 mean** 是因为 tier_c 内部样本差异较大 (累积
        了不同瞬间外观), mean 后反而模糊化, 逐张比对更准。

        Returns:
            list[ndarray] — 可能为空 (该 person 无 tier_c 样本 / 全部加载失败)。
        """
        tier_c_dir = self.persons_dir / person_id / "tier_c"
        if not tier_c_dir.is_dir():
            return []
        out: list[NDArray[np.float32]] = []
        # 跨摄去重:递归读全相机子目录 + 根下 legacy 的 emb(此处刻意不按 cam 过滤)。
        for npy_path in tier_c_dir.rglob("body_*.npy"):
            try:
                arr = np.load(str(npy_path)).astype(np.float32)
                out.append(arr)
            except Exception:
                logger.warning("读 tier_c ReID emb 失败 %s", npy_path, exc_info=True)
                continue
        return out

    @staticmethod
    def _npy_capture_ts(npy_path: Path) -> float:
        """估计样本采集时间(秒, 墙钟 epoch), 给身份漂移自检的"近期"过滤用。

        tier_c 文件名是 ``body_<ts_ms>.npy``(``ts_ms = int(captured_at*1000)``), 直接解析
        最精确; tier_a 文件名是 ``body_<NNN>.npy``(序号, 非时间)——解析出的小整数不是
        epoch, 退回文件 mtime。判据: 解析值 > 1e12(约 2001 年的毫秒 epoch)才认作时间戳。
        """
        parts = npy_path.stem.split("_")  # body_<x>
        if len(parts) >= 2 and parts[-1].isdigit():
            v = int(parts[-1])
            if v > 1_000_000_000_000:  # 足够大才是 ms epoch, 排除 tier_a 序号
                return v / 1000.0
        try:
            return npy_path.stat().st_mtime
        except OSError:
            return 0.0

    @staticmethod
    def _mean_l2_from_npys(npy_paths: list[Path]) -> NDArray[np.float32] | None:
        """加载一组 .npy → mean → L2 归一化; 空 / 全失败 / 零向量返 None。"""
        embs: list[NDArray[np.float32]] = []
        for p in npy_paths:
            try:
                embs.append(np.load(str(p)).astype(np.float32))
            except Exception:
                logger.warning("读 ReID emb 失败 %s", p, exc_info=True)
                continue
        if not embs:
            return None
        mean = np.mean(np.stack(embs, axis=0), axis=0)
        norm = float(np.linalg.norm(mean))
        if norm <= 0:
            return None
        return (mean / norm).astype(np.float32)

    def get_person_recent_tier_c_centroid(
        self, person_id: str, cam_id: str, within_sec: float, now_ts: float,
    ) -> tuple[NDArray[np.float32] | None, int, str]:
        """取 person 在 ``cam_id`` 下、近 ``within_sec`` 秒内 TierC body 样本质心(mean+L2)。

        身份漂移自检的**参考向量**: 与 track 当前外观质心比 cos。优先近期同摄 tier_c——
        body ReID 输入是原始像素、对衣着/光照敏感, 只有时间临近样本外观可比, 跨天旧样本
        会误判本人偏离; 无近期 tier_c 则退近期 tier_a; 再无返 ``(None, 0, "none")``。

        与 track ``features`` 同源同空间(单一 ``human_body_reid_v2.onnx``, 128-dim、
        L2-normalized), 直接 ``np.dot`` 即 cos。

        Returns:
            ``(centroid|None, n_samples, ref_kind∈{"tierc","tiera","none"})``。

        **按在窗样本集 memo 化**(``_drift_ref_cache``): 每窗每个 confirmed track 都调一次,
        稳态(无新写入、无样本滑出窗)直接返回缓存、零 np.load。指纹取 **now_ts 过滤后的在窗集**
        (名,mtime)——不是整目录: 旧样本随 now_ts 推进滑出 recency 窗即令指纹变、自然失效,
        否则会返回含过期外观的陈旧质心、架空"近期可比"。glob+解析 ts(文件名)便宜、每窗照做。
        """
        cutoff = now_ts - within_sec
        person_dir = self.persons_dir / person_id

        def _in_window(d: Path) -> list[Path]:
            if not d.is_dir():
                return []
            return sorted(
                p for p in d.glob("body_*.npy") if self._npy_capture_ts(p) >= cutoff
            )

        # 在窗集(便宜: glob + 文件名解析 ts, 无 np.load)。tier_a 也先算好供指纹/兜底,
        # 但仅在 tier_c 质心为 None 时才 np.load 它(与原短路同义)。
        tier_c_dir = person_dir / "tier_c" / _sanitize_cam_did(cam_id)
        recent_c = _in_window(tier_c_dir)
        recent_a = _in_window(person_dir / "tier_a")

        fp = (self._fingerprint(recent_c), self._fingerprint(recent_a))
        key = (person_id, cam_id)
        with self._cache_lock:
            cached = self._drift_ref_cache.get(key)
            if cached is not None and cached[0] == fp:
                return cached[1]

        # miss: 仅此处付 np.load(mean+L2)。tier_c 优先, 空/全失败退近期 tier_a(同原顺序)。
        result: tuple[NDArray[np.float32] | None, int, str]
        c = self._mean_l2_from_npys(recent_c)
        if c is not None:
            result = (c, len(recent_c), "tierc")
        else:
            a = self._mean_l2_from_npys(recent_a)
            result = (a, len(recent_a), "tiera") if a is not None else (None, 0, "none")

        with self._cache_lock:
            self._drift_ref_cache[key] = (fp, result)
        return result

    def get_sample_embedding(
        self, person_id: str, tier: str, filename: str,
    ) -> NDArray[np.float32] | None:
        """读取 sample 对应的 ReID emb;无 .npy / 读失败返 None。

        Args:
            person_id: UUID4。
            tier: ``"a"`` 或 ``"c"``。
            filename: 如 ``"body_001.jpg"`` 或 ``"body_001.npy"`` 都可。
        """
        if tier not in ("a", "c"):
            return None
        stem = filename.rsplit(".", 1)[0]
        npy_path = self.persons_dir / person_id / f"tier_{tier}" / f"{stem}.npy"
        if not npy_path.exists():
            return None
        try:
            arr = np.load(str(npy_path))
            return arr.astype(np.float32)
        except Exception:
            logger.warning("读 ReID emb 失败 %s", npy_path, exc_info=True)
            return None

    @staticmethod
    def _write_sidecar(
        image_path: Path,
        *,
        tier: str,
        kind: str,
        source: str,
        captured_at: float,
        extra_meta: dict | None = None,
    ) -> None:
        """为单张 crop 写同名 .json sidecar（per-file 元信息）。

        文件位置：``image_path.with_suffix(".json")``——例如 ``body_001.jpg`` 旁是 ``body_001.json``。
        """
        # captured_at 单位不一(tier_c 走 engine now_ts=毫秒, tier_a 走 time.time()=秒): 算可读
        # iso 时把毫秒(>1e11, 约公元 5138 年才到该秒数)折成秒, 否则 gmtime 把毫秒当秒、年份炸飞。
        # 只修可读 iso; captured_at 原值不动(文件名等下游按原值算)。
        _cap_s = captured_at / 1000.0 if captured_at > 1e11 else captured_at
        meta = {
            "tier": tier,
            "kind": kind,                 # "body" | "face"
            "source": source,
            "captured_at": captured_at,
            "captured_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_cap_s)),
        }
        if extra_meta:
            # 不允许 extra 字段覆盖系统字段
            for k, v in extra_meta.items():
                if k not in meta:
                    meta[k] = v
        sidecar = image_path.with_suffix(".json")
        sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
