# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""宠物外观观察（pet:observe）—— 上传图/视频，选出最优宠物 crop，让 omni 按维度生成外观描述。

设计（见 .wsh_cc/宠物识别方案.md §2.3(2) / §3a.6，仅设计参考）：
- **选 crop**：单图取最大 CAT/DOG 框 + 扩边 5%；视频用**无 ReID 的 SortTracker** 把同相机
  多只宠物分到各自 track，每 track 选综合质量（置信×清晰度×面积）最优一张。送 omni 的是
  这张**静态 crop**（视频也只送选出的最优帧，不必把视频喂 omni）。
- **detector / tracker 每次新建**（镜像人体注册的工厂模式；tracker 带状态必须 fresh）；
  不接 IdentityEngine。喂 SortTracker 的是预过滤的宠物检测（不依赖将被移除的类别开关来选类）。
- **回退**：检测器（YOLO 只认猫/狗）框不到时，把整幅画面交给 omni 聚焦最明显的一只动物
  描述——兼容兔/鸟/仓鼠等非猫狗物种；omni 判定确无动物才算未检出。
- **grounding**：开关开时 prompt 追加要头部归一化 bbox（相对选出的 crop），解析回传。

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

OBSERVE_SYSTEM_PROMPT = """你是宠物外貌观察助手。根据给定的宠物图片，客观描述这只宠物用于日后在监控画面中区分和称呼它的**稳定外观特征**。
只描述可见且长期稳定的特征；不描述背景、动作、情绪；把握不大的维度（如品种）宁可留空也不要猜。
严格输出如下 JSON（不要输出多余文字、不要 markdown 代码围栏）：
{"species":"按实际动物填，如 猫 / 狗 / 兔 / 仓鼠 / 鸟 / 龟 等","breed":"<高把握才填，否则空字符串>","size_build":"如 中等体型、体态壮实","coat_color_pattern":"如 黑色 / 橘白双色 / 狸花","coat_length_texture":"如 短毛顺滑 / 长毛蓬松","distinctive_markings":["如 尾尖一撮白","左耳缺口"],"accessories":"<可变动配饰，如项圈颜色；无则空字符串>","summary":"一句规范化外观句，有把握判断品种时把品种写进去（如 英短 / 柯基 / 金毛），拿不准则不写品种；如 中等体型的黑色短毛英短猫，尾巴尖有一撮白毛"}"""

_GROUNDING_CLAUSE = """
另外，在上述 JSON 中**额外**加一个键 "head_bbox"：宠物头部区域相对本图的归一化边界框 [x, y, w, h]（四个 0~1 之间的小数，x/y 为左上角，w/h 为宽高占比）；头部不可见则置 null。"""

# 回退路径（检测器没框到猫/狗时）用整幅画面，让 omni 自己找最明显的动物——
# 兼容兔/鸟/仓鼠等 YOLO 不识别的物种，并给出"确无动物"的信号。
_WHOLE_FRAME_CLAUSE = """
注意：这是未裁剪的整幅画面，可能含背景或多只动物。请只聚焦画面中**最明显的一只动物**来描述（宠物不限于猫狗）；若画面中确实没有任何动物，把 species 与 summary 都置为空字符串。"""


def default_detector() -> Detector:
    """新建一个 YOLO Detector（走 ``models_dir/det_4C.onnx``）。

    镜像人体注册 ``person/router._load_detector`` 的口径：每次调用新建（observe 低频）。
    """
    det_path = get_settings().directories.models_dir / "det_4C.onnx"
    return Detector(model_path=str(det_path), conf_threshold=0.4, use_gpu=False)


def _jpeg_b64(crop: np.ndarray, quality: int = 85) -> str:
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode() if ok else ""


def _largest_pet_crop(frame: np.ndarray, detector: Any) -> dict | None:
    """单图：取面积最大的 CAT/DOG 框、扩边 5% 裁出。返回 {class_id, crop} 或 None。"""
    dets = [d for d in detector.detect_pets(frame) if d.class_id in _PET_CLASS_IDS]
    if not dets:
        return None
    best = max(dets, key=lambda d: d.w * d.h)
    crop = _crop_with_padding(frame, (best.x, best.y, best.w, best.h), _PADDING_RATIO)
    if crop is None or crop.size == 0:
        return None
    return {"track_id": None, "class_id": best.class_id, "crop": crop}


def _video_pet_crops(
    frames: list[tuple[int, np.ndarray]], detector: Any, fps: int
) -> list[dict]:
    """视频：无 ReID 的 SORT 把多只宠物分 track，每 track 选综合质量最优一张 crop。

    喂 SortTracker 的是**预过滤的宠物检测**（detect_pets），并设 ``track_human_only=False``
    让其 else 分支保留 CAT/DOG——即便日后该开关被移除（只跟踪所喂检测），逻辑仍成立。
    返回按质量降序的 [{track_id, class_id, crop, score}]。
    """
    from miloco.perception.engine.identity.sort import SortConfig, SortTracker

    tracker = SortTracker(
        config=SortConfig(track_human_only=False),
        detector=detector,
        fps=max(1, fps),
    )
    best_by_track: dict[int, dict] = {}
    for _idx, frame in frames:
        pet_dets = [d for d in detector.detect_pets(frame) if d.class_id in _PET_CLASS_IDS]
        tracker.update_with_detections(frame, pet_dets)
        for tr in tracker.get_tracking_results():
            if tr["class_id"] not in _PET_CLASS_IDS:
                continue
            crop = _crop_with_padding(frame, tr["bbox"], _PADDING_RATIO)
            if crop is None or crop.size == 0:
                continue
            bw, bh = tr["bbox"][2], tr["bbox"][3]
            score = float(tr["confidence"]) * compute_sharpness(crop) * float(bw * bh)
            cur = best_by_track.get(tr["id"])
            if cur is None or score > cur["score"]:
                best_by_track[tr["id"]] = {
                    "track_id": tr["id"],
                    "class_id": tr["class_id"],
                    "crop": crop,
                    "score": score,
                }
    return sorted(best_by_track.values(), key=lambda c: c["score"], reverse=True)


async def _omni_describe(
    crop: np.ndarray, *, grounding: bool, whole_frame: bool = False
) -> tuple[dict, Any]:
    """把一张图送 omni，返回 (结构化描述 dict, head_bbox|None)。

    ``whole_frame=True`` 用于回退：传的是未裁剪整幅画面，让 omni 聚焦最明显的一只动物
    （兼容非猫狗物种），并允许其在无动物时回空。
    """
    config = resolve_live_omni_config(OmniConfig())
    system_prompt = (
        OBSERVE_SYSTEM_PROMPT
        + (_WHOLE_FRAME_CLAUSE if whole_frame else "")
        + (_GROUNDING_CLAUSE if grounding else "")
    )
    payload = {
        "system_prompt": system_prompt,
        "user_content": "请观察画面中的动物，按要求输出 JSON。",
        "crops": [{"media_type": "image/jpeg", "data": _jpeg_b64(crop)}],
    }
    raw = await call_omni(payload, config, type="on_demand")
    content = response_parser.parse_query_response(raw)
    data = json.loads(response_parser.extract_json(content))
    head_bbox = data.pop("head_bbox", None) if grounding else None
    return data, head_bbox


def _candidate_out(c: dict) -> dict:
    return {
        "track_id": c.get("track_id"),
        "species_guess": _SPECIES_BY_CLASS.get(c["class_id"], "其他"),
        "crop_b64": _jpeg_b64(c["crop"]),
    }


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
        "candidates": [],
    }


async def observe_pet(
    media: bytes, *, is_video: bool, grounding: bool, max_frames: int = _MAX_VIDEO_FRAMES
) -> dict:
    """上传媒体 → 选最优宠物 crop → omni 生成外观描述。无副作用。

    返回 ``{detected, description, head_bbox, primary_crop_b64, candidates}``。
    检测器（YOLO 只认猫/狗）框到时用最优 crop；**框不到时回退**：把整幅画面交给
    omni 聚焦最明显的一只动物描述（兼容兔/鸟/仓鼠等非猫狗物种），仅当 omni 判定画面
    确无动物时才 ``detected=False``。
    """
    detector = default_detector()
    if is_video:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        try:
            tmp.write(media)
            tmp.close()
            sampled, fps = _sample_video_frames(tmp.name, max_frames=max_frames)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        crops = _video_pet_crops(sampled, detector, int(fps) or 1)
        fallback_frame = _sharpest_frame(sampled)
    else:
        img = cv2.imdecode(np.frombuffer(media, dtype=np.uint8), cv2.IMREAD_COLOR)
        one = _largest_pet_crop(img, detector) if img is not None else None
        crops = [one] if one is not None else []
        fallback_frame = img

    # 检测器框到了猫/狗 → 用最优 crop 描述
    if crops:
        primary = crops[0]
        description, head_bbox = await _omni_describe(
            primary["crop"], grounding=grounding
        )
        return {
            "detected": True,
            "description": description,
            "head_bbox": head_bbox,
            "primary_crop_b64": _jpeg_b64(primary["crop"]),
            "candidates": [_candidate_out(c) for c in crops],
        }

    # 回退：检测器没框到猫/狗（其他物种 / 角度 / 遮挡）→ 让 omni 看整幅画面。
    # primary_crop 回传整幅画面，由前端裁剪器手动收窄；grounding 头部框相对整幅画面。
    if fallback_frame is None or fallback_frame.size == 0:
        return _empty_result()
    description, head_bbox = await _omni_describe(
        fallback_frame, grounding=grounding, whole_frame=True
    )
    has_animal = bool(
        str(description.get("species") or "").strip()
        or str(description.get("summary") or "").strip()
    )
    if not has_animal:
        return _empty_result()  # omni 确认画面无动物
    return {
        "detected": True,
        "description": description,
        "head_bbox": head_bbox,
        "primary_crop_b64": _jpeg_b64(fallback_frame),
        "candidates": [],
    }
