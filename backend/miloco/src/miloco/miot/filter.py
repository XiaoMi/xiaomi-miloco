# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""miloco scope 过滤工具：家庭接入范围 + 相机接入范围。

数据落在 SQLite ``kv`` 表的 ``HOME_WHITE_LIST_KEY``（启用的家庭集合）、
``CAMERA_BLACK_LIST_KEY``（停用的相机集合）和 ``CAMERA_VOICE_ALLOW_LIST_KEY``
（**开启**拾音的相机集合，opt-in / 默认关语义），JSON array 字符串，由 :class:`KVRepo` 缓存。

另有 ``CAMERA_PROMPT_MAP_KEY``（每摄像头自定义「感知须知」prompt，did→文本）——
唯一的 map 语义 key（JSON object，非集合），供逐设备注入 omni 场景指导。
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

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")

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


def load_schedule_map(kv_repo: KVRepo) -> dict[str, dict]:
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
        pass  # fall through to warning + empty default below
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
        return {
            "enabled": False,
            "weekdays": list(DEFAULT_CAMERA_WEEKDAYS),
            "windows": [],
        }

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


def camera_schedule_for(
    kv_repo: KVRepo,
    did: str,
    *,
    schedules: dict[str, dict] | None = None,
) -> dict:
    if schedules is None:
        schedules = load_schedule_map(kv_repo)
    return normalize_camera_schedule(schedules.get(did))


def set_camera_schedule(kv_repo: KVRepo, did: str, schedule: dict) -> tuple[dict, bool]:
    schedules = load_schedule_map(kv_repo)
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


def camera_schedule_paused(schedule: dict, now: datetime) -> bool:
    normalized = normalize_camera_schedule(schedule)
    if not normalized["enabled"]:
        return False

    weekdays = set(normalized["weekdays"])
    minute = now.hour * 60 + now.minute
    today = now.weekday()
    yesterday = (today - 1) % 7

    for window in normalized["windows"]:
        start = _minute_of_day(window["start"])
        end = _minute_of_day(window["end"])
        if start < end:
            if today in weekdays and start <= minute < end:
                return False
        else:
            if today in weekdays and minute >= start:
                return False
            if yesterday in weekdays and minute < end:
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


# 每摄像头「感知须知」自定义 prompt 长度上限（字符数）。filter 层截断作为纵深防御，
# service/schema 层已有校验。
MAX_CAMERA_PROMPT_LEN = 500


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


def voice_allowed_camera_dids(kv_repo: KVRepo) -> set[str]:
    """已**开启**「拾音」的相机 did 集合（allow-list / opt-in）；空表示全部关闭（**默认关闭**）。

    与 ``denied_camera_dids`` 正交：相机可照常投喂**视频**感知，但只有本集内相机的
    **音频才被处理**（转写 / 语音派生 suggestion / 上云 token）；不在集内的相机——
    引擎入口整批剥离音频，dispatch/落库闸门作第二道防线。各执法点实时读取本集，
    改开关即时生效、不重启感知引擎。读 KV 失败时按空集处理（fail-closed）。
    """
    return set(_load_list(kv_repo, ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY))


def is_home_allowed(kv_repo: KVRepo, home_id: str | None) -> bool:
    """单条 ``home_id`` 是否被允许。空集合表示未启用任何家庭。"""
    allow = allowed_home_ids(kv_repo)
    return home_id is not None and home_id in allow


def physical_camera_did(did: str) -> str:
    """合成通道 did → 物理 did（``{did}:ch{n}`` → ``did``）；裸 did 原样返回。

    会话 / manager / 启停 / 拾音白名单都按整台相机（物理 did）走，合成 did 先归一。
    """
    return did.rsplit(":ch", 1)[0] if ":ch" in did else did


def synthetic_camera_did(physical_did: str, channel: int, channel_count: int) -> str:
    """多通道相机每路的合成 did ``{did}:ch{n}``；单通道（``channel_count<=1``）返回裸 did。

    合成 did 是全拆后相机感知的一等身份（每路一台）；单摄裸 did 零回归。
    """
    return f"{physical_did}:ch{channel}" if channel_count > 1 else physical_did


def is_camera_channel_denied(
    denied: set[str], physical_did: str, channel: int, channel_count: int
) -> bool:
    """该「相机某路」是否被拉黑（黑名单 per-channel 读，OR 容错）。

    多通道：合成 did ``{did}:ch{n}`` 在黑名单 **或** 裸物理 did 在黑名单（后者=整台全关，
    兼容全拆上线前按物理 did 写的旧条目）即视为该路停用。单通道：就看裸 did 本身。
    """
    if channel_count > 1:
        return f"{physical_did}:ch{channel}" in denied or physical_did in denied
    return physical_did in denied


def select_active_camera_dids(
    kv_repo: KVRepo,
    cameras: dict[str, T],
    *,
    online_only: bool = True,
    require_lan: bool = True,
    cap: bool = True,
    apply_schedule: bool = True,
    now: datetime | None = None,
    awake_map: dict[str, dict[int, bool | None]] | None = None,
) -> list[str]:
    """决定「哪些相机通道该投喂/拉流」的**单一口径**——感知投喂(camera_adapter)与 native
    会话建销(refresh_cameras)共用此函数，避免两套判定漂移。

    **全拆后返回合成 did（通道粒度）**：单摄裸 did、多摄每路 ``{did}:ch{n}``。过滤按
    「相机级」（家庭 / 在线）+「通道级」（黑名单 per-channel / 镜头 per-lens）两层：
    在启用家庭内、（``online_only`` 时）在线（``require_lan=True`` 看 ``online and
    lan_online``、``False`` 只看云端 ``online``）；再对每路——未被拉黑
    （``is_camera_channel_denied``）、该路镜头未关（``awake_map[did][channel]`` 明确
    ``False`` 才排除；``None``/缺失/``True`` 放行）。

    **上限与定时的顺序（重要）**：先按「未叠加定时」的活跃集做 ``cap`` 截断，再在
    ``apply_schedule=True`` 时从截断结果里扣掉暂停相机。定时暂停**占名额、不释放**，
    从而保证投喂集 ⊆ manager 建销集（``refresh_cameras`` 用 ``apply_schedule=False``），
    不会出现「投喂选中一台从未建 native 会话的相机」。

    调用方语义：
    - ``camera_adapter``（感知投喂）：默认 ``apply_schedule=True``，暂停窗口内不投喂。
    - ``refresh_cameras``（native 会话建销，服务 watch/live）：``apply_schedule=False``，
      定时暂停不拆 manager；仍应用 awake_map 镜头门。
    - ``get_devices``（列全集/rule 校验）：``cap=False, apply_schedule=False``。

    ``cap=True`` 时上限按**启用通道数**（= 截断前合成 did 数）确定性截断到
    ``MAX_ENABLED_CAMERAS``——每路独立占一个名额，超限按合成 did 升序 ``[:MAX]``，**允许在
    一台多摄相机中间切开**。``cap=False`` 用于「列全集」语义（如 rule target 校验 /
    ``capped_out`` 判定）。会话/manager 生命周期由调用方（refresh_cameras）对返回集取
    物理 did 收敛：任一路在→会话在、两路都不在→拆。

    ``cameras`` 的 value 需带 ``home_id`` / ``online`` / ``lan_online``（及可选
    ``channel_count``）属性。``awake_map`` 是 per-lens 的 ``{did: {channel: bool|None}}``。
    定时规则按**物理 did** 存；``apply_schedule=True`` 时整台暂停则该物理机所有通道都不入选。
    """
    from miloco.utils.time_utils import deploy_timezone

    denied = denied_camera_dids(kv_repo)
    result: list[str] = []
    for did, info in cameras.items():
        if not is_home_allowed(kv_repo, getattr(info, "home_id", None)):
            continue
        online = bool(getattr(info, "online", False))
        lan = bool(getattr(info, "lan_online", False))
        connectable = (online and lan) if require_lan else online
        if online_only and not connectable:
            continue
        channel_count = getattr(info, "channel_count", None) or 1
        lens_awake = (awake_map or {}).get(did) or {}
        for ch in range(channel_count):
            if is_camera_channel_denied(denied, did, ch, channel_count):
                continue
            # 该路镜头关（per-lens awake == False）排除；None/缺失/True 放行(未知不误杀)。
            if lens_awake.get(ch) is False:
                continue
            result.append(synthetic_camera_did(did, ch, channel_count))
    if cap and len(result) > MAX_ENABLED_CAMERAS:
        # 超限：按合成 did 升序确定性截断（同一账号每轮选同一批），每路独占一个名额、可拦半台。
        # 必须在定时门控之前截断：暂停相机仍占名额，投喂集才是 manager 池的子集。
        result = sorted(result)[:MAX_ENABLED_CAMERAS]
    if not apply_schedule:
        return result

    schedules = load_schedule_map(kv_repo)
    now = now or datetime.now(deploy_timezone())
    paused_physical: dict[str, bool] = {}
    feeding: list[str] = []
    for syn in result:
        physical = physical_camera_did(syn)
        if physical not in paused_physical:
            paused_physical[physical] = camera_schedule_paused(
                camera_schedule_for(kv_repo, physical, schedules=schedules), now
            )
        if not paused_physical[physical]:
            feeding.append(syn)
    return feeding


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


def denied_channels_of(
    denied: set[str], physical_did: str, channel_count: int
) -> set[int]:
    """当前该物理相机被拉黑的通道集（黑名单读，裸 did 展开）。

    裸物理 did 在黑名单 = 整台全关 → 展开成全部通道；否则按各路合成 did 是否在黑名单收集。
    与 :func:`is_camera_channel_denied` 同口径（OR 容错）。
    """
    if physical_did in denied:
        return set(range(channel_count))
    return {
        ch
        for ch in range(channel_count)
        if synthetic_camera_did(physical_did, ch, channel_count) in denied
    }


def set_cameras_channels_in_use(
    kv_repo: KVRepo,
    updates: dict[str, dict[int, bool]],
    channel_counts: dict[str, int],
) -> tuple[list[str], bool]:
    """按**物理相机整台重算 + 覆盖**写黑名单（D3 写路径）。

    ``updates``：``{physical_did: {channel: in_use}}``。对每个受影响物理 did：读当前禁用通道集
    （``denied_channels_of``，裸 did 展开）→ 应用本次 ``{channel: in_use}``（True 从禁用集移除、
    False 加入）→ **删掉该 did 的所有旧条目（裸 did + 各 ``:chN``），写回规范 per-channel 合成
    did**。此「按 did 覆盖」天然吞掉任何 stray 并存项；**新代码永不写裸多通道 did**（多摄两路都
    禁也写两条 ``:chN``，单摄禁用写裸 did = ``synthetic_camera_did(did,0,1)``）。与本 did 无关的
    条目原样保留。返回 ``(new_list, changed)``；无变化不写 KV，调用方可据此跳过下游副作用。
    """
    current = _load_list(kv_repo, ScopeConfigKeys.CAMERA_BLACK_LIST_KEY)
    denied_now = set(current)
    affected = set(updates)
    new = [d for d in current if physical_camera_did(d) not in affected]
    for pdid, ch_updates in updates.items():
        cc = channel_counts.get(pdid, 1) or 1
        disabled = denied_channels_of(denied_now, pdid, cc)
        for ch, in_use in ch_updates.items():
            disabled.discard(ch) if in_use else disabled.add(ch)
        for ch in sorted(disabled):
            new.append(synthetic_camera_did(pdid, ch, cc))
    # 去重保序
    seen: set[str] = set()
    deduped = [d for d in new if not (d in seen or seen.add(d))]
    # changed 按**集合**判（黑名单语义是集合、顺序对行为无意义）：整台重算会把受影响 did 的
    # 条目挪到末尾，纯重排（如 ["dual:ch1","other"] 开已开的 ch0 → ["other","dual:ch1"]）集合
    # 没变但列表序变了；若按有序比较会误报 changed → 白触发一轮 refresh/会话收敛。
    if set(deduped) == set(current):
        return current, False
    kv_repo.set(
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY, json.dumps(deduped, ensure_ascii=False)
    )
    return deduped, True


def set_camera_voice_in_use(
    kv_repo: KVRepo, did: str, in_use: bool
) -> tuple[list[str], bool]:
    """切换单个相机的拾音启用状态。``in_use=True`` 即加入拾音开启集（allow-list）。"""
    return _toggle_member(
        kv_repo, ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY, did, include=in_use
    )


def set_cameras_voice_in_use(
    kv_repo: KVRepo, dids: list[str], in_use: bool
) -> tuple[list[str], bool]:
    """批量切换相机拾音启用状态。去重后一次性写入 KV。拾音无投喂上限，不设 cap。"""
    return _toggle_members(
        kv_repo, ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY, dids, include=in_use
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


def _load_str_map(kv_repo: KVRepo, key: str) -> dict[str, str]:
    """读取存 JSON object（str→str）的 KV；缺省 / 非法 / 非 object 一律回落空 dict。

    跳过 JSON ``null`` 值（避免 ``str(None) → "None"`` 注入业务逻辑）。
    """
    raw = kv_repo.get(key) or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("KV %s 不是合法 JSON，视为空: %r", key, raw)
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if v is not None}
    logger.warning("KV %s 不是 JSON object，视为空: %r", key, raw)
    return {}


def camera_prompts(kv_repo: KVRepo) -> dict[str, str]:
    """全部摄像头「感知须知」自定义 prompt（did→文本）；空表示无任何自定义。"""
    return _load_str_map(kv_repo, ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY)


def set_camera_prompt(kv_repo: KVRepo, did: str, prompt: str) -> tuple[dict[str, str], bool]:
    """设置 / 清除单台相机的自定义感知 prompt。

    超长截断：仅存储前 ``MAX_CAMERA_PROMPT_LEN`` 字符（service/schema 层已有校验，
    此处防御直接调用 filter 的内部路径）。
    """
    current = _load_str_map(kv_repo, ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY)
    text = prompt.strip()[:MAX_CAMERA_PROMPT_LEN]
    new = dict(current)
    if text:
        new[did] = text
    else:
        new.pop(did, None)
    if new == current:
        return current, False
    kv_repo.set(ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY, json.dumps(new, ensure_ascii=False))
    return new, True


def clear_camera_prompt(kv_repo: KVRepo, did: str) -> tuple[dict[str, str], bool]:
    """清除单台相机的自定义感知 prompt（直接从 map 中 del）。"""
    current = _load_str_map(kv_repo, ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY)
    if did not in current:
        return current, False
    new = dict(current)
    del new[did]
    kv_repo.set(ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY, json.dumps(new, ensure_ascii=False))
    return new, True
