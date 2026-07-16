# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""宠物花名册 HTTP 路由（``/api/identity/pets``）。

与 persons 整齐并列、但代码独立、**不接 IdentityEngine**：CRUD 与头像走 ``PetLibrary``；
删除时联动清理家庭档案中绑定该宠物的条目（复用 ``HomeProfileService.remove_subject``）。

``pet:observe``（上传媒体生成外观描述）见后续阶段，不在本文件。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from miloco.config import get_settings
from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.perception.engine.identity.pet_library import (
    PetNameConflict,
    get_pet_library,
)
from miloco.schema.common_schema import NormalResponse

router = APIRouter(prefix="/identity", tags=["Pet"])

_PET_ID_RE = re.compile(r"^pet_[0-9a-f]{12}$")
_AVATAR_MEDIA = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


class PetCreate(BaseModel):
    name: str
    species: str = ""


class PetUpdate(BaseModel):
    name: str | None = None
    species: str | None = None


def _require_pet_id(pet_id: str) -> None:
    if not _PET_ID_RE.match(pet_id):
        raise HTTPException(status_code=400, detail="Invalid pet_id format")


@router.post("/pets:observe", summary="Observe Pet From Media", response_model=NormalResponse)
async def observe_pet_media(
    media: UploadFile = File(..., description="宠物图片或视频"),
    grounding: bool | None = Form(
        None, description="是否要头部 grounding；缺省取 features.pet_head_grounding"
    ),
    current_user: str = Depends(verify_token),
):
    """上传图/视频 → 选最优宠物 crop → omni 按维度生成外观描述（无副作用，不落库）。

    总开关 ``pet_recognition`` 关闭时该端点不可用；``grounding`` 缺省取
    ``features.pet_head_grounding``。
    """
    from miloco.pet.observe import observe_pet

    settings = get_settings()
    if not settings.features.pet_recognition:
        raise HTTPException(status_code=404, detail="pet recognition 未启用")
    raw = await media.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    ct = (media.content_type or "").lower()
    fn = (media.filename or "").lower()
    is_video = ct.startswith("video/") or fn.endswith(
        (".mp4", ".webm", ".mov", ".avi", ".mkv")
    )
    use_grounding = (
        settings.features.pet_head_grounding if grounding is None else grounding
    )
    # observe_pet 已支持多图（图 1~3 张）；本端点当前仅收单个 media，包成单元素列表调用
    # （向后兼容——多图上传入口随 P1b web 一起加，届时端点同时接单/多，避免部署顺序耦合）。
    # body_grounding（D7）仅影响回退路径（框不到猫狗时裁本体作参考图），取 feature 开关。
    result = await observe_pet(
        [raw],
        is_video=is_video,
        grounding=use_grounding,
        body_grounding=settings.features.pet_body_grounding,
    )
    return NormalResponse(code=0, message="OK", data=result)


@router.get("/pets", summary="List Pets", response_model=NormalResponse)
async def list_pets(current_user: str = Depends(verify_token)):
    pets = get_pet_library().list()
    return NormalResponse(
        code=0, message="OK", data={"pets": [p.model_dump() for p in pets]}
    )


@router.post("/pets", summary="Create Pet", response_model=NormalResponse)
async def create_pet(body: PetCreate, current_user: str = Depends(verify_token)):
    try:
        pet = get_pet_library().create(name=body.name, species=body.species)
    except PetNameConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return NormalResponse(code=0, message="Pet created", data=pet.model_dump())


@router.get("/pets/{pet_id}", summary="Get Pet", response_model=NormalResponse)
async def get_pet(pet_id: str, current_user: str = Depends(verify_token)):
    _require_pet_id(pet_id)
    pet = get_pet_library().get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail=f"Pet '{pet_id}' not found")
    return NormalResponse(code=0, message="OK", data=pet.model_dump())


@router.patch("/pets/{pet_id}", summary="Update Pet", response_model=NormalResponse)
async def update_pet(
    pet_id: str, body: PetUpdate, current_user: str = Depends(verify_token)
):
    _require_pet_id(pet_id)
    try:
        pet = get_pet_library().update(pet_id, name=body.name, species=body.species)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Pet '{pet_id}' not found") from e
    except PetNameConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return NormalResponse(code=0, message="Pet updated", data=pet.model_dump())


@router.delete("/pets/{pet_id}", summary="Delete Pet", response_model=NormalResponse)
async def delete_pet(pet_id: str, current_user: str = Depends(verify_token)):
    _require_pet_id(pet_id)
    removed = get_pet_library().delete(pet_id)
    # 联动清理家庭档案中绑定该宠物的条目；用 managed service（含 person_service），
    # 以便其内部 re-render 仍正确保留人类成员名册。
    cleanup = get_manager().home_profile_service.remove_subject(pet_id)
    return NormalResponse(
        code=0,
        message="Pet deleted" if removed else "Pet not found (no-op)",
        data={"removed": removed, **cleanup},
    )


@router.get("/pets/{pet_id}/avatar", summary="Get Pet Avatar")
async def get_pet_avatar(pet_id: str, current_user: str = Depends(verify_token)):
    _require_pet_id(pet_id)
    path = get_pet_library().avatar_path(pet_id)
    if path is None:
        raise HTTPException(status_code=404, detail="avatar 不存在")
    ext = path.suffix.lstrip(".").lower()
    return FileResponse(
        str(path), media_type=_AVATAR_MEDIA.get(ext, "application/octet-stream")
    )


@router.post(
    "/pets/{pet_id}/avatar", summary="Upload Pet Avatar", response_model=NormalResponse
)
async def upload_pet_avatar(
    pet_id: str,
    image: UploadFile = File(..., description="头像图片（jpg/jpeg/png/webp）"),
    current_user: str = Depends(verify_token),
):
    _require_pet_id(pet_id)
    data = await image.read()
    ext = Path(image.filename or "").suffix.lstrip(".").lower()
    try:
        pet = get_pet_library().set_avatar(pet_id, data=data, ext=ext)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Pet '{pet_id}' not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return NormalResponse(code=0, message="Avatar updated", data=pet.model_dump())


@router.post(
    "/pets/{pet_id}/reference-crops",
    summary="Set/Append Pet Reference Crops",
    response_model=NormalResponse,
)
async def upload_pet_reference_crops(
    pet_id: str,
    crops: list[UploadFile] = File(..., description="参考 crop（客户端已裁好的 JPEG）"),
    scores: list[float] = Form(
        [], description="每张的【绝对】质量分（conf×sharpness×area_ratio），与 crops 对齐"
    ),
    mode: str = Form(
        "replace", description="replace=整组替换（注册）/ append=追加（按绝对分留 top-3）"
    ),
    current_user: str = Depends(verify_token),
):
    """存客户端已裁好的参考 crop（③ 多姿态参照图）。服务端只存不裁（同 avatar 范式）。

    ``mode=replace`` 整组替换（注册时一次性写 ≤3）；``append`` 追加，与现有合并后
    按绝对质量分降序留 top-3（决策5(b)）。``scores`` 与 ``crops`` 对齐、缺省补 0。
    """
    _require_pet_id(pet_id)
    if mode not in ("replace", "append"):
        raise HTTPException(status_code=400, detail="mode 只能是 replace 或 append")
    if not crops:
        raise HTTPException(status_code=400, detail="no reference crops")
    data = [await c.read() for c in crops]
    if any(not d for d in data):
        raise HTTPException(status_code=400, detail="empty reference crop")
    if mode == "replace" and len(data) > 3:  # 存储层也会 cap，这里给更友好的 400
        raise HTTPException(status_code=400, detail="最多 3 张参考 crop")
    lib = get_pet_library()
    fn = lib.append_reference_crops if mode == "append" else lib.set_reference_crops
    try:
        pet = fn(pet_id, data, scores=scores or None)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Pet '{pet_id}' not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return NormalResponse(code=0, message="Reference crops updated", data=pet.model_dump())


@router.get(
    "/pets/{pet_id}/reference-crops/{idx}", summary="Get Pet Reference Crop"
)
async def get_pet_reference_crop(
    pet_id: str, idx: int, current_user: str = Depends(verify_token)
):
    """读第 idx 张参考 crop（调试/校验用；识别端 P2 直接读盘、不走 HTTP）。"""
    _require_pet_id(pet_id)
    paths = get_pet_library().reference_crop_paths(pet_id)
    if idx < 0 or idx >= len(paths):
        raise HTTPException(status_code=404, detail="reference crop 不存在")
    return FileResponse(str(paths[idx]), media_type="image/jpeg")
