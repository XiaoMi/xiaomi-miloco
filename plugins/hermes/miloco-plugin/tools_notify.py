"""miloco_im_push 通知投递。

对齐 OpenClaw 版 ``subagent.run({deliver:true})`` 的体验：装好就能用，cron
场景下也能自动投递，不需要 LLM 配合做"两段式 bind"。

**投递路径**：读插件 state.json 里的 ``deliver.target``（格式
``platform[:chat_id[:thread_id]]``，对齐 Hermes 官方 ``hermes send`` CLI 的 ``--to`` 参数格式），通过
``subprocess.run(["hermes", "send", "--to", target, "--json", "-q", body])`` 投递。

为什么不用 ``ctx.dispatch_tool("send_message", ...)``：Hermes 从某个版本起
故意把 ``send_message`` 从 agent-callable model tools 里移除（见 hermes-agent
源码 ``tools/send_message_tool.py:1680-1691`` 注释），目的是防止 agent 自作
主张发跨平台消息。``hermes send`` 是 Hermes 官方为 cron / ops script / 监控
daemon 提供的 standalone 入口（``hermes_cli/send_cmd.py``），不依赖 agent
loop、不需要 gateway 运行（bot-token 类平台走 REST 直发，plugin 类平台走
registry 的 standalone_sender_fn）。

**state.json 由 install-hermes.sh 在安装时自动写**：探测 ~/.hermes/config.yaml
里已配 bot_token 的 platform，取第一个作为默认 deliver target，用户零感知。
若未检测到任何已配平台，state.json 里无 deliver 字段，im_push 返回
``ok:false, error:"no deliver target configured"``，提示用户去 Hermes 里配 IM
或手动编辑 state.json。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# state.json 文件名（与 OpenClaw 版 notify.ts 对齐，存于插件根目录）
_STATE_FILENAME = "state.json"

# hermes send CLI 超时（秒）。bot-token REST 投递通常 < 5s，留余量给慢通道
_HERMES_SEND_TIMEOUT_S = 30


def _state_path(ctx: Any) -> Path:
    """插件目录下的 state.json。

    优先级：
    1. ``ctx.manifest.path`` —— dev 安装（fork 仓库）下是真目录
    2. ``$HERMES_HOME/plugins/miloco/miloco-plugin/`` —— install-hermes.sh 的
       唯一装入点（不管 dev / pip 装，state.json 实际都落这）
    3. 兜底 ``~/.hermes/plugins/miloco/miloco-plugin/``

    pip entry-point 装的 plugin（``manifest.path = "pkg.module:entry"``）不是
    目录，不能用；直接走 2。
    """
    base = getattr(getattr(ctx, "manifest", None), "path", None)
    if base and Path(base).is_dir():
        return Path(base) / _STATE_FILENAME
    # install-hermes.sh 把 plugin 装到 $HERMES_HOME/plugins/miloco/miloco-plugin/，
    # state.json 也写这。这是 source of truth。
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return hermes_home / "plugins" / "miloco" / "miloco-plugin" / _STATE_FILENAME


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


def get_deliver_target(ctx: Any) -> Optional[str]:
    """从 state.json 读 deliver.target；缺失返回 None。

    返回值是 Hermes ``send_message`` 工具接受的 target 字符串，格式
    ``platform[:chat_id[:thread_id]]``。裸 ``platform`` 表示用 home channel。
    """
    return load_state(ctx).get("deliver", {}).get("target") or None


def set_deliver_target(ctx: Any, target: str) -> None:
    """手动覆盖 deliver.target（高级用户用；正常路径 install-hermes.sh 自动写）。"""
    state = load_state(ctx)
    state["deliver"] = {
        "target": target,
        "auto_configured": False,
        "configured_at": None,
        "source": "manual set via plugin API",
    }
    save_state(ctx, state)


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------

def _deliver_via_hermes_send(target: str, body: str) -> Dict[str, Any]:
    """经 ``hermes send --to TARGET --json -q BODY`` 投递。

    ``hermes`` CLI（hermes-agent/hermes_cli/send_cmd.py）是 Hermes 官方为
    cron / ops script 提供的 standalone 入口。subprocess 调它而不是
    ``ctx.dispatch_tool("send_message", ...)``，因为后者在当前 Hermes 版本里
    会报 "Unknown tool: send_message"（send_message 已从 model tools 移除，
    见 tools/send_message_tool.py:1680-1691 注释）。

    返回 ``{"ok": bool, "error"?: str, "platform"?: str, "chat_id"?: str}``。
    """
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        return {
            "ok": False,
            "error": (
                "找不到 hermes CLI（PATH 里没有 'hermes'）。"
                "Hermes Agent 没装？或没把 ~/.hermes/bin 加 PATH？"
            ),
        }

    cmd = [
        hermes_bin,
        "send",
        "--to", target,
        "--json",
        "-q",
        body,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_HERMES_SEND_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"hermes send 超时（>{_HERMES_SEND_TIMEOUT_S}s）：{target}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"hermes send 调用失败: {exc}"}

    stdout = (proc.stdout or "").strip()
    payload: Dict[str, Any] = {}
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {"error": f"hermes send 返回非 JSON: {stdout!r}"}

    # exit code: 0 = ok, 1 = delivery fail, 2 = usage error
    # 接受 success=True 或 ok=True 作为成功标志（Hermes 当前用 success，但
    # 前向兼容 ok=，未来 Hermes 改签名不会突然全挂）
    if proc.returncode == 0:
        if isinstance(payload, dict) and (
            payload.get("success") is True or payload.get("ok") is True
        ):
            return {
                "ok": True,
                "platform": payload.get("platform"),
                "chat_id": payload.get("chat_id"),
                "skipped": payload.get("skipped", False),
            }
        if isinstance(payload, dict) and payload.get("skipped"):
            return {
                "ok": True,
                "skipped": True,
                "reason": payload.get("reason"),
                "note": payload.get("note"),
            }
        return {
            "ok": False,
            "error": str(payload.get("error") if isinstance(payload, dict) else payload),
        }

    err_msg = ""
    if isinstance(payload, dict):
        err_msg = str(payload.get("error") or "")
    if not err_msg:
        err_msg = (proc.stderr or "").strip() or f"hermes send exit={proc.returncode}"
    return {"ok": False, "error": err_msg}


def notify_owner(ctx: Any, message: str) -> Dict[str, Any]:
    """投递入口。与 OpenClaw 版 ``notifyOwner`` 行为对齐：装好就能用。

    没有 deliver target → 返回 ok:false + 明确错误，指引用户去装 IM 或手动
    编辑 state.json。不再做"两段式 bind"——cron session 没人可对话，bind 走
    不通。
    """
    target = get_deliver_target(ctx)
    if not target:
        return {
            "ok": False,
            "error": (
                "no deliver target configured. Run install-hermes.sh after connecting "
                "an IM platform in Hermes, or manually edit state.json "
                "(`{\"deliver\": {\"target\": \"telegram\"}}`)."
            ),
        }
    body = f"<miloco-notification>{message}</miloco-notification>"
    return _deliver_via_hermes_send(target, body)


# ---------------------------------------------------------------------------
# tool schema + handler 工厂
# ---------------------------------------------------------------------------

MILOCO_IM_PUSH_SCHEMA: Dict[str, Any] = {
    "name": "miloco_im_push",
    "description": (
        "给主人推送一条 IM 通知。通常只传 message 调用即可——通知会自动送到 "
        "install-hermes.sh 配置好的 IM 频道（无需 bind）。\n"
        "本工具配合 miloco-notify skill 使用（分级、选人、文案规范都在其中）。\n"
        "失败时返回 ok=false + error：常见原因是 Hermes 还没接 IM 平台，"
        "按 error 提示跑 hermes config set 或编辑 state.json 后重试。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "要发给主人的通知正文",
            },
        },
        "required": ["message"],
    },
}


def make_im_push_handler(ctx: Any):
    """返回 ``miloco_im_push`` 的 handler（闭包捕获 ctx）。"""
    def _handler(args: Dict[str, Any], **kwargs: Any) -> str:
        message = (args.get("message") or "").strip()
        if not message:
            return json.dumps({"ok": False, "error": "message 不能为空"}, ensure_ascii=False)
        try:
            result = notify_owner(ctx, message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("miloco_im_push 失败: %s", exc)
            result = {"ok": False, "error": f"internal error: {exc}"}
        return json.dumps(result, ensure_ascii=False)
    return _handler