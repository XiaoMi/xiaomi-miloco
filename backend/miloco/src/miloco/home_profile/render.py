# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""home_profile 渲染"""

from __future__ import annotations

import math
import re

from miloco.home_profile.schema import ProfileEntry

_CHARS_PER_TOKEN = 4
_NON_LATIN_RE = re.compile(
    r"[\u2E80-\u9FFF\uA000-\uA4FF\uAC00-\uD7AF\uF900-\uFAFF\U00020000-\U0002FA1F]"
)

MEMBER_TYPE_ORDER = [
    "member_persona",
    "member_health",
    "member_routine",
    "member_entertain",
    "member_preference",
]
_MEMBER_TYPES = set(MEMBER_TYPE_ORDER)


def estimate_tokens(text: str) -> int:
    non_latin = len(_NON_LATIN_RE.findall(text))
    adjusted = len(text) + non_latin * (_CHARS_PER_TOKEN - 1)
    return math.ceil(adjusted / _CHARS_PER_TOKEN)


def render_registered_members(members: list[dict]) -> str:
    """members: [{name, role?}, ...] —— 已注册成员清单块。"""
    if not members:
        return "(暂无已注册成员)"
    lines = []
    for m in members:
        parts = []
        if m.get("id"):
            parts.append(f"id:{m['id']}")
        parts.append(f"name:{m.get('name', '')}")
        if m.get("role"):
            parts.append(f"role:{m['role']}")
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


def _member_order(t: str) -> int:
    try:
        return MEMBER_TYPE_ORDER.index(t)
    except ValueError:
        return len(MEMBER_TYPE_ORDER)


def _resolve_member_subject(entry: ProfileEntry, members_by_id: dict[str, dict]) -> str:
    """member_* 条目的分组键：subject_id→当前名 优先，回落 subject_name / '未知成员'。

    'shared' 原样保留（渲染期归到「共享」分组）。
    """
    if entry.subject_name == "shared":
        return "shared"
    if entry.subject_id and entry.subject_id in members_by_id:
        m = members_by_id[entry.subject_id]
        return m.get("name") or "未知成员"
    if entry.subject_name:
        return entry.subject_name
    return "未知成员"


def _resolve_pet_subject(entry: ProfileEntry, pets_by_id: dict[str, dict]) -> str:
    """宠物条目的分组键：subject_id→花名册当前名 优先，回落 subject_name / '未知宠物'。"""
    if entry.subject_id and entry.subject_id in pets_by_id:
        return pets_by_id[entry.subject_id].get("name") or "未知宠物"
    if entry.subject_name:
        return entry.subject_name
    return "未知宠物"


def render_profile_markdown(
    entries: list[ProfileEntry],
    members: list[dict],
    pets: list[dict] | None = None,
) -> str:
    members_by_id = {m["id"]: m for m in members if m.get("id")}
    pets_by_id = {p["id"]: p for p in (pets or []) if p.get("id")}
    pet_names = {p["name"] for p in (pets or []) if p.get("name")}

    def _is_pet(e: ProfileEntry) -> bool:
        # 宠物主体判定：subject_id 命中花名册，或（旧数据）subject_id 空且 subject_name
        # 命中宠物名。pets 为空时恒 False（无花名册 → 不分宠物段，回退原行为）。
        return bool(e.subject_id and e.subject_id in pets_by_id) or (
            not e.subject_id and e.subject_name in pet_names
        )

    # 仅渲染调用方传入的条目；archived 过滤由调用方负责，
    # 此处不再自行过滤，否则 token 二分查找会漏算前缀中已归档条目。
    member_entries = [
        e for e in entries if e.type in _MEMBER_TYPES and not _is_pet(e)
    ]
    pet_entries = [e for e in entries if e.type in _MEMBER_TYPES and _is_pet(e)]
    family_entries = [e for e in entries if e.type == "family"]
    space_entries = [e for e in entries if e.type == "space"]
    device_entries = [e for e in entries if e.type == "device"]

    md = "# 家庭档案\n\n"

    # ─── 家庭成员 ───
    md += f"## 家庭成员\n\n{render_registered_members(members)}\n\n"

    if member_entries:
        resolved = [(_resolve_member_subject(e, members_by_id), e) for e in member_entries]
        subjects: list[str] = []
        for subj, _ in resolved:
            if subj != "shared" and subj not in subjects:
                subjects.append(subj)
                
        shared = sorted(
            (e for s, e in resolved if s == "shared"),
            key=lambda e: _member_order(e.type),
        )
        if shared:
            md += "### 共享\n\n"
            md += "\n".join(f"- {e.content}" for e in shared) + "\n\n"

        for subj in subjects:
            md += f"### {subj}\n\n"
            items = sorted(
                (e for s, e in resolved if s == subj),
                key=lambda e: _member_order(e.type),
            )
            md += "\n".join(f"- {e.content}" for e in items) + "\n\n"

    # ─── 宠物 ───
    if pet_entries:
        md += "## 宠物\n\n"
        resolved_pets = [(_resolve_pet_subject(e, pets_by_id), e) for e in pet_entries]
        pet_subjects: list[str] = []
        for subj, _ in resolved_pets:
            if subj not in pet_subjects:
                pet_subjects.append(subj)
        for subj in pet_subjects:
            md += f"### {subj}\n\n"
            items = sorted(
                (e for s, e in resolved_pets if s == subj),
                key=lambda e: _member_order(e.type),
            )
            md += "\n".join(f"- {e.content}" for e in items) + "\n\n"

    # ─── 家庭规则 ───
    if family_entries:
        md += "## 家庭规则\n\n"
        md += "\n".join(f"- {e.content}" for e in family_entries) + "\n\n"

    # ─── 空间信息 / 设备信息 ───
    for section, section_entries in (
        ("空间信息", space_entries),
        ("设备信息", device_entries),
    ):
        if not section_entries:
            continue
        md += f"## {section}\n\n"

        general = [e for e in section_entries if e.subject_name == "general"]
        named: list[str] = []
        for e in section_entries:
            key = e.subject_name
            if key and key != "general" and key not in named:
                named.append(key)

        if general:
            md += "### 通用\n\n"
            md += "\n".join(f"- {e.content}" for e in general) + "\n\n"

        for subj in named:
            md += f"### {subj}\n\n"
            items = [e for e in section_entries if e.subject_name == subj]
            md += "\n".join(f"- {e.content}" for e in items) + "\n\n"

    # ─── 未知类型兜底 ───
    other = [
        e
        for e in entries
        if e.type not in _MEMBER_TYPES
        and e.type not in ("family", "space", "device")
    ]
    if other:
        md += "## 其他\n\n"
        md += "\n".join(f"- {e.content}" for e in other) + "\n\n"

    return md.strip()
