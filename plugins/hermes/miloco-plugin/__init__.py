"""Miloco for Hermes Agent —— 出站核心插件。

把 miloco（家庭智能管家）的能力以 Hermes 插件形式接入：
- ``pre_llm_call`` 钩子按 session profile 注入 miloco 上下文（identity / capabilities /
  perception / memory / notify / language + home-profile + pending-suggestions +
  device-catalog），移植自 openclaw ``hooks/prompt.ts``。
- 三个 tool：``miloco_im_push`` / ``miloco_notify_bind``（两段式通知协议，移植自
  ``tools/notify.ts``）、``miloco_habit_suggest``（习惯建议防骚扰状态机，移植自
  ``home-profile/suggestions.ts``）。
- 启动时 reconcile 4 个受管 cron job（移植自 ``home-profile/scheduler.ts``）。

移植的 openclaw TS 源（逻辑 1:1）：
- ``plugins/openclaw/src/miloco/paths.ts``       → paths.py
- ``plugins/openclaw/src/miloco/config.ts``      → config.py（读部分）
- ``plugins/openclaw/src/services/catalog.ts``   → catalog.py
- ``plugins/openclaw/src/hooks/prompt.ts``       → context_injection.py
- ``plugins/openclaw/src/home-profile/helpers.ts`` → context_injection.py
- ``plugins/openclaw/src/home-profile/injection.ts`` → context_injection.py
- ``plugins/openclaw/src/tools/notify.ts``       → tools_notify.py + notify_target.py
- ``plugins/openclaw/src/home-profile/suggestions.ts`` → tools_habit.py
- ``plugins/openclaw/src/home-profile/scheduler.ts`` → cron_setup.py

约束：Python 3.11+，标准库 + httpx（Hermes 依赖里已有）。所有调 Hermes ctx 的地方
try/except，插件加载不能因某个注册失败而崩。
"""

from __future__ import annotations

import logging

from .context_injection import inject_context
from .cron_setup import reconcile_cron_jobs
from .tools_habit import (
    MILOCO_HABIT_SUGGEST_SCHEMA,
    handle_habit_suggest,
)
from .tools_notify import (
    MILOCO_IM_PUSH_SCHEMA,
    MILOCO_NOTIFY_BIND_SCHEMA,
    make_im_push_handler,
    make_notify_bind_handler,
)

logger = logging.getLogger(__name__)

TOOLSET = "miloco"


def register(ctx) -> None:
    """注册 pre_llm_call 钩子 + 3 个 tool，并 reconcile 受管 cron。

    每个注册独立 try/except：单个失败不影响其余功能，也绝不让插件加载崩掉 Hermes。
    """
    # ── pre_llm_call 钩子 ──────────────────────────────────────────────
    try:
        ctx.register_hook("pre_llm_call", inject_context)
    except Exception as exc:  # noqa: BLE001
        logger.exception("注册 pre_llm_call 失败: %s", exc)

    # ── tools ──────────────────────────────────────────────────────────
    try:
        ctx.register_tool(
            name="miloco_im_push",
            toolset=TOOLSET,
            schema=MILOCO_IM_PUSH_SCHEMA,
            handler=make_im_push_handler(ctx),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("注册 miloco_im_push 失败: %s", exc)

    try:
        ctx.register_tool(
            name="miloco_notify_bind",
            toolset=TOOLSET,
            schema=MILOCO_NOTIFY_BIND_SCHEMA,
            handler=make_notify_bind_handler(ctx),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("注册 miloco_notify_bind 失败: %s", exc)

    try:
        ctx.register_tool(
            name="miloco_habit_suggest",
            toolset=TOOLSET,
            schema=MILOCO_HABIT_SUGGEST_SCHEMA,
            handler=handle_habit_suggest,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("注册 miloco_habit_suggest 失败: %s", exc)

    # ── 受管 cron reconcile ────────────────────────────────────────────
    # 放最后：cron 模块不在时 graceful 跳过，不影响已注册的 hook/tool。
    try:
        result = reconcile_cron_jobs()
        if result.get("skipped"):
            logger.info("miloco cron reconcile 跳过（cron 模块不可用）")
        else:
            logger.info(
                "miloco cron reconcile 完成: created=%s updated=%s removed=%s",
                result.get("created"), result.get("updated"), result.get("removed"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("miloco cron reconcile 失败: %s", exc)
