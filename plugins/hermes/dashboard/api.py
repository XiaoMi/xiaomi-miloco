import asyncio
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hermes import trace
from hermes.agent_runner import AgentSessionPool

__all__ = ["router"]

router = APIRouter()


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
    result = agent.run_turn(
        message,
        run_id=run_id,
        extra_system_prompt=extra_system_prompt,
    )
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


@router.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    action = body.get("action") if isinstance(body, dict) else None
    if not action:
        return JSONResponse(_fail(1001, "Missing action field"), status_code=400)
    payload = body.get("payload") or {}
    if action == "agent":
        result = await _handle_agent(payload)
    elif action == "get_trace":
        result = _handle_get_trace(payload)
    else:
        return JSONResponse(
            _fail(2001, "Action '{}' not found".format(action)),
            status_code=404,
        )
    return JSONResponse(_ok(result), status_code=200)
