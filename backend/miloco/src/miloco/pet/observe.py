# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""宠物外观观察（pet:observe）—— 上传图/视频，选出最优宠物 crop，让 omni 按维度生成外观描述。

设计（见 .wsh_cc/宠物识别方案.md §2.3(2) / §3a.6，仅设计参考）：
- **选 crop**：单图取最大 CAT/DOG 框 + 扩边 5%；视频用**无 ReID 的 SortTracker** 把同相机
  多只宠物分到各自 track，每 track 选综合质量（置信×清晰度×面积）最优一张。送 omni 的是
  这张**静态 crop**（视频也只送选出的最优帧，不必把视频喂 omni）。
- **detector / tracker 每次新建**（镜像人体注册的工厂模式；tracker 带状态必须 fresh）；
  不接 IdentityEngine。喂 SortTracker 的是预过滤的宠物检测（不依赖将被移除的类别开关来选类）。
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
{"species":"猫|狗|其他","breed":"<高把握才填，否则空字符串>","size_build":"如 中等体型、体态壮实","coat_color_pattern":"如 黑色 / 橘白双色 / 狸花","coat_length_texture":"如 短毛顺滑 / 长毛蓬松","distinctive_markings":["如 尾尖一撮白","左耳缺口"],"accessories":"<可变动配饰，如项圈颜色；无则空字符串>","summary":"一句规范化外观句，有把握判断品种时把品种写进去（如 英短 / 柯基 / 金毛），拿不准则不写品种；如 中等体型的黑色短毛英短猫，尾巴尖有一撮白毛"}"""

_GROUNDING_CLAUSE = """
另外，在上述 JSON 中**额外**加一个键 "head_bbox"：宠物头部区域相对本图的归一化边界框 [x, y, w, h]（四个 0~1 之间的小数，x/y 为左上角，w/h 为宽高占比）；头部不可见则置 null。"""


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


async def _omni_describe(crop: np.ndarray, *, grounding: bool) -> tuple[dict, Any]:
    """把一张宠物 crop 送 omni，返回 (结构化描述 dict, head_bbox|None)。"""
    config = resolve_live_omni_config(OmniConfig())
    system_prompt = OBSERVE_SYSTEM_PROMPT + (_GROUNDING_CLAUSE if grounding else "")
    payload = {
        "system_prompt": system_prompt,
        "user_content": "请观察这只宠物，按要求输出 JSON。",
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


async def observe_pet(
    media: bytes, *, is_video: bool, grounding: bool, max_frames: int = _MAX_VIDEO_FRAMES
) -> dict:
    """上传媒体 → 选最优宠物 crop → omni 生成外观描述。无副作用。

    返回 ``{detected, description, head_bbox, primary_crop_b64, candidates}``；
    未检出宠物时 ``detected=False``、其余为空。
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
    else:
        img = cv2.imdecode(np.frombuffer(media, dtype=np.uint8), cv2.IMREAD_COLOR)
        one = _largest_pet_crop(img, detector) if img is not None else None
        crops = [one] if one is not None else []

    if not crops:
        return {
            "detected": False,
            "description": None,
            "head_bbox": None,
            "primary_crop_b64": "",
            "candidates": [],
        }

    primary = crops[0]
    description, head_bbox = await _omni_describe(primary["crop"], grounding=grounding)
    return {
        "detected": True,
        "description": description,
        "head_bbox": head_bbox,
        "primary_crop_b64": _jpeg_b64(primary["crop"]),
        "candidates": [_candidate_out(c) for c in crops],
    }
