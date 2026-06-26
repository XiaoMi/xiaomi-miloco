"""miloco (sessionKey, lane) → Hermes session_id 的确定性映射。

miloco 后端按 ``sessionKey`` + ``lane`` 标识一个对话流（见
``backend/miloco/src/miloco/utils/agent_client.py::run_agent_turn`` 的 payload 字段），
Hermes api_server 则用字符串 ``session_id`` 寻址一个持久化会话
（``POST /api/sessions/{session_id}/chat``）。本模块提供稳定单向映射，使同一个
miloco 会话始终落到同一个 Hermes session，便于上下文连续与幂等去重。

映射函数纯函数、无副作用、确定性：相同输入永远产相同输出，便于排查与重建。
"""

from __future__ import annotations

import re

# Hermes api_server 在 _handle_create_session 里拒绝含控制字符或超长（>256）的
# session_id（见 gateway/platforms/api_server.py）。我们对 sessionKey/lane 做轻量
# 清洗：去掉控制字符、限长，避免 miloco 侧传入异常值时建会话被 400 拒掉。
_MAX_SESSION_ID_LEN = 200  # 留余量给 "miloco:" 前缀与冒号
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f\r\n]")


def _sanitize(value: str, fallback: str) -> str:
    """清洗单段标识：去控制字符、去首尾空白、空则用 fallback、限长。"""
    if not value:
        return fallback
    cleaned = _CTRL_RE.sub("", str(value)).strip()
    if not cleaned:
        return fallback
    if len(cleaned) > _MAX_SESSION_ID_LEN:
        cleaned = cleaned[:_MAX_SESSION_ID_LEN]
    return cleaned


def map_session(session_key: str | None, lane: str | None) -> str:
    """把 (session_key, lane) 映射为稳定的 Hermes session_id。

    返回形如 ``miloco:<session_key>:<lane>`` 的字符串。None/空值降级为
    ``main`` / ``default``，与 OpenClaw webhook 的默认值一致
    （``plugins/openclaw/src/webhooks/agent.ts`` 里 ``sessionKey = "main"``）。

    确定性：相同输入永远相同输出，不依赖时间或随机数。
    """
    key = _sanitize(session_key or "", "main")
    ln = _sanitize(lane or "", "default")
    return f"miloco:{key}:{ln}"
