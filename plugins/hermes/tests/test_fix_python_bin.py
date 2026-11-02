"""fix_python_bin.py 的 fail-closed 写盘契约测试（miloco issue #569）。

install-hermes.sh step 1 的 python_bin auto-fix 曾把「读失败」当空基底整体写回，
且跑在 step 5 备份之前——损坏 config 跑一次安装，server.token / model.omni.*
全丢且无备份可恢复。逻辑提为 scripts/fix_python_bin.py 后在此固化契约。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERMES_DIR = Path(__file__).resolve().parent.parent
SCRIPT = HERMES_DIR / "scripts" / "fix_python_bin.py"


@pytest.fixture(scope="module")
def fixer():
    spec = importlib.util.spec_from_file_location("hermes_fix_python_bin", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_corrupt_config_refuses_and_preserves_bytes(fixer, tmp_path):
    cfg = tmp_path / "config.json"
    original = '{ "server": { "token": "tok-half-written"'
    cfg.write_text(original, encoding="utf-8")

    assert fixer.merge_python_bin(cfg, "/usr/bin/python3") is False
    assert cfg.read_text(encoding="utf-8") == original


def test_non_object_config_refuses(fixer, tmp_path):
    cfg = tmp_path / "config.json"
    original = "[1, 2, 3]"
    cfg.write_text(original, encoding="utf-8")

    assert fixer.merge_python_bin(cfg, "/usr/bin/python3") is False
    assert cfg.read_text(encoding="utf-8") == original


def test_valid_config_preserves_other_sections(fixer, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"server": {"token": "tok-keep"}, "model": {"omni": {"api_key": "sk"}}}),
        encoding="utf-8",
    )

    assert fixer.merge_python_bin(cfg, "/usr/bin/python3") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["server"]["python_bin"] == "/usr/bin/python3"
    assert data["server"]["token"] == "tok-keep"
    assert data["model"]["omni"]["api_key"] == "sk"


def test_missing_file_initializes(fixer, tmp_path):
    cfg = tmp_path / "config.json"

    assert fixer.merge_python_bin(cfg, "/usr/bin/python3") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data == {"server": {"python_bin": "/usr/bin/python3"}}


def test_empty_file_initializes(fixer, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("", encoding="utf-8")

    assert fixer.merge_python_bin(cfg, "/usr/bin/python3") is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data == {"server": {"python_bin": "/usr/bin/python3"}}


def test_cli_exit_code_reflects_refusal(fixer, tmp_path):
    """shell 侧靠退出码分流：拒绝 = 1（warn 分支），落盘 = 0。"""
    import subprocess

    cfg = tmp_path / "config.json"
    cfg.write_text("NOT JSON {{{", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path), "/usr/bin/python3"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "拒绝写入" in result.stderr
    assert cfg.read_text(encoding="utf-8") == "NOT JSON {{{"


def test_cli_exit_code_zero_on_success(fixer, tmp_path):
    import subprocess

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path), "/usr/bin/python3"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["server"]["python_bin"] == "/usr/bin/python3"


def test_install_hermes_sh_uses_helper_not_heredoc():
    """install-hermes.sh 必须走 fix_python_bin.py，不能再内联 fail-open heredoc。"""
    script = (HERMES_DIR / "install-hermes.sh").read_text(encoding="utf-8")
    assert 'scripts/fix_python_bin.py' in script
    # 旧的 fail-open 写回形态：读失败回退空 dict 后 write_text 直写真身
    assert 'p.write_text(json.dumps(cfg' not in script
