# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""miloco scope 过滤工具：家庭接入范围 + 相机接入范围。

数据落在 SQLite ``kv`` 表的 ``HOME_WHITE_LIST_KEY``（启用的家庭集合）和
``CAMERA_BLACK_LIST_KEY``（停用的相机集合），JSON array 字符串，由
:class:`KVRepo` 缓存。
"""

from __future__ import annotations

import json
import logging
from typing import TypeVar

from miloco.database.kv_repo import KVRepo, ScopeConfigKeys

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 同时投喂给 miloco 感知的摄像头数量上限（前端展示上限也以此为唯一来源，经
# /api/miot/status 下发）。用户主动 enable 超限直接报错（service.toggle_camera 校验）。
MAX_ENABLED_CAMERAS = 4


def _load_list(kv_repo: KVRepo, key: str) -> list[str]:
    raw = kv_repo.get(key) or "[]"
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return [str(item) for item in value]
    except json.JSONDecodeError:
        pass
    logger.warning("KV %s holds non-list-JSON value, treating as empty: %r", key, raw)
    return []


def _toggle_member(
    kv_repo: KVRepo, key: str, item: str, *, include: bool
) -> tuple[list[str], bool]:
    """Ensure ``item`` is (``include=True``) or isn't (``include=False``) in the
    JSON-list stored at ``key``. Returns ``(new_list, changed)``; no-ops skip
    the kv write so callers can also skip downstream side-effects.

    并发约束：read-modify-write，依赖 single-writer 假设。backend 单进程使用 OK；
    多 writer 时需要换 atomic update 接口。
    """
    current = _load_list(kv_repo, key)
    if include:
        new = current if item in current else current + [item]
    else:
        new = [x for x in current if x != item]
    if new == current:
        return current, False
    kv_repo.set(key, json.dumps(new, ensure_ascii=False))
    return new, True


def allowed_home_ids(kv_repo: KVRepo) -> set[str]:
    """已启用的家庭 id 集合；空集合表示未启用任何家庭。"""
    return set(_load_list(kv_repo, ScopeConfigKeys.HOME_WHITE_LIST_KEY))


def denied_camera_dids(kv_repo: KVRepo) -> set[str]:
    """已停用的相机 did 集合；空表示全部启用。"""
    return set(_load_list(kv_repo, ScopeConfigKeys.CAMERA_BLACK_LIST_KEY))


def is_home_allowed(kv_repo: KVRepo, home_id: str | None) -> bool:
    """单条 ``home_id`` 是否被允许。空集合表示未启用任何家庭。"""
    allow = allowed_home_ids(kv_repo)
    return home_id is not None and home_id in allow


def select_active_camera_dids(
    kv_repo: KVRepo,
    cameras: dict[str, T],
    *,
    online_only: bool = True,
    require_lan: bool = True,
    cap: bool = True,
) -> list[str]:
    """决定「哪些相机该投喂/拉流」的**单一口径**——感知投喂(camera_adapter)与 native
    会话建销(refresh_cameras)共用此函数，避免两套判定漂移。

    过滤：在启用家庭内 + 未拉黑 +（``online_only`` 时）在线。``require_lan=True`` 看
    ``online and lan_online``；``False`` 只看云端 ``online``（放过 lan_online 陈旧的卡死态
    相机）。``cap=True`` 时按 did 升序确定性截断到 ``MAX_ENABLED_CAMERAS``——投喂/拉流
    上限的唯一兜底，与 ``service.toggle_camera`` 的主动 enable 校验互补；不写 KV、不碰
    黑名单。``cap=False`` 用于「列全集」语义（如 rule target 校验）。

    返回 did 列表：未截断为输入顺序，截断为 did 升序前 N。``cameras`` 的 value 需带
    ``home_id`` / ``online`` / ``lan_online`` 属性。
    """
    denied = denied_camera_dids(kv_repo)
    result: list[str] = []
    for did, info in cameras.items():
        if did in denied:
            continue
        if not is_home_allowed(kv_repo, getattr(info, "home_id", None)):
            continue
        online = bool(getattr(info, "online", False))
        lan = bool(getattr(info, "lan_online", False))
        connectable = (online and lan) if require_lan else online
        if online_only and not connectable:
            continue
        result.append(did)
    if not cap or len(result) <= MAX_ENABLED_CAMERAS:
        return result
    # 超限：按 did 升序确定性截断（同一账号每轮选同一批）。
    return sorted(result)[:MAX_ENABLED_CAMERAS]


def filter_by_home(kv_repo: KVRepo, items: dict[str, T]) -> dict[str, T]:
    """按 ``home_id`` 过滤 dict（value 需带 ``home_id`` 属性）。空启用集表示未选择家庭。"""
    allow = allowed_home_ids(kv_repo)
    if not allow:
        return {}
    return {k: v for k, v in items.items() if getattr(v, "home_id", None) in allow}


def set_home_in_use(
    kv_repo: KVRepo, home_id: str, in_use: bool
) -> tuple[list[str], bool]:
    """切换单个家庭的启用状态。``in_use=True`` 加入启用集；``False`` 移出。"""
    return _toggle_member(
        kv_repo, ScopeConfigKeys.HOME_WHITE_LIST_KEY, home_id, include=in_use
    )


def set_camera_in_use(
    kv_repo: KVRepo, did: str, in_use: bool
) -> tuple[list[str], bool]:
    """切换单个相机的启用状态。``in_use=False`` 即加入停用集。"""
    return _toggle_member(
        kv_repo, ScopeConfigKeys.CAMERA_BLACK_LIST_KEY, did, include=not in_use
    )


def set_homes_in_use(
    kv_repo: KVRepo, home_ids: list[str], in_use: bool
) -> tuple[list[str], bool]:
    """批量切换家庭启用状态。去重后一次性写入 KV。"""
    return _toggle_members(
        kv_repo, ScopeConfigKeys.HOME_WHITE_LIST_KEY, home_ids, include=in_use
    )


def set_cameras_in_use(
    kv_repo: KVRepo, dids: list[str], in_use: bool
) -> tuple[list[str], bool]:
    """批量切换相机启用状态。去重后一次性写入 KV。"""
    return _toggle_members(
        kv_repo, ScopeConfigKeys.CAMERA_BLACK_LIST_KEY, dids, include=not in_use
    )


def _toggle_members(
    kv_repo: KVRepo, key: str, items: list[str], *, include: bool
) -> tuple[list[str], bool]:
    """批量版本的 _toggle_member；一次性写入，返回 ``(new_list, changed)``。"""
    current = _load_list(kv_repo, key)
    # 去重，保持输入顺序
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)

    if include:
        new = list(current)
        for item in ordered:
            if item not in new:
                new.append(item)
    else:
        to_remove = set(ordered)
        new = [x for x in current if x not in to_remove]

    if new == current:
        return current, False
    kv_repo.set(key, json.dumps(new, ensure_ascii=False))
    return new, True
