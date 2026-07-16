# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""P4a：/api/identity/pets CRUD + 头像端点（TestClient 端到端）。

隔离 $MILOCO_HOME；token 默认空 → 鉴权跳过。DELETE 的家庭档案联动 stub get_manager，
聚焦路由本身（remove_subject 的真实行为另由 home_profile 测试覆盖）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
    reset_settings()
    from miloco.pet.router import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    yield TestClient(app)
    reset_settings()


def _create(client, name="小黑", species="猫") -> dict:
    r = client.post("/api/identity/pets", json={"name": name, "species": species})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_create_list_get(client):
    pet = _create(client)
    assert pet["id"].startswith("pet_")
    assert pet["name"] == "小黑" and pet["species"] == "猫"

    listed = client.get("/api/identity/pets").json()["data"]["pets"]
    assert [p["id"] for p in listed] == [pet["id"]]

    got = client.get(f"/api/identity/pets/{pet['id']}").json()["data"]
    assert got == pet


def test_create_duplicate_name_409(client):
    _create(client, name="旺财", species="狗")
    r = client.post("/api/identity/pets", json={"name": "旺财", "species": "狗"})
    assert r.status_code == 409


def test_create_empty_name_400(client):
    r = client.post("/api/identity/pets", json={"name": "  ", "species": "猫"})
    assert r.status_code == 400


def test_get_unknown_404_and_bad_id_400(client):
    assert client.get("/api/identity/pets/pet_000000000000").status_code == 404
    assert client.get("/api/identity/pets/not-a-pet-id").status_code == 400


def test_update_name_species(client):
    pet = _create(client)
    r = client.patch(
        f"/api/identity/pets/{pet['id']}", json={"name": "小白", "species": "狗"}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["name"] == "小白" and data["species"] == "狗"
    assert client.get(f"/api/identity/pets/{pet['id']}").json()["data"]["name"] == "小白"


def test_update_unknown_404_and_dup_409(client):
    a = _create(client, name="A")
    _create(client, name="B", species="狗")
    assert (
        client.patch("/api/identity/pets/pet_000000000000", json={"name": "x"}).status_code
        == 404
    )
    assert (
        client.patch(f"/api/identity/pets/{a['id']}", json={"name": "B"}).status_code == 409
    )


def test_delete_with_homeprofile_cleanup(client, monkeypatch):
    calls = {}

    def _fake_remove(pid):
        calls["pid"] = pid
        return {"removed_profile": [], "removed_candidates": []}

    monkeypatch.setattr(
        "miloco.pet.router.get_manager",
        lambda: SimpleNamespace(
            home_profile_service=SimpleNamespace(remove_subject=_fake_remove)
        ),
    )
    pet = _create(client)
    r = client.delete(f"/api/identity/pets/{pet['id']}")
    assert r.status_code == 200
    assert r.json()["data"]["removed"] is True
    assert calls["pid"] == pet["id"]  # 联动按 pet_id 清档案
    assert client.get(f"/api/identity/pets/{pet['id']}").status_code == 404


def test_avatar_upload_and_get(client):
    pet = _create(client)
    assert client.get(f"/api/identity/pets/{pet['id']}/avatar").status_code == 404

    img = b"\x89PNG\r\n\x1a\n_fake_png_bytes"
    r = client.post(
        f"/api/identity/pets/{pet['id']}/avatar",
        files={"image": ("cat.png", img, "image/png")},
    )
    assert r.status_code == 200
    assert r.json()["data"]["avatar_ext"] == "png"

    got = client.get(f"/api/identity/pets/{pet['id']}/avatar")
    assert got.status_code == 200
    assert got.content == img
    assert got.headers["content-type"].startswith("image/png")


def test_avatar_bad_ext_400(client):
    pet = _create(client)
    r = client.post(
        f"/api/identity/pets/{pet['id']}/avatar",
        files={"image": ("cat.gif", b"gifdata", "image/gif")},
    )
    assert r.status_code == 400


def _stub_settings(
    monkeypatch, *, recognition: bool, grounding: bool = False, body_grounding: bool = True
):
    monkeypatch.setattr(
        "miloco.pet.router.get_settings",
        lambda: SimpleNamespace(
            features=SimpleNamespace(
                pet_recognition=recognition,
                pet_head_grounding=grounding,
                pet_body_grounding=body_grounding,
            )
        ),
    )


def test_observe_disabled_returns_404(client, monkeypatch):
    _stub_settings(monkeypatch, recognition=False)
    r = client.post(
        "/api/identity/pets:observe",
        files={"media": ("c.jpg", b"x", "image/jpeg")},
    )
    assert r.status_code == 404


def test_observe_enabled_returns_description(client, monkeypatch):
    _stub_settings(monkeypatch, recognition=True)

    async def _fake_observe(media, *, is_video, grounding, **kw):
        assert is_video is False  # image/jpeg → 非视频
        return {
            "detected": True,
            "description": {"species": "猫", "summary": "黑猫"},
            "head_bbox": None,
            "primary_crop_b64": "abc",
            "candidates": [],
        }

    monkeypatch.setattr("miloco.pet.observe.observe_pet", _fake_observe)
    r = client.post(
        "/api/identity/pets:observe",
        files={"media": ("c.jpg", b"jpgbytes", "image/jpeg")},
    )
    assert r.status_code == 200
    assert r.json()["data"]["description"]["species"] == "猫"


def test_observe_passes_body_grounding_from_feature(client, monkeypatch):
    # body_grounding（D7）取 features.pet_body_grounding 并透传给 observe_pet
    _stub_settings(monkeypatch, recognition=True, body_grounding=False)
    holder = {}

    async def _fake(medias, *, is_video, grounding, body_grounding=True, **kw):
        holder["body_grounding"] = body_grounding
        holder["medias_is_list"] = isinstance(medias, list)
        return {
            "detected": True,
            "description": {"species": "猫"},
            "head_bbox": None,
            "primary_crop_b64": "x",
            "candidates": [],
        }

    monkeypatch.setattr("miloco.pet.observe.observe_pet", _fake)
    r = client.post(
        "/api/identity/pets:observe",
        files={"media": ("c.jpg", b"x", "image/jpeg")},
    )
    assert r.status_code == 200
    assert holder["body_grounding"] is False  # 关时透传 False
    assert holder["medias_is_list"] is True  # 端点包成单元素列表（向后兼容）


def _stub_observe_capture(monkeypatch):
    """桩 observe_pet：记录收到的 medias 张数与 is_video，返回最简 detected。"""
    holder = {}

    async def _fake(medias, *, is_video, grounding, body_grounding=True, **kw):
        holder["n"] = len(medias)
        holder["is_video"] = is_video
        return {
            "detected": True,
            "description": {"species": "猫"},
            "head_bbox": None,
            "primary_crop_b64": "x",
            "candidates": [],
        }

    monkeypatch.setattr("miloco.pet.observe.observe_pet", _fake)
    return holder


def test_observe_multi_image_passes_list(client, monkeypatch):
    # 多图走 medias：2 张 → observe_pet 收 2 张、非视频
    _stub_settings(monkeypatch, recognition=True)
    holder = _stub_observe_capture(monkeypatch)
    r = client.post(
        "/api/identity/pets:observe",
        files=[
            ("medias", ("a.jpg", b"a", "image/jpeg")),
            ("medias", ("b.jpg", b"b", "image/jpeg")),
        ],
    )
    assert r.status_code == 200
    assert holder["n"] == 2 and holder["is_video"] is False


def test_observe_too_many_images_400(client, monkeypatch):
    _stub_settings(monkeypatch, recognition=True)
    _stub_observe_capture(monkeypatch)
    r = client.post(
        "/api/identity/pets:observe",
        files=[("medias", (f"{i}.jpg", b"x", "image/jpeg")) for i in range(4)],
    )
    assert r.status_code == 400


def test_observe_video_not_mixed_with_images_400(client, monkeypatch):
    _stub_settings(monkeypatch, recognition=True)
    _stub_observe_capture(monkeypatch)
    r = client.post(
        "/api/identity/pets:observe",
        files=[
            ("medias", ("v.mp4", b"v", "video/mp4")),
            ("medias", ("a.jpg", b"a", "image/jpeg")),
        ],
    )
    assert r.status_code == 400


def test_observe_single_video_ok(client, monkeypatch):
    _stub_settings(monkeypatch, recognition=True)
    holder = _stub_observe_capture(monkeypatch)
    r = client.post(
        "/api/identity/pets:observe",
        files=[("medias", ("v.mp4", b"v", "video/mp4"))],
    )
    assert r.status_code == 200
    assert holder["n"] == 1 and holder["is_video"] is True


def test_observe_no_media_400(client, monkeypatch):
    _stub_settings(monkeypatch, recognition=True)
    _stub_observe_capture(monkeypatch)
    r = client.post("/api/identity/pets:observe", data={"grounding": "false"})
    assert r.status_code == 400


# ── 参考 crop 端点（P1a-C）────────────────────────────────────────────


def _upload_refs(client, pet_id, crops, scores, mode="replace"):
    files = [("crops", (f"r{i}.jpg", b, "image/jpeg")) for i, b in enumerate(crops)]
    data = {"mode": mode}
    if scores is not None:
        data["scores"] = [str(s) for s in scores]
    return client.post(f"/api/identity/pets/{pet_id}/reference-crops", files=files, data=data)


def test_reference_crops_set_and_get(client):
    pet = _create(client)
    r = _upload_refs(client, pet["id"], [b"c0", b"c1"], [10.0, 20.0])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["reference_crop_count"] == 2
    assert data["reference_crop_scores"] == [10.0, 20.0]
    got = client.get(f"/api/identity/pets/{pet['id']}/reference-crops/0")
    assert got.status_code == 200 and got.content == b"c0"
    assert got.headers["content-type"].startswith("image/jpeg")


def test_reference_crops_append_top3_by_score(client):
    pet = _create(client)
    _upload_refs(client, pet["id"], [b"lo1", b"lo2"], [1.0, 2.0])
    r = _upload_refs(client, pet["id"], [b"hi1", b"hi2"], [9.0, 8.0], mode="append")
    assert r.status_code == 200
    assert r.json()["data"]["reference_crop_count"] == 3
    assert r.json()["data"]["reference_crop_scores"] == [9.0, 8.0, 2.0]  # 绝对分 top-3


def test_reference_crops_replace_over3_400(client):
    pet = _create(client)
    assert _upload_refs(client, pet["id"], [b"a", b"b", b"c", b"d"], [1, 2, 3, 4]).status_code == 400


def test_reference_crops_bad_mode_400(client):
    pet = _create(client)
    assert _upload_refs(client, pet["id"], [b"a"], [1], mode="weird").status_code == 400


def test_reference_crops_empty_400(client):
    pet = _create(client)
    assert _upload_refs(client, pet["id"], [b""], [1]).status_code == 400


def test_reference_crops_unknown_pet_404(client):
    assert _upload_refs(client, "pet_000000000000", [b"a"], [1]).status_code == 404


def test_reference_crops_get_out_of_range_404(client):
    pet = _create(client)
    _upload_refs(client, pet["id"], [b"c0"], [1])
    assert client.get(f"/api/identity/pets/{pet['id']}/reference-crops/5").status_code == 404
