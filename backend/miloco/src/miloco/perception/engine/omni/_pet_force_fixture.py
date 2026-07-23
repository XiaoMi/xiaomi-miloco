# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""【test/pet-prompt-force 分支专用】pet prompt「完整带样本」注入的样本加载 + 合成。

把 `.wsh_cc/pet_samples_bundle` 的样本随分支内嵌进 package（同目录 `_pet_force_fixture/`：manifest +
`*_sheet.jpg` 多姿态拼图），强开门时**合成**家庭档案「## 宠物」名单 roster + `<pets>` 参考图，让同事
零配置拿到「完整真实形态」的 pet 注入。逻辑源自 `.wsh_cc/eval_harness/pet_inject.py` 的 pet_on 分支，
移进分支代码以便开箱即用。

三档由 `pet_samples_on()` 判定（见其 docstring）。任何加载失败都退空 + warning、**绝不抛**（识别是逐帧
热路径）。⚠️ 仅供感知回归测试，不并入 main。
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_FIXTURE_DIR = Path(__file__).resolve().parent / "_pet_force_fixture"
_MIN_JPEG_BYTES = 100  # 对齐 pet_refs：更短视为损坏，跳过

# 载入一次缓存：[(name, species, appearance, sheet_jpg_bytes)]
_cache: list[tuple] | None = None


def pet_samples_on() -> bool:
    """是否注入「完整带样本」（roster + <pets> 图）。三档：

    - ``MILOCO_FORCE_PET_PROMPT=0`` → False（关闭基线，pet prompt 整体不注入）
    - ``MILOCO_FORCE_PET_SAMPLES=0`` → False（纯文字消融：字段 + 命名纪律，无名单/图）
    - 默认（都不设）→ True（完整带样本）
    """
    if os.environ.get("MILOCO_FORCE_PET_PROMPT") == "0":
        return False
    if os.environ.get("MILOCO_FORCE_PET_SAMPLES") == "0":
        return False
    return True


def _load() -> list[tuple]:
    """读内嵌 fixture → [(name, species, appearance, sheet_jpg_bytes)]；失败/缺图退空。缓存一次。"""
    global _cache
    if _cache is not None:
        return _cache
    out: list[tuple] = []
    try:
        manifest = json.loads((_FIXTURE_DIR / "manifest.json").read_text("utf-8"))
    except Exception:  # noqa: BLE001 - 任意失败都退空，绝不影响主链路
        logger.warning("pet 样本 fixture: 读 manifest 失败 (%s)", _FIXTURE_DIR, exc_info=True)
        _cache = []
        return _cache
    for p in manifest:
        name = p.get("name")
        sheet = p.get("sheet")
        if not name or not sheet:
            continue
        try:
            jpg = (_FIXTURE_DIR / sheet).read_bytes()
        except Exception:  # noqa: BLE001
            logger.warning("pet 样本 fixture: 读图失败 %s", sheet)
            continue
        if not jpg or len(jpg) < _MIN_JPEG_BYTES:
            continue
        out.append((name, p.get("species", ""), p.get("appearance", ""), jpg))
    _cache = out
    return out


def synthetic_pet_section() -> str:
    """合成家庭档案「## 宠物」名单段（行首精确 ``## 宠物``，供 pet_identities 有名单可匹配）。空 → ""。"""
    pets = _load()
    if not pets:
        return ""
    lines = ["## 宠物"]
    lines += [f"- {name}（{sp}）：{appr}" for name, sp, appr, _ in pets]
    return "\n".join(lines)


def synthetic_pets_content(max_pets: int = 3) -> list[dict]:
    """合成 ``<pets>`` 多姿态参考图段（格式对齐 pet_refs._build_content）。空 → []。"""
    pets = _load()
    blocks: list[dict] = []
    for name, sp, _, jpg in pets[:max_pets]:
        b64 = base64.b64encode(jpg).decode()
        label = f"【{name}】" + (f"（{sp}）" if sp else "")
        blocks.append({"type": "text", "text": label})
        blocks.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )
    if not blocks:
        return []
    header = {
        "type": "text",
        "text": (
            "下方 <pets> 为家中已登记宠物的多姿态参考图，用于在画面中辨认并按「宠物命名」纪律"
            "称呼它们；图仅供比对个体，命名仍以名单与纪律为准。"
        ),
    }
    return [header, {"type": "text", "text": "<pets>"}, *blocks, {"type": "text", "text": "</pets>"}]
