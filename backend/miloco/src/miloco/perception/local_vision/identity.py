"""本地身份识别 —— 用 ReID 指纹比对身份库,**不经过大模型**。

## 为什么不让本地大模型认人

实测(7 个双人场景、真值人工核对):把云端那套原样搬过来(成员参考图 + 整段
视频 + bbox 指人 + 要 JSON),Mage-VL 逐人正确 8/28 —— 二选一瞎猜是 50%,它
比瞎猜还低;而且把名单里两人的先后调换会改掉 4/7 的答案。它**看得见**画面
(只问性别与衣着时 5/5 全对),但「两张图是不是同一个人」这种细粒度比对不是
一个 4B 视频理解模型的能力。同一批场景纯 ReID 是 14/14。

云端把认人交给大模型,是因为云端手边只有大模型这一个工具:视频反正要传上去,
顺带让它一并作答还省一次调用。那是成本取舍,不是技术上的必然 —— 本地两样都有。

## 产出怎么用

产出是一份「名字 + 位置」的名册,以文本塞进给模型的提问里,与云端 prompt 里
``已识别人物:阳阳[bbox=(788, 627, 979, 981)]`` 完全同构。模型只需要把**给定的
名字**贴到**给定的位置**上 —— 这比让它认人容易得多(同样 7 个场景实测 7/7)。

## 两条必须守住的边界

- **fail-open**:这一层的任何异常都只让名册为空,绝不能让整窗感知失败。认不出
  人最多是描述里写「一名男子」,而抛异常会让这台相机整窗没有输出。
- **同名只出现一次**:两个框都判成小亮时只保留分高的那个。名册里说「小亮同时
  在画面两处」会让模型写出自相矛盾的描述,比不给名字更糟。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)

#: 判成员的相似度下限。实测(同日库、7 场景):真人 0.77~0.95,而**电视屏幕里的
#: 人**(检测器会以 0.94 置信度把它当真人)只有 0.44~0.67 —— 0.70 这一刀把 8/8
#: 电视误检全挡掉且 0 误拒。往下调会先放进来的就是电视里的人,不是陌生人。
DEFAULT_THRESHOLD = 0.70

#: 一个窗口里抽几帧跑检测。检测是这一层唯一的重活,而窗口内人的位置变化不大,
#: 抽 3 帧已足够;取「检出人数最多」的那一帧作为名册基准,保证同一份名册里的
#: 各个 bbox 来自**同一时刻**(混用不同帧的框会让位置互相矛盾)。
DEFAULT_SAMPLE_FRAMES = 3

#: 太小的框喂进 ReID 只会得到噪声指纹(模型输入是 192x96)。这个下限不是画质
#: 偏好,是防止「远处一个 20 像素高的影子」拿到 0.8 的相似度而被安上名字。
_MIN_CROP_HEIGHT = 80

#: "有人但都没认出来"的日志节流间隔(秒)。
_MISS_LOG_INTERVAL_SEC = 300.0

#: bbox 归一化区间。与云端 prompt 的约定一致([0,1000],左上 0,0)——两条通路
#: 说同一种坐标语言,提示词模板才能共用。
_NORM = 1000


@dataclass(frozen=True)
class PersonHit:
    """名册里的一项:某个已登记成员本窗出现在哪。"""

    person_id: str
    name: str
    role: str | None
    #: (x1, y1, x2, y2),已归一化到 [0, _NORM]
    bbox: tuple[int, int, int, int]
    #: 与该成员库内样本的最高余弦相似度,用于排障与阈值调整
    score: float


class LocalIdentityResolver:
    """把一窗画面解析成「谁在画面的哪个位置」。

    重活(检测器、ReID)都是懒加载:``perception.engine_backend`` 不是 local 时
    这个类根本不会被构造,而即便构造了,身份库为空也不该白白吃掉两个 ONNX 模型的
    内存 —— 没有登记成员时比对本身就是不可能的。
    """

    def __init__(
        self,
        library_root: str | Path,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        sample_frames: int = DEFAULT_SAMPLE_FRAMES,
        reid_model_path: str | None = None,
        detector_model_path: str | None = None,
        use_gpu: bool = False,
    ) -> None:
        self.root = Path(library_root)
        self.threshold = threshold
        self.sample_frames = max(1, sample_frames)
        self._reid_model_path = reid_model_path
        self._detector_model_path = detector_model_path
        self._use_gpu = use_gpu

        self._detector: Any = None
        self._reid: Any = None
        #: person_id -> (name, role, 特征矩阵 [N,128] L2 归一化)
        self._gallery: dict[str, tuple[str, str | None, NDArray[np.float32]]] = {}
        #: 身份库指纹快照,用于判断「用户重新登记过没有」
        self._gallery_fp: tuple = ()
        self._gallery_loaded_at: float = 0.0
        #: 加载失败的原因。要能从外面看到 —— 否则「库是空的」和「读库炸了」在
        #: 行为上同形(都是名册为空),而后者是故障。
        self.load_error: str | None = None
        #: 本窗见过的最高相似度(不论是否过阈值)。**库过期时的表现是「什么都不说」**
        #: —— 与「画面里没人」「认人没开」在日志上完全同形,而三者的处置南辕北辙。
        self.last_best_score: float | None = None
        #: 上次就"有人但一个都没认出来"打过日志的时刻。必须节流:这个情形一旦发生
        #: 往往是**持续**的(库过期了),每窗一条会把日志刷没。
        self._last_miss_log: float = 0.0

    # ── 身份库 ────────────────────────────────────────────────────────────

    @property
    def gallery_size(self) -> int:
        return len(self._gallery)

    def refresh_gallery(self) -> bool:
        """按需重载身份库。返回是否发生了重载。

        用户在面板上重新登记之后,不重启也要能生效 —— 但每窗都扫一遍目录是浪费,
        所以用 ``list_persons`` 给出的 tier_a 指纹(含文件 mtime)做变更检测。
        """
        try:
            from miloco.perception.engine.identity.library import IdentityLibrary

            persons = IdentityLibrary(self.root).list_persons()
        except Exception as e:  # noqa: BLE001 —— 读不到库只该让名册为空
            self.load_error = f"{type(e).__name__}: {e}"
            logger.warning("[local-vision] 身份库读取失败,本窗不做认人: %s", e)
            self._gallery, self._gallery_fp = {}, ()
            return False

        fp = tuple(sorted((p.person_id, p.tier_a_fingerprint) for p in persons))
        if fp == self._gallery_fp and self._gallery:
            return False

        gallery: dict[str, tuple[str, str | None, NDArray[np.float32]]] = {}
        for p in persons:
            if not p.name:
                # 没名字的成员进了名册也只能显示成空字符串,不如不进。
                continue
            embs = _load_embeddings(self.root / "persons" / p.person_id / "tier_a")
            if embs is None:
                continue
            gallery[p.person_id] = (p.name, p.role, embs)

        self._gallery, self._gallery_fp = gallery, fp
        self._gallery_loaded_at = time.time()
        self.load_error = None
        logger.info(
            "[local-vision] 身份库已加载:%d 名成员(%s)",
            len(gallery), ", ".join(v[0] for v in gallery.values()) or "无",
        )
        return True

    # ── 主流程 ────────────────────────────────────────────────────────────

    def resolve(self, frames: list) -> list[PersonHit]:
        """从一窗的帧里解析出名册。任何异常都收敛成空名册。"""
        self.last_best_score = None
        try:
            return self._resolve(frames)
        except Exception as e:  # noqa: BLE001 —— 见模块文档:fail-open
            logger.warning("[local-vision] 认人失败,本窗名册留空: %s", e, exc_info=True)
            return []

    def _resolve(self, frames: list) -> list[PersonHit]:
        self.refresh_gallery()
        if not self._gallery:
            # 一个成员都没登记 —— 比对不可能,也不必为此加载两个模型。
            return []
        if not frames:
            return []

        best_frame, dets = self._detect_best_frame(frames)
        if best_frame is None or not dets:
            return []

        h, w = best_frame.shape[:2]
        if h <= 0 or w <= 0:
            return []

        reid = self._get_reid()
        # person_id -> 该成员本窗最像的那个框
        picked: dict[str, PersonHit] = {}
        for d in dets:
            crop = _crop(best_frame, d)
            if crop is None:
                continue
            feat = _embed(reid, crop)
            if feat is None:
                continue
            pid, score = self._match(feat)
            if self.last_best_score is None or score > self.last_best_score:
                self.last_best_score = score
            if pid is None or score < self.threshold:
                # 不够像就**不进名册**。刻意不产出「陌生人」条目:本通路的名册只
                # 用来把真名贴到位置上,而凭空多一个"陌生人"会让模型在描述里写出
                # 画面上并不存在的人(云端 prompt 对这条有专门的约束,本地不引入
                # 这个风险面)。
                continue
            name, role, _ = self._gallery[pid]
            hit = PersonHit(
                person_id=pid, name=name, role=role,
                bbox=_norm_bbox(d, w, h), score=score,
            )
            # 同名只留分最高的一个 —— 见模块文档。
            if pid not in picked or score > picked[pid].score:
                picked[pid] = hit

        if not picked and self.last_best_score is not None:
            # 画面里明明有人,却一个都没认出来 —— 最常见的成因是**身份库过期**
            # (参考图是几周前另一身衣着拍的,人体 ReID 主要吃衣着,相似度整体
            # 塌到阈值以下)。不把这个数说出来的话,它与"屋里没人"在日志上完全
            # 同形,而处置南辕北辙:一个该去重新登记,一个什么都不用做。
            now = time.time()
            if now - self._last_miss_log > _MISS_LOG_INTERVAL_SEC:
                self._last_miss_log = now
                logger.info(
                    "[local-vision] 画面里有 %d 个人但都没认出来,最高相似度 %.3f "
                    "(阈值 %.2f)。持续如此多半是身份库过期,建议用当前画面重新登记",
                    len(dets), self.last_best_score, self.threshold,
                )

        # 按 x 从左到右排,名册读起来与画面一致。
        return sorted(picked.values(), key=lambda p: p.bbox[0])

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _detect_best_frame(self, frames: list):
        """在抽样帧里挑「检出人数最多」的一帧,连同它的人体框一起返回。

        取单帧而不是合并多帧的结果:名册里各个 bbox 必须来自同一时刻,否则「小亮
        在左、阳阳在右」可能描述的是两个不同瞬间,模型按坐标对号入座就会对错。
        """
        from miloco.perception.engine.identity.tracker.detector import Detection

        det = self._get_detector()
        picks = _sample(frames, self.sample_frames)
        best, best_dets = None, []
        for f in picks:
            img = getattr(f, "data", None)
            if img is None or getattr(img, "size", 0) == 0:
                continue
            humans = [
                d for d in det.detect(img)
                if d.class_id == Detection.CLASS_HUMAN and d.h >= _MIN_CROP_HEIGHT
            ]
            if len(humans) > len(best_dets):
                best, best_dets = img, humans
        return best, best_dets

    def _match(self, feat: NDArray[np.float32]) -> tuple[str | None, float]:
        """与库内每个成员的**最相似样本**比。

        取 max 而不是 mean:同一个人在库里的 5 张样本可能横跨不同衣着/角度,平均
        会把「这一张恰好高度吻合」稀释掉,而识别要的正是那一张。
        """
        best_pid, best = None, -1.0
        for pid, (_, _, embs) in self._gallery.items():
            s = float(np.max(embs @ feat))
            if s > best:
                best_pid, best = pid, s
        return best_pid, best

    def _get_detector(self):
        if self._detector is None:
            from miloco.perception.engine.identity.tracker.detector import Detector

            kw = {"model_path": self._detector_model_path} if self._detector_model_path else {}
            self._detector = Detector(**kw)
        return self._detector

    def _get_reid(self):
        if self._reid is None:
            from miloco.perception.engine.identity.tracker.human_reid import HumanReID
            from miloco.perception.engine.identity.tracking_service import (
                RealTrackingService,
            )

            # **必须**走这个解析器,不能用 HumanReID 的类默认值 —— 那个默认是
            # ``models/human_body_reid_v2.onnx``,相对进程 cwd。supervisor 拉起时
            # cwd 不在包目录下,于是认人会在**第一次真的遇到人**时才报找不到模型,
            # 而不是启动时。检测器那边为同一个理由早就锚定了 __file__。
            path = self._reid_model_path or RealTrackingService._resolve_model_path(
                None, "human_body_reid_v2.onnx"
            )
            self._reid = HumanReID(model_path=path, use_gpu=self._use_gpu)
        return self._reid


# ── 纯函数 ────────────────────────────────────────────────────────────────


def _load_embeddings(tier_a: Path) -> NDArray[np.float32] | None:
    """读某成员登记时存下的 ReID 特征(``body_001.npy`` 之类)。

    这些 ``.npy`` 是登记流程一直在写、但至今没人读的东西 —— ``library.py`` 的
    注释原文写着「后续做『未识别 track 跟已注册成员快速比对』等场景能直接用」。
    这里就是那个「后续」。
    """
    if not tier_a.is_dir():
        return None
    vecs = []
    for p in sorted(tier_a.glob("body_*.npy")):
        try:
            v = np.load(p).astype(np.float32).reshape(-1)
        except Exception as e:  # noqa: BLE001 —— 单个坏文件不该让该成员整个失效
            logger.warning("[local-vision] 特征文件读取失败 %s: %s", p.name, e)
            continue
        n = float(np.linalg.norm(v))
        if v.size == 0 or not np.isfinite(n) or n <= 0:
            # 落盘时本应已 L2 归一化;这里仍然重新归一,因为比对用的是点积 ——
            # 一个没归一化的向量会凭模长而不是方向拿到高分。
            continue
        vecs.append(v / n)
    if not vecs:
        return None
    dims = {v.shape[0] for v in vecs}
    if len(dims) != 1:
        # 换过 ReID 模型而库里混着两代特征。混着算点积得到的是无意义的数,
        # 宁可整个成员不参与比对。
        logger.warning("[local-vision] %s 的特征维度不一致 %s,跳过该成员", tier_a, dims)
        return None
    return np.stack(vecs)


def _sample(frames: list, n: int) -> list:
    """在窗口内均匀抽 n 帧(含首尾)。"""
    if len(frames) <= n:
        return list(frames)
    idx = np.linspace(0, len(frames) - 1, n).round().astype(int)
    return [frames[i] for i in dict.fromkeys(idx.tolist())]


def _crop(img: NDArray[np.uint8], d) -> NDArray[np.uint8] | None:
    """按检测框裁出人体,四周留一点余量。

    余量是有必要的:检测框常常贴着人体边缘,而 ReID 的训练样本一般带一圈背景。
    """
    h, w = img.shape[:2]
    pad = int(0.05 * max(d.w, d.h))
    y0, y1 = max(0, d.y - pad), min(h, d.y + d.h + pad)
    x0, x1 = max(0, d.x - pad), min(w, d.x + d.w + pad)
    if y1 - y0 < _MIN_CROP_HEIGHT or x1 <= x0:
        return None
    crop = img[y0:y1, x0:x1]
    return crop if crop.size else None


def _embed(reid, crop: NDArray[np.uint8]) -> NDArray[np.float32] | None:
    """抽一张 crop 的 ReID 特征并 L2 归一化。抽不出来返回 None。"""
    try:
        v = np.asarray(reid.extract_feature(crop), dtype=np.float32).reshape(-1)
    except Exception as e:  # noqa: BLE001 —— 单张失败不该毁掉整份名册
        logger.warning("[local-vision] ReID 特征抽取失败: %s", e)
        return None
    n = float(np.linalg.norm(v))
    if v.size == 0 or not np.isfinite(n) or n <= 0:
        return None
    return v / n


def _norm_bbox(d, w: int, h: int) -> tuple[int, int, int, int]:
    """像素框 → [0, _NORM] 归一化框,并夹到区间内。"""
    def clamp(v: float) -> int:
        return max(0, min(_NORM, int(round(v))))

    return (
        clamp(d.x * _NORM / w), clamp(d.y * _NORM / h),
        clamp((d.x + d.w) * _NORM / w), clamp((d.y + d.h) * _NORM / h),
    )


def render_roster(hits: list[PersonHit]) -> list[dict]:
    """名册 → 边车契约里的 roster 载荷。"""
    return [{"name": h.name, "bbox": list(h.bbox)} for h in hits]
