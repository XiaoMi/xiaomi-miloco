"""Agent webhook fallback — POST /miloco/webhook 路由。

当 adapter 未加载时 dispatcher 走 webhook 通路，后端自己处理这个路由。
把入站请求转发给加载的 adapter（如果有的话），否则返回 503。
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from miloco.agent_platform import get_adapter
from miloco.agent_platform.base import TurnContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/miloco", tags=["Agent Webhook"])


@router.post("/webhook")
async def agent_webhook(request: Request) -> JSONResponse:
    """Agent webhook 入站路由。

    适配器未加载时 dispatcher 会将事件 POST 到这个 URL。
    本路由把请求转发给 adapter.send_turn()，不管当前加载的是哪个 adapter。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid JSON"}, status_code=400)

    action = body.get("action", "agent")
    payload = body.get("payload", body)

    if action == "agent":
        msg = payload.get("message", "")
        session_key = payload.get("sessionKey", "agent:main:miloco")
        lane = payload.get("lane", "miloco-interactive")
        wait_ms = payload.get("timeoutMs", 180_000)

        adapter = get_adapter()
        ctx = TurnContext(
            text=msg,
            session_key=session_key,
            lane=lane,
            trace_id=payload.get("traceId", ""),
            wait_timeout_ms=wait_ms,
            profile="full",
            extra={"delivery": payload if payload.get("deliver") else {}},
        )
        result = await adapter.send_turn(ctx)
        return JSONResponse({
            "status": result.status,
            "run_id": result.run_id,
            "rtt_ms": result.rtt_ms,
        })

    return JSONResponse({"status": "error", "error": f"unknown action: {action}"}, status_code=400)
