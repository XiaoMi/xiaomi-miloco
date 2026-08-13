"""Smart Crop / 自适应分辨率 —— crop 区域计算(纯函数,numpy/cv2)。

在 OMNI 推理前定向裁切「活动区域」,给模型更高等效分辨率的局部视频。crop 区域 =
union(主体检测框, 帧差分运动块) → 非对称扩展 → 最小/最大面积约束。

算法从离线评测原型 PORT(原型脚本与调参结论**未入库**,产物含真实场景抽帧,只在本地留档;
函数级出处见下方各 docstring 的 "PORT of ..." 行)。落地的**最终参数**:非对称扩展 h40%/v30%、
最小面积 10%、最大面积 ≈49% 回退、characteristic 运动过滤(面积+紧凑度);各参数的取值依据
写在下方对应实现处,不依赖仓外文件也能读懂。

不变量:裁出的区域必须包住窗口内检测到的一切主体(人 + 宠物)和所有变化区域 —— 包不住就
不裁(面积超上限返回 None、回退全景,全景本就什么都看得见)。所以主体框由调用方从
``IdentityPacket.main_det_boxes`` 给入:那是**每一抽帧**的 human/cat/dog 检测框,而不是
``targets``/``box_info`` 的末帧快照(它只有末帧、且宠物永远不在其中,tracker 只跟 HUMAN)。

与原型的两处已知差异(都不打算改,记在此免得被当成 bug 再"修"一遍):
  1. 并集覆盖的帧比 omni 视频多。tracker 逐帧消费 ``input.fps``(默认 3)全帧、框按这些帧
     累积,而送 omni 的视频由 ``pipeline._downsample_for_omni`` 抽到 ``omni_fps``(默认 1);
     原型的并集与被编码的帧同源(离线脚本直接按 omni_fps 解码)。故生产的并集只会更宽 ——
     方向保守(宁可回退全景,不会把该看的裁在画外),但 ``crop_max_area_ratio`` 是在原型的
     窄并集上标定的,线上 area_rejected 触发率会略高于离线对照,别拿离线分布解释线上日志。
  2. 不剔除静物误检框。det_4C 会把衣帽架/落地灯之类稳定误报成 human;身份侧对这类 track
     有 no_person 抑制,但那只作用于 track 状态,``last_detections`` 里那个框照旧存在。
     代价是这类房间的并集被一个固定误检长期撑开、可能持续 area_rejected 回退全景;
     唯一诊断手段是回退日志里的 union 框(见 prompt_builder._maybe_encode_adaptive)。

本模块不做 I/O、不调 OMNI;编码/参考帧由 prompt_builder 负责。
"""

from __future__ import annotations

import logging
import math

import cv2
import numpy as np
from numpy.typing import NDArray

from ..config import CropEnhanceConfig

logger = logging.getLogger(__name__)

# crop 区域,帧像素坐标 (x1, y1, x2, y2)
Region = tuple[int, int, int, int]

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
    det_boxes: list[Region],
    frames: list[NDArray[np.uint8]],
    cfg: CropEnhanceConfig,
    *,
    motion_blocks: list[Region] | None = None,
) -> Region | None:
    """综合主体检测框 + 运动块算 crop 区域;无依据或面积超上限 → None(回退全景)。

    ``det_boxes`` 是窗口内**每一抽帧**的 human/cat/dog 框(``IdentityPacket.main_det_boxes``),
    由调用方给入 —— 本模块不从 ``targets``/``box_info`` 取框,那只有末帧且没有宠物。
    ``motion_blocks`` 可由调用方预算后传入(免重复算 CV,也便于打诊断日志),缺省时内部自算。

    只要区域本身;需要区分"为什么没裁"时用 ``compute_crop_region_detail``。
    """
    return compute_crop_region_detail(
        det_boxes, frames, cfg, motion_blocks=motion_blocks
    )[0]


def compute_crop_region_detail(
    det_boxes: list[Region],
    frames: list[NDArray[np.uint8]],
    cfg: CropEnhanceConfig,
    *,
    motion_blocks: list[Region] | None = None,
) -> tuple[Region | None, str]:
    """同 ``compute_crop_region``,但一并返回拒因,供调用方打诊断日志。

    返回 ``(region, reason)``;``region`` 非 None 时 ``reason == "ok"``。拒因取值:
    ``no_frames`` / ``no_activity``(无框且无运动块) / ``degenerate``(区域退化成零宽高) /
    ``area_too_large`` / ``area_too_small``。

    拒因**必须由本函数给出**,调用方拿并集面积去反推会算错:并集只是入参,真正过闸的是
    非对称扩展 + 最小面积放大之后的 region,后者面积恒 ≥ 并集(两步都只扩不缩)。据并集判
    会把 ``并集面积 ∈ [min, max]`` 区间内的每一次拒绝(成因必为 area_too_large)误标成
    area_too_small —— 而两者的运维处置正好相反。
    """
    if not frames:
        return None, "no_frames"
    h, w = frames[0].shape[:2]
    if w == 0 or h == 0:
        return None, "no_frames"

    if motion_blocks is None:
        motion_blocks = compute_motion_blocks(frames, cfg)
    all_boxes = list(det_boxes) + list(motion_blocks)
    if not all_boxes:
        return None, "no_activity"  # 无检测框且无显著运动块 → 无裁切依据

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
        return None, "degenerate"
    area_ratio = ((rx2 - rx1) * (ry2 - ry1)) / (w * h)
    # 最大面积:区域大到接近全景时裁切已没有意义(视野几乎没收窄,却多付一次编码)→ 回退全景。
    # 它是**语义上限,不是像素预算** —— 像素开销不靠本闸封:crop 视频等比放到逐轴贴住同档全景
    # 画面,编码像素恒 <= 同档全景,主体像素密度也不低于同档全景(倍数 = 纯几何倍数
    # min(w/区域宽, h/区域高) >= 1),与本值取多少无关。详见 _maybe_encode_adaptive 的注释。
    if area_ratio > cfg.crop_max_area_ratio:
        return None, "area_too_large"
    # 最小面积复检:目标紧贴画面边缘时 _enforce_min_area 绕中心放大会被 clamp 截断、
    # 达不到下限,此时裁切等效分辨率无收益 → 回退全景(_enforce_min_area 只尽力、不保证)。
    if area_ratio < cfg.crop_min_area_ratio:
        return None, "area_too_small"
    return region, "ok"


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

    区域**通常**包含每个 bbox:bbox 来自 track 末帧状态框,区域是窗口内逐帧检测框的并集
    → 只向外扩展 → 只放大的最小面积约束,所以 track 最后一次匹配上检测的那一帧只要还落在
    本窗内,该框就在并集里。但不当作前置条件依赖:这条相容性靠的是「一窗的帧数 > track
    coasting 上限」这个数值关系,而它由两个可调旋钮决定 —— 窗长是采集侧的
    ``perception.collect.window_size``(设置接口可写、采集循环拿它当 tick 周期),存活上限
    是 ``identity_engine.deep_sort.max_age_sec``。**此处不写它们当前的取值**:写了就会随
    调参失真,当前关系由 ``perception/engine/config_checks.py`` 集中判定 —— 随包配置那一层
    由 ``test_config_relationships.py`` 钉住,部署现场那一层由建引擎时的告警负责。即便关系成立它也不是不变式 —— 短窗(启动首窗、丢帧)照样能让末帧框比并集更旧;
    且两侧终究是不同对象(track 状态框 vs 原始检测框)。故此处一律 clamp,换算后退化
    (宽或高 <= 0,即框完全落在区域外)返回 None。

    两个调用点据此的处置**不同**:名册 bbox 是先验,退化为"只给姓名不给位置"
    (``_render_roster_entry``);候选 bbox 是模型分配身份的定位锚,不允许退化 ——
    ``build_fused_payload`` 的 region_ok 在裁切前先跑一遍本函数,任一候选换不出就整窗
    回退全景(``_candidate_bbox_ok``)。两者都不输出错坐标。
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
    # raw 非 mapping 时 fail-closed 退默认(=禁用)。`or {}` 只吞 falsy,truthy 非 dict
    # (如 env MILOCO_PERCEPTION__ENGINE__CROP_ENHANCE=false 得到的字符串 "false",或
    # config.json 里手写 `"crop_enhance": true`)会原样留下,下面的 .items() 就抛
    # AttributeError。两个生产调用点抛了都是坏事:推理主路径(_maybe_encode_adaptive)会折成
    # reason=exception 的回退、admin GET/PUT(_perception_config_payload)会 500 —— 而 PUT
    # 正是「把配置改回来」的自救入口,它先投影响应再 update_shared_config,一抛连写盘都到不了,
    # deep_merge 自愈跑不起来。故在此就地拦住,与下面 gate_not_bool / not_number 同款。
    # 注:CLI(`miloco config set`)不调本函数(它是独立进程,不 import miloco 后端),
    # 有自己的 _validate_structure —— 对 perception.engine.crop_enhance 非 object 一律
    # raise ValueError,本闸救不了那条路,只能手改 config.json(见 cli/src/miloco_cli/config.py)。
    # 另注:env 注入的这一份 PUT 也盖不掉,env 优先级高于 config.json
    # (config/settings.py 的 settings_customise_sources),得先去掉环境变量。
    if not isinstance(raw, dict):
        logger.warning(
            "event=crop_enhance_config_bad reason=not_mapping raw=%r 退默认(禁用)", raw
        )
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
        "crop_min_area_ratio", "crop_max_area_ratio",
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
