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
import threading
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

#: 超过多少天的身份库开始告警。人体 ReID 主要吃衣着,而衣着按季节和天气换 ——
#: 14 天是"大概率已经换过一身"的量级。这个数**没有实测标定**,只有两个观测点
#: (36 天的库 0/14、当天的库 14/14),取值偏保守:误报只是多一条 WARNING,
#: 漏报是继续高置信度认错人。
_STALE_LIBRARY_DAYS = 14.0

#: 登记图的扩展名。新登记一律写 .png(无损),但历史库里是 .jpg/.jpeg,仓库对它们
#: 保持可读(见 ``engine/identity/library.py`` 的目录图注、``_backfill`` 的
#: ``body_*.jpg`` 扫描,以及 ``person/router.py`` 的文件名白名单)。计龄必须认全,
#: 否则老库一张都匹配不上,而老库正是最需要过期告警的那一类。
_REGISTRATION_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})

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
        self._gallery_fp: tuple | None = None
        self._gallery_loaded_at: float = 0.0
        #: 加载失败的原因。要能从外面看到 —— 否则「库是空的」和「读库炸了」在
        #: 行为上同形(都是名册为空),而后者是故障。
        self.load_error: str | None = None
        #: 身份库的"登记距今多少天"。库过期是唯一被证实的失效根因,而它此前在运行时
        #: 完全不可见。取自登记**图**的 mtime,刻意不取 .npy —— 特征补算会重写 .npy,
        #: 那样一个 36 天的旧库在补算之后会显示成"全新",把唯一可靠的信号废掉。
        self.library_age_days: float | None = None
        #: 上次就"有人但一个都没认出来"打过日志的时刻,**按相机分开**。
        #: 共用一个的话:4 台相机库都过期时只有第一台打得出日志,另外 3 台永远无人报。
        self._last_miss_log: dict[str, float] = {}
        #: 懒加载两个 ONNX session 的锁。resolve 是从多个 per-device 协程经
        #: asyncio.to_thread 并发进来的,check-then-act 实测会让 6 个并发窗构造出
        #: 6 个 Detector(5 个立刻被丢弃),RSS 峰值 976MB —— 而这台机器上还跑着
        #: 本地视觉模型的边车。
        self._init_lock = threading.Lock()

    # ── 身份库 ────────────────────────────────────────────────────────────

    @property
    def gallery_size(self) -> int:
        return len(self._gallery)

    def refresh_gallery(self) -> bool:
        """按需重载身份库。返回是否发生了重载。

        变更检测**只盯这一层真正读的东西**:``meta.json``(姓名)与 ``tier_a/*.npy``
        (特征向量)。此前盯的是 ``list_persons()`` 给出的 tier_a **图文件**指纹,
        而这一层一张图都不读 —— 盯错输入实测出两条静默故障:

        1. 用户在面板上把认错的名字改对(只动 meta.json),本进程生命周期内**永远**
           继续用旧名字。刚刚叫错过名字的系统改不动名字,是最不能接受的一种。
        2. 启动时 ``main.py`` 的 ``_backfill_tier_a_reid_embeddings`` 后台补算出来的
           ``.npy``(只动 .npy)永远进不了库。它与这里的启动期 eager 加载是竞态,
           resolver 先跑完的话,被补算的成员在整个进程生命周期里都不参与比对 ——
           而按 ``_match`` 的取 argmax 语义,他们的检测框会被分给**别的成员**。

        另注意 ``_gallery_fp`` 用 ``None`` 当"从未成功加载过"的哨兵,而不是拿
        ``self._gallery`` 的真假来兼作判据:后者会让**合法的空库**(还没登记任何人,
        也就是每个新装用户的初始状态)每窗重扫目录并打一条 INFO —— 约 7000 行/天/相机。
        """
        try:
            fp = _library_fingerprint(self.root)
        except Exception as e:  # noqa: BLE001 —— 读不到库只该让名册为空
            self.load_error = f"{type(e).__name__}: {e}"
            logger.warning("[local-vision] 身份库指纹读取失败,本窗不做认人: %s", e)
            # 哨兵置 None 而不是 ():目录恢复正常后必须能自愈,而 () 恰好是
            # "库为空"的合法指纹 —— 撞上就再也不会重载了。
            self._gallery, self._gallery_fp = {}, None
            return False

        if self._gallery_fp is not None and fp == self._gallery_fp:
            return False

        try:
            # **不构造 IdentityLibrary** —— 它的 __init__ 会 _ensure_dirs(),库路径
            # 配错时会在错误位置默默建出一副空目录骨架,把「目录不存在」这个最直接的
            # 排障信号抹掉:用户以为路径配对了,实际是我们刚给他造了个空的。
            # 这一层只读,不该有任何副作用。
            persons = _read_persons(self.root)
        except Exception as e:  # noqa: BLE001
            self.load_error = f"{type(e).__name__}: {e}"
            logger.warning("[local-vision] 身份库读取失败,本窗不做认人: %s", e)
            self._gallery, self._gallery_fp = {}, None
            return False

        gallery: dict[str, tuple[str, str | None, NDArray[np.float32]]] = {}
        skipped: list[str] = []
        for p in persons:
            if not p.name:
                # 没名字的成员进了名册也只能显示成空字符串,不如不进。
                continue
            embs = _load_embeddings(self.root / "persons" / p.person_id / "tier_a")
            if embs is None:
                # **必须报出来**:没有特征的成员会静默地不参与比对,而他本人的检测框
                # 随后被 argmax 分给别的成员 —— 表现是"叫错名字",而不是"认不出"。
                skipped.append(p.name)
                continue
            gallery[p.person_id] = (p.name, p.role, embs)

        self._gallery, self._gallery_fp = gallery, fp
        self._gallery_loaded_at = time.time()
        self.library_age_days = _library_age_days(self.root)
        self.load_error = None
        age = "未知" if self.library_age_days is None else f"{self.library_age_days:.0f} 天"
        logger.info(
            "[local-vision] 身份库已加载:%d 名成员(%s),登记距今 %s%s",
            len(gallery), ", ".join(v[0] for v in gallery.values()) or "无", age,
            f";**已跳过无特征成员**:{', '.join(skipped)}" if skipped else "",
        )
        if self.library_age_days is not None and self.library_age_days >= _STALE_LIBRARY_DAYS:
            # 库过期是这套方案唯一被证实的失效根因,而且表现是**高置信度认错**,
            # 不是认不出 —— 实测一份 36 天前、另一房间另一身衣服的库,14 个人体框
            # 全部偏向同一个成员,逐人正确率 0/14。代码侧无解(margin 规则的正确/
            # 错误分布完全重叠),所以只能把它喊出来。
            logger.warning(
                "[local-vision] 身份库登记距今已 %.0f 天。人体 ReID 主要依据衣着,"
                "旧库会**高置信度认错人**(实测 36 天的库逐人正确率 0/14),"
                "建议用当前画面重新登记", self.library_age_days,
            )
        return True

    # ── 主流程 ────────────────────────────────────────────────────────────

    def resolve(self, frames: list, source: str = "") -> list[PersonHit]:
        """从一窗的帧里解析出名册。任何异常都收敛成空名册。

        ``source`` 是相机标识,只用于日志归属。给了默认值是因为这个对象是**注入**的:
        只实现 ``resolve(frames)`` 的替身或第三方实现,加位置参数会 TypeError,
        落进下面的兜底 → 名册恒空 —— 一个"改了个签名把认人静默关掉"的故障。
        """
        try:
            return self._resolve(frames, source)
        except Exception as e:  # noqa: BLE001 —— 见模块文档:fail-open
            logger.warning("[local-vision] 认人失败,本窗名册留空 did=%s: %s",
                           source or "-", e, exc_info=True)
            return []

    def _resolve(self, frames: list, source: str) -> list[PersonHit]:
        self.refresh_gallery()
        # **取一次快照,整窗只用它**。此前是直接读 self._gallery:_match 取到 pid
        # 之后、用 pid 取姓名之前,另一台相机的协程可能刚好触发重载(或走进异常分支
        # 把它清空),于是 KeyError → 兜底 → **整份名册被吞成空**。
        gallery = self._gallery
        if not gallery:
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
        # 先把「每个框 vs 每个成员」的相似度全算出来,再统一裁决。
        # 逐框独立取 argmax 是不行的:两个人可以同时最像同一个成员,于是一个人被安上
        # 别人的名字、另一个人整个从名册里消失(线上实测发生过)。
        rows: list[tuple[Any, dict[str, float]]] = []
        best_score: float | None = None
        for d in dets:
            crop = _crop(best_frame, d)
            if crop is None:
                continue
            feat = _embed(reid, crop)
            if feat is None:
                continue
            sims = {pid: float(np.max(embs @ feat)) for pid, (_, _, embs) in gallery.items()}
            if sims:
                top = max(sims.values())
                best_score = top if best_score is None else max(best_score, top)
            rows.append((d, sims))

        picked = _assign(rows, gallery, self.threshold, w, h)

        if not picked and best_score is not None:
            self._log_all_missed(source, len(dets), best_score)
        return sorted(picked, key=lambda p: p.bbox[0])

    def _log_all_missed(self, source: str, n_people: int, best: float) -> None:
        """画面里明明有人却一个都没认出来 —— 把最高相似度喊出来。

        不说的话,它与"屋里没人""认人没开"在日志上完全同形,而三者的处置南辕北辙。
        按相机分开节流:共用一个计时器的话,多相机同时过期时只有第一台报得出来。
        """
        now = time.time()
        if now - self._last_miss_log.get(source, 0.0) <= _MISS_LOG_INTERVAL_SEC:
            return
        self._last_miss_log[source] = now
        age = "" if self.library_age_days is None else f",库已登记 {self.library_age_days:.0f} 天"
        logger.info(
            "[local-vision] did=%s 画面里有 %d 个人但都没认出来,最高相似度 %.3f"
            "(阈值 %.2f)%s。持续如此多半是身份库过期,建议用当前画面重新登记",
            source or "-", n_people, best, self.threshold, age,
        )

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

    def _get_detector(self):
        # 双重检查 + 锁:resolve 从多个 per-device 协程经 to_thread 并发进来,
        # 裸的 check-then-act 实测让 6 个并发窗构造出 6 个 Detector。
        if self._detector is None:
            with self._init_lock:
                return self._detector_locked()
        return self._detector

    def _detector_locked(self):
        if self._detector is None:
            from miloco.perception.engine.identity.tracker.detector import Detector

            kw = {"model_path": self._detector_model_path} if self._detector_model_path else {}
            self._detector = Detector(**kw)
        return self._detector

    def _get_reid(self):
        if self._reid is None:
            with self._init_lock:
                return self._reid_locked()
        return self._reid

    def _reid_locked(self):
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


def _assign(
    rows: list, gallery: dict, threshold: float, w: int, h: int,
) -> list[PersonHit]:
    """把「框 × 成员」的相似度矩阵裁决成一份名册。

    **顺序不能反,这是整件事里最容易写错的一处。**

    正确顺序是:先用阈值把亚阈值格子封死 → 在剩下的矩阵上做一对一最优指派 →
    逐对再验一次阈值。反过来(先指派再卡阈值)会**凭空制造错名字**:画面里
    1 个真人 + 1 个电视误检时,指派被迫把两个成员都发出去,而电视框系统性地更像
    某一个成员,于是那个名字被发给电视、真人被挤到另一个名字上;随后阈值把电视
    那一对丢掉,**真人却留着错名字**。104 组实测 96/104 → 40/104。

    一对一还把「同名只出现一次」从事后去重变成结构性保证。事后去重的老写法会在
    两个人同时最像同一成员时,把另一个人整个从名册里删掉。

    注意这一步把「画面里的人都是已登记成员」当前提,阈值是唯一的守卫,而它今天的
    余量只有 0.03(电视 crop 最高 0.670 vs 阈值 0.70)。所以合入指派之后,阈值
    **更不能往下调** —— 实测降到 0.65 是 96/104 → 47/104,降到 0.60 时电视框
    104/104 全被安上姓名。
    """
    if not rows or not gallery:
        return []
    from scipy.optimize import linear_sum_assignment

    pids = list(gallery.keys())
    # 亚阈值格子直接封死(置 -1)。用 -1 而不是 0:余弦可以为 0,而被封死的格子
    # 必须严格劣于任何可用格子,否则指派可能挑中它。
    cost = np.full((len(rows), len(pids)), -1.0, dtype=np.float64)
    for i, (_, sims) in enumerate(rows):
        for j, pid in enumerate(pids):
            v = sims.get(pid)
            if v is not None and v >= threshold:
                cost[i, j] = v
    if not (cost >= threshold).any():
        return []

    out: list[PersonHit] = []
    for i, j in zip(*linear_sum_assignment(cost, maximize=True)):
        score = float(cost[i, j])
        # 指派会把每一行都配出去(含被封死的格子),所以必须逐对再验一次。
        if score < threshold:
            continue
        d = rows[i][0]
        name, role, _ = gallery[pids[j]]
        out.append(PersonHit(
            person_id=pids[j], name=name, role=role,
            bbox=_norm_bbox(d, w, h), score=score,
        ))
    return out


def _read_persons(root: Path) -> list:
    """只读地列出成员(person_id / name / role)。

    **刻意不构造 ``IdentityLibrary``** —— 它的 ``__init__`` 会 ``_ensure_dirs()``,
    库路径配错时会在错误位置默默建出一副空目录骨架,把"目录不存在"这个最直接的
    排障信号抹掉。而这一层只是读,不该有任何副作用。

    返回一个带 ``person_id`` / ``name`` / ``role`` 的轻量对象列表,字段名与
    ``IdentityLibrary.list_persons()`` 的 ``PersonRef`` 对齐,调用方无需分辨。
    """
    import json as _json
    from types import SimpleNamespace

    persons = root / "persons"
    if not persons.is_dir():
        return []
    out = []
    for pdir in sorted(persons.iterdir()):
        if not pdir.is_dir() or pdir.name.startswith("."):
            continue
        name = role = None
        meta = pdir / "meta.json"
        if meta.is_file():
            try:
                m = _json.loads(meta.read_text(encoding="utf-8"))
                name, role = m.get("name"), m.get("role")
            except Exception as e:  # noqa: BLE001 —— 单个坏 meta 不该让整库读不出来
                logger.warning("[local-vision] meta.json 解析失败 %s: %s", pdir.name, e)
        out.append(SimpleNamespace(person_id=pdir.name, name=name, role=role))
    return out


def _library_fingerprint(root: Path) -> tuple:
    """身份库的变更指纹 —— **只盯这一层真正读的东西**,而且盯的是内容不是 mtime。

    读的是 ``meta.json``(姓名)与 ``tier_a/*.npy``(特征)。刻意**不含图文件**:
    盯图会让"改名字"(只动 meta.json)和"补算特征"(只动 .npy)都察觉不到,
    两者都实测复现过,而且失效方式都是静默的。

    **为什么哈希内容而不是取 (mtime, size)**:实测这台机器上 ``st_mtime_ns`` 根本
    没有纳秒精度 —— 相继两次写拿到完全相同的时间戳,连不同文件之间都一样。改名
    「小亮」→「亮亮」字节数又恰好相同,于是 (mtime, size) 完全看不出变化。
    数据量本来就极小(每人 meta ~40B + 5 个向量各 640B ≈ 3KB),读一遍再哈希
    远比重跑 ``np.load`` + 归一化 + ``list_persons()`` 便宜,而且是**精确**的 ——
    没有"granularity 够不够"这个问题。

    同样刻意不用 ``IdentityLibrary.list_persons()`` 来取指纹 —— 它的构造函数会
    ``_ensure_dirs()``,库路径配错时会把一个空库**悄悄物化出来**,抹掉"目录不存在"
    这个最直接的排障信号。(注册侧各路径都自己 mkdir,不依赖这个副作用。)
    """
    import hashlib

    persons = root / "persons"
    if not persons.is_dir():
        return ()
    out: list[tuple] = []
    for pdir in sorted(persons.iterdir()):
        if not pdir.is_dir() or pdir.name.startswith("."):
            continue
        h = hashlib.blake2b(digest_size=16)
        meta = pdir / "meta.json"
        if meta.is_file():
            h.update(meta.read_bytes())
        for npy in sorted((pdir / "tier_a").glob("body_*.npy")):
            h.update(npy.name.encode())
            h.update(npy.read_bytes())
        out.append((pdir.name, h.hexdigest()))
    return tuple(out)


def _library_age_days(root: Path) -> float | None:
    """身份库里最新一张**登记图**距今多少天。库为空时返回 None。

    取图而不取 ``.npy``:``main.py`` 的 ``_backfill_tier_a_reid_embeddings`` 会
    重写每一个 .npy —— 若按 .npy 计龄,一个 36 天的过期库在补算之后会显示成"全新",
    亲手把唯一可靠的过期信号废掉。图只在真的重新登记时才变。
    """
    persons = root / "persons"
    if not persons.is_dir():
        return None
    newest: float | None = None
    for img in persons.glob("*/tier_a/body_*"):
        # **必须认全三种扩展名。** 新登记写 .png,但历史库是 .jpg/.jpeg,而仓库明确
        # 保留了对它们的读取(见 library.py 的目录图注与 _backfill 里的 body_*.jpg
        # 扫描、person/router.py 的文件名白名单)。只 glob .png 的话,jpg 老库会一张
        # 图都匹配不到 → 返回 None → 上面那条 `is not None` 判断落空 → 过期告警
        # 永不触发。方向恰好是反的:越老的库越可能是 jpg 时代留下的,也就越需要
        # 这条告警,而它偏偏在这些库上静默失效。
        #
        # 反过来,.npy / .json 必须排除掉,理由见本函数开头 —— 按 .npy 计龄会被
        # backfill 重写成"全新"。所以这里用后缀白名单,而不是"排除已知的几种"。
        if img.suffix.lower() not in _REGISTRATION_IMAGE_SUFFIXES:
            continue
        try:
            m = img.stat().st_mtime
        except OSError:
            continue
        newest = m if newest is None else max(newest, m)
    if newest is None:
        return None
    return max(0.0, (time.time() - newest) / 86400.0)
