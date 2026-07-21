# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""home_profile 持久化（repo 角色）——JSON 文件读写 + 文件锁 + 原子落盘。

数据落 ``$MILOCO_HOME/home-profile/{profile.json, candidates.json}``，
canonical 渲染产物落同目录 ``profile.md``。

并发（R2）：所有「读-改-写」在跨进程文件锁（fcntl.flock）内串行化，
兼容 CLI / cron / 后续独立进程多写者。落盘走 write-temp-then-rename，
保证无锁读者（injection/omni）永远读到完整旧版或完整新版。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from miloco.home_profile.schema import CandidatesIndex, ProfileIndex
from miloco.utils.paths import miloco_home


def home_profile_dir() -> Path:
    return miloco_home() / "home-profile"


def profile_json_path() -> Path:
    return home_profile_dir() / "profile.json"


def candidates_json_path() -> Path:
    return home_profile_dir() / "candidates.json"


def profile_md_path() -> Path:
    return home_profile_dir() / "profile.md"


def task_suggestions_path() -> Path:
    """习惯建议候选库（miloco-cli habit 维护），与本目录其余文件同级、互不干扰。"""
    return home_profile_dir() / "task-suggestions.json"


def load_task_created_item_ids() -> set[str]:
    """已建成任务（status=created）的源家庭档案条目 id 集合。

    只读 miloco-cli 维护的 task-suggestions.json（temp→rename 原子落盘，无锁读安全）；
    文件缺失/损坏/字段缺失一律回落空集，渲染照常。
    """
    path = task_suggestions_path()
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return set()
    return {
        e["item_id"]
        for e in raw.get("entries", [])
        if e.get("status") == "created" and e.get("item_id")
    }


def _lock_path() -> Path:
    return home_profile_dir() / ".lock"


@contextlib.contextmanager
def file_lock() -> Iterator[None]:
    """跨进程独占锁。所有 write/commit 的「读-改-写」须在此锁内。"""
    home_profile_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_profile() -> ProfileIndex:
    path = profile_json_path()
    if not path.exists():
        return ProfileIndex()
    return ProfileIndex.model_validate_json(path.read_text("utf-8"))


def load_candidates() -> CandidatesIndex:
    path = candidates_json_path()
    if not path.exists():
        return CandidatesIndex()
    return CandidatesIndex.model_validate_json(path.read_text("utf-8"))


def save_profile(data: ProfileIndex) -> None:
    _atomic_write_text(
        profile_json_path(),
        json.dumps(data.model_dump(), ensure_ascii=False, indent=2),
    )


def save_candidates(data: CandidatesIndex) -> None:
    _atomic_write_text(
        candidates_json_path(),
        json.dumps(data.model_dump(), ensure_ascii=False, indent=2),
    )


def save_rendered_md(text: str) -> None:
    _atomic_write_text(profile_md_path(), text)


def read_rendered_md() -> str:
    path = profile_md_path()
    if not path.exists():
        return ""
    return path.read_text("utf-8")
