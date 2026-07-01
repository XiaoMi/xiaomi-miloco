"""feedback-pack: 按 event_id 打包单事件的完整 omni 复现数据到 tar.gz.

打包内容:
  - metadata.json         事件元数据 + 用户反馈 + 版本 + 数据完整性记录
  - omni_trace.json.gz    omni 调用记录(prompt + response + 推理参数)
  - clips/{device}/clip.* 视频/音频(零重编,omni 原始输入)
  - gallery/*.jpg         画廊合成图(可选,用户勾选时包含)

个人信息脱敏: 对 omni_trace 文本做正则替换(手机号/IP/身份证号 → ***).
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import os
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
LRU_KEEP = 5

_PII_PATTERNS = [
    (re.compile(r"\b1[3-9]\d{9}\b"), "***"),
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "***"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "***"),
]


class FeedbackPackError(Exception):
    pass


class EventNotFoundError(FeedbackPackError):
    pass


def _sanitize_pii(text: str) -> str:
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _sanitize_trace(trace_bytes: bytes) -> bytes:
    try:
        raw = gzip.decompress(trace_bytes)
        text = raw.decode("utf-8")
        sanitized = _sanitize_pii(text)
        return gzip.compress(sanitized.encode("utf-8"))
    except Exception as e:
        logger.error("Failed to sanitize trace: %s", e)
        return trace_bytes


def _git_hash() -> str | None:
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
            cwd=Path(__file__).resolve().parent,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _packs_dir() -> Path:
    return miloco_home() / "packs"


def _lru_cleanup() -> list[str]:
    packs = _packs_dir()
    if not packs.exists():
        return []
    files = sorted(
        packs.glob(f"{_PACK_PREFIX}*{_PACK_SUFFIX}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    evicted: list[str] = []
    for old in files[LRU_KEEP:]:
        try:
            os.remove(old)
            evicted.append(old.as_posix())
        except OSError:
            pass
    return evicted


def build_feedback_pack(
    *,
    event_id: str,
    error_types: list[str],
    feedback_text: str,
    include_gallery: bool = False,
) -> dict:
    """打包单事件反馈数据 -> $MILOCO_HOME/packs/feedback-{event_id[:8]}-HHMMSS.tar.gz.

    Args:
        event_id: meaningful_events 的 id.
        error_types: 用户选择的错误类别.
        feedback_text: 用户补充说明.
        include_gallery: 是否包含画廊合成图.

    Returns:
        {path, size_bytes, components}

    Raises:
        EventNotFoundError: event_id 对应的 snapshots 目录不存在.
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
    if trace_path.exists():
        components["omni_trace_found"] = True

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
        version = mgr.app_version
    except Exception:
        version = "unknown"

    metadata = {
        "event_id": event_id,
        "uid": "",
        "timestamp": event.get("timestamp"),
        "text": event.get("text", ""),
        "device_ids": device_ids,
        "error_types": error_types,
        "user_feedback": feedback_text,
        "created_at": ms_to_iso_local(now_ms()),
        "miloco_version": version,
        "git_hash": _git_hash(),
        "omni_trace_found": components["omni_trace_found"],
        "clips_found": components["clips_found"],
        "clips_missing": components["clips_missing"],
        "gallery_included": include_gallery and has_gallery,
    }

    packs_dir = _packs_dir()
    packs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final_path = packs_dir / f"{_PACK_PREFIX}{event_id}-{stamp}{_PACK_SUFFIX}"

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

            if trace_path.exists():
                sanitized = _sanitize_trace(trace_path.read_bytes())
                info = tarfile.TarInfo(name="omni_trace.json.gz")
                info.size = len(sanitized)
                tar.addfile(info, io.BytesIO(sanitized))

            for clip_rel in components["clips_found"]:
                clip_path = event_dir / clip_rel
                if clip_path.exists():
                    tar.add(clip_path, arcname=f"clips/{clip_rel}")

            if include_gallery and has_gallery:
                for jpg in gallery_dir.glob("*.jpg"):
                    tar.add(jpg, arcname=f"gallery/{jpg.name}")
                components["gallery_included"] = True

        shutil.move(str(tar_tmp), final_path)

    _lru_cleanup()

    return {
        "path": final_path.as_posix(),
        "size_bytes": final_path.stat().st_size,
        "components": components,
    }
