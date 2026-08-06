# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""P4a：/api/identity/pets CRUD + 头像端点（TestClient 端到端）。

隔离 $MILOCO_HOME；token 默认空 → 鉴权跳过。DELETE 的家庭档案联动 stub get_manager，
聚焦路由本身（remove_subject 的真实行为另由 home_profile 测试覆盖）。
"""

from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 真·PNG 字节（cv2 可解码）：头像端点会 imdecode 验真 + 按魔数取真实格式，假字节被 400。
_PNG = cv2.imencode(".png", np.zeros((4, 4, 3), np.uint8))[1].tobytes()
# 真·JPEG：参考图端点同样验真（客户端本就传裁好的 JPEG）；两张内容不同以便区分 crop 顺序。
_JPEG = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))[1].tobytes()
_JPEG2 = cv2.imencode(".jpg", np.full((12, 12, 3), 255, np.uint8))[1].tobytes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings

    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
    reset_settings()
    from miloco.pet.router import router

    # 宠物端点默认在 pet_recognition 开启下测试；功能门本身由专门用例覆盖（关→404）。
    monkeypatch.setattr(
        "miloco.pet.router.get_settings",
        lambda: SimpleNamespace(
            features=SimpleNamespace(
                pet_recognition=True, pet_head_grounding=False, pet_body_grounding=True
            )
        ),
    )
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

    r = client.post(
        f"/api/identity/pets/{pet['id']}/avatar",
        files={"image": ("cat.png", _PNG, "image/png")},
    )
    assert r.status_code == 200
    assert r.json()["data"]["avatar_ext"] == "png"

    got = client.get(f"/api/identity/pets/{pet['id']}/avatar")
    assert got.status_code == 200
    assert got.content == _PNG
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
    r = _upload_refs(client, pet["id"], [_JPEG, _JPEG2], [10.0, 20.0])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["reference_crop_count"] == 2
    assert data["reference_crop_scores"] == [10.0, 20.0]
    got = client.get(f"/api/identity/pets/{pet['id']}/reference-crops/0")
    assert got.status_code == 200 and got.content == _JPEG
    assert got.headers["content-type"].startswith("image/jpeg")


def test_reference_crops_append_top3_by_score(client):
    pet = _create(client)
    _upload_refs(client, pet["id"], [_JPEG, _JPEG2], [1.0, 2.0])
    r = _upload_refs(client, pet["id"], [_JPEG2, _JPEG], [9.0, 8.0], mode="append")
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
    assert _upload_refs(client, pet["id"], [_JPEG], [1]).status_code == 200
    assert client.get(f"/api/identity/pets/{pet['id']}/reference-crops/5").status_code == 404


# ── 功能门：pet_recognition 关 → 注册类端点（建 / 头像 / 参考图）一律 404 ──────

def test_register_endpoints_404_when_disabled(client, monkeypatch):
    _stub_settings(monkeypatch, recognition=False)
    assert (
        client.post("/api/identity/pets", json={"name": "x", "species": "猫"}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/identity/pets/pet_000000000000/avatar",
            files={"image": ("a.png", _PNG, "image/png")},
        ).status_code
        == 404
    )
    assert _upload_refs(client, "pet_000000000000", [_PNG], [1]).status_code == 404


# ── 头像加固：与 person 端点对齐（体积上限 / 存在性前置 / 按魔数取真实格式）──────

def test_avatar_oversized_400(client):
    pet = _create(client)
    big = _PNG + b"\x00" * (5 * 1024 * 1024)
    r = client.post(
        f"/api/identity/pets/{pet['id']}/avatar",
        files={"image": ("a.png", big, "image/png")},
    )
    assert r.status_code == 400


def test_avatar_nonexistent_pet_404(client):
    r = client.post(
        "/api/identity/pets/pet_000000000000/avatar",
        files={"image": ("a.png", _PNG, "image/png")},
    )
    assert r.status_code == 404


def test_avatar_ext_from_content_not_filename(client):
    # 真 PNG 命名 x.jpg → 落盘扩展名按内容(魔数)取 png，不信文件名后缀
    pet = _create(client)
    r = client.post(
        f"/api/identity/pets/{pet['id']}/avatar",
        files={"image": ("x.jpg", _PNG, "image/jpeg")},
    )
    assert r.status_code == 200 and r.json()["data"]["avatar_ext"] == "png"


def test_avatar_bad_bytes_400(client):
    # 后缀合法、内容不可解码 → 400
    pet = _create(client)
    r = client.post(
        f"/api/identity/pets/{pet['id']}/avatar",
        files={"image": ("a.png", b"not-an-image", "image/png")},
    )
    assert r.status_code == 400


def test_reference_crops_append_over3_400(client):
    # append 现也在读盘前卡张数（此前只 replace 卡、append 无上限）
    pet = _create(client)
    r = _upload_refs(
        client, pet["id"], [b"a", b"b", b"c", b"d"], [1, 2, 3, 4], mode="append"
    )
    assert r.status_code == 400


def test_reference_crops_reject_non_image(client):
    # 回归：不验图的话任意字节都能存成 ref_crop_*.jpg 并**计入张数**，识别侧再静默跳过 →
    # 界面显示「N 张参考图」而实际注入 0 张。同头像端点口径：imdecode 验真 + 魔数白名单。
    pet = _create(client)
    assert _upload_refs(client, pet["id"], [b"not-an-image"], [1]).status_code == 400
    # 存量未被污染：一张都没落
    assert client.get(f"/api/identity/pets/{pet['id']}").json()["data"][
        "reference_crop_count"
    ] == 0


def test_reference_crops_oversized_400(client):
    # 单张 > 5MB → 400（读盘前 size 前置闸 / 读后 len 兜底）
    pet = _create(client)
    big = b"\x00" * (5 * 1024 * 1024 + 1)
    assert _upload_refs(client, pet["id"], [big], [1]).status_code == 400


# ── observe 端点：单文件体积闸（图 / 视频各用各的阈值）+ 模型输出不可解析 → 502 ──────

def test_observe_image_oversized_400(client, monkeypatch):
    # 单张图 > 15MB → 400，且**读盘前**就拒（observe_pet 不该被调用）
    called = []
    monkeypatch.setattr(
        "miloco.pet.observe.observe_pet", lambda *a, **k: called.append(1)
    )
    big = b"\x00" * (15 * 1024 * 1024 + 1)
    r = client.post(
        "/api/identity/pets:observe", files={"media": ("c.jpg", big, "image/jpeg")}
    )
    assert r.status_code == 400
    assert called == []


def test_observe_video_oversized_400(client, monkeypatch):
    # 视频走独立阈值：把上限调小再传超过它的包 → 400（不真造 100MB body）
    monkeypatch.setattr("miloco.pet.router._OBSERVE_VIDEO_MAX_BYTES", 1024)
    r = client.post(
        "/api/identity/pets:observe",
        files={"media": ("v.mp4", b"\x00" * 2048, "video/mp4")},
    )
    assert r.status_code == 400


def test_observe_video_not_capped_by_image_limit(client, monkeypatch):
    # 守住「闸门在判形之后」：6MB 视频 > 图片阈值但 < 视频阈值 → 不被误杀
    monkeypatch.setattr("miloco.pet.router._OBSERVE_IMAGE_MAX_BYTES", 1024)
    holder = _stub_observe_capture(monkeypatch)
    r = client.post(
        "/api/identity/pets:observe",
        files={"media": ("v.mp4", b"\x00" * 4096, "video/mp4")},
    )
    assert r.status_code == 200, r.text
    assert holder["is_video"] is True


def test_observe_unparsable_model_output_502(client, monkeypatch):
    # 模型没给可解析 JSON → OmniDescribeError → 502 + 可读原因（前端直接展示 message）
    from miloco.pet.observe import OmniDescribeError

    async def _boom(*a, **k):
        raise OmniDescribeError("模型未返回可解析的外观描述，请重试")

    monkeypatch.setattr("miloco.pet.observe.observe_pet", _boom)
    r = client.post(
        "/api/identity/pets:observe", files={"media": ("c.jpg", b"x", "image/jpeg")}
    )
    assert r.status_code == 502
    body = r.json()
    # 生产经全局 exception_handler 落在 message；裸 FastAPI（本测试 app）落在 detail
    assert "外观描述" in (body.get("message") or body.get("detail") or "")


# ── HEIC / ISO BMFF 判形（iPhone 相机默认格式）─────────────────────────────

def test_avatar_accepts_heic_and_stores_webp(client):
    """iPhone HEIC 传头像：原先被 400「不支持的图片格式」，现在解码后重编无损 webp 落盘。"""
    from tests.test_image_decode import HEIC_BYTES

    pet = _create(client)
    r = client.post(
        f"/api/identity/pets/{pet['id']}/avatar",
        files={"image": ("IMG_1.HEIC", HEIC_BYTES, "image/heic")},
    )
    assert r.status_code == 200
    assert r.json()["data"]["avatar_ext"] == "webp"
    got = client.get(f"/api/identity/pets/{pet['id']}/avatar")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/webp"


def test_reference_crops_accept_heic_and_store_jpeg(client):
    """参考图落 JPEG（非 webp）：唯一消费者 omni 恒收 JPEG，且 ref_crop_N.jpg 后缀是硬编码。"""
    from tests.test_image_decode import HEIC_BYTES

    pet = _create(client)
    r = client.post(
        f"/api/identity/pets/{pet['id']}/reference-crops",
        files=[("crops", ("a.HEIC", HEIC_BYTES, "image/heic"))],
        data={"mode": "replace", "scores": ["1.0"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["reference_crop_count"] == 1
    got = client.get(f"/api/identity/pets/{pet['id']}/reference-crops/0")
    assert got.status_code == 200
    assert got.content[:3] == b"\xff\xd8\xff"  # 真的是 JPEG，不是 webp


def test_observe_heic_declared_as_video_is_treated_as_image(client, monkeypatch):
    """回归：HEIF/AVIF 与 mp4/mov 共用 ftyp 容器，客户端把 HEIC 报成 video/* 时若按视频抽帧，
    ffmpeg 只给 512x512 瓦片（不拼 tile grid）→ 静默在错素材上跑。须按 brand 掰回图片路径。"""
    from tests.test_image_decode import HEIC_BYTES

    holder = {}

    async def _fake(medias, *, is_video, grounding, body_grounding=True, max_frames=60):
        holder["is_video"] = is_video
        return {"detected": False, "warnings": []}

    monkeypatch.setattr("miloco.pet.observe.observe_pet", _fake)
    r = client.post(
        "/api/identity/pets:observe",
        files={"media": ("clip.mov", HEIC_BYTES, "video/quicktime")},
    )
    assert r.status_code == 200, r.text
    assert holder["is_video"] is False  # 被掰回图片路径
