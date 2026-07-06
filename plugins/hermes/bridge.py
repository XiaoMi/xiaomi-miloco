import asyncio
import logging
import threading
import uuid

from aiohttp import web

from . import trace
from .agent_runner import AgentSessionPool

__all__ = [
    "register_bridge",
]

logger = logging.getLogger(__name__)

_BRIDGE_HOST = "127.0.0.1"
_BRIDGE_PORT = 18789


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
    result = agent.run_conversation(message)
    if isinstance(result, dict):
        return {
            "status": result.get("status", "ok"),
            "error": result.get("error"),
        }
    return {"status": "ok", "error": None}


def _agent_turn_blocking(pool, session_key, message, run_id, extra_system_prompt):
    agent = pool.get_or_create(
        session_key=session_key,
        extra_system_prompt=extra_system_prompt,
    )
    return _run_turn_sync(agent, message, run_id, extra_system_prompt)


async def _handle_agent(payload):
    message = payload.get("message", "")
    session_key = payload.get("sessionKey") or "main"
    trace_id = payload.get("traceId")
    extra_system_prompt = payload.get("extraSystemPrompt")
    run_id = payload.get("idempotencyKey") or _new_run_id()
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
                return web.json_response(_fail(1401, "unauthorized"), status=401)
        body = await request.json()
        action = body.get("action") if isinstance(body, dict) else None
        if not action:
            return web.json_response(_fail(1001, "Missing action field"), status=400)
        payload = body.get("payload") or {}
        if action == "agent":
            result = await _handle_agent(payload)
        elif action == "get_trace":
            result = _handle_get_trace(payload)
        else:
            return web.json_response(
                _fail(2001, "Action '{}' not found".format(action)),
                status=404,
            )
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


_BRIDGE_STARTED = False


def register_bridge(ctx, plugin_cfg=None):
    global _BRIDGE_STARTED
    if _BRIDGE_STARTED:
        return
    cfg = plugin_cfg or {}
    host = cfg.get("bridge_host", _BRIDGE_HOST)
    port = cfg.get("bridge_port", _BRIDGE_PORT)
    auth_token = cfg.get("bridge_auth_token", "")
    app = build_app(ctx, auth_token)

    def target():
        global _BRIDGE_STARTED
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_serve(app, host, port))
        finally:
            loop.close()
            _BRIDGE_STARTED = False

    thread = threading.Thread(target=target, name="miloco-bridge", daemon=True)
    thread.start()
    _BRIDGE_STARTED = True
