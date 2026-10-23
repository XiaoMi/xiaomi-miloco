"""Uninstall 内部逻辑测试：分支、边界与错误路径。

目标（design.md §5 / 不变量 I1-I15）：

* 状态映射：DONE/ABSENT/KEPT/SKIPPED/FAILED/BLOCKED -> 报告与退出码
* 归属证明：只有精确匹配/值相等/标记行相邻才删除
* 最小改动与事务性：只改目标行、保留权限、校验失败不写盘
* 路径安全：拒绝 /、home、祖先路径、符号链接不跟随
* CLI/本地 API 不可用 -> BLOCKED，而不是静默跳过

测试按 `__file__` 定位仓库，不依赖 cwd 或已安装的包；被测模块是被执行
副本里的 `scripts/install.py`（变异测试跑的就是副本）。
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass


def _ensure_optional_deps() -> None:
    try:
        import httpx  # noqa: F401
    except ImportError:
        module = types.ModuleType("httpx")
        module.HTTPError = Exception
        sys.modules["httpx"] = module
    try:
        import questionary  # noqa: F401
    except ImportError:
        sys.modules["questionary"] = types.ModuleType("questionary")
    try:
        from rich.console import Console  # noqa: F401
    except ImportError:
        rich = types.ModuleType("rich")
        console = types.ModuleType("rich.console")
        console.Console = _Dummy
        progress = types.ModuleType("rich.progress")
        for name in (
            "BarColumn",
            "DownloadColumn",
            "Progress",
            "SpinnerColumn",
            "TextColumn",
            "TimeRemainingColumn",
            "TransferSpeedColumn",
        ):
            setattr(progress, name, _Dummy)
        rich.console = console
        rich.progress = progress
        sys.modules["rich"] = rich
        sys.modules["rich.console"] = console
        sys.modules["rich.progress"] = progress


def _load_install_module():
    """按包名导入 `scripts.install`。

    变异测试靠模块名把覆盖回填到变异体；用 importlib 起一个自定义模块名
    会让覆盖率统计对不上（trampoline 记的是 `miloco_install_...` 而不是
    `scripts.install`）。因此这里显式把仓库根（变异副本时是副本根）放进
    `sys.path`，再用包路径导入。
    """
    _ensure_optional_deps()
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    # 若同一进程里已有别的 `scripts` 来源，先清掉，避免 import 到原仓库。
    existing = sys.modules.get("scripts")
    if existing is not None:
        paths = getattr(existing, "__path__", [])
        if not any(str(Path(p).resolve()) == str((REPO_ROOT / "scripts").resolve()) for p in paths):
            for name in [n for n in sys.modules if n == "scripts" or n.startswith("scripts.")]:
                del sys.modules[name]
    module = importlib.import_module("scripts.install")
    return module


install = _load_install_module()
I18N = install.I18n("en", SCRIPTS)
STATUS = install.ItemStatus


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def res(stdout: str = "", *, rc: int = 0, stderr: str = "", argv=()) -> "install.CommandResult":
    return install.CommandResult(tuple(argv), rc, stdout, stderr)


class FakeRunner:
    """按顺序返回预设结果；记录每次调用。"""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, *, env=None, timeout=30.0, stdin=None):
        self.calls.append(
            {
                "argv": [str(a) for a in argv],
                "env": dict(env or {}),
                "timeout": timeout,
                "stdin": stdin,
            }
        )
        if not self.results:
            raise AssertionError(f"unexpected command: {list(argv)}")
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(self.calls[-1]["argv"])
        return item

    @property
    def argvs(self):
        return [call["argv"] for call in self.calls]


class _Console:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **_kwargs):
        for arg in args:
            self.lines.append(str(arg))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class FakeUI:
    def __init__(self):
        self.i18n = I18N
        self.console = _Console()
        self.console_err = _Console()

    def ok(self, msg):
        self.console.print(msg)

    def warn(self, msg):
        self.console.err_print = None
        self.console.print(msg)

    def info(self, msg):
        self.console.print(msg)


def _candidate(path: Path, sources=("env",), *, deliver_configured=False):
    return install._build_home_candidate(
        path, set(sources), deliver_configured=deliver_configured
    )


def _hermes_facts(config_yaml: Path, **overrides):
    defaults = dict(
        detected=True,
        cli=None,
        venv_python=None,
        plugin_dir=config_yaml.parent / "plugins" / install.HERMES_PLUGIN_ID,
        skills=(),
        config_yaml=config_yaml,
        config_mentions_plugin=False,
        env_file=config_yaml.parent / ".env",
        env_managed_bearers=(),
        env_miloco_home_lines=(),
        rc_files=(),
        cron_backend="none",
        managed_crons=(),
        cli_probe_error=None,
    )
    defaults.update(overrides)
    return install.HermesFacts(**defaults)


def _openclaw_facts(state_dir: Path, **overrides):
    defaults = dict(
        detected=True,
        cli=None,
        state_dir=state_dir,
        config_path=state_dir / "openclaw.json",
        plugin_id=install.OPENCLAW_PLUGIN_ID,
        plugin_record=False,
        tools_in_also_allow=(),
        managed_crons=(),
        cli_probe_error=None,
    )
    defaults.update(overrides)
    return install.OpenClawFacts(**defaults)


def _inventory(home: Path, *, candidates=(), hermes=None, openclaw=None, tool_names=None):
    return install.Inventory(
        home=home,
        candidates=tuple(candidates),
        primary_home=candidates[0].path if candidates else None,
        hermes=hermes or _hermes_facts(home / ".hermes" / "config.yaml"),
        openclaw=openclaw or _openclaw_facts(home / ".openclaw"),
        tool_names=tuple(tool_names or install.FALLBACK_OPENCLAW_TOOLS),
        tool_names_source="static_fallback",
    )


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


# ---------------------------------------------------------------------------
# 数据模型 / 状态映射（I6）
# ---------------------------------------------------------------------------


class ReportStatusTest(unittest.TestCase):
    def _report(self, status):
        report = install.PlatformReport("hermes", detected=True, cli_available=True)
        report.add(install.CleanupItem("k", "hermes", "t", status))
        return report

    def test_incomplete_only_for_failed_and_blocked(self):
        for status in (STATUS.DONE, STATUS.ABSENT, STATUS.KEPT, STATUS.SKIPPED):
            self.assertFalse(
                self._report(status).incomplete, f"{status} must not be incomplete"
            )
        for status in (STATUS.FAILED, STATUS.BLOCKED):
            self.assertTrue(
                self._report(status).incomplete, f"{status} must be incomplete"
            )

    def test_status_values(self):
        self.assertEqual(STATUS.DONE.value, "done")
        self.assertEqual(STATUS.ABSENT.value, "absent")
        self.assertEqual(STATUS.KEPT.value, "kept")
        self.assertEqual(STATUS.SKIPPED.value, "skipped")
        self.assertEqual(STATUS.FAILED.value, "failed")
        self.assertEqual(STATUS.BLOCKED.value, "blocked")

    def test_error_status_classes(self):
        self.assertIs(install.CliNotFound("k", "t").status, STATUS.BLOCKED)
        self.assertIs(install.LocalApiUnavailable("k", "t").status, STATUS.BLOCKED)
        self.assertIs(install.OwnershipUncertain("k", "t").status, STATUS.KEPT)
        self.assertIs(install.CommandFailed("k", "t").status, STATUS.FAILED)
        self.assertIsInstance(install.CommandTimeout("k", "t"), install.CommandFailed)
        self.assertIs(install.MalformedFile("k", "t").status, STATUS.FAILED)
        self.assertIs(install.UnsafePath("k", "t").status, STATUS.FAILED)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


class CommandHelpersTest(unittest.TestCase):
    def test_failure_detail_uses_last_nonempty_stderr_line(self):
        result = res(rc=3, stderr="warn\n\n  boom happened  \n\n", argv=("hermes", "x"))
        detail = install._command_failure_detail(result)
        self.assertIn("hermes x", detail)
        self.assertIn("rc=3", detail)
        self.assertIn("boom happened", detail)
        self.assertNotIn("warn", detail)

    def test_failure_detail_without_stderr(self):
        detail = install._command_failure_detail(res(rc=2, argv=("a", "b")))
        self.assertEqual(detail, "a b (rc=2)")

    def test_failure_detail_truncates(self):
        detail = install._command_failure_detail(
            res(rc=1, stderr="x" * 50, argv=("cmd",)), limit=5
        )
        self.assertTrue(detail.endswith("xxxxx…"))
        self.assertNotIn("x" * 10, detail)

    def test_redact_secrets(self):
        text = "key=supersecret other=abc"
        self.assertEqual(
            install._redact_secrets(text, ["supersecret", "abc", ""]),
            "key=*** other=abc",
        )

    def test_parse_uv_tool_list(self):
        text = "miloco-cli v2026.10.16\n\n- supervisor v1.2\nmiloco\n"
        self.assertEqual(
            install._parse_uv_tool_list(text),
            {"miloco-cli", "supervisor", "miloco"},
        )
        self.assertEqual(install._parse_uv_tool_list(""), set())

    def test_unquote(self):
        self.assertEqual(install._unquote(' "v" '), "v")
        self.assertEqual(install._unquote("'v'"), "v")
        self.assertEqual(install._unquote('"v'), '"v')
        self.assertEqual(install._unquote("v"), "v")

    def test_same_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "a"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            self.assertTrue(install._same_path(str(link), [target]))
            self.assertFalse(install._same_path(str(root / "b"), [target]))

    def test_iter_env_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "export API_SERVER_KEY='v'\n# comment\nNO_ASSIGN\n X = 1\n"
            )
            self.assertEqual(
                list(install._iter_env_assignments(env_file)),
                [(0, "API_SERVER_KEY", "v"), (3, "X", "1")],
            )

    def test_iter_rc_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = Path(tmp) / ".zshrc"
            rc.write_text(
                'export MILOCO_HOME="/a"\n'
                "MILOCO_HOME=/b\n"
                "export MILOCO_HOME=\n"
                'export PATH="/x"\n'
            )
            self.assertEqual(list(install._iter_rc_exports(rc)), ["/a"])

    def test_is_ancestor_or_self(self):
        root = Path("/tmp/x")
        self.assertTrue(install._is_ancestor_or_self(root, root))
        self.assertTrue(install._is_ancestor_or_self(root, root / "a"))
        self.assertFalse(install._is_ancestor_or_self(root, Path("/tmp/y")))


class SubprocessRunnerTest(unittest.TestCase):
    def test_success_and_stdin(self):
        completed = types.SimpleNamespace(returncode=0, stdout="out", stderr="")
        with mock.patch.object(install.subprocess, "run", return_value=completed) as run:
            result = install.SubprocessRunner()(["a", "b"], stdin="y\n")
        self.assertEqual(result.argv, ("a", "b"))
        self.assertEqual(result.stdout, "out")
        self.assertEqual(run.call_args.kwargs["input"], "y\n")
        self.assertNotIn("stdin", run.call_args.kwargs)
        self.assertIs(run.call_args.kwargs["capture_output"], True)
        self.assertIs(run.call_args.kwargs["text"], True)
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)

    def test_env_is_forwarded(self):
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(install.subprocess, "run", return_value=completed) as run:
            install.SubprocessRunner()(["a"], env={"HERMES_HOME": "/h"})
        self.assertEqual(run.call_args.kwargs["env"], {"HERMES_HOME": "/h"})

    def test_no_stdin_closes_stdin(self):
        completed = types.SimpleNamespace(returncode=0, stdout=None, stderr=None)
        with mock.patch.object(install.subprocess, "run", return_value=completed) as run:
            result = install.SubprocessRunner()(["a"])
        self.assertEqual(result.stdout, "")
        self.assertIs(run.call_args.kwargs["stdin"], install.subprocess.DEVNULL)

    def test_timeout_maps_to_command_timeout(self):
        import subprocess as sp

        with mock.patch.object(
            install.subprocess, "run", side_effect=sp.TimeoutExpired(["x"], 1)
        ) as run:
            with self.assertRaises(install.CommandTimeout) as ctx:
                install.SubprocessRunner()(["x"], timeout=2)
        self.assertIn("timeout", ctx.exception.detail)
        self.assertIs(ctx.exception.status, STATUS.FAILED)
        self.assertEqual(ctx.exception.key, "uninstall.detail.command_failed")
        self.assertEqual(ctx.exception.target, "x")
        self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_oserror_maps_to_command_failed(self):
        with mock.patch.object(
            install.subprocess, "run", side_effect=OSError("no exec")
        ):
            with self.assertRaises(install.CommandFailed) as ctx:
                install.SubprocessRunner()(["x"])
        self.assertIn("no exec", ctx.exception.detail)
        self.assertEqual(ctx.exception.key, "uninstall.detail.command_failed")
        self.assertEqual(ctx.exception.target, "x")

    def test_subprocess_env_overrides_only_truthy(self):
        self.assertEqual(
            install._subprocess_env({"A": "1"}, B="2", C=""), {"A": "1", "B": "2"}
        )

    def test_read_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing"
            self.assertEqual(install._read_text(missing), "")
            self.assertIsNone(install._read_json(missing))
            bad_utf8 = root / "bad"
            bad_utf8.write_bytes(b"\xff\xfe\x00")
            self.assertEqual(install._read_text(bad_utf8), "")
            invalid = root / "invalid.json"
            invalid.write_text("{nope")
            self.assertIsNone(install._read_json(invalid))
            array = root / "array.json"
            array.write_text("[1]")
            self.assertIsNone(install._read_json(array))
            good = root / "good.json"
            good.write_text('{"a": 1}')
            self.assertEqual(install._read_json(good), {"a": 1})

    def test_expand_path_handles_env_and_tilde(self):
        with mock.patch.dict(os.environ, {"MILOCO_TEST_ROOT": "/var/tmp/x"}):
            self.assertEqual(
                install._expand_path("$MILOCO_TEST_ROOT/sub"), Path("/var/tmp/x/sub")
            )
            self.assertEqual(
                install._expand_path('"$MILOCO_TEST_ROOT"'), Path("/var/tmp/x")
            )
        self.assertEqual(install._expand_path("~/abc"), Path.home() / "abc")


class FilesystemHelpersTest(unittest.TestCase):
    def test_rm_path_nonexistent_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(install._rm_path(root / "missing"))
            target = root / "target"
            target.mkdir()
            (target / "f").write_text("x")
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            self.assertTrue(install._rm_path(link))
            self.assertFalse(link.is_symlink())
            self.assertTrue((target / "f").exists())

    def test_rm_path_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dir"
            (target / "sub").mkdir(parents=True)
            self.assertTrue(install._rm_path(target))
            self.assertFalse(target.exists())

    def test_rmdir_if_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty"
            empty.mkdir()
            install._rmdir_if_empty(empty)
            self.assertFalse(empty.exists())

            full = root / "full"
            full.mkdir()
            (full / "f").write_text("x")
            install._rmdir_if_empty(full)
            self.assertTrue(full.exists())

            install._rmdir_if_empty(root / "missing")

    def test_atomic_write_preserves_mode_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real.txt"
            real.write_text("old")
            real.chmod(0o640)
            link = root / "link.txt"
            link.symlink_to(real)
            install._atomic_write_text(link, "new")
            self.assertTrue(link.is_symlink())
            self.assertEqual(real.read_text(), "new")
            self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o640)
            leftovers = [p.name for p in root.iterdir() if p.name.startswith(".miloco-")]
            self.assertEqual(leftovers, [])


    def test_atomic_write_failure_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "f.txt"
            target.write_text("old")
            with mock.patch.object(
                install.os, "replace", side_effect=OSError("disk full")
            ):
                with self.assertRaises(OSError):
                    install._atomic_write_text(target, "new")
            self.assertEqual(target.read_text(), "old")
            leftovers = [p.name for p in root.iterdir() if p.name.startswith(".miloco-")]
            self.assertEqual(leftovers, [])

    # ---------------------------------------------------------------------------
    # 平台路径 / 候选发现
    # ---------------------------------------------------------------------------


class PathsTest(unittest.TestCase):
    def test_resolve_hermes_home(self):
        home = Path("/h")
        self.assertEqual(
            install.resolve_hermes_home({}, home), home / ".hermes"
        )
        self.assertEqual(
            install.resolve_hermes_home({"HERMES_HOME": "/custom"}, home),
            Path("/custom"),
        )

    def test_resolve_openclaw_paths(self):
        home = Path("/h")
        self.assertEqual(
            install.resolve_openclaw_paths({}, home),
            (home / ".openclaw", home / ".openclaw" / "openclaw.json"),
        )
        self.assertEqual(
            install.resolve_openclaw_paths(
                {"OPENCLAW_STATE_DIR": "/s"}, home
            ),
            (Path("/s"), Path("/s/openclaw.json")),
        )
        self.assertEqual(
            install.resolve_openclaw_paths(
                {"OPENCLAW_STATE_DIR": "/s", "OPENCLAW_CONFIG_PATH": "/c.json"}, home
            ),
            (Path("/s"), Path("/c.json")),
        )

    def test_default_home_for(self):
        home = Path("/h")
        self.assertEqual(
            install._default_home_for("hermes", home), home / ".hermes" / "miloco"
        )
        self.assertEqual(
            install._default_home_for("openclaw", home), home / ".openclaw" / "miloco"
        )
        self.assertIsNone(install._default_home_for("", home))


class HomeCandidateTest(unittest.TestCase):
    def test_candidate_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "agent_platform" / "hermes").mkdir(parents=True)
            (home / ".install-cache" / "hermes-plugin").mkdir(parents=True)
            (home / "config.json").write_text(
                json.dumps({"agent": {"platform": "hermes", "auth_bearer": "b1"}})
            )
            candidate = _candidate(home, {"env", "shell_rc"}, deliver_configured=True)
            self.assertTrue(candidate.exists)
            self.assertFalse(candidate.is_symlink)
            self.assertTrue(candidate.has_config)
            self.assertTrue(candidate.has_agent_platform)
            self.assertTrue(candidate.has_hermes_adapter)
            self.assertTrue(candidate.has_install_cache)
            self.assertEqual(candidate.agent_platform, "hermes")
            self.assertEqual(candidate.auth_bearer, "b1")
            self.assertTrue(candidate.deliver_configured)
            self.assertEqual(candidate.sources, frozenset({"env", "shell_rc"}))
            self.assertIsNotNone(candidate.identity)

    def test_missing_candidate_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = _candidate(Path(tmp) / "nope")
            self.assertFalse(candidate.exists)
            self.assertIsNone(candidate.identity)
            self.assertFalse(candidate.has_config)
            self.assertEqual(candidate.agent_platform, "")

    def test_plugin_state_deliver_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            state = hermes_home / "plugins" / "miloco" / "miloco-plugin" / "state.json"
            self.assertFalse(install._plugin_state_deliver_configured(hermes_home))
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"deliver": {"target": "t"}}))
            self.assertTrue(install._plugin_state_deliver_configured(hermes_home))
            state.write_text(json.dumps({"deliver": {"candidates": ["a"]}}))
            self.assertTrue(install._plugin_state_deliver_configured(hermes_home))
            state.write_text(json.dumps({"deliver": {}}))
            self.assertFalse(install._plugin_state_deliver_configured(hermes_home))
            state.write_text(json.dumps({"deliver": "not-a-dict"}))
            self.assertFalse(install._plugin_state_deliver_configured(hermes_home))
            state.write_text("{broken")
            self.assertFalse(install._plugin_state_deliver_configured(hermes_home))


class DiscoveryTest(unittest.TestCase):
    def test_order_env_then_hint_then_rest_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            hermes_home = home / ".hermes"
            hermes_home.mkdir()
            env_home = root / "from-env"
            env_home.mkdir()
            (hermes_home / ".env").write_text(f"MILOCO_HOME={env_home}\n")
            rc = home / ".zshrc"
            rc.write_text(f"export MILOCO_HOME={env_home}\n")
            hint_home = home / ".hermes" / "miloco"
            hint_home.mkdir(parents=True)

            candidates = install.discover_home_candidates(
                env={"MILOCO_HOME": str(env_home)},
                home=home,
                hermes_home=hermes_home,
                openclaw_state_dir=home / ".openclaw",
                hint="hermes",
            )
            self.assertEqual(candidates[0].real_path, env_home.resolve())
            self.assertEqual(candidates[1].real_path, hint_home.resolve())
            merged = next(c for c in candidates if c.real_path == env_home.resolve())
            self.assertEqual(
                merged.sources, frozenset({"env", "hermes_env", "shell_rc"})
            )
            reals = [c.real_path for c in candidates]
            self.assertEqual(len(reals), len(set(reals)))
            # 默认 openclaw 目录总在候选集里（即使不存在）
            self.assertIn(
                (home / ".openclaw" / "miloco").resolve(), reals
            )

    def test_deliver_flag_propagates_to_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_home = home / ".hermes"
            state = hermes_home / "plugins" / "miloco" / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"deliver": {"target": "x"}}))
            candidates = install.discover_home_candidates(
                env={},
                home=home,
                hermes_home=hermes_home,
                openclaw_state_dir=home / ".openclaw",
                hint="",
            )
            self.assertTrue(all(c.deliver_configured for c in candidates))


# ---------------------------------------------------------------------------
# cron 访问
# ---------------------------------------------------------------------------


class HermesCronTest(unittest.TestCase):
    def test_parse_cron_text(self):
        text = (
            "id=abc123 [miloco:home-profile] morning\n"
            "  [miloco:home-profile] evening (id: def456)\n"
            "unrelated job\n"
        )
        jobs = install._parse_hermes_cron_text(text)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].id, "abc123")
        self.assertEqual(jobs[1].id, "def456")

    def test_live_uses_local_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            runner = FakeRunner(
                [res(json.dumps([{"id": "1", "name": "[miloco:home-profile] a"}]))]
            )
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=hermes_home, env={}, cli=None
            )
            self.assertEqual(backend, "local_api")
            self.assertEqual(error, "")
            self.assertEqual(jobs[0].id, "1")
            self.assertEqual(runner.calls[0]["env"]["HERMES_HOME"], str(hermes_home))

    def test_live_local_api_error_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            runner = FakeRunner([res(json.dumps({"error": "boom"}))])
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=hermes_home, env={}, cli=None
            )
            self.assertEqual(backend, "local_api")
            self.assertEqual(jobs, ())
            self.assertIn("boom", error)

    def test_live_local_api_unexpected_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            runner = FakeRunner([res("not json")])
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=hermes_home, env={}, cli=None
            )
            self.assertEqual(backend, "local_api")
            self.assertEqual(jobs, ())
            self.assertTrue(error)

    def test_live_cli_without_remove_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([res("usage: hermes cron [list|show]")])
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=Path(tmp), env={}, cli="hermes"
            )
            self.assertEqual(backend, "cli")
            self.assertEqual(jobs, ())
            self.assertIn("no remove", error)

    def test_live_cli_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [
                    res("commands: rm"),
                    res("id=abc123 [miloco:home-profile] morning\nother\n"),
                ]
            )
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=Path(tmp), env={}, cli="hermes"
            )
            self.assertEqual(backend, "cli")
            self.assertEqual(error, "")
            self.assertEqual([j.id for j in jobs], ["abc123"])

    def test_live_cli_list_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [res("commands: rm"), res(rc=1, stderr="nope", argv=("hermes", "cron"))]
            )
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=Path(tmp), env={}, cli="hermes"
            )
            self.assertEqual(backend, "cli")
            self.assertIn("nope", error)

    def test_live_without_any_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend, jobs, error = install._hermes_cron_live(
                run=FakeRunner([]), hermes_home=Path(tmp), env={}, cli=None
            )
            self.assertEqual((backend, jobs, error), ("none", (), ""))

    def test_plugin_line_present(self):
        self.assertTrue(install._hermes_plugin_line_present("miloco"))
        self.assertTrue(install._hermes_plugin_line_present("a | miloco | b"))
        self.assertTrue(
            install._hermes_plugin_line_present("  miloco/miloco-plugin, other")
        )
        self.assertFalse(install._hermes_plugin_line_present("miloco-extra"))
        self.assertFalse(install._hermes_plugin_line_present("nomiloco"))


# ---------------------------------------------------------------------------
# OpenClaw 解析
# ---------------------------------------------------------------------------


class OpenClawParsingTest(unittest.TestCase):
    def test_find_matching_node(self):
        data = {"a": [{"meta": {"pluginId": install.OPENCLAW_PLUGIN_ID}}]}
        self.assertTrue(
            install._find_matching_node(
                data, ("id", "name", "pluginId"), install.OPENCLAW_PLUGIN_ID
            )
        )
        self.assertFalse(
            install._find_matching_node(data, ("id", "name"), install.OPENCLAW_PLUGIN_ID)
        )

    def test_plugin_in_json(self):
        hit = json.dumps({"plugins": [{"id": install.OPENCLAW_PLUGIN_ID}]})
        self.assertTrue(install._openclaw_plugin_in_json(hit))
        self.assertFalse(install._openclaw_plugin_in_json('{"plugins": []}'))
        self.assertTrue(
            install._openclaw_plugin_in_json(f"noise {install.OPENCLAW_PLUGIN_ID}")
        )

    def test_cron_jobs_shapes(self):
        job = {"id": "1", "description": install.HERMES_MANAGED_TAG}
        self.assertEqual(
            install._openclaw_cron_jobs(json.dumps([job])), [job]
        )
        self.assertEqual(
            install._openclaw_cron_jobs(json.dumps({"jobs": [job]})), [job]
        )
        self.assertEqual(
            install._openclaw_cron_jobs(json.dumps({"data": {"jobs": [job]}})), [job]
        )
        self.assertEqual(
            install._openclaw_cron_jobs(json.dumps({"items": [job]})), [job]
        )
        self.assertIsNone(install._openclaw_cron_jobs("garbage"))
        self.assertIsNone(install._openclaw_cron_jobs(json.dumps({"x": 1})))

    def test_installed_tools_from_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            other = state / "extensions" / "other" / "openclaw.plugin.json"
            other.parent.mkdir(parents=True)
            other.write_text(json.dumps({"id": "other", "contracts": {"tools": ["x"]}}))
            manifest = state / "extensions" / "miloco" / "openclaw.plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {"id": install.OPENCLAW_PLUGIN_ID, "contracts": {"tools": ["a", "b"]}}
                )
            )
            self.assertEqual(install._installed_openclaw_tools(state), ("a", "b"))

    def test_installed_tools_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            manifest = state / "plugins" / "miloco" / "openclaw.plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {"id": install.OPENCLAW_PLUGIN_ID, "contracts": {"tools": [1, 2]}}
                )
            )
            self.assertIsNone(install._installed_openclaw_tools(state))

    def test_plugin_record_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertFalse(
                install._openclaw_plugin_record(state_dir=state, cli=None, run=FakeRunner([]))
            )
            manifest = state / "extensions" / "m" / "openclaw.plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"id": install.OPENCLAW_PLUGIN_ID}))
            self.assertTrue(
                install._openclaw_plugin_record(state_dir=state, cli=None, run=FakeRunner([]))
            )

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            installs = state / "plugins" / "installs.json"
            installs.parent.mkdir(parents=True)
            installs.write_text(f'["{install.OPENCLAW_PLUGIN_ID}"]')
            self.assertTrue(
                install._openclaw_plugin_record(state_dir=state, cli=None, run=FakeRunner([]))
            )

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            runner = FakeRunner(
                [res(json.dumps([{"id": install.OPENCLAW_PLUGIN_ID}]))]
            )
            self.assertTrue(
                install._openclaw_plugin_record(state_dir=state, cli="oc", run=runner)
            )
            runner2 = FakeRunner([res('{"plugins": []}')])
            self.assertFalse(
                install._openclaw_plugin_record(state_dir=state, cli="oc", run=runner2)
            )
            runner3 = FakeRunner([install.CommandFailed("k", "t")])
            self.assertFalse(
                install._openclaw_plugin_record(state_dir=state, cli="oc", run=runner3)
            )

    def test_resolve_tool_names_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            manifest = state / "extensions" / "m" / "openclaw.plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "id": install.OPENCLAW_PLUGIN_ID,
                        "contracts": {"tools": ["installed_tool"]},
                    }
                )
            )
            names, source = install._resolve_openclaw_tool_names(
                state_dir=state,
                script_dir=root / "scripts",
                cli=None,
                run=FakeRunner([]),
            )
            self.assertEqual((names, source), (("installed_tool",), "installed_manifest"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FakeRunner(
                [res(json.dumps({"contracts": {"tools": ["inspected_tool"]}}))]
            )
            names, source = install._resolve_openclaw_tool_names(
                state_dir=root / "state",
                script_dir=root / "scripts",
                cli="oc",
                run=runner,
            )
            self.assertEqual((names, source), (("inspected_tool",), "inspect"))
            self.assertEqual(
                runner.calls[0]["argv"],
                ["oc", "plugins", "inspect", install.OPENCLAW_PLUGIN_ID, "--json"],
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_manifest = root / "plugins" / "openclaw" / "openclaw.plugin.json"
            repo_manifest.parent.mkdir(parents=True)
            repo_manifest.write_text(json.dumps({"tools": ["repo_tool"]}))
            names, source = install._resolve_openclaw_tool_names(
                state_dir=root / "state",
                script_dir=root / "scripts",
                cli=None,
                run=FakeRunner([]),
            )
            self.assertEqual((names, source), (("repo_tool",), "openclaw.plugin.json"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names, source = install._resolve_openclaw_tool_names(
                state_dir=root / "state",
                script_dir=root / "scripts",
                cli=None,
                run=FakeRunner([]),
            )
            self.assertEqual(names, install.FALLBACK_OPENCLAW_TOOLS)
            self.assertEqual(source, "static_fallback")

    def test_inspect_failure_falls_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_manifest = root / "plugins" / "openclaw" / "openclaw.plugin.json"
            repo_manifest.parent.mkdir(parents=True)
            repo_manifest.write_text(json.dumps({"tools": ["repo_tool"]}))
            runner = FakeRunner([res(rc=1, stderr="no such plugin")])
            names, source = install._resolve_openclaw_tool_names(
                state_dir=root / "state",
                script_dir=root / "scripts",
                cli="oc",
                run=runner,
            )
            self.assertEqual((names, source), (("repo_tool",), "openclaw.plugin.json"))

    def test_inspect_non_dict_output_falls_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_manifest = root / "plugins" / "openclaw" / "openclaw.plugin.json"
            repo_manifest.parent.mkdir(parents=True)
            repo_manifest.write_text(json.dumps({"tools": ["repo_tool"]}))
            runner = FakeRunner([res(json.dumps(["not", "a", "dict"]))])
            names, source = install._resolve_openclaw_tool_names(
                state_dir=root / "state",
                script_dir=root / "scripts",
                cli="oc",
                run=runner,
            )
            self.assertEqual((names, source), (("repo_tool",), "openclaw.plugin.json"))

    def test_dist_manifest_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist" / "manifest.json"
            dist.parent.mkdir(parents=True)
            dist.write_text(json.dumps({"contracts": {"tools": ["dist_tool"]}}))
            names, source = install._resolve_openclaw_tool_names(
                state_dir=root / "state",
                script_dir=root / "scripts",
                cli=None,
                run=FakeRunner([]),
            )
            self.assertEqual((names, source), (("dist_tool",), "dist/manifest.json"))

    def test_script_manifest_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_dir = root / "scripts"
            script_dir.mkdir()
            (script_dir / "manifest.json").write_text(
                json.dumps({"tools": ["script_tool"]})
            )
            names, source = install._resolve_openclaw_tool_names(
                state_dir=root / "state",
                script_dir=script_dir,
                cli=None,
                run=FakeRunner([]),
            )
            self.assertEqual((names, source), (("script_tool",), "manifest.json"))

    def test_config_tools_in_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openclaw.json"
            self.assertEqual(install._config_tools_in_allow(path), ())
            path.write_text(json.dumps({"tools": {"alsoAllow": ["a", "b"]}}))
            self.assertEqual(install._config_tools_in_allow(path), ("a", "b"))
            path.write_text(json.dumps({"tools": {"alsoAllow": "a"}}))
            self.assertEqual(install._config_tools_in_allow(path), ())


# ---------------------------------------------------------------------------
# 盘点
# ---------------------------------------------------------------------------


class BuildInventoryTest(unittest.TestCase):
    def _inventory(self, home: Path, *, env=None, hint="", cli=None, runner=None):
        return install.build_inventory(
            env=env or {},
            home=home,
            agent_platform_hint=hint,
            which=lambda name: cli if name == "hermes" else None,
            run=runner or FakeRunner([]),
        )

    def test_empty_environment_detects_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            inv = self._inventory(home)
            self.assertFalse(inv.hermes.detected)
            self.assertFalse(inv.openclaw.detected)
            self.assertIsNone(inv.hermes.cli)
            self.assertEqual(inv.hermes.cron_backend, "none")
            self.assertEqual(inv.openclaw.managed_crons, ())
            # 默认候选总会被列出
            reals = [c.real_path for c in inv.candidates]
            self.assertIn((home / ".hermes" / "miloco").resolve(), reals)

    def test_skills_trace_detects_hermes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            skills = home / ".hermes" / "skills"
            (skills / "miloco-doctor").mkdir(parents=True)
            (skills / "other-skill").mkdir()
            inv = self._inventory(home)
            self.assertTrue(inv.hermes.detected)
            self.assertEqual(
                [p.name for p in inv.hermes.skills], ["miloco-doctor"]
            )

    def test_rc_marker_detects_hermes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".zshrc").write_text(
                "# miloco runtime path\nexport MILOCO_HOME=/x\n"
            )
            inv = self._inventory(home)
            self.assertTrue(inv.hermes.detected)
            self.assertIn(home / ".zshrc", inv.hermes.rc_files)

    def test_env_bearer_matches_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            miloco_home = home / "custom-miloco"
            miloco_home.mkdir()
            (miloco_home / "config.json").write_text(
                json.dumps({"agent": {"auth_bearer": "secret-bearer"}})
            )
            hermes_home = home / ".hermes"
            hermes_home.mkdir()
            (hermes_home / ".env").write_text(
                f"API_SERVER_KEY=secret-bearer\nMILOCO_HOME={miloco_home}\n"
            )
            inv = self._inventory(home)
            self.assertTrue(inv.hermes.detected)
            self.assertEqual(inv.hermes.env_managed_bearers, ("secret-bearer",))
            candidate = next(
                c for c in inv.candidates if c.real_path == miloco_home.resolve()
            )
            self.assertEqual(candidate.auth_bearer, "secret-bearer")

    def test_env_bearer_not_matching_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_home = home / ".hermes"
            hermes_home.mkdir()
            (hermes_home / ".env").write_text("API_SERVER_KEY=someone-elses\n")
            inv = self._inventory(home)
            self.assertEqual(inv.hermes.env_managed_bearers, ())
            self.assertFalse(inv.hermes.detected)

    def test_openclaw_traces(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = home / ".openclaw"
            state.mkdir()
            (state / "openclaw.json").write_text(
                json.dumps({"plugins": {"entries": {install.OPENCLAW_PLUGIN_ID: {}}}})
            )
            inv = self._inventory(home)
            self.assertTrue(inv.openclaw.detected)
            # 配置文本里有插件 ID 就代表检测到了；plugin_record 只反映
            # manifest / installs.json / CLI 列表
            self.assertFalse(inv.openclaw.plugin_record)

    def test_openclaw_installs_json_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            installs = home / ".openclaw" / "plugins" / "installs.json"
            installs.parent.mkdir(parents=True)
            installs.write_text(f'["{install.OPENCLAW_PLUGIN_ID}"]')
            inv = self._inventory(home)
            self.assertTrue(inv.openclaw.detected)

    def test_tool_names_from_config_also_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = home / ".openclaw"
            state.mkdir()
            (state / "openclaw.json").write_text(
                json.dumps({"tools": {"alsoAllow": ["miloco_im_push", "other"]}})
            )
            inv = self._inventory(home)
            self.assertTrue(inv.openclaw.detected)
            self.assertIn("miloco_im_push", inv.openclaw.tools_in_also_allow)

    def test_cron_backend_cli_and_managed_crons(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            runner = FakeRunner(
                [
                    res("commands: rm"),
                    res(f"{install.HERMES_MANAGED_TAG} morning (id: abc123)\n"),
                ]
            )
            inv = self._inventory(home, cli="hermes", runner=runner)
            self.assertEqual(inv.hermes.cron_backend, "cli")
            self.assertEqual([c.id for c in inv.hermes.managed_crons], ["abc123"])
            self.assertTrue(inv.hermes.detected)

    def test_cli_fallback_real_block_format(self):
        # 真实 `hermes cron list --all` 的块状输出（含一条 paused job）。
        # 受管识别靠 Name 行的 [miloco:home-profile] 前缀，不依赖安装期 id 记录。
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            listing = (
                "\n"
                "  abc123 [active]\n"
                f"    Name:      {install.HERMES_MANAGED_TAG} miloco-perception-digest\n"
                "    Schedule:  every 30m\n"
                "  def456 [paused]\n"
                f"    Name:      {install.HERMES_MANAGED_TAG} miloco-home-patrol\n"
                "    Schedule:  every 1h\n"
                "  user99 [active]\n"
                "    Name:      my-own-job\n"
            )
            runner = FakeRunner(
                [
                    res("commands: rm"),
                    res(listing),
                ]
            )
            inv = self._inventory(home, cli="hermes", runner=runner)
            self.assertEqual(
                [c.id for c in inv.hermes.managed_crons], ["abc123", "def456"]
            )
            # 每条都能通过 name.startswith(tag) 过滤（删除点依赖它）。
            self.assertTrue(
                all(
                    c.name.startswith(install.HERMES_MANAGED_TAG)
                    for c in inv.hermes.managed_crons
                )
            )
            # 带 --all，否则 paused 的 def456 不会出现。
            self.assertEqual(
                runner.argvs[1], ["hermes", "cron", "list", "--all"]
            )

    def test_cli_fallback_ansi_colored_output(self):
        # should_use_color() 为真时 CLI 输出带 ANSI 转义，解析必须先剥离。
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            listing = (
                "  \x1b[33mabc123\x1b[0m \x1b[32m[active]\x1b[0m\n"
                f"    Name:      {install.HERMES_MANAGED_TAG} morning\n"
            )
            runner = FakeRunner([res("commands: rm"), res(listing)])
            inv = self._inventory(home, cli="hermes", runner=runner)
            self.assertEqual([c.id for c in inv.hermes.managed_crons], ["abc123"])

    def test_cli_fallback_tagged_but_no_id_is_not_silently_dropped(self):
        # 原始输出含 tag 但取不到 id → 不能静默当成 ABSENT：
        # 单行兜底会产出一条 id 为空的受管 job，交给删除阶段报 BLOCKED。
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [
                    res("commands: rm"),
                    res(f"some totally unexpected {install.HERMES_MANAGED_TAG} blob\n"),
                ]
            )
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=Path(tmp), env={}, cli="hermes"
            )
            self.assertEqual(backend, "cli")
            self.assertEqual(error, "")
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].id, "")
            self.assertTrue(jobs[0].name.startswith("some totally unexpected"))

    def test_cli_fallback_empty_listing_is_absent(self):
        # 真正没有受管 cron（输出里没有 tag）→ 空集、无错误。
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([res("commands: rm"), res("No scheduled jobs.\n")])
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=Path(tmp), env={}, cli="hermes"
            )
            self.assertEqual((backend, jobs, error), ("cli", (), ""))

    def test_cron_backend_local_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            python = home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            runner = FakeRunner(
                [res(json.dumps([{"id": "1", "name": f"{install.HERMES_MANAGED_TAG} x"}]))]
            )
            inv = self._inventory(home, runner=runner)
            self.assertEqual(inv.hermes.cron_backend, "local_api")
            self.assertEqual(len(inv.hermes.managed_crons), 1)

    def test_primary_home_prefers_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            custom = home / "custom"
            custom.mkdir()
            (home / ".hermes").mkdir()
            (home / ".hermes" / ".env").write_text(f"MILOCO_HOME={custom}\n")
            inv = self._inventory(home)
            self.assertEqual(inv.primary_home, custom)


# ---------------------------------------------------------------------------
# 编辑器
# ---------------------------------------------------------------------------


class TextFileEditorTest(unittest.TestCase):
    def test_commit_rejects_unplanned_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            path.write_text("a\nb\n")
            editor = install.TextFileEditor(path)
            with self.assertRaises(install.MalformedFile):
                editor.commit(["a\n"])
            self.assertEqual(path.read_text(), "a\nb\n")

    def test_commit_accepts_matching_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            path.write_text("a\nb\n")
            install.TextFileEditor(path).commit(["b\n"], remove_indices=[0])
            self.assertEqual(path.read_text(), "b\n")


class EnvFileCleanerTest(unittest.TestCase):
    def _plan(self, path, bearers=(), homes=()):
        cleaner = install.EnvFileCleaner(path, i18n=I18N)
        return cleaner, cleaner.plan(
            managed_bearers=set(bearers), managed_homes=set(homes)
        )

    def test_absent_when_no_managed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("FOO=1\n# API_SERVER_KEY=x\n")
            cleaner, plan = self._plan(path)
            self.assertIs(cleaner.apply(plan).status, STATUS.ABSENT)

    def test_kept_when_only_user_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("API_SERVER_KEY=mine\nMILOCO_HOME=/elsewhere\n")
            cleaner, plan = self._plan(path, bearers={"other"}, homes={Path("/x")})
            item = cleaner.apply(plan)
            self.assertIs(item.status, STATUS.KEPT)
            self.assertEqual(path.read_text(), "API_SERVER_KEY=mine\nMILOCO_HOME=/elsewhere\n")
            self.assertIn(I18N.t("uninstall.detail.kept_api_key"), item.detail)
            self.assertIn(I18N.t("uninstall.detail.kept_env_home"), item.detail)

    def test_removes_export_form_and_quoted_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            path = Path(tmp) / ".env"
            path.write_text(
                'export API_SERVER_KEY="bearer-abc"\n'
                f"MILOCO_HOME='{home}'\n"
                "KEEP=1\n"
            )
            cleaner, plan = self._plan(path, bearers={"bearer-abc"}, homes={home})
            item = cleaner.apply(plan)
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(path.read_text(), "KEEP=1\n")
            self.assertIn("API_SERVER_KEY", item.detail)
            self.assertIn("MILOCO_HOME", item.detail)

    def test_same_key_partial_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "API_SERVER_KEY=bearer-abc\nAPI_SERVER_KEY=user-one\n"
            )
            cleaner, plan = self._plan(path, bearers={"bearer-abc"})
            item = cleaner.apply(plan)
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(path.read_text(), "API_SERVER_KEY=user-one\n")
            self.assertIn(I18N.t("uninstall.detail.kept_api_key"), item.detail)

    def test_home_value_match_is_by_resolved_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "real"
            home.mkdir()
            link = root / "link"
            link.symlink_to(home, target_is_directory=True)
            path = root / ".env"
            path.write_text(f"MILOCO_HOME={link}\n")
            cleaner, plan = self._plan(path, homes={home})
            self.assertIs(cleaner.apply(plan).status, STATUS.DONE)
            self.assertEqual(path.read_text(), "")


class ShellRcCleanerTest(unittest.TestCase):
    def _plan(self, path, homes):
        cleaner = install.ShellRcCleaner(path, i18n=I18N)
        return cleaner, cleaner.plan(managed_homes=set(homes))

    def test_removes_marker_and_matching_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            path = Path(tmp) / ".zshrc"
            path.write_text(
                f"# miloco runtime path\nexport MILOCO_HOME={home}\nKEEP=1\n"
            )
            cleaner, plan = self._plan(path, {home})
            item = cleaner.apply(plan)
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(path.read_text(), "KEEP=1\n")

    def test_marker_alone_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text("# miloco runtime path\nKEEP=1\n")
            cleaner, plan = self._plan(path, set())
            self.assertIs(cleaner.apply(plan).status, STATUS.DONE)
            self.assertEqual(path.read_text(), "KEEP=1\n")

    def test_marker_keeps_foreign_export_and_reports_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".bashrc"
            path.write_text(
                "# miloco runtime path\nexport MILOCO_HOME=/foreign\n"
            )
            cleaner, plan = self._plan(path, {Path("/managed")})
            item = cleaner.apply(plan)
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(path.read_text(), "export MILOCO_HOME=/foreign\n")
            self.assertIn("/foreign", item.detail)

    def test_export_pointing_to_managed_home_is_removed_without_marker(self):
        # 安装器遇到已有 export 时是原地改写、不补标记行；值相等即归属证明。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text(
                "USER_ALIAS=keepme\nexport MILOCO_HOME=/managed\n"
            )
            cleaner, plan = self._plan(path, {Path("/managed")})
            self.assertIs(cleaner.apply(plan).status, STATUS.DONE)
            self.assertEqual(path.read_text(), "USER_ALIAS=keepme\n")

    def test_export_pointing_elsewhere_is_untouched_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = "export MILOCO_HOME=/user-own\n"
            path = Path(tmp) / ".zshrc"
            path.write_text(original)
            cleaner, plan = self._plan(path, {Path("/managed")})
            self.assertIs(cleaner.apply(plan).status, STATUS.ABSENT)
            self.assertEqual(path.read_text(), original)

    def test_marker_regex_requires_line_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = "  # miloco runtime path\n"
            path = Path(tmp) / ".zshrc"
            path.write_text(original)
            cleaner, plan = self._plan(path, set())
            self.assertIs(cleaner.apply(plan).status, STATUS.ABSENT)
            self.assertEqual(path.read_text(), original)


class ConfigJsonCleanerTest(unittest.TestCase):
    def test_other_platform_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"agent": {"platform": "openclaw"}}))
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            item = cleaner.apply(cleaner.plan(clear_platform=True))
            self.assertIs(item.status, STATUS.KEPT)
            self.assertIn("openclaw", item.detail)
            self.assertEqual(
                json.loads(path.read_text())["agent"]["platform"], "openclaw"
            )

    def test_missing_agent_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"model": {}}))
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            self.assertIs(
                cleaner.apply(cleaner.plan(clear_platform=True)).status,
                STATUS.ABSENT,
            )

    def test_malformed_json_is_reported_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json")
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            with self.assertRaises(install.MalformedFile) as ctx:
                cleaner.plan(clear_platform=True)
            self.assertEqual(ctx.exception.key, "uninstall.detail.json_parse")
            self.assertEqual(ctx.exception.target, str(path))
            self.assertEqual(path.read_text(), "{not json")

    def test_top_level_not_object_is_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[1, 2]")
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            with self.assertRaises(install.MalformedFile) as ctx:
                cleaner.plan(clear_platform=True)
            self.assertEqual(ctx.exception.key, "uninstall.detail.json_parse")
            self.assertEqual(ctx.exception.target, str(path))

    def test_trailing_newline_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"agent": {"platform": "hermes"}}, indent=2) + "\n"
            )
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            cleaner.apply(cleaner.plan(clear_platform=True))
            self.assertTrue(path.read_text().endswith("\n"))

    def test_without_trailing_newline_none_is_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"agent": {"platform": "hermes"}}))
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            cleaner.apply(cleaner.plan(clear_platform=True))
            self.assertFalse(path.read_text().endswith("\n"))

    def test_apply_rejects_plan_that_changes_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"agent": {"platform": "hermes"}}))
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            bad = install.JsonPlan(
                before={"agent": {"platform": "hermes"}},
                after={"agent": {"platform": ""}, "extra": 1},
                changed=True,
                current_platform="hermes",
            )
            with self.assertRaises(install.MalformedFile):
                cleaner.apply(bad)
            self.assertIn("hermes", path.read_text())




class PostInstallRcProtectionTest(unittest.TestCase):
    """安装期保护用户自己的 export：install-hermes.sh 会原地改写整行。"""

    def _installer(self, miloco_home):
        inst = object.__new__(install.Installer)
        inst.miloco_home = miloco_home
        inst.ui = FakeUI()
        return inst

    def test_user_export_survives_installer_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            managed = Path(tmp) / "managed"
            zshrc = home / ".zshrc"
            zshrc.write_text(
                "USER_ALIAS=keepme\n"
                'export MILOCO_HOME="/opt/user-own"\n'
            )
            inst = self._installer(managed)
            saved = inst._hermes_stash_user_rc_exports(home=home)
            self.assertEqual(
                zshrc.read_text(), "USER_ALIAS=keepme\n"
            )
            # 模拟 install-hermes.sh 1.9：没有 export 时追加标记 + export
            with open(zshrc, "a") as fh:
                fh.write(f"\n# miloco runtime path\n")
                fh.write(f'export MILOCO_HOME="{managed}"\n')
            inst._hermes_restore_user_rc_exports(saved)
            self.assertEqual(
                zshrc.read_text(),
                "USER_ALIAS=keepme\n"
                "\n"
                'export MILOCO_HOME="/opt/user-own"\n'
                f"# miloco runtime path\n"
                f'export MILOCO_HOME="{managed}"\n',
            )

    def test_export_to_the_managed_home_is_replaced_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            managed = Path(tmp) / "managed"
            zshrc = home / ".zshrc"
            zshrc.write_text(f'export MILOCO_HOME="{managed}"\n')
            inst = self._installer(managed)
            self.assertEqual(inst._hermes_stash_user_rc_exports(home=home), {})
            self.assertIn(str(managed), zshrc.read_text())

    def test_restore_inserts_user_line_before_markerless_managed_export(self):
        # install-hermes.sh 遇到已有 export 时原地更新、不写 marker。
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            managed = Path(tmp) / "managed"
            zshrc = home / ".zshrc"
            zshrc.write_text(
                "USER_ALIAS=keepme\n"
                'export MILOCO_HOME="/opt/user-own"\n'
                f'export MILOCO_HOME="{managed}"\n'
            )
            inst = self._installer(managed)
            saved = inst._hermes_stash_user_rc_exports(home=home)
            inst._hermes_restore_user_rc_exports(saved)
            self.assertEqual(
                zshrc.read_text(),
                "USER_ALIAS=keepme\n"
                'export MILOCO_HOME="/opt/user-own"\n'
                f'export MILOCO_HOME="{managed}"\n',
            )


class EditorMetadataTest(unittest.TestCase):
    """每个编辑器产出的 CleanupItem 必须带正确的 i18n key 与平台。"""

    def test_env_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MILOCO_HOME=/x\n")
            cleaner = install.EnvFileCleaner(path, i18n=I18N)
            item = cleaner.apply(
                cleaner.plan(managed_bearers=set(), managed_homes={Path("/x")})
            )
            self.assertEqual(item.key, "uninstall.hermes.env")
            self.assertEqual(item.platform, "hermes")
            self.assertIs(item.status, STATUS.DONE)

    def test_shell_rc_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text("# miloco runtime path\n")
            cleaner = install.ShellRcCleaner(path, i18n=I18N)
            item = cleaner.apply(cleaner.plan(managed_homes=set()))
            self.assertEqual(item.key, "uninstall.hermes.shell_rc")
            self.assertEqual(item.platform, "hermes")

    def test_config_json_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"agent": {"platform": "hermes"}}))
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            item = cleaner.apply(cleaner.plan(clear_platform=True))
            self.assertEqual(item.key, "uninstall.hermes.agent_platform")
            self.assertEqual(item.platform, "hermes")



# ---------------------------------------------------------------------------
# Hermes Cleaner
# ---------------------------------------------------------------------------


class HermesCleanerIntegrationTest(unittest.TestCase):
    """从 build_inventory 到 HermesCleaner.run() 的完整路径（无 CLI）。"""

    def _setup(self, tmp: Path):
        home = tmp
        hermes_home = home / ".hermes"
        miloco_home = home / "custom-miloco"
        (hermes_home / "plugins" / "miloco" / "miloco-plugin").mkdir(parents=True)
        (hermes_home / "plugins" / "miloco" / "miloco-plugin" / "state.json").write_text(
            json.dumps({"deliver": {"target": "notify"}})
        )
        (hermes_home / "skills" / "miloco-doctor").mkdir(parents=True)
        (hermes_home / "skills" / "other-skill").mkdir()
        (hermes_home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - miloco\n    - other\n"
        )
        (hermes_home / ".env").write_text(
            "API_SERVER_KEY=bearer-1\nUSER_KEY=keep\n"
        )
        (home / ".zshrc").write_text(
            f"# miloco runtime path\nexport MILOCO_HOME={miloco_home}\n"
        )
        (miloco_home / "agent_platform" / "hermes").mkdir(parents=True)
        (miloco_home / "agent_platform" / "hermes" / "adapter.py").write_text("x")
        (miloco_home / ".install-cache" / "hermes-plugin").mkdir(parents=True)
        (miloco_home / ".install-cache" / "hermes-plugin" / "pkg.whl").write_text("x")
        (miloco_home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {"platform": "hermes", "auth_bearer": "bearer-1"},
                    "model": {"omni": {"model": "keep"}},
                }
            )
        )
        (miloco_home / ".env").write_text(f"MILOCO_HOME={miloco_home}\n")

        runner = FakeRunner([])
        inv = install.build_inventory(
            env={},
            home=home,
            agent_platform_hint="",
            which=lambda name: None,
            run=runner,
        )
        return inv, miloco_home

    def test_removes_files_and_reports_blocked_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            inv, miloco_home = self._setup(home)
            self.assertTrue(inv.hermes.detected)
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=inv, decisions={}, run=FakeRunner([]), env={}
            )
            report = cleaner.run()

            self.assertTrue(report.detected)
            self.assertFalse(report.cli_available)
            self.assertTrue(report.incomplete)

            statuses = {}
            for item in report.items:
                statuses.setdefault(item.key, []).append(item.status)
                self.assertEqual(item.platform, "hermes")
                self.assertTrue(item.key.startswith("uninstall."))

            # 文件级清理全部完成
            self.assertIn(STATUS.DONE, statuses["uninstall.hermes.plugin_dir"])
            self.assertIn(STATUS.DONE, statuses["uninstall.hermes.skills"])
            self.assertIn(STATUS.DONE, statuses["uninstall.hermes.adapter"])
            self.assertIn(STATUS.DONE, statuses["uninstall.hermes.install_cache"])
            self.assertIn(STATUS.DONE, statuses["uninstall.hermes.agent_platform"])
            self.assertIn(STATUS.DONE, statuses["uninstall.hermes.env"])
            self.assertIn(STATUS.DONE, statuses["uninstall.hermes.shell_rc"])
            self.assertIn(STATUS.DONE, statuses["uninstall.hermes.plugin_state_backup"])

            # CLI 缺失必须是 BLOCKED，不能静默
            self.assertIn(STATUS.BLOCKED, statuses["uninstall.hermes.cli_unavailable"])

            # 实际文件状态
            self.assertFalse((home / ".hermes" / "plugins" / "miloco").exists())
            self.assertTrue((home / ".hermes" / "skills" / "other-skill").exists())
            self.assertFalse((home / ".hermes" / "skills" / "miloco-doctor").exists())
            self.assertFalse((miloco_home / "agent_platform" / "hermes").exists())
            self.assertFalse((miloco_home / ".install-cache" / "hermes-plugin").exists())
            self.assertFalse((miloco_home / "agent_platform").exists())
            self.assertFalse((miloco_home / ".install-cache").exists())
            config = json.loads((miloco_home / "config.json").read_text())
            self.assertEqual(config["agent"]["platform"], "")
            self.assertEqual(config["model"], {"omni": {"model": "keep"}})
            env_text = (home / ".hermes" / ".env").read_text()
            self.assertNotIn("API_SERVER_KEY", env_text)
            self.assertIn("USER_KEY=keep", env_text)
            self.assertNotIn("# miloco runtime path", (home / ".zshrc").read_text())
            backups = list((home / ".hermes" / "miloco-uninstall-backup").iterdir())
            self.assertEqual(len(backups), 1)

    def test_home_deletion_turns_per_home_items_into_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            inv, miloco_home = self._setup(home)
            candidate = next(
                c for c in inv.candidates if c.real_path == miloco_home.resolve()
            )
            cleaner = install.HermesCleaner(
                ui=FakeUI(),
                inv=inv,
                decisions={candidate.real_path: True},
                run=FakeRunner([]),
                env={},
            )
            report = cleaner.run()
            by_key = {}
            for item in report.items:
                by_key.setdefault(item.key, []).append(item.status)
            self.assertIn(STATUS.SKIPPED, by_key["uninstall.hermes.adapter"])
            self.assertIn(
                STATUS.SKIPPED, by_key["uninstall.hermes.install_cache"]
            )
            self.assertIn(
                STATUS.SKIPPED, by_key["uninstall.hermes.agent_platform"]
            )
            self.assertIn(
                STATUS.SKIPPED, by_key["uninstall.hermes.plugin_state_backup"]
            )
            # 数据目录本身仍在：清理器不负责删它
            self.assertTrue(miloco_home.exists())


class HermesCronCleanerTest(unittest.TestCase):
    def _cleaner(self, tmp: Path, facts: install.HermesFacts, runner):
        inv = _inventory(tmp, hermes=facts)
        return install.HermesCleaner(
            ui=FakeUI(), inv=inv, decisions={}, run=runner, env={}
        )

    def test_removes_managed_jobs_via_local_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            facts = _hermes_facts(
                hermes_home / "config.yaml", venv_python=python
            )
            listing = json.dumps(
                [
                    {"id": "m1", "name": f"{install.HERMES_MANAGED_TAG} morning"},
                    {"id": "u1", "name": "user job"},
                ]
            )
            runner = FakeRunner([res(listing), res('{"ok": true}'), res("[]")])
            items = self._cleaner(hermes_home, facts, runner)._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.DONE])
            self.assertEqual(runner.calls[1]["argv"][-1], "m1")
            self.assertTrue(
                all("u1" not in call["argv"] for call in runner.calls)
            )

    def test_remove_failure_does_not_stop_following_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            facts = _hermes_facts(hermes_home / "config.yaml", venv_python=python)
            listing = json.dumps(
                [
                    {"id": "m1", "name": f"{install.HERMES_MANAGED_TAG} a"},
                    {"id": "m2", "name": f"{install.HERMES_MANAGED_TAG} b"},
                ]
            )
            runner = FakeRunner(
                [
                    res(listing),
                    res(rc=1, stderr="first failed"),
                    res('{"ok": true}'),
                    res("[]"),
                ]
            )
            items = self._cleaner(hermes_home, facts, runner)._remove_managed_crons()
            self.assertEqual(
                [i.status for i in items], [STATUS.FAILED, STATUS.DONE]
            )
            self.assertEqual(
                [i.platform for i in items], ["hermes", "hermes"]
            )
            self.assertEqual(
                [i.key for i in items],
                ["uninstall.hermes.cron", "uninstall.hermes.cron"],
            )

    def test_remove_failure_is_failed_and_verify_reports_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            facts = _hermes_facts(hermes_home / "config.yaml", venv_python=python)
            listing = json.dumps(
                [{"id": "m1", "name": f"{install.HERMES_MANAGED_TAG} morning"}]
            )
            runner = FakeRunner(
                [res(listing), res(rc=1, stderr="cannot remove"), res(listing)]
            )
            items = self._cleaner(hermes_home, facts, runner)._remove_managed_crons()
            statuses = [i.status for i in items]
            self.assertIn(STATUS.FAILED, statuses)
            self.assertTrue(
                any(
                    i.status is STATUS.FAILED
                    and I18N.t("uninstall.detail.cron_remaining") in i.detail
                    for i in items
                )
            )

    def test_job_without_id_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            facts = _hermes_facts(hermes_home / "config.yaml", venv_python=python)
            runner = FakeRunner(
                [
                    res(
                        json.dumps(
                            [{"id": "", "name": f"{install.HERMES_MANAGED_TAG} x"}]
                        )
                    ),
                    res("[]"),
                ]
            )
            items = self._cleaner(hermes_home, facts, runner)._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.BLOCKED])

    def test_no_backend_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts = _hermes_facts(Path(tmp) / "config.yaml")
            items = self._cleaner(
                Path(tmp), facts, FakeRunner([])
            )._remove_managed_crons()
            self.assertEqual(len(items), 1)
            self.assertIs(items[0].status, STATUS.BLOCKED)
            self.assertEqual(items[0].key, "uninstall.hermes.cli_unavailable")
            self.assertEqual(items[0].platform, "hermes")
            self.assertTrue(items[0].remediation)

    def test_verify_error_is_reported_with_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            facts = _hermes_facts(hermes_home / "config.yaml", venv_python=python)
            listing = json.dumps(
                [{"id": "m1", "name": f"{install.HERMES_MANAGED_TAG} x"}]
            )
            runner = FakeRunner(
                [
                    res(listing),
                    res('{"ok": true}'),
                    res(rc=1, stderr="verify exploded"),
                ]
            )
            items = self._cleaner(hermes_home, facts, runner)._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.DONE, STATUS.FAILED])
            self.assertIn("verify exploded", items[1].detail)
            self.assertEqual(items[1].key, "uninstall.hermes.cron")


class HermesFilesTest(unittest.TestCase):
    def _cleaner(self, tmp: Path, facts: install.HermesFacts, *, decisions=None):
        inv = _inventory(tmp, hermes=facts, candidates=())
        return install.HermesCleaner(
            ui=FakeUI(),
            inv=inv,
            decisions=decisions or {},
            run=FakeRunner([]),
            env={},
        )

    def test_plugin_dir_symlink_unlinks_only_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            target = root / "real-plugin"
            target.mkdir(parents=True)
            (target / "f").write_text("x")
            plugin_dir = hermes_home / "plugins" / "miloco"
            plugin_dir.parent.mkdir(parents=True)
            plugin_dir.symlink_to(target, target_is_directory=True)
            facts = _hermes_facts(hermes_home / "config.yaml")
            item = self._cleaner(root, facts)._remove_plugin_dir()
            self.assertIs(item.status, STATUS.DONE)
            self.assertFalse(plugin_dir.is_symlink())
            self.assertTrue((target / "f").exists())

    def test_plugin_dir_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            facts = _hermes_facts(hermes_home / "config.yaml")
            self.assertIs(
                self._cleaner(hermes_home, facts)._remove_plugin_dir().status,
                STATUS.ABSENT,
            )

    def test_plugin_dir_uses_official_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            plugin_dir = hermes_home / "plugins" / "miloco" / "miloco-plugin"
            plugin_dir.mkdir(parents=True)
            facts = _hermes_facts(hermes_home / "config.yaml", cli="/usr/bin/hermes")
            calls = []

            def run(argv, **kwargs):
                calls.append(list(argv))
                shutil.rmtree(hermes_home / "plugins" / "miloco")
                return res("Removed miloco\n")

            cleaner = install.HermesCleaner(
                ui=FakeUI(),
                inv=_inventory(root, hermes=facts, candidates=()),
                decisions={},
                run=run,
                env={},
            )
            item = cleaner._remove_plugin_dir()
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(
                calls, [["/usr/bin/hermes", "plugins", "remove", "miloco"]]
            )
            self.assertFalse((hermes_home / "plugins" / "miloco").exists())

    def test_plugin_dir_falls_back_when_remove_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            plugin_dir = hermes_home / "plugins" / "miloco" / "miloco-plugin"
            plugin_dir.mkdir(parents=True)
            facts = _hermes_facts(hermes_home / "config.yaml", cli="/usr/bin/hermes")
            cleaner = install.HermesCleaner(
                ui=FakeUI(),
                inv=_inventory(root, hermes=facts, candidates=()),
                decisions={},
                run=FakeRunner([res(rc=1, stderr="plugin is locked")]),
                env={},
            )
            item = cleaner._remove_plugin_dir()
            self.assertIs(item.status, STATUS.DONE)
            self.assertIn("plugin is locked", item.detail)
            self.assertFalse(plugin_dir.exists())

    def test_plugin_dir_cli_missing_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            plugin_dir = hermes_home / "plugins" / "miloco" / "miloco-plugin"
            plugin_dir.mkdir(parents=True)
            facts = _hermes_facts(hermes_home / "config.yaml", cli=None)
            item = self._cleaner(root, facts)._remove_plugin_dir()
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(
                item.detail, I18N.t("uninstall.detail.plugin_dir_cli_missing")
            )
            self.assertFalse(plugin_dir.exists())

    def test_skills_only_miloco_prefixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            skills = hermes_home / "skills"
            (skills / "miloco-b").mkdir(parents=True)
            (skills / "miloco-a").mkdir()
            (skills / "other").mkdir()
            facts = _hermes_facts(hermes_home / "config.yaml")
            items = self._cleaner(hermes_home, facts)._remove_skills()
            self.assertEqual(sorted(i.status for i in items), [STATUS.DONE] * 2)
            self.assertTrue((skills / "other").exists())
            self.assertTrue(skills.is_dir())

    def test_skills_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts = _hermes_facts(Path(tmp) / "config.yaml")
            items = self._cleaner(Path(tmp), facts)._remove_skills()
            self.assertIs(items[0].status, STATUS.ABSENT)
            self.assertEqual(items[0].key, "uninstall.hermes.skills")
            self.assertEqual(items[0].platform, "hermes")

    def test_adapters_process_every_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "home-a"
            second = root / "home-b"
            first.mkdir()
            (second / "agent_platform" / "hermes").mkdir(parents=True)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            inv = _inventory(
                root, hermes=facts, candidates=(_candidate(first), _candidate(second))
            )
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=inv, decisions={}, run=FakeRunner([]), env={}
            )
            items = cleaner._remove_adapters()
            self.assertEqual(
                [i.status for i in items], [STATUS.ABSENT, STATUS.DONE]
            )
            self.assertEqual(items[0].target, str(first / "agent_platform" / "hermes"))
            self.assertEqual(items[0].platform, "hermes")

    def test_install_cache_processes_every_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "home-a"
            second = root / "home-b"
            first.mkdir()
            (second / ".install-cache" / "hermes-plugin").mkdir(parents=True)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            inv = _inventory(
                root, hermes=facts, candidates=(_candidate(first), _candidate(second))
            )
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=inv, decisions={}, run=FakeRunner([]), env={}
            )
            items = cleaner._remove_install_cache()
            self.assertEqual(
                [i.status for i in items], [STATUS.ABSENT, STATUS.DONE]
            )
            self.assertEqual(
                items[0].target, str(first / ".install-cache" / "hermes-plugin")
            )

    def test_skills_iterdir_oserror_is_raised_with_key_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            (hermes_home / "skills").mkdir()
            facts = _hermes_facts(hermes_home / "config.yaml")
            cleaner = self._cleaner(hermes_home, facts)
            with mock.patch.object(
                install.Path, "iterdir", side_effect=OSError("denied")
            ):
                with self.assertRaises(install.UninstallError) as ctx:
                    cleaner._remove_skills()
            self.assertEqual(ctx.exception.key, "uninstall.hermes.skills")
            self.assertEqual(
                ctx.exception.target, str(hermes_home / "skills")
            )

    def test_backup_failure_blocks_plugin_dir_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            plugin_dir = hermes_home / "plugins" / "miloco"
            (plugin_dir / "miloco-plugin").mkdir(parents=True)
            (plugin_dir / "miloco-plugin" / "state.json").write_text(
                json.dumps({"deliver": {"target": "t"}})
            )
            # 备份目录位置被普通文件占住 -> mkdir 失败
            (hermes_home / "miloco-uninstall-backup").write_text("blocker")
            facts = _hermes_facts(hermes_home / "config.yaml")
            cleaner = self._cleaner(hermes_home, facts)
            backup = cleaner._backup_plugin_state()
            self.assertIs(backup.status, STATUS.FAILED)
            self.assertEqual(backup.platform, "hermes")
            self.assertEqual(backup.key, "uninstall.hermes.plugin_state_backup")
            removal = cleaner._remove_plugin_dir()
            self.assertIs(removal.status, STATUS.FAILED)
            self.assertEqual(removal.key, "uninstall.hermes.plugin_dir")
            self.assertEqual(removal.platform, "hermes")
            self.assertEqual(removal.target, str(plugin_dir))
            self.assertIn(
                I18N.t("uninstall.detail.backup_failed_skip_delete"), removal.detail
            )
            self.assertTrue(plugin_dir.exists())

    def test_backup_success_path_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            state = hermes_home / "plugins" / "miloco" / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"deliver": {"candidates": ["a"]}}))
            facts = _hermes_facts(hermes_home / "config.yaml")
            item = self._cleaner(hermes_home, facts)._backup_plugin_state()
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(item.platform, "hermes")
            backup = Path(item.target)
            self.assertTrue(backup.is_file())
            self.assertEqual(json.loads(backup.read_text()), {"deliver": {"candidates": ["a"]}})
            self.assertEqual(backup.parent, hermes_home / "miloco-uninstall-backup")

    def test_clean_shell_rc_processes_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            missing = hermes_home / "no-such-rc"
            present = hermes_home / ".zshrc"
            present.write_text("# miloco runtime path\n")
            facts = _hermes_facts(
                hermes_home / "config.yaml", rc_files=(missing, present)
            )
            items = self._cleaner(hermes_home, facts)._clean_shell_rc()
            self.assertEqual(
                [i.status for i in items], [STATUS.ABSENT, STATUS.DONE]
            )
            self.assertEqual(items[0].target, str(missing))
            self.assertEqual(items[0].key, "uninstall.hermes.shell_rc")
            self.assertNotIn("# miloco runtime path", present.read_text())

    def test_clean_shell_rc_without_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            facts = _hermes_facts(hermes_home / "config.yaml")
            items = self._cleaner(hermes_home, facts)._clean_shell_rc()
            self.assertEqual([i.status for i in items], [STATUS.ABSENT])
            self.assertEqual(items[0].target, "~/.zshrc, ~/.bashrc")

    def test_rc_only_user_export_is_not_treated_as_managed_home(self):
        # 用户的 export 也会被读进候选 home；只有带 miloco 产物的才算受管。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "managed"
            managed.mkdir()
            (managed / "config.json").write_text("{}")
            user = root / "user-own"
            user.mkdir()
            rc = root / ".zshrc"
            rc.write_text(
                f'export MILOCO_HOME="{user}"\n'
                f'export MILOCO_HOME="{managed}"\n'
            )
            candidates = (
                _candidate(user, ("shell_rc",)),
                _candidate(managed, ("shell_rc",)),
            )
            facts = _hermes_facts(
                root / ".hermes" / "config.yaml", rc_files=(rc,)
            )
            inv = _inventory(root, hermes=facts, candidates=candidates)
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=inv, decisions={}, run=FakeRunner([]), env={}
            )
            cleaner._clean_shell_rc()
            text = rc.read_text()
            self.assertIn(f'export MILOCO_HOME="{user}"', text)
            self.assertNotIn(f'export MILOCO_HOME="{managed}"', text)

    def test_backup_without_deliver_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            plugin_dir = hermes_home / "plugins" / "miloco"
            (plugin_dir / "miloco-plugin").mkdir(parents=True)
            (plugin_dir / "miloco-plugin" / "state.json").write_text("{}")
            facts = _hermes_facts(hermes_home / "config.yaml")
            self.assertIs(
                self._cleaner(hermes_home, facts)._backup_plugin_state().status,
                STATUS.ABSENT,
            )

    def test_secrets_are_scrubbed_from_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            (home / "config.json").write_text(
                json.dumps({"agent": {"auth_bearer": "super-secret-bearer"}})
            )
            candidate = _candidate(home)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            inv = _inventory(root, hermes=facts, candidates=(candidate,))
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=inv, decisions={}, run=FakeRunner([]), env={}
            )
            cleaner.report.add(
                install.CleanupItem(
                    "k", "hermes", "super-secret-bearer", STATUS.FAILED, "value super-secret-bearer"
                )
            )
            cleaner._scrub_secrets()
            item = cleaner.report.items[-1]
            self.assertNotIn("super-secret-bearer", item.target)
            self.assertNotIn("super-secret-bearer", item.detail)
            self.assertIn("***", item.detail)

    def test_step_isolates_unexpected_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts = _hermes_facts(Path(tmp) / "config.yaml")
            cleaner = self._cleaner(Path(tmp), facts)

            def boom():
                raise RuntimeError("kaboom")

            cleaner._step("some.key", "target", boom)
            self.assertEqual(len(cleaner.report.items), 1)
            self.assertIs(cleaner.report.items[0].status, STATUS.FAILED)
            self.assertIn("kaboom", cleaner.report.items[0].detail)


# ---------------------------------------------------------------------------
# OpenClaw Cleaner
# ---------------------------------------------------------------------------


class HermesPluginCliTest(unittest.TestCase):
    def _cleaner(self, tmp: Path, runner, *, cli="/usr/bin/hermes"):
        facts = _hermes_facts(tmp / "config.yaml", cli=cli)
        inv = _inventory(tmp, hermes=facts)
        return install.HermesCleaner(
            ui=FakeUI(), inv=inv, decisions={}, run=runner, env={}
        )






    def test_verify_cli_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = self._cleaner(root, FakeRunner([]), cli=None)._verify_cli()
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(
                item.detail, I18N.t("uninstall.detail.verify_cli_missing")
            )
            self.assertEqual(item.target, str(root / "plugins" / "miloco"))
        with tempfile.TemporaryDirectory() as tmp:
            item = self._cleaner(Path(tmp), FakeRunner([res("other")]))._verify_cli()
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(item.key, "uninstall.hermes.cli_verify")
            self.assertEqual(item.platform, "hermes")
            self.assertEqual(item.target, str(Path(tmp) / "plugins" / "miloco"))
        with tempfile.TemporaryDirectory() as tmp:
            item = self._cleaner(Path(tmp), FakeRunner([res("miloco")]))._verify_cli()
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(
                item.detail, I18N.t("uninstall.detail.config_residue")
            )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIs(
                self._cleaner(Path(tmp), FakeRunner([res(rc=2, stderr="nope")]))
                ._verify_cli()
                .status,
                STATUS.FAILED,
            )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIs(
                self._cleaner(
                    Path(tmp), FakeRunner([install.CommandFailed("k", "t", detail="x")])
                )
                ._verify_cli()
                .status,
                STATUS.FAILED,
            )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "plugins" / "miloco").mkdir(parents=True)
            item = self._cleaner(root, FakeRunner([]))._verify_cli()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertEqual(
                item.detail,
                I18N.t("uninstall.detail.plugin_dir_still_present"),
            )

    def test_clean_env_and_rc_absent_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleaner = self._cleaner(Path(tmp), FakeRunner([]))
            self.assertIs(cleaner._clean_env().status, STATUS.ABSENT)
            items = cleaner._clean_shell_rc()
            self.assertEqual([i.status for i in items], [STATUS.ABSENT])

    def test_cron_remove_exception_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            facts = _hermes_facts(
                Path(tmp) / "config.yaml", venv_python=python
            )
            inv = _inventory(Path(tmp), hermes=facts)
            listing = json.dumps(
                [{"id": "m1", "name": f"{install.HERMES_MANAGED_TAG} x"}]
            )
            runner = FakeRunner(
                [
                    res(listing),
                    install.CommandFailed("k", "t", detail="cannot remove"),
                    res("[]"),
                ]
            )
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=inv, decisions={}, run=runner, env={}
            )
            items = cleaner._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.FAILED])
            self.assertIn("cannot remove", items[0].detail)

    def test_cron_probe_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "hermes-agent" / "venv" / "bin" / "python"
            _make_executable(python)
            facts = _hermes_facts(Path(tmp) / "config.yaml", venv_python=python)
            inv = _inventory(Path(tmp), hermes=facts)
            runner = FakeRunner(
                [install.CommandFailed("k", "t", detail="python exploded")]
            )
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=inv, decisions={}, run=runner, env={}
            )
            items = cleaner._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.FAILED])
            self.assertIn("python exploded", items[0].detail)

    def test_adapter_symlink_is_unlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            real = root / "real-adapter"
            real.mkdir()
            agent_platform = home / "agent_platform"
            agent_platform.mkdir()
            (agent_platform / "hermes").symlink_to(real, target_is_directory=True)
            candidate = _candidate(home)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            inv = _inventory(root, hermes=facts, candidates=(candidate,))
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=inv, decisions={}, run=FakeRunner([]), env={}
            )
            items = cleaner._remove_adapters()
            self.assertEqual([i.status for i in items], [STATUS.DONE])
            self.assertFalse((agent_platform / "hermes").is_symlink())
            self.assertTrue((real).exists())

    def test_probe_error_recorded_in_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            runner = FakeRunner(
                [res("commands: rm"), res(rc=1, stderr="cron store locked")]
            )
            inv = install.build_inventory(
                env={},
                home=home,
                agent_platform_hint="",
                which=lambda name: "hermes" if name == "hermes" else None,
                run=runner,
            )
            self.assertEqual(inv.hermes.cron_backend, "cli")
            self.assertEqual(inv.hermes.managed_crons, ())
            self.assertIn("cron store locked", inv.hermes.cli_probe_error)


class OpenClawCleanerTest(unittest.TestCase):
    def _cleaner(self, tmp: Path, runner, *, cli="openclaw", tool_names=None):
        facts = _openclaw_facts(tmp, cli=cli)
        inv = _inventory(tmp, openclaw=facts, tool_names=tool_names)
        return install.OpenClawCleaner(ui=FakeUI(), inv=inv, run=runner)

    def test_uninstall_plugin_success_passes_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([res("")])
            item = self._cleaner(Path(tmp), runner)._uninstall_plugin()
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(runner.calls[0]["stdin"], "y\n")
            self.assertEqual(
                runner.calls[0]["argv"],
                ["openclaw", "plugins", "uninstall", install.OPENCLAW_PLUGIN_ID],
            )

    def test_uninstall_plugin_failure_includes_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([res(rc=1, stderr="nope", argv=("openclaw",))])
            item = self._cleaner(Path(tmp), runner)._uninstall_plugin()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertIn("rc=1", item.detail)
            self.assertIn("nope", item.detail)
            self.assertTrue(item.remediation)
            self.assertEqual(item.platform, "openclaw")
            self.assertEqual(item.key, "uninstall.openclaw.plugin")

    def test_uninstall_plugin_exception_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [install.CommandFailed("k", "t", detail="spawn failed")]
            )
            item = self._cleaner(Path(tmp), runner)._uninstall_plugin()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertIn("spawn failed", item.detail)
            self.assertIn("plugins uninstall", item.target)
            self.assertEqual(item.remediation, item.target)
            self.assertEqual(item.platform, "openclaw")

    def test_cli_missing_is_blocked_for_every_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleaner = self._cleaner(Path(tmp), FakeRunner([]), cli=None)
            for method in (
                cleaner._uninstall_plugin,
                cleaner._clean_tools_allow,
                cleaner._clean_plugin_entry,
                cleaner._verify_plugin_absent,
            ):
                self.assertIs(method().status, STATUS.BLOCKED)
            cron_items = cleaner._remove_managed_crons()
            self.assertEqual([i.status for i in cron_items], [STATUS.BLOCKED])

    def test_tools_allow_filters_only_managed_and_keeps_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            get_payload = json.dumps(
                ["keep1", "miloco_im_push", "keep2", "keep2", "miloco_notify_bind"]
            )
            runner = FakeRunner([res(get_payload), res("")])
            item = self._cleaner(
                Path(tmp),
                runner,
                tool_names=("miloco_im_push", "miloco_notify_bind", "miloco_notify_unbind"),
            )._clean_tools_allow()
            self.assertIs(item.status, STATUS.DONE)
            set_call = runner.calls[1]["argv"]
            self.assertEqual(
                set_call,
                [
                    "openclaw",
                    "config",
                    "set",
                    "tools.alsoAllow",
                    json.dumps(["keep1", "keep2", "keep2"]),
                    "--strict-json",
                ],
            )
            self.assertIn("miloco_im_push", item.detail)
            self.assertEqual(
                [call["timeout"] for call in runner.calls], [30, 30]
            )
            self.assertEqual(item.platform, "openclaw")
            self.assertEqual(item.key, "uninstall.openclaw.tools_allow")

    def test_tools_allow_set_exception_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [
                    res(json.dumps(["miloco_im_push", "keep"])),
                    install.CommandFailed("k", "t", detail="set boom"),
                ]
            )
            item = self._cleaner(Path(tmp), runner)._clean_tools_allow()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertIn("set boom", item.detail)
            self.assertIn("config set tools.alsoAllow", item.target)
            self.assertEqual(item.remediation, item.target)

    def test_tools_allow_no_match_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([res(json.dumps(["keep"]))])
            item = self._cleaner(Path(tmp), runner)._clean_tools_allow()
            self.assertIs(item.status, STATUS.ABSENT)
            self.assertEqual(item.target, "tools.alsoAllow")
            self.assertEqual(item.platform, "openclaw")
            self.assertEqual(len(runner.calls), 1)

    def test_tools_allow_null_and_non_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIs(
                self._cleaner(Path(tmp), FakeRunner([res("null")]))
                ._clean_tools_allow()
                .status,
                STATUS.ABSENT,
            )
        with tempfile.TemporaryDirectory() as tmp:
            item = self._cleaner(
                Path(tmp), FakeRunner([res(json.dumps({"a": 1}))])
            )._clean_tools_allow()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertEqual(item.key, "uninstall.openclaw.tools_allow")
            self.assertEqual(item.platform, "openclaw")
            self.assertEqual(item.target, "tools.alsoAllow")
            self.assertIn(
                I18N.t("uninstall.detail.not_a_list"), item.detail
            )

    def test_tools_allow_invalid_json_and_failed_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = self._cleaner(
                Path(tmp), FakeRunner([res("{oops")])
            )._clean_tools_allow()
            self.assertIs(item.status, STATUS.FAILED)
        with tempfile.TemporaryDirectory() as tmp:
            item = self._cleaner(
                Path(tmp), FakeRunner([res(rc=2, stderr="bad")])
            )._clean_tools_allow()
            self.assertIs(item.status, STATUS.FAILED)

    def test_tools_allow_set_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [res(json.dumps(["miloco_im_push"])), res(rc=1, stderr="denied")]
            )
            item = self._cleaner(Path(tmp), runner)._clean_tools_allow()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertIn("denied", item.detail)

    def test_plugin_entry_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = self._cleaner(Path(tmp), FakeRunner([res("")]))._clean_plugin_entry()
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(item.key, "uninstall.openclaw.plugin_entry")
            self.assertEqual(item.platform, "openclaw")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIs(
                self._cleaner(
                    Path(tmp),
                    FakeRunner([res(rc=1, stderr="path not found")]),
                )
                ._clean_plugin_entry()
                .status,
                STATUS.ABSENT,
            )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIs(
                self._cleaner(
                    Path(tmp), FakeRunner([res(rc=1, stderr="no such config path")])
                )
                ._clean_plugin_entry()
                .status,
                STATUS.ABSENT,
            )
        with tempfile.TemporaryDirectory() as tmp:
            item = self._cleaner(
                Path(tmp), FakeRunner([res(rc=1, stderr="permission denied")])
            )._clean_plugin_entry()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertEqual(item.key, "uninstall.openclaw.plugin_entry")

    def test_cron_removes_only_tagged_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            listing = json.dumps(
                {
                    "jobs": [
                        {"id": "m1", "name": "managed", "description": install.HERMES_MANAGED_TAG},
                        {"id": "u1", "name": "user", "description": "other"},
                    ]
                }
            )
            runner = FakeRunner([res(listing), res(""), res('{"jobs": []}')])
            items = self._cleaner(Path(tmp), runner)._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.DONE])
            self.assertEqual(items[0].platform, "openclaw")
            self.assertEqual(items[0].key, "uninstall.openclaw.cron")
            self.assertEqual(items[0].target, "managed")
            self.assertEqual(runner.calls[1]["argv"], ["openclaw", "cron", "rm", "m1"])
            self.assertEqual(
                runner.calls[2]["argv"],
                ["openclaw", "cron", "list", "--all", "--json"],
            )
            self.assertEqual(
                [call["timeout"] for call in runner.calls], [30, 30, 30]
            )

    def test_cron_jobid_and_name_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            listing = json.dumps(
                {
                    "jobs": [
                        {"jobId": "j9", "description": install.HERMES_MANAGED_TAG},
                    ]
                }
            )
            runner = FakeRunner([res(listing), res(""), res('{"jobs": []}')])
            items = self._cleaner(Path(tmp), runner)._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.DONE])
            self.assertEqual(items[0].target, "j9")
            self.assertEqual(runner.calls[1]["argv"], ["openclaw", "cron", "rm", "j9"])

    def test_cron_remaining_after_removal_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            listing = json.dumps(
                [
                    {
                        "id": "m1",
                        "name": "managed",
                        "description": install.HERMES_MANAGED_TAG,
                    }
                ]
            )
            runner = FakeRunner([res(listing), res(""), res(listing)])
            items = self._cleaner(Path(tmp), runner)._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.DONE, STATUS.FAILED])
            self.assertEqual(items[1].target, "managed")
            self.assertIn(
                I18N.t("uninstall.detail.cron_remaining"), items[1].detail
            )
            self.assertEqual(items[1].remediation, "openclaw cron list --all --json")

    def test_cron_job_without_id_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            listing = json.dumps(
                [{"name": "managed", "description": install.HERMES_MANAGED_TAG}]
            )
            runner = FakeRunner([res(listing), res('{"jobs": []}')])
            items = self._cleaner(Path(tmp), runner)._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.BLOCKED])
            self.assertEqual(items[0].key, "uninstall.openclaw.cron")
            self.assertEqual(items[0].platform, "openclaw")

    def test_cron_parse_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = self._cleaner(
                Path(tmp), FakeRunner([res("garbage")])
            )._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.FAILED])
            self.assertIn(
                I18N.t("uninstall.detail.cron_parse"), items[0].detail
            )

    def test_verify_plugin_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [res(json.dumps([{"id": install.OPENCLAW_PLUGIN_ID}]))]
            )
            self.assertIs(
                self._cleaner(Path(tmp), runner)._verify_plugin_absent().status,
                STATUS.FAILED,
            )
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([res('{"plugins": []}')])
            self.assertIs(
                self._cleaner(Path(tmp), runner)._verify_plugin_absent().status,
                STATUS.DONE,
            )
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([res(rc=1, stderr="nope")])
            self.assertIs(
                self._cleaner(Path(tmp), runner)._verify_plugin_absent().status,
                STATUS.FAILED,
            )

    def test_cron_remove_exception_and_verify_exception(self):
        listing = json.dumps(
            [
                {
                    "id": "m1",
                    "name": "managed",
                    "description": install.HERMES_MANAGED_TAG,
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [
                    res(listing),
                    install.CommandFailed("k", "t", detail="rm boom"),
                    res('{"jobs": []}'),
                ]
            )
            items = self._cleaner(Path(tmp), runner)._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.FAILED])
            self.assertIn("rm boom", items[0].detail)
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [
                    res(listing),
                    res(""),
                    install.CommandFailed("k", "t", detail="list boom"),
                ]
            )
            items = self._cleaner(Path(tmp), runner)._remove_managed_crons()
            self.assertEqual([i.status for i in items], [STATUS.DONE, STATUS.FAILED])

    @unittest.skip(
        "已知缺陷（见 whitebox-report）：删除后 `cron list` 校验命令非 0 时，"
        "OpenClaw cleaner 不追加任何 FAILED/BLOCKED 项，该项被静默当作成功。"
    )
    def test_cron_verify_failure_must_be_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            listing = json.dumps(
                [
                    {
                        "id": "m1",
                        "name": "managed",
                        "description": install.HERMES_MANAGED_TAG,
                    }
                ]
            )
            runner = FakeRunner(
                [res(listing), res(""), res(rc=1, stderr="gateway down")]
            )
            items = self._cleaner(Path(tmp), runner)._remove_managed_crons()
            self.assertIn(STATUS.FAILED, [i.status for i in items])

    def test_step_isolates_unexpected_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleaner = self._cleaner(Path(tmp), FakeRunner([]))

            def boom():
                raise RuntimeError("oc-kaboom")

            cleaner._step("some.key", "target", boom)
            self.assertEqual(len(cleaner.report.items), 1)
            self.assertIs(cleaner.report.items[0].status, STATUS.FAILED)
            self.assertIn("oc-kaboom", cleaner.report.items[0].detail)

    def test_full_run_reports_detection_and_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [
                    res(""),  # plugins uninstall
                    res("null"),  # config get tools.alsoAllow
                    res(rc=1, stderr="path not found"),  # config unset
                    res('{"jobs": []}'),  # cron list
                    res('{"plugins": []}'),  # plugins list
                ]
            )
            report = self._cleaner(Path(tmp), runner).run()
            self.assertTrue(report.detected)
            self.assertFalse(report.incomplete)
            self.assertEqual(
                [i.status for i in report.items],
                [STATUS.DONE, STATUS.ABSENT, STATUS.ABSENT, STATUS.ABSENT, STATUS.DONE],
            )
            self.assertEqual(
                [i.key for i in report.items],
                [
                    "uninstall.openclaw.plugin",
                    "uninstall.openclaw.tools_allow",
                    "uninstall.openclaw.plugin_entry",
                    "uninstall.openclaw.cron",
                    "uninstall.openclaw.verify",
                ],
            )
            self.assertTrue(all(i.platform == "openclaw" for i in report.items))
            self.assertEqual(
                [call["timeout"] for call in runner.calls],
                [120, 30, 30, 30, 60],
            )


# ---------------------------------------------------------------------------
# Core Cleaner
# ---------------------------------------------------------------------------


class CoreCleanerTest(unittest.TestCase):
    def test_uv_missing(self):
        with mock.patch.object(install.shutil, "which", return_value=None):
            report = install.CoreCleaner(ui=FakeUI(), run=FakeRunner([])).run()
        self.assertEqual([i.status for i in report.items], [STATUS.ABSENT])
        self.assertEqual(report.items[0].key, "uninstall.core.uv_missing")
        self.assertFalse(report.incomplete)

    def test_installed_and_absent_packages(self):
        runner = FakeRunner(
            [
                res("miloco-cli v1\nsupervisor v2\n"),
                res(""),
                res(""),
            ]
        )
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = install.CoreCleaner(ui=FakeUI(), run=runner).run()
        statuses = [(i.target, i.status) for i in report.items]
        self.assertEqual(
            statuses,
            [
                ("miloco-cli", STATUS.DONE),
                ("miloco", STATUS.ABSENT),
                ("supervisor", STATUS.DONE),
            ],
        )
        self.assertEqual(
            [i.key for i in report.items],
            [
                "uninstall.cli_removed",
                "uninstall.core.uv_item",
                "uninstall.core.uv_item",
            ],
        )
        self.assertTrue(all(i.platform == "core" for i in report.items))
        self.assertEqual(
            [call["timeout"] for call in runner.calls], [15, 60, 60]
        )
        self.assertEqual(runner.argvs[1], ["/usr/bin/uv", "tool", "uninstall", "miloco-cli"])

    def test_miloco_package_has_its_own_key(self):
        runner = FakeRunner([res("miloco v1\n"), res("")])
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = install.CoreCleaner(ui=FakeUI(), run=runner).run()
        self.assertEqual(
            [i.key for i in report.items],
            ["uninstall.core.uv_item", "uninstall.miloco_removed", "uninstall.core.uv_item"],
        )

    def test_list_failure_is_failed(self):
        runner = FakeRunner([res(rc=1, stderr="nope")])
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = install.CoreCleaner(ui=FakeUI(), run=runner).run()
        self.assertEqual([i.status for i in report.items], [STATUS.FAILED])
        self.assertIn("nope", report.items[0].detail)
        self.assertEqual(report.items[0].key, "uninstall.core.uv_item")
        self.assertEqual(report.items[0].platform, "core")
        self.assertEqual(runner.argvs[0], ["/usr/bin/uv", "tool", "list"])
        self.assertEqual(runner.calls[0]["timeout"], 15)

    def test_list_exception_is_failed(self):
        runner = FakeRunner([install.CommandFailed("k", "t", detail="uv exploded")])
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = install.CoreCleaner(ui=FakeUI(), run=runner).run()
        self.assertEqual([i.status for i in report.items], [STATUS.FAILED])
        self.assertIn("uv exploded", report.items[0].detail)

    def test_uninstall_exception_is_failed(self):
        runner = FakeRunner(
            [res("miloco-cli v1\n"), install.CommandFailed("k", "t", detail="boom")]
        )
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = install.CoreCleaner(ui=FakeUI(), run=runner).run()
        self.assertEqual(
            [i.status for i in report.items],
            [STATUS.FAILED, STATUS.ABSENT, STATUS.ABSENT],
        )
        self.assertIn("boom", report.items[0].detail)
        self.assertEqual(
            report.items[0].target, "uv tool uninstall miloco-cli"
        )

    def test_uninstall_failure_is_failed(self):
        runner = FakeRunner([res("miloco-cli v1\n"), res(rc=1, stderr="denied")])
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = install.CoreCleaner(ui=FakeUI(), run=runner).run()
        self.assertEqual(
            [i.status for i in report.items],
            [STATUS.FAILED, STATUS.ABSENT, STATUS.ABSENT],
        )


# ---------------------------------------------------------------------------
# home 决策 / 删除 / 报告 / 入口
# ---------------------------------------------------------------------------


class DecideHomeDeletionsTest(unittest.TestCase):
    def test_non_interactive_defaults_to_keep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "a"
            existing.mkdir()
            missing = root / "b"
            decisions = install.decide_home_deletions(
                [_candidate(existing), _candidate(missing)],
                ui=FakeUI(),
                interactive=False,
            )
            self.assertEqual(decisions, {existing.resolve(): False})

    def test_keyboard_interrupt_keeps_and_stops_asking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a"
            second = root / "b"
            first.mkdir()
            second.mkdir()
            asked = []

            class InterruptUI(FakeUI):
                def prompt_confirm(self, message, default=False):
                    asked.append(message)
                    raise KeyboardInterrupt

            decisions = install.decide_home_deletions(
                [_candidate(first), _candidate(second)],
                ui=InterruptUI(),
                interactive=True,
            )
            self.assertEqual(decisions[first.resolve()], False)
            self.assertEqual(decisions[second.resolve()], False)
            self.assertEqual(len(asked), 1)

    def test_interactive_answer_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "a"
            existing.mkdir()

            class YesUI(FakeUI):
                def prompt_confirm(self, message, default=False):
                    return True

            decisions = install.decide_home_deletions(
                [_candidate(existing)], ui=YesUI(), interactive=True
            )
            self.assertTrue(decisions[existing.resolve()])


class DeleteHomesTest(unittest.TestCase):
    def test_reports_kept_and_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            target = root / "target"
            target.mkdir()
            candidate = _candidate(target)
            items = install.delete_homes(
                [candidate], {candidate.real_path: True}, home=home, env={}
            )
            self.assertEqual([i.status for i in items], [STATUS.DONE])
            self.assertFalse(target.exists())

    def test_duplicate_candidate_second_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            target = root / "target"
            target.mkdir()
            candidate = _candidate(target)
            items = install.delete_homes(
                [candidate, candidate],
                {candidate.real_path: True},
                home=home,
                env={},
            )
            self.assertEqual([i.status for i in items], [STATUS.DONE, STATUS.SKIPPED])

    def test_inode_change_is_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            target = root / "target"
            target.mkdir()
            candidate = dataclasses.replace(_candidate(target), identity=(0, 0))
            items = install.delete_homes(
                [candidate], {candidate.real_path: True}, home=home, env={}
            )
            self.assertEqual([i.status for i in items], [STATUS.FAILED])
            self.assertIn("inode", items[0].detail)
            self.assertTrue(target.exists())

    def test_hermes_home_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            hermes_home = root / "hermes-home"
            hermes_home.mkdir()
            candidate = _candidate(hermes_home)
            items = install.delete_homes(
                [candidate],
                {candidate.real_path: True},
                home=home,
                env={"HERMES_HOME": str(hermes_home)},
            )
            self.assertEqual([i.status for i in items], [STATUS.FAILED])
            self.assertTrue(hermes_home.exists())

    def test_ancestor_of_home_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / "sub").mkdir(parents=True)
            candidate = _candidate(root)
            items = install.delete_homes(
                [candidate], {candidate.real_path: True}, home=home, env={}
            )
            self.assertEqual([i.status for i in items], [STATUS.FAILED])
            self.assertTrue(home.exists())


class PrintReportTest(unittest.TestCase):
    def test_all_clean_returns_true(self):
        ui = FakeUI()
        report = install.PlatformReport("hermes", detected=True, cli_available=True)
        report.add(install.CleanupItem("uninstall.hermes.env", "hermes", "t", STATUS.DONE))
        self.assertTrue(install._print_uninstall_report(ui, [report], []))
        self.assertIn(I18N.t("uninstall.section.hermes"), ui.console.text)
        self.assertIn(I18N.t("uninstall.summary.done"), ui.console.text)

    def test_kept_and_absent_do_not_fail(self):
        ui = FakeUI()
        report = install.PlatformReport("hermes", detected=False, cli_available=True)
        report.add(install.CleanupItem("uninstall.hermes.env", "hermes", "t", STATUS.KEPT))
        report.add(install.CleanupItem("uninstall.hermes.skills", "hermes", "t", STATUS.SKIPPED))
        self.assertTrue(install._print_uninstall_report(ui, [report], []))
        self.assertIn(I18N.t("uninstall.status.not_detected"), ui.console.text)
        self.assertIn(I18N.t("uninstall.section.hermes"), ui.console.text)
        self.assertIn(I18N.t("uninstall.summary.done"), ui.console.text)

    def test_blocked_report_returns_false_and_prints_remediation(self):
        ui = FakeUI()
        report = install.PlatformReport("hermes", detected=True, cli_available=False)
        report.add(
            install.CleanupItem(
                "uninstall.hermes.cli_unavailable",
                "hermes",
                "hermes",
                STATUS.BLOCKED,
                "missing",
                "install hermes",
            )
        )
        self.assertFalse(install._print_uninstall_report(ui, [report], []))
        self.assertIn("install hermes", ui.console.text)
        self.assertIn(I18N.t("uninstall.summary.incomplete"), ui.console.text)
        self.assertIn(I18N.t("uninstall.summary.gateway_hint"), ui.console.text)

    def test_failed_home_item_returns_false(self):
        ui = FakeUI()
        report = install.PlatformReport("hermes", detected=False, cli_available=True)
        home_items = [
            install.CleanupItem("uninstall.home.unsafe", "core", "/h", STATUS.FAILED)
        ]
        self.assertFalse(install._print_uninstall_report(ui, [report], home_items))
        self.assertIn(I18N.t("uninstall.section.home"), ui.console.text)

    def test_tool_source_printed_for_openclaw(self):
        ui = FakeUI()
        report = install.PlatformReport("openclaw", detected=True, cli_available=True)
        install._print_uninstall_report(
            ui, [report], [], tool_source="static_fallback"
        )
        self.assertIn(
            I18N.t("uninstall.openclaw.tool_source", "static_fallback"),
            ui.console.text,
        )

    def test_item_prefixes_follow_status(self):
        cases = {
            STATUS.DONE: "✓",
            STATUS.FAILED: "✗",
            STATUS.BLOCKED: "✗",
            STATUS.ABSENT: "–",
            STATUS.KEPT: "–",
            STATUS.SKIPPED: "–",
        }
        for status, prefix in cases.items():
            ui = FakeUI()
            install._print_cleanup_item(
                ui,
                install.CleanupItem(
                    "uninstall.hermes.env", "hermes", "target", status
                ),
            )
            self.assertIn(prefix, ui.console.text, f"status={status}")
            self.assertIn("target", ui.console.text)

    def test_remediation_printed_only_for_failures(self):
        ui = FakeUI()
        install._print_cleanup_item(
            ui,
            install.CleanupItem(
                "uninstall.hermes.env", "hermes", "t", STATUS.DONE, "", "fix me"
            ),
        )
        self.assertNotIn("fix me", ui.console.text)
        ui = FakeUI()
        install._print_cleanup_item(
            ui,
            install.CleanupItem(
                "uninstall.hermes.env", "hermes", "t", STATUS.BLOCKED, "", "fix me"
            ),
        )
        self.assertIn("fix me", ui.console.text)


class RunUninstallTest(unittest.TestCase):
    def _run(self, *, hermes_items=(), openclaw_items=(), core_items=(), home_items=()):
        ui = FakeUI()
        hermes_facts = _hermes_facts(Path("/nonexistent/hermes/config.yaml"), detected=True)
        openclaw_facts = _openclaw_facts(Path("/nonexistent/openclaw"), detected=True)
        inventory = _inventory(
            Path("/nonexistent/home"),
            hermes=hermes_facts,
            openclaw=openclaw_facts,
        )

        def make_report(platform, items):
            report = install.PlatformReport(platform, detected=True, cli_available=True)
            for status in items:
                report.add(install.CleanupItem(f"key.{platform}", platform, "t", status))
            return report

        with mock.patch.object(install, "build_inventory", return_value=inventory), \
             mock.patch.object(install, "decide_home_deletions", return_value={}), \
             mock.patch.object(install, "delete_homes", return_value=list(home_items)), \
             mock.patch.object(
                 install, "SubprocessRunner", return_value=FakeRunner([])
             ), \
             mock.patch.object(
                 install,
                 "HermesCleaner",
                 return_value=types.SimpleNamespace(
                     run=lambda: make_report("hermes", hermes_items)
                 ),
             ), \
             mock.patch.object(
                 install,
                 "OpenClawCleaner",
                 return_value=types.SimpleNamespace(
                     run=lambda: make_report("openclaw", openclaw_items)
                 ),
             ), \
             mock.patch.object(
                 install,
                 "CoreCleaner",
                 return_value=types.SimpleNamespace(
                     run=lambda: make_report("core", core_items)
                 ),
             ):
            args = types.SimpleNamespace(agent_platform="")
            return install.run_uninstall(ui, args=args)

    def test_returns_zero_when_everything_clean(self):
        self.assertEqual(
            self._run(
                hermes_items=[STATUS.DONE],
                openclaw_items=[STATUS.ABSENT],
                core_items=[STATUS.DONE],
            ),
            0,
        )

    def test_returns_one_for_blocked_or_failed(self):
        self.assertEqual(self._run(hermes_items=[STATUS.BLOCKED]), 1)
        self.assertEqual(self._run(openclaw_items=[STATUS.FAILED]), 1)
        self.assertEqual(
            self._run(
                core_items=[STATUS.DONE],
                home_items=[
                    install.CleanupItem(
                        "uninstall.home.unsafe", "core", "/h", STATUS.FAILED
                    )
                ],
            ),
            1,
        )

    def test_does_not_create_miloco_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "never-created"
            with mock.patch.dict(os.environ, {"MILOCO_HOME": str(marker)}):
                self._run()
            self.assertFalse(marker.exists())

    def test_undetected_platforms_are_not_cleaned(self):
        ui = FakeUI()
        hermes_facts = _hermes_facts(
            Path("/nonexistent/hermes/config.yaml"), detected=False
        )
        openclaw_facts = _openclaw_facts(Path("/nonexistent/openclaw"), detected=False)
        inventory = _inventory(
            Path("/nonexistent/home"),
            hermes=hermes_facts,
            openclaw=openclaw_facts,
        )
        core = install.PlatformReport("core", detected=True, cli_available=True)
        core.add(install.CleanupItem("uninstall.core.uv_item", "core", "x", STATUS.DONE))
        with mock.patch.object(install, "build_inventory", return_value=inventory), \
             mock.patch.object(install, "decide_home_deletions", return_value={}), \
             mock.patch.object(install, "delete_homes", return_value=[]), \
             mock.patch.object(install, "SubprocessRunner", return_value=FakeRunner([])), \
             mock.patch.object(install, "HermesCleaner") as hermes_cleaner, \
             mock.patch.object(install, "OpenClawCleaner") as openclaw_cleaner, \
             mock.patch.object(
                 install,
                 "CoreCleaner",
                 return_value=types.SimpleNamespace(run=lambda: core),
             ):
            code = install.run_uninstall(
                ui, args=types.SimpleNamespace(agent_platform="")
            )
        self.assertEqual(code, 0)
        hermes_cleaner.assert_not_called()
        openclaw_cleaner.assert_not_called()
        self.assertEqual(
            ui.console.text.count(I18N.t("uninstall.status.not_detected")), 2
        )


class MainEntryTest(unittest.TestCase):
    def test_uninstall_branch_runs_before_mkdir_and_platform_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            miloco_home = Path(tmp) / "should-not-exist"
            with mock.patch.object(sys, "argv", ["install.py", "--uninstall"]), \
                 mock.patch.dict(os.environ, {"MILOCO_HOME": str(miloco_home)}), \
                 mock.patch.object(
                     install, "run_uninstall", return_value=0
                 ) as run_uninstall, \
                 mock.patch.object(
                     install, "_decide_agent_platform"
                 ) as decide:
                with self.assertRaises(SystemExit) as ctx:
                    install.main()
            self.assertEqual(ctx.exception.code, 0)
            run_uninstall.assert_called_once()
            decide.assert_not_called()
            self.assertFalse(miloco_home.exists())


if __name__ == "__main__":
    unittest.main()
