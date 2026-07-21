"""读取 miloco 共享配置 ``$MILOCO_HOME/config.json``。

移植自 openclaw TypeScript 插件 ``plugins/openclaw/src/miloco/config.ts`` 的
**读取部分**（``loadSharedConfig`` 的纯读分支）——不做写回、不合并 gateway 凭据、
不解析 plugin 自身配置。Hermes 插件侧只关心 ``server.token`` / ``server.url`` /
``model.omni.*`` 这几个 miloco 后端运行时关键字段。

返回值结构（所有字段缺失时给 schema 默认值）::

    {
      "debug": bool,
      "server": {"url": str, "token": str, "tls_verify": bool, "python_bin": str},
      "agent":  {"webhook_url": str, "auth_bearer": str},
      "model":  {"omni": {"model": str, "base_url": str, "api_key": str}},
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from .paths import miloco_config_file

logger = logging.getLogger(__name__)


# 与 backend/miloco/src/miloco/config/settings.schema.json 对齐的默认值。
# 只保留 Hermes 出站插件实际用得到的字段，省略已废弃的 tls_certfile / tls_keyfile。
_DEFAULTS: Dict[str, Any] = {
    "debug": False,
    "server": {
        "url": "http://127.0.0.1:1810",
        "token": "",
        "tls_verify": False,
        "python_bin": "",
    },
    "agent": {
        "webhook_url": "http://127.0.0.1:18789/miloco/webhook",
        "auth_bearer": "",
    },
    "model": {
        "omni": {
            "model": "xiaomi/mimo-v2.5",
            "base_url": "https://api.xiaomimimo.com/v1",
            "api_key": "",
        }
    },
}


def _deep_merge(defaults: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 深合并进 defaults，返回新 dict（不修改入参）。"""
    out: Dict[str, Any] = {}
    for key, dval in defaults.items():
        if isinstance(dval, dict):
            oval = override.get(key, {})
            out[key] = _deep_merge(dval, oval if isinstance(oval, dict) else {})
        elif key in override:
            out[key] = override[key]
        else:
            out[key] = dval
    # 保留 override 里 defaults 没声明的额外字段（schema additionalProperties=true）。
    for key, oval in override.items():
        if key not in out:
            out[key] = oval
    return out


def _safe_load_json(path: Path) -> Dict[str, Any]:
    """读 JSON 文件，缺失/解析失败时返回空 dict（不抛错）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("miloco config.json 解析失败 (%s): %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load_shared_config() -> Dict[str, Any]:
    """读取 ``$MILOCO_HOME/config.json``，用 schema 默认值补齐缺失字段。

    文件缺失或损坏时不抛异常，返回全默认值——Hermes 插件加载不能因此崩。
    返回的 dict 结构稳定，调用方可直接 ``cfg["server"]["token"]`` 取值。
    """
    raw = _safe_load_json(miloco_config_file())
    return _deep_merge(_DEFAULTS, raw)
