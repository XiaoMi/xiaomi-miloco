# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""有意义事件 Service 层.

通过 `mgr.events_service` lazy 单例持有(对齐 register_session_manager 套路).
对接两个 endpoint:
- `GET /api/events`         → list_events
- `GET /api/events/{event_id}/clip/{device_id}` → locate_clip → FileResponse
- `GET /api/events/{event_id}/ref/{device_id}`  → locate_ref → FileResponse
- `GET /api/events/{event_id}/crop/{device_id}` → read_crop_meta → JSON
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from miloco.perception.schema import EventCropMeta, MeaningfulEvent
from miloco.perception.snapshot_writer import (
    CLIP_CANDIDATES,
    get_snapshot_root,
    locate_clip_file,
    region_slug,
)
from miloco.utils.paths import miloco_home

if TYPE_CHECKING:
    from miloco.database.meaningful_events_dao import MeaningfulEventDao

logger = logging.getLogger(__name__)

SnapshotStatus = Literal["found", "gone", "not_found"]

# read_crop_meta 专用:比 SnapshotStatus 多一个 "unreadable" —— crop 坐标必须把
# 「确定没裁切」和「裁了但坐标读不出来」分开,前端对二者的处置相反(隐藏整卡 / 保留卡不画框).
# 见 EventsService._no_box_status.
CropMetaStatus = Literal["found", "gone", "unreadable", "not_found"]

# Smart Crop 模式下与 crop 视频同附上送 LLM 的全景参考帧(整帧 JPEG,字节级 = omni 所见).
# 非 crop 事件无此文件 —— 前端据 list 的 has_ref 决定是否请求.
_REF_FILENAME = "ref.jpg"

# 事件级 omni trace(gzip JSON).Smart Crop 的 crop 坐标挂在其 calls[].crop 下,
# read_crop_meta 从这里读 —— 不为画框另加 sidecar 文件.
_TRACE_FILENAME = "omni_trace.json.gz"


def _safe_log(value: object) -> str:
    """写日志前去掉 CR/LF,防 log injection(CodeQL py/log-injection).同 schedule/router._safe_log.

    凡来自 URL path / query / 配置 / 模型输出的值,写进日志前都该过一遍 —— 即便调用点
    当下有校验保证注不进换行,那是**调用方校验**给的安全,不是这行日志自己的;校验哪天
    放宽,伪造整行就会直接落进日志,而这种回归不会有任何测试变红.

    注意保留 `.replace("\\n", ...)` 的字面量形态:CodeQL 只认第一个实参是字面量
    "\\n" / "\\r\\n" 的 replace 为 sanitizer,改成正则或变量会重新触发告警.
    """
    return str(value).replace("\r", "").replace("\n", " ")


def probe_has_ref(snapshot_root: Path, event_id: str, device_ids: list[str]) -> bool:
    """任一 device 目录下有 ref.jpg → True(该事件走了 Smart Crop,有全景参考帧).

    模块级公开:list 通路(_row_to_event)与 SSE 实时通路(client._persist_meaningful_event)
    都据此填 has_ref,两条通路口径必须一致(payload 字段与 /api/events list 元素同形).
    """
    for did in device_ids:
        if (snapshot_root / event_id / region_slug(did) / _REF_FILENAME).exists():
            return True
    return False


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
        feedback_index = self.build_feedback_index()
        return [self._row_to_event(row, snapshot_root, feedback_index) for row in rows]

    async def locate_clip(
        self, event_id: str, device_id: str
    ) -> tuple[SnapshotStatus, Path | None, str | None, int | None]:
        """定位指定 event × device 的 clip 文件路径(字节级 = omni 看到的).

        路由层用这个返 FileResponse(path, media_type=...),让 Starlette 走 sendfile +
        Range/206 流式响应 — 避免把整段 mp4 读进内存阻塞 event loop,且支持 <video>
        scrubber 的 seek.

        探测顺序:先 clip.mp4 (视频路径产物),后 clip.m4a (audio-only 路径产物).
        对应 media_type 由 MEDIA_TYPE_BY_SUFFIX 决定.

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
        result = locate_clip_file(device_dir)
        if result is not None:
            path, media_type = result
            return ("found", path, media_type, row["timestamp"])
        return ("gone", None, None, None)

    async def locate_ref(
        self, event_id: str, device_id: str
    ) -> tuple[SnapshotStatus, Path | None, int | None]:
        """定位指定 event × device 的全景参考帧 ref.jpg(仅 Smart Crop 事件有).

        与 locate_clip 同款状态语义:
        - ("found", Path, timestamp_ms):ref.jpg 存在 → 路由层 FileResponse(image/jpeg);
          timestamp_ms 是 meaningful_events.timestamp,用途同 locate_clip —— 路由层拼按
          事件时间命名的下载文件名(否则"另存为"拿到的是 URL 末段 device_id、还没后缀)
        - ("gone", None, None):event 存在且 device_id 合法,但无 ref.jpg(非 crop 事件 / 已被 cleanup 清)
        - ("not_found", None, None):event 不存在 / device_id 不在 device_ids 内

        非 crop 事件本就无参考帧 → 返 "gone";前端应据 list 的 has_ref 门控请求,
        误请求时降级即可(不当错误).
        """
        row = self._dao.get_by_id(event_id)
        if row is None:
            return ("not_found", None, None)
        if device_id not in row["device_ids"]:
            return ("not_found", None, None)
        path = get_snapshot_root() / event_id / region_slug(device_id) / _REF_FILENAME
        if path.exists():
            return ("found", path, row["timestamp"])
        return ("gone", None, None)

    async def read_crop_meta(
        self, event_id: str, device_id: str
    ) -> tuple[CropMetaStatus, EventCropMeta | None]:
        """从事件级 omni_trace 里取该 device 的 Smart Crop 元数据(region / 帧尺寸 / 短边).

        不另落盘 —— crop 坐标已随 omni_trace.json.gz 持久化(snapshot_context.push_crop_meta
        挂在 call 记录的 "crop" 键下),这里只做「解压 + 挑出对应 device 的最后一条」的读侧投影,
        免得为一个画框需求再加一份 sidecar 文件.

        取 **最后一条**匹配 call:同 device 同事件正常只一次 omni 调用,但 stream/重试路径
        可能追加多条,以最后一次实际上送的为准.

        状态语义(比 locate_ref 多一档 "unreadable",分档判据见 _no_box_status):
        - ("found", EventCropMeta):trace 里有该 device 的 crop 记录且字段合法
        - ("gone", None):这台 device 确定没裁切(该 device 目录下无 ref.jpg)→ 路由层 410
        - ("unreadable", None):裁过(ref.jpg 在盘上),但坐标读不出来(trace 被清 / 损坏 /
          crop 字段形状或坐标值不合法)→ 路由层 500
        - ("not_found", None):event 不存在 / device_id 不在 device_ids 内

        解析与校验失败折成状态码而非让异常冒成 500,是为了让「读不出来」也带确定语义:
        校验若留在 router 里做 EventCropMeta(**crop),半截 dict(schema 演进 / 写入被截断)
        抛的 ValidationError 会和真正的服务端 bug 混在同一个 500 里,分不出是数据坏还是代码坏.
        但**不能因此把它折成 410** —— 410 是前端隐藏整张参考帧卡的信号,盘上明明有 ref.jpg
        却因为 trace 读坏而整卡消失,丢的信息远多于少画一个框.
        """
        row = self._dao.get_by_id(event_id)
        if row is None:
            return ("not_found", None)
        if device_id not in row["device_ids"]:
            return ("not_found", None)
        path = get_snapshot_root() / event_id / _TRACE_FILENAME
        if not path.exists():
            return (self._no_box_status(event_id, device_id), None)
        # 遍历也包在 try 里:trace 损坏不止"crop 数组半截"一种形态,calls 本身可能不是 list
        # (`reversed(123)` → TypeError)、元素可能不是 dict(`call.get` → AttributeError).
        # 这些若漏在 try 外就会裸冒成 500 —— 状态码看着和 "unreadable" 那档一样,但少了
        # _no_box_status 的分档:该返 410 的(压根没裁切)也会变 500,前端反而多留一张空卡.
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                trace = json.load(f)
            calls = trace.get("calls") or []
            if not isinstance(calls, list):
                raise TypeError(f"calls is {type(calls).__name__}, expected list")
            for call in reversed(calls):
                if not isinstance(call, dict):
                    continue  # 坏元素跳过即可,别让整个事件的画框都拿不到
                if call.get("device_id") != device_id:
                    continue
                crop = call.get("crop")
                if not crop:
                    continue
                try:
                    return ("found", EventCropMeta(**crop))
                except (TypeError, ValidationError) as e:
                    # 这两个值来自 URL path.走到这里它们其实已过两道校验(_dao.get_by_id
                    # 查得到 + device_id 在 row["device_ids"] 内),即必然等于后端自己生成的
                    # UUID / 设备号,当下注不进换行 —— 但那是调用方校验给的安全,不是这行
                    # 日志自己的,所以照样过 _safe_log。
                    logger.warning(
                        "read_crop_meta bad crop meta event_id=%s device_id=%s: %s",
                        _safe_log(event_id),
                        _safe_log(device_id),
                        e,
                    )
                    return (self._no_box_status(event_id, device_id), None)
        except Exception as e:  # noqa: BLE001
            # path 里拼着 event_id,与上面那条同源 —— CodeQL 的 taint 没追进 Path 拼接
            # 只报了上面两处,但只清洗被点名的那处、留下这处,防线就是漏的。
            logger.warning("read_crop_meta failed to parse trace %s: %s", _safe_log(path), e)
            return (self._no_box_status(event_id, device_id), None)
        return (self._no_box_status(event_id, device_id), None)

    @staticmethod
    def _no_box_status(event_id: str, device_id: str) -> Literal["gone", "unreadable"]:
        """拿不到 crop 坐标时,区分「这台 device 本次没裁切」与「裁了但坐标读不出来」.

        判据是 ref.jpg 在不在,而不是 trace 在不在:两者由同一段代码一并产出 ——
        prompt_builder._maybe_encode_adaptive 末尾紧挨着调 push_ref_frame(参考帧字节)
        和 push_crop_meta(坐标,随后由 call_omni finally 里的 push_omni_trace 挂进 trace),
        中间没有提前 return;且 ref.jpg 是**按 device** 落的 —— trace 是事件级、还兼着
        crop 之外的用途,拿它判"这台 device 走没走过 Smart Crop"口径不对.

        - ref.jpg 不存在 → "gone":确定没裁切(非 crop 事件 / 本 device 落到全景兜底 /
          整个事件目录已被 cleanup 清).前端据此隐藏整张参考帧卡是对的.
        - ref.jpg 存在   → "unreadable":裁过、参考帧还在盘上,只是框画不出来.此时必须让
          前端保留卡片(只是不画框),所以不能复用 "gone".

        cleanup_snapshots 是整个事件目录 rmtree,不会留下 ref.jpg 而单独清掉 trace;
        所以"ref.jpg 在、trace 没了"实际只出现在盘被外部动过的场合,归到 "unreadable"
        (数据不自洽)比归到 "gone"(确定没裁切)更贴事实.
        """
        ref = get_snapshot_root() / event_id / region_slug(device_id) / _REF_FILENAME
        return "unreadable" if ref.exists() else "gone"

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
            for filename in CLIP_CANDIDATES:
                path = device_dir / filename
                if path.exists():
                    return path.suffix[1:]
        return None

    _UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    @staticmethod
    def build_feedback_index() -> dict[str, tuple[str, int]]:
        """一次扫描 packs 目录,建 event_id / log_id → (path, size) 索引.

        两种包名共用本索引:
        - 事件包     feedback-{uid}-{event_id}-{YYYYMMDD-HHMMSS}.tar.gz
        - 主动查询包 feedback-{uid}-od-{log_id}-{YYYYMMDD-HHMMSS}.tar.gz
        取 UUID 正则 findall 的最后一个匹配,故 uid/字面量 od/时间戳都不会被误命中.
        调用方: EventsService.list_events 与 PerceptionService.query_on_demand_logs.
        同一 id 有多个 pack 时取最新(mtime 最大).
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
        has_ref = probe_has_ref(snapshot_root, event_id, device_ids)
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
            has_ref=has_ref,
        )
