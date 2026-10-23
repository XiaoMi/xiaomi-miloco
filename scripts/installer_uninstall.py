"""Miloco 卸载器的数据模型、cleaners 与主流程。

由 ``scripts/install.py`` 导入。发行包会把本文件与 ``install.py`` 一起内嵌，
运行时放在同一目录，因此这里只使用标准库。
"""

from __future__ import annotations

import argparse
import copy
import datetime
import enum
import json
import os
import platform as _platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

if TYPE_CHECKING:
    from install import I18n, UI


# ---------------------------------------------------------------------------
# Uninstall — data model
# ---------------------------------------------------------------------------

HERMES_MANAGED_TAG = "[miloco:home-profile]"
HERMES_PLUGIN_ID = "miloco"
HERMES_PLUGIN_NAMES = frozenset({"miloco", "miloco/miloco-plugin"})
OPENCLAW_PLUGIN_ID = "miloco-openclaw-plugin"
FALLBACK_OPENCLAW_TOOLS = (
    "miloco_im_push",
    "miloco_notify_bind",
    "miloco_notify_unbind",
)
UV_TOOL_PACKAGES = ("miloco-cli", "miloco", "supervisor")
_RC_MARKER_RE = re.compile(r"^# miloco runtime path\s*$")
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_RC_EXPORT_RE = re.compile(r"^\s*export\s+MILOCO_HOME\s*=(.*)$")


class ItemStatus(str, enum.Enum):
    DONE = "done"
    ABSENT = "absent"
    KEPT = "kept"
    SKIPPED = "skipped"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CleanupItem:
    key: str
    platform: str
    target: str
    status: ItemStatus
    detail: str = ""
    remediation: str = ""


@dataclass
class PlatformReport:
    platform: str
    detected: bool
    cli_available: bool
    items: list[CleanupItem] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        return any(
            item.status in (ItemStatus.FAILED, ItemStatus.BLOCKED)
            for item in self.items
        )

    def add(self, item: CleanupItem) -> None:
        self.items.append(item)


@dataclass(frozen=True)
class ManagedCron:
    id: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class HomeCandidate:
    path: Path
    real_path: Path
    sources: frozenset[str]
    identity: tuple[int, int] | None
    exists: bool
    is_symlink: bool
    has_config: bool
    has_agent_platform: bool
    has_hermes_adapter: bool
    has_install_cache: bool
    agent_platform: str
    auth_bearer: str
    deliver_configured: bool


@dataclass(frozen=True)
class HermesFacts:
    detected: bool
    cli: str | None
    venv_python: Path | None
    plugin_dir: Path
    skills: tuple[Path, ...]
    config_yaml: Path
    config_mentions_plugin: bool
    env_file: Path
    env_managed_bearers: tuple[str, ...]
    env_miloco_home_lines: tuple[str, ...]
    rc_files: tuple[Path, ...]
    cron_backend: str
    managed_crons: tuple[ManagedCron, ...]
    cli_probe_error: str | None


@dataclass(frozen=True)
class OpenClawFacts:
    detected: bool
    cli: str | None
    state_dir: Path
    config_path: Path
    plugin_id: str
    plugin_record: bool
    tools_in_also_allow: tuple[str, ...]
    managed_crons: tuple[ManagedCron, ...]
    cli_probe_error: str | None


@dataclass(frozen=True)
class Inventory:
    home: Path
    candidates: tuple[HomeCandidate, ...]
    primary_home: Path | None
    hermes: HermesFacts
    openclaw: OpenClawFacts
    tool_names: tuple[str, ...]
    tool_names_source: str


# ---------------------------------------------------------------------------
# Uninstall — command execution & errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

_LAUNCHD_LABEL = "com.xiaomi.miloco.backend"


def _remove_launchagent(ui: UI) -> None:
    """macOS：卸载前拆掉 LaunchAgent job 并删 plist。

    plist 是本安装器引入的持久产物且 RunAtLoad=true：不清的话 miloco.app
    随 miloco_home 一起被删（或 uv tool 卸载后 python_bin 失效）之后，下次
    登录 launchd 仍会按 plist 去 spawn 一个不存在的二进制。
    """
    if _platform.system() != "Darwin":
        return
    plist = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
    target = f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"
    try:
        subprocess.run(
            ["launchctl", "bootout", target], capture_output=True, timeout=20
        )
    except Exception:
        pass  # 没加载 / 无 launchctl：best-effort，删 plist 才是关键一步
    if plist.exists():
        try:
            plist.unlink()
            ui.ok(f"LaunchAgent removed: {plist}")
        except OSError as exc:
            ui.warn(f"remove LaunchAgent plist failed: {exc}")

class UninstallError(Exception):
    status: ItemStatus = ItemStatus.FAILED

    def __init__(
        self,
        key: str,
        target: str,
        *,
        detail: str = "",
        remediation: str = "",
    ) -> None:
        super().__init__(detail or key)
        self.key = key
        self.target = target
        self.detail = detail
        self.remediation = remediation


class CliNotFound(UninstallError):
    status = ItemStatus.BLOCKED


class CommandFailed(UninstallError):
    pass


class CommandTimeout(CommandFailed):
    pass


class LocalApiUnavailable(UninstallError):
    status = ItemStatus.BLOCKED


class MalformedFile(UninstallError):
    pass


class UnsafePath(UninstallError):
    pass


class OwnershipUncertain(UninstallError):
    status = ItemStatus.KEPT


class SubprocessRunner:
    """所有卸载子进程调用的唯一出口；非 0 由调用方分类。"""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        stdin: str | None = None,
    ) -> CommandResult:
        argv_t = tuple(str(a) for a in argv)
        kwargs: dict = {}
        if stdin is not None:
            kwargs["input"] = stdin
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            proc = subprocess.run(
                list(argv_t),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=dict(env) if env is not None else None,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeout(
                "uninstall.detail.command_failed",
                " ".join(argv_t),
                detail=f"timeout after {timeout:g}s",
            ) from exc
        except OSError as exc:
            raise CommandFailed(
                "uninstall.detail.command_failed",
                " ".join(argv_t),
                detail=str(exc),
            ) from exc
        return CommandResult(argv_t, proc.returncode, proc.stdout or "", proc.stderr or "")


def _subprocess_env(base: Mapping[str, str], **overrides: object) -> dict[str, str]:
    env = dict(base)
    for key, value in overrides.items():
        if value:
            env[key] = str(value)
    return env


def _command_failure_detail(result: CommandResult, limit: int = 400) -> str:
    stderr_lines = [ln.strip() for ln in (result.stderr or "").splitlines() if ln.strip()]
    excerpt = stderr_lines[-1] if stderr_lines else ""
    if len(excerpt) > limit:
        excerpt = excerpt[:limit] + "…"
    base = f"{' '.join(result.argv)} (rc={result.returncode})"
    return f"{base}: {excerpt}" if excerpt else base


def _redact_secrets(text: str, secrets_: Iterable[str]) -> str:
    for secret in secrets_:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


# ---------------------------------------------------------------------------
# Uninstall — small filesystem / parsing helpers
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(_unquote(value))))


def _same_path(value: str, candidates: Iterable[Path]) -> bool:
    target = _expand_path(value).resolve(strict=False)
    return any(target == Path(c).resolve(strict=False) for c in candidates)


def _iter_env_assignments(path: Path):
    for index, line in enumerate(_read_text(path).splitlines()):
        match = _ENV_LINE_RE.match(line)
        if match:
            yield index, match.group(1), _unquote(match.group(2))


def _iter_rc_exports(path: Path):
    for line in _read_text(path).splitlines():
        match = _RC_EXPORT_RE.match(line)
        if match:
            value = _unquote(match.group(1))
            if value:
                yield value


def _atomic_write_text(path: Path, text: str) -> None:
    target = Path(os.path.realpath(path))
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".miloco-uninstall-", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _rm_path(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)
    return True


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _is_ancestor_or_self(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Uninstall — platform paths & discovery
# ---------------------------------------------------------------------------


def resolve_hermes_home(env: Mapping[str, str], home: Path) -> Path:
    raw = env.get("HERMES_HOME")
    return Path(raw).expanduser() if raw else home / ".hermes"


def resolve_openclaw_paths(env: Mapping[str, str], home: Path) -> tuple[Path, Path]:
    state_raw = env.get("OPENCLAW_STATE_DIR")
    state_dir = Path(state_raw).expanduser() if state_raw else home / ".openclaw"
    config_raw = env.get("OPENCLAW_CONFIG_PATH")
    config_path = (
        Path(config_raw).expanduser() if config_raw else state_dir / "openclaw.json"
    )
    return state_dir, config_path


def _default_home_for(hint: str, home: Path) -> Path | None:
    if hint == "hermes":
        return home / ".hermes" / "miloco"
    if hint == "openclaw":
        return home / ".openclaw" / "miloco"
    return None


def _build_home_candidate(
    path: Path, sources: set[str], *, deliver_configured: bool
) -> HomeCandidate:
    real_path = path.resolve(strict=False)
    exists = path.exists()
    identity: tuple[int, int] | None = None
    if exists:
        try:
            st = path.stat()
            identity = (st.st_dev, st.st_ino)
        except OSError:
            identity = None
    config = _read_json(path / "config.json") if (path / "config.json").is_file() else None
    agent = config.get("agent") if isinstance(config, dict) else None
    agent = agent if isinstance(agent, dict) else {}
    return HomeCandidate(
        path=path,
        real_path=real_path,
        sources=frozenset(sources),
        identity=identity,
        exists=exists,
        is_symlink=path.is_symlink(),
        has_config=(path / "config.json").is_file(),
        has_agent_platform=(path / "agent_platform").is_dir(),
        has_hermes_adapter=(path / "agent_platform" / "hermes").exists()
        or (path / "agent_platform" / "hermes").is_symlink(),
        has_install_cache=(path / ".install-cache" / "hermes-plugin").exists()
        or (path / ".install-cache" / "hermes-plugin").is_symlink(),
        agent_platform=str(agent.get("platform") or ""),
        auth_bearer=str(agent.get("auth_bearer") or ""),
        deliver_configured=deliver_configured,
    )


def _plugin_state_deliver_configured(hermes_home: Path) -> bool:
    state = _read_json(
        hermes_home / "plugins" / "miloco" / "miloco-plugin" / "state.json"
    )
    if not state:
        return False
    deliver = state.get("deliver")
    if not isinstance(deliver, dict):
        return False
    return bool(deliver.get("target") or deliver.get("candidates"))


def discover_home_candidates(
    *,
    env: Mapping[str, str],
    home: Path,
    hermes_home: Path,
    openclaw_state_dir: Path,
    hint: str,
) -> tuple[HomeCandidate, ...]:
    raw_entries: list[tuple[str, str]] = []
    if env.get("MILOCO_HOME"):
        raw_entries.append((env["MILOCO_HOME"], "env"))
    for value in _iter_env_assignments(hermes_home / ".env"):
        key, val = value[1], value[2]
        if key == "MILOCO_HOME" and val:
            raw_entries.append((val, "hermes_env"))
    for rc_name in (".zshrc", ".bashrc"):
        rc = home / rc_name
        if rc.is_file():
            for value in _iter_rc_exports(rc):
                raw_entries.append((value, "shell_rc"))
    raw_entries.append((str(home / ".hermes" / "miloco"), "default_hermes"))
    raw_entries.append((str(home / ".openclaw" / "miloco"), "default_openclaw"))

    deliver_configured = _plugin_state_deliver_configured(hermes_home)
    merged: dict[Path, tuple[Path, set[str]]] = {}
    for raw, source in raw_entries:
        if not raw:
            continue
        path = _expand_path(raw)
        real = path.resolve(strict=False)
        if real in merged:
            merged[real][1].add(source)
        else:
            merged[real] = (path, {source})

    hint_default = _default_home_for(hint, home)
    hint_real = hint_default.resolve(strict=False) if hint_default else None

    def sort_key(pair: tuple[Path, set[str]]) -> tuple[int, str]:
        _path, sources = pair
        if "env" in sources:
            return (0, str(_path))
        if hint_real is not None and _path.resolve(strict=False) == hint_real:
            return (1, str(_path))
        return (2, str(_path))

    candidates = [
        _build_home_candidate(path, sources, deliver_configured=deliver_configured)
        for path, sources in sorted(merged.values(), key=sort_key)
    ]
    return tuple(candidates)


# ---------------------------------------------------------------------------
# Uninstall — Hermes cron access
# ---------------------------------------------------------------------------

_HERMES_CRON_LIST_SNIPPET = """
import json, sys
try:
    from cron.jobs import list_jobs
except Exception as exc:
    print(json.dumps({"error": "import: %s" % exc}))
    sys.exit(3)
try:
    jobs = list_jobs(include_disabled=True) or []
except Exception as exc:
    print(json.dumps({"error": "list: %s" % exc}))
    sys.exit(4)
out = []
for job in jobs:
    if isinstance(job, dict):
        out.append({
            "id": str(job.get("id") or job.get("job_id") or ""),
            "name": str(job.get("name") or ""),
        })
print(json.dumps(out))
"""

_HERMES_CRON_REMOVE_SNIPPET = """
import json, sys
try:
    from cron.jobs import remove_job
except Exception as exc:
    print(json.dumps({"error": "import: %s" % exc}))
    sys.exit(3)
try:
    remove_job(sys.argv[1])
except Exception as exc:
    print(json.dumps({"error": "remove: %s" % exc}))
    sys.exit(4)
print(json.dumps({"ok": True}))
"""


def _hermes_venv_python(hermes_home: Path) -> Path | None:
    candidate = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def _parse_json_output(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# 块状 id 行： "  e044a389aede [active]"（status 只会是 active/paused/completed/disabled）
_CRON_ID_HEADER_RE = re.compile(r"^\s+([A-Za-z0-9][A-Za-z0-9_.:-]*)\s+\[[A-Za-z]+\]\s*$")
# 块状名称行： "    Name:      [miloco:home-profile] miloco-home-patrol"
_CRON_NAME_RE = re.compile(r"^\s*Name:\s+(.*?)\s*$")


def _parse_hermes_cron_text(text: str) -> tuple[ManagedCron, ...]:
    """解析 `hermes cron list` 文本输出。

    真实 CLI 是块状：上一行 `  <id> [status]`、下一行 `    Name:  <name>`
    （`hermes_cli/cron.py` cron_list）。受管 job 的归属证明是 **name 以
    `[miloco:home-profile]` 开头**，不依赖任何安装期 id 记录；id 从紧邻的
    id 行取。仍保留一条单行 `id=`/`id:` 的宽松兜底，但只在该行本身带 tag 时触发。
    """
    jobs: list[ManagedCron] = []
    current_id = ""
    for raw_line in (text or "").splitlines():
        line = _ANSI_RE.sub("", raw_line)
        header = _CRON_ID_HEADER_RE.match(line)
        if header:
            current_id = header.group(1)
            continue
        name_match = _CRON_NAME_RE.match(line)
        if name_match:
            name = name_match.group(1)
            if HERMES_MANAGED_TAG in name:
                jobs.append(ManagedCron(current_id, name, name))
            current_id = ""
            continue
        if HERMES_MANAGED_TAG in line:
            idm = re.search(r"\bid[=:]\s*([A-Za-z0-9_.:-]+)", line)
            stripped = line.strip()
            jobs.append(ManagedCron(idm.group(1) if idm else "", stripped, stripped))
    return tuple(jobs)


def _hermes_cron_live(
    *,
    run: SubprocessRunner,
    hermes_home: Path,
    env: Mapping[str, str],
    cli: str | None,
) -> tuple[str, tuple[ManagedCron, ...], str]:
    """现场读取受管 cron。返回 (backend, jobs, error)。

    CLI 是首选；CLI 不存在或调用失败时才退回 Hermes venv 的本地 API。
    """
    python = _hermes_venv_python(hermes_home)
    cli_error = ""
    if cli:
        cli_env = _subprocess_env(env, HERMES_HOME=hermes_home)
        try:
            help_result = run([cli, "cron", "--help"], env=cli_env, timeout=60)
        except (CommandFailed, CommandTimeout) as exc:
            cli_error = exc.detail or str(exc)
        else:
            help_text = f"{help_result.stdout}\n{help_result.stderr}"
            if help_result.returncode != 0 or not re.search(
                r"\b(rm|remove|delete)\b", help_text
            ):
                cli_error = "hermes cron has no remove subcommand"
            else:
                try:
                    result = run([cli, "cron", "list", "--all"], env=cli_env, timeout=60)
                except (CommandFailed, CommandTimeout) as exc:
                    cli_error = exc.detail or str(exc)
                else:
                    if result.returncode != 0:
                        cli_error = _command_failure_detail(result)
                    else:
                        # 解析器的兜底会把任何含受管 tag 的行收成一个 id 为空的
                        # job；后续 _remove_managed_crons 对空 id 记 BLOCKED。
                        return ("cli", _parse_hermes_cron_text(result.stdout), "")

    local_error = ""
    if python is not None:
        try:
            result = run(
                [str(python), "-c", _HERMES_CRON_LIST_SNIPPET],
                env=_subprocess_env(env, HERMES_HOME=hermes_home),
                timeout=60,
            )
        except (CommandFailed, CommandTimeout) as exc:
            local_error = exc.detail or str(exc)
        else:
            if result.returncode != 0:
                local_error = _command_failure_detail(result)
            else:
                data = _parse_json_output(result.stdout)
                if isinstance(data, dict) and data.get("error"):
                    local_error = str(data["error"])
                elif not isinstance(data, list):
                    local_error = "unexpected cron output"
                else:
                    jobs = tuple(
                        ManagedCron(str(j.get("id") or ""), str(j.get("name") or ""))
                        for j in data
                        if isinstance(j, dict)
                    )
                    return ("local_api", jobs, "")

    if cli_error or local_error:
        parts = []
        if cli_error:
            parts.append(f"cli: {cli_error}")
        if local_error:
            parts.append(f"local_api: {local_error}")
        backend = "local_api" if python is not None else "cli"
        return (backend, (), "; ".join(parts))
    if python is not None:
        return ("local_api", (), "")
    return ("none", (), "")


def _hermes_plugin_line_present(text: str) -> bool:
    for line in (text or "").splitlines():
        tokens = re.split(r"[\s|,]+", line.strip())
        if any(token in HERMES_PLUGIN_NAMES for token in tokens):
            return True
    return False


# ---------------------------------------------------------------------------
# Uninstall — OpenClaw helpers
# ---------------------------------------------------------------------------


def _find_matching_node(value: object, keys: Sequence[str], wanted: str) -> bool:
    if isinstance(value, dict):
        if any(str(value.get(k) or "") == wanted for k in keys):
            return True
        return any(_find_matching_node(v, keys, wanted) for v in value.values())
    if isinstance(value, list):
        return any(_find_matching_node(v, keys, wanted) for v in value)
    return False


def _openclaw_plugin_in_json(text: str) -> bool:
    data = _parse_json_output(text)
    if data is None:
        return OPENCLAW_PLUGIN_ID in (text or "")
    return _find_matching_node(
        data, ("id", "name", "pluginId"), OPENCLAW_PLUGIN_ID
    )


def _openclaw_cron_jobs(text: str) -> list[dict] | None:
    data = _parse_json_output(text)
    if isinstance(data, list):
        return [j for j in data if isinstance(j, dict)]
    if isinstance(data, dict):
        for key in ("jobs", "data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [j for j in value if isinstance(j, dict)]
            if isinstance(value, dict):
                inner = value.get("jobs")
                if isinstance(inner, list):
                    return [j for j in inner if isinstance(j, dict)]
    return None


def _iter_openclaw_plugin_manifests(state_dir: Path):
    for pattern in (
        "extensions/*/openclaw.plugin.json",
        "plugins/*/openclaw.plugin.json",
    ):
        try:
            yield from state_dir.glob(pattern)
        except OSError:
            continue


def _installed_openclaw_tools(state_dir: Path) -> tuple[str, ...] | None:
    for manifest in _iter_openclaw_plugin_manifests(state_dir):
        config = _read_json(manifest)
        if not config or config.get("id") != OPENCLAW_PLUGIN_ID:
            continue
        contracts = config.get("contracts")
        tools = contracts.get("tools") if isinstance(contracts, dict) else None
        if isinstance(tools, list) and all(isinstance(t, str) for t in tools):
            return tuple(tools)
    return None


def _openclaw_plugin_record(
    *, state_dir: Path, cli: str | None, run: SubprocessRunner
) -> bool:
    for manifest in _iter_openclaw_plugin_manifests(state_dir):
        config = _read_json(manifest)
        if config and config.get("id") == OPENCLAW_PLUGIN_ID:
            return True
    installs = state_dir / "plugins" / "installs.json"
    if installs.is_file() and OPENCLAW_PLUGIN_ID in _read_text(installs):
        return True
    if cli:
        try:
            result = run([cli, "plugins", "list", "--json"], timeout=60)
        except (CommandFailed, CommandTimeout):
            return False
        if result.returncode == 0 and _openclaw_plugin_in_json(result.stdout):
            return True
    return False


def _resolve_openclaw_tool_names(
    *, state_dir: Path, script_dir: Path, cli: str | None, run: SubprocessRunner
) -> tuple[tuple[str, ...], str]:
    installed = _installed_openclaw_tools(state_dir)
    if installed:
        return installed, "installed_manifest"

    if cli:
        try:
            result = run(
                [cli, "plugins", "inspect", OPENCLAW_PLUGIN_ID, "--json"],
                timeout=60,
            )
        except (CommandFailed, CommandTimeout):
            result = None
        if result is not None and result.returncode == 0:
            data = _parse_json_output(result.stdout)
            contracts = data.get("contracts") if isinstance(data, dict) else None
            tools = contracts.get("tools") if isinstance(contracts, dict) else None
            if isinstance(tools, list) and tools and all(
                isinstance(t, str) for t in tools
            ):
                return tuple(tools), "inspect"

    repo_root = script_dir.parent
    for path, source in (
        (repo_root / "plugins" / "openclaw" / "openclaw.plugin.json", "openclaw.plugin.json"),
        (repo_root / "dist" / "manifest.json", "dist/manifest.json"),
        (script_dir / "manifest.json", "manifest.json"),
    ):
        if not path.is_file():
            continue
        data = _read_json(path)
        if not data:
            continue
        tools = None
        contracts = data.get("contracts")
        if isinstance(contracts, dict):
            tools = contracts.get("tools")
        if tools is None:
            tools = data.get("tools")
        if isinstance(tools, list) and all(isinstance(t, str) for t in tools) and tools:
            return tuple(tools), source

    return FALLBACK_OPENCLAW_TOOLS, "static_fallback"


def _config_tools_in_allow(config_path: Path) -> tuple[str, ...]:
    config = _read_json(config_path)
    if not config:
        return ()
    tools = config.get("tools")
    values = tools.get("alsoAllow") if isinstance(tools, dict) else None
    if isinstance(values, list):
        return tuple(str(v) for v in values)
    return ()


# ---------------------------------------------------------------------------
# Uninstall — inventory
# ---------------------------------------------------------------------------


def build_inventory(
    *,
    env: Mapping[str, str],
    home: Path,
    agent_platform_hint: str,
    which: Callable[[str], str | None] = shutil.which,
    run: SubprocessRunner,
) -> Inventory:
    hermes_home = resolve_hermes_home(env, home)
    openclaw_state_dir, openclaw_config_path = resolve_openclaw_paths(env, home)
    hermes_cli = which("hermes")
    openclaw_cli = which("openclaw")

    tool_names, tool_source = _resolve_openclaw_tool_names(
        state_dir=openclaw_state_dir,
        script_dir=Path(__file__).parent,
        cli=openclaw_cli,
        run=run,
    )

    candidates = discover_home_candidates(
        env=env,
        home=home,
        hermes_home=hermes_home,
        openclaw_state_dir=openclaw_state_dir,
        hint=agent_platform_hint,
    )

    plugin_dir = hermes_home / "plugins" / HERMES_PLUGIN_ID
    skills_dir = hermes_home / "skills"
    skills: tuple[Path, ...] = ()
    if skills_dir.is_dir():
        try:
            skills = tuple(
                path
                for path in sorted(skills_dir.iterdir())
                if path.name.startswith("miloco-")
            )
        except OSError:
            skills = ()
    config_yaml = hermes_home / "config.yaml"
    config_text = _read_text(config_yaml)
    env_file = hermes_home / ".env"
    env_assignments = list(_iter_env_assignments(env_file))
    managed_bearers = {c.auth_bearer for c in candidates if c.auth_bearer}
    env_managed_bearers = tuple(
        value
        for _idx, key, value in env_assignments
        if key == "API_SERVER_KEY" and value in managed_bearers
    )
    env_homes = tuple(
        value for _idx, key, value in env_assignments if key == "MILOCO_HOME"
    )
    rc_files = tuple(
        rc for rc in (home / ".zshrc", home / ".bashrc") if rc.is_file()
    )
    rc_marker = any(
        _RC_MARKER_RE.match(line)
        for rc in rc_files
        for line in _read_text(rc).splitlines()
    )

    venv_python = _hermes_venv_python(hermes_home)
    if hermes_cli:
        cron_backend = "cli"
    elif venv_python is not None:
        cron_backend = "local_api"
    else:
        cron_backend = "none"
    managed_crons: tuple[ManagedCron, ...] = ()
    hermes_probe_error: str | None = None
    if cron_backend != "none":
        backend, jobs, probe_error = _hermes_cron_live(
            run=run, hermes_home=hermes_home, env=env, cli=hermes_cli
        )
        if backend != "none":
            cron_backend = backend
        managed_crons = tuple(
            job for job in jobs if job.name.startswith(HERMES_MANAGED_TAG)
        )
        if probe_error:
            hermes_probe_error = probe_error

    candidate_homes = [c.real_path for c in candidates]
    detected_hermes = bool(
        os.path.lexists(plugin_dir)
        or skills
        or any(
            c.has_hermes_adapter or c.has_install_cache or c.agent_platform == "hermes"
            for c in candidates
        )
        or any(_same_path(value, candidate_homes) for value in env_homes)
        or env_managed_bearers
        or rc_marker
        or "miloco" in config_text
        or managed_crons
    )

    openclaw_plugin_record = _openclaw_plugin_record(
        state_dir=openclaw_state_dir, cli=openclaw_cli, run=run
    )
    openclaw_probe_error: str | None = None
    openclaw_crons: tuple[ManagedCron, ...] = ()
    if openclaw_cli:
        try:
            result = run([openclaw_cli, "cron", "list", "--all", "--json"], timeout=30)
            if result.returncode == 0:
                jobs = _openclaw_cron_jobs(result.stdout) or []
                openclaw_crons = tuple(
                    ManagedCron(
                        str(j.get("id") or j.get("jobId") or j.get("name") or ""),
                        str(j.get("name") or ""),
                        str(j.get("description") or ""),
                    )
                    for j in jobs
                    if HERMES_MANAGED_TAG in str(j.get("description") or "")
                )
            else:
                openclaw_probe_error = _command_failure_detail(result)
        except (CommandFailed, CommandTimeout) as exc:
            openclaw_probe_error = exc.detail or str(exc)

    config_text_oc = _read_text(openclaw_config_path)
    installs_text = _read_text(openclaw_state_dir / "plugins" / "installs.json")
    config_tools = _config_tools_in_allow(openclaw_config_path)
    detected_openclaw = bool(
        openclaw_plugin_record
        or any(tool in config_text_oc for tool in tool_names)
        or OPENCLAW_PLUGIN_ID in config_text_oc
        or OPENCLAW_PLUGIN_ID in installs_text
        or openclaw_crons
        or any(tool in config_tools for tool in tool_names)
    )

    hermes_facts = HermesFacts(
        detected=detected_hermes,
        cli=hermes_cli,
        venv_python=venv_python,
        plugin_dir=plugin_dir,
        skills=skills,
        config_yaml=config_yaml,
        config_mentions_plugin="miloco" in config_text,
        env_file=env_file,
        env_managed_bearers=env_managed_bearers,
        env_miloco_home_lines=env_homes,
        rc_files=rc_files,
        cron_backend=cron_backend,
        managed_crons=managed_crons,
        cli_probe_error=hermes_probe_error,
    )
    openclaw_facts = OpenClawFacts(
        detected=detected_openclaw,
        cli=openclaw_cli,
        state_dir=openclaw_state_dir,
        config_path=openclaw_config_path,
        plugin_id=OPENCLAW_PLUGIN_ID,
        plugin_record=openclaw_plugin_record,
        tools_in_also_allow=config_tools,
        managed_crons=openclaw_crons,
        cli_probe_error=openclaw_probe_error,
    )
    existing = [c for c in candidates if c.exists]
    primary_home = existing[0].path if existing else (
        candidates[0].path if candidates else None
    )
    return Inventory(
        home=home,
        candidates=candidates,
        primary_home=primary_home,
        hermes=hermes_facts,
        openclaw=openclaw_facts,
        tool_names=tool_names,
        tool_names_source=tool_source,
    )


# ---------------------------------------------------------------------------
# Uninstall — text file editors
# ---------------------------------------------------------------------------


class TextFileEditor:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> tuple[str, list[str]]:
        target = Path(os.path.realpath(self.path))
        text = target.read_bytes().decode("utf-8")
        return text, text.splitlines(keepends=True)

    def commit(
        self, new_lines: Sequence[str], *, remove_indices: Iterable[int] = ()
    ) -> None:
        _text, lines = self.read()
        removed = set(remove_indices)
        expected = [line for i, line in enumerate(lines) if i not in removed]
        if list(new_lines) != expected:
            raise MalformedFile(
                "uninstall.detail.plan_mismatch",
                str(self.path),
                detail="planned content differs from on-disk content",
            )
        _atomic_write_text(self.path, "".join(new_lines))


@dataclass(frozen=True)
class EnvPlan:
    remove: tuple[int, ...]
    kept: tuple[tuple[int, str], ...]
    removed_keys: tuple[str, ...]
    lines: tuple[str, ...]


class EnvFileCleaner:
    def __init__(self, path: Path, *, i18n: I18n) -> None:
        self.path = path
        self.i18n = i18n

    def plan(
        self, *, managed_bearers: set[str], managed_homes: set[Path]
    ) -> EnvPlan:
        _text, lines = TextFileEditor(self.path).read()
        remove: list[int] = []
        kept: list[tuple[int, str]] = []
        removed_keys: list[str] = []
        for index, line in enumerate(lines):
            match = _ENV_LINE_RE.match(line.rstrip("\r\n"))
            if not match:
                continue
            key, raw_value = match.group(1), match.group(2)
            if key not in ("API_SERVER_KEY", "MILOCO_HOME"):
                continue
            value = _unquote(raw_value)
            if key == "API_SERVER_KEY":
                if value and value in managed_bearers:
                    remove.append(index)
                    removed_keys.append(key)
                else:
                    kept.append((index, "uninstall.detail.kept_api_key"))
            else:
                if value and _same_path(value, managed_homes):
                    remove.append(index)
                    removed_keys.append(key)
                else:
                    kept.append((index, "uninstall.detail.kept_env_home"))
        return EnvPlan(
            tuple(remove), tuple(kept), tuple(removed_keys), tuple(lines)
        )

    def apply(self, plan: EnvPlan, *, platform: str = "hermes") -> CleanupItem:
        key = "uninstall.hermes.env"
        target = str(self.path)
        if not plan.lines:
            return CleanupItem(key, platform, target, ItemStatus.ABSENT)
        if not plan.remove:
            if plan.kept:
                reasons = []
                for _index, reason_key in plan.kept:
                    text = self.i18n.t(reason_key)
                    if text not in reasons:
                        reasons.append(text)
                return CleanupItem(
                    key,
                    platform,
                    target,
                    ItemStatus.KEPT,
                    "; ".join(reasons),
                )
            return CleanupItem(key, platform, target, ItemStatus.ABSENT)
        removed = set(plan.remove)
        new_lines = [line for i, line in enumerate(plan.lines) if i not in removed]
        TextFileEditor(self.path).commit(new_lines, remove_indices=plan.remove)
        detail = ", ".join(sorted(set(plan.removed_keys)))
        if plan.kept:
            reasons = []
            for _index, reason_key in plan.kept:
                text = self.i18n.t(reason_key)
                if text not in reasons:
                    reasons.append(text)
            detail += " · " + "; ".join(reasons)
        return CleanupItem(key, platform, target, ItemStatus.DONE, detail)


@dataclass(frozen=True)
class RcPlan:
    remove: tuple[int, ...]
    kept_exports: tuple[tuple[int, Path], ...]
    lines: tuple[str, ...]
    marker_found: bool


class ShellRcCleaner:
    def __init__(self, path: Path, *, i18n: I18n) -> None:
        self.path = path
        self.i18n = i18n

    def plan(self, *, managed_homes: set[Path]) -> RcPlan:
        _text, lines = TextFileEditor(self.path).read()
        remove: set[int] = set()
        kept: list[tuple[int, Path]] = []
        marker_found = False
        index = 0
        while index < len(lines):
            body = lines[index].rstrip("\r\n")
            match = _RC_MARKER_RE.match(body)
            if match:
                marker_found = True
                remove.add(index)
                next_index = index + 1
                if next_index < len(lines):
                    export = _RC_EXPORT_RE.match(lines[next_index].rstrip("\r\n"))
                    if export:
                        value = _unquote(export.group(1))
                        if value and _same_path(value, managed_homes):
                            remove.add(next_index)
                        else:
                            kept.append((next_index, _expand_path(value)))
                        index = next_index + 1
                        continue
                index += 1
                continue
            export = _RC_EXPORT_RE.match(body)
            if export:
                # 已有 export 时安装器是原地改写、不会补标记行；值指向本次清理
                # 的 home 即是归属证明，否则（用户自己的其他目录）保留并报告。
                value = _unquote(export.group(1))
                if value and _same_path(value, managed_homes):
                    remove.add(index)
                elif value:
                    kept.append((index, _expand_path(value)))
            index += 1
        return RcPlan(
            tuple(sorted(remove)), tuple(kept), tuple(lines), marker_found
        )

    def apply(self, plan: RcPlan, *, platform: str = "hermes") -> CleanupItem:
        key = "uninstall.hermes.shell_rc"
        target = str(self.path)
        if not plan.lines or not plan.remove:
            return CleanupItem(key, platform, target, ItemStatus.ABSENT)
        new_lines = [
            line for i, line in enumerate(plan.lines) if i not in set(plan.remove)
        ]
        TextFileEditor(self.path).commit(new_lines, remove_indices=plan.remove)
        detail = ""
        if plan.kept_exports:
            paths = ", ".join(str(p) for _i, p in plan.kept_exports)
            detail = self.i18n.t("uninstall.detail.kept_export", paths)
        return CleanupItem(key, platform, target, ItemStatus.DONE, detail)


@dataclass(frozen=True)
class JsonPlan:
    before: dict
    after: dict
    changed: bool
    current_platform: str


class ConfigJsonCleaner:
    def __init__(self, path: Path, *, i18n: I18n) -> None:
        self.path = path
        self.i18n = i18n

    def plan(self, *, clear_platform: bool) -> JsonPlan:
        try:
            before = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MalformedFile(
                "uninstall.detail.json_parse",
                str(self.path),
                detail=str(exc),
            ) from exc
        if not isinstance(before, dict):
            raise MalformedFile(
                "uninstall.detail.json_parse",
                str(self.path),
                detail="top-level JSON is not an object",
            )
        agent = before.get("agent")
        agent = agent if isinstance(agent, dict) else {}
        current = str(agent.get("platform") or "")
        after = copy.deepcopy(before)
        changed = bool(clear_platform and current == "hermes")
        if changed:
            after.setdefault("agent", {})["platform"] = ""
        return JsonPlan(before, after, changed, current)

    def apply(self, plan: JsonPlan, *, platform: str = "hermes") -> CleanupItem:
        key = "uninstall.hermes.agent_platform"
        target = str(self.path)
        if not plan.changed:
            if plan.current_platform:
                detail = self.i18n.t(
                    "uninstall.detail.platform_not_hermes", plan.current_platform
                )
                return CleanupItem(
                    key, platform, target, ItemStatus.KEPT, detail
                )
            return CleanupItem(key, platform, target, ItemStatus.ABSENT)

        expected = copy.deepcopy(plan.before)
        expected.setdefault("agent", {})["platform"] = ""
        if expected != plan.after:
            raise MalformedFile(
                "uninstall.detail.plan_mismatch",
                target,
                detail="only agent.platform may change",
            )
        original = self.path.read_text(encoding="utf-8")
        text = json.dumps(plan.after, indent=2, ensure_ascii=False)
        if original.endswith("\n"):
            text += "\n"
        _atomic_write_text(self.path, text)
        return CleanupItem(key, platform, target, ItemStatus.DONE)


# ---------------------------------------------------------------------------
# Uninstall — platform cleaners
# ---------------------------------------------------------------------------


class _PlatformCleaner:
    """Hermes/OpenClaw cleaner 共用的单步执行与异常记录。"""

    platform: str

    def _step(self, key: str, target: str, fn) -> None:
        try:
            result = fn()
        except UninstallError as exc:
            self.report.add(
                CleanupItem(
                    exc.key or key,
                    self.platform,
                    exc.target or target,
                    exc.status,
                    exc.detail,
                    exc.remediation,
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 - one item must not kill the rest
            self.report.add(
                CleanupItem(key, self.platform, target, ItemStatus.FAILED, repr(exc))
            )
            return
        if result is None:
            return
        items = [result] if isinstance(result, CleanupItem) else list(result)
        for item in items:
            self.report.add(item)


class HermesCleaner(_PlatformCleaner):
    platform = "hermes"

    def __init__(
        self,
        *,
        ui: UI,
        inv: Inventory,
        decisions: Mapping[Path, bool],
        run: SubprocessRunner,
        env: Mapping[str, str],
    ) -> None:
        self.ui = ui
        self.i18n = ui.i18n
        self.inv = inv
        self.facts = inv.hermes
        self.decisions = decisions
        self._run = run
        self.env = env
        self.hermes_home = self.facts.config_yaml.parent
        self.report = PlatformReport(
            "hermes",
            detected=self.facts.detected,
            cli_available=bool(self.facts.cli),
        )
        self._plugin_dir_removal_blocked = False

    def run(self) -> PlatformReport:
        self._step("uninstall.hermes.cron", HERMES_MANAGED_TAG, self._remove_managed_crons)
        self._step(
            "uninstall.hermes.plugin_state_backup",
            str(self.facts.plugin_dir / "miloco-plugin" / "state.json"),
            self._backup_plugin_state,
        )
        self._step(
            "uninstall.hermes.plugin_dir",
            str(self.facts.plugin_dir),
            self._remove_plugin_dir,
        )
        self._step(
            "uninstall.hermes.skills",
            str(self.hermes_home / "skills"),
            self._remove_skills,
        )
        self._step(
            "uninstall.hermes.adapter",
            str(self.inv.primary_home or ""),
            self._remove_adapters,
        )
        self._step(
            "uninstall.hermes.install_cache",
            str(self.inv.primary_home or ""),
            self._remove_install_cache,
        )
        self._step(
            "uninstall.hermes.agent_platform",
            str(self.inv.primary_home or ""),
            self._clear_agent_platform,
        )
        self._step("uninstall.hermes.env", str(self.facts.env_file), self._clean_env)
        self._step(
            "uninstall.hermes.shell_rc",
            ", ".join(str(p) for p in self.facts.rc_files) or "~/.zshrc, ~/.bashrc",
            self._clean_shell_rc,
        )
        self._step("uninstall.hermes.cli_verify", "hermes", self._verify_cli)
        self._scrub_secrets()
        return self.report

    def _scrub_secrets(self) -> None:
        """I10：bearer 只参与比较，绝不进入任何输出字段。"""
        secrets_ = [c.auth_bearer for c in self.inv.candidates if c.auth_bearer]
        if not secrets_:
            return
        self.report.items = [
            replace(
                item,
                target=_redact_secrets(item.target, secrets_),
                detail=_redact_secrets(item.detail, secrets_),
            )
            for item in self.report.items
        ]

    # -- step plumbing -----------------------------------------------------

    def _env(self) -> dict[str, str]:
        return _subprocess_env(self.env, HERMES_HOME=self.hermes_home)

    def _home_will_be_deleted(self, candidate: HomeCandidate) -> bool:
        return bool(self.decisions.get(candidate.real_path, False))

    # -- cron --------------------------------------------------------------

    def _remove_managed_crons(self) -> list[CleanupItem]:
        key = "uninstall.hermes.cron"
        backend, jobs, error = _hermes_cron_live(
            run=self._run,
            hermes_home=self.hermes_home,
            env=self.env,
            cli=self.facts.cli,
        )
        if backend == "none":
            return [
                CleanupItem(
                    "uninstall.hermes.cli_unavailable",
                    "hermes",
                    "hermes cron",
                    ItemStatus.BLOCKED,
                    self.i18n.t("uninstall.detail.no_cron_backend"),
                    "install Hermes or run `hermes cron list` after restoring the CLI",
                )
            ]
        if error:
            return [
                CleanupItem(
                    key,
                    "hermes",
                    HERMES_MANAGED_TAG,
                    ItemStatus.FAILED,
                    error,
                    "hermes cron list",
                )
            ]
        managed = [
            job
            for job in jobs
            if job.name.startswith(HERMES_MANAGED_TAG)
            # 解析器兜底产出的空 id job：tag 在行内但无法定位块结构，
            # 交给下面的空 id 分支报 BLOCKED，不能静默 ABSENT。
            or (not job.id and HERMES_MANAGED_TAG in job.name)
        ]
        if not managed:
            return [CleanupItem(key, "hermes", HERMES_MANAGED_TAG, ItemStatus.ABSENT)]

        items: list[CleanupItem] = []
        for job in managed:
            target = job.name or job.id
            if not job.id:
                items.append(
                    CleanupItem(
                        key,
                        "hermes",
                        target,
                        ItemStatus.BLOCKED,
                        self.i18n.t("uninstall.detail.cron_id_unknown"),
                        "hermes cron list",
                    )
                )
                continue
            if backend == "local_api":
                try:
                    result = self._run(
                        [
                            str(self.facts.venv_python),
                            "-c",
                            _HERMES_CRON_REMOVE_SNIPPET,
                            job.id,
                        ],
                        env=self._env(),
                        timeout=60,
                    )
                except (CommandFailed, CommandTimeout) as exc:
                    items.append(
                        CleanupItem(
                            key,
                            "hermes",
                            target,
                            ItemStatus.FAILED,
                            exc.detail or str(exc),
                            f"hermes cron rm {job.id}",
                        )
                    )
                    continue
            else:
                try:
                    result = self._run(
                        [str(self.facts.cli), "cron", "rm", job.id], timeout=60
                    )
                except (CommandFailed, CommandTimeout) as exc:
                    items.append(
                        CleanupItem(
                            key,
                            "hermes",
                            target,
                            ItemStatus.FAILED,
                            exc.detail or str(exc),
                            f"hermes cron rm {job.id}",
                        )
                    )
                    continue
            if result.returncode != 0:
                items.append(
                    CleanupItem(
                        key,
                        "hermes",
                        target,
                        ItemStatus.FAILED,
                        _command_failure_detail(result),
                        f"hermes cron rm {job.id}",
                    )
                )
            else:
                items.append(CleanupItem(key, "hermes", target, ItemStatus.DONE))

        _backend, remaining, verify_error = _hermes_cron_live(
            run=self._run,
            hermes_home=self.hermes_home,
            env=self.env,
            cli=self.facts.cli,
        )
        left = [j.name for j in remaining if j.name.startswith(HERMES_MANAGED_TAG)]
        if verify_error:
            items.append(
                CleanupItem(
                    key,
                    "hermes",
                    HERMES_MANAGED_TAG,
                    ItemStatus.FAILED,
                    verify_error,
                    "hermes cron list",
                )
            )
        elif left:
            items.append(
                CleanupItem(
                    key,
                    "hermes",
                    ", ".join(left),
                    ItemStatus.FAILED,
                    self.i18n.t("uninstall.detail.cron_remaining"),
                    "hermes cron list",
                )
            )
        return items

    # -- plugin registry ---------------------------------------------------

    def _plugins_present(self) -> tuple[bool | None, str]:
        """返回 (miloco 是否在 plugins list 中, 错误)；None 表示列表不可用。"""
        try:
            result = self._run(
                [self.facts.cli, "plugins", "list", "--plain", "--no-bundled"],
                env=self._env(),
                timeout=60,
            )
        except (CommandFailed, CommandTimeout) as exc:
            return None, exc.detail or str(exc)
        if result.returncode != 0:
            return None, _command_failure_detail(result)
        return _hermes_plugin_line_present(result.stdout), ""

    # -- plugin state / dir ------------------------------------------------

    def _backup_plugin_state(self) -> CleanupItem:
        key = "uninstall.hermes.plugin_state_backup"
        state_path = self.facts.plugin_dir / "miloco-plugin" / "state.json"
        target = str(state_path)
        if not state_path.is_file():
            return CleanupItem(key, "hermes", target, ItemStatus.ABSENT)
        state = _read_json(state_path)
        deliver = state.get("deliver") if state else None
        if not isinstance(deliver, dict) or not (
            deliver.get("target") or deliver.get("candidates")
        ):
            return CleanupItem(key, "hermes", target, ItemStatus.ABSENT)

        existing = [c for c in self.inv.candidates if c.exists]
        if existing and all(
            self.decisions.get(c.real_path, False) for c in existing
        ):
            return CleanupItem(
                key,
                "hermes",
                target,
                ItemStatus.SKIPPED,
                self.i18n.t("uninstall.detail.home_deleted"),
            )

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = self.hermes_home / "miloco-uninstall-backup"
        backup = backup_dir / f"{stamp}-plugin-state.json"
        counter = 1
        while backup.exists():
            backup = backup_dir / f"{stamp}-{counter}-plugin-state.json"
            counter += 1
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(state_path, backup)
        except OSError as exc:
            self._plugin_dir_removal_blocked = True
            return CleanupItem(
                key,
                "hermes",
                target,
                ItemStatus.FAILED,
                self.i18n.t("uninstall.detail.backup_failed", str(exc)),
            )
        return CleanupItem(
            key,
            "hermes",
            str(backup),
            ItemStatus.DONE,
            self.i18n.t("uninstall.detail.state_backed_up"),
        )

    def _remove_plugin_dir(self) -> CleanupItem:
        key = "uninstall.hermes.plugin_dir"
        target = str(self.facts.plugin_dir)
        if self._plugin_dir_removal_blocked:
            return CleanupItem(
                key,
                "hermes",
                target,
                ItemStatus.FAILED,
                self.i18n.t("uninstall.detail.backup_failed_skip_delete"),
            )
        if not os.path.lexists(self.facts.plugin_dir):
            return CleanupItem(key, "hermes", target, ItemStatus.ABSENT)
        if not self.facts.cli:
            return self._remove_plugin_dir_direct(
                target, self.i18n.t("uninstall.detail.plugin_dir_cli_missing")
            )
        try:
            result = self._run(
                [self.facts.cli, "plugins", "remove", HERMES_PLUGIN_ID],
                env=self._env(),
                timeout=60,
            )
        except (CommandFailed, CommandTimeout) as exc:
            return self._remove_plugin_dir_direct(target, exc.detail or str(exc))
        if result.returncode != 0:
            if not os.path.lexists(self.facts.plugin_dir):
                return CleanupItem(key, "hermes", target, ItemStatus.ABSENT)
            return self._remove_plugin_dir_direct(
                target,
                self.i18n.t(
                    "uninstall.detail.plugin_dir_fallback",
                    _command_failure_detail(result),
                ),
            )
        if os.path.lexists(self.facts.plugin_dir):
            return self._remove_plugin_dir_direct(
                target,
                self.i18n.t(
                    "uninstall.detail.plugin_dir_fallback",
                    "returned 0 but directory still exists",
                ),
            )
        return CleanupItem(key, "hermes", target, ItemStatus.DONE)

    def _remove_plugin_dir_direct(self, target: str, detail: str) -> CleanupItem:
        key = "uninstall.hermes.plugin_dir"
        try:
            _rm_path(self.facts.plugin_dir)
        except OSError as exc:
            return CleanupItem(
                key, "hermes", target, ItemStatus.FAILED, f"{detail}; {exc}"
            )
        return CleanupItem(key, "hermes", target, ItemStatus.DONE, detail)

    def _remove_skills(self) -> list[CleanupItem]:
        key = "uninstall.hermes.skills"
        skills_dir = self.hermes_home / "skills"
        if not skills_dir.is_dir():
            return [CleanupItem(key, "hermes", str(skills_dir), ItemStatus.ABSENT)]
        try:
            skills = [
                path
                for path in sorted(skills_dir.iterdir())
                if path.name.startswith("miloco-")
            ]
        except OSError as exc:
            raise UninstallError(key, str(skills_dir), detail=str(exc)) from exc
        if not skills:
            return [CleanupItem(key, "hermes", str(skills_dir), ItemStatus.ABSENT)]
        items: list[CleanupItem] = []
        for skill in skills:
            try:
                _rm_path(skill)
            except OSError as exc:
                items.append(
                    CleanupItem(key, "hermes", str(skill), ItemStatus.FAILED, str(exc))
                )
            else:
                items.append(CleanupItem(key, "hermes", str(skill), ItemStatus.DONE))
        return items

    # -- per-home artifacts ------------------------------------------------

    def _candidate_homes(self) -> list[HomeCandidate]:
        homes = [c for c in self.inv.candidates if c.exists]
        return homes or [c for c in self.inv.candidates[:1]]

    def _remove_home_artifact(self, key: str, *parts: str) -> list[CleanupItem]:
        items: list[CleanupItem] = []
        for candidate in self._candidate_homes():
            target = candidate.path.joinpath(*parts)
            if self._home_will_be_deleted(candidate):
                status = (
                    ItemStatus.SKIPPED
                    if os.path.lexists(target)
                    else ItemStatus.ABSENT
                )
                items.append(CleanupItem(key, "hermes", str(target), status))
                continue
            if not os.path.lexists(target):
                items.append(
                    CleanupItem(key, "hermes", str(target), ItemStatus.ABSENT)
                )
                continue
            try:
                _rm_path(target)
                _rmdir_if_empty(target.parent)
            except OSError as exc:
                items.append(
                    CleanupItem(key, "hermes", str(target), ItemStatus.FAILED, str(exc))
                )
            else:
                items.append(
                    CleanupItem(key, "hermes", str(target), ItemStatus.DONE)
                )
        return items

    def _remove_adapters(self) -> list[CleanupItem]:
        return self._remove_home_artifact(
            "uninstall.hermes.adapter", "agent_platform", "hermes"
        )

    def _remove_install_cache(self) -> list[CleanupItem]:
        return self._remove_home_artifact(
            "uninstall.hermes.install_cache", ".install-cache", "hermes-plugin"
        )

    def _clear_agent_platform(self) -> list[CleanupItem]:
        key = "uninstall.hermes.agent_platform"
        items: list[CleanupItem] = []
        for candidate in self._candidate_homes():
            config = candidate.path / "config.json"
            if self._home_will_be_deleted(candidate):
                items.append(
                    CleanupItem(
                        key,
                        "hermes",
                        str(config),
                        ItemStatus.SKIPPED,
                        self.i18n.t("uninstall.detail.home_deleted"),
                    )
                )
                continue
            if not config.is_file():
                items.append(
                    CleanupItem(key, "hermes", str(config), ItemStatus.ABSENT)
                )
                continue
            cleaner = ConfigJsonCleaner(config, i18n=self.i18n)
            items.append(cleaner.apply(cleaner.plan(clear_platform=True)))
        return items

    # -- env / shell rc ----------------------------------------------------

    def _clean_env(self) -> CleanupItem:
        if not self.facts.env_file.is_file():
            return CleanupItem(
                "uninstall.hermes.env",
                "hermes",
                str(self.facts.env_file),
                ItemStatus.ABSENT,
            )
        managed_bearers = {
            c.auth_bearer for c in self.inv.candidates if c.auth_bearer
        }
        managed_homes = {c.real_path for c in self.inv.candidates}
        cleaner = EnvFileCleaner(self.facts.env_file, i18n=self.i18n)
        plan = cleaner.plan(
            managed_bearers=managed_bearers, managed_homes=managed_homes
        )
        return cleaner.apply(plan)

    def _managed_homes(self) -> set[Path]:
        """rc 归属判断用的 miloco home 集合。

        仅由 shell rc 发现的候选要再看一眼内容：用户自己的 `export MILOCO_HOME`
        也会被读进候选集，只有当目标目录确实带 miloco 产物（config.json /
        agent_platform / 安装缓存 / agent.platform）时才算受管 home。
        """
        homes: set[Path] = set()
        for candidate in self.inv.candidates:
            if candidate.sources - {"shell_rc"}:
                homes.add(candidate.real_path)
            elif (
                candidate.has_config
                or candidate.has_agent_platform
                or candidate.has_install_cache
                or candidate.agent_platform
            ):
                homes.add(candidate.real_path)
        return homes

    def _clean_shell_rc(self) -> list[CleanupItem]:
        key = "uninstall.hermes.shell_rc"
        if not self.facts.rc_files:
            return [
                CleanupItem(
                    key, "hermes", "~/.zshrc, ~/.bashrc", ItemStatus.ABSENT
                )
            ]
        managed_homes = self._managed_homes()
        items: list[CleanupItem] = []
        for rc in self.facts.rc_files:
            if not rc.is_file():
                items.append(
                    CleanupItem(key, "hermes", str(rc), ItemStatus.ABSENT)
                )
                continue
            cleaner = ShellRcCleaner(rc, i18n=self.i18n)
            items.append(cleaner.apply(cleaner.plan(managed_homes=managed_homes)))
        return items

    # -- verification ------------------------------------------------------

    def _verify_cli(self) -> CleanupItem:
        key = "uninstall.hermes.cli_verify"
        target = str(self.facts.plugin_dir)
        # 目录是 Hermes 插件加载的唯一依据；目录不存在时 config 残留是惰性的，
        # 按本 PR 的职责边界交给 Hermes，不因此把卸载判失败。
        if os.path.lexists(self.facts.plugin_dir):
            return CleanupItem(
                key,
                "hermes",
                target,
                ItemStatus.FAILED,
                self.i18n.t("uninstall.detail.plugin_dir_still_present"),
                f"hermes plugins remove {HERMES_PLUGIN_ID}",
            )
        if not self.facts.cli:
            return CleanupItem(
                key,
                "hermes",
                target,
                ItemStatus.DONE,
                self.i18n.t("uninstall.detail.verify_cli_missing"),
            )
        present, error = self._plugins_present()
        if error:
            # 插件目录已删（ground truth）：验证命令自身失败不推翻清理结果。
            return CleanupItem(
                key,
                "hermes",
                target,
                ItemStatus.DONE,
                self.i18n.t("uninstall.detail.verify_list_error", error),
            )
        if present:
            return CleanupItem(
                key,
                "hermes",
                target,
                ItemStatus.DONE,
                self.i18n.t("uninstall.detail.config_residue"),
            )
        return CleanupItem(key, "hermes", target, ItemStatus.DONE)


class OpenClawCleaner(_PlatformCleaner):
    platform = "openclaw"

    def __init__(
        self,
        *,
        ui: UI,
        inv: Inventory,
        run: SubprocessRunner,
    ) -> None:
        self.ui = ui
        self.i18n = ui.i18n
        self.inv = inv
        self.facts = inv.openclaw
        self._run = run
        self.report = PlatformReport(
            "openclaw",
            detected=self.facts.detected,
            cli_available=bool(self.facts.cli),
        )

    def run(self) -> PlatformReport:
        self._step(
            "uninstall.openclaw.plugin",
            f"openclaw plugins uninstall {OPENCLAW_PLUGIN_ID}",
            self._uninstall_plugin,
        )
        self._step(
            "uninstall.openclaw.tools_allow",
            "tools.alsoAllow",
            self._clean_tools_allow,
        )
        self._step(
            "uninstall.openclaw.plugin_entry",
            f'plugins.entries["{OPENCLAW_PLUGIN_ID}"]',
            self._clean_plugin_entry,
        )
        self._step(
            "uninstall.openclaw.cron",
            HERMES_MANAGED_TAG,
            self._remove_managed_crons,
        )
        self._step(
            "uninstall.openclaw.verify",
            OPENCLAW_PLUGIN_ID,
            self._verify_plugin_absent,
        )
        return self.report

    def _cli_missing(self, command: str) -> CleanupItem:
        return CleanupItem(
            "uninstall.openclaw.cli_unavailable",
            "openclaw",
            command,
            ItemStatus.BLOCKED,
            self.i18n.t("uninstall.detail.cli_missing", "openclaw"),
            command,
        )

    def _uninstall_plugin(self) -> CleanupItem:
        key = "uninstall.openclaw.plugin"
        command = f"openclaw plugins uninstall {OPENCLAW_PLUGIN_ID}"
        if not self.facts.cli:
            return self._cli_missing(command)
        try:
            result = self._run(
                [self.facts.cli, "plugins", "uninstall", OPENCLAW_PLUGIN_ID],
                timeout=120,
                stdin="y\n",
            )
        except (CommandFailed, CommandTimeout) as exc:
            return CleanupItem(
                key, "openclaw", command, ItemStatus.FAILED, exc.detail or str(exc), command
            )
        if result.returncode == 0:
            return CleanupItem(key, "openclaw", command, ItemStatus.DONE)
        return CleanupItem(
            key,
            "openclaw",
            command,
            ItemStatus.FAILED,
            _command_failure_detail(result),
            command,
        )

    def _clean_tools_allow(self) -> CleanupItem:
        key = "uninstall.openclaw.tools_allow"
        command = "openclaw config get tools.alsoAllow --json"
        if not self.facts.cli:
            return self._cli_missing(command)
        try:
            result = self._run(
                [self.facts.cli, "config", "get", "tools.alsoAllow", "--json"],
                timeout=30,
            )
        except (CommandFailed, CommandTimeout) as exc:
            return CleanupItem(
                key, "openclaw", command, ItemStatus.FAILED, exc.detail or str(exc), command
            )
        if result.returncode != 0:
            return CleanupItem(
                key,
                "openclaw",
                command,
                ItemStatus.FAILED,
                _command_failure_detail(result),
                command,
            )
        raw = result.stdout.strip()
        if not raw or raw == "null":
            return CleanupItem(key, "openclaw", "tools.alsoAllow", ItemStatus.ABSENT)
        try:
            current = json.loads(raw)
        except json.JSONDecodeError as exc:
            return CleanupItem(key, "openclaw", command, ItemStatus.FAILED, str(exc))
        if not isinstance(current, list):
            return CleanupItem(
                key,
                "openclaw",
                "tools.alsoAllow",
                ItemStatus.FAILED,
                self.i18n.t("uninstall.detail.not_a_list"),
            )
        managed = set(self.inv.tool_names)
        filtered = [value for value in current if value not in managed]
        if len(filtered) == len(current):
            return CleanupItem(key, "openclaw", "tools.alsoAllow", ItemStatus.ABSENT)
        removed = ", ".join(str(v) for v in current if v in managed)
        set_command = (
            "openclaw config set tools.alsoAllow "
            f"{json.dumps(filtered)} --strict-json"
        )
        try:
            set_result = self._run(
                [
                    self.facts.cli,
                    "config",
                    "set",
                    "tools.alsoAllow",
                    json.dumps(filtered),
                    "--strict-json",
                ],
                timeout=30,
            )
        except (CommandFailed, CommandTimeout) as exc:
            return CleanupItem(
                key, "openclaw", set_command, ItemStatus.FAILED, exc.detail or str(exc), set_command
            )
        if set_result.returncode != 0:
            return CleanupItem(
                key,
                "openclaw",
                set_command,
                ItemStatus.FAILED,
                _command_failure_detail(set_result),
                set_command,
            )
        return CleanupItem(
            key, "openclaw", "tools.alsoAllow", ItemStatus.DONE, removed
        )

    def _clean_plugin_entry(self) -> CleanupItem:
        key = "uninstall.openclaw.plugin_entry"
        path = f'plugins.entries["{OPENCLAW_PLUGIN_ID}"]'
        command = f"openclaw config unset {path}"
        if not self.facts.cli:
            return self._cli_missing(command)
        try:
            result = self._run(
                [self.facts.cli, "config", "unset", path], timeout=30
            )
        except (CommandFailed, CommandTimeout) as exc:
            return CleanupItem(
                key, "openclaw", command, ItemStatus.FAILED, exc.detail or str(exc), command
            )
        if result.returncode == 0:
            return CleanupItem(key, "openclaw", path, ItemStatus.DONE)
        output = f"{result.stdout}\n{result.stderr}".lower()
        if any(
            marker in output
            for marker in ("not found", "not exist", "no such", "does not exist")
        ):
            return CleanupItem(
                key,
                "openclaw",
                path,
                ItemStatus.ABSENT,
                self.i18n.t("uninstall.detail.already_absent"),
            )
        return CleanupItem(
            key,
            "openclaw",
            command,
            ItemStatus.FAILED,
            _command_failure_detail(result),
            command,
        )

    def _remove_managed_crons(self) -> list[CleanupItem]:
        key = "uninstall.openclaw.cron"
        list_command = "openclaw cron list --all --json"
        if not self.facts.cli:
            return [self._cli_missing(list_command)]
        try:
            result = self._run(
                [self.facts.cli, "cron", "list", "--all", "--json"], timeout=30
            )
        except (CommandFailed, CommandTimeout) as exc:
            return [
                CleanupItem(
                    key,
                    "openclaw",
                    list_command,
                    ItemStatus.FAILED,
                    exc.detail or str(exc),
                    "openclaw gateway start",
                )
            ]
        if result.returncode != 0:
            return [
                CleanupItem(
                    key,
                    "openclaw",
                    list_command,
                    ItemStatus.FAILED,
                    _command_failure_detail(result),
                    "openclaw gateway start",
                )
            ]
        jobs = _openclaw_cron_jobs(result.stdout)
        if jobs is None:
            return [
                CleanupItem(
                    key,
                    "openclaw",
                    list_command,
                    ItemStatus.FAILED,
                    self.i18n.t("uninstall.detail.cron_parse"),
                    list_command,
                )
            ]
        managed = [
            job
            for job in jobs
            if HERMES_MANAGED_TAG in str(job.get("description") or "")
        ]
        if not managed:
            return [CleanupItem(key, "openclaw", HERMES_MANAGED_TAG, ItemStatus.ABSENT)]
        items: list[CleanupItem] = []
        for job in managed:
            job_id = str(job.get("id") or job.get("jobId") or "")
            name = str(job.get("name") or job_id)
            if not job_id:
                items.append(
                    CleanupItem(
                        key,
                        "openclaw",
                        name,
                        ItemStatus.BLOCKED,
                        self.i18n.t("uninstall.detail.cron_id_unknown"),
                        list_command,
                    )
                )
                continue
            try:
                rm_result = self._run(
                    [self.facts.cli, "cron", "rm", job_id], timeout=30
                )
            except (CommandFailed, CommandTimeout) as exc:
                items.append(
                    CleanupItem(
                        key,
                        "openclaw",
                        name,
                        ItemStatus.FAILED,
                        exc.detail or str(exc),
                        f"openclaw cron rm {job_id}",
                    )
                )
                continue
            if rm_result.returncode != 0:
                items.append(
                    CleanupItem(
                        key,
                        "openclaw",
                        name,
                        ItemStatus.FAILED,
                        _command_failure_detail(rm_result),
                        f"openclaw cron rm {job_id}",
                    )
                )
            else:
                items.append(CleanupItem(key, "openclaw", name, ItemStatus.DONE))

        try:
            verify = self._run(
                [self.facts.cli, "cron", "list", "--all", "--json"], timeout=30
            )
        except (CommandFailed, CommandTimeout) as exc:
            items.append(
                CleanupItem(
                    key,
                    "openclaw",
                    list_command,
                    ItemStatus.FAILED,
                    exc.detail or str(exc),
                    list_command,
                )
            )
            return items
        remaining_jobs = _openclaw_cron_jobs(verify.stdout) if verify.returncode == 0 else None
        if remaining_jobs is not None:
            remaining = [
                str(job.get("name") or job.get("id") or "")
                for job in remaining_jobs
                if HERMES_MANAGED_TAG in str(job.get("description") or "")
            ]
            if remaining:
                items.append(
                    CleanupItem(
                        key,
                        "openclaw",
                        ", ".join(remaining),
                        ItemStatus.FAILED,
                        self.i18n.t("uninstall.detail.cron_remaining"),
                        list_command,
                    )
                )
        return items

    def _verify_plugin_absent(self) -> CleanupItem:
        key = "uninstall.openclaw.verify"
        command = "openclaw plugins list --json"
        if not self.facts.cli:
            return self._cli_missing(command)
        try:
            result = self._run(
                [self.facts.cli, "plugins", "list", "--json"], timeout=60
            )
        except (CommandFailed, CommandTimeout) as exc:
            return CleanupItem(
                key, "openclaw", command, ItemStatus.FAILED, exc.detail or str(exc), command
            )
        if result.returncode != 0:
            return CleanupItem(
                key,
                "openclaw",
                command,
                ItemStatus.FAILED,
                _command_failure_detail(result),
                command,
            )
        if _openclaw_plugin_in_json(result.stdout):
            return CleanupItem(
                key,
                "openclaw",
                OPENCLAW_PLUGIN_ID,
                ItemStatus.FAILED,
                self.i18n.t("uninstall.detail.plugin_still_installed"),
                f"openclaw plugins uninstall {OPENCLAW_PLUGIN_ID}",
            )
        return CleanupItem(key, "openclaw", OPENCLAW_PLUGIN_ID, ItemStatus.DONE)


class CoreCleaner:
    def __init__(self, *, ui: UI, run: SubprocessRunner) -> None:
        self.ui = ui
        self.i18n = ui.i18n
        self._run = run
        self.report = PlatformReport("core", detected=True, cli_available=True)

    def run(self) -> PlatformReport:
        uv = shutil.which("uv")
        if not uv:
            self.report.add(
                CleanupItem(
                    "uninstall.core.uv_missing",
                    "core",
                    "uv",
                    ItemStatus.ABSENT,
                    self.i18n.t("uninstall.detail.uv_missing"),
                )
            )
            return self.report
        try:
            listed = self._run([uv, "tool", "list"], timeout=15)
        except (CommandFailed, CommandTimeout) as exc:
            self.report.add(
                CleanupItem(
                    "uninstall.core.uv_item",
                    "core",
                    "uv tool list",
                    ItemStatus.FAILED,
                    exc.detail or str(exc),
                    "uv tool list",
                )
            )
            return self.report
        if listed.returncode != 0:
            self.report.add(
                CleanupItem(
                    "uninstall.core.uv_item",
                    "core",
                    "uv tool list",
                    ItemStatus.FAILED,
                    _command_failure_detail(listed),
                    "uv tool list",
                )
            )
            return self.report
        installed = _parse_uv_tool_list(listed.stdout)
        for package in UV_TOOL_PACKAGES:
            if package not in installed:
                self.report.add(
                    CleanupItem(
                        "uninstall.core.uv_item", "core", package, ItemStatus.ABSENT
                    )
                )
                continue
            self.report.add(self._uninstall_package(uv, package))
        return self.report

    def _uninstall_package(self, uv: str, package: str) -> CleanupItem:
        command = f"uv tool uninstall {package}"
        try:
            result = self._run([uv, "tool", "uninstall", package], timeout=60)
        except (CommandFailed, CommandTimeout) as exc:
            return CleanupItem(
                "uninstall.core.uv_item",
                "core",
                command,
                ItemStatus.FAILED,
                exc.detail or str(exc),
                command,
            )
        if result.returncode != 0:
            return CleanupItem(
                "uninstall.core.uv_item",
                "core",
                command,
                ItemStatus.FAILED,
                _command_failure_detail(result),
                command,
            )
        key = (
            "uninstall.cli_removed"
            if package == "miloco-cli"
            else "uninstall.miloco_removed"
            if package == "miloco"
            else "uninstall.core.uv_item"
        )
        return CleanupItem(key, "core", package, ItemStatus.DONE)


def _parse_uv_tool_list(text: str) -> set[str]:
    names: set[str] = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("-"):
            stripped = stripped[1:].strip()
        if stripped:
            names.add(stripped.split()[0])
    return names


# ---------------------------------------------------------------------------
# Uninstall — home decisions / deletion / reporting / entry point
# ---------------------------------------------------------------------------


def decide_home_deletions(
    candidates: Sequence[HomeCandidate], *, ui: UI, interactive: bool
) -> dict[Path, bool]:
    decisions: dict[Path, bool] = {}
    ask = interactive
    for candidate in candidates:
        if not candidate.exists:
            continue
        if not ask:
            decisions[candidate.real_path] = False
            continue
        try:
            answer = ui.prompt_confirm(
                ui.i18n.t("uninstall.home.ask", str(candidate.path)), default=False
            )
        except KeyboardInterrupt:
            # 用户中断后续询问：保留数据目录，其余清理照常完成。
            ask = False
            decisions[candidate.real_path] = False
            continue
        decisions[candidate.real_path] = bool(answer)
    return decisions


def delete_homes(
    candidates: Sequence[HomeCandidate],
    decisions: Mapping[Path, bool],
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[CleanupItem]:
    home = home or Path.home()
    env = env if env is not None else os.environ
    hermes_home = resolve_hermes_home(env, home)
    openclaw_state_dir, _config = resolve_openclaw_paths(env, home)
    protected = [home, hermes_home, openclaw_state_dir]
    existing = [c for c in candidates if c.exists]
    items: list[CleanupItem] = []
    for candidate in sorted(
        existing, key=lambda c: len(c.real_path.parts), reverse=True
    ):
        key = "uninstall.home.deleted"
        target = str(candidate.path)
        if not decisions.get(candidate.real_path, False):
            items.append(
                CleanupItem("uninstall.home.kept", "core", target, ItemStatus.KEPT)
            )
            continue
        if not os.path.lexists(candidate.path):
            items.append(CleanupItem(key, "core", target, ItemStatus.SKIPPED))
            continue
        try:
            _delete_home(candidate, home=home, protected=protected)
        except UnsafePath as exc:
            items.append(
                CleanupItem(
                    "uninstall.home.unsafe",
                    "core",
                    target,
                    ItemStatus.FAILED,
                    exc.detail,
                )
            )
        except OSError as exc:
            items.append(
                CleanupItem(key, "core", target, ItemStatus.FAILED, str(exc))
            )
        else:
            items.append(CleanupItem(key, "core", target, ItemStatus.DONE))
    return items


def _delete_home(
    candidate: HomeCandidate, *, home: Path, protected: Sequence[Path]
) -> None:
    path = candidate.path
    real = candidate.real_path
    if not path.is_absolute():
        raise UnsafePath(
            "uninstall.home.unsafe",
            str(path),
            detail="path is not absolute",
        )
    for protected_path in protected:
        protected_real = protected_path.resolve(strict=False)
        if _is_ancestor_or_self(real, protected_real):
            raise UnsafePath(
                "uninstall.home.unsafe",
                str(path),
                detail=f"refuses to delete an ancestor of {protected_real}",
            )
    try:
        st = path.stat()
    except OSError as exc:
        raise UnsafePath(
            "uninstall.home.unsafe", str(path), detail=str(exc)
        ) from exc
    if candidate.identity is not None and (st.st_dev, st.st_ino) != candidate.identity:
        raise UnsafePath(
            "uninstall.home.unsafe",
            str(path),
            detail="inode changed since inventory",
        )
    _rm_path(path)


def _print_cleanup_item(ui: UI, item: CleanupItem) -> None:
    i18n = ui.i18n
    label = i18n.t(item.key)
    if item.status is ItemStatus.DONE:
        prefix = "[green]✓[/green]"
        line = f"  {prefix} {label}: {item.target}"
        # DONE 也可能带“部分行被保留”的原因（如 .env 的 API key 被用户改过），
        # 这些说明必须可见，否则用户不知道某一行为什么没删。
        if item.detail:
            line += f" [dim]— {item.detail}[/dim]"
    elif item.status in (ItemStatus.FAILED, ItemStatus.BLOCKED):
        prefix = "[red]✗[/red]"
        line = f"  {prefix} {label}: {item.target}"
        if item.detail:
            line += f" [dim]— {item.detail}[/dim]"
    else:
        prefix = "[dim]–[/dim]"
        line = (
            f"  {prefix} {label}: {item.target}"
            f" [dim]({i18n.t('uninstall.status.' + item.status.value)})[/dim]"
        )
        if item.detail:
            line += f" [dim]— {item.detail}[/dim]"
    ui.console.print(line)
    if item.remediation and item.status in (ItemStatus.FAILED, ItemStatus.BLOCKED):
        ui.console.print(
            f"    [yellow]{i18n.t('uninstall.summary.remediation')}:[/yellow] "
            f"[cyan]{item.remediation}[/cyan]"
        )


def _print_uninstall_report(
    ui: UI,
    reports: Sequence[PlatformReport],
    home_items: Sequence[CleanupItem],
    *,
    tool_source: str = "",
) -> bool:
    i18n = ui.i18n
    for report in reports:
        ui.console.print()
        ui.console.print(
            f"[bold cyan]{i18n.t('uninstall.section.' + report.platform)}[/bold cyan]"
        )
        if not report.detected:
            ui.console.print(
                f"  [dim]– {i18n.t('uninstall.status.not_detected')}[/dim]"
            )
            continue
        if report.platform == "openclaw" and tool_source:
            ui.console.print(
                f"  [dim]{i18n.t('uninstall.openclaw.tool_source', tool_source)}[/dim]"
            )
        for item in report.items:
            _print_cleanup_item(ui, item)

    if home_items:
        ui.console.print()
        ui.console.print(
            f"[bold cyan]{i18n.t('uninstall.section.home')}[/bold cyan]"
        )
        for item in home_items:
            _print_cleanup_item(ui, item)

    ok = all(not report.incomplete for report in reports) and all(
        item.status not in (ItemStatus.FAILED, ItemStatus.BLOCKED)
        for item in home_items
    )
    ui.console.print()
    if ok:
        ui.ok(i18n.t("uninstall.summary.done"))
    else:
        ui.warn(i18n.t("uninstall.summary.incomplete"))
        ui.console.print(f"[dim]{i18n.t('uninstall.summary.remediation')}[/dim]")
    if any(
        report.detected and report.platform in ("hermes", "openclaw")
        for report in reports
    ):
        ui.console.print(f"[dim]{i18n.t('uninstall.summary.gateway_hint')}[/dim]")
    ui.console.print()
    return ok


def run_uninstall(
    ui: UI, *, args: argparse.Namespace, miloco_home: Path | None = None
) -> int:
    ui.info(ui.i18n.t("uninstall.title"))
    _remove_launchagent(ui)
    env = dict(os.environ)
    # 安装器把实际使用的 home 持久化在 runtime pointer；卸载也要把它纳入盘点。
    if "MILOCO_HOME" not in env and miloco_home is not None:
        env["MILOCO_HOME"] = str(miloco_home)
    home = Path.home()
    run = SubprocessRunner()

    inventory = build_inventory(
        env=env,
        home=home,
        agent_platform_hint=getattr(args, "agent_platform", "") or "",
        run=run,
    )
    decisions = decide_home_deletions(
        inventory.candidates,
        ui=ui,
        interactive=sys.stdin.isatty(),
    )

    reports: list[PlatformReport] = []
    if inventory.hermes.detected:
        reports.append(
            HermesCleaner(
                ui=ui,
                inv=inventory,
                decisions=decisions,
                run=run,
                env=env,
            ).run()
        )
    else:
        reports.append(
            PlatformReport(
                "hermes", detected=False, cli_available=bool(inventory.hermes.cli)
            )
        )
    if inventory.openclaw.detected:
        reports.append(
            OpenClawCleaner(ui=ui, inv=inventory, run=run).run()
        )
    else:
        reports.append(
            PlatformReport(
                "openclaw",
                detected=False,
                cli_available=bool(inventory.openclaw.cli),
            )
        )

    home_items = delete_homes(inventory.candidates, decisions, home=home, env=env)
    reports.append(CoreCleaner(ui=ui, run=run).run())

    ok = _print_uninstall_report(
        ui, reports, home_items, tool_source=inventory.tool_names_source
    )
    return 0 if ok else 1
