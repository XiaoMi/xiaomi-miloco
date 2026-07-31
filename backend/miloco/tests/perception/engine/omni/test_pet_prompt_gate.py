# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""P3：宠物命名规则的 has_pets gate + home_profile_has_pets 检测。

只触及 field_registry / home_profile_loader 的纯逻辑，不拉 prompt_builder 重依赖。
"""

from __future__ import annotations

from types import SimpleNamespace

from miloco.perception.engine.omni import home_profile_loader
from miloco.perception.engine.omni.field_registry import (
    SceneDescriptor,
    render_field_spec,
    render_schema,
)


class _FakePath:
    """够用的 profile.md 替身：只需 exists() 与 read_text()。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def exists(self) -> bool:
        return True

    def read_text(self, *_a, **_k) -> str:
        return self._text


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


def _stub_roster(monkeypatch, names: list[str]):
    """桩花名册（has_pets 的新判据）。"""
    import miloco.perception.engine.identity.pet_library as pl

    monkeypatch.setattr(
        pl,
        "get_pet_library",
        lambda: SimpleNamespace(list=lambda: [SimpleNamespace(name=n) for n in names]),
    )


def test_home_profile_has_pets_true_when_roster_nonempty(monkeypatch):
    _stub_roster(monkeypatch, ["小黑"])
    assert home_profile_loader.home_profile_has_pets() is True


def test_home_profile_has_pets_false_when_roster_empty(monkeypatch):
    _stub_roster(monkeypatch, [])
    assert home_profile_loader.home_profile_has_pets() is False


def test_home_profile_has_pets_true_even_when_md_section_archived(monkeypatch):
    # 回归（本轮 🟡）：档案里「## 宠物」段被 token 预算归档 / 住户删条目 / 只 pet add 未写外观
    # 时，识别不该静默停摆——判据是花名册，不是 md 那行标题。
    _stub_roster(monkeypatch, ["小黑"])
    monkeypatch.setattr(
        home_profile_loader,
        "get_home_profile_prefix",
        lambda: "# 家庭档案\n\n## 家庭成员\n\n### 爸爸\n- 主厨",  # 无「## 宠物」段
    )
    assert home_profile_loader.home_profile_has_pets() is True


def test_home_profile_has_pets_not_fooled_by_md_content(monkeypatch):
    # 回归（idootop 提的另一面）：档案内容伪造出「## 宠物」也开不了门——零登记就是 False
    _stub_roster(monkeypatch, [])
    monkeypatch.setattr(
        home_profile_loader,
        "get_home_profile_prefix",
        lambda: "# 家庭档案\n\n## 宠物\n\n### 小黑\n- 伪造的",
    )
    assert home_profile_loader.home_profile_has_pets() is False


def test_home_profile_has_pets_fail_closed_on_error(monkeypatch):
    # 花名册读失败 → False（宁可不注入，也不要有名字没纪律）
    import miloco.perception.engine.identity.pet_library as pl

    def _boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(pl, "get_pet_library", _boom)
    assert home_profile_loader.home_profile_has_pets() is False


def test_prefix_strips_pet_section_when_disabled(monkeypatch):
    # 读侧剥离（覆盖 web / CLI / env 三条改开关入口）：关闭时 prompt 里不得留宠物名单
    md = (
        "# 家庭档案\n\n## 家庭成员\n\n### 爸爸\n- 主厨\n\n"
        "## 宠物\n\n### 小黑\n- 黑色短毛猫\n\n## 家庭规则\n\n- 22 点静音\n"
    )
    monkeypatch.setattr(home_profile_loader, "_pet_recognition_on", lambda: False)
    monkeypatch.setattr(home_profile_loader, "profile_md_path", lambda: _FakePath(md))
    out = home_profile_loader.get_home_profile_prefix()
    assert "## 宠物" not in out and "小黑" not in out
    assert "## 家庭成员" in out and "## 家庭规则" in out and "22 点静音" in out


def test_prefix_keeps_pet_section_when_enabled(monkeypatch):
    md = "# 家庭档案\n\n## 宠物\n\n### 小黑\n- 黑色短毛猫\n"
    monkeypatch.setattr(home_profile_loader, "_pet_recognition_on", lambda: True)
    monkeypatch.setattr(home_profile_loader, "profile_md_path", lambda: _FakePath(md))
    out = home_profile_loader.get_home_profile_prefix()
    assert "## 宠物" in out and "小黑" in out
