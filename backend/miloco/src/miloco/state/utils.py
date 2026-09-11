# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""快照结果的形状转换与调试打印。"""

from __future__ import annotations

from typing import Any

from miloco.state.types import Entry

DUMP_MAX_LEAVES = 2000
"""dump 最多打几条叶子。叶子上限远高于人能读的行数，全打出来会把日志撑爆。"""

DUMP_PATH_WIDTH_MAX = 60
"""路径列最宽补到多少。一条超长路径不该把其余每一行都推到屏幕外。"""


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


def format_dump(snapshot: dict, now: int) -> str:
    """把快照排成每叶子一行的文本，给调试用。

    `snapshot` 必须是 `with_meta=True` 取的。按路径排序，两次 dump 可以直接 diff。
    """
    leaves = sorted(flatten(snapshot).items())
    if not leaves:
        return "state dump: empty"

    shown = leaves[:DUMP_MAX_LEAVES]
    width = min(max(len(path) for path, _ in shown), DUMP_PATH_WIDTH_MAX)
    lines = [f"state dump: {len(leaves)} leaves"]
    lines.extend(_format_leaf(path, entry, now, width) for path, entry in shown)
    hidden = len(leaves) - len(shown)
    if hidden:
        lines.append(f"... {hidden} more leaves not shown")
    return "\n".join(lines)


def _format_leaf(path: str, entry: Any, now: int, width: int) -> str:
    if not isinstance(entry, Entry):
        raise TypeError(f"{path} 没有元数据；dump 的快照要用 with_meta=True 取")
    # 值打 repr：容器按精确类型判等，看不出类型就查不了「同一个数写成两种类型」
    return (
        f"{path:<{width}} = {entry.value!r}  src={entry.source} "
        f"changed={_format_age(now - entry.last_changed)} "
        f"reported={_format_age(now - entry.last_reported)}"
    )


def _format_age(elapsed_ms: int) -> str:
    """相对时间比绝对时间戳好读：真正想知道的是「这条多久没动过」。"""
    seconds = elapsed_ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"
