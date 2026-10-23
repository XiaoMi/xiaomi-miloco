# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "rich>=13.0",
#   "httpx[socks]>=0.27",
#   "questionary>=2.1",
# ]
# [tool.uv]
# exclude-newer = "2026-04-30"
# ///
"""Miloco 安装器的 Python 核心逻辑。

完整处理以下安装流程：
  阶段 1：环境检查
  阶段 2：安装软件包和平台进程管理器（Linux supervisord / macOS 签名启动器；
          web 静态资源由 miloco wheel 自带）
  阶段 3：初始化服务（引擎预热；仅临时启动，退出时自动停止）
  阶段 4：绑定米家账号
  阶段 5：配置 Omni 模型
  阶段 6：下载感知模型
  阶段 7：安装 Agent 插件（可选）
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import copy
import datetime
import enum
import hashlib
import json
import locale
import os
import platform as _platform
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, NoReturn, Sequence

import httpx
import questionary
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

if TYPE_CHECKING:
    from rich.progress import TaskID

# ---------------------------------------------------------------------------
# Cleanup registry
# ---------------------------------------------------------------------------

_cleanup_paths: list[Path] = []


def _cleanup() -> None:
    for p in _cleanup_paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


atexit.register(_cleanup)


def _signal_handler(_sig: int, _frame: object) -> None:
    _cleanup()
    sys.exit(130)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, lambda s, f: (_cleanup(), sys.exit(143)))

# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------

# linux 标 manylinux_2_28（与 build.sh 一致）：miot native libs 在 manylinux_2_28 容器内编译，glibc≥2.28 可装。
_WHEEL_TAGS: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "macosx_11_0_arm64",
    ("darwin", "x86_64"): "macosx_10_9_x86_64",
    ("linux", "aarch64"): "manylinux_2_28_aarch64",
    ("linux", "x86_64"): "manylinux_2_28_x86_64",
}


def get_system_language():
    lang = _get_os_language()
    if lang and lang.startswith("zh"):
        return "zh"
    return "en"


def _tarfile_extract_safe(tf: tarfile.TarFile, dest: Path) -> None:
    """``extractall(filter="data")`` PEP 706 在 3.12 正式，3.11.4+ / 3.10.12+ 也接受
    filter 参数；但 3.10.0–3.10.11 / 3.11.0–3.11.3 不接受会抛 TypeError。
    install.py ``requires-python = ">=3.10"`` 涵盖这些版本，所以条件传 filter，
    老版本退化到手工 path-traversal 校验（防 zip-slip）+ extractall。
    backend wheel 与 web tarball 是自家 build.sh 打的，源可信；本层加 path-
    traversal 防御深度,跟 SHA-256 校验互补 — 兜未来万一 build 流程引入路径漏洞
    (打包脚本 bug 写出含 ../ 的 member),不是为了防 supply chain (攻击者篡改
    manifest.json 后 SHA 一并改,这层挡不住,SHA 校验本身才是对供应链的兜底)。
    """
    if sys.version_info >= (3, 12):
        tf.extractall(path=dest, filter="data")
        return
    # 3.10/3.11 早期版手工 zip-slip 校验
    for member in tf.getmembers():
        # 校验 name + linkname 两个 path attr——symlink/hardlink target 也得在 dest 内,
        # 否则攻击者可构造 member.name="x" + type=SYMTYPE + linkname="../../etc/passwd"
        # 让 extract 创出指向受保护文件的 symlink。3.12 filter="data" 已挡 link target,
        # 这里手工兜底走防御深度路线。
        for path_attr in (member.name, member.linkname):
            if not path_attr:
                continue
            if path_attr.startswith("/") or ".." in path_attr.split("/"):
                raise ValueError(
                    f"危险 tar member（疑似 zip-slip）: name={member.name!r} linkname={member.linkname!r}"
                )
        # 即便相对路径合法,绝对禁止 sym/hard link——backend wheel + web tarball 应该
        # 都是普通文件,出现 link 就是异常状况,直接拒。
        if member.issym() or member.islnk():
            raise ValueError(
                f"tar 含 link member 拒装（手工兜底路径不允许）: {member.name!r}"
            )
    tf.extractall(path=dest)


def _visible(paths) -> list[Path]:
    """过滤隐藏 / AppleDouble (._*) 文件——macOS tar 可能把它们带进归档，
    若混入 wheel/tgz glob 会被误当成安装包。"""
    return [p for p in paths if not p.name.startswith(".")]


def _get_os_language():
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleLanguages"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                line = line.strip().strip('",')
                if line and not line.startswith("(") and not line.startswith(")"):
                    return line
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes

            windll = ctypes.windll.kernel32
            lid = windll.GetUserDefaultUILanguage()
            return locale.windows_locale.get(lid, "en")
        except Exception:
            pass
    else:
        import os

        return os.environ.get("LANG", "en")
    return "en"


@dataclass(frozen=True)
class Platform:
    os: str
    arch: str
    is_interactive: bool
    lang: str

    @staticmethod
    def detect(*, lang_override: str | None = None) -> Platform:
        raw_system = _platform.system().lower()
        raw_machine = _platform.machine().lower()

        # Windows 原生（含 CYGWIN/MSYS2）暂不支持：miloco-miot 无 Windows wheel。
        # WSL 下 platform.system() 返回 "Linux"，会正常走 linux 分支，不受此拦截。
        if "windows" in raw_system or "cygwin" in raw_system or "msys" in raw_system:
            print(
                "[FAIL] Please install Miloco inside WSL (Windows Subsystem for Linux).",
                file=sys.stderr,
            )
            sys.exit(1)

        os_name = {"darwin": "darwin", "linux": "linux"}.get(raw_system)
        if os_name is None:
            print(f"[FAIL] Unsupported OS: {raw_system}", file=sys.stderr)
            sys.exit(1)

        arch = {
            "x86_64": "x86_64",
            "amd64": "x86_64",
            "arm64": "arm64",
            "aarch64": "aarch64",
        }.get(raw_machine)
        if arch is None:
            print(f"[FAIL] Unsupported architecture: {raw_machine}", file=sys.stderr)
            sys.exit(1)

        is_interactive = sys.stdin.isatty()

        lang = (
            (lang_override or os.environ.get("MILOCO_LANG") or get_system_language())
            .strip()
            .lower()
        )

        return Platform(os=os_name, arch=arch, is_interactive=is_interactive, lang=lang)

    @property
    def wheel_platform_tag(self) -> str:
        tag = _WHEEL_TAGS.get((self.os, self.arch))
        if tag is None:
            raise ValueError(f"No wheel tag for {self.os}/{self.arch}")
        return tag


# ---------------------------------------------------------------------------
# I18n
# ---------------------------------------------------------------------------


class I18n:
    def __init__(self, lang: str, script_dir: Path) -> None:
        self._strings: dict[str, str] = {}
        for try_lang in (lang, "en"):
            path = script_dir / "i18n" / f"{try_lang}.json"
            if path.is_file():
                self._strings = json.loads(path.read_text(encoding="utf-8"))
                break

    def t(self, key: str, *args: str) -> str:
        text = self._strings.get(key, key)
        for i, arg in enumerate(args, 1):
            text = text.replace(f"%{i}", arg)
        return text


# ---------------------------------------------------------------------------
# TTY fallback
# ---------------------------------------------------------------------------


def _force_asyncio_select_selector() -> None:
    """把 asyncio 默认 event loop 从 KqueueSelector 换成 SelectSelector。

    实测（Python 3.14.6 macOS）：即使 fd 0 是 tty character device，
    ``KqueueSelector.register(0, EVENT_READ)`` 也稳定返回 ``[Errno 22]
    Invalid argument``（无论 O_RDONLY / O_RDWR、无论 fd 号）。这是 3.14
    asyncio KqueueSelector 对 tty fd 的回归。SelectSelector 用 ``select(2)``，
    对 tty fd 无差别支持。

    install.py 主流程只跑几个 questionary prompt + 下载器 httpx 调用，换
    selector 对性能无感知影响。

    ``asyncio.DefaultEventLoopPolicy`` / ``set_event_loop_policy`` 在 3.14 被
    标记为 3.16 移除，但 3.14 目前没有稳定的无 warning API 能干预
    ``asyncio.run()`` 内部创建的 loop 类型，暂静音 DeprecationWarning。
    """
    import asyncio
    import selectors
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)

        class _SelectPolicy(asyncio.DefaultEventLoopPolicy):
            def new_event_loop(self):
                return asyncio.SelectorEventLoop(selectors.SelectSelector())

        asyncio.set_event_loop_policy(_SelectPolicy())


def _try_tty_fallback() -> bool:
    """把 fd 0 换成 /dev/tty，让 curl|bash 场景下 questionary/prompt_toolkit 能读交互输入。

    仅换 ``sys.stdin`` 对象（原 upstream 做法）不够 —— prompt_toolkit 从
    ``sys.stdin.fileno()`` 拿 fd 交给 asyncio；macOS 上默认 KqueueSelector
    对 tty fd 直接 EINVAL（见 ``_force_asyncio_select_selector`` docstring）。
    双重修补：
      1. ``os.dup2(tty_fd, 0)`` 把 fd 0 本身换成 tty character device
      2. macOS 场景顺手把 asyncio selector 换成 SelectSelector 绕开 kqueue
    """
    if sys.stdin.isatty():
        return True
    if sys.platform == "win32":
        return False
    tty_path = "/dev/tty"
    if not os.path.exists(tty_path):
        return False
    try:
        tty_fd = os.open(tty_path, os.O_RDONLY)
        try:
            os.dup2(tty_fd, 0)  # 让 fd 0 本身指向 tty，覆盖 pipe stdin
        finally:
            os.close(tty_fd)
        sys.stdin = os.fdopen(0, "r", closefd=False)
        if sys.platform == "darwin":
            _force_asyncio_select_selector()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


class UI:
    def __init__(self, i18n: I18n) -> None:
        self.console = Console(force_terminal=sys.stdout.isatty())
        self.console_err = Console(stderr=True, force_terminal=sys.stderr.isatty())
        self.i18n = i18n
        # 累积失败 step（msg + hint）—— Installer._print_summary 用来在最终
        # ✓ Installation complete 框前列出哪些 step 没成功，避免单步 ✗ 被绿框盖住。
        self.failed_steps: list[tuple[str, str]] = []

    def phase(self, title: str, subtitle: str = "") -> None:
        self.console.print()
        self.console.print(f"[bold cyan]{title}[/bold cyan]")
        if subtitle:
            self.console.print(f"[dim]{subtitle}[/dim]")
        self.console.print()

    def step(self, msg: str) -> None:
        self.console.print(f"[cyan]▸[/cyan] {msg}")

    def step_ok(self, msg: str, detail: str = "") -> None:
        suffix = f"[dim]{detail}[/dim]" if detail else ""
        self.console.print(f"[green]✓[/green] {msg}{suffix}")

    def step_skip(self, msg: str) -> None:
        self.console.print(f"[dim]–[/dim] [dim]{msg}[/dim]")

    def step_fail(self, msg: str, hint: str = "") -> None:
        self.console.print(f"[red]✗[/red] {msg}")
        if hint:
            self.console.print(f"[dim]{hint}[/dim]")
        self.failed_steps.append((msg, hint))

    def info(self, msg: str) -> None:
        self.console.print(f"[blue]ℹ[/blue] {msg}")

    def ok(self, msg: str) -> None:
        self.console.print(f"[green]✓[/green] {msg}")

    def warn(self, msg: str) -> None:
        self.console_err.print(f"[yellow]⚠[/yellow] {msg}")

    def fail(self, msg: str) -> NoReturn:
        self.console_err.print(f"[red]✗[/red] {msg}")
        sys.exit(1)

    def run_with_spinner(
        self,
        cmd: list[str],
        message: str,
        *,
        check: bool = True,
        text: bool = False,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        """Run a subprocess with a spinner animation for visual feedback."""
        if self.console.is_terminal:
            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                console=self.console,
                transient=True,
            ) as progress:
                progress.add_task(message, total=None)
                result = subprocess.run(cmd, capture_output=True, text=text, **kwargs)
        else:
            self.console.print(f"[dim]{message}[/dim]")
            result = subprocess.run(cmd, capture_output=True, text=text, **kwargs)
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        return result

    def progress_context(self) -> Progress:
        return Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=self.console,
        )

    def prompt_select(
        self, message: str, choices: list[str], default: str | None = None
    ) -> str:
        result = questionary.select(message, choices=choices, default=default).ask()
        if result is None:
            self.fail(self.i18n.t("error.no_selection"))
        return result

    def prompt_input(
        self,
        message: str,
        default: str = "",
        password: bool = False,
        validate: Callable[[str], bool | str] | None = None,
    ) -> str:
        kwargs: dict = {"default": default}
        if validate:
            kwargs["validate"] = validate
        if password:
            result = questionary.password(message, **kwargs).ask()
        else:
            result = questionary.text(message, **kwargs).ask()
        if result is None:
            self.fail(self.i18n.t("error.no_input"))
        return result

    def prompt_confirm(self, message: str, default: bool = False) -> bool:
        yes_label = self.i18n.t("common.yes")
        no_label = self.i18n.t("common.no")
        choices = [yes_label, no_label] if default else [no_label, yes_label]
        result = questionary.select(message, choices=choices, default=choices[0]).ask()
        if result is None:
            return default
        return result == yes_label


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------


@dataclass
class DownloadTask:
    name: str
    sha256: str
    size: int
    dest: Path
    urls: list[str]


@dataclass
class DownloadResult:
    name: str
    success: bool
    error: str | None = None
    skipped: bool = False


class Downloader:
    def __init__(
        self,
        *,
        max_concurrent: int = 3,
        max_retries: int = 3,
        chunk_size: int = 256 * 1024,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.chunk_size = chunk_size
        self.timeout = httpx.Timeout(read_timeout, connect=connect_timeout)

    async def download_all(
        self, tasks: list[DownloadTask], progress: Progress
    ) -> list[DownloadResult]:
        sem = asyncio.Semaphore(self.max_concurrent)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            coros = [self._download_one(client, sem, task, progress) for task in tasks]
            return list(await asyncio.gather(*coros))

    async def _download_one(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        task: DownloadTask,
        progress: Progress,
    ) -> DownloadResult:
        if task.dest.is_file() and self._verify_sha256(task.dest, task.sha256):
            return DownloadResult(name=task.name, success=True, skipped=True)

        async with sem:
            task_id = progress.add_task(task.name, total=task.size, completed=0)
            for url in task.urls:
                for attempt in range(1, self.max_retries + 1):
                    try:
                        await self._stream_download(
                            client, url, task, progress, task_id
                        )
                        if self._verify_sha256(task.dest, task.sha256):
                            progress.update(task_id, completed=task.size)
                            return DownloadResult(name=task.name, success=True)
                        task.dest.unlink(missing_ok=True)
                    except (httpx.HTTPError, OSError):
                        if attempt < self.max_retries:
                            await asyncio.sleep(2 ** (attempt - 1))
                        continue

            return DownloadResult(
                name=task.name, success=False, error="all sites and retries exhausted"
            )

    async def _stream_download(
        self,
        client: httpx.AsyncClient,
        url: str,
        task: DownloadTask,
        progress: Progress,
        task_id: TaskID,
    ) -> None:
        existing_size = 0
        tmp_path = task.dest.with_suffix(task.dest.suffix + ".tmp")
        _cleanup_paths.append(tmp_path)

        if tmp_path.is_file():
            existing_size = tmp_path.stat().st_size
            progress.update(task_id, completed=existing_size)

        headers: dict[str, str] = {}
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"

        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416:
                existing_size = 0
                tmp_path.unlink(missing_ok=True)
                progress.update(task_id, completed=0)
                async with client.stream("GET", url) as resp2:
                    resp2.raise_for_status()
                    await self._write_stream(resp2, tmp_path, progress, task_id, 0)
                tmp_path.rename(task.dest)
                return

            if resp.status_code == 200:
                existing_size = 0
                progress.update(task_id, completed=0)

            resp.raise_for_status()
            await self._write_stream(resp, tmp_path, progress, task_id, existing_size)

        tmp_path.rename(task.dest)

    async def _write_stream(
        self,
        resp: httpx.Response,
        path: Path,
        progress: Progress,
        task_id: TaskID,
        initial_offset: int,
    ) -> None:
        mode = "ab" if initial_offset > 0 else "wb"
        written = initial_offset
        with open(path, mode) as f:
            async for chunk in resp.aiter_bytes(self.chunk_size):
                f.write(chunk)
                written += len(chunk)
                progress.update(task_id, completed=written)

    @staticmethod
    def _verify_sha256(path: Path, expected: str) -> bool:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(256 * 1024), b""):
                h.update(chunk)
        return h.hexdigest() == expected


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


class Installer:
    def __init__(
        self,
        plat: Platform,
        ui: UI,
        downloader: Downloader,
        *,
        dev: bool = False,
        omni_api_key: str | None = None,
        account_auth: str | None = None,
        miloco_home: Path,
        skip_openclaw: bool = False,
        agent_platform: str = "",
    ) -> None:
        self.platform = plat
        self.ui = ui
        self.downloader = downloader
        self.dev = dev
        self.omni_api_key = omni_api_key
        self.account_auth = account_auth
        self.miloco_home = miloco_home
        self.skip_openclaw = skip_openclaw
        self.agent_platform = agent_platform
        self.script_dir = Path(__file__).parent
        # dev 安装源：仓库 dist/（build.sh 产物）；release 下为下载归档解压后的缓存目录。
        self.dist_dir = self.script_dir.parent / "dist"
        self._src_dir: Path | None = None
        self._keep_cache = False
        self._service_started = False

    def run(self) -> None:
        self._print_welcome()
        if self.dev:
            self._run_dev_build()
        self._service_started = False
        # download 前置到 install 之后、service 之前：模型解压不依赖 service/account/model
        # 配置，放在 service 之前才能保证服务启动时磁盘上已有模型；否则 service 先起来，
        # 任何交互步骤（account/model）中止都会让模型未解压，落到"服务在跑但缺模型"的中间态。
        self._steps: list[tuple[str, Callable[[], None]]] = [
            ("platform", self._step_choose_platform),
            ("env", self._step_check_deps),
            ("install", self._step_install),
            ("download", self._step_download),
            ("service", self._step_init_service),
            ("account", self._step_account),
            ("model", self._step_configure),
            ("plugin", self._step_plugin),
        ]
        self._total_steps = len(self._steps)
        for i, (_, fn) in enumerate(self._steps, 1):
            self._current_step = i
            fn()
        self._print_summary()

    def _step_choose_platform(self) -> None:
        self._step_header("platform.title", "platform.subtitle")

        if self.agent_platform:
            self.ui.step_skip(self.ui.i18n.t("platform.already_set", self.agent_platform))
            return

        if not self.platform.is_interactive:
            raise RuntimeError(
                self.ui.i18n.t("platform.non_interactive_requires_explicit")
            )

        openclaw_label = self.ui.i18n.t("platform.openclaw_option")
        hermes_label = self.ui.i18n.t("platform.hermes_option")
        choice = self.ui.prompt_select(
            self.ui.i18n.t("platform.ask"),
            choices=[openclaw_label, hermes_label],
            default=openclaw_label,
        )
        self.agent_platform = "hermes" if choice == hermes_label else "openclaw"
        self.ui.step_ok(self.ui.i18n.t("platform.selected", self.agent_platform))

    def _step_header(self, title_key: str, subtitle_key: str) -> None:
        prefix = f"{self._current_step}/{self._total_steps}"
        self.ui.phase(
            f"{prefix} {self.ui.i18n.t(title_key)}",
            self.ui.i18n.t(subtitle_key),
        )

    def _print_welcome(self) -> None:
        manifest_path = self.script_dir / "manifest.json"
        ver = "0.0.0"
        if manifest_path.is_file():
            ver = json.loads(manifest_path.read_text(encoding="utf-8")).get(
                "version", ver
            )
        self.ui.console.print()
        self.ui.console.print(f"[bold]Miloco Installer[/bold]  [dim]v{ver}[/dim]")

    # ── Dev build ──────────────────────────────────────────

    def _run_dev_build(self) -> None:
        """--dev：从源码完整跑一遍 build.sh，确保每次 install 都装最新产物。

        build.sh 已默认 clean，无需传参；输出不捕获（流式打印，build 数分钟，
        spinner 会让住户以为卡死）。失败抛 CalledProcessError 交给 main() 现有
        错误处理（交互打印 retry / agent 输出 error JSON）。
        """
        build_sh = self.script_dir / "build.sh"
        # 打包后的自包含脚本不含 build.sh，--dev 只在仓库内有意义。
        if not build_sh.is_file():
            self.ui.fail(self.ui.i18n.t("error.dev_outside_repo"))
        self.ui.info(self.ui.i18n.t("install.dev_building"))
        subprocess.run(
            ["bash", str(build_sh)],
            cwd=str(self.script_dir.parent),
            check=True,
        )

    # ── Check deps ─────────────────────────────────────────

    def _step_check_deps(self) -> None:
        self._step_header("env.title", "env.subtitle")
        result = subprocess.run(["uv", "--version"], capture_output=True, text=True)
        uv_ver = result.stdout.strip() if result.returncode == 0 else "unknown"
        self.ui.step_ok(self.ui.i18n.t("env.uv_ok", uv_ver))

    # ── Install ───────────────────────────────────────────

    def _step_install(self) -> None:
        self._step_header("install.title", "install.subtitle")
        # dev 装本地 dist/；release 装下载归档解压后的缓存目录（见 _get_src_dir）。
        src_dir = self._get_src_dir()
        self._install_from_dir(src_dir, reinstall=self.dev)
        if sys.platform == "darwin":
            # macOS 不用 supervisord：装 vendored 签名启动器 miloco.app，backend 由
            # launchd → 启动器 → python 拉起以绕过 Local Network Privacy
            # （见 cli service.py 的 launchd 分支）。
            self._install_launcher(src_dir)
        else:
            self._install_supervisor()
        self._configure_python_bin()

    def _install_launcher(self, src_dir: Path) -> None:
        tgz = _visible(src_dir.glob("miloco-launcher-darwin*.tar.gz"))
        if not tgz:
            # macOS 上后端**必须**由签名启动器拉起,否则 launchd 起不来(service start
            # 侧是硬失败)。安装侧原先只 warn 然后照样报"安装成功",两侧严重度不一致:
            # 用户拿到一个装完就起不来的部署,还得自己翻日志才知道是漏了 launcher。
            # release 路径缺包属于发布包不完整 → 硬失败;dev 路径本地 dist 可能就没
            # 打这个包(开发者另走 sync-to-remote.sh),保持 warn 不打断。
            if self.dev:
                self.ui.warn(self.ui.i18n.t("install.launcher_missing"))
                return
            self.ui.fail(self.ui.i18n.t("error.launcher_missing"))
        app_dir = self.miloco_home / "miloco.app"
        shutil.rmtree(app_dir, ignore_errors=True)
        self.miloco_home.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tgz[0]) as tf:
            _tarfile_extract_safe(tf, self.miloco_home)  # 保留签名字节
        exe = app_dir / "Contents" / "MacOS" / "miloco"
        try:
            exe.chmod(0o755)
        except OSError:
            pass
        self._verify_launcher_signature(app_dir)
        self.ui.step_ok(self.ui.i18n.t("install.launcher_ok", str(app_dir)))
        # macOS 用 launchd 启动器绕过 LNP:首次启动后在系统设置里打开本地网络开关。
        self.ui.warn(self.ui.i18n.t("install.lnp_hint"))

    def _verify_launcher_signature(self, app_dir: Path) -> None:
        """校验启动器签名完整性——这是整条 LNP 授权链上唯一的护栏。

        启动器的价值完全建立在"字节不变 → 签名摘要不变 → LNP 授权持续有效"上。
        归档在传输 / 存储 / 某次误提交里被动过一个字节时,签名失效,用户表现为
        "中枢一直连不上、日志里只有 Errno 65",而系统设置里 miloco 的开关看起来
        明明是开着的(旧摘要的授权对不上新二进制)——这是整个方案里最难自查的失败
        模式。dev 部署脚本(sync-to-remote.sh)一直有这道校验,生产安装路径反而没有,
        所以补上。

        codesign 本身取不到时只 warn 不 fail:那是环境缺陷而非产物损坏,不该把
        安装拦死;签名明确校验不过才是硬失败。
        """
        try:
            verify = subprocess.run(
                ["codesign", "-v", str(app_dir)],
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, OSError):
            self.ui.warn(self.ui.i18n.t("install.launcher_verify_skipped"))
            return
        if verify.returncode != 0:
            detail = (verify.stderr or verify.stdout or "").strip()
            self.ui.fail(
                self.ui.i18n.t("error.launcher_signature", str(app_dir), detail)
            )

    def _install_from_dir(self, src_dir: Path, *, reinstall: bool) -> None:
        if not src_dir.is_dir():
            self.ui.fail(self.ui.i18n.t("error.dist_not_found", str(src_dir)))

        tag = self.platform.wheel_platform_tag
        miot_wheels = _visible(src_dir.glob(f"miloco_miot-*{tag}*.whl"))
        if not miot_wheels:
            self.ui.fail(self.ui.i18n.t("error.no_miot_wheel", tag))
        miot_wheel = miot_wheels[0]

        miloco_wheels = [
            w
            for w in _visible(src_dir.glob("miloco-*.whl"))
            if "miloco_miot" not in w.name and "miloco_cli" not in w.name
        ]
        if not miloco_wheels:
            self.ui.fail(f"No miloco wheel found in {src_dir}")
        miloco_wheel = miloco_wheels[0]

        cli_wheels = _visible(src_dir.glob("miloco_cli-*.whl"))
        if not cli_wheels:
            self.ui.fail(f"No miloco-cli wheel found in {src_dir}")
        cli_wheel = cli_wheels[0]

        # release 版本随发版递增，--force 即可；dev 本地构建版本号常不变，需 --reinstall 覆盖。
        miloco_cmd = [
            "uv", "tool", "install",
            str(miloco_wheel), "--with", str(miot_wheel), "--force",
        ]
        cli_cmd = ["uv", "tool", "install", str(cli_wheel), "--force"]
        if reinstall:
            miloco_cmd.append("--reinstall")
            cli_cmd.append("--reinstall")

        self.ui.run_with_spinner(miloco_cmd, self.ui.i18n.t("install.install_miloco"))
        self.ui.step_ok(self.ui.i18n.t("install.miloco_ok"))

        self.ui.run_with_spinner(cli_cmd, self.ui.i18n.t("install.install_cli"))
        self.ui.step_ok(self.ui.i18n.t("install.cli_ok"))

    def _get_src_dir(self) -> Path:
        """安装产物目录（进程内 memoize）：dev=仓库 dist/；release=下载归档解压后的缓存目录。"""
        if self._src_dir is None:
            self._src_dir = self.dist_dir if self.dev else self._fetch_release_bundle()
        return self._src_dir

    def _fetch_release_bundle(self) -> Path:
        """release 安装：按平台从 GitHub Release 下载对应归档（整包 SHA 校验）并解压到
        版本化缓存目录，返回该目录。缓存供同进程后续步骤、以及 agent step1→step3 跨进程
        复用，免重复下载整包。
        """
        manifest_path = self.script_dir / "manifest.json"
        if not manifest_path.is_file():
            self.ui.fail(self.ui.i18n.t("download.manifest_missing"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        ver = manifest.get("version", "0.0.0")
        base = self.miloco_home / ".install-cache"
        cache = base / ver
        # 命中缓存（同进程后续步骤 / agent step1→step3）：要求 wheel + 模型 tarball + 插件
        # tgz 三类齐全，避免半成品缓存导致缺模型/缺插件的静默安装。
        if (
            any(cache.glob("miloco-*.whl"))
            and any(cache.glob("miloco-models-*.tar.gz"))
            and any(cache.glob("*.tgz"))
        ):
            return cache

        dl = manifest.get("download", {})
        sites = [s.rstrip("/") for s in dl.get("sites", [])]
        if env_url := os.environ.get("MILOCO_DOWNLOAD_URL"):
            sites = [env_url.rstrip("/")] + sites
        if not sites:
            self.ui.fail(self.ui.i18n.t("download.no_sites"))
        tag = dl.get("tag", "")

        key = f"{self.platform.os}-{self.platform.arch}"
        bundle = manifest.get("bundles", {}).get(key)
        if not bundle:
            self.ui.fail(self.ui.i18n.t("error.no_bundle", key))

        shutil.rmtree(base, ignore_errors=True)  # 清旧版本/残留缓存
        cache.mkdir(parents=True, exist_ok=True)
        archive = cache / bundle["name"]
        task = DownloadTask(
            name=bundle["name"],
            sha256=bundle["sha256"],
            size=bundle["size"],
            dest=archive,
            urls=[f"{s}/{tag}/{bundle['name']}" for s in sites],
        )

        self.ui.step(self.ui.i18n.t("install.downloading_bundle"))
        with self.ui.progress_context() as progress:
            results = asyncio.run(self.downloader.download_all([task], progress))
        if not results[0].success:
            self.ui.fail(self.ui.i18n.t("install.bundle_failed", results[0].error or ""))
        self.ui.step_ok(self.ui.i18n.t("install.bundle_ok"))

        # 归档已整体 SHA 校验通过 → 内含 wheel/tgz/模型完整性随之保证，解压后不再逐文件校验。
        with tarfile.open(archive) as tf:
            _tarfile_extract_safe(tf, cache)
        archive.unlink(missing_ok=True)  # 解压后删归档，缓存只留可装产物
        return cache

    def _cleanup_install_cache(self) -> None:
        # dev 无缓存；agent step1 须保留缓存供 step3 复用 → 跳过清理。
        if self.dev or self._keep_cache:
            return
        shutil.rmtree(self.miloco_home / ".install-cache", ignore_errors=True)

    def _install_supervisor(self) -> None:
        self.ui.run_with_spinner(
            ["uv", "tool", "install", "supervisor", "--force"],
            self.ui.i18n.t("install.install_supervisor"),
        )
        self.ui.step_ok(self.ui.i18n.t("install.supervisor_ok"))

    def _configure_python_bin(self) -> None:
        self.ui.step(self.ui.i18n.t("install.configure_python"))

        result = subprocess.run(
            [
                "uv",
                "tool",
                "run",
                "--from",
                "miloco",
                "python",
                "-c",
                "import sys; print(sys.executable)",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.ui.warn("Could not detect Python path from miloco virtualenv")
            return

        py_path = result.stdout.strip()

        result = subprocess.run(
            [
                py_path,
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            capture_output=True,
            text=True,
        )
        py_ver = result.stdout.strip() if result.returncode == 0 else "?"

        try:
            subprocess.run(
                [
                    "miloco-cli",
                    "config",
                    "set",
                    "server.python_bin",
                    py_path,
                    "--no-restart",
                ],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            self._fallback_write_config(py_path)

        self.ui.step_ok(self.ui.i18n.t("install.python_ok", py_ver, py_path))

    # ── Init service ────────────────────────────────────────

    def _step_init_service(self) -> None:
        self._step_header("service.title", "service.subtitle")

        result = subprocess.run(
            [
                "uv",
                "tool",
                "run",
                "--from",
                "miloco",
                "python",
                "-c",
                "import sys; print(sys.executable)",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            py_path = result.stdout.strip()
            try:
                self.ui.run_with_spinner(
                    [py_path, "-c", "import miloco.main"],
                    self.ui.i18n.t("service.warmup"),
                )
                self.ui.step_ok(self.ui.i18n.t("service.warmup_ok"))
            except subprocess.CalledProcessError:
                self.ui.step_skip(self.ui.i18n.t("service.warmup_skip"))

        try:
            self.ui.run_with_spinner(
                ["miloco-cli", "service", "restart"],
                self.ui.i18n.t("service.starting"),
            )
            self._service_started = True
            self.ui.step_ok(self.ui.i18n.t("service.started"))
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            self.ui.step_fail(self.ui.i18n.t("service.start_failed"))
            self.ui.warn(str(e))

    def _stop_service(self) -> None:
        if not self._service_started:
            return
        try:
            subprocess.run(
                ["miloco-cli", "service", "stop"],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        self._service_started = False

    def _fallback_write_config(self, py_path: str) -> None:
        config_path = self.miloco_home / "config.json"
        config: dict = {}
        if config_path.is_file():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        server = config.setdefault("server", {})
        server["python_bin"] = py_path
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.rename(config_path)

    def _extract_models(self, src_dir: Path, models_dest: Path) -> int:
        """从 src_dir 内的 miloco-models-*.tar.gz 解压模型到 models_dest，返回**本次解压**的模型数。
        归档下载时已整体 SHA 校验，模型完整性随之保证，此处不再逐文件校验。

        计数只统计本归档内的文件成员，而非 models_dest 的目录全量：升级重装时 models_dest 可能
        残留上一版本的模型（解压只覆盖同名、不删多余），按目录计数会把陈旧残留也算进去 → 虚高。
        """
        tarballs = list(src_dir.glob("miloco-models-*.tar.gz"))
        if not tarballs:
            return 0
        models_dest.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tarballs[0]) as tf:
                members = [
                    m
                    for m in tf.getmembers()
                    if m.isfile() and not Path(m.name).name.startswith(".")
                ]
                _tarfile_extract_safe(tf, models_dest)
        except (tarfile.TarError, OSError) as exc:
            # tarball 损坏必须硬失败，warn+return 0 会被 _step_download 当作"没模型"
            # 走 fail 路径，但 UI 上只显示"缺失模型文件"看不出是解压错，排查会走弯路。
            self.ui.fail(
                self.ui.i18n.t("download.extract_failed", tarballs[0].name, str(exc))
            )
        return len(members)  # 仅本次归档内的模型成员数

    # ── Account ───────────────────────────────────────────

    def _step_account(self) -> None:
        self._step_header("account.title", "account.subtitle")

        if not self.platform.is_interactive and not self.account_auth:
            self.ui.step_skip(self.ui.i18n.t("account.skip_non_interactive"))
            return

        if not self._service_started:
            self.ui.step_skip(self.ui.i18n.t("account.service_start_failed"))
            return

        if self.account_auth:
            self._authorize_with_payload()
            return

        already_bound = False
        try:
            result = subprocess.run(
                ["miloco-cli", "account", "status"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                status_data = json.loads(result.stdout)
                already_bound = (
                    status_data.get("code") == 0
                    and status_data.get("data", {}).get("is_bound") is True
                )
        except Exception:
            pass

        if already_bound:
            self.ui.step_ok(self.ui.i18n.t("account.already_logged_in"))
            keep_label = self.ui.i18n.t("account.keep_current_option")
            rebind_label = self.ui.i18n.t("account.rebind_option")
            choice = self.ui.prompt_select(
                self.ui.i18n.t("account.rebind_ask"),
                choices=[keep_label, rebind_label],
            )
            if choice == keep_label:
                self.ui.ok(self.ui.i18n.t("account.keep_current"))
                return
        else:
            # account bind 一旦发起就无法中途取消，先让用户选择是否绑定（默认绑定）。
            bind_label = self.ui.i18n.t("account.bind_option")
            skip_label = self.ui.i18n.t("account.skip_option")
            choice = self.ui.prompt_select(
                self.ui.i18n.t("account.bind_ask"),
                choices=[bind_label, skip_label],
            )
            if choice == skip_label:
                self.ui.step_skip(self.ui.i18n.t("account.skipped_hint"))
                return

        self.ui.step(self.ui.i18n.t("account.binding"))
        try:
            subprocess.run(
                ["miloco-cli", "account", "bind"],
                check=True,
            )
            self.ui.step_ok(self.ui.i18n.t("account.bind_ok"))
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.ui.step_fail(self.ui.i18n.t("account.bind_failed"))

    def _authorize_with_payload(self) -> None:
        assert self.account_auth is not None
        self.ui.step(self.ui.i18n.t("account.authorizing"))
        try:
            subprocess.run(
                ["miloco-cli", "account", "authorize", self.account_auth],
                check=True,
                capture_output=True,
            )
            self.ui.step_ok(self.ui.i18n.t("account.bind_ok"))
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.ui.step_fail(self.ui.i18n.t("account.bind_failed"))

    # ── Model config ──────────────────────────────────────

    def _step_configure(self) -> None:
        self._step_header("model.title", "model.subtitle")

        if self.omni_api_key:
            self._quick_configure_mimo()
            return

        # 非交互模式直接跳过
        if not self.platform.is_interactive:
            self.ui.step_skip(self.ui.i18n.t("model.skip_non_interactive"))
            return

        cur_model, cur_base_url, cur_api_key = self._get_current_config()

        if cur_model and cur_base_url and cur_api_key:
            self.ui.info(self.ui.i18n.t("model.current_config"))
            self.ui.info(f"  model:    {cur_model}")
            self.ui.info(f"  base_url: {cur_base_url}")
            self.ui.info(f"  api_key:  {self._mask_key(cur_api_key)}")

            keep_label = self.ui.i18n.t("model.use_current_option")
            reconfigure_label = self.ui.i18n.t("model.reconfigure_option")
            choice = self.ui.prompt_select(
                self.ui.i18n.t("model.modify_ask"),
                choices=[keep_label, reconfigure_label],
            )
            if choice == keep_label:
                self.ui.ok(self.ui.i18n.t("model.keep_current"))
                return
        else:
            # 无配置：提供跳过选项
            skip_label = self.ui.i18n.t("model.skip_option")
            configure_label = self.ui.i18n.t("model.configure_option")
            choice = self.ui.prompt_select(
                self.ui.i18n.t("model.configure_ask"),
                choices=[configure_label, skip_label],
            )

            if choice == skip_label:
                self.ui.step_skip(self.ui.i18n.t("model.skipped_hint"))
                return

        self.ui.info(self.ui.i18n.t("model.get_key"))

        provider = self.ui.prompt_select(
            self.ui.i18n.t("model.select_provider"),
            choices=[
                self.ui.i18n.t("model.provider_mimo"),
                self.ui.i18n.t("model.provider_custom"),
            ],
        )

        if provider == self.ui.i18n.t("model.provider_mimo"):
            model_name = "mimo-v2.6-flash"
            base_url = "https://api.xiaomimimo.com/v1"
        else:
            model_name = self.ui.prompt_input(
                self.ui.i18n.t("model.enter_model"),
                default=cur_model,
                validate=lambda v: (
                    True if v.strip() else self.ui.i18n.t("model.model_required")
                ),
            )
            base_url = self.ui.prompt_input(
                self.ui.i18n.t("model.enter_base_url"),
                default=cur_base_url,
                validate=lambda v: (
                    True if v.strip() else self.ui.i18n.t("model.url_required")
                ),
            )

        api_key = self.ui.prompt_input(
            self.ui.i18n.t("model.enter_key"),
            default="",
            password=True,
            validate=lambda v: (
                True if v.strip() else self.ui.i18n.t("model.key_required")
            ),
        )

        self._write_model_config(model_name, base_url, api_key)
        self.ui.step_ok(self.ui.i18n.t("model.config_saved"))

    def _quick_configure_mimo(self) -> None:
        self.ui.step(self.ui.i18n.t("model.mimo_quick"))
        self._write_model_config(
            "mimo-v2.6-flash",
            "https://api.xiaomimimo.com/v1",
            self.omni_api_key or "",
        )
        self.ui.step_ok(self.ui.i18n.t("model.config_saved"))

    def _write_model_config(self, model: str, base_url: str, api_key: str) -> None:
        for field_name, value in [
            ("model", model),
            ("base_url", base_url),
            ("api_key", api_key),
        ]:
            if not value.strip():
                self.ui.fail(self.ui.i18n.t("model.config_field_empty", field_name))
        try:
            subprocess.run(
                [
                    "miloco-cli",
                    "config",
                    "set",
                    "model.omni.model",
                    model,
                    "model.omni.base_url",
                    base_url,
                    "model.omni.api_key",
                    api_key,
                    "--no-restart",
                ],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            config_path = self.miloco_home / "config.json"
            config: dict = {}
            if config_path.is_file():
                try:
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            omni = config.setdefault("model", {}).setdefault("omni", {})
            omni["model"] = model
            omni["base_url"] = base_url
            omni["api_key"] = api_key
            tmp = config_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.rename(config_path)

    def _get_current_config(self) -> tuple[str, str, str]:
        values: list[str] = []
        for key in ("model.omni.model", "model.omni.base_url", "model.omni.api_key"):
            try:
                result = subprocess.run(
                    ["miloco-cli", "config", "get", key, "--value-only"],
                    capture_output=True,
                    text=True,
                )
                values.append(result.stdout.strip() if result.returncode == 0 else "")
            except FileNotFoundError:
                values.append("")
        return values[0], values[1], values[2]

    def _mask_key(self, key: str) -> str:
        if len(key) <= 8:
            return key[:4] + "****"
        return key[:4] + "****" + key[-4:]

    # ── Download ──────────────────────────────────────────

    def _step_download(self) -> None:
        self._step_header("download.title", "download.subtitle")
        models_dest = self.miloco_home / "models"
        self.ui.step(self.ui.i18n.t("download.extracting_models"))
        # 缺模型是硬失败：dev 通道源码目录若无 .onnx、release 通道若打包漏 tarball，
        # 都必须在此暴露而非静默 skip——服务后续依赖这些文件，跳过等于装了个空壳。
        count = self._extract_models(self._get_src_dir(), models_dest)
        if not count:
            self.ui.fail(self.ui.i18n.t("download.models_missing"))
        self.ui.step_ok(self.ui.i18n.t("download.models_verified", str(count)))

    # ── Plugin ────────────────────────────────────────────

    def _ensure_openclaw(self) -> None:
        """Ensure openclaw CLI exists and version >= 2026.5.2."""
        if not shutil.which("openclaw"):
            self.ui.fail(self.ui.i18n.t("plugin.openclaw_not_found"))

        min_version = (2026, 5, 2)
        try:
            result = subprocess.run(
                ["openclaw", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            import re

            # Output format: "OpenClaw 2026.5.17 (7754722)"
            m = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
            if not m:
                raise ValueError("no version found")
            parts = tuple(int(x) for x in m.group(1).split("."))
            if parts < min_version:
                self.ui.fail(
                    self.ui.i18n.t(
                        "plugin.version_too_old",
                        m.group(1),
                        ".".join(map(str, min_version)),
                    )
                )
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            self.ui.warn(self.ui.i18n.t("plugin.version_check_failed"))

    def _step_plugin(self) -> None:
        self._step_header("plugin.title", "plugin.subtitle")

        if self.agent_platform == "hermes":
            self._step_plugin_hermes()
            return

        if self.skip_openclaw:
            self.ui.step_skip(self.ui.i18n.t("plugin.skipped"))
            return

        self._step_plugin_openclaw()

    def _step_plugin_openclaw(self) -> None:
        self._ensure_openclaw()

        # dev 与 release 都从本地 .tgz 装（release 的来自下载归档解压后的缓存目录）。
        tgz_files = _visible(self._get_src_dir().glob("*.tgz"))
        if not tgz_files:
            self.ui.step_fail(self.ui.i18n.t("plugin.no_tgz"))
            raise subprocess.CalledProcessError(
                1, "openclaw plugins install", stderr="no .tgz found"
            )
        pkg = str(tgz_files[0])

        # openclaw >= 2026.8 引入 capability consent：带 capabilities 的插件安装需
        # --accept-capabilities；老版本无此旗标，探测 install --help 决定是否追加。
        # 判定只看输出文本（与 sync-to-remote.sh 同口径），退出码/超时不参与匹配；
        # 超时时已捕获的部分输出也参与匹配——help 文本打印出该旗标即代表支持。
        accept = []
        timed_out = False
        try:
            probe = subprocess.run(
                ["openclaw", "plugins", "install", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            probe_out = f"{probe.stdout}\n{probe.stderr}"
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            probe_out = f"{out}\n{err}"
        if "--accept-capabilities" in probe_out:
            accept = ["--accept-capabilities"]
        elif timed_out:
            # 超时且没匹配到旗标：以空 accept 继续安装，8.x 上会被 consent 门拒——
            # 提示实际原因免得误判成版本号问题。
            self.ui.warn(self.ui.i18n.t("plugin.capability_probe_timeout"))

        self.ui.run_with_spinner(
            ["openclaw", "plugins", "install", "--force", *accept, pkg],
            self.ui.i18n.t("plugin.installing"),
            text=True,
        )
        self.ui.step_ok(self.ui.i18n.t("plugin.plugin_ok"))

        self._register_plugin_tools()
        self._enable_conversation_access()

    def _step_plugin_hermes(self) -> None:
        hermes_home = Path(
            os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
        ).expanduser()
        if not shutil.which("hermes"):
            self.ui.step_fail(self.ui.i18n.t("plugin.hermes_not_found"))
            return

        tarballs = _visible(self._get_src_dir().glob("miloco-hermes-plugin-*.tar.gz"))
        if not tarballs:
            self.ui.step_fail(self.ui.i18n.t("plugin.no_hermes_tarball"))
            raise subprocess.CalledProcessError(
                1, "install hermes plugin", stderr="no hermes plugin tarball found"
            )

        extract_dir = self.miloco_home / ".install-cache" / "hermes-plugin"
        shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarballs[0]) as tf:
            _tarfile_extract_safe(tf, extract_dir)

        self._hermes_deploy_plugin(hermes_home, extract_dir)
        self._hermes_deploy_adapter(extract_dir)

        bearer = self._hermes_get_or_create_bearer(hermes_home)
        webhook_url = f"http://127.0.0.1:{os.environ.get('ADAPTER_PORT', '18789')}/miloco/webhook"
        try:
            subprocess.run(
                ["miloco-cli", "config", "set",
                 "agent.webhook_url", webhook_url,
                 "agent.auth_bearer", bearer,
                 "agent.platform", "hermes"],
                check=True, capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            self.ui.step_fail(self.ui.i18n.t("plugin.hermes_config_failed"))
            return
        self._hermes_patch_env(hermes_home, bearer)
        try:
            subprocess.run(
                ["hermes", "plugins", "enable", "miloco"],
                capture_output=True, text=True, check=True,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            self.ui.warn(self.ui.i18n.t("plugin.hermes_enable_failed"))
        self._hermes_run_post_install(hermes_home, extract_dir)
        self.ui.step_ok(self.ui.i18n.t("plugin.hermes_ok"))

    def _hermes_run_post_install(self, hermes_home: Path, extract_dir: Path) -> None:
        """调 install-hermes.sh --post-install 补齐 env 持久化 / cron / 收尾提示。

        install.py step 8 已做的部分（plugin 分发 / adapter 部署 / config.json patch /
        .env 写入 / hermes plugins enable），install-hermes.sh --post-install 的分工：

          - **跳过**：step 3 (sync-skills) / step 4 (复制插件)——依赖 tarball 里没有的
            scripts/ / skills/，只能整段跳，由 POST_INSTALL_SKIP guard 兜底
          - **幂等重跑**：step 5 (config set) / 6 (.env) / 7 (backend 重启) / 8 (enable)
            —— 都是幂等的，重跑一遍保证状态收敛（代价是 step 7 会多一次 stop+3s+start）
          - **本模式真正补齐**：环境变量持久化、感知模型配置和 disable 残留清理、
            9 版本记录、10 cron reconcile、收尾 banner「hermes gateway restart」提示

        install-hermes.sh 由 build.sh::build_hermes 打进 miloco-hermes-plugin tarball，
        跟 miloco-plugin 平级。找不到就 warn 跳过（不阻塞主流程）。
        """
        script = extract_dir / "install-hermes.sh"
        if not script.is_file():
            self.ui.warn(self.ui.i18n.t("plugin.hermes_post_install_missing"))
            return
        env = os.environ.copy()
        env["MILOCO_HOME"] = str(self.miloco_home)
        env["HERMES_HOME"] = str(hermes_home)
        # install-hermes.sh 的 1.9 遇到已有 `export MILOCO_HOME=` 时会原地改写
        # 整行（re.sub 全局），会把用户自己指向别处的 export 覆盖掉，卸载时
        # 就再也无法保留用户数据。先把用户行摘出来，跑完再把它们插回安装器
        # 标记块之前（保证安装器的 export 在后、新 shell 里生效）。
        saved = self._hermes_stash_user_rc_exports()
        try:
            subprocess.run(
                ["bash", str(script), "--post-install"],
                check=True, env=env, stdin=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            self.ui.warn(self.ui.i18n.t("plugin.hermes_post_install_failed", str(exc.returncode)))
        finally:
            self._hermes_restore_user_rc_exports(saved)

    def _hermes_stash_user_rc_exports(
        self, home: Path | None = None
    ) -> dict[Path, list[str]]:
        """临时摘掉用户自己的 `export MILOCO_HOME`，返回 {rc 文件: 原行}。"""
        saved: dict[Path, list[str]] = {}
        home = home or Path.home()
        for rc in (home / ".zshrc", home / ".bashrc"):
            if not rc.is_file():
                continue
            try:
                text = rc.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            keep: list[str] = []
            user_lines: list[str] = []
            for line in text.splitlines(keepends=True):
                match = _RC_EXPORT_RE.match(line.rstrip("\r\n"))
                value = _unquote(match.group(1)) if match else ""
                if value and not _same_path(value, {self.miloco_home}):
                    user_lines.append(line)
                else:
                    keep.append(line)
            if not user_lines:
                continue
            try:
                _atomic_write_text(rc, "".join(keep))
            except OSError:
                continue
            saved[rc] = user_lines
        return saved

    def _hermes_restore_user_rc_exports(self, saved: dict[Path, list[str]]) -> None:
        for rc, user_lines in saved.items():
            try:
                text = rc.read_text(encoding="utf-8")
                lines = text.splitlines(keepends=True)
                index = next(
                    (
                        i
                        for i, line in enumerate(lines)
                        if _RC_MARKER_RE.match(line.rstrip("\r\n"))
                    ),
                    None,
                )
                if index is None:
                    # install-hermes.sh 遇到已有 export 是原地更新，不写 marker；
                    # 此时以第一个受管 export 作为锚点，保证用户行仍在它之前。
                    for i, line in enumerate(lines):
                        match = _RC_EXPORT_RE.match(line.rstrip("\r\n"))
                        if match and _same_path(
                            _unquote(match.group(1)), {self.miloco_home}
                        ):
                            index = i
                            break
                if index is None:
                    if text and not text.endswith("\n"):
                        text += "\n"
                    text = text + "".join(user_lines)
                else:
                    lines[index:index] = user_lines
                    text = "".join(lines)
                _atomic_write_text(rc, text)
            except (OSError, UnicodeDecodeError) as exc:
                self.ui.warn(
                    self.ui.i18n.t("plugin.hermes_rc_restore_failed", str(rc), str(exc))
                )

    def _hermes_deploy_plugin(self, hermes_home: Path, extract_dir: Path) -> None:
        plugin_dir = hermes_home / "plugins" / "miloco" / "miloco-plugin"
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir)
        plugin_dir.parent.mkdir(parents=True, exist_ok=True)
        src_plugin = extract_dir / "miloco-plugin"
        if src_plugin.exists():
            shutil.copytree(src_plugin, plugin_dir)

        skills_dir = extract_dir / "miloco-plugin-skills"
        target_skills = hermes_home / "skills"
        if skills_dir.exists():
            target_skills.mkdir(parents=True, exist_ok=True)
            for skill in skills_dir.iterdir():
                dest = target_skills / skill.name
                if dest.exists():
                    shutil.rmtree(dest)
                if skill.is_dir():
                    shutil.copytree(skill, dest)
                else:
                    shutil.copy2(skill, dest)

    def _hermes_deploy_adapter(self, extract_dir: Path) -> None:
        adapter_dir = self.miloco_home / "agent_platform" / "hermes"
        adapter_dir.mkdir(parents=True, exist_ok=True)
        # adapter.py + __init__.py（hermes_adapter 目录内）
        src_adapter = extract_dir / "miloco-plugin" / "hermes_adapter"
        if src_adapter.exists():
            for f in src_adapter.iterdir():
                if f.suffix == ".py":
                    shutil.copy2(f, adapter_dir / f.name)
        # 适配器运行时依赖：跟 adapter.py 平铺同目录才被 submodule_search_locations 发现
        src_plugin = extract_dir / "miloco-plugin"
        for dep in ("context_injection.py", "catalog.py", "paths.py"):
            dep_path = src_plugin / dep
            if dep_path.exists():
                shutil.copy2(dep_path, adapter_dir / dep)

    def _hermes_get_or_create_bearer(self, hermes_home: Path) -> str:
        cfg = self.miloco_home / "config.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                existing = (data.get("agent") or {}).get("auth_bearer")
                if existing:
                    return existing
            except Exception:
                pass
        bearer = secrets.token_hex(32)
        return bearer

    def _hermes_patch_env(self, hermes_home: Path, bearer: str) -> None:
        env_file = hermes_home / ".env"
        existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        key_line = f"API_SERVER_KEY={bearer}"
        if "API_SERVER_KEY=" in existing:
            existing = "\n".join(
                line if not line.startswith("API_SERVER_KEY=") else key_line
                for line in existing.split("\n")
            )
        else:
            existing = existing.rstrip("\n") + "\n" + key_line + "\n"
        env_file.write_text(existing, encoding="utf-8")

    # plugin 注册到 OpenClaw 后还要把 builtin tool 名加进 tools.alsoAllow 白名单
    def _plugin_tools(self) -> list[str]:
        manifest_path = self.script_dir / "manifest.json"
        if not manifest_path.is_file():
            self.ui.warn(self.ui.i18n.t("plugin.manifest_not_found"))
            return []

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = manifest.get("tools", [])
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            self.ui.warn(self.ui.i18n.t("plugin.manifest_invalid_tools"))
            return []

        # dev 模式下，plugin.json 可能包含 manifest 中尚未同步的新 tool
        if self.dev:
            plugin_json = (
                self.script_dir.parent / "plugins" / "openclaw" / "openclaw.plugin.json"
            )
            if plugin_json.is_file():
                try:
                    plugin_cfg = json.loads(plugin_json.read_text(encoding="utf-8"))
                    extra = plugin_cfg.get("contracts", {}).get("tools", [])
                    if isinstance(extra, list) and all(isinstance(t, str) for t in extra):
                        merged = sorted({*tools, *extra})
                        if len(merged) > len(tools):
                            return merged
                except (json.JSONDecodeError, KeyError):
                    pass

        return tools

    def _register_plugin_tools(self) -> None:
        get = self.ui.run_with_spinner(
            ["openclaw", "config", "get", "tools.alsoAllow", "--json"],
            self.ui.i18n.t("plugin.registering_tools"),
            check=False,
            text=True,
        )
        if get.returncode == 0 and get.stdout.strip() and get.stdout.strip() != "null":
            try:
                current = json.loads(get.stdout)
            except json.JSONDecodeError:
                current = []
        else:
            current = []
        if not isinstance(current, list):
            current = []

        merged = sorted({*current, *self._plugin_tools()})
        if merged == sorted(current):
            self.ui.step_skip(self.ui.i18n.t("plugin.tools_already_registered"))
            return
        self.ui.run_with_spinner(
            [
                "openclaw",
                "config",
                "set",
                "tools.alsoAllow",
                json.dumps(merged),
                "--strict-json",
            ],
            self.ui.i18n.t("plugin.registering_tools"),
            text=True,
        )
        self.ui.step_ok(self.ui.i18n.t("plugin.tools_registered"))

    def _enable_conversation_access(self) -> None:
        """开启 agent I/O hook：插件 trace（debug）靠它 hook agent 输入输出；插件的
        上下文溢出自愈也从 trace meta 读取结果判定，故同样依赖此开关——关闭时自愈静默
        退化为原有"卡死"行为（安全降级，不报错）。失败不致命，仅 skip 提示。"""
        key = (
            'plugins.entries["miloco-openclaw-plugin"]'
            ".hooks.allowConversationAccess"
        )
        result = self.ui.run_with_spinner(
            ["openclaw", "config", "set", key, "true"],
            self.ui.i18n.t("plugin.enabling_conversation_access"),
            check=False,
            text=True,
        )
        if result.returncode == 0:
            self.ui.step_ok(self.ui.i18n.t("plugin.conversation_access_ok"))
        else:
            self.ui.step_skip(self.ui.i18n.t("plugin.conversation_access_fail"))

    # ── Agent mode ─────────────────────────────────────────

    def run_agent_step1(self) -> None:
        """Step 1: env check, install, service init. Output JSON status."""
        self._print_welcome()
        # dev 下在 prepare 阶段构建一次；finish (step3) 复用产物不再构建。
        if self.dev:
            self._run_dev_build()
        # release 下保留下载缓存，供 step3（finish）复用，免重复下载整包。
        self._keep_cache = True
        self._service_started = False
        self._steps = [
            ("env", self._step_check_deps),
            ("install", self._step_install),
            ("download", self._step_download),
            ("service", self._step_init_service),
        ]
        self._total_steps = len(self._steps)
        for i, (_, fn) in enumerate(self._steps, 1):
            self._current_step = i
            fn()

        # Gather account status
        account_info = self._agent_get_account_info()
        # Gather model config
        model_info = self._agent_get_model_info()

        result = {
            "status": "ok",
            "account": account_info,
            "model": model_info,
        }
        # Output JSON to stdout for agent consumption
        print("\n--- AGENT_JSON_START ---")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("--- AGENT_JSON_END ---")

    def run_agent_step3(
        self,
        *,
        omni_model: str | None = None,
        omni_base_url: str | None = None,
    ) -> None:
        """Step 3: service init, configure account/model, download, plugin."""
        self._print_welcome()
        self._service_started = False
        # download 前置到 service 之前：与 Installer.run 对齐；step1 已解压过时再解一次
        # 幂等（tar 覆盖同名文件），代价 <1s，换来 step1 未跑到解压时的兜底补齐。
        self._steps = [
            ("download", self._step_download),
            ("service", self._step_init_service),
            ("account", self._step_account),
            ("model", self._agent_step_configure_model),
            ("plugin", self._step_plugin),
        ]
        self._omni_model = omni_model
        self._omni_base_url = omni_base_url
        self._total_steps = len(self._steps)
        for i, (_, fn) in enumerate(self._steps, 1):
            self._current_step = i
            fn()
        self._print_summary()

    def _agent_get_account_info(self) -> dict:
        info: dict = {"is_bound": False, "bind_url": None, "user": None}
        try:
            result = subprocess.run(
                ["miloco-cli", "account", "status"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                status_data = json.loads(result.stdout)
                if status_data.get("code") == 0:
                    data = status_data.get("data", {})
                    info["is_bound"] = data.get("is_bound", False)
                    info["user"] = data.get("user_info")
        except Exception:
            pass

        # Always generate a new bind URL for agent to offer re-binding
        try:
            result = subprocess.run(
                ["miloco-cli", "account", "bind", "--no-wait"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("http"):
                        info["bind_url"] = line
                        break
        except Exception:
            pass

        return info

    def _agent_get_model_info(self) -> dict:
        model, base_url, api_key = self._get_current_config()
        return {
            "configured": bool(model and base_url and api_key),
            "model": model,
            "base_url": base_url,
            "api_key_masked": self._mask_key(api_key) if api_key else None,
        }

    def _agent_step_configure_model(self) -> None:
        """Configure model in agent mode using CLI args."""
        self._step_header("model.title", "model.subtitle")

        pairs: list[str] = []
        if getattr(self, "_omni_model", None):
            pairs.extend(["model.omni.model", self._omni_model])
        if getattr(self, "_omni_base_url", None):
            pairs.extend(["model.omni.base_url", self._omni_base_url])
        if self.omni_api_key:
            pairs.extend(["model.omni.api_key", self.omni_api_key])

        if not pairs:
            self.ui.step_skip(self.ui.i18n.t("model.skip_non_interactive"))
            return

        subprocess.run(
            ["miloco-cli", "config", "set", *pairs, "--no-restart"],
            check=True,
            capture_output=True,
        )
        self.ui.step_ok(self.ui.i18n.t("model.config_saved"))

    # ── Summary ────────────────────────────────────────────

    def _print_summary(self) -> None:
        self.ui.console.print()
        # 有失败 step 时先打 yellow 警告 + 列出，避免绿框盖住单步 ✗。
        if self.ui.failed_steps:
            self.ui.console.print(
                "[bold yellow]══════════════════════════════════════[/bold yellow]"
            )
            self.ui.console.print(
                f"[bold yellow]⚠[/bold yellow] [bold]{self.ui.i18n.t('summary.with_failures')}[/bold]"
            )
            for msg, hint in self.ui.failed_steps:
                self.ui.console.print(f"  [red]✗[/red] {msg}")
                if hint:
                    self.ui.console.print(f"    [dim]{hint}[/dim]")
            self.ui.console.print()
        self.ui.console.print(
            "[bold green]══════════════════════════════════════[/bold green]"
        )
        self.ui.console.print(
            f"[bold green]✓[/bold green] [bold]{self.ui.i18n.t('summary.title')}[/bold]"
        )
        self.ui.console.print()
        self.ui.console.print(f"[dim]{self.ui.i18n.t('summary.next_steps')}[/dim]")
        if self.agent_platform == "hermes":
            self.ui.console.print(
                f"  [cyan]hermes gateway restart[/cyan]    {self.ui.i18n.t('summary.restart_hermes_desc')}"
            )
        elif self.skip_openclaw:
            self.ui.console.print(
                f"  [cyan]miloco-cli service start[/cyan]    {self.ui.i18n.t('summary.start_service_desc')}"
            )
        else:
            self.ui.console.print(
                f"  [cyan]openclaw gateway restart[/cyan]    {self.ui.i18n.t('summary.restart_gateway_desc')}"
            )
        self.ui.console.print(
            f"  [cyan]miloco-cli --help[/cyan]           {self.ui.i18n.t('summary.help_desc')}"
        )
        self.ui.console.print()
        self.ui.console.print(
            f"[dim]{self.ui.i18n.t('summary.rc_hint')}[/dim]"
        )
        self.ui.console.print()
        self.ui.console.print(
            f"[dim]{self.ui.i18n.t('summary.config_label')}[/dim]  {self.miloco_home}"
        )
        self.ui.console.print()


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
            return CleanupItem(
                key,
                "hermes",
                target,
                ItemStatus.FAILED,
                error,
                "hermes plugins list --plain --no-bundled",
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


def run_uninstall(ui: UI, *, args: argparse.Namespace) -> int:
    ui.info(ui.i18n.t("uninstall.title"))
    _remove_launchagent(ui)
    env = dict(os.environ)
    # 安装器把实际使用的 home 持久化在 runtime pointer；卸载也要把它纳入盘点。
    if "MILOCO_HOME" not in env:
        env["MILOCO_HOME"] = str(_resolve_uninstall_home())
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



# ---------------------------------------------------------------------------
# Arg parsing & entry
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Miloco Installer")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Dev install: build from source (scripts/build.sh) then install from dist/",
    )
    parser.add_argument("--lang", default=None, help="Language (en/zh)")
    parser.add_argument(
        "--omni-api-key",
        dest="omni_api_key",
        help="Omni model API key (auto-configure MiMo)",
    )
    parser.add_argument(
        "--omni-model",
        dest="omni_model",
        help="Omni model name (default: mimo-v2.6-flash)",
    )
    parser.add_argument(
        "--omni-base-url",
        dest="omni_base_url",
        help="Omni model base URL (default: https://api.xiaomimimo.com/v1)",
    )
    parser.add_argument(
        "--uninstall", action="store_true", help="Uninstall all miloco components"
    )
    parser.add_argument(
        "--account-auth",
        dest="account_auth",
        metavar="PAYLOAD",
        help="Base64 auth payload for non-interactive Mi Home account binding",
    )
    parser.add_argument("--skip-openclaw", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--agent-platform",
        dest="agent_platform",
        default="",
        choices=["", "openclaw", "hermes"],
        help="Agent platform for plugin deployment (auto-detected in non-interactive mode; defaults to openclaw interactively)",
    )
    parser.add_argument(
        "--agent-prepare",
        dest="agent_prepare",
        action="store_true",
        help="Agent mode step 1: env check, install, service init, output status JSON",
    )
    parser.add_argument(
        "--agent-finish",
        dest="agent_finish",
        action="store_true",
        help="Agent mode step 2: configure account/model, download models, install plugin",
    )
    return parser.parse_args()


def _print_error_summary(ui: UI, _args: argparse.Namespace) -> None:
    ui.console.print()
    original_args = " ".join(sys.argv[1:])
    ui.console.print(
        f"[bold red]✗[/bold red] [bold]{ui.i18n.t('summary.error_title')}[/bold]"
    )
    ui.console.print()
    ui.console.print(f"  {ui.i18n.t('summary.retry_hint', original_args)}")
    ui.console.print(f"  {ui.i18n.t('summary.uninstall_hint')}")
    ui.console.print()


def _default_runtime_pointer() -> Path:
    return Path.home() / ".config" / "miloco" / "default.env"


def _runtime_pointer_candidates() -> list[Path]:
    candidates: list[Path] = []
    if runtime_env := os.environ.get("MILOCO_RUNTIME_ENV"):
        candidates.append(Path(runtime_env).expanduser())
    default = _default_runtime_pointer()
    if default not in candidates:
        candidates.append(default)
    return candidates


def _read_runtime_pointer(path: Path) -> dict[str, str]:
    """读取与 CLI 配置及 Hermes 安装脚本共用的 runtime 指针格式。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        try:
            assignment = shlex.split(line, comments=False, posix=True)
        except ValueError:
            continue
        if len(assignment) != 1 or "=" not in assignment[0]:
            continue
        key, _, value = assignment[0].partition("=")
        key = key.strip()
        if not key or not (key[0].isalpha() or key[0] == "_"):
            continue
        if not all(char.isalnum() or char == "_" for char in key):
            continue
        values[key] = value
    return values


def _detect_installed_agent_platform() -> str | None:
    """检测本机已装的 agent runtime。hermes / openclaw / None（未识别）。

    判定信号（任一命中即返）：
    - 安装器持久化的 ``MILOCO_AGENT_PLATFORM``（环境变量优先于指针文件）
    - hermes: $HERMES_HOME / ~/.hermes / hermes CLI / systemd unit
    - openclaw: ~/.openclaw / openclaw CLI / systemd unit
    - 两边都命中或都未命中 → None（调用方显式指定，避免猜错）
    """
    explicit = os.environ.get("MILOCO_AGENT_PLATFORM", "")
    if explicit in {"hermes", "openclaw"}:
        return explicit
    for path in _runtime_pointer_candidates():
        platform = _read_runtime_pointer(path).get("MILOCO_AGENT_PLATFORM", "")
        if platform in {"hermes", "openclaw"}:
            return platform

    hermes_home_env = os.environ.get("HERMES_HOME", "")
    if hermes_home_env and Path(hermes_home_env).expanduser().is_dir():
        return "hermes"

    user_units = Path.home() / ".config" / "systemd" / "user"
    hermes_home_detected = any(
        (
            (Path.home() / ".hermes").is_dir(),
            (user_units / "hermes-gateway.service").exists(),
        )
    )
    openclaw_home_detected = any(
        (
            (Path.home() / ".openclaw").is_dir(),
            (user_units / "openclaw-gateway.service").exists(),
            (user_units / "clawdbot-gateway.service").exists(),
        )
    )

    # 目录/unit 比 PATH 中可能残留的 CLI 更可靠；只有两边都没有持久化信号时
    # 才使用 CLI 兜底。两边同时命中则要求调用方显式选择。
    if hermes_home_detected or openclaw_home_detected:
        if hermes_home_detected == openclaw_home_detected:
            return None
        return "hermes" if hermes_home_detected else "openclaw"

    hermes_cli = shutil.which("hermes") is not None
    openclaw_cli = shutil.which("openclaw") is not None
    if hermes_cli == openclaw_cli:
        return None
    return "hermes" if hermes_cli else "openclaw"


def _verify_platform_installed(agent_platform: str, i18n: I18n) -> None:
    """检查用户指定的平台并以显式参数为准。

    平台参数可能来自后端升级链路。调用方已经明确指定平台时，探测只能作为
    诊断提示，不能因为 macOS、非默认 HERMES_HOME 或残留 runtime 误判而阻断升级。
    """
    detected = _detect_installed_agent_platform()
    if detected is None:
        print(
            f"[install] {i18n.t('platform.explicit_not_detected', agent_platform)}",
            file=sys.stderr,
        )
    elif detected != agent_platform:
        print(
            f"[install] {i18n.t('platform.explicit_mismatch', agent_platform, detected)}",
            file=sys.stderr,
        )


def _decide_agent_platform(
    args: argparse.Namespace, plat: "Platform", ui: "UI"
) -> str:
    """决定 agent_platform：--agent-platform > 本机 detect（必命中）> 交互 prompt。

    必须在 miloco_home 计算之前跑，否则交互选 hermes 时 miloco_home 仍走 openclaw 默认。

    非交互路径（agent-prepare/agent-finish/no tty）走本机 detect：
    - 检测命中（hermes/openclaw）→ 用检测结果
    - 检测未命中（都没装）→ 视为配置错误，raise SystemExit 中断安装

    原因：silent fallback "openclaw" 会导致在只装 hermes 的机器上把 miloco plugin 装到
    ~/.openclaw/miloco，hermes 找不到 → 后续 agent flow 拉 miloco 全失败。
    同样的反向问题也存在 hermes fallback 到 openclaw 上。

    用户显式 --agent-platform=hermes/openclaw：探测结果只告警，不否决显式参数。
    --uninstall 在 main() 里更早分流，不会走到这里。
    """
    if args.agent_platform:
        # 用户显式指定：探测只用于告警，不能否决调用方的权威参数。
        # agent-prepare/agent-finish 自动流程不走这条（args.agent_platform == ""），
        # 所以这里只处理明确指定的平台。
        _verify_platform_installed(args.agent_platform, ui.i18n)
        return args.agent_platform
    if (
        args.agent_prepare
        or args.agent_finish
        or not plat.is_interactive
    ):
        detected = _detect_installed_agent_platform()
        if detected is not None:
            return detected
        # 非交互 + 探测未命中 = 配置错误，中断。不再兼容 fallback openclaw。
        msg = ui.i18n.t("platform.detect_failed")
        raise SystemExit(f"[install] FATAL: {msg}")
    openclaw_label = ui.i18n.t("platform.openclaw_option")
    hermes_label = ui.i18n.t("platform.hermes_option")
    choice = ui.prompt_select(
        ui.i18n.t("platform.ask"),
        choices=[openclaw_label, hermes_label],
        default=openclaw_label,
    )
    return "hermes" if choice == hermes_label else "openclaw"


def _default_miloco_home(agent_platform: str) -> Path:
    """返回指定平台的 Miloco 默认数据目录。

    Hermes 通过 ``HERMES_HOME`` 管理 runtime 根目录；OpenClaw 为保持兼容性，
    继续使用历史默认目录 ``~/.openclaw/miloco``。
    """
    if agent_platform == "hermes":
        hermes_home = Path(
            os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
        ).expanduser()
        return hermes_home / "miloco"
    return Path.home() / ".openclaw" / "miloco"


def _resolve_install_home(agent_platform: str) -> Path:
    """解析本次安装使用的数据目录，并保留同平台已持久化的自定义目录。

    显式 ``MILOCO_HOME`` 最优先；否则依次读取 ``MILOCO_RUNTIME_ENV`` 指定的
    profile 和默认指针。profile 缺少平台字段时从低优先级候选补齐，与 CLI 和
    shell header 的按键合并语义一致；补齐后平台仍须与本次选择一致，避免切换
    agent 平台时误复用另一平台的数据目录。没有匹配指针时才回退平台默认目录。
    """
    if env_home := os.environ.get("MILOCO_HOME"):
        return Path(env_home).expanduser()
    candidates = [
        _read_runtime_pointer(path) for path in _runtime_pointer_candidates()
    ]
    for index, values in enumerate(candidates):
        persisted_home = values.get("MILOCO_HOME")
        if not persisted_home:
            continue
        platform = values.get("MILOCO_AGENT_PLATFORM", "")
        if not platform:
            for lower_priority in candidates[index + 1 :]:
                platform = lower_priority.get("MILOCO_AGENT_PLATFORM", "")
                if platform:
                    break
        if platform == agent_platform:
            return Path(persisted_home).expanduser()
    return _default_miloco_home(agent_platform)


def _warn_runtime_pointer_overwrite(agent_platform: str, i18n: I18n) -> None:
    """默认指针将切换平台时提示使用独立 profile，避免旧目录静默失联。"""
    existing = _read_runtime_pointer(_default_runtime_pointer())
    existing_platform = existing.get("MILOCO_AGENT_PLATFORM", "")
    if existing_platform and existing_platform != agent_platform:
        warning = i18n.t(
            "platform.pointer_overwrite_warning",
            existing_platform,
            agent_platform,
        )
        print(
            f"[install] {warning}",
            file=sys.stderr,
        )


def _write_runtime_pointer(miloco_home: Path, agent_platform: str) -> Path:
    """写入与 CLI 配置及 Hermes 安装脚本共用的 runtime 指针格式。"""
    runtime_dir = _default_runtime_pointer().parent
    runtime_file = runtime_dir / "default.env"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_dir, 0o700)

    updates = {
        "MILOCO_HOME": str(miloco_home),
        "MILOCO_AGENT_PLATFORM": agent_platform,
    }
    try:
        lines = runtime_file.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        lines = []

    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in updates:
            if key not in seen:
                output.append(f"{key}={shlex.quote(updates[key])}\n")
                seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={shlex.quote(value)}\n")

    runtime_file.write_text("".join(output), encoding="utf-8")
    os.chmod(runtime_file, 0o600)
    return runtime_file


def _resolve_uninstall_home() -> Path:
    if env_home := os.environ.get("MILOCO_HOME"):
        return Path(env_home).expanduser()
    for path in _runtime_pointer_candidates():
        if home := _read_runtime_pointer(path).get("MILOCO_HOME"):
            return Path(home).expanduser()
    detected = _detect_installed_agent_platform()
    return _default_miloco_home(detected or "openclaw")


def _warn_legacy_hermes_home(
    miloco_home: Path, agent_platform: str, i18n: I18n
) -> None:
    """在改用 HERMES_HOME 派生的新目录、放弃旧版数据前发出告警。"""
    if agent_platform != "hermes":
        return
    legacy_home = Path.home() / ".hermes" / "miloco"
    if legacy_home == miloco_home:
        return
    if (legacy_home / "config.json").is_file() and not (
        miloco_home / "config.json"
    ).exists():
        print(
            f"[install] {i18n.t('platform.legacy_home_warning', str(legacy_home), str(miloco_home))}",
            file=sys.stderr,
        )


def main() -> None:
    args = parse_args()

    plat = Platform.detect(lang_override=args.lang)
    i18n = I18n(plat.lang, Path(__file__).parent)
    ui = UI(i18n)

    # curl|bash 场景 stdin 被 pipe 占用，Platform.detect() 拿到 is_interactive=False。
    # 主动 open("/dev/tty") 抢回交互能力（下面还会再做一次严格 fallback + fatal 报错，
    # 这里只是提前，让 _decide_agent_platform 的 platform prompt 能弹出来）。
    if not plat.is_interactive and _try_tty_fallback():
        plat = replace(plat, is_interactive=True)

    # --uninstall 必须在 mkdir / 平台决策之前分流：卸载是只读盘点 + 按痕迹清理，
    # 不依赖 --agent-platform，也不能因为非交互环境而无法清理。
    if args.uninstall:
        rc = run_uninstall(ui, args=args)
        _default_runtime_pointer().unlink(missing_ok=True)
        sys.exit(rc)

    agent_platform = _decide_agent_platform(args, plat, ui)
    miloco_home = _resolve_install_home(agent_platform)
    miloco_home.mkdir(parents=True, exist_ok=True)
    _warn_legacy_hermes_home(miloco_home, agent_platform, ui.i18n)
    # 关键：导出到 environ，让所有子进程 (miloco-cli / supervisord / backend / 打包 wheel
    # 起的 miloco.main) 继承正确的 MILOCO_HOME，否则 backend fallback 到 ~/.openclaw/miloco。
    os.environ["MILOCO_HOME"] = str(miloco_home)
    _warn_runtime_pointer_overwrite(agent_platform, ui.i18n)
    _write_runtime_pointer(miloco_home, agent_platform)

    # Agent mode: --agent-prepare or --agent-finish implies non-interactive agent flow
    if args.agent_prepare or args.agent_finish:
        downloader = Downloader()
        installer = Installer(
            plat=plat,
            ui=ui,
            downloader=downloader,
            dev=args.dev,
            omni_api_key=args.omni_api_key,
            account_auth=args.account_auth,
            miloco_home=miloco_home,
            skip_openclaw=args.skip_openclaw,
            agent_platform=agent_platform,
        )
        atexit.register(installer._stop_service)
        atexit.register(installer._cleanup_install_cache)

        try:
            if args.agent_prepare:
                installer.run_agent_step1()
            else:
                installer.run_agent_step3(
                    omni_model=args.omni_model,
                    omni_base_url=args.omni_base_url,
                )
            # 安装期间服务只为预热/账号/模型等步骤临时拉起，无论成功失败都在退出时
            # 停掉，不留长驻进程；需要运行时由用户/agent 显式 service start。
        except subprocess.CalledProcessError as e:
            error_output = {
                "status": "error",
                "command": " ".join(str(x) for x in e.cmd),
                "returncode": e.returncode,
                "stderr": (
                    e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr
                )
                or "",
            }
            print("\n--- AGENT_JSON_START ---")
            print(json.dumps(error_output, indent=2, ensure_ascii=False))
            print("--- AGENT_JSON_END ---")
            sys.exit(1)
        return

    if not plat.is_interactive:
        if not _try_tty_fallback():
            ui.fail(ui.i18n.t("error.non_interactive"))

    downloader = Downloader()
    installer = Installer(
        plat=plat,
        ui=ui,
        downloader=downloader,
        dev=args.dev,
        omni_api_key=args.omni_api_key,
        account_auth=args.account_auth,
        miloco_home=miloco_home,
        skip_openclaw=args.skip_openclaw,
        agent_platform=agent_platform,
    )

    atexit.register(installer._stop_service)
    atexit.register(installer._cleanup_install_cache)

    try:
        installer.run()
        # 安装期间服务只为预热/账号/模型等步骤临时拉起，无论成功失败都在退出时
        # 停掉，不留长驻进程；需要运行时由用户/agent 显式 service start。
    except KeyboardInterrupt:
        ui.warn(f"\n{ui.i18n.t('error.cancelled')}")
        sys.exit(130)
    except subprocess.CalledProcessError as e:
        ui.warn(ui.i18n.t("error.command_failed", " ".join(str(x) for x in e.cmd)))
        _print_error_summary(ui, args)
        sys.exit(1)


if __name__ == "__main__":
    main()
