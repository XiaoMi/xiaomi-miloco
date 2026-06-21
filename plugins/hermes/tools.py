import json

from . import schemas
from .config import atomic_write_json, read_config_dict
from .suggestions import apply_habit_action

__all__ = [
    "register_tools",
]

_BIND_HINT_EXAMPLE = {
    "not_configured": "您尚未设置 Miloco 通知频道，本条消息已临时发送到最近活跃的对话。回复「绑定通知频道」可将当前对话设为固定的 Miloco 通知频道，后续提醒、定时任务、告警等通知都将发送至此。",
    "configured_but_invalid": "您原先绑定的 Miloco 通知频道已失效，本条消息已临时发送到最近活跃的对话。请回复「绑定通知频道」重新绑定。",
}

_NO_CHANNEL_ERROR = "no available IM channel"


def _resolve_notify_target():
    cfg = read_config_dict()
    key = cfg.get("notify_session_key") or ""
    if key:
        return {"target": {"session_key": key}, "needs_bind": False}
    return {
        "target": None,
        "needs_bind": True,
        "bind_reason": "not_configured",
    }


def _deliver_notification(session_key, message):
    return {"ok": True}


def _miloco_im_push_handler(args, **kwargs):
    message = args.get("message", "")
    bind_hint = (args.get("bindHint") or "").strip()
    resolved = _resolve_notify_target()
    target = resolved.get("target")
    needs_bind = resolved.get("needs_bind", False)
    bind_reason = resolved.get("bind_reason")

    if target is None and not needs_bind:
        return json.dumps({"ok": False, "error": _NO_CHANNEL_ERROR})

    if needs_bind and not bind_hint:
        return json.dumps(
            {
                "ok": False,
                "needsBind": True,
                "bindReason": bind_reason,
                "bindHintExample": _BIND_HINT_EXAMPLE.get(
                    bind_reason, _BIND_HINT_EXAMPLE["not_configured"]
                ),
                "error": "本条通知尚未发出，请补上 bindHint 后再次调用",
                "nextAction": (
                    "保持 message 不变，补上 bindHint 参数（把 bindHintExample "
                    "翻译成主人当前使用的语言）再次调用，通知才会真正发送"
                ),
            }
        )

    if needs_bind and bind_hint:
        body = message + "\n---\n" + bind_hint
    else:
        body = message

    deliver_message = "<miloco-notification>" + body + "</miloco-notification>"
    session_key = target.get("session_key") if target else None
    deliver_result = _deliver_notification(session_key, deliver_message)
    result = dict(deliver_result)
    if needs_bind:
        result["fallback"] = True
    elif target:
        result["channel"] = target.get("session_key")
    return json.dumps(result)


def _miloco_notify_bind_handler(args, **kwargs):
    session_key = args.get("sessionKey") or kwargs.get("session_key")
    if not session_key:
        return json.dumps(
            {"ok": False, "error": "未指定 sessionKey 且当前上下文无 sessionKey"}
        )
    cfg = read_config_dict()
    cfg["notify_session_key"] = session_key
    atomic_write_json(cfg)
    return json.dumps({"ok": True, "session_key": session_key})


def _miloco_habit_suggest_handler(args, **kwargs):
    return json.dumps(apply_habit_action(args))


def register_tools(ctx):
    ctx.register_tool(
        name=schemas.MILOCO_IM_PUSH["name"],
        toolset="miloco",
        schema=schemas.MILOCO_IM_PUSH,
        handler=_miloco_im_push_handler,
    )
    ctx.register_tool(
        name=schemas.MILOCO_NOTIFY_BIND["name"],
        toolset="miloco",
        schema=schemas.MILOCO_NOTIFY_BIND,
        handler=_miloco_notify_bind_handler,
    )
    ctx.register_tool(
        name=schemas.MILOCO_HABIT_SUGGEST["name"],
        toolset="miloco",
        schema=schemas.MILOCO_HABIT_SUGGEST,
        handler=_miloco_habit_suggest_handler,
    )
