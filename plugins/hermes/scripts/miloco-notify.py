#!/usr/bin/env python3
"""miloco-notify —— 确定性 IM 渠道管理 (不走 LLM)。

历史: PR #279 miloco_notify_bind tool 走 LLM 推断,L1 守门会让 oc id 被改坏
(实测:`oc_806ed7124bae73745846704be33ae2b3` 被 LLM 改成 `...be33ae33ae2b3`,
多了 `33ae`)。

本 wrapper 直接读 / 写 state.json::deliver.target,避免 LLM 干预。

用法:
  miloco-notify list                           # 列候选 + 当前 target
  miloco-notify switch <target>                # 切 target (无 LLM 干扰)
  miloco-notify switch feishu:oc_xxx           # 同上
  miloco-notify switch all                     # fanout:同时发到所有 candidates
  miloco-notify reset                          # 重置回 auto-detected target

target 格式必须是 `platform:chat_id@provider`,或特殊值 `all`(fanout 全部发),
否则 ok=false exit 2。

state.json 路径遵循 tools_notify.py::_state_path 优先级:
  1. $HERMES_HOME/plugins/miloco/miloco-plugin/state.json (默认,install-hermes.sh 的 source of truth)
  2. $MILOCO_HOME/state.json (兜底)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_FILENAME = "state.json"


def _state_path() -> Path:
    """与 plugins/hermes/miloco-plugin/tools_notify.py::_state_path 保持一致。"""
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return hermes_home / "plugins" / "miloco" / "miloco-plugin" / STATE_FILENAME


def load_state() -> dict:
    path = _state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def cmd_list(_args: argparse.Namespace) -> int:
    state = load_state()
    deliver = state.get("deliver") or {}
    candidates = deliver.get("candidates") or []
    current = deliver.get("target")
    auto = deliver.get("auto_configured")
    print(json.dumps({
        "ok": True,
        "current": current,
        "auto_configured": auto,
        "candidates": candidates,
        "candidates_count": len(candidates),
        "state_path": str(_state_path()),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    target = (args.target or "").strip()
    if not target:
        print(json.dumps({"ok": False, "error": "target 不能为空"}, ensure_ascii=False), file=sys.stderr)
        return 2

    # 1. 校验 target 格式：必须是 platform:chat_id@provider 形式,或特殊值 "all" (fanout)
    #    (与 tools_notify.resolve_notify_target + im_push fanout 接受的格式一致)
    if target != "all" and ":" not in target:
        print(json.dumps({
            "ok": False,
            "error": f"target 格式不对: '{target}' (期望 'platform:chat_id@provider' 或 'all' fanout,例 'feishu:oc_xxx')"
        }, ensure_ascii=False), file=sys.stderr)
        return 2

    # 2. 如果 candidates 已存在且 target 不在里面,warn(但不阻止)
    state = load_state()
    deliver = state.get("deliver") or {}
    candidates = deliver.get("candidates") or []
    warn = None
    if candidates and target != "all" and target not in candidates:
        warn = f"target '{target}' 不在 candidates 里(可能没经 install-hermes.sh 探测);仍写入"

    state["deliver"] = {
        "target": target,
        "auto_configured": False,
        "configured_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": "manual via miloco-notify CLI",
        "candidates": candidates,
    }
    save_state(state)

    out = {
        "ok": True,
        "target": target,
        "note": "已切换;下次 miloco_im_push 会用新 target(无 LLM 干预)",
        "state_path": str(_state_path()),
    }
    if warn:
        out["warning"] = warn
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_reset(_args: argparse.Namespace) -> int:
    """重置回 auto-detected target (把 source 改回 install-hermes.sh auto-detect)。

    实现:重新跑 install-hermes.sh 的 IM 探测逻辑（call detect_im_platforms.py），
    写新 candidates + target。
    """
    import subprocess

    # 找 detect_im_platforms.py
    here = Path(__file__).resolve().parent
    candidates_paths = [
        here / "detect_im_platforms.py",
        Path.home() / ".hermes" / "plugins" / "miloco" / "scripts" / "detect_im_platforms.py",
    ]
    script = next((p for p in candidates_paths if p.exists()), None)
    if not script:
        print(json.dumps({
            "ok": False,
            "error": "找不到 detect_im_platforms.py(请重跑 install-hermes.sh)",
        }, ensure_ascii=False), file=sys.stderr)
        return 2

    hermes_home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    proc = subprocess.run(["python3", str(script), hermes_home], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        print(json.dumps({
            "ok": False,
            "error": f"detect_im_platforms.py 失败: {proc.stderr.strip()}",
        }, ensure_ascii=False), file=sys.stderr)
        return proc.returncode

    detected = json.loads(proc.stdout)
    targets = detected.get("targets", [])
    state = load_state()
    state["deliver"] = {
        "target": targets[0] if targets else None,
        "auto_configured": True,
        "configured_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": "install-hermes.sh auto-detect (via miloco-notify reset)",
        "candidates": targets,
    }
    save_state(state)
    print(json.dumps({
        "ok": True,
        "target": state["deliver"]["target"],
        "candidates": state["deliver"]["candidates"],
        "note": "已重置回 auto-detect 结果",
    }, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="miloco IM 渠道确定性管理(不走 LLM,避免 oc id 被改坏)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列候选 + 当前 target")

    p_switch = sub.add_parser("switch", help="切换 target(无 LLM 干预)")
    p_switch.add_argument("target", help="新 target, 格式 'platform:chat_id@provider' (例 feishu:oc_xxx)")

    sub.add_parser("reset", help="重置回 install-hermes.sh auto-detect 结果")

    args = parser.parse_args()
    return {
        "list": cmd_list,
        "switch": cmd_switch,
        "reset": cmd_reset,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())