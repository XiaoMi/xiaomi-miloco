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
import re
from datetime import datetime, time, timedelta, tzinfo
from typing import TypeVar

from miloco.database.kv_repo import KVRepo, ScopeConfigKeys
from miloco.middleware.exceptions import ValidationException

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 同时投喂给 miloco 感知的摄像头数量上限（前端展示上限也以此为唯一来源，经
# /api/miot/status 下发）。用户主动 enable 超限直接报错（service.toggle_camera 校验）。
MAX_ENABLED_CAMERAS = 4

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_CAMERA_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]
DEFAULT_CAMERA_SCHEDULE = {
    "enabled": False,
    "weekdays": DEFAULT_CAMERA_WEEKDAYS,
    "windows": [],
}


def _minute_of_day(value: str) -> int:
    match = _TIME_RE.match(value)
    if not match:
        raise ValidationException(
            f"Invalid time {value!r}; expected HH:MM in 24-hour format"
        )
    return int(match.group(1)) * 60 + int(match.group(2))


def _time_from_minute(minute: int) -> time:
    minute %= 24 * 60
    return time(hour=minute // 60, minute=minute % 60)


def _as_day_intervals(start: int, end: int) -> list[tuple[int, int]]:
    if start == end:
        raise ValidationException("Camera schedule windows must not be zero-length")
    if start < end:
        return [(start, end)]
    return [(start, 24 * 60), (0, end)]


def _normalize_weekdays(raw_weekdays: object, *, enabled: bool) -> list[int]:
    if raw_weekdays is None:
        return list(DEFAULT_CAMERA_WEEKDAYS)
    if not isinstance(raw_weekdays, list):
        raise ValidationException("Camera schedule weekdays must be a list")

    weekdays: set[int] = set()
    for raw in raw_weekdays:
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ValidationException("Camera schedule weekdays must be integers")
        if raw < 0 or raw > 6:
            raise ValidationException("Camera schedule weekdays must be between 0 and 6")
        weekdays.add(raw)

    if enabled and not weekdays:
        raise ValidationException("Camera schedule weekdays must not be empty")
    return sorted(weekdays)


def _load_schedule_map(kv_repo: KVRepo) -> dict[str, dict]:
    raw = kv_repo.get(ScopeConfigKeys.CAMERA_SCHEDULES_KEY) or "{}"
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return {
                str(did): schedule
                for did, schedule in value.items()
                if isinstance(schedule, dict)
            }
    except json.JSONDecodeError:
        pass
    logger.warning(
        "KV %s holds non-object-JSON value, treating as empty: %r",
        ScopeConfigKeys.CAMERA_SCHEDULES_KEY,
        raw,
    )
    return {}


def normalize_camera_schedule(schedule: dict | None) -> dict:
    """Validate and normalize a per-camera daily schedule.

    ``enabled=false`` or no windows means unrestricted sensing. Windows are
    half-open daily intervals [start, end), may cross midnight, and must not
    overlap after splitting at midnight. ``weekdays`` uses Python's weekday
    convention (0=Monday ... 6=Sunday); missing weekdays means every day.
    """
    if not schedule:
        return dict(DEFAULT_CAMERA_SCHEDULE)

    enabled = bool(schedule.get("enabled", False))
    weekdays = _normalize_weekdays(schedule.get("weekdays"), enabled=enabled)
    raw_windows = schedule.get("windows") or []
    if not isinstance(raw_windows, list):
        raise ValidationException("Camera schedule windows must be a list")

    windows: list[dict[str, str]] = []
    occupied: list[tuple[int, int]] = []
    for raw in raw_windows:
        if not isinstance(raw, dict):
            raise ValidationException("Camera schedule window must be an object")
        start_raw = raw.get("start")
        end_raw = raw.get("end")
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            raise ValidationException("Camera schedule window requires start/end")

        start = _minute_of_day(start_raw)
        end = _minute_of_day(end_raw)
        for interval in _as_day_intervals(start, end):
            occupied.append(interval)
        windows.append({"start": start_raw, "end": end_raw})

    occupied.sort()
    for prev, curr in zip(occupied, occupied[1:]):
        if curr[0] < prev[1]:
            raise ValidationException("Camera schedule windows must not overlap")

    return {
        "enabled": enabled and bool(windows),
        "weekdays": weekdays,
        "windows": windows,
    }


def camera_schedule_for(kv_repo: KVRepo, did: str) -> dict:
    return normalize_camera_schedule(_load_schedule_map(kv_repo).get(did))


def set_camera_schedule(kv_repo: KVRepo, did: str, schedule: dict) -> tuple[dict, bool]:
    schedules = _load_schedule_map(kv_repo)
    current = normalize_camera_schedule(schedules.get(did))
    if (
        schedule.get("enabled") is False
        and not schedule.get("windows")
        and current["windows"]
    ):
        schedule = {**schedule, "windows": current["windows"]}
        if schedule.get("weekdays") is None:
            schedule = {**schedule, "weekdays": current["weekdays"]}
    normalized = normalize_camera_schedule(schedule)
    if normalized == current:
        return normalized, False

    if normalized == DEFAULT_CAMERA_SCHEDULE:
        schedules.pop(did, None)
    else:
        schedules[did] = normalized
    kv_repo.set(
        ScopeConfigKeys.CAMERA_SCHEDULES_KEY,
        json.dumps(schedules, ensure_ascii=False),
    )
    return normalized, True


def _minute_in_window(minute: int, start: int, end: int) -> bool:
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def camera_schedule_paused(schedule: dict, now: datetime) -> bool:
    normalized = normalize_camera_schedule(schedule)
    if not normalized["enabled"]:
        return False
    if now.weekday() not in normalized["weekdays"]:
        return True
    minute = now.hour * 60 + now.minute
    for window in normalized["windows"]:
        if _minute_in_window(
            minute,
            _minute_of_day(window["start"]),
            _minute_of_day(window["end"]),
        ):
            return False
    return True


def next_camera_schedule_change_at(
    schedule: dict,
    now: datetime,
    tz: tzinfo,
) -> datetime | None:
    """Return the next schedule boundary that changes paused state after ``now``."""
    normalized = normalize_camera_schedule(schedule)
    if not normalized["enabled"]:
        return None

    local_now = now.astimezone(tz)
    current_paused = camera_schedule_paused(normalized, local_now)
    start_day = local_now.date()
    candidates: set[datetime] = set()
    for offset in range(0, 9):
        day = start_day + timedelta(days=offset)
        candidates.add(datetime.combine(day, time.min, tzinfo=tz))
        for window in normalized["windows"]:
            for key in ("start", "end"):
                minute = _minute_of_day(window[key])
                candidates.add(
                    datetime.combine(day, _time_from_minute(minute), tzinfo=tz)
                )

    for candidate in sorted(c for c in candidates if c > local_now):
        if camera_schedule_paused(normalized, candidate) != current_paused:
            return candidate
    return None


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
