# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MILOCO Server main application entry point.
Provides FastAPI application setup, middleware configuration, and server startup.
"""

import asyncio
import functools
import logging
import os
import time
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from miloco.admin.router import router as admin_router
from miloco.config import get_settings, register_reset_hook
from miloco.database.connector import init_database
from miloco.dispatch import AgentDispatcher, set_agent_dispatcher
from miloco.home_profile.router import router as home_profile_router
from miloco.manager import get_manager
from miloco.middleware.exception_handler import handle_exception
from miloco.miot.router import router as miot_router
from miloco.node_monitor.event_log import NodeEventLog
from miloco.node_monitor.monitor import get_monitor
from miloco.node_monitor.resource_monitor import ResourceMonitor
from miloco.node_monitor.router import router as monitor_router
from miloco.node_monitor.router import set_resource_monitor
from miloco.node_monitor.watchdog import WatchdogTask
from miloco.observability.agent_meta_poller import (
    AgentMetaPoller,
    set_agent_meta_poller,
)
from miloco.observability.cleanup import (
    cleanup_agent_runs_table,
    cleanup_events_table,
    cleanup_trace_jsonl,
    cleanup_traces_device_table,
    cleanup_traces_table,
)
from miloco.observability.metrics_client import MetricsClient, set_metrics_client
from miloco.observability.metrics_db import connect as obs_connect
from miloco.observability.metrics_db import init_schema as obs_init_schema
from miloco.observability.router import router as observability_router
from miloco.perception.events_router import router as events_router
from miloco.perception.router import router as perception_router
from miloco.person.router import router as person_router
from miloco.rule.router import router as rule_router
from miloco.task.router import router as task_router
from miloco.task_record.router import router as task_record_router
from miloco.utils.common import escape_for_js_string
from miloco.utils.paths import miloco_home

load_dotenv()

logger = logging.getLogger(__name__)

# 持久持有后台 task 引用,asyncio 只 weakref tasks → 局部变量 lifecycle 一过
# task 就被 GC 中断。lifespan 里 create_task 后挂进这个 set,task done 时
# add_done_callback 自清,确定性比"靠 generator frame 保活"强。
_BG_TASKS: set[asyncio.Task[object]] = set()


async def _log_cleanup_loop() -> None:
    """Background task: clean up perception/rule logs + observability tables/files."""
    settings = get_settings()
    mgr = get_manager()
    obs_db_path = settings.directories.workspace_dir / "observability.db"
    # trace/agent 由 plugin 写到 $MILOCO_HOME 下(不带 storage 前缀),
    # cleanup 必须对齐源写路径,否则 root.exists() 假返回 0 导致永不清理。
    trace_root = miloco_home() / "trace" / "agent"
    await asyncio.sleep(60)  # delay first cleanup to avoid cold-start I/O spike
    while True:
        try:
            deleted_p = mgr.perception_service.cleanup_logs(settings.perception.log_ttl)
            logger.info("Perception log cleanup: deleted %d entries", deleted_p)
        except Exception as e:
            logger.error("Perception log cleanup failed: %s", e)
        try:
            deleted_r = await mgr.rule_service.cleanup_logs(settings.rule.log_ttl)
            logger.info("Rule log cleanup: deleted %d entries", deleted_r)
        except Exception as e:
            logger.error("Rule log cleanup failed: %s", e)
        # 跟 trace.ts 的 DEBUG flag 对称:debug 关 → 不产文件 → 不清理。
        # 每轮现读,运行时建/删 flag 下个周期立即生效。
        if (miloco_home() / ".debug_observability").exists():
            try:
                dj = cleanup_trace_jsonl(trace_root, settings.perf.retention.trace_jsonl_days)
                logger.info("Trace jsonl cleanup: removed %d day-dirs", dj)
            except Exception as e:
                logger.error("Trace jsonl cleanup failed: %s", e)
        # perf.enabled=false 时跳过 observability cleanup — obs_init_schema 在
        # conn 上无条件建表,这里不门控会让关闭 perf 后 observability.db 仍被建出。
        if settings.perf.enabled:
            try:
                conn = obs_connect(obs_db_path)
                try:
                    obs_init_schema(conn)
                    dt = cleanup_traces_table(conn, settings.perf.retention.traces_days)
                    dtd = cleanup_traces_device_table(conn, settings.perf.retention.traces_days)
                    de = cleanup_events_table(conn, settings.perf.retention.events_days)
                    da = cleanup_agent_runs_table(
                        conn, settings.perf.retention.agent_runs_days
                    )
                    logger.info(
                        "Observability cleanup: traces=%d, traces_device=%d, events=%d, agent_runs=%d",
                        dt, dtd, de, da,
                    )
                    # auto_vacuum=INCREMENTAL 下,DELETE 把页标 free 但不还 OS。
                    # 这里集中触发 incremental_vacuum,每页 4KB × 10000 ≈ 40MB 回收上限。
                    try:
                        conn.execute("PRAGMA incremental_vacuum(10000)")
                    except Exception as e:
                        logger.error("incremental_vacuum failed: %s", e)
                finally:
                    conn.close()
            except Exception as e:
                logger.error("Observability DB cleanup failed: %s", e)
        # meaningful_events 行清理(按 event_ttl_days)
        try:
            deleted_e = mgr.meaningful_events_dao.delete_before_days(
                settings.perception.event_ttl_days
            )
            logger.info("Meaningful events cleanup: deleted %d rows", deleted_e)
        except Exception as e:
            logger.error("Meaningful events cleanup failed: %s", e)
        # 事件截图目录清理(TTL + LRU 两阶段)
        try:
            from miloco.perception.snapshot_writer import cleanup_snapshots

            stats = cleanup_snapshots(
                ttl_days=settings.perception.snapshot_ttl_days,
                max_disk_mb=settings.perception.snapshot_max_disk_mb,
            )
            logger.info(
                "Snapshots cleanup: ttl=%d lru=%d remaining=%dMB",
                stats["deleted_by_ttl"],
                stats["deleted_by_lru"],
                stats["remaining_mb"],
            )
        except Exception as e:
            logger.error("Snapshots cleanup failed: %s", e)
        # miloco.db 已 DELETE 旧 perception_log / rule_log / meaningful_events,
        # 走 incremental_vacuum 把 free pages 还 OS。
        try:
            from miloco.database.connector import get_db_connector

            with get_db_connector().get_connection() as conn:
                conn.execute("PRAGMA incremental_vacuum(10000)")
        except Exception as e:
            logger.error("Miloco DB incremental_vacuum failed: %s", e)
        await asyncio.sleep(86400)


async def _rollover_daily_loop() -> None:
    """每天 0:05 (Asia/Shanghai) 触发 task_record rollover（spec §6.3）。

    startup 立即跑一次（self-heal 跨日重启窗口），之后按 daily cadence sleep
    到下次 0:05 再跑。跑 rollover 用 ``asyncio.to_thread`` 把 sqlite3 阻塞 I/O
    移出事件循环。
    """
    from datetime import datetime as _dt

    from miloco.task_record.rollover import (
        rollover_daily_job,
        seconds_until_next_run,
    )
    from miloco.task_record.service import TaskRecordService
    from miloco.utils.time_utils import deploy_timezone

    service = TaskRecordService()
    loop = asyncio.get_running_loop()

    def _invoke_notify_on_loop(
        task_id: str,
        pre_state: tuple[int | None, int] | None,
    ) -> None:
        """在 event loop 线程里跑：调 rule engine，里面 asyncio.create_task。"""
        try:
            from miloco.manager import get_manager

            mgr = get_manager()
            rule_service = getattr(mgr, "_rule_service", None)
            if rule_service is None:
                return
            rule_service.notify_record_rollover(task_id, pre_state)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to notify rule engine of rollover: task_id=%s", task_id
            )

    def _notify_rule_engine_rollover(
        task_id: str,
        pre_state: tuple[int | None, int] | None,
    ) -> None:
        """rollover_daily_job 跑在 asyncio.to_thread 工作线程，rule engine 调
        asyncio.create_task 必须在 event loop 线程。借 call_soon_threadsafe 跨线程
        排回调到 loop。pre_state 是 rollover 前 snapshot，供 rule engine 兜底
        判断旧一天是否累计达标。"""
        loop.call_soon_threadsafe(_invoke_notify_on_loop, task_id, pre_state)

    # 所有 now 都用 _dt.now(deploy_timezone()) ——容器默认 UTC 时区时 naive
    # datetime.now() 会被 _to_aware 误标本地时区，与真实部署时间差几小时，
    # period_start 错位导致 rollover 静默跳过。
    try:
        result = await asyncio.to_thread(
            rollover_daily_job, service, _dt.now(deploy_timezone()),
            _notify_rule_engine_rollover,
        )
        logger.info("Rollover self-heal at startup done: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.error("Rollover self-heal at startup failed: %s", e)

    while True:
        wait = seconds_until_next_run(_dt.now(deploy_timezone()))
        await asyncio.sleep(wait)
        try:
            result = await asyncio.to_thread(
                rollover_daily_job, service, _dt.now(deploy_timezone()),
                _notify_rule_engine_rollover,
            )
            logger.info("Daily rollover at 0:05 done: %s", result)
        except Exception as e:  # noqa: BLE001
            logger.error("Daily rollover failed: %s", e)


async def _backfill_tier_a_reid_embeddings() -> None:
    """启动后台一次性: 给缺 ReID .npy 的 tier_a body 补抽 emb(幂等)。

    历史/迁移库里(注册时无活动感知引擎、get_reid_extractor 返 None)写入的 tier_a body
    缺同名 .npy, 使依赖 tier_a ReID emb 的去重/匹配(如 pool_fetch get_person_mean_emb)
    对这些人静默降级。这里幂等补齐: 已有 .npy 的跳过, 首次补完后每次启动只扫一遍。
    复用引擎已加载的 ReID(get_reid_extractor 兜底), 不额外加载 ONNX。只补 tier_a——它运行时
    引擎不写, 与实时写库无锁竞争; tier_c 由实时引擎写时即带 emb, 无需在此 backfill。
    """
    try:
        from miloco.perception.engine.identity.engine import build_identity_library
        extractor = get_manager().perception_service.get_reid_extractor()
        if extractor is None:
            logger.info("启动 backfill tier_a ReID emb 跳过: 无可用 ReID extractor")
            return
        lib = build_identity_library()
        result = await asyncio.to_thread(
            lib.backfill_reid_embeddings, extractor, tiers=("a",)
        )
        logger.info("启动 backfill tier_a ReID emb 完成: %s", result)
    except Exception:  # noqa: BLE001
        logger.warning("启动 backfill tier_a ReID emb 失败(忽略)", exc_info=True)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan event handler."""
    # Startup
    app_start = time.monotonic()
    logger.info("Initializing application...")

    # 单 worker 守护见 start_server() 末尾;**不**放 lifespan — 通用 PaaS / Docker
    # 镜像常含 WEB_CONCURRENCY env(取值 CPU 核数,8/16),即使住户最终单 worker
    # 跑 uvicorn.run,只要 host 环境继承了这条 env,lifespan 就误抛 NotImplementedError
    # 让整个 backend 起不来。守护放在 CLI 入口处只挡 `gunicorn -w N` 显式多 worker
    # 命令,不误伤通用 env。
    try:
        init_database()
        logger.info("Database initialization completed")
    except Exception as e:
        logger.error("Database initialization failed: %s", e)
        raise

    # Initialize node event log
    settings = get_settings()
    log_dir = settings.directories.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    event_log = NodeEventLog(str(log_dir / "node_events.log"))
    get_monitor().set_event_log(event_log)

    logger.info("Application initialization completed")

    # agent turn 调度器必须先于任何 producer 就绪 — manager.initialize() 内部即拉起
    # producer(MIoT user-level bind 订阅、perception_runner.start()),它们的回调会
    # 直接调 dispatch_event;若 dispatcher 还是 None,启动窗口内到达的事件(如启动那
    # 几秒里在米家 App 绑的新设备 bind push)会被 WARN drop。dispatcher 无依赖 manager
    # 的初始化(drainer 内 track_agent_run 在 poller 缺省时自短路),故安全前置。
    dispatcher = AgentDispatcher()
    await dispatcher.start()
    set_agent_dispatcher(dispatcher)
    _app.state.dispatcher = dispatcher

    try:
        await get_manager().initialize()
        logger.info("Manager initialization completed")
    except Exception as e:
        logger.error("Manager initialization failed: %s", e)
        raise

    # Start monitoring threads after manager.initialize() completes
    mon = get_monitor()
    watchdog = WatchdogTask(mon)
    watchdog.start()

    resource_mon = ResourceMonitor(
        mon,
        db_path=str(settings.database_path),
        log_dir=str(log_dir),
    )
    resource_mon.start()
    set_resource_monitor(resource_mon, app_start)

    # observability: SQLite trace pipeline + agent meta poller
    # perf.enabled=false 时整套不启动 — singleton 留 None,业务侧 track_agent_run /
    # get_metrics_client 的判空分支兜底,observability.db 不被建。shutdown 段用
    # hasattr(_app.state, ...) 守护,这里跳过绑 state 即可自动跳过 stop。
    if settings.perf.enabled:
        obs_db_path = settings.directories.workspace_dir / "observability.db"
        metrics_client = MetricsClient(db_path=obs_db_path)
        await metrics_client.start()
        set_metrics_client(metrics_client)
        _app.state.metrics_client = metrics_client
        _app.state.obs_db_path = obs_db_path

        agent_meta_poller = AgentMetaPoller(metrics_client=metrics_client)
        await agent_meta_poller.start()
        set_agent_meta_poller(agent_meta_poller)
        _app.state.agent_meta_poller = agent_meta_poller

    # 启动后台补齐 tier_a 缺失的 ReID .npy(历史/迁移库遗留); 幂等、零阻塞
    _backfill_task = asyncio.create_task(_backfill_tier_a_reid_embeddings())
    _BG_TASKS.add(_backfill_task)
    _backfill_task.add_done_callback(_BG_TASKS.discard)

    cleanup_task = asyncio.create_task(_log_cleanup_loop())

    rollover_task = asyncio.create_task(_rollover_daily_loop())
    _BG_TASKS.add(rollover_task)
    rollover_task.add_done_callback(_BG_TASKS.discard)

    logger.info(os.getenv("MILOCO_SERVER_READY", "Server is ready"))

    yield

    # Shutdown
    logger.info("Application is shutting down...")

    watchdog.enter_shutdown()

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    rollover_task.cancel()
    try:
        await rollover_task
    except asyncio.CancelledError:
        pass

    # 关闭顺序遵循"生产者先于消费者":
    #   perception engine (traces/events 主要生产者)
    #   → agent_meta_poller (消费 enqueue,同时是 metrics_client 的生产者:record_agent_run)
    #   → metrics_client (drain queue + close conn)
    # 顺序反了会让 stop_engine 期间的 in-flight cycle 命中 client=None 静默丢 trace,
    # 而这窗口恰好是排查"关机/重启卡住"最想看的最后几秒。
    # wait_for 防 PPCS SDK C 扩展卡住 → 让后面 cleanup 来不及跑。uvicorn graceful
    # 默认 30s 后强 kill 整个进程,这里 10s 给 perception 自己 stop 的窗口。
    try:
        await asyncio.wait_for(
            get_manager().perception_service.stop_engine(),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "perception engine stop_engine timeout(10s),继续 cleanup 其它资源"
        )
    except Exception as e:
        logger.error("Failed to stop perception engine: %s", e)

    # dispatcher 在 perception(producer)之后、poller(消费 track_agent_run)之前停,
    # 续上"生产者先于消费者"链路:producer → dispatcher → poller → metrics_client。
    if hasattr(_app.state, "dispatcher"):
        try:
            await _app.state.dispatcher.stop()
        except Exception as e:
            logger.error("Failed to stop agent dispatcher: %s", e)
        set_agent_dispatcher(None)

    if hasattr(_app.state, "agent_meta_poller"):
        try:
            await _app.state.agent_meta_poller.stop()
        except Exception as e:
            logger.error("Failed to stop agent meta poller: %s", e)
        set_agent_meta_poller(None)

    if hasattr(_app.state, "metrics_client"):
        try:
            await _app.state.metrics_client.stop()
        except Exception as e:
            logger.error("Failed to stop metrics client: %s", e)
        set_metrics_client(None)

    # Stop monitoring threads after engine
    watchdog.stop()
    resource_mon.stop()
    event_log.shutdown()

    logger.info("Application has been shut down")


_settings = get_settings()
app = FastAPI(
    title=_settings.app.title,
    description=_settings.app.description,
    version=_settings.app.version,
    lifespan=lifespan,
)


@app.middleware("http")
async def catch_all_exceptions_middleware(request: Request, call_next):
    """Global exception handling middleware"""
    try:
        return await call_next(request)
    except Exception as exc:
        return handle_exception(request, exc)


app.include_router(admin_router, prefix="/api")
app.include_router(miot_router, prefix="/api")
app.include_router(person_router, prefix="/api")
app.include_router(home_profile_router, prefix="/api")
app.include_router(rule_router, prefix="/api")
app.include_router(task_router, prefix="/api")
app.include_router(task_record_router, prefix="/api")
app.include_router(perception_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(monitor_router, prefix="/api")
# observability_router 整个 router 都依赖 _app.state.obs_db_path,
# perf.enabled=false 时该 state 不绑,这里不门控会让访问端点触发
# AttributeError → 500。跟 perception_router /metrics 端点的 perf 门控对齐。
if _settings.perf.enabled:
    app.include_router(observability_router)


@app.get("/health", include_in_schema=False)
async def health():
    """Health check endpoint — no auth required.

    Semantics: "not unhealthy", not "fully ready".
    - 503 {"status": "unhealthy"} — any node lifecycle is FAILED or STALLED
    - 503 {"status": "unknown"}   — the health check itself raised
    - 200 {"status": "ok"}        — everything else, including transient states
                                    REGISTERED / STARTING / STOPPED / PREREQ_MISSING
                                    and idle RUNNING_END (反应式节点等待上游事件时的常态)

    /health is intentionally permissive so probes don't flap during startup /
    shutdown / idle periods. For "is this node actually doing work" use the
    authenticated /api/monitor/nodes endpoint instead.

    Body intentionally contains only the high-level status — detailed node
    information lives behind the authenticated /api/monitor/nodes endpoints.
    """
    try:
        mon = get_monitor()
        for state in mon.iter_states():
            if state.lifecycle.is_unhealthy:
                return JSONResponse(status_code=503, content={"status": "unhealthy"})
        return {"status": "ok"}
    except Exception:
        logger.exception("health monitor check failed")
        return JSONResponse(status_code=503, content={"status": "unknown"})


# identity 注册 / 感知 metrics 等模板不会被住户裸链访问(前端代码不发裸 GET /
# 没公开链接),裸链时走下面 path-traversal 校验后的 is_file() 真文件分支即可。
# 这些模板自身不含 token 占位,泄漏也只是模板原文(无 token),无敏感信息。
# watch.html 仍单独走 RedirectResponse(token 注入在 /api/miot/watch),不在这里。


@functools.lru_cache(maxsize=1)
def _resolved_static_dirs() -> tuple[Path, Path]:
    """Resolve static_dir + 校验 root 一次缓存。spa_handler 每次 GET 都同时用
    `static_dir`(用作 (static_dir / full_path).resolve() 拼路径)和 `static_root`
    (用作 relative_to 越界校验)。两者一起缓存:
    1. computed_field 不 cache,每次 access 都重算 workspace_dir / "static",
       高 QPS 下重复无意义。
    2. 半套缓存(只 cache root 不 cache static_dir)让 reset hook 维护成本上升,
       未来加 cache 又忘进 RESET_HOOKS 时出现两路径不一致 silent bug。
    settings.RESET_HOOKS 注册了 cache_clear,reset_settings() 自动联动。
    **假设单 worker 进程**(start_server 末尾 workers!=1 守护)。
    """
    d = get_settings().directories.static_dir
    return d, d.resolve()


# 注:仅覆盖 RESET_HOOKS dict 内 entry,不能消除指向"老 wrapper 实例"的悬挂引用
# — importlib.reload 后某些 caller 闭包仍可能持有老 wrapper,reset_settings 不会
# 清那条老缓存。生产无 reload 路径,OK。
register_reset_hook(
    "miloco.main:_resolved_static_dirs", _resolved_static_dirs.cache_clear
)

@app.get("/{full_path:path}")
async def spa_handler(full_path: str, request: Request):
    """SPA route + 静态资源 catch-all。

    优先级：
      1. /api/* /health → 不归 SPA 管，返 404（让正常 API 路由处理）
      2. static_dir/<full_path> 是真实文件（如 /assets/main.js、/fonts/*.woff2）→ 返
      3. 否则视为 SPA 前端路由 → 返 index.html，并把字面量
         ``__MILOCO_INJECT_TOKEN_HERE__`` 替换为真 ``server.token``（生产期 token
         注入入口；浏览器 fetch 时从 ``window.__MILOCO_TOKEN__`` 读出加 Bearer）

    Trust model（重要）：
      本端点把 server.token 嵌进 HTML body 公开返回 —— 等价于"凡是能 GET / 的
      网络位置，都能拿到 token 后调任意 /api/* 端点（增删 rule、解绑米家、读全部
      perception_logs）"。settings 默认 ``host=127.0.0.1`` 仅本机可达；住户若改
      成 ``0.0.0.0`` 暴露 LAN，应自行评估 LAN 是否可信：
        · 私网 + 单管理员 → 默认信任（miloco 的目标场景）
        · 共享网络 / 公司宿舍 / 路由器穿透 → 应通过反代（带 TLS + 认证）对外暴露

      跟 watch.html 的 query-token 暴露面同口径（miot/router.py 上有对应注释），
      只是触达概率不同：watch.html 需要主动访问 watch URL，spa_handler 是 SPA 根
      路径 —— 住户开浏览器就会触发，因此把 trust 边界写在这里提醒运维。
    """
    # 规范化后判定:trailing slash / 大小写 / URL 编码 都不能让 /health/ /HEALTH /api%20
    # 等变体绕过短路 fallthrough 到 SPA token 注入分支,健康探针拿 200 + token-injected
    # HTML 误判服务正常。startswith 只在 norm 跟 norm + "/" 边界判,防 /apiX 误命中 api 分支。
    norm = full_path.rstrip("/").lower()
    if norm == "api" or norm.startswith("api/") or norm == "health":
        return Response(status_code=404, content="404 Not Found")

    # backend 自带模板 HTML watch.html 单独走重定向:
    # · watch.html 的 __MILOCO_TOKEN__ 占位必须由 /api/miot/watch 路由替换后才能用,
    #   否则浏览器拿到字面量字符串接 WS 会被拒。直链 /watch.html 重定向到正路由。
    # case-insensitive + trailing-slash 处理：
    # · macOS / 默认 NTFS 等 case-insensitive FS 上 `Watch.html` 会绕过精确匹配
    # · `/watch.html/` trailing slash 在 (static_dir / "watch.html/").resolve()
    #   会退回 watch.html，is_file() 返 True → 真文件分支吐出带 __MILOCO_TOKEN__
    #   占位的 raw HTML
    # 跟下面 SPA fallback 的 rstrip("/") 同口径。
    # NFC 归一化兜 macOS APFS 边界——攻击者构造 NFD 路径(组合重音 vs 预组合)
    # 经 .lower() 比较失败走真文件分支,APFS 文件系统会归一化命中 index.html 返
    # 带 __MILOCO_INJECT_TOKEN_HERE__ 占位的 raw HTML,前端拿到无 token 全 401。
    full_path_ci = unicodedata.normalize("NFC", full_path).lower().rstrip("/")
    if full_path_ci == "watch.html":
        # GET-only：catch-all 是 @app.get，POST /watch.html 自动 405 Allow: GET。
        # 把原 query string 拼回去(302 默认不带 query)——老链接 /watch.html?camera_id=X
        # 走这里时 watch.html JS 从 location.search 读 camera_id/channel,丢 query
        # 会让 iframe 起来后画面拉不动。**只透传白名单 keys**(camera_id / channel /
        # embedded / token):防老链接里携带未知字段扩散到 /api/miot/watch 路由,
        # 留给未来该路由副作用参数演进的安全余量。
        target = "/api/miot/watch"
        if request.url.query:
            # 白名单透传:camera_id / channel / embedded 是 watch.html 主消费路径;
            # token 给 watch.html resolveToken() 兜底用(backend 没注入时 dev 链接 /
            # 老链接走 query token),verify_token 中间件不读 query token,所以不会
            # 把 query token 当 backend 鉴权用,但 watch.html JS 自己仍 fallback 读它。
            allow = {"camera_id", "channel", "embedded", "token"}
            kept = [(k, v) for k, v in parse_qsl(request.url.query) if k in allow]
            if kept:
                target = f"{target}?{urlencode(kept)}"
        return RedirectResponse(url=target, status_code=302)
    static_dir, static_root = _resolved_static_dirs()
    # /index.html 直链也得走 token 注入路径——若把它当真文件 FileResponse 出去，
    # 浏览器拿到的是带占位 __MILOCO_INJECT_TOKEN_HERE__ 的版本，apiFetch 会无 token。
    # 同理 / 由于 full_path="" 自然不会命中真文件分支，这里只需补 index.html。
    # 用 .lower().rstrip("/") 处理：
    # · macOS / 默认 NTFS 等 case-insensitive FS 上 `/INDEX.html` 命中 index.html
    #   的真文件分支会返带 __MILOCO_INJECT_TOKEN_HERE__ 占位的 raw HTML（前端
    #   resolveToken 兜回空串，所有 fetch 401 → 整页打不开）。跟 watch.html
    #   case-insensitive 防御同口径。
    # · `/index.html/` 末尾斜杠：starlette 不规范化，Path("index.html/").resolve()
    #   退回 index.html → is_file() 命中 → 同样返未替换 token 的版本。
    if full_path and full_path_ci != "index.html":
        # 防 path traversal：starlette 不会 normalize URL-encoded `..%2F`，
        # 直接 (static_dir / "../etc/passwd") + is_file() 会越界读宿主任意文件。
        # resolve() 后用 relative_to() 校验真实路径仍落在 static_dir 内。
        try:
            real_file = (static_dir / full_path).resolve()
            real_file.relative_to(static_root)
        except (ValueError, OSError):
            # 真 path-traversal(..%2Fetc/passwd 等)→ 不在 static_dir 内 → 404。
            # warning 级 log 让运维事后审计能发现扫描器;截断 200 字符防灌爆 log,
            # %r repr 转义防 log injection。
            logger.warning("spa_handler path-traversal blocked: %r", full_path[:200])
            return Response(status_code=404, content="404 Not Found")
        if real_file.is_file():
            # Cache-Control:vite 输出 /assets/*.{js,css,woff2} 都带 hash,内容
            # 改 hash 必变 → 强 cache 1 年 + immutable;/vendor/jmuxer.min.js 跟
            # /fonts/* 不带 hash 但实质 immutable,1 天软 cache 兼顾换库时的及时
            # 生效。其它真文件(原 html 模板等)不加 cache header,走默认协商缓存。
            # 按 **请求路径 full_path** 分类(就是 URL,与文件系统布局解耦):缓存策略
            # 跟 URL 语义绑定,跟磁盘上具体怎么放无关。
            headers: dict[str, str] = {}
            if full_path.startswith("assets/"):
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif full_path.startswith("vendor/") or full_path.startswith("fonts/"):
                headers["Cache-Control"] = "public, max-age=86400"
            return FileResponse(str(real_file), headers=headers or None)
        # 路径合法但不是真文件 → 同样 404，不 fallthrough。
        # 本应用不用 url-based routing（family-ui 是 tab state 单页，URL 始终 /），
        # 因此除 / 和 /index.html 外的"任意子路径"都是噪声（扫描器探测 /admin/login
        # /wp-admin /.env 等）。返 404 让扫描器不能稳拿 token-injected HTML。
        # 未来若需要 react-router 等 url 路由，需把已知前端 path 加白名单分支。
        return Response(status_code=404, content="404 Not Found")

    index_file = static_dir / "index.html"
    if not index_file.exists():
        return Response(status_code=404, content="404 Not Found")

    # token 空兜底:跟 miot/router.py::watch_page 同口径,返 503 + 中文错误,而不是
    # 返一个含空 token 占位的 SPA — 后者前端拿到的 __MILOCO_INJECT_TOKEN_HERE__
    # 替换为空串,所有 apiFetch 静默 401 死循环,运维更难定位"是 token 没配"。
    token = get_settings().server.token
    if not token:
        return HTMLResponse(
            content="<h1>503: server.token 未配置,无法启动 web 页</h1>",
            status_code=503,
        )

    template = index_file.read_text(encoding="utf-8")
    # 用 unique placeholder __MILOCO_INJECT_TOKEN_HERE__ 而不是 __MILOCO_TOKEN__：
    # 后者在 index.html 注释 / 变量名里也会出现，粗暴 string-replace 会破坏 JS。
    # token 走 escape_for_js_string(JSON encode + </script> 防御),共享 helper 跟
    # miot/router.py::watch_page 同口径。watch.html 同名占位也走它。
    template = template.replace(
        "__MILOCO_INJECT_TOKEN_HERE__", escape_for_js_string(token)
    )
    # Cache-Control: no-store —— HTML body 里嵌着明文 server.token，不能被浏览器
    # disk cache / 反代 / CDN 缓存。token 长期不轮转，缓存把"知道访问 / 才能拿
    # token"的窗口扩到"任何能读 cache 的人"（共用电脑 / 浏览器 history-cache 等）。
    # FileResponse 的 hashed asset 不动，那些不含 token，强 cache 是想要的。
    return HTMLResponse(template, headers={"Cache-Control": "no-store"})


def start_server():
    """Start server with singleton check."""

    from miloco.utils.bootstrap import bootstrap
    from miloco.utils.uvicorn import get_uvicorn_config

    bootstrap("server")

    uv_config = get_uvicorn_config()

    # 强约束 workers=1:miloco 后端是住户家用单实例服务,横向扩展应在反代层做
    # (nginx 多上游 / haproxy);单进程是设计选择不是 todo。具体踩点:
    #   - perception engine / watchdog / resource_monitor 是单实例 daemon,
    #     多 worker fork 后会建多份 daemon,撞 db lock + double-bind 监听端口。
    #   - reset_settings / register_reset_hook 只在调用 worker 内生效,运行时
    #     改 settings 后 N-1 个 worker 仍持老缓存,住户随机看旧值。
    # workers>1 直接拒,而不是装作能跑;住户/运维要扩展请走反代层而非进程级。
    workers = uv_config.get("workers", 1)
    if workers and workers != 1:
        raise NotImplementedError(
            f"workers={workers} 不支持:miloco 后端是单实例 daemon(感知引擎 / "
            "监控守护 / 配置缓存均假设单进程),横向扩展请在反代层做(nginx 多上游 "
            "/ haproxy 而非 uvicorn workers)。"
        )

    logger.info("Starting Miloco backend...")

    uvicorn.run(app, **uv_config)


if __name__ == "__main__":
    start_server()
