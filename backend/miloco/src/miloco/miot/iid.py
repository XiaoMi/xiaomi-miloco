# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""`<prefix>.<siid>.<xiid>` 形态 iid 的解析。

宽松版返回 None，严格版在调用方那侧包一层抛异常 —— 两种出口共用一套形态判定，
以后 iid 形态变了只改这里。
"""

from __future__ import annotations


def try_parse_iid(iid: str, prefix: str) -> tuple[int, int] | None:
    """`<prefix>.<siid>.<xiid>` → `(siid, xiid)`；形态不对返回 None。"""
    parts = iid.split(".")
    if len(parts) != 3 or parts[0] != prefix:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None
