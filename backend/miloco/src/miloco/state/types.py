# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""状态容器的值类型：`Entry` 叶子、`Change` 变更、`MISSING` 哨兵。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

Scalar: TypeAlias = str | int | float | bool | None

# 容器内的存储形态。写入接受 list，存进来统一转 tuple——`Entry` 是 frozen 只保证字段
# 不被重新赋值，挡不住调用方改 list 的内容。
StateValue: TypeAlias = Scalar | tuple[Scalar, ...]


class MissingType:
    """`MISSING` 的类型。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        # 取假：误用 `if store.get(path):` 时走否定分支，与「路径不存在」的直觉一致
        return False


MISSING = MissingType()
"""「不存在」哨兵。`None` 是合法状态值，不能兼任这个角色。"""


@dataclass(frozen=True, slots=True)
class Entry:
    """一个叶子。

    `slots=True` 不是可选的：不加会给每个实例挂一个用不上的 `__dict__`。

    `source` 记的是最后一次上报的来源，与 `last_reported` 同步——两个来源写同一路径同一个
    值时，`last_changed` 不动，`last_reported` 和 `source` 都更新成后写的那个。
    """

    value: StateValue
    last_changed: int
    last_reported: int
    source: str


@dataclass(frozen=True)
class Change:
    """一条叶子级变更。`old` 为 `MISSING` 表示新增，`new` 为 `MISSING` 表示删除。

    自带 `old` / `new` 的值是必需的：异步投递下回调拿到事件时容器可能已被后续写入改过，
    回头读当前值判断变化会漏边沿。
    """

    path: str
    old: StateValue | MissingType
    new: StateValue | MissingType
    source: str
    at: int


Node: TypeAlias = "Entry | dict[str, Node]"
"""树节点：叶子是 `Entry`，中间节点是 `dict`。"""
