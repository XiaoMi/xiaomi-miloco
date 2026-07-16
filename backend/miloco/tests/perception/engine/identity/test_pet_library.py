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
        "reference_crop_count",
        "reference_crop_scores",
        "created_at",
        "updated_at",
    }


# ── 参考 crop（③ 多姿态参照图；P1a-B 存储）────────────────────────────


def test_set_reference_crops_and_read(lib: PetLibrary) -> None:
    pet = lib.create(name="小黑", species="猫")
    assert lib.reference_crop_paths(pet.id) == []
    updated = lib.set_reference_crops(pet.id, [b"c0", b"c1"], scores=[10.0, 20.0])
    assert updated.reference_crop_count == 2
    paths = lib.reference_crop_paths(pet.id)
    assert [p.name for p in paths] == ["ref_crop_0.jpg", "ref_crop_1.jpg"]
    assert [p.read_bytes() for p in paths] == [b"c0", b"c1"]  # set 保序、不重排
    assert lib.reference_crop_scores(pet.id) == [10.0, 20.0]


def test_set_reference_crops_caps_at_3(lib: PetLibrary) -> None:
    pet = lib.create(name="x", species="猫")
    lib.set_reference_crops(pet.id, [b"a", b"b", b"c", b"d"], scores=[1, 2, 3, 4])
    paths = lib.reference_crop_paths(pet.id)
    assert [p.read_bytes() for p in paths] == [b"a", b"b", b"c"]  # 取前 3、连号


def test_append_keeps_top3_by_absolute_score(lib: PetLibrary) -> None:
    pet = lib.create(name="x", species="猫")
    lib.set_reference_crops(pet.id, [b"lo1", b"lo2"], scores=[1.0, 2.0])
    # 追加两张更高分 → 按绝对分留 top-3（决策5(b)，非 FIFO）
    lib.append_reference_crops(pet.id, [b"hi1", b"hi2"], scores=[9.0, 8.0])
    got = {p.read_bytes() for p in lib.reference_crop_paths(pet.id)}
    assert got == {b"hi1", b"hi2", b"lo2"}  # 分 9/8/2 胜出，lo1(1) 被挤掉
    assert lib.get(pet.id).reference_crop_count == 3
    assert lib.reference_crop_scores(pet.id) == [9.0, 8.0, 2.0]  # 与 crop 对齐、降序


def test_reference_crops_renumbered_no_gaps(lib: PetLibrary) -> None:
    pet = lib.create(name="x", species="猫")
    lib.set_reference_crops(pet.id, [b"a", b"b", b"c"], scores=[1, 2, 3])
    lib.set_reference_crops(pet.id, [b"z"], scores=[5])  # 替换成 1 张 → 清掉 1/2 槽
    names = sorted(p.name for p in lib._pet_dir(pet.id).glob("ref_crop_*.jpg"))
    assert names == ["ref_crop_0.jpg"]  # 连号无空洞
    assert lib.reference_crop_paths(pet.id)[0].read_bytes() == b"z"


def test_count_authoritative_ignores_stale_high_index(lib: PetLibrary) -> None:
    # 模拟崩溃残留：count=1 但盘上多出 ref_crop_1 → count 权威、切掉 stale
    pet = lib.create(name="x", species="猫")
    lib.set_reference_crops(pet.id, [b"keep"], scores=[1])
    (lib._pet_dir(pet.id) / "ref_crop_1.jpg").write_bytes(b"stale")
    assert [p.name for p in lib.reference_crop_paths(pet.id)] == ["ref_crop_0.jpg"]


def test_set_reference_crops_empty_data_raises(lib: PetLibrary) -> None:
    pet = lib.create(name="x", species="猫")
    with pytest.raises(ValueError):
        lib.set_reference_crops(pet.id, [b""], scores=[1])


def test_reference_crops_missing_pet_raises(lib: PetLibrary) -> None:
    with pytest.raises(KeyError):
        lib.set_reference_crops("pet_nope", [b"a"], scores=[1])


def test_delete_removes_reference_crops(lib: PetLibrary) -> None:
    pet = lib.create(name="x", species="猫")
    lib.set_reference_crops(pet.id, [b"a", b"b"], scores=[1, 2])
    d = lib._pet_dir(pet.id)
    assert list(d.glob("ref_crop_*.jpg"))
    lib.delete(pet.id)
    assert not d.exists()  # rmtree 整目录 → 参考 crop 随之清


def test_reference_crops_persist_across_instances(tmp_path) -> None:
    lib1 = PetLibrary(root_dir=tmp_path)
    pet = lib1.create(name="x", species="猫")
    lib1.set_reference_crops(pet.id, [b"a", b"b"], scores=[3, 4])
    lib2 = PetLibrary(root_dir=tmp_path)
    assert [p.read_bytes() for p in lib2.reference_crop_paths(pet.id)] == [b"a", b"b"]
    assert lib2.reference_crop_scores(pet.id) == [3.0, 4.0]
