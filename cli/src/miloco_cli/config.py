"""CLI 配置管理 (``$MILOCO_HOME/config.json``)。

- 结构与 ``backend/miloco/src/miloco/config/settings.schema.json`` 对齐
- 优先级：环境变量 (``MILOCO_*`` / ``MILOCO_SERVER__*`` / ``MILOCO_MODEL__OMNI__*``)
  > ``$MILOCO_HOME/config.json`` > 默认值。
- Token 由 miloco 后端 bootstrap 写入 ``server.token``，CLI 不应覆盖。

schema 白名单与类型由常量 ``_SCHEMA_PATHS`` 维护；新增字段时两处同步。
"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any


def miloco_home() -> Path:
    """返回 ``$MILOCO_HOME``，未设置则落回 ``~/.openclaw/miloco``。"""
    if env := os.environ.get("MILOCO_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".openclaw" / "miloco"


def config_file() -> Path:
    """返回 ``$MILOCO_HOME/config.json``。"""
    return miloco_home() / "config.json"


# ─── schema 白名单（与 settings.schema.json 保持一致） ───────────────────────

# 点号路径 → (python 类型, 默认值, 中文 description)
_SCHEMA_PATHS: dict[str, tuple[type, Any, str]] = {
    "debug": (bool, False, "是否启用调试模式"),
    "timezone": (
        str,
        "",
        "部署时区（IANA 名，如 Asia/Shanghai / America/Los_Angeles）；空 = 跟随系统时区。"
        "影响感知推送与 omni 注入的时刻、\"今天/本周\"等业务概念、API 出口 ISO 偏移",
    ),
    "server.url": (str, "http://127.0.0.1:1810", "miloco 后端 HTTP Base URL"),
    "server.token": (str, "", "后端 Bearer Token（后端首次启动生成，CLI 勿覆盖）"),
    "server.tls_verify": (
        bool,
        False,
        "访问后端时是否校验 TLS 证书；当前 backend 永远 HTTP 故无作用，保留供未来反代场景",
    ),
    "server.python_bin": (str, "", "启动 miloco-backend 的 Python 解释器绝对路径"),
    "server.tls_certfile": (
        str,
        "",
        "【已废弃】backend 永远 HTTP，跨网加密走反代+真证书；写了不生效，仅启动 warning",
    ),
    "server.tls_keyfile": (str, "", "【已废弃】见 tls_certfile"),
    "agent.webhook_url": (
        str,
        "http://127.0.0.1:18789/miloco/webhook",
        "agent webhook 回调地址",
    ),
    "agent.auth_bearer": (
        str,
        "",
        "agent webhook 鉴权 Bearer 值",
    ),
    "model.omni.model": (str, "xiaomi/mimo-v2.5", "多模态模型标识"),
    "model.omni.base_url": (
        str,
        "https://api.xiaomimimo.com/v1",
        "多模态模型服务 Base URL",
    ),
    "model.omni.api_key": (str, "", "多模态模型 API Key"),
    "perception.engine.input.video_short_edge": (
        int,
        512,
        "视频编码短边像素（保持宽高比缩放），重启生效",
    ),
    "perception.engine.input.omni_fps": (
        int,
        1,
        "送给 omni 的视频帧率（应为 fps 的因数），重启生效",
    ),
    "perception.collect.window_size": (
        int,
        4,
        "感知窗口时长（秒），重启生效",
    ),
}

# ─── 基础读写 ────────────────────────────────────────────────────────────────


def atomic_write(path: Path, data: dict) -> None:
    """原子写：临时文件 + ``os.replace``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_raw() -> dict[str, Any]:
    path = config_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    _validate_structure(data)
    return data


def _dict_paths() -> set[str]:
    """schema 中任何点号路径的前缀都必须是 dict。"""
    out: set[str] = set()
    for path in _SCHEMA_PATHS:
        parts = path.split(".")
        for i in range(1, len(parts)):
            out.add(".".join(parts[:i]))
    return out


def _validate_structure(data: dict[str, Any]) -> None:
    """校验 raw config 顶层/中间层是否为 schema 预期的 dict 结构。

    当用户手动编辑 ``config.json`` 写出如 ``"server": "not-a-dict"`` 的非法值时，
    直接 raise ValueError 抛出清晰错误，避免后续调用方在 ``cfg["server"]["url"]``
    处收到指向调用栈而非根因的 ``TypeError``。
    """
    for path in _dict_paths():
        cur: Any = data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                break
            cur = cur[part]
        else:
            if not isinstance(cur, dict):
                raise ValueError(
                    f"config.json 结构错误: 期望 {path!r} 为对象(object), "
                    f"实际为 {type(cur).__name__}。请检查 {config_file()}"
                )


# ─── 嵌套操作 ────────────────────────────────────────────────────────────────


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _get_nested(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _UNSET
        cur = cur[part]
    return cur


def _set_nested(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        node = cur.get(part)
        if not isinstance(node, dict):
            node = {}
            cur[part] = node
        cur = node
    cur[parts[-1]] = value


_UNSET: Any = object()


def _defaults() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, (_, default, _desc) in _SCHEMA_PATHS.items():
        _set_nested(out, path, default)
    return out


def _coerce(path: str, raw: str) -> Any:
    """根据 schema 类型把字符串 CLI 输入转成目标类型。"""
    if path not in _SCHEMA_PATHS:
        raise ValueError(
            f"unknown config path: {path} (known: {', '.join(sorted(_SCHEMA_PATHS))})"
        )
    pytype = _SCHEMA_PATHS[path][0]
    if pytype is bool:
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{path} 需要 bool 值，收到 {raw!r}")
    if pytype is int:
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{path} 需要整数，收到 {raw!r}") from exc
    if pytype is float:
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{path} 需要浮点数，收到 {raw!r}") from exc
    # timezone 额外做 IANA 名校验（与 backend settings 的 field_validator 对齐），
    # 拦住 "Beijing" / "+08:00" 这类会让 backend 启动期 ValidationError 的脏值。
    if path == "timezone" and raw:
        from zoneinfo import available_timezones

        if raw not in available_timezones():
            raise ValueError(
                f"timezone 需要合法 IANA 时区名（如 Asia/Shanghai、America/Los_Angeles），"
                f"收到 {raw!r}"
            )
    return raw  # str


def _warn_fps_divisibility(config: dict[str, Any]) -> None:
    """omni_fps 设置后校验 fps 整除关系，不整除时 warning。"""
    inp = config.get("perception", {}).get("engine", {}).get("input", {})
    fps = inp.get("fps", 3)
    omni_fps = inp.get("omni_fps", 1)
    if omni_fps <= 0 or fps % omni_fps == 0:
        return
    import logging
    new_fps = omni_fps if omni_fps > fps else omni_fps * -(-fps // omni_fps)
    logging.getLogger(__name__).warning(
        "fps(%d) %% omni_fps(%d) != 0：引擎启动时会自动将 fps 调整为 %d 保证整除",
        fps, omni_fps, new_fps,
    )


# ─── 环境变量覆盖 ────────────────────────────────────────────────────────────

_ENV_PREFIX = "MILOCO_"
_ENV_NESTED_DELIM = "__"


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """将 ``MILOCO_SERVER__URL`` 形式的环境变量覆盖进嵌套 dict。"""
    out = deepcopy(config)
    for key, raw_value in os.environ.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        if key in ("MILOCO_HOME",):
            continue  # 控制路径，不是配置
        suffix = key[len(_ENV_PREFIX) :].lower()
        parts = suffix.split(_ENV_NESTED_DELIM)
        path = ".".join(parts)
        if path not in _SCHEMA_PATHS:
            continue  # 忽略未知环境变量
        try:
            value = _coerce(path, raw_value)
        except ValueError:
            continue
        _set_nested(out, path, value)
    return out


# ─── 对外 API ────────────────────────────────────────────────────────────────


def load_config() -> dict[str, Any]:
    """返回合并后的嵌套配置（默认 + config.json + env）。

    只返回嵌套结构 ``{debug, server.*, model.omni.*}``；所有调用方应通过
    ``cfg["server"]["url"]`` / ``get_value("server.url")`` 之类的嵌套路径访问。
    """
    raw_file = _read_raw()
    merged = _deep_merge(_defaults(), raw_file)
    merged = _apply_env_overrides(merged)
    return merged


def show_config() -> dict[str, Any]:
    """返回合并后的配置（用于 ``miloco-cli config show``）。"""
    merged = load_config()
    return merged


def get_value(path: str) -> Any:
    """按点号路径取值；不存在时抛 KeyError。"""
    merged = load_config()
    value = _get_nested(merged, path)
    if value is _UNSET:
        raise KeyError(path)
    return value


def set_value(path: str, raw_value: str) -> Any:
    """校验 + 原子写入 ``$MILOCO_HOME/config.json``；返回写入后的值。"""
    return set_values([(path, raw_value)])[path]


def set_values(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """批量校验并原子写入多个 (path, raw_value)；返回 ``{path: persisted_value}``。

    并发说明：当前业务场景不存在并发写入（install/CLI/plugin 启动均为串行），暂不加锁。
    """
    if not pairs:
        return {}
    resolved: dict[str, Any] = {}
    for path, raw in pairs:
        if path not in _SCHEMA_PATHS:
            raise ValueError(
                f"unknown config path: {path} "
                f"(known: {', '.join(sorted(_SCHEMA_PATHS))})"
            )
        resolved[path] = _coerce(path, raw)
    raw_file = _read_raw()
    for path, value in resolved.items():
        _set_nested(raw_file, path, value)
    atomic_write(config_file(), raw_file)
    if any(p.startswith("perception.engine.input.") for p in resolved):
        _warn_fps_divisibility(raw_file)
    return resolved


def known_paths() -> list[str]:
    """返回全部合法配置点号路径（供 ``config set --help`` 等展示）。"""
    return sorted(_SCHEMA_PATHS)


def describe(path: str) -> str:
    """返回字段的中文 description；未知路径抛 KeyError。"""
    if path not in _SCHEMA_PATHS:
        raise KeyError(path)
    return _SCHEMA_PATHS[path][2]
