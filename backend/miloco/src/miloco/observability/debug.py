"""observability debug 开关。

解析顺序:
  1. _runtime_override ∈ {True, False} -> 直接返回
  2. _runtime_override is None(进程启动后未调过 set) -> 退回读
     $MILOCO_HOME/.debug_observability(进程内缓存)

set_runtime_override(True/False) 同步创建/删除 .debug_observability,
所以 on/off 是持久的:重启后 override 回到 None,从文件 flag 恢复状态。

当前是一个占位 toggle:omni trace 已默认走 artifacts 落盘,本 flag 不再控
任何已有行为,但保留供后续 debug 选项接入(避免每次新功能都重造一套开关)。
"""
from __future__ import annotations

import threading
from pathlib import Path

from miloco.utils.paths import miloco_home

_FLAG_NAME = ".debug_observability"
_cached: bool | None = None
_runtime_override: bool | None = None
_override_lock = threading.Lock()


def _flag_path() -> Path:
    return miloco_home() / _FLAG_NAME


def _read_file_flag() -> bool:
    global _cached
    if _cached is None:
        _cached = _flag_path().exists()
    return _cached


def is_debug_enabled() -> bool:
    with _override_lock:
        override = _runtime_override
    if override is not None:
        return override
    return _read_file_flag()


def set_runtime_override(value: bool) -> None:
    """设置 runtime override 并同步文件 flag。

    True  -> override=True  + 创建 .debug_observability
    False -> override=False + 删除 .debug_observability
    """
    global _runtime_override, _cached
    with _override_lock:
        _runtime_override = value
        if value:
            _flag_path().touch()
            _cached = True
        else:
            _flag_path().unlink(missing_ok=True)
            _cached = False


def get_state() -> dict:
    file_present = _flag_path().exists()
    with _override_lock:
        override = _runtime_override
    if override is not None:
        enabled = override
        source = "runtime"
    elif file_present:
        enabled = True
        source = "file"
    else:
        enabled = False
        source = "default"
    return {
        "enabled": enabled,
        "source": source,
        "runtime_override": override,
        "file_flag_present": file_present,
    }


def _reset_cache_for_tests() -> None:
    """测试用:清文件 flag 缓存 + 清 runtime override,保证测试间无污染。"""
    global _cached, _runtime_override
    _cached = None
    with _override_lock:
        _runtime_override = None
