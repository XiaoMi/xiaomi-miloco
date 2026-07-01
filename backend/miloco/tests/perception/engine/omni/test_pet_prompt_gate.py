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


def test_pet_rule_appended_when_has_pets_video():
    spec = render_field_spec(SceneDescriptor(route="video", has_pets=True))
    assert "## 宠物命名" in spec
    assert "逐项明确吻合" in spec  # §2.3(1) 收紧措辞


def test_pet_rule_absent_when_no_pets():
    spec = render_field_spec(SceneDescriptor(route="video", has_pets=False))
    assert "## 宠物命名" not in spec


def test_pet_rule_absent_on_audio_route_even_if_has_pets():
    # 宠物命名是视觉判断，audio 路由（无 caption）即便 has_pets 也不注入
    spec = render_field_spec(SceneDescriptor(route="audio", has_pets=True))
    assert "## 宠物命名" not in spec


def test_render_schema_unaffected_by_has_pets():
    # 宠物命名是内容纪律、非新输出字段 → JSON schema 不随 has_pets 变化
    on = SceneDescriptor(route="video", has_pets=True)
    off = SceneDescriptor(route="video", has_pets=False)
    assert render_schema(on) == render_schema(off)


def test_selected_fields_unaffected_by_has_pets():
    on = SceneDescriptor(route="video", has_pets=True)
    off = SceneDescriptor(route="video", has_pets=False)
    assert [f.name for f in on.selected_fields()] == [
        f.name for f in off.selected_fields()
    ]


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
