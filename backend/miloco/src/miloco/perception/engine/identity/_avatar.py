# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""统一头像存取（人 / 宠物共用）。

落点：``<lib_root>/avatars/<kind>/<subject_id>.<ext>``，其中 ``kind`` ∈ {"persons",
"pets"}。**展示头像与识别数据分离**——识别数据在 ``<lib_root>/persons/`` 与
``<lib_root>/pets/``，头像统一在 ``<lib_root>/avatars/``。

``avatars/`` 与 ``persons/`` / ``pets/`` 平级：``IdentityLibrary`` 只遍历
``root/persons``、``PetLibrary`` 只遍历 ``root/pets``，两者都从不遍历 ``root`` 本身，
故 ``avatars/`` 不会被任何识别扫描误触——给未录入的人设头像也**不会**扰动
IdentityEngine 的 person 快照（不新建 ``persons/<id>/`` 目录）。

头像**纯展示**：不进 person 表 / ReID / gallery / 识别参照。文件在盘即为权威
（ext 由文件名推导，无需 meta 指针）。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

import cv2

from miloco.perception.engine.identity._image_utils import decode_image

logger = logging.getLogger(__name__)

# subject_id 白名单：人的 UUID 与宠物 pet_<hex> 都只含这些字符。用作路径/日志前一律
# 强制校验——helper 不盲信调用方，从源头杜绝路径穿越与日志注入（纵深防御）。
_SAFE_SUBJECT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_subject_id(subject_id: str) -> str:
    if not _SAFE_SUBJECT_ID.fullmatch(subject_id or ""):
        raise ValueError(f"非法 subject_id: {subject_id!r}")
    return subject_id

# 允许的头像扩展名（小写、无点）；_EXT_ORDER 供确定性遍历（正常只存在一个）。
AVATAR_EXTS = frozenset({"jpg", "jpeg", "png", "webp"})
_EXT_ORDER = ("jpg", "jpeg", "png", "webp")
_AVATAR_MEDIA = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def normalize_avatar_ext(ext: str) -> str:
    e = (ext or "").lower().lstrip(".")
    if e not in AVATAR_EXTS:
        raise ValueError(f"不支持的头像格式: {ext!r}（允许 {sorted(AVATAR_EXTS)}）")
    return e


def media_type(ext: str) -> str:
    return _AVATAR_MEDIA.get((ext or "").lower().lstrip("."), "application/octet-stream")


# 头像体积上限（人 / 宠物端点共用，单一真源）：前端裁剪产物恒 ~20-50KB；直连 API 时用
# image.size 前置闸拦超大包（不必先读进内存）、读后 len 兜底。
AVATAR_MAX_BYTES = 5 * 1024 * 1024


def sniff_image_ext(data: bytes) -> str | None:
    """按文件头魔数判定「可直接落盘」的图片格式（jpg/png/webp），不看文件名后缀——杜绝
    「后缀与内容不符」，让盘上后缀 / Content-Type / 真实字节恒一致；不在此集合则 None。

    注意语义：``None`` **不等于**「不支持的格式」。HEIC/BMP/TIFF/GIF/AVIF 都能被
    ``decode_image`` 解开，只是不能原字节落盘（浏览器渲染不了 HEIC、且盘上格式集合要收敛），
    由 ``normalize_for_storage`` 解码后重编。判「支不支持」看后者的返回值，别看本函数。
    （不引 imghdr——3.13 已移除。）"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# 注：WebP 容器边长上限是 16383，超了 cv2.imencode 返回 ok=False 且只往 stderr 打一行、
# 不抛异常。下面的长边封顶（1024）已让这条不可能发生，但 ok 判定仍保留——它同时兜住
# 编码器内部的其它失败（OOM 等），不判就会把空 buf 当图落盘。

# 重编产物的长边上限，按用途分档——两个数字都不是拍脑袋的，各自锚在真实消费端上：
#
# 头像 256：正是 web 裁剪器 ``AvatarCropEditor`` 的 ``OUT``。让 CLI/API 直传这条路与 web
#   主路径产出**同规格**的东西，而不是各走各的。展示端是 28~48px 的圆（前端整个 blob
#   fetch 且 cache: no-store），256 已是 3x 屏所需的上限。实测 12MP HEIC：不封顶 5.26MB、
#   封到 1024 是 0.56MB、封到 256 是 0.05MB——最后这档与 web 裁剪器的 20-50KB 同量级。
# 参考图 640：它喂 omni 的横拼图，那里统一缩到高 320（``pet_refs._PET_COMPOSITE_HEIGHT``），
#   640 留了一倍余量给非方形 crop；不能跟头像同档，否则拼图会被放大糊掉。
#
# 只作用于**重编支**；白名单直通仍逐字节不动（零转码承诺）。
# 注：封顶之后仍用无损编码——看似矛盾（缩放丢掉的信息远多于编码），但这一档的体积已经
# 落在 web 主路径同量级，无损换来的是「不在已有损的 HEIC 上再叠一代」这条口径统一。
_AVATAR_MAX_SIDE = 256
_REF_CROP_MAX_SIDE = 640


def _cap_long_side(img, max_side: int):
    """长边超过 ``max_side`` 时等比缩小；否则原样返回（不复制）。"""
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img
    scale = max_side / longest
    return cv2.resize(
        img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA
    )


def normalize_for_storage(
    data: bytes, *, prefer: str = "webp"
) -> tuple[bytes, str] | None:
    """上传字节 →（可落盘字节, 扩展名）；解不出返回 ``None``。

    白名单内（jpg/png/webp）**原字节直通**，与本函数引入前逐字节一致——不重编、不缩放，
    保住「存储层零转码」这条既有承诺。其余容器（HEIC/HEIF/BMP/TIFF/GIF/AVIF）解码后重编：

    - ``prefer="webp"``：无损 WebP。用于头像——它会被 web 直接展示，无损可避免在原本已经
      有损的源（HEIC 本身是有损的）上再叠一代。
    - ``prefer="jpg"``：JPEG q90。用于宠物参考图——其唯一消费者是 omni，而 ``pet_refs``
      拼图时恒重编成 JPEG q85，存 WebP 只是多一道转换；且 ``ref_crop_N.jpg`` 这个硬编码
      文件名牵动 glob / 下标解析等多处逻辑，让盘上后缀与内容脱钩不划算。

    注意 ``prefer`` 只管**重编那一支**：白名单格式恒原字节直通，所以 ``prefer="jpg"`` 下一张
    PNG 参考图仍以 PNG 字节存进 ``ref_crop_N.jpg``。这是刻意保留的既有行为——参考图的读取方
    （``pet_refs``、GET 端点）都按内容而非后缀判型，而直通换来的是「零转码」这条承诺。
    """
    # 验真**先行**：白名单格式也必须能完整解码才允许直通。只查魔数不够——``b"\xff\xd8\xff"``
    # 后面接一堆垃圾（传输截断、拷了一半的照片）同样命中前 3 字节，直通落盘后识别侧解不出、
    # 静默跳过，于是界面显示「3 张参考图」而实际注入 0 张（参考图端点的注释点名要防这个）。
    # 改前三个落盘端点本来就先 cv2.imdecode 验真，所以这次解码不是新增成本；非直通格式复用
    # 这一次解码结果去重编，全程只解一次。
    img = decode_image(data)
    if img is None:
        return None
    ext = sniff_image_ext(data)
    if ext:
        return data, ext
    # 重编前按长边封顶。理由是量级而非美观：一张 24MP 的 iPhone HEIC（过 5MB 上传闸）无损
    # 编码后是 15.6MB、耗时约 9.7s，而这些字节的唯一消费者是「28~48px 的圆头像」（前端把整个
    # blob fetch 下来渲染，且 cache: no-store）与「omni 的 320px 拼图」。改前非白名单格式一律
    # 400，落盘物件被 5MB 上传闸隐含地夹住；本函数放开了格式，就得自己补回这个上界。
    # 封顶只作用于**重编那一支**，白名单直通仍逐字节不动（零转码承诺）。
    img = _cap_long_side(
        img, _AVATAR_MAX_SIDE if prefer == "webp" else _REF_CROP_MAX_SIDE
    )
    h, w = img.shape[:2]
    if prefer == "webp":
        ok, buf = cv2.imencode(".webp", img, [cv2.IMWRITE_WEBP_QUALITY, 101])  # >100 = 无损
        if ok:
            return buf.tobytes(), "webp"
        logger.warning("event=webp_encode_failed h=%s w=%s 退 JPEG", h, w)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        logger.warning("event=jpeg_encode_failed h=%s w=%s", h, w)
        return None
    return buf.tobytes(), "jpg"


def _atomic_write(path: Path, data: bytes) -> None:
    """write-temp-then-rename 原子落盘 + fsync——避免崩溃留"半张图"。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _avatar_dir(root: Path, kind: str) -> Path:
    return Path(root) / "avatars" / kind


def _avatar_file(root: Path, kind: str, subject_id: str, ext: str) -> Path:
    """构造 ``avatars/<kind>/<id>.<ext>`` 并双重防穿越：白名单 id + 规范化后仍须落在该
    目录内（后者是 CodeQL 认可的 path-containment sanitizer：os.path.realpath + commonpath）。"""
    subject_id = _safe_subject_id(subject_id)
    d = _avatar_dir(root, kind)
    p = d / f"{subject_id}.{ext}"
    base = os.path.realpath(d)
    if os.path.commonpath((base, os.path.realpath(p))) != base:
        raise ValueError(f"非法 subject_id: {subject_id!r}")
    return p


def avatar_path(root: Path, kind: str, subject_id: str) -> Path | None:
    """返回 ``<root>/avatars/<kind>/<id>.<ext>`` 中实际存在的那张（无则 None）。

    逐 ext 精确探测（不 glob）；路径经白名单 + containment 校验，杜绝穿越。
    """
    for ext in _EXT_ORDER:
        p = _avatar_file(root, kind, subject_id, ext)
        if p.is_file():
            return p
    return None


def avatar_ext(root: Path, kind: str, subject_id: str) -> str | None:
    p = avatar_path(root, kind, subject_id)
    return p.suffix.lstrip(".").lower() if p else None


def set_avatar(root: Path, kind: str, subject_id: str, data: bytes, ext: str) -> str:
    """原子写头像并清掉该 subject 的其它扩展名旧图；返回规范化后的 ext。"""
    norm = normalize_avatar_ext(ext)
    if not data:
        raise ValueError("头像数据为空")
    _atomic_write(_avatar_file(root, kind, subject_id, norm), data)
    _remove(root, kind, subject_id, keep=norm)
    return norm


def remove_avatar(root: Path, kind: str, subject_id: str) -> None:
    """删掉该 subject 的所有头像文件（恢复默认 / 删除实体级联时用）。"""
    _remove(root, kind, subject_id, keep=None)


def _remove(root: Path, kind: str, subject_id: str, keep: str | None) -> None:
    subject_id = _safe_subject_id(subject_id)
    keep_name = f"{subject_id}.{keep}" if keep else None
    for ext in _EXT_ORDER:
        p = _avatar_file(root, kind, subject_id, ext)
        if keep_name and p.name == keep_name:
            continue
        if p.is_file():
            try:
                p.unlink()
            except OSError:  # noqa: PERF203
                logger.warning("删除旧头像失败: %s", p, exc_info=True)


def list_avatar_exts(root: Path, kind: str) -> dict[str, str]:
    """一次扫描 ``avatars/<kind>/`` 返回 ``{subject_id: ext}``，供列表端点批量取。"""
    d = _avatar_dir(root, kind)
    out: dict[str, str] = {}
    if not d.is_dir():
        return out
    for p in d.iterdir():
        if not p.is_file():
            continue
        ext = p.suffix.lstrip(".").lower()
        if ext in AVATAR_EXTS:
            out[p.stem] = ext
    return out


__all__ = [
    "AVATAR_EXTS",
    "AVATAR_MAX_BYTES",
    "avatar_ext",
    "avatar_path",
    "list_avatar_exts",
    "media_type",
    "normalize_avatar_ext",
    "normalize_for_storage",
    "remove_avatar",
    "set_avatar",
    "sniff_image_ext",
]
