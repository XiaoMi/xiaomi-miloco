# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""识别端宠物参考图注入（P2）——把已登记宠物的多姿态参考 crop 拼进 fused 多模态 user content。

设计（见 .wsh_cc/宠物注册改进_决策清单.md C 段 / 宠物注册流程改造_方案.md §10 / pet_eval ③）：
- **只读、每只一张 composite**：读 ``PetLibrary`` 落盘的参考 crop（P1a-B 存储），把每只 ≤3 张
  多姿态 crop **缩到统一高度横向拼成 1 张 composite** 再注入（对齐 pet_eval ③ 验证过的口径、
  也同人 gallery 的 ``hstack_to_height``）——比"每只多张独立图"省 ~3× token、且与被验证配置一致。
  最多 ``max_pets`` 只；**绝不接 IdentityEngine / ReID / person 表**（红线）。
- **纪律仍走文字**：命名规则由 system prompt 的 ``PET_NAMING_SPEC`` + 家庭档案「## 宠物」段承载，
  本模块只补"个体长啥样"的视觉参照；图仅供比对，命名以名单+纪律为准（C-D2：最小 ③ prompt①）。
- **失败即退纯文字**：任何读取/解码/编码异常都吞掉、跳过该图/该宠物，最坏返回 ``[]``——上游 fused
  prompt 退化为"无参考图"，PET_NAMING_SPEC + 档案文字仍在，不影响主链路。
- **缓存**：参考图变动极少（仅注册/补充素材时），识别是逐帧热路径 → 按 (max_pets, 各宠物
  参考 crop 文件的 name/mtime_ns/size) 签名缓存已编码内容，签名不变直接复用，避免每帧重读盘+重
  编码。用文件 stat（非 updated_at 秒级字符串）作签名 → 同秒原地整组替换（张数不变、内容变）
  也能失效（updated_at 秒粒度会漏掉该窗口）。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from miloco.perception.engine.identity.gallery_composite import hstack_to_height

logger = logging.getLogger(__name__)

_MIN_JPEG_BYTES = 100  # 对齐 prompt_builder._MIN_JPEG_BYTES：更短视为损坏，跳过
# composite 统一高度：对齐 pet_eval run_folder_eval2 的 H=320（③ 召回数字即在此口径下测得）。
_PET_COMPOSITE_HEIGHT = 320

# 单条目缓存：签名不变则复用已编码内容（识别逐帧调用，避免重读盘/重编码）。
_cache: dict[str, Any] = {"sig": None, "content": []}


def _composite_block(imgs: list[np.ndarray]) -> dict | None:
    """把一只宠物的 ≤3 张 BGR crop 缩到统一高度横向拼成 1 张 → JPEG image_url 块；失败 → None。"""
    sheet = hstack_to_height(imgs, _PET_COMPOSITE_HEIGHT)
    if sheet is None or sheet.size == 0:
        return None
    ok, buf = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return None
    data = buf.tobytes()
    if len(data) < _MIN_JPEG_BYTES:
        return None
    b64 = base64.b64encode(data).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def _crop_stat(p: Path) -> tuple:
    """crop 文件身份 (name, mtime_ns, size)；stat 失败给 0——原地覆盖会改 mtime/size，故可捕获同秒替换。"""
    try:
        st = p.stat()
        return (p.name, st.st_mtime_ns, st.st_size)
    except OSError:
        return (p.name, 0, 0)


def _build_content(entries: list[tuple], max_pets: int) -> list[dict]:
    """entries=[(pet, paths)]（paths 已在签名阶段列好，复用不重列）。"""
    blocks: list[dict] = []
    used = 0
    for pet, paths in entries:
        if used >= max_pets:
            logger.warning(
                "event=pet_refs_truncated max_pets=%d 家中宠物数超上限，仅注入前 %d 只参考图",
                max_pets,
                max_pets,
            )
            break
        imgs: list[np.ndarray] = []
        for p in paths:
            try:
                data = p.read_bytes()
                if not data or len(data) < _MIN_JPEG_BYTES:
                    continue
                arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            except Exception:  # noqa: BLE001 — 任一读/解码失败跳过该图，不拖垮整体
                arr = None
            if arr is not None and arr.size > 0:
                imgs.append(arr)
        if not imgs:
            continue  # 该宠物无有效参考图 → 跳过（仍靠档案文字命名，不阻断）
        block = _composite_block(imgs)  # 每只 ≤3 张 → 1 张 composite（对齐 ③ 口径、省 token）
        if block is None:
            continue
        label = f"【{pet.name}】" + (f"（{pet.species}）" if pet.species else "")
        blocks.append({"type": "text", "text": label})
        blocks.append(block)
        used += 1
    if not blocks:
        return []
    header = {
        "type": "text",
        "text": (
            "下方 <pets> 为家中已登记宠物的多姿态参考图，用于在画面中辨认并按「宠物命名」纪律"
            "称呼它们；图仅供比对个体，命名仍以名单与纪律为准。"
        ),
    }
    return [
        header,
        {"type": "text", "text": "<pets>"},
        *blocks,
        {"type": "text", "text": "</pets>"},
    ]


def build_pet_reference_content(max_pets: int = 3) -> list[dict]:
    """构建已登记宠物参考图段（text + image_url 块列表）。无宠物/无图/任何失败 → ``[]``、**绝不抛**。

    调用方（``prompt_builder._build_fused_user_content``）在 has_pets 为真时注入；本函数只读
    ``PetLibrary`` 拼块，不做启用/软关闭门控（由上游 has_pets 决定）。全程 try 包裹，是逐帧热路径
    的硬约束（任何异常都退纯文字，PET_NAMING_SPEC + 档案文字仍在）。
    """
    try:
        from miloco.perception.engine.identity.pet_library import get_pet_library

        lib = get_pet_library()
        pets = lib.list()
        if not pets:
            return []
        # 一次列好每只的 crop 路径 → 既用于签名（文件 stat）又复用于构建，不重复 glob。
        entries: list[tuple] = []
        sig_parts: list[tuple] = []
        for pet in pets:
            try:
                paths = list(lib.reference_crop_paths(pet.id))
            except Exception:  # noqa: BLE001 — 单只读失败不拖垮整体
                logger.warning(
                    "event=pet_refs_paths_fail pet_id=%s", pet.id, exc_info=True
                )
                paths = []
            entries.append((pet, paths))
            sig_parts.append((pet.id, tuple(_crop_stat(p) for p in paths)))
        sig = (max_pets, tuple(sig_parts))
        if sig == _cache["sig"]:
            return _cache["content"]
        content = _build_content(entries, max_pets)
        _cache["sig"] = sig
        _cache["content"] = content
        return content
    except Exception:  # noqa: BLE001 — 逐帧热路径：任何失败退纯文字，绝不抛
        logger.warning("event=pet_refs_build_fail", exc_info=True)
        return []
