# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""宠物花名册（PetLibrary）—— 宠物作为「非人家庭成员」的轻量身份壳存取。

落点：``<identity_lib_root>/pets/<pet_id>/{meta.json, ref_crop_*.jpg}``，与人类样本
目录 ``<root>/persons/`` 并列（存储整齐）。展示头像不在此——统一落
``<root>/avatars/pets/<pet_id>.<ext>``（见 ``_avatar``，展示层与识别数据分离）。

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
from miloco.perception.engine.identity import _avatar
from miloco.perception.engine.identity.config_loader import resolve_library_root
from miloco.utils.time_utils import now_iso

logger = logging.getLogger(__name__)

# 参考 crop（③ 识别的多姿态参照图）存储上限
_MAX_REF_CROPS = 3


class Pet(BaseModel):
    """宠物花名册条目（身份壳）。"""

    id: str
    name: str
    species: str
    avatar_ext: str | None = None
    # 参考 crop（喂 ③ 识别的多姿态参照图）：文件名锁死 ref_crop_{0..N-1}.jpg。
    # count 为读取权威（崩溃残留的高位 stale 帧被切掉、不喂识别，评审 §H A2）；
    # scores 为每张的【绝对】质量分（conf×sharpness×area_ratio），与 crop 对齐，
    # 供 append 跨批次留 top-3（决策5(b)：绝对分跨批可比，池内归一化分不可比）。
    reference_crop_count: int = 0
    reference_crop_scores: list[float] = []
    created_at: str
    updated_at: str


class PetNameConflict(ValueError):
    """宠物名重复（service / router 层据此映射为 409）。"""


def _gen_pet_id() -> str:
    return f"pet_{secrets.token_hex(6)}"


def _atomic_write(path: Path, data: bytes) -> None:
    """write-temp-then-rename 原子落盘 + fsync（同 home_profile/store 的范式）。

    fsync 保证 rename 前数据已落盘——否则崩溃可能留下"半张图"，喂给识别会污染
    （参考图/头像/meta 皆经此，评审 C11）。
    """
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


def _ref_index(path: Path) -> int | None:
    """从 ref_crop_<n>.jpg 取数字下标；非法名返回 None（用于数值排序 / 清理）。"""
    try:
        return int(path.stem.split("_")[-1])
    except (ValueError, IndexError):
        return None


def _pair_crops(
    crops: list[bytes], scores: list[float] | None
) -> list[tuple[bytes, float]]:
    """把 crop 字节与绝对质量分配对；scores 缺失/短缺补 0.0。"""
    scores = scores or []
    return [(c, float(scores[i]) if i < len(scores) else 0.0) for i, c in enumerate(crops)]


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
            existed = d.exists()
            if existed:
                shutil.rmtree(d, ignore_errors=True)
            # 头像在 <root>/avatars/pets/ 内（不在 pet 目录下），须显式清、否则残留孤儿。
            _avatar.remove_avatar(self.root, "pets", pet_id)
            if existed:
                logger.info("宠物已删除: id=%s", pet_id)
            return existed

    def set_avatar(self, pet_id: str, data: bytes, ext: str) -> Pet:
        # 头像落点 <root>/avatars/pets/<id>.<ext>（展示层，与识别数据分离）。
        # 顺序保证「meta.avatar_ext 始终指向一个存在的头像文件」：先原子落新图 +
        # 清旧扩展名，再让 meta 指向它——中断也不会出现「meta 指向已删文件」的窗口。
        with self._lock:
            pet = self.get(pet_id)
            if pet is None:
                raise KeyError(pet_id)
            norm_ext = _avatar.set_avatar(self.root, "pets", pet_id, data, ext)
            pet.avatar_ext = norm_ext
            pet.updated_at = now_iso()
            self._write_meta(pet)
            return pet

    def avatar_path(self, pet_id: str) -> Path | None:
        return _avatar.avatar_path(self.root, "pets", pet_id)

    # ── 参考 crop（③ 多姿态参照图）────────────────────────────────────────

    def reference_crop_paths(self, pet_id: str) -> list[Path]:
        """按序返回参考 crop 路径（供 P2 识别端读）。

        **count 权威**：仅取前 `reference_crop_count` 张——崩溃残留的高位 stale 帧
        被切掉、不喂识别（评审 §H A2）；再 `is_file()` 过滤自愈 meta↔实体不齐。
        """
        pet = self.get(pet_id)
        if pet is None:
            return []
        d = self._pet_dir(pet_id)
        if not d.is_dir():
            return []
        ordered = sorted(
            (p for p in d.glob("ref_crop_*.jpg") if _ref_index(p) is not None),
            key=lambda p: _ref_index(p) or 0,
        )
        return [p for p in ordered[: pet.reference_crop_count] if p.is_file()]

    def reference_crop_scores(self, pet_id: str) -> list[float]:
        """与 `reference_crop_paths` 对齐的绝对质量分（越界补 0）。"""
        pet = self.get(pet_id)
        if pet is None:
            return []
        n = len(self.reference_crop_paths(pet_id))
        sc = list(pet.reference_crop_scores)
        return [(sc[i] if i < len(sc) else 0.0) for i in range(n)]

    def set_reference_crops(
        self, pet_id: str, crops: list[bytes], scores: list[float] | None = None
    ) -> Pet:
        """整组替换（注册时一次性写；上限 3，按给定顺序取前 3）。"""
        return self._rewrite_ref_crops(pet_id, _pair_crops(crops, scores)[:_MAX_REF_CROPS])

    def append_reference_crops(
        self, pet_id: str, crops: list[bytes], scores: list[float] | None = None
    ) -> Pet:
        """追加（决策5(b)）：现有 + 新，按【绝对质量分】降序留 top-3（非 FIFO）。"""
        with self._lock:
            merged = self._read_ref_items(pet_id) + _pair_crops(crops, scores)
            merged.sort(key=lambda it: it[1], reverse=True)
            return self._rewrite_ref_crops(pet_id, merged[:_MAX_REF_CROPS])

    def _read_ref_items(self, pet_id: str) -> list[tuple[bytes, float]]:
        paths = self.reference_crop_paths(pet_id)
        scores = self.reference_crop_scores(pet_id)
        return [(p.read_bytes(), scores[i]) for i, p in enumerate(paths)]

    def _rewrite_ref_crops(
        self, pet_id: str, items: list[tuple[bytes, float]]
    ) -> Pet:
        """连号重写 ref_crop_0..k-1 + 清多余旧槽 + 最后写 meta（count 权威）。

        set/append 都收敛到此。写序（评审 §H A2，崩溃安全）：① 原子写全部 crop
        （每张 fsync）② 删索引 ≥k 的旧槽 ③ **最后**写 meta。全程持锁。
        """
        with self._lock:
            pet = self.get(pet_id)
            if pet is None:
                raise KeyError(pet_id)
            items = items[:_MAX_REF_CROPS]
            if any(not b for b, _ in items):
                raise ValueError("参考 crop 数据为空")
            d = self._pet_dir(pet_id)
            for i, (data, _s) in enumerate(items):
                _atomic_write(d / f"ref_crop_{i}.jpg", data)
            for p in d.glob("ref_crop_*.jpg"):  # 清多余旧槽（别误伤 avatar/meta）
                idx = _ref_index(p)
                if idx is None or idx >= len(items):
                    try:
                        p.unlink()
                    except OSError:  # noqa: PERF203
                        logger.warning("删除多余参考 crop 失败: %s", p, exc_info=True)
            pet.reference_crop_count = len(items)
            pet.reference_crop_scores = [float(s) for _, s in items]
            pet.updated_at = now_iso()
            self._write_meta(pet)
            return pet

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _write_meta(self, pet: Pet) -> None:
        _atomic_write(
            self._meta_path(pet.id),
            pet.model_dump_json(indent=2).encode("utf-8"),
        )


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
