# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""miloco scope 过滤工具：家庭接入范围 + 相机接入范围。

数据落在 SQLite ``kv`` 表的 ``HOME_WHITE_LIST_KEY``（启用的家庭集合）、
``CAMERA_BLACK_LIST_KEY``（停用的相机集合）和 ``CAMERA_VOICE_ALLOW_LIST_KEY``
（**开启**拾音的相机集合，opt-in / 默认关语义），JSON array 字符串，由 :class:`KVRepo` 缓存。
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
    awake_map: dict[str, dict[int, bool | None]] | None = None,
    priority_dids: set[str] | None = None,
    deprioritized_dids: set[str] | None = None,
) -> list[str]:
    """决定「哪些相机通道该投喂/拉流」的**单一口径**——感知投喂(camera_adapter)与 native
    会话建销(refresh_cameras)共用此函数，避免两套判定漂移。

    **全拆后返回合成 did（通道粒度）**：单摄裸 did、多摄每路 ``{did}:ch{n}``。过滤按
    「相机级」（家庭 / 在线）+「通道级」（黑名单 per-channel / 镜头 per-lens）两层：
    在启用家庭内、（``online_only`` 时）在线（``require_lan=True`` 看 ``online and lan_online``、
    ``False`` 只看云端 ``online``——放过 OT 未发现但云端在线、仍可走 direct-IP/PPCS 的相机）；
    再对每路——未被拉黑（``is_camera_channel_denied``：合成 did 或裸 did 在黑名单皆停）、
    该路镜头未关（``awake_map[did][channel]`` 明确 ``False`` 才排除；``None``/缺失/``True``
    放行，未知不误杀）。

    ``cap=True`` 时上限按**启用通道数**（= 返回的合成 did 数）确定性截断到
    ``MAX_ENABLED_CAMERAS``——每路独立占一个名额，**允许在一台多摄相机中间切开**（会话已为
    在选的路起、另一路只少一条解码线程）。截断顺序按健康度分三档：已连通的
    ``priority_dids`` 优先、普通候选其次、超过连接宽限期的 ``deprioritized_dids`` 最后，
    组内仍按合成 did 升序（同一账号每轮选同一批）。两个健康集合按**物理 did** 给出——
    native/PPCS 会话是整台相机一条，同一台的各路共享连通命运——排序时把合成 did 归一回
    物理 did 再查。它们仅影响超额排序，不改变 scope / online / awake 过滤，因此
    ≤``MAX_ENABLED_CAMERAS`` 时谁都不淘汰，真正连不上的相机只在名额紧张时临时让位。
    这是投喂/拉流上限的唯一兜底，与 ``service.toggle_camera`` 的主动 enable 校验互补；
    不写 KV、不碰黑名单。``cap=False`` 用于「列全集」语义（如 rule target 校验）。
    会话/manager 生命周期由调用方（refresh_cameras）对返回集取物理 did 收敛：
    任一路在→会话在、两路都不在→拆。

    ``cameras`` 的 value 需带 ``home_id`` / ``online`` / ``lan_online``（及可选
    ``channel_count``）属性。``awake_map`` 是 per-lens 的 ``{did: {channel: bool|None}}``。
    """
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
    if not cap or len(result) <= MAX_ENABLED_CAMERAS:
        return result
    # 超限：已连通 > 普通 > 持续失败/冷却；组内按合成 did 升序稳定排序（同一账号每轮选
    # 同一批），每路独占一个名额、可拦半台。健康集合按物理 did 记（会话是整台一条），
    # 故合成 did 先归一再查——同一台的两路一起优先/一起让位。
    priority = priority_dids or set()
    deprioritized = deprioritized_dids or set()

    def _rank(did: str) -> tuple[int, str]:
        physical = physical_camera_did(did)
        if physical in priority:
            return (0, did)
        if physical in deprioritized:
            return (2, did)
        return (1, did)

    return sorted(result, key=_rank)[:MAX_ENABLED_CAMERAS]


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
