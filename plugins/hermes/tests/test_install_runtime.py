"""安装器 runtime 平台探测与指针文件测试。

这些测试放在 Hermes CI 已覆盖的测试目录中，避免安装器核心回归测试只在本地执行。
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

try:
    import questionary  # noqa: F401
except ModuleNotFoundError:
    # 这些用例只覆盖安装器的标准库 runtime helper；不需要交互式 questionary。
    sys.modules["questionary"] = types.ModuleType("questionary")

try:
    from rich.console import Console  # noqa: F401
except ModuleNotFoundError:
    class _RichStub:
        def __init__(self, *args, **kwargs):
            pass

    rich = types.ModuleType("rich")
    rich.__path__ = []
    rich_console = types.ModuleType("rich.console")
    rich_console.Console = _RichStub
    rich_progress = types.ModuleType("rich.progress")
    for _name in (
        "BarColumn",
        "DownloadColumn",
        "Progress",
        "SpinnerColumn",
        "TextColumn",
        "TimeRemainingColumn",
        "TransferSpeedColumn",
    ):
        setattr(rich_progress, _name, _RichStub)
    sys.modules.update(
        {
            "rich": rich,
            "rich.console": rich_console,
            "rich.progress": rich_progress,
        }
    )


@pytest.fixture(scope="module")
def installer():
    path = Path(__file__).parents[3] / "scripts" / "install.py"
    spec = importlib.util.spec_from_file_location("miloco_install_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_home(monkeypatch, installer, tmp_path):
    monkeypatch.setattr(
        installer.Path,
        "home",
        classmethod(lambda _cls: tmp_path),
    )
    monkeypatch.delenv("MILOCO_HOME", raising=False)
    monkeypatch.delenv("MILOCO_AGENT_PLATFORM", raising=False)
    monkeypatch.delenv("MILOCO_RUNTIME_ENV", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)


def test_detect_prefers_explicit_platform_env(monkeypatch, installer, tmp_path):
    _fake_home(monkeypatch, installer, tmp_path)
    monkeypatch.setenv("MILOCO_AGENT_PLATFORM", "hermes")

    assert installer._detect_installed_agent_platform() == "hermes"


def test_detect_reads_runtime_pointer_before_heuristics(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("MILOCO_AGENT_PLATFORM='openclaw'\n", encoding="utf-8")

    assert installer._detect_installed_agent_platform() == "openclaw"


def test_explicit_platform_is_not_rejected_when_detection_is_missing(
    monkeypatch, installer, tmp_path, capsys
):
    _fake_home(monkeypatch, installer, tmp_path)

    installer._verify_platform_installed("hermes")

    assert "以显式指定为准" in capsys.readouterr().err


def test_write_runtime_pointer_quotes_paths_and_preserves_unknown_lines(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("CUSTOM='keep me'\nMILOCO_HOME=/old\n", encoding="utf-8")
    home = tmp_path / "miloco data"

    installer._write_runtime_pointer(home, "hermes")

    assert pointer.read_text(encoding="utf-8") == (
        "CUSTOM='keep me'\n"
        "MILOCO_HOME='" + str(home) + "'\n"
        "MILOCO_AGENT_PLATFORM=hermes\n"
    )
    assert oct(pointer.stat().st_mode & 0o777) == "0o600"


def test_resolve_uninstall_home_from_pointer_without_runtime(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    home = tmp_path / "miloco data"
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"MILOCO_HOME='{home}'\n", encoding="utf-8")

    assert installer._resolve_uninstall_home() == home
