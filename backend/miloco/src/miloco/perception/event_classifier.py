# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""有意义事件分类纯函数.

判断一次推理(全屋视图 result)是否有意义 + 计算 has_* 标志位.

注:result 是 `_merge_results` 合并后的全屋视图;classifier 本身只算 has_* 标志位,
不关心 device 归属——device 层面的收窄(把 device_ids 收窄到真正触发事件的来源摄像头)
由 client.py 的 `_collect_relevant_device_ids` 基于 matched_rules / suggestions /
speeches 各自的 source_device_ids 另外处理,不在这里做.
"""

from __future__ import annotations

from miloco.perception.types import RealtimePerceptionResult


def classify(result: RealtimePerceptionResult) -> dict:
    """计算 has_* 标志位 + 整体是否"有意义".

    入表条件(任一为真即入表):
    - has_rule_hit:`result.matched_rules` 非空
    - has_suggestion:`result.suggestions` 非空
    - has_asr:存在至少一个 `Speech.needs_response=True AND is_complete=True`
      (家人闲聊 / 未说完的指令不算)

    Returns:
        {
            "is_meaningful": bool,     # 任一 has_* 为 true
            "has_rule_hit": bool,
            "has_suggestion": bool,
            "has_asr": bool,
        }
    """
    has_rule_hit = bool(result.matched_rules)
    has_suggestion = bool(result.suggestions)
    has_asr = any(
        s.needs_response and s.is_complete for s in result.speeches
    )

    return {
        "is_meaningful": has_rule_hit or has_suggestion or has_asr,
        "has_rule_hit": has_rule_hit,
        "has_suggestion": has_suggestion,
        "has_asr": has_asr,
    }
