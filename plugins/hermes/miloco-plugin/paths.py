"""MILOCO_HOME 路径解析。

本模块只在 Hermes runtime 里被加载（context_injection / trace / adapter /
tools_habit 等 hermes plugin 内部文件调用），跟 openclaw runtime 隔离。所以默认
路径也对齐 hermes runtime 的默认（``~/.hermes/miloco``）——env 传递失败时不去
误读 openclaw 数据，避免 split-brain。

优先读 ``$MILOCO_HOME``（install-hermes.sh 会写进 shell rc 和 ~/.hermes/.env），
未设置才落回默认；后端 Python 侧 ``miloco.utils.paths`` 与 CLI 侧
``miloco_cli.config`` 是两个 runtime 共享代码，仍默认 openclaw 兼容历史契约。
"""

from __future__ import annotations

import os
from pathlib import Path


def miloco_home() -> Path:
    """返回 ``$MILOCO_HOME``，未设置则使用 ``~/.hermes/miloco``（本模块只 hermes 加载）。

    每次调用都读取环境变量，便于测试用 ``MILOCO_HOME`` 临时注入。
    以 ``~`` 开头的值会按主目录展开（与 TS 端 ``env.startsWith("~")`` 分支一致）。
    """
    env = os.environ.get("MILOCO_HOME", "")
    if env:
        if env.startswith("~"):
            return Path.home() / env[1:].lstrip("/\\")
        return Path(env)
    return Path.home() / ".hermes" / "miloco"


def miloco_config_file() -> Path:
    """返回 ``$MILOCO_HOME/config.json``（共享嵌套配置文件）。"""
    return miloco_home() / "config.json"
