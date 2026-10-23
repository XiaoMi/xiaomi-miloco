"""卸载路径的紧凑单元测试（不依赖 hermes/openclaw/uv CLI）。

覆盖归属证明、最小改动、路径安全，以及本次修复涉及的关键回归：
真实 cron 块状输出、plugins.entries、rc 用户 export 保护、DONE detail。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts.tests._module_proxy import MergedModule

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
HERMES_TAG = "[miloco:home-profile]"


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass


def _ensure_optional_deps() -> None:
    """install.py 顶部 import 了安装期依赖；测试环境没装时给最小 stub。"""
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
    _ensure_optional_deps()
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "miloco_install_uninstall_tests", SCRIPTS / "install.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return MergedModule(module, sys.modules["installer_uninstall"])


install = _load_install_module()
I18N = install.I18n("en", SCRIPTS)


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **kwargs) -> None:
        self.lines.append(" ".join(str(a) for a in args if a is not None))


class FakeUI:
    def __init__(self) -> None:
        self.i18n = I18N
        self.console = _Console()
        self.console_err = _Console()
        self.oks: list[str] = []
        self.warns: list[str] = []
        self.infos: list[str] = []

    def ok(self, msg: str) -> None:
        self.oks.append(msg)
        self.console.print(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)
        self.console_err.print(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)
        self.console.print(msg)


class FakeRunner:
    """按顺序返回 CommandResult；同时记录 argv / env / timeout。"""

    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, *, env=None, timeout=None):
        self.calls.append({"argv": list(argv), "env": env, "timeout": timeout})
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _res(stdout: str = "", rc: int = 0, stderr: str = "") -> install.CommandResult:
    return install.CommandResult(("hermes",), rc, stdout, stderr)




# ---------------------------------------------------------------------------
# 文本编辑器
# ---------------------------------------------------------------------------


class EnvFileCleanerTest(unittest.TestCase):
    def test_keeps_user_modified_key_and_other_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n"
                "API_SERVER_KEY=user-changed\n"
                "MILOCO_HOME=/elsewhere\n"
                "USER_TOKEN=keepme\n"
            )
            path.chmod(0o600)
            cleaner = install.EnvFileCleaner(path, i18n=I18N)
            item = cleaner.apply(
                cleaner.plan(
                    managed_bearers={"bearer-abc"},
                    managed_homes={Path(tmp) / "home"},
                )
            )
            self.assertEqual(item.status, install.ItemStatus.KEPT)
            text = path.read_text()
            self.assertIn("API_SERVER_KEY=user-changed", text)
            self.assertIn("MILOCO_HOME=/elsewhere", text)
            self.assertIn("USER_TOKEN=keepme", text)
            self.assertIn("# comment", text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_removes_only_lines_owned_by_miloco(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            path = Path(tmp) / ".env"
            path.write_text(
                f"API_SERVER_KEY=bearer-abc\nMILOCO_HOME={home}\nKEEP=1\n"
            )
            cleaner = install.EnvFileCleaner(path, i18n=I18N)
            item = cleaner.apply(
                cleaner.plan(managed_bearers={"bearer-abc"}, managed_homes={home})
            )
            self.assertEqual(path.read_text(), "KEEP=1\n")
            self.assertEqual(item.status, install.ItemStatus.DONE)

    def test_empty_file_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("")
            cleaner = install.EnvFileCleaner(path, i18n=I18N)
            item = cleaner.apply(
                cleaner.plan(managed_bearers={"x"}, managed_homes={Path(tmp)})
            )
            self.assertEqual(item.status, install.ItemStatus.ABSENT)

    def test_done_detail_lists_removed_and_kept_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            path = Path(tmp) / ".env"
            path.write_text(
                f"API_SERVER_KEY=bearer-abc\nMILOCO_HOME=/user-own\n"
            )
            cleaner = install.EnvFileCleaner(path, i18n=I18N)
            item = cleaner.apply(
                cleaner.plan(managed_bearers={"bearer-abc"}, managed_homes={home})
            )
            self.assertEqual(item.status, install.ItemStatus.DONE)
            self.assertIn("API_SERVER_KEY", item.detail)
            self.assertIn("MILOCO_HOME", item.detail)


class ShellRcCleanerTest(unittest.TestCase):
    def test_removes_marker_but_keeps_foreign_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text(
                "USER_ALIAS=keepme\n"
                "# miloco runtime path\n"
                'export MILOCO_HOME="/other"\n'
            )
            cleaner = install.ShellRcCleaner(path, i18n=I18N)
            item = cleaner.apply(cleaner.plan(managed_homes={Path(tmp) / "home"}))
            text = path.read_text()
            self.assertNotIn("# miloco runtime path", text)
            self.assertIn('export MILOCO_HOME="/other"', text)
            self.assertIn("USER_ALIAS=keepme", text)
            self.assertIn("/other", item.detail)

    def test_markerless_managed_export_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text("USER_ALIAS=keepme\nexport MILOCO_HOME=/managed\n")
            cleaner = install.ShellRcCleaner(path, i18n=I18N)
            item = cleaner.apply(cleaner.plan(managed_homes={Path("/managed")}))
            self.assertEqual(item.status, install.ItemStatus.DONE)
            self.assertEqual(path.read_text(), "USER_ALIAS=keepme\n")

    def test_markerless_user_export_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            original = "export MILOCO_HOME=/user-own\n"
            path.write_text(original)
            cleaner = install.ShellRcCleaner(path, i18n=I18N)
            plan = cleaner.plan(managed_homes={Path("/managed")})
            item = cleaner.apply(plan)
            self.assertEqual(item.status, install.ItemStatus.ABSENT)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(plan.kept_exports[0][1], Path("/user-own"))

    def test_empty_file_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text("")
            cleaner = install.ShellRcCleaner(path, i18n=I18N)
            item = cleaner.apply(cleaner.plan(managed_homes={Path("/managed")}))
            self.assertEqual(item.status, install.ItemStatus.ABSENT)

    def test_marker_and_managed_export_are_removed_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zshrc"
            path.write_text(
                "USER_ALIAS=keepme\n"
                "# miloco runtime path\n"
                "export MILOCO_HOME=/managed\n"
            )
            cleaner = install.ShellRcCleaner(path, i18n=I18N)
            item = cleaner.apply(cleaner.plan(managed_homes={Path("/managed")}))
            self.assertEqual(item.status, install.ItemStatus.DONE)
            self.assertEqual(path.read_text(), "USER_ALIAS=keepme\n")


class ConfigJsonCleanerTest(unittest.TestCase):
    def test_only_clears_agent_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "agent": {"platform": "hermes", "auth_bearer": "x"},
                        "model": {"omni": {"model": "keep"}},
                        "directories": ["a"],
                    },
                    indent=2,
                )
            )
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            cleaner.apply(cleaner.plan(clear_platform=True))
            data = json.loads(path.read_text())
            self.assertEqual(data["agent"]["platform"], "")
            self.assertEqual(data["agent"]["auth_bearer"], "x")
            self.assertEqual(data["model"], {"omni": {"model": "keep"}})
            self.assertEqual(data["directories"], ["a"])

    def test_other_platform_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"agent": {"platform": "openclaw"}}))
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            item = cleaner.apply(cleaner.plan(clear_platform=True))
            self.assertEqual(item.status, install.ItemStatus.KEPT)
            self.assertIn("openclaw", item.detail)

    def test_malformed_json_is_reported_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json")
            cleaner = install.ConfigJsonCleaner(path, i18n=I18N)
            with self.assertRaises(install.MalformedFile):
                cleaner.plan(clear_platform=True)
            self.assertEqual(path.read_text(), "{not json")




# ---------------------------------------------------------------------------
# Hermes cron
# ---------------------------------------------------------------------------


class HermesCronParseTest(unittest.TestCase):
    def test_block_format_uses_id_line_and_name_tag(self):
        text = (
            "  e044a389aede [active]\n"
            f"    Name:      {HERMES_TAG} patrol\n"
            "\n"
            "  f00dcafe1234 [paused]\n"
            f"    Name:      {HERMES_TAG} digest\n"
        )
        jobs = install._parse_hermes_cron_text(text)
        self.assertEqual(
            [(j.id, j.name) for j in jobs],
            [
                ("e044a389aede", f"{HERMES_TAG} patrol"),
                ("f00dcafe1234", f"{HERMES_TAG} digest"),
            ],
        )

    def test_ansi_is_stripped(self):
        text = (
            "  \x1b[1;33mid1\x1b[0m \x1b[1;31m[active]\x1b[0m\n"
            f"    \x1b[1mName:\x1b[0m      {HERMES_TAG} x\n"
        )
        jobs = install._parse_hermes_cron_text(text)
        self.assertEqual([(j.id, j.name) for j in jobs], [("id1", f"{HERMES_TAG} x")])

    def test_name_without_id_line_has_empty_id(self):
        jobs = install._parse_hermes_cron_text(f"    Name: {HERMES_TAG} orphan\n")
        self.assertEqual((jobs[0].id, jobs[0].name), ("", f"{HERMES_TAG} orphan"))

    def test_non_managed_name_is_ignored(self):
        jobs = install._parse_hermes_cron_text(
            "  id1 [active]\n    Name:      user-own-job\n"
        )
        self.assertEqual(jobs, ())

    def test_empty_and_none(self):
        self.assertEqual(install._parse_hermes_cron_text(None), ())
        self.assertEqual(install._parse_hermes_cron_text(""), ())


class HermesCronLiveTest(unittest.TestCase):
    def test_cli_list_uses_all_and_parses_block_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                [
                    _res("usage: remove <id>"),
                    _res(f"  id1 [active]\n    Name: {HERMES_TAG} x\n"),
                ]
            )
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=Path(tmp), env={}, cli="hermes"
            )
        self.assertEqual((backend, error), ("cli", ""))
        self.assertEqual([j.id for j in jobs], ["id1"])
        self.assertEqual(runner.calls[1]["argv"], ["hermes", "cron", "list", "--all"])
        self.assertEqual(runner.calls[1]["timeout"], 60)

    def test_cli_is_preferred_when_both_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
            runner = FakeRunner(
                [
                    _res("remove"),
                    _res(f"  id1 [active]\n    Name: {HERMES_TAG} x\n"),
                ]
            )
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=hermes_home, env={}, cli="hermes"
            )
        self.assertEqual((backend, error), ("cli", ""))
        self.assertEqual([j.id for j in jobs], ["id1"])
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[0]["argv"], ["hermes", "cron", "--help"])

    def test_local_api_used_when_cli_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
            runner = FakeRunner(
                [_res(json.dumps([{"id": "local-1", "name": f"{HERMES_TAG} x"}]))]
            )
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=hermes_home, env={}, cli=None
            )
        self.assertEqual((backend, error), ("local_api", ""))
        self.assertEqual([j.id for j in jobs], ["local-1"])
        self.assertEqual(runner.calls[0]["argv"][1], "-c")

    def test_cli_failure_falls_back_to_local_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            python = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
            runner = FakeRunner(
                [
                    _res(rc=1, stderr="cli boom"),
                    _res(json.dumps([{"id": "local-1", "name": f"{HERMES_TAG} x"}])),
                ]
            )
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=hermes_home, env={}, cli="hermes"
            )
        self.assertEqual((backend, error), ("local_api", ""))
        self.assertEqual([j.id for j in jobs], ["local-1"])

    def test_unparseable_tag_output_yields_empty_id_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([_res("remove"), _res("broken [miloco:home-profile]\n")])
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=Path(tmp), env={}, cli="hermes"
            )
        self.assertEqual((backend, error), ("cli", ""))
        self.assertEqual(
            [(j.id, j.name) for j in jobs],
            [("", "broken [miloco:home-profile]")],
        )

    def test_empty_id_job_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleaner = object.__new__(install.HermesCleaner)
            cleaner.facts = types.SimpleNamespace(venv_python=None, cli="hermes")
            cleaner.inv = types.SimpleNamespace(candidates=())
            cleaner.i18n = I18N
            cleaner.env = {}
            cleaner.hermes_home = Path(tmp) / "hermes"
            cleaner._run = FakeRunner(
                [
                    _res("remove"),
                    _res("broken [miloco:home-profile]\n"),
                    _res("remove"),
                    _res(""),
                ]
            )
            items = cleaner._remove_managed_crons()
        self.assertEqual(items[0].status, install.ItemStatus.BLOCKED)
        self.assertEqual(
            items[0].detail, I18N.t("uninstall.detail.cron_id_unknown")
        )

    def test_no_remove_subcommand_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([_res("usage: list")])
            backend, jobs, error = install._hermes_cron_live(
                run=runner, hermes_home=Path(tmp), env={}, cli="hermes"
            )
        self.assertEqual((backend, jobs), ("cli", ()))
        self.assertIn("no remove subcommand", error)


# ---------------------------------------------------------------------------
# OpenClaw
# ---------------------------------------------------------------------------


class OpenClawHelpersTest(unittest.TestCase):
    def test_plugin_json_finds_nested_id(self):
        self.assertTrue(
            install._openclaw_plugin_in_json(
                json.dumps({"plugins": [{"id": install.OPENCLAW_PLUGIN_ID}]})
            )
        )
        self.assertFalse(install._openclaw_plugin_in_json('{"plugins": [{"id": "x"}]}'))

    def test_cron_jobs_accepts_list_and_wrapped_forms(self):
        self.assertEqual(
            install._openclaw_cron_jobs('[{"id": "a"}]'), [{"id": "a"}]
        )
        self.assertEqual(
            install._openclaw_cron_jobs('{"data": {"jobs": [{"id": "b"}]}}'),
            [{"id": "b"}],
        )
        self.assertIsNone(install._openclaw_cron_jobs("not json"))

    def test_installed_tools_come_from_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            manifest = state / "extensions" / "x" / "openclaw.plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "id": install.OPENCLAW_PLUGIN_ID,
                        "contracts": {"tools": ["tool-a", "tool-b"]},
                    }
                )
            )
            self.assertEqual(
                install._installed_openclaw_tools(state), ("tool-a", "tool-b")
            )

    def test_plugin_record_from_installs_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "plugins").mkdir()
            (state / "plugins" / "installs.json").write_text(install.OPENCLAW_PLUGIN_ID)
            self.assertTrue(
                install._openclaw_plugin_record(
                    state_dir=state, cli=None, run=FakeRunner([])
                )
            )


class OpenClawCleanerTest(unittest.TestCase):
    def _cleaner(self, tool_names=(), cli="openclaw"):
        cleaner = object.__new__(install.OpenClawCleaner)
        cleaner.ui = FakeUI()
        cleaner.i18n = I18N
        cleaner.inv = types.SimpleNamespace(tool_names=tuple(tool_names))
        cleaner.facts = types.SimpleNamespace(cli=cli)
        return cleaner

    def test_tools_allow_removes_only_managed_names(self):
        cleaner = self._cleaner(tool_names=("miloco-tool",))
        runner = FakeRunner([_res('["miloco-tool", "other"]'), _res()])
        cleaner._run = runner
        item = cleaner._clean_tools_allow()
        self.assertEqual(item.status, install.ItemStatus.DONE)
        self.assertEqual(item.detail, "miloco-tool")
        self.assertEqual(
            runner.calls[1]["argv"],
            [
                "openclaw",
                "config",
                "set",
                "tools.alsoAllow",
                '["other"]',
                "--strict-json",
            ],
        )

    def test_tools_allow_absent_when_no_managed_name(self):
        cleaner = self._cleaner(tool_names=("miloco-tool",))
        cleaner._run = FakeRunner([_res('["other"]')])
        item = cleaner._clean_tools_allow()
        self.assertEqual(item.status, install.ItemStatus.ABSENT)

    def test_plugin_entry_not_found_is_absent(self):
        cleaner = self._cleaner()
        cleaner._run = FakeRunner([_res(rc=1, stderr="plugin not found")])
        item = cleaner._clean_plugin_entry()
        self.assertEqual(item.status, install.ItemStatus.ABSENT)


# ---------------------------------------------------------------------------
# 报告与退出码
# ---------------------------------------------------------------------------


class ReportRenderingTest(unittest.TestCase):
    def test_done_detail_is_visible(self):
        ui = FakeUI()
        item = install.CleanupItem(
            "uninstall.hermes.env",
            "hermes",
            "/x/.env",
            install.ItemStatus.DONE,
            "API_SERVER_KEY kept",
        )
        install._print_cleanup_item(ui, item)
        self.assertIn("API_SERVER_KEY kept", ui.console.lines[-1])

    def test_blocked_detail_and_remediation_are_visible(self):
        ui = FakeUI()
        item = install.CleanupItem(
            "uninstall.hermes.plugin_dir",
            "hermes",
            "/x/config.yaml",
            install.ItemStatus.BLOCKED,
            "flow style",
            "edit manually",
        )
        install._print_cleanup_item(ui, item)
        joined = "\n".join(ui.console.lines)
        self.assertIn("flow style", joined)
        self.assertIn("edit manually", joined)

    def test_incomplete_report_returns_false(self):
        ui = FakeUI()
        report = install.PlatformReport("hermes", True, True)
        report.add(
            install.CleanupItem(
                "uninstall.hermes.shell_rc",
                "hermes",
                "~/.zshrc",
                install.ItemStatus.BLOCKED,
                "bad",
                "fix",
            )
        )
        self.assertFalse(install._print_uninstall_report(ui, [report], []))
        self.assertTrue(ui.warns)

    def test_all_done_report_returns_true(self):
        ui = FakeUI()
        report = install.PlatformReport("hermes", True, True)
        report.add(
            install.CleanupItem(
                "uninstall.hermes.shell_rc",
                "hermes",
                "~/.zshrc",
                install.ItemStatus.DONE,
            )
        )
        self.assertTrue(install._print_uninstall_report(ui, [report], []))
        self.assertTrue(ui.oks)


# ---------------------------------------------------------------------------
# 安装期 rc 保护
# ---------------------------------------------------------------------------


class InstallerRcProtectionTest(unittest.TestCase):
    def _installer(self, managed: Path) -> install.Installer:
        inst = object.__new__(install.Installer)
        inst.miloco_home = managed
        inst.ui = FakeUI()
        return inst

    def test_stash_removes_user_export_and_restore_puts_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            managed = Path(tmp) / "managed"
            zshrc = home / ".zshrc"
            zshrc.write_text("USER_ALIAS=keepme\nexport MILOCO_HOME=/opt/user\n")
            inst = self._installer(managed)
            saved = inst._hermes_stash_user_rc_exports(home=home)
            self.assertEqual(zshrc.read_text(), "USER_ALIAS=keepme\n")
            self.assertIn(zshrc, saved)
            zshrc.write_text(
                zshrc.read_text()
                + f"\n# miloco runtime path\n"
                + f'export MILOCO_HOME="{managed}"\n'
            )
            inst._hermes_restore_user_rc_exports(saved)
            text = zshrc.read_text()
            self.assertIn("export MILOCO_HOME=/opt/user", text)
            self.assertLess(text.index("/opt/user"), text.index("# miloco"))

    def test_managed_export_is_not_stashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            managed = Path(tmp) / "managed"
            zshrc = home / ".zshrc"
            original = f'export MILOCO_HOME="{managed}"\n'
            zshrc.write_text(original)
            saved = self._installer(managed)._hermes_stash_user_rc_exports(home=home)
            self.assertEqual(saved, {})
            self.assertEqual(zshrc.read_text(), original)


# ---------------------------------------------------------------------------
# 发现 / 删除 / 小工具
# ---------------------------------------------------------------------------


class DiscoveryTest(unittest.TestCase):
    def test_custom_home_from_hermes_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes_home = home / ".hermes"
            hermes_home.mkdir(parents=True)
            custom = Path(tmp) / "custom-miloco"
            custom.mkdir()
            (hermes_home / ".env").write_text(f"MILOCO_HOME={custom}\n")
            candidates = install.discover_home_candidates(
                env={},
                home=home,
                hermes_home=hermes_home,
                openclaw_state_dir=home / ".openclaw",
                hint="",
            )
            matches = [
                c
                for c in candidates
                if c.real_path == custom.resolve() and "hermes_env" in c.sources
            ]
            self.assertEqual(len(matches), 1)


class DeleteHomesTest(unittest.TestCase):
    def test_delete_kept_symlink_and_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()

            target = root / "home-data"
            (target / "sub").mkdir(parents=True)
            (target / "sub" / "f").write_text("x")
            candidate = install._build_home_candidate(
                target, {"env"}, deliver_configured=False
            )
            items = install.delete_homes(
                [candidate], {candidate.real_path: True}, home=home, env={}
            )
            self.assertFalse(target.exists())
            self.assertEqual(items[0].status, install.ItemStatus.DONE)

            kept = root / "kept"
            kept.mkdir()
            candidate = install._build_home_candidate(
                kept, {"env"}, deliver_configured=False
            )
            items = install.delete_homes(
                [candidate], {candidate.real_path: False}, home=home, env={}
            )
            self.assertTrue(kept.exists())
            self.assertEqual(items[0].status, install.ItemStatus.KEPT)

            link_target = root / "link-target"
            link_target.mkdir()
            link = root / "link"
            link.symlink_to(link_target, target_is_directory=True)
            candidate = install._build_home_candidate(
                link, {"env"}, deliver_configured=False
            )
            install.delete_homes(
                [candidate], {candidate.real_path: True}, home=home, env={}
            )
            self.assertFalse(link.is_symlink())
            self.assertTrue(link_target.exists())

            candidate = install._build_home_candidate(
                home, {"env"}, deliver_configured=False
            )
            items = install.delete_homes(
                [candidate], {candidate.real_path: True}, home=home, env={}
            )
            self.assertTrue(home.exists())
            self.assertEqual(items[0].status, install.ItemStatus.FAILED)


class ManagedHomesTest(unittest.TestCase):
    def test_rc_only_candidate_needs_miloco_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "managed"
            managed.mkdir()
            (managed / "config.json").write_text("{}")
            user = root / "user"
            user.mkdir()
            inv = types.SimpleNamespace(
                candidates=(
                    install._build_home_candidate(
                        user, {"shell_rc"}, deliver_configured=False
                    ),
                    install._build_home_candidate(
                        managed, {"shell_rc"}, deliver_configured=False
                    ),
                )
            )
            cleaner = object.__new__(install.HermesCleaner)
            cleaner.inv = inv
            homes = cleaner._managed_homes()
            self.assertIn(managed.resolve(), homes)
            self.assertNotIn(user.resolve(), homes)


class HelperTest(unittest.TestCase):
    def test_plugin_line_requires_exact_token(self):
        self.assertTrue(install._hermes_plugin_line_present("miloco | other"))
        self.assertTrue(
            install._hermes_plugin_line_present("owner | miloco/miloco-plugin")
        )
        self.assertFalse(install._hermes_plugin_line_present("description says miloco-ish"))

    def test_command_failure_detail_uses_last_stderr_line(self):
        result = install.CommandResult(("hermes", "cron", "list"), 2, "", "line1\nboom\n")
        self.assertEqual(
            install._command_failure_detail(result),
            "hermes cron list (rc=2): boom",
        )

    def test_redact_secrets(self):
        self.assertEqual(
            install._redact_secrets("key=abcdef home=abcdef", {"abcdef"}),
            "key=*** home=***",
        )

    def test_parse_json_output_rejects_noise(self):
        self.assertIsNone(install._parse_json_output("not json"))
        self.assertEqual(install._parse_json_output('{"a": 1}'), {"a": 1})

    def test_unquote_only_strips_matching_pairs(self):
        self.assertEqual(install._unquote('"x"'), "x")
        self.assertEqual(install._unquote("'x'"), "x")
        self.assertEqual(install._unquote('"x\''), '"x\'')

    def test_same_path_compares_resolved_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            self.assertTrue(install._same_path(str(home), {home}))
            self.assertFalse(install._same_path(str(home / "other"), {home}))

    def test_env_and_rc_iterators_strip_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            rc = Path(tmp) / ".zshrc"
            env.write_text('API_SERVER_KEY="abc"\n# ignored\n')
            rc.write_text('export MILOCO_HOME="/managed"\nother=1\n')
            self.assertEqual(
                list(install._iter_env_assignments(env)),
                [(0, "API_SERVER_KEY", "abc")],
            )
            self.assertEqual(list(install._iter_rc_exports(rc)), ["/managed"])


class I18nTest(unittest.TestCase):
    def test_en_zh_key_sets_match(self):
        en = json.loads((SCRIPTS / "i18n" / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((SCRIPTS / "i18n" / "zh.json").read_text(encoding="utf-8"))
        self.assertEqual(set(en), set(zh))


if __name__ == "__main__":
    unittest.main()
