"""通知投递目标解析（best-effort）。

移植自 openclaw TypeScript 插件 ``plugins/openclaw/src/tools/notify.ts`` 的
``resolveNotifyTarget`` / ``notifyOwner`` 中"选目标"那一段。

**与 openclaw 的关键差异**：Hermes 没有 openclaw 那种带 ``lastTo`` / ``lastChannel``
的 SessionDB（``api.runtime.agent.session.loadSessionStore``），所以无法像 TS 端那样
"fallback 到最近活跃 channel"。本模块采取更保守的策略：

- 优先用 ``notifySessionKey``（由 ``miloco_notify_bind`` 写入插件配置 JSON）。
  绑定的 key 形如 ``platform:chat_id[:thread_id]``，直接作为 ``send_message`` 的
  ``target`` 用。
- 未绑定或绑定失效 → 返回 ``needsBind=True``，让 agent 走两段式协议（补 bindHint
  重调 ``miloco_im_push``）。**不强依赖** Hermes 内部 session 结构。

binding 校验只做"非空字符串"级别；真实可用性交给 ``send_message`` 在投递时验证
（target 不存在会返回 error，``miloco_im_push`` 据此返回 ``ok:false`` 让 agent 重绑）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


BindReason = str  # "not_configured" | "configured_but_invalid"


def resolve_notify_target(notify_session_key: Optional[str]) -> Dict[str, Any]:
    """返回 ``{"needsBind": bool, "target": {...}|None, "bindReason": str}``。

    与 TS 端 ``resolveNotifyTarget`` 返回结构对齐，但 ``target`` 只含
    Hermes ``send_message`` 需要的 ``target`` 字段（``platform:chat_id[:thread_id]``）。
    """
    if notify_session_key:
        return {
            "needsBind": False,
            "bindReason": "",
            "target": {"target": notify_session_key, "sessionKey": notify_session_key},
        }
    # 未绑定：target 留空。tools_notify.notify_owner 据此走两段式协议——
    # 返回 needsBind=true + bindHintExample，让 agent 引导用户先
    # miloco_notify_bind 绑定一个 target，再重发。不强依赖 Hermes
    # SessionDB（Hermes 无 openclaw 那种 lastTo/lastChannel 结构）。
    return {
        "needsBind": True,
        "bindReason": "not_configured",
        "target": None,
    }
