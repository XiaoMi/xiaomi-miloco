# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""resolve_media_resolution 适配层测试.

覆盖:
- MiMo 三档映射（low→default, high→max, max→max）
- 空 model_name 透传原值
- 未知模型走 default preset（low→low, high→high, max→max）
- model 字符串包含 "mimo" 子串即可匹配（大小写不敏感）
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_config_module():
    """加载 perception.engine.config 模块，优先走标准包导入。

    当仓库尚未以可编辑模式安装、或其重型依赖（如 pydantic）在当前解释器
    不可用时，直接按文件路径加载该模块，使本测试可独立运行。
    """
    try:
        module = importlib.import_module(
            "miloco.perception.engine.config"
        )
        if hasattr(module, "resolve_media_resolution"):
            return module
    except Exception:
        pass

    here = Path(__file__).resolve().parent
    # tests/ → src/miloco/perception/engine/config.py
    src_root = here.parent / "src"
    config_path = src_root / "miloco" / "perception" / "engine" / "config.py"
    spec = importlib.util.spec_from_file_location(
        "miloco.perception.engine.config", config_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_config = _load_config_module()
resolve_media_resolution = _config.resolve_media_resolution


# ---- MiMo 三档映射 ---------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("low", "default"),
        ("high", "max"),
        ("max", "max"),
    ],
)
def test_mimo_preset_mapping(value, expected):
    # MiMo 默认 model；max 解除单帧 300 token 上限，default 保留上限
    assert resolve_media_resolution(value, "xiaomi/mimo-v2.5") == expected


def test_mimo_max_unsets_token_cap():
    """max 在 MiMo 上映射为 "max"（解除单帧 300 token 上限），非"最高分辨率"。"""
    assert resolve_media_resolution("max", "xiaomi/mimo-v2.5") == "max"


def test_mimo_low_keeps_token_cap():
    """low 在 MiMo 上映射为 "default"，即保留单帧 300 token 上限。"""
    assert resolve_media_resolution("low", "xiaomi/mimo-v2.5") == "default"


# ---- 空 model_name 透传 ----------------------------------------------------

@pytest.mark.parametrize("value", ["low", "high", "max", "default", "anything"])
def test_empty_model_name_passes_through(value):
    assert resolve_media_resolution(value, "") == value


def test_none_equivalent_to_empty():
    # 调用方可能传入 None（来自可选配置），等价于空字符串透传
    assert resolve_media_resolution("high", None) == "high"  # type: ignore[arg-type]


# ---- 未知模型走 default preset --------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("low", "low"),
        ("high", "high"),
        ("max", "max"),
    ],
)
def test_unknown_model_uses_default_preset(value, expected):
    # 未在 MEDIA_RESOLUTION_PRESETS 中显式列出的模型走 default 透传
    assert resolve_media_resolution(value, "qwen/qwen2.5-vl") == expected


def test_unknown_model_unknown_value_passes_through():
    assert resolve_media_resolution("ultra", "qwen/qwen2.5-vl") == "ultra"


# ---- model 子串匹配（大小写不敏感）----------------------------------------

def test_mimo_substring_matches_case_insensitive():
    assert resolve_media_resolution("high", "XIAOMI/MIMO-V2.5") == "max"


def test_mimo_substring_in_custom_model_name():
    # 只要 model 字符串包含 "mimo" 子串即可命中 mimo preset
    assert resolve_media_resolution("high", "my-mimo-finetune-7b") == "max"


def test_non_mimo_model_not_matched_by_partial():
    # "mimosaic" 含 "mimo" 子串会命中——这是子串匹配的预期行为，
    # 这里确认不含 "mimo" 的模型确实走 default
    assert resolve_media_resolution("high", "llava-next") == "high"
