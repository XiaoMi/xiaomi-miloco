"""Shared nested config helpers for ``$MILOCO_HOME/config.json``.

Shared config is a multi-writer store: any of the collaborating components
can update it independently —

- CLI   : ``miloco-cli config set <path> <value>`` writes arbitrary schema
          paths (``server.url``, ``model.omni.api_key``, ``server.token``, ...).
- Plugin: the openclaw plugin writes fields it owns (e.g.
          ``agent.webhook_url``, ``agent.auth_bearer``).
- Backend: ``ensure_backend_token()`` persists ``server.token``; other
           backend-side writes go through :func:`update_shared_config`.

All writers deep-merge into the same file via an atomic ``tmpfile +
os.replace``, so concurrent writes don't produce partial JSON. Whichever
writer publishes a field first wins, and the others pick it up on next read.

Backend token bootstrap priority (see :func:`ensure_backend_token`):
  ``MILOCO_SERVER__TOKEN`` env / ``settings.server.token`` (already loaded)
  > existing ``config.json`` token (stable across restarts, and respects
    values written by CLI/backend)
  > new UUID (first boot, no writer has claimed the token yet).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from miloco.config import get_settings, reset_settings
from miloco.middleware.exceptions import BadRequestException
from miloco.utils.common import deep_merge
from miloco.utils.paths import config_file

logger = logging.getLogger(__name__)


class ConfigFileUnreadableError(BadRequestException):
    """``config.json`` 存在但读不懂（损坏 / 顶层不是 JSON 对象）。

    合并写入必须以完整的旧文件为基底。把读失败当空配置会让 ``deep_merge``
    退化成整体覆盖，一次保存就清掉 ``server.token`` / ``model.omni.api_key``
    等其它段落（见 issue #569：保存后 503 无法打开页面）。

    继承 ``BadRequestException``：HTTP 入口的统一异常中间件会把它变成
    400 + 可读 message，前端「保存失败」提示里能看到原因。
    """


def _user_config_path() -> Path:
    """Return ``$MILOCO_HOME/config.json`` (single source of shared config)."""
    return config_file()


def _read_config_dict(path: Path) -> dict[str, Any]:
    """读出待合并的现有配置：文件不存在（或为空）返回 {}，存在但读不懂则抛错。

    只有「文件里确实没有可保留的内容」才按空配置初始化；文件存在但解析不出
    JSON 对象时抛 :class:`ConfigFileUnreadableError`，由调用方拒绝写盘，
    保住用户已有的段落等人工恢复（``config.json.bak.*``）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigFileUnreadableError(f"config.json 无法读取: {path}: {e}") from e
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigFileUnreadableError(f"config.json 无法解析: {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigFileUnreadableError(
            f"config.json 顶层不是 JSON 对象: {path}: {type(data).__name__}"
        )
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically via tmpfile + fsync + ``os.replace``.

    fsync 数据页与目录项后再 rename（与 CLI ``atomic_write`` 同款），
    掉电也不会留下改了名却没落盘的空文件 —— 空文件正是喂给
    「读失败按空配置处理」类缺陷的损坏源。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # 目录项 fsync 是掉电保险；Windows 打不开目录句柄，退化为跳过
        # （文件内容已在上面 fsync，rename 本身原子）。
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_backend_token() -> str:
    """Resolve the backend bearer token and persist it under ``server.token``.

    Returns the resolved token. Called once during bootstrap.
    """
    path = _user_config_path()
    try:
        existing = _read_config_dict(path)
    except ConfigFileUnreadableError as e:
        # 启动不能反过来把用户配置清空：跳过落盘（update_shared_config 同样会
        # 拒绝写入），用本次内存 token 起服务，等人工恢复 config.json 后重启。
        logger.error(
            "跳过 token 落盘以免覆盖 config.json 其它段落（请恢复备份后重启）: %s",
            e,
        )
        return get_settings().server.token or str(uuid.uuid4())

    settings_token = get_settings().server.token
    if settings_token:
        token = settings_token
    else:
        existing_token = (
            existing.get("server", {}).get("token")
            if isinstance(existing.get("server"), dict)
            else None
        )
        token = existing_token or str(uuid.uuid4())

    persisted = (
        existing.get("server", {}).get("token")
        if isinstance(existing.get("server"), dict)
        else None
    )
    if persisted != token:
        update_shared_config(server={"token": token})
        logger.info("Persisted backend token to %s", path)

    return token


def update_shared_config(**updates: Any) -> dict[str, Any]:
    """Deep-merge ``updates`` into ``$MILOCO_HOME/config.json`` and persist.

    现有文件读不懂时抛 :class:`ConfigFileUnreadableError` 并拒绝写盘，
    避免把「深合并」退化成「只落本次更新的字段」的整体覆盖。

    Concurrency note: The current deployment has no concurrent writers —
    bootstrap and CLI writes are serialised by install.sh / user workflow.
    If that assumption changes, add file-level locking here.
    """
    path = _user_config_path()
    existing = _read_config_dict(path)
    merged = deep_merge(existing, updates)
    _atomic_write_json(path, merged)
    reset_settings()
    return merged
