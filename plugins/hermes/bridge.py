import asyncio
import logging
import threading
import uuid

from aiohttp import web

from . import trace
from .agent_runner import AgentSessionPool
from .config import read_config_dict

logger = logging.getLogger(__name__)


def _ok(data=None):
    payload = {"code": 0, "message": "ok"}
    if data is not None:
        payload["data"] = data
    return payload


def _fail(code, message):
    return {"code": code, "message": message}


def _new_run_id():
    return uuid.uuid4().hex


def _run_turn_sync(agent, message, run_id, extra_system_prompt):
    try:
        result = agent.run_turn(
            message,
            run_id=run_id,
            extra_system_prompt=extra_system_prompt,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    if isinstance(result, dict):
        return {
            "status": result.get("status", "ok"),
            "error": result.get("error"),
        }
    return {"status": "ok", "error": None}


def _agent_turn_blocking(
    pool, session_key, message, run_id, extra_system_prompt, cfg
):
    agent = pool.get_or_create(
        session_key=session_key,
        model=cfg.get("omni_model", ""),
        api_key=cfg.get("omni_api_key", ""),
        base_url=cfg.get("omni_base_url", ""),
        provider=cfg.get("omni_provider", "openai"),
        extra_system_prompt=extra_system_prompt,
    )
    return _run_turn_sync(agent, message, run_id, extra_system_prompt)


async def _handle_agent(ctx, payload):
    message = payload.get("message", "")
    session_key = payload.get("sessionKey") or "main"
    trace_id = payload.get("traceId")
    extra_system_prompt = payload.get("extraSystemPrompt")
    run_id = payload.get("idempotencyKey") or _new_run_id()
    cfg = read_config_dict()
    pool = AgentSessionPool.instance()
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        pool.executor,
        _agent_turn_blocking,
        pool,
        session_key,
        message,
        run_id,
        extra_system_prompt,
        cfg,
    )
    if trace_id:
        trace.register_trace_link(run_id, trace_id)
    return {
        "runId": run_id,
        "status": result["status"],
        "error": result.get("error"),
    }


def _handle_get_trace(payload):
    run_id = payload.get("runId")
    if not run_id:
        return {"status": "error", "message": "runId required"}
    status = trace.get_turn_status(run_id)
    if status != "done":
        return {"status": status}
    meta = trace.pop_done_turn(run_id)
    if not meta:
        return {"status": "unknown"}
    result = {"status": "done"}
    result.update(meta)
    return result


def _make_handler(ctx, auth_token):
    async def handle(request):
        if auth_token:
            header = request.headers.get("Authorization", "")
            if header != "Bearer {}".format(auth_token):
                return web.json_response(
                    _fail(1401, "unauthorized"), status=401
                )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                _fail(1001, "Invalid JSON body"), status=400
            )
        action = body.get("action") if isinstance(body, dict) else None
        if not action:
            return web.json_response(
                _fail(1001, "Missing action field"), status=400
            )
        payload = body.get("payload") or {}
        try:
            if action == "agent":
                result = await _handle_agent(ctx, payload)
            elif action == "get_trace":
                result = _handle_get_trace(payload)
            else:
                return web.json_response(
                    _fail(2001, "Action '{}' not found".format(action)),
                    status=404,
                )
        except Exception as exc:
            return web.json_response(_fail(3000, str(exc)), status=500)
        return web.json_response(_ok(result), status=200)

    return handle


def build_app(ctx, auth_token):
    app = web.Application()
    app.router.add_post("/miloco/webhook", _make_handler(ctx, auth_token))
    return app


async def _serve(app, host, port):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("bridge listening on %s:%s", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def _start_server_thread(app, host, port):
    def target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve(app, host, port))
        except Exception:
            logger.exception("bridge server failed")
        finally:
            loop.close()

    thread = threading.Thread(
        target=target, name="miloco-bridge", daemon=True
    )
    thread.start()
    return thread


def register_bridge(ctx):
    cfg = read_config_dict()
    host = cfg.get("bridge_host", "127.0.0.1")
    port = cfg.get("bridge_port", 18789)
    auth_token = cfg.get("bridge_auth_token", "")
    app = build_app(ctx, auth_token)
    _start_server_thread(app, host, port)
