"""Miloco Hermes plugin."""

import logging
import shutil
import subprocess
from pathlib import Path

from .config import ensure_miloco_home_env, get_plugin_config

__all__ = ["register", "__version__"]

__version__ = "2.0.0"

logger = logging.getLogger(__name__)

_BRIDGE_HOST = "127.0.0.1"
_BRIDGE_PORT = 18789


def _write_webhook_url(plugin_cfg: dict, cli_path: str) -> None:
    host = plugin_cfg.get("bridge_host", _BRIDGE_HOST)
    port = plugin_cfg.get("bridge_port", _BRIDGE_PORT)
    webhook_url = f"http://{host}:{port}/miloco/webhook"

    result = subprocess.run(
        [cli_path, "config", "set", "agent.webhook_url", webhook_url, "--no-restart"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        logger.info("agent.webhook_url set to %s", webhook_url)
    else:
        logger.warning("miloco-cli config set failed: %s", result.stderr.strip())


def _find_binary(name: str, bin_path: str = "") -> str | None:
    if bin_path:
        candidate = Path(bin_path) / name
        if candidate.exists():
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    return None


def _resolve_cli(plugin_cfg: dict) -> str:
    bin_path = plugin_cfg.get("bin_path", "")

    found = _find_binary("miloco-cli", bin_path)
    if found:
        return found
    return "miloco-cli"


def _start_backend(cli_path: str):
    result = subprocess.run(
        [cli_path, "service", "start", "--pretty"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        logger.info("miloco-backend started via miloco-cli")
    else:
        logger.warning(
            "miloco-cli service start failed: %s",
            (result.stdout + result.stderr).strip(),
        )


def register(ctx):
    ensure_miloco_home_env()
    plugin_cfg = get_plugin_config(ctx)

    cli_path = _resolve_cli(plugin_cfg)

    _write_webhook_url(plugin_cfg, cli_path)
    _start_backend(cli_path)

    from .bridge import register_bridge
    from .cron_sync import register_cron_sync
    from .hooks import register_hooks
    from .skills_loader import register_skills
    from .tools import register_tools

    register_skills(ctx)
    register_hooks(ctx)
    register_tools(ctx, plugin_cfg)
    register_cron_sync(ctx)
    register_bridge(ctx, plugin_cfg)
    logger.info("Miloco plugin registered")
