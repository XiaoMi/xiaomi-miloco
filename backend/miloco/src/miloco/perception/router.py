"""
Perception API controller.

Provides endpoints for realtime engine control, active perception queries,
perception log retrieval, and device listing.
"""

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query

from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.middleware.exceptions import HTTPException
from miloco.perception.engine_state import set_perception_enabled
from miloco.perception.schema import OnDemandPerceptionRequest
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/perception", tags=["Perception"])

manager = get_manager()


def _require_engine_ready():
    """Dependency guard: returns 503 when perception engine is not ready."""
    status = manager.perception_service.engine_status()
    if not status.engine.ready:
        raise HTTPException(
            message=f"perception_unavailable: {status.engine.status}",
            status_code=503,
        )


@router.get(
    "/engine/status",
    summary="Get perception engine status",
    dependencies=[Depends(verify_token)],
)
async def get_engine_status():
    status = manager.perception_service.engine_status()
    return NormalResponse(code=0, message="ok", data=status)


@router.post(
    "/engine/start",
    summary="Start realtime perception engine",
    dependencies=[Depends(verify_token)],
)
async def start_engine():
    # 用户主动「唤醒」：先落盘意图=开再启动。持久化只挂在这条用户动作上，
    # 系统路径(开机/重新授权)不走本端点，故不会误翻此 flag。
    set_perception_enabled(manager.kv_repo, True)
    await manager.perception_service.start_engine()
    return NormalResponse(code=0, message="Perception engine started")


@router.post(
    "/engine/stop",
    summary="Stop realtime perception engine",
    dependencies=[Depends(verify_token)],
)
async def stop_engine():
    # 用户主动「让它休息」：落盘意图=关，重启后开机门控据此跳过自动启动。
    set_perception_enabled(manager.kv_repo, False)
    await manager.perception_service.stop_engine()
    return NormalResponse(code=0, message="Perception engine stopped")


@router.post(
    "/clear",
    summary="Clear all device stream buffers",
    dependencies=[Depends(verify_token)],
)
async def clear_buffers():
    manager.perception_service.clear_buffers()
    return NormalResponse(code=0, message="All perception buffers cleared")


@router.post(
    "/perceive",
    summary="Active perception query",
    dependencies=[Depends(verify_token), Depends(_require_engine_ready)],
)
async def on_demand_perceive(request: OnDemandPerceptionRequest):
    result = await manager.perception_service.on_demand_perceive(request)
    return NormalResponse(code=0, message="ok", data=result)


@router.get(
    "/logs",
    summary="Query perception logs",
    dependencies=[Depends(verify_token)],
)
async def query_logs(
    limit: int | None = Query(None, ge=1, le=1000, description="Max entries; omit for unlimited"),
    after: str | None = Query(None, description="ISO 8601 timestamp cursor"),
    before: str | None = Query(
        None,
        description="ISO 8601 upper-bound; combined with ``after`` allows windowed pagination",
    ),
    since: str | None = Query(None, description="Relative time, e.g. '1h', '30m', '2h30m'"),
):
    data = manager.perception_service.query_logs(
        after=after, before=before, since=since, limit=limit
    )
    return NormalResponse(code=0, message="ok", data=data)


@router.get(
    "/devices",
    summary="List perception-capable devices",
    dependencies=[Depends(verify_token)],
)
async def list_devices():
    devices = await manager.perception_service.get_devices()
    return NormalResponse(
        code=0,
        message="ok",
        data=[asdict(d) for d in devices],
    )


