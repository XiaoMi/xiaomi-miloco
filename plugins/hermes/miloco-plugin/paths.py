"""MILOCO_HOME 路径解析。

移植自 openclaw TypeScript 插件 ``plugins/openclaw/src/miloco/paths.ts``，
与后端 Python 侧 ``miloco.utils.paths.miloco_home()`` 以及 CLI 侧
``miloco_cli.config.miloco_home()`` 行为保持一致：优先读 ``$MILOCO_HOME``，
未设置则落回 ``~/.openclaw/miloco``。
"""

from __future__ import annotations

import os
from pathlib import Path


def miloco_home() -> Path:
    """返回 ``$MILOCO_HOME``，未设置则使用 ``~/.openclaw/miloco``。

    每次调用都读取环境变量，便于测试用 ``MILOCO_HOME`` 临时注入。
    以 ``~`` 开头的值会按主目录展开（与 TS 端 ``env.startsWith("~")`` 分支一致）。
    """
    env = os.environ.get("MILOCO_HOME", "")
    if env:
        if env.startswith("~"):
            return Path.home() / env[1:].lstrip("/\\")
        return Path(env)
    return Path.home() / ".openclaw" / "miloco"


def miloco_config_file() -> Path:
    """返回 ``$MILOCO_HOME/config.json``（共享嵌套配置文件）。"""
    return miloco_home() / "config.json"
