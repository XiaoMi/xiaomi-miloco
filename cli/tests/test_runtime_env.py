"""运行环境指针文件的解析与优先级测试。"""

import os
from pathlib import Path

import miloco_cli.config as config


def _clear_runtime_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MILOCO_"):
            monkeypatch.delenv(key, raising=False)


def test_bootstrap_reads_quoted_default_pointer(monkeypatch, tmp_path):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        "MILOCO_HOME='" + str(tmp_path / "miloco data") + "'\n"
        "MILOCO_AGENT_PLATFORM=hermes\n",
        encoding="utf-8",
    )

    config.bootstrap_runtime_env()

    assert config.miloco_home() == tmp_path / "miloco data"
    assert os.environ["MILOCO_AGENT_PLATFORM"] == "hermes"


def test_bootstrap_prefers_explicit_home_over_pointer(monkeypatch, tmp_path):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("MILOCO_HOME", str(explicit))
    pointer = tmp_path / ".config" / "miloco" / "default.env"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        f"MILOCO_HOME={tmp_path / 'pointer'}\nMILOCO_AGENT_PLATFORM=hermes\n",
        encoding="utf-8",
    )

    config.bootstrap_runtime_env()

    assert config.miloco_home() == explicit
    assert "MILOCO_AGENT_PLATFORM" not in os.environ


def test_read_runtime_env_supports_selected_profile(monkeypatch, tmp_path):
    _clear_runtime_env(monkeypatch)
    profile = tmp_path / "profile.env"
    profile.write_text("MILOCO_HOME='/tmp/profile home'\nMILOCO_TOKEN='a%b'\n")
    monkeypatch.setenv("MILOCO_RUNTIME_ENV", str(profile))

    assert config.read_runtime_env() == {
        "MILOCO_HOME": "/tmp/profile home",
        "MILOCO_TOKEN": "a%b",
    }


def test_read_env_file_skips_invalid_shell_assignments(tmp_path):
    path = tmp_path / "runtime.env"
    path.write_text(
        "# comment\n"
        "GOOD=value\n"
        "WITH_SPACE='hello world'\n"
        "not valid=value\n"
        "BROKEN='unterminated\n"
    )

    assert config._read_env_file(path) == {
        "GOOD": "value",
        "WITH_SPACE": "hello world",
    }


def test_miloco_home_falls_back_to_openclaw(monkeypatch, tmp_path):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert config.miloco_home() == Path.home() / ".openclaw" / "miloco"
