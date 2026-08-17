# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""CI 环境校验工具。

在 CI 流程或容器化部署中，环境变量可能因 Docker 层缓存、.env 文件遗漏
或 systemd EnvironmentFile 配置错误而缺失。本模块提供轻量级校验函数，
在 miloco 启动早期检测缺失/冲突的 ``MILOCO_*`` 变量，避免运行时静默降级。

用法::

    from miloco.config.env_audit import audit_env
    issues = audit_env()
    if issues:
        logger.warning("环境配置问题: %s", issues)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

REQUIRED_VARS = [
    "MILOCO_HOME",
    "MILOCO_DEVICE_MODEL",
]

OPTIONAL_VARS = [
    "MILOCO_LOG_LEVEL",
    "MILOCO_STORAGE_ROOT",
    "MILOCO_MIOT_CACHE_DIR",
]

_MILOCO_PREFIX = re.compile(r"^MILOCO_")


def _read_proc_environ() -> dict[str, str]:
    """从 /proc/self/environ 读取原始进程环境。

    与 os.environ 不同，/proc/self/environ 反映进程启动时的原始变量，
    不受运行时 os.environ 修改（如 dotenv 覆盖）影响。用于审计 CI
    容器中实际注入的变量是否与预期一致。
    """
    try:
        raw = Path("/proc/self/environ").read_bytes()
        pairs = (entry.split(b"=", 1) for entry in raw.split(b"\0") if b"=" in entry)
        return {k.decode(): v.decode() for k, v in pairs}
    except (OSError, UnicodeDecodeError):
        return {}


def audit_env(*, use_proc: bool = False) -> list[str]:
    """校验当前环境中的 MILOCO_* 变量。

    Args:
        use_proc: 为 True 时从 /proc/self/environ 读取，用于对比 os.environ
                  以检测运行时覆盖。

    Returns:
        问题描述列表（空列表 = 无问题）。
    """
    source = _read_proc_environ() if use_proc else dict(os.environ)
    issues: list[str] = []

    for var in REQUIRED_VARS:
        if var not in source:
            issues.append(f"缺少必需变量: {var}")
        elif not source[var].strip():
            issues.append(f"变量为空: {var}")

    miloco_vars = {k: v for k, v in source.items() if _MILOCO_PREFIX.match(k)}
    if not miloco_vars:
        issues.append("未发现任何 MILOCO_* 变量（可能未加载 .env 或 EnvironmentFile）")

    return issues
