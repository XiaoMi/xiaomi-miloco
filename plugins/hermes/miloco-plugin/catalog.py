"""设备目录注入（spec-injection-plan §5.4）。

移植自 openclaw TypeScript 插件 ``plugins/openclaw/src/services/catalog.ts``。

在 ``pre_llm_call`` 钩子里跑 ``miloco-cli device catalog`` 生成目录文本。CLI
内部每次都调后端 ``GET /api/miot/device_history`` 拿最新 LRU snapshot 并读本地
``home_info.json``，所以重跑就能反映用户最近的控制行为。

5 秒节流：只防同一对话片段里 hook 被多次调用的 spam。**不**把 home_info.json
mtime 作为缓存命中条件——LRU 变化在控制路径写入后端 SQLite，不会改
home_info.json mtime，用 mtime 判断会把 LRU 永远卡在缓存里。

调 CLI 失败（未安装 / 后端未起 / spec 未填）时沿用旧缓存或返回空字符串，让
prompt 不带目录工作（agent 走 ``device list`` + ``device spec`` fallback）。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 防抖：CLI 调用不应被 spam。与 TS 端 REGEN_THROTTLE_MS = 5_000 一致。
REGEN_THROTTLE_SECONDS = 5.0
# 后端慢（如批量 parse spec）会让 CLI 内部 httpx 等到 30s 超时。10s 上限到点
# SIGTERM，run_cli_catalog 返回 None → get_catalog 沿用旧缓存。
_CLI_TIMEOUT_SECONDS = 10.0

_lock = threading.Lock()
# 进程内缓存：{text, generated_at}。None 表示从未成功生成过。
_cached: Optional[dict] = None
_generating = False


def _run_cli_catalog() -> Optional[str]:
    """调 ``miloco-cli device catalog``，失败/超时返回 None。"""
    try:
        proc = subprocess.run(
            ["miloco-cli", "device", "catalog"],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("miloco-cli 未找到，device catalog 跳过")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("miloco-cli device catalog 超时（%ss）", _CLI_TIMEOUT_SECONDS)
        return None
    except Exception as exc:  # noqa: BLE001 - 子进程异常全部降级为 None
        logger.warning("miloco-cli device catalog spawn failed: %s", exc)
        return None

    if proc.returncode != 0:
        stderr = (proc.stderr or "")[:200]
        logger.warning("miloco-cli device catalog exited %s: %s", proc.returncode, stderr)
        return None

    stdout = proc.stdout or ""
    return stdout or None


def get_catalog() -> str:
    """拿到当前缓存中的目录文本，必要时刷新。失败返回空字符串。

    线程安全：持锁判过期后置 _generating 标志、释放锁跑 CLI、再持锁写
    缓存。同一时刻最多一个线程在生成，其它并发调用直接沿用旧缓存。
    """
    global _cached, _generating

    with _lock:
        now = time.monotonic()
        if _cached and now - _cached["generated_at"] < REGEN_THROTTLE_SECONDS:
            return _cached["text"]
        if _generating:
            return _cached["text"] if _cached else ""
        _generating = True

    text = _run_cli_catalog()

    with _lock:
        if text is not None:
            _cached = {"text": text, "generated_at": time.monotonic()}
            logger.info("device catalog refreshed (%d chars)", len(text))
        _generating = False

    if text is not None:
        return text
    with _lock:
        return _cached["text"] if _cached else ""


def _reset_catalog_cache() -> None:
    """仅为测试 / hot-reload 之用：手动清缓存。"""
    global _cached, _generating
    with _lock:
        _cached = None
        _generating = False
