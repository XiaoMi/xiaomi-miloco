"""IM 平台探测：列出用户已配置的 IM 渠道，输出 hermes send --to 接受的 target 串。

提取自 install-hermes.sh 的 step 4.5，原 bash heredoc 实现跟 bash 3.2 / 4 / 5 在
复杂嵌套 + body 内含 ``(fallback)`` 这种括号时偶发 syntax error。挪到外部
脚本彻底消除 bash ↔ heredoc 嵌套，macOS 自带 bash 3.2 也能跑。

用法:
    python3 detect_im_platforms.py <HERMES_HOME>

输出（stdout 一行 JSON）:
    {"targets": ["feishu:oc_xxx:om_yyy"], "source": "hermes send --list --json"}
    {"targets": [], "source": "no platform found"}

target 格式对齐 hermes send --to:
    - "feishu"               裸平台（用 home channel）
    - "feishu:oc_xxx"        指定 chat_id
    - "feishu:oc_xxx:om_yyy" 指定 chat_id + thread_id
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 候选顺序: 国内用户优先（weixin/feishu/wecom/dingtalk/qqbot），然后海外主流
CANDIDATES = (
    "weixin", "feishu", "wecom", "dingtalk", "qqbot",
    "telegram", "discord", "slack",
    "whatsapp", "signal", "mattermost", "bluebubbles", "matrix",
)

# 各平台判定"已配置"的 token 字段名（config.yaml 段）
TOKEN_KEYS = {
    "telegram":  ("bot_token", "token"),
    "discord":   ("bot_token", "token"),
    "slack":     ("bot_token", "app_token"),
    "feishu":    ("app_id", "app_secret", "verification_token"),
    "wecom":     ("corp_id", "corp_secret", "agent_id"),
    "whatsapp":  ("phone_number", "access_token"),
    "signal":    ("phone_number",),
    "mattermost": ("url", "token"),
    "dingtalk":  ("app_key", "app_secret"),
    "bluebubbles": ("server_url", "password"),
    "matrix":    ("homeserver", "access_token"),
    "qqbot":     ("app_id", "client_secret"),
    "weixin":    ("app_id", "app_secret", "token", "encoding_aes_key"),
}

# 各平台 → 必现的环境变量名（任一存在即算"已配置"）
ENV_VARS = {
    "telegram":  ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN"),
    "discord":   ("DISCORD_BOT_TOKEN", "DISCORD_TOKEN"),
    "slack":     ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
    "feishu":    ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN"),
    "wecom":     ("WECOM_CORP_ID", "WECOM_CORP_SECRET", "WECOM_AGENT_ID"),
    "dingtalk":  ("DINGTALK_APP_KEY", "DINGTALK_APP_SECRET"),
    "weixin":    ("WEIXIN_APP_ID", "WEIXIN_APP_SECRET", "WEIXIN_TOKEN"),
    "qqbot":     ("QQBOT_APP_ID", "QQBOT_CLIENT_SECRET"),
    "whatsapp":  ("WHATSAPP_PHONE_NUMBER", "WHATSAPP_ACCESS_TOKEN"),
    "signal":    ("SIGNAL_PHONE_NUMBER",),
    "mattermost": ("MATTERMOST_URL", "MATTERMOST_TOKEN"),
}


def _build_target_from_channel(plat: str, ch: dict) -> str | None:
    chat_id = ch.get("id") or ch.get("chat_id") or ""
    if not chat_id:
        return None
    thread_id = ch.get("thread_id") or ""
    return f"{plat}:{chat_id}" + (f":{thread_id}" if thread_id else "")


def _build_target(plat: str, sec: dict) -> str:
    hc = sec.get("home_channel") or {}
    chat_id = (hc.get("chat_id") if isinstance(hc, dict) else None) or ""
    thread_id = (hc.get("thread_id") if isinstance(hc, dict) else None) or ""
    if chat_id:
        return f"{plat}:{chat_id}" + (f":{thread_id}" if thread_id else "")
    return plat


def _probe_hermes_send_list(hermes_bin: str) -> tuple[list[str], bool]:
    """调 ``hermes send --list --json`` 拿 channel directory，返回 (targets, ok)."""
    try:
        proc = subprocess.run(
            [hermes_bin, "send", "--list", "--json"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return [], False
    if proc.returncode != 0 or not proc.stdout.strip():
        return [], False
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [], False
    platforms = data.get("platforms") or {}
    targets: list[str] = []
    for plat in CANDIDATES:
        for ch in platforms.get(plat) or []:
            t = _build_target_from_channel(plat, ch)
            if t:
                targets.append(t)
                break  # 每平台只取第一个 channel (home channel)
    return targets, bool(targets)


def _probe_auth_json_providers(home: Path) -> list[str]:
    auth_path = home / "auth.json"
    if not auth_path.is_file():
        return []
    try:
        cfg = json.loads(auth_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(cfg, dict):
        return []
    targets: list[str] = []
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for plat in CANDIDATES:
            p = providers.get(plat)
            if not isinstance(p, dict):
                continue
            if not any(p.get(k) for k in ("connected", "status", "token", "bot_token", "app_id")):
                continue
            if p.get("connected") is True or p.get("status") == "connected":
                chat_id = p.get("chat_id") or p.get("home_chat_id") or ""
                thread_id = p.get("thread_id") or ""
                if chat_id:
                    targets.append(f"{plat}:{chat_id}" + (f":{thread_id}" if thread_id else ""))
                else:
                    targets.append(plat)
    # 顶层 fallback（旧 Hermes 版本把平台状态挂在根）
    for plat in CANDIDATES:
        top = cfg.get(plat)
        if not isinstance(top, dict):
            continue
        if top.get("connected") is True or top.get("status") == "connected":
            chat_id = top.get("chat_id") or top.get("home_chat_id") or ""
            thread_id = top.get("thread_id") or ""
            if chat_id:
                targets.append(f"{plat}:{chat_id}" + (f":{thread_id}" if thread_id else ""))
            else:
                targets.append(plat)
    return targets


def _probe_config_yaml(home: Path) -> list[str]:
    cfg_path = home / "config.yaml"
    if not cfg_path.is_file():
        return []
    try:
        import yaml  # type: ignore
    except ImportError:
        return []
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(cfg, dict):
        return []
    targets: list[str] = []
    for plat in CANDIDATES:
        sec = cfg.get(plat)
        if not isinstance(sec, dict):
            continue
        keys = TOKEN_KEYS.get(plat, ())
        if any(sec.get(k) for k in keys):
            targets.append(_build_target(plat, sec))
    return targets


def _probe_env_vars() -> list[str]:
    targets: list[str] = []
    for plat, vars_ in ENV_VARS.items():
        if any(os.environ.get(v) for v in vars_):
            targets.append(plat)
    return targets


def _probe_xdg_auth_json() -> list[str]:
    alt_auth = Path.home() / ".config" / "hermes" / "auth.json"
    if not alt_auth.is_file():
        return []
    try:
        cfg = json.loads(alt_auth.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(cfg, dict):
        return []
    targets: list[str] = []
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for plat in CANDIDATES:
            p = providers.get(plat)
            if not isinstance(p, dict):
                continue
            if p.get("connected") is True or p.get("status") == "connected":
                chat_id = p.get("chat_id") or p.get("home_chat_id") or ""
                thread_id = p.get("thread_id") or ""
                if chat_id:
                    targets.append(f"{plat}:{chat_id}" + (f":{thread_id}" if thread_id else ""))
                else:
                    targets.append(plat)
    return targets


def detect(home: Path) -> dict:
    """按优先级探测，返回 ``{"targets": [...], "source": "..."}``。"""
    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        targets, ok = _probe_hermes_send_list(hermes_bin)
        if ok and targets:
            return {"targets": targets, "source": "hermes send --list --json"}

    targets = _probe_auth_json_providers(home)
    if targets:
        return {"targets": targets, "source": "auth.json"}

    targets = _probe_config_yaml(home)
    if targets:
        return {"targets": targets, "source": "config.yaml"}

    targets = _probe_env_vars()
    if targets:
        return {"targets": targets, "source": "env vars"}

    targets = _probe_xdg_auth_json()
    if targets:
        return {"targets": targets, "source": "XDG auth.json"}

    return {"targets": [], "source": "no platform found"}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <HERMES_HOME>", file=sys.stderr)
        return 2
    home = Path(argv[1])
    result = detect(home)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))