# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Agent turn 调度包：按会话单飞 + 同类批量合并，收口 producer → agent 投递。"""

from miloco.dispatch.dispatcher import (
    MILOCO_SESSION_KEYS,
    AgentDispatcher,
    EventType,
    dispatch_event,
    get_agent_dispatcher,
    join_text_blocks,
    set_agent_dispatcher,
)

__all__ = [
    "MILOCO_SESSION_KEYS",
    "AgentDispatcher",
    "EventType",
    "dispatch_event",
    "get_agent_dispatcher",
    "join_text_blocks",
    "set_agent_dispatcher",
]
