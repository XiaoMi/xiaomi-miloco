# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""有意义感知事件 API.

独立于 perception_router(events 是独立功能,不属于 perception 子领域),
挂在 `/api/events` 前缀下.

Endpoints:
- `GET /api/events`                              — list_events
- `GET /api/events/{event_id}/clip/{device_id}`  — locate_clip + FileResponse(Range/206)
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
        # 类型收窄
        assert path is not None and media_type is not None and timestamp_ms is not None
        # 下载文件名按事件部署时区时间命名:用户保存后一眼能看出"哪天发生的什么事件"
        # (比 event_id 前 8 位 UUID 字符串友好得多).格式 `clip-YYYY-MM-DD-HH-MM-SS.ext`:
        # - 全连字符避免 `:` 在 Windows 文件名非法
        # - 同毫秒多事件只发生在落盘冲突时(单 device 同 event 只一个文件,不冲突)
        # - 走 deploy_timezone() 与感知推送「时间」字段同源,host TZ≠部署时区时不错标
        # content_disposition_type="inline":页面 <audio>/<video> 仍 inline 播放,
        # 浏览器"另存为"时把这个 filename 当默认下载名.
        from datetime import datetime

        from miloco.utils.time_utils import deploy_timezone
        local_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=deploy_timezone())
        download_name = (
            f"clip-{local_dt.strftime('%Y-%m-%d-%H-%M-%S')}.{path.suffix[1:]}"
        )
        return FileResponse(
            path=path,
            media_type=media_type,
            filename=download_name,
            content_disposition_type="inline",
        )
    if status == "gone":
        raise HTTPException(message="clip expired", status_code=410)
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
