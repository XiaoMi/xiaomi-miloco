"""Shared path helpers rooted at ``$MILOCO_HOME``.

``MILOCO_HOME`` is the single user-scoped root for all Miloco state:
``config.json``, logs, DB, supervisor sockets, perception cache, certs, etc.
Default is ``~/.openclaw/miloco``; override via the ``MILOCO_HOME`` env var.

This module intentionally has no dependency on the Pydantic settings module
so that early-boot code (logging setup, config discovery) can import it
without triggering ``get_settings()``.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_MILOCO_HOME = Path.home() / ".openclaw" / "miloco"

# slim（独立 App）发行版的数据根：macOS 规范位置。仅当 MILOCO_HOME 未设置时生效，
# 与 App launcher 显式注入的值保持一致（直接跑内置解释器时也能兜住）。
_DEFAULT_SLIM_HOME = Path.home() / "Library" / "Application Support" / "Miloco"


def miloco_home() -> Path:
    """Return the resolved ``$MILOCO_HOME`` directory.

    - Reads the ``MILOCO_HOME`` environment variable each call (not cached),
      so tests using ``monkeypatch.setenv`` see fresh values immediately.
    - ``~`` is expanded via :meth:`Path.expanduser`.
    - Falls back to ``~/.openclaw/miloco`` when the env var is unset.
    - ``MILOCO_EDITION=slim`` (独立 App) 未设 ``MILOCO_HOME`` 时回退到
      ``~/Library/Application Support/Miloco``。

    这里只看环境变量、**不**读 settings：``get_settings()`` 会经
    ``$MILOCO_HOME/config.json`` 回到本函数，读 settings 会形成递归。
    """
    if env := os.environ.get("MILOCO_HOME"):
        return Path(env).expanduser()
    if (os.environ.get("MILOCO_EDITION") or "").strip().lower() == "slim":
        return _DEFAULT_SLIM_HOME
    return _DEFAULT_MILOCO_HOME


def config_file() -> Path:
    """Return ``$MILOCO_HOME/config.json`` (shared nested config file)."""
    return miloco_home() / "config.json"
