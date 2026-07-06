# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "rich>=13.0",
#   "httpx>=0.27",
#   "questionary>=2.1",
# ]
# [tool.uv]
# ///
"""Miloco Installer — Python core logic.

Handles the full installation flow:
  Phase 1: Environment check
  Phase 2: Package install + supervisor (web 静态资源由 miloco wheel 自带)
  Phase 3: Initialize service (engine warm-up; 仅临时拉起，退出时自动 stop)
  Phase 4: Mi Home account binding
  Phase 5: Omni model configuration
  Phase 6: Perception model download
  Phase 7: OpenClaw plugin installation (optional)
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import hashlib
import json
import locale
import os
import platform as _platform
import shutil
import signal
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, NoReturn

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


def _try_tty_fallback() -> bool:
    """Attempt /dev/tty fallback. Returns True if stdin is now interactive."""
    if sys.stdin.isatty():
        return True
    if sys.platform == "win32":
        return False
    tty_path = "/dev/tty"
    if not os.path.exists(tty_path):
        return False
    try:
        tty_file = open(tty_path)  # noqa: SIM115
        sys.stdin = tty_file
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
        agent: str = "openclaw",
    ) -> None:
        self.platform = plat
        self.ui = ui
        self.downloader = downloader
        self.dev = dev
        self.omni_api_key = omni_api_key
        self.account_auth = account_auth
        self.miloco_home = miloco_home
        self.agent = agent
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
        self._steps: list[tuple[str, Callable[[], None]]] = [
            ("env", self._step_check_deps),
            ("install", self._step_install),
            ("service", self._step_init_service),
            ("account", self._step_account),
            ("model", self._step_configure),
            ("download", self._step_download),
            ("plugin", self._step_plugin),
        ]
        self._total_steps = len(self._steps)
        for i, (_, fn) in enumerate(self._steps, 1):
            self._current_step = i
            fn()
        self._print_summary()

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
        self._install_from_dir(self._get_src_dir(), reinstall=self.dev)
        self._install_supervisor()
        self._configure_python_bin()

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
            self.ui.warn(f"Failed to extract {tarballs[0].name}: {exc}")
            return 0
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
            model_name = "xiaomi/mimo-v2.5"
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
            "xiaomi/mimo-v2.5",
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
        count = self._extract_models(self._get_src_dir(), models_dest)
        if count:
            self.ui.step_ok(self.ui.i18n.t("download.models_verified", str(count)))
        else:
            self.ui.step_skip(self.ui.i18n.t("download.no_models"))

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

        if self.agent == "none":
            self.ui.step_skip(self.ui.i18n.t("plugin.skipped"))
        elif self.agent == "hermes":
            self._step_plugin_hermes()
        else:
            self._step_plugin_openclaw()

    def _step_plugin_hermes(self) -> None:
        hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        plugins_dir = hermes_home / "plugins" / "miloco"
        repo_root = self.script_dir.parent
        plugin_src = repo_root / "plugins" / "hermes"
        skills_src = repo_root / "plugins" / "skills"

        if not plugin_src.is_dir():
            self.ui.step_fail(self.ui.i18n.t("plugin.hermes_src_not_found"))
            return

        import shutil as _shutil

        if plugins_dir.exists():
            _shutil.rmtree(plugins_dir)
        _shutil.copytree(
            plugin_src, plugins_dir,
            ignore=_shutil.ignore_patterns("tests", "__pycache__", "*.pyc",
                                            ".pytest_cache", "integration-test", "scripts"),
        )
        self.ui.step_ok(self.ui.i18n.t("plugin.hermes_copied"))

        skills_dst = plugins_dir / "skills"
        if skills_src.is_dir():
            if skills_dst.exists():
                _shutil.rmtree(skills_dst)
            _shutil.copytree(skills_src, skills_dst)
            count = sum(1 for d in skills_dst.iterdir()
                        if d.is_dir() and (d / "SKILL.md").exists())
            self.ui.step_ok(self.ui.i18n.t("plugin.hermes_skills_synced", str(count)))

        if shutil.which("hermes"):
            subprocess.run(["hermes", "plugins", "enable", "miloco"],
                           capture_output=True, check=False)
            self.ui.step_ok(self.ui.i18n.t("plugin.hermes_enabled"))

            bin_dir = str(Path.home() / ".local" / "bin")
            try:
                subprocess.run(
                    [
                        "hermes", "config", "set",
                        "plugins.entries.miloco.bin_path", bin_dir,
                    ],
                    capture_output=True, check=True, timeout=10,
                )
                self.ui.step_ok(f"plugins.entries.miloco.bin_path set to {bin_dir}")
            except Exception:
                self.ui.step_skip("Could not set bin_path config")
        else:
            self.ui.step_skip(self.ui.i18n.t("plugin.hermes_cli_not_found"))

    def _step_plugin_openclaw(self) -> None:

        # dev 与 release 都从本地 .tgz 装（release 的来自下载归档解压后的缓存目录）。
        tgz_files = _visible(self._get_src_dir().glob("*.tgz"))
        if not tgz_files:
            self.ui.step_fail(self.ui.i18n.t("plugin.no_tgz"))
            raise subprocess.CalledProcessError(
                1, "openclaw plugins install", stderr="no .tgz found"
            )
        pkg = str(tgz_files[0])

        self.ui.run_with_spinner(
            ["openclaw", "plugins", "install", "--force", pkg],
            self.ui.i18n.t("plugin.installing"),
            text=True,
        )
        self.ui.step_ok(self.ui.i18n.t("plugin.plugin_ok"))

        self._register_plugin_tools()
        self._enable_conversation_access()

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
        self._steps = [
            ("service", self._step_init_service),
            ("account", self._step_account),
            ("model", self._agent_step_configure_model),
            ("download", self._step_download),
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
        if self.agent == "hermes":
            self.ui.console.print(
                f"  [cyan]hermes gateway restart[/cyan]        {self.ui.i18n.t('summary.restart_hermes_desc')}"
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
            f"[dim]{self.ui.i18n.t('summary.config_label')}[/dim]  {self.miloco_home}"
        )
        self.ui.console.print()


# ---------------------------------------------------------------------------
# Uninstaller
# ---------------------------------------------------------------------------


class Uninstaller:
    def __init__(self, ui: UI, miloco_home: Path, *, agent: str = "openclaw") -> None:
        self.ui = ui
        self.miloco_home = miloco_home
        self.agent = agent

    def run(self) -> None:
        self.ui.info(self.ui.i18n.t("uninstall.title"))

        for pkg, msg_key in [
            ("miloco-cli", "uninstall.cli_removed"),
            ("miloco", "uninstall.miloco_removed"),
            ("supervisor", None),
        ]:
            try:
                subprocess.run(
                    ["uv", "tool", "uninstall", pkg], capture_output=True, check=True
                )
                self.ui.ok(msg_key if msg_key else f"{pkg} uninstalled")
            except subprocess.CalledProcessError:
                pass

        if self.agent == "hermes":
            hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
            plugins_dir = hermes_home / "plugins" / "miloco"
            if plugins_dir.is_dir():
                shutil.rmtree(plugins_dir)
                self.ui.ok(self.ui.i18n.t("uninstall.plugin_removed"))
        elif shutil.which("openclaw"):
            try:
                subprocess.run(
                    ["openclaw", "plugins", "uninstall", "miloco-openclaw-plugin"],
                    input="y\n",
                    text=True,
                    capture_output=True,
                )
                self.ui.ok(self.ui.i18n.t("uninstall.plugin_removed"))
            except subprocess.CalledProcessError:
                pass

        if self.miloco_home.is_dir():
            if self.ui.prompt_confirm(
                self.ui.i18n.t("uninstall.delete_home_ask", str(self.miloco_home)),
                default=False,
            ):
                shutil.rmtree(self.miloco_home)
                self.ui.ok(self.ui.i18n.t("uninstall.home_deleted"))

        self.ui.ok(self.ui.i18n.t("uninstall.done"))


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
        help="Omni model name (default: xiaomi/mimo-v2.5)",
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
        "--agent",
        nargs="?",
        const="none",
        default=None,
        choices=["openclaw", "hermes"],
        help="Agent plugin to install: openclaw, hermes, or omit value for none. "
        "Without this flag, openclaw plugin is installed (default behavior).",
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


def main() -> None:
    args = parse_args()

    # --agent openclaw → 安装 OpenClaw 插件
    # --agent hermes  → 安装 Hermes 插件
    # --agent（不带值）→ 不安装任何插件
    # --skip-openclaw  → 不安装任何插件（兼容旧参数）
    # 无参数           → 安装 OpenClaw 插件（默认行为）
    if args.agent is not None:
        agent = args.agent  # "openclaw" / "hermes" / "none"
    elif args.skip_openclaw:
        agent = "none"
    else:
        agent = "openclaw"

    miloco_home = Path(
        os.environ.get("MILOCO_HOME", Path.home() / ".openclaw" / "miloco")
    )
    miloco_home.mkdir(parents=True, exist_ok=True)

    plat = Platform.detect(lang_override=args.lang)
    i18n = I18n(plat.lang, Path(__file__).parent)
    ui = UI(i18n)

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
            agent=agent,
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

    if args.uninstall:
        Uninstaller(ui, miloco_home, agent=agent).run()
        return

    downloader = Downloader()
    installer = Installer(
        plat=plat,
        ui=ui,
        downloader=downloader,
        dev=args.dev,
        omni_api_key=args.omni_api_key,
        account_auth=args.account_auth,
        miloco_home=miloco_home,
        agent=agent,
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
