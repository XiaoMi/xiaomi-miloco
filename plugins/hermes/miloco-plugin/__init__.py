"""Miloco for Hermes Agent —— 出站核心插件。

把 miloco（家庭智能管家）的能力以 Hermes 插件形式接入：
- **上下文注入走 backend AgentPlatformAdapter 的 ``<system>`` 消息**
  （``plugins/hermes/miloco-plugin/hermes_adapter/adapter.py::build_system``，
  Author #2 收敛：硬约束 / 工具索引 / 感知格式 / 数据源 / 档案 / 目录 全部塞
  ``<system>`` 消息，缓存友好）。plugin 这边 ``pre_llm_call`` hook 注册为 noop
  （Hermes 要求 hook 名存在才认 plugin 加载成功）。
- 两个 tool：``miloco_im_push``（通知投递，对齐 OpenClaw 版
  ``subagent.run({deliver:true})`` 体验：装好就能用，cron 场景下也能直接
  投递）、``miloco_notify_bind``（IM 渠道切换：list/switch）。
  注：``miloco_habit_suggest`` 防骚扰状态机已随 PR #419 迁入 ``miloco-cli habit``
  命令组；本插件只保留 ``context_injection.py`` 里内嵌的只读注入 reader
  （``build_pending_suggestion_block``，对齐 ``home-profile/injection.ts``），
  不再注册 habit tool。
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
  （只读注入 reader；状态机已迁入 ``miloco-cli habit``，见 PR #419）
- ``plugins/openclaw/src/tools/notify.ts``       → tools_notify.py
- ``plugins/openclaw/src/home-profile/scheduler.ts`` → cron_setup.py

约束：Python 3.11+，标准库 + httpx（Hermes 依赖里已有）。所有调 Hermes ctx 的地方
try/except，插件加载不能因某个注册失败而崩。
"""

from __future__ import annotations

import logging
import math
import os

from .cron_setup import reconcile_cron_jobs
from .tools_notify import (
    MILOCO_IM_PUSH_SCHEMA,
    MILOCO_NOTIFY_BIND_SCHEMA,
    handle_notify_bind,
    make_im_push_handler,
)
from .trace import register_trace_hooks

logger = logging.getLogger(__name__)

TOOLSET = "miloco"


_START_LOCK_NAME = ".backend-start.lock"

_DEFAULT_STARTUP_TIMEOUT_S = 90.0
# CLI 的 service start 端到端 = 就绪等待（MILOCO_STARTUP_TIMEOUT）+ 固定开销。
# 固定开销按「每个外部命令都贴近自己的超时线、但最终成功返回」的最坏情形记账：
#   前置 6 次 supervisorctl（pid / status / pid / reread / update / start）= 48s
#   启动前的 _is_port_in_use（TCP 1s）= 1s
#   窗口最后一轮越界（fatal status 8 + http 2 + sleep 0.5）≈ 10.5s
#   出循环后的 fatal 复核 = 8s
#   端口身份校验（_port_open 1 + lsof 5 + ps 5）= 11s
#   _resolve_backend_pid（status 8 + lsof 兜底 5）= 13s
# 合计 ≈ 91.5s，取 100 让「外层预算 ≥ CLI 端到端」在这套假设下真的成立。
_CLI_START_MARGIN_S = 100.0


def _cli_start_budget_s() -> float:
    """外层拉起预算：与 CLI 的就绪等待同源推导，不能写死。

    写死 150s 只在默认 90s 下成立。运维按 hint 把 ``MILOCO_STARTUP_TIMEOUT`` 调到
    180 时，CLI 还在等，外层却先超时杀掉子进程，``TimeoutExpired`` 把 CLI 新加的
    detail/hint 全部吞掉 —— 正是要避免的场景。
    """
    return _startup_timeout_base_s() + _CLI_START_MARGIN_S


def _startup_timeout_base_s() -> float:
    """CLI 侧就绪等待上限的基线（``MILOCO_STARTUP_TIMEOUT``，默认 90s）。

    锁的等待窗口必须 ≥ 外层预算，而外层预算要盖住这个基线；否则运维把
    ``MILOCO_STARTUP_TIMEOUT`` 调大后，后到进程会在先到者还没跑完时放弃锁、重新并发
    进入 —— 正是这把锁要消灭的互杀场景。
    """
    raw = os.environ.get("MILOCO_STARTUP_TIMEOUT", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return _DEFAULT_STARTUP_TIMEOUT_S
        if math.isfinite(value) and value > 0:
            return value
    return _DEFAULT_STARTUP_TIMEOUT_S


def _backend_health_ok() -> bool:
    """一次探 backend 的 /health：已经 200 就不必再拉起。

    URL 走本包的 ``load_shared_config()``（与 CLI 读同一份 config.json，缺字段时带
    schema 默认值），不再内联一遍 json 解析 —— 两份实现的边界迟早漂。任何异常都当作
    「不确定」返回 False：预检失败不该阻止正常的拉起。
    """
    try:
        import httpx

        from .config import load_shared_config

        url = load_shared_config()["server"]["url"]
        resp = httpx.get(url.rstrip("/") + "/health", timeout=3)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _acquire_start_lock(timeout_s: float | None = None) -> "int | None":
    """拿 MILOCO_HOME 下的独占 flock，把「拉起 backend」在进程间串行化。

    等待上限默认按「CLI 就绪等待 + 60s 余量」推导，且不低于外层预算，保证先到者跑满
    时后到者仍在等锁。拿不到（平台无 fcntl、文件系统不支持 flock、目录不可写、等到
    超时）就返回 None，调用方按原行为并发直跑，不因为加锁反而拉不起后端。
    """
    import errno
    import time

    from .paths import miloco_home

    try:
        import fcntl
    except ImportError:
        return None
    if timeout_s is None:
        # 锁窗口 = 外层预算 + 60s 余量，两层都随 MILOCO_STARTUP_TIMEOUT 走：
        # 「锁等待 ≥ 外层预算 ≥ CLI 就绪等待」这条链不能有写死的中间环。
        timeout_s = _cli_start_budget_s() + 60.0
    try:
        path = miloco_home() / _START_LOCK_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except Exception:  # noqa: BLE001
        return None
    deadline = time.time() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            # 只把「别人占着锁」当忙；ENOTSUP / ENOLCK（部分 FUSE、网络家目录）意味着
            # 这台机器根本不支持 flock，继续重试只会把线程卡满整个超时窗口。
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(fd)
                return None
            if time.time() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.5)


def _release_start_lock(fd) -> None:
    """释放并关闭拉起锁（fd 为 None 时是 no-op）。"""
    if fd is None:
        return
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001
        pass
    try:
        os.close(fd)
    except Exception:  # noqa: BLE001
        pass


def register(ctx) -> None:
    """注册 pre_llm_call 钩子 + trace hooks + 2 个 tool，并 reconcile 受管 cron。

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
    # 放后台 daemon 线程：register() 是 Hermes 插件加载的同步调用，
    # subprocess.run(timeout=_cli_start_budget_s()) 会让 Hermes 启动阻塞。
    # 挪到 daemon 线程，不影响 Hermes 启动。
    def _start_backend_if_needed() -> None:
        try:
            import subprocess

            # 已经 200 就别再拉：dashboard 与 gateway 是两个进程，各自加载本插件、
            # 各跑一次 register()。`service start` 在自己认为 supervisord 没起来时会
            # terminate 掉所有 supervisord 并 unlink socket（见 cli/service.py:
            # _supervisord_is_running / service_start 的防重复孵化分支）—— 两个进程
            # 同时进来就是互杀：一个拿到 exit 2，另一个的就绪等待被拖过预算，
            # 日志上表现为「拉起失败」，backend 其实已经起来了。
            if _backend_health_ok():
                logger.info("[miloco-backend] backend 已在跑(/health 200)，无需拉起")
                return

            # 冷启动时两边都探不到 /health，仍会并发进入；用 MILOCO_HOME 下的文件锁
            # 把拉起串行化，后到的一方拿到锁后先复核一次 /health。
            lock_fd = _acquire_start_lock()
            try:
                # 拿没拿到锁都先复核一次：拿锁失败意味着并发窗口更大，这一步更该做，
                # 否则回落路径会直接进 `service start`，重新打开这把锁要消灭的互杀。
                if _backend_health_ok():
                    logger.info("[miloco-backend] 另一进程已拉起 backend，免拉")
                    return
                proc = subprocess.run(
                    ["miloco-cli", "service", "start"],
                    capture_output=True, text=True,
                    timeout=_cli_start_budget_s(),
                )
            finally:
                _release_start_lock(lock_fd)
            if proc.returncode == 0:
                # 放行档（仍在预热 / 端口通但 unhealthy）的退出码也是 0，归因在 stderr；
                # 不转出来，本 PR 特意补的诊断线索到插件日志这一层就断了。
                if (proc.stderr or "").strip():
                    logger.warning("[miloco-backend] %s", proc.stderr.strip()[:500])
                logger.info("[miloco-backend] register() 拉起 backend OK")
                return
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("[miloco-backend] register() 拉起异常: %s", exc)

    import threading
    threading.Thread(target=_start_backend_if_needed, daemon=True).start()
