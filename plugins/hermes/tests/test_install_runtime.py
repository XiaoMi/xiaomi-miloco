"""安装器 runtime 平台探测与指针文件测试。

这些测试放在 Hermes CI 已覆盖的测试目录中，避免安装器核心回归测试只在本地执行。
"""

import importlib.util
import json
import subprocess
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
    monkeypatch.setattr(installer.shutil, "which", lambda _name: None)


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


def test_detect_openclaw_from_home_without_systemd(
    monkeypatch, installer, tmp_path
):
    """macOS/仅 CLI 安装没有 systemd unit 时也能识别 OpenClaw。"""
    _fake_home(monkeypatch, installer, tmp_path)
    (tmp_path / ".openclaw").mkdir()

    assert installer._detect_installed_agent_platform() == "openclaw"


def test_detect_openclaw_from_cli_without_systemd_or_home(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: "/usr/local/bin/openclaw" if name == "openclaw" else None,
    )

    assert installer._detect_installed_agent_platform() == "openclaw"


def test_detect_requires_explicit_platform_when_both_runtimes_exist(
    monkeypatch, installer, tmp_path
):
    """旧安装没有指针且双 runtime 并存时，不按目录顺序静默猜平台。"""
    _fake_home(monkeypatch, installer, tmp_path)
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".openclaw").mkdir()

    assert installer._detect_installed_agent_platform() is None


def test_explicit_platform_is_not_rejected_when_detection_is_missing(
    monkeypatch, installer, tmp_path, capsys
):
    _fake_home(monkeypatch, installer, tmp_path)

    installer._verify_platform_installed(
        "hermes", installer.I18n("zh", Path(installer.__file__).parent)
    )

    assert "将按显式指定执行" in capsys.readouterr().err


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


def test_resolve_install_home_preserves_same_platform_pointer(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    custom_home = tmp_path / "custom miloco"
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        f"MILOCO_HOME='{custom_home}'\nMILOCO_AGENT_PLATFORM=hermes\n",
        encoding="utf-8",
    )

    resolved = installer._resolve_install_home("hermes")
    installer._write_runtime_pointer(resolved, "hermes")

    assert resolved == custom_home
    assert f"MILOCO_HOME='{custom_home}'" in pointer.read_text(encoding="utf-8")


def test_resolve_install_home_ignores_pointer_for_other_platform(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        "MILOCO_HOME=/data/openclaw\nMILOCO_AGENT_PLATFORM=openclaw\n",
        encoding="utf-8",
    )

    assert installer._resolve_install_home("hermes") == (
        tmp_path / ".hermes" / "miloco"
    )


def test_resolve_install_home_prefers_selected_profile(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    default_pointer = tmp_path / ".config" / "miloco" / "default.env"
    default_pointer.parent.mkdir(parents=True)
    default_pointer.write_text(
        "MILOCO_HOME=/data/default\nMILOCO_AGENT_PLATFORM=hermes\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profiles" / "custom.env"
    profile.parent.mkdir()
    profile.write_text(
        "MILOCO_HOME=/data/profile\nMILOCO_AGENT_PLATFORM=hermes\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MILOCO_RUNTIME_ENV", str(profile))

    assert installer._resolve_install_home("hermes") == Path("/data/profile")


def test_resolve_install_home_partial_profile_inherits_platform(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    default_pointer = tmp_path / ".config" / "miloco" / "default.env"
    default_pointer.parent.mkdir(parents=True)
    default_pointer.write_text(
        "MILOCO_HOME=/data/default\nMILOCO_AGENT_PLATFORM=hermes\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profiles" / "partial.env"
    profile.parent.mkdir()
    profile.write_text("MILOCO_HOME=/data/profile\n", encoding="utf-8")
    monkeypatch.setenv("MILOCO_RUNTIME_ENV", str(profile))

    assert installer._resolve_install_home("hermes") == Path("/data/profile")


def test_resolve_install_home_prefers_explicit_env(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        "MILOCO_HOME=/data/persisted\nMILOCO_AGENT_PLATFORM=hermes\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_HOME", "~/explicit-miloco")

    assert installer._resolve_install_home("hermes") == (
        tmp_path / "explicit-miloco"
    )


def test_warn_runtime_pointer_overwrite_is_localized(
    monkeypatch, installer, tmp_path, capsys
):
    _fake_home(monkeypatch, installer, tmp_path)
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        "MILOCO_HOME=/data/hermes\nMILOCO_AGENT_PLATFORM=hermes\n",
        encoding="utf-8",
    )
    i18n = installer.I18n("en", Path(installer.__file__).parent)

    installer._warn_runtime_pointer_overwrite("openclaw", i18n)

    warning = capsys.readouterr().err
    assert "currently targets hermes" in warning
    assert "MILOCO_RUNTIME_ENV" in warning


def test_warn_runtime_pointer_overwrite_skips_same_platform(
    monkeypatch, installer, tmp_path, capsys
):
    _fake_home(monkeypatch, installer, tmp_path)
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        "MILOCO_HOME=/data/hermes\nMILOCO_AGENT_PLATFORM=hermes\n",
        encoding="utf-8",
    )
    i18n = installer.I18n("en", Path(installer.__file__).parent)

    installer._warn_runtime_pointer_overwrite("hermes", i18n)

    assert capsys.readouterr().err == ""


def test_resolve_uninstall_home_from_pointer_without_runtime(
    monkeypatch, installer, tmp_path
):
    _fake_home(monkeypatch, installer, tmp_path)
    home = tmp_path / "miloco data"
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"MILOCO_HOME='{home}'\n", encoding="utf-8")

    assert installer._resolve_uninstall_home() == home


# ─── 安装器兜底写者：读不懂 config.json 必须拒绝写盘（issue #569） ──────────
#
# 背景：安装器优先调 `miloco-cli config set`，子进程失败（CLI 不存在 / 退出码
# 非 0）就落进手写的读-改-写兜底。这两处兜底原来把「读失败」当空基底，深合并
# 退化成整体覆盖——而 CLI 改为严格拒绝后，损坏文件场景反而必然落进兜底，
# 一次升级就把 server.token / model.omni.api_key 全清掉。


class _FakeI18n:
    def t(self, key: str, *args: str) -> str:
        return key


class _FakeUI:
    def __init__(self) -> None:
        self.i18n = _FakeI18n()
        self.warnings: list[str] = []

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _installer_for(installer, tmp_path):
    ui = _FakeUI()
    inst = installer.Installer.__new__(installer.Installer)
    inst.ui = ui
    inst.miloco_home = tmp_path
    return inst, ui


def test_fallback_write_config_corrupt_refuses_and_preserves_bytes(
    installer, tmp_path
):
    inst, ui = _installer_for(installer, tmp_path)
    config = tmp_path / "config.json"
    original = '{ "server": { "token": "tok-half-written"'
    config.write_text(original, encoding="utf-8")

    inst._fallback_write_config("/usr/bin/python3")

    assert config.read_text(encoding="utf-8") == original
    assert "install.config_unparsable" in ui.warnings


def test_fallback_write_config_non_object_refuses(installer, tmp_path):
    inst, ui = _installer_for(installer, tmp_path)
    config = tmp_path / "config.json"
    original = "[1, 2, 3]"
    config.write_text(original, encoding="utf-8")

    inst._fallback_write_config("/usr/bin/python3")

    assert config.read_text(encoding="utf-8") == original
    assert "install.config_not_object" in ui.warnings


def test_fallback_write_config_preserves_other_sections(installer, tmp_path):
    inst, _ = _installer_for(installer, tmp_path)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"server": {"token": "tok-keep"}, "model": {"omni": {"api_key": "sk"}}}),
        encoding="utf-8",
    )

    inst._fallback_write_config("/usr/bin/python3")

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["server"]["python_bin"] == "/usr/bin/python3"
    assert data["server"]["token"] == "tok-keep"
    assert data["model"]["omni"]["api_key"] == "sk"


def test_fallback_write_config_missing_file_initializes(installer, tmp_path):
    inst, _ = _installer_for(installer, tmp_path)
    config = tmp_path / "config.json"

    inst._fallback_write_config("/usr/bin/python3")

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["server"]["python_bin"] == "/usr/bin/python3"


def test_fallback_write_config_empty_file_initializes(installer, tmp_path):
    inst, _ = _installer_for(installer, tmp_path)
    config = tmp_path / "config.json"
    config.write_text("", encoding="utf-8")

    inst._fallback_write_config("/usr/bin/python3")

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["server"]["python_bin"] == "/usr/bin/python3"


def test_write_model_config_fallback_refuses_on_corrupt(
    installer, tmp_path, monkeypatch
):
    """CLI 严格拒绝 → 兜底触发 → 兜底也必须拒绝，且原字节保留。"""
    inst, ui = _installer_for(installer, tmp_path)
    config = tmp_path / "config.json"
    original = "NOT JSON {{{"
    config.write_text(original, encoding="utf-8")

    real_run = installer.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["miloco-cli", "config", "set"]:
            raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    inst._write_model_config("mimo-v2.6-flash", "https://api.xiaomimimo.com/v1", "sk-key")

    assert config.read_text(encoding="utf-8") == original
    assert "install.config_unparsable" in ui.warnings


def test_write_model_config_fallback_preserves_other_sections(
    installer, tmp_path, monkeypatch
):
    inst, _ = _installer_for(installer, tmp_path)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"server": {"token": "tok-keep"}}), encoding="utf-8"
    )

    real_run = installer.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["miloco-cli", "config", "set"]:
            raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    inst._write_model_config("mimo-v2.6-flash", "https://api.xiaomimimo.com/v1", "sk-key")

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["server"]["token"] == "tok-keep"
    assert data["model"]["omni"]["api_key"] == "sk-key"
    assert data["model"]["omni"]["base_url"] == "https://api.xiaomimimo.com/v1"
