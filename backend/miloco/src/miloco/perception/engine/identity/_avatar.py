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
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

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


def avatar_path(root: Path, kind: str, subject_id: str) -> Path | None:
    """返回 ``<root>/avatars/<kind>/<id>.<ext>`` 中实际存在的那张（无则 None）。

    逐 ext 精确探测（不 glob）：subject_id 已由上层白名单校验，此处不引入 glob 语义。
    """
    d = _avatar_dir(root, kind)
    for ext in _EXT_ORDER:
        p = d / f"{subject_id}.{ext}"
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
    _atomic_write(_avatar_dir(root, kind) / f"{subject_id}.{norm}", data)
    _remove(root, kind, subject_id, keep=norm)
    return norm


def remove_avatar(root: Path, kind: str, subject_id: str) -> None:
    """删掉该 subject 的所有头像文件（恢复默认 / 删除实体级联时用）。"""
    _remove(root, kind, subject_id, keep=None)


def _remove(root: Path, kind: str, subject_id: str, keep: str | None) -> None:
    d = _avatar_dir(root, kind)
    keep_name = f"{subject_id}.{keep}" if keep else None
    for ext in _EXT_ORDER:
        p = d / f"{subject_id}.{ext}"
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
    "avatar_ext",
    "avatar_path",
    "list_avatar_exts",
    "media_type",
    "normalize_avatar_ext",
    "remove_avatar",
    "set_avatar",
]
