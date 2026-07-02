"""feedback-pack: 按 event_id 打包单事件的完整 omni 复现数据到 tar.gz.

打包内容:
  - metadata.json         事件元数据 + 用户反馈 + 版本 + 数据完整性记录
  - omni_trace.json.gz    omni 调用记录(prompt + response + 推理参数)
  - clips/{device}/clip.* 视频/音频(零重编,omni 原始输入)
  - gallery/*.{jpg,png}   画廊合成图(可选,用户勾选时包含)

个人信息脱敏: 对 omni_trace 文本做正则替换(手机号/IP/身份证号 → ***).
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import re
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from miloco.perception.snapshot_writer import get_snapshot_root, region_slug
from miloco.utils.paths import miloco_home
from miloco.utils.time_utils import ms_to_iso_local, now_ms

logger = logging.getLogger(__name__)

_PACK_PREFIX = "feedback-"
_PACK_SUFFIX = ".tar.gz"
_MAX_TOTAL_MB = 2048

_PII_PATTERNS = [
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***"),
    (re.compile(r"(?<![\d.a-zA-Z])\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.a-zA-Z])"), "***"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "***"),
]


def _cleanup_by_total_size(packs_dir: Path, max_total_mb: int = _MAX_TOTAL_MB) -> None:
    """feedback pack 总大小超限时按 mtime 从旧到新删."""
    files = sorted(
        packs_dir.glob(f"{_PACK_PREFIX}*{_PACK_SUFFIX}"),
        key=lambda p: p.stat().st_mtime,
    )
    total = sum(p.stat().st_size for p in files)
    limit = max_total_mb * 1024 * 1024
    for p in files:
        if total <= limit:
            break
        try:
            size = p.stat().st_size
            p.unlink()
            total -= size
        except OSError:
            pass


_ERROR_TYPE_LABELS = {
    "person": "人物识别错误",
    "pet": "宠物识别错误",
    "action": "动作识别错误",
    "envDevice": "环境/设备状态识别错误",
    "voice": "语音识别错误",
    "ruleFalse": "规则/建议误触发",
    "other": "其他",
}


class FeedbackPackError(Exception):
    pass


class EventNotFoundError(FeedbackPackError):
    pass


def _sanitize_pii(text: str) -> str:
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _sanitize_trace(trace_bytes: bytes) -> bytes | None:
    """脱敏 trace 文本中的个人信息.失败时返回 None(宁可缺 trace 也不泄 PII)."""
    try:
        raw = gzip.decompress(trace_bytes)
        text = raw.decode("utf-8")
        sanitized = _sanitize_pii(text)
        return gzip.compress(sanitized.encode("utf-8"))
    except Exception as e:
        logger.error("Failed to sanitize trace, dropping trace from pack: %s", e)
        return None



def _packs_dir() -> Path:
    return miloco_home() / "packs"



def build_feedback_pack(
    *,
    event_id: str,
    error_types: list[str],
    feedback_text: str,
    include_gallery: bool = False,
    uid: str = "",
) -> dict:
    """打包单事件反馈数据 -> $MILOCO_HOME/packs/feedback-{uid}-{event_id}-YYYYMMDD-HHMMSS.tar.gz.

    uid 取不到时文件名中的 {uid} 段落地为字面量 "anonymous".

    Args:
        event_id: meaningful_events 的 id.
        error_types: 用户选择的错误类别.
        feedback_text: 用户补充说明.
        include_gallery: 是否包含画廊合成图.
        uid: 米家用户 uid,写入 metadata.json 供反馈溯源;取不到时为空串.

    Returns:
        {path, size_bytes, components}

    Raises:
        EventNotFoundError: meaningful_events 表中不存在该 event_id 对应的记录.
    """
    from miloco.manager import get_manager

    mgr = get_manager()
    dao = mgr.meaningful_events_dao

    event = dao.get_by_id(event_id)
    if event is None:
        raise EventNotFoundError(f"Event {event_id} not found")

    snapshot_root = get_snapshot_root()
    event_dir = snapshot_root / event_id

    components: dict = {
        "omni_trace_found": False,
        "clips_found": [],
        "clips_missing": [],
        "gallery_included": False,
    }

    trace_path = event_dir / "omni_trace.json.gz"
    sanitized_trace: bytes | None = None
    if trace_path.exists():
        sanitized_trace = _sanitize_trace(trace_path.read_bytes())
    components["omni_trace_found"] = sanitized_trace is not None

    device_ids: list[str] = event.get("device_ids", [])
    for did in device_ids:
        slug = region_slug(did)
        clip_dir = event_dir / slug
        found = False
        for ext in ("mp4", "m4a"):
            if (clip_dir / f"clip.{ext}").exists():
                components["clips_found"].append(f"{slug}/clip.{ext}")
                found = True
                break
        if not found:
            components["clips_missing"].append(slug)

    gallery_dir = event_dir / "gallery"
    has_gallery = gallery_dir.is_dir() and any(gallery_dir.iterdir())

    try:
        from importlib.metadata import version as get_version
        version = get_version("miloco")
    except Exception:
        version = "unknown"

    metadata = {
        "event_id": event_id,
        "uid": uid,
        "timestamp": event.get("timestamp"),
        "text": _sanitize_pii(event.get("text", "")),
        "device_ids": device_ids,
        "error_types": [_ERROR_TYPE_LABELS.get(k, k) for k in error_types],
        "user_feedback": _sanitize_pii(feedback_text),
        "created_at": ms_to_iso_local(now_ms()),
        "miloco_version": version,
        "omni_trace_found": components["omni_trace_found"],
        "clips_found": components["clips_found"],
        "clips_missing": components["clips_missing"],
        "gallery_included": include_gallery and has_gallery,
    }

    packs_dir = _packs_dir()
    packs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    uid_slug = uid if uid else "anonymous"
    final_path = packs_dir / f"{_PACK_PREFIX}{uid_slug}-{event_id}-{stamp}{_PACK_SUFFIX}"

    with tempfile.TemporaryDirectory() as tmp_root:
        tmp_root_p = Path(tmp_root)
        with tempfile.NamedTemporaryFile(
            suffix=_PACK_SUFFIX, dir=tmp_root_p, delete=False
        ) as tf:
            tar_tmp = Path(tf.name)

        with tarfile.open(tar_tmp, "w:gz") as tar:
            meta_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode()
            info = tarfile.TarInfo(name="metadata.json")
            info.size = len(meta_bytes)
            tar.addfile(info, io.BytesIO(meta_bytes))

            if sanitized_trace is not None:
                info = tarfile.TarInfo(name="omni_trace.json.gz")
                info.size = len(sanitized_trace)
                tar.addfile(info, io.BytesIO(sanitized_trace))

            for clip_rel in components["clips_found"]:
                clip_path = event_dir / clip_rel
                if clip_path.exists():
                    tar.add(clip_path, arcname=f"clips/{clip_rel}")

            if include_gallery and has_gallery:
                for img in gallery_dir.iterdir():
                    if img.suffix in (".jpg", ".png") and img.is_file():
                        tar.add(img, arcname=f"gallery/{img.name}")
                components["gallery_included"] = True

        shutil.move(str(tar_tmp), final_path)

    size_bytes = final_path.stat().st_size
    _cleanup_by_total_size(packs_dir)

    return {
        "path": final_path.as_posix(),
        "size_bytes": size_bytes,
        "components": components,
    }
