"""adapter 侧：watch ``~/.hermes/gateway_state.json``，URL 变就自动 reload。

Hermes gateway 升级 / 重启后 api URL 可能变（默认 ``http://127.0.0.1:8642``），
adapter 里 ``HERMES_API_URL`` 是启动时定的，URL 一变就 502/connect refused。

watcher：
- 每 30s 比一次 ``gateway_state.json`` 的 mtime + parse api URL
- URL 变 / 端口变 → 调 on_change 回调（默认 restart adapter 进程）
- state.json::adapter.auto_restart_on_url_change=false 可关

为什么不用 inotify：Git Bash / WSL / macOS 行为差异大，简单轮询最稳。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

WATCH_INTERVAL_S = 30.0
DEFAULT_HERMES_HOME = "~/.hermes"


def _state_path() -> Path:
    """``~/.hermes/gateway_state.json``（HERMES_HOME 可覆盖）。"""
    home = os.environ.get("HERMES_HOME") or os.path.expanduser(DEFAULT_HERMES_HOME)
    return Path(home) / "gateway_state.json"


def _extract_api_url(state: dict) -> Optional[str]:
    """从 gateway_state.json 提 api server URL。

    不同 hermes 版本字段名差异，best-effort 兼容：
    - ``api_server.url``
    - ``api.url``
    - ``api_url``
    - ``endpoints.api``
    """
    if not isinstance(state, dict):
        return None
    for key_chain in (
        ("api_server", "url"),
        ("api", "url"),
        ("api_url",),
        ("endpoints", "api"),
        ("endpoints", "api_server"),
    ):
        cur: Any = state
        ok = True
        for k in key_chain:
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if ok and isinstance(cur, str) and cur:
            return cur.rstrip("/")
    return None


def read_current_api_url() -> Optional[str]:
    """读 gateway_state.json 当前 api URL；文件不存在/解析失败返 None。"""
    path = _state_path()
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[adapter-watch] state read failed %s: %s", path, exc)
        return None
    return _extract_api_url(state)


class GatewayUrlWatcher:
    """daemon 线程：周期 poll gateway_state.json，URL 变触发 on_change。

    on_change 签名：``on_change(new_url: str, old_url: Optional[str]) -> None``
    """

    def __init__(self, on_change: Callable[[str, Optional[str]], None], interval_s: float = WATCH_INTERVAL_S) -> None:
        self._on_change = on_change
        self._interval = interval_s
        self._last_url: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._last_url = read_current_api_url()
        self._thread = threading.Thread(
            target=self._run, name="adapter-gateway-watch", daemon=True
        )
        self._thread.start()
        logger.info("[adapter-watch] started (interval=%.0fs, current_url=%s)",
                    self._interval, self._last_url or "(unknown)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self._interval + 1)
        logger.info("[adapter-watch] stopped")

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                cur = read_current_api_url()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[adapter-watch] poll error: %s", exc)
                continue
            if cur != self._last_url:
                old = self._last_url
                self._last_url = cur
                logger.warning(
                    "[adapter-watch] hermes api URL 变更: %s → %s → on_change()",
                    old or "(none)", cur or "(none)",
                )
                try:
                    self._on_change(cur or "", old)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[adapter-watch] on_change raised: %s", exc)


def auto_restart_callback(new_url: str, old_url: Optional[str]) -> None:
    """默认 on_change：URL 变 → ``os.execv`` 自重启。

    install-hermes.sh 起的 nohup 父进程是 bash，execv 替换当前 adapter 进程；
    父脚本的 nohup 关联会保留（因为已经 detached），新进程继续读新 HERMES_API_URL。
    副作用：短暂 in-flight webhook 会断；正常情况下 1-2s 内恢复。

    关闭：``state.json::adapter.auto_restart_on_url_change = false``（v0.3.0 实现）。
    """
    logger.warning(
        "[adapter-watch] 自动重启 adapter（URL: %s → %s）",
        old_url or "(none)", new_url or "(none)",
    )
    # 给 200ms 让日志 flush
    time.sleep(0.2)
    # 替换当前进程为 python -m adapter（保留 env，新 env var 由父脚本下轮 set）
    py = os.environ.get("MILOCO_ADAPTER_PYTHON") or "python3"
    if not py or not Path(py).exists() and not _which(py):
        py = "python"  # 兜底
    argv = [py, "-m", "adapter"]
    try:
        os.execvp(py, argv)
    except OSError as exc:
        logger.error("[adapter-watch] execvp 失败 %s: %s — adapter 没重启，请手动 restart", py, exc)


def _which(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None