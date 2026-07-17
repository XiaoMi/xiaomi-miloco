# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""P3：宠物命名规则的 has_pets gate + home_profile_has_pets 检测。

只触及 field_registry / home_profile_loader 的纯逻辑，不拉 prompt_builder 重依赖。
"""

from __future__ import annotations

from miloco.perception.engine.omni import home_profile_loader
from miloco.perception.engine.omni.field_registry import (
    SceneDescriptor,
    render_field_spec,
    render_schema,
)


def test_pet_field_and_naming_when_has_pets_video():
    # P3：has_pets && video → pet_identities 结构化字段（弃权纪律）进 schema+spec，
    # 且追加「## 宠物称呼」派生规则（caption/suggestions/matched_rules 从 pet_identities 派生）。
    on = SceneDescriptor(route="video", has_pets=True)
    spec = render_field_spec(on)
    assert "## pet_identities" in spec  # 结构化字段 spec（唯一真源）
    assert "## 宠物称呼" in spec  # 派生规则
    assert "疑似" in spec  # mid → 疑似档
    assert '"pet_identities"' in render_schema(on)  # 进 JSON schema


def test_pet_field_and_naming_absent_when_no_pets():
    off = SceneDescriptor(route="video", has_pets=False)
    spec = render_field_spec(off)
    assert "## pet_identities" not in spec and "## 宠物称呼" not in spec
    assert '"pet_identities"' not in render_schema(off)


def test_pet_absent_on_audio_route_even_if_has_pets():
    # pet_identities requires_video；「## 宠物称呼」仅 video 追加 → audio 两者皆无
    spec = render_field_spec(SceneDescriptor(route="audio", has_pets=True))
    assert "## pet_identities" not in spec and "## 宠物称呼" not in spec
    assert '"pet_identities"' not in render_schema(SceneDescriptor(route="audio", has_pets=True))


def test_pet_identities_field_gated_by_has_pets():
    # pet_identities 现是真字段：仅 has_pets 时进 selected_fields，且置于 caption 前；其余字段不变
    on = [f.name for f in SceneDescriptor(route="video", has_pets=True).selected_fields()]
    off = [f.name for f in SceneDescriptor(route="video", has_pets=False).selected_fields()]
    assert on == ["pet_identities", *off]
    assert "pet_identities" not in off


def test_home_profile_has_pets_true_when_section_present(monkeypatch):
    monkeypatch.setattr(
        home_profile_loader,
        "get_home_profile_prefix",
        lambda: "# 家庭档案\n\n## 家庭成员\n\n## 宠物\n\n### 小黑\n- 黑色短毛猫",
    )
    assert home_profile_loader.home_profile_has_pets() is True


def test_home_profile_has_pets_false_without_section(monkeypatch):
    monkeypatch.setattr(
        home_profile_loader,
        "get_home_profile_prefix",
        lambda: "# 家庭档案\n\n## 家庭成员\n\n### 爸爸\n- 主厨",
    )
    assert home_profile_loader.home_profile_has_pets() is False


def test_home_profile_has_pets_not_fooled_by_member_named_pets(monkeypatch):
    # 人类成员名恰为「宠物」会渲染成 ### 宠物，按行精确匹配不应误判为有宠物段
    monkeypatch.setattr(
        home_profile_loader,
        "get_home_profile_prefix",
        lambda: "# 家庭档案\n\n## 家庭成员\n\n### 宠物\n- 这其实是个人名",
    )
    assert home_profile_loader.home_profile_has_pets() is False
