# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""identity_assignments 反查回归测试。

双标签上线后, prompt 把成员标注成复合标签"真名(角色:X)"并约束 omni 从中选一个输出,
omni 倾向整串回显。本测试锁住: 真名 / 角色 / 复合标签(含全角括号变体) 任一回显, 都能
反查回正确 person_id, 不被误判 unknown——防止"有家庭角色的成员识别后被打回 unknown"回归。
"""

import json
from collections import Counter

from miloco.perception.engine.omni.response_parser import parse_identity_assignments

PID = "8278c2c9-4c28-40f2-9c92-e501ecac349c"


def _name_to_pid(name: str, role: str | None) -> dict[str, str]:
    """构造解析器单测的别名输入。"""
    n2p: dict[str, str] = {}
    if role:
        label = f"{name}(角色:{role})"
        n2p[label] = PID
    else:
        label = name
        n2p[label] = PID
    n2p[name] = PID
    if role:
        n2p[role] = PID
    n2p[PID] = PID
    return n2p


def _resolve(omni_name: str, *, role: str | None = "爸爸") -> str | None:
    raw = json.dumps(
        {"identity_assignments": [{"track_id": 1, "name": omni_name, "confidence": 0.9}]}
    )
    res = parse_identity_assignments(
        raw,
        name_to_pid=_name_to_pid("张三", role),
        prompt_track_ids={1},
        confidence_cutoff=0.5,
    )
    return res[0]["person_id"] if res else None


def test_reverse_map_composite_label_hits():
    # omni 遵守约束回显完整复合标签 → 必须命中 pid（这是 .5 实测过的回归点）
    assert _resolve("张三(角色:爸爸)") == PID


def test_reverse_map_plain_name_hits():
    assert _resolve("张三") == PID


def test_reverse_map_role_only_hits():
    assert _resolve("爸爸") == PID


def test_reverse_map_fullwidth_paren_via_strip():
    # omni 用全角括号变体 → response_parser 剥尾部括号退回真名命中
    assert _resolve("张三（角色：爸爸）") == PID


def test_reverse_map_role_empty_member_plain_name():
    # role 为空的成员：标签退化成纯真名，仍命中
    assert _resolve("张三", role=None) == PID


def test_reverse_map_unknown_stays_unknown():
    assert _resolve("unknown") == "unknown"


def test_reverse_map_stranger_not_in_gallery():
    # 库里没有的名字 → unknown（不能误命中）
    assert _resolve("李四") == "unknown"


# ---- role 不唯一时的反查 guard（role 可空、不唯一，schema 不拦两人同角色）----

PID_A = "11111111-1111-4111-8111-111111111111"
PID_B = "22222222-2222-4222-8222-222222222222"


def _name_to_pid_multi(persons: list[tuple[str, str, str | None]]) -> dict[str, str]:
    """构造含角色歧义的解析器单测输入：[(pid, name, role), ...]。"""
    n2p: dict[str, str] = {}
    role_counts = Counter(r for _, _, r in persons if r)
    for pid, name, role in persons:
        if name:
            label = f"{name}(角色:{role})" if role else name
            n2p.setdefault(label, pid)
            n2p.setdefault(name, pid)
            if role and role_counts[role] == 1:
                n2p.setdefault(role, pid)
        n2p.setdefault(pid, pid)
    return n2p


def _resolve_with_map(omni_name: str, n2p: dict[str, str]) -> str | None:
    raw = json.dumps(
        {"identity_assignments": [{"track_id": 1, "name": omni_name, "confidence": 0.9}]}
    )
    res = parse_identity_assignments(
        raw, name_to_pid=n2p, prompt_track_ids={1}, confidence_cutoff=0.5
    )
    return res[0]["person_id"] if res else None


def test_reverse_map_duplicate_role_not_built():
    # 两人同 role("爷爷")：role 不唯一 → 纯 role key 不建立，omni 简化输出"爷爷"落 unknown,
    # 绝不能误命中最早遍历到的那个人。各自真名 / 复合标签仍精确命中本人。
    n2p = _name_to_pid_multi([(PID_A, "张三", "爷爷"), (PID_B, "李四", "爷爷")])
    assert "爷爷" not in n2p
    assert _resolve_with_map("爷爷", n2p) == "unknown"
    assert _resolve_with_map("张三", n2p) == PID_A
    assert _resolve_with_map("李四", n2p) == PID_B
    assert _resolve_with_map("张三(角色:爷爷)", n2p) == PID_A
    assert _resolve_with_map("李四(角色:爷爷)", n2p) == PID_B


def test_reverse_map_unique_role_still_built():
    # 角色全局唯一（一爷爷一奶奶）→ role key 照建，纯 role 回显仍命中
    n2p = _name_to_pid_multi([(PID_A, "张三", "爷爷"), (PID_B, "王五", "奶奶")])
    assert _resolve_with_map("爷爷", n2p) == PID_A
    assert _resolve_with_map("奶奶", n2p) == PID_B
