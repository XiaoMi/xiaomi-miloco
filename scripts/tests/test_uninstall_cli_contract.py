"""Uninstall CLI 契约测试：平台 CLI 的契约级测试替身。

此前缺的是**主路径的夹具**：AC-1/2/4/5/11/15/17/18/19 依赖真实的
hermes / openclaw CLI，之前因「本机无 CLI」整条被标 BLOCKED，卸载的正确性
只能靠实现自述。本文件按两个 runtime 的**真实 CLI 契约**写有状态的测试替身
（命令形态来自 ``hermes-agent/hermes_cli/{cron,plugins_cmd}.py`` 与 OpenClaw
的 ``plugins/cron/config`` 子命令），让 ``build_inventory`` → ``HermesCleaner`` /
``OpenClawCleaner`` → 报告 → 退出码 这条主路径真正跑起来，并断言：

* 受管 cron 从 ``hermes cron list --all`` 的**块状输出**按 ``[miloco:*]``
  名称前缀识别并删除（不依赖安装期 id 记录）——这是此前的阻断缺陷；
* 官方 ``plugins remove`` 后，``plugins list`` 不再有 miloco；config.yaml 的
  enabled/entries 残留归 Hermes 管理，卸载器不再直接改写；
* OpenClaw 选择性清理：只删 miloco 工具名 / 插件条目 / 受管 cron；
* 两平台同时装时都被清理，happy-path 退出码 = 0；
* hermes CLI 不可用时明确报出且退出码 ≠ 0（AC-13），OpenClaw 同理（AC-14）；
* 卸载 → 重跑 install 的写入 → 再断言无重复 / 无孤儿（AC-17/18/19 的闭环）。

替身是**有状态**的：`plugins remove` 真的删插件目录、`cron rm` 真的从列表里
去掉、`config set/unset` 真的改内存配置。这样「删完复验」不是自证循环——复验
读的是替身被前一条命令改过的状态。

约束与既有测试一致：按 ``__file__`` 定位仓库、包路径 ``import scripts.install``、
全部进程内执行、不连网、不调用真实 CLI/uv。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

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
    _ensure_optional_deps()
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    existing = sys.modules.get("scripts")
    if existing is not None:
        paths = getattr(existing, "__path__", [])
        if not any(
            str(Path(p).resolve()) == str((REPO_ROOT / "scripts").resolve())
            for p in paths
        ):
            for name in [
                n for n in sys.modules if n == "scripts" or n.startswith("scripts.")
            ]:
                del sys.modules[name]
    import importlib

    return importlib.import_module("scripts.install")


install = _load_install_module()
I18N = install.I18n("en", SCRIPTS)
STATUS = install.ItemStatus
TAG = install.HERMES_MANAGED_TAG
HERMES_PLUGIN_ID = install.HERMES_PLUGIN_ID
OPENCLAW_PLUGIN_ID = install.OPENCLAW_PLUGIN_ID


# ---------------------------------------------------------------------------
# UI / 结果小工具
# ---------------------------------------------------------------------------


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
    def __init__(self, answers=None):
        self.i18n = I18N
        self.console = _Console()
        self.console_err = _Console()
        self.answers = list(answers or [])

    def ok(self, msg):
        self.console.print(msg)

    def warn(self, msg):
        self.console.print(msg)

    def info(self, msg):
        self.console.print(msg)

    def prompt_confirm(self, message, default=False):
        if not self.answers:
            return default
        return self.answers.pop(0)


def res(stdout="", *, rc=0, stderr=""):
    return install.CommandResult((), rc, stdout, stderr)


# ---------------------------------------------------------------------------
# 契约级的有状态 CLI 替身
# ---------------------------------------------------------------------------


def _parse_plugins_block(text):
    """从 config.yaml 文本里读出 plugins.enabled / plugins.disabled 列表。

    与真实 hermes 一致：插件启用态就持久化在 config.yaml 里，`plugins list` /
    `plugins list` 从它读状态。只认块状列表（安装器写入的形态）。
    """
    enabled, disabled = [], []
    section = None  # "enabled" | "disabled" | None
    in_plugins = False
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        if line.rstrip() == "plugins:":
            in_plugins = True
            section = None
            continue
        if in_plugins and line and not line[0].isspace():
            in_plugins = False  # 离开 plugins 块
        if not in_plugins:
            continue
        stripped = line.strip()
        if stripped in ("enabled:", "disabled:"):
            section = stripped[:-1]
            continue
        if stripped.startswith("- ") and section is not None:
            value = stripped[2:].strip().strip('"').strip("'")
            (enabled if section == "enabled" else disabled).append(value)
            continue
        # 任何缩进更浅的非列表行结束当前小节
        if stripped and not stripped.startswith("- "):
            section = None
    return enabled, disabled


class HermesCLI:
    """模拟 `hermes` CLI 的真实命令契约（0.20.5）。

    关键保真点：**插件安装态是目录，config.yaml 只是 allow-list/entries**。
    `plugins remove` 只删目录 + install metadata，不改 config.yaml；`plugins
    list` 只枚举目录。因此卸载后 config 残留、但 list 不再出现——这正是当前
    卸载器的契约。cron 状态不在 config.yaml（真实在 jobs.json / 本地 API），
    用内存维护。

    - `cron --help`：列出含 `rm` 的子命令帮助；
    - `cron list [--all]`：块状输出，id 行在上、`Name:` 行在下；不带 `--all`
      时隐藏 disabled（paused）job —— 复刻真实行为；
    - `cron rm <id>`：从内存删除；
    - `plugins remove <name>`：删插件目录，不改 config.yaml；
    - `plugins list --plain --no-bundled`：只枚举已安装目录。
    """

    def __init__(self, *, config_yaml, crons, colored=False):
        self.config_yaml = Path(config_yaml)
        self.hermes_home = self.config_yaml.parent
        self.plugin_dir = self.hermes_home / "plugins" / "miloco"
        self.installed = {"miloco"} if self.plugin_dir.exists() else set()
        self.crons = [dict(c) for c in crons]
        self.colored = colored
        self.calls: list[list[str]] = []

    @property
    def enabled(self):
        return _parse_plugins_block(self.config_yaml.read_text())[0]

    @property
    def disabled(self):
        return _parse_plugins_block(self.config_yaml.read_text())[1]

    def _c(self, text, code="33"):
        if not self.colored:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def __call__(self, argv, *, env=None, timeout=30.0, stdin=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        rest = argv[1:]  # drop program name
        if rest[:2] == ["cron", "--help"]:
            return res("usage: hermes cron ...\n  list\n  create\n  rm\n  edit\n")
        if rest[:2] == ["cron", "list"]:
            show_all = "--all" in rest
            return res(self._render_cron(show_all))
        if rest[:2] == ["cron", "rm"]:
            job_id = rest[2]
            before = len(self.crons)
            self.crons = [c for c in self.crons if c["id"] != job_id]
            return res("removed\n") if len(self.crons) < before else res(
                "no such job\n", rc=1
            )
        if rest[:2] == ["plugins", "remove"]:
            return self._remove(rest[2])
        if rest[:2] == ["plugins", "list"]:
            return res(self._render_plugins())
        raise AssertionError(f"unexpected hermes argv: {argv}")

    def _remove(self, name):
        if name not in self.installed:
            return res(f"{name} is not installed\n", rc=1)
        self.installed.discard(name)
        if self.plugin_dir.exists():
            shutil.rmtree(self.plugin_dir)
        return res(f"Removed {name}\n")

    def _render_cron(self, show_all):
        if not self.crons:
            return "No scheduled jobs.\n"
        out = ["", "  Scheduled Jobs", ""]
        for c in self.crons:
            if not c.get("enabled", True) and not show_all:
                continue  # hidden without --all (paused jobs)
            status = "[active]" if c.get("enabled", True) else "[paused]"
            out.append(f"  {self._c(c['id'])} {status}")
            out.append(f"    Name:      {c['name']}")
            out.append(f"    Schedule:  every 30m")
        return "\n".join(out) + "\n"

    def _render_plugins(self):
        # `plugins list --plain --no-bundled`: 只枚举已安装目录。
        enabled, disabled = _parse_plugins_block(self.config_yaml.read_text())
        lines = []
        for name in sorted(self.installed):
            if name in enabled:
                status = "enabled"
            elif name in disabled:
                status = "disabled"
            else:
                status = "not enabled"
            lines.append(f"{status:12} {'user':8} {'1.0':8} {name}")
        return "\n".join(lines) + ("\n" if lines else "")


class OpenClawCLI:
    """模拟 `openclaw` CLI 契约：plugins uninstall/list/inspect、config get/set/unset、cron。"""

    def __init__(self, *, plugins, crons, tools_allow, entries, tools_contract):
        self.plugins = list(plugins)  # list of plugin id
        self.crons = [dict(c) for c in crons]  # dict(id, name, description)
        self.tools_allow = list(tools_allow)
        self.entries = dict(entries)  # id -> config
        self.tools_contract = list(tools_contract)
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, env=None, timeout=30.0, stdin=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        rest = argv[1:]
        if rest[:2] == ["plugins", "uninstall"]:
            pid = rest[2]
            if pid in self.plugins:
                self.plugins.remove(pid)
                self.entries.pop(pid, None)
                return res(f"Uninstalled {pid}\n")
            return res(f"{pid} is not installed\n", rc=1)
        if rest[:2] == ["plugins", "list"]:
            payload = [{"id": p, "name": p} for p in self.plugins]
            return res(json.dumps(payload))
        if rest[:2] == ["plugins", "inspect"]:
            pid = rest[2]
            if pid not in self.plugins and pid != OPENCLAW_PLUGIN_ID:
                return res("not found\n", rc=1)
            return res(json.dumps({"contracts": {"tools": self.tools_contract}}))
        if rest[:3] == ["config", "get", "tools.alsoAllow"]:
            return res(json.dumps(self.tools_allow))
        if rest[:3] == ["config", "set", "tools.alsoAllow"]:
            self.tools_allow = json.loads(rest[3])
            return res("ok\n")
        if rest[:2] == ["config", "unset"]:
            path = rest[2]
            key = path.split('"')[1] if '"' in path else path
            if key in self.entries:
                self.entries.pop(key)
                return res("ok\n")
            return res("path not found\n", rc=1)
        if rest[:2] == ["cron", "list"]:
            return res(json.dumps({"jobs": self.crons}))
        if rest[:2] == ["cron", "rm"]:
            job_id = rest[2]
            before = len(self.crons)
            self.crons = [c for c in self.crons if c["id"] != job_id]
            return res("ok\n") if len(self.crons) < before else res("no job\n", rc=1)
        raise AssertionError(f"unexpected openclaw argv: {argv}")


class CompositeRunner:
    """按 argv[0] 把调用分派给对应 CLI 替身；uv 未安装时抛 ABSENT 式结果。"""

    def __init__(self, *, hermes=None, openclaw=None, uv=None):
        self._by_prog = {}
        if hermes is not None:
            self._by_prog["hermes"] = hermes
        if openclaw is not None:
            self._by_prog["openclaw"] = openclaw
        self.uv = uv

    def __call__(self, argv, *, env=None, timeout=30.0, stdin=None):
        argv = [str(a) for a in argv]
        prog = Path(argv[0]).name
        if prog in ("uv",):
            if self.uv is None:
                return res("", rc=0)  # `uv tool list` empty → ABSENT handling
            return self.uv(argv, env=env, timeout=timeout, stdin=stdin)
        target = self._by_prog.get(prog)
        if target is None:
            raise AssertionError(f"no CLI double for {prog!r}: {argv}")
        return target(argv, env=env, timeout=timeout, stdin=stdin)


def _which(mapping):
    def which(name):
        return mapping.get(name)

    return which


# ---------------------------------------------------------------------------
# 安装痕迹构造（文件侧），再叠加 CLI 替身
# ---------------------------------------------------------------------------


def make_hermes_install(home: Path, *, miloco_home=None, bearer="BEARER-1"):
    """铺出一次 Hermes 安装后的文件痕迹。返回 (hermes_home, miloco_home)。"""
    hermes_home = home / ".hermes"
    miloco_home = miloco_home or (home / ".hermes" / "miloco")
    (hermes_home / "plugins" / "miloco" / "miloco-plugin").mkdir(parents=True)
    (hermes_home / "plugins" / "miloco" / "miloco-plugin" / "state.json").write_text(
        json.dumps({"deliver": {"target": "feishu:oc_test"}})
    )
    (hermes_home / "skills" / "miloco-home").mkdir(parents=True)
    (hermes_home / "skills" / "miloco-notify").mkdir()
    (hermes_home / "skills" / "user-skill").mkdir()
    (hermes_home / "skills" / "user-skill" / "SKILL.md").write_text("mine\n")
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - miloco\n    - other-plugin\n"
        "  entries:\n"
        "    miloco/miloco-plugin:\n"
        "      allow_tool_override: false\n"
        "    other-plugin:\n"
        "      keep: true\n"
    )
    (hermes_home / ".env").write_text(
        f"# comment\nAPI_SERVER_KEY={bearer}\nMILOCO_HOME={miloco_home}\nUSER_TOKEN=keepme\n"
    )
    (hermes_home / "memory").mkdir()
    (hermes_home / "memory" / "user-note.md").write_text("note\n")
    (miloco_home / "agent_platform" / "hermes").mkdir(parents=True)
    (miloco_home / "agent_platform" / "hermes" / "adapter.py").write_text("x")
    (miloco_home / "agent_platform" / "custom").mkdir(parents=True)
    (miloco_home / "agent_platform" / "custom" / "adapter.py").write_text("mine")
    (miloco_home / ".install-cache" / "hermes-plugin").mkdir(parents=True)
    (miloco_home / ".install-cache" / "hermes-plugin" / "pkg.whl").write_text("x")
    (miloco_home / "config.json").write_text(
        json.dumps(
            {
                "account": {"id": "acc-1"},
                "model": {"name": "mimo-x"},
                "agent": {"platform": "hermes", "auth_bearer": bearer},
            }
        )
    )
    (miloco_home / "user-sentinel.txt").write_text("keep\n")
    return hermes_home, miloco_home


def make_openclaw_install(home: Path):
    """铺出一次 OpenClaw 安装后的文件痕迹（state dir + config）。"""
    state_dir = home / ".openclaw"
    config_path = state_dir / "openclaw.json"
    (state_dir / "plugins").mkdir(parents=True)
    (state_dir / "plugins" / "installs.json").write_text(
        json.dumps({OPENCLAW_PLUGIN_ID: {"version": "1.0"}})
    )
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "alsoAllow": [
                        "miloco_im_push",
                        "miloco_notify_bind",
                        "miloco_notify_unbind",
                        "other_tool",
                    ]
                },
                "plugins": {"entries": {OPENCLAW_PLUGIN_ID: {"x": 1}}},
            }
        )
    )
    return state_dir, config_path


def default_hermes_crons():
    # 4 个受管 cron，其中一个 paused（模型 key 未配时常见），外加一个用户 job。
    return [
        {"id": "aa01", "name": f"{TAG} miloco-perception-digest", "enabled": True},
        {"id": "bb02", "name": f"{TAG} miloco-home-patrol", "enabled": True},
        {"id": "cc03", "name": f"{TAG} miloco-scene-report", "enabled": True},
        {"id": "dd04", "name": f"{TAG} miloco-health-check", "enabled": False},
        {"id": "ee05", "name": "miloco-user-task", "enabled": True},
        {"id": "ff06", "name": "daily-standup", "enabled": True},
    ]


def run_full_uninstall(home, env, *, runner, which, answers=None):
    """驱动完整卸载主路径，返回 (ok, reports, home_items, ui)。"""
    ui = FakeUI(answers=answers)
    inventory = install.build_inventory(
        env=env, home=home, agent_platform_hint="", which=which, run=runner
    )
    decisions = install.decide_home_deletions(
        inventory.candidates, ui=ui, interactive=bool(answers)
    )
    reports = []
    if inventory.hermes.detected:
        reports.append(
            install.HermesCleaner(
                ui=ui, inv=inventory, decisions=decisions, run=runner, env=env
            ).run()
        )
    else:
        reports.append(
            install.PlatformReport(
                "hermes", detected=False, cli_available=bool(inventory.hermes.cli)
            )
        )
    if inventory.openclaw.detected:
        reports.append(
            install.OpenClawCleaner(ui=ui, inv=inventory, run=runner).run()
        )
    else:
        reports.append(
            install.PlatformReport(
                "openclaw", detected=False, cli_available=bool(inventory.openclaw.cli)
            )
        )
    home_items = install.delete_homes(inventory.candidates, decisions, home=home, env=env)
    ok = install._print_uninstall_report(
        ui, reports, home_items, tool_source=inventory.tool_names_source
    )
    return ok, reports, home_items, ui, inventory


def _flat(reports):
    items = []
    for r in reports:
        items.extend(r.items)
    return items


def _status_by_key(reports):
    out = {}
    for item in _flat(reports):
        out.setdefault(item.key, []).append(item.status)
    return out


# ---------------------------------------------------------------------------
# AC-1 / AC-4 / AC-5：Hermes happy path（CLI 可用），退出码 0
# ---------------------------------------------------------------------------


class HermesHappyPathTest(unittest.TestCase):
    def _run(self, home, *, colored=False, crons=None):
        env = {}
        hermes_home, miloco_home = make_hermes_install(home)
        hermes = HermesCLI(
            config_yaml=hermes_home / "config.yaml",
            crons=crons if crons is not None else default_hermes_crons(),
            colored=colored,
        )
        runner = CompositeRunner(hermes=hermes)
        which = _which({"hermes": str(hermes_home / "bin" / "hermes")})
        ok, reports, home_items, ui, inv = run_full_uninstall(
            home, env, runner=runner, which=which
        )
        return ok, reports, home_items, ui, inv, hermes, miloco_home

    def test_ac1_ac4_ac5_hermes_happy_path_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ok, reports, home_items, ui, inv, hermes, miloco_home = self._run(home)

            # AC-1：退出码 0
            self.assertTrue(ok, ui.console.text)

            # AC-4：所有 4 个受管 cron（含 paused dd04）被删，用户 cron 保留。
            remaining_ids = {c["id"] for c in hermes.crons}
            self.assertEqual(remaining_ids, {"ee05", "ff06"})
            self.assertNotIn("dd04", remaining_ids)  # paused 也被删（靠 --all）

            # AC-5（新契约）：官方 remove 后插件目录消失；config 残留归 Hermes。
            self.assertNotIn("miloco", hermes.installed)
            self.assertIn("miloco", hermes.enabled)
            self.assertIn("other-plugin", hermes.enabled)

            # cron 项是 DONE 不是 ABSENT（此前的阻断缺陷正是这里误报 ABSENT）。
            cron_statuses = [
                i.status for i in _flat(reports) if i.key == "uninstall.hermes.cron"
            ]
            self.assertTrue(all(s is STATUS.DONE for s in cron_statuses), cron_statuses)
            self.assertEqual(len(cron_statuses), 4)

            # 文件侧：插件目录 / miloco skills / adapter 已删，用户 skill 与 config.yaml 的 other 保留。
            self.assertFalse((home / ".hermes" / "plugins" / "miloco").exists())
            self.assertFalse((home / ".hermes" / "skills" / "miloco-home").exists())
            self.assertTrue((home / ".hermes" / "skills" / "user-skill").exists())
            self.assertFalse((miloco_home / "agent_platform" / "hermes").exists())
            self.assertTrue((miloco_home / "agent_platform" / "custom").exists())
            config_text = (home / ".hermes" / "config.yaml").read_text()
            self.assertIn("other-plugin", config_text)
            # Hermes 管理的 enabled/entries 残留允许存在；不再由 miloco 手改。
            self.assertIn("- miloco\n", config_text)
            self.assertIn("miloco/miloco-plugin:", config_text)
            self.assertIn("other-plugin:\n      keep: true", config_text)
            # MILOCO_HOME 保留（回答默认否），用户数据不丢。
            self.assertTrue(miloco_home.exists())
            self.assertTrue((miloco_home / "user-sentinel.txt").exists())
            self.assertTrue((home / ".hermes" / "memory" / "user-note.md").exists())

    def test_ac1_absence_of_managed_cron_is_not_a_failure(self):
        # 没有任何受管 cron 时 happy path 仍应退出 0（ABSENT 不是失败）。
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            user_only = [
                {"id": "ee05", "name": "miloco-user-task", "enabled": True},
            ]
            ok, reports, _hi, ui, _inv, hermes, _mh = self._run(home, crons=user_only)
            self.assertTrue(ok, ui.console.text)
            self.assertEqual({c["id"] for c in hermes.crons}, {"ee05"})
            cron_statuses = [
                i.status for i in _flat(reports) if i.key == "uninstall.hermes.cron"
            ]
            self.assertEqual(cron_statuses, [STATUS.ABSENT])

    def test_ac4_ansi_colored_cron_output_still_parsed(self):
        # should_use_color() 为真时 CLI 输出带 ANSI；受管 cron 仍被识别并删除。
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ok, reports, _hi, ui, _inv, hermes, _mh = self._run(home, colored=True)
            self.assertTrue(ok, ui.console.text)
            self.assertEqual({c["id"] for c in hermes.crons}, {"ee05", "ff06"})

    def test_cron_list_uses_all_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _ok, _r, _hi, _ui, _inv, hermes, _mh = self._run(home)
            list_calls = [c for c in hermes.calls if c[1:3] == ["cron", "list"]]
            self.assertTrue(list_calls)
            for c in list_calls:
                self.assertIn("--all", c)


# ---------------------------------------------------------------------------
# AC-11：明确确认后删除 MILOCO_HOME（CLI 可用 → 无未完成项 → 询问才会出现）
# ---------------------------------------------------------------------------


class HermesHomeDeletionTest(unittest.TestCase):
    def test_ac11_confirm_deletes_home_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_home, miloco_home = make_hermes_install(home)
            hermes = HermesCLI(config_yaml=hermes_home / "config.yaml", crons=default_hermes_crons())
            runner = CompositeRunner(hermes=hermes)
            which = _which({"hermes": str(hermes_home / "bin" / "hermes")})
            # 对每个存在的候选都回答「是」。候选可能不止一个（默认路径），全答是。
            ok, reports, home_items, ui, inv = run_full_uninstall(
                home, {}, runner=runner, which=which, answers=[True] * 6
            )
            self.assertTrue(ok, ui.console.text)
            # miloco_home 被删除。
            self.assertFalse(miloco_home.exists())
            deleted = [i for i in home_items if i.status is STATUS.DONE]
            self.assertTrue(
                any(str(miloco_home) in i.target for i in deleted), home_items
            )
            # 删 home 时，home 内的单项记 SKIPPED（随数据目录删除）。
            adapter = [
                i for i in _flat(reports) if i.key == "uninstall.hermes.adapter"
            ]
            self.assertTrue(any(i.status is STATUS.SKIPPED for i in adapter), adapter)


# ---------------------------------------------------------------------------
# AC-2：OpenClaw + Hermes 同时安装，两者都被清理，退出码 0
# ---------------------------------------------------------------------------


class DualPlatformTest(unittest.TestCase):
    def test_ac2_both_platforms_cleaned_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_home, miloco_home = make_hermes_install(home)
            state_dir, config_path = make_openclaw_install(home)
            hermes = HermesCLI(
                config_yaml=hermes_home / "config.yaml", crons=default_hermes_crons()
            )
            openclaw = OpenClawCLI(
                plugins=[OPENCLAW_PLUGIN_ID, "other-oc-plugin"],
                crons=[
                    {"id": "oc1", "name": "digest", "description": f"{TAG} digest"},
                    {"id": "oc2", "name": "mine", "description": "user cron"},
                ],
                tools_allow=[
                    "miloco_im_push",
                    "miloco_notify_bind",
                    "miloco_notify_unbind",
                    "other_tool",
                ],
                entries={OPENCLAW_PLUGIN_ID: {"x": 1}},
                tools_contract=list(install.FALLBACK_OPENCLAW_TOOLS),
            )
            runner = CompositeRunner(hermes=hermes, openclaw=openclaw)
            which = _which(
                {
                    "hermes": str(hermes_home / "bin" / "hermes"),
                    "openclaw": str(state_dir / "bin" / "openclaw"),
                }
            )
            ok, reports, home_items, ui, inv = run_full_uninstall(
                home, {}, runner=runner, which=which
            )
            self.assertTrue(ok, ui.console.text)
            # 两平台都被探测到并清理。
            self.assertTrue(inv.hermes.detected)
            self.assertTrue(inv.openclaw.detected)
            # Hermes 侧。
            self.assertEqual({c["id"] for c in hermes.crons}, {"ee05", "ff06"})
            self.assertNotIn("miloco", hermes.installed)
            self.assertIn("miloco", hermes.enabled)
            # OpenClaw 侧：插件卸载、工具白名单只剩 other_tool、entry 清除、受管 cron 删除。
            self.assertNotIn(OPENCLAW_PLUGIN_ID, openclaw.plugins)
            self.assertIn("other-oc-plugin", openclaw.plugins)
            self.assertEqual(openclaw.tools_allow, ["other_tool"])
            self.assertNotIn(OPENCLAW_PLUGIN_ID, openclaw.entries)
            self.assertEqual({c["id"] for c in openclaw.crons}, {"oc2"})


# ---------------------------------------------------------------------------
# AC-15：OpenClaw 选择性清理
# ---------------------------------------------------------------------------


class OpenClawSelectiveTest(unittest.TestCase):
    def test_ac15_selective_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state_dir, config_path = make_openclaw_install(home)
            openclaw = OpenClawCLI(
                plugins=[OPENCLAW_PLUGIN_ID, "keep-plugin"],
                crons=[
                    {"id": "m1", "name": "patrol", "description": f"{TAG} patrol"},
                    {"id": "u1", "name": "user", "description": "my own job"},
                ],
                tools_allow=[
                    "other_tool",
                    "miloco_im_push",
                    "miloco_notify_bind",
                    "miloco_notify_unbind",
                ],
                entries={OPENCLAW_PLUGIN_ID: {"x": 1}, "keep-plugin": {"y": 2}},
                tools_contract=list(install.FALLBACK_OPENCLAW_TOOLS),
            )
            runner = CompositeRunner(openclaw=openclaw)
            which = _which({"openclaw": str(state_dir / "bin" / "openclaw")})
            ok, reports, home_items, ui, inv = run_full_uninstall(
                home, {}, runner=runner, which=which
            )
            self.assertTrue(ok, ui.console.text)
            # 插件：miloco 卸载，keep-plugin 保留。
            self.assertEqual(openclaw.plugins, ["keep-plugin"])
            # 工具：只剩 other_tool，顺序保持（other_tool 原本在首位）。
            self.assertEqual(openclaw.tools_allow, ["other_tool"])
            # 插件条目：miloco 删，keep-plugin 留。
            self.assertNotIn(OPENCLAW_PLUGIN_ID, openclaw.entries)
            self.assertIn("keep-plugin", openclaw.entries)
            # 受管 cron 删，用户 cron 留。
            self.assertEqual({c["id"] for c in openclaw.crons}, {"u1"})
            # 验证项 DONE。
            verify = [i for i in _flat(reports) if i.key == "uninstall.openclaw.verify"]
            self.assertTrue(any(i.status is STATUS.DONE for i in verify), verify)


# ---------------------------------------------------------------------------
# AC-13 / AC-14：CLI 不可用必须明确报出且退出码 ≠ 0
# ---------------------------------------------------------------------------


class CliMissingTest(unittest.TestCase):
    def test_ac13_hermes_cli_missing_blocks_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_home, miloco_home = make_hermes_install(home)
            runner = CompositeRunner()  # 任何 CLI 调用都会报错（但 which 返回 None 不会调）
            which = _which({})  # hermes 不在 PATH
            ok, reports, home_items, ui, inv = run_full_uninstall(
                home, {}, runner=runner, which=which
            )
            # 退出码非 0。
            self.assertFalse(ok)
            # 明确报出 CLI 不可用。
            keys = _status_by_key(reports)
            self.assertIn(STATUS.BLOCKED, keys.get("uninstall.hermes.cli_unavailable", []))
            # 不依赖 CLI 的文件清理仍完成。
            self.assertFalse((home / ".hermes" / "plugins" / "miloco").exists())
            self.assertFalse((miloco_home / "agent_platform" / "hermes").exists())
            self.assertNotIn(
                "API_SERVER_KEY", (home / ".hermes" / ".env").read_text()
            )
            # 不输出无条件「卸载完成」。
            self.assertNotIn(
                I18N.t("uninstall.summary.done"), ui.console.text
            )

    def test_ac14_openclaw_cli_missing_blocks_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state_dir, config_path = make_openclaw_install(home)
            runner = CompositeRunner()
            which = _which({})  # openclaw 不在 PATH
            ok, reports, home_items, ui, inv = run_full_uninstall(
                home, {}, runner=runner, which=which
            )
            self.assertFalse(ok)
            self.assertTrue(inv.openclaw.detected)
            items = [i for i in _flat(reports) if i.platform == "openclaw"]
            blocked = [i for i in items if i.status is STATUS.BLOCKED]
            self.assertTrue(blocked, items)
            # 输出含 openclaw 与插件 id，并给出补救命令。
            text = ui.console.text
            self.assertIn("openclaw", text)
            self.assertIn(OPENCLAW_PLUGIN_ID, text)
            self.assertTrue(
                any(OPENCLAW_PLUGIN_ID in (i.remediation or "") for i in blocked)
            )
            self.assertNotIn(I18N.t("uninstall.summary.done"), ui.console.text)


# ---------------------------------------------------------------------------
# AC-20：没有任何痕迹时不误报
# ---------------------------------------------------------------------------


class NoTraceTest(unittest.TestCase):
    def test_ac20_no_trace_no_false_positive_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            runner = CompositeRunner()
            which = _which({})
            ok, reports, home_items, ui, inv = run_full_uninstall(
                home, {}, runner=runner, which=which
            )
            self.assertTrue(ok, ui.console.text)
            self.assertFalse(inv.hermes.detected)
            self.assertFalse(inv.openclaw.detected)
            self.assertNotIn("CLI not available", ui.console.text)


# ---------------------------------------------------------------------------
# AC-17 / AC-18 / AC-19：卸载 → 重跑 install 的闭环收敛
# ---------------------------------------------------------------------------


class _FakeInstaller:
    """用真实的安装期 helper `_hermes_patch_env` 复刻 install 对 .env 的写入。"""

    def __init__(self, miloco_home, ui):
        self.miloco_home = miloco_home
        self.ui = ui


def reinstall_hermes_writes(home, hermes_home, miloco_home, hermes_cli, bearer="BEARER-1"):
    """复刻 `--agent-platform hermes` 重装对可收敛产物的写入（与设计 §5.7 同口径）。

    只覆盖「卸载后须恰好收敛」的那几项：.env、config.yaml 的 enabled、cron reconcile。
    用真实的 `_hermes_patch_env`（有则替换、无则追加）与 reconcile-by-tag 逻辑，
    因此能忠实检验「卸载是否给重装留了会产生重复/孤儿的残留」。
    """
    ui = FakeUI()
    inst = object.__new__(install.Installer)
    inst.miloco_home = miloco_home
    inst.ui = ui
    # .env：安装侧「有则替换、无则追加」
    inst._hermes_patch_env(hermes_home, bearer)
    # 插件目录恢复（官方 remove 已删掉它）。
    plugin_dir = hermes_home / "plugins" / "miloco" / "miloco-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    hermes_cli.installed.add("miloco")
    # config.yaml enable：安装侧只在 enabled 里加一条 miloco（若缺）。
    # 启用态存在 config.yaml 里，HermesCLI.enabled/disabled 都从它读。
    cy = hermes_home / "config.yaml"
    enabled, disabled = _parse_plugins_block(cy.read_text())
    if "miloco" not in enabled:
        text = cy.read_text()
        text = text.replace("  enabled:\n", "  enabled:\n    - miloco\n", 1)
        cy.write_text(text)
    # cron reconcile：按 name.startswith(tag) 识别已存在的受管 job，缺的补齐到 4 个
    wanted = [
        f"{TAG} miloco-perception-digest",
        f"{TAG} miloco-home-patrol",
        f"{TAG} miloco-scene-report",
        f"{TAG} miloco-health-check",
    ]
    existing_names = {c["name"] for c in hermes_cli.crons if c["name"].startswith(TAG)}
    for i, name in enumerate(wanted):
        if name not in existing_names:
            hermes_cli.crons.append({"id": f"re{i}", "name": name, "enabled": True})
    return ui


class ReinstallConvergenceTest(unittest.TestCase):
    def test_ac17_uninstall_then_reinstall_converges(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_home, miloco_home = make_hermes_install(home)
            hermes = HermesCLI(config_yaml=hermes_home / "config.yaml", crons=default_hermes_crons())
            runner = CompositeRunner(hermes=hermes)
            which = _which({"hermes": str(hermes_home / "bin" / "hermes")})

            # 1) 完整卸载（保留 home）。
            ok, reports, _hi, ui, _inv = run_full_uninstall(
                home, {}, runner=runner, which=which
            )
            self.assertTrue(ok, ui.console.text)

            # 卸载后终态：无受管 cron、插件目录消失；config enabled 残留归 Hermes。
            self.assertEqual(
                [c for c in hermes.crons if c["name"].startswith(TAG)], []
            )
            self.assertNotIn("miloco", hermes.installed)
            self.assertIn("miloco", hermes.enabled)
            env_text = (hermes_home / ".env").read_text()
            self.assertNotIn("API_SERVER_KEY", env_text)
            self.assertNotIn("MILOCO_HOME", env_text)

            # 2) 重跑 install 的写入。
            reinstall_hermes_writes(home, hermes_home, miloco_home, hermes)

            # 3) 闭环断言：无重复 / 无孤儿。
            # cron 恰好 4 个受管且名称互不重复。
            managed = [c for c in hermes.crons if c["name"].startswith(TAG)]
            self.assertEqual(len(managed), 4)
            self.assertEqual(len({c["name"] for c in managed}), 4)
            # .env：API_SERVER_KEY 恰好 1 行，MILOCO_HOME 至多 1 行。
            env_text = (hermes_home / ".env").read_text()
            self.assertEqual(env_text.count("API_SERVER_KEY="), 1)
            self.assertLessEqual(env_text.count("MILOCO_HOME="), 1)
            # 插件恰好 1 条 enabled，无同名 disabled 残留。
            self.assertEqual(hermes.enabled.count("miloco"), 1)
            self.assertNotIn("miloco", hermes.disabled)

    def test_ac19_partial_uninstall_then_recover_cli_converges(self):
        # AC-19：先在 CLI 不可用下卸载（部分完成），恢复 CLI 后重装收敛。
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_home, miloco_home = make_hermes_install(home)

            # 第一次：hermes 不在 PATH → cron / 插件注册项 BLOCKED，文件侧仍清理。
            ok1, reports1, _hi, ui1, _inv = run_full_uninstall(
                home, {}, runner=CompositeRunner(), which=_which({})
            )
            self.assertFalse(ok1)  # 必须非 0（有 BLOCKED）

            # 恢复 CLI：cron 仍残留（卸载时 BLOCKED，没能动它们）；
            # config.yaml 的 enabled 仍保留 miloco（Hermes 管理，不再手改）。
            hermes = HermesCLI(
                config_yaml=hermes_home / "config.yaml", crons=default_hermes_crons()
            )

            # 重跑 install：reconcile 按 tag 复用，不应产生重复。
            reinstall_hermes_writes(home, hermes_home, miloco_home, hermes)

            managed = [c for c in hermes.crons if c["name"].startswith(TAG)]
            # 原本 4 个受管（含 paused），reconcile 全部复用，不新增 → 仍是 4。
            self.assertEqual(len(managed), 4)
            self.assertEqual(len({c["name"] for c in managed}), 4)
            self.assertEqual(hermes.enabled.count("miloco"), 1)
            self.assertNotIn("miloco", hermes.disabled)
            env_text = (hermes_home / ".env").read_text()
            self.assertEqual(env_text.count("API_SERVER_KEY="), 1)

    def test_ac18_hermes_uninstall_then_openclaw_install_no_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_home, miloco_home = make_hermes_install(home)
            hermes = HermesCLI(config_yaml=hermes_home / "config.yaml", crons=default_hermes_crons())
            runner = CompositeRunner(hermes=hermes)
            which = _which({"hermes": str(hermes_home / "bin" / "hermes")})
            ok, reports, _hi, ui, _inv = run_full_uninstall(
                home, {}, runner=runner, which=which
            )
            self.assertTrue(ok, ui.console.text)
            # 改装 OpenClaw 的前置条件：无 Hermes 插件目录 / skills / adapter。
            self.assertFalse((hermes_home / "plugins" / "miloco").exists())
            self.assertFalse((hermes_home / "skills" / "miloco-home").exists())
            self.assertFalse((miloco_home / "agent_platform" / "hermes").exists())
            # config.json::agent.platform 不再是 hermes（置空）。
            cfg = json.loads((miloco_home / "config.json").read_text())
            self.assertNotEqual(cfg["agent"]["platform"], "hermes")


if __name__ == "__main__":
    unittest.main()
