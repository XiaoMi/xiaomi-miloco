# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""路径与 pattern 的纯函数行为。

`match_pattern` 只在投递链路上跑，绕 `_dispatch` 去测它抓不准——那里抛的异常会被 asyncio
吞掉，只剩一条 loop 级日志，断言「没收到事件」照样成立。
"""

from __future__ import annotations

import pytest
from miloco.state import StateStore
from miloco.state.path import match_pattern, parse_pattern, split_path


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("iot/dev1/online", "iot/dev1/online", True),
        ("iot/dev1/online", "iot/dev1", False),
        ("iot/dev1/online", "iot/dev1/online/extra", False),
        ("iot/*/online", "iot/dev1/online", True),
        ("iot/*", "iot/dev1/online", False),
        ("iot/*", "iot", False),
        ("iot/*/online", "iot/online", False),
        ("iot/**", "iot/dev1", True),
        ("iot/**", "iot/dev1/prop/2.1", True),
        ("iot/**", "iot", False),
        ("iot/**", "omni/dev1", False),
        ("iot", "iot", True),
        ("iot", "iot/dev1", False),
    ],
)
def test_match_pattern(pattern, path, matches):
    assert match_pattern(parse_pattern(pattern), split_path(path)) is matches


LEAVES = [
    "iot/dev1/online",
    "iot/dev1/prop/2.1",
    "iot/dev1/prop/2.2",
    "iot/dev2/online",
    "iot/dev2/prop/2.3",
    "omni/device/cam0/caption",
    "omni/rule/r1/dev1",
    "tracker/person/p1/room",
]

PATTERNS = [
    "iot/**",
    "iot/*/online",
    "iot/*/prop/*",
    "iot/*/prop/2.1",
    "iot/dev1/prop/2.1",
    "iot/dev1",
    "iot/*",
    "omni/*/*/caption",
    "omni/rule/**",
    "tracker",
    "tracker/**",
    "tracker/*/*/room",
    "nothing/**",
]


@pytest.mark.parametrize("pattern", PATTERNS)
def test_snapshot_and_match_pattern_agree(pattern):
    """快照走 `_collect_matches`、投递走 `match_pattern`，两套独立实现必须给出同一个集合。

    消费方的标准用法是「先 `snapshot` 对齐、再用同一个 pattern `subscribe` 跟增量」，两边
    一旦漂开就出现盲区，而各自的测试还都是绿的。
    """
    store = StateStore()
    for path in LEAVES:
        store._commit(path, 1, source="t")
    # 前提：LEAVES 之间不能互相覆盖，否则 by_match 会拿一份树里根本不存在的清单去比
    assert store._leaf_count == len(LEAVES)

    by_snapshot = sorted(_flatten(store.snapshot(pattern)))
    by_match = sorted(
        path
        for path in LEAVES
        if match_pattern(parse_pattern(pattern), split_path(path))
    )

    assert by_snapshot == by_match


def _flatten(tree: dict, prefix: str = "") -> list[str]:
    found = []
    for key, value in tree.items():
        path = f"{prefix}/{key}" if prefix else key
        if not isinstance(value, dict):
            found.append(path)
        elif value:
            found.extend(_flatten(value, path))
        else:
            # 空壳不该出现在快照里，原样报出来才会跟 by_match 对不上
            found.append(f"{path}/(empty)")
    return found
