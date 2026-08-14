# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""快照结果的形状转换。"""

from __future__ import annotations

from typing import Any


def flatten(snapshot: dict) -> dict[str, Any]:
    """把快照的嵌套结果摊平成 `{完整路径: 叶子}`。

    叶子是什么取决于取快照时的 `with_meta`：不带元数据是裸值，带元数据是 `Entry`。两种
    都能摊，因为认叶子看的是类型不是键名。
    """
    flat: dict[str, Any] = {}
    _flatten_into(snapshot, "", flat)
    return flat


def _flatten_into(node: dict, prefix: str, flat: dict[str, Any]) -> None:
    for key, child in node.items():
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(child, dict):
            _flatten_into(child, path, flat)
        else:
            flat[path] = child
