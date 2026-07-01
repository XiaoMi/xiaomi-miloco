# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""宠物花名册（PetLibrary）—— 宠物作为「非人家庭成员」的轻量身份壳存取。

落点：``<identity_lib_root>/pets/<pet_id>/{meta.json, avatar.<ext>}``，与人类样本
目录 ``<root>/persons/`` 并列（存储整齐）。

**红线**：本模块只做文件存取，**不接 IdentityEngine / ReID / 身份状态机 / person
表**。``IdentityLibrary`` 只遍历 ``root/persons``、从不遍历 ``root`` 本身，故
``pets/`` 不会被它的扫描 / 补 ReID 向量逻辑误触。

花名册只存「身份壳」（名 / 物种 / 头像）；宠物的外观描述等不在此——它们沉淀进
home_profile 的 ``member_persona.content``。
"""

from __future__ import annotations

import functools
import logging
import os
import secrets
import shutil
import tempfile
import threading
from pathlib import Path

from pydantic import BaseModel

from miloco.config.settings import register_reset_hook
from miloco.perception.engine.identity.config_loader import resolve_library_root
from miloco.utils.time_utils import now_iso

logger = logging.getLogger(__name__)

# 头像允许的图片扩展名（小写、无点）
_AVATAR_EXTS = frozenset({"jpg", "jpeg", "png", "webp"})


class Pet(BaseModel):
    """宠物花名册条目（身份壳）。"""

    id: str
    name: str
    species: str
    avatar_ext: str | None = None
    created_at: str
    updated_at: str


class PetNameConflict(ValueError):
    """宠物名重复（service / router 层据此映射为 409）。"""


def _gen_pet_id() -> str:
    return f"pet_{secrets.token_hex(6)}"


def _normalize_avatar_ext(ext: str) -> str:
    e = (ext or "").lower().lstrip(".")
    if e not in _AVATAR_EXTS:
        raise ValueError(
            f"不支持的头像格式: {ext!r}（允许 {sorted(_AVATAR_EXTS)}）"
        )
    return e


def _atomic_write(path: Path, data: bytes) -> None:
    """write-temp-then-rename 原子落盘（同 home_profile/store 的范式）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class PetLibrary:
    """宠物花名册的磁盘读写封装（进程内共享单例，见 ``get_pet_library``）。"""

    def __init__(self, root_dir: Path | str | None = None) -> None:
        root = Path(root_dir) if root_dir is not None else resolve_library_root()
        self.root = root
        self.pets_dir = self.root / "pets"
        # create/update/delete/set_avatar 的写操作经后端 API 汇聚到单进程，
        # 用进程内 RLock 串行化「读-改-写」即可避免 TOCTOU 重名 / 竞态。
        self._lock = threading.RLock()

    # ── 路径 ──────────────────────────────────────────────────────────────

    def _pet_dir(self, pet_id: str) -> Path:
        return self.pets_dir / pet_id

    def _meta_path(self, pet_id: str) -> Path:
        return self._pet_dir(pet_id) / "meta.json"

    # ── 读 ────────────────────────────────────────────────────────────────

    def get(self, pet_id: str) -> Pet | None:
        path = self._meta_path(pet_id)
        if not path.is_file():
            return None
        try:
            return Pet.model_validate_json(path.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            logger.warning("宠物 meta 解析失败: %s", path, exc_info=True)
            return None

    def list(self) -> list[Pet]:
        if not self.pets_dir.is_dir():
            return []
        out: list[Pet] = []
        for d in sorted(self.pets_dir.iterdir()):
            if not d.is_dir():
                continue
            pet = self.get(d.name)
            if pet is not None:
                out.append(pet)
        return out

    def get_by_name(self, name: str) -> Pet | None:
        for p in self.list():
            if p.name == name:
                return p
        return None

    # ── 写 ────────────────────────────────────────────────────────────────

    def create(self, name: str, species: str) -> Pet:
        name = (name or "").strip()
        if not name:
            raise ValueError("宠物名不能为空")
        with self._lock:
            if self.get_by_name(name) is not None:
                raise PetNameConflict(f"宠物名已存在: {name}")
            pet_id = _gen_pet_id()
            while self._pet_dir(pet_id).exists():  # 极低概率撞名，重抽
                pet_id = _gen_pet_id()
            now = now_iso()
            pet = Pet(
                id=pet_id,
                name=name,
                species=species,
                created_at=now,
                updated_at=now,
            )
            self._write_meta(pet)
            logger.info("宠物已创建: id=%s name=%s", pet_id, name)
            return pet

    def update(
        self,
        pet_id: str,
        *,
        name: str | None = None,
        species: str | None = None,
    ) -> Pet:
        with self._lock:
            pet = self.get(pet_id)
            if pet is None:
                raise KeyError(pet_id)
            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError("宠物名不能为空")
                if name != pet.name and self.get_by_name(name) is not None:
                    raise PetNameConflict(f"宠物名已存在: {name}")
                pet.name = name
            if species is not None:
                pet.species = species
            pet.updated_at = now_iso()
            self._write_meta(pet)
            return pet

    def delete(self, pet_id: str) -> bool:
        with self._lock:
            d = self._pet_dir(pet_id)
            if not d.exists():
                return False
            shutil.rmtree(d, ignore_errors=True)
            logger.info("宠物已删除: id=%s", pet_id)
            return True

    def set_avatar(self, pet_id: str, data: bytes, ext: str) -> Pet:
        norm_ext = _normalize_avatar_ext(ext)
        if not data:
            raise ValueError("头像数据为空")
        with self._lock:
            pet = self.get(pet_id)
            if pet is None:
                raise KeyError(pet_id)
            # 顺序保证「meta 始终指向一个存在的头像文件」：先落新图（原子），
            # 再让 meta 指向它，最后清理其它扩展名的旧图——任一步中断都不会
            # 出现「meta 指向已删文件」的窗口。
            _atomic_write(self._pet_dir(pet_id) / f"avatar.{norm_ext}", data)
            pet.avatar_ext = norm_ext
            pet.updated_at = now_iso()
            self._write_meta(pet)
            self._remove_avatar_files(pet_id, keep=norm_ext)
            return pet

    def avatar_path(self, pet_id: str) -> Path | None:
        pet = self.get(pet_id)
        if pet is None or not pet.avatar_ext:
            return None
        p = self._pet_dir(pet_id) / f"avatar.{pet.avatar_ext}"
        return p if p.is_file() else None

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _write_meta(self, pet: Pet) -> None:
        _atomic_write(
            self._meta_path(pet.id),
            pet.model_dump_json(indent=2).encode("utf-8"),
        )

    def _remove_avatar_files(self, pet_id: str, keep: str | None = None) -> None:
        d = self._pet_dir(pet_id)
        if not d.is_dir():
            return
        keep_name = f"avatar.{keep}" if keep else None
        for f in d.glob("avatar.*"):
            if keep_name and f.name == keep_name:
                continue
            try:
                f.unlink()
            except OSError:  # noqa: PERF203
                logger.warning("删除旧头像失败: %s", f, exc_info=True)


@functools.lru_cache(maxsize=1)
def get_pet_library() -> PetLibrary:
    """进程内共享的 PetLibrary 单例（root 取 ``resolve_library_root()`` / pets）。"""
    return PetLibrary()


# settings 重载（测试 monkeypatch / bootstrap 改 library_root）时清缓存，
# 与项目「派生缓存注册 reset hook」惯例一致（见 config/settings.py 注释）。
register_reset_hook("pet_library.get_pet_library", get_pet_library.cache_clear)


__all__ = [
    "Pet",
    "PetLibrary",
    "PetNameConflict",
    "get_pet_library",
]
