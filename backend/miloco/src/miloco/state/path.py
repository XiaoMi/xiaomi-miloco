# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""路径与 pattern 的解析和匹配。

路径和 pattern 共用一套段语法，所以路径段内一律不许出现 `*`——否则 `iot/*/online`
无法回答是「所有设备的 online」还是「id 恰好叫 `*` 那台的 online」。
"""

from __future__ import annotations

MAX_SEGMENT_LENGTH = 128
MAX_DEPTH = 16


def validate_segment(segment: str) -> None:
    """校验单个路径段。payload 的 dict key 也走这里，两个入口只有一套规则。"""
    if not isinstance(segment, str):
        raise TypeError(f"路径段必须是 str，收到 {type(segment).__name__}")
    if not segment:
        raise ValueError("路径段不能为空")
    if "/" in segment:
        raise ValueError(f"路径段不能含 '/'：{segment!r}")
    if "*" in segment:
        raise ValueError(f"路径段不能含 '*'：{segment!r}")
    if len(segment) > MAX_SEGMENT_LENGTH:
        raise ValueError(f"路径段超过 {MAX_SEGMENT_LENGTH} 字符：{segment[:32]!r}...")


def split_path(path: str) -> tuple[str, ...]:
    """`"iot/dev1/online"` → `("iot", "dev1", "online")`，非法路径抛异常。"""
    if not isinstance(path, str):
        raise TypeError(f"路径必须是 str，收到 {type(path).__name__}")
    segments = tuple(path.split("/"))
    if len(segments) > MAX_DEPTH:
        raise ValueError(f"路径深度超过 {MAX_DEPTH} 层：{path!r}")
    for segment in segments:
        validate_segment(segment)
    return segments


def parse_pattern(pattern: str) -> tuple[str, ...]:
    """解析订阅 / 快照 pattern，非法一律抛异常，不静默降级成「匹配不到」。"""
    if not isinstance(pattern, str):
        raise TypeError(f"pattern 必须是 str，收到 {type(pattern).__name__}")
    segments = tuple(pattern.split("/"))
    if len(segments) > MAX_DEPTH:
        raise ValueError(f"pattern 深度超过 {MAX_DEPTH} 层：{pattern!r}")
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment == "**":
            if index != last:
                raise ValueError(f"'**' 只能出现在最后一段：{pattern!r}")
            continue
        if segment == "*":
            continue
        validate_segment(segment)
    return segments


def match_pattern(pattern: tuple[str, ...], segments: tuple[str, ...]) -> bool:
    """pattern 是否匹配这条路径。`*` 匹配单段，`**` 匹配末尾一段或多段。"""
    for index, expected in enumerate(pattern):
        if expected == "**":
            return len(segments) > index
        if index >= len(segments):
            return False
        if expected != "*" and expected != segments[index]:
            return False
    return len(segments) == len(pattern)
