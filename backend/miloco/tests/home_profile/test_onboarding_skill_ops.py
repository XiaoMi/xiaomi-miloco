# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""校验 miloco-onboarding SKILL.md 内嵌的 profile-write ops 示例与真实 schema/service 一致。

onboarding skill 让 agent 照抄示例结构生成 home-profile 批量 ops。示例若和
``ProfileWriteBody`` / ``ProfileOp`` / ``EntryPayload`` 漂移（字段名、type 枚举、结构），
线上 agent 就会照着写出被后端拒绝的 ops。这里把 SKILL.md 里 marker 标注的 JSON 示例
提取出来，先过 pydantic 校验，再经 ``HomeProfileService.profile_write(user_edit=True)``
真正落盘，确认全部 op ok 且落成 user_told / 1.0。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from miloco.home_profile.schema import ProfileWriteBody
from miloco.home_profile.service import HomeProfileService

# SKILL.md 里标记「待校验 ops 示例」的锚点，其后紧跟的 ```json 块被本测试提取。
_MARKER = "<!-- onboarding-ops-example -->"
_BLOCK_RE = re.compile(re.escape(_MARKER) + r"\s*```json\s*(.*?)```", re.DOTALL)


def _find_skill_md() -> Path | None:
    """从测试文件向上找 repo 根下的 plugins/skills/miloco-onboarding/SKILL.md。

    backend 包在 CI 里 checkout 到 repo 内，SKILL.md 在同一 repo 的 plugins/ 下；
    若在无 plugins 目录的隔离环境跑（仅装了 wheel），返回 None → 测试 skip。
    """
    for parent in Path(__file__).resolve().parents:
        cand = parent / "plugins" / "skills" / "miloco-onboarding" / "SKILL.md"
        if cand.exists():
            return cand
    return None


def _load_ops_blocks() -> list[list[dict]]:
    skill = _find_skill_md()
    if skill is None:
        pytest.skip("miloco-onboarding/SKILL.md 不在本 checkout 内（隔离环境）")
    blocks = [json.loads(m.group(1)) for m in _BLOCK_RE.finditer(skill.read_text(encoding="utf-8"))]
    assert blocks, "SKILL.md 未找到 <!-- onboarding-ops-example --> 后的 ```json 示例块"
    return blocks


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    yield


def test_ops_examples_parse_against_schema():
    """示例每个 op 都能被 ProfileWriteBody 接受（type 枚举、结构、字段名皆合法）。"""
    for ops in _load_ops_blocks():
        body = ProfileWriteBody(ops=ops, user_edit=True)
        assert body.ops, "示例 ops 数组为空"
        for op in body.ops:
            # onboarding 首次初始化只用 add；每个 add 必须带 entry。
            assert op.op == "add", f"onboarding 示例应全为 add，出现了 {op.op!r}"
            assert op.entry is not None, "add op 缺少 entry"
            assert op.entry.content, "entry.content 不可为空"


def test_ops_examples_apply_via_service():
    """示例经真实 service 落盘：全部 op ok，commit 后条目均为 user_told / 1.0。"""
    svc = HomeProfileService(person_service=None)
    total = 0
    for ops in _load_ops_blocks():
        body = ProfileWriteBody(ops=ops, user_edit=True)
        results = svc.profile_write(body.ops, user_edit=True)
        failed = [r.model_dump() for r in results if not r.ok]
        assert not failed, f"部分 op 未成功：{failed}"
        total += len(results)

    svc.commit()
    entries = svc.list_entries("profile")["profile"]
    assert len(entries) == total, "落盘条目数与 add op 数不一致"
    assert all(e["source"] == "user_told" for e in entries), "--user-edit 应置 source=user_told"
    assert all(e["confidence"] == 1.0 for e in entries), "--user-edit 应置 confidence=1.0"

    # 保留值 subject 的 subject_id 必须被清空（schema validator 契约）。
    for e in entries:
        if e["subject_name"] in ("shared", "general"):
            assert e["subject_id"] is None, "shared/general 条目不应绑定 subject_id"


def test_ops_examples_cover_all_entry_types():
    """示例应覆盖全部 8 种 entry type，作为 agent 的完整参照。"""
    seen = {
        op.entry.type
        for ops in _load_ops_blocks()
        for op in ProfileWriteBody(ops=ops, user_edit=True).ops
        if op.entry is not None
    }
    expected = {
        "member_persona", "member_health", "member_routine", "member_entertain",
        "member_preference", "family", "space", "device",
    }
    assert seen == expected, f"示例未覆盖全部 type，缺：{expected - seen}"
