# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""iot 源：把一条 MIoT 属性的当前值判成条件项的 bool。

设计见 docs/superpowers/specs/2026-09-08-iot-trigger-source-design.md。本文件先放
条件项的读取与求值这两件纯粹的事 —— 它们同时被手动触发、创建校验和源层用到。
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass
from typing import Any

from miloco.rule.schema import IOT_SOURCE_TYPE

logger = logging.getLogger(__name__)

_ORDERING_OPS = ("gt", "gte", "lt", "lte")

SUPPORTED_OPS: dict[str, Any] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}

# 数值 / 布尔 / 字符串三族，跨族即不兼容。
_NUMBER, _BOOL, _STR = "number", "bool", "str"


class EvalFailed(Exception):
    """这次求值做不了。**调用方要置未就绪，不能喂假** —— 假会驱动一次凭空的退出边沿。"""


@dataclass(frozen=True)
class IotRef:
    """一条 iot 条件项。一条 rule 一个 ``(did, iid, op, value)``。"""

    rule_id: str
    did: str
    iid: str
    op: str
    value: Any


def iot_ref_of(rule) -> IotRef | None:
    """rule 的条件项是不是 iot 源，是就返回它引用的那条属性。

    判源走 ``resolved_source_type``（唯一那份判据），本函数只负责取 spec。

    形状不认识时记日志返 None，不抛：建 rule 时已经校验过，跑到这里还不认识说明是
    库里的存量脏数据（迁移和代建那两条入口绕过创建校验），让这条不触发就行。
    """
    if rule.resolved_source_type != IOT_SOURCE_TYPE:
        return None
    dnf = getattr(rule, "condition_dnf", None)
    if dnf is None or not dnf.any_of:
        return None
    for conjunction in dnf.any_of:
        for item in conjunction:
            spec = item.spec or {}
            did, iid, op = spec.get("did"), spec.get("iid"), spec.get("op")
            if not did or not iid:
                logger.warning("rule %s 的 iot 条件项缺 did / iid, 不触发", rule.id)
                return None
            if op not in SUPPORTED_OPS:
                logger.warning("rule %s 的 iot 条件项不支持 op=%s, 不触发", rule.id, op)
                return None
            if "value" not in spec:
                logger.warning("rule %s 的 iot 条件项没写 value, 不触发", rule.id)
                return None
            return IotRef(rule_id=rule.id, did=did, iid=iid, op=op, value=spec["value"])
    return None


def value_family(value: Any) -> str | None:
    """值属于哪一族。认不出来（含元组）返回 None —— 本版没有需要比较序列的条件项。

    ``bool`` 单独一族：它是 ``int`` 的子类，``True == 1`` 为真 —— 不分开的话「开关
    属性配了一个数值阈值」这种配置错误会被静默当成合法比较。

    ``int`` 与 ``float`` 同族不再细分：云端对 spec 标 float 的属性会返回 int。
    """
    if isinstance(value, bool):
        return _BOOL
    if isinstance(value, (int, float)):
        return _NUMBER
    if isinstance(value, str):
        return _STR
    return None


def compare(current: Any, op: str, expected: Any) -> bool:
    """按谓词求值。类型不兼容抛 ``EvalFailed``。

    **比较之前先查类型兼容，不能靠「异类型会抛 TypeError」兜底** —— 那句话只对大小
    比较成立。Python 里 ``"on" == 1`` 返回 False、``"on" != 1`` 返回 True，都不抛，
    所以脏数据下 ``ne`` 会被静默判成真，产生一次凭空的进入边沿。

    比较本身仍然包在 try 里：类型检查按 spec 声明的 format 做，而容器里的值来自
    云端，可能与声明不一致。
    """
    func = SUPPORTED_OPS.get(op)
    if func is None:
        raise EvalFailed(f"不支持的运算符 {op!r}")
    left, right = value_family(current), value_family(expected)
    if left is None or right is None or left != right:
        raise EvalFailed(
            f"类型不兼容: 当前值 {current!r} 与阈值 {expected!r} 不属于同一族"
        )
    if left is _STR and op in _ORDERING_OPS:
        raise EvalFailed(f"字符串不支持大小比较 (op={op})")
    try:
        return bool(func(current, expected))
    except TypeError as e:
        raise EvalFailed(f"比较失败: {e}") from e
