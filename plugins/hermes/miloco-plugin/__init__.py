"""Miloco for Hermes Agent —— 出站核心插件。

把 miloco（家庭智能管家）的能力以 Hermes 插件形式接入：
- **上下文注入走 backend AgentPlatformAdapter 的 ``<system>`` 消息**
  （``plugins/hermes/miloco-plugin/hermes_adapter/adapter.py::build_system``，
  Author #2 收敛：硬约束 / 工具索引 / 感知格式 / 数据源 / 档案 / 目录 全部塞
  ``<system>`` 消息，缓存友好）。plugin 这边 ``pre_llm_call`` hook 注册为 noop
  （Hermes 要求 hook 名存在才认 plugin 加载成功）。
- 三个 tool：``miloco_im_push``（通知投递，对齐 OpenClaw 版
  ``subagent.run({deliver:true})`` 体验：装好就能用，cron 场景下也能直接
  投递）、``miloco_habit_suggest``（习惯建议防骚扰状态机，移植自
  ``home-profile/suggestions.ts``）、``miloco_notify_bind``（IM 渠道切换：list/switch）。
  注：早期版本有 ``miloco_status`` + ``miloco_test_push`` 两个调试工具，PR #279 收敛为
  外部脚本 ``plugins/hermes/scripts/miloco-status.sh``（确定性 wrapper，不走 LLM 推断）。
- 启动时 reconcile 4 个受管 cron job（移植自 ``home-profile/scheduler.ts``）。
- register() 时拉起 backend（``miloco-cli service start``，Author #8：插件自管
  backend 生命周期，install 漏跑 / backend 异常退出时 register 自动拉起）。

移植的 openclaw TS 源（逻辑 1:1）：
- ``plugins/openclaw/src/miloco/paths.ts``       → paths.py
- ``plugins/openclaw/src/miloco/config.ts``      → config.py（读部分）
- ``plugins/openclaw/src/services/catalog.ts``   → catalog.py
- ``plugins/openclaw/src/hooks/prompt.ts``       → context_injection.py
- ``plugins/openclaw/src/home-profile/helpers.ts`` → context_injection.py
- ``plugins/openclaw/src/home-profile/injection.ts`` → context_injection.py
- ``plugins/openclaw/src/tools/notify.ts``       → tools_notify.py
- ``plugins/openclaw/src/home-profile/suggestions.ts`` → tools_habit.py
- ``plugins/openclaw/src/home-profile/scheduler.ts`` → cron_setup.py

约束：Python 3.11+，标准库 + httpx（Hermes 依赖里已有）。所有调 Hermes ctx 的地方
try/except，插件加载不能因某个注册失败而崩。
"""

from __future__ import annotations

import logging

from .cron_setup import reconcile_cron_jobs
from .trace import register_trace_hooks
from .tools_habit import (
    MILOCO_HABIT_SUGGEST_SCHEMA,
    handle_habit_suggest,
)
from .tools_notify import (
    MILOCO_IM_PUSH_SCHEMA,
    make_im_push_handler,
)
from .tools_status import (
    MILOCO_NOTIFY_BIND_SCHEMA,
    handle_notify_bind,
)

logger = logging.getLogger(__name__)

TOOLSET = "miloco"


def register(ctx) -> None:
    """注册 pre_llm_call 钩子 + trace hooks + 3 个 tool，并 reconcile 受管 cron。

    每个注册独立 try/except：单个失败不影响其余功能，也绝不让插件加载崩掉 Hermes。
    """
    # ── Author #2 收敛:删 pre_llm_call 注入,改走 backend Adapter 的 <system> 消息 ──
    # 历史:plugin 之前用 pre_llm_call 把硬约束/设备目录塞进 user message —— 但塞
    # user 消息不命中 Hermes 的 prompt cache。
    # 现在:backend AgentPlatformAdapter (plugins/hermes/miloco-plugin/hermes_adapter/
    # adapter.py::build_system) 组装 OpenAI <system> 消息,塞硬约束/工具索引/感知格式
    # /数据源/档案/目录,缓存友好。
    # 注:hook 仍然要注册(否则 Hermes 报 "plugin X 已加载但无 hook"),所以注册一个空函数。
    try:
        ctx.register_hook("pre_llm_call", lambda *a, **kw: None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("注册 pre_llm_call (空 noop) 失败: %s", exc)

    # ── trace hooks（6 事件：pre/post_llm_call + pre/post_tool_call + on_session_start/end） ──
    # 对齐 OpenClaw trace.ts：debug 模式写 $MILOCO_HOME/trace/agent/<date>/*.jsonl.gz + .meta.json
    try:
        n = register_trace_hooks(ctx)
        logger.info("[miloco-trace] 已注册 %d 个 trace hooks", n)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[miloco-trace] 注册失败: %s", exc)

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
            name="miloco_habit_suggest",
            toolset=TOOLSET,
            schema=MILOCO_HABIT_SUGGEST_SCHEMA,
            handler=handle_habit_suggest,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("注册 miloco_habit_suggest 失败: %s", exc)

    # ── IM 渠道切换（list / switch target） ──────────────────────────────
    try:
        ctx.register_tool(
            name="miloco_notify_bind",
            toolset=TOOLSET,
            schema=MILOCO_NOTIFY_BIND_SCHEMA,
            handler=lambda args, **kw: handle_notify_bind(args, ctx),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("注册 miloco_notify_bind 失败: %s", exc)

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

    # ── Author #8: register 时拉起后端 ─────────────────────────────────
    # 插件启动时确认 miloco backend 在跑(miloco-cli service start 幂等,已在跑也无副作用)。
    # 不在 install-hermes.sh 里硬拉,改在 plugin register() 触发 —— install 漏跑或 backend
    # 异常退出后重启 Hermes,plugin register 时自动拉起。
    try:
        import subprocess
        proc = subprocess.run(
            ["miloco-cli", "service", "start"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            logger.info("[miloco-backend] register() 拉起 backend OK")
            return
        # 区分"已经在跑"(expected,幂等)和真错:
        # miloco-cli service start 在已跑时返 exit=1 + stdout/stderr 含
        # `already running (pid=...)`。这种情况 plugin 不该当 WARN 报 —— 正常
        # 重启 Hermes 时 plugin register() 总会撞到这条,75+ 次/20 分钟是噪音。
        out = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()
        already_running = "already running" in out.lower()
        if already_running:
            logger.info("[miloco-backend] register() 检测 backend 已在跑(pid 已存在,免拉)")
        else:
            logger.warning(
                "[miloco-backend] register() 拉起失败 (exit=%s): %s",
                proc.returncode, out[:200],
            )
    except FileNotFoundError:
        logger.warning("[miloco-backend] miloco-cli 不在 PATH,跳过 (install 时会处理)")
    except subprocess.TimeoutExpired:
        logger.warning("[miloco-backend] miloco-cli service start 30s 超时,跳过")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[miloco-backend] register() 拉起异常: %s", exc)
