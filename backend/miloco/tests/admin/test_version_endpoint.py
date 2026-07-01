"""GET /api/admin/version 的端到端测试。

覆盖:
- pkg version 存在 (importlib.metadata) → 返回
- git 可用 → 返回 commit/branch/dirty/commit_time
- git 不可用 → git=null, version 仍返回
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin.router import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    from miloco.config.settings import reset_settings
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.delenv("MILOCO_DIRECTORIES__STORAGE", raising=False)
    reset_settings()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    yield TestClient(app)
    reset_settings()


def test_version_returns_pkg_version(client):
    """基础调用: version 字段应非空。"""
    resp = client.get("/api/admin/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert isinstance(body["data"]["version"], str)
    assert body["data"]["version"]  # 非空


def test_version_git_null_when_not_git_checkout(client):
    """git 命令失败 + version 无 hatch-vcs marker (纯 tag / fallback) → git=None。"""
    with (
        patch("miloco.admin.router._run_git", return_value=None),
        patch("miloco.admin.router._pkg_version", return_value="0.1.0"),
    ):
        resp = client.get("/api/admin/version")
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["git"] is None
    assert body["data"]["version"] == "0.1.0"


def test_version_git_present_all_fields(client):
    """git 全部字段就位 → 返回结构完整。"""
    def fake_git(args):
        return {
            ("rev-parse", "HEAD"): "4a2b3c1d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("status", "--porcelain"): "",  # 空 = clean
            ("log", "-1", "--format=%cI", "HEAD"): "2026-07-01T10:16:07+08:00",
        }.get(tuple(args))

    with patch("miloco.admin.router._run_git", side_effect=fake_git):
        resp = client.get("/api/admin/version")
    body = resp.json()
    assert body["code"] == 0
    git = body["data"]["git"]
    assert git["commit"] == "4a2b3c1d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
    assert git["commit_short"] == "4a2b3c1"
    assert git["branch"] == "main"
    assert git["dirty"] is False
    assert git["commit_time"] == "2026-07-01T10:16:07+08:00"


def test_version_git_dirty_detected(client):
    """git status --porcelain 有输出 → dirty=True。"""
    def fake_git(args):
        return {
            ("rev-parse", "HEAD"): "a" * 40,
            ("rev-parse", "--abbrev-ref", "HEAD"): "feat/x",
            ("status", "--porcelain"): " M some_file.py\n?? new_file",
            ("log", "-1", "--format=%cI", "HEAD"): "2026-07-01T00:00:00+00:00",
        }.get(tuple(args))

    with patch("miloco.admin.router._run_git", side_effect=fake_git):
        resp = client.get("/api/admin/version")
    body = resp.json()
    assert body["data"]["git"]["dirty"] is True


def test_version_git_detached_head_branch_none(client):
    """detached HEAD 时 abbrev-ref 返 'HEAD' → branch 映射为 None。"""
    def fake_git(args):
        return {
            ("rev-parse", "HEAD"): "b" * 40,
            ("rev-parse", "--abbrev-ref", "HEAD"): "HEAD",
            ("status", "--porcelain"): "",
            ("log", "-1", "--format=%cI", "HEAD"): "2026-01-01T00:00:00+00:00",
        }.get(tuple(args))

    with patch("miloco.admin.router._run_git", side_effect=fake_git):
        resp = client.get("/api/admin/version")
    body = resp.json()
    assert body["data"]["git"]["branch"] is None


def test_run_git_timeout_returns_none():
    """_run_git 遇到 subprocess.TimeoutExpired 时返 None (不抛)。"""
    import subprocess

    from miloco.admin.router import _run_git
    with patch("miloco.admin.router.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="git", timeout=2)):
        assert _run_git(["rev-parse", "HEAD"]) is None


def test_run_git_not_installed_returns_none():
    """git 二进制缺失 (FileNotFoundError) 时返 None。"""
    from miloco.admin.router import _run_git
    with patch("miloco.admin.router.subprocess.run",
               side_effect=FileNotFoundError("git not found")):
        assert _run_git(["rev-parse", "HEAD"]) is None


# ─── _parse_version_git: wheel 部署 fallback ──────────────────────────────────


class TestParseVersionGit:
    def test_clean_release_with_local(self):
        """hatch-vcs 派生格式 `0.1.0.dev5+g4a2b3c1`, 无 .d 标记 → dirty=False。"""
        from miloco.admin.router import _parse_version_git
        r = _parse_version_git("0.1.0.dev5+g4a2b3c1")
        assert r == {
            "commit": None, "commit_short": "4a2b3c1",
            "branch": None, "dirty": False, "commit_time": None,
        }

    def test_dirty_build_marker(self):
        """`.d20260701` 存在 → dirty=True。"""
        from miloco.admin.router import _parse_version_git
        r = _parse_version_git("0.1.0.dev5+g4a2b3c1.d20260701")
        assert r["commit_short"] == "4a2b3c1"
        assert r["dirty"] is True

    def test_full_40_char_sha_preserved(self):
        from miloco.admin.router import _parse_version_git
        full = "a" * 40
        r = _parse_version_git(f"0.1.0+g{full}")
        assert r["commit"] == full
        assert r["commit_short"] == "a" * 7

    def test_plain_tag_no_local_returns_none(self):
        """打 tag 的 release 版本 `0.1.0` 无 local 段 → 无 git 信息可提。"""
        from miloco.admin.router import _parse_version_git
        assert _parse_version_git("0.1.0") is None

    def test_fallback_version_returns_none(self):
        """hatch-vcs fallback `0.0.0` (无 .git 且构建时未注入) → None。"""
        from miloco.admin.router import _parse_version_git
        assert _parse_version_git("0.0.0") is None

    def test_unknown_returns_none(self):
        from miloco.admin.router import _parse_version_git
        assert _parse_version_git("unknown") is None


def test_git_info_falls_back_to_version_when_subprocess_fails():
    """subprocess 返 None (wheel 部署无 .git) + version 含 hatch-vcs marker
    → _git_info 返 parsed dict, 而非 None。"""
    from miloco.admin.router import _git_info
    with patch("miloco.admin.router._run_git", return_value=None):
        r = _git_info("0.1.0.dev3+g1234567.d20260101")
    assert r is not None
    assert r["commit_short"] == "1234567"
    assert r["dirty"] is True


def test_git_info_returns_none_when_no_source_and_no_marker():
    """subprocess 失败 + version 无 marker → None (纯 wheel + fallback version)。"""
    from miloco.admin.router import _git_info
    with patch("miloco.admin.router._run_git", return_value=None):
        assert _git_info("0.0.0") is None


def test_endpoint_wheel_scenario_returns_git_from_version(client):
    """端到端: subprocess 全 None + version 有 marker → data.git 非空。"""
    def fake_pkg_version():
        return "0.1.0.dev5+gdeadbee.d20260701"

    with (
        patch("miloco.admin.router._run_git", return_value=None),
        patch("miloco.admin.router._pkg_version", side_effect=fake_pkg_version),
    ):
        resp = client.get("/api/admin/version")
    body = resp.json()
    assert body["data"]["version"] == "0.1.0.dev5+gdeadbee.d20260701"
    git = body["data"]["git"]
    assert git is not None
    assert git["commit_short"] == "deadbee"
    assert git["dirty"] is True
    assert git["branch"] is None
    assert git["commit_time"] is None
