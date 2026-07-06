# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""AgentPlatformAdapter —— Backend 侧 Agent 平台适配器抽象。

设计背景: 见 ``hermes-pr.md`` §五 主线 #1+#2。Backend 不再向 webhook 发 agent turn,
而是调用一个 ``AgentPlatformAdapter`` 实例直调目标 Agent 平台的 chat API。
Adapter 实现由 Plugin 侧提供(随插件打包,装到 ``MILOCO_HOME/agent_platform/<name>/``),
Backend 通过 :func:`load_adapter` 动态加载。

**Backend 不写任何平台相关逻辑** —— 全部抽象在 ``AgentPlatformAdapter`` 子类里。
具体平台(Hermes / OpenClaw / 其他)走各自的 plugin 注入实现。
"""

from __future__ import annotations

from .base import (
    AgentPlatformAdapter,
    AdapterTransportError,
    AdapterTransientError,
    AgentTurnResult,
    SystemPromptBuilder,
    TraceMeta,
    TurnContext,
)
from .loader import get_adapter, load_adapter, reset_cache

__all__ = [
    "AgentPlatformAdapter",
    "AdapterTransportError",
    "AdapterTransientError",
    "AgentTurnResult",
    "SystemPromptBuilder",
    "TraceMeta",
    "TurnContext",
    "get_adapter",
    "load_adapter",
    "reset_cache",
]