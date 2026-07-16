# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""P2：识别端宠物参考图注入（pet_refs.build_pet_reference_content）。

用 tmp PetLibrary 注入替代进程单例；断言 <pets> 段结构、每只一张 composite、坏图跳过、
max_pets 截断、缓存复用/失效、失败退空。参考图现为**每只 ≤3 张缩到统一高度横拼的 1 张 composite**
（对齐 pet_eval ③ 口径），故断言"每只 1 张 image_url"而非 N 张。
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from miloco.perception.engine.identity.pet_library import PetLibrary
from miloco.perception.engine.omni import pet_refs


def _jpeg(seed: int = 0, size: int = 48) -> bytes:
    """真实可解码 JPEG（种子噪声，确定性、>100 字节）。"""
    arr = np.random.default_rng(seed).integers(0, 255, (size, size, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", arr)
    return buf.tobytes()


_JPEG = _jpeg(1)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
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


def test_pet_with_refs_builds_one_composite(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    pet = lib.create(name="小黑", species="猫")
    lib.set_reference_crops(pet.id, [_jpeg(1), _jpeg(2)], scores=[10.0, 5.0])
    _use_lib(monkeypatch, lib)

    content = pet_refs.build_pet_reference_content(max_pets=3)
    texts = _texts(content)
    assert "<pets>" in texts and "</pets>" in texts
    assert any("【小黑】" in t and "猫" in t for t in texts)
    imgs = _imgs(content)
    assert len(imgs) == 1  # 2 张姿态 → 拼成 1 张 composite
    assert imgs[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_short_crop_skipped_then_composited(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    pet = lib.create(name="x", species="狗")
    lib.set_reference_crops(pet.id, [_jpeg(1), b"xy"], scores=[2.0, 1.0])  # 第二张过短
    _use_lib(monkeypatch, lib)
    assert len(_imgs(pet_refs.build_pet_reference_content())) == 1  # 短图跳过、剩 1 张成 composite


def test_pet_with_no_valid_crops_skipped(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    pet = lib.create(name="无图", species="猫")
    lib.set_reference_crops(pet.id, [b"xy"], scores=[1.0])  # 仅一张过短
    _use_lib(monkeypatch, lib)
    assert pet_refs.build_pet_reference_content() == []  # 无有效图 → 整体空


def test_undecodable_crop_skipped(tmp_path, monkeypatch):
    # 足够长但非图像字节 → imdecode 返回 None → 跳过
    lib = PetLibrary(root_dir=tmp_path)
    pet = lib.create(name="坏", species="猫")
    lib.set_reference_crops(pet.id, [b"\x00" * 500], scores=[1.0])
    _use_lib(monkeypatch, lib)
    assert pet_refs.build_pet_reference_content() == []


def test_max_pets_truncation(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    for i, name in enumerate(("a", "b", "c")):
        p = lib.create(name=name, species="猫")
        lib.set_reference_crops(p.id, [_jpeg(i)], scores=[1.0])
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
    assert c1 is c2  # 签名不变 → 复用同一对象


def test_cache_invalidated_on_append(tmp_path, monkeypatch):
    lib = PetLibrary(root_dir=tmp_path)
    p = lib.create(name="小黑", species="猫")
    lib.set_reference_crops(p.id, [_jpeg(1)], scores=[1.0])
    _use_lib(monkeypatch, lib)
    c1 = pet_refs.build_pet_reference_content()
    u1 = _imgs(c1)[0]["image_url"]["url"]
    lib.append_reference_crops(p.id, [_jpeg(2)], scores=[2.0])  # 参考图变 → 签名变
    c2 = pet_refs.build_pet_reference_content()
    assert c2 is not c1
    assert _imgs(c2)[0]["image_url"]["url"] != u1  # composite 变了（2 姿态）


def test_cache_invalidated_on_inplace_same_count_replace(tmp_path, monkeypatch):
    # 回归（对抗验证 #1）：同张数原地整组替换成不同 bytes → 缓存必须失效（stat 签名捕获）
    lib = PetLibrary(root_dir=tmp_path)
    p = lib.create(name="小黑", species="猫")
    lib.set_reference_crops(p.id, [_jpeg(1, 48)], scores=[1.0])
    _use_lib(monkeypatch, lib)
    u1 = _imgs(pet_refs.build_pet_reference_content())[0]["image_url"]["url"]
    lib.set_reference_crops(p.id, [_jpeg(9, 64)], scores=[1.0])  # 不同内容+不同尺寸，张数仍 1
    u2 = _imgs(pet_refs.build_pet_reference_content())[0]["image_url"]["url"]
    assert u1 != u2  # 内容变 → 重编码，不复用旧 base64


def test_list_failure_degrades_to_empty(monkeypatch):
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "miloco.perception.engine.identity.pet_library.get_pet_library", _boom
    )
    assert pet_refs.build_pet_reference_content() == []
