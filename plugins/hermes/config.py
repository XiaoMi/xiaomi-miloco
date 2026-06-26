import logging
import os
from pathlib import Path

__all__ = [
    "miloco_home",
    "ensure_miloco_home_env",
    "get_plugin_config",
]

logger = logging.getLogger(__name__)


def miloco_home() -> Path:
    env = os.environ.get("MILOCO_HOME")
    if env:
        return Path(env)
    import hermes_constants

    return hermes_constants.get_hermes_home() / "miloco"


def ensure_miloco_home_env() -> Path:
    home = miloco_home()
    os.environ["MILOCO_HOME"] = str(home)
    return home


def get_plugin_config(ctx) -> dict:
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
    except ImportError:
        return {}
    raw = cfg_get(cfg, "plugins", "entries", "miloco", default={})
    return raw if isinstance(raw, dict) else {}
