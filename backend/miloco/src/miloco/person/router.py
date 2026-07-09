# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Person controller — family member CRUD + Tier A 样本登记 + 习惯（v1.2 新增）。"""

import asyncio
import logging
import re
import threading

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from miloco.config import get_settings, register_reset_hook
from miloco.database.person_repo import UNSET
from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.perception.engine.identity.config_loader import resolve_library_root
from miloco.perception.engine.identity.library import IdentityLibrary, _list_crop_files
from miloco.person.schema import PersonCreate, PersonUpdate, _normalize_optional_str
from miloco.schema.common_schema import NormalResponse
from miloco.utils.paths import miloco_home

# 严格 UUID4 白名单：拒绝路径分隔符、`..` 等可构造路径穿越的字符
_PERSON_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/identity", tags=["Identity"])

manager = get_manager()


@router.get("/persons", summary="List Persons", response_model=NormalResponse)
async def list_persons(current_user: str = Depends(verify_token)):
    """列家庭成员。除 DB 字段外,合并 identity_lib 下样本计数(tier_a / tier_c),
    供 web 端"已登记 / 待补样本"分桶展示。identity_lib 读不到时计数留 0,
    不阻塞主列表(注册前 person 目录尚未创建)。"""
    logger.info("List persons - user: %s", current_user)
    persons = manager.person_service.list_persons()
    # PersonRef 索引化:避免 N×M scan
    try:
        refs = {r.person_id: r for r in _get_identity_library().list_persons()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_persons: 读 identity_lib 失败,sample 计数归零: %s", exc)
        refs = {}
    data = []
    for p in persons:
        d = p.model_dump()
        r = refs.get(p.id)
        d["num_tier_a_body"] = r.num_tier_a_body if r else 0
        d["num_tier_c"] = r.num_tier_c if r else 0
        d["has_tier_a"] = bool(r and r.has_tier_a)
        data.append(d)
    return NormalResponse(
        code=0,
        message=f"Retrieved {len(persons)} persons",
        data=data,
    )


@router.post("/persons", summary="Create Person", response_model=NormalResponse)
async def create_person(body: PersonCreate, current_user: str = Depends(verify_token)):
    logger.info("Create person - user: %s, name: %s", current_user, body.name)
    person_id = manager.person_service.create_person(body.name, body.role)
    # 新增成员后级联刷新家庭档案 md（与 update_person 同款），否则 profile.md 的家庭成员段不含新成员
    try:
        manager.home_profile_service.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("级联刷新家庭档案失败(create) person_id=%s: %s", person_id, e)
    return NormalResponse(
        code=0, message="Person created", data={"person_id": person_id}
    )


@router.put(
    "/persons/{person_id}", summary="Update Person", response_model=NormalResponse
)
async def update_person(
    person_id: str, body: PersonUpdate, current_user: str = Depends(verify_token)
):
    logger.info("Update person - user: %s, id: %s", current_user, person_id)
    # role 三态:本次 PATCH 未带 role → 不改(UNSET);带了(空串已归一成 None) → 写,None 即清空
    # 家庭角色。靠 model_fields_set 区分"未传"与"显式传空",否则可空字段无法经 update 清空。
    role_provided = "role" in body.model_fields_set
    role_arg = body.role if role_provided else UNSET
    manager.person_service.update_person(person_id, body.name, role_arg)
    # 同步文件层 meta.json——**仅对"已有样本目录"的 person 同步**。无样本 person(创建后、
    # 录样本前)的 name/role 由 SQL 持有,enroll 时 add_tier_a_samples_batch 落 meta、或重启
    # sync_person_meta_from_sql 兜底;不给它凭空建目录,否则 list_persons 多出 (pid,False,0,0)
    # 扰动 IdentityEngine snapshot、触发全量 _promote_all_to_pending(见 engine.py 故意不监听
    # name/role 的设计注释)。best-effort:写失败仅 warn,不回滚已提交 SQL;role 提供 None 即清空 meta。
    try:
        lib = _get_identity_library()
        if lib.has_person_dir(person_id):
            # name/role 一次合并写(omit 的字段不动)——省两次 set_* 对同一 meta.json 的 read+write。
            meta_fields: dict = {}
            if body.name is not None:
                meta_fields["name"] = body.name
            if role_provided:
                meta_fields["role"] = body.role  # None 即清空 role
            if meta_fields:
                lib.set_meta(person_id, **meta_fields)
    except Exception as e:  # noqa: BLE001
        logger.warning("同步 identity_lib meta 失败 person_id=%s: %s", person_id, e)
    # 级联刷新家庭档案：person 改名/改 role 不走 home_profile 写入，需显式触发一次
    # commit 重渲染 md；已绑定 subject_id 的条目 subject_name 在 commit 内按成员当前 name 自动纠偏。
    try:
        manager.home_profile_service.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "级联刷新家庭档案失败 person_id=%s: %s", person_id, e
        )
    return NormalResponse(code=0, message="Person updated", data=None)


@router.delete(
    "/persons/{person_id}", summary="Delete Person", response_model=NormalResponse
)
async def delete_person(person_id: str, current_user: str = Depends(verify_token)):
    logger.info("Delete person - user: %s, id: %s", current_user, person_id)
    # defense-in-depth：在路径穿越敏感操作（shutil.rmtree）前先做 UUID4 白名单校验，
    # 与 register_sample 保持一致；person_service.delete_person 会先查 DB，理论上
    # 已能拦截非法 ID，但同样的检查在入口加一层更稳。
    if not _PERSON_ID_RE.match(person_id):
        raise HTTPException(status_code=400, detail="Invalid person_id format")
    manager.person_service.delete_person(person_id)
    # 级联删 identity_lib 文件——否则 list_persons / gallery 仍含此人，
    # omni 会继续把画面中的人识别为已删除成员
    try:
        _get_identity_library().delete_person(person_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("级联删除 identity_lib 失败 person_id=%s: %s", person_id, e)
    # 级联清家庭档案：移除该成员绑定的候选+正式条目并重渲染 md，
    # 否则条目会回落陈旧 subject_name、漂移到家庭档案面板而非消失。
    try:
        manager.home_profile_service.remove_subject(person_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("级联清家庭档案失败 person_id=%s: %s", person_id, e)
    return NormalResponse(code=0, message="Person deleted", data=None)


def _get_identity_library() -> IdentityLibrary:
    """构造与 IdentityEngine 同源的 IdentityLibrary 实例。

    ``resolve_library_root`` 是 library_root 的 single source of truth：会加载
    ``default_config.yaml`` + 合并 ``settings.yaml::perception.engine.identity_engine``
    override + 锚定 workspace_dir，与 ``build_identity_engine`` 走完全相同的解析。
    IdentityLibrary 是无状态的文件系统封装，多次实例化无副作用；两端实例分别 new，
    但路径必然一致。
    """
    return IdentityLibrary(resolve_library_root())


def _get_or_create_person_by_name(name: str, role: str | None) -> str:
    """注册流程按 name 解析身份(供 _resolve_member 调用):**name 已存在则复用,不抛 Conflict**。

    `register from-cluster` / `register/commit` 走"无现成 member_id 但给了 name"路径时,
    语义应该是"找这个名字的人,把样本追加进去"——不是"必须新建,撞名报错"。

    实现:扫 list_persons 找 name 完全匹配 → 拿现有 person_id;没匹配走 create_person。
    """
    existing = next(
        (p for p in manager.person_service.list_persons() if p.name == name),
        None,
    )
    if existing is not None:
        logger.info("register: 追加样本到已有 person name=%s id=%s", name, existing.id)
        # 复用已有 person 时,带进来的新 role 也要落 SQL——否则只写文件层 meta,重启
        # sync_person_meta_from_sql 会拿 SQL 旧值(多半 None)反向覆盖,本次 role 静默丢失。
        if role is not None and role != existing.role:
            manager.person_service.update_person(existing.id, None, role)
        return existing.id
    return manager.person_service.create_person(name, role)


def _resolve_member(member_id: str | None, name: str | None, role: str | None) -> str | None:
    """注册 commit 的统一身份解析:按 member_id 绑定既有成员(带与 SQL 不同的 name/role 也补写),
    或按 name 新建 / 复用(name 重复=追加样本)。

    SQL 是 name/role 单一事实源;commit 只写文件层 meta 的话,重启 sync_person_meta_from_sql
    会拿 SQL 旧值反向覆盖、本次改动静默丢失。故按 id 绑定既有成员时,带了与 SQL 不同的
    name/role 也补写 SQL(name 与 role 同属这类漂移)。
    """
    if member_id is not None:
        # 绑定既有成员时若带了与"第三人"撞名的 name,update_person 的唯一性校验会抛
        # ConflictException→409(追加样本流程里少见,但语义正确:不能两个同名),属有意为之。
        if name is not None or role is not None:
            existing = manager.person_service.get_person(member_id)
            if existing is not None:
                new_name = name if (name and name != existing.name) else None
                new_role = role if (role is not None and role != existing.role) else UNSET
                if new_name is not None or new_role is not UNSET:
                    manager.person_service.update_person(member_id, new_name, new_role)
        return member_id
    if name:
        return _get_or_create_person_by_name(name, role)
    return None


@router.post(
    "/persons/{person_id}/samples",
    summary="Register Identity Sample (Tier A)",
    response_model=NormalResponse,
)
async def register_sample(
    person_id: str,
    body_image: UploadFile = File(..., description="人体 crop（jpg/png）"),
    face_image: UploadFile | None = File(None, description="人脸 crop（可选，face_recog 备料）"),
    source: str = Form("user_upload"),
    current_user: str = Depends(verify_token),
):
    """Tier A 样本登记入口。

    - 接 multipart：``body_image``（必填）+ ``face_image``（可选）
    - 调 ``IdentityLibrary.add_tier_a_sample`` 写到 ``data/identity_lib/persons/<id>/tier_a/``
    - face_image 当前不进入识别管线，但作为登记数据落盘——为后续接入 face_recog 备料
    """
    logger.info(
        "Register sample - user: %s, person_id: %s, body=%s face=%s",
        current_user, person_id, body_image.filename, face_image.filename if face_image else None,
    )

    # person_id 校验：格式白名单 + 必须已在 DB 中注册
    # 防止 ../ 等路径穿越构造，同时拒绝对未注册 ID 写文件
    if not _PERSON_ID_RE.match(person_id):
        raise HTTPException(status_code=400, detail="Invalid person_id format")
    if not manager.person_service.exists(person_id):
        raise HTTPException(status_code=404, detail=f"Person '{person_id}' not found")

    body_arr = await _decode_image_upload(body_image)
    if body_arr is None:
        raise HTTPException(status_code=400, detail="body_image 解码失败")

    face_arr = None
    if face_image is not None:
        face_arr = await _decode_image_upload(face_image)
        if face_arr is None:
            raise HTTPException(status_code=400, detail="face_image 解码失败")

    library = _get_identity_library()

    # 抽 body 的 ReID emb 给 tier_a body_NNN 落同名 .npy: 跟 add_tier_a_samples_batch
    # 兜底路径同源。无活动 tracker 时 get_reid_extractor 返 None, 库就跳过 .npy
    # (行为退回旧版, 不报错)。抽取失败也吞掉, 不阻塞登记主流程。
    reid_extractor = manager.perception_service.get_reid_extractor()
    reid_emb = None
    if reid_extractor is not None and body_arr is not None:
        try:
            reid_emb = reid_extractor.extract_feature(body_arr)
        except Exception:  # noqa: BLE001
            logger.warning("ReID emb 抽取失败 person_id=%s (登记继续)", person_id, exc_info=True)

    ok = library.add_tier_a_sample(
        person_id=person_id,
        body_crop=body_arr,
        face_crop=face_arr,
        source=source,
        reid_embedding=reid_emb,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Tier A 容量已满或写入失败")

    return NormalResponse(
        code=0,
        message="Tier A 样本登记成功",
        data={"person_id": person_id, "has_face": face_arr is not None},
    )


async def _decode_image_upload(upload: UploadFile) -> "np.ndarray | None":
    """读 UploadFile 并 cv2.imdecode；失败返回 None。"""
    raw = await upload.read()
    if not raw:
        return None
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        return None
    return img


# =============================================================================
# Web 注册:extract (multipart) + samples/batch — 统一在主 backend 提供,前端 EnrollFlow
# 直连,不依赖独立进程
# =============================================================================


# 单 frame 内最多取 N 个候选(防一张人多合影炸图)。改动影响注册抽帧候选数,评估后再调。
_EXTRACT_MAX_PER_FRAME = 2
# 视频均匀采样上限:超出后 select_topk 已能从这么多里挑出差异化样本,
# 多采没有边际收益且明显延长接口延时(每帧一次 ONNX detect)。
_EXTRACT_VIDEO_MAX_FRAMES = 12


class SampleBatchItem(BaseModel):
    type: str  # "body" | "face"
    image_b64: str


class SampleBatchPayload(BaseModel):
    items: list[SampleBatchItem]


def _decode_b64_image(b64: str) -> "np.ndarray | None":
    import base64
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        return None
    return img


def _encode_jpeg_b64(img: "np.ndarray", quality: int = 85) -> str:
    import base64
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode() if ok else ""


@router.post(
    "/persons/{person_id}/samples/batch",
    summary="批量登记 Tier A 样本(body/face 混合 list)",
    response_model=NormalResponse,
)
async def register_sample_batch(
    person_id: str,
    body: SampleBatchPayload,
    current_user: str = Depends(verify_token),
):
    """配套 /extract 用:前端把用户勾选的 body / face crops(base64 jpeg)一次提交。

    body / face 各自写到 tier_a/body_{idx}.png、tier_a/face_{idx}.png,
    与单图入口 ``/samples`` 共享 ``add_tier_a_sample`` 容量限制(tier_a_max // 2)。
    部分写入失败不阻断剩余(容量满 / 解码失败的 item 计入 failed,继续下一个)。
    """
    logger.info(
        "Sample batch - user: %s, person_id: %s, n=%d",
        current_user, person_id, len(body.items),
    )
    if not _PERSON_ID_RE.match(person_id):
        raise HTTPException(status_code=400, detail="Invalid person_id format")
    if not manager.person_service.exists(person_id):
        raise HTTPException(status_code=404, detail=f"Person '{person_id}' not found")
    if not body.items:
        raise HTTPException(status_code=400, detail="items 不能为空")

    # 取 person 的 name(真名) —— batch endpoint 不接 name 入参, 从 person 库查出来传进
    # library 写进 meta.json, 否则感知层 list_persons 读不到 name → omni prompt 渲染退化
    # 成 UUID 而非姓名。
    person = next((p for p in manager.person_service.list_persons() if p.id == person_id), None)
    name = person.name if person else None

    # ReID extractor 借 perception service 现场抽 emb, 给 body 路径落 .npy。
    # 无活动 tracker 时返 None, 抽取失败也吞掉, 不阻塞登记主流程。
    reid_extractor = manager.perception_service.get_reid_extractor()

    library = _get_identity_library()
    written_body = 0
    written_face = 0
    failed: list[dict] = []
    for i, item in enumerate(body.items):
        if item.type not in ("body", "face"):
            failed.append({"index": i, "reason": f"unknown type {item.type!r}"})
            continue
        img = _decode_b64_image(item.image_b64)
        if img is None:
            failed.append({"index": i, "reason": "image decode failed"})
            continue
        # body / face 走同一个 add_tier_a_sample API:只填对应那一格,另一个留 None
        # library 的容量管理对 body / face 是分开计数的,所以两类各自独立。
        if item.type == "body":
            reid_emb = None
            if reid_extractor is not None:
                try:
                    reid_emb = reid_extractor.extract_feature(img)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "ReID emb 抽取失败 person_id=%s index=%d (登记继续)",
                        person_id, i, exc_info=True,
                    )
            ok = library.add_tier_a_sample(
                person_id=person_id, body_crop=img, source="user_upload",
                name=name, reid_embedding=reid_emb,
            )
            if ok:
                written_body += 1
            else:
                failed.append({"index": i, "reason": "body 容量满或写入失败"})
        else:  # face
            # add_tier_a_sample 必须先有 body 才能写 face(API 设计):用 face 当 body
            # 兜底——face crop 本身也是 body 的局部,这里复用入口写 face_only。但是
            # 该路径会同时多写一张 body_*.png。不可接受。
            # 改走低层:直接 imwrite + sidecar(模拟 add_tier_a_sample 的 face 分支)。
            face_ok = library.add_face_only_sample(person_id, img, source="user_upload")
            if face_ok:
                written_face += 1
            else:
                failed.append({"index": i, "reason": "face 容量满或写入失败"})

    # 兜底写 meta.json name: body 路径 add_tier_a_sample(name=...) 已写过 meta, 只有 face-only
    # 批量(_write_face_only 不写 meta)才需补一次, 保证感知层 get_name 查得到姓名; 避免 body 冗余写。
    if name is not None and written_body == 0 and written_face > 0:
        library.set_meta(person_id, name=name)

    return NormalResponse(
        code=0,
        message=f"written body={written_body} face={written_face} failed={len(failed)}",
        data={
            "written_body": written_body,
            "written_face": written_face,
            "failed": failed,
        },
    )


def _topup_selected_to_target(
    scored: list, selected_ids: set[int], target: int,
) -> set[int]:
    """select_topk 因 pHash + ReID 双过严返回 < target 时,按 ``scored`` 的既有
    顺序补到 target。

    ``scored`` 的顺序由调用方决定,并非保证按 score 全局降序:单图路径确实按
    score 排好,但 extract_samples 视频路径是按帧时间序拼接(跨帧未做全局排序),
    所以补进来的是"时间线上较早的差异化候选"而非严格"次高分"——对视频沿时间
    均匀取样反而合理。用户预期"录了 N 秒该看到 target 张默认勾",算法选不齐时
    优先保证数量。返回新 set(不修改入参)。
    """
    out = set(selected_ids)
    if len(out) >= target:
        return out
    for c in scored:
        if len(out) >= target:
            break
        if id(c) not in out:
            out.add(id(c))
    return out


def _flatten_candidates_with_auto(
    scored: list, selected_ids: set[int],
) -> tuple[list[dict], list[int], list[int]]:
    """把 ScoredCandidate 列表平展成 ``{type, image_b64, ...}`` 扁平 list,同时
    算 ``auto_selected`` 的 body / face 索引(指 flat list 内的下标)。

    交错规则:每个 ScoredCandidate 先吐一行 body,再吐其配对的 face(如有且
    ``size > 0``)。``id(c) in selected_ids`` 决定 body 行是否入 auto_body,且
    其配对 face 同样跟随入 auto_face——face 不独立 select,跟 body 绑定。
    """
    candidates: list[dict] = []
    auto_body: list[int] = []
    auto_face: list[int] = []
    for c in scored:
        body_idx = len(candidates)
        candidates.append({
            "type": "body",
            "image_b64": _encode_jpeg_b64(c.body_crop, quality=85),
            "confidence": float(c.detector_conf),
            "frame_index": c.frame_index,
            "bbox": list(c.bbox_xyxy),
        })
        is_selected = id(c) in selected_ids
        if is_selected:
            auto_body.append(body_idx)
        if c.face_crop is not None and c.face_crop.size > 0:
            face_idx = len(candidates)
            candidates.append({
                "type": "face",
                "image_b64": _encode_jpeg_b64(c.face_crop, quality=85),
                "confidence": float(c.detector_conf),
                "frame_index": c.frame_index,
                "bbox": None,
            })
            if is_selected:
                auto_face.append(face_idx)
    return candidates, auto_body, auto_face


@router.post(
    "/persons/{person_id}/extract",
    summary="从图片 / 视频抽取 body+face 候选样本(含算法预选)",
    response_model=NormalResponse,
)
async def extract_samples(
    person_id: str,
    media: UploadFile = File(..., description="图片(jpg/png)或视频(mp4/webm)"),
    max_frames: int = Form(_EXTRACT_VIDEO_MAX_FRAMES),
    current_user: str = Depends(verify_token),
):
    """飞书附件注册路径在主 backend 的复现:支持 multipart 上传图片或视频,
    返回扁平 candidates list + ``auto_selected``(算法预选 indices)。

    前端策略:默认勾选 ``auto_selected.body`` / ``auto_selected.face`` 里的 indices,
    用户可手改;一键保存即调 ``/samples/batch``。

    算法:
    - 图片 / 视频每帧都跑 ``extract_from_image``,得到 body + 可选配对 face 的 candidate 列表
    - 跨帧汇总后跑 ``select_topk`` 算 body 端预选(pHash + 时间间隔 + ReID 备路径)
    - face 端跟随:其配对 body 入选则该 face 入选

    Returns:
        ``{ is_video, n_frames, candidates: [{type, image_b64, confidence, frame_index, bbox?}],
            auto_selected: { body: [int], face: [int] } }``
    """
    logger.info(
        "Extract samples - user: %s, person_id: %s, file=%s ct=%s",
        current_user, person_id, media.filename, media.content_type,
    )
    if not _PERSON_ID_RE.match(person_id):
        raise HTTPException(status_code=400, detail="Invalid person_id format")
    if not manager.person_service.exists(person_id):
        raise HTTPException(status_code=404, detail=f"Person '{person_id}' not found")

    raw = await media.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")

    # 判断 image / video:优先 content-type,缺失时按扩展名兜底
    ct = (media.content_type or "").lower()
    fname = (media.filename or "").lower()
    is_video = ct.startswith("video/") or fname.endswith((".mp4", ".webm", ".mov", ".avi", ".mkv"))

    detector = _load_detector()
    reid_extractor = manager.perception_service.get_reid_extractor()

    # ---- 1) 收集 frames(image: 单帧, video: 均匀采样) ----
    frames: list[tuple[int, "np.ndarray"]] = []
    if is_video:
        import tempfile
        suffix = ".mp4"
        if fname.endswith((".webm", ".mov", ".avi", ".mkv")):
            suffix = "." + fname.rsplit(".", 1)[-1]
        tf = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tf.write(raw)
        finally:
            tf.close()
        try:
            from miloco.perception.engine.identity.extractor import _sample_video_frames
            # _sample_video_frames 返回 (frames, fps) 二元 tuple, 这里只用 frames;
            # fps 由下游 extract_from_image 走 captured_at=frame_index 占位计算。
            frames, _video_fps = _sample_video_frames(tf.name, max_frames=max_frames)
        finally:
            import os as _os
            try:
                _os.unlink(tf.name)
            except OSError:
                pass
    else:
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            raise HTTPException(status_code=400, detail="image decode failed")
        frames = [(0, img)]

    if not frames:
        return NormalResponse(
            code=0, message="no decodable frames",
            data={"is_video": is_video, "n_frames": 0, "candidates": [],
                  "auto_selected": {"body": [], "face": []}},
        )

    # ---- 2) 每帧 extract_from_image → 收 ScoredCandidate(自带 body+face 配对) ----
    from miloco.perception.engine.identity.extractor import extract_from_image
    scored_all = []
    for fi, frame in frames:
        # 同步 ONNX 推理丢线程池:主 backend 同进程还跑着 live transcode /
        # record_clip / MQTT 感知,串行 ≤max_frames 帧若占着事件循环会卡住它们。
        per = await asyncio.to_thread(
            extract_from_image,
            frame, detector=detector, reid_extractor=reid_extractor,
            # captured_at 用 frame_index 作占位(秒);确保 select_topk 时间间隔判断
            # 能 fire(单图路径仅 1 帧时只有 1 个候选不受影响)。
            captured_at=float(fi),
        )
        # 单帧内只取前 N 个候选(防合影炸图)
        per = per[:_EXTRACT_MAX_PER_FRAME]
        for c in per:
            # frame_index 写回(extract_from_image 默认填 0,这里覆盖以便前端追溯)
            c.frame_index = fi
            scored_all.append(c)

    if not scored_all:
        return NormalResponse(
            code=0, message="no candidates detected",
            data={"is_video": is_video, "n_frames": len(frames), "candidates": [],
                  "auto_selected": {"body": [], "face": []}},
        )

    # ---- 3) 跑 select_topk 算 body 端预选;face 跟随其配对 body ----
    from miloco.perception.engine.identity.registration_filter import select_topk
    # 视频路径默认 topk=5(够前端默认勾选展示);图片单帧时也跑一次(等价"挑全部")。
    sr = select_topk(scored_all, topk=5, min_k=1)
    selected_ids = _topup_selected_to_target(scored_all, {id(s) for s in sr.samples}, 5)

    # ---- 4) 把 ScoredCandidate 平展成 {type, image_b64, ...} list,并算 auto_selected ----
    candidates, auto_body_indices, auto_face_indices = _flatten_candidates_with_auto(
        scored_all, selected_ids,
    )

    return NormalResponse(
        code=0,
        message=f"extracted {len(candidates)} candidates from {len(frames)} frames",
        data={
            "is_video": is_video,
            "n_frames": len(frames),
            "candidates": candidates,
            "auto_selected": {
                "body": auto_body_indices,
                "face": auto_face_indices,
            },
        },
    )


# 文件名白名单:防止 ../ 路径穿越 + 限制为 body_* / face_* 的图像文件(.jpg/.jpeg/.png)
_FILENAME_SAFE = re.compile(r"^(body|face)_[0-9a-zA-Z_\-]{1,64}\.(jpg|jpeg|png)$")


def _list_tier_a_files_dict(person_id: str) -> tuple[list[dict], list[dict]]:
    """枚举 identity_lib/persons/<id>/tier_a/ 下 body_* / face_* 图像(.jpg/.jpeg/.png)。返回
    ``(body[], face[])``,每项 ``{filename, size}``。person_id 已 _PERSON_ID_RE 校过。"""
    library = _get_identity_library()
    person_dir = library.persons_dir / person_id / "tier_a"
    body: list[dict] = []
    face: list[dict] = []
    if person_dir.is_dir():
        for p in sorted(_list_crop_files(person_dir, "body")):
            body.append({"filename": p.name, "size": p.stat().st_size})
        for p in sorted(_list_crop_files(person_dir, "face")):
            face.append({"filename": p.name, "size": p.stat().st_size})
    return body, face


@router.get(
    "/persons/{person_id}/samples",
    summary="列出 Tier A 样本文件名(给 PersonAvatar 拉首张 face)",
    response_model=NormalResponse,
)
async def list_tier_a_samples(
    person_id: str, current_user: str = Depends(verify_token),
):
    """返回 ``{body:[{filename,size}], face:[{filename,size}]}``。person 不存在则
    返回空数组(不抛 404,避免前端 Avatar 拉到一半报错——空就显示色块占位)。"""
    if not _PERSON_ID_RE.match(person_id):
        raise HTTPException(status_code=400, detail="Invalid person_id format")
    body, face = _list_tier_a_files_dict(person_id)
    return NormalResponse(code=0, message="OK", data={"body": body, "face": face})


@router.get(
    "/persons/{person_id}/sample/{tier}/{filename}",
    summary="读取单张 Tier A/C 样本图(给 PersonAvatar <img src> 用)",
)
async def read_tier_sample(
    person_id: str, tier: str, filename: str,
    current_user: str = Depends(verify_token),
):
    """按文件原格式(jpg/jpeg/png)返回文件流。tier 限 'a'/'c',filename 走
    ``_FILENAME_SAFE`` 白名单防穿越。
    """
    if tier not in ("a", "c"):
        raise HTTPException(status_code=400, detail="tier must be 'a' or 'c'")
    if not _PERSON_ID_RE.match(person_id):
        raise HTTPException(status_code=400, detail="Invalid person_id format")
    if not _FILENAME_SAFE.match(filename):
        raise HTTPException(status_code=400, detail=f"非法文件名:{filename}")
    library = _get_identity_library()
    path = library.persons_dir / person_id / f"tier_{tier}" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="sample 不存在")
    # 落盘格式 jpg→png 迁移后, Content-Type 随实际后缀走, 否则浏览器按 jpeg 解析 png 字节会坏图
    media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(str(path), media_type=media_type)


@router.get(
    "/persons/{person_id}/samples/montage",
    summary="拼接该 person 的样本图(body 横排 + 可选 face 横排在下方)",
    response_model=NormalResponse,
)
async def get_sample_montage(
    person_id: str,
    with_face: bool = False,
    tier: str = "a",
    current_user: str = Depends(verify_token),
):
    """一次性返回该 person 所有样本的合并图。

    Agent 给用户展示某人样本时,**直接调本端点拿一张合并图**,不要反复读单图 +
    多次发图。

    布局:
    - body 横排,等比 resize 到高度 256 后 hconcat
    - 如果 `with_face=true`,face 横排,等比 resize 到高度 128 后 hconcat,纵向贴 body 下方
    - body / face 行宽不一致时,**短的一行白边居中 pad** 对齐宽度

    Args:
        with_face: 是否在 body 下方追加 face 行(默认 false)
        tier: 'a'(用户登记,永久,默认)或 'c'(omni 累积,FIFO)

    Returns:
        ``{image_jpeg_b64, body_count, face_count, width, height}``。无样本时
        ``image_jpeg_b64=""``、counts=0,不抛错。
    """
    import base64
    if not _PERSON_ID_RE.match(person_id):
        raise HTTPException(status_code=400, detail="Invalid person_id format")
    if tier not in ("a", "c"):
        raise HTTPException(status_code=400, detail="tier must be 'a' or 'c'")
    library = _get_identity_library()
    tier_dir = library.persons_dir / person_id / f"tier_{tier}"
    if not tier_dir.is_dir():
        return NormalResponse(
            code=0, message=f"person has no tier_{tier} samples",
            data={"image_jpeg_b64": "", "body_count": 0, "face_count": 0,
                  "width": 0, "height": 0},
        )

    def _resize_to_height(img, target_h: int):
        h, w = img.shape[:2]
        if h <= 0 or w <= 0:
            return None
        new_w = max(1, int(round(w * target_h / h)))
        return cv2.resize(img, (new_w, target_h), interpolation=cv2.INTER_AREA)

    def _row_concat(files, target_h: int):
        imgs = []
        for f in files:
            img = cv2.imread(str(f), cv2.IMREAD_COLOR)
            if img is None:
                continue
            r = _resize_to_height(img, target_h)
            if r is not None:
                imgs.append(r)
        return (cv2.hconcat(imgs) if imgs else None), len(imgs)

    body_files = sorted(_list_crop_files(tier_dir, "body"))
    # face_count 始终反映磁盘上 face 样本数(信息字段,跟 with_face 是否绘制脱钩)。
    # 否则 agent 在默认调用(不带 --with-face)拿到 face_count=0 会误判"该 person 无
    # 人脸样本",其实只是没把 face 行拼进图。
    face_files = sorted(_list_crop_files(tier_dir, "face"))
    face_count = len(face_files)
    body_row, body_count = _row_concat(body_files, target_h=256)
    if body_row is None:
        return NormalResponse(
            code=0, message="no decodable body samples",
            data={"image_jpeg_b64": "", "body_count": 0, "face_count": face_count,
                  "width": 0, "height": 0},
        )

    output = body_row
    if with_face:
        face_row, _ = _row_concat(face_files, target_h=128)
        if face_row is not None:
            # 宽度对齐:窄的一行白边居中 pad
            bw, fw = body_row.shape[1], face_row.shape[1]
            if bw != fw:
                tgt = max(bw, fw)
                if bw < tgt:
                    pad = tgt - bw
                    body_row = cv2.copyMakeBorder(
                        body_row, 0, 0, pad // 2, pad - pad // 2,
                        cv2.BORDER_CONSTANT, value=(255, 255, 255),
                    )
                if fw < tgt:
                    pad = tgt - fw
                    face_row = cv2.copyMakeBorder(
                        face_row, 0, 0, pad // 2, pad - pad // 2,
                        cv2.BORDER_CONSTANT, value=(255, 255, 255),
                    )
            output = cv2.vconcat([body_row, face_row])

    ok, buf = cv2.imencode(".jpg", output, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise HTTPException(status_code=500, detail="jpg encode failed")
    return NormalResponse(
        code=0, message="OK",
        data={
            "image_jpeg_b64": base64.b64encode(buf.tobytes()).decode(),
            "body_count": body_count,
            "face_count": face_count,
            "width": int(output.shape[1]),
            "height": int(output.shape[0]),
            "tier": tier,
        },
    )


# =============================================================================
# rollback —— 按 register_session_id 删该批次写入的 tier_a 样本（v1.2 新增）
# =============================================================================


class RollbackPayload(BaseModel):
    person_id: str
    register_session_id: str


@router.post(
    "/register/rollback",
    summary="Rollback a Register Session",
    response_model=NormalResponse,
)
async def rollback_register_session(
    body: RollbackPayload,
    current_user: str = Depends(verify_token),
):
    """删除指定 register_session_id 写入该 person 的所有 tier_a 样本（含 sidecar）。"""
    if not _PERSON_ID_RE.match(body.person_id):
        raise HTTPException(status_code=400, detail="Invalid person_id format")
    n = _get_identity_library().delete_by_register_session(
        body.person_id, body.register_session_id,
    )
    return NormalResponse(
        code=0,
        message=f"deleted {n} samples",
        data={"deleted": n, "register_session_id": body.register_session_id},
    )


# =============================================================================
# 身份库管理 M10:合并 / 拆分(v1.2 新增)
# =============================================================================


class MergePayload(BaseModel):
    target_id: str
    source_ids: list[str]


class SplitPayload(BaseModel):
    # 拆分必须给新人物一个真名(必填唯一)，role(家庭角色)可选；
    # 不再像旧版那样只给一个显示名、又把它灌进真名槽。
    new_name: str = Field(min_length=1)
    new_role: str | None = None
    selector_filenames: list[str] | None = None
    selector_cluster_ids: list[str] | None = None
    selector_cam_ids: list[str] | None = None
    selector_session_ids: list[str] | None = None

    # new_role 同 register 入口: 空串/纯空白 → None, 与 CRUD role 口径一致(调用处原 `or None`
    # 只挡空串、不挡纯空白)。
    _norm_role = field_validator("new_role")(_normalize_optional_str)


@router.post(
    "/persons/merge",
    summary="Merge Persons",
    response_model=NormalResponse,
)
async def merge_persons_endpoint(
    body: MergePayload,
    current_user: str = Depends(verify_token),
):
    """合并 source_ids 到 target_id:样本并入,删除 source 目录 + DB 行。"""
    if not _PERSON_ID_RE.match(body.target_id):
        raise HTTPException(status_code=400, detail="Invalid target_id format")
    for sid in body.source_ids:
        if not _PERSON_ID_RE.match(sid):
            raise HTTPException(status_code=400, detail=f"Invalid source_id: {sid}")
    if not manager.person_service.exists(body.target_id):
        raise HTTPException(status_code=404, detail="target person not found")

    result = _get_identity_library().merge_persons(body.target_id, body.source_ids)
    db_deleted: list[str] = []
    for sid in result.merged_sources:
        try:
            manager.person_service.delete_person(sid)
            db_deleted.append(sid)
        except Exception:  # noqa: BLE001
            logger.warning("merge: 删 DB person %s 失败", sid, exc_info=True)
    return NormalResponse(
        code=0,
        message=f"merged {len(result.merged_sources)} sources",
        data={
            "target_id": result.target_id,
            "merged_sources": result.merged_sources,
            "written_tier_a": result.written_tier_a,
            "written_tier_c": result.written_tier_c,
            "db_deleted": db_deleted,
        },
    )


@router.post(
    "/persons/{person_id}/split",
    summary="Split a Person",
    response_model=NormalResponse,
)
async def split_person_endpoint(
    person_id: str,
    body: SplitPayload,
    current_user: str = Depends(verify_token),
):
    """按 selector 把 person 的部分样本拆到新 person。新 person_id + DB 行同步创建。"""
    if not _PERSON_ID_RE.match(person_id):
        raise HTTPException(status_code=400, detail="Invalid person_id format")
    if not manager.person_service.exists(person_id):
        raise HTTPException(status_code=404, detail="person not found")

    # 新人物用真名建库(name 唯一)，role 可选；不再把同一个值灌进 name 和 role。
    new_role = body.new_role or None
    new_pid = manager.person_service.create_person(body.new_name, new_role)
    # 真名 + 角色一并写进新人物的文件层 meta.json，与 SQL 即时一致(不靠重启 backfill 兜底)。
    result = _get_identity_library().split_person(
        person_id, new_pid, body.new_name,
        new_role=new_role,
        selector_filenames=body.selector_filenames,
        selector_cluster_ids=body.selector_cluster_ids,
        selector_cam_ids=body.selector_cam_ids,
        selector_session_ids=body.selector_session_ids,
    )
    if not result.moved:
        try:
            manager.person_service.delete_person(new_pid)
        except Exception:  # noqa: BLE001
            pass
        return NormalResponse(
            code=0, message="selector matched zero samples; no-op",
            data={"new_person_id": None, "moved": []},
        )
    return NormalResponse(
        code=0,
        message=f"split {len(result.moved)} samples to new person",
        data={"new_person_id": new_pid, "moved": result.moved},
    )


# =============================================================================
# 注册流程 M6:preview / commit / sessions(v1.2 新增 web 两步走)
# =============================================================================


class RegisterPreviewPayload(BaseModel):
    media_b64: str | None = None
    media_kind: str | None = None     # "image" / "video"
    # 多图批量入口:用户一次发 N 张图,服务端循环 extract_from_image,把所有
    # candidates 平铺到一个 pending session;走 select_topk 跨图做 pHash + ReID
    # + 时间间隔 三维联合去重。
    # media_b64_list 非空时:走多图路径,media_b64 / media_kind 忽略。
    # 列表长度建议 ≤10(每张 base64 编码后大小 ~100KB-1MB)。
    media_b64_list: list[str] | None = None
    cluster_id: str | None = None     # 陌生人池路径
    member_id: str | None = None
    # 默认 5: 跟 CLI --topk 默认 + SKILL.md 注册示例对齐。注册主路径是飞书 agent
    # 直连本 endpoint, 不传 topk 时也拿 5 (之前默认 3, 只有 CLI 用户能拿到对齐值)。
    topk: int = 5


class RegisterCommitPayload(BaseModel):
    register_session_id_pending: str
    indices: list[int]
    member_name: str | None = None
    member_role: str | None = None

    # member_role 不经 PersonUpdate 校验直下 SQL, 这里挂同款归一化: 空串/纯空白 → None,
    # 避免写出 role="" 与 CRUD 路径的 NULL 不一致(隐性脏数据)。
    _norm_role = field_validator("member_role")(_normalize_optional_str)


_detector_lock = threading.Lock()
_detector_singleton = None


def _load_detector():
    # 走 settings.directories.models_dir($MILOCO_HOME/models/)而非 __file__ 相对路径:
    # uv tool install miloco 后 __file__ 落在 site-packages 内, 但 wheel 不打 onnx,
    # 真模型部署到 $MILOCO_HOME/models/。主流程 perception/client.py 走的也是这个口径。
    #
    # 显式双检锁做进程内单例:本函数被 7+ 处注册/分析路径调用,且都在
    # asyncio.to_thread 里跑(真实多线程并发)。用锁把构造串起来 —— 而非依赖
    # functools.lru_cache(其执行期间不持锁,冷启动并发 miss 会各建一个 Detector、
    # 只留一个),让「单例」在冷启动瞬间也严格成立、不多建 CoreML session。
    # detect() 只读 self、ORT session.run 线程安全,故复用同一实例并发安全;生产原地
    # 热替换模型需重启进程才生效。
    global _detector_singleton
    if _detector_singleton is not None:
        return _detector_singleton
    with _detector_lock:
        if _detector_singleton is None:
            from miloco.perception.engine.identity.tracker.detector import Detector
            det_path = get_settings().directories.models_dir / "det_4C.onnx"
            _detector_singleton = Detector(
                model_path=str(det_path), conf_threshold=0.4, use_gpu=False
            )
    return _detector_singleton


def _reset_detector_singleton():
    # settings reset(换 workspace / 测试)后让单例失效,避免返回指向旧 models_dir
    # 的残留 Detector。
    global _detector_singleton
    _detector_singleton = None


register_reset_hook("person_router_detector", _reset_detector_singleton)


def _should_use_frontal_seed(
    is_video: bool, video_per_track: "dict | None",
) -> bool:
    """create_pending 是否用 V_combined helper (select_topk_with_frontal_seed)。

    **仅单 track 视频用。多人视频 (≥2 track) 绝不能用** —— helper 在「全量混合
    候选」上跑 farthest-first 最大化 emb 多样性, body_picks / face_picks 必然横跨
    不同 track; stage 4 按列表位置 (而非身份) 配对, 就地把 ``body_picks[i].face_crop``
    改写成 ``face_picks[i]`` 的同帧脸 —— 二者常分属不同 track, 且被改写的正是
    create_pending 存的 candidates 里的共享对象。multi_track commit 按 track 全局
    index 取到的就是这批被污染对象 → A 的脸被注册进 B 的画像库 (静默跨身份污染)。
    多人路径改走 plain select_topk (号码图粒度是「挑人」不是「挑样本质量」)。

    非视频路径 (image / pool / batch) 一律 False。is_video=True ⟹ video_per_track
    已在 video 分支赋值 (可能空 dict), 故 None 兜底为 0 track。
    """
    if not is_video:
        return False
    n_tracks = len(video_per_track) if video_per_track else 0
    return n_tracks < 2


@router.post(
    "/register/preview",
    summary="Register Preview (step 1 of 2)",
    response_model=NormalResponse,
)
async def register_preview(
    body: RegisterPreviewPayload,
    current_user: str = Depends(verify_token),
):
    """两步走第 1 步:跑抽取 + 筛选,返回 pending_id + candidates,不写盘。

    支持四种输入(四选一,按下面顺序匹配):
    - ``cluster_id`` → 从陌生人池 cluster 抽 candidate(走 pool.fetch + extract_from_pool)
    - ``media_b64_list`` → 多图批量,循环 extract_from_image 平铺,select_topk 跨图去重
    - ``media_b64`` + ``media_kind='image'`` → 单图,extract_from_image
    - ``media_b64`` + ``media_kind='video'`` → 视频,extract_from_video (DeepSORT 多 track)

    candidates 里附带 ``image_jpeg_b64``(缩略图)让 Web v2 直接渲染,不用再调端点取图。
    """
    import base64

    from miloco.perception.engine.identity.extractor import (
        extract_from_image,
        extract_from_pool,
        extract_from_video,
    )

    candidates = []
    source = "from_media"
    # video_per_track 仅在 video 分支生成(extract_from_video 输出 ``{track_id: list}``);
    # 其他路径(image / cluster / images 批量)留 None。下面 multi_track 分支用 None 检查
    # 替代 ``"in dir()"`` 的局部变量内省 hack。
    video_per_track: dict[int, list] | None = None
    if body.cluster_id:
        # 从陌生人池 cluster 抽。基于已知 cluster_id 操作(不是探索 cluster 列表),
        # 不按时间窗过滤——跟 register_from_cluster 行为一致,避免 web 流程里
        # ``pool/fetch?window=N`` 看到的 cluster 在 preview 时因窗口不同消失。
        pool = manager.perception_service.tier_u_pool
        if pool is None:
            raise HTTPException(status_code=503, detail="陌生人池未启用")
        # reid_extractor 兜底:让"DeepSORT fast 模式跳 ReID 抽取 → entry 终身无 emb
        # → intra_cam_dedup_tick 过滤 → 同人多 track 不合"的边界 case 在 fetch
        # 时补 emb + 重跑 dedup,确保同人 cluster 都能合上(详见 tier_u.fetch docstring)。
        cluster_cands = pool.fetch(
            target_cluster_id=body.cluster_id,
            reid_extractor=manager.perception_service.get_reid_extractor(),
        )
        target = cluster_cands[0] if cluster_cands else None
        if target is None:
            raise HTTPException(status_code=404, detail=f"cluster {body.cluster_id} 不在池内")
        # 用 target.cluster_id 而非 body.cluster_id: fetch 期间 dedup tick 可能把
        # 用户选的 cluster 合并到另一个, target.cluster_id 是落地后的真实 id
        scored_by_cid = extract_from_pool([target])
        candidates = scored_by_cid.get(target.cluster_id, [])
        # cluster 存在但 L2 crop 全失败的 case:跟 register_from_cluster 同款 409,
        # 让前端拿到清晰错误信号("cluster 暂无可用样本"),而不是默默拿空 candidates。
        if not candidates:
            raise HTTPException(
                status_code=409,
                detail="cluster 无 L2 crop,等更多帧累积后再 preview",
            )
        source = "from_cluster"
    elif body.media_b64_list:
        # 多图批量:循环 extract_from_image,把每张图的 candidates 平铺合并,送 select_topk
        # 跨图做 pHash + ReID + 时间间隔 三维联合去重。这样用户一次发 N 张图,topk 自动从
        # 全集挑出最差异化的 K 张入库,而不是退化为"每张图选 1 张"。
        # 共享 detector / extractor 实例避免每张图重建 onnx session。
        det = _load_detector()
        ext = manager.perception_service.get_reid_extractor()
        merged: list = []
        for i, b64 in enumerate(body.media_b64_list):
            raw = base64.b64decode(b64)
            arr = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None or img.size == 0:
                # 单张解码失败不阻断整批——记 warning,跳过这张继续。
                logger.warning("register/preview: media_b64_list[%d] decode failed,跳过", i)
                continue
            per_image = await asyncio.to_thread(
                extract_from_image, img, detector=det, reid_extractor=ext,
            )
            # captured_at 用图序号 ×2.0 当 epoch 占位:
            # - 避免全部相同导致 select_topk 时间间隔判重(< 1s gap 直接拒)
            # - 用 ×2.0 (不是 ×1.0)是给"严格大于阈值"留余量,免得撞临界 1.0 == 1.0
            #   边界(浮点比较里 1.0 < 1.0 是 False 不拒,但任何上游微调阈值都可能翻车)。
            for c in per_image:
                if c.captured_at == 0.0:
                    c.captured_at = float(i) * 2.0
            merged.extend(per_image)
        if not merged:
            raise HTTPException(
                status_code=422,
                detail="media_b64_list 内全部图解码失败或未检测到目标",
            )
        candidates = merged
        source = "from_media_batch"
    elif body.media_b64 and body.media_kind == "image":
        raw = base64.b64decode(body.media_b64)
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            raise HTTPException(status_code=400, detail="image decode failed")
        # 图像路径没有 DeepSORT 关联预算好的 emb,这里现场抽:借 perception_service
        # 共享的 HumanReID 实例(随便挑一个活动 tracker 的);摄像头未启动时返 None,
        # ScoredCandidate.reid_embedding 留 None,下游 add_tier_a_samples_batch 还有
        # 一道 reid_extractor 兜底保底,但提前算更稳。
        candidates = await asyncio.to_thread(
            extract_from_image,
            img, detector=_load_detector(),
            reid_extractor=manager.perception_service.get_reid_extractor(),
        )
    elif body.media_b64 and body.media_kind == "video":
        # 视频路径:落临时文件 → DeepSORT 关联抽 per-track candidates → 平铺成 list
        # extract_from_video 已支持 bytes 入参,内部自己落 tempfile,我们直接传 bytes
        raw = base64.b64decode(body.media_b64)
        if not raw:
            raise HTTPException(status_code=400, detail="video bytes empty")
        # DeepSORT tracker factory 独立实例(不污染主流程 track_id 空间);要把
        # human_reid 的绝对路径传进去——跟 deep_sort PR 7 修法一致
        def _make_tracker():
            from miloco.perception.engine.identity.deep_sort import DeepSortTracker
            from miloco.perception.engine.identity.tracking_service import (
                RealTrackingService,
            )
            # 同 _load_detector: 走 settings.directories.models_dir 而非 __file__ 相对,
            # 因为 wheel 不打 onnx, __file__ 在 site-packages 下找不到。
            mdir = str(get_settings().directories.models_dir)
            # 用 yaml-resolved DeepSortConfigDC,跟主流程 tracking_service 共享同一份
            # 配置(尤其 max_age_sec)。硬编码 ``DeepSortConfigDC()`` 默认 max_age_sec=1.0
            # 会让视频注册路径只容忍 1 帧 miss,而主流程已被 yaml 调到 3.0;边界帧
            # confidence 抖动时 track 过早杀死、分裂为多 track,误触发号码图分支。
            return DeepSortTracker(
                detector=_load_detector(),
                config=manager.perception_service.deep_sort_config,
                fps=1,
                reid_model_path=RealTrackingService._resolve_model_path(mdir, "human_body_reid_v2.onnx"),
                use_gpu=False,
            )
        video_per_track = extract_from_video(
            raw,
            detector=_load_detector(),
            deep_sort_tracker_factory=_make_tracker,
            max_frames=60,
            min_track_hits=3,
        )
        # 平铺成 candidates list(create_pending 需要);per_track 信息留到响应组装
        # 阶段用,多 track 时生成"号码图 + tracks 元信息",单 track 时走老的拼图。
        for tid in sorted(video_per_track.keys()):
            candidates.extend(video_per_track[tid])
        source = "from_media"
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "register/preview 需要四选一:cluster_id / media_b64_list(多图)/ "
                "media_b64+media_kind=image / media_b64+media_kind=video"
            ),
        )

    if not candidates:
        return NormalResponse(
            code=0, message="no valid subject",
            data={"status_preview": "no_valid_subject", "candidates": []},
        )
    # 多图批量(from_media_batch)路径跳过 pHash 去重——用户手挑的多张图,场景接近时
    # pHash 经常 < 28 导致全收敛到 1,违反用户"我发 N 张就该看到 N 张候选"预期。
    # ReID 备路径(cos ≥ 0.9)仍然走,挡掉"真的重复发同一张图"corner case。
    # 视频 / 单图 / 池路径保持 pHash 去重(冗余天然高,select_topk 设计本意)。
    topk_kwargs: dict = {"topk": body.topk}
    if source == "from_media_batch":
        topk_kwargs["skip_phash_dedup"] = True
    # 视频附件路径走 select_topk_with_frontal_seed: 正脸优先 + face cand 优先 +
    # 凑满 topk 三件套, 解决"侧脸/抬头屠榜 score 排序 / 无脸 body 抢有脸名额 /
    # 同人 dedup 阈值过严选不满"三个问题。详见 helper docstring。
    # 单图 / 多图 / 池注册路径保持默认 select_topk, 行为零回归。
    from miloco.perception.engine.identity.registration_filter import (
        select_topk as _select_topk,
    )
    from miloco.perception.engine.identity.registration_filter import (
        select_topk_with_frontal_seed as _select_topk_frontal,
    )
    # bool(): body.media_b64 是 str | None, 用 and 短路时返回 str/None 而非 bool,
    # 真值上下文里没事但给 select_fn 加类型注解 / mypy strict 时会告警。
    is_video = bool(body.media_b64) and body.media_kind == "video"
    # create_pending 的 select_fn: 仅单 track 视频走 V_combined helper, 多人视频用
    # plain select_topk (防混合候选跨 track 污染, 详见 _should_use_frontal_seed)。
    # 多 track 时 create_pending 的合并选样结果会被丢弃 (号码图改走下方 per-track
    # 选样), per-track V_combined 在下方 montage 循环里跑 (每 track 单人候选, 安全)。
    use_frontal_seed = _should_use_frontal_seed(is_video, video_per_track)
    if use_frontal_seed:
        select_fn = _select_topk_frontal
        # 启用 V_combined: body ReID-driven 选 cand + face 独立 ReID-driven 选 face,
        # 解决 V_reid 单 ReID 让 face 多样性退化的问题 (sim 实测 4/4 视频上 face_emb
        # mean cos 从 V_reid 的 0.87-0.96 降到 V_combined 的 0.78-0.89)。
        # reid_extractor=None 时 helper 自动退化到 V0 (兜底), 不破当前体验。
        # 摄像头掉线时 get_reid_extractor 走懒加载独立 HumanReID, 不会拿到 None
        # (memory: identity_reid_camera_decoupled.md)。
        topk_kwargs["reid_extractor"] = manager.perception_service.get_reid_extractor()
    else:
        select_fn = _select_topk
    pending_id, sr, sess = manager.register_session_manager.create_pending(
        candidates, source=source, member_id=body.member_id,
        select_topk_kwargs=topk_kwargs,
        select_fn=select_fn,
    )

    def _crop_b64(crop) -> str:
        if crop is None or crop.size == 0:
            return ""
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return base64.b64encode(buf.tobytes()).decode() if ok else ""

    def _resize_h(img, target_h: int):
        h, w = img.shape[:2]
        if h <= 0 or w <= 0:
            return None
        new_w = max(1, int(round(w * target_h / h)))
        return cv2.resize(img, (new_w, target_h), interpolation=cv2.INTER_AREA)

    # ===== 视频多 track 分支:每 track 各自 select_topk,拼"号码图"让用户选 =====
    # 视频 video_per_track 在 video 分支生成;image / cluster 路径没有此变量。
    # 多 track 时 turn 1 必须让用户选号(不能直接给 auto_selected 全图,跨人 commit 会
    # 污染身份样本);单 track 走老路径 = body 256h + face 128h 拼图给用户确认。
    multi_track = False
    track_count = 1
    tracks_meta: list[dict] = []
    numbered_montage_b64 = ""
    # 多 track 判定跟 create_pending 的 select_fn 决策共用单一真相源
    # (_should_use_frontal_seed): 视频且 ≥2 track ⟺ not use_frontal_seed。避免
    # 两处独立写判定漂移 (漏一处 → create_pending 跑 V_combined 但 montage 不走
    # 号码图, 或反之, 引发跨身份污染 / 状态歧义)。
    if is_video and not use_frontal_seed:
        multi_track = True
        track_count = len(video_per_track)
        rep_imgs = []
        # per-track 选样跟单 track 同款 V_combined (正脸优先 + face 优先 + 凑满 topk),
        # 保证每个 track 真正入库的样本质量一致 —— 用户挑号后 commit 走的就是这里产的
        # auto_selected_indices_global。reid_extractor 循环外取一次 (同一实例)。
        reid_extractor = manager.perception_service.get_reid_extractor()
        for rank, tid in enumerate(sorted(video_per_track.keys())):
            track_cands = video_per_track[tid]
            if not track_cands:
                continue
            # 每 track 独立跑 V_combined (select_topk_with_frontal_seed): body
            # ReID-driven + face 独立 ReID-driven, 正脸/face 优先, 跟单 track 一致。
            # face_cands==0 (全程背身/戴口罩/头被挡) 时 helper 契约返 no_valid_subject
            # + samples=[], 会让该 track global_picks=[] → 用户挑该号后
            # commit_pending(indices=[]) 注册 0 张崩溃 → 兜底回退轻量 select_topk
            # (不要求 face, 至少凑 body 样本跑通体验)。
            sr_t = _select_topk_frontal(
                track_cands, topk=body.topk, reid_extractor=reid_extractor,
            )
            if not sr_t.samples:
                sr_t = _select_topk(track_cands, topk=body.topk, min_k=1)
            # 防御性 check:依赖选样 helper 直接 ``selected.append(cand)`` 保持对象
            # 引用(没 deepcopy / dataclass.replace 重建; V_combined 只 in-place 改
            # face_crop 不换对象, identity 不变)。若未来内部破坏 identity, 显式抛
            # 500 让 client 看到语义化错误, 而非 global_picks 静默空 list 导致前端
            # 拿不到 auto_selected。
            # 注:不用 ``assert`` —— python -O 模式下 assert 被编译移除会 silent
            # 失效;HTTPException 永远有效。
            if not all(any(s is c for c in track_cands) for s in sr_t.samples):
                logger.error(
                    "选样 helper 返回的 sample 不在原 track_cands 引用集合内 — "
                    "对象 identity 被破坏(可能内部加了 deepcopy/dataclass.replace),"
                    "id() 反向映射 global_picks 会失效"
                )
                raise HTTPException(
                    status_code=500,
                    detail="register/preview: 选样 helper 内部对象 identity 破坏,"
                           "请检查代码改动(global_picks 映射失效)",
                )
            picked_ids = {id(s) for s in sr_t.samples}
            global_picks = [i for i, c in enumerate(candidates)
                            if id(c) in picked_ids and c.track_id == tid]
            # 号码图代表样本(封面):**有 face 的 body 优先**,同 face 状态下按
            # score 降序。理由同 tier_u._cluster_candidate_for:带正脸的封面让
            # 用户挑人更容易辨认;整 track 全无 face 时退化为单纯按 score。
            cands_sorted = sorted(
                track_cands,
                key=lambda c: (c.face_crop is not None, c.score),
                reverse=True,
            )
            rep = cands_sorted[0]
            rep_img = _resize_h(rep.body_crop, 256) if rep.body_crop is not None else None
            if rep_img is not None:
                # 黑底白字 [N] 大标签(左上角)— cv2 内置不支持 unicode 圆圈字符,
                # 用 ASCII [1] [2] [3] 代替,大字号清晰
                label = f"[{rank+1}]"
                rep_img = rep_img.copy()  # 防共享内存写回 candidates
                # 标签框尺寸:原 80×50 缩到 70% = 56×35, font 1.2→0.84, thickness 2→1
                cv2.rectangle(rep_img, (0, 0), (56, 35), (0, 0, 0), -1)
                cv2.putText(rep_img, label, (5, 27),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.84, (255, 255, 255), 1)
                rep_imgs.append(rep_img)
            tracks_meta.append({
                "label": rank + 1,
                "track_id": tid,
                "body_count": len(track_cands),
                "face_count": sum(1 for c in track_cands if c.face_crop is not None),
                "auto_selected_indices_global": global_picks,
                "auto_status": sr_t.status,
            })
        if rep_imgs:
            numbered_row = cv2.hconcat(rep_imgs)
            ok, buf = cv2.imencode(".jpg", numbered_row, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                numbered_montage_b64 = base64.b64encode(buf.tobytes()).decode()

    # ===== 单 track / 图片路径:走 auto_selected body 256h + face 128h 拼图 =====
    auto_montage_b64 = ""
    auto_body_count = 0
    auto_face_count = 0
    if not multi_track:
        auto_cands = [candidates[i] for i in sess.auto_selected_indices
                       if 0 <= i < len(candidates)]
        if auto_cands:
            body_imgs = [r for c in auto_cands
                         if (r := _resize_h(c.body_crop, 256)) is not None]
            face_imgs = [r for c in auto_cands
                         if c.face_crop is not None
                         and (r := _resize_h(c.face_crop, 128)) is not None]
            if body_imgs:
                body_row = cv2.hconcat(body_imgs)
                output = body_row
                if face_imgs:
                    face_row = cv2.hconcat(face_imgs)
                    bw, fw = body_row.shape[1], face_row.shape[1]
                    if bw != fw:
                        tgt = max(bw, fw)
                        if bw < tgt:
                            pad = tgt - bw
                            body_row = cv2.copyMakeBorder(
                                body_row, 0, 0, pad // 2, pad - pad // 2,
                                cv2.BORDER_CONSTANT, value=(255, 255, 255))
                        if fw < tgt:
                            pad = tgt - fw
                            face_row = cv2.copyMakeBorder(
                                face_row, 0, 0, pad // 2, pad - pad // 2,
                                cv2.BORDER_CONSTANT, value=(255, 255, 255))
                    output = cv2.vconcat([body_row, face_row])
                ok, buf = cv2.imencode(".jpg", output, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    auto_montage_b64 = base64.b64encode(buf.tobytes()).decode()
                    auto_body_count = len(body_imgs)
                    auto_face_count = len(face_imgs)

    return NormalResponse(
        code=0, message="preview ready",
        data={
            "register_session_id_pending": pending_id,
            "expires_at": sess.expires_at,
            # 多 track 时 auto_selected_indices 仍是跨 track 平铺的 — agent **不应使用**,
            # 应使用 tracks[i].auto_selected_indices_global(用户选号后取对应那组)
            "auto_selected_indices": sess.auto_selected_indices,
            "status_preview": sr.status,
            # 多 track 标志 + 号码图 + per-track 元信息(agent 看到 multi_track=true 时
            # 必发号码图让用户选号,不要直接 commit)
            "multi_track": multi_track,
            "track_count": track_count,
            "tracks": tracks_meta,
            "numbered_montage_jpeg_b64": numbered_montage_b64,
            # 单 track 路径:body 256h + face 128h 拼图(用户看图确认即可)
            "auto_selected_montage_jpeg_b64": auto_montage_b64,
            "auto_selected_body_count": auto_body_count,
            "auto_selected_face_count": auto_face_count,
            "candidates": [
                {
                    "index": i,
                    "score": c.score,
                    "sharpness": c.sharpness,
                    "detector_conf": c.detector_conf,
                    "captured_at": c.captured_at,
                    "phash_hex": format(c.phash, "x"),
                    "auto_selected": i in sess.auto_selected_indices,
                    "track_id": c.track_id,
                    "cam_id": c.cam_id,
                    "image_jpeg_b64": _crop_b64(c.body_crop),
                    "has_face": c.face_crop is not None,
                }
                for i, c in enumerate(candidates)
            ],
        },
    )


@router.post(
    "/register/commit",
    summary="Register Commit (step 2 of 2)",
    response_model=NormalResponse,
)
async def register_commit(
    body: RegisterCommitPayload,
    current_user: str = Depends(verify_token),
):
    """两步走第 2 步:按 indices 真正入库。member_id 缺失时按 name+role 新建。"""
    # 注册流程统一 member resolver:按 member_id 绑定既有成员(带 role 补写 SQL),或按 name
    # 新建 / 复用(name 重复=追加样本不报 Conflict)。两路都保证 role 落 SQL。
    _resolver = _resolve_member

    # reid_extractor:身份库写盘时,若 BodySample.reid_embedding 为 None
    # (陌生人池 L1/L2 都没拉到 emb 的极端 race) 用它现场抽一次。无活动
    # deep_sort tracker 时返 None,库就跳过兑底(行为退回旧版)。
    result = manager.register_session_manager.commit_pending(
        body.register_session_id_pending,
        indices=body.indices,
        member_name=body.member_name,
        member_role=body.member_role,
        member_resolver=_resolver,
        reid_extractor=manager.perception_service.get_reid_extractor(),
    )
    if result is None:
        raise HTTPException(
            status_code=410,
            detail="pending session 过期 / 不存在 / 无目标身份",
        )
    # 注册可能按 name 新建成员（member_id 缺失时），级联刷新家庭档案 md 让新成员进档案，否则 profile.md 不刷新
    try:
        manager.home_profile_service.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("级联刷新家庭档案失败(register_commit) person_id=%s: %s", result.person_id, e)
    return NormalResponse(
        code=0, message="committed",
        data={
            "person_id": result.person_id,
            "register_session_id": result.register_session_id,
            "written_samples": result.written_samples,
            "status": result.selection_status,
        },
    )


@router.get(
    "/register/sessions",
    summary="List Register Sessions",
    response_model=NormalResponse,
)
async def list_register_sessions(
    member_id: str | None = None,
    limit: int = 20,
    current_user: str = Depends(verify_token),
):
    """列历史注册批次。member_id 给定时只看该成员;否则全库扫。"""
    if member_id is not None and not _PERSON_ID_RE.match(member_id):
        raise HTTPException(status_code=400, detail="Invalid member_id format")
    sessions = manager.register_session_manager.list_sessions(
        member_id=member_id, limit=limit,
    )
    return NormalResponse(
        code=0, message=f"{len(sessions)} sessions",
        data={"sessions": [
            {
                "register_session_id": s.register_session_id,
                "member_id": s.member_id,
                "member_name": s.member_name,
                "created_at": s.created_at,
                "written_count": s.written_count,
                "source": s.source,
                "cluster_id": s.cluster_id,
            } for s in sessions
        ]},
    )


# =============================================================================
# 算法独立入口 M4 / M5:extract / select(给 CLI 与 web 直接调)
# =============================================================================


class ExtractPayload(BaseModel):
    media_b64: str
    media_kind: str = "image"


class SelectPayload(BaseModel):
    candidates: list[dict]
    topk: int = 3
    min_k: int = 1


@router.post(
    "/extract",
    summary="Extract candidate samples (M4)",
    response_model=NormalResponse,
)
async def extract_endpoint(
    body: ExtractPayload,
    current_user: str = Depends(verify_token),
):
    """从图像抽 ScoredCandidate。返回 candidates(base64 jpeg + 元数据)。"""
    import base64

    from miloco.perception.engine.identity.extractor import extract_from_image

    if body.media_kind != "image":
        raise HTTPException(
            status_code=400, detail="本期 extract 端点仅支持 media_kind='image'",
        )
    raw = base64.b64decode(body.media_b64)
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        raise HTTPException(status_code=400, detail="image decode failed")
    candidates = await asyncio.to_thread(
        extract_from_image,
        img, detector=_load_detector(),
        reid_extractor=manager.perception_service.get_reid_extractor(),
    )

    out = []
    for c in candidates:
        ok, buf = cv2.imencode(".jpg", c.body_crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        body_jpeg_b64 = base64.b64encode(buf.tobytes()).decode() if ok else ""
        out.append({
            "score": c.score,
            "bbox": list(c.bbox_xyxy),
            "sharpness": c.sharpness,
            "detector_conf": c.detector_conf,
            "captured_at": c.captured_at,
            "track_id": c.track_id,
            "cluster_id": c.cluster_id,
            "cam_id": c.cam_id,
            "phash_hex": format(c.phash, "x"),
            "image_jpeg_b64": body_jpeg_b64,
        })
    return NormalResponse(
        code=0, message=f"extracted {len(out)} candidates",
        data={"candidates": out},
    )


# =============================================================================
# 陌生人池(TierU)M3:status / list / fetch / show / cluster-split / from-cluster
# =============================================================================


class PoolClusterSplitPayload(BaseModel):
    cluster_id: str
    # 精确剥离的成员: (cam_id, track_id) 二元组列表;pydantic 强制校验形状,
    # client 传错(单元素 / 三元素 / 类型错)在 parse 阶段就 422,不进 split 路径。
    remove_members: list[tuple[str, int]] | None = None
    remove_cams: list[str] | None = None


class RegisterFromClusterPayload(BaseModel):
    cluster_id: str
    member_name: str | None = None
    member_role: str | None = None
    member_id: str | None = None
    # 默认 5: 跟 RegisterPreviewPayload + CLI --topk + SKILL.md 注册示例对齐,
    # 统一注册口径 (从陌生人池注册某人也是注册主流程的一种, agent 不传 topk 也拿 5)。
    topk: int = 5

    # 同 RegisterCommitPayload: member_role 空串/纯空白归一成 None, 与 CRUD 口径一致。
    _norm_role = field_validator("member_role")(_normalize_optional_str)


def _get_tier_u_pool():
    """统一拿池子(失败 → 503 Service Unavailable),所有 pool/* 端点共用。

    503 而非 404:池"服务不可用"语义更准 — 404 通常表示具体资源(cluster_id 等)
    不存在,池整个未启用是配置 / 启动失败问题,client 拿到 404 会误以为找错 id。
    """
    pool = manager.perception_service.tier_u_pool
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="TierUPool 未启用(identity_engine 关闭或启动失败)",
        )
    return pool


def _encode_crop_b64(crop) -> str:
    """把 CropEntry.body_crop 编 jpeg base64,失败返 ''(给 JSON 序列化用)。"""
    import base64
    if crop is None or crop.size == 0:
        return ""
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf.tobytes()).decode() if ok else ""


def _cluster_to_dict(cand, *, with_crops: bool = False) -> dict:
    """ClusterCandidate → dict;with_crops=True 时把 representative crop 编 b64。"""
    out = {
        "cluster_id": cand.cluster_id,
        "members": [list(m) for m in cand.members],
        "total_crops": cand.total_crops,
        "span_cam_count": cand.span_cam_count,
        "earliest_ts": cand.earliest_ts,
        "latest_ts": cand.latest_ts,
        "representative": {
            "cam_id": cand.representative_crop.cam_id,
            "track_id": cand.representative_crop.track_id,
            "sharpness": cand.representative_crop.sharpness,
            "bbox_xyxy": list(cand.representative_crop.bbox_xyxy),
            "captured_at": cand.representative_crop.captured_at,
        },
        "per_cam_representative": {
            cam: {
                "sharpness": c.sharpness,
                "bbox_xyxy": list(c.bbox_xyxy),
                "captured_at": c.captured_at,
            }
            for cam, c in cand.per_cam_representative.items()
        },
    }
    if with_crops:
        out["representative"]["image_jpeg_b64"] = _encode_crop_b64(
            cand.representative_crop.body_crop,
        )
        for cam, c in cand.per_cam_representative.items():
            out["per_cam_representative"][cam]["image_jpeg_b64"] = _encode_crop_b64(
                c.body_crop,
            )
    return out


@router.get(
    "/pool/status",
    summary="陌生人池状态",
    response_model=NormalResponse,
)
async def pool_status(current_user: str = Depends(verify_token)):
    """池子总览:entry 数、cluster 数、内存占用、match_cache 大小。"""
    pool = _get_tier_u_pool()
    return NormalResponse(code=0, message="OK", data=pool.status())


# 多 cluster 时拼成号码图给 agent 一张图发用户;超过这个数截断 + has_more 提示
# (对齐 plan §10.2 "按清晰度排序的前 6 个 cluster")。
_POOL_FETCH_MAX_DISPLAY = 6


@router.get(
    "/pool/fetch",
    summary="陌生人池取注册候选",
    response_model=NormalResponse,
)
async def pool_fetch(
    cam: str | None = None,
    track: int | None = None,
    window: float | None = None,
    with_crops: bool = False,
    offset: int = 0,
    current_user: str = Depends(verify_token),
):
    """取近 window 秒的 cluster 候选。

    - ``cam`` + ``track`` 都给 → 锁定该 entry 所属 cluster(推送响应路径)。
    - ``cam`` 单独 → 该相机近 window 的全部 cluster。
    - 都不给 → 全局,跨 cam 跑一次 union。

    分页(``offset``,从 0 开始):
        cands 已按 representative.sharpness + face 优先排序;每次返回从 offset
        开始的至多 _POOL_FETCH_MAX_DISPLAY 个 cluster。用户回"更多"时 agent 用
        本次响应的 ``next_offset`` 重发 fetch,即可拉下一页。``next_offset`` 为
        null 表示已到末页。

        ⚠️ **分页稳定性受池写入速率影响**: cands 排序键 (face 可选, sharpness) 是
        实时计算结果, 两次 fetch 之间若有新 crop 进池触发 cluster 创建 / 现有
        cluster 的 representative 变化, ``offset=N`` 取出的内容可能与"上一页未展示
        的第 N+1 个"不一致, 出现部分重复或漏 cluster。家用场景(几秒内池子稳定)
        无感, 高频推送 / 多人同时活动场景可能见到。

    Returns:
        - ``clusters_total`` 总共多少 cluster
        - ``clusters_displayed`` 本页 montage 展示了几个
        - ``offset`` 本页起始位置(回显入参)
        - ``next_offset`` 下一页起始位置 (no more → null)
        - ``has_more`` 是否还有下一页

    Note:
        默认 ``with_crops=false``——只返元数据 + numbered_montage(避免 11×
        100KB base64 把 stdout 撑大被 OpenClaw 截);要 web v2 用各 crop
        base64 时显式传 ``with_crops=true``。
    """
    import base64
    pool = _get_tier_u_pool()
    # v2 重构后 TierU entry.cam_id 已统一改为米家 device_id, 跟前端 device list 入参
    # 命名空间一致, 直接透传无需中间解析层(老版的 resolve_cam_id_to_scope_label 已删)。
    #
    # 双层去重 (case b/c): fetch 末尾跟 TierA 已注册成员 + 当前 confirmed track
    # 的实时 emb 比对, 命中 cluster 被 close_write_gate 物理清池 + 不进挑号拼图。
    # - tier_a_emb_lookup: 拼 {person_id: mean_emb}, 覆盖"已入库人在 TierU 残留"
    # - confirmed_track_keys: (cam_id, track_id) 列表, pool 内部从 reid_provider 取
    #   实时 emb, 覆盖"镜头里某人刚被识别成已知, TierU 里同人 cluster 还在"
    # emb lookup 构造涉及 fs glob + 多次 np.load (同步 IO), 包进 to_thread
    # 避免阻塞 event loop —— 跟 engine.py add_tier_c_sample 同款并发惯例对齐。
    # 家用 ≤10 person 体感无感, 但跨摄像头扩到 50 person 时单次可能阻塞 500ms-1s,
    # 期间 push notification / camera ingest 等并发请求被拖延。
    #
    # TODO(follow-up PR): 给 IdentityLibrary 加 _mean_emb_cache + _tier_c_emb_cache
    # 按 tier_a_dir/tier_c_dir mtime 做 invalidate, 写路径 (add_tier_a_sample /
    # delete_by_register_session / add_tier_c_sample) 主动清相关 person 缓存。
    # 现况是 user 每次"更多"翻页都要重扫所有 person, ≥50 person + tier_c 上百时
    # 单次 wall-clock 数百毫秒, 用户挑号交互路径上能感知。
    library = _get_identity_library()

    # target 锁定单 cluster (track 给定) 时 fetch 内部整段跳过三层去重 (见 fetch
    # docstring: target_track_id/target_cluster_id 锁定时本层及下两层都跳过),
    # 此时构造 lookup 传进去根本不消费 —— 纯浪费 fs 扫描 (≥50 person 数百 ms~1s,
    # 用户点一次"这是我自己"白等)。track is None (cam 单独 / 全局) 才需要 lookup。
    tier_a_emb_lookup: dict[str, "np.ndarray"] = {}
    tier_c_emb_lookup: dict[str, list["np.ndarray"]] = {}
    confirmed_track_keys: list[tuple[str, int]] = []
    if track is None:
        def _build_emb_lookups() -> tuple[dict[str, "np.ndarray"],
                                           dict[str, list["np.ndarray"]]]:
            tier_a: dict[str, "np.ndarray"] = {}
            tier_c: dict[str, list["np.ndarray"]] = {}
            for ref in library.list_persons():
                emb = library.get_person_mean_emb(ref.person_id)
                if emb is not None:
                    tier_a[ref.person_id] = emb
                tc_embs = library.get_person_tier_c_embs(ref.person_id)
                if tc_embs:
                    tier_c[ref.person_id] = tc_embs
            return tier_a, tier_c

        try:
            tier_a_emb_lookup, tier_c_emb_lookup = await asyncio.to_thread(
                _build_emb_lookups,
            )
        except Exception:  # noqa: BLE001
            logger.warning("pool_fetch 构造 tier_a/tier_c emb lookup 失败,本次跳过去重",
                           exc_info=True)
        confirmed_track_keys = manager.perception_service.get_active_confirmed_track_keys()
    cands = pool.fetch(
        cam_id=cam, target_track_id=track, window_sec=window,
        reid_extractor=manager.perception_service.get_reid_extractor(),
        tier_a_emb_lookup=tier_a_emb_lookup,
        confirmed_track_keys=confirmed_track_keys,
        tier_c_emb_lookup=tier_c_emb_lookup,
    )
    # cands 已按 representative.sharpness 降序(TierUPool._build_cluster_candidates)
    total = len(cands)
    offset = max(0, offset)  # 防御性 clamp,负数当 0 处理
    display = cands[offset:offset + _POOL_FETCH_MAX_DISPLAY]

    # 拼号码图:每 cluster 一张 representative body crop 横排,标 [N]
    def _resize_h(img, h: int):
        ih, iw = img.shape[:2]
        if ih <= 0 or iw <= 0:
            return None
        nw = max(1, int(round(iw * h / ih)))
        return cv2.resize(img, (nw, h), interpolation=cv2.INTER_AREA)

    rep_imgs = []
    tracks_meta = []
    for rank, c in enumerate(display):
        body = c.representative_crop.body_crop
        if body is None or body.size == 0:
            continue
        img = _resize_h(body, 256)
        if img is None:
            continue
        img = img.copy()
        # 标签框尺寸:原 80×50 缩到 70% = 56×35, font 1.2→0.84, thickness 2→1
        cv2.rectangle(img, (0, 0), (56, 35), (0, 0, 0), -1)
        cv2.putText(img, f"[{rank+1}]", (5, 27),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.84, (255, 255, 255), 1)
        rep_imgs.append(img)
        tracks_meta.append({
            "label": rank + 1,
            "cluster_id": c.cluster_id,
            "members": [list(m) for m in c.members],
            "total_crops": c.total_crops,
            "span_cam_count": c.span_cam_count,
            "rep_sharpness": c.representative_crop.sharpness,
            "earliest_ts": c.earliest_ts,
            "latest_ts": c.latest_ts,
        })
    numbered_montage_b64 = ""
    if rep_imgs:
        row = cv2.hconcat(rep_imgs)
        ok, buf = cv2.imencode(".jpg", row, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            numbered_montage_b64 = base64.b64encode(buf.tobytes()).decode()

    end = offset + len(display)
    has_more = end < total
    return NormalResponse(
        code=0, message=f"{total} clusters",
        data={
            "clusters_total": total,
            "clusters_displayed": len(display),
            "offset": offset,
            "next_offset": end if has_more else None,
            "has_more": has_more,
            "tracks": tracks_meta,           # agent 选号后用 tracks[N-1].cluster_id 走 from-cluster
            "numbered_montage_jpeg_b64": numbered_montage_b64,
            # 旧字段 clusters 保留给 web v2 / 调试(默认 with_crops=false 时不含 base64)
            "clusters": [_cluster_to_dict(c, with_crops=with_crops) for c in cands],
        },
    )


def _pool_dump_safe_prefix() -> str:
    """落盘根目录: ``$MILOCO_HOME/snapshots/tier_u`` (跟随 MILOCO_HOME env 覆盖)。

    用函数而非模块级常量,保证 ``monkeypatch.setenv("MILOCO_HOME", ...)`` 在测试
    里立刻生效 (``miloco_home()`` 不缓存,每次调用读 env)。

    选 $MILOCO_HOME 系跟项目其他 state 同根 (config.json / SQLite / 模型 / 日志 /
    证书都在这里),避免 /tmp/ 选型带来的三类坑:
      1. macOS /private/tmp 跨日清理 / Linux systemd-tmpfiles 默认 10 天清扫
      2. /tmp/ 全局 0777, 多用户 / 容器共享 host 时被串读
      3. 跟项目惯例分裂, oncall 找快照按惯例去 $MILOCO_HOME 下找不到
    """
    return str(miloco_home() / "snapshots" / "tier_u")


def _validate_pool_dump_path(target: str) -> str:
    """校验 ``target`` 落在 ``_pool_dump_safe_prefix()`` 下,返回 realpath。

    抽成纯函数让 security boundary 走单元测试 (不依赖 FastAPI / pool / token),
    handler 调一行即可。realpath 解析 ``..`` 与 symlink, SAFE_PREFIX 也 realpath
    跨 macOS (``/tmp`` → ``/private/tmp``) / Linux 一致。

    **本函数无副作用** —— 不建目录、不写文件;真正落盘时由 ``pool.dump_to(real_target)``
    内部 ``os.makedirs(real_target, exist_ok=True)`` 递归建链路上所有祖先目录
    (含 SAFE_PREFIX), validator 自身只做纯校验。这让 validator 可被 CLI
    dry-run 之类纯校验场景复用, 不污染文件系统。

    layering: 函数 raise 平凡 ``ValueError`` 而非 ``HTTPException`` —— 校验逻辑
    不耦合 web 层异常体系,长期可被 CLI / 后台任务复用。``pool_dump`` handler
    捕获后翻译成 ``HTTPException(400, ...)``。

    Raises:
        ValueError: target 实际落点不在 SAFE_PREFIX 下;消息体已含传入 + 解析后路径。
    """
    import os
    real_target = os.path.realpath(target)
    safe_prefix = _pool_dump_safe_prefix()
    real_prefix = os.path.realpath(safe_prefix)
    if not (real_target == real_prefix or real_target.startswith(real_prefix + os.sep)):
        raise ValueError(
            f"path 必须在 {safe_prefix} 下 (传入: {target!r}, 解析后: {real_target!r})",
        )
    return real_target


@router.post(
    "/pool/dump",
    summary="陌生人池快照(离线调试用)",
    response_model=NormalResponse,
)
async def pool_dump(
    path: str | None = None,
    current_user: str = Depends(verify_token),
):
    """把当前池子状态完整落到本地目录。给离线调阈值/dedup 逻辑用——dump 出来后
    scp 回开发机,本地 Python REPL ``TierUPool.load_from(path)`` 反复试。

    Args:
        path: 落盘目录(server 本地路径), **必须**在 ``_pool_dump_safe_prefix()``
            (默认 ``$MILOCO_HOME/snapshots/tier_u``) 下。None → 自动用
            ``$MILOCO_HOME/snapshots/tier_u/tier_u_snapshot_{unix_ts}``。
            传 ``../`` / 绝对路径越界 → 400(防 API token 泄漏后被写任意路径)。

    Returns:
        ``{path, real_path, entries, clusters, arrays, manifest_bytes, arrays_bytes}``:

        - ``path``: 客户端传入字面绝对路径 (与用户视角一致)
        - ``real_path``: realpath 解析后的真实磁盘路径 (macOS /tmp 系列下不同, 但
          $MILOCO_HOME 下默认两者相等); 落盘实际写到这里, 客户端 scp 二选一都行

    Raises:
        HTTPException(404): ``perception.tier_u_dump_enable=false`` 时返 404
            (生产默认关闭, 不暴露端点存在防 fingerprint)。
        HTTPException(400): path 不在 ``_pool_dump_safe_prefix()`` 下。
    """
    import os
    import time as _time
    # 调试端点 enable 检查: 生产默认关 (settings.yaml: perception.tier_u_dump_enable=false),
    # 防 API token 泄漏后被持续触发 dump 拉走 body crop 像素 + cluster 拓扑 + 时间戳。
    # 404 而非 403: 减少 fingerprint 面 (不暴露端点存在)。本地调试请在 config.json /
    # 环境变量里设 perception.tier_u_dump_enable=true。
    if not get_settings().perception.tier_u_dump_enable:
        raise HTTPException(
            status_code=404,
            detail="pool/dump 已关闭; 需要离线调试快照请设 perception.tier_u_dump_enable=true",
        )
    pool = _get_tier_u_pool()
    target = path or f"{_pool_dump_safe_prefix()}/tier_u_snapshot_{int(_time.time())}"

    # 路径白名单校验抽成纯函数 (无副作用);校验逻辑细节见 ``_validate_pool_dump_path``。
    # 落盘传 ``real_target`` 而非原始 ``target``:消除 TOCTOU 窗口——校验后到
    # ``dump_to`` 之间若有人把 ``target`` 路径上的目录改成 symlink → 任意位置,
    # 用 realpath 已解析过的固定路径继续写就不再受影响。
    try:
        real_target = _validate_pool_dump_path(target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 路径建出: dump_to 内部 os.makedirs(real_target, exist_ok=True) 递归建链路上所有
    # 祖先目录, 自动覆盖 SAFE_PREFIX → 不需要 handler 再单独建一次。
    summary = pool.dump_to(real_target)
    # 双字段返回:``path`` 保留客户端传入字面 (与用户视角一致), ``real_path`` 给磁盘
    # 真实位置 (macOS /tmp 系列下不同, $MILOCO_HOME 下默认两者相等)。客户端 scp 任挑一个。
    return NormalResponse(
        code=0, message="dump ok",
        data={"path": os.path.abspath(target), "real_path": real_target, **summary},
    )


@router.post(
    "/pool/cluster-split",
    summary="拆分误合并的 cluster",
    response_model=NormalResponse,
)
async def pool_cluster_split(
    body: PoolClusterSplitPayload,
    current_user: str = Depends(verify_token),
):
    """commit 前的"误合并修正":把 cluster 内一批成员剥到新 cluster_id。

    - ``remove_members``: 精确剥离 [(cam_id, track_id), ...]
    - ``remove_cams``: 按 cam 批量剥(快速路径)
    两者 OR;命中 0 个或剩余 0 个 → 410(no-op)。
    """
    pool = _get_tier_u_pool()
    # pydantic 已强校验 list[tuple[str, int]] 形状,直接传给 split_cluster
    result = pool.split_cluster(
        body.cluster_id,
        remove_members=body.remove_members,
        remove_cams=body.remove_cams,
    )
    if result is None:
        raise HTTPException(status_code=410, detail="cluster 不存在或 selector 无效")
    kept_cid, new_cid = result
    return NormalResponse(
        code=0, message="split ok",
        data={"kept_cluster_id": kept_cid, "new_cluster_id": new_cid},
    )


@router.post(
    "/register/from-cluster",
    summary="按陌生人池 cluster_id 注册",
    response_model=NormalResponse,
)
async def register_from_cluster(
    body: RegisterFromClusterPayload,
    current_user: str = Depends(verify_token),
):
    """从已有 cluster 直接登记成员(SKILL 工作流 B / C 终态)。

    流程:fetch cluster → 把 L2 crops 转 ScoredCandidate → select_topk → commit。
    """
    from miloco.perception.engine.identity.extractor import extract_from_pool

    pool = _get_tier_u_pool()
    cands = pool.fetch(
        target_cluster_id=body.cluster_id,
        reid_extractor=manager.perception_service.get_reid_extractor(),
    )
    target = cands[0] if cands else None
    if target is None:
        raise HTTPException(status_code=404, detail=f"cluster {body.cluster_id} 不在池内")

    # 用 target.cluster_id 而非 body.cluster_id: fetch 期间 dedup tick 可能把
    # 用户选的 cluster 合并到另一个, target.cluster_id 是落地后的真实 id
    scored_by_cid = extract_from_pool([target])
    scored = scored_by_cid.get(target.cluster_id, [])
    if not scored:
        raise HTTPException(
            status_code=409, detail="cluster 无 L2 crop,等更多帧累积后再注册",
        )

    # 注册流程统一 member resolver:按 member_id 绑定既有成员(带 role 补写 SQL),或按 name
    # 新建 / 复用(name 重复=追加样本不报 Conflict)。两路都保证 role 落 SQL。
    _resolver = _resolve_member

    # cluster_id 不显式传入 commit_oneshot;extract_from_pool 已把 cluster_id 写到
    # 每个 ScoredCandidate.cluster_id,sidecar 落盘后历史 sessions 端点能查到。
    result = manager.register_session_manager.commit_oneshot(
        scored,
        member_name=body.member_name,
        member_role=body.member_role,
        member_id=body.member_id,
        source="from_cluster",
        select_topk_kwargs={"topk": body.topk},
        member_resolver=_resolver,
        reid_extractor=manager.perception_service.get_reid_extractor(),
    )
    if result is None:
        raise HTTPException(status_code=410, detail="提交失败(筛选无可用样本)")

    # commit 成功 → 关闭 cluster 全部成员的写入 gate(决策 1.1 α 行为)
    try:
        for cam_id, track_id in target.members:
            pool.close_write_gate(cam_id, track_id)
    except Exception:  # noqa: BLE001
        logger.warning("close_write_gate 失败 cluster=%s", body.cluster_id,
                       exc_info=True)

    return NormalResponse(
        code=0, message="committed from cluster",
        data={
            "person_id": result.person_id,
            "register_session_id": result.register_session_id,
            "written_samples": result.written_samples,
            "status": result.selection_status,
            "cluster_id": target.cluster_id,
        },
    )


@router.post(
    "/select",
    summary="Select topk from scored candidates (M5)",
    response_model=NormalResponse,
)
async def select_endpoint(
    body: SelectPayload,
    current_user: str = Depends(verify_token),
):
    """从 candidates 数组挑 topk(主 pHash + 时间;备 ReID)。"""
    import base64

    from miloco.perception.engine.identity.extractor import ScoredCandidate
    from miloco.perception.engine.identity.registration_filter import select_topk

    scored: list[ScoredCandidate] = []
    for d in body.candidates:
        b64 = d.get("image_jpeg_b64") or ""
        if b64:
            arr = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
            crop = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            crop = np.zeros((1, 1, 3), dtype=np.uint8)
        scored.append(ScoredCandidate(
            body_crop=crop,
            face_crop=None,
            score=float(d.get("score", 0.0)),
            bbox_xyxy=tuple(d.get("bbox", (0, 0, 1, 1))),
            frame_index=int(d.get("frame_index", 0)),
            captured_at=float(d.get("captured_at", 0.0)),
            track_id=d.get("track_id"),
            cluster_id=d.get("cluster_id"),
            cam_id=d.get("cam_id"),
            detector_conf=float(d.get("detector_conf", 0.0)),
            sharpness=float(d.get("sharpness", 0.0)),
            reid_embedding=None,
            phash=int(d.get("phash_hex", "0"), 16) if d.get("phash_hex") else 0,
        ))
    sr = select_topk(scored, topk=body.topk, min_k=body.min_k)
    sel_idx_set = {id(s) for s in sr.samples}
    selected_indices = [i for i, c in enumerate(scored) if id(c) in sel_idx_set]
    return NormalResponse(
        code=0, message=sr.status,
        data={
            "status": sr.status,
            "selected_indices": selected_indices,
            "rejected": [
                {"reason": reason, "score": c.score}
                for c, reason in sr.rejected
            ],
        },
    )
