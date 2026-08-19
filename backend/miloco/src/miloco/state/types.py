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
        # 布尔上下文里一律报错，不给真也不给假。`False` / `0` / `""` / `None` / `()` 都是
        # 合法状态值，取真取假都会让「路径不存在」和其中一半混进同一个分支 —— 而 omni 的
        # rule 判定正是拿「路径不存在」表达未就绪态的，混掉就读不出来了。
        raise TypeError(
            "MISSING 不能用在布尔上下文里：判存在写 `x is MISSING`，"
            "要兜底值传 `get(path, default)`"
        )


MISSING = MissingType()
"""「不存在」哨兵。`None` 是合法状态值，不能兼任这个角色。

放进布尔上下文会抛 `TypeError`（见 `MissingType.__bool__`），所以 `get(path) or fallback`
这种写法要改成 `get(path, fallback)` —— 前者对 `0` / `False` 本来也是错的。"""


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
