# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""有意义事件 artifacts(clip + omni trace)落盘 + 清理工具.

磁盘路径:
- per-device clip: `{snapshot_root}/{event_id}/{device_id_slug}/clip.{mp4|m4a}`
  (一次推理 1 行 event,参与的每个摄像头各落 1 个;字节级 = omni 上传给 LLM 的内容,
   零重编;`device_id_slug` 通过 region_slug 做 URL-safe 化)
- per-device 参考帧: `{snapshot_root}/{event_id}/{device_id_slug}/ref.jpg`
  (仅 Smart Crop 模式;与 crop 视频同附上送 LLM 的整帧上下文,字节级 = omni 所见)
- 事件级 trace: `{snapshot_root}/{event_id}/omni_trace.json.gz`
  (prompt + response + latency + usage + error 的 gzip JSON,用于复盘 LLM 决策)

工具函数:
- `region_slug(s)` — URL-safe 化 device_id / 区域名
- `get_snapshot_root()` — 优先 settings.perception.snapshot_root,fallback DirectorySettings.snapshot_dir
- `check_disk_space(root, min_free_mb)` — 写前预检(B6a)
- `save_event_artifacts(event_id, artifacts)` — 落盘核心(clip + trace 一次完成)
- `cleanup_snapshots(ttl_days, max_disk_mb)` — 24h cleanup loop 调用(目录结构不变,
  老 jpeg 路径下的事件也能正常按 mtime 清理)
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from miloco.config import get_settings
from miloco.perception.snapshot_context import ClipKind, OmniEventArtifacts

logger = logging.getLogger(__name__)



def region_slug(s: str) -> str:
    """URL-safe 化字符串:仅保留字母/数字/连字符/下划线/点,其它字符 → '_'.

    device_id 通常是形如 'cam_living_01' 或 '12345abc-def',基本已合法,
    但 miot device_id 偶尔有 '/' 或 '#',落地为目录名会破坏路径结构.

    路径安全约束(M4):
    - 字面 '..' / '.' 被允许会让 `event_dir / slug` 逃出 event_dir 到 snapshot_root
      甚至更上层 → 拒绝以 '.' 开头(包括 '.' / '..' / '.hidden' 等)
    - 空串 fallback '_'
    """
    if not s:
        return "_"
    slug = re.sub(r"[^a-zA-Z0-9._\-]", "_", s)
    # 防 '..' / '.' / '.foo' 等路径遍历或隐藏目录;以 '_' 前缀替代,保留 device 可读性
    if slug.startswith("."):
        slug = "_" + slug.lstrip(".")
    return slug or "_"


# 视频路径产物 clip.mp4 (H264+AAC);audio-only 路径产物 clip.m4a (仅 AAC,ipod muxer).
# 探测顺序:先 mp4 后 m4a,先找到的优先返回。
# 落盘/事件 clip 端点/主动查询 clip 端点/反馈打包四处同源;加新容器改这里 + ClipKind。
CLIP_CANDIDATES: tuple[str, ...] = ("clip.mp4", "clip.m4a")
MEDIA_TYPE_BY_SUFFIX: dict[str, str] = {".mp4": "video/mp4", ".m4a": "audio/mp4"}


def locate_clip_file(device_dir: Path) -> tuple[Path, str] | None:
    for filename in CLIP_CANDIDATES:
        path = device_dir / filename
        if path.exists():
            return path, MEDIA_TYPE_BY_SUFFIX[path.suffix]
    return None


def clip_download_name(timestamp_ms: int, suffix: str) -> str:
    from datetime import datetime

    from miloco.utils.time_utils import deploy_timezone

    local_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=deploy_timezone())
    return f"clip-{local_dt.strftime('%Y-%m-%d-%H-%M-%S')}.{suffix}"


def get_snapshot_root() -> Path:
    """返回截图根目录绝对路径.

    优先级:`settings.perception.snapshot_root`(非 None) → `settings.directories.snapshot_dir`.
    """
    settings = get_settings()
    if settings.perception.snapshot_root:
        return Path(settings.perception.snapshot_root).expanduser()
    return settings.directories.snapshot_dir


def check_disk_space(root: Path, min_free_mb: int) -> bool:
    """写前预检:磁盘可用空间是否充足.

    Args:
        root: 检查的目录(用其挂载点的可用空间)
        min_free_mb: 最小可用 MB

    Returns:
        True 表示 free >= min_free_mb,允许落盘;
        False 表示空间不足,调用方应跳过 save_event_artifacts.

    检查失败(如目录不存在)按"True 可用"处理,避免误杀;真有问题在 imwrite 时 raise.
    """
    try:
        # disk_usage 接受任意目录,会返回该目录所在挂载点的统计
        # 若 root 还不存在,用 parent
        check_path = root if root.exists() else root.parent
        usage = shutil.disk_usage(check_path)
        return usage.free >= min_free_mb * 1024 * 1024
    except OSError as e:
        logger.error("check_disk_space failed for %s: %s", root, e)
        return True


def save_event_artifacts(event_id: str, artifacts: OmniEventArtifacts) -> list[str]:
    """落盘一次 omni 触发事件的所有产物(clip 字节 + omni trace).

    路径:
    - per-device clip: `{snapshot_root}/{event_id}/{region_slug(device_id)}/clip.{mp4|m4a}`
    - per-device 参考帧: `{snapshot_root}/{event_id}/{region_slug(device_id)}/ref.jpg`(仅 Smart Crop)
    - 事件级 trace: `{snapshot_root}/{event_id}/omni_trace.json.gz`

    Args:
        event_id: 事件 UUID
        artifacts: 含 clips / trace / gallery / ref_frames 的容器.四者全空时返空列表、
            不落任何文件(只有 ref_frames 非空时照样落 ref.jpg).

    Returns:
        成功落盘的 device_id 列表;trace / gallery / ref 均不计入.
        len(result) 等价于原 snapshot_count.

    Caller 责任:调用前已 check_disk_space 确认有空间;本函数遇 OSError 静默跳过.
    """
    if (
        not artifacts.clips
        and artifacts.trace is None
        and not artifacts.gallery
        and not artifacts.ref_frames
    ):
        return []

    snapshot_root = get_snapshot_root()
    event_dir = snapshot_root / event_id
    try:
        event_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Failed to create event dir %s: %s", event_dir, e)
        return []

    clip_dids = _save_clips(event_dir, artifacts.clips)
    if artifacts.ref_frames:
        _save_ref_frames(event_dir, artifacts.ref_frames)
    if artifacts.trace is not None:
        _save_trace(event_dir, artifacts.trace)
    if artifacts.gallery:
        _save_gallery(event_dir, artifacts.gallery)
    return clip_dids


def _save_clips(
    event_dir: Path,
    clips: dict[str, tuple[bytes, ClipKind]],
) -> list[str]:
    """落 per-device clip 字节到 event_dir.kind 非法 / 空字节 → 跳过该 device.

    Returns:
        成功落盘的 device_id 列表.
    """
    saved: list[str] = []
    for device_id, (clip_bytes, kind) in clips.items():
        if not clip_bytes:
            continue
        if f"clip.{kind}" not in CLIP_CANDIDATES:
            logger.error("Unknown clip kind %r for %s; skipping", kind, device_id)
            continue
        device_dir = event_dir / region_slug(device_id)
        try:
            device_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("Failed to create device dir %s: %s", device_dir, e)
            continue
        path = device_dir / f"clip.{kind}"
        try:
            path.write_bytes(clip_bytes)
            saved.append(device_id)
        except OSError as e:
            logger.error("Failed to write %s: %s", path, e)
            continue
    return saved


def _save_ref_frames(event_dir: Path, ref_frames: dict[str, bytes]) -> None:
    """落 per-device 全景参考帧 JPEG 到 `{device_slug}/ref.jpg`(与 clip 同目录).

    空字节 → 跳过该 device.失败 logger.error 不抛,不影响 clip / trace 落盘.
    """
    for device_id, jpeg_bytes in ref_frames.items():
        if not jpeg_bytes:
            continue
        device_dir = event_dir / region_slug(device_id)
        try:
            device_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("Failed to create device dir %s: %s", device_dir, e)
            continue
        path = device_dir / "ref.jpg"
        try:
            path.write_bytes(jpeg_bytes)
        except OSError as e:
            logger.error("Failed to write %s: %s", path, e)


def _save_trace(event_dir: Path, trace: dict[str, Any]) -> None:
    """gzip 压缩 trace dict 并落盘.失败 logger.error 不抛,clip 落盘不受影响."""
    try:
        payload = json.dumps(trace, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        gz_bytes = gzip.compress(payload)
        (event_dir / "omni_trace.json.gz").write_bytes(gz_bytes)
    except (OSError, TypeError, ValueError) as e:
        logger.error("Failed to write trace for %s: %s", event_dir.name, e)


def _save_gallery(event_dir: Path, gallery: dict[str, dict[str, bytes]]) -> None:
    """落盘画廊合成图到 {event_dir}/gallery/{person_id}_{kind}.{ext}.

    通过 magic bytes 判断实际格式(PNG/JPEG),扩展名与内容一致.
    """
    gallery_dir = event_dir / "gallery"
    try:
        gallery_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Failed to create gallery dir %s: %s", gallery_dir, e)
        return
    for person_id, images in gallery.items():
        slug = region_slug(person_id)
        for kind, image_bytes in images.items():
            if not image_bytes:
                continue
            ext = "png" if image_bytes[:4] == b"\x89PNG" else "jpg"
            path = gallery_dir / f"{slug}_{kind}.{ext}"
            try:
                path.write_bytes(image_bytes)
            except OSError as e:
                logger.error("Failed to write gallery %s: %s", path, e)


def cleanup_snapshots(ttl_days: int, max_disk_mb: int) -> dict:
    """24h cleanup loop 调用的两阶段清理.

    Stage 1 (TTL):删 mtime 早于 ttl_days 天前的整个 event 子目录.
    Stage 2 (LRU 兜底):若总占用 > max_disk_mb,按 mtime 升序删整个 event 子目录到达标.

    Returns:
        {"deleted_by_ttl": int, "deleted_by_lru": int, "remaining_mb": int}
    """
    root = get_snapshot_root()
    stats = {"deleted_by_ttl": 0, "deleted_by_lru": 0, "remaining_mb": 0}

    if not root.exists():
        return stats

    # 收集所有顶级 event 子目录(每个对应一个 event_id)+ mtime + size
    event_dirs: list[tuple[Path, float, int]] = []
    for p in root.iterdir():
        if not p.is_dir():
            continue
        try:
            mtime = p.stat().st_mtime
            size = _dir_size(p)
        except OSError:
            continue
        event_dirs.append((p, mtime, size))

    now = time.time()
    cutoff = now - ttl_days * 86400

    # Stage 1: TTL 删除
    survivors: list[tuple[Path, float, int]] = []
    for path, mtime, size in event_dirs:
        if mtime < cutoff:
            try:
                shutil.rmtree(path)
                stats["deleted_by_ttl"] += 1
            except OSError as e:
                logger.error("rmtree TTL failed for %s: %s", path, e)
                survivors.append((path, mtime, size))
        else:
            survivors.append((path, mtime, size))

    # Stage 2: LRU 兜底
    total_bytes = sum(size for _, _, size in survivors)
    cap_bytes = max_disk_mb * 1024 * 1024
    if total_bytes > cap_bytes:
        # 按 mtime 升序排(最旧在前)
        survivors.sort(key=lambda t: t[1])
        for path, _, size in survivors:
            if total_bytes <= cap_bytes:
                break
            try:
                shutil.rmtree(path)
                total_bytes -= size
                stats["deleted_by_lru"] += 1
            except OSError as e:
                logger.error("rmtree LRU failed for %s: %s", path, e)

    stats["remaining_mb"] = int(total_bytes / (1024 * 1024))
    logger.info(
        "cleanup_snapshots: ttl=%d lru=%d remaining=%dMB",
        stats["deleted_by_ttl"],
        stats["deleted_by_lru"],
        stats["remaining_mb"],
    )
    return stats


def _dir_size(path: Path) -> int:
    """递归计算目录大小(字节);跳过出错的子项."""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total
