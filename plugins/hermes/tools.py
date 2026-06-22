import json
import logging
import asyncio
from typing import Any, Dict

from . import schemas
from .suggestions import apply_habit_action

__all__ = [
    "register_tools",
]

logger = logging.getLogger(__name__)


def _resolve_platform_adapter(platform_name: str):
    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config()
    try:
        platform = Platform(platform_name)
    except ValueError:
        logger.error("unknown deliver platform: %s", platform_name)
        return None, None

    pconfig = config.platforms.get(platform)
    if not pconfig:
        logger.error("platform %s not configured in gateway", platform_name)
        return None, None

    from gateway.run import _gateway_runner_ref

    runner = _gateway_runner_ref()
    if runner is not None:
        adapter = runner.adapters.get(platform)
        if adapter is not None:
            return adapter, pconfig

    logger.error(
        "platform %s adapter not available (gateway not running?)", platform_name
    )
    return None, None


def _resolve_home_channel(platform_name: str):
    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config()
    try:
        platform = Platform(platform_name)
    except ValueError:
        return None, None

    hc = config.get_home_channel(platform)
    if hc and hc.chat_id:
        return hc.chat_id, hc.thread_id
    return None, None


def _resolve_deliver_target(plugin_cfg: Dict[str, Any]):
    deliver = (plugin_cfg.get("deliver") or "").strip()
    deliver_extra = plugin_cfg.get("deliver_extra") or {}
    chat_id = (deliver_extra.get("chat_id") or "").strip()
    thread_id = (deliver_extra.get("message_thread_id") or "").strip()

    if not deliver:
        logger.warning(
            "deliver not configured; notification will not be sent. "
            "Set plugins.entries.miloco.deliver in config.yaml"
        )
        return None, None, None

    adapter, _pconfig = _resolve_platform_adapter(deliver)
    if adapter is None:
        return None, None, None

    if not chat_id:
        chat_id, home_thread_id = _resolve_home_channel(deliver)
        if not chat_id:
            logger.warning(
                "deliver_extra.chat_id not set and no home channel for %s; "
                "notification will not be sent",
                deliver,
            )
            return None, None, None
        if not thread_id and home_thread_id:
            thread_id = home_thread_id

    return adapter, chat_id, thread_id


def _deliver_notification(content: str, plugin_cfg: Dict[str, Any]) -> Dict[str, Any]:
    adapter, chat_id, thread_id = _resolve_deliver_target(plugin_cfg)
    if adapter is None:
        return {"ok": False, "error": "no deliver target available"}

    metadata = {}
    if thread_id:
        metadata["thread_id"] = thread_id

    try:
        result = asyncio.get_event_loop().run_until_complete(
            adapter.send(chat_id, content, metadata=metadata or None)
        )
        if result.success:
            return {"ok": True, "platform": type(adapter).__name__}
        return {"ok": False, "error": result.error or "send failed"}
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                adapter.send(chat_id, content, metadata=metadata or None)
            )
            if result.success:
                return {"ok": True, "platform": type(adapter).__name__}
            return {"ok": False, "error": result.error or "send failed"}
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("deliver notification failed")
        return {"ok": False, "error": str(exc)}


def _miloco_im_push_handler(args, **kwargs):
    message = args.get("message", "")
    deliver_message = "<miloco-notification>" + message + "</miloco-notification>"
    plugin_cfg = kwargs.get("plugin_cfg") or {}
    result = _deliver_notification(deliver_message, plugin_cfg)
    return json.dumps(result)


def _miloco_habit_suggest_handler(args, **kwargs):
    return json.dumps(apply_habit_action(args))


def register_tools(ctx, plugin_cfg=None):
    plugin_cfg = plugin_cfg or {}
    ctx.register_tool(
        name=schemas.MILOCO_IM_PUSH["name"],
        toolset="miloco",
        schema=schemas.MILOCO_IM_PUSH,
        handler=lambda args, **kw: _miloco_im_push_handler(args, plugin_cfg=plugin_cfg),
    )
    ctx.register_tool(
        name=schemas.MILOCO_HABIT_SUGGEST["name"],
        toolset="miloco",
        schema=schemas.MILOCO_HABIT_SUGGEST,
        handler=_miloco_habit_suggest_handler,
    )
