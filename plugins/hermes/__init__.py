"""Miloco Hermes plugin."""

import logging
import shutil
import subprocess
from pathlib import Path

from .config import ensure_miloco_home_env, get_plugin_config

__all__ = ["register", "__version__"]

__version__ = "2.0.0"

logger = logging.getLogger(__name__)


def _resolve_dashboard_url(plugin_cfg: dict) -> str:
    from hermes_cli.config import cfg_get, load_config

    cfg = load_config()
    host = cfg_get(cfg, "dashboard", "host", default="127.0.0.1")
    port = cfg_get(cfg, "dashboard", "port", default=9119)
    return f"http://{host}:{port}"


def _write_webhook_url(plugin_cfg: dict, cli_path: str) -> None:
    base_url = _resolve_dashboard_url(plugin_cfg)
    webhook_url = f"{base_url}/api/plugins/miloco/webhook"

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

    from .skills_loader import register_skills
    from .hooks import register_hooks
    from .tools import register_tools
    from .cron_sync import register_cron_sync

    register_skills(ctx)
    register_hooks(ctx)
    register_tools(ctx, plugin_cfg)
    register_cron_sync(ctx)
    logger.info("Miloco plugin registered")
