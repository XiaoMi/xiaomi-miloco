"""Smart Crop / 自适应分辨率 —— crop 区域计算(纯函数,numpy/cv2)。

在 OMNI 推理前定向裁切「活动区域」,给模型更高等效分辨率的局部视频。crop 区域 =
union(主体检测框, 帧差分运动块) → 非对称扩展 → 最小/最大面积约束。

算法从离线评测原型 PORT(原型脚本与调参结论**未入库**,产物含真实场景抽帧,只在本地留档;
函数级出处见下方各 docstring 的 "PORT of ..." 行)。落地的**最终参数**:非对称扩展 h40%/v30%、
最小面积 10%、最大面积 ≈49% 回退、characteristic 运动过滤(面积+紧凑度);各参数的取值依据
写在下方对应实现处,不依赖仓外文件也能读懂。

与原型差异:生产 tracker 每 target 只出 1 个 last-frame body box(tracking_service._build_response),
不做逐帧累积;窗口内的时序范围由运动块补齐。故检测框取末帧、运动块跨帧累积。

本模块不做 I/O、不调 OMNI;编码/参考帧由 prompt_builder 负责。
"""

from __future__ import annotations

import logging
import math

import cv2
import numpy as np
from numpy.typing import NDArray

from ..config import CropEnhanceConfig
from ..types import BoxType, IdentityTarget

logger = logging.getLogger(__name__)

# crop 区域,帧像素坐标 (x1, y1, x2, y2)
Region = tuple[int, int, int, int]

# 主体类:human_body / pet_body 参与 crop;human_face(head/face 子部件)排除——
# 子部件框分散,纳入并集会撑大 crop 区域。
_MAIN_BOX_TYPES = (BoxType.HUMAN_BODY.value, BoxType.PET_BODY.value)

_MORPH_KERNEL = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))


def compute_motion_blocks(
    frames: list[NDArray[np.uint8]], cfg: CropEnhanceConfig
) -> list[Region]:
    """帧差分 → 形态学去噪 → 连通域 → characteristic 过滤,返回运动块 xyxy 列表。

    PORT of exp_motion_filter.compute_motion_blocks + filter_characteristic。
    三层噪声过滤:①整帧变化 > motion_global_drift_ratio → 全局漂移(光影),丢弃全部;
    ②单块面积占比 < motion_min_block_ratio → 点状噪声;③紧凑度 fill_ratio < motion_min_fill_ratio
    → 条纹/散碎噪声。<2 帧无法差分 → []。
    """
    if len(frames) < 2:
        return []

    h, w = frames[0].shape[:2]
    total_px = h * w
    if total_px == 0:
        return []

    mask = np.zeros((h, w), dtype=np.uint8)
    for i in range(1, len(frames)):
        g1 = cv2.cvtColor(frames[i - 1], cv2.COLOR_BGR2GRAY)
        g2 = cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(g1, g2)
        _, b = cv2.threshold(diff, cfg.motion_diff_threshold, 255, cv2.THRESH_BINARY)
        b = cv2.morphologyEx(b, cv2.MORPH_OPEN, _MORPH_KERNEL)
        b = cv2.morphologyEx(b, cv2.MORPH_CLOSE, _MORPH_KERNEL)
        mask = cv2.bitwise_or(mask, b)

    # ① 全局漂移:整帧大面积变化(开关灯/镜头动/大遮挡)→ 运动信号无意义,丢弃全部
    if np.count_nonzero(mask) / total_px > cfg.motion_global_drift_ratio:
        return []

    n_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    blocks: list[Region] = []
    for i in range(1, n_labels):  # 0 是背景
        area = int(stats[i, cv2.CC_STAT_AREA])
        bx = int(stats[i, cv2.CC_STAT_LEFT])
        by = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        bbox_area = bw * bh
        # ② 点状噪声:面积太小
        if area / total_px < cfg.motion_min_block_ratio:
            continue
        # ③ 条纹/散碎噪声:blob 填不满外接框
        fill_ratio = area / bbox_area if bbox_area > 0 else 0.0
        if fill_ratio < cfg.motion_min_fill_ratio:
            continue
        blocks.append((bx, by, bx + bw, by + bh))
    return blocks


def _body_boxes(targets: list[IdentityTarget]) -> list[Region]:
    """从 targets 取主体检测框(human_body / pet_body 末帧),xywh → xyxy,排除 face。

    用像素 box_info(与 all_frames 同像素空间),不用 bbox_xyxy_norm(有损、coasting/引擎关时 None)。

    跳过已落定 no_person 的 track:这些是检测器把静物(衣帽架 / 落地灯)误报成人体、
    已被身份侧投票压掉的框。裁切区域是**并集**,一个位置固定的误检就能把区域撑过
    crop_max_area_ratio 上限,让整个房间的 Smart Crop 永久回退全景,而日志只说
    "面积超限"、看不出根因。不能用 `person_id == "none"` 代替 —— no_person 与
    "有人但没识别出身份"都渲染成 "none"(state.get_face_id_value),那样会把陌生人一起排掉。
    """
    boxes: list[Region] = []
    for t in targets:
        if t.no_person:
            continue
        if not t.box_info:
            continue
        last = t.box_info[-1]
        for btype in _MAIN_BOX_TYPES:
            xywh = last.boxes.get(btype)
            if not xywh:
                continue
            x, y, bw, bh = xywh
            if bw <= 0 or bh <= 0:
                continue
            boxes.append((int(x), int(y), int(x + bw), int(y + bh)))
    return boxes


def _clamp(region: Region, w: int, h: int) -> Region:
    x1, y1, x2, y2 = region
    return (max(0, x1), max(0, y1), min(w, x2), min(h, y2))


def _enforce_min_area(region: Region, w: int, h: int, min_ratio: float) -> Region:
    """crop 面积不足 min_ratio 时,绕中心等比放大到达标(clamp 到画面),防小目标过度放大。"""
    x1, y1, x2, y2 = region
    rw, rh = x2 - x1, y2 - y1
    frame_area = w * h
    if rw <= 0 or rh <= 0 or frame_area == 0:
        return region
    if (rw * rh) / frame_area >= min_ratio:
        return region
    scale = (min_ratio * frame_area / (rw * rh)) ** 0.5
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    nw, nh = rw * scale, rh * scale
    # 向外取整(floor 左上 / ceil 右下),避免 int 截断把面积压回下限之下
    return _clamp(
        (
            math.floor(cx - nw / 2),
            math.floor(cy - nh / 2),
            math.ceil(cx + nw / 2),
            math.ceil(cy + nh / 2),
        ),
        w,
        h,
    )


def compute_crop_region(
    targets: list[IdentityTarget],
    frames: list[NDArray[np.uint8]],
    cfg: CropEnhanceConfig,
    *,
    det_boxes: list[Region] | None = None,
    motion_blocks: list[Region] | None = None,
) -> Region | None:
    """综合检测框 + 运动块算 crop 区域;无依据或面积超上限 → None(回退全景)。

    det_boxes / motion_blocks 可由调用方预算后传入(免重复算 CV,也便于打诊断日志);
    缺省 None 时内部自算,老调用行为不变。
    """
    if not frames:
        return None
    h, w = frames[0].shape[:2]
    if w == 0 or h == 0:
        return None

    if det_boxes is None:
        det_boxes = _body_boxes(targets)
    if motion_blocks is None:
        motion_blocks = compute_motion_blocks(frames, cfg)
    all_boxes = det_boxes + motion_blocks
    if not all_boxes:
        return None  # 无检测框且无显著运动块 → 无裁切依据

    # 并集
    ux1 = min(b[0] for b in all_boxes)
    uy1 = min(b[1] for b in all_boxes)
    ux2 = max(b[2] for b in all_boxes)
    uy2 = max(b[3] for b in all_boxes)

    # 非对称扩展(水平 expand_ratio_h / 垂直 expand_ratio_v)
    uw, uh = ux2 - ux1, uy2 - uy1
    ex = int(uw * cfg.expand_ratio_h)
    ey = int(uh * cfg.expand_ratio_v)
    region = _clamp((ux1 - ex, uy1 - ey, ux2 + ex, uy2 + ey), w, h)

    # 最小面积
    region = _enforce_min_area(region, w, h, cfg.crop_min_area_ratio)

    rx1, ry1, rx2, ry2 = region
    if (rx2 - rx1) <= 0 or (ry2 - ry1) <= 0:
        return None
    area_ratio = ((rx2 - rx1) * (ry2 - ry1)) / (w * h)
    # 最大面积:超过则 crop 缩到 crop_short_edge 后等效分辨率反低于全景 → 回退全景
    if area_ratio > cfg.crop_max_area_ratio:
        return None
    # 最小面积复检:目标紧贴画面边缘时 _enforce_min_area 绕中心放大会被 clamp 截断、
    # 达不到下限,此时裁切等效分辨率无收益 → 回退全景(_enforce_min_area 只尽力、不保证)。
    if area_ratio < cfg.crop_min_area_ratio:
        return None
    return region


def crop_frames(
    frames: list[NDArray[np.uint8]], region: Region
) -> list[NDArray[np.uint8]]:
    """逐帧裁切到 region(副本,不改原帧)。"""
    x1, y1, x2, y2 = region
    return [f[y1:y2, x1:x2].copy() for f in frames]


def remap_bbox_norm_to_crop(
    bbox_norm: tuple[int, int, int, int],
    region: Region,
    frame_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    """把全景帧的 [0,1000] 归一化 bbox 换算成 crop 区域内的 [0,1000] 坐标。

    crop 生效时主视频是局部放大画面,而名册 bbox 由
    ``identity.engine._normalize_bbox_to_1000(…, all_frames[-1])`` 按**全景整帧**归一化——
    两者坐标系不同。不换算就直接锚到视频,等于拿全景坐标读局部画面,会把姓名贴到
    crop 中央那个人身上(方向性错误,不是精度损失)。

    换算链:归一化 → 全景像素 → 减 region 原点 → 按 crop 尺寸重新归一化。
    ``frame_size`` 是 ``(w, h)``,与 ``region`` 同一像素空间(即 ``all_frames`` 的原生尺寸)。

    区域**通常**包含每个名册 bbox:二者同源于 ``box_info[-1].boxes["human_body"]``
    (``_body_boxes`` 与 ``identity._to_tracking_dicts``),而区域 = 并集 → 扩展 → 只放大的
    最小面积约束。但不当作前置条件依赖:``identity._to_tracking_dicts`` 有一条
    ``boxes.get("human")`` 回退键不在 ``_MAIN_BOX_TYPES`` 里(只可能来自已废弃的
    ``tracking_service.convert_response``,生产 ``_build_response`` 只写 BoxType 枚举值),
    且检测框可能溢出帧外。故此处一律 clamp,换算后退化(宽或高 <= 0,即框完全落在区域外)
    返回 None —— 调用方据此退化为"只给姓名不给位置",不输出错坐标。
    """
    fw, fh = frame_size
    rx1, ry1, rx2, ry2 = region
    cw, ch = rx2 - rx1, ry2 - ry1
    if fw <= 0 or fh <= 0 or cw <= 0 or ch <= 0:
        return None

    # 归一化 → 全景像素(round 与 _normalize_bbox_to_1000 的取整方向一致)
    px1, px2 = bbox_norm[0] * fw / 1000, bbox_norm[2] * fw / 1000
    py1, py2 = bbox_norm[1] * fh / 1000, bbox_norm[3] * fh / 1000

    # 平移到 crop 原点 → 按 crop 尺寸重新归一化 → clamp 回 [0,1000]
    out = (
        max(0, min(1000, round((px1 - rx1) * 1000 / cw))),
        max(0, min(1000, round((py1 - ry1) * 1000 / ch))),
        max(0, min(1000, round((px2 - rx1) * 1000 / cw))),
        max(0, min(1000, round((py2 - ry1) * 1000 / ch))),
    )
    if out[2] - out[0] <= 0 or out[3] - out[1] <= 0:
        return None  # 框完全落在 crop 区域外(或被 clamp 压成零宽/零高)
    return out


def crop_enhance_config_from_settings() -> CropEnhanceConfig:
    """热读 settings 的 perception.engine.crop_enhance,过滤未知键,缺省补默认(免重启)。"""
    try:
        from miloco.config import get_settings

        raw = get_settings().perception.engine.get("crop_enhance", {}) or {}
    except Exception:  # noqa: BLE001 —— settings 不可用时退默认(=禁用)
        return CropEnhanceConfig()
    known = CropEnhanceConfig.__dataclass_fields__.keys()
    filtered = {k: v for k, v in raw.items() if k in known}
    try:
        cfg = CropEnhanceConfig(**filtered)
    except (TypeError, ValueError):
        logger.warning("event=crop_enhance_config_bad 回退默认 raw=%s", raw)
        return CropEnhanceConfig()
    # dataclass 不校验值类型(上面的 except 只在**字段名**不匹配时触发),所以必须自己查:
    # yaml 里 `enabled: "false"` 加了引号会变成非空字符串 → truthy → 闸静默 fail-open
    # (以为已经关掉了,实际还在裁)。闸只认真 bool,其余一律当配置错误退禁用。
    # admin GET 的 smart_crop_enabled / available 也从这里取值,两侧判定不会分裂。
    if not isinstance(cfg.enabled, bool) or not isinstance(cfg.user_enabled, bool):
        logger.warning(
            "event=crop_enhance_config_bad reason=gate_not_bool enabled=%r user_enabled=%r 退禁用",
            cfg.enabled, cfg.user_enabled,
        )
        return CropEnhanceConfig()
    # 数值字段写成字符串时,dataclass 同样放行,后续比较才抛 TypeError → 被主流程的宽
    # except 吞成 reason=exception,日志里看不出是配置写错。在这里就报出来。
    for name in (
        "expand_ratio_h", "expand_ratio_v", "motion_diff_threshold",
        "motion_min_block_ratio", "motion_min_fill_ratio", "motion_global_drift_ratio",
        "crop_min_area_ratio", "crop_max_area_ratio", "crop_short_edge",
    ):
        if isinstance(getattr(cfg, name), bool) or not isinstance(getattr(cfg, name), (int, float)):
            logger.warning(
                "event=crop_enhance_config_bad reason=not_number field=%s value=%r 退默认",
                name, getattr(cfg, name),
            )
            return CropEnhanceConfig()
    if cfg.crop_min_area_ratio > cfg.crop_max_area_ratio:
        logger.warning(
            "event=crop_enhance_config_bad reason=area_ratio_inverted min=%s max=%s 退默认",
            cfg.crop_min_area_ratio, cfg.crop_max_area_ratio,
        )
        return CropEnhanceConfig()
    return cfg
