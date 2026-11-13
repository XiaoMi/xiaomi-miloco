"""service 就绪判定的回归测试：超时可配置、慢启动放行、失败信息可诊断。"""

import subprocess

import pytest

from miloco_cli.commands import service

_CFG = {"server": {"url": "http://127.0.0.1:1810"}}


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(service._STARTUP_TIMEOUT_ENV, raising=False)


def test_startup_timeout_default():
    assert service._startup_timeout_s() == service._STARTUP_TIMEOUT_DEFAULT_S


def test_startup_timeout_env_override(monkeypatch):
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "15")
    assert service._startup_timeout_s() == 15.0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
def test_startup_timeout_env_invalid_falls_back(monkeypatch, raw):
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", raw)
    assert service._startup_timeout_s() == service._STARTUP_TIMEOUT_DEFAULT_S


def test_wait_for_health_returns_immediately_when_ready(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(200))
    service._wait_for_health(_CFG, pretty=False)  # 未抛 SystemExit 即通过


def test_wait_for_health_allows_slow_start_when_port_open(monkeypatch, capsys):
    """冷启动超过等待上限但端口已监听：放行（安装不应因此中断）。"""
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "1")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(503))
    monkeypatch.setattr(service, "_port_open", lambda *a, **k: True)

    service._wait_for_health(_CFG, pretty=False)

    out = capsys.readouterr().out
    assert "warming up" in out


def test_wait_for_health_fails_when_port_closed(monkeypatch, capsys):
    """进程确实没起来：仍返回非零，并带上最后一次探测与 supervisor 状态。"""
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "1")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(503))
    monkeypatch.setattr(service, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(
        service,
        "_supervisorctl",
        lambda *a, **k: subprocess.CompletedProcess(
            list(a), 0, stdout="miloco-backend  FATAL  pid 0", stderr=""
        ),
    )

    with pytest.raises(SystemExit) as exc:
        service._wait_for_health(_CFG, pretty=False)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "did not become ready" in out
    assert "HTTP 503" in out


def test_supervisorctl_timeout_keeps_reason(monkeypatch):
    """超时兜底必须把原因留在 stderr，否则上层拼装只能得到空串。"""

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="supervisorctl", timeout=8)

    monkeypatch.setattr(service.subprocess, "run", _boom)

    result = service._supervisorctl("status", timeout=8)

    assert result.returncode == 1
    assert "timeout after 8s" in result.stderr
