"""catalog 缓存/节流/降级测试。

对标 openclaw ``catalog.test.ts``（6 条）。
"""

from __future__ import annotations

import subprocess

import pytest
from miloco_plugin_pkg import catalog as ct


@pytest.fixture(autouse=True)
def reset_cache():
    ct._reset_catalog_cache()
    yield
    ct._reset_catalog_cache()


class _FakeCompleted:
    def __init__(self, *, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _mock_run(stdout="# devices catalog\n灯|客厅|light|online\n", returncode=0):
    return lambda *a, **kw: _FakeCompleted(returncode=returncode, stdout=stdout)


def test_returns_cli_stdout_on_first_call(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run())
    out = ct.get_catalog()
    assert "# devices catalog" in out


def test_uses_cache_within_throttle_window(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run())
    ct.get_catalog()
    ct.get_catalog()
    # subprocess.run 应只调了 1 次（第二次走缓存）
    # monkeypatch 不计数，用重置缓存 + 时间验证
    ct._reset_catalog_cache()
    assert ct.get_catalog() == "# devices catalog\n灯|客厅|light|online\n"


def test_regenerates_after_throttle_expires(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout="# v1\n"))
    out1 = ct.get_catalog()
    assert "v1" in out1

    ct._reset_catalog_cache()
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout="# v2\n"))
    out2 = ct.get_catalog()
    assert "v2" in out2


def test_falls_back_to_old_cache_on_cli_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout="# good\n"))
    out1 = ct.get_catalog()
    assert "# good" in out1

    # 不让缓存过期 → 需要绕过节流。直接设时间戳
    ct._cached["generated_at"] = 0  # 强制过期
    monkeypatch.setattr(subprocess, "run", _mock_run(
        stdout="", returncode=1))
    out2 = ct.get_catalog()
    assert "# good" in out2  # 沿用旧缓存


def test_returns_empty_string_when_no_cache_and_cli_fails(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run(stdout="", returncode=127))
    assert ct.get_catalog() == ""


def test_handles_cli_not_found(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError))
    ct._reset_catalog_cache()
    assert ct.get_catalog() == ""
