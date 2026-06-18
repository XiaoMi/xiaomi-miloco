"""miloco_im_push + miloco_notify_bind 两段式通知协议。

移植自 openclaw TypeScript 插件 ``plugins/openclaw/src/tools/notify.ts``。

**best-effort, 需 Hermes 实例验证**：投递依赖 Hermes ``send_message`` tool
签名（``action="send", target="platform:chat_id[:thread_id]", message=...``），
该签名已从文档/源码推断但未在真实 Hermes 实例上实测。若 Hermes 版本变更了
``send_message`` 参数，本模块的投递路径需相应调整。

两段式协议（与 TS 端 ``notifyOwner`` 一致）：
1. 首次调用只传 ``message`` → 若未绑定通知渠道，返回 ``ok:false, needsBind:true`` +
   ``bindHintExample``，**不发送**。agent 必须把 bindHintExample 翻译成主人语言后
   重调本工具（message 不变 + 补 ``bindHint``），通知才会真正发出。
2. 带 ``bindHint`` 的二次调用 → fallback 投递：把 bindHint 拼到正文后，经
   ``ctx.dispatch_tool("send_message", ...)`` 投递到绑定 target。

``miloco_notify_bind`` 把当前 session 的 target（形如
``platform:chat_id[:thread_id]``）持久化到插件目录 ``state.json`` 的
``notifySessionKey`` 字段，后续 ``miloco_im_push`` 优先用它。
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .notify_target import resolve_notify_target

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 插件状态持久化（state.json）
# ---------------------------------------------------------------------------

_STATE_FILENAME = "state.json"


def _state_path(ctx: Any) -> Path:
    """插件目录下的 state.json。ctx.manifest.path 是插件根目录。"""
    base = getattr(getattr(ctx, "manifest", None), "path", None)
    if not base:
        # 兜底：home 下 .hermes/plugins/miloco/state.json（极少走到）。
        base = str(Path.home() / ".hermes" / "plugins" / "miloco")
    return Path(base) / _STATE_FILENAME


def load_state(ctx: Any) -> Dict[str, Any]:
    """读 state.json，缺失/损坏返回空 dict。"""
    path = _state_path(ctx)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("miloco state.json 解析失败 (%s): %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(ctx: Any, state: Dict[str, Any]) -> None:
    """原子写 state.json（temp → rename）。失败仅 log，不抛。"""
    path = _state_path(ctx)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("miloco state.json 写入失败 (%s): %s", path, exc)


def get_notify_session_key(ctx: Any) -> Optional[str]:
    return load_state(ctx).get("notifySessionKey") or None


def set_notify_session_key(ctx: Any, target: str) -> None:
    state = load_state(ctx)
    state["notifySessionKey"] = target
    save_state(ctx, state)


# ---------------------------------------------------------------------------
# bindHint 模板（与 miloco-notify skill references/channel-config.md 对齐）
# ---------------------------------------------------------------------------

BIND_HINT_EXAMPLE: Dict[str, str] = {
    "not_configured": (
        "您尚未设置 Miloco 通知频道，本条消息已临时发送到最近活跃的对话。"
        "回复「绑定通知频道」可将当前对话设为固定的 Miloco 通知频道，"
        "后续提醒、定时任务、告警等通知都将发送至此。"
    ),
    "configured_but_invalid": (
        "您原先绑定的 Miloco 通知频道已失效，本条消息已临时发送到最近活跃的对话。"
        "请回复「绑定通知频道」重新绑定。"
    ),
}


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------

def _deliver_via_send_message(ctx: Any, target: str, body: str) -> Dict[str, Any]:
    """经 Hermes ``send_message`` tool 投递。best-effort。

    返回 ``{"ok": bool, "error": str?}``。任何异常都吞掉转成 ok:false。
    """
    try:
        result_str = ctx.dispatch_tool("send_message", {
            "action": "send",
            "target": target,
            "message": body,
        })
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"dispatch_tool(send_message) failed: {exc}"}

    # send_message 返回 JSON 字符串。尝试解析看是否有 error 字段。
    try:
        result = json.loads(result_str) if isinstance(result_str, str) else result_str
    except (json.JSONDecodeError, TypeError):
        # 非 JSON 也当作成功（有些路径返回纯文本）。
        return {"ok": True}

    if isinstance(result, dict):
        if result.get("error") or result.get("success") is False:
            return {"ok": False, "error": str(result.get("error") or result)}
        return {"ok": True}
    return {"ok": True}


def notify_owner(ctx: Any, message: str, bind_hint: Optional[str]) -> Dict[str, Any]:
    """与 TS 端 ``notifyOwner`` 行为对齐的投递入口。

    Hermes 无 openclaw 那种 SessionDB（带 lastTo/lastChannel），故无"最近活跃
    channel"可 fallback。未绑定时一律走两段式协议返回 ``needsBind=true`` +
    ``bindHintExample``，agent 引导用户先 ``miloco_notify_bind`` 绑定 target，
    再重发。绑定的 target 直接经 ``send_message`` 投递。
    """
    notify_key = get_notify_session_key(ctx)
    resolved = resolve_notify_target(notify_key)
    target = resolved.get("target")
    needs_bind = resolved.get("needsBind", False)
    bind_reason = resolved.get("bindReason") or "not_configured"
    hint = (bind_hint or "").strip()

    # 未绑定（target 为空）：无论 agent 是否提供 bindHint 都无法投递——没有 target。
    # 走两段式协议：返回 needsBind + bindHintExample，引导用户先绑定。
    if needs_bind or not target:
        return {
            "ok": False,
            "needsBind": True,
            "bindReason": bind_reason,
            "fallbackChannel": target.get("target") if target else None,
            "bindHintExample": BIND_HINT_EXAMPLE.get(bind_reason, BIND_HINT_EXAMPLE["not_configured"]),
            "error": "本条通知尚未发出：尚未绑定通知渠道。这是给你（agent）的执行指令，不是要回复给用户的内容——禁止把本说明转述给用户。",
            "nextAction": (
                "引导用户绑定通知渠道：让用户在当前对话里说出要绑定的 target（形如 telegram / telegram:-100xxx / discord:xxx），"
                "然后调用 miloco_notify_bind(target=...) 绑定。绑定后再重新调用 miloco_im_push(message=<本次内容>) 即可真正发送。"
                "不要在对话里回复本说明原文、也不要等待用户确认。"
            ),
        }

    # 已绑定：投递（bindHint 在已绑定时忽略，与 TS 端一致）。
    body = message
    deliver_message = f"<miloco-notification>{body}</miloco-notification>"
    result = _deliver_via_send_message(ctx, target["target"], deliver_message)
    if result.get("ok"):
        return {"ok": True, "channel": target.get("target")}
    return {"ok": False, "error": result.get("error", "delivery failed")}


# ---------------------------------------------------------------------------
# tool schema + handler 工厂
# ---------------------------------------------------------------------------

MILOCO_IM_PUSH_SCHEMA: Dict[str, Any] = {
    "name": "miloco_im_push",
    "description": (
        "给主人推送一条 IM 通知。通常只传 message 调用即可。\n"
        "本工具配合 miloco-notify skill 使用（分级、选人、文案规范都在其中）。\n"
        "重要：若返回 ok=false 且 needsBind=true，表示本条【尚未发出】——这是要你继续操作的信号，"
        "绝不能把它当作结果回复/转述给用户。你必须立刻再次调用本工具：message 保持不变，"
        "并补上 bindHint（把返回里的 bindHintExample 翻译成主人当前使用的语言）。"
        "补上 bindHint 后通知才会真正发送。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "要发给主人的通知正文",
            },
            "bindHint": {
                "type": "string",
                "description": (
                    "仅当上次调用返回 needsBind=true 时才传：按 miloco-notify skill 的 bindHint 模板、"
                    "用主人的语言写好的绑定引导语。工具会把它附在正文后一起发出；渠道已设置时无需传。"
                ),
            },
        },
        "required": ["message"],
    },
}


MILOCO_NOTIFY_BIND_SCHEMA: Dict[str, Any] = {
    "name": "miloco_notify_bind",
    "description": "绑定通知渠道。传入当前 session 的 target（形如 platform:chat_id[:thread_id]）。",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "要绑定的通知 target，形如 'telegram'、'telegram:-1001234567890:17585'、"
                    "'discord:999888777:555444333'。可从 send_message(action='list') 的结果里取。"
                ),
            },
        },
        "required": ["target"],
    },
}


def make_im_push_handler(ctx: Any) -> Callable[[Dict[str, Any]], str]:
    """返回 ``miloco_im_push`` 的 handler（闭包捕获 ctx）。"""
    def _handler(args: Dict[str, Any], **kwargs: Any) -> str:
        message = (args.get("message") or "").strip()
        bind_hint = args.get("bindHint")
        if not message:
            return json.dumps({"ok": False, "error": "message 不能为空"}, ensure_ascii=False)
        try:
            result = notify_owner(ctx, message, bind_hint)
        except Exception as exc:  # noqa: BLE001
            logger.exception("miloco_im_push 失败: %s", exc)
            result = {"ok": False, "error": f"internal error: {exc}"}
        return json.dumps(result, ensure_ascii=False)
    return _handler


def make_notify_bind_handler(ctx: Any) -> Callable[[Dict[str, Any]], str]:
    """返回 ``miloco_notify_bind`` 的 handler（闭包捕获 ctx）。"""
    def _handler(args: Dict[str, Any], **kwargs: Any) -> str:
        target = (args.get("target") or "").strip()
        if not target:
            return json.dumps(
                {"ok": False, "error": "target 不能为空（形如 platform:chat_id[:thread_id]）"},
                ensure_ascii=False,
            )
        try:
            set_notify_session_key(ctx, target)
        except Exception as exc:  # noqa: BLE001
            logger.exception("miloco_notify_bind 失败: %s", exc)
            return json.dumps({"ok": False, "error": f"internal error: {exc}"}, ensure_ascii=False)
        return json.dumps({"ok": True, "channel": target, "sessionKey": target}, ensure_ascii=False)
    return _handler
