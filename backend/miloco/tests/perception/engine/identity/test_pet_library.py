# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""PetLibrary 单元测试（宠物花名册磁盘存取）。

用 tmp_path 作 root 直接注入，不依赖 settings / identity_lib 真实根。
"""

from __future__ import annotations

import pytest
from miloco.perception.engine.identity.pet_library import (
    Pet,
    PetLibrary,
    PetNameConflict,
)


@pytest.fixture
def lib(tmp_path) -> PetLibrary:
    return PetLibrary(root_dir=tmp_path)


def test_create_get_list_roundtrip(lib: PetLibrary) -> None:
    pet = lib.create(name="小黑", species="猫")
    assert pet.id.startswith("pet_")
    assert pet.name == "小黑"
    assert pet.species == "猫"
    assert pet.avatar_ext is None
    assert pet.created_at and pet.updated_at

    got = lib.get(pet.id)
    assert got == pet  # pydantic 值相等
    assert [p.id for p in lib.list()] == [pet.id]
    assert lib.get_by_name("小黑").id == pet.id


def test_get_missing_returns_none(lib: PetLibrary) -> None:
    assert lib.get("pet_does_not_exist") is None
    assert lib.get_by_name("查无此宠") is None
    assert lib.list() == []


def test_create_duplicate_name_raises(lib: PetLibrary) -> None:
    lib.create(name="旺财", species="狗")
    with pytest.raises(PetNameConflict):
        lib.create(name="旺财", species="狗")


def test_create_empty_name_raises(lib: PetLibrary) -> None:
    with pytest.raises(ValueError):
        lib.create(name="  ", species="猫")


def test_update_name_and_species(lib: PetLibrary) -> None:
    pet = lib.create(name="小黑", species="猫")
    updated = lib.update(pet.id, name="小白", species="狗")
    assert updated.name == "小白"
    assert updated.species == "狗"
    # ISO 字符串可字典序比较，更新时刻不早于创建时刻
    assert updated.updated_at >= pet.created_at
    # 落盘生效
    assert lib.get(pet.id).name == "小白"
    assert lib.get_by_name("小黑") is None
    assert lib.get_by_name("小白").id == pet.id


def test_update_to_existing_name_raises(lib: PetLibrary) -> None:
    a = lib.create(name="A", species="猫")
    lib.create(name="B", species="狗")
    with pytest.raises(PetNameConflict):
        lib.update(a.id, name="B")
    # 改回自己的名字不算冲突
    assert lib.update(a.id, name="A").name == "A"


def test_update_missing_raises_keyerror(lib: PetLibrary) -> None:
    with pytest.raises(KeyError):
        lib.update("pet_nope", name="x")


def test_delete(lib: PetLibrary) -> None:
    pet = lib.create(name="小黑", species="猫")
    assert lib.delete(pet.id) is True
    assert lib.get(pet.id) is None
    assert lib.list() == []
    # 再删返回 False（幂等）
    assert lib.delete(pet.id) is False


def test_set_avatar_and_path(lib: PetLibrary) -> None:
    pet = lib.create(name="小黑", species="猫")
    assert lib.avatar_path(pet.id) is None

    updated = lib.set_avatar(pet.id, data=b"\xff\xd8\xff_fake_jpeg", ext="jpg")
    assert updated.avatar_ext == "jpg"
    path = lib.avatar_path(pet.id)
    assert path is not None and path.is_file()
    assert path.read_bytes() == b"\xff\xd8\xff_fake_jpeg"
    assert path.name == "avatar.jpg"


def test_set_avatar_ext_change_removes_old(lib: PetLibrary) -> None:
    pet = lib.create(name="小黑", species="猫")
    lib.set_avatar(pet.id, data=b"jpgdata", ext="jpg")
    lib.set_avatar(pet.id, data=b"pngdata", ext="png")
    avatars = sorted(p.name for p in lib._pet_dir(pet.id).glob("avatar.*"))
    assert avatars == ["avatar.png"]  # 旧 avatar.jpg 已清
    assert lib.avatar_path(pet.id).read_bytes() == b"pngdata"
    assert lib.get(pet.id).avatar_ext == "png"


def test_set_avatar_same_ext_overwrites(lib: PetLibrary) -> None:
    pet = lib.create(name="小黑", species="猫")
    lib.set_avatar(pet.id, data=b"v1", ext="png")
    lib.set_avatar(pet.id, data=b"v2", ext="png")
    avatars = sorted(p.name for p in lib._pet_dir(pet.id).glob("avatar.*"))
    assert avatars == ["avatar.png"]
    assert lib.avatar_path(pet.id).read_bytes() == b"v2"
    # meta 始终指向存在的文件
    assert lib.get(pet.id).avatar_ext == "png"


def test_set_avatar_unsupported_ext_raises(lib: PetLibrary) -> None:
    pet = lib.create(name="小黑", species="猫")
    with pytest.raises(ValueError):
        lib.set_avatar(pet.id, data=b"x", ext="gif")


def test_set_avatar_missing_pet_raises_keyerror(lib: PetLibrary) -> None:
    with pytest.raises(KeyError):
        lib.set_avatar("pet_nope", data=b"x", ext="png")


def test_meta_persisted_across_instances(tmp_path) -> None:
    """换一个 PetLibrary 实例（同 root）仍读得到——确认真落盘。"""
    lib1 = PetLibrary(root_dir=tmp_path)
    pet = lib1.create(name="小黑", species="猫")
    lib2 = PetLibrary(root_dir=tmp_path)
    assert lib2.get(pet.id) == pet


def test_pet_model_shape() -> None:
    """Pet 模型字段稳定（与 meta.json 契约一致）。"""
    assert set(Pet.model_fields) == {
        "id",
        "name",
        "species",
        "avatar_ext",
        "created_at",
        "updated_at",
    }
