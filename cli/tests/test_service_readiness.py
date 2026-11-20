"""service 就绪判定的回归测试：超时可配置、慢启动放行、失败信息可诊断。"""

import subprocess

import pytest

from miloco_cli.commands import service

_CFG = {"server": {"url": "http://127.0.0.1:1810"}}


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _connect_error(*_a, **_k):
    """模拟「还没 bind / 还没响应」的真预热形态（uvicorn 跑完 lifespan 才监听）。"""
    import httpx

    raise httpx.ConnectError("connection refused")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(service._STARTUP_TIMEOUT_ENV, raising=False)


def test_startup_timeout_default():
    assert service._startup_timeout_s() == service._STARTUP_TIMEOUT_DEFAULT_S


def test_startup_timeout_env_override(monkeypatch):
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "15")
    assert service._startup_timeout_s() == 15.0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "inf", "nan"])
def test_startup_timeout_env_invalid_falls_back(monkeypatch, raw):
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", raw)
    assert service._startup_timeout_s() == service._STARTUP_TIMEOUT_DEFAULT_S


def test_wait_for_health_returns_immediately_when_ready(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(200))
    service._wait_for_health(_CFG, pretty=False)  # 未抛 SystemExit 即通过


def test_wait_for_health_allows_slow_start_when_port_open(monkeypatch, capsys):
    """冷启动超过等待上限、端口已监听且监听者确为 miloco：放行（安装不应中断）。"""
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "1")
    monkeypatch.setattr("httpx.get", _connect_error)
    monkeypatch.setattr(service, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(service, "_find_pid_by_port", lambda *a, **k: 4321)
    monkeypatch.setattr(service, "_is_miloco_backend_proc", lambda pid: True)

    service._wait_for_health(_CFG, pretty=False)

    captured = capsys.readouterr()
    # 非 pretty 的 stdout 只留最终结果：人看的提示必须走 stderr，
    # 否则调用方的 json.loads(stdout) 会拿到两段 JSON。
    assert captured.out == ""
    assert "warming up" in captured.err


def test_wait_for_health_reports_unhealthy_when_health_returns_5xx(monkeypatch, capsys):
    """/health 回 5xx = 某个 node 已 FAILED/STALLED，不能报成「首次较慢」。

    放行策略不变（安装不因为一个临时坏掉的相机而中断），但归因必须写对，否则现场会
    往「再等等」的方向排查。
    """
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "1")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(503))
    monkeypatch.setattr(service, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(service, "_find_pid_by_port", lambda *a, **k: 4321)
    monkeypatch.setattr(service, "_is_miloco_backend_proc", lambda pid: True)

    service._wait_for_health(_CFG, pretty=False)

    err = capsys.readouterr().err
    assert "up but unhealthy" in err
    assert "FAILED/STALLED" in err
    assert "首次较慢" not in err


def test_wait_for_health_fails_when_fatal_despite_open_port(monkeypatch, capsys):
    """端口可连但进程管理器已判失败：不能放行，否则安装器会继续打真实 API。"""
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "1")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(503))
    monkeypatch.setattr(service, "_port_open", lambda *a, **k: True)

    with pytest.raises(SystemExit) as exc:
        service._wait_for_health(_CFG, pretty=False, fatal_check=lambda: True)

    assert exc.value.code == 1
    assert "failed to start" in capsys.readouterr().out


def test_wait_for_health_fails_when_port_held_by_non_miloco(monkeypatch, capsys):
    """端口被非 miloco 进程占着：不能当作「预热中」放行。"""
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "1")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(503))
    monkeypatch.setattr(service, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(service, "_find_pid_by_port", lambda *a, **k: 4321)
    monkeypatch.setattr(service, "_is_miloco_backend_proc", lambda pid: False)

    with pytest.raises(SystemExit) as exc:
        service._wait_for_health(_CFG, pretty=False)

    assert exc.value.code == 1
    assert "non-miloco process" in capsys.readouterr().out


def test_wait_for_health_passes_when_port_owner_unknown(monkeypatch, capsys):
    """机器没有 lsof/ss、拿不到 pid：按现状放行，不退化成误报失败。"""
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "1")
    monkeypatch.setattr("httpx.get", _connect_error)
    monkeypatch.setattr(service, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(service, "_find_pid_by_port", lambda *a, **k: None)

    service._wait_for_health(_CFG, pretty=False)

    assert "warming up" in capsys.readouterr().err


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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:1810", ("127.0.0.1", 1810)),
        ("http://localhost", ("localhost", 80)),
        ("https://localhost", ("localhost", 443)),
        ("not-a-url", ("127.0.0.1", 80)),
    ],
)
def test_port_open_parses_url(monkeypatch, url, expected):
    """_port_open 现在是全文件唯一的 TCP 探测实现，URL → (host, port) 的推导必须钉住。

    它是放行/判死的关键分支（_wait_for_health、service_start、_do_stop、launchd 各
    路径都走它）。如果缺端口默认、scheme 推导或 host 兜底被改，上层几个调用方会静默
    拿到不同结论，而现有用例都是把 _port_open 整体 monkeypatch 掉，抓不到。
    """
    seen: dict[str, tuple[str, int]] = {}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _connect(addr, timeout=None):
        seen["addr"] = addr
        return _Conn()

    monkeypatch.setattr(service.socket, "create_connection", _connect)

    assert service._port_open(url) is True
    assert seen["addr"] == expected


def test_port_open_false_on_oserror(monkeypatch):
    """连不上就是 False，不能把异常漏给调用方。"""

    def _boom(addr, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(service.socket, "create_connection", _boom)

    assert service._port_open("http://127.0.0.1:1810") is False


def test_wait_for_health_rechecks_fatal_after_window(monkeypatch, capsys):
    """FATAL 只在窗口末尾出现（循环内那次还是 False）时也必须判死。

    循环里的 ``fatal_check`` 只跑在每轮开头，出循环后的第二次复核是独立的一道；
    删掉它，「窗口之后才 FATAL、端口又被别的进程占着」会被当成预热放行。
    """
    monkeypatch.setenv("MILOCO_STARTUP_TIMEOUT", "0.01")
    monkeypatch.setattr("httpx.get", _connect_error)
    monkeypatch.setattr(service, "_port_open", lambda *a, **k: True)
    calls = {"n": 0}

    def _fatal():
        calls["n"] += 1
        return calls["n"] > 1  # 循环内那次 False，出循环后那次 True

    with pytest.raises(SystemExit) as exc:
        service._wait_for_health(_CFG, pretty=False, fatal_check=_fatal)

    assert exc.value.code == 1
    assert calls["n"] == 2  # 两次都调到了，确实是出循环后那次判死的
    assert "failed to start" in capsys.readouterr().out


def test_service_start_failure_keeps_stderr(monkeypatch, capsys):
    """supervisorctl start 超时：原因在 stderr，必须拼进错误，不能只拼 stdout。

    与 restart 分支同一口径。start 超时通常是 startsecs=5 下进程反复崩溃重试，
    只拼 stdout 会打出 `supervisorctl start failed: `（空串），完全看不出为什么。
    """
    monkeypatch.setattr(service, "_use_launchd", lambda: False)
    monkeypatch.setattr(service, "_supervisord_is_running", lambda: True)
    monkeypatch.setattr(service, "_get_backend_pid_from_supervisor", lambda: None)
    monkeypatch.setattr(service, "_is_port_in_use", lambda *_a, **_k: False)
    monkeypatch.setattr(service, "_server_cmd_or_exit", lambda _p: ["python", "-m", "x"])
    monkeypatch.setattr(service, "_generate_supervisor_conf", lambda _cmd: None)
    monkeypatch.setattr(
        "miloco_cli.config.load_config", lambda: {"server": {"url": _CFG["server"]["url"]}}
    )

    def _ctl(*args, **_kwargs):
        if args and args[0] == "start":
            return subprocess.CompletedProcess(list(args), 1, stdout="", stderr="timeout after 8s")
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

    monkeypatch.setattr(service, "_supervisorctl", _ctl)

    with pytest.raises(SystemExit) as exc:
        # service_start 是 click 命令，直接调用会走 click 的参数解析；测函数本体用 callback
        service.service_start.callback(foreground=False, pretty=False)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "supervisorctl start failed (rc=1)" in out
    assert "timeout after 8s" in out


def test_supervisorctl_timeout_keeps_reason(monkeypatch):
    """超时兜底必须把原因留在 stderr，否则上层拼装只能得到空串。"""

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="supervisorctl", timeout=8)

    monkeypatch.setattr(service.subprocess, "run", _boom)

    result = service._supervisorctl("status", timeout=8)

    assert result.returncode == 1
    assert "timeout after 8s" in result.stderr
