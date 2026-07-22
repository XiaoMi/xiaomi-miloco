"""
Perception API controller.

Provides endpoints for realtime engine control, active perception queries,
perception log retrieval, and device listing.
"""

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from miloco.manager import get_manager
from miloco.middleware import verify_token, verify_token_query_fallback
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
    # 落盘失败则 fail loud、不继续，避免用户以为已保存而实际重启后复位。
    if not set_perception_enabled(manager.kv_repo, True):
        raise HTTPException(
            message="failed to persist perception resume intent",
            status_code=500,
        )
    await manager.perception_service.start_engine()
    return NormalResponse(code=0, message="Perception engine started")


@router.post(
    "/engine/stop",
    summary="Stop realtime perception engine",
    dependencies=[Depends(verify_token)],
)
async def stop_engine():
    # 用户主动「让它休息」：先落盘意图=关（重启后开机门控据此跳过自动启动）。
    # 落盘失败则 fail loud、不继续 —— 否则用户以为已暂停，重启后引擎仍被拉起继续烧 token。
    if not set_perception_enabled(manager.kv_repo, False):
        raise HTTPException(
            message="failed to persist perception pause intent",
            status_code=500,
        )
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
    "/on-demand-logs",
    summary="Query on-demand perception query logs",
    dependencies=[Depends(verify_token)],
)
async def query_on_demand_logs(
    limit: int | None = Query(None, ge=1, le=1000, description="Max entries; omit for unlimited"),
    since: int | None = Query(None, description="Unix ms lower bound (inclusive)"),
    before: int | None = Query(None, description="Unix ms upper bound (exclusive)"),
    before_id: str | None = Query(None, description="Compound cursor tiebreaker (used with before)"),
):
    data = manager.perception_service.query_on_demand_logs(
        since_ms=since, before_ms=before, before_id=before_id, limit=limit
    )
    return NormalResponse(code=0, message="ok", data=data)


@router.get(
    "/on-demand-logs/{log_id}/clip/{device_id}",
    summary="Get on-demand query clip (omni 看到的字节级 mp4/m4a)",
    dependencies=[Depends(verify_token_query_fallback)],
)
async def get_on_demand_clip(log_id: str, device_id: str) -> FileResponse:
    """Serve the clip file for an on-demand query log entry."""
    from miloco.perception.snapshot_writer import get_snapshot_root, region_slug

    row = manager.perception_service.get_on_demand_log(log_id)
    if row is None:
        raise HTTPException(message="not found", status_code=404)
    if device_id not in row["sources"]:
        raise HTTPException(message="not found", status_code=404)

    device_dir = get_snapshot_root() / log_id / region_slug(device_id)
    for filename, media_type in [("clip.mp4", "video/mp4"), ("clip.m4a", "audio/mp4")]:
        path = device_dir / filename
        if path.exists():
            from datetime import datetime

            from miloco.utils.time_utils import deploy_timezone

            local_dt = datetime.fromtimestamp(row["timestamp"] / 1000, tz=deploy_timezone())
            download_name = f"clip-{local_dt.strftime('%Y-%m-%d-%H-%M-%S')}.{filename.split('.')[1]}"
            return FileResponse(
                path=path, media_type=media_type,
                filename=download_name, content_disposition_type="inline",
            )
    raise HTTPException(message="clip expired", status_code=410)


class OnDemandFeedbackBody(BaseModel):
    error_types: list[str] = Field(default_factory=list)
    feedback_text: str = Field(default="")


@router.post(
    "/on-demand-logs/{log_id}/feedback",
    summary="Submit feedback for an on-demand query",
    dependencies=[Depends(verify_token)],
)
async def submit_on_demand_feedback(log_id: str, body: OnDemandFeedbackBody):
    """Build a feedback pack for an on-demand query (trace + clips + user annotation)."""
    import asyncio

    from miloco.admin.feedback_pack import build_on_demand_feedback_pack

    row = manager.perception_service.get_on_demand_log(log_id)
    if row is None:
        raise HTTPException(message="not found", status_code=404)

    pack_path, pack_size = await asyncio.to_thread(
        build_on_demand_feedback_pack,
        log_id=log_id,
        row=row,
        error_types=body.error_types,
        feedback_text=body.feedback_text,
    )
    return NormalResponse(
        code=0, message="ok",
        data={
            "log_id": log_id,
            "pack_path": str(pack_path),
            "pack_size_bytes": pack_size,
        },
    )


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


