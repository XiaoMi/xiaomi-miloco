"""LLM client for the rule tester (OpenAI-compatible).

Drives the miloco-create-task SOP: load skill markdown into system prompt,
expose mock tools, let the LLM emit a sequence of tool calls. The tester
executes ``miloco_cli_rule_create`` for real (via subprocess) and records
``openclaw_cron_add`` as log-only.

**方案 P 切换后未跟进**：record 初始化已改走 ``miloco-cli task record init``
（Bash 命令），但本 tester 暂没暴露 Bash mock；旧的 ``miloco_task_memory_write``
mock 已删除——若要在 tester 里完整复现 SOP，需要新增一个 ``miloco_cli_task_record_init``
mock（与 rule create 同款 subprocess+record 风格）。当前 tester 仅能校验
SOP 中"建 rule"那部分。

Configuration (environment variables, all optional with defaults):
    MILOCO_TESTER_LLM_BASE_URL   default: https://api.openai.com/v1
    MILOCO_TESTER_LLM_API_KEY    required
    MILOCO_TESTER_LLM_MODEL      default: gpt-4o-mini
    MILOCO_TESTER_CLI_BIN        default: miloco-cli
    MILOCO_TESTER_MAX_ITERS      default: 8 (max LLM round-trips per query)

Falls back to ``config.toml`` next to this file when env vars are unset.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # py311+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore

import httpx

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SKILL_PATH = _REPO_ROOT / "plugins" / "skills" / "miloco-create-task" / "SKILL.md"
_CONFIG_PATH = _HERE / "config.toml"


# ---- Tool schemas (OpenAI-compatible) ---------------------------------------

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "miloco_cli_rule_create",
            "description": (
                "Create a Rule via miloco-cli. The tester runs "
                "`miloco-cli rule create <args>` as a subprocess and captures "
                "stdout.\n\n"
                "Required flags depend on the mode matrix:\n"
                "- event + 设备直控: --action × N\n"
                "- event + Agent 回调: --action-desc × N\n"
                "- state mode: --mode state + at least one of "
                "  --on-enter-action / --on-enter-desc / --on-exit-action / "
                "  --on-exit-desc; same direction can have actions OR desc, "
                "  not both.\n\n"
                "**设备直控 action JSON** (used by --action / --on-*-action). "
                "Two shapes:\n"
                "1. Device control (idempotent):\n"
                "   {\"did\":\"<id>\",\"iid\":\"prop.<siid>.<piid>\","
                "\"value\":<v>,\"idempotent\":true}\n"
                "2. Notify / TTS (non-idempotent, MUST include a cooldown):\n"
                "   {\"did\":\"<id>\",\"iid\":\"action.<siid>.<aiid>\","
                "\"params\":[\"<text>\"],\"idempotent\":false,"
                "\"cooldown_minutes\":10}\n"
                "   cooldown_seconds may be used instead (wins over "
                "cooldown_minutes; 0 disables it).\n"
                "CLI strictly rejects idempotent=false without "
                "cooldown_seconds or cooldown_minutes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Argument list to pass after `rule create`, e.g. "
                            "[\"--name\", \"[drink_water] 感知喝水\", "
                            "\"--task-id\", \"drink_water\", "
                            "\"--mode\", \"event\", "
                            "\"--source\", \"cam_living_room\", "
                            "\"--condition\", \"检测到用户喝水\", "
                            "\"--action-desc\", \"progress++,达标时播报恭喜\"]. "
                            "Each string is one CLI token; values containing "
                            "spaces should be a single string, not pre-quoted."
                        ),
                    }
                },
                "required": ["args"],
            },
        },
    },
    # NOTE: `miloco_task_memory_write` mock 已删除（方案 P 后 record init 走
    # `miloco-cli task record init` Bash 命令，tester 暂未提供 Bash mock）。
    {
        "type": "function",
        "function": {
            "name": "openclaw_cron_add",
            "description": (
                "Stage B schedule. NOT EXECUTED in this tester (cron lives in "
                "OpenClaw, out of scope). Recorded so the developer can verify "
                "the SOP would have scheduled the right job."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "cron": {"type": "string", "description": "cron expr / --at / --every"},
                    "tz": {"type": "string", "default": "Asia/Shanghai"},
                    "session": {"type": "string", "default": "isolated"},
                    "message": {"type": "string"},
                },
                "required": ["name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Signal the tester that the SOP is done. Call this once all "
                "cron adds / rule creates for the user request have been emitted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Audit summary"},
                },
                "required": [],
            },
        },
    },
]


# ---- Config -----------------------------------------------------------------


@dataclass
class TesterConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    cli_bin: str = "miloco-cli"
    max_iters: int = 8

    @classmethod
    def load(cls) -> TesterConfig:
        cfg = cls()

        # 1. Optional config.toml next to this file
        if _CONFIG_PATH.exists() and tomllib is not None:
            try:
                with open(_CONFIG_PATH, "rb") as f:
                    data = tomllib.load(f)
                section = data.get("llm", {})
                cfg.base_url = section.get("base_url", cfg.base_url)
                cfg.api_key = section.get("api_key", cfg.api_key)
                cfg.model = section.get("model", cfg.model)
                tester = data.get("tester", {})
                cfg.cli_bin = tester.get("cli_bin", cfg.cli_bin)
                cfg.max_iters = int(tester.get("max_iters", cfg.max_iters))
            except Exception as e:
                logger.warning("Failed to load %s: %s", _CONFIG_PATH, e)

        # 2. Environment variables override file
        cfg.base_url = os.environ.get("MILOCO_TESTER_LLM_BASE_URL", cfg.base_url)
        cfg.api_key = os.environ.get("MILOCO_TESTER_LLM_API_KEY", cfg.api_key)
        cfg.model = os.environ.get("MILOCO_TESTER_LLM_MODEL", cfg.model)
        cfg.cli_bin = os.environ.get("MILOCO_TESTER_CLI_BIN", cfg.cli_bin)
        cfg.max_iters = int(
            os.environ.get("MILOCO_TESTER_MAX_ITERS", cfg.max_iters)
        )
        return cfg


# ---- Trace records ---------------------------------------------------------


@dataclass
class ToolCallRecord:
    """One emitted tool call + its outcome."""

    name: str
    arguments: dict[str, Any]
    executed: bool                  # True when the tester really ran it
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskTrace:
    """Full round-trip trace for one user query."""

    user_query: str
    config: dict[str, Any]
    iterations: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    assistant_text: str = ""
    finished: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_query": self.user_query,
            "config": self.config,
            "iterations": self.iterations,
            "assistant_text": self.assistant_text,
            "finished": self.finished,
            "error": self.error,
            "tool_calls": [asdict(c) for c in self.tool_calls],
        }


# ---- Skill loading ---------------------------------------------------------


def load_skill_sop() -> str:
    if not _SKILL_PATH.exists():
        raise FileNotFoundError(
            f"miloco-create-task SOP not found: {_SKILL_PATH}. "
            "Expected the production skill at plugins/skills/miloco-create-task/SKILL.md."
        )
    return _SKILL_PATH.read_text(encoding="utf-8")


def system_prompt() -> str:
    sop = load_skill_sop()
    return (
        "You are the OpenClaw agent simulating miloco-create-task skill behavior in a "
        "developer test harness. Follow the SOP below exactly.\n\n"
        "Constraints in this harness:\n"
        "- Emit tool calls only via the provided functions.\n"
        "- Stage A (memory writes) and Stage B (cron) are recorded but NOT "
        "  executed here -- the tester is rule-focused.\n"
        "- Stage C (miloco-cli rule create) IS executed via subprocess; "
        "  pay attention to required flags per the mode/type matrix.\n"
        "- Call `finish` when the SOP is complete for this user request.\n\n"
        "=" * 60 + "\n"
        "miloco-create-task SOP\n"
        + "=" * 60 + "\n\n"
        + sop
    )


# ---- LLM driver ------------------------------------------------------------


async def run_create_task(user_query: str) -> TaskTrace:
    """Drive the LLM through the miloco-create-task SOP for one user query.

    Returns a :class:`TaskTrace` with every tool call + outcome.
    """
    cfg = TesterConfig.load()
    trace = TaskTrace(
        user_query=user_query,
        config={
            "base_url": cfg.base_url,
            "model": cfg.model,
            "cli_bin": cfg.cli_bin,
            "max_iters": cfg.max_iters,
            "api_key_set": bool(cfg.api_key),
        },
    )

    if not cfg.api_key:
        trace.error = "MILOCO_TESTER_LLM_API_KEY not set (env or config.toml)"
        return trace

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": user_query},
    ]

    async with httpx.AsyncClient(base_url=cfg.base_url.rstrip("/"), timeout=120.0) as client:
        for i in range(cfg.max_iters):
            trace.iterations = i + 1
            try:
                resp = await client.post(
                    "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {cfg.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": cfg.model,
                        "messages": messages,
                        "tools": _TOOLS,
                        "tool_choice": "auto",
                    },
                )
            except httpx.HTTPError as e:
                trace.error = f"LLM request failed: {e}"
                return trace

            if resp.status_code != 200:
                trace.error = f"LLM returned {resp.status_code}: {resp.text[:500]}"
                return trace

            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
            text = msg.get("content") or ""
            if text:
                trace.assistant_text += text + "\n"

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                # No tool calls + no `finish` -> SOP didn't progress; stop.
                trace.assistant_text += "\n[tester] no tool_calls in response, stopping.\n"
                return trace

            # Append the assistant message verbatim (with tool_calls) so the
            # follow-up tool messages can reference tool_call_id.
            messages.append(msg)

            saw_finish = False
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}

                outcome = _execute_tool(name, args, cfg)
                trace.tool_calls.append(
                    ToolCallRecord(
                        name=name,
                        arguments=args,
                        executed=outcome["executed"],
                        result=outcome["result"],
                    )
                )

                # Feed the tool result back to the LLM
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(outcome["result"], ensure_ascii=False),
                })

                if name == "finish":
                    saw_finish = True

            if saw_finish:
                trace.finished = True
                return trace

    trace.error = f"max_iters {cfg.max_iters} reached without finish"
    return trace


# ---- Tool execution --------------------------------------------------------


def _execute_tool(name: str, args: dict, cfg: TesterConfig) -> dict[str, Any]:
    """Run the tool. Only `miloco_cli_rule_create` performs real work."""
    if name == "miloco_cli_rule_create":
        return _run_cli_rule_create(args.get("args") or [], cfg)
    if name == "finish":
        return {"executed": True, "result": {"ok": True, "summary": args.get("summary", "")}}
    # memory write / cron add: recorded but not executed
    return {
        "executed": False,
        "result": {
            "ok": True,
            "note": f"{name} recorded (not executed; out of repo scope)",
            "stub_inputs": args,
        },
    }


def _run_cli_rule_create(args: list[str], cfg: TesterConfig) -> dict[str, Any]:
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return {
            "executed": False,
            "result": {"ok": False, "error": "args must be a list of strings"},
        }
    cmd = [cfg.cli_bin, "rule", "create", *args]
    logger.info("Running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return {
            "executed": False,
            "result": {
                "ok": False,
                "error": f"cli binary not found: {cfg.cli_bin}",
            },
        }
    except subprocess.TimeoutExpired:
        return {
            "executed": True,
            "result": {"ok": False, "error": "cli timeout (30s)"},
        }

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parsed: Any = None
    if out:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = None
    return {
        "executed": True,
        "result": {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": out,
            "stderr": err,
            "parsed_stdout": parsed,
            "cmd": cmd,
        },
    }
