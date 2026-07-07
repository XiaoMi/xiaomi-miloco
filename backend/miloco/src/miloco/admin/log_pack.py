"""log-pack: 打包 $MILOCO_HOME 下排查数据到 tar.gz。

打包内容(缺则跳过):
  - $WORKSPACE/observability.db  (SQLite 在线备份,保证一致性快照)
  - $SNAPSHOT_ROOT/<event_id>/omni_trace.json.gz  (事件级 omni 决策 trace)
  - $MILOCO_HOME/trace/agent/**/*.jsonl.gz
  - $WORKSPACE/log/*  (backend log_dir)

omni trace 只 glob trace 文件,不打 clip mp4/m4a(体量大,撞 MAX_TOTAL_BYTES)。

miloco.db 不入包: 含 MiOT OAuth token、person/biometric 等敏感数据,
排查需要时另行单独提取。

plugin 端日志由 OpenClaw 宿主统一管理,不在此打包;排查 plugin 行为需另到
宿主日志目录查阅。

体量保护: 预扫描总和 > MAX_TOTAL_BYTES -> 抛 LogPackSizeExceeded。
LRU: packs/ 下最多保留 2 个,多余的删最旧。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from miloco.config.settings import get_settings
from miloco.observability import debug as debug_mod
from miloco.perception.snapshot_writer import get_snapshot_root
from miloco.utils.paths import miloco_home
from miloco.utils.time_utils import ms_to_iso_local, now_ms

MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB
LRU_KEEP = 2
_PACK_PREFIX = "log-pack-"
_PACK_SUFFIX = ".tar.gz"


class LogPackSizeExceeded(Exception):
    """预扫描体量超 MAX_TOTAL_BYTES。``info`` 给前端展示。"""

    def __init__(self, info: dict):
        super().__init__("log-pack size exceeded")
        self.info = info


def _workspace_dir() -> Path:
    """读 settings 的 workspace_dir,因 storage 字段可能为 "."(默认,= MILOCO_HOME 顶级)
    或自定义子目录或绝对路径。硬编码会在 storage 非默认时打包错路径。"""
    return get_settings().directories.workspace_dir


def _packs_dir() -> Path:
    return miloco_home() / "packs"


def _dir_size(path: Path) -> tuple[int, int]:
    """返回 (total_bytes, file_count)。"""
    total = 0
    files = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            files += 1
    return total, files


def _scan_omni_traces() -> list[Path]:
    """扫 snapshot_root 下事件级 omni trace 文件(每事件 1 个 ~6 KB).

    只 glob `*/omni_trace.json.gz` 一层子目录,跳过 clip mp4/m4a 等大文件,
    避免撞 MAX_TOTAL_BYTES。snapshot_root 不存在时返空列表。
    """
    snapshot_root = get_snapshot_root()
    if not snapshot_root.exists():
        return []
    return list(snapshot_root.glob("*/omni_trace.json.gz"))


def _scan_components() -> dict:
    """扫描各组件存在与大小;present=False 时 size/files 仍给 0。"""
    home = miloco_home()
    ws = _workspace_dir()

    obs_db_path = ws / "observability.db"
    agent_dir = home / "trace" / "agent"
    log_dir = ws / "log"

    comps: dict = {}
    comps["observability_db"] = {
        "present": obs_db_path.exists(),
        "size": obs_db_path.stat().st_size if obs_db_path.exists() else 0,
    }
    omni_traces = _scan_omni_traces()
    if omni_traces:
        size = sum(p.stat().st_size for p in omni_traces)
        comps["trace_omni"] = {"present": True, "files": len(omni_traces), "size": size}
    else:
        comps["trace_omni"] = {"present": False, "files": 0, "size": 0}
    if agent_dir.exists():
        size, files = _dir_size(agent_dir)
        comps["trace_agent"] = {"present": True, "files": files, "size": size}
    else:
        comps["trace_agent"] = {"present": False, "files": 0, "size": 0}
    if log_dir.exists():
        size, files = _dir_size(log_dir)
        comps["backend_log"] = {"present": True, "files": files, "size": size}
    else:
        comps["backend_log"] = {"present": False, "files": 0, "size": 0}
    return comps


def _git_hash() -> str | None:
    """尝试读 git rev-parse HEAD,失败返回 None。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2,
            cwd=Path(__file__).resolve().parent,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _online_backup_db(src: Path, dst: Path) -> None:
    """SQLite 在线备份: src 仍可被 backend 写,dst 是一致快照。"""
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _lru_cleanup() -> list[str]:
    """packs/ 下按 mtime 降序,保留 LRU_KEEP 个;返回被删的绝对路径。"""
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


def build_log_pack() -> dict:
    """打包 -> $MILOCO_HOME/packs/log-pack-YYYYMMDD-HHMMSS.tar.gz。

    Returns: {path, size_bytes, components, evicted}
    Raises: LogPackSizeExceeded
    """
    home = miloco_home()
    ws = _workspace_dir()
    comps = _scan_components()
    total = sum(c["size"] for c in comps.values())
    if total > MAX_TOTAL_BYTES:
        raise LogPackSizeExceeded({
            "estimated_size_bytes": total,
            "limit_bytes": MAX_TOTAL_BYTES,
            "components": comps,
        })

    packs_dir = _packs_dir()
    packs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final_path = packs_dir / f"{_PACK_PREFIX}{stamp}{_PACK_SUFFIX}"

    with tempfile.TemporaryDirectory() as tmp_root:
        tmp_root_p = Path(tmp_root)
        # observability.db SQLite 在线备份,保证 backend 仍可写入时拿到一致性快照
        obs_snapshot: Path | None = None
        if comps["observability_db"]["present"]:
            obs_snapshot = tmp_root_p / "observability.db"
            _online_backup_db(ws / "observability.db", obs_snapshot)

        # tar 写到 tempfile,完成后 shutil.move 落最终路径
        with tempfile.NamedTemporaryFile(
            suffix=_PACK_SUFFIX, dir=tmp_root_p, delete=False
        ) as tf:
            tar_tmp = Path(tf.name)

        with tarfile.open(tar_tmp, "w:gz") as tar:
            if obs_snapshot is not None:
                tar.add(obs_snapshot, arcname="observability.db")
            if comps["trace_omni"]["present"]:
                # 逐个 add,arcname 保留 event_id 维度,reader 能直接定位事件
                for p in _scan_omni_traces():
                    tar.add(p, arcname=f"trace/omni/{p.parent.name}/omni_trace.json.gz")
            if comps["trace_agent"]["present"]:
                tar.add(home / "trace" / "agent", arcname="trace/agent")
            if comps["backend_log"]["present"]:
                tar.add(ws / "log", arcname="log")
            metadata = {
                "created_at": ms_to_iso_local(now_ms()),
                "miloco_home": str(home),
                "components": comps,
                "git_hash": _git_hash(),
                "debug_state": debug_mod.get_state(),
            }
            meta_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode()
            info = tarfile.TarInfo(name="metadata.json")
            info.size = len(meta_bytes)
            tar.addfile(info, io.BytesIO(meta_bytes))

        shutil.move(str(tar_tmp), final_path)

    evicted = _lru_cleanup()
    return {
        "path": final_path.as_posix(),
        "size_bytes": final_path.stat().st_size,
        "components": comps,
        "evicted": evicted,
    }
