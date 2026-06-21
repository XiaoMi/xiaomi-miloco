"""Miloco Hermes plugin."""

import logging

from . import config as _config

__all__ = ["register", "__version__"]


__version__ = "2.0.0"

logger = logging.getLogger(__name__)


def register(ctx):
    _config.ensure_miloco_home_env()
    _config.load_shared_config(ctx)
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
