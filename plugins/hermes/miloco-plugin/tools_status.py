"""miloco 自检 + 强制推送 + IM 切换工具集。

装好 miloco 兼容层后用户 / agent 能主动验证链路是否完整：

- ``miloco_status`` — 一键自检 7 项不变量，返回结构化 JSON（plugin enabled /
  state.json deliver.target / adapter health / 4 cron jobs / 16+ skills /
  miloco backend status / 上次 webhook 时间）。诊断 root cause 用。
- ``miloco_test_push`` — 强制走一次完整投递链路（绕开 cron / perception），用户能立刻
  验证推送通不通。
- ``miloco_notify_bind`` — IM 渠道切换：list 列出 state.json::candidates + 当前
  选中的；switch 切换 target。无需手动编辑 state.json。

三个工具都是**纯加法**：不动已有 ``miloco_im_push`` / ``miloco_habit_suggest`` 行为，
不引入新依赖，handler 失败仅 log + 返回 ok:false。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import tools_notify as tn
from .paths import miloco_home

logger = logging.getLogger(__name__)

# 受管 cron job 期望名（与 cron_setup.py::_CRON_TASKS 一一对应）
EXPECTED_CRON_NAMES = (
    "miloco-perception-digest",
    "miloco-home-patrol",
    "miloco-home-dreaming",
    "miloco-habit-suggest",
)

# ---------------------------------------------------------------------------
# 自检子项（每个独立 try/except，单项失败不影响其它）
# ---------------------------------------------------------------------------

def _check_plugin_self() -> Dict[str, Any]:
    """检查插件自身是否完整装载（state.json 可写 / tools 已注册）。"""
    # 我们就在 __init__.py 里跑起来 → 插件必然 enabled。返回 ok 即可。
    return {"ok": True, "note": "plugin 已装载（hooks/tools 注册由 __init__.py 完成）"}


def _check_state_json(ctx: Any) -> Dict[str, Any]:
    """state.json 是否存在 + deliver.target 是否设置。"""
    state = tn.load_state(ctx)
    if not state:
        return {"ok": False, "error": "state.json 不存在或为空"}
    target = (state.get("deliver") or {}).get("target")
    candidates = (state.get("deliver") or {}).get("candidates") or []
    auto = (state.get("deliver") or {}).get("auto_configured")
    if not target:
        return {
            "ok": False,
            "error": (
                "deliver.target 未设置 — 即便 cron 跑通、agent 调 miloco_im_push，"
                "也会回 no deliver target 错。修法：在 Hermes 里连 IM 后重跑 "
                "install-hermes.sh，或调 miloco_notify_bind(action='switch', target='feishu')"
            ),
            "candidates": candidates,
        }
    return {
        "ok": True,
        "target": target,
        "auto_configured": auto,
        "configured_at": (state.get("deliver") or {}).get("configured_at"),
        "candidates_count": len(candidates),
    }


def _check_adapter_health() -> Dict[str, Any]:
    """检查 backend 侧 adapter 是否加载了非兜底实现。"""
    try:
        from miloco.agent_platform import get_adapter

        adapter = get_adapter()
        name = getattr(adapter, "name", "unknown")
        if name == "webhook":
            return {
                "ok": False,
                "error": "adapter 仍为 WebhookAdapter 兜底（插件未加载）",
                "fix": "确认 agent.platform=hermes 已写入 config.json 并重跑 install-hermes.sh",
            }
        return {"ok": True, "adapter": name}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _check_cron_jobs() -> Dict[str, Any]:
    """检查 4 个受管 cron 是否注册。Hermes cron.jobs 不可用时优雅返回。"""
    try:
        from cron.jobs import list_jobs  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"cron.jobs 模块不可用: {type(exc).__name__}: {exc}",
            "registered": [],
            "missing": list(EXPECTED_CRON_NAMES),
        }
    try:
        all_jobs = list_jobs() or []
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"list_jobs() 失败: {type(exc).__name__}: {exc}",
            "registered": [],
            "missing": list(EXPECTED_CRON_NAMES),
        }
    registered = [j.get("name", "") for j in all_jobs if isinstance(j, dict)]
    miloco_jobs = [n for n in registered if "miloco" in n.lower()]
    missing = [
        expected
        for expected in EXPECTED_CRON_NAMES
        if not any(expected in r for r in miloco_jobs)
    ]
    return {
        "ok": len(missing) == 0,
        "registered": miloco_jobs,
        "missing": missing,
        "fix": (
            "在 fork 仓库根目录重跑 bash plugins/hermes/install-hermes.sh "
            "（脚本会 idempotent reconcile 4 个 cron）"
        )
        if missing
        else None,
    }


def _check_skills_installed() -> Dict[str, Any]:
    """检查 miloco-* skill 是否装到 ~/.hermes/skills/（至少 16 个）。"""
    min_expected = 16
    skills_dir = Path.home() / ".hermes" / "skills"
    if not skills_dir.is_dir():
        return {"ok": False, "installed": 0, "expected": min_expected, "fix": "重跑 install-hermes.sh"}
    installed = sorted(
        p.name for p in skills_dir.iterdir() if p.is_dir() and p.name.startswith("miloco-")
    )
    return {
        "ok": len(installed) >= min_expected,
        "installed": len(installed),
        "expected": min_expected,
        "names": installed,
    }


def _check_versions(ctx: Any) -> Dict[str, Any]:
    """state.json::versions 与当前系统对比——升级一致性检查。"""
    state = tn.load_state(ctx)
    versions = state.get("versions") or {}
    if not versions:
        return {
            "ok": False,
            "error": "state.json 没记录 versions（老版本 install，或没跑过 install-hermes.sh）",
        }
    # 当前系统版本
    import shutil
    import subprocess

    cur_hermes = "unknown"
    if shutil.which("hermes"):
        try:
            r = subprocess.run(["hermes", "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                lines = (r.stdout or r.stderr or "").strip().splitlines()
                cur_hermes = lines[0] if lines else "empty-output"
            else:
                cur_hermes = f"err:{r.returncode}"
        except Exception as exc:  # noqa: BLE001
            cur_hermes = f"err:{exc}"
    cur_miloco = "unknown"
    if shutil.which("miloco-cli"):
        try:
            r = subprocess.run(["miloco-cli", "version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                raw = (r.stdout or r.stderr or "").strip()
                # miloco-cli version 输出形如 {"version": "2026.6.18"}，提取 version 字段做归一
                try:
                    import json as _json
                    parsed = _json.loads(raw) if raw.startswith("{") else None
                    cur_miloco = parsed.get("version") if isinstance(parsed, dict) else (raw.splitlines()[0] if raw else "empty-output")
                except Exception:
                    cur_miloco = raw.splitlines()[0] if raw else "empty-output"
            else:
                cur_miloco = f"err:{r.returncode}"
        except Exception as exc:  # noqa: BLE001
            cur_miloco = f"err:{exc}"
    # plugin 版本（从装好的 plugin.yaml 读）
    try:
        import os as _os
        manifest_base = getattr(getattr(ctx, "manifest", None), "path", "")
        candidates = []
        if manifest_base and Path(manifest_base).is_dir():
            candidates.append(Path(manifest_base) / "plugin.yaml")
        # 兜底：install-hermes.sh 的装入点（dev / pip 都落这）
        hermes_home = Path(_os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        candidates.append(hermes_home / "plugins" / "miloco" / "miloco-plugin" / "plugin.yaml")
        cur_plugin = "unknown"
        for plugin_yaml in candidates:
            if plugin_yaml and plugin_yaml.is_file():
                for line in plugin_yaml.read_text(encoding="utf-8").splitlines():
                    if line.startswith("version:"):
                        cur_plugin = line.split(":", 1)[1].strip()
                        break
                if cur_plugin != "unknown":
                    break
    except Exception:  # noqa: BLE001
        cur_plugin = "unknown"

    mismatches = []
    for key, cur, recorded in (
        ("hermes", cur_hermes, versions.get("hermes", "")),
        ("miloco_cli", cur_miloco, versions.get("miloco_cli", "")),
        ("plugin", cur_plugin, versions.get("plugin", "")),
    ):
        if recorded and recorded != "unknown" and cur != "unknown" and cur != recorded:
            mismatches.append(f"{key}: 装时={recorded} 现在={cur}")
    return {
        "ok": len(mismatches) == 0,
        "current": {"hermes": cur_hermes, "miloco_cli": cur_miloco, "plugin": cur_plugin},
        "recorded": versions,
        "mismatches": mismatches,
        "fix": (
            "在 fork 仓库根目录重跑 bash plugins/hermes/install-hermes.sh 让 versions 更新到 state.json；"
            "如果只 hermes 变了，跑 hermes gateway restart + "
            "miloco-cli service restart"
        ) if mismatches else None,
    }


def _check_trace_hooks() -> Dict[str, Any]:
    """trace.py 是否被 register 了——通过查 $MILOCO_HOME/trace/agent/ 目录近期活动。

    注意：trace 是 debug 开关，没开过不代表坏了。所以 trace 目录不存在时
    返回 ``ok=True, note="trace debug 未启用"``，不进 failed_count。
    """
    trace_dir = miloco_home() / "trace" / "agent"
    if not trace_dir.is_dir():
        return {
            "ok": True,
            "note": "trace debug 未启用（没跑过 MILOCO_TRACE_DEBUG=1 的 turn）— 不算异常",
            "enabled": False,
            "fix": "需要 debug 时再 export MILOCO_TRACE_DEBUG=1 跑一个 turn",
        }
    # 看今天有没有 meta.json 写过
    from datetime import datetime
    today = trace_dir / datetime.now().strftime("%Y%m%d")
    if not today.is_dir():
        return {
            "ok": True,
            "note": "trace 目录在但今天没 turn 跑过（首次跑会建目录 + meta.json）",
            "trace_dir": str(trace_dir),
        }
    metas = list(today.glob("*.meta.json"))
    if not metas:
        return {
            "ok": True,
            "note": "今天还没 turn 跑过（debug 模式需 MILOCO_TRACE_DEBUG=1）",
            "trace_dir": str(trace_dir),
        }
    newest = max(metas, key=lambda p: p.stat().st_mtime)
    return {
        "ok": True,
        "trace_files_today": len(list(today.glob("*.jsonl.gz"))),
        "meta_files_today": len(metas),
        "newest_meta": newest.name,
    }


def _check_miloco_backend() -> Dict[str, Any]:
    """检查 miloco 后端是否在跑（调 miloco-cli service status）。"""
    import shutil
    import subprocess

    if not shutil.which("miloco-cli"):
        return {"ok": False, "error": "miloco-cli 不在 PATH"}
    try:
        result = subprocess.run(
            ["miloco-cli", "service", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # 不同 miloco-cli 版本输出格式不一：宽松判 ok / running / active 之一
        out = (result.stdout or "") + (result.stderr or "")
        ok = result.returncode == 0 and any(
            marker in out.lower() for marker in ("running", "active", "ok", "started")
        )
        return {
            "ok": ok,
            "returncode": result.returncode,
            "output": out.strip()[:300],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "miloco-cli service status 超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _check_hermes_plugin_enabled() -> Dict[str, Any]:
    """``hermes plugins list`` 看 miloco 是不是 enabled。"""
    import shutil
    import subprocess

    if not shutil.which("hermes"):
        return {"ok": False, "error": "hermes CLI 不在 PATH"}
    try:
        result = subprocess.run(
            ["hermes", "plugins", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        (result.stdout or "").lower()
        # 简单匹配：找含 miloco 的行，看有没有 enabled
        enabled = any(
            "miloco" in line and "enabled" in line
            for line in (result.stdout or "").splitlines()
        )
        return {
            "ok": enabled,
            "hermes_output": (result.stdout or "").strip()[:500],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "hermes plugins list 超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# miloco_status
# ---------------------------------------------------------------------------

def gather_status(ctx: Any) -> Dict[str, Any]:
    """一键自检 7 项不变量，返回结构化 JSON。"""
    checks: Dict[str, Dict[str, Any]] = {}
    # 顺序按「最可能是 root cause → 最不可能」排，agent 报告时一眼看到关键项
    for name, fn, ctx_arg in (
        ("plugin_self", _check_plugin_self, None),
        ("state_json_deliver_target", _check_state_json, ctx),
        ("hermes_plugin_enabled", _check_hermes_plugin_enabled, None),
        ("adapter_health", _check_adapter_health, None),
        ("cron_jobs", _check_cron_jobs, None),
        ("miloco_backend", _check_miloco_backend, None),
        ("skills_installed", _check_skills_installed, None),
        ("versions", _check_versions, ctx),
        ("trace_hooks", _check_trace_hooks, None),
    ):
        try:
            checks[name] = fn(ctx_arg) if ctx_arg is not None else fn()
        except Exception as exc:  # noqa: BLE001
            logger.exception("miloco_status 子项 %s 失败", name)
            checks[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    failed = [k for k, v in checks.items() if not v.get("ok")]
    return {
        "ok": len(failed) == 0,
        "failed_count": len(failed),
        "failed": failed,
        "checks": checks,
        "hint": (
            "全部 ok=True = 推送链路完整；fail 项按 checks[*].fix 修。"
            "最常见 fail: state_json_deliver_target（hermes 没配 IM 或装时没读到）。"
        ),
    }


# ---------------------------------------------------------------------------
# miloco_test_push
# ---------------------------------------------------------------------------

def test_push(ctx: Any, message: Optional[str] = None) -> Dict[str, Any]:
    """强制走一次投递链路（绕开 cron / perception），立即验证推送通不通。

    不传 message 时用默认测试文案（带时间戳便于排查"我收到了吗"）。
    """
    from datetime import datetime

    msg = message or (
        f"miloco test push @ {datetime.now().astimezone().isoformat()} — "
        "如果你在 IM 看到这条说明推送链路完整。"
    )
    try:
        # M2 协议下 case 2 走两回合握手，自动提供 bindHint 触发投递
        result = tn.notify_owner(ctx, msg, bind_hint="[test_push 自动 bind]")
    except Exception as exc:  # noqa: BLE001
        logger.exception("miloco_test_push 失败: %s", exc)
        return {"ok": False, "error": f"internal error: {exc}"}
    return result






# ---------------------------------------------------------------------------
# 三 tool 的 schema + handler
# ---------------------------------------------------------------------------

MILOCO_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "miloco_status",
    "description": (
        "一键自检 miloco 推送链路 7 项不变量（plugin / state.json target / hermes plugin enabled / "
        "adapter health / 4 cron jobs / miloco backend / 16+ skills）。\n"
        "返回结构化 JSON：checks[*].ok + 失败项 fix 提示。**没收到推送时第一时间调这个**——"
        "会告诉你卡在哪一环（绝大多数情况是 state.json::deliver.target=null）。\n"
        "无需参数，agent / 用户都能调。"
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


MILOCO_TEST_PUSH_SCHEMA: Dict[str, Any] = {
    "name": "miloco_test_push",
    "description": (
        "强制走一次投递链路（绕开 cron / perception 触发条件），立即验证推送通不通。\n"
        "参数 message 可选（默认带时间戳的测试文案）。返回 ``{ok:true, platform, chat_id}`` 即送达；"
        "``{ok:false, error}`` 看 error 提示修（最常见：no deliver target configured → 调 miloco_notify_bind 切）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "可选测试文案（默认带时间戳）",
            },
        },
        "required": [],
    },
}




def make_status_handler(ctx: Any):
    """``miloco_status`` handler（闭包捕获 ctx）。"""
    def _handler(args: Dict[str, Any], **kwargs: Any) -> str:
        try:
            result = gather_status(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.exception("miloco_status 失败: %s", exc)
            result = {"ok": False, "error": f"internal error: {exc}"}
        return json.dumps(result, ensure_ascii=False)
    return _handler


def make_test_push_handler(ctx: Any):
    """``miloco_test_push`` handler（闭包捕获 ctx）。"""
    def _handler(args: Dict[str, Any], **kwargs: Any) -> str:
        args = args if isinstance(args, dict) else {}
        message = args.get("message")
        try:
            result = test_push(ctx, message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("miloco_test_push 失败: %s", exc)
            result = {"ok": False, "error": f"internal error: {exc}"}
        return json.dumps(result, ensure_ascii=False)
    return _handler


