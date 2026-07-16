# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""宠物外观观察（pet:observe）—— 上传图/视频，选出最优宠物 crop，让 omni 按维度生成外观描述。

设计（见 .wsh_cc/宠物识别方案.md §2.3(2) / §3a.6 与 P1a spec，仅设计参考）：
- **选 crop（门控）**：单图取最大 CAT/DOG 框 + 扩边 5%，过单图硬门槛（面积 / 多只同框）；
  视频用**无 ReID 的 SortTracker** 把多只宠物分 track，取**主体（dominant）track**，在其候选池里
  硬排除（多只同框 / 面积过小 / 太糊）后按加权分 + dHash 多样性选出**同一只的 ≤3 张多姿态** crop。
- **一次成型 describe**：不逐图、不多次 omni——把选出的 ≤3 张 crop **一次性**送 omni，让其提炼
  共性稳定特征、输出**一条**描述，并隐式忽略看不清 / 无目标的图（决策 D8）。
- **detector / tracker 每次新建**（镜像人体注册的工厂模式；tracker 带状态必须 fresh）；
  不接 IdentityEngine（红线：宠物永不进 person 表 / ReID / gallery）。
- **无 ReID 的同一性保证**：视频靠 SortTracker 分轨（同 track≈同一只，短片段内）、多图靠 omni
  ``refs_inconsistent`` 判"疑似不是同一只" + 用户候选选择。⚠️ SORT 无 ReID 会断轨，v1 接受、注释标注。
- **回退**：检测器（YOLO 只认猫/狗）框不到时，把整幅画面交给 omni 聚焦最明显的一只动物
  描述——兼容兔/鸟/仓鼠等非猫狗物种；omni 判定确无动物才算未检出。
- **grounding**：开关开时 prompt 追加要头部归一化 bbox（相对各 crop），解析回传（每 crop 一个）。

本模块无副作用：只观察、不落库；落库由调用方在用户确认后走既有写入路径。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from typing import Any

import cv2
import numpy as np

from miloco.config import get_settings
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.identity._image_utils import compute_sharpness
from miloco.perception.engine.identity.extractor import (
    _crop_with_padding,
    _sample_video_frames,
)
from miloco.perception.engine.identity.tracker.detector import Detection, Detector
from miloco.perception.engine.omni import response_parser
from miloco.perception.engine.omni.omni_client import (
    call_omni,
    resolve_live_omni_config,
)

logger = logging.getLogger(__name__)

_PET_CLASS_IDS = (Detection.CLASS_CAT, Detection.CLASS_DOG)
_SPECIES_BY_CLASS = {Detection.CLASS_CAT: "猫", Detection.CLASS_DOG: "狗"}
_PADDING_RATIO = 0.05
_MAX_VIDEO_FRAMES = 60
_MAX_SELECT = 3  # 参考 crop 上限（对齐 pet_library._MAX_REF_CROPS）

# ── 宠物注册专用门控常量（决策6，刻意不复用 identity/extractor._passes_quality_gate）──
# extractor 那套是 area5% / 绝对 sharpness50，服务 TierU/TierC 人体质量门。宠物注册素材
# 多为用户随手拍，尺寸门用「短边 + 绝对面积 + 相对线性占比」三条，兼顾低分辨率来源与大画面远景。
# 尺寸硬门槛（三条 AND，见 _size_gate_ok）——起因：旧的"面积比例 ≥2%"对大画面过苛（2960×1666 里
# 一只清晰的猫只占 1.4% 面积就被误杀）。改成：
PET_GATE_MIN_SIDE_PX = 25  # 短边 min(w,h) 下限：挡细长条 / 极小框
PET_GATE_MIN_AREA_PX = 4096  # 绝对面积下限 ≈64×64：保证低分辨率下 crop 也够大够辨个体
PET_GATE_MIN_LINEAR_RATIO = 0.05  # (w+h) ≥ 该系数*(fw+fh)：够份量（≈0.27%面积；高清下主力门）
# 决策 D2：清晰度用**绝对低阈**（非分位）——只卡很糊。实测 22 张注册 crop：绝对30 卡 ~5%、
# 绝对50 卡 ~14%（20 分位恒卡 20% 不合适已弃）；先取保守下界 30，**须在生产 60 帧口径复标**。
# sharpness 仍进 gate_score 加权（此处只做硬下限）。
PET_GATE_SHARP_MIN = 30.0
PET_GATE_OVERLAP_MAX = 0.10  # 同帧两框相交 > min(area)*该值 → 判多只同框（crowded）
POOL_CAP_PER_TRACK = 12  # 每 track 候选池上限（控内存/算力）
# conf 硬门槛不单列：detector(0.4)/tracker(0.5) 既有过滤已承担；conf 仅进加权分。


def _size_gate_ok(bw: int, bh: int, fw: int, fh: int) -> bool:
    """crop 尺寸硬门槛：短边 / 绝对面积 / 相对线性占比 三条都过才算够大够辨。

    - 短边 ``min(w,h) > 25``：挡细长条 / 极小框。
    - 绝对面积 ``w*h >= 4096``（≈64×64）：低分辨率来源也保证内在细节够辨个体。
    - 线性占比 ``w+h >= 0.05*(fw+fh)``（≈0.27% 面积）：够份量，挡巨帧里的远景小点。
    用线性占比而非面积比例，是因为大画面里裁出的框即便面积占比很低、绝对尺寸仍可很大很清晰。
    """
    return (
        min(bw, bh) > PET_GATE_MIN_SIDE_PX
        and bw * bh >= PET_GATE_MIN_AREA_PX
        and (bw + bh) >= PET_GATE_MIN_LINEAR_RATIO * (fw + fh)
    )

OBSERVE_SYSTEM_PROMPT = """你是宠物外貌观察助手。先**准确判定物种**（狗 / 猫 / 兔 / 鸟 / 龟 …；别把狗当猫、也别把猫当狗），再客观描述这只动物用于日后在监控画面中区分和称呼它的**稳定外观特征**。
只描述可见且长期稳定的特征；不描述背景、动作、情绪；把握不大的维度（如品种）宁可留空也不要猜。
严格输出如下 JSON（不要输出多余文字、不要 markdown 代码围栏）：
{"species":"按实际动物填，如 猫 / 狗 / 兔 / 仓鼠 / 鸟 / 龟 等","breed":"<高把握才填，否则空字符串>","size_build":"如 中等体型、体态壮实","coat_color_pattern":"如 黑色 / 橘白双色 / 狸花","coat_length_texture":"如 短毛顺滑 / 长毛蓬松","distinctive_markings":["如 尾尖一撮白","左耳缺口"],"accessories":"<可变动配饰，如项圈颜色；无则空字符串>","summary":"一句能把这一只和同物种其它个体区分开的规范化外观句：优先写最具辨识度的 1-2 个稳定特征（独特花纹 / 毛型 / 体态或品种 / 项圈 / 耳缺口等）+ 物种；避免只写通用毛色这种大众脸描述；拿不准的特征不写"}"""

_GROUNDING_CLAUSE = """
另外，在上述 JSON 中**额外**加一个键 "head_bbox"：宠物头部区域相对本图的归一化边界框 [x, y, w, h]（四个 0~1 之间的小数，x/y 为左上角，w/h 为宽高占比）；头部不可见则置 null。"""

# 多图（≥2 crop）时把 head_bbox 换成 head_bboxes（按输入图顺序，每张一个）。
_GROUNDING_CLAUSE_MULTI = """
另外，在上述 JSON 中**额外**加一个键 "head_bboxes"：**按输入图片顺序**每张一个头部区域相对该图的归一化边界框 [x, y, w, h]（四个 0~1 之间的小数，x/y 为左上角，w/h 为宽高占比）；某张头部不可见则该位置置 null。"""

# 多图共性描述（决策 D8「一次成型」）：一次调用出一条共性描述，隐式忽略看不清/无目标的图。
_MULTI_CLAUSE = """
以下多张图片是**同一只宠物**的不同姿态/角度。请**综合所有图片提炼共性、长期稳定的外观特征**，只输出**一条**描述（不要逐图罗列、不要写"第一张/第二张"）。**若某张看不清、被遮挡或没有目标宠物，请直接忽略它**，用其余清晰的图判断。若这些图**物种/毛色/花纹差异过大、疑似根本不是同一只宠物**，在 JSON 中额外加一个键 "refs_inconsistent": true（否则不加或置 false）。"""

# 回退路径（检测器没框到猫/狗时）用整幅画面，让 omni 自己找最明显的动物——
# 兼容兔/鸟/仓鼠等 YOLO 不识别的物种，并给出"确无动物"的信号。
_WHOLE_FRAME_CLAUSE = """
注意：这是未裁剪的整幅画面，可能含背景或多只动物。请只聚焦画面中**最明显的一只动物**来描述（宠物不限于猫狗）；若画面中确实没有任何动物，把 species 与 summary 都置为空字符串。"""

# 本体 grounding（决策 D7）：仅回退路径用，让 omni 框出所描述那只动物的整体，裁本体作参考 crop。
_BODY_GROUNDING_CLAUSE = """
另外，在上述 JSON 中**额外**加一个键 "body_bbox"：你所描述的**那一只动物整体**相对本图的归一化边界框 [x, y, w, h]（四个 0~1 之间的小数，x/y 为左上角，w/h 为宽高占比）；无法确定或画面无动物则置 null。"""

_SINGLE_USER_CONTENT = "请观察画面中的动物，按要求输出 JSON。"
_MULTI_USER_CONTENT = "请综合以下多张同一只宠物的图片，按要求输出 JSON。"


def default_detector() -> Detector:
    """新建一个 YOLO Detector（走 ``models_dir/det_4C.onnx``）。

    镜像人体注册 ``person/router._load_detector`` 的口径：每次调用新建（observe 低频）。
    """
    det_path = get_settings().directories.models_dir / "det_4C.onnx"
    return Detector(model_path=str(det_path), conf_threshold=0.4, use_gpu=False)


def _jpeg_b64(crop: np.ndarray, quality: int = 85) -> str:
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode() if ok else ""


# ── 门控叶子函数（纯 numpy/cv2，无新依赖）───────────────────────────────────────


def _box_crowded(
    target_xyxy: tuple, others_xyxy: list[tuple], ratio: float
) -> bool:
    """target 框与同帧其它框相交面积 > min(两框面积)*ratio → 判为多只同框（crowded）。"""
    ax1, ay1, ax2, ay2 = target_xyxy
    aa = max((ax2 - ax1) * (ay2 - ay1), 1)
    for bx1, by1, bx2, by2 in others_xyxy:
        ba = max((bx2 - bx1) * (by2 - by1), 1)
        iw = max(0, min(ax2, bx2) - max(ax1, bx1))
        ih = max(0, min(ay2, by2) - max(ay1, by1))
        if iw * ih > ratio * min(aa, ba):
            return True
    return False


def _push_bounded(pool: list[dict], cand: dict, cap: int) -> None:
    """把候选压入池；超上限则按初步质量分（conf×sharpness×area）保留 top-cap。"""
    pool.append(cand)
    if len(pool) > cap:
        pool.sort(
            key=lambda c: c["conf"] * c["sharpness"] * c["area_ratio"], reverse=True
        )
        del pool[cap:]


def _minmax_normalize(items: list[dict], key: str, out: str) -> None:
    """池内 min-max 归一化 items[*][key] → items[*][out]（全同则 out 恒 0）。"""
    vs = [c[key] for c in items]
    lo, hi = min(vs), max(vs)
    rng = (hi - lo) or 1.0
    for c in items:
        c[out] = (c[key] - lo) / rng


def _dhash(crop: np.ndarray, n: int = 8) -> int:
    """感知哈希（difference hash）：缩到 (n+1)×n 灰度，逐行相邻比较 → n*n bit。"""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    r = cv2.resize(g, (n + 1, n))
    diff = r[:, 1:] > r[:, :-1]
    return int("".join("1" if b else "0" for b in diff.flatten()), 2)


def _dhash_diverse_topk(
    cands_sorted: list[dict], k: int, min_ham: int = 6
) -> list[dict]:
    """贪心多样性：按 gate_score 高→低取，跳过与已选感知哈希过近的；不足则补高分剩余。"""
    out: list[dict] = []
    hashes: list[int] = []
    for c in cands_sorted:
        h = c.get("dhash")
        if h is None:
            h = _dhash(c["crop"])
            c["dhash"] = h
        if all(bin(h ^ ph).count("1") >= min_ham for ph in hashes):
            out.append(c)
            hashes.append(h)
        if len(out) >= k:
            break
    if len(out) < k:  # 多样性不足 → 补 gate_score 最高的剩余
        # 按 id() 判重：候选 dict 里含 ndarray（crop），绝不能用 `in`/`==`（触发 ndarray 真值判断 → ValueError）
        chosen = {id(c) for c in out}
        for c in cands_sorted:
            if len(out) >= k:
                break
            if id(c) not in chosen:
                out.append(c)
                chosen.add(id(c))
    return out[:k]


def _gate_select(cands: list[dict]) -> list[dict]:
    """主体 track 候选池 → 硬排除 → 加权分 → dHash 多样性 → 同一只的 ≤3 张多姿态。

    决策4：硬排除后为空 → 返回 ``[]``（退纯描述），**绝不放宽**（勿抄 eval 的 pool=ok or raw）。
    """
    ok = [
        c
        for c in cands
        if (not c["crowded"])
        and c["size_ok"]  # 尺寸硬门槛（短边/绝对面积/线性占比，见 _size_gate_ok）
        and c["sharpness"] >= PET_GATE_SHARP_MIN
    ]
    if not ok:
        return []
    for key in ("conf", "sharpness", "area_ratio"):
        _minmax_normalize(ok, key, f"{key}_n")
    for c in ok:
        c["gate_score"] = (
            0.5 * c["conf_n"] + 0.4 * c["sharpness_n"] + 0.1 * c["area_ratio_n"]
        )
    ok.sort(key=lambda c: c["gate_score"], reverse=True)
    return _dhash_diverse_topk(ok, _MAX_SELECT)


def _largest_pet_crop(frame: np.ndarray, detector: Any) -> dict | None:
    """单图：取面积最大的 CAT/DOG 框、扩边 5% 裁出，过单图硬门槛。

    返回候选 dict（含 P0 质量分 + ``crowded``）或 ``None``（无宠 / 裁空 / 硬门槛未过 → 退回整幅描述）。
    """
    dets = [d for d in detector.detect_pets(frame) if d.class_id in _PET_CLASS_IDS]
    if not dets:
        return None
    best = max(dets, key=lambda d: d.w * d.h)
    crop = _crop_with_padding(frame, (best.x, best.y, best.w, best.h), _PADDING_RATIO)
    if crop is None or crop.size == 0:
        return None
    fh, fw = frame.shape[:2]
    area_ratio = float(best.w * best.h) / float(max(fw * fh, 1))
    crowded = _box_crowded(
        best.xyxy, [d.xyxy for d in dets if d is not best], PET_GATE_OVERLAP_MAX
    )
    # A3 单图硬门槛：尺寸不够（短边/绝对面积/线性占比）或多只同框无法择一 → 退回整幅描述（不产参考 crop）
    if crowded or not _size_gate_ok(best.w, best.h, fw, fh):
        return None
    return {
        "track_id": None,
        "class_id": best.class_id,
        "crop": crop,
        # P0 契约：如实暴露质量分（纯几何/检测器、零额外 omni）
        "conf": float(best.confidence),
        "sharpness": float(compute_sharpness(crop)),
        "area_ratio": area_ratio,
        "bbox": (best.x, best.y, best.w, best.h),
        "frame_idx": None,  # 单图无帧序号
        "crowded": crowded,
    }


def _select_video_crops(
    frames: list[tuple[int, np.ndarray]], detector: Any, fps: int
) -> tuple[list[dict], int]:
    """视频：无 ReID 的 SORT 分 track → 主体 track 候选池 → 门控 ≤3 张同一只多姿态。

    返回 ``(selected≤3, n_coincident)``——``n_coincident`` = **同一帧内真检测匹配的宠物数的跨帧最大值**
    （判 multiple_pets 用；见循环内注释）。喂 SortTracker 的是**预过滤宠物检测**（detect_pets）+
    ``track_human_only=False``，即便日后该开关被移除（只跟踪所喂检测）逻辑仍成立。
    ⚠️ 无 ReID 时同一只可能被 SORT 断成两 track → 主体选错；短注册片段少见，v1 接受。**但"多只"判定
    不再用"累计 track 数"（会把断轨的一只算成多只），改用"同帧共现最大只数"，见下。**
    """
    from miloco.perception.engine.identity.sort import SortConfig, SortTracker

    tracker = SortTracker(
        config=SortConfig(track_human_only=False),
        detector=detector,
        fps=max(1, fps),
    )
    pool: dict[int, list[dict]] = {}
    n_coincident = 0  # 同帧真检测匹配宠物数的跨帧最大值（= 最多同时有几只入镜）
    for _idx, frame in frames:
        fh, fw = frame.shape[:2]
        pet_dets = [
            d for d in detector.detect_pets(frame) if d.class_id in _PET_CLASS_IDS
        ]
        tracker.update_with_detections(frame, pet_dets)
        tracks = [
            t for t in tracker.get_tracking_results() if t["class_id"] in _PET_CLASS_IDS
        ]
        # 「多只」判定维度 = **同一帧**共现，且只数**本帧真检测匹配**（time_since_update==0）的 track：
        # 一只宠物跟丢又重现时，旧 track 靠 Kalman 预测续命、新 track 已开启——若按"整段累计 track 数"
        # 或把预测框也算进来，会把这**一只**误判成"多只/同帧多只"（伪多宠）。get_tracking_results 已
        # pre-filter tsu>=1，这里显式再夹一道 tsu==0，兼容将来 DeepSORT 路径可能回预测框。
        matched_ids = {t["id"] for t in tracks if t.get("time_since_update", 0) == 0}
        n_coincident = max(n_coincident, len(matched_ids))
        for tr in tracks:
            crop = _crop_with_padding(frame, tr["bbox"], _PADDING_RATIO)
            if crop is None or crop.size == 0:
                continue
            bw, bh = tr["bbox"][2], tr["bbox"][3]
            crowded = _box_crowded(
                tr["xyxy"],
                [t["xyxy"] for t in tracks if t["id"] != tr["id"]],
                PET_GATE_OVERLAP_MAX,
            )
            cand = {
                "track_id": tr["id"],
                "class_id": tr["class_id"],
                "crop": crop,
                # P0 契约质量分（conf=选中帧 detection 置信；bbox=Kalman 后验框，来源本帧真实匹配）
                "conf": float(tr["confidence"]),
                "sharpness": float(compute_sharpness(crop)),
                "area_ratio": float(bw * bh) / float(max(fw * fh, 1)),
                "bbox": tuple(tr["bbox"]),
                "frame_idx": _idx,
                "crowded": crowded,
                # 尺寸门在此算好（有帧尺寸），_gate_select 直接读（短边/绝对面积/线性占比）
                "size_ok": _size_gate_ok(bw, bh, fw, fh),
            }
            _push_bounded(pool.setdefault(tr["id"], []), cand, POOL_CAP_PER_TRACK)
    if not pool:
        return [], 0
    # 主体（dominant）track = 被注册那一只：匹配帧数最多，平手取累计 sharpness 最大。
    # 多只【同帧】入镜时只取主体，由 observe_pet 出 multiple_pets 提示（别静默丢，决策 D8）。
    primary_tid = max(
        pool, key=lambda t: (len(pool[t]), sum(c["sharpness"] for c in pool[t]))
    )
    return _gate_select(pool[primary_tid]), n_coincident


def _video_pet_crops(
    frames: list[tuple[int, np.ndarray]], detector: Any, fps: int
) -> list[dict]:
    """薄封装：只取主体 track 的门控结果（丢弃 n_coincident，供直接测试/复用）。"""
    return _select_video_crops(frames, detector, fps)[0]


def _align_head_bboxes(raw: Any, n: int) -> list:
    """把 omni 回传的 head_bboxes 对齐到 n 张：非法项→None，截断/补齐到 n。"""
    if not isinstance(raw, list):
        return [None] * n
    out = [
        (b if isinstance(b, list) and len(b) == 4 else None) for b in raw[:n]
    ]
    out += [None] * (n - len(out))
    return out


def _omni_config_for(n: int) -> OmniConfig:
    """单图/回退（n≤1）用默认配置，与改造前逐字节一致；多图给更多输出预算。"""
    if n <= 1:
        return resolve_live_omni_config(OmniConfig())
    bump = min(1024, int(512 * (1 + 0.25 * (n - 1))))  # n=2→640, n=3→768
    return resolve_live_omni_config(OmniConfig(max_completion_tokens=bump))


def _valid_norm_bbox(b: Any) -> list | None:
    """校验归一化 bbox：必须是 4 个数的 list，否则 None。"""
    return b if isinstance(b, list) and len(b) == 4 else None


async def _omni_describe(
    crops: list[np.ndarray],
    *,
    grounding: bool,
    whole_frame: bool = False,
    body_grounding: bool = False,
) -> tuple[dict, list, bool | None, list | None]:
    """把 ≤3 张同一只宠物的 crop **一次性**送 omni，返回 (描述 dict, head_bboxes, refs_inconsistent, body_bbox)。

    - ``head_bboxes``：与 crops 对齐的 list（每张一个框或 None）；``grounding=False`` 时全 None。
    - ``refs_inconsistent``：多图时 omni 判"疑似不是同一只"→ bool；单图恒 ``None``。
    - ``body_bbox``：本体归一化框（决策 D7）——仅 ``body_grounding=True``（回退/整幅画面）时解析，否则 ``None``。
    - 单图（len==1、非 multi、非 body）退化为与改造前**等价**：无 multi 片段、``head_bbox`` 单键、默认 token。
    - ``whole_frame=True`` 用于回退：传未裁剪整幅画面，让 omni 聚焦最明显的一只动物（兼容非猫狗物种）。
    """
    multi = len(crops) > 1
    ground_clause = _GROUNDING_CLAUSE_MULTI if multi else _GROUNDING_CLAUSE
    system_prompt = (
        OBSERVE_SYSTEM_PROMPT
        + (_WHOLE_FRAME_CLAUSE if whole_frame else "")
        + (_MULTI_CLAUSE if multi else "")
        + (ground_clause if grounding else "")
        + (_BODY_GROUNDING_CLAUSE if body_grounding else "")
    )
    payload = {
        "system_prompt": system_prompt,
        "user_content": _MULTI_USER_CONTENT if multi else _SINGLE_USER_CONTENT,
        "crops": [{"media_type": "image/jpeg", "data": _jpeg_b64(c)} for c in crops],
    }
    config = _omni_config_for(len(crops))
    raw = await call_omni(payload, config, type="on_demand")
    content = response_parser.parse_query_response(raw)
    data = json.loads(response_parser.extract_json(content))
    refs_inconsistent = (
        bool(data.pop("refs_inconsistent", False)) if multi else None
    )
    # body_bbox 始终从 description 弹出（避免污染落库描述），仅 body_grounding 时才采用
    raw_body = data.pop("body_bbox", None)
    body_bbox = _valid_norm_bbox(raw_body) if body_grounding else None
    if not grounding:
        head_bboxes: list = [None] * len(crops)
    elif multi:
        head_bboxes = _align_head_bboxes(data.pop("head_bboxes", None), len(crops))
    else:
        head_bboxes = [data.pop("head_bbox", None)]
    return data, head_bboxes, refs_inconsistent, body_bbox


def _candidate_out(c: dict, head_bbox: Any = None) -> dict:
    """候选对外契约：质量分 + 每候选 head_bbox；内部字段（score/gate_score/*_n/dhash/crowded）不外露。"""
    return {
        "track_id": c.get("track_id"),
        "species_guess": _SPECIES_BY_CLASS.get(c["class_id"], "其他"),
        "crop_b64": _jpeg_b64(c["crop"]),
        "conf": c.get("conf"),
        "sharpness": c.get("sharpness"),
        "area_ratio": c.get("area_ratio"),
        "bbox": list(c["bbox"]) if c.get("bbox") is not None else None,
        "frame_idx": c.get("frame_idx"),
        "head_bbox": head_bbox,
    }


def _norm_species(s: str) -> str:
    """中文物种别名归一化（仅用于 warnings 一致性比较）。"""
    s = s.strip()
    if s in ("猫", "猫咪", "猫猫"):
        return "猫"
    if s in ("狗", "犬", "狗狗", "犬类"):
        return "狗"
    return s


def _build_warnings(
    description: dict | None,
    selected: list[dict],
    refs_inconsistent: bool | None,
    n_coincident: int,
) -> list[dict]:
    """后端算的建议类不阻断提示（web 渲染黄叹号 / 软确认）。"""
    out: list[dict] = []
    if description:
        # species_mismatch：仅当 detector 判定为猫/狗（selected 非空）时才与描述物种比对
        guesses = {
            g
            for g in (_SPECIES_BY_CLASS.get(c["class_id"]) for c in selected)
            if g in ("猫", "狗")
        }
        desc_sp = _norm_species(str(description.get("species") or ""))
        if guesses and desc_sp and desc_sp not in {_norm_species(g) for g in guesses}:
            out.append(
                {
                    "type": "species_mismatch",
                    "level": "warn",
                    "message": f"检测判定为{'/'.join(sorted(guesses))}，描述判定为{desc_sp}，请核对物种。",
                }
            )
        # generic_look（大众脸）：无区分性花纹/品种/配饰 → 可能与同类难区分（v1 用字段空缺作代理）
        markings = description.get("distinctive_markings") or []
        breed = str(description.get("breed") or "").strip()
        acc = str(description.get("accessories") or "").strip()
        if not markings and not breed and not acc:
            out.append(
                {
                    "type": "generic_look",
                    "level": "warn",
                    "message": "未发现明显的区分性特征（花纹/品种/配饰），可能与同类难以区分，建议补充更具特征或更清晰的素材。",
                }
            )
    if refs_inconsistent:
        out.append(
            {
                "type": "refs_inconsistent",
                "level": "warn",
                "message": "上传的几张图差异较大，疑似不是同一只宠物，请确认是否继续。",
            }
        )
    if n_coincident > 1:
        out.append(
            {
                "type": "multiple_pets",
                "level": "warn",
                "message": "画面中检测到多只宠物，已自动选择主体，如需注册其它只请单独上传该只的素材。",
            }
        )
    return out


def _decode_and_sample(
    video_bytes: bytes, max_frames: int
) -> tuple[list[tuple[int, np.ndarray]], float]:
    """把视频 bytes 写临时 .mp4 → 采样帧 → 清理临时文件（``_sample_video_frames`` 收 path 不收 bytes）。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    try:
        tmp.write(video_bytes)
        tmp.close()
        return _sample_video_frames(tmp.name, max_frames=max_frames)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _first_decodable(medias: list[bytes]) -> np.ndarray | None:
    """回退用：取第一张能解码的图（多图时任一可用即可作整幅画面）。"""
    for m in medias:
        img = cv2.imdecode(np.frombuffer(m, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is not None and img.size > 0:
            return img
    return None


def _crop_from_norm_bbox(
    frame: np.ndarray, norm_bbox: Any, padding: float = _PADDING_RATIO
) -> tuple[np.ndarray | None, tuple | None]:
    """按归一化 bbox [x,y,w,h] 从整幅画面裁本体（D7 回退用），返回 (crop, 像素 bbox) 或 (None, None)。"""
    nb = _valid_norm_bbox(norm_bbox)
    if nb is None:
        return None, None
    try:
        nx, ny, nw, nh = (float(v) for v in nb)
    except (TypeError, ValueError):
        return None, None
    # 夹到 [0,1] 且不越界：omni 回传的归一化框可能越界，area_ratio 落盘不能 > 1
    nx, ny = min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)
    nw, nh = min(max(nw, 0.0), 1.0 - nx), min(max(nh, 0.0), 1.0 - ny)
    fh, fw = frame.shape[:2]
    x, y = int(nx * fw), int(ny * fh)
    w, h = int(nw * fw), int(nh * fh)
    if w <= 0 or h <= 0:
        return None, None
    crop = _crop_with_padding(frame, (x, y, w, h), padding)
    if crop is None or crop.size == 0:
        return None, None
    return crop, (x, y, w, h)


def _sharpest_frame(frames: list[tuple[int, np.ndarray]]) -> np.ndarray | None:
    """从采样帧里挑最清晰的一帧（回退用整幅画面时选质量最好的送 omni）。"""
    best: np.ndarray | None = None
    best_score = -1.0
    for _idx, f in frames:
        s = compute_sharpness(f)
        if s > best_score:
            best_score, best = s, f
    return best


def _empty_result() -> dict:
    return {
        "detected": False,
        "description": None,
        "head_bbox": None,
        "primary_crop_b64": "",
        "primary_index": 0,
        "candidates": [],
        "refs_inconsistent": None,
        "warnings": [],
    }


async def observe_pet(
    medias: list[bytes],
    *,
    is_video: bool,
    grounding: bool,
    body_grounding: bool = True,
    max_frames: int = _MAX_VIDEO_FRAMES,
) -> dict:
    """上传媒体（图 1~3 张 / 视频 1 个）→ 门控选 ≤3 张同一只 crop → omni 一次性生成共性描述。无副作用。

    返回 ``{detected, description, head_bbox, primary_crop_b64, primary_index,
    candidates, refs_inconsistent, warnings}``。检测器（YOLO 只认猫/狗）框到并过门槛时用选出的
    crop；**框不到 / 门控全灭时回退**：把整幅画面交给 omni 聚焦最明显的一只动物描述（兼容兔/鸟/
    仓鼠等非猫狗物种）。回退路径 ``body_grounding=True`` 时让 omni 框出本体、裁本体作**一张参考 crop**
    （决策 D7）；关或框不出则不产参考 crop（candidates=[]）。仅当 omni 判画面确无动物时 detected=False。
    """
    detector = default_detector()
    if is_video:  # 视频恒单个
        sampled, fps = _decode_and_sample(medias[0], max_frames)
        selected, n_coincident = _select_video_crops(sampled, detector, int(fps) or 1)
        fallback_frame = _sharpest_frame(sampled)
    else:  # 图 1~3 张，每张各取最大 crop 过单图硬门槛
        selected = []
        for m in medias[:_MAX_SELECT]:
            img = cv2.imdecode(np.frombuffer(m, dtype=np.uint8), cv2.IMREAD_COLOR)
            one = _largest_pet_crop(img, detector) if img is not None else None
            if one is not None:
                selected.append(one)
        n_coincident = 0
        fallback_frame = _first_decodable(medias)

    # 检测器框到并过门槛 → 一次性把 ≤3 张 crop 送 omni 出共性描述（主路径不做本体 grounding）
    if selected:
        crops = [c["crop"] for c in selected]
        description, head_bboxes, refs_inconsistent, _ = await _omni_describe(
            crops, grounding=grounding
        )
        for c, hb in zip(selected, head_bboxes):
            c["head_bbox"] = hb
        # primary = gate_score 最高那张（图/多图无 gate_score → 恒第 0 张）
        primary_index = max(
            range(len(selected)), key=lambda i: selected[i].get("gate_score", 0.0)
        )
        primary = selected[primary_index]
        return {
            "detected": True,
            "description": description,
            "head_bbox": primary.get("head_bbox"),
            "primary_crop_b64": _jpeg_b64(primary["crop"]),
            "primary_index": primary_index,
            "candidates": [
                _candidate_out(c, head_bbox=c.get("head_bbox")) for c in selected
            ],
            "refs_inconsistent": refs_inconsistent,
            "warnings": _build_warnings(
                description, selected, refs_inconsistent, n_coincident
            ),
        }

    # 回退：检测器没框到猫/狗 / 门控全灭 → 让 omni 看整幅画面（并按 D7 尝试框本体）。
    if fallback_frame is None or fallback_frame.size == 0:
        return _empty_result()
    description, head_bboxes, _, body_bbox = await _omni_describe(
        [fallback_frame], grounding=grounding, whole_frame=True, body_grounding=body_grounding
    )
    has_animal = bool(
        str(description.get("species") or "").strip()
        or str(description.get("summary") or "").strip()
    )
    if not has_animal:
        return _empty_result()  # omni 确认画面无动物
    # D7：omni 框出了本体 → 裁本体作唯一参考 crop（非猫狗物种也能有参考图）
    body_crop, body_px = _crop_from_norm_bbox(fallback_frame, body_bbox)
    if body_crop is not None:
        fh, fw = fallback_frame.shape[:2]
        cand = {
            "track_id": None,
            "class_id": None,  # 非检测器候选 → species_guess 归"其他"
            "crop": body_crop,
            "conf": None,  # 无检测器置信
            "sharpness": float(compute_sharpness(body_crop)),
            "area_ratio": float(body_px[2] * body_px[3]) / float(max(fw * fh, 1)),
            "bbox": body_px,
            "frame_idx": None,
        }
        return {
            "detected": True,
            "description": description,
            # head_bbox 是相对整幅画面算的；primary/candidate 已裁成本体子图 → 整幅头框对它无意义，置 None
            "head_bbox": None,
            "primary_crop_b64": _jpeg_b64(body_crop),
            "primary_index": 0,
            "candidates": [_candidate_out(cand, head_bbox=None)],
            "refs_inconsistent": None,
            # 视频多 track 但主体被门控全灭而落到回退时，仍要透出 multiple_pets（别静默丢，决策 D8）
            "warnings": _build_warnings(description, [], None, n_coincident),
        }
    # 未开本体 grounding / omni 框不出 → 回传整幅画面，由前端裁剪器手动收窄，不产参考 crop
    return {
        "detected": True,
        "description": description,
        "head_bbox": head_bboxes[0] if head_bboxes else None,  # primary=整幅画面 → 头框仍相对整幅有效
        "primary_crop_b64": _jpeg_b64(fallback_frame),
        "primary_index": 0,
        "candidates": [],
        "refs_inconsistent": None,
        "warnings": _build_warnings(description, [], None, n_coincident),
    }
