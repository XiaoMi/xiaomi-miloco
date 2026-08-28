# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""单帧人像注入 —— 给每个待识别 track 附一张"外观单帧",绑定 track_id 后拼进 fused user content。

**做什么**:本窗每个待识别 track,从视频帧里裁出该 track **面积最大**的那一帧人像,归一到统一
高度、PNG 无损编码,配一句 ``待识别 track_id=N 的外观单帧`` 注入 prompt。画面本身不动 ——
注入是补充而非替代(去掉视频的对照臂已验证会掉)。

**为什么**:离线评测(主指标 = 多人同框子集的逐窗召回率,配对 McNemar)显示,把"只给 bbox 数字
坐标"升级为"额外给一张绑定 track_id 的干净人像",多人同框召回显著上升;在已开 Smart Crop 的
条件下仍有可观增益。增益不来自像素级高清,而来自**省掉了模型自己在多人画面里定位并抠人的负担**
—— 去掉 track_id 绑定的对照臂显著变差,所以绑定文本是配置的一部分、不是可优化的文案。

**规格要点**(均为已验证口径,改动须重跑等价性 A/B):
- 每 track **恰好 1 张**:多帧横拼无增益且更贵。
- 选帧 = **裁出面积最大**的一帧,纯面积、不做任何清晰度算法(面积只作"近=清晰"的代理);
  原生框高不足 ``min_bbox_height_px`` 的帧跳过。
- 抠图 padding ``_PAD_RATIO``,与候选自带的 ``body_crop``(``body_crop_padding_ratio``)同值,
  所以走兜底路径时几何口径一致、不会因 padding 不同而偏离。
- 归一高度 ``crop_height``,PNG 无损。

**失败语义**:与 gallery 的"全或无"**相反** —— 单个 track 裁不出图就跳过该 track,其余照常注入。
逐帧热路径的硬约束:全程 try 包裹、任何失败返回 ``[]``,**绝不抛**(payload 构造抛异常会被上游
折成整相机本窗 skipped)。同 ``pet_refs.build_pet_reference_content`` 的口径。
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.config import PersonCropInjectConfig
from miloco.perception.engine.identity.gallery_composite import (
    encode_png_bytes,
    hstack_to_height,
)

if TYPE_CHECKING:
    from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

logger = logging.getLogger(__name__)

_MIN_PNG_BYTES = 100  # 对齐 prompt_builder._MIN_JPEG_BYTES:更短视为损坏,跳过
# 抠图 padding:bbox 各边外扩 5%。与 IdentityConfig.body_crop_padding_ratio 同值(见模块 docstring),
# 故不做成配置项 —— 它从未作为实验维度被扫过,暴露出来只会招致两条路径口径漂移。
_PAD_RATIO = 0.05

# 前置说明块(整段注入图之前只出现一次)。措辞**不要**当文案润色顺手改,但要分清哪部分是什么:
#   · 已验证:``track_id=N`` 的显式绑定 + 图的顺序 —— 有对照臂(去掉绑定并按内容哈希打乱顺序)
#     显著变差,省下的正是"把图对回某个 track"这份负担。
#   · 未验证:"外观单帧"这个名词本身。它只是离线评测当时的写法,没有任何对照臂测过它,
#     所以换名词不是"违反已验证配置",而是"引入一处未验证改动" —— 要改走等价性 A/B。
#   · 已知瑕疵(故意保留):句中"最清晰"是给模型的先验(这张图看得清、可信),而实现是纯面积最大、
#     不做任何清晰度算法。不订正它:prompt 的职责不是文档化选帧算法,删掉是净减信息。
# 名词在本文件出现三次(本句内两次 + 下方绑定文本一次)。两处不一致会让模型看到两个名字、指代断掉,
# 而各处又都有自己的断言、谁也发现不了 —— 由 test_label_reuses_the_term_from_the_note 守着。
_INJECT_NOTE = "【识别辅助】下方为每个待识别 track 的“外观单帧”：从本段视频中裁出的该 track 最大最清晰的一帧。请优先把每个 track 的外观单帧与上方 gallery 成员参考图逐一比对来判定身份；track 的 bbox 数字坐标仍可用于在视频画面中交叉核对位置。输出 identity_assignments 时照旧用 track_id 数字。"


def person_crop_inject_config_from_settings() -> PersonCropInjectConfig:
    """热读 settings 的 perception.engine.person_crop_inject,过滤未知键,缺省补默认(免重启)。

    任何异常/非法结构都 fail-closed 退默认(= 关闸):本函数在推理主路径上,抛出去会被
    ``omni.run_omni_fused`` 折成整相机本窗 skipped。同 ``crop_enhance_config_from_settings``。
    """
    try:
        from miloco.config import get_settings

        raw = get_settings().perception.engine.get("person_crop_inject", {}) or {}
    except Exception:  # noqa: BLE001 —— settings 不可用时退默认(禁用)
        return PersonCropInjectConfig()
    # raw 非 mapping 时 fail-closed。`or {}` 只吞 falsy,truthy 非 dict(如 env 注入得到的字符串
    # "false"、或 config.json 里手写 `"person_crop_inject": true`)会原样留下,下面 .items() 就抛。
    if not isinstance(raw, dict):
        logger.warning(
            "event=person_crop_inject_config_bad reason=not_mapping raw=%r 退默认(禁用)", raw
        )
        return PersonCropInjectConfig()
    known = PersonCropInjectConfig.__dataclass_fields__.keys()
    filtered = {k: v for k, v in raw.items() if k in known}
    try:
        cfg = PersonCropInjectConfig(**filtered)
    except (TypeError, ValueError):
        logger.warning("event=person_crop_inject_config_bad 回退默认 raw=%s", raw)
        return PersonCropInjectConfig()
    # dataclass 不校验值类型(上面的 except 只在**字段名**不匹配时触发),所以必须自己查。
    # 闸只认真 bool:yaml 里 `enabled: "false"` 加了引号会变成非空字符串 → truthy → 闸静默
    # fail-open(以为已经关掉了,实际还在注入)。
    if not isinstance(cfg.enabled, bool):
        logger.warning(
            "event=person_crop_inject_config_bad reason=gate_not_bool enabled=%r 退默认(禁用)",
            cfg.enabled,
        )
        return PersonCropInjectConfig()
    # 数值字段写成字符串时 dataclass 同样放行,要到 cv2.resize / 比较那步才抛 —— 而本模块外层
    # 有宽 except,一抛就被吞成 build_fail 的"整段不注入",日志里看不出根因是配置写错。在这里
    # 就报出来。bool 是 int 的子类,得单独排掉(True 会当 1 通过 isinstance 检查)。
    for name in ("crop_height", "min_bbox_height_px"):
        v = getattr(cfg, name)
        if isinstance(v, bool) or not isinstance(v, int):
            logger.warning(
                "event=person_crop_inject_config_bad reason=not_int field=%s value=%r 退默认(禁用)",
                name, v,
            )
            return PersonCropInjectConfig()
    # crop_height <= 0 类型合法但会让 cv2.resize 抛;min_bbox_height_px < 0 等于取消小框过滤,
    # 不是"更宽松"而是把远处糊图放回选帧池,同样当配置错误处理。
    if cfg.crop_height <= 0 or cfg.min_bbox_height_px < 0:
        logger.warning(
            "event=person_crop_inject_config_bad reason=out_of_range crop_height=%d "
            "min_bbox_height_px=%d 退默认(禁用)",
            cfg.crop_height, cfg.min_bbox_height_px,
        )
        return PersonCropInjectConfig()
    return cfg


def _crop_box(
    frame: NDArray[np.uint8], xyxy: tuple[int, int, int, int]
) -> NDArray[np.uint8] | None:
    """按 ``xyxy`` 外扩 ``_PAD_RATIO`` 从 ``frame`` 裁一张;越界 clamp 到帧内,退化成空 → None。"""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = xyxy
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0:
        return None
    px, py = int(round(bw * _PAD_RATIO)), int(round(bh * _PAD_RATIO))
    cx1, cy1 = max(0, x1 - px), max(0, y1 - py)
    cx2, cy2 = min(w, x2 + px), min(h, y2 + py)
    if cx2 <= cx1 or cy2 <= cy1:
        return None
    return frame[cy1:cy2, cx1:cx2].copy()


def _pick_largest_crop(
    frames: list[NDArray[np.uint8]],
    per_frame_boxes: list[dict[int, tuple[int, int, int, int]]],
    track_id: int,
    min_bbox_h: int,
) -> NDArray[np.uint8] | None:
    """该 track 在窗内裁出**面积最大**的一帧;无一帧合格 → None(调用方走 body_crop 兜底)。

    面积按**裁出图**算而非框:贴边时 clamp 会真的裁小,按框算会挑中一张实际更小的图。

    ``per_frame_boxes`` 必须与 ``frames`` **同序同长**;长度不等即整条逐帧路径放弃(返回 None,
    退末帧兜底),**不按较短者截断**。截断是错的:两边下标含义不同的话,配对出来的是"用另一个
    时刻的框去裁这一帧",人一动就裁到背景或隔壁那个人 —— 而图是绑 track_id 的。上游确实出过
    这种错位(omni 下采只抽帧没抽框),而当时这里正是靠静默截断把它藏住、无任何日志。
    """
    if len(frames) != len(per_frame_boxes):
        logger.warning(
            "event=person_crop_inject_len_mismatch n_frames=%d n_boxes=%d "
            "放弃逐帧选帧(退末帧兜底)——两者下标必须同源",
            len(frames), len(per_frame_boxes),
        )
        return None
    best: NDArray[np.uint8] | None = None
    best_area = 0
    for frame, boxes in zip(frames, per_frame_boxes):
        box = boxes.get(track_id)
        if box is None or (box[3] - box[1]) < min_bbox_h:
            continue
        crop = _crop_box(frame, box)
        if crop is None or crop.size == 0:
            continue
        area = crop.shape[0] * crop.shape[1]
        if area > best_area:
            best, best_area = crop, area
    return best


def _crop_block(crop: NDArray[np.uint8], height: int) -> dict | None:
    """单张人像 → 归一到 ``height`` 高 → PNG image_url 块;任一步失败 → None。

    ``max_total_width=None`` 是必须的:默认的 768 宽帽在超宽时会**连高一起缩**
    (``new_h = int(target_height * scale)``),即 ``height`` 不再是硬保证。单张人像宽高比
    几乎恒 < 1、触发不了,但显式关掉才是"归一高度"这个规格的真实表达。
    """
    sheet = hstack_to_height([crop], height, max_total_width=None)
    if sheet is None or sheet.size == 0:
        return None
    data = encode_png_bytes(sheet)
    if not data or len(data) < _MIN_PNG_BYTES:
        return None
    b64 = base64.b64encode(data).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def build_person_crop_content(
    *,
    candidates: list[IdentityQueryItem],
    frames: list[NDArray[np.uint8]],
    per_frame_boxes: list[dict[int, tuple[int, int, int, int]]],
) -> list[dict]:
    """构建单帧人像注入段(text + image_url 块列表)。关闸/无候选/全部裁不出 → ``[]``、**绝不抛**。

    Args:
        candidates:      本窗待识别 track(顺序即注入顺序,与"待识别 track"文本段一致)
        frames:          本窗全部抽帧(``IdentityPacket.all_frames``,全景原帧)
        per_frame_boxes: 逐帧带 track_id 的框,与 ``frames`` 同序
                         (``IdentityPacket.per_frame_track_boxes``)

    逐候选独立成败:某 track 拿不到逐帧框(mock 跟踪服务、全窗 coasting)时退用它自带的
    ``body_crop``(末帧、同 padding);连兜底也没有就跳过该 track,不影响其余。
    """
    try:
        if not candidates:
            return []
        cfg = person_crop_inject_config_from_settings()
        if not cfg.enabled:
            return []
        blocks: list[dict] = []
        skipped: list[int] = []
        for cand in candidates:
            crop: NDArray[np.uint8] | None = None
            if frames and per_frame_boxes:
                crop = _pick_largest_crop(
                    frames, per_frame_boxes, cand.track_id, cfg.min_bbox_height_px
                )
            if crop is None:
                # 兜底:候选自带的末帧 body_crop(IdentityEngine 每窗已算好、padding 同值)。
                # 走到这里的常见情形:mock 跟踪服务不产逐帧框、该 track 全窗 coasting、
                # 或所有真匹配帧的框都没过 min_bbox_height_px。
                crop = cand.body_crop
            if crop is None or getattr(crop, "size", 0) == 0:
                skipped.append(cand.track_id)
                continue
            block = _crop_block(crop, cfg.crop_height)
            if block is None:
                skipped.append(cand.track_id)
                continue
            blocks.append(
                {"type": "text", "text": f"待识别 track_id={cand.track_id} 的外观单帧："}
            )
            blocks.append(block)
        if skipped:
            # 跳过是设计内的降级(不是错误),但要能看见:全员跳过时本段等价没注入,
            # 而线上召回若回落到基线水平,这行日志是唯一能区分"闸没开"与"图没裁出来"的证据。
            logger.info(
                "event=person_crop_inject_skip track_ids=%s injected=%d/%d",
                skipped, len(blocks) // 2, len(candidates),
            )
        if not blocks:
            return []
        return [{"type": "text", "text": _INJECT_NOTE}, *blocks]
    except Exception:  # noqa: BLE001 —— 逐帧热路径:任何失败退无图,绝不抛
        logger.warning("event=person_crop_inject_build_fail", exc_info=True)
        return []
