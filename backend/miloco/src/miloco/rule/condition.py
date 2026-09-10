# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""旧 ``condition`` 列与 ``condition_dnf`` 之间的两个方向。

反推（``condition`` → DNF）由迁移和创建路径共用：两处各写一份的话，从旧库升上来的
rule 与新建的 rule 分流结果会不同，而这种差异从规则本身看不出来。

渲染（DNF → 人话）是另一个方向，不与反推共用 —— 两个函数方向相反、各一份。
"""

from __future__ import annotations

from typing import Any

from miloco.rule.schema import (
    IOT_SOURCE_TYPE,
    KNOWN_SOURCE_TYPES,
    OMNI_SOURCE_TYPE,
    ConditionItem,
    RuleCondition,
    RuleConditionDNF,
)

# 谓词渲染成人话时用的符号。
_OP_LABEL = {
    "eq": "=",
    "ne": "≠",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
}


def condition_to_dnf(condition: RuleCondition) -> RuleConditionDNF:
    """旧 ``condition`` → 1×1 的 omni 条件项。

    收对象、出对象。迁移侧那个 JSON 串进出、异常吞成降级值的包装留在迁移自己那边:
    它的失败策略与创建路径相反（存量脏数据不能卡住启动，而创建路径要的是拒）。
    """
    return RuleConditionDNF(
        any_of=[
            [
                ConditionItem(
                    source_type=OMNI_SOURCE_TYPE,
                    spec={
                        "perceive_device_ids": list(condition.perceive_device_ids),
                        "query": condition.query,
                    },
                    negate=False,
                )
            ]
        ]
    )


def single_item_of(dnf: RuleConditionDNF | None) -> ConditionItem | None:
    """1×1 下那唯一的条件项。结构不是 1×1 时返回 None。"""
    if dnf is None or len(dnf.any_of) != 1 or len(dnf.any_of[0]) != 1:
        return None
    return dnf.any_of[0][0]


def dnf_structure_error(dnf: RuleConditionDNF | None) -> str | None:
    """结构校验（对全部源）。合法返 None，非法返错误文案。

    不认识的 ``source_type`` 直接拒，不静默当 omni —— 那会让一条 presence 条件项被
    塞进摄像头 prompt，而错误现象离根因很远。
    """
    if dnf is None:
        return "condition_dnf 不能为空"
    item = single_item_of(dnf)
    if item is None:
        return (
            "condition_dnf 本次只支持 1×1: any_of 恰好一个合取项、合取项恰好一个条件项"
        )
    if item.negate:
        return "condition_dnf 的 negate 本次强制为 false"
    if item.source_type not in KNOWN_SOURCE_TYPES:
        return (
            f"不认识的 source_type {item.source_type!r}: "
            f"已实现的是 {', '.join(sorted(KNOWN_SOURCE_TYPES))}"
        )
    return None


def render_iot_condition(
    device_name: str, prop_entry: dict[str, Any], op: str, value: Any
) -> str:
    """一条 iot 条件项渲染成住户看得懂的一句话，例如 ``玄关门锁 门 门状态 = 开``。

    落库的就是这句（``create_rule`` 在写库之前覆盖 ``condition.query``），展示层不
    现算 —— 现算就是第二份。代价是设备改名后这句仍是旧名，直到这条 rule 下次被改。
    """
    label = str(prop_entry.get("description") or "")
    parts = [device_name.strip(), label.strip(), _OP_LABEL.get(op, op)]
    return " ".join(p for p in parts if p) + f" {_value_label(prop_entry, value)}"


def _value_label(prop_entry: dict[str, Any], value: Any) -> str:
    """枚举值换成它的名字，别的原样。``开`` 比 ``1`` 有意义得多。"""
    for choice in prop_entry.get("value_list") or []:
        if isinstance(choice, dict) and choice.get("value") == value:
            name = str(choice.get("name") or "").strip()
            if name:
                return name
    return str(value)


def is_server_rendered(source_type: str) -> bool:
    """这个源的 ``condition.query`` 由服务端渲染，不接受用户输入。

    今天只有 iot。record 那条是服务端代建的（走 ``_repo.create`` 绕过创建路径），
    它自己带一句固定文案。
    """
    return source_type == IOT_SOURCE_TYPE
