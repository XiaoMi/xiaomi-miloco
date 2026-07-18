"""人类成员「显式头像」单测（B-统一：avatars/persons/<id>.<ext>，展示层）。

覆盖：库层 set/path/clear/list_exts + **零引擎扰动不变量**（设/删头像不建 persons/<id>/）；
端点解析优先级（显式头像 > 回落 tier_a face[0] > 404）；POST 存在性校验；DELETE 恢复默认。
"""
from __future__ import annotations

import io

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from miloco.perception.engine.identity.library import IdentityLibrary
from miloco.person import router as prouter
from miloco.person.router import (
    delete_person_avatar,
    get_person_avatar,
    upload_person_avatar,
)

_PID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def lib(tmp_path):
    return IdentityLibrary(tmp_path)


@pytest.fixture
def wired(monkeypatch, lib):
    """把路由的 _get_identity_library + manager.person_service 指到本 tmp 库/桩。"""
    monkeypatch.setattr(prouter, "_get_identity_library", lambda: lib)
    monkeypatch.setattr(
        prouter, "manager",
        type("M", (), {"person_service": type("P", (), {"exists": staticmethod(lambda pid: True)})()})(),
    )
    return lib


# ── 库层 ──────────────────────────────────────────────────────────────────

def test_set_path_clear(lib):
    assert lib.person_avatar_path(_PID) is None
    norm = lib.set_person_avatar(_PID, data=b"\xff\xd8\xffX", ext="JPG")
    assert norm == "jpg"
    p = lib.person_avatar_path(_PID)
    assert p is not None and p.is_file()
    assert p == lib.root / "avatars" / "persons" / f"{_PID}.jpg"
    assert lib.list_person_avatar_exts() == {_PID: "jpg"}
    lib.clear_person_avatar(_PID)
    assert lib.person_avatar_path(_PID) is None
    assert lib.list_person_avatar_exts() == {}


def test_ext_change_removes_old(lib):
    lib.set_person_avatar(_PID, data=b"a", ext="jpg")
    lib.set_person_avatar(_PID, data=b"b", ext="png")
    d = lib.root / "avatars" / "persons"
    assert sorted(x.name for x in d.glob(f"{_PID}.*")) == [f"{_PID}.png"]
    assert lib.person_avatar_path(_PID).read_bytes() == b"b"


def test_zero_engine_perturbation(lib):
    """**关键不变量**：给（没录入的）人设/删头像绝不创建 persons/<id>/ 目录，
    从而不会让空目录进 list_persons、扰动 IdentityEngine 快照。"""
    lib.set_person_avatar(_PID, data=b"x", ext="png")
    assert not (lib.persons_dir / _PID).exists()
    assert lib.list_persons() == []  # 引擎快照来源为空，无扰动
    lib.clear_person_avatar(_PID)
    assert not (lib.persons_dir / _PID).exists()


def test_bad_ext_raises(lib):
    with pytest.raises(ValueError):
        lib.set_person_avatar(_PID, data=b"x", ext="gif")


def test_bad_subject_id_raises(lib):
    """路径穿越防御：非法 id（含 / . 或 ..）在库层即 raise、不落盘。"""
    for bad in ("../etc/passwd", "a/b", "..", "x.y", ""):
        with pytest.raises(ValueError):
            lib.set_person_avatar(bad, data=b"x", ext="png")


# ── 端点：GET 解析优先级 ─────────────────────────────────────────────────────

async def test_get_explicit_avatar(wired, lib):
    lib.set_person_avatar(_PID, data=b"x", ext="png")
    resp = await get_person_avatar(_PID, current_user="t")
    assert isinstance(resp, FileResponse)
    assert str(resp.path).endswith(f"avatars/persons/{_PID}.png")


async def test_get_fallback_face(wired, lib):
    # 无显式头像但有 tier_a face → 回落 face[0]（旧行为）
    tier_a = lib.persons_dir / _PID / "tier_a"
    tier_a.mkdir(parents=True)
    (tier_a / "face_1.jpg").write_bytes(b"\xff\xd8\xffFACE")
    resp = await get_person_avatar(_PID, current_user="t")
    assert isinstance(resp, FileResponse)
    assert str(resp.path).endswith("tier_a/face_1.jpg")


async def test_get_404_when_none(wired):
    with pytest.raises(HTTPException) as ei:
        await get_person_avatar(_PID, current_user="t")
    assert ei.value.status_code == 404


async def test_explicit_beats_face(wired, lib):
    tier_a = lib.persons_dir / _PID / "tier_a"
    tier_a.mkdir(parents=True)
    (tier_a / "face_1.jpg").write_bytes(b"FACE")
    lib.set_person_avatar(_PID, data=b"EXPLICIT", ext="png")
    resp = await get_person_avatar(_PID, current_user="t")
    assert str(resp.path).endswith(f"avatars/persons/{_PID}.png")


# ── 端点：POST / DELETE ─────────────────────────────────────────────────────

async def test_post_sets_avatar(wired, lib):
    up = UploadFile(filename="a.png", file=io.BytesIO(b"\x89PNGdata"))
    res = await upload_person_avatar(_PID, image=up, current_user="t")
    assert res.code == 0 and res.data["avatar_ext"] == "png"
    assert lib.person_avatar_path(_PID) is not None


async def test_post_404_no_person(monkeypatch, lib):
    monkeypatch.setattr(prouter, "_get_identity_library", lambda: lib)
    monkeypatch.setattr(
        prouter, "manager",
        type("M", (), {"person_service": type("P", (), {"exists": staticmethod(lambda pid: False)})()})(),
    )
    up = UploadFile(filename="a.png", file=io.BytesIO(b"x"))
    with pytest.raises(HTTPException) as ei:
        await upload_person_avatar(_PID, image=up, current_user="t")
    assert ei.value.status_code == 404


async def test_post_bad_ext_400(wired):
    up = UploadFile(filename="a.gif", file=io.BytesIO(b"x"))
    with pytest.raises(HTTPException) as ei:
        await upload_person_avatar(_PID, image=up, current_user="t")
    assert ei.value.status_code == 400


async def test_delete_clears(wired, lib):
    lib.set_person_avatar(_PID, data=b"x", ext="png")
    res = await delete_person_avatar(_PID, current_user="t")
    assert res.code == 0 and res.data["avatar_ext"] is None
    assert lib.person_avatar_path(_PID) is None


async def test_bad_id_400(wired):
    with pytest.raises(HTTPException) as ei:
        await get_person_avatar("../etc/passwd", current_user="t")
    assert ei.value.status_code == 400
