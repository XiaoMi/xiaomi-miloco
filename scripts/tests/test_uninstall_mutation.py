"""卸载路径的变异靶向测试：逐字段断言 + 逐实参断言。

与既有测试的分工：这里不重复"功能是否可用"，而是把每个返回值（整个
`CleanupItem`）、每一次命令调用（argv/env/timeout/stdin）与每一次 UI 调用
（消息、默认项）都钉死，让只改动实参/字面量的变异体也会被杀死。

测试按 `__file__` 定位仓库，不依赖 cwd 或已安装的包；被测模块是被执行
副本（变异测试）里的 `scripts/install.py`。
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

from scripts.tests._module_proxy import MergedModule

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
TAG = "[miloco:home-profile]"
DONE = None  # 延迟到模块加载后赋值


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
    """按包名导入 `scripts.install`，保证变异副本时导入的也是副本。"""
    _ensure_optional_deps()
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    existing = sys.modules.get("scripts")
    if existing is not None:
        paths = getattr(existing, "__path__", [])
        target = (REPO_ROOT / "scripts").resolve()
        if not any(Path(p).resolve() == target for p in paths):
            for name in [n for n in sys.modules if n == "scripts" or n.startswith("scripts.")]:
                del sys.modules[name]
    module = importlib.import_module("scripts.install")
    uninstall = importlib.import_module("scripts.installer_uninstall")
    return MergedModule(module, uninstall)


install = _load_install_module()
I18N = install.I18n("en", SCRIPTS)
STATUS = install.ItemStatus
TAG = install.HERMES_MANAGED_TAG
PLUGIN_ID = install.OPENCLAW_PLUGIN_ID


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def res(stdout: str = "", *, rc: int = 0, stderr: str = "", argv=()):
    return install.CommandResult(tuple(argv), rc, stdout, stderr)


class RecordingRunner:
    """按顺序返回预设结果，完整记录每次调用的全部实参。"""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, *, env=None, timeout=30.0, stdin=None):
        self.calls.append(
            {
                "argv": [str(a) for a in argv],
                "env": dict(env) if env is not None else None,
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
        if not args:
            self.lines.append("")
            return
        for arg in args:
            self.lines.append(str(arg))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class FakeUI:
    """记录 ok/warn/info 与确认提示的每一个实参。"""

    def __init__(self, answers=()):
        self.i18n = I18N
        self.console = _Console()
        self.console_err = _Console()
        self.oks: list[str] = []
        self.warns: list[str] = []
        self.infos: list[str] = []
        self.prompts: list[dict] = []
        self.answers = list(answers)

    def ok(self, msg):
        self.oks.append(msg)

    def warn(self, msg):
        self.warns.append(msg)

    def info(self, msg):
        self.infos.append(msg)

    def prompt_confirm(self, message, default=False):
        self.prompts.append({"message": message, "default": default})
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return default


def make_item(key, platform, target, status, detail="", remediation=""):
    return install.CleanupItem(key, platform, target, status, detail, remediation)


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


def _inventory(home: Path, *, candidates=(), hermes=None, openclaw=None,
               tool_names=None, tool_source="static_fallback"):
    return install.Inventory(
        home=home,
        candidates=tuple(candidates),
        primary_home=candidates[0].path if candidates else None,
        hermes=hermes or _hermes_facts(home / ".hermes" / "config.yaml"),
        openclaw=openclaw or _openclaw_facts(home / ".openclaw"),
        tool_names=tuple(tool_names if tool_names is not None else install.FALLBACK_OPENCLAW_TOOLS),
        tool_names_source=tool_source,
    )


def _candidate(path: Path, sources=("env",), *, deliver_configured=False):
    return install._build_home_candidate(
        path, set(sources), deliver_configured=deliver_configured
    )


def _hermes_cleaner(tmp: Path, *, facts, runner, decisions=None, candidates=()):
    return install.HermesCleaner(
        ui=FakeUI(),
        inv=_inventory(tmp, hermes=facts, candidates=candidates),
        decisions=decisions or {},
        run=runner,
        env={"OTHER": "1", "HERMES_HOME": "from-env"},
    )


def _openclaw_cleaner(tmp: Path, *, facts, runner):
    return install.OpenClawCleaner(
        ui=FakeUI(), inv=_inventory(tmp, openclaw=facts), run=runner
    )


# ---------------------------------------------------------------------------
# 小工具：逐字段断言
# ---------------------------------------------------------------------------


class SmallHelperTest(unittest.TestCase):
    def test_command_failure_detail_exact(self):
        result = res(stderr="warn\n\n  boom happened  \n\n", rc=3, argv=("hermes", "x"))
        self.assertEqual(
            install._command_failure_detail(result), "hermes x (rc=3): boom happened"
        )
        self.assertEqual(
            install._command_failure_detail(res(rc=2, argv=("a", "b"))),
            "a b (rc=2)",
        )
        long = install._command_failure_detail(
            res(rc=1, stderr="y" * 50, argv=("c",)), limit=5
        )
        self.assertEqual(long, "c (rc=1): yyyyy…")
        # 默认上限 400，不是 40/4000
        boundary = install._command_failure_detail(
            res(rc=1, stderr="z" * 400, argv=("c",))
        )
        self.assertTrue(boundary.endswith("z" * 400))
        self.assertNotIn("…", boundary)
        truncated = install._command_failure_detail(
            res(rc=1, stderr="z" * 401, argv=("c",))
        )
        self.assertEqual(truncated, "c (rc=1): " + "z" * 400 + "…")

    def test_redact_secrets_boundaries(self):
        self.assertEqual(
            install._redact_secrets("k=abcd zz=abc", ["abcd", "abc", "", "x"]),
            "k=*** zz=abc",
        )
        # 恰好 4 个字符才替换；3 个不替换
        self.assertEqual(install._redact_secrets("aaaa", ["aaaa"]), "***")
        self.assertEqual(install._redact_secrets("aaa", ["aaa"]), "aaa")

    def test_read_text_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing"
            self.assertEqual(install._read_text(missing), "")
            binary = root / "bin"
            binary.write_bytes(b"\xff\xfe\x00")
            self.assertEqual(install._read_text(binary), "")
            good = root / "good.json"
            good.write_text('{"a": 1}', encoding="utf-8")
            self.assertEqual(install._read_json(good), {"a": 1})
            listy = root / "list.json"
            listy.write_text("[1, 2]", encoding="utf-8")
            self.assertIsNone(install._read_json(listy))
            bad = root / "bad.json"
            bad.write_text("{", encoding="utf-8")
            self.assertIsNone(install._read_json(bad))
            self.assertIsNone(install._read_json(missing))

    def test_unquote_and_expand(self):
        self.assertEqual(install._unquote(' "a b" '), "a b")
        self.assertEqual(install._unquote("'x'"), "x")
        self.assertEqual(install._unquote('"x'), '"x')
        self.assertEqual(install._unquote('"'), '"')
        self.assertEqual(install._unquote("''"), "")
        with mock.patch.dict(os.environ, {"MILOCO_TEST_ROOT": "/opt/miloco"}):
            self.assertEqual(
                install._expand_path('"${MILOCO_TEST_ROOT}/data"'),
                Path("/opt/miloco/data"),
            )
            self.assertEqual(
                install._expand_path("~/x"), Path(os.path.expanduser("~")) / "x"
            )

    def test_same_path_resolves_each_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            target = (root / "b").resolve()
            self.assertFalse(install._same_path(str(root / "a"), [target]))
            self.assertTrue(install._same_path(str(root / "b"), [root / "a", target]))
            self.assertFalse(install._same_path(str(root / "a"), []))
            self.assertFalse(install._same_path("", [target]))

    def test_env_and_rc_iterators(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / ".env"
            env.write_text(
                "# c\n"
                "API_SERVER_KEY=plain\n"
                'export MILOCO_HOME="/x/y"\n'
                "  spaced  =  'v'  \n"
                "not a key=1\n"
                "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                list(install._iter_env_assignments(env)),
                [(1, "API_SERVER_KEY", "plain"), (2, "MILOCO_HOME", "/x/y"),
                 (3, "spaced", "v")],
            )
            rc = root / ".zshrc"
            rc.write_text(
                "# c\n"
                "export MILOCO_HOME=/a\n"
                'export MILOCO_HOME="/b c"\n'
                "export MILOCO_HOME=\n"
                "export OTHER=1\n",
                encoding="utf-8",
            )
            self.assertEqual(list(install._iter_rc_exports(rc)), ["/a", "/b c"])

    def test_rm_path_and_rmdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing"
            self.assertFalse(install._rm_path(missing))
            file_path = root / "f"
            file_path.write_text("x")
            self.assertTrue(install._rm_path(file_path))
            self.assertFalse(file_path.exists())
            tree = root / "tree" / "sub"
            tree.mkdir(parents=True)
            (tree / "f").write_text("x")
            self.assertTrue(install._rm_path(root / "tree"))
            self.assertFalse((root / "tree").exists())
            # 符号链接只删链接本身
            target = root / "target"
            target.mkdir()
            (target / "keep").write_text("x")
            link = root / "link"
            link.symlink_to(target)
            self.assertTrue(install._rm_path(link))
            self.assertFalse(os.path.lexists(link))
            self.assertTrue((target / "keep").exists())
            empty = root / "empty"
            empty.mkdir()
            install._rmdir_if_empty(empty)
            self.assertFalse(empty.exists())
            nonempty = root / "nonempty"
            nonempty.mkdir()
            (nonempty / "f").write_text("x")
            install._rmdir_if_empty(nonempty)  # 不抛
            self.assertTrue(nonempty.exists())
            install._rmdir_if_empty(root / "gone")  # 不抛

    def test_is_ancestor_or_self(self):
        self.assertTrue(install._is_ancestor_or_self(Path("/a"), Path("/a/b")))
        self.assertTrue(install._is_ancestor_or_self(Path("/a"), Path("/a")))
        self.assertFalse(install._is_ancestor_or_self(Path("/a/b"), Path("/a")))
        self.assertFalse(install._is_ancestor_or_self(Path("/a/b"), Path("/ab")))

    def test_subprocess_env(self):
        base = {"A": "1", "B": "keep"}
        self.assertEqual(
            install._subprocess_env(base, A="2", C="3", D="", E=None),
            {"A": "2", "B": "keep", "C": "3"},
        )

    def test_parse_uv_tool_list_exact(self):
        text = "miloco-cli v2026.10.16\n\n- supervisor v1.2\nmiloco\n-\n  pkg one two\n"
        self.assertEqual(
            install._parse_uv_tool_list(text),
            {"miloco-cli", "supervisor", "miloco", "pkg"},
        )
        self.assertEqual(install._parse_uv_tool_list(None), set())

    def test_parse_json_output(self):
        self.assertIsNone(install._parse_json_output(""))
        self.assertIsNone(install._parse_json_output("  \n"))
        self.assertEqual(install._parse_json_output(' {"a": 1} '), {"a": 1})
        self.assertIsNone(install._parse_json_output("noise"))

    def test_hermes_plugin_line_present_exact_tokens(self):
        self.assertTrue(install._hermes_plugin_line_present("miloco | enabled"))
        self.assertTrue(install._hermes_plugin_line_present("x\nmiloco/miloco-plugin,y"))
        self.assertFalse(install._hermes_plugin_line_present("miloco-extra"))
        self.assertFalse(install._hermes_plugin_line_present("xmiloco x/miloco"))
        self.assertFalse(install._hermes_plugin_line_present(""))


# ---------------------------------------------------------------------------
# 原子写入 / 路径 / 盘点候选
# ---------------------------------------------------------------------------


class AtomicWriteTest(unittest.TestCase):
    def test_preserves_mode_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o600)
            install._atomic_write_text(path, "new\nline\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "new\nline\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                [p.name for p in Path(tmp).iterdir()], ["f"], "no temp file left"
            )

    def test_symlink_writes_through_and_keeps_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "real"
            target.write_text("old", encoding="utf-8")
            link = root / "link"
            link.symlink_to(target)
            install._atomic_write_text(link, "new")
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_failure_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "f"
            path.write_text("old", encoding="utf-8")
            with mock.patch.object(install.os, "fsync", side_effect=OSError("nope")):
                with self.assertRaises(OSError):
                    install._atomic_write_text(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(root.iterdir()), [path])


class PlatformPathTest(unittest.TestCase):
    def test_resolve_hermes_home(self):
        home = Path("/home/u")
        self.assertEqual(
            install.resolve_hermes_home({"HERMES_HOME": "/opt/hermes"}, home),
            Path("/opt/hermes"),
        )
        self.assertEqual(
            install.resolve_hermes_home({"HERMES_HOME": "~/h"}, home),
            Path(os.path.expanduser("~")) / "h",
        )
        self.assertEqual(install.resolve_hermes_home({}, home), home / ".hermes")
        self.assertEqual(install.resolve_hermes_home({"HERMES_HOME": ""}, home),
                         home / ".hermes")

    def test_resolve_openclaw_paths(self):
        home = Path("/home/u")
        self.assertEqual(
            install.resolve_openclaw_paths({}, home),
            (home / ".openclaw", home / ".openclaw" / "openclaw.json"),
        )
        self.assertEqual(
            install.resolve_openclaw_paths(
                {"OPENCLAW_STATE_DIR": "/s", "OPENCLAW_CONFIG_PATH": "/c.json"}, home
            ),
            (Path("/s"), Path("/c.json")),
        )
        self.assertEqual(
            install.resolve_openclaw_paths({"OPENCLAW_STATE_DIR": "~/s"}, home)[1],
            Path(os.path.expanduser("~")) / "s" / "openclaw.json",
        )

    def test_default_home_for(self):
        home = Path("/home/u")
        self.assertEqual(install._default_home_for("hermes", home),
                         home / ".hermes" / "miloco")
        self.assertEqual(install._default_home_for("openclaw", home),
                         home / ".openclaw" / "miloco")
        self.assertIsNone(install._default_home_for("", home))
        self.assertIsNone(install._default_home_for("other", home))

    def test_hermes_venv_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            self.assertIsNone(install._hermes_venv_python(hermes_home))
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o644)  # 不可执行
            self.assertIsNone(install._hermes_venv_python(hermes_home))
            python.chmod(0o755)
            self.assertEqual(install._hermes_venv_python(hermes_home), python)
            # 目录不是文件
            python.unlink()
            python.mkdir()
            self.assertIsNone(install._hermes_venv_python(hermes_home))


class HomeCandidateTest(unittest.TestCase):
    def test_missing_path_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = _candidate(Path(tmp) / "nope")
            self.assertFalse(candidate.exists)
            self.assertIsNone(candidate.identity)
            self.assertFalse(candidate.is_symlink)
            self.assertFalse(candidate.has_config)
            self.assertFalse(candidate.has_agent_platform)
            self.assertFalse(candidate.has_hermes_adapter)
            self.assertFalse(candidate.has_install_cache)
            self.assertEqual(candidate.agent_platform, "")
            self.assertEqual(candidate.auth_bearer, "")
            self.assertFalse(candidate.deliver_configured)
            self.assertEqual(candidate.sources, frozenset({"env"}))
            self.assertEqual(candidate.real_path, (Path(tmp) / "nope").resolve())

    def test_flags_from_filesystem_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "miloco"
            (home / "agent_platform" / "hermes").mkdir(parents=True)
            (home / ".install-cache" / "hermes-plugin").mkdir(parents=True)
            (home / "config.json").write_text(
                json.dumps({"agent": {"platform": "hermes", "auth_bearer": "BEAR"}}),
                encoding="utf-8",
            )
            candidate = _candidate(home, sources=("env", "shell_rc"),
                                   deliver_configured=True)
            self.assertTrue(candidate.exists)
            self.assertEqual(candidate.sources, frozenset({"env", "shell_rc"}))
            self.assertTrue(candidate.has_config)
            self.assertTrue(candidate.has_agent_platform)
            self.assertTrue(candidate.has_hermes_adapter)
            self.assertTrue(candidate.has_install_cache)
            self.assertEqual(candidate.agent_platform, "hermes")
            self.assertEqual(candidate.auth_bearer, "BEAR")
            self.assertTrue(candidate.deliver_configured)
            self.assertEqual(
                candidate.identity,
                (home.stat().st_dev, home.stat().st_ino),
            )

    def test_symlinked_artifacts_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "miloco"
            home.mkdir()
            outside = root / "outside"
            outside.mkdir()
            link = home / "agent_platform" / "hermes"
            link.parent.mkdir()
            link.symlink_to(outside)
            cache = home / ".install-cache" / "hermes-plugin"
            cache.parent.mkdir()
            cache.symlink_to(outside)
            candidate = _candidate(home)
            self.assertTrue(candidate.has_hermes_adapter)
            self.assertTrue(candidate.has_install_cache)

    def test_config_without_agent_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "miloco"
            home.mkdir()
            (home / "config.json").write_text("[1]", encoding="utf-8")
            candidate = _candidate(home)
            self.assertEqual(candidate.agent_platform, "")
            self.assertEqual(candidate.auth_bearer, "")

    def test_symlink_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target)
            candidate = _candidate(link)
            self.assertTrue(candidate.is_symlink)
            self.assertEqual(candidate.path, link)
            self.assertEqual(candidate.real_path, target.resolve())

    def test_deliver_configured_from_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            state = hermes_home / "plugins" / "miloco" / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            self.assertFalse(install._plugin_state_deliver_configured(hermes_home))
            state.write_text("{", encoding="utf-8")
            self.assertFalse(install._plugin_state_deliver_configured(hermes_home))
            state.write_text('{"deliver": []}', encoding="utf-8")
            self.assertFalse(install._plugin_state_deliver_configured(hermes_home))
            state.write_text('{"deliver": {"target": ""}}', encoding="utf-8")
            self.assertFalse(install._plugin_state_deliver_configured(hermes_home))
            state.write_text('{"deliver": {"candidates": ["a"]}}', encoding="utf-8")
            self.assertTrue(install._plugin_state_deliver_configured(hermes_home))


class DiscoverCandidatesTest(unittest.TestCase):
    def _discover(self, root, env=None, hint=""):
        home = root / "home"
        hermes_home = home / ".hermes"
        openclaw = home / ".openclaw"
        return install.discover_home_candidates(
            env=env or {},
            home=home,
            hermes_home=hermes_home,
            openclaw_state_dir=openclaw,
            hint=hint,
        )

    def test_sources_dedupe_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / ".hermes").mkdir(parents=True)
            custom = root / "custom"
            custom.mkdir()
            other = root / "other"
            other.mkdir()
            (home / ".hermes" / ".env").write_text(
                f"MILOCO_HOME={custom}\nMILOCO_HOME=\n", encoding="utf-8"
            )
            (home / ".zshrc").write_text(
                f"export MILOCO_HOME={custom}\nexport MILOCO_HOME={other}\n",
                encoding="utf-8",
            )
            candidates = self._discover(root, env={"MILOCO_HOME": str(custom)})
            paths = [c.path for c in candidates]
            # env 候选排第一；其余按路径字典序（含两个默认值）
            self.assertEqual(paths[0], custom)
            self.assertEqual(
                [str(p) for p in paths[1:]],
                sorted(str(p) for p in paths[1:]),
            )
            self.assertEqual(
                candidates[0].sources, frozenset({"env", "hermes_env", "shell_rc"})
            )
            # 默认值也在候选里
            self.assertIn(home / ".hermes" / "miloco", paths)
            self.assertIn(home / ".openclaw" / "miloco", paths)
            self.assertIn(other, paths)
            # 空值不产生候选
            self.assertEqual(
                len([c for c in candidates if c.path == home / ".hermes"]), 0
            )

    def test_hint_moves_default_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            env_home = root / "zzz"
            candidates = self._discover(
                root, env={"MILOCO_HOME": str(env_home)}, hint="openclaw"
            )
            self.assertEqual(candidates[0].path, env_home)
            self.assertEqual(candidates[1].path, home / ".openclaw" / "miloco")
            self.assertEqual(
                [str(c.path) for c in candidates[2:]],
                sorted(str(c.path) for c in candidates[2:]),
            )
            self.assertEqual(candidates[1].sources, frozenset({"default_openclaw"}))
            self.assertEqual(candidates[2].sources, frozenset({"default_hermes"}))

    def test_deliver_flag_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / "home" / ".hermes"
            state = hermes_home / "plugins" / "miloco" / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"deliver": {"target": "x"}}', encoding="utf-8")
            candidates = self._discover(root)
            self.assertTrue(all(c.deliver_configured for c in candidates))

    def test_empty_sources_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "home").mkdir()
            candidates = self._discover(root, env={"MILOCO_HOME": ""})
            self.assertEqual(
                {c.sources for c in candidates},
                {frozenset({"default_hermes"}), frozenset({"default_openclaw"})},
            )


# ---------------------------------------------------------------------------
# Hermes cron：文本解析与现场读取
# ---------------------------------------------------------------------------


class HermesCronParseTest(unittest.TestCase):
    def test_block_format_exact_fields(self):
        text = (
            "  e044a389aede [active]\n"
            "    Name:      [miloco:home-profile] miloco-home-patrol\n"
            "  abc123 [paused]\n"
            "    Name:      user-task\n"
        )
        self.assertEqual(
            install._parse_hermes_cron_text(text),
            (install.ManagedCron("e044a389aede",
                                 "[miloco:home-profile] miloco-home-patrol",
                                 "[miloco:home-profile] miloco-home-patrol"),),
        )

    def test_ansi_and_paused(self):
        text = (
            "\x1b[33m  11aa22bb [paused]\x1b[0m\n"
            "\x1b[33m    Name:  [miloco:home-profile] digest\x1b[0m\n"
        )
        self.assertEqual(
            install._parse_hermes_cron_text(text),
            (install.ManagedCron("11aa22bb", "[miloco:home-profile] digest",
                                 "[miloco:home-profile] digest"),),
        )

    def test_id_does_not_leak_into_next_block(self):
        text = (
            "  first [active]\n"
            "    Name:  user-task\n"
            "    Description: x\n"
            "    Name:  [miloco:home-profile] second\n"
        )
        jobs = install._parse_hermes_cron_text(text)
        self.assertEqual(
            jobs,
            (install.ManagedCron("", "[miloco:home-profile] second",
                                 "[miloco:home-profile] second"),),
        )

    def test_name_line_without_id_header(self):
        text = "    Name:  [miloco:home-profile] orphan\n"
        self.assertEqual(
            install._parse_hermes_cron_text(text),
            (install.ManagedCron("", "[miloco:home-profile] orphan",
                                 "[miloco:home-profile] orphan"),),
        )

    def test_tag_outside_name_block_is_fallback(self):
        text = "job id=xyz [miloco:home-profile] whatever\n"
        self.assertEqual(
            install._parse_hermes_cron_text(text),
            (install.ManagedCron("xyz",
                                 "job id=xyz [miloco:home-profile] whatever",
                                 "job id=xyz [miloco:home-profile] whatever"),),
        )

    def test_tag_line_without_id_fallback(self):
        text = "loose [miloco:home-profile] line\n"
        self.assertEqual(
            install._parse_hermes_cron_text(text),
            (install.ManagedCron("", "loose [miloco:home-profile] line",
                                 "loose [miloco:home-profile] line"),),
        )

    def test_non_managed_and_empty(self):
        self.assertEqual(install._parse_hermes_cron_text(""), ())
        self.assertEqual(install._parse_hermes_cron_text(None), ())
        self.assertEqual(install._parse_hermes_cron_text("  id1 [active]\n"), ())

    def test_header_requires_status_bracket(self):
        # 没有 [status] 的行不是 id 头，随后的 Name 行拿不到 id
        text = "  id1 active\n    Name:  [miloco:home-profile] x\n"
        self.assertEqual(
            install._parse_hermes_cron_text(text),
            (install.ManagedCron("", "[miloco:home-profile] x",
                                 "[miloco:home-profile] x"),),
        )


class HermesCronLiveTest(unittest.TestCase):
    HERMES_ENV = {"HERMES_HOME": "/h", "PATH": "/bin"}

    def _live(self, runner, *, cli="/usr/bin/hermes", hermes_home=Path("/hh"),
              venv_python=None, env=None):
        with mock.patch.object(install, "_hermes_venv_python",
                               return_value=venv_python):
            return install._hermes_cron_live(
                run=runner, hermes_home=hermes_home,
                env=env or self.HERMES_ENV, cli=cli,
            )

    def test_cli_happy_path_exact_calls(self):
        runner = RecordingRunner([
            res("cron --help\n  rm <id>"),
            res("  id9 [active]\n    Name:  [miloco:home-profile] a\n"),
        ])
        backend, jobs, error = self._live(runner, hermes_home=Path("/hh"))
        self.assertEqual(backend, "cli")
        self.assertEqual(jobs, (install.ManagedCron("id9", "[miloco:home-profile] a",
                                                    "[miloco:home-profile] a"),))
        self.assertEqual(error, "")
        self.assertEqual(runner.calls[0]["argv"], ["/usr/bin/hermes", "cron", "--help"])
        self.assertEqual(runner.calls[0]["timeout"], 60)
        self.assertEqual(runner.calls[0]["env"],
                         {"HERMES_HOME": "/hh", "PATH": "/bin"})
        self.assertIsNone(runner.calls[0]["stdin"])
        self.assertEqual(runner.calls[1]["argv"],
                         ["/usr/bin/hermes", "cron", "list", "--all"])
        self.assertEqual(runner.calls[1]["timeout"], 60)
        self.assertEqual(runner.calls[1]["env"],
                         {"HERMES_HOME": "/hh", "PATH": "/bin"})

    def test_help_missing_remove_falls_back_to_local(self):
        python = Path("/venv/python")
        runner = RecordingRunner([
            res("usage: hermes cron\n  list\n", rc=0),
            res(json.dumps([{"id": "a", "name": "n"}])),
        ])
        backend, jobs, error = self._live(runner, venv_python=python)
        self.assertEqual(backend, "local_api")
        self.assertEqual(jobs, (install.ManagedCron("a", "n"),))
        self.assertEqual(error, "")
        self.assertEqual(runner.calls[1]["argv"],
                         [str(python), "-c", install._HERMES_CRON_LIST_SNIPPET])
        self.assertEqual(runner.calls[1]["timeout"], 60)
        self.assertEqual(runner.calls[1]["env"],
                         {"HERMES_HOME": "/hh", "PATH": "/bin"})

    def test_help_nonzero_and_delete_keyword(self):
        runner = RecordingRunner([res("nope", rc=1)])
        backend, jobs, error = self._live(runner, cli="/usr/bin/hermes", venv_python=None)
        self.assertEqual(backend, "cli")
        self.assertEqual(jobs, ())
        self.assertIn("cli: ", error)
        # 只有一条调用
        self.assertEqual(len(runner.calls), 1)

        runner = RecordingRunner([res("usage: delete a job"), res("")])
        backend, jobs, error = self._live(runner)
        # help 里出现 delete（而非 rm/remove）也算有删除子命令
        self.assertEqual((backend, jobs, error), ("cli", (), ""))
        self.assertEqual(runner.calls[1]["argv"],
                         ["/usr/bin/hermes", "cron", "list", "--all"])

    def test_cli_list_failure_records_detail(self):
        runner = RecordingRunner([
            res("rm"),
            res(rc=2, stderr="gateway down"),
        ])
        backend, jobs, error = self._live(runner)
        self.assertEqual(backend, "cli")
        self.assertEqual(jobs, ())
        self.assertIn("gateway down", error)
        self.assertIn("rc=2", error)

    def test_cli_command_exception_becomes_error(self):
        runner = RecordingRunner([
            install.CommandFailed("k", "hermes cron --help", detail="boom"),
        ])
        backend, jobs, error = self._live(runner, venv_python=None)
        self.assertEqual(backend, "cli")
        self.assertEqual(error, "cli: boom")

    def test_local_api_error_dict(self):
        python = Path("/venv/python")
        runner = RecordingRunner([res(json.dumps({"error": "no cron module"}))])
        backend, jobs, error = self._live(runner, cli=None, venv_python=python)
        self.assertEqual(backend, "local_api")
        self.assertEqual(jobs, ())
        self.assertEqual(error, "local_api: no cron module")

    def test_local_api_non_list_and_nonzero(self):
        python = Path("/venv/python")
        backend, _jobs, error = self._live(
            RecordingRunner([res('{"ok": true}')]), cli=None, venv_python=python
        )
        self.assertEqual(error, "local_api: unexpected cron output")
        backend, _jobs, error = self._live(
            RecordingRunner([res(rc=4, stderr="remove failed")]),
            cli=None, venv_python=python,
        )
        self.assertIn("rc=4", error)
        self.assertIn("remove failed", error)

    def test_local_api_filters_non_dict_jobs(self):
        python = Path("/venv/python")
        runner = RecordingRunner([
            res(json.dumps([{"id": 1, "name": 2}, "junk", {"name": "n"}]))
        ])
        _backend, jobs, _error = self._live(runner, cli=None, venv_python=python)
        self.assertEqual(
            jobs,
            (install.ManagedCron("1", "2"), install.ManagedCron("", "n")),
        )

    def test_both_backends_fail_join_errors(self):
        python = Path("/venv/python")
        runner = RecordingRunner([
            res("rm"),  # help ok
            res(rc=1, stderr="cli-list-down", argv=("/usr/bin/hermes", "cron", "list", "--all")),
            res(rc=3, stderr="local-down", argv=("/venv/python", "-c", "<snippet>")),
        ])
        backend, jobs, error = self._live(runner, venv_python=python)
        self.assertEqual(backend, "local_api")
        self.assertEqual(jobs, ())
        self.assertEqual(
            error,
            "cli: /usr/bin/hermes cron list --all (rc=1): cli-list-down; "
            "local_api: /venv/python -c <snippet> (rc=3): local-down",
        )
        self.assertIn("local_api: ", error)
        self.assertIn("local-down", error)
        self.assertIn("; ", error)

    def test_no_backend(self):
        backend, jobs, error = self._live(
            RecordingRunner([]), cli=None, venv_python=None
        )
        self.assertEqual((backend, jobs, error), ("none", (), ""))


# ---------------------------------------------------------------------------
# OpenClaw 解析辅助
# ---------------------------------------------------------------------------


class OpenClawHelperTest(unittest.TestCase):
    def test_find_matching_node_recursive(self):
        data = {"a": [{"x": {"id": "target"}}], "b": {"name": "other"}}
        self.assertTrue(install._find_matching_node(data, ("id", "name"), "target"))
        self.assertTrue(install._find_matching_node([{"pluginId": "target"}],
                                                    ("id", "name", "pluginId"), "target"))
        self.assertFalse(install._find_matching_node(data, ("id",), "missing"))
        self.assertFalse(install._find_matching_node("target", ("id",), "target"))

    def test_plugin_in_json(self):
        self.assertTrue(
            install._openclaw_plugin_in_json(
                json.dumps({"plugins": [{"id": install.OPENCLAW_PLUGIN_ID}]})
            )
        )
        # 非 JSON 时退回子串判断
        self.assertTrue(
            install._openclaw_plugin_in_json(
                f"noise\n{install.OPENCLAW_PLUGIN_ID}\n"
            )
        )
        self.assertFalse(install._openclaw_plugin_in_json("{}"))
        self.assertFalse(install._openclaw_plugin_in_json(""))

    def test_cron_jobs_shapes(self):
        jobs = [{"id": "1"}, {"name": "n"}]
        self.assertEqual(install._openclaw_cron_jobs(json.dumps(jobs)), jobs)
        self.assertEqual(install._openclaw_cron_jobs(json.dumps({"jobs": jobs})), jobs)
        self.assertEqual(install._openclaw_cron_jobs(json.dumps({"data": jobs})), jobs)
        self.assertEqual(install._openclaw_cron_jobs(json.dumps({"items": jobs})), jobs)
        self.assertEqual(
            install._openclaw_cron_jobs(json.dumps({"data": {"jobs": jobs}})), jobs
        )
        self.assertEqual(install._openclaw_cron_jobs(json.dumps({"jobs": ["x", 1, jobs[0]]})),
                         [jobs[0]])
        self.assertIsNone(install._openclaw_cron_jobs("noise"))
        self.assertIsNone(install._openclaw_cron_jobs("{}"))
        self.assertIsNone(install._openclaw_cron_jobs(json.dumps({"data": "x"})))

    def test_manifests_both_glob_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            first = state / "extensions" / "a" / "openclaw.plugin.json"
            second = state / "plugins" / "b" / "openclaw.plugin.json"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("{}", encoding="utf-8")
            second.write_text("{}", encoding="utf-8")
            self.assertEqual(
                sorted(str(p) for p in install._iter_openclaw_plugin_manifests(state)),
                sorted([str(first), str(second)]),
            )

    def test_installed_tools_priority_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            manifest = state / "extensions" / "a" / "openclaw.plugin.json"
            manifest.parent.mkdir(parents=True)
            self.assertIsNone(install._installed_openclaw_tools(state))
            manifest.write_text(json.dumps({"id": "other", "contracts": {"tools": ["x"]}}),
                                encoding="utf-8")
            self.assertIsNone(install._installed_openclaw_tools(state))
            manifest.write_text(
                json.dumps({"id": install.OPENCLAW_PLUGIN_ID, "contracts": {"tools": [1]}}),
                encoding="utf-8",
            )
            self.assertIsNone(install._installed_openclaw_tools(state))
            manifest.write_text(
                json.dumps({"id": install.OPENCLAW_PLUGIN_ID,
                             "contracts": {"tools": ["t1", "t2"]}}),
                encoding="utf-8",
            )
            self.assertEqual(install._installed_openclaw_tools(state), ("t1", "t2"))

    def test_plugin_record_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            runner = RecordingRunner([])
            self.assertFalse(install._openclaw_plugin_record(state_dir=state,
                                                              cli=None, run=runner))
            installs = state / "plugins" / "installs.json"
            installs.parent.mkdir(parents=True)
            installs.write_text(f'["{install.OPENCLAW_PLUGIN_ID}"]', encoding="utf-8")
            self.assertTrue(install._openclaw_plugin_record(state_dir=state,
                                                             cli=None, run=runner))
            installs.unlink()
            manifest = state / "extensions" / "p" / "openclaw.plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"id": install.OPENCLAW_PLUGIN_ID}),
                                encoding="utf-8")
            self.assertTrue(install._openclaw_plugin_record(state_dir=state,
                                                             cli=None, run=runner))
            manifest.unlink()

            cli = "/usr/bin/openclaw"
            ok_runner = RecordingRunner([
                res(json.dumps({"plugins": [{"id": install.OPENCLAW_PLUGIN_ID}]}))
            ])
            self.assertTrue(install._openclaw_plugin_record(state_dir=state,
                                                             cli=cli, run=ok_runner))
            self.assertEqual(ok_runner.calls[0]["argv"],
                             [cli, "plugins", "list", "--json"])
            self.assertEqual(ok_runner.calls[0]["timeout"], 60)
            fail_runner = RecordingRunner([
                install.CommandFailed("k", "list", detail="boom")
            ])
            self.assertFalse(install._openclaw_plugin_record(state_dir=state,
                                                              cli=cli, run=fail_runner))
            rc_runner = RecordingRunner([res(rc=1)])
            self.assertFalse(install._openclaw_plugin_record(state_dir=state,
                                                              cli=cli, run=rc_runner))


class OpenClawToolNamesTest(unittest.TestCase):
    def _resolve(self, *, state_dir, script_dir, cli=None, runner=None):
        return install._resolve_openclaw_tool_names(
            state_dir=state_dir, script_dir=script_dir, cli=cli,
            run=runner or RecordingRunner([]),
        )

    def test_installed_manifest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            manifest = state / "extensions" / "p" / "openclaw.plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"id": install.OPENCLAW_PLUGIN_ID,
                             "contracts": {"tools": ["a", "b"]}}),
                encoding="utf-8",
            )
            self.assertEqual(
                self._resolve(state_dir=state, script_dir=root / "scripts"),
                (("a", "b"), "installed_manifest"),
            )

    def test_inspect_second_and_empty_contracts_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            scripts = root / "scripts"
            scripts.mkdir()
            cli = "/usr/bin/openclaw"
            runner = RecordingRunner([
                res(json.dumps({"contracts": {"tools": ["c"]}})),
            ])
            self.assertEqual(
                self._resolve(state_dir=state, script_dir=scripts, cli=cli, runner=runner),
                (("c",), "inspect"),
            )
            self.assertEqual(
                runner.calls[0]["argv"],
                [cli, "plugins", "inspect", install.OPENCLAW_PLUGIN_ID, "--json"],
            )
            self.assertEqual(runner.calls[0]["timeout"], 60)
            empty_runner = RecordingRunner([
                res(json.dumps({"contracts": {"tools": []}})),
            ])
            self.assertEqual(
                self._resolve(state_dir=state, script_dir=scripts, cli=cli,
                              runner=empty_runner),
                (install.FALLBACK_OPENCLAW_TOOLS, "static_fallback"),
            )

    def test_inspect_failure_falls_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            runner = RecordingRunner([install.CommandFailed("k", "inspect", detail="x")])
            self.assertEqual(
                self._resolve(state_dir=root / "state", script_dir=scripts,
                              cli="/usr/bin/openclaw", runner=runner),
                (install.FALLBACK_OPENCLAW_TOOLS, "static_fallback"),
            )

    def test_repo_manifest_priorities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            repo_manifest = root / "plugins" / "openclaw" / "openclaw.plugin.json"
            repo_manifest.parent.mkdir(parents=True)
            repo_manifest.write_text(json.dumps({"contracts": {"tools": ["r"]}}),
                                     encoding="utf-8")
            self.assertEqual(
                self._resolve(state_dir=root / "state", script_dir=scripts),
                (("r",), "openclaw.plugin.json"),
            )
            repo_manifest.unlink()
            dist = root / "dist" / "manifest.json"
            dist.parent.mkdir()
            dist.write_text(json.dumps({"tools": ["d"]}), encoding="utf-8")
            self.assertEqual(
                self._resolve(state_dir=root / "state", script_dir=scripts),
                (("d",), "dist/manifest.json"),
            )
            dist.unlink()
            local = scripts / "manifest.json"
            local.write_text(json.dumps({"tools": ["m"]}), encoding="utf-8")
            self.assertEqual(
                self._resolve(state_dir=root / "state", script_dir=scripts),
                (("m",), "manifest.json"),
            )
            local.write_text(json.dumps({"tools": []}), encoding="utf-8")
            self.assertEqual(
                self._resolve(state_dir=root / "state", script_dir=scripts),
                (install.FALLBACK_OPENCLAW_TOOLS, "static_fallback"),
            )

    def test_config_tools_in_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openclaw.json"
            self.assertEqual(install._config_tools_in_allow(path), ())
            path.write_text("[1]", encoding="utf-8")
            self.assertEqual(install._config_tools_in_allow(path), ())
            path.write_text('{"tools": []}', encoding="utf-8")
            self.assertEqual(install._config_tools_in_allow(path), ())
            path.write_text('{"tools": {"alsoAllow": "x"}}', encoding="utf-8")
            self.assertEqual(install._config_tools_in_allow(path), ())
            path.write_text('{"tools": {"alsoAllow": ["a", 2]}}', encoding="utf-8")
            self.assertEqual(install._config_tools_in_allow(path), ("a", "2"))


# ---------------------------------------------------------------------------
# 文本编辑器：最小改动 + 事务
# ---------------------------------------------------------------------------


class TextFileEditorTest(unittest.TestCase):
    def test_read_returns_text_and_keepends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_text("a\nb\r\nc", encoding="utf-8")
            editor = install.TextFileEditor(path)
            self.assertEqual(
                editor.read(), ("a\nb\r\nc", ["a\n", "b\r\n", "c"])
            )

    def test_read_follows_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.write_text("data", encoding="utf-8")
            link = root / "link"
            link.symlink_to(real)
            self.assertEqual(install.TextFileEditor(link).read(), ("data", ["data"]))

    def test_commit_rejects_unplanned_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_text("a\nb\nc\n", encoding="utf-8")
            editor = install.TextFileEditor(path)
            with self.assertRaises(install.MalformedFile) as ctx:
                editor.commit(["a\n", "B\n", "c\n"], remove_indices=(1,))
            self.assertEqual(
                ctx.exception.key,
                "uninstall.detail.plan_mismatch",
            )
            self.assertEqual(ctx.exception.target, str(path))
            self.assertEqual(
                ctx.exception.detail, "planned content differs from on-disk content"
            )
            self.assertIs(ctx.exception.status, STATUS.FAILED)
            self.assertEqual(path.read_text(encoding="utf-8"), "a\nb\nc\n")

    def test_commit_with_remove_indices(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_text("a\nb\nc\n", encoding="utf-8")
            path.chmod(0o640)
            install.TextFileEditor(path).commit(["a\n", "c\n"], remove_indices=(1,))
            self.assertEqual(path.read_text(encoding="utf-8"), "a\nc\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_commit_through_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.write_text("a\nb\n", encoding="utf-8")
            link = root / "link"
            link.symlink_to(real)
            install.TextFileEditor(link).commit(["a\n"], remove_indices=(1,))
            self.assertTrue(link.is_symlink())
            self.assertEqual(real.read_text(encoding="utf-8"), "a\n")


class EnvFileCleanerTest(unittest.TestCase):
    def _cleaner(self, tmp, content):
        path = Path(tmp) / ".env"
        path.write_text(content, encoding="utf-8")
        return path, install.EnvFileCleaner(path, i18n=I18N)

    def test_plan_exact_indices_and_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp,
                "# c\n"
                "API_SERVER_KEY=managed\n"
                "API_SERVER_KEY=user\n"
                "MILOCO_HOME=/managed\n"
                "MILOCO_HOME=/elsewhere\n"
                "OTHER=1\n"
                "MILOCO_HOME=\n",
            )
            plan = cleaner.plan(
                managed_bearers={"managed"},
                managed_homes={Path("/managed")},
            )
            self.assertEqual(plan.remove, (1, 3))
            self.assertEqual(
                plan.kept,
                (
                    (2, "uninstall.detail.kept_api_key"),
                    (4, "uninstall.detail.kept_env_home"),
                    (6, "uninstall.detail.kept_env_home"),
                ),
            )
            self.assertEqual(plan.removed_keys, ("API_SERVER_KEY", "MILOCO_HOME"))
            self.assertEqual(len(plan.lines), 7)
            self.assertEqual(plan.lines[0], "# c\n")

    def test_plan_quotes_and_spacing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _path, cleaner = self._cleaner(
                tmp, '  export API_SERVER_KEY = "managed"  \n'
            )
            plan = cleaner.plan(managed_bearers={"managed"}, managed_homes=set())
            self.assertEqual(plan.remove, (0,))
            self.assertEqual(plan.removed_keys, ("API_SERVER_KEY",))

    def test_apply_empty_file_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("", encoding="utf-8")
            cleaner = install.EnvFileCleaner(path, i18n=I18N)
            plan = cleaner.plan(managed_bearers=set(), managed_homes=set())
            self.assertEqual(
                cleaner.apply(plan),
                make_item("uninstall.hermes.env", "hermes", str(path), STATUS.ABSENT),
            )

    def test_apply_kept_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp, "API_SERVER_KEY=user\nMILOCO_HOME=/elsewhere\n"
            )
            plan = cleaner.plan(managed_bearers={"managed"},
                                managed_homes={Path("/managed")})
            expected_detail = (
                I18N.t("uninstall.detail.kept_api_key")
                + "; "
                + I18N.t("uninstall.detail.kept_env_home")
            )
            self.assertEqual(
                cleaner.apply(plan),
                make_item("uninstall.hermes.env", "hermes", str(path), STATUS.KEPT,
                          expected_detail),
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "API_SERVER_KEY=user\nMILOCO_HOME=/elsewhere\n",
            )

    def test_apply_no_managed_lines_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(tmp, "OTHER=1\n")
            plan = cleaner.plan(managed_bearers=set(), managed_homes=set())
            self.assertEqual(
                cleaner.apply(plan),
                make_item("uninstall.hermes.env", "hermes", str(path), STATUS.ABSENT),
            )

    def test_apply_removes_only_matching_lines_and_keeps_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp,
                "# keep\n"
                "API_SERVER_KEY=managed\n"
                "API_SERVER_KEY=user\n"
                "MILOCO_HOME=/managed\n",
            )
            path.chmod(0o600)
            plan = cleaner.plan(managed_bearers={"managed"},
                                managed_homes={Path("/managed")})
            expected_detail = (
                "API_SERVER_KEY, MILOCO_HOME"
                + " · "
                + I18N.t("uninstall.detail.kept_api_key")
            )
            self.assertEqual(
                cleaner.apply(plan),
                make_item("uninstall.hermes.env", "hermes", str(path), STATUS.DONE,
                          expected_detail),
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "# keep\nAPI_SERVER_KEY=user\n",
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class ShellRcCleanerTest(unittest.TestCase):
    def _cleaner(self, tmp, content):
        path = Path(tmp) / ".zshrc"
        path.write_text(content, encoding="utf-8")
        return path, install.ShellRcCleaner(path, i18n=I18N)

    def test_marker_with_managed_export_removes_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp,
                "# miloco runtime path\n"
                "export MILOCO_HOME=/managed\n"
                "export PATH=x\n",
            )
            plan = cleaner.plan(managed_homes={Path("/managed")})
            self.assertEqual(plan.remove, (0, 1))
            self.assertEqual(plan.kept_exports, ())
            self.assertTrue(plan.marker_found)
            self.assertEqual(
                cleaner.apply(plan),
                make_item("uninstall.hermes.shell_rc", "hermes", str(path), STATUS.DONE),
            )
            self.assertEqual(path.read_text(encoding="utf-8"), "export PATH=x\n")

    def test_marker_with_foreign_export_keeps_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp,
                "# miloco runtime path\n"
                "export MILOCO_HOME=/user\n",
            )
            plan = cleaner.plan(managed_homes={Path("/managed")})
            self.assertEqual(plan.remove, (0,))
            self.assertEqual(plan.kept_exports, ((1, Path("/user")),))
            self.assertTrue(plan.marker_found)
            expected = make_item(
                "uninstall.hermes.shell_rc", "hermes", str(path), STATUS.DONE,
                I18N.t("uninstall.detail.kept_export", "/user"),
            )
            self.assertEqual(cleaner.apply(plan), expected)
            self.assertEqual(
                path.read_text(encoding="utf-8"), "export MILOCO_HOME=/user\n"
            )

    def test_marker_without_following_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp, "# miloco runtime path\n# next\n"
            )
            plan = cleaner.plan(managed_homes={Path("/m")})
            self.assertEqual(plan.remove, (0,))
            self.assertEqual(plan.kept_exports, ())
            self.assertTrue(plan.marker_found)
            self.assertEqual(
                cleaner.apply(plan).status, STATUS.DONE
            )

    def test_marker_at_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(tmp, "x\n# miloco runtime path")
            plan = cleaner.plan(managed_homes=set())
            self.assertEqual(plan.remove, (1,))
            self.assertTrue(plan.marker_found)

    def test_markerless_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp,
                "export MILOCO_HOME=/managed\n"
                "export MILOCO_HOME=/user\n"
                "export MILOCO_HOME=\n"
                "  export  MILOCO_HOME = /managed  \n",
            )
            plan = cleaner.plan(managed_homes={Path("/managed")})
            self.assertEqual(plan.remove, (0, 3))
            self.assertEqual(plan.kept_exports, ((1, Path("/user")),))
            self.assertFalse(plan.marker_found)

    def test_marker_regex_requires_exact_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp,
                "# miloco(MILOCO_HOME=/m)\n"
                "#  miloco (MILOCO_HOME=/m)\n"
                "# miloco runtime path trailing\n",
            )
            plan = cleaner.plan(managed_homes={Path("/m")})
            self.assertEqual(plan.remove, ())
            self.assertFalse(plan.marker_found)

    def test_apply_without_removal_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(tmp, "export MILOCO_HOME=/user\n")
            plan = cleaner.plan(managed_homes=set())
            self.assertEqual(
                cleaner.apply(plan),
                make_item("uninstall.hermes.shell_rc", "hermes", str(path), STATUS.ABSENT),
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"), "export MILOCO_HOME=/user\n"
            )

    def test_apply_empty_file_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text("", encoding="utf-8")
            cleaner = install.ShellRcCleaner(path, i18n=I18N)
            plan = cleaner.plan(managed_homes=set())
            self.assertTrue(plan.marker_found is False)
            self.assertEqual(
                cleaner.apply(plan),
                make_item("uninstall.hermes.shell_rc", "hermes", str(path), STATUS.ABSENT),
            )


class ConfigJsonCleanerTest(unittest.TestCase):
    def _cleaner(self, tmp, payload):
        path = Path(tmp) / "config.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return path, install.ConfigJsonCleaner(path, i18n=I18N)

    def test_plan_parse_error_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{", encoding="utf-8")
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            with self.assertRaises(install.MalformedFile) as ctx:
                cleaner.plan(clear_platform=True)
            self.assertEqual(ctx.exception.key, "uninstall.detail.json_parse")
            self.assertEqual(ctx.exception.target, str(path))
            self.assertTrue(ctx.exception.detail.startswith("Expecting"))
            self.assertIs(ctx.exception.status, STATUS.FAILED)
            self.assertEqual(path.read_text(encoding="utf-8"), "{")

    def test_plan_non_object_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(tmp, "[1, 2]")
            with self.assertRaises(install.MalformedFile) as ctx:
                cleaner.plan(clear_platform=True)
            self.assertEqual(ctx.exception.detail, "top-level JSON is not an object")
            self.assertEqual(ctx.exception.target, str(path))

    def test_plan_changed_only_for_hermes_and_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _path, cleaner = self._cleaner(
                tmp, {"agent": {"platform": "hermes", "k": 1}, "other": {"x": 2}}
            )
            plan = cleaner.plan(clear_platform=True)
            self.assertTrue(plan.changed)
            self.assertEqual(plan.current_platform, "hermes")
            self.assertEqual(plan.before, {"agent": {"platform": "hermes", "k": 1},
                                           "other": {"x": 2}})
            self.assertEqual(plan.after["agent"]["platform"], "")
            self.assertEqual(plan.after["agent"]["k"], 1)
            self.assertEqual(plan.after["other"], {"x": 2})
            # before 必须是深拷贝：改 after 的嵌套值不能影响 before
            plan.after["other"]["x"] = 99
            self.assertEqual(plan.before["other"], {"x": 2})
            plan.after["agent"]["k"] = 99
            self.assertEqual(plan.before["agent"]["k"], 1)

            plan = cleaner.plan(clear_platform=False)
            self.assertFalse(plan.changed)
            self.assertEqual(plan.after["agent"]["platform"], "hermes")

    def test_plan_other_platform_and_missing_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            _path, cleaner = self._cleaner(tmp, {"agent": {"platform": "openclaw"}})
            plan = cleaner.plan(clear_platform=True)
            self.assertFalse(plan.changed)
            self.assertEqual(plan.current_platform, "openclaw")
            _path, cleaner = self._cleaner(tmp, {"agent": []})
            plan = cleaner.plan(clear_platform=True)
            self.assertEqual(plan.current_platform, "")
            self.assertEqual(plan.after, {"agent": []})

    def test_apply_kept_and_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(tmp, {"agent": {"platform": "openclaw"}})
            self.assertEqual(
                cleaner.apply(cleaner.plan(clear_platform=True)),
                make_item(
                    "uninstall.hermes.agent_platform", "hermes", str(path),
                    STATUS.KEPT,
                    I18N.t("uninstall.detail.platform_not_hermes", "openclaw"),
                ),
            )
            _path, cleaner = self._cleaner(tmp, {"agent": {}})
            self.assertEqual(
                cleaner.apply(cleaner.plan(clear_platform=True)),
                make_item("uninstall.hermes.agent_platform", "hermes", str(path),
                          STATUS.ABSENT),
            )

    def test_apply_rewrites_exactly_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp,
                json.dumps({"agent": {"platform": "hermes"}, "name": "小米"},
                           ensure_ascii=False) + "\n",
            )
            path.chmod(0o600)
            plan = cleaner.plan(clear_platform=True)
            self.assertEqual(
                cleaner.apply(plan),
                make_item("uninstall.hermes.agent_platform", "hermes", str(path),
                          STATUS.DONE),
            )
            expected = json.dumps(
                {"agent": {"platform": ""}, "name": "小米"},
                indent=2, ensure_ascii=False,
            ) + "\n"
            self.assertEqual(path.read_text(encoding="utf-8"), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_apply_without_original_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = '{"agent": {"platform": "hermes"}}'
            path, cleaner = self._cleaner(tmp, payload)
            cleaner.apply(cleaner.plan(clear_platform=True))
            self.assertFalse(path.read_text(encoding="utf-8").endswith("\n"))

    def test_apply_rejects_unexpected_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, cleaner = self._cleaner(
                tmp, {"agent": {"platform": "hermes"}, "keep": 1}
            )
            plan = cleaner.plan(clear_platform=True)
            tampered = install.JsonPlan(
                before=plan.before,
                after={**plan.after, "keep": 2},
                changed=True,
                current_platform=plan.current_platform,
            )
            with self.assertRaises(install.MalformedFile) as ctx:
                cleaner.apply(tampered)
            self.assertEqual(ctx.exception.key, "uninstall.detail.plan_mismatch")
            self.assertEqual(ctx.exception.target, str(path))
            self.assertEqual(ctx.exception.detail, "only agent.platform may change")
            self.assertIn('"keep": 1', path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# build_inventory：只读盘点与痕迹判定
# ---------------------------------------------------------------------------


class BuildInventoryTest(unittest.TestCase):
    def _build(self, root, *, env=None, which=None, runner=None, hint=""):
        home = root / "user"
        home.mkdir(exist_ok=True)
        return install.build_inventory(
            env=env or {},
            home=home,
            agent_platform_hint=hint,
            which=which or (lambda _name: None),
            run=runner or RecordingRunner([]),
        ), home

    def test_empty_environment_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv, home = self._build(Path(tmp))
            self.assertFalse(inv.hermes.detected)
            self.assertFalse(inv.openclaw.detected)
            self.assertIsNone(inv.hermes.cli)
            self.assertIsNone(inv.hermes.venv_python)
            self.assertEqual(inv.hermes.plugin_dir,
                             home / ".hermes" / "plugins" / install.HERMES_PLUGIN_ID)
            self.assertEqual(inv.hermes.skills, ())
            self.assertEqual(inv.hermes.config_yaml, home / ".hermes" / "config.yaml")
            self.assertFalse(inv.hermes.config_mentions_plugin)
            self.assertEqual(inv.hermes.env_file, home / ".hermes" / ".env")
            self.assertEqual(inv.hermes.env_managed_bearers, ())
            self.assertEqual(inv.hermes.env_miloco_home_lines, ())
            self.assertEqual(inv.hermes.rc_files, ())
            self.assertEqual(inv.hermes.cron_backend, "none")
            self.assertEqual(inv.hermes.managed_crons, ())
            self.assertIsNone(inv.hermes.cli_probe_error)
            self.assertIsNone(inv.openclaw.cli)
            self.assertEqual(inv.openclaw.state_dir, home / ".openclaw")
            self.assertEqual(inv.openclaw.config_path, home / ".openclaw" / "openclaw.json")
            self.assertEqual(inv.openclaw.plugin_id, install.OPENCLAW_PLUGIN_ID)
            self.assertFalse(inv.openclaw.plugin_record)
            self.assertEqual(inv.openclaw.tools_in_also_allow, ())
            self.assertEqual(inv.openclaw.managed_crons, ())
            self.assertIsNone(inv.openclaw.cli_probe_error)
            # 两个默认 home 都是候选；没有存在的候选时 primary 取排序第一个
            self.assertEqual(len(inv.candidates), 2)
            self.assertEqual(inv.primary_home, inv.candidates[0].path)
            self.assertEqual(inv.primary_home, home / ".hermes" / "miloco")

    def test_hermes_plugin_dir_detects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / "user" / ".hermes" / "plugins" / install.HERMES_PLUGIN_ID
            plugin.mkdir(parents=True)
            inv, _home = self._build(root)
            self.assertTrue(inv.hermes.detected)

    def test_hermes_skills_detects_and_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "user" / ".hermes" / "skills"
            (skills / "miloco-x").mkdir(parents=True)
            (skills / "other").mkdir()
            inv, _home = self._build(root)
            self.assertTrue(inv.hermes.detected)
            self.assertEqual(inv.hermes.skills, (skills / "miloco-x",))

    def test_hermes_detected_from_candidate_artifacts(self):
        for marker in ("adapter", "cache", "platform"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                miloco = root / "user" / ".hermes" / "miloco"
                miloco.mkdir(parents=True)
                if marker == "adapter":
                    (miloco / "agent_platform" / "hermes").mkdir(parents=True)
                elif marker == "cache":
                    (miloco / ".install-cache" / "hermes-plugin").mkdir(parents=True)
                else:
                    (miloco / "config.json").write_text(
                        json.dumps({"agent": {"platform": "hermes"}}), encoding="utf-8"
                    )
                inv, _home = self._build(root)
                self.assertTrue(inv.hermes.detected, marker)
                candidate = [c for c in inv.candidates if c.path == miloco][0]
                self.assertEqual(candidate.agent_platform,
                                 "hermes" if marker == "platform" else "")
                self.assertEqual(candidate.has_hermes_adapter, marker == "adapter")
                self.assertEqual(candidate.has_install_cache, marker == "cache")

    def test_hermes_env_bearer_and_home_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            miloco = root / "user" / ".hermes" / "miloco"
            miloco.mkdir(parents=True)
            (miloco / "config.json").write_text(
                json.dumps({"agent": {"auth_bearer": "BEAR"}}), encoding="utf-8"
            )
            (root / "user" / ".hermes" / ".env").write_text(
                "API_SERVER_KEY=BEAR\n"
                "API_SERVER_KEY=OTHER\n"
                f"MILOCO_HOME={miloco}\n",
                encoding="utf-8",
            )
            inv, _home = self._build(root)
            self.assertEqual(inv.hermes.env_managed_bearers, ("BEAR",))
            self.assertEqual(inv.hermes.env_miloco_home_lines, (str(miloco),))
            self.assertTrue(inv.hermes.detected)

    def test_hermes_rc_files_and_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "user"
            home.mkdir()
            (home / ".zshrc").write_text("# normal\n", encoding="utf-8")
            (home / ".bashrc").write_text(
                "# miloco runtime path\n", encoding="utf-8"
            )
            inv, _home = self._build(root)
            self.assertEqual(inv.hermes.rc_files, (home / ".zshrc", home / ".bashrc"))
            self.assertTrue(inv.hermes.detected)

    def test_hermes_config_yaml_substring_detects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "user" / ".hermes" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("# mention miloco in a comment\n", encoding="utf-8")
            inv, _home = self._build(root)
            self.assertTrue(inv.hermes.config_mentions_plugin)
            self.assertTrue(inv.hermes.detected)

    def test_hermes_cli_cron_backend_and_managed_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingRunner([
                res("usage: rm"),
                res(
                    "  a1 [active]\n"
                    f"    Name:  {TAG} managed\n"
                    "  b2 [paused]\n"
                    "    Name:  user-task\n"
                ),
            ])
            inv, _home = self._build(
                root, which=lambda name: "/usr/bin/hermes" if name == "hermes" else None,
                runner=runner,
            )
            self.assertEqual(inv.hermes.cli, "/usr/bin/hermes")
            self.assertEqual(inv.hermes.cron_backend, "cli")
            self.assertEqual(
                inv.hermes.managed_crons,
                (install.ManagedCron("a1", f"{TAG} managed", f"{TAG} managed"),),
            )
            self.assertTrue(inv.hermes.detected)
            self.assertEqual(runner.calls[0]["argv"][:3], ["/usr/bin/hermes", "cron", "--help"])

    def test_hermes_cron_probe_error_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingRunner([
                res("usage: rm"),
                res(rc=1, stderr="gateway down",
                    argv=("/usr/bin/hermes", "cron", "list", "--all")),
            ])
            inv, _home = self._build(
                root, which=lambda name: "/usr/bin/hermes" if name == "hermes" else None,
                runner=runner,
            )
            self.assertEqual(inv.hermes.managed_crons, ())
            self.assertEqual(
                inv.hermes.cli_probe_error,
                "cli: /usr/bin/hermes cron list --all (rc=1): gateway down",
            )

    def test_hermes_local_venv_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "user" / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
            runner = RecordingRunner([
                res(json.dumps([{"id": "j1", "name": f"{TAG} x"}])),
            ])
            inv, _home = self._build(root, runner=runner)
            self.assertEqual(inv.hermes.cron_backend, "local_api")
            self.assertEqual(inv.hermes.managed_crons,
                             (install.ManagedCron("j1", f"{TAG} x"),))
            self.assertEqual(runner.calls[0]["argv"],
                             [str(python), "-c", install._HERMES_CRON_LIST_SNIPPET])

    def test_openclaw_detection_matrix(self):
        cases = ("manifest", "installs", "plugin_id_in_config", "tool_in_config",
                 "tool_in_allow")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / "user"
                home.mkdir()
                state = home / ".openclaw"
                state.mkdir()
                config = state / "openclaw.json"
                if case == "manifest":
                    manifest = state / "extensions" / "p" / "openclaw.plugin.json"
                    manifest.parent.mkdir(parents=True)
                    manifest.write_text(json.dumps({
                        "id": install.OPENCLAW_PLUGIN_ID,
                        "contracts": {"tools": ["t1"]},
                    }), encoding="utf-8")
                elif case == "installs":
                    installs = state / "plugins" / "installs.json"
                    installs.parent.mkdir(parents=True)
                    installs.write_text(f'["{install.OPENCLAW_PLUGIN_ID}"]', encoding="utf-8")
                elif case == "plugin_id_in_config":
                    config.write_text(json.dumps({"x": install.OPENCLAW_PLUGIN_ID}),
                                      encoding="utf-8")
                elif case == "tool_in_config":
                    # 工具名来自静态兜底（无 manifest、无 CLI inspect）
                    config.write_text(json.dumps({"notes": install.FALLBACK_OPENCLAW_TOOLS[0]}),
                                      encoding="utf-8")
                else:
                    config.write_text(json.dumps({
                        "tools": {"alsoAllow": [install.FALLBACK_OPENCLAW_TOOLS[0], "keep"]}
                    }), encoding="utf-8")
                inv, _home = self._build(root)
                self.assertTrue(inv.openclaw.detected, case)
                if case == "tool_in_allow":
                    self.assertEqual(
                        inv.openclaw.tools_in_also_allow,
                        (install.FALLBACK_OPENCLAW_TOOLS[0], "keep"),
                    )

    def test_openclaw_cli_cron_and_probe_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "user" / ".openclaw"
            state.mkdir(parents=True)
            installs = state / "plugins" / "installs.json"
            installs.parent.mkdir(parents=True)
            installs.write_text(f'["{install.OPENCLAW_PLUGIN_ID}"]', encoding="utf-8")
            manifest = state / "extensions" / "p" / "openclaw.plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({
                "id": install.OPENCLAW_PLUGIN_ID,
                "contracts": {"tools": ["t1"]},
            }), encoding="utf-8")
            runner = RecordingRunner([
                res(json.dumps({"jobs": [
                    {"id": "1", "name": "n1", "description": f"{TAG} a"},
                    {"jobId": "2", "description": "user"},
                    {"name": "n3", "description": f"x {TAG} b"},
                ]})),
            ])
            inv, _home = self._build(
                root, which=lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
                runner=runner,
            )
            self.assertEqual(
                inv.openclaw.managed_crons,
                (install.ManagedCron("1", "n1", f"{TAG} a"),
                 install.ManagedCron("n3", "n3", f"x {TAG} b")),
            )
            self.assertEqual(
                runner.calls[0]["argv"],
                ["/usr/bin/openclaw", "cron", "list", "--all", "--json"],
            )
            self.assertEqual(runner.calls[0]["timeout"], 30)

            failing = RecordingRunner([res(rc=5, stderr="gateway down",
                                           argv=("/usr/bin/openclaw", "cron", "list",
                                                 "--all", "--json"))])
            inv, _home = self._build(
                root, which=lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
                runner=failing,
            )
            self.assertEqual(inv.openclaw.managed_crons, ())
            self.assertEqual(
                inv.openclaw.cli_probe_error,
                "/usr/bin/openclaw cron list --all --json (rc=5): gateway down",
            )

    def test_primary_home_prefers_existing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custom = root / "custom"
            custom.mkdir()
            inv, home = self._build(root, env={"MILOCO_HOME": str(custom)})
            self.assertEqual(inv.primary_home, custom)
            self.assertEqual(inv.candidates[0].sources, frozenset({"env"}))

    def test_hint_only_changes_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv, home = self._build(root, hint="openclaw")
            paths = [c.path for c in inv.candidates]
            self.assertEqual(paths[0], home / ".openclaw" / "miloco")
            self.assertIn(home / ".hermes" / "miloco", paths)


# ---------------------------------------------------------------------------
# _PlatformCleaner 单步模板
# ---------------------------------------------------------------------------


class _StepCleaner(install._PlatformCleaner):
    platform = "hermes"

    def __init__(self):
        self.report = install.PlatformReport("hermes", True, True)


class PlatformStepTest(unittest.TestCase):
    def test_single_item_and_list_and_none(self):
        cleaner = _StepCleaner()
        item = make_item("k", "hermes", "t", STATUS.DONE)
        cleaner._step("fallback", "fallback-target", lambda: item)
        cleaner._step("fallback", "fallback-target", lambda: [item,
                                                             make_item("k2", "hermes", "t2",
                                                                       STATUS.ABSENT)])
        cleaner._step("fallback", "fallback-target", lambda: None)
        self.assertEqual(
            cleaner.report.items,
            [item, item, make_item("k2", "hermes", "t2", STATUS.ABSENT)],
        )

    def test_uninstall_error_fields(self):
        cleaner = _StepCleaner()
        def boom():
            raise install.CliNotFound("exc.key", "exc.target", detail="why",
                                      remediation="fix it")
        cleaner._step("fallback", "fallback-target", boom)
        self.assertEqual(
            cleaner.report.items,
            [make_item("exc.key", "hermes", "exc.target", STATUS.BLOCKED,
                       "why", "fix it")],
        )
        self.assertTrue(cleaner.report.incomplete)

    def test_uninstall_error_empty_fields_fall_back(self):
        cleaner = _StepCleaner()
        def boom():
            raise install.UninstallError("", "", detail="d")
        cleaner._step("fallback.key", "fallback-target", boom)
        self.assertEqual(
            cleaner.report.items,
            [make_item("fallback.key", "hermes", "fallback-target", STATUS.FAILED, "d", "")],
        )

    def test_ownership_uncertain_is_kept(self):
        cleaner = _StepCleaner()
        def boom():
            raise install.OwnershipUncertain("k", "t", detail="uncertain")
        cleaner._step("fallback", "t", boom)
        self.assertEqual(cleaner.report.items[0].status, STATUS.KEPT)
        self.assertFalse(cleaner.report.incomplete)

    def test_unexpected_exception_is_isolated(self):
        cleaner = _StepCleaner()
        def boom():
            raise ValueError("bad thing")
        cleaner._step("k", "target", boom)
        cleaner._step("k2", "t2", lambda: make_item("k2", "hermes", "t2", STATUS.DONE))
        self.assertEqual(
            cleaner.report.items,
            [make_item("k", "hermes", "target", STATUS.FAILED, "ValueError('bad thing')"),
             make_item("k2", "hermes", "t2", STATUS.DONE)],
        )


# ---------------------------------------------------------------------------
# HermesCleaner
# ---------------------------------------------------------------------------


class HermesRunTest(unittest.TestCase):
    def test_run_step_order_keys_and_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            rc1 = root / ".zshrc"
            rc2 = root / ".bashrc"
            facts = _hermes_facts(
                hermes_home / "config.yaml", cli="/usr/bin/hermes",
                plugin_dir=hermes_home / "plugins" / "miloco",
                rc_files=(rc1, rc2),
            )
            cleaner = install.HermesCleaner(
                ui=FakeUI(),
                inv=_inventory(root, hermes=facts, candidates=()),
                decisions={}, run=RecordingRunner([]), env={},
            )
            calls = []
            def fake_step(key, target, fn):
                calls.append((key, target, fn.__name__))
            cleaner._step = fake_step
            cleaner.run()
            self.assertEqual(
                calls,
                [
                    ("uninstall.hermes.cron", TAG, "_remove_managed_crons"),
                    ("uninstall.hermes.plugin_state_backup",
                     str(hermes_home / "plugins" / "miloco" / "miloco-plugin" / "state.json"),
                     "_backup_plugin_state"),
                    ("uninstall.hermes.plugin_dir", str(hermes_home / "plugins" / "miloco"),
                     "_remove_plugin_dir"),
                    ("uninstall.hermes.skills", str(hermes_home / "skills"),
                     "_remove_skills"),
                    ("uninstall.hermes.adapter", "", "_remove_adapters"),
                    ("uninstall.hermes.install_cache", "", "_remove_install_cache"),
                    ("uninstall.hermes.agent_platform", "", "_clear_agent_platform"),
                    ("uninstall.hermes.env", str(hermes_home / ".env"), "_clean_env"),
                    ("uninstall.hermes.shell_rc",
                     f"{rc1}, {rc2}", "_clean_shell_rc"),
                    ("uninstall.hermes.cli_verify", "hermes", "_verify_cli"),
                ],
            )
            self.assertEqual(cleaner.report.platform, "hermes")
            self.assertTrue(cleaner.report.detected)
            self.assertTrue(cleaner.report.cli_available)

    def test_run_shell_rc_default_target_when_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli=None)
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=RecordingRunner([]), env={},
            )
            targets = []
            cleaner._step = lambda key, target, fn: targets.append(target)
            cleaner.run()
            self.assertEqual(targets[8], "~/.zshrc, ~/.bashrc")

    def test_scrub_secrets_redacts_every_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 直接构造带 bearer 的候选
            candidate = install._build_home_candidate(
                root / "miloco", {"env"}, deliver_configured=False
            )
            candidate = install.HomeCandidate(
                path=candidate.path, real_path=candidate.real_path,
                sources=candidate.sources, identity=candidate.identity,
                exists=candidate.exists, is_symlink=candidate.is_symlink,
                has_config=False, has_agent_platform=False,
                has_hermes_adapter=False, has_install_cache=False,
                agent_platform="", auth_bearer="SECRETKEY",
                deliver_configured=False,
            )
            cleaner = install.HermesCleaner(
                ui=FakeUI(),
                inv=_inventory(root, hermes=_hermes_facts(root / "c.yaml"), candidates=(candidate,)),
                decisions={}, run=RecordingRunner([]), env={},
            )
            cleaner.report.add(make_item("k", "hermes", "SECRETKEY", STATUS.DONE,
                                         "leak SECRETKEY here"))
            cleaner.report.add(make_item("k2", "hermes", "short", STATUS.DONE))
            cleaner._scrub_secrets()
            self.assertEqual(
                cleaner.report.items,
                [make_item("k", "hermes", "***", STATUS.DONE, "leak *** here"),
                 make_item("k2", "hermes", "short", STATUS.DONE)],
            )

    def test_env_and_home_will_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            facts = _hermes_facts(hermes_home / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=RecordingRunner([]), env={"PATH": "/bin", "HERMES_HOME": "ignored"},
            )
            self.assertEqual(cleaner._env(), {"PATH": "/bin", "HERMES_HOME": str(hermes_home)})
            candidate = _candidate(root / "miloco")
            self.assertFalse(cleaner._home_will_be_deleted(candidate))
            cleaner.decisions = {candidate.real_path: True}
            self.assertTrue(cleaner._home_will_be_deleted(candidate))


class HermesCronCleanupTest(unittest.TestCase):
    def _cleaner(self, root, *, facts, runner):
        return install.HermesCleaner(
            ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
            run=runner, env={"HERMES_HOME": "x"},
        )

    def test_no_backend_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli=None)
            runner = RecordingRunner([])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items,
                [make_item(
                    "uninstall.hermes.cli_unavailable", "hermes", "hermes cron",
                    STATUS.BLOCKED,
                    I18N.t("uninstall.detail.no_cron_backend"),
                    "install Hermes or run `hermes cron list` after restoring the CLI",
                )],
            )
            self.assertEqual(runner.calls, [])

    def test_probe_error_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res(rc=1, stderr="denied",
                    argv=("/usr/bin/hermes", "cron", "list", "--all")),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items,
                [make_item(
                    "uninstall.hermes.cron", "hermes", TAG, STATUS.FAILED,
                    "cli: /usr/bin/hermes cron list --all (rc=1): denied",
                    "hermes cron list",
                )],
            )

    def test_absent_when_no_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli="/usr/bin/hermes")
            runner = RecordingRunner([res("usage: rm"), res("")])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items, [make_item("uninstall.hermes.cron", "hermes", TAG, STATUS.ABSENT)]
            )
            self.assertEqual(len(runner.calls), 2)

    def test_cli_removes_each_job_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res("  a1 [active]\n"
                    f"    Name:  {TAG} one\n"
                    "  b2 [paused]\n"
                    f"    Name:  {TAG} two\n"),
                res(""),
                res(""),
                res("usage: rm"),
                res(""),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items,
                [make_item("uninstall.hermes.cron", "hermes", f"{TAG} one", STATUS.DONE),
                 make_item("uninstall.hermes.cron", "hermes", f"{TAG} two", STATUS.DONE)],
            )
            self.assertEqual(
                runner.argvs,
                [
                    ["/usr/bin/hermes", "cron", "--help"],
                    ["/usr/bin/hermes", "cron", "list", "--all"],
                    ["/usr/bin/hermes", "cron", "rm", "a1"],
                    ["/usr/bin/hermes", "cron", "rm", "b2"],
                    ["/usr/bin/hermes", "cron", "--help"],
                    ["/usr/bin/hermes", "cron", "list", "--all"],
                ],
            )
            self.assertEqual([c["timeout"] for c in runner.calls], [60] * 6)
            # CLI 删除调用不带 env，但列举调用带 HERMES_HOME
            self.assertIsNone(runner.calls[2]["env"])
            self.assertIsNone(runner.calls[3]["env"])
            self.assertEqual(runner.calls[1]["env"], {"HERMES_HOME": str(root / ".hermes")})

    def test_local_api_backend_removes_with_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
            facts = _hermes_facts(hermes_home / "config.yaml", cli=None,
                                  venv_python=python)
            runner = RecordingRunner([
                res(json.dumps([{"id": "j1", "name": f"{TAG} x"},
                                {"id": "j2", "name": "user"}])),
                res(json.dumps({"ok": True})),
                res(json.dumps([])),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items,
                [make_item("uninstall.hermes.cron", "hermes", f"{TAG} x", STATUS.DONE)],
            )
            self.assertEqual(
                runner.argvs[1],
                [str(python), "-c", install._HERMES_CRON_REMOVE_SNIPPET, "j1"],
            )
            self.assertEqual(runner.calls[1]["timeout"], 60)
            self.assertEqual(runner.calls[1]["env"], {"HERMES_HOME": str(hermes_home)})
            self.assertEqual(runner.calls[0]["env"], {"HERMES_HOME": str(hermes_home)})

    def test_rm_failure_and_remaining_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res(f"  a1 [active]\n    Name:  {TAG} one\n"),
                res(rc=7, stderr="locked", argv=("/usr/bin/hermes", "cron", "rm", "a1")),
                res("usage: rm"),
                res(f"  a1 [active]\n    Name:  {TAG} one\n"),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items,
                [
                    make_item("uninstall.hermes.cron", "hermes", f"{TAG} one",
                              STATUS.FAILED,
                              "/usr/bin/hermes cron rm a1 (rc=7): locked",
                              "hermes cron rm a1"),
                    make_item("uninstall.hermes.cron", "hermes", f"{TAG} one",
                              STATUS.FAILED,
                              I18N.t("uninstall.detail.cron_remaining"),
                              "hermes cron list"),
                ],
            )

    def test_rm_exception_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res(f"  a1 [active]\n    Name:  {TAG} one\n"),
                install.CommandTimeout("k", "hermes cron rm a1", detail="timed out"),
                res("usage: rm"),
                res(""),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(items[0].status, STATUS.FAILED)
            self.assertEqual(items[0].detail, "timed out")
            self.assertEqual(items[0].remediation, "hermes cron rm a1")

    def test_empty_id_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli="/usr/bin/hermes")
            name = f"x {TAG} y"
            runner = RecordingRunner([
                res("usage: rm"),
                res(f"    Name:  {name}\n"),
                res("usage: rm"),
                res(""),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items,
                [make_item("uninstall.hermes.cron", "hermes", name, STATUS.BLOCKED,
                           I18N.t("uninstall.detail.cron_id_unknown"),
                           "hermes cron list")],
            )
            self.assertEqual([c["argv"][1:3] for c in runner.calls],
                             [["cron", "--help"], ["cron", "list"],
                              ["cron", "--help"], ["cron", "list"]])

    def test_verify_error_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res(f"  a1 [active]\n    Name:  {TAG} one\n"),
                res(""),
                res("usage: rm"),
                res(rc=3, stderr="verify-down",
                    argv=("/usr/bin/hermes", "cron", "list", "--all")),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(items[0].status, STATUS.DONE)
            self.assertEqual(items[1].status, STATUS.FAILED)
            self.assertEqual(items[1].target, TAG)
            self.assertEqual(
                items[1].detail,
                "cli: /usr/bin/hermes cron list --all (rc=3): verify-down",
            )
            self.assertEqual(items[1].remediation, "hermes cron list")


class HermesPluginCleanupTest(unittest.TestCase):
    def _cleaner(self, root, *, facts, runner, decisions=None, candidates=()):
        return install.HermesCleaner(
            ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=candidates),
            decisions=decisions or {}, run=runner, env={},
        )

    def test_plugins_present_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            facts = _hermes_facts(hermes_home / "config.yaml", cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("miloco | enabled"),
                res(rc=2, stderr="boom"),
                install.CommandFailed("k", "list", detail="exc"),
            ])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(cleaner._plugins_present(), (True, ""))
            self.assertEqual(
                runner.calls[0]["argv"],
                ["/usr/bin/hermes", "plugins", "list", "--plain", "--no-bundled"],
            )
            self.assertEqual(runner.calls[0]["timeout"], 60)
            self.assertEqual(runner.calls[0]["env"], {"HERMES_HOME": str(hermes_home)})
            present, error = cleaner._plugins_present()
            self.assertIsNone(present)
            self.assertIn("rc=2", error)
            self.assertIn("boom", error)
            self.assertEqual(cleaner._plugins_present(), (None, "exc"))

    def test_backup_skipped_when_state_missing_or_no_deliver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            facts = _hermes_facts(hermes_home / "config.yaml")
            state = facts.plugin_dir / "miloco-plugin" / "state.json"
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]))
            self.assertEqual(
                cleaner._backup_plugin_state(),
                make_item("uninstall.hermes.plugin_state_backup", "hermes", str(state),
                          STATUS.ABSENT),
            )
            state.parent.mkdir(parents=True)
            state.write_text("{}", encoding="utf-8")
            self.assertEqual(cleaner._backup_plugin_state().status, STATUS.ABSENT)

    def test_backup_skipped_when_all_homes_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            miloco = root / "miloco"
            miloco.mkdir()
            facts = _hermes_facts(hermes_home / "config.yaml",
                                  plugin_dir=hermes_home / "plugins" / "miloco")
            state = facts.plugin_dir / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"deliver": {"target": "t"}}', encoding="utf-8")
            candidate = _candidate(miloco)
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]),
                                    decisions={candidate.real_path: True},
                                    candidates=(candidate,))
            self.assertEqual(
                cleaner._backup_plugin_state(),
                make_item("uninstall.hermes.plugin_state_backup", "hermes", str(state),
                          STATUS.SKIPPED, I18N.t("uninstall.detail.home_deleted")),
            )

    def test_backup_writes_file_with_stamp_and_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            miloco = root / "miloco"
            miloco.mkdir()
            facts = _hermes_facts(hermes_home / "config.yaml",
                                  plugin_dir=hermes_home / "plugins" / "miloco")
            state = facts.plugin_dir / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"deliver": {"candidates": ["c"]}}', encoding="utf-8")
            candidate = _candidate(miloco)
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]),
                                    candidates=(candidate,))
            item = cleaner._backup_plugin_state()
            self.assertIs(item.status, STATUS.DONE)
            backup = Path(item.target)
            self.assertRegex(
                backup.name, r"^\d{8}-\d{6}-plugin-state\.json$"
            )
            self.assertEqual(backup.parent, hermes_home / "miloco-uninstall-backup")
            self.assertEqual(backup.read_text(encoding="utf-8"),
                             state.read_text(encoding="utf-8"))
            self.assertEqual(item.detail,
                             I18N.t("uninstall.detail.state_backed_up"))
            # 再次备份不覆盖已有文件（同一秒内追加序号；跨秒则换时间戳）
            second = cleaner._backup_plugin_state()
            self.assertNotEqual(second.target, item.target)
            self.assertRegex(
                Path(second.target).name,
                r"^\d{8}-\d{6}(-\d+)?-plugin-state\.json$",
            )
            self.assertEqual(Path(item.target).read_text(encoding="utf-8"),
                             state.read_text(encoding="utf-8"))

    def test_backup_failure_blocks_plugin_dir_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            facts = _hermes_facts(hermes_home / "config.yaml",
                                  plugin_dir=hermes_home / "plugins" / "miloco")
            state = facts.plugin_dir / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"deliver": {"target": "t"}}', encoding="utf-8")
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]))
            with mock.patch.object(install.shutil, "copy2", side_effect=OSError("disk full")):
                item = cleaner._backup_plugin_state()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertEqual(
                item.detail, I18N.t("uninstall.detail.backup_failed", "disk full")
            )
            self.assertTrue(cleaner._plugin_dir_removal_blocked)
            self.assertEqual(
                cleaner._remove_plugin_dir(),
                make_item("uninstall.hermes.plugin_dir", "hermes", str(facts.plugin_dir),
                          STATUS.FAILED,
                          I18N.t("uninstall.detail.backup_failed_skip_delete")),
            )
            self.assertTrue(facts.plugin_dir.exists())

    def test_plugin_dir_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", cli="/usr/bin/hermes")
            runner = RecordingRunner([])
            item = self._cleaner(root, facts=facts, runner=runner)._remove_plugin_dir()
            self.assertEqual(
                item,
                make_item("uninstall.hermes.plugin_dir", "hermes", str(facts.plugin_dir),
                          STATUS.ABSENT),
            )
            self.assertEqual(runner.calls, [])

    def test_plugin_dir_cli_missing_removes_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            plugin = hermes_home / "plugins" / install.HERMES_PLUGIN_ID
            (plugin / "miloco-plugin").mkdir(parents=True)
            facts = _hermes_facts(hermes_home / "config.yaml", cli=None,
                                  plugin_dir=plugin)
            item = self._cleaner(root, facts=facts,
                                 runner=RecordingRunner([]))._remove_plugin_dir()
            self.assertEqual(
                item,
                make_item("uninstall.hermes.plugin_dir", "hermes", str(plugin),
                          STATUS.DONE,
                          I18N.t("uninstall.detail.plugin_dir_cli_missing")),
            )
            self.assertFalse(plugin.exists())

    def test_plugin_dir_cli_success_and_lingering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            plugin = hermes_home / "plugins" / install.HERMES_PLUGIN_ID
            plugin.mkdir(parents=True)
            facts = _hermes_facts(hermes_home / "config.yaml", cli="/usr/bin/hermes",
                                  plugin_dir=plugin)
            runner = RecordingRunner([
                lambda argv: (shutil.rmtree(plugin), res(""))[1],
            ])
            item = self._cleaner(root, facts=facts, runner=runner)._remove_plugin_dir()
            self.assertEqual(
                item,
                make_item("uninstall.hermes.plugin_dir", "hermes", str(plugin),
                          STATUS.DONE),
            )
            self.assertEqual(
                runner.calls[0]["argv"],
                ["/usr/bin/hermes", "plugins", "remove", "miloco"],
            )
            self.assertEqual(runner.calls[0]["timeout"], 60)
            self.assertEqual(runner.calls[0]["env"], {"HERMES_HOME": str(hermes_home)})

            plugin.mkdir(parents=True)
            lingering = RecordingRunner([res("")])
            item = self._cleaner(root, facts=facts, runner=lingering)._remove_plugin_dir()
            self.assertIs(item.status, STATUS.DONE)
            self.assertIn("returned 0 but directory still exists", item.detail)
            self.assertFalse(plugin.exists())

    def test_plugin_dir_failure_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            plugin = hermes_home / "plugins" / install.HERMES_PLUGIN_ID
            plugin.mkdir(parents=True)
            facts = _hermes_facts(hermes_home / "config.yaml", cli="/usr/bin/hermes",
                                  plugin_dir=plugin)
            runner = RecordingRunner([res(rc=1, stderr="locked",
                                          argv=("/usr/bin/hermes", "plugins", "remove",
                                                "miloco"))])
            item = self._cleaner(root, facts=facts, runner=runner)._remove_plugin_dir()
            self.assertIs(item.status, STATUS.DONE)
            self.assertEqual(
                item.detail,
                I18N.t(
                    "uninstall.detail.plugin_dir_fallback",
                    "/usr/bin/hermes plugins remove miloco (rc=1): locked",
                ),
            )
            self.assertFalse(plugin.exists())

    def test_plugin_dir_failure_but_already_gone_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            plugin = hermes_home / "plugins" / install.HERMES_PLUGIN_ID
            plugin.mkdir(parents=True)
            facts = _hermes_facts(hermes_home / "config.yaml", cli="/usr/bin/hermes",
                                  plugin_dir=plugin)
            runner = RecordingRunner([
                lambda argv: (shutil.rmtree(plugin), res(rc=1))[1],
            ])
            item = self._cleaner(root, facts=facts, runner=runner)._remove_plugin_dir()
            self.assertEqual(
                item,
                make_item("uninstall.hermes.plugin_dir", "hermes", str(plugin),
                          STATUS.ABSENT),
            )

    def test_plugin_dir_exception_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            plugin = hermes_home / "plugins" / install.HERMES_PLUGIN_ID
            plugin.mkdir(parents=True)
            facts = _hermes_facts(hermes_home / "config.yaml", cli="/usr/bin/hermes",
                                  plugin_dir=plugin)
            runner = RecordingRunner([
                install.CommandFailed("k", "remove", detail="kaput")
            ])
            item = self._cleaner(root, facts=facts, runner=runner)._remove_plugin_dir()
            self.assertEqual(
                item,
                make_item("uninstall.hermes.plugin_dir", "hermes", str(plugin),
                          STATUS.DONE, "kaput"),
            )
            self.assertFalse(plugin.exists())

    def test_remove_plugin_dir_direct_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / ".hermes" / "plugins" / "miloco"
            plugin.mkdir(parents=True)
            facts = _hermes_facts(root / ".hermes" / "config.yaml", plugin_dir=plugin)
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]))
            with mock.patch.object(install, "_rm_path", side_effect=OSError("nope")):
                item = cleaner._remove_plugin_dir_direct(str(plugin), "detail")
            self.assertEqual(
                item,
                make_item("uninstall.hermes.plugin_dir", "hermes", str(plugin),
                          STATUS.FAILED, "detail; nope"),
            )

    def test_remove_skills_only_prefixed_direct_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / ".hermes" / "skills"
            keep = skills / "other"
            keep.mkdir(parents=True)
            (skills / "miloco-a").mkdir()
            (skills / "miloco-b").mkdir()
            nested = keep / "miloco-nested"
            nested.mkdir()
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]))
            items = cleaner._remove_skills()
            self.assertEqual(
                items,
                [make_item("uninstall.hermes.skills", "hermes",
                           str(skills / "miloco-a"), STATUS.DONE),
                 make_item("uninstall.hermes.skills", "hermes",
                           str(skills / "miloco-b"), STATUS.DONE)],
            )
            self.assertTrue(keep.exists())
            self.assertTrue(nested.exists())
            self.assertTrue(skills.is_dir())

    def test_remove_skills_absent_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]))
            skills = root / ".hermes" / "skills"
            self.assertEqual(
                cleaner._remove_skills(),
                [make_item("uninstall.hermes.skills", "hermes", str(skills),
                           STATUS.ABSENT)],
            )
            skills.mkdir(parents=True)
            (skills / "other").mkdir()
            self.assertEqual(
                cleaner._remove_skills(),
                [make_item("uninstall.hermes.skills", "hermes", str(skills),
                           STATUS.ABSENT)],
            )

    def test_remove_skills_iterdir_error_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / ".hermes" / "skills"
            skills.mkdir(parents=True)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]))
            real_iterdir = Path.iterdir

            def fake(self):
                if self == skills:
                    raise OSError("boom")
                return real_iterdir(self)

            with mock.patch.object(Path, "iterdir", fake):
                with self.assertRaises(install.UninstallError) as ctx:
                    cleaner._remove_skills()
            self.assertEqual(ctx.exception.key, "uninstall.hermes.skills")
            self.assertEqual(ctx.exception.target, str(skills))
            self.assertEqual(ctx.exception.detail, "boom")
            self.assertIs(ctx.exception.status, STATUS.FAILED)

    def test_remove_skills_per_file_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / ".hermes" / "skills"
            (skills / "miloco-a").mkdir(parents=True)
            (skills / "miloco-b").mkdir()
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]))
            real_rm = install._rm_path

            def fake_rm(path):
                if Path(path).name == "miloco-a":
                    raise OSError("locked")
                return real_rm(path)

            with mock.patch.object(install, "_rm_path", fake_rm):
                items = cleaner._remove_skills()
            self.assertEqual(
                items,
                [make_item("uninstall.hermes.skills", "hermes", str(skills / "miloco-a"),
                           STATUS.FAILED, "locked"),
                 make_item("uninstall.hermes.skills", "hermes", str(skills / "miloco-b"),
                           STATUS.DONE)],
            )


class HermesHomeArtifactsTest(unittest.TestCase):
    def test_candidate_homes_prefers_existing_else_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "two").mkdir()
            one = _candidate(root / "one")
            two = _candidate(root / "two")
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=(one, two)),
                decisions={}, run=RecordingRunner([]), env={},
            )
            self.assertEqual(cleaner._candidate_homes(), [two])
            cleaner2 = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=(one,)),
                decisions={}, run=RecordingRunner([]), env={},
            )
            self.assertEqual(cleaner2._candidate_homes(), [one])

    def test_remove_home_artifact_statuses_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deleted = root / "deleted"
            absent = root / "absent"
            present = root / "present"
            deleted.mkdir()
            absent.mkdir()
            present.mkdir()
            (deleted / "agent_platform" / "hermes").mkdir(parents=True)
            (present / "agent_platform" / "hermes").mkdir(parents=True)
            candidates = tuple(_candidate(p) for p in (deleted, absent, present))
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=candidates),
                decisions={candidates[0].real_path: True},
                run=RecordingRunner([]), env={},
            )
            items = cleaner._remove_adapters()
            self.assertEqual(
                items,
                [
                    make_item("uninstall.hermes.adapter", "hermes",
                              str(deleted / "agent_platform" / "hermes"),
                              STATUS.SKIPPED),
                    make_item("uninstall.hermes.adapter", "hermes",
                              str(absent / "agent_platform" / "hermes"),
                              STATUS.ABSENT),
                    make_item("uninstall.hermes.adapter", "hermes",
                              str(present / "agent_platform" / "hermes"),
                              STATUS.DONE),
                ],
            )
            self.assertTrue((deleted / "agent_platform" / "hermes").exists())
            self.assertFalse((present / "agent_platform").exists())
            self.assertFalse((present / "agent_platform" / "hermes").exists())
            # 空父目录被清掉，但 home 本身保留
            self.assertTrue(present.is_dir())

    def test_remove_home_artifact_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / ".install-cache" / "hermes-plugin").mkdir(parents=True)
            candidate = _candidate(home)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=(candidate,)),
                decisions={}, run=RecordingRunner([]), env={},
            )
            with mock.patch.object(install, "_rm_path", side_effect=OSError("nope")):
                items = cleaner._remove_install_cache()
            self.assertEqual(
                items,
                [make_item("uninstall.hermes.install_cache", "hermes",
                           str(home / ".install-cache" / "hermes-plugin"),
                           STATUS.FAILED, "nope")],
            )

    def test_clear_agent_platform_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deleted = root / "deleted"
            missing = root / "missing"
            hermes = root / "hermes"
            other = root / "other"
            empty = root / "empty"
            for path in (deleted, missing, hermes, other, empty):
                path.mkdir()
            (deleted / "config.json").write_text(
                json.dumps({"agent": {"platform": "hermes"}}), encoding="utf-8"
            )
            (hermes / "config.json").write_text(
                json.dumps({"agent": {"platform": "hermes"}}), encoding="utf-8"
            )
            (other / "config.json").write_text(
                json.dumps({"agent": {"platform": "openclaw"}}), encoding="utf-8"
            )
            candidates = tuple(_candidate(p) for p in (deleted, missing, hermes, other, empty))
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=candidates),
                decisions={candidates[0].real_path: True},
                run=RecordingRunner([]), env={},
            )
            items = cleaner._clear_agent_platform()
            self.assertEqual(
                items,
                [
                    make_item("uninstall.hermes.agent_platform", "hermes",
                              str(deleted / "config.json"), STATUS.SKIPPED,
                              I18N.t("uninstall.detail.home_deleted")),
                    make_item("uninstall.hermes.agent_platform", "hermes",
                              str(missing / "config.json"), STATUS.ABSENT),
                    make_item("uninstall.hermes.agent_platform", "hermes",
                              str(hermes / "config.json"), STATUS.DONE),
                    make_item("uninstall.hermes.agent_platform", "hermes",
                              str(other / "config.json"), STATUS.KEPT,
                              I18N.t("uninstall.detail.platform_not_hermes", "openclaw")),
                    make_item("uninstall.hermes.agent_platform", "hermes",
                              str(empty / "config.json"), STATUS.ABSENT),
                ],
            )
            self.assertEqual(
                json.loads((hermes / "config.json").read_text(encoding="utf-8")),
                {"agent": {"platform": ""}},
            )
            self.assertEqual(
                json.loads((other / "config.json").read_text(encoding="utf-8")),
                {"agent": {"platform": "openclaw"}},
            )

    def test_clean_env_uses_candidate_bearer_and_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "miloco"
            home.mkdir()
            (root / ".hermes").mkdir()
            env_file = root / ".hermes" / ".env"
            env_file.write_text(
                f"API_SERVER_KEY=BEAR\nMILOCO_HOME={home}\n", encoding="utf-8"
            )
            candidate = install._build_home_candidate(
                home, {"env"}, deliver_configured=False
            )
            candidate = install.HomeCandidate(
                path=candidate.path, real_path=candidate.real_path,
                sources=candidate.sources, identity=candidate.identity,
                exists=candidate.exists, is_symlink=candidate.is_symlink,
                has_config=candidate.has_config, has_agent_platform=False,
                has_hermes_adapter=False, has_install_cache=False,
                agent_platform="", auth_bearer="BEAR", deliver_configured=False,
            )
            facts = _hermes_facts(root / ".hermes" / "config.yaml", env_file=env_file)
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=(candidate,)),
                decisions={}, run=RecordingRunner([]), env={},
            )
            item = cleaner._clean_env()
            self.assertEqual(
                item,
                make_item("uninstall.hermes.env", "hermes", str(env_file), STATUS.DONE,
                          "API_SERVER_KEY, MILOCO_HOME"),
            )
            self.assertEqual(env_file.read_text(encoding="utf-8"), "")

    def test_clean_env_absent_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=RecordingRunner([]), env={},
            )
            self.assertEqual(
                cleaner._clean_env(),
                make_item("uninstall.hermes.env", "hermes", str(root / ".hermes" / ".env"),
                          STATUS.ABSENT),
            )

    def test_managed_homes_narrows_rc_only_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_home = root / "env-home"
            env_home.mkdir()
            rc_with_config = root / "rc-config"
            rc_with_config.mkdir()
            (rc_with_config / "config.json").write_text("{}", encoding="utf-8")
            rc_bare = root / "rc-bare"
            rc_bare.mkdir()
            rc_with_platform = root / "rc-platform"
            rc_with_platform.mkdir()
            (rc_with_platform / "config.json").write_text(
                json.dumps({"agent": {"platform": "hermes"}}), encoding="utf-8"
            )
            rc_with_adapter = root / "rc-adapter"
            (rc_with_adapter / "agent_platform").mkdir(parents=True)
            candidates = (
                _candidate(env_home, sources=("env",)),
                _candidate(rc_with_config, sources=("shell_rc",)),
                _candidate(rc_bare, sources=("shell_rc",)),
                _candidate(rc_with_platform, sources=("shell_rc",)),
                _candidate(rc_with_adapter, sources=("shell_rc",)),
            )
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=candidates),
                decisions={}, run=RecordingRunner([]), env={},
            )
            self.assertEqual(
                cleaner._managed_homes(),
                {env_home.resolve(), rc_with_config.resolve(),
                 rc_with_platform.resolve(), rc_with_adapter.resolve()},
            )

    def test_clean_shell_rc_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=RecordingRunner([]), env={},
            )
            self.assertEqual(
                cleaner._clean_shell_rc(),
                [make_item("uninstall.hermes.shell_rc", "hermes",
                           "~/.zshrc, ~/.bashrc", STATUS.ABSENT)],
            )
            missing = root / ".zshrc"
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  rc_files=(missing,))
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=RecordingRunner([]), env={},
            )
            self.assertEqual(
                cleaner._clean_shell_rc(),
                [make_item("uninstall.hermes.shell_rc", "hermes", str(missing),
                           STATUS.ABSENT)],
            )

    def test_clean_shell_rc_applies_plan_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "miloco"
            managed.mkdir()
            zshrc = root / ".zshrc"
            zshrc.write_text(f"# miloco runtime path\n"
                             f"export MILOCO_HOME={managed}\n", encoding="utf-8")
            bashrc = root / ".bashrc"
            bashrc.write_text("export MILOCO_HOME=/user\n", encoding="utf-8")
            candidate = _candidate(managed, sources=("env",))
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  rc_files=(zshrc, bashrc))
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=(candidate,)),
                decisions={}, run=RecordingRunner([]), env={},
            )
            items = cleaner._clean_shell_rc()
            self.assertEqual(
                items,
                [make_item("uninstall.hermes.shell_rc", "hermes", str(zshrc),
                           STATUS.DONE),
                 make_item("uninstall.hermes.shell_rc", "hermes", str(bashrc),
                           STATUS.ABSENT)],
            )
            self.assertEqual(zshrc.read_text(encoding="utf-8"), "")
            self.assertEqual(bashrc.read_text(encoding="utf-8"),
                             "export MILOCO_HOME=/user\n")

    def test_verify_cli_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            facts = _hermes_facts(hermes_home / "config.yaml", cli=None)
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=RecordingRunner([]), env={},
            )
            self.assertEqual(
                cleaner._verify_cli(),
                make_item(
                    "uninstall.hermes.cli_verify", "hermes",
                    str(hermes_home / "plugins" / "miloco"), STATUS.DONE,
                    I18N.t("uninstall.detail.verify_cli_missing"),
                ),
            )

            facts = _hermes_facts(hermes_home / "config.yaml", cli="/usr/bin/hermes")
            runner = RecordingRunner([res("miloco | enabled")])
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=runner, env={},
            )
            self.assertEqual(
                cleaner._verify_cli(),
                make_item(
                    "uninstall.hermes.cli_verify", "hermes",
                    str(hermes_home / "plugins" / "miloco"), STATUS.DONE,
                    I18N.t("uninstall.detail.config_residue"),
                ),
            )

            runner = RecordingRunner([res("other | enabled")])
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=runner, env={},
            )
            self.assertEqual(
                cleaner._verify_cli(),
                make_item(
                    "uninstall.hermes.cli_verify", "hermes",
                    str(hermes_home / "plugins" / "miloco"), STATUS.DONE,
                ),
            )

            runner = RecordingRunner([res(rc=2, stderr="down",
                                          argv=("/usr/bin/hermes", "plugins", "list",
                                                "--plain", "--no-bundled"))])
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=runner, env={},
            )
            item = cleaner._verify_cli()
            self.assertEqual(item.status, STATUS.FAILED)
            self.assertEqual(item.key, "uninstall.hermes.cli_verify")
            self.assertEqual(
                item.detail,
                "/usr/bin/hermes plugins list --plain --no-bundled (rc=2): down",
            )
            self.assertEqual(item.remediation,
                             "hermes plugins list --plain --no-bundled")


# ---------------------------------------------------------------------------
# OpenClawCleaner
# ---------------------------------------------------------------------------


class OpenClawCleanerTest(unittest.TestCase):
    def _cleaner(self, root, *, facts, runner):
        return install.OpenClawCleaner(
            ui=FakeUI(), inv=_inventory(root, openclaw=facts), run=runner
        )

    def _facts(self, root, **overrides):
        state = root / ".openclaw"
        state.mkdir(exist_ok=True)
        return _openclaw_facts(state, **overrides)

    def test_run_step_order_keys_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cleaner = self._cleaner(root, facts=self._facts(root),
                                    runner=RecordingRunner([]))
            calls = []
            cleaner._step = lambda key, target, fn: calls.append((key, target, fn.__name__))
            cleaner.run()
            self.assertEqual(
                calls,
                [
                    ("uninstall.openclaw.plugin",
                     f"openclaw plugins uninstall {PLUGIN_ID}", "_uninstall_plugin"),
                    ("uninstall.openclaw.tools_allow", "tools.alsoAllow",
                     "_clean_tools_allow"),
                    ("uninstall.openclaw.plugin_entry",
                     f'plugins.entries["{PLUGIN_ID}"]', "_clean_plugin_entry"),
                    ("uninstall.openclaw.cron", TAG, "_remove_managed_crons"),
                    ("uninstall.openclaw.verify", PLUGIN_ID, "_verify_plugin_absent"),
                ],
            )
            self.assertEqual(cleaner.report.platform, "openclaw")
            self.assertTrue(cleaner.report.cli_available is False)

    def test_cli_missing_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cleaner = self._cleaner(root, facts=self._facts(root),
                                    runner=RecordingRunner([]))
            self.assertEqual(
                cleaner._cli_missing("cmd here"),
                make_item("uninstall.openclaw.cli_unavailable", "openclaw", "cmd here",
                          STATUS.BLOCKED,
                          I18N.t("uninstall.detail.cli_missing", "openclaw"),
                          "cmd here"),
            )

    def test_uninstall_plugin_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = f"openclaw plugins uninstall {PLUGIN_ID}"
            cleaner = self._cleaner(root, facts=self._facts(root),
                                    runner=RecordingRunner([]))
            self.assertEqual(cleaner._uninstall_plugin(),
                             cleaner._cli_missing(command))

            runner = RecordingRunner([res("")])
            cleaner = self._cleaner(root, facts=self._facts(root, cli="/usr/bin/openclaw"),
                                    runner=runner)
            self.assertEqual(
                cleaner._uninstall_plugin(),
                make_item("uninstall.openclaw.plugin", "openclaw", command, STATUS.DONE),
            )
            self.assertEqual(runner.calls[0]["argv"],
                             ["/usr/bin/openclaw", "plugins", "uninstall", PLUGIN_ID])
            self.assertEqual(runner.calls[0]["timeout"], 120)
            self.assertEqual(runner.calls[0]["stdin"], "y\n")
            self.assertIsNone(runner.calls[0]["env"])

            runner = RecordingRunner([
                res(rc=7, stderr="locked",
                    argv=("/usr/bin/openclaw", "plugins", "uninstall", PLUGIN_ID))
            ])
            cleaner = self._cleaner(root, facts=self._facts(root, cli="/usr/bin/openclaw"),
                                    runner=runner)
            self.assertEqual(
                cleaner._uninstall_plugin(),
                make_item("uninstall.openclaw.plugin", "openclaw", command, STATUS.FAILED,
                          f"/usr/bin/openclaw plugins uninstall {PLUGIN_ID} (rc=7): locked",
                          command),
            )

            runner = RecordingRunner([install.CommandTimeout("k", command, detail="slow")])
            cleaner = self._cleaner(root, facts=self._facts(root, cli="/usr/bin/openclaw"),
                                    runner=runner)
            self.assertEqual(
                cleaner._uninstall_plugin(),
                make_item("uninstall.openclaw.plugin", "openclaw", command, STATUS.FAILED,
                          "slow", command),
            )

    def test_tools_allow_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            get_cmd = "openclaw config get tools.alsoAllow --json"

            cleaner = self._cleaner(root, facts=self._facts(root),
                                    runner=RecordingRunner([]))
            self.assertEqual(cleaner._clean_tools_allow(),
                             cleaner._cli_missing(get_cmd))

            for stdout in ("", "null", "  null  "):
                cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([res(stdout)]))
                self.assertEqual(
                    cleaner._clean_tools_allow(),
                    make_item("uninstall.openclaw.tools_allow", "openclaw",
                              "tools.alsoAllow", STATUS.ABSENT),
                )

            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([res("not json")]))
            item = cleaner._clean_tools_allow()
            self.assertEqual(item.key, "uninstall.openclaw.tools_allow")
            self.assertEqual(item.target, get_cmd)
            self.assertIs(item.status, STATUS.FAILED)
            self.assertTrue(item.detail)

            cleaner = self._cleaner(root, facts=facts,
                                    runner=RecordingRunner([res('{"a": 1}')]))
            self.assertEqual(
                cleaner._clean_tools_allow(),
                make_item("uninstall.openclaw.tools_allow", "openclaw", "tools.alsoAllow",
                          STATUS.FAILED, I18N.t("uninstall.detail.not_a_list")),
            )

            # 没有受管工具名 → ABSENT，不发 set
            runner = RecordingRunner([res('["keep", "other"]')])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._clean_tools_allow(),
                make_item("uninstall.openclaw.tools_allow", "openclaw", "tools.alsoAllow",
                          STATUS.ABSENT),
            )
            self.assertEqual(len(runner.calls), 1)

            # 删除精确相等项，保留顺序、重复项与其他值
            managed = install.FALLBACK_OPENCLAW_TOOLS
            current = ["keep", managed[0], managed[1], "keep2", managed[0]]
            filtered = ["keep", "keep2"]
            runner = RecordingRunner([res(json.dumps(current)), res("")])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._clean_tools_allow(),
                make_item("uninstall.openclaw.tools_allow", "openclaw", "tools.alsoAllow",
                          STATUS.DONE, f"{managed[0]}, {managed[1]}, {managed[0]}"),
            )
            self.assertEqual(
                runner.argvs,
                [
                    [cli, "config", "get", "tools.alsoAllow", "--json"],
                    [cli, "config", "set", "tools.alsoAllow", json.dumps(filtered),
                     "--strict-json"],
                ],
            )
            self.assertEqual([c["timeout"] for c in runner.calls], [30, 30])

            # get 失败
            runner = RecordingRunner([res(rc=1, stderr="nope",
                                          argv=(cli, "config", "get", "tools.alsoAllow",
                                                "--json"))])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            item = cleaner._clean_tools_allow()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertEqual(item.target, get_cmd)
            self.assertEqual(item.remediation, get_cmd)
            self.assertEqual(item.detail,
                             f"{cli} config get tools.alsoAllow --json (rc=1): nope")

            # set 失败
            set_cmd = f"openclaw config set tools.alsoAllow {json.dumps(['keep'])} --strict-json"
            runner = RecordingRunner([
                res(json.dumps([managed[0], "keep"])),
                res(rc=9, stderr="denied",
                    argv=(cli, "config", "set", "tools.alsoAllow", json.dumps(["keep"]),
                          "--strict-json")),
            ])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._clean_tools_allow(),
                make_item("uninstall.openclaw.tools_allow", "openclaw", set_cmd,
                          STATUS.FAILED,
                          f"{cli} config set tools.alsoAllow {json.dumps(['keep'])} "
                          "--strict-json (rc=9): denied",
                          set_cmd),
            )

            # set 抛异常
            runner = RecordingRunner([
                res(json.dumps([managed[0]])),
                install.CommandFailed("k", "set", detail="boom"),
            ])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            item = cleaner._clean_tools_allow()
            self.assertIs(item.status, STATUS.FAILED)
            self.assertEqual(item.detail, "boom")
            self.assertTrue(item.target.startswith("openclaw config set tools.alsoAllow"))

    def test_plugin_entry_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            path = f'plugins.entries["{PLUGIN_ID}"]'
            command = f"openclaw config unset {path}"

            cleaner = self._cleaner(root, facts=self._facts(root),
                                    runner=RecordingRunner([]))
            self.assertEqual(cleaner._clean_plugin_entry(),
                             cleaner._cli_missing(command))

            runner = RecordingRunner([res("")])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._clean_plugin_entry(),
                make_item("uninstall.openclaw.plugin_entry", "openclaw", path, STATUS.DONE),
            )
            self.assertEqual(runner.calls[0]["argv"], [cli, "config", "unset", path])
            self.assertEqual(runner.calls[0]["timeout"], 30)

            for stderr in ("Path not found", "does not exist", "NO SUCH key",
                           "config not exist"):
                cleaner = self._cleaner(root, facts=facts,
                                        runner=RecordingRunner([res(rc=1, stderr=stderr)]))
                self.assertEqual(
                    cleaner._clean_plugin_entry(),
                    make_item("uninstall.openclaw.plugin_entry", "openclaw", path,
                              STATUS.ABSENT, I18N.t("uninstall.detail.already_absent")),
                )

            runner = RecordingRunner([res(rc=2, stderr="denied",
                                          argv=(cli, "config", "unset", path))])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._clean_plugin_entry(),
                make_item("uninstall.openclaw.plugin_entry", "openclaw", command,
                          STATUS.FAILED,
                          f"{cli} config unset {path} (rc=2): denied", command),
            )

            runner = RecordingRunner([install.CommandFailed("k", "unset", detail="exc")])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._clean_plugin_entry(),
                make_item("uninstall.openclaw.plugin_entry", "openclaw", command,
                          STATUS.FAILED, "exc", command),
            )

    def test_openclaw_crons_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            list_cmd = "openclaw cron list --all --json"

            cleaner = self._cleaner(root, facts=self._facts(root),
                                    runner=RecordingRunner([]))
            self.assertEqual(cleaner._remove_managed_crons(),
                             [cleaner._cli_missing(list_cmd)])

            for runner in (
                RecordingRunner([install.CommandFailed("k", list_cmd, detail="boom")]),
                RecordingRunner([res(rc=1, stderr="down", argv=(cli, "cron", "list",
                                                                 "--all", "--json"))]),
            ):
                cleaner = self._cleaner(root, facts=facts, runner=runner)
                item = cleaner._remove_managed_crons()[0]
                self.assertIs(item.status, STATUS.FAILED)
                self.assertEqual(item.remediation, "openclaw gateway start")
                self.assertEqual(item.target, list_cmd)

            cleaner = self._cleaner(root, facts=facts,
                                    runner=RecordingRunner([res("noise")]))
            self.assertEqual(
                cleaner._remove_managed_crons(),
                [make_item("uninstall.openclaw.cron", "openclaw", list_cmd,
                           STATUS.FAILED, I18N.t("uninstall.detail.cron_parse"),
                           list_cmd)],
            )

            cleaner = self._cleaner(root, facts=facts,
                                    runner=RecordingRunner([res(json.dumps({"jobs": []}))]))
            self.assertEqual(
                cleaner._remove_managed_crons(),
                [make_item("uninstall.openclaw.cron", "openclaw", TAG, STATUS.ABSENT)],
            )

            # 缺 id → BLOCKED；正常 job → rm + DONE；用户 cron 不动
            jobs = [
                {"name": "no-id", "description": f"x {TAG}"},
                {"id": "1", "name": "managed-one", "description": f"{TAG} a"},
            ]
            runner = RecordingRunner([
                res(json.dumps({"jobs": jobs})),
                res(""),
                res(json.dumps({"jobs": [{"id": "u", "description": "user"}]})),
            ])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._remove_managed_crons(),
                [
                    make_item("uninstall.openclaw.cron", "openclaw", "no-id",
                              STATUS.BLOCKED,
                              I18N.t("uninstall.detail.cron_id_unknown"), list_cmd),
                    make_item("uninstall.openclaw.cron", "openclaw", "managed-one",
                              STATUS.DONE),
                ],
            )
            self.assertEqual(
                runner.argvs,
                [
                    [cli, "cron", "list", "--all", "--json"],
                    [cli, "cron", "rm", "1"],
                    [cli, "cron", "list", "--all", "--json"],
                ],
            )
            self.assertEqual([c["timeout"] for c in runner.calls], [30, 30, 30])

            # rm 失败 + 复验仍有残留
            runner = RecordingRunner([
                res(json.dumps({"jobs": [{"id": "1", "name": "n",
                                          "description": f"{TAG} a"}]})),
                res(rc=4, stderr="locked", argv=(cli, "cron", "rm", "1")),
                res(json.dumps({"jobs": [{"id": "1", "name": "n",
                                          "description": f"{TAG} a"}]})),
            ])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._remove_managed_crons(),
                [
                    make_item("uninstall.openclaw.cron", "openclaw", "n", STATUS.FAILED,
                              f"{cli} cron rm 1 (rc=4): locked", "openclaw cron rm 1"),
                    make_item("uninstall.openclaw.cron", "openclaw", "n", STATUS.FAILED,
                              I18N.t("uninstall.detail.cron_remaining"), list_cmd),
                ],
            )

            # rm 抛异常
            runner = RecordingRunner([
                res(json.dumps({"jobs": [{"id": "1", "description": f"{TAG} a"}]})),
                install.CommandTimeout("k", "rm", detail="slow"),
                res(json.dumps({"jobs": []})),
            ])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            items = cleaner._remove_managed_crons()
            self.assertEqual(items,
                             [make_item("uninstall.openclaw.cron", "openclaw", "1",
                                        STATUS.FAILED, "slow", "openclaw cron rm 1")])

            # 复验异常
            runner = RecordingRunner([
                res(json.dumps({"jobs": [{"id": "1", "description": f"{TAG} a"}]})),
                res(""),
                install.CommandFailed("k", "list", detail="verify-boom"),
            ])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            items = cleaner._remove_managed_crons()
            self.assertEqual(items[-1],
                             make_item("uninstall.openclaw.cron", "openclaw", list_cmd,
                                       STATUS.FAILED, "verify-boom", list_cmd))

    def test_verify_plugin_absent_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            command = "openclaw plugins list --json"

            cleaner = self._cleaner(root, facts=self._facts(root),
                                    runner=RecordingRunner([]))
            self.assertEqual(cleaner._verify_plugin_absent(),
                             cleaner._cli_missing(command))

            runner = RecordingRunner([res(json.dumps({"plugins": []}))])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._verify_plugin_absent(),
                make_item("uninstall.openclaw.verify", "openclaw", PLUGIN_ID, STATUS.DONE),
            )
            self.assertEqual(runner.calls[0]["argv"], [cli, "plugins", "list", "--json"])
            self.assertEqual(runner.calls[0]["timeout"], 60)

            runner = RecordingRunner([
                res(json.dumps({"plugins": [{"id": PLUGIN_ID}]}))
            ])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._verify_plugin_absent(),
                make_item("uninstall.openclaw.verify", "openclaw", PLUGIN_ID,
                          STATUS.FAILED,
                          I18N.t("uninstall.detail.plugin_still_installed"),
                          f"openclaw plugins uninstall {PLUGIN_ID}"),
            )

            runner = RecordingRunner([res(rc=3, stderr="down",
                                          argv=(cli, "plugins", "list", "--json"))])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._verify_plugin_absent(),
                make_item("uninstall.openclaw.verify", "openclaw", command, STATUS.FAILED,
                          f"{cli} plugins list --json (rc=3): down", command),
            )

            runner = RecordingRunner([install.CommandFailed("k", "list", detail="exc")])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._verify_plugin_absent(),
                make_item("uninstall.openclaw.verify", "openclaw", command, STATUS.FAILED,
                          "exc", command),
            )


# ---------------------------------------------------------------------------
# CoreCleaner
# ---------------------------------------------------------------------------


class CoreCleanerTest(unittest.TestCase):
    def _cleaner(self, runner):
        return install.CoreCleaner(ui=FakeUI(), run=runner)

    def test_uv_missing_is_absent(self):
        with mock.patch.object(install.shutil, "which", return_value=None):
            cleaner = self._cleaner(RecordingRunner([]))
            report = cleaner.run()
        self.assertEqual(
            report.items,
            [make_item("uninstall.core.uv_missing", "core", "uv", STATUS.ABSENT,
                       I18N.t("uninstall.detail.uv_missing"))],
        )

    def test_uv_list_failure(self):
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            for runner, expected_detail in (
                (RecordingRunner([install.CommandFailed("k", "list", detail="boom")]),
                 "boom"),
                (RecordingRunner([res(rc=1, stderr="bad", argv=("/usr/bin/uv", "tool",
                                                                "list"))]),
                 "/usr/bin/uv tool list (rc=1): bad"),
            ):
                report = self._cleaner(runner).run()
                self.assertEqual(
                    report.items,
                    [make_item("uninstall.core.uv_item", "core", "uv tool list",
                               STATUS.FAILED, expected_detail, "uv tool list")],
                )

    def test_uv_installed_and_absent_packages(self):
        stdout = "miloco-cli v1\n- supervisor v2\n"
        runner = RecordingRunner([
            res(stdout),
            res(""),
            res(""),
        ])
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = self._cleaner(runner).run()
        self.assertEqual(
            report.items,
            [
                make_item("uninstall.cli_removed", "core", "miloco-cli", STATUS.DONE),
                make_item("uninstall.core.uv_item", "core", "miloco", STATUS.ABSENT),
                make_item("uninstall.core.uv_item", "core", "supervisor", STATUS.DONE),
            ],
        )
        self.assertEqual(
            runner.argvs,
            [
                ["/usr/bin/uv", "tool", "list"],
                ["/usr/bin/uv", "tool", "uninstall", "miloco-cli"],
                ["/usr/bin/uv", "tool", "uninstall", "supervisor"],
            ],
        )
        self.assertEqual([c["timeout"] for c in runner.calls], [15, 60, 60])

    def test_uv_uninstall_keys_and_failures(self):
        runner = RecordingRunner([
            res("miloco v1\nmiloco-cli v1\nsupervisor v1\n"),
            res(rc=1, stderr="locked", argv=("/usr/bin/uv", "tool", "uninstall",
                                             "miloco-cli")),
            res(""),
            res(""),
        ])
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = self._cleaner(runner).run()
        self.assertEqual(
            report.items,
            [
                make_item("uninstall.core.uv_item", "core",
                          "uv tool uninstall miloco-cli", STATUS.FAILED,
                          "/usr/bin/uv tool uninstall miloco-cli (rc=1): locked",
                          "uv tool uninstall miloco-cli"),
                make_item("uninstall.miloco_removed", "core", "miloco", STATUS.DONE),
                make_item("uninstall.core.uv_item", "core", "supervisor", STATUS.DONE),
            ],
        )

        runner = RecordingRunner([res("miloco v1\n"),
                                  install.CommandFailed("k", "u", detail="exc")])
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = self._cleaner(runner).run()
        self.assertEqual(
            report.items,
            [make_item("uninstall.core.uv_item", "core", "miloco-cli", STATUS.ABSENT),
             make_item("uninstall.core.uv_item", "core", "uv tool uninstall miloco",
                       STATUS.FAILED, "exc", "uv tool uninstall miloco"),
             make_item("uninstall.core.uv_item", "core", "supervisor", STATUS.ABSENT)],
        )


# ---------------------------------------------------------------------------
# 报告渲染与状态前缀
# ---------------------------------------------------------------------------


class PrintItemTest(unittest.TestCase):
    def _print(self, item):
        ui = FakeUI()
        install._print_cleanup_item(ui, item)
        return ui, ui.console.lines

    def test_done_with_and_without_detail(self):
        _ui, lines = self._print(
            make_item("uninstall.hermes.cron", "hermes", "target", STATUS.DONE)
        )
        self.assertEqual(lines, ["  [green]✓[/green] Managed cron jobs: target"])
        _ui, lines = self._print(
            make_item("uninstall.hermes.cron", "hermes", "target", STATUS.DONE, "note")
        )
        self.assertEqual(
            lines,
            ["  [green]✓[/green] Managed cron jobs: target [dim]— note[/dim]"],
        )

    def test_failed_and_blocked_use_cross_and_remediation(self):
        for status in (STATUS.FAILED, STATUS.BLOCKED):
            ui, lines = self._print(
                make_item("uninstall.hermes.cron", "hermes", "target", status,
                          "why", "run me")
            )
            self.assertEqual(
                lines,
                [
                    "  [red]✗[/red] Managed cron jobs: target [dim]— why[/dim]",
                    "    [yellow]fix manually:[/yellow] [cyan]run me[/cyan]",
                ],
            )
        ui, lines = self._print(
            make_item("uninstall.hermes.cron", "hermes", "target", STATUS.FAILED)
        )
        self.assertEqual(lines, ["  [red]✗[/red] Managed cron jobs: target"])

    def test_remediation_hidden_for_non_failures(self):
        _ui, lines = self._print(
            make_item("uninstall.hermes.cron", "hermes", "target", STATUS.DONE,
                      "", "run me")
        )
        self.assertEqual(lines, ["  [green]✓[/green] Managed cron jobs: target"])

    def test_other_statuses_print_status_label_and_detail(self):
        for status, label in (
            (STATUS.ABSENT, "not present"),
            (STATUS.KEPT, "kept"),
            (STATUS.SKIPPED, "skipped (data directory removed)"),
        ):
            _ui, lines = self._print(
                make_item("uninstall.hermes.cron", "hermes", "target", status, "why")
            )
            self.assertEqual(
                lines,
                [
                    f"  [dim]–[/dim] Managed cron jobs: target [dim]({label})[/dim]"
                    " [dim]— why[/dim]"
                ],
            )

    def test_unknown_key_is_printed_verbatim(self):
        _ui, lines = self._print(make_item("custom.key", "hermes", "t", STATUS.DONE))
        self.assertEqual(lines, ["  [green]✓[/green] custom.key: t"])


class PrintReportTest(unittest.TestCase):
    def _report(self, platform, *, detected, items=()):
        report = install.PlatformReport(platform, detected=detected,
                                        cli_available=True)
        for item in items:
            report.add(item)
        return report

    def test_all_ok_and_sections(self):
        ui = FakeUI()
        reports = [
            self._report("hermes", detected=False),
            self._report("openclaw", detected=True,
                         items=[make_item("uninstall.openclaw.plugin", "openclaw",
                                          "cmd", STATUS.DONE)]),
            self._report("core", detected=True,
                         items=[make_item("uninstall.core.uv_item", "core", "miloco",
                                          STATUS.ABSENT)]),
        ]
        ok = install._print_uninstall_report(
            ui, reports, [], tool_source="installed_manifest"
        )
        self.assertTrue(ok)
        self.assertEqual(
            ui.console.lines,
            [
                "",
                "[bold cyan]Hermes[/bold cyan]",
                "  [dim]– not installed[/dim]",
                "",
                "[bold cyan]OpenClaw[/bold cyan]",
                "  [dim]tool names source: installed_manifest[/dim]",
                "  [green]✓[/green] OpenClaw plugin: cmd",
                "",
                "[bold cyan]Core tools[/bold cyan]",
                "  [dim]–[/dim] uv tool: miloco [dim](not present)[/dim]",
                "",
                "[dim]Restart the hermes/openclaw gateway to unload the plugin; "
                "this installer does not restart it automatically.[/dim]",
                "",
            ],
        )
        self.assertEqual(ui.oks, [I18N.t("uninstall.summary.done")])
        self.assertEqual(ui.warns, [])

    def test_incomplete_report_and_home_items(self):
        ui = FakeUI()
        reports = [
            self._report("hermes", detected=True,
                         items=[make_item("uninstall.hermes.cron", "hermes", TAG,
                                          STATUS.BLOCKED)]),
            self._report("openclaw", detected=False),
        ]
        home_items = [make_item("uninstall.home.deleted", "core", "/home", STATUS.FAILED,
                                "nope")]
        ok = install._print_uninstall_report(ui, reports, home_items)
        self.assertFalse(ok)
        text = ui.console.text
        self.assertIn("[bold cyan]Data directories[/bold cyan]", text)
        self.assertIn("  [red]✗[/red] Data directory deleted: /home [dim]— nope[/dim]",
                      text)
        self.assertEqual(ui.oks, [])
        self.assertEqual(ui.warns, [I18N.t("uninstall.summary.incomplete")])
        self.assertIn("[dim]fix manually[/dim]", ui.console.lines)
        # hermes 已检测 → 仍有 gateway 提示
        self.assertIn(f"[dim]{I18N.t('uninstall.summary.gateway_hint')}[/dim]",
                      ui.console.lines)

    def test_no_gateway_hint_without_platform_detection(self):
        ui = FakeUI()
        ok = install._print_uninstall_report(
            ui, [self._report("core", detected=True)], []
        )
        self.assertTrue(ok)
        self.assertNotIn(f"[dim]{I18N.t('uninstall.summary.gateway_hint')}[/dim]",
                         ui.console.lines)
        self.assertNotIn("[dim]tool names source:[/dim]", ui.console.text)

    def test_home_items_alone_ok(self):
        ui = FakeUI()
        ok = install._print_uninstall_report(
            ui, [], [make_item("uninstall.home.kept", "core", "/x", STATUS.KEPT)]
        )
        self.assertTrue(ok)
        self.assertEqual(ui.oks, [I18N.t("uninstall.summary.done")])


# ---------------------------------------------------------------------------
# home 删除决策与路径安全
# ---------------------------------------------------------------------------


class DecideHomeDeletionsTest(unittest.TestCase):
    def _candidates(self, root):
        one = root / "one"
        two = root / "two"
        one.mkdir()
        two.mkdir()
        return (_candidate(root / "missing"), _candidate(one), _candidate(two))

    def test_non_interactive_defaults_to_keep_without_prompting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = self._candidates(root)
            ui = FakeUI()
            decisions = install.decide_home_deletions(
                candidates, ui=ui, interactive=False
            )
            self.assertEqual(
                decisions,
                {candidates[1].real_path: False, candidates[2].real_path: False},
            )
            self.assertEqual(ui.prompts, [])

    def test_interactive_prompt_text_default_and_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = self._candidates(root)
            ui = FakeUI(answers=[True, False])
            decisions = install.decide_home_deletions(
                candidates, ui=ui, interactive=True
            )
            self.assertEqual(decisions, {candidates[1].real_path: True,
                                          candidates[2].real_path: False})
            self.assertEqual(
                ui.prompts,
                [
                    {"message": I18N.t("uninstall.home.ask", str(candidates[1].path)),
                     "default": False},
                    {"message": I18N.t("uninstall.home.ask", str(candidates[2].path)),
                     "default": False},
                ],
            )

    def test_truthy_answer_is_coerced_to_bool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = self._candidates(root)
            ui = FakeUI(answers=["yes"])
            decisions = install.decide_home_deletions(
                candidates, ui=ui, interactive=True
            )
            self.assertEqual(decisions, {candidates[1].real_path: True,
                                          candidates[2].real_path: False})

    def test_keyboard_interrupt_keeps_and_stops_asking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = self._candidates(root)
            ui = FakeUI(answers=[KeyboardInterrupt()])
            decisions = install.decide_home_deletions(
                candidates, ui=ui, interactive=True
            )
            self.assertEqual(decisions, {candidates[1].real_path: False,
                                          candidates[2].real_path: False})
            self.assertEqual(len(ui.prompts), 1)


class DeleteHomesTest(unittest.TestCase):
    def test_kept_skipped_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kept = root / "kept"
            kept.mkdir()
            gone = root / "gone"
            gone.mkdir()
            removed = root / "removed"
            removed.mkdir()
            (removed / "f").write_text("x", encoding="utf-8")
            candidates = (_candidate(kept), _candidate(gone), _candidate(removed))
            gone.rmdir()  # 盘点后消失
            decisions = {c.real_path: (c.path != kept) for c in candidates}
            items = install.delete_homes(candidates, decisions, home=root / "user",
                                         env={})
            self.assertEqual(
                items,
                [
                    make_item("uninstall.home.kept", "core", str(kept), STATUS.KEPT),
                    make_item("uninstall.home.deleted", "core", str(gone),
                              STATUS.SKIPPED),
                    make_item("uninstall.home.deleted", "core", str(removed),
                              STATUS.DONE),
                ],
            )
            self.assertTrue(kept.exists())
            self.assertFalse(removed.exists())

    def test_deeper_paths_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "p"
            child = parent / "c"
            child.mkdir(parents=True)
            parent_candidate = _candidate(parent)
            child_candidate = _candidate(child)
            decisions = {parent_candidate.real_path: True,
                         child_candidate.real_path: True}
            items = install.delete_homes((parent_candidate, child_candidate),
                                         decisions, home=root / "user", env={})
            self.assertEqual([i.target for i in items], [str(child), str(parent)])
            self.assertTrue(all(i.status is STATUS.DONE for i in items))
            self.assertFalse(parent.exists())

    def test_unsafe_and_oserror_are_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unsafe = root / "home"
            unsafe.mkdir()
            broken = root / "broken"
            broken.mkdir()
            candidates = (_candidate(unsafe), _candidate(broken))
            decisions = {candidates[0].real_path: True,
                         candidates[1].real_path: True}
            real_delete = install._delete_home

            def fake_delete(candidate, *, home, protected):
                if candidate.path == unsafe:
                    raise install.UnsafePath("uninstall.home.unsafe", str(unsafe),
                                             detail="refused")
                raise OSError("disk error")

            with mock.patch.object(install, "_delete_home", fake_delete):
                items = install.delete_homes(candidates, decisions,
                                             home=root / "user", env={})
            self.assertEqual(
                items,
                [
                    make_item("uninstall.home.unsafe", "core", str(unsafe),
                              STATUS.FAILED, "refused"),
                    make_item("uninstall.home.deleted", "core", str(broken),
                              STATUS.FAILED, "disk error"),
                ],
            )
            self.assertTrue(unsafe.exists())
            self.assertTrue(broken.exists())
            self.assertIs(real_delete, install._delete_home)


class DeleteHomeSafetyTest(unittest.TestCase):
    def _candidate_with(self, path, **kwargs):
        base = _candidate(path)
        return dataclasses.replace(base, **kwargs)

    def test_relative_path_is_refused(self):
        candidate = self._candidate_with(Path("relative"))
        with self.assertRaises(install.UnsafePath) as ctx:
            install._delete_home(candidate, home=Path("/home/u"),
                                 protected=[Path("/home/u")])
        self.assertEqual(ctx.exception.detail, "path is not absolute")

    def test_ancestor_of_home_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user = root / "user"
            user.mkdir()
            candidate = _candidate(root)
            with self.assertRaises(install.UnsafePath) as ctx:
                install._delete_home(candidate, home=user,
                                     protected=[user, root / ".hermes"])
            self.assertEqual(
                ctx.exception.detail,
                f"refuses to delete an ancestor of {user.resolve()}",
            )
            self.assertTrue(root.exists())

    def test_self_home_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user = root / "user"
            user.mkdir()
            candidate = _candidate(user)
            with self.assertRaises(install.UnsafePath) as ctx:
                install._delete_home(candidate, home=user,
                                     protected=[user])
            self.assertIn("refuses to delete an ancestor of", ctx.exception.detail)
            self.assertTrue(user.exists())

    def test_missing_path_reports_stat_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            candidate = _candidate(home)
            home.rmdir()
            with self.assertRaises(install.UnsafePath) as ctx:
                install._delete_home(candidate, home=root / "user", protected=[])
            self.assertTrue(ctx.exception.detail)

    def test_inode_change_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            candidate = self._candidate_with(home, identity=(0, 0))
            with self.assertRaises(install.UnsafePath) as ctx:
                install._delete_home(candidate, home=root / "user", protected=[])
            self.assertEqual(ctx.exception.detail, "inode changed since inventory")
            self.assertTrue(home.exists())

    def test_symlink_is_unlinked_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            (target / "keep").write_text("x", encoding="utf-8")
            link = root / "link"
            link.symlink_to(target)
            candidate = _candidate(link)
            self.assertTrue(candidate.is_symlink)
            install._delete_home(candidate, home=root / "user", protected=[])
            self.assertFalse(os.path.lexists(link))
            self.assertTrue((target / "keep").exists())


# ---------------------------------------------------------------------------
# LaunchAgent / 子进程出口 / 入口
# ---------------------------------------------------------------------------


class RemoveLaunchAgentTest(unittest.TestCase):
    def test_non_darwin_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ui = FakeUI()
            with mock.patch.object(install.Path, "home", classmethod(lambda cls: home)), \
                 mock.patch.object(install._platform, "system", return_value="Linux"), \
                 mock.patch.object(install.subprocess, "run") as run:
                install._remove_launchagent(ui)
            run.assert_not_called()
            self.assertEqual(ui.oks, [])

    def test_darwin_boots_out_and_removes_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist = home / "Library" / "LaunchAgents" / f"{install._LAUNCHD_LABEL}.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text("x", encoding="utf-8")
            ui = FakeUI()
            with mock.patch.object(install.Path, "home", classmethod(lambda cls: home)), \
                 mock.patch.object(install._platform, "system", return_value="Darwin"), \
                 mock.patch.object(install.subprocess, "run") as run, \
                 mock.patch.object(install.os, "getuid", return_value=501):
                install._remove_launchagent(ui)
            run.assert_called_once_with(
                ["launchctl", "bootout", f"gui/501/{install._LAUNCHD_LABEL}"],
                capture_output=True, timeout=20,
            )
            self.assertFalse(plist.exists())
            self.assertEqual(ui.oks, [f"LaunchAgent removed: {plist}"])

    def test_darwin_bootout_failure_still_removes_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist = home / "Library" / "LaunchAgents" / f"{install._LAUNCHD_LABEL}.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text("x", encoding="utf-8")
            ui = FakeUI()
            with mock.patch.object(install.Path, "home", classmethod(lambda cls: home)), \
                 mock.patch.object(install._platform, "system", return_value="Darwin"), \
                 mock.patch.object(install.subprocess, "run",
                                   side_effect=OSError("no launchctl")):
                install._remove_launchagent(ui)
            self.assertFalse(plist.exists())
            self.assertEqual(ui.oks, [f"LaunchAgent removed: {plist}"])

    def test_darwin_unlink_failure_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist = home / "Library" / "LaunchAgents" / f"{install._LAUNCHD_LABEL}.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text("x", encoding="utf-8")
            ui = FakeUI()
            real_unlink = Path.unlink

            def fake_unlink(self, *args, **kwargs):
                if self == plist:
                    raise OSError("denied")
                return real_unlink(self, *args, **kwargs)

            with mock.patch.object(install.Path, "home", classmethod(lambda cls: home)), \
                 mock.patch.object(install._platform, "system", return_value="Darwin"), \
                 mock.patch.object(install.subprocess, "run"), \
                 mock.patch.object(Path, "unlink", fake_unlink):
                install._remove_launchagent(ui)
            self.assertEqual(ui.warns, ["remove LaunchAgent plist failed: denied"])
            self.assertTrue(plist.exists())


class SubprocessRunnerTest(unittest.TestCase):
    def test_success_without_stdin_gets_eof(self):
        result = install.SubprocessRunner()(
            ["sh", "-c", "read x; echo rc=$?"], timeout=10
        )
        self.assertEqual(result.argv, ("sh", "-c", "read x; echo rc=$?"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "rc=1\n")
        self.assertEqual(result.stderr, "")

    def test_stdin_text_is_piped(self):
        result = install.SubprocessRunner()(
            ["sh", "-c", "cat"], stdin="abc\n", timeout=10
        )
        self.assertEqual(result.stdout, "abc\n")
        self.assertEqual(result.returncode, 0)

    def test_env_is_used(self):
        result = install.SubprocessRunner()(
            ["sh", "-c", "echo $MILOCO_TEST_VAR"], env={"MILOCO_TEST_VAR": "v"},
            timeout=10,
        )
        self.assertEqual(result.stdout, "v\n")

    def test_nonzero_does_not_raise(self):
        result = install.SubprocessRunner()(["sh", "-c", "echo bad >&2; exit 3"],
                                            timeout=10)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, "bad\n")

    def test_timeout_maps_to_command_timeout(self):
        with self.assertRaises(install.CommandTimeout) as ctx:
            install.SubprocessRunner()(["sh", "-c", "sleep 5"], timeout=0.05)
        self.assertEqual(ctx.exception.status, STATUS.FAILED)
        self.assertEqual(ctx.exception.target, "sh -c sleep 5")
        self.assertEqual(ctx.exception.detail, "timeout after 0.05s")

    def test_oserror_maps_to_command_failed(self):
        with self.assertRaises(install.CommandFailed) as ctx:
            install.SubprocessRunner()(["/nonexistent/binary"], timeout=5)
        self.assertIs(ctx.exception.status, STATUS.FAILED)
        self.assertEqual(ctx.exception.target, "/nonexistent/binary")
        self.assertIn("No such file", ctx.exception.detail)


class RunUninstallTest(unittest.TestCase):
    def _run(self, root, *, env=None, extra_setup=None):
        user = root / "user"
        user.mkdir(exist_ok=True)
        miloco = root / "miloco"
        ui = FakeUI()
        with mock.patch.object(install.Path, "home", classmethod(lambda cls: user)), \
             mock.patch.object(install, "_resolve_uninstall_home", return_value=miloco), \
             mock.patch.object(install.shutil, "which", return_value=None), \
             mock.patch.object(install, "_remove_launchagent") as launch, \
             mock.patch.object(install.sys, "stdin",
                               mock.Mock(isatty=lambda: False)), \
             mock.patch.dict(os.environ, env or {}, clear=True):
            if extra_setup:
                extra_setup(root)
            rc = install.run_uninstall(ui, args=__import__("argparse").Namespace(
                agent_platform=""))
        return rc, ui, launch, miloco, user

    def test_clean_environment_returns_zero_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc, ui, launch, miloco, _user = self._run(root)
            self.assertEqual(rc, 0)
            launch.assert_called_once_with(ui)
            self.assertFalse(miloco.exists(), "inventory must not create MILOCO_HOME")
            self.assertEqual(ui.oks, [I18N.t("uninstall.summary.done")])

    def test_existing_home_is_kept_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def setup(_root):
                (root / "miloco").mkdir()
            rc, ui, _launch, miloco, _user = self._run(
                root, env={"MILOCO_HOME": str(root / "miloco")}, extra_setup=setup
            )
            self.assertEqual(rc, 0)
            self.assertTrue(miloco.exists())
            self.assertIn("[bold cyan]Data directories[/bold cyan]", ui.console.text)
            self.assertIn(
                f"  [dim]–[/dim] Data directory kept: {miloco} [dim](kept)[/dim]",
                ui.console.lines,
            )

    def test_hermes_trace_without_cli_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def setup(_root):
                plugin = root / ".hermes" / "plugins" / "miloco"
                (plugin / "miloco-plugin").mkdir(parents=True)
            rc, ui, _launch, _miloco, _user = self._run(
                root,
                env={"MILOCO_HOME": str(root / "miloco"),
                     "HERMES_HOME": str(root / ".hermes")},
                extra_setup=setup,
            )
            self.assertEqual(rc, 1)
            self.assertEqual(ui.oks, [])
            self.assertEqual(ui.warns, [I18N.t("uninstall.summary.incomplete")])
            self.assertIn("Hermes", ui.console.text)
            self.assertFalse((root / ".hermes" / "plugins" / "miloco").exists())


# ---------------------------------------------------------------------------
# 编排层：run_uninstall 传给协作者的每一个实参
# ---------------------------------------------------------------------------


class RunUninstallWiringTest(unittest.TestCase):
    def _invoke(self, *, env_home, explicit_miloco=None, args=None,
                hermes_detected=False, openclaw_detected=False):
        root = Path("/somewhere/unused")  # Path.home 被替换，仅用于字符串比较
        inventory = _inventory(
            root,
            hermes=_hermes_facts(root / ".hermes" / "config.yaml",
                                 detected=hermes_detected),
            openclaw=_openclaw_facts(root / ".openclaw", detected=openclaw_detected),
            tool_names=("t1",), tool_source="installed_manifest",
        )
        reports = {
            "hermes": install.PlatformReport("hermes", hermes_detected, True),
            "openclaw": install.PlatformReport("openclaw", openclaw_detected, True),
            "core": install.PlatformReport("core", True, True),
        }
        decisions = {Path("/decided"): True}
        home_items = [make_item("uninstall.home.kept", "core", "/x", STATUS.KEPT)]
        ui = FakeUI()

        class FakeCleaner:
            def __init__(inner, **kwargs):
                inner.kwargs = kwargs
                self.seen.append((type(inner).__name__, kwargs, self.name))
            def run(inner):
                return reports[self.name]

        seen = []
        env = dict(os.environ)
        # 测试要控制 MILOCO_HOME，不能被宿主机（如 mac 的全局 export）污染。
        env.pop("MILOCO_HOME", None)
        if explicit_miloco is not None:
            env["MILOCO_HOME"] = explicit_miloco
        with mock.patch.object(install.Path, "home",
                               classmethod(lambda cls: Path("/home/user"))), \
             mock.patch.object(install, "_resolve_uninstall_home",
                               return_value=Path(env_home)) as resolve, \
             mock.patch.object(install, "_remove_launchagent") as launch, \
             mock.patch.object(install, "SubprocessRunner") as runner_cls, \
             mock.patch.object(install, "build_inventory", return_value=inventory) as build, \
             mock.patch.object(install, "decide_home_deletions",
                               return_value=decisions) as decide, \
             mock.patch.object(install, "delete_homes",
                               return_value=home_items) as delete, \
             mock.patch.object(install, "_print_uninstall_report",
                               return_value=True) as report, \
             mock.patch.object(install, "HermesCleaner") as hermes_cls, \
             mock.patch.object(install, "OpenClawCleaner") as openclaw_cls, \
             mock.patch.object(install, "CoreCleaner") as core_cls, \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(install.sys, "stdin",
                               mock.Mock(isatty=lambda: True)):
            def make(cls, name):
                def factory(**kwargs):
                    seen.append((name, kwargs))
                    return mock.Mock(run=mock.Mock(return_value=reports[name]))
                return factory
            hermes_cls.side_effect = make(hermes_cls, "hermes")
            openclaw_cls.side_effect = make(openclaw_cls, "openclaw")
            core_cls.side_effect = make(core_cls, "core")
            clean_args = args or __import__("argparse").Namespace(agent_platform="hermes")
            miloco_home = (
                None
                if "MILOCO_HOME" in os.environ
                else install._resolve_uninstall_home()
            )
            rc = install.run_uninstall(
                ui, args=clean_args, miloco_home=miloco_home
            )

        return {
            "rc": rc, "ui": ui, "seen": seen, "inventory": inventory,
            "reports": reports, "decisions": decisions, "home_items": home_items,
            "launch": launch, "runner_cls": runner_cls, "build": build,
            "decide": decide, "delete": delete, "report": report,
            "resolve": resolve, "home": Path("/home/user"),
        }

    def test_exact_collaborator_arguments(self):
        out = self._invoke(env_home="/resolved/home", hermes_detected=True,
                           openclaw_detected=True)
        self.assertEqual(out["rc"], 0)
        self.assertEqual(out["ui"].infos, [I18N.t("uninstall.title")])
        out["launch"].assert_called_once_with(out["ui"])

        build = out["build"].call_args
        self.assertEqual(build.kwargs["home"], out["home"])
        self.assertEqual(build.kwargs["agent_platform_hint"], "hermes")
        self.assertIs(build.kwargs["run"], out["runner_cls"].return_value)
        self.assertEqual(build.kwargs["env"]["MILOCO_HOME"], "/resolved/home")
        out["resolve"].assert_called_once_with()

        decide = out["decide"].call_args
        self.assertEqual(decide.args[0], out["inventory"].candidates)
        self.assertIs(decide.kwargs["ui"], out["ui"])
        self.assertTrue(decide.kwargs["interactive"])

        self.assertEqual(
            out["seen"],
            [
                ("hermes", {"ui": out["ui"], "inv": out["inventory"],
                            "decisions": out["decisions"],
                            "run": out["runner_cls"].return_value,
                            "env": build.kwargs["env"]}),
                ("openclaw", {"ui": out["ui"], "inv": out["inventory"],
                              "run": out["runner_cls"].return_value}),
                ("core", {"ui": out["ui"], "run": out["runner_cls"].return_value}),
            ],
        )
        done = out["delete"].call_args
        self.assertEqual(done.args[0], out["inventory"].candidates)
        self.assertEqual(done.args[1], out["decisions"])
        self.assertEqual(done.kwargs["home"], out["home"])
        self.assertEqual(done.kwargs["env"], build.kwargs["env"])

        report = out["report"].call_args
        self.assertIs(report.args[0], out["ui"])
        self.assertEqual(
            report.args[1],
            [out["reports"]["hermes"], out["reports"]["openclaw"],
             out["reports"]["core"]],
        )
        self.assertEqual(report.args[2], out["home_items"])
        self.assertEqual(report.kwargs["tool_source"], "installed_manifest")

    def test_explicit_miloco_home_is_not_rewritten(self):
        out = self._invoke(env_home="/resolved/home",
                           explicit_miloco="/explicit/home",
                           args=__import__("argparse").Namespace(agent_platform=""))
        out["resolve"].assert_not_called()
        self.assertEqual(out["build"].call_args.kwargs["env"]["MILOCO_HOME"],
                         "/explicit/home")
        self.assertEqual(out["build"].call_args.kwargs["agent_platform_hint"], "")

    def test_missing_hint_attribute_defaults_to_empty(self):
        args = __import__("argparse").Namespace()  # 没有 agent_platform
        out = self._invoke(env_home="/resolved/home", args=args)
        self.assertEqual(out["build"].call_args.kwargs["agent_platform_hint"], "")

    def test_undetected_platforms_still_get_reports(self):
        out = self._invoke(env_home="/resolved/home", hermes_detected=False,
                           openclaw_detected=False)
        self.assertEqual(
            [type(r).__name__ for r in out["report"].call_args.args[1]],
            ["PlatformReport", "PlatformReport", "PlatformReport"],
        )
        hermes_report, openclaw_report, _core = out["report"].call_args.args[1]
        self.assertEqual((hermes_report.platform, hermes_report.detected,
                          hermes_report.cli_available), ("hermes", False, False))
        self.assertEqual((openclaw_report.platform, openclaw_report.detected,
                          openclaw_report.cli_available), ("openclaw", False, False))


# ---------------------------------------------------------------------------
# 补充：盘点与 OpenClaw 清理的剩余分支
# ---------------------------------------------------------------------------


class DiscoverExtraTest(unittest.TestCase):
    def test_exact_candidate_paths_with_other_env_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / ".hermes").mkdir(parents=True)
            custom = root / "custom"
            (home / ".hermes" / ".env").write_text(
                "API_SERVER_KEY=BEAR\n"
                f"MILOCO_HOME={custom}\n",
                encoding="utf-8",
            )
            candidates = install.discover_home_candidates(
                env={}, home=home, hermes_home=home / ".hermes",
                openclaw_state_dir=home / ".openclaw", hint="",
            )
            self.assertEqual(
                [c.path for c in candidates],
                [custom, home / ".hermes" / "miloco", home / ".openclaw" / "miloco"],
            )

    def test_bashrc_exports_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            bashrc_home = root / "from-bashrc"
            (home / ".bashrc").write_text(
                f"export MILOCO_HOME={bashrc_home}\n", encoding="utf-8"
            )
            candidates = install.discover_home_candidates(
                env={}, home=home, hermes_home=home / ".hermes",
                openclaw_state_dir=home / ".openclaw", hint="",
            )
            self.assertIn(bashrc_home, [c.path for c in candidates])
            self.assertEqual(
                [c for c in candidates if c.path == bashrc_home][0].sources,
                frozenset({"shell_rc"}),
            )

    def test_empty_env_value_does_not_stop_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / ".hermes").mkdir(parents=True)
            custom = root / "custom"
            (home / ".hermes" / ".env").write_text(
                f"MILOCO_HOME={custom}\n", encoding="utf-8"
            )
            candidates = install.discover_home_candidates(
                env={"MILOCO_HOME": ""}, home=home,
                hermes_home=home / ".hermes",
                openclaw_state_dir=home / ".openclaw", hint="",
            )
            paths = [c.path for c in candidates]
            self.assertIn(custom, paths)
            self.assertIn(home / ".hermes" / "miloco", paths)
            self.assertIn(home / ".openclaw" / "miloco", paths)
            self.assertNotIn(Path(""), paths)


class BuildInventoryExtraTest(unittest.TestCase):
    def _home(self, root):
        home = root / "user"
        home.mkdir(exist_ok=True)
        return home

    def test_openclaw_inspect_source_when_no_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            runner = RecordingRunner([
                res(json.dumps({"contracts": {"tools": ["i1"]}})),
                res(json.dumps({"plugins": []})),
                res(json.dumps({"jobs": []})),
            ])
            inv = install.build_inventory(
                env={}, home=home, agent_platform_hint="",
                which=lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
                run=runner,
            )
            self.assertEqual(inv.tool_names, ("i1",))
            self.assertEqual(inv.tool_names_source, "inspect")
            self.assertEqual(
                runner.calls[0]["argv"],
                ["/usr/bin/openclaw", "plugins", "inspect", PLUGIN_ID, "--json"],
            )
            self.assertEqual(inv.home, home)

    def test_skills_scan_error_yields_empty_tuple(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            skills = home / ".hermes" / "skills"
            skills.mkdir(parents=True)
            real_iterdir = Path.iterdir

            def fake(self):
                if self == skills:
                    raise OSError("denied")
                return real_iterdir(self)

            with mock.patch.object(Path, "iterdir", fake):
                inv = install.build_inventory(
                    env={}, home=home, agent_platform_hint="",
                    which=lambda _n: None, run=RecordingRunner([]),
                )
            self.assertEqual(inv.hermes.skills, ())

    def test_bearer_alone_detects_hermes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            miloco = home / ".hermes" / "miloco"
            miloco.mkdir(parents=True)
            (miloco / "config.json").write_text(
                json.dumps({"agent": {"auth_bearer": "BEAR"}}), encoding="utf-8"
            )
            (home / ".hermes" / ".env").write_text(
                "API_SERVER_KEY=BEAR\n", encoding="utf-8"
            )
            inv = install.build_inventory(
                env={"MILOCO_HOME": str(miloco)}, home=home,
                agent_platform_hint="", which=lambda _n: None,
                run=RecordingRunner([]),
            )
            # 目录本身没有 adapter/cache；只有 .env 的 bearer 证明
            self.assertEqual(inv.hermes.env_managed_bearers, ("BEAR",))
            self.assertTrue(inv.hermes.detected)

    def test_openclaw_detected_by_cli_plugin_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            runner = RecordingRunner([
                res(json.dumps({"contracts": {"tools": ["t1"]}})),  # inspect
                res(json.dumps({"plugins": [{"id": PLUGIN_ID}]})),  # plugin list
                res(json.dumps({"jobs": []})),                      # cron list
            ])
            inv = install.build_inventory(
                env={}, home=home, agent_platform_hint="",
                which=lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
                run=runner,
            )
            self.assertTrue(inv.openclaw.plugin_record)
            self.assertTrue(inv.openclaw.detected)
            self.assertEqual(inv.tool_names_source, "inspect")

    def test_openclaw_detected_by_managed_cron_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            runner = RecordingRunner([
                res(json.dumps({"contracts": {"tools": ["t1"]}})),
                res(json.dumps([])),
                res(json.dumps({"jobs": [
                    {"id": "1", "name": "n", "description": f"{TAG} a"},
                ]})),
            ])
            inv = install.build_inventory(
                env={}, home=home, agent_platform_hint="",
                which=lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
                run=runner,
            )
            self.assertFalse(inv.openclaw.plugin_record)
            self.assertEqual(len(inv.openclaw.managed_crons), 1)
            self.assertTrue(inv.openclaw.detected)

    def test_openclaw_cron_field_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            runner = RecordingRunner([
                res(json.dumps({"contracts": {"tools": ["t1"]}})),
                res(json.dumps([])),
                res(json.dumps({"jobs": [
                    {"jobId": "j2", "description": f"{TAG} b"},
                    {"id": "j3", "name": "n3"},
                    {"id": "j4", "name": "n4", "description": f"x {TAG}"},
                ]})),
            ])
            inv = install.build_inventory(
                env={}, home=home, agent_platform_hint="",
                which=lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
                run=runner,
            )
            self.assertEqual(
                inv.openclaw.managed_crons,
                (install.ManagedCron("j2", "", f"{TAG} b"),
                 install.ManagedCron("j4", "n4", f"x {TAG}")),
            )

    def test_openclaw_probe_exception_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            runner = RecordingRunner([
                res(json.dumps({"contracts": {"tools": ["t1"]}})),
                res(json.dumps([])),
                install.CommandFailed("k", "list", detail="probe-boom"),
            ])
            inv = install.build_inventory(
                env={}, home=home, agent_platform_hint="",
                which=lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
                run=runner,
            )
            self.assertEqual(inv.openclaw.cli_probe_error, "probe-boom")
            self.assertEqual(inv.openclaw.managed_crons, ())

    def test_facts_carry_venv_python_and_plugin_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            python = home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
            installs = home / ".openclaw" / "plugins" / "installs.json"
            installs.parent.mkdir(parents=True)
            installs.write_text(f'["{PLUGIN_ID}"]', encoding="utf-8")
            runner = RecordingRunner([res(json.dumps([]))])
            inv = install.build_inventory(
                env={}, home=home, agent_platform_hint="",
                which=lambda _n: None, run=runner,
            )
            self.assertEqual(inv.hermes.venv_python, python)
            self.assertTrue(inv.openclaw.plugin_record)
            self.assertIs(inv.openclaw.plugin_record, True)


class OpenClawExtraTest(unittest.TestCase):
    def _facts(self, root, **overrides):
        state = root / ".openclaw"
        state.mkdir(exist_ok=True)
        return _openclaw_facts(state, **overrides)

    def _cleaner(self, root, *, facts, runner):
        return install.OpenClawCleaner(
            ui=FakeUI(), inv=_inventory(root, openclaw=facts), run=runner
        )

    def test_tools_allow_get_exception_full_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            command = "openclaw config get tools.alsoAllow --json"
            runner = RecordingRunner([install.CommandFailed("k", command, detail="boom")])
            self.assertEqual(
                self._cleaner(root, facts=facts, runner=runner)._clean_tools_allow(),
                make_item("uninstall.openclaw.tools_allow", "openclaw", command,
                          STATUS.FAILED, "boom", command),
            )

    def test_tools_allow_set_exception_full_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            managed = install.FALLBACK_OPENCLAW_TOOLS
            set_cmd = (f"openclaw config set tools.alsoAllow "
                       f"{json.dumps(['keep'])} --strict-json")
            runner = RecordingRunner([
                res(json.dumps([managed[0], "keep"])),
                install.CommandFailed("k", set_cmd, detail="set-boom"),
            ])
            self.assertEqual(
                self._cleaner(root, facts=facts, runner=runner)._clean_tools_allow(),
                make_item("uninstall.openclaw.tools_allow", "openclaw", set_cmd,
                          STATUS.FAILED, "set-boom", set_cmd),
            )

    def test_cron_rm_exception_continues_with_next_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            jobs = [
                {"id": "1", "name": "one", "description": f"{TAG} a"},
                {"id": "2", "name": "two", "description": f"{TAG} b"},
            ]
            runner = RecordingRunner([
                res(json.dumps({"jobs": jobs})),
                install.CommandTimeout("k", "rm", detail="slow"),
                res(""),
                res(json.dumps({"jobs": []})),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items,
                [
                    make_item("uninstall.openclaw.cron", "openclaw", "one",
                              STATUS.FAILED, "slow", "openclaw cron rm 1"),
                    make_item("uninstall.openclaw.cron", "openclaw", "two",
                              STATUS.DONE),
                ],
            )
            self.assertEqual([c["argv"][3] for c in runner.calls[1:3]], ["1", "2"])

    def test_jobs_without_description_are_not_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            runner = RecordingRunner([
                res(json.dumps({"jobs": [
                    {"id": "1", "name": "no-desc"},
                    {"id": "2", "name": "also-none"},
                ]})),
            ])
            self.assertEqual(
                self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons(),
                [make_item("uninstall.openclaw.cron", "openclaw", TAG, STATUS.ABSENT)],
            )

    def test_verify_remaining_targets_and_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            runner = RecordingRunner([
                res(json.dumps({"jobs": [
                    {"id": "1", "name": "one", "description": f"{TAG} a"},
                    {"id": "2", "name": "two", "description": f"{TAG} b"},
                ]})),
                res(""),
                res(""),
                res(json.dumps({"jobs": [
                    {"id": "only-id", "description": f"{TAG} c"},
                    {"name": "only-name", "description": f"{TAG} d"},
                    {"id": "user", "name": "user-job", "description": "nope"},
                ]})),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items[-1],
                make_item("uninstall.openclaw.cron", "openclaw",
                          "only-id, only-name", STATUS.FAILED,
                          I18N.t("uninstall.detail.cron_remaining"),
                          "openclaw cron list --all --json"),
            )


# ---------------------------------------------------------------------------
# 卸载入口依赖：runtime pointer / 平台探测 / main 早分流
# ---------------------------------------------------------------------------


class RuntimePointerTest(unittest.TestCase):
    def test_default_pointer_path(self):
        with mock.patch.object(install.Path, "home",
                               classmethod(lambda cls: Path("/home/u"))):
            self.assertEqual(install._default_runtime_pointer(),
                             Path("/home/u/.config/miloco/default.env"))

    def test_pointer_candidates_prefer_env_and_dedupe(self):
        with mock.patch.object(install.Path, "home",
                               classmethod(lambda cls: Path("/home/u"))):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    install._runtime_pointer_candidates(),
                    [Path("/home/u/.config/miloco/default.env")],
                )
            same = "/home/u/.config/miloco/default.env"
            with mock.patch.dict(os.environ, {"MILOCO_RUNTIME_ENV": same},
                                 clear=True):
                self.assertEqual(install._runtime_pointer_candidates(),
                                 [Path(same)])
            with mock.patch.dict(os.environ,
                                 {"MILOCO_RUNTIME_ENV": "~/profile.env"},
                                 clear=True):
                self.assertEqual(
                    install._runtime_pointer_candidates(),
                    [Path(os.path.expanduser("~")) / "profile.env",
                     Path("/home/u/.config/miloco/default.env")],
                )

    def test_read_pointer_parses_shell_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "default.env"
            path.write_text(
                "# comment\n"
                "MILOCO_HOME=/a\n"
                'MILOCO_AGENT_PLATFORM="x y"\n'
                "EQUALS=a=b\n"
                "EMPTY=\n"
                "_UNDER=1\n"
                "1BAD=v\n"
                "DASH-KEY=v\n"
                "TWO words\n"
                "NOEQUALS\n"
                "SPACED = v\n"
                "HASH=a#b\n"
                "MILOCO_HOME=/last\n",
                encoding="utf-8",
            )
            self.assertEqual(
                install._read_runtime_pointer(path),
                {
                    "MILOCO_HOME": "/last",
                    "MILOCO_AGENT_PLATFORM": "x y",
                    "EQUALS": "a=b",
                    "EMPTY": "",
                    "_UNDER": "1",
                    "HASH": "a#b",
                },
            )

    def test_unreadable_pointer_is_empty(self):
        self.assertEqual(install._read_runtime_pointer(Path("/nope/none")), {})

    def test_resolve_uninstall_home_precedence(self):
        with mock.patch.object(install.Path, "home",
                               classmethod(lambda cls: Path("/home/u"))):
            with mock.patch.dict(os.environ,
                                 {"MILOCO_HOME": "~/explicit"}, clear=True):
                self.assertEqual(install._resolve_uninstall_home(),
                                 Path(os.path.expanduser("~")) / "explicit")

            with tempfile.TemporaryDirectory() as tmp:
                profile = Path(tmp) / "profile.env"
                profile.write_text("MILOCO_HOME=/from-profile\n", encoding="utf-8")
                with mock.patch.dict(os.environ,
                                     {"MILOCO_RUNTIME_ENV": str(profile)},
                                     clear=True), \
                     mock.patch.object(install, "_detect_installed_agent_platform",
                                       return_value="hermes") as detect:
                    self.assertEqual(install._resolve_uninstall_home(),
                                     Path("/from-profile"))
                    detect.assert_not_called()

                with mock.patch.dict(os.environ, {}, clear=True), \
                     mock.patch.object(install, "_detect_installed_agent_platform",
                                       return_value="hermes"):
                    self.assertEqual(
                        install._resolve_uninstall_home(),
                        Path("/home/u/.hermes/miloco"),
                    )
                with mock.patch.dict(os.environ, {}, clear=True), \
                     mock.patch.object(install, "_detect_installed_agent_platform",
                                       return_value=None), \
                     mock.patch.object(install.Path, "home",
                                       classmethod(lambda cls: Path("/home/u"))):
                    self.assertEqual(
                        install._resolve_uninstall_home(),
                        Path("/home/u/.openclaw/miloco"),
                    )


class DefaultMilocoHomeTest(unittest.TestCase):
    def test_hermes_home_env(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/opt/hermes"},
                             clear=True), \
             mock.patch.object(install.Path, "home",
                               classmethod(lambda cls: Path("/home/u"))):
            self.assertEqual(install._default_miloco_home("hermes"),
                             Path("/opt/hermes/miloco"))
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(install.Path, "home",
                               classmethod(lambda cls: Path("/home/u"))):
            self.assertEqual(install._default_miloco_home("hermes"),
                             Path("/home/u/.hermes/miloco"))
            self.assertEqual(install._default_miloco_home("openclaw"),
                             Path("/home/u/.openclaw/miloco"))
            self.assertEqual(install._default_miloco_home("other"),
                             Path("/home/u/.openclaw/miloco"))


class DetectPlatformTest(unittest.TestCase):
    def _detect(self, root, env=None, which=None, units=()):
        home = root / "home"
        home.mkdir(exist_ok=True)
        unit_dir = home / ".config" / "systemd" / "user"
        if units:
            unit_dir.mkdir(parents=True, exist_ok=True)
            for unit in units:
                (unit_dir / unit).write_text("", encoding="utf-8")
        with mock.patch.object(install.Path, "home",
                               classmethod(lambda cls: home)), \
             mock.patch.dict(os.environ, env or {}, clear=True), \
             mock.patch.object(install.shutil, "which",
                               side_effect=which or (lambda _n: None)):
            return install._detect_installed_agent_platform()

    def test_explicit_env_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for value in ("hermes", "openclaw"):
                self.assertEqual(
                    self._detect(root, env={"MILOCO_AGENT_PLATFORM": value}), value
                )
            self.assertEqual(
                self._detect(root, env={"MILOCO_AGENT_PLATFORM": "bogus"},
                             which=lambda n: "/x" if n == "hermes" else None),
                "hermes",
            )

    def test_pointer_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "p.env"
            pointer.write_text("MILOCO_AGENT_PLATFORM=openclaw\n", encoding="utf-8")
            self.assertEqual(
                self._detect(root, env={"MILOCO_RUNTIME_ENV": str(pointer)}),
                "openclaw",
            )
            pointer.write_text("MILOCO_AGENT_PLATFORM=invalid\n", encoding="utf-8")
            self.assertIsNone(
                self._detect(root, env={"MILOCO_RUNTIME_ENV": str(pointer)})
            )

    def test_hermes_home_env_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes = root / "hh"
            hermes.mkdir()
            self.assertEqual(
                self._detect(root, env={"HERMES_HOME": str(hermes)}), "hermes"
            )
            self.assertIsNone(
                self._detect(root, env={"HERMES_HOME": str(root / "missing")})
            )

    def test_dot_directories_and_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "home" / ".hermes").mkdir(parents=True)
            self.assertEqual(self._detect(root), "hermes")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "home" / ".openclaw").mkdir(parents=True)
            self.assertEqual(self._detect(root), "openclaw")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "home" / ".hermes").mkdir(parents=True)
            (root / "home" / ".openclaw").mkdir(parents=True)
            self.assertIsNone(self._detect(root))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self._detect(root, units=("hermes-gateway.service",)),
                             "hermes")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self._detect(root, units=("clawdbot-gateway.service",)),
                             "openclaw")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self._detect(root, units=("openclaw-gateway.service",)),
                             "openclaw")

    def test_cli_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                self._detect(root, which=lambda n: "/x" if n == "hermes" else None),
                "hermes",
            )
            self.assertEqual(
                self._detect(root, which=lambda n: "/x" if n == "openclaw" else None),
                "openclaw",
            )
            self.assertIsNone(self._detect(root, which=lambda n: "/x"))
            self.assertIsNone(self._detect(root, which=lambda n: None))


class MainUninstallDispatchTest(unittest.TestCase):
    def _main(self, *, rc, interactive=True, tty_fallback=False, pointer=None):
        args = __import__("argparse").Namespace(uninstall=True, lang="en")
        plat = install.Platform(os="linux", arch="x86_64",
                                is_interactive=interactive, lang="en")
        with mock.patch.object(install, "parse_args", return_value=args), \
             mock.patch.object(install.Platform, "detect", return_value=plat), \
             mock.patch.object(install, "I18n") as i18n_cls, \
             mock.patch.object(install, "UI") as ui_cls, \
             mock.patch.object(install, "_try_tty_fallback",
                               return_value=tty_fallback), \
             mock.patch.object(install, "run_uninstall", return_value=rc) as run, \
             mock.patch.object(install, "_decide_agent_platform") as decide, \
             mock.patch.object(install, "_default_runtime_pointer",
                               return_value=pointer), \
             mock.patch.object(install.Path, "home",
                               classmethod(lambda cls: Path("/home/u"))):
            with self.assertRaises(SystemExit) as ctx:
                install.main()
        return ctx.exception.code, run, decide, ui_cls

    def test_uninstall_dispatches_before_platform_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "default.env"
            pointer.write_text("x", encoding="utf-8")
            code, run, decide, ui_cls = self._main(rc=0, pointer=pointer)
            self.assertEqual(code, 0)
            run.assert_called_once()
            args = run.call_args.kwargs["args"]
            self.assertTrue(args.uninstall)
            self.assertIs(run.call_args.args[0], ui_cls.return_value)
            decide.assert_not_called()
            self.assertFalse(pointer.exists())
            # 早分流不创建任何数据目录
            self.assertFalse((Path("/home/u") / ".openclaw" / "miloco").exists())

    def test_uninstall_nonzero_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "default.env"
            code, run, _decide, _ui = self._main(rc=1, pointer=pointer)
            self.assertEqual(code, 1)
            run.assert_called_once()

    def test_non_interactive_with_tty_fallback_still_uninstalls(self):
        with tempfile.TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "default.env"
            code, run, _decide, _ui = self._main(
                rc=0, interactive=False, tty_fallback=True, pointer=pointer
            )
            self.assertEqual(code, 0)
            run.assert_called_once()

    def test_missing_pointer_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "missing.env"
            code, _run, _decide, _ui = self._main(rc=0, pointer=pointer)
            self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# 收尾：异常分支全字段 / 空 detail / 边界输入
# ---------------------------------------------------------------------------


def _empty_detail_failure(target="t"):
    """detail 为空时 str(exc) 退回 key，用于区分 `or str(exc)` 的变异。"""
    return install.CommandFailed("k", target)


class ExceptionFieldTest(unittest.TestCase):
    def test_delete_home_unsafe_error_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user = root / "user"
            user.mkdir()
            cases = []

            relative = dataclasses.replace(_candidate(root / "x"), path=Path("rel"))
            cases.append((relative, user, [user], "path is not absolute"))

            cases.append((_candidate(root), user, [user], None))  # ancestor
            missing = root / "missing"
            candidate = _candidate(root / "gone")
            candidate = dataclasses.replace(candidate, path=missing,
                                            real_path=missing.resolve())
            cases.append((candidate, user, [], None))  # stat 失败
            inode = dataclasses.replace(_candidate(user), identity=(0, 0))
            cases.append((inode, user, [], "inode changed since inventory"))

            for cand, home, protected, detail in cases:
                with self.subTest(detail=detail, path=cand.path):
                    with self.assertRaises(install.UnsafePath) as ctx:
                        install._delete_home(cand, home=home, protected=protected)
                    exc = ctx.exception
                    self.assertEqual(exc.key, "uninstall.home.unsafe")
                    self.assertEqual(exc.target, str(cand.path))
                    self.assertIs(exc.status, STATUS.FAILED)
                    if detail is not None:
                        self.assertEqual(exc.detail, detail)
                    else:
                        self.assertTrue(exc.detail)

    def test_atomic_write_mkstemp_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_text("old", encoding="utf-8")
            real = install.tempfile.mkstemp
            seen = {}

            def fake(*args, **kwargs):
                seen.update(kwargs)
                return real(*args, **kwargs)

            with mock.patch.object(install.tempfile, "mkstemp", fake):
                install._atomic_write_text(path, "new")
            self.assertEqual(seen["prefix"], ".miloco-uninstall-")
            self.assertEqual(seen["suffix"], ".tmp")
            # mac 的 /var 是 /private/var 的符号链接，比较解析后的路径。
            self.assertEqual(Path(seen["dir"]).resolve(), Path(tmp).resolve())

    def test_build_candidate_broken_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "miloco"
            home.mkdir()
            for parts in (("agent_platform", "hermes"),
                          (".install-cache", "hermes-plugin")):
                link = home.joinpath(*parts)
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(root / "missing-target")
            candidate = _candidate(home)
            self.assertTrue(candidate.has_hermes_adapter)
            self.assertTrue(candidate.has_install_cache)

    def test_build_candidate_stat_error_leaves_identity_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "miloco"
            home.mkdir()
            real_stat = Path.stat

            def fake(self, *args, **kwargs):
                # path.exists()/is_file() 会带 follow_symlinks；身份 stat 不带
                if self == home and "follow_symlinks" not in kwargs:
                    raise OSError("boom")
                return real_stat(self, *args, **kwargs)

            with mock.patch.object(Path, "stat", fake):
                candidate = _candidate(home)
            self.assertIsNone(candidate.identity)

    def test_subprocess_oserror_target_joins_argv(self):
        with self.assertRaises(install.CommandFailed) as ctx:
            install.SubprocessRunner()(["/nonexistent/binary", "arg1", "arg2"])
        self.assertEqual(ctx.exception.target, "/nonexistent/binary arg1 arg2")
        self.assertIs(ctx.exception.status, STATUS.FAILED)


class HermesCronExceptionTest(unittest.TestCase):
    def _cleaner(self, root, *, facts, runner):
        return install.HermesCleaner(
            ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
            run=runner, env={"HERMES_HOME": "x"},
        )

    def test_cli_rm_exception_full_item_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res(f"  a1 [active]\n    Name:  {TAG} one\n"
                    f"  b2 [active]\n    Name:  {TAG} two\n"),
                install.CommandFailed("k", "remove-one"),  # 空 detail
                res(""),
                res("usage: rm"),
                res(""),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items,
                [
                    make_item("uninstall.hermes.cron", "hermes", f"{TAG} one",
                              STATUS.FAILED, "k", "hermes cron rm a1"),
                    make_item("uninstall.hermes.cron", "hermes", f"{TAG} two",
                              STATUS.DONE),
                ],
            )
            self.assertEqual(runner.argvs[2], ["/usr/bin/hermes", "cron", "rm", "a1"])
            self.assertEqual(runner.argvs[3], ["/usr/bin/hermes", "cron", "rm", "b2"])

    def test_local_api_rm_exception_full_item_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
            facts = _hermes_facts(hermes_home / "config.yaml", cli=None,
                                  venv_python=python)
            runner = RecordingRunner([
                res(json.dumps([{"id": "j1", "name": f"{TAG} one"},
                                {"id": "j2", "name": f"{TAG} two"}])),
                install.CommandTimeout("k", "remove-one"),
                res(json.dumps({"ok": True})),
                res(json.dumps([])),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items,
                [
                    make_item("uninstall.hermes.cron", "hermes", f"{TAG} one",
                              STATUS.FAILED, "k", "hermes cron rm j1"),
                    make_item("uninstall.hermes.cron", "hermes", f"{TAG} two",
                              STATUS.DONE),
                ],
            )
            self.assertEqual(
                runner.argvs[1],
                [str(python), "-c", install._HERMES_CRON_REMOVE_SNIPPET, "j1"],
            )
            self.assertEqual(runner.argvs[2][-1], "j2")

    def test_empty_id_continues_with_next_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res(f"    Name:  x {TAG} no-id\n"),
                res("usage: rm"),
                res(""),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(len(items), 1)
            self.assertIs(items[0].status, STATUS.BLOCKED)

    def test_job_with_id_but_tag_not_prefix_is_not_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res(f"  a1 [active]\n    Name:  user {TAG} suffix\n"),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items, [make_item("uninstall.hermes.cron", "hermes", TAG, STATUS.ABSENT)]
            )
            self.assertEqual(len(runner.calls), 2)

    def test_verify_error_full_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res(f"  a1 [active]\n    Name:  {TAG} one\n"),
                res(""),
                res("usage: rm"),
                res(rc=3, stderr="verify-down",
                    argv=("/usr/bin/hermes", "cron", "list", "--all")),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items[1],
                make_item("uninstall.hermes.cron", "hermes", TAG, STATUS.FAILED,
                          "cli: /usr/bin/hermes cron list --all (rc=3): verify-down",
                          "hermes cron list"),
            )

    def test_multiple_remaining_jobs_joined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  cli="/usr/bin/hermes")
            runner = RecordingRunner([
                res("usage: rm"),
                res(f"  a1 [active]\n    Name:  {TAG} one\n"),
                res(""),
                res("usage: rm"),
                res(f"  a1 [active]\n    Name:  {TAG} one\n"
                    f"  b2 [active]\n    Name:  {TAG} two\n"),
            ])
            items = self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons()
            self.assertEqual(
                items[-1],
                make_item("uninstall.hermes.cron", "hermes", f"{TAG} one, {TAG} two",
                          STATUS.FAILED,
                          I18N.t("uninstall.detail.cron_remaining"),
                          "hermes cron list"),
            )

    def test_plugins_present_empty_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  cli="/usr/bin/hermes")
            runner = RecordingRunner([install.CommandFailed("k", "list")])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(cleaner._plugins_present(), (None, "k"))

    def test_plugin_dir_empty_detail_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            plugin = hermes_home / "plugins" / install.HERMES_PLUGIN_ID
            plugin.mkdir(parents=True)
            facts = _hermes_facts(hermes_home / "config.yaml",
                                  cli="/usr/bin/hermes", plugin_dir=plugin)
            runner = RecordingRunner([install.CommandFailed("k", "remove")])
            item = self._cleaner(root, facts=facts, runner=runner)._remove_plugin_dir()
            self.assertEqual(
                item,
                make_item("uninstall.hermes.plugin_dir", "hermes", str(plugin),
                          STATUS.DONE, "k"),
            )

    def test_verify_cli_error_full_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  cli="/usr/bin/hermes")
            runner = RecordingRunner([res(rc=2, stderr="down",
                                          argv=("/usr/bin/hermes", "plugins", "list",
                                                "--plain", "--no-bundled"))])
            cleaner = self._cleaner(root, facts=facts, runner=runner)
            self.assertEqual(
                cleaner._verify_cli(),
                make_item(
                    "uninstall.hermes.cli_verify", "hermes",
                    str(root / ".hermes" / "plugins" / "miloco"), STATUS.FAILED,
                    "/usr/bin/hermes plugins list --plain --no-bundled "
                    "(rc=2): down",
                    "hermes plugins list --plain --no-bundled",
                ),
            )

    def test_clean_env_kept_reasons_use_i18n(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / ".hermes" / ".env"
            env_file.parent.mkdir()
            env_file.write_text("API_SERVER_KEY=user\n", encoding="utf-8")
            facts = _hermes_facts(root / ".hermes" / "config.yaml", env_file=env_file)
            cleaner = self._cleaner(root, facts=facts, runner=RecordingRunner([]))
            self.assertEqual(
                cleaner._clean_env(),
                make_item("uninstall.hermes.env", "hermes", str(env_file), STATUS.KEPT,
                          I18N.t("uninstall.detail.kept_api_key")),
            )

    def test_managed_homes_cache_only_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "rc-cache"
            (home / ".install-cache" / "hermes-plugin").mkdir(parents=True)
            candidate = _candidate(home, sources=("shell_rc",))
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=(candidate,)),
                decisions={}, run=RecordingRunner([]), env={},
            )
            self.assertEqual(cleaner._managed_homes(), {home.resolve()})

    def test_home_artifact_deleted_and_missing_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "gone"
            home.mkdir()
            candidate = _candidate(home)
            facts = _hermes_facts(root / ".hermes" / "config.yaml")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts, candidates=(candidate,)),
                decisions={candidate.real_path: True},
                run=RecordingRunner([]), env={},
            )
            self.assertEqual(
                cleaner._remove_adapters(),
                [make_item("uninstall.hermes.adapter", "hermes",
                           str(home / "agent_platform" / "hermes"), STATUS.ABSENT)],
            )

    def test_backup_invalid_json_and_full_absent_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  plugin_dir=root / ".hermes" / "plugins" / "miloco")
            state = facts.plugin_dir / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=RecordingRunner([]), env={},
            )
            state.write_text("{", encoding="utf-8")
            self.assertEqual(
                cleaner._backup_plugin_state(),
                make_item("uninstall.hermes.plugin_state_backup", "hermes", str(state),
                          STATUS.ABSENT),
            )
            state.write_text('{"deliver": {}}', encoding="utf-8")
            self.assertEqual(
                cleaner._backup_plugin_state(),
                make_item("uninstall.hermes.plugin_state_backup", "hermes", str(state),
                          STATUS.ABSENT),
            )

    def test_backup_counter_with_frozen_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  plugin_dir=root / ".hermes" / "plugins" / "miloco")
            state = facts.plugin_dir / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"deliver": {"target": "t"}}', encoding="utf-8")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=RecordingRunner([]), env={},
            )
            backup_dir = root / ".hermes" / "miloco-uninstall-backup"
            backup_dir.mkdir()
            (backup_dir / "20260102-030405-plugin-state.json").write_text("old")

            class Frozen(install.datetime.datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 1, 2, 3, 4, 5)

            with mock.patch.object(install.datetime, "datetime", Frozen):
                item = cleaner._backup_plugin_state()
            self.assertEqual(
                Path(item.target),
                backup_dir / "20260102-030405-1-plugin-state.json",
            )

    def test_backup_failure_full_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts = _hermes_facts(root / ".hermes" / "config.yaml",
                                  plugin_dir=root / ".hermes" / "plugins" / "miloco")
            state = facts.plugin_dir / "miloco-plugin" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"deliver": {"target": "t"}}', encoding="utf-8")
            cleaner = install.HermesCleaner(
                ui=FakeUI(), inv=_inventory(root, hermes=facts), decisions={},
                run=RecordingRunner([]), env={},
            )
            with mock.patch.object(install.shutil, "copy2",
                                   side_effect=OSError("disk full")):
                item = cleaner._backup_plugin_state()
            self.assertEqual(
                item,
                make_item("uninstall.hermes.plugin_state_backup", "hermes", str(state),
                          STATUS.FAILED,
                          I18N.t("uninstall.detail.backup_failed", "disk full")),
            )


class EnvAndRcEdgeTest(unittest.TestCase):
    def test_env_detail_joins_two_kept_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "API_SERVER_KEY=user\nMILOCO_HOME=/elsewhere\nOTHER=1\n",
                encoding="utf-8",
            )
            cleaner = install.EnvFileCleaner(path, i18n=I18N)
            plan = cleaner.plan(managed_bearers={"managed"},
                                managed_homes={Path("/managed")})
            self.assertEqual(
                cleaner.apply(plan),
                make_item("uninstall.hermes.env", "hermes", str(path), STATUS.KEPT,
                          I18N.t("uninstall.detail.kept_api_key") + "; "
                          + I18N.t("uninstall.detail.kept_env_home")),
            )

    def test_env_plan_value_ending_in_x(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MILOCO_HOME=/managedX\n", encoding="utf-8")
            plan = install.EnvFileCleaner(path, i18n=I18N).plan(
                managed_bearers=set(), managed_homes={Path("/managedX")}
            )
            self.assertEqual(plan.remove, (0,))

    def test_rc_marker_then_plain_then_managed_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text(
                "# miloco runtime path\n"
                "# plain comment\n"
                "export MILOCO_HOME=/managed\n",
                encoding="utf-8",
            )
            plan = install.ShellRcCleaner(path, i18n=I18N).plan(
                managed_homes={Path("/managed")}
            )
            self.assertEqual(plan.remove, (0, 2))

    def test_rc_marker_with_foreign_then_managed_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text(
                "# miloco runtime path\n"
                "export MILOCO_HOME=/user\n"
                "export MILOCO_HOME=/managedX\n",
                encoding="utf-8",
            )
            plan = install.ShellRcCleaner(path, i18n=I18N).plan(
                managed_homes={Path("/managed"), Path("/managedX")}
            )
            self.assertEqual(plan.remove, (0, 2))
            self.assertEqual(plan.kept_exports, ((1, Path("/user")),))

    def test_rc_marker_managed_export_then_another_managed_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text(
                "# miloco runtime path\n"
                "export MILOCO_HOME=/managed\n"
                "export MILOCO_HOME=/managed\n",
                encoding="utf-8",
            )
            plan = install.ShellRcCleaner(path, i18n=I18N).plan(
                managed_homes={Path("/managed")}
            )
            self.assertEqual(plan.remove, (0, 1, 2))

    def test_rc_export_value_ending_in_x(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text("export MILOCO_HOME=/managedX\n", encoding="utf-8")
            plan = install.ShellRcCleaner(path, i18n=I18N).plan(
                managed_homes={Path("/managedX")}
            )
            self.assertEqual(plan.remove, (0,))

    def test_rc_plain_line_then_managed_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text(
                "# plain\n"
                "export MILOCO_HOME=/managed\n",
                encoding="utf-8",
            )
            plan = install.ShellRcCleaner(path, i18n=I18N).plan(
                managed_homes={Path("/managed")}
            )
            self.assertEqual(plan.remove, (1,))


class OpenClawExceptionTest(unittest.TestCase):
    def _facts(self, root, **overrides):
        state = root / ".openclaw"
        state.mkdir(exist_ok=True)
        return _openclaw_facts(state, **overrides)

    def _cleaner(self, root, *, facts, runner):
        return install.OpenClawCleaner(
            ui=FakeUI(), inv=_inventory(root, openclaw=facts), run=runner
        )

    def test_init_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cleaner = install.OpenClawCleaner(
                ui=FakeUI(), inv=_inventory(root), run=RecordingRunner([]),
            )
            self.assertIsNotNone(cleaner.ui)
            self.assertEqual(cleaner.report.platform, "openclaw")
            self.assertFalse(cleaner.report.cli_available)
            cleaner = install.OpenClawCleaner(
                ui=FakeUI(), inv=_inventory(root, openclaw=self._facts(root, cli="/x")),
                run=RecordingRunner([]),
            )
            self.assertTrue(cleaner.report.cli_available)

    def test_every_exception_branch_with_empty_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)

            cleaner = self._cleaner(
                root, facts=facts,
                runner=RecordingRunner([install.CommandFailed("k", "uninstall")]),
            )
            self.assertEqual(
                cleaner._uninstall_plugin(),
                make_item("uninstall.openclaw.plugin", "openclaw",
                          f"openclaw plugins uninstall {PLUGIN_ID}", STATUS.FAILED,
                          "k", f"openclaw plugins uninstall {PLUGIN_ID}"),
            )

            cleaner = self._cleaner(
                root, facts=facts,
                runner=RecordingRunner([install.CommandFailed("k", "get")]),
            )
            self.assertEqual(
                cleaner._clean_tools_allow(),
                make_item("uninstall.openclaw.tools_allow", "openclaw",
                          "openclaw config get tools.alsoAllow --json", STATUS.FAILED,
                          "k", "openclaw config get tools.alsoAllow --json"),
            )

            managed = install.FALLBACK_OPENCLAW_TOOLS[0]
            cleaner = self._cleaner(
                root, facts=facts,
                runner=RecordingRunner([
                    res(json.dumps([managed])),
                    install.CommandFailed("k", "set"),
                ]),
            )
            item = cleaner._clean_tools_allow()
            self.assertEqual(item.status, STATUS.FAILED)
            self.assertEqual(item.detail, "k")
            self.assertEqual(item.platform, "openclaw")
            self.assertEqual(item.remediation, item.target)

            cleaner = self._cleaner(
                root, facts=facts,
                runner=RecordingRunner([install.CommandFailed("k", "unset")]),
            )
            path = f'plugins.entries["{PLUGIN_ID}"]'
            self.assertEqual(
                cleaner._clean_plugin_entry(),
                make_item("uninstall.openclaw.plugin_entry", "openclaw",
                          f"openclaw config unset {path}", STATUS.FAILED,
                          "k", f"openclaw config unset {path}"),
            )

            list_cmd = "openclaw cron list --all --json"
            cleaner = self._cleaner(
                root, facts=facts,
                runner=RecordingRunner([install.CommandFailed("k", "list")]),
            )
            self.assertEqual(
                cleaner._remove_managed_crons(),
                [make_item("uninstall.openclaw.cron", "openclaw", list_cmd,
                           STATUS.FAILED, "k", "openclaw gateway start")],
            )

            cleaner = self._cleaner(
                root, facts=facts,
                runner=RecordingRunner([install.CommandFailed("k", "verify")]),
            )
            self.assertEqual(
                cleaner._verify_plugin_absent(),
                make_item("uninstall.openclaw.verify", "openclaw",
                          "openclaw plugins list --json", STATUS.FAILED,
                          "k", "openclaw plugins list --json"),
            )

    def test_cron_list_rc_full_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            runner = RecordingRunner([
                res(rc=1, stderr="down",
                    argv=(cli, "cron", "list", "--all", "--json"))
            ])
            self.assertEqual(
                self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons(),
                [make_item("uninstall.openclaw.cron", "openclaw",
                           "openclaw cron list --all --json", STATUS.FAILED,
                           f"{cli} cron list --all --json (rc=1): down",
                           "openclaw gateway start")],
            )

    def test_cron_rm_exception_empty_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            runner = RecordingRunner([
                res(json.dumps({"jobs": [
                    {"id": "1", "name": "n", "description": f"{TAG} a"}]})),
                install.CommandFailed("k", "rm"),
                res(json.dumps({"jobs": []})),
            ])
            self.assertEqual(
                self._cleaner(root, facts=facts, runner=runner)._remove_managed_crons(),
                [make_item("uninstall.openclaw.cron", "openclaw", "n", STATUS.FAILED,
                           "k", "openclaw cron rm 1")],
            )

    def test_cron_verify_exception_empty_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            runner = RecordingRunner([
                res(json.dumps({"jobs": [
                    {"id": "1", "name": "n", "description": f"{TAG} a"}]})),
                res(""),
                install.CommandFailed("k", "list"),
            ])
            items = self._cleaner(root, facts=facts,
                                  runner=runner)._remove_managed_crons()
            self.assertEqual(
                items[-1],
                make_item("uninstall.openclaw.cron", "openclaw",
                          "openclaw cron list --all --json", STATUS.FAILED,
                          "k", "openclaw cron list --all --json"),
            )

    def test_remaining_job_without_name_or_id_target_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = "/usr/bin/openclaw"
            facts = self._facts(root, cli=cli)
            runner = RecordingRunner([
                res(json.dumps({"jobs": [{"id": "1", "description": f"{TAG} a"}]})),
                res(""),
                res(json.dumps({"jobs": [{"description": f"{TAG} b"}]})),
            ])
            items = self._cleaner(root, facts=facts,
                                  runner=runner)._remove_managed_crons()
            self.assertEqual(
                items[-1],
                make_item("uninstall.openclaw.cron", "openclaw", "", STATUS.FAILED,
                          I18N.t("uninstall.detail.cron_remaining"),
                          "openclaw cron list --all --json"),
            )


class CoreCleanerWiringTest(unittest.TestCase):
    def test_init_fields(self):
        ui = FakeUI()
        cleaner = install.CoreCleaner(ui=ui, run=RecordingRunner([]))
        self.assertIs(cleaner.ui, ui)
        self.assertEqual(cleaner.report.platform, "core")
        self.assertTrue(cleaner.report.detected)
        self.assertTrue(cleaner.report.cli_available)

    def test_which_uv_argument(self):
        seen = []
        with mock.patch.object(install.shutil, "which",
                               side_effect=lambda name: seen.append(name) or None):
            install.CoreCleaner(ui=FakeUI(), run=RecordingRunner([])).run()
        self.assertEqual(seen, ["uv"])

    def test_empty_detail_exceptions(self):
        with mock.patch.object(install.shutil, "which", return_value="/usr/bin/uv"):
            report = install.CoreCleaner(
                ui=FakeUI(), run=RecordingRunner([install.CommandFailed("k", "list")])
            ).run()
            self.assertEqual(
                report.items,
                [make_item("uninstall.core.uv_item", "core", "uv tool list",
                           STATUS.FAILED, "k", "uv tool list")],
            )
            report = install.CoreCleaner(
                ui=FakeUI(), run=RecordingRunner([res("miloco v1\n"),
                                                  install.CommandFailed("k", "u")])
            ).run()
            self.assertEqual(
                report.items[1],
                make_item("uninstall.core.uv_item", "core",
                          "uv tool uninstall miloco", STATUS.FAILED,
                          "k", "uv tool uninstall miloco"),
            )


class I18nBehaviourTest(unittest.TestCase):
    def test_missing_dir_returns_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            i18n = install.I18n("en", Path(tmp))
            self.assertEqual(i18n.t("k"), "k")
            self.assertEqual(i18n.t("k %1 %2", "a", "b"), "k a b")

    def test_unknown_language_falls_back_to_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "i18n").mkdir()
            (Path(tmp) / "i18n" / "en.json").write_text(
                json.dumps({"k": "english"}), encoding="utf-8"
            )
            self.assertEqual(install.I18n("xx", Path(tmp)).t("k"), "english")
            # 已知语言直接用该语言
            (Path(tmp) / "i18n" / "zh.json").write_text(
                json.dumps({"k": "中文"}), encoding="utf-8"
            )
            self.assertEqual(install.I18n("zh", Path(tmp)).t("k"), "中文")

    def test_real_english_file_loads(self):
        i18n = install.I18n("en", SCRIPTS)
        self.assertEqual(i18n.t("uninstall.title"), "Uninstalling miloco components...")


class ParseEdgeTest(unittest.TestCase):
    def test_openclaw_plugin_json_matches_name_and_plugin_id(self):
        self.assertTrue(
            install._openclaw_plugin_in_json(
                json.dumps({"plugins": [{"name": PLUGIN_ID}]})
            )
        )
        self.assertTrue(
            install._openclaw_plugin_in_json(
                json.dumps({"plugins": [{"pluginId": PLUGIN_ID}]})
            )
        )
        self.assertFalse(
            install._openclaw_plugin_in_json(json.dumps({"plugins": [{"id": "other"}]}))
        )

    def test_runtime_pointer_unbalanced_quote_does_not_stop_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.env"
            path.write_text(
                'BROKEN="unclosed\nAFTER=1\n', encoding="utf-8"
            )
            self.assertEqual(install._read_runtime_pointer(path), {"AFTER": "1"})

    def test_runtime_pointer_two_token_assignment_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.env"
            path.write_text("KEY=v extra\nAFTER=1\n", encoding="utf-8")
            self.assertEqual(install._read_runtime_pointer(path), {"AFTER": "1"})


class InventoryEdgeTest(unittest.TestCase):
    def test_openclaw_cron_job_without_id_or_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "user"
            home.mkdir()
            runner = RecordingRunner([
                res(json.dumps({"contracts": {"tools": ["t1"]}})),
                res(json.dumps([])),
                res(json.dumps({"jobs": [{"description": f"{TAG} a"}]})),
            ])
            inv = install.build_inventory(
                env={}, home=home, agent_platform_hint="",
                which=lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
                run=runner,
            )
            self.assertEqual(
                inv.openclaw.managed_crons,
                (install.ManagedCron("", "", f"{TAG} a"),),
            )

    def test_openclaw_probe_exception_empty_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "user"
            home.mkdir()
            runner = RecordingRunner([
                res(json.dumps({"contracts": {"tools": ["t1"]}})),
                res(json.dumps([])),
                install.CommandFailed("k", "list"),
            ])
            inv = install.build_inventory(
                env={}, home=home, agent_platform_hint="",
                which=lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
                run=runner,
            )
            self.assertEqual(inv.openclaw.cli_probe_error, "k")


class CronLiveMessageTest(unittest.TestCase):
    def _live(self, runner, *, cli="/usr/bin/hermes", venv=None):
        with mock.patch.object(install, "_hermes_venv_python", return_value=venv):
            return install._hermes_cron_live(
                run=runner, hermes_home=Path("/hh"), env={}, cli=cli
            )

    def test_no_remove_subcommand_exact_error(self):
        runner = RecordingRunner([res("usage: hermes cron\n  list\n")])
        self.assertEqual(
            self._live(runner),
            ("cli", (), "cli: hermes cron has no remove subcommand"),
        )

    def test_help_exception_empty_detail(self):
        runner = RecordingRunner([install.CommandFailed("k", "help")])
        self.assertEqual(self._live(runner), ("cli", (), "cli: k"))

    def test_list_exception_empty_detail(self):
        runner = RecordingRunner([res("rm"), install.CommandFailed("k", "list")])
        self.assertEqual(self._live(runner), ("cli", (), "cli: k"))

    def test_local_api_job_without_name(self):
        python = Path("/venv/python")
        runner = RecordingRunner([res(json.dumps([{"id": "x"}]))])
        self.assertEqual(
            self._live(runner, cli=None, venv=python),
            ("local_api", (install.ManagedCron("x", ""),), ""),
        )

    def test_local_api_exception_empty_detail(self):
        python = Path("/venv/python")
        runner = RecordingRunner([install.CommandFailed("k", "list")])
        self.assertEqual(
            self._live(runner, cli=None, venv=python),
            ("local_api", (), "local_api: k"),
        )


class ReportDefaultToolSourceTest(unittest.TestCase):
    def test_default_tool_source_is_not_printed(self):
        ui = FakeUI()
        report = install.PlatformReport("openclaw", detected=True, cli_available=True)
        report.add(make_item("uninstall.openclaw.plugin", "openclaw", "c", STATUS.DONE))
        install._print_uninstall_report(ui, [report], [])
        self.assertNotIn("XXXX", ui.console.text)
        self.assertNotIn("tool names source", ui.console.text)


class UndetectedCliAvailableTest(unittest.TestCase):
    def test_cli_available_reflects_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "user"
            home.mkdir()
            miloco = root / "miloco"

            class Noop:
                def __init__(self, **kwargs):
                    pass

            with mock.patch.object(install.Path, "home",
                                   classmethod(lambda cls: home)), \
                 mock.patch.object(install, "_resolve_uninstall_home",
                                   return_value=miloco), \
                 mock.patch.object(
                     install.shutil, "which",
                     side_effect=lambda n: {"hermes": "/bin/hermes",
                                            "openclaw": "/bin/openclaw"}.get(n)), \
                 mock.patch.object(install, "_remove_launchagent"), \
                 mock.patch.object(install.sys, "stdin",
                                   mock.Mock(isatty=lambda: False)), \
                 mock.patch.dict(os.environ,
                                 {"MILOCO_HOME": str(miloco)}, clear=True):
                ui = FakeUI()
                rc = install.run_uninstall(
                    ui, args=__import__("argparse").Namespace(agent_platform="")
                )
            self.assertEqual(rc, 0)
            hermes_section = [r for r in ui.console.lines if "Hermes" in r]
            self.assertTrue(hermes_section)
            self.assertIn("  [dim]– not installed[/dim]", ui.console.lines)


# ---------------------------------------------------------------------------
# 安装期 rc 保护（本提交新增）：stash / restore
# ---------------------------------------------------------------------------


class RcStashRestoreTest(unittest.TestCase):
    def _installer(self, miloco_home: Path):
        installer = object.__new__(install.Installer)
        installer.miloco_home = miloco_home
        installer.ui = FakeUI()
        return installer

    def test_stash_reads_both_rc_files_and_keeps_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            managed = home / "miloco"
            zshrc = home / ".zshrc"
            zshrc.write_text(
                "# user\nexport MILOCO_HOME=/user-one\n"
                f"export MILOCO_HOME={managed}\n",
                encoding="utf-8",
            )
            bashrc = home / ".bashrc"
            bashrc.write_text(
                "export MILOCO_HOME='/user two'\n", encoding="utf-8"
            )
            installer = self._installer(managed)
            saved = installer._hermes_stash_user_rc_exports(home)
            self.assertEqual(
                saved,
                {
                    zshrc: ["export MILOCO_HOME=/user-one\n"],
                    bashrc: ["export MILOCO_HOME='/user two'\n"],
                },
            )
            self.assertEqual(
                zshrc.read_text(encoding="utf-8"),
                "# user\n" + f"export MILOCO_HOME={managed}\n",
            )
            self.assertEqual(bashrc.read_text(encoding="utf-8"), "")

    def test_stash_skips_missing_and_undecodable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            managed = home / "miloco"
            (home / ".zshrc").write_bytes(b"\xff\xfe export MILOCO_HOME=/x\n")
            bashrc = home / ".bashrc"
            bashrc.write_text("export MILOCO_HOME=/user\n", encoding="utf-8")
            saved = self._installer(managed)._hermes_stash_user_rc_exports(home)
            self.assertEqual(saved, {bashrc: ["export MILOCO_HOME=/user\n"]})

    def test_stash_no_user_lines_does_not_touch_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            managed = home / "miloco"
            zshrc = home / ".zshrc"
            zshrc.write_text(
                f"export MILOCO_HOME={managed}\n# keep\n", encoding="utf-8"
            )
            before = zshrc.read_text(encoding="utf-8")
            saved = self._installer(managed)._hermes_stash_user_rc_exports(home)
            self.assertEqual(saved, {})
            self.assertEqual(zshrc.read_text(encoding="utf-8"), before)

    def test_stash_write_failure_does_not_stop_next_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            managed = home / "miloco"
            zshrc = home / ".zshrc"
            zshrc.write_text("export MILOCO_HOME=/user-zsh\n", encoding="utf-8")
            bashrc = home / ".bashrc"
            bashrc.write_text("export MILOCO_HOME=/user-bash\n", encoding="utf-8")
            real = install._atomic_write_text

            def fake(path, text):
                if Path(path) == zshrc:
                    raise OSError("denied")
                return real(path, text)

            with mock.patch.object(install, "_atomic_write_text", fake):
                saved = self._installer(managed)._hermes_stash_user_rc_exports(home)
            self.assertEqual(saved, {bashrc: ["export MILOCO_HOME=/user-bash\n"]})

    def test_stash_value_ending_in_x(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            managed = home / "milocoX"
            zshrc = home / ".zshrc"
            zshrc.write_text(f"export MILOCO_HOME={managed}\n", encoding="utf-8")
            saved = self._installer(managed)._hermes_stash_user_rc_exports(home)
            self.assertEqual(saved, {})
            self.assertEqual(zshrc.read_text(encoding="utf-8"),
                             f"export MILOCO_HOME={managed}\n")

    def test_restore_inserts_before_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            zshrc = home / ".zshrc"
            zshrc.write_text(
                "# miloco runtime path\n"
                "export MILOCO_HOME=/managed\n",
                encoding="utf-8",
            )
            installer = self._installer(home / "miloco")
            installer._hermes_restore_user_rc_exports(
                {zshrc: ["export MILOCO_HOME=/user\n"]}
            )
            self.assertEqual(
                zshrc.read_text(encoding="utf-8"),
                "export MILOCO_HOME=/user\n"
                "# miloco runtime path\n"
                "export MILOCO_HOME=/managed\n",
            )

    def test_restore_appends_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            zshrc = home / ".zshrc"
            zshrc.write_text("no newline", encoding="utf-8")
            installer = self._installer(home / "miloco")
            installer._hermes_restore_user_rc_exports({zshrc: ["export X=1\n"]})
            self.assertEqual(zshrc.read_text(encoding="utf-8"),
                             "no newline\nexport X=1\n")

    def test_restore_appends_to_empty_file_without_extra_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            zshrc = home / ".zshrc"
            zshrc.write_text("", encoding="utf-8")
            installer = self._installer(home / "miloco")
            installer._hermes_restore_user_rc_exports({zshrc: ["A=1\n", "B=2\n"]})
            self.assertEqual(zshrc.read_text(encoding="utf-8"), "A=1\nB=2\n")

    def test_restore_trailing_newline_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            zshrc = home / ".zshrc"
            zshrc.write_text("existing\n", encoding="utf-8")
            installer = self._installer(home / "miloco")
            installer._hermes_restore_user_rc_exports({zshrc: ["A=1\n"]})
            self.assertEqual(zshrc.read_text(encoding="utf-8"),
                             "existing\nA=1\n")

    def test_restore_failure_warns_with_path_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            missing = home / ".zshrc"
            installer = self._installer(home / "miloco")
            installer._hermes_restore_user_rc_exports({missing: ["A=1\n"]})
            self.assertEqual(len(installer.ui.warns), 1)
            warning = installer.ui.warns[0]
            self.assertIn(str(missing), warning)
            self.assertIn("No such file", warning)
            self.assertTrue(warning.startswith("restore user rc export lines failed"))
