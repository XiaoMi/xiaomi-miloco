"""register() 拉起 backend 的回归测试：探活早退、锁串行化、预算推导。

这几个函数都是「同时被 dashboard 与 gateway 两个进程触发」的并发路径，只能靠单测
把不变量钉住：已经 200 就不拉；拿到锁前后都复核；锁拿不到也不能卡满线程。
"""

from __future__ import annotations

import os

import pytest
import miloco_plugin_pkg as plugin


class _Resp:
    status_code = 200


def test_backend_health_ok_false_on_error(monkeypatch, tmp_path):
    """探活失败（连不上 / httpx 缺失 / 异常）一律当「不确定」，返回 False。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    def _boom(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr("httpx.get", _boom)

    assert plugin._backend_health_ok() is False


def test_backend_health_ok_true_on_200(monkeypatch, tmp_path):
    """200 才算已在跑，且探的是 config 里的 server.url + /health。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    calls: list[str] = []

    def _get(url, **_kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr("httpx.get", _get)

    assert plugin._backend_health_ok() is True
    assert calls[0].endswith("/health")


def test_acquire_start_lock_times_out(monkeypatch, tmp_path):
    """锁被别人占着时，等到上限就返回 None，交回调用方按原行为直跑。"""
    fcntl = pytest.importorskip("fcntl")  # Windows 无 fcntl，插件本身也走 no-lock 分支
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    holder = os.open(tmp_path / ".backend-start.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        assert plugin._acquire_start_lock(timeout_s=0.2) is None
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_acquire_and_release_start_lock(monkeypatch, tmp_path):
    """拿到锁 → 释放 → 能再次拿到（不把锁泄漏成永久占用）。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    fd = plugin._acquire_start_lock(timeout_s=1)
    assert fd is not None
    plugin._release_start_lock(fd)

    fd2 = plugin._acquire_start_lock(timeout_s=1)
    assert fd2 is not None
    plugin._release_start_lock(fd2)


def test_release_start_lock_none_is_noop():
    plugin._release_start_lock(None)  # 不抛异常即通过


def test_startup_timeout_base_respects_env(monkeypatch):
    """锁等待窗口的基线跟随 MILOCO_STARTUP_TIMEOUT，调大后端等待时不会脱钩。"""
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "180")
    assert plugin._startup_timeout_base_s() == 180.0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "inf"])
def test_startup_timeout_base_falls_back(monkeypatch, raw):
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", raw)
    assert plugin._startup_timeout_base_s() == plugin._DEFAULT_STARTUP_TIMEOUT_S
