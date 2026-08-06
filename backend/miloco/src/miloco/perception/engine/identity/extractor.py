"""人物片段抽取算法 — v4 全景图第 1 个黄色块。

三种输入入口,同输出格式 ``list[ScoredCandidate]``:

  - ``extract_from_image(image, detector, reid_extractor=...)`` : 单张图,跳过
        轨迹关联;每个入选 body 都现场调 ``HumanReID.extract_feature`` 算 emb
  - ``extract_from_video(video_bytes, detector,
        deep_sort_tracker_factory, max_frames)`` : 视频,DeepSORT 关联(主流程同款),
        每个 track 的 emb 直接读 ``tracker.get_track_embedding``(零额外推理)
  - ``extract_from_pool(cluster_candidates)``    : 从陌生人池 fetch 拿回的
        ClusterCandidate 直接评分,emb 已含

每张 ScoredCandidate = bbox + body crop + (可选) face crop + 元数据 + 评分 +
phash + reid_emb(全部入口都给,空时为 None),给下游 M5 筛选算法挑 topk 入库用。

⚠️ 关于"零额外推理"硬约束的边界:**M4 抽取算法不在约束范围内**。§6.1 约束
针对的是 ``tier_u.py`` 内陌生人池的代码路径(给已有跟踪 emb 兜底,严禁重抽)。
M4 处理的是用户主动上传的图——没有"跟踪侧已经算好的 emb"可复用,只能现场抽,
这是合法且必要的。视频路径仍然零额外推理(DeepSORT 关联本就要算 emb,M4 复用)。

评分公式(v4 §2.2.3):
    score = log(area_ratio + 1) × detector_conf × sharpness_norm × face_bonus
    face_bonus = 1.2 if 单帧关联到 face else 1.0

质量 Gate(与陌生人池同口径,与 TierC 同阈值,v4 §2.2.3):
    area_ratio ≥ 0.05 / aspect ∈ [0.20, 2.5] / sharpness ≥ 50 / detector_conf ≥ 0.4

视频路径抽帧策略(v4 §2.2.1):
    先 3× max_frames 粗均匀采样 → 逐帧 detector 过滤"无 body"帧 → 剩余有效帧
    子序列均匀抽 max_frames 进下游。
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.identity._image_utils import (
    compute_sharpness as _compute_sharpness,
)
from miloco.perception.engine.identity._image_utils import (
    phash as _phash,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Quality Gate 阈值(与 TierC / TierU 同口径,本期不暴露 yaml)
# =============================================================================

_GATE_AREA_RATIO_MIN = 0.05
_GATE_ASPECT_MIN = 0.20
_GATE_ASPECT_MAX = 2.5
_GATE_SHARPNESS_MIN = 50.0
_GATE_DET_CONF_MIN = 0.4

# 评分用 sharpness 归一化的"参考值"——超过即视为 1.0,低于按比例
_SHARPNESS_NORM_REF = 300.0

# face 关联加成系数
_FACE_BONUS = 1.2

# 视频抽帧:粗采样倍数(N × max_frames 张先 detector,过滤后均匀抽 max_frames)
_VIDEO_COARSE_SAMPLE_MULTIPLIER = 3


# =============================================================================
# 数据结构
# =============================================================================


@dataclass
class ScoredCandidate:
    """一张打完分的候选样本。

    M5 筛选算法的输入单元:按 score 降序排列后,在 pHash / 时间间隔 / ReID 维度上
    挑 topk 入库。注册完成时直接喂进 BodySample → IdentityLibrary.add_tier_a_samples_batch。
    """

    body_crop: NDArray[np.uint8]
    face_crop: NDArray[np.uint8] | None
    score: float
    bbox_xyxy: tuple[int, int, int, int]
    frame_index: int
    captured_at: float
    track_id: int | None              # 视频 / 池路径有,图像路径无
    cluster_id: str | None            # 池路径有,其他路径 None
    cam_id: str | None
    detector_conf: float
    sharpness: float
    reid_embedding: NDArray[np.float32] | None = None  # 池路径已知;图像/视频路径留 None
    phash: int = 0                    # 64-bit pHash(M5 筛选用)
    # 视频路径 extract_from_video 末尾会跑 face 跨帧分发 (top-K body 拿 top-K
    # face 按 sharpness 全局排序后分配), 导致 cand.face_crop 不一定是该 body
    # frame_index 同帧的 face。same_frame_face_crop 永久保存"body 那一帧关联到
    # 的 face crop" (没关联到则 None), 给 select_topk_with_frontal_seed 用来:
    #   1) 判 frontal 候选 (same_frame_face_crop 的 w/h ∈ [0.70, 0.80))
    #   2) seed 选定后覆盖 face_crop = same_frame_face_crop, 让号码图首位 cand
    #      的 body 跟 face 来自同一帧、朝向一致 (用户实际诉求)
    # image / pool 路径 cand 没经过跨帧分发, face_crop 本身就是同帧, 该字段写
    # None 即可 (helper 检 None 退化跳过对齐逻辑)。
    same_frame_face_crop: NDArray[np.uint8] | None = None


# =============================================================================
# 通用辅助
# =============================================================================


def _crop_with_padding(
    frame: NDArray[np.uint8],
    bbox_xywh: tuple[int, int, int, int],
    padding_ratio: float = 0.05,
) -> NDArray[np.uint8] | None:
    """按 bbox + padding 裁图。bbox = (x, y, w, h)。"""
    x, y, w, h = bbox_xywh
    if w <= 0 or h <= 0:
        return None
    H, W = frame.shape[:2]
    px = int(round(w * padding_ratio))
    py = int(round(h * padding_ratio))
    x1 = max(0, x - px)
    y1 = max(0, y - py)
    x2 = min(W, x + w + px)
    y2 = min(H, y + h + py)
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2].copy()


def _passes_quality_gate(
    *, area_ratio: float, aspect: float, sharpness: float, detector_conf: float,
) -> bool:
    """与 TierU / TierC 同口径的 4 项物理过滤。"""
    return (
        area_ratio >= _GATE_AREA_RATIO_MIN
        and _GATE_ASPECT_MIN <= aspect <= _GATE_ASPECT_MAX
        and sharpness >= _GATE_SHARPNESS_MIN
        and detector_conf >= _GATE_DET_CONF_MIN
    )


def _score_candidate(
    *,
    area_ratio: float,
    detector_conf: float,
    sharpness: float,
    has_face: bool,
) -> float:
    """v4 §2.2.3 评分公式。

    sharpness 归一化到 ``[0, 1]``:在 ``_SHARPNESS_NORM_REF`` 处饱和。
    """
    sharp_norm = min(1.0, max(0.0, sharpness / _SHARPNESS_NORM_REF))
    face_bonus = _FACE_BONUS if has_face else 1.0
    return math.log(area_ratio + 1.0) * detector_conf * sharp_norm * face_bonus


def _associate_face_to_body(
    body_det: Any,
    face_dets: list[Any],
    matcher: Any | None = None,
) -> Any | None:
    """单帧 body 跟该帧 face 列表关联,取最匹配的 1 个 face(若有)。

    用 ``face_person_match.FacePersonMatcher``(IoA + 匈牙利,既有实现);单 body
    场景下退化为"找 IoA 最大且过阈值的 face"。无 matcher 时本函数 fallback 简单
    IoA 判断,主要给单测路径用——业务路径都注入 matcher。
    """
    if not face_dets:
        return None
    if matcher is not None:
        # FacePersonMatcher 期望 list[Detection] 输入
        matches = matcher.match(face_dets, [body_det])
        if matches:
            face_idx = matches[0].face_idx
            return face_dets[face_idx]
        return None
    # 无 matcher fallback:简单 IoA(face ∩ body / face 面积)阈值
    bx1, by1, bx2, by2 = body_det.xyxy
    best_ioa, best_face = 0.0, None
    for fd in face_dets:
        fx1, fy1, fx2, fy2 = fd.xyxy
        ix1, iy1 = max(fx1, bx1), max(fy1, by1)
        ix2, iy2 = min(fx2, bx2), min(fy2, by2)
        if ix1 >= ix2 or iy1 >= iy2:
            continue
        inter = (ix2 - ix1) * (iy2 - iy1)
        face_area = max(1, (fx2 - fx1) * (fy2 - fy1))
        ioa = inter / face_area
        if ioa > best_ioa:
            best_ioa, best_face = ioa, fd
    return best_face if best_ioa >= 0.5 else None


# =============================================================================
# 入口 1:从图像抽取(图像 = 用户已预先确认目标)
# =============================================================================


def extract_from_image(
    image: NDArray[np.uint8],
    *,
    detector: Any,
    cam_id: str | None = None,
    captured_at: float = 0.0,
    face_matcher: Any | None = None,
    reid_extractor: Any | None = None,
) -> list[ScoredCandidate]:
    """单张图入口:跳过轨迹关联,每个 body bbox 直接成候选。

    多人合影场景:多 body bbox → 多 ScoredCandidate,各自带打分;上层(§3)
    多人歧义路径继续处理。

    Args:
        reid_extractor: ``HumanReID`` 实例(可选)。传入则对每个入选 body crop 调
            ``extract_feature`` 算 128-dim emb 存进 ScoredCandidate.reid_embedding,
            给 M5 筛选的 ReID 备路径用,也给后续身份库 .npy 落盘用;未传则 emb=None。
            注:图像路径没有"跟踪侧已算的 emb"可复用,只能现场抽——不违反 §6.1
            零额外推理硬约束(那条只针对陌生人池 tier_u.py 内代码)。
    """
    if image is None or image.size == 0:
        return []
    dets = detector.detect(image)
    # 提前从 detector 模块拿 Detection class 以判断 class_id
    Detection = type(dets[0]) if dets else None
    if Detection is None:
        return []

    body_dets = [d for d in dets if d.class_id == Detection.CLASS_HUMAN]
    face_dets = [d for d in dets if d.class_id == Detection.CLASS_FACE]
    if not body_dets:
        return []

    out: list[ScoredCandidate] = []
    H, W = image.shape[:2]
    frame_area = float(H * W) if H > 0 and W > 0 else 1.0

    for body in body_dets:
        crop = _crop_with_padding(image, body.bbox)
        if crop is None:
            continue
        ch, cw = crop.shape[:2]
        if ch <= 0 or cw <= 0:
            continue
        area_ratio = (body.w * body.h) / frame_area
        aspect = body.w / max(body.h, 1)
        sharp = _compute_sharpness(crop)
        if not _passes_quality_gate(
            area_ratio=area_ratio, aspect=aspect,
            sharpness=sharp, detector_conf=body.confidence,
        ):
            continue
        matched_face = _associate_face_to_body(body, face_dets, face_matcher)
        face_crop = None
        if matched_face is not None:
            face_crop = _crop_with_padding(image, matched_face.bbox)
        score = _score_candidate(
            area_ratio=area_ratio,
            detector_conf=body.confidence,
            sharpness=sharp,
            has_face=matched_face is not None,
        )
        # 图像路径:现场调 HumanReID.extract_feature 算 emb(不违反 §6.1)
        emb = None
        if reid_extractor is not None:
            try:
                emb = reid_extractor.extract_feature(crop)
            except Exception:  # noqa: BLE001
                logger.warning("ReID extract_feature 失败,该 candidate emb 留空",
                                exc_info=True)
        out.append(ScoredCandidate(
            body_crop=crop,
            face_crop=face_crop,
            score=score,
            bbox_xyxy=body.xyxy,
            frame_index=0,
            captured_at=captured_at,
            track_id=None,          # 图像路径无轨迹概念
            cluster_id=None,
            cam_id=cam_id,
            detector_conf=float(body.confidence),
            sharpness=sharp,
            reid_embedding=emb,
            phash=_phash(crop),
        ))

    out.sort(key=lambda c: c.score, reverse=True)
    return out


# =============================================================================
# 入口 2:从视频抽取(detector 锚定抽帧 + SORT 关联)
# =============================================================================


def _sample_video_frames(
    path: str, max_frames: int,
) -> tuple[list[tuple[int, NDArray[np.uint8]]], float]:
    """从视频均匀采样 max_frames 帧。返回 ([(frame_index, frame)], fps)。

    fps 来自 ``cv2.CAP_PROP_FPS``,极少数无 fps 元信息的视频/流兜底 30.0
    (中位手机视频 fps,跟 captured_at 时间戳计算同步)。

    注: 30.0 fallback 假设源头是**手机视频**(主流 30/60 fps)。若未来扩到监控
    摄像头注册路径 (典型 1-5 fps), 用 30.0 会把相邻 0.2s 的真实间隔算成
    0.033s, select_topk 的 time_gap_sec_min=1.0 会让真实"差 1 秒"的帧错过
    去重检查, 反而过严。届时调用方应改成显式传 fps 而非靠本函数兜底。
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return [], 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        fps = 30.0  # 元信息缺失兜底, 假设手机视频; 见函数 docstring 注解
    out: list[tuple[int, NDArray[np.uint8]]] = []
    try:
        if total <= 0:
            # 流式 fallback:无 total 逐帧读
            i = 0
            while len(out) < max_frames:
                ret, fr = cap.read()
                if not ret:
                    break
                out.append((i, fr))
                i += 1
        else:
            indices = (
                list(range(total)) if total <= max_frames
                else np.linspace(0, total - 1, max_frames).astype(int).tolist()
            )
            seen: set[int] = set()
            for idx in indices:
                if idx in seen:
                    continue
                seen.add(idx)
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, fr = cap.read()
                if ret:
                    out.append((idx, fr))
    finally:
        cap.release()
    return out, fps


def extract_from_video(
    video_bytes_or_path: bytes | str | Path,
    *,
    detector: Any,
    deep_sort_tracker_factory: Callable[[], Any],
    max_frames: int = 60,
    cam_id: str | None = None,
    captured_at_base: float = 0.0,
    face_matcher: Any | None = None,
    min_track_hits: int = 3,
) -> dict[int, list[ScoredCandidate]]:
    """视频路径:抽帧 → DeepSORT 关联 → 评分。

    与主流程 (M2) 同款 DeepSORT,关联阶段顺带算 ReID emb,本函数从
    ``tracker.get_track_embedding(tid)`` 直接读末尾元素塞进 ScoredCandidate
    (**零额外推理**——DeepSORT 已经算了,M4 只是把内存里现有的 numpy
    引用导出来)。这跟陌生人池 (M3) 的零额外推理设计同源,链路一致。

    Args:
        deep_sort_tracker_factory: 返回 ``DeepSortTracker`` 实例(新建独立实例,
            避免污染主流程 track_id 空间)。

    Returns:
        ``{track_id: list[ScoredCandidate]}``——按 track 分组,**含 ≥3 帧的 track**
        (短于此视为噪声丢弃)。上层判断 track 数 ≥ 2 时走多人歧义路径。
    """
    # 1) 物化为本地文件(兼容 bytes / path 两种入参)
    cleanup_path: str | None = None
    if isinstance(video_bytes_or_path, (bytes, bytearray)):
        tf = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        try:
            tf.write(video_bytes_or_path)
        finally:
            tf.close()
        video_path = tf.name
        cleanup_path = video_path
    else:
        video_path = str(video_bytes_or_path)

    try:
        # 2) 粗采样 N × max_frames,逐帧 detector 过滤无 body 帧
        coarse, video_fps = _sample_video_frames(
            video_path, max_frames * _VIDEO_COARSE_SAMPLE_MULTIPLIER,
        )
        if not coarse:
            return {}

        Detection = None
        valid_frames: list[tuple[int, NDArray[np.uint8], list[Any], list[Any]]] = []
        for fidx, frame in coarse:
            dets = detector.detect(frame)
            if Detection is None and dets:
                Detection = type(dets[0])
            if Detection is None:
                continue
            body_dets = [d for d in dets if d.class_id == Detection.CLASS_HUMAN]
            face_dets = [d for d in dets if d.class_id == Detection.CLASS_FACE]
            if body_dets:
                valid_frames.append((fidx, frame, body_dets, face_dets))

        if not valid_frames:
            return {}

        # 3) 有效帧子序列里均匀抽 max_frames
        if len(valid_frames) > max_frames:
            picks = np.linspace(0, len(valid_frames) - 1, max_frames).astype(int).tolist()
            valid_frames = [valid_frames[i] for i in picks]

        # 4) DeepSORT 关联:factory 起独立 tracker 实例(不污染主流程 track_id 空间)
        #    DeepSortTracker.update 内部:跑 detector + DeepSORT 关联 + 每检测框
        #    算 ReID emb 存进 Track.features deque。我们直接读
        #    get_track_embedding(tid) 拿末尾元素——零额外推理。
        tracker = deep_sort_tracker_factory()
        per_track: dict[int, list[ScoredCandidate]] = {}
        # per-track 跨帧 face 池(face 解绑 body 帧):每 track 累积所有跟 body 关联
        # 到的 face_det,主循环结束后按 face crop sharpness 排序,top-K 重新分发给该
        # track 的 top-K body candidates。**face 可以跟 body 不同帧**——避免"body 最
        # 清晰的那帧人脸正好侧脸/被挡/糊"导致 face 质量低。
        per_track_face_pool: dict[int, list[tuple[NDArray[np.uint8], float]]] = {}
        for fidx, frame, body_dets, face_dets in valid_frames:
            tracker.update(frame)
            tracking_results = tracker.get_tracking_results()
            for tr in tracking_results:
                if tr.get("class_id") != Detection.CLASS_HUMAN:
                    continue
                # coasting(本帧未匹配, bbox 停在上一次真匹配的位置)不裁图: 人已经不在
                # 那儿了, 裁出的背景/他人图会进该 track 的 top-K 候选、经注册写进身份库。
                # 下面的质量门拦不住它——置信度同样停在最后一次真匹配的值(通常过阈值),
                # 背景裁图清晰度也不低; 打分公式同样没有"本帧是否真匹配"这一项。
                # 与 IdentityEngine 各消费闸同口径。
                # 召回代价有界, 但上界跟随配置而非代码常量: 本路径 tracker 按 fps=1 建,
                # 每个关联缺口最多少 sec_to_frames(deep_sort.max_age_sec, 1) 张候选
                # (当前 yaml 配 2.0 → 2 张), 少掉的正是幻影框那些。调大该 yaml 项会
                # 同比放宽这个上界, 需连同 min_track_hits 的下限一起评估。
                # 缺字段按真检测兜底(同全链路 fail-open 口径)。
                if not tr.get("detected_this_frame", True):
                    continue
                tid = int(tr["id"])
                bbox_xywh = tr["bbox"]
                bbox_xyxy = tr["xyxy"]
                # 缺 confidence 按满置信兜底,口径同 IdentityEngine 侧:tracker 决定
                # 维持该 track 即已过检测阈值,"未知"不等于"零信";取 0.0 会被下面的
                # 质量门全数拒掉。
                conf = float(tr.get("confidence", 1.0))
                crop = _crop_with_padding(frame, bbox_xywh)
                if crop is None:
                    continue
                fh, fw = frame.shape[:2]
                area_ratio = (bbox_xywh[2] * bbox_xywh[3]) / max(1.0, float(fh * fw))
                aspect = bbox_xywh[2] / max(1, bbox_xywh[3])
                sharp = _compute_sharpness(crop)
                if not _passes_quality_gate(
                    area_ratio=area_ratio, aspect=aspect,
                    sharpness=sharp, detector_conf=conf,
                ):
                    continue
                # face 关联用本帧 face_dets + 这个 body bbox 构造 pseudo-Detection。
                # 同帧关联到的 face 进 track 的 face pool(供主循环后跨帧 top-K 挑选);
                # has_face 仍参与 score(标记"该 body 帧能看到脸"作为质量加成),但
                # ScoredCandidate.face_crop **暂不填**,等主循环结束后从 pool 里挑。
                pseudo_body = type("PseudoBody", (), {
                    "xyxy": bbox_xyxy, "bbox": bbox_xywh,
                })()
                matched_face = _associate_face_to_body(pseudo_body, face_dets, face_matcher)
                has_face = matched_face is not None
                same_frame_face = None  # 给 select_topk_with_frontal_seed 用 (方案 A)
                if has_face:
                    face_crop_this_frame = _crop_with_padding(frame, matched_face.bbox)
                    if face_crop_this_frame is not None and face_crop_this_frame.size > 0:
                        per_track_face_pool.setdefault(tid, []).append(
                            (face_crop_this_frame, _compute_sharpness(face_crop_this_frame))
                        )
                        # 同帧 face 永久保存到 cand, 后续跨帧分发不动它。让 V6b helper
                        # 选 frontal seed 时按 body 帧朝向判 (而非按被跨帧覆盖的 face_crop),
                        # 并在 seed 选定后覆盖 face_crop 回同帧, 保证 body/face 朝向一致。
                        same_frame_face = face_crop_this_frame
                # DeepSORT 关联阶段已算好的 emb,直接读 deque 末尾(零额外推理)
                emb = None
                if hasattr(tracker, "get_track_embedding"):
                    emb = tracker.get_track_embedding(tid)
                score = _score_candidate(
                    area_ratio=area_ratio,
                    detector_conf=conf,
                    sharpness=sharp,
                    has_face=has_face,
                )
                per_track.setdefault(tid, []).append(ScoredCandidate(
                    body_crop=crop,
                    face_crop=None,  # 主循环后跨帧 top-K 分发
                    score=score,
                    bbox_xyxy=bbox_xyxy,
                    frame_index=fidx,
                    # 用真实 fps 算时间戳, 不能硬编码 0.1 s/帧 (= 假设 10 fps)。
                    # 60 fps 视频用 0.1 会把相邻 0.35s 的两帧算成 2.1s, 让
                    # select_topk 的 time_gap_sec_min=1.0 检查失效, 同人相邻
                    # 帧通过去重 → topk 选出来"看着差不多"的 body。
                    captured_at=captured_at_base + fidx / max(1.0, video_fps),
                    track_id=tid,
                    cluster_id=None,
                    cam_id=cam_id,
                    detector_conf=conf,
                    sharpness=sharp,
                    reid_embedding=emb,
                    phash=_phash(crop),
                    same_frame_face_crop=same_frame_face,
                ))

        # 5) 过滤命中数 < min_track_hits 的 track(1-2 帧噪声丢弃)
        per_track = {tid: cands for tid, cands in per_track.items()
                     if len(cands) >= min_track_hits}
        # 每个 track 按 score 降序
        for cands in per_track.values():
            cands.sort(key=lambda c: c.score, reverse=True)

        # 6) face 跨帧分发:每 track 把 face pool 按 sharpness 降序,top-K 配给该 track
        # 的 top-K body candidate(face 数 ≤ body 数;face quality 排第 i 配 body
        # score 排第 i)。剩余 body 保持 face_crop=None。
        # 设计理由(用户提:face 不能严格与 body 帧对应,否则 body 最清晰那帧人脸
        # 可能侧脸/被挡):face 来自同 track 所有帧的全局质量 top-K,跨帧但仍同人。
        for tid, cands in per_track.items():
            face_pool = per_track_face_pool.get(tid, [])
            face_pool.sort(key=lambda fc_s: fc_s[1], reverse=True)
            k = min(len(cands), len(face_pool))
            for i in range(k):
                cands[i].face_crop = face_pool[i][0]
        return per_track
    finally:
        if cleanup_path:
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass


# =============================================================================
# 入口 3:从陌生人池 ClusterCandidate 抽取(无附件路径:跳过 detector + 跟踪重跑)
# =============================================================================


# L2 不足时从 L1 补足的最小目标数(确保用户注册有 ≥ N 张差异化候选可挑)
_POOL_MIN_CANDIDATES = 3


def _crop_entry_to_scored(crop_entry: Any, cluster_id: str) -> ScoredCandidate | None:
    """单个 CropEntry → ScoredCandidate;过 quality gate 失败返 None。

    extract_from_pool 主循环 + L1 fallback 都用本 helper,逻辑统一。
    """
    body = crop_entry.body_crop
    if body is None or body.size == 0:
        return None
    ch, cw = body.shape[:2]
    if ch <= 0 or cw <= 0:
        return None
    # 池里的 CropEntry 没存 frame 全尺寸,area_ratio 用"crop 面积 vs 假设 1280×720
    # 全屏"粗估(M5 筛选主要看 pHash + 时间 + ReID,score 是排序参考而非硬阈值)
    assumed_frame_area = 1280.0 * 720.0
    area_ratio = (crop_entry.bbox_xyxy[2] - crop_entry.bbox_xyxy[0]) * \
                 (crop_entry.bbox_xyxy[3] - crop_entry.bbox_xyxy[1]) / assumed_frame_area
    aspect = cw / max(1, ch)
    sharp = crop_entry.sharpness
    if not _passes_quality_gate(
        area_ratio=max(area_ratio, _GATE_AREA_RATIO_MIN),
        aspect=aspect, sharpness=sharp, detector_conf=crop_entry.detector_conf,
    ):
        return None
    score = _score_candidate(
        area_ratio=max(area_ratio, _GATE_AREA_RATIO_MIN),
        detector_conf=crop_entry.detector_conf,
        sharpness=sharp,
        has_face=crop_entry.face_crop is not None,
    )
    return ScoredCandidate(
        body_crop=body,
        face_crop=None,  # 主循环末跨帧 top-K 分发,这里先置 None
        score=score,
        bbox_xyxy=crop_entry.bbox_xyxy,
        frame_index=crop_entry.frame_index,
        captured_at=crop_entry.captured_at,
        track_id=crop_entry.track_id,
        cluster_id=cluster_id,
        cam_id=crop_entry.cam_id,
        detector_conf=crop_entry.detector_conf,
        sharpness=sharp,
        reid_embedding=crop_entry.reid_embedding,
        phash=_phash(body),
    )


def extract_from_pool(
    cluster_candidates: list[Any],
) -> dict[str, list[ScoredCandidate]]:
    """无附件路径:从 ``TierUPool.fetch`` 拿回的 ClusterCandidate 转 ScoredCandidate。

    使用 ``cluster.all_l2_crops`` 全集(精华,过 quality gate),如果 L2 数 < 3
    再用 ``cluster.all_l1_crops`` 按 sharpness 降序补足到 3 张(L1 raw 仍要过 quality gate)。
    face_crop 不绑 body 同帧——同 cluster 内 face 跨帧按 sharpness top-K 分发到
    top-K body candidate(跟视频路径 extract_from_video 一致;原因:body 最清晰那
    帧人脸可能侧脸/被挡)。

    Returns:
        ``{cluster_id: list[ScoredCandidate]}``,按 cluster 分组,每组按 score 降序。
    """
    out: dict[str, list[ScoredCandidate]] = {}
    for cluster in cluster_candidates:
        cluster_id = cluster.cluster_id
        cands: list[ScoredCandidate] = []
        seen_ids: set[int] = set()

        # 1) 用全 L2 集(覆盖 representative + per_cam_representative + 其他)
        l2_crops = list(getattr(cluster, "all_l2_crops", None) or [])
        if not l2_crops:
            # 老代码兼容:cluster 没有 all_l2_crops 字段时退回 representative + per_cam
            l2_crops = [cluster.representative_crop, *cluster.per_cam_representative.values()]
        face_pool: list[tuple[Any, float]] = []  # (face_crop_array, sharpness)
        for crop_entry in l2_crops:
            if id(crop_entry) in seen_ids:
                continue
            seen_ids.add(id(crop_entry))
            sc = _crop_entry_to_scored(crop_entry, cluster_id)
            if sc is None:
                continue
            cands.append(sc)
            if crop_entry.face_crop is not None and crop_entry.face_crop.size > 0:
                face_pool.append(
                    (crop_entry.face_crop, _compute_sharpness(crop_entry.face_crop))
                )

        # 2) L2 不足 _POOL_MIN_CANDIDATES 张时,从 L1 按 sharpness 降序补
        if len(cands) < _POOL_MIN_CANDIDATES:
            l1_crops = list(getattr(cluster, "all_l1_crops", None) or [])
            l1_crops.sort(key=lambda c: c.sharpness, reverse=True)
            for crop_entry in l1_crops:
                if id(crop_entry) in seen_ids:
                    continue
                seen_ids.add(id(crop_entry))
                sc = _crop_entry_to_scored(crop_entry, cluster_id)
                if sc is None:
                    continue
                cands.append(sc)
                if crop_entry.face_crop is not None and crop_entry.face_crop.size > 0:
                    face_pool.append(
                        (crop_entry.face_crop, _compute_sharpness(crop_entry.face_crop))
                    )
                if len(cands) >= _POOL_MIN_CANDIDATES:
                    break

        if not cands:
            continue

        # 3) 按 score 降序,然后跨帧 top-K face 分发(face 不绑 body 同帧)
        cands.sort(key=lambda c: c.score, reverse=True)
        face_pool.sort(key=lambda x: x[1], reverse=True)
        k = min(len(cands), len(face_pool))
        for i in range(k):
            cands[i].face_crop = face_pool[i][0]
        out[cluster_id] = cands
    return out
