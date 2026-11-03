# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""`<prefix>.<siid>.<xiid>` 形态 iid 的解析。

宽松版返回 None，严格版在调用方那侧包一层抛异常 —— 两种出口共用一套形态判定，
以后 iid 形态变了只改这里。

**这里也是「哪条 iid 指事件」的唯一判据。** 规则条件项、容器叶、spec 键三处认的是
同一个串：属性是裸 `<siid>.<piid>`（历史形态，不带前缀），事件带 `event.` 前缀 ——
所以前缀本身就是命名空间，别处不许再各写一份 `startswith`。
"""

from __future__ import annotations

# 事件参数的 iid 前缀。事件叶与属性叶在容器里分属 `/event/` 与 `/prop/` 两棵子树，
# 规则侧的 iid 必须在自带信息里就能看出该走哪棵。
EVENT_IID_PREFIX = "event."


def try_parse_iid(iid: str, prefix: str) -> tuple[int, int] | None:
    """`<prefix>.<siid>.<xiid>` → `(siid, xiid)`；形态不对返回 None。"""
    parts = iid.split(".")
    if len(parts) != 3 or parts[0] != prefix:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def is_event_iid(iid: str) -> bool:
    """这条 iid 指的是事件参数，不是属性。"""
    return iid.startswith(EVENT_IID_PREFIX)


def event_iid(siid: int, eiid: int, argname: str) -> str:
    """事件参数的 iid。与 `_fetch_device_events` 的键、容器叶的末段同源。"""
    return f"{EVENT_IID_PREFIX}{siid}.{eiid}.{argname}"


def leaf_path(did: str, iid: str) -> str:
    """规则侧 iid → 状态容器里的叶路径。

    `event.` 前缀走事件子树，其余一律当属性 —— 属性侧的历史形态（裸
    `<siid>.<piid>`）不带前缀，所以这里不是「认前缀」，是「前缀即命名空间」。
    """
    if is_event_iid(iid):
        return f"iot/device/{did}/event/{iid[len(EVENT_IID_PREFIX):]}"
    return f"iot/device/{did}/prop/{iid}"
