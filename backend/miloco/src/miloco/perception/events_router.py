# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""有意义感知事件 API.

独立于 perception_router(events 是独立功能,不属于 perception 子领域),
挂在 `/api/events` 前缀下.

Endpoints:
- `GET /api/events`                              — list_events
- `GET /api/events/{event_id}/clip/{device_id}`  — locate_clip + FileResponse(Range/206)
- `GET /api/events/{event_id}/ref/{device_id}`   — locate_ref + FileResponse(全景参考帧)
- `GET /api/events/{event_id}/crop/{device_id}`  — read_crop_meta(crop 区域坐标,画框用)
- `GET /api/events/stream`                       — SSE
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from miloco.manager import get_manager
from miloco.middleware import verify_token, verify_token_query_fallback
from miloco.middleware.exceptions import HTTPException
from miloco.perception.events_service import EventsService
from miloco.perception.schema import EventListResponse
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["Events"])


def get_events_service() -> EventsService:
    """FastAPI dependency:返回 events_service 单例(对齐 mgr.events_service)."""
    return get_manager().events_service


@router.get(
    "",
    summary="List meaningful events",
    response_model=NormalResponse,
    dependencies=[Depends(verify_token)],
)
async def list_events(
    since: int = Query(0, ge=0, description="Unix ms UTC, 含, timestamp >= since"),
    before: int | None = Query(
        None, ge=0, description="Unix ms UTC, 不含, timestamp < before;默认当前时间"
    ),
    limit: int = Query(50, ge=1, le=200, description="每页条数,上限 200"),
    offset: int = Query(0, ge=0, description="分页偏移"),
    svc: EventsService = Depends(get_events_service),
):
    """拉取有意义事件列表,按 timestamp DESC 排序.

    响应不含 payload_json / schema_version / created_at(都是后端内部字段).
    前端按 "返回长度 < limit" 判断到尾,无 has_more 字段.
    """
    events = await svc.list_events(
        since=since, before=before, limit=limit, offset=offset
    )
    return NormalResponse(
        code=0, message="ok", data=EventListResponse(events=events)
    )


@router.get(
    "/{event_id}/clip/{device_id}",
    summary="Get event clip(omni 看到的字节级 mp4/m4a)",
    dependencies=[Depends(verify_token_query_fallback)],
)
async def get_event_clip(
    event_id: str,
    device_id: str,
    svc: EventsService = Depends(get_events_service),
) -> FileResponse:
    """拉取指定 event × device 的 clip 文件,FileResponse 走 sendfile + Range/206.

    字节级 = omni 实际上传给 LLM 的内容(零重编):
    - 视频路径:H264 + AAC mp4,media_type=video/mp4
    - audio-only 路径:仅 AAC m4a (ipod muxer),media_type=audio/mp4

    用 FileResponse(而非 Response(content=read_bytes())):
    - sendfile 零拷贝,不把整段 mp4 读进 Python 内存(避免阻塞 async loop)
    - Starlette 自动响应 Range,返 206 Partial Content,<video> scrubber 可正常 seek
    - 自动设 Content-Length / Last-Modified
    - 设 `Content-Disposition: inline; filename=clip-YYYY-MM-DD-HH-MM-SS.{ext}`:
      inline 让 `<audio>`/`<video>` 仍可页面内播放(不触发下载弹窗);
      filename 按事件本地时间命名,用户右键"另存为"默认带正确后缀 + 一眼看出
      "哪天发生的"(否则 URL `/clip/{did}` 无扩展名,用户保存的文件名是 device_id
      字符串无后缀,会被误以为是 raw PCM / 不认识的格式).

    返回:
    - 200 (整段) / 206 (Range):文件存在,media_type 由后缀决定
    - 404:event 不存在 / device_id 不在该 event 的 device_ids 内
    - 410:文件已被 cleanup 清理(clip 已过期);前端用此触发降级 UI
    """
    status, path, media_type, timestamp_ms = await svc.locate_clip(
        event_id, device_id
    )
    if status == "found":
        assert path is not None and media_type is not None and timestamp_ms is not None
        from miloco.perception.snapshot_writer import clip_download_name

        return FileResponse(
            path=path,
            media_type=media_type,
            filename=clip_download_name(timestamp_ms, path.suffix[1:]),
            content_disposition_type="inline",
        )
    if status == "gone":
        raise HTTPException(message="clip expired", status_code=410)
    raise HTTPException(message="not found", status_code=404)


@router.get(
    "/{event_id}/ref/{device_id}",
    summary="Get event 全景参考帧(Smart Crop:crop 视频同附的整帧上下文)",
    dependencies=[Depends(verify_token_query_fallback)],
)
async def get_event_ref(
    event_id: str,
    device_id: str,
    svc: EventsService = Depends(get_events_service),
) -> FileResponse:
    """拉取指定 event × device 的全景参考帧 ref.jpg(image/jpeg,inline 展示).

    仅 Smart Crop 事件落有此文件(字节级 = omni 收到的参考帧);前端应据 list 的
    has_ref 门控请求.用 FileResponse 走 sendfile,不把 JPEG 读进内存.

    返回:
    - 200:ref.jpg 存在
    - 404:event 不存在 / device_id 不在 device_ids 内
    - 410:event 合法但无 ref.jpg(非 crop 事件 / 已被 cleanup 清)
    """
    status, path, timestamp_ms = await svc.locate_ref(event_id, device_id)
    if status == "found":
        assert path is not None and timestamp_ms is not None
        # filename 与 clip 端点同源同格式(见 get_event_clip 注释):不设时"另存为"会拿
        # URL 末段 device_id 当名字、且没后缀;设了才能一眼看出是哪天哪个事件的参考帧。
        from datetime import datetime

        from miloco.utils.time_utils import deploy_timezone
        local_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=deploy_timezone())
        return FileResponse(
            path=path,
            media_type="image/jpeg",
            filename=f"ref-{local_dt.strftime('%Y-%m-%d-%H-%M-%S')}.jpg",
            content_disposition_type="inline",
        )
    if status == "gone":
        raise HTTPException(message="ref frame not available", status_code=410)
    raise HTTPException(message="not found", status_code=404)


@router.get(
    "/{event_id}/crop/{device_id}",
    summary="Get event Smart Crop 元数据(crop 区域在全景帧中的位置)",
    response_model=NormalResponse,
    dependencies=[Depends(verify_token)],
)
async def get_event_crop_meta(
    event_id: str,
    device_id: str,
    svc: EventsService = Depends(get_events_service),
):
    """拉取指定 event × device 的 crop 区域坐标,供前端在参考帧上画框.

    坐标源自 omni_trace(calls[].crop),与 ref.jpg 同一全景像素空间.

    返回:
    - 200:{region_xyxy, frame_size_wh, crop_short_edge}
    - 404:event 不存在 / device_id 不在 device_ids 内
    - 410:非 crop 事件 / trace 已被 cleanup 清 / trace 损坏 / crop 字段形状不合法 ——
      前端把这台 device 视作「本次没裁切」,不渲染参考卡
    """
    status, crop = await svc.read_crop_meta(event_id, device_id)
    if status == "found":
        assert crop is not None
        return NormalResponse(code=0, message="ok", data=crop)
    if status == "gone":
        raise HTTPException(message="crop meta not available", status_code=410)
    raise HTTPException(message="not found", status_code=404)


@router.get(
    "/stream",
    summary="SSE stream of new meaningful events",
    dependencies=[Depends(verify_token_query_fallback)],
)
async def events_stream():
    """SSE 实时推送新事件.复用 pipeline.subscribe_sse / _publish 三件套.

    鉴权:支持 Authorization header 或 ?token=... query 参数(EventSource 无法传 header).
    Generator 内过滤 event_type == "meaningful_event"(避免 metric / preview 污染本路).
    客户端断开时 CancelledError + finally unsubscribe 清理.

    与 `/api/perception/metrics/stream` 共享同一组 pipeline._sse_subscribers 队列,
    新增事件会广播到所有订阅者(本路过滤后只见 meaningful_event).
    """
    pipeline = get_manager().perception_service._pipeline
    q = pipeline.subscribe_sse()

    async def event_generator():
        try:
            while True:
                event_type, data = await q.get()
                if event_type != "meaningful_event":
                    continue  # 过滤其它类型(metric / preview)
                yield {"event": "new_event", "data": json.dumps(data, ensure_ascii=False)}
        except asyncio.CancelledError:
            pass
        finally:
            pipeline.unsubscribe_sse(q)

    return EventSourceResponse(event_generator())
