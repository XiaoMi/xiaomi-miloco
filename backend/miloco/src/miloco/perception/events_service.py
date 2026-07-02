# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""有意义事件 Service 层.

通过 `mgr.events_service` lazy 单例持有(对齐 register_session_manager 套路).
对接两个 endpoint:
- `GET /api/events`         → list_events
- `GET /api/events/{event_id}/clip/{device_id}` → locate_clip → FileResponse
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from miloco.perception.schema import MeaningfulEvent
from miloco.perception.snapshot_writer import get_snapshot_root, region_slug
from miloco.utils.paths import miloco_home

if TYPE_CHECKING:
    from miloco.database.meaningful_events_dao import MeaningfulEventDao

logger = logging.getLogger(__name__)

SnapshotStatus = Literal["found", "gone", "not_found"]

# 视频路径产物 clip.mp4 (H264+AAC);audio-only 路径产物 clip.m4a (仅 AAC,ipod muxer).
# 探测顺序:先 mp4 后 m4a,先找到的优先返回。
_CLIP_CANDIDATES = ("clip.mp4", "clip.m4a")
_MEDIA_TYPE_BY_SUFFIX = {".mp4": "video/mp4", ".m4a": "audio/mp4"}


class EventsService:
    """有意义事件读取 Service.

    本 Service 只负责读取 + 解码 + 校验,不负责写入(写入在 client.py 的
    _persist_meaningful_event 内,通过 dao 直写).
    """

    def __init__(self, dao: "MeaningfulEventDao"):
        self._dao = dao

    async def list_events(
        self,
        *,
        since: int = 0,
        before: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MeaningfulEvent]:
        """拉取事件列表,按 timestamp DESC 排序.

        Args:
            since: Unix ms UTC,含,timestamp ≥ since(默认 0)
            before: Unix ms UTC,不含,timestamp < before(默认当前时间)
            limit: 每页条数 [1, 200]
            offset: 分页偏移

        Returns:
            list[MeaningfulEvent](Pydantic 模型,不含 payload_json / created_at / schema_version)
        """
        if before is None:
            before = int(time.time() * 1000)

        rows = self._dao.query(
            since_ms=since, before_ms=before, limit=limit, offset=offset
        )
        snapshot_root = get_snapshot_root()
        feedback_index = self._build_feedback_index()
        return [self._row_to_event(row, snapshot_root, feedback_index) for row in rows]

    async def locate_clip(
        self, event_id: str, device_id: str
    ) -> tuple[SnapshotStatus, Path | None, str | None, int | None]:
        """定位指定 event × device 的 clip 文件路径(字节级 = omni 看到的).

        路由层用这个返 FileResponse(path, media_type=...),让 Starlette 走 sendfile +
        Range/206 流式响应 — 避免把整段 mp4 读进内存阻塞 event loop,且支持 <video>
        scrubber 的 seek.

        探测顺序:先 clip.mp4 (视频路径产物),后 clip.m4a (audio-only 路径产物).
        对应 media_type 由 _MEDIA_TYPE_BY_SUFFIX 决定.

        Args:
            event_id: UUID
            device_id: 必须在 event.device_ids 列表内

        Returns:
            (status, path, media_type, timestamp_ms):
            - ("found", Path, "video/mp4" | "audio/mp4", int):文件存在,timestamp_ms 是
              meaningful_events.timestamp(Unix ms,用于路由层拼下载文件名按事件时间命名)
            - ("gone", None, None, None):event 存在且 device_id 合法,但文件已被 cleanup 清掉(410)
            - ("not_found", None, None, None):event 不存在 / device_id 不在 device_ids 内(404)
        """
        row = self._dao.get_by_id(event_id)
        if row is None:
            return ("not_found", None, None, None)
        if device_id not in row["device_ids"]:
            return ("not_found", None, None, None)

        device_dir = get_snapshot_root() / event_id / region_slug(device_id)
        for filename in _CLIP_CANDIDATES:
            path = device_dir / filename
            if path.exists():
                return ("found", path, _MEDIA_TYPE_BY_SUFFIX[path.suffix], row["timestamp"])
        # event metadata 在表里,但文件已被 cleanup 清掉(或写前预检跳过没落)
        return ("gone", None, None, None)

    @staticmethod
    def _probe_clip_kind(snapshot_root: Path, event_id: str, device_ids: list[str]) -> str | None:
        """Stat 落盘文件后缀,推断 clip 容器类型.

        多 device 时取第一个找到 clip 文件的 device 的 kind(同次推理:同 batch
        要么全走 video 路径,要么全走 audio-only 路径,_is_audio_only 是 batch 级
        共识 — 见 prompt_builder._is_audio_only;所以多 device 间 kind 一致,
        取第一个有效结果即可).

        Returns: "mp4" / "m4a" / None(未落盘 / 已被 cleanup 清掉).
        """
        if not device_ids:
            return None
        for did in device_ids:
            device_dir = snapshot_root / event_id / region_slug(did)
            for filename, kind in (("clip.mp4", "mp4"), ("clip.m4a", "m4a")):
                if (device_dir / filename).exists():
                    return kind
        return None

    _UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    @staticmethod
    def _build_feedback_index() -> dict[str, tuple[str, int]]:
        """一次扫描 packs 目录,建 event_id → (path, size) 索引.

        文件名格式: feedback-{uid}-{event_id}-{YYYYMMDD-HHMMSS}.tar.gz
        通过 UUID 正则匹配 event_id,兼容有无 uid 前缀.
        同一 event_id 有多个 pack 时取最新(mtime 最大).
        """
        packs_dir = miloco_home() / "packs"
        if not packs_dir.exists():
            return {}
        index: dict[str, tuple[str, int, float]] = {}
        for p in packs_dir.rglob("feedback-*.tar.gz"):
            matches = EventsService._UUID_RE.findall(p.name)
            if not matches:
                continue
            eid = matches[-1]
            try:
                st = p.stat()
                prev = index.get(eid)
                if prev is None or st.st_mtime > prev[2]:
                    index[eid] = (p.as_posix(), st.st_size, st.st_mtime)
            except OSError:
                continue
        return {eid: (path, size) for eid, (path, size, _) in index.items()}

    @staticmethod
    def _row_to_event(
        row: dict,
        snapshot_root: Path,
        feedback_index: dict[str, tuple[str, int]],
    ) -> MeaningfulEvent:
        """DAO 行(dict)→ Pydantic 模型;过滤掉内部字段(payload_json/schema_version/created_at).

        clip_kind 由 stat 落盘文件后缀动态计算(50 行列表 = 50×1 stat syscall,
        ms 级开销可接受;避免 schema migration).
        """
        device_ids = row["device_ids"]
        event_id = row["id"]
        clip_kind = EventsService._probe_clip_kind(snapshot_root, event_id, device_ids)
        has_trace = (snapshot_root / event_id / "omni_trace.json.gz").exists()
        fb = feedback_index.get(event_id)
        has_feedback = fb is not None
        feedback_pack_path = fb[0] if fb else None
        feedback_pack_size = fb[1] if fb else None
        return MeaningfulEvent(
            event_id=event_id,
            timestamp=row["timestamp"],
            text=row["text"],
            has_rule_hit=row["has_rule_hit"],
            has_suggestion=row["has_suggestion"],
            has_asr=row["has_asr"],
            snapshot_count=row["snapshot_count"],
            device_ids=device_ids,
            rule_names=row.get("rule_names") or {},
            has_trace=has_trace,
            has_feedback=has_feedback,
            feedback_pack_path=feedback_pack_path,
            feedback_pack_size=feedback_pack_size,
            clip_kind=clip_kind,
        )
