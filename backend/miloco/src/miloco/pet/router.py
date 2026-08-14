# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""宠物花名册 HTTP 路由（``/api/identity/pets``）。

与 persons 整齐并列、但代码独立、**不接 IdentityEngine**：CRUD 与头像走 ``PetLibrary``；
删除时联动清理家庭档案中绑定该宠物的条目（复用 ``HomeProfileService.remove_subject``）。

``pet:observe``（上传媒体生成外观描述）见后续阶段，不在本文件。
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from miloco.config import get_settings
from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.perception.engine.identity import _avatar
from miloco.perception.engine.identity._image_utils import is_still_image_container
from miloco.perception.engine.identity.pet_library import (
    PetNameConflict,
    get_pet_library,
)
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/identity", tags=["Pet"])

_PET_ID_RE = re.compile(r"^pet_[0-9a-f]{12}\Z")  # \Z 而非 $：与 person 一致，禁尾随换行 id
_MAX_REF_CROPS = 3  # 参考图上传张数上限（与 pet_library._MAX_REF_CROPS 同口径）
# observe 收的是**未裁剪原图 / 原视频**（非头像那种客户端裁好的 20-50KB 产物），故不复用
# _avatar.AVATAR_MAX_BYTES：高像素手机照可达 10-25MB，卡 5MB 会误拒真实照片。
_OBSERVE_IMAGE_MAX_BYTES = 15 * 1024 * 1024  # 单张图上限
_OBSERVE_VIDEO_MAX_BYTES = 100 * 1024 * 1024  # 视频上限（≤15s 4K 手机片仍放过，拦「传整部电影」）


class PetCreate(BaseModel):
    name: str
    species: str = ""


class PetUpdate(BaseModel):
    name: str | None = None
    species: str | None = None


def _require_pet_id(pet_id: str) -> None:
    if not _PET_ID_RE.match(pet_id):
        raise HTTPException(status_code=400, detail="Invalid pet_id format")


def _resync_home_profile(action: str, pet_id: str) -> None:
    """花名册变更后重渲家庭档案，让 prompt 里的宠物名单与花名册同步。

    识别门（``home_profile_has_pets``）以花名册为判据、**立即**生效，而 prompt 里的名单来自
    ``profile.md``（commit 产物）。建档 / 改名后不重渲，就会出现「门开了而名单缺失或还是旧名字」。
    与 delete 的联动清理、admin ``set_features`` 的重渲同一范式；失败只告警——花名册已经写成功，
    不能因为展示层重渲失败就把 2xx 变 5xx（下次任何 commit 会自愈）。

    ``pet_id`` 只进日志。调用方须传服务端生成的 id，或已过 ``_require_pet_id`` 白名单
    （``^pet_[0-9a-f]{12}\\Z``）——否则那行 warning 就成了日志注入的落点。
    """
    try:
        get_manager().home_profile_service.commit()
    except Exception:  # noqa: BLE001
        logger.warning("宠物%s后重渲家庭档案失败: pet_id=%s", action, pet_id, exc_info=True)


def _require_pet_enabled() -> None:
    # 总开关关闭时，宠物「注册」链路整体不可用（建花名册 / 头像 / 参考图 / observe）；
    # 纯家庭事实由 miloco-home-profile 走 subject_id 留空记录。
    # 门控口径（**刻意**）：只卡「新增 / 写入注册数据」。读取（list / get / 头像 / 参考图）
    # 与存量管理（update / delete）不门控——关掉开关后仍要能查看、改名、删除已录宠物并联动清档案。
    if not get_settings().features.pet_recognition:
        raise HTTPException(status_code=404, detail="pet recognition 未启用")


_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".avi", ".mkv")


def _is_video_upload(u: UploadFile) -> bool:
    ct = (u.content_type or "").lower()
    fn = (u.filename or "").lower()
    return ct.startswith("video/") or fn.endswith(_VIDEO_EXTS)


@router.post("/pets:observe", summary="Observe Pet From Media", response_model=NormalResponse)
async def observe_pet_media(
    media: UploadFile | None = File(
        None, description="宠物图片或视频（单个，向后兼容旧前端）"
    ),
    medias: list[UploadFile] | None = File(
        None, description="宠物图片 1~3 张（多图注册）；视频仍恰 1 个，不与图混传"
    ),
    grounding: bool | None = Form(
        None, description="是否要头部 grounding；缺省取 features.pet_head_grounding"
    ),
    current_user: str = Depends(verify_token),
):
    """上传图（1~3 张）/视频（1 个）→ 门控选 ≤3 张同一只 crop → omni 一次性生成共性描述（无副作用，不落库）。

    总开关 ``pet_recognition`` 关闭时该端点不可用；``grounding`` 缺省取
    ``features.pet_head_grounding``。**向后兼容**：单个走 ``media``、多图走 ``medias``，两者取其一。
    """
    from miloco.pet.observe import MediaDecodeError, OmniDescribeError, observe_pet

    settings = get_settings()
    _require_pet_enabled()
    # 多图优先走 medias；两者都传时 medias 胜（单个 media 仅为旧前端兼容）。
    uploads = [u for u in (medias or []) if u is not None] or ([media] if media else [])
    if not uploads:
        raise HTTPException(status_code=400, detail="no media")
    # 先按头部（content_type/filename）判形，超量 / 混传 / 超大在**读盘前**就拒，
    # 避免把将被 400 的请求整批读进内存（multipart 最多 1000 个文件片，无单片大小上限）。
    any_video = any(_is_video_upload(u) for u in uploads)
    if any_video:
        if len(uploads) != 1:
            raise HTTPException(status_code=400, detail="视频仅支持单个文件、且不与图片混传")
        is_video = True
    else:
        if len(uploads) > 3:
            raise HTTPException(status_code=400, detail="最多 3 张图片")
        is_video = False
    # 体积闸（判形之后，图 / 视频各用各的阈值）：前置看 multipart 自带字节数，读后 len 兜底
    # （size 缺失时），同 avatar / reference-crops 口径。
    cap = _OBSERVE_VIDEO_MAX_BYTES if is_video else _OBSERVE_IMAGE_MAX_BYTES
    too_big = "视频过大（上限 100 MB）" if is_video else "图片过大（单张上限 15 MB）"
    if any(u.size is not None and u.size > cap for u in uploads):
        raise HTTPException(status_code=400, detail=too_big)
    raws: list[bytes] = []
    for u in uploads:
        raw = await u.read()
        if len(raw) > cap:  # size 缺失时兜底
            raise HTTPException(status_code=400, detail=too_big)
        if not raw:
            raise HTTPException(status_code=400, detail="empty file")
        raws.append(raw)
    # 判形复核（读到字节之后）：HEIF/AVIF 与 mp4/mov 共用 ISO BMFF 容器，客户端若把一张 HEIC
    # 报成 video/*，按视频抽帧会静默拿到一块 512x512 瓦片当整帧（ffmpeg 不拼 HEIC 的 tile
    # grid），全链路零报错地在错素材上跑。这里按 brand 把它掰回图片路径——不报错是有意的：
    # Agent 通路上任何「报错让重试」都会被 skill 的纠错表放大成新的误用。
    if is_video and is_still_image_container(raws[0][:16]):
        logger.info("event=pet_observe_still_image_declared_as_video 已按图片处理")
        is_video = False
        # 体积闸要跟着改判走：上面按视频档放过了 100MB，掰成图片后必须用图片档 15MB 重卡一次，
        # 否则「谎报成 video」就成了绕过图片体积闸的口子（解码后的像素闸能兜住内存，但请求体
        # 本身的上限该由这里守）。
        if len(raws[0]) > _OBSERVE_IMAGE_MAX_BYTES:
            raise HTTPException(status_code=400, detail="图片过大（单张上限 15 MB）")
    use_grounding = (
        settings.features.pet_head_grounding if grounding is None else grounding
    )
    # body_grounding（D7）仅影响回退路径（框不到猫狗时裁本体作参考图），取 feature 开关。
    try:
        result = await observe_pet(
            raws,
            is_video=is_video,
            grounding=use_grounding,
            body_grounding=settings.features.pet_body_grounding,
        )
    except MediaDecodeError as e:  # 字节一张都解不出（截断/损坏/非图片）→ 400，与「无动物」区分
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OmniDescribeError as e:  # 模型没给可解析 JSON → 502 + 可读原因（前端直接展示）
        raise HTTPException(status_code=502, detail=str(e)) from e
    return NormalResponse(code=0, message="OK", data=result)


@router.get("/pets", summary="List Pets", response_model=NormalResponse)
async def list_pets(current_user: str = Depends(verify_token)):
    pets = get_pet_library().list()
    return NormalResponse(
        code=0, message="OK", data={"pets": [p.model_dump() for p in pets]}
    )


@router.post("/pets", summary="Create Pet", response_model=NormalResponse)
async def create_pet(body: PetCreate, current_user: str = Depends(verify_token)):
    _require_pet_enabled()
    try:
        pet = get_pet_library().create(name=body.name, species=body.species)
    except PetNameConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _resync_home_profile("建档", pet.id)
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
    """改名 / 改物种。**不受总开关门控**：属存量管理，关掉 pet_recognition 后仍允许整理已录数据。"""
    _require_pet_id(pet_id)
    try:
        pet = get_pet_library().update(pet_id, name=body.name, species=body.species)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Pet '{pet_id}' not found") from e
    except PetNameConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _resync_home_profile("改名/改物种", pet_id)  # 改名后 md 里的 subject_name 才会纠偏
    return NormalResponse(code=0, message="Pet updated", data=pet.model_dump())


@router.delete("/pets/{pet_id}", summary="Delete Pet", response_model=NormalResponse)
async def delete_pet(pet_id: str, current_user: str = Depends(verify_token)):
    """删除宠物并联动清理家庭档案绑定。**不受总开关门控**：关掉 pet_recognition 后正是要能清存量。"""
    _require_pet_id(pet_id)
    removed = get_pet_library().delete(pet_id)
    # 联动清理家庭档案中绑定该宠物的条目；用 managed service（含 person_service），
    # 以便其内部 re-render 仍正确保留人类成员名册。
    # 包 try（同 person/router.py 的删除端点）：本体已 rmtree、不可回滚，此处再抛只会让客户端
    # 收到 500 以为整删失败，而重试是 no-op、绑定条目再也清不掉。
    cleanup: dict = {}
    try:
        cleanup = get_manager().home_profile_service.remove_subject(pet_id)
    except Exception:  # noqa: BLE001
        logger.warning("宠物删除后清理家庭档案失败: pet_id=%s", pet_id, exc_info=True)
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
    return FileResponse(str(path), media_type=_avatar.media_type(ext))


@router.post(
    "/pets/{pet_id}/avatar", summary="Upload Pet Avatar", response_model=NormalResponse
)
async def upload_pet_avatar(
    pet_id: str,
    image: UploadFile = File(
        ..., description="头像图片（常见格式均可，含 iPhone 的 HEIC）"
    ),
    current_user: str = Depends(verify_token),
):
    # 闸门顺序与 person 端点对齐：功能门 → 存在性(先于 read) → 体积前置闸 → 读 → 读后兜底
    # → 归一化（验真 + 取真实 ext；非直落格式解码后重编无损 webp，维持 盘上后缀/Content-Type/
    # 真实字节 一致）。体积闸卡的是**上传**字节；无损重编后可能大于该闸，这是有意的——闸的
    # 用途是拦超大请求体，不是约束盘上物件。
    _require_pet_id(pet_id)
    _require_pet_enabled()
    lib = get_pet_library()
    if lib.get(pet_id) is None:  # 先查存在，别为不存在的 id 读满内存
        raise HTTPException(status_code=404, detail=f"Pet '{pet_id}' not found")
    if image.size is not None and image.size > _avatar.AVATAR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="图片过大（上限 5 MB）")
    data = await image.read()
    if len(data) > _avatar.AVATAR_MAX_BYTES:  # size 缺失时兜底
        raise HTTPException(status_code=400, detail="图片过大（上限 5 MB）")
    # 走 to_thread：解码 + 重编是纯 CPU 活（HEIC 经 libheif、无损 WebP 编码），同进程还并行着
    # 直播转码 / 感知推理，占着事件循环会把它们一起饿死（本仓对这类活儿的一致口径：person 侧 7 处解码同样走 to_thread）。
    normalized = await asyncio.to_thread(
        _avatar.normalize_for_storage, data, prefer="webp"
    )
    if normalized is None:
        raise HTTPException(
            status_code=400, detail="无法打开这张图片（文件可能损坏，或不是图片）"
        )
    data, ext = normalized
    try:
        pet = lib.set_avatar(pet_id, data=data, ext=ext)
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
    crops: list[UploadFile] = File(
        ..., description="参考 crop（常见格式均可；非 jpg/png/webp 会转 JPEG 落盘）"
    ),
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
    _require_pet_enabled()
    if mode not in ("replace", "append"):
        raise HTTPException(status_code=400, detail="mode 只能是 replace 或 append")
    if not crops:
        raise HTTPException(status_code=400, detail="no reference crops")
    # 张数 / 体积在读盘前卡（含 append）——存储层反正只留 top-3，一次传 >3 或超大无语义，
    # 避免把将被 400 的请求整批 materialize 进内存（同 avatar / observe 闸门顺序）。
    if len(crops) > _MAX_REF_CROPS:
        raise HTTPException(status_code=400, detail="最多 3 张参考 crop")
    if any(c.size is not None and c.size > _avatar.AVATAR_MAX_BYTES for c in crops):
        raise HTTPException(status_code=400, detail="参考图过大（单张上限 5 MB）")
    lib = get_pet_library()
    if lib.get(pet_id) is None:  # 存在性前置闸（同 avatar）：别为不存在的 id 读满内存
        raise HTTPException(status_code=404, detail=f"Pet '{pet_id}' not found")
    data: list[bytes] = []
    for c in crops:
        raw = await c.read()
        if len(raw) > _avatar.AVATAR_MAX_BYTES:  # size 缺失时兜底
            raise HTTPException(status_code=400, detail="参考图过大（单张上限 5 MB）")
        if not raw:
            raise HTTPException(status_code=400, detail="empty reference crop")
        # 归一化兼验真（同 avatar 口径）：不验图的话任意字节都能存成 ref_crop_*.jpg 并**计入
        # 张数**，识别侧再静默跳过 → 界面显示「3 张参考图」而实际注入 0 张。
        # prefer="jpg"：参考图的唯一消费者是 omni，pet_refs 拼图时恒重编 JPEG q85，存 webp
        # 只是多一道转换；且 ref_crop_N.jpg 的硬编码后缀牵动 glob / 下标解析，不宜与内容脱钩。
        # 走 to_thread：解码 + 重编是纯 CPU 活（HEIC 经 libheif、再重编 JPEG），同进程还并行着
        # 直播转码 / 感知推理，占着事件循环会把它们一起饿死（本仓一致口径：person 侧 7 处解码同样走 to_thread）。
        normalized = await asyncio.to_thread(
            _avatar.normalize_for_storage, raw, prefer="jpg"
        )
        if normalized is None:
            raise HTTPException(
                status_code=400, detail="无法打开这张参考图（文件可能损坏，或不是图片）"
            )
        data.append(normalized[0])
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
    # 入口按魔数放过 jpg/png/webp（同 avatar 口径），故出口也按真实字节出 Content-Type——
    # 否则把「盘上后缀 / Content-Type / 真实字节」的不一致从入口挪到出口。
    p = paths[idx]
    try:
        with p.open("rb") as f:  # 只读前 16 字节嗅魔数，别把整张图（最大 5MB）读进内存
            ext = _avatar.sniff_image_ext(f.read(16)) or "jpg"
    except OSError:
        ext = "jpg"
    return FileResponse(str(p), media_type=_avatar.media_type(ext))
