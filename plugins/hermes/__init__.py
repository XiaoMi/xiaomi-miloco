"""Miloco Hermes plugin."""

import logging
import subprocess
import threading

from . import config as _config

__all__ = ["register", "__version__"]


__version__ = "2.0.0"

logger = logging.getLogger(__name__)


def _start_backend():
    def _run():
        try:
            subprocess.Popen(
                ["miloco-backend"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info("miloco-backend started")
        except Exception:
            logger.warning("miloco-backend start failed", exc_info=True)

    threading.Thread(target=_run, daemon=True).start()


def register(ctx):
    _config.ensure_miloco_home_env()
    _config.load_shared_config(ctx)
    _start_backend()
    from .skills_loader import register_skills
    from .hooks import register_hooks
    from .tools import register_tools
    from .cron_sync import register_cron_sync
    from .bridge import register_bridge

    register_skills(ctx)
    register_hooks(ctx)
    register_tools(ctx)
    register_cron_sync(ctx)
    register_bridge(ctx)
    logger.info("Miloco plugin registered")
