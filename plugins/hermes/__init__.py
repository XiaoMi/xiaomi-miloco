"""Miloco Hermes plugin."""

import logging
import shutil
import subprocess
import threading
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

    try:
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
    except Exception:
        logger.warning("miloco-cli config set failed", exc_info=True)


def _find_binary(name: str, bin_path: str = "") -> str | None:
    if bin_path:
        candidate = Path(bin_path) / name
        if candidate.exists():
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    return None


def _resolve_backend(plugin_cfg: dict) -> str:
    bin_path = plugin_cfg.get("bin_path", "")

    found = _find_binary("miloco-backend", bin_path)
    if found:
        return found

    cli = _find_binary("miloco-cli", bin_path)
    if cli:
        backend = str(Path(cli).parent / "miloco-backend")
        if Path(backend).exists():
            return backend

    try:
        from miloco_cli.config import get_value

        python_bin = get_value("server.python_bin")
        if python_bin and Path(python_bin).exists():
            return python_bin
    except Exception:
        pass

    return "miloco-backend"


def _resolve_cli(plugin_cfg: dict) -> str:
    bin_path = plugin_cfg.get("bin_path", "")

    found = _find_binary("miloco-cli", bin_path)
    if found:
        return found
    return "miloco-cli"


def _start_backend(backend_cmd: str):
    def _run():
        try:
            if backend_cmd.endswith(("python", "python3")):
                cmd = [backend_cmd, "-m", "miloco.main"]
            else:
                cmd = [backend_cmd]
            logger.info("launching miloco-backend: %s", cmd)
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info("miloco-backend started")
        except Exception:
            logger.warning("miloco-backend start failed", exc_info=True)

    threading.Thread(target=_run, daemon=True).start()


def register(ctx):
    ensure_miloco_home_env()
    plugin_cfg = get_plugin_config(ctx)

    cli_path = _resolve_cli(plugin_cfg)
    backend_path = _resolve_backend(plugin_cfg)

    _write_webhook_url(plugin_cfg, cli_path)
    _start_backend(backend_path)

    from .skills_loader import register_skills
    from .hooks import register_hooks
    from .tools import register_tools
    from .cron_sync import register_cron_sync
    from .bridge import register_bridge

    register_skills(ctx)
    register_hooks(ctx)
    register_tools(ctx, plugin_cfg)
    register_cron_sync(ctx)
    register_bridge(ctx, plugin_cfg)
    logger.info("Miloco plugin registered")
