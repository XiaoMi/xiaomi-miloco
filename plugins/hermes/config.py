import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    "debug": False,
    "omni_model": "",
    "omni_base_url": "",
    "omni_api_key": "",
    "notify_session_key": "",
    "bridge_host": "127.0.0.1",
    "bridge_port": 18789,
    "bridge_auth_token": "",
}


def miloco_home() -> Path:
    env = os.environ.get("MILOCO_HOME")
    if env:
        return Path(env)
    try:
        import hermes_constants

        return hermes_constants.get_hermes_home() / "miloco"
    except ImportError:
        return Path.home() / ".hermes" / "miloco"


def ensure_miloco_home_env() -> Path:
    home = miloco_home()
    os.environ["MILOCO_HOME"] = str(home)
    return home


def config_file() -> Path:
    return miloco_home() / "config.json"


def read_config_dict() -> dict:
    try:
        text = config_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}


def atomic_write_json(data: dict) -> None:
    home = miloco_home()
    home.mkdir(parents=True, exist_ok=True)
    target = home / "config.json"
    tmp = home / "config.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def deep_merge(target: dict, source: dict) -> None:
    for key, value in source.items():
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(value, dict)
        ):
            deep_merge(target[key], value)
        else:
            target[key] = value


def get_plugin_config(ctx) -> dict:
    try:
        from hermes_cli import config as cli_config
    except ImportError:
        return {}
    plugins = cli_config.config.get("plugins", {})
    entries = plugins.get("entries", {})
    return entries.get("miloco", {})


def load_shared_config(ctx) -> None:
    merged = dict(DEFAULT_CONFIG)
    deep_merge(merged, get_plugin_config(ctx))
    atomic_write_json(merged)
