# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""P2：识别端宠物参考图注入（pet_refs.build_pet_reference_content）。

用 tmp PetLibrary 注入替代进程单例；断言 <pets> 段结构、坏图跳过、max_pets 截断、缓存复用、失败退空。
"""

from __future__ import annotations

import pytest
from miloco.perception.engine.identity.pet_library import PetLibrary
from miloco.perception.engine.omni import pet_refs

_JPEG = b"\xff\xd8\xff" + b"\x00" * 300  # >100 字节，过 _MIN_JPEG_BYTES


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    # 每例重置模块级缓存，避免跨例串扰
    monkeypatch.setattr(pet_refs, "_cache", {"sig": None, "content": []})


def _use_lib(monkeypatch, lib: PetLibrary):
    monkeypatch.setattr(
        "miloco.perception.engine.identity.pet_library.get_pet_library", lambda: lib
    )


def _texts(content):
    return [b["text"] for b in content if b["type"] == "text"]


def _imgs(content):
    return [b for b in content if b["type"] == "image_url"]


def test_no_pets_returns_empty(tmp_path, monkeypatch):
    _use_lib(monkeypatch, PetLibrary(root_dir=tmp_path))
    assert pet_refs.build_pet_reference_content() == []


def test_pet_with_refs_builds_pets_block(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    pet = lib.create(name="小黑", species="猫")
    lib.set_reference_crops(pet.id, [_JPEG, _JPEG], scores=[10.0, 5.0])
    _use_lib(monkeypatch, lib)

    content = pet_refs.build_pet_reference_content(max_pets=3)
    texts = _texts(content)
    assert "<pets>" in texts and "</pets>" in texts
    assert any("【小黑】" in t and "猫" in t for t in texts)
    imgs = _imgs(content)
    assert len(imgs) == 2
    assert imgs[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_short_crop_skipped(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    pet = lib.create(name="x", species="狗")
    lib.set_reference_crops(pet.id, [_JPEG, b"xy"], scores=[2.0, 1.0])  # 第二张过短
    _use_lib(monkeypatch, lib)
    assert len(_imgs(pet_refs.build_pet_reference_content())) == 1  # 短图被跳过


def test_pet_with_no_valid_crops_skipped(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    pet = lib.create(name="无图", species="猫")
    lib.set_reference_crops(pet.id, [b"xy"], scores=[1.0])  # 仅一张过短
    _use_lib(monkeypatch, lib)
    assert pet_refs.build_pet_reference_content() == []  # 无有效图 → 整体空


def test_max_pets_truncation(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    for name in ("a", "b", "c"):
        p = lib.create(name=name, species="猫")
        lib.set_reference_crops(p.id, [_JPEG], scores=[1.0])
    _use_lib(monkeypatch, lib)
    content = pet_refs.build_pet_reference_content(max_pets=2)
    labels = [t for t in _texts(content) if t.startswith("【")]
    assert len(labels) == 2  # 仅前 2 只


def test_cache_reused_when_signature_unchanged(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    p = lib.create(name="小黑", species="猫")
    lib.set_reference_crops(p.id, [_JPEG], scores=[1.0])
    _use_lib(monkeypatch, lib)
    c1 = pet_refs.build_pet_reference_content()
    c2 = pet_refs.build_pet_reference_content()
    assert c1 is c2  # 签名不变 → 复用同一对象（不重编码）


def test_cache_invalidated_on_ref_change(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    p = lib.create(name="小黑", species="猫")
    lib.set_reference_crops(p.id, [_JPEG], scores=[1.0])
    _use_lib(monkeypatch, lib)
    c1 = pet_refs.build_pet_reference_content()
    assert len(_imgs(c1)) == 1
    lib.append_reference_crops(p.id, [_JPEG], scores=[2.0])  # 参考图变了 → updated_at/count 变
    c2 = pet_refs.build_pet_reference_content()
    assert c2 is not c1 and len(_imgs(c2)) == 2  # 重建、含新图


def test_cache_invalidated_on_inplace_same_count_replace(tmp_path, monkeypatch):
    # 回归（对抗验证 #1）：同张数原地整组替换成不同 bytes → 缓存必须失效（stat 签名捕获）
    lib = PetLibrary(root_dir=tmp_path)
    p = lib.create(name="小黑", species="猫")
    lib.set_reference_crops(p.id, [_JPEG], scores=[1.0])
    _use_lib(monkeypatch, lib)
    url1 = _imgs(pet_refs.build_pet_reference_content())[0]["image_url"]["url"]
    # 换成不同内容且不同大小（size 进签名，不依赖 mtime 分辨率，稳）——张数仍为 1
    other = b"\xff\xd8\xff" + b"\x11" * 400
    lib.set_reference_crops(p.id, [other], scores=[1.0])
    url2 = _imgs(pet_refs.build_pet_reference_content())[0]["image_url"]["url"]
    assert url1 != url2  # 内容变 → 重编码，不复用旧 base64


def test_list_failure_degrades_to_empty(monkeypatch):
    # get_pet_library 抛错 → 退纯文字（[]），不炸上游 prompt
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "miloco.perception.engine.identity.pet_library.get_pet_library", _boom
    )
    assert pet_refs.build_pet_reference_content() == []
