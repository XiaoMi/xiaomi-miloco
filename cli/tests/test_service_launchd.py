"""macOS launchd 分支：plist 生成 + 平台选路 + launchctl 生命周期。"""

from __future__ import annotations

import os
import plistlib
import subprocess
import time

import pytest

from miloco_cli.commands import service


def _cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["launchctl"], returncode=rc, stdout=out, stderr=err
    )


@pytest.fixture(autouse=True)
def _redirect_plist(monkeypatch, tmp_path):
    """把 LaunchAgent plist 重定向到 tmp，避免污染真实 ~/Library/LaunchAgents。"""
    monkeypatch.setattr(
        service, "_launchagent_plist", lambda: tmp_path / "com.xiaomi.miloco.backend.plist"
    )


def test_use_launchd_matches_platform(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    assert service._use_launchd() is True
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert service._use_launchd() is False


def test_generate_launchagent_plist_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_resolve_timezone", lambda: None)

    cmd = ["/opt/py/bin/python", "-m", "miloco.main"]
    service._generate_launchagent_plist(cmd)

    plist_path = service._launchagent_plist()
    assert plist_path.exists()
    data = plistlib.loads(plist_path.read_bytes())

    assert data["Label"] == "com.xiaomi.miloco.backend"
    # 启动器在前，backend 命令在后 —— python 作为签名启动器的子进程运行
    launcher = str(tmp_path / "miloco.app" / "Contents" / "MacOS" / "miloco")
    assert data["ProgramArguments"] == [launcher, *cmd]
    assert data["RunAtLoad"] is True
    # 对齐 supervisord autorestart=true:非 0 退出码或信号崩溃都重拉,clean exit 不拉
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["WorkingDirectory"] == str(tmp_path)
    # launchd 不做轮转,故它只兜底 backend dup2 之前的输出,写独立的小文件;
    # miloco-backend.log 由 backend 自己接管并轮转,不能再交给 StandardOutPath
    # (否则无人轮转,实测涨到 874MB)。
    assert data["StandardOutPath"] == str(tmp_path / "log" / "launchd-stdio.log")
    assert data["StandardErrorPath"] == str(tmp_path / "log" / "launchd-stdio.log")

    env = data["EnvironmentVariables"]
    # 关键契约:MILOCO_SUPERVISED 的语义是"外部管理者会收集并轮转我的 stdout"。
    # launchd 只重定向、不轮转,声称它会让 backend 的 bootstrap 跳过自管日志分支,
    # 于是两边都放手 → 日志无限增长。这条断言防止它被手滑加回来。
    assert "MILOCO_SUPERVISED" not in env
    assert env["MILOCO_HOME"] == str(tmp_path)
    assert env["HOME"]  # 必须显式给（launchd 不保证）
    assert "/opt/homebrew/bin" in env["PATH"]  # 后端 shell 调 ffmpeg 需要
    assert "/.local/bin" in env["PATH"]  # uv 工具所在，不能丢失继承
    # 未配置时区 → 不注入 TZ
    assert "TZ" not in env and "MILOCO_TIMEZONE" not in env


def test_generate_launchagent_plist_timezone(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_resolve_timezone", lambda: "Asia/Shanghai")

    service._generate_launchagent_plist(["/opt/py/bin/python", "-m", "miloco.main"])
    env = plistlib.loads(service._launchagent_plist().read_bytes())[
        "EnvironmentVariables"
    ]
    assert env["TZ"] == "Asia/Shanghai"
    assert env["MILOCO_TIMEZONE"] == "Asia/Shanghai"


def test_generate_launchagent_plist_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_resolve_timezone", lambda: None)

    cmd = ["/opt/py/bin/python", "-m", "miloco.main"]
    service._generate_launchagent_plist(cmd)
    plist_path = service._launchagent_plist()
    mtime1 = plist_path.stat().st_mtime_ns
    service._generate_launchagent_plist(cmd)  # 内容不变 → 不重写
    assert plist_path.stat().st_mtime_ns == mtime1


# ─── launchctl 生命周期 ──────────────────────────────────────────────────────


def test_launchd_backend_pid_parses(monkeypatch):
    monkeypatch.setattr(
        service, "_launchctl", lambda *a: _cp(0, "\tstate = running\n\tpid = 4242\n")
    )
    assert service._launchd_backend_pid() == 4242
    # 未加载(print 非零)→ None
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(1))
    assert service._launchd_backend_pid() is None


def test_launchd_is_loaded(monkeypatch):
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(0))
    assert service._launchd_is_loaded() is True
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(1))
    assert service._launchd_is_loaded() is False


def test_launchd_reload_retries_on_eio(monkeypatch):
    """bootout→bootstrap 撞 EIO 竞态 → 重试后成功。"""
    monkeypatch.setattr(service.time, "sleep", lambda *_: None)
    calls = {"bootstrap": 0}

    def fake(*args):
        cmd = args[0]
        if cmd == "print":  # 一直未加载 → wait 循环秒退、事后判定也 False
            return _cp(1)
        if cmd == "bootout":
            return _cp(0)
        if cmd == "bootstrap":
            calls["bootstrap"] += 1
            if calls["bootstrap"] == 1:
                return _cp(5, "", "Bootstrap failed: 5: Input/output error")
            return _cp(0)
        return _cp(0)

    monkeypatch.setattr(service, "_launchctl", fake)
    ok, err = service._launchd_reload()
    assert ok is True and err == ""
    assert calls["bootstrap"] == 2  # 首次 EIO → 重试第二次成功


def test_launchd_reload_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(service.time, "sleep", lambda *_: None)

    def fake(*args):
        if args[0] == "print":
            return _cp(1)
        if args[0] == "bootout":
            return _cp(0)
        return _cp(5, "", "persistent EIO")  # bootstrap 永远失败

    monkeypatch.setattr(service, "_launchctl", fake)
    ok, err = service._launchd_reload()
    assert ok is False and "persistent EIO" in err


def test_launchd_crashloop_check(monkeypatch):
    """窗口内 backend 换 ≥3 个 pid → 判 crashloop 并 bootout。单次重启(2 pid)容忍。"""
    pids = iter([100, 100, 200, 300, 300])
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: next(pids, None))
    booted = {"n": 0}
    monkeypatch.setattr(
        service,
        "_launchctl",
        lambda *a: (booted.__setitem__("n", booted["n"] + 1), _cp(0))[1],
    )
    check = service._launchd_crashloop_check()
    assert check() is False  # {100}
    assert check() is False  # {100}
    assert check() is False  # {100,200} = 2，容忍单次重启
    assert check() is True  # {100,200,300} = 3 → crashloop
    assert booted["n"] >= 1  # 已 bootout 停掉


def test_reap_legacy_supervisord(monkeypatch, tmp_path):
    """darwin 升级迁移:reap 残留 supervisord + 清老运行时文件。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_find_supervisord_pids", lambda: [111, 222])
    terminated: list[int] = []
    monkeypatch.setattr(service, "_terminate", lambda pid, *a, **k: terminated.append(pid))
    (tmp_path / "supervisord.conf").write_text("x")
    (tmp_path / "supervisord.pid").write_text("1")
    (tmp_path / "supervisor.sock").write_text("")

    reaped = service._reap_legacy_supervisord()
    assert reaped == [111, 222]
    assert terminated == [111, 222]
    assert not (tmp_path / "supervisord.conf").exists()
    assert not (tmp_path / "supervisord.pid").exists()
    assert not (tmp_path / "supervisor.sock").exists()


def test_reap_legacy_supervisord_noop(monkeypatch, tmp_path):
    """无残留时 no-op:不 terminate、不误删文件。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_find_supervisord_pids", lambda: [])
    monkeypatch.setattr(
        service, "_terminate", lambda *a, **k: pytest.fail("不应 terminate")
    )
    (tmp_path / "supervisord.conf").write_text("keep")
    assert service._reap_legacy_supervisord() == []
    assert (tmp_path / "supervisord.conf").exists()  # 未误删


def test_tail_lines(tmp_path):
    f = tmp_path / "log"
    f.write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
    assert service._tail_lines(f, 3) == ["line4", "line5", "line6"]
    assert service._tail_lines(f, 10) == [
        "line1", "line2", "line3", "line4", "line5", "line6"
    ]
    assert service._tail_lines(tmp_path / "nope", 3) == []


def test_extract_log_ts():
    ts = service._extract_log_ts(
        "2026-07-30 12:34:56 - miot.central_hub - ERROR - ..."
    )
    # time.mktime 按本地时区解析;只要 > 0 即可（时区不等同,不判绝对值）
    assert ts > 0
    assert service._extract_log_ts("not a log line") == 0.0


def test_is_miloco_backend_proc(monkeypatch):
    """端口清理只针对 miloco backend，不误杀用户进程。"""
    def _ps(args, **k):
        # args = ["ps", "-ww", "-p", pid, "-o", "command="]
        pid = args[args.index("-p") + 1]
        if pid == "1111":
            return subprocess.CompletedProcess(args, 0, stdout="/usr/bin/python -m miloco.main\n")
        return subprocess.CompletedProcess(args, 0, stdout="/usr/sbin/nginx -g daemon off;\n")

    monkeypatch.setattr(
        service.subprocess, "run", _ps
    )
    assert service._is_miloco_backend_proc(1111) is True
    assert service._is_miloco_backend_proc(2222) is False


def test_launchd_start_kills_only_miloco_port_pid(monkeypatch):
    """启动前端口清理：非 miloco 进程不杀，直接报 port already in use。"""
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: None)
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_find_pid_by_port", lambda url: 9999)  # nginx 之类的用户进程
    monkeypatch.setattr(service, "_is_miloco_backend_proc", lambda pid: False)
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: True)
    monkeypatch.setattr(service, "_launcher_ready_or_exit", lambda pretty: None)
    monkeypatch.setattr(service, "_server_cmd_or_exit", lambda pretty: ["python"])
    killed = []
    monkeypatch.setattr(service, "_terminate", lambda pid, *a, **k: killed.append(pid))
    captured = {}
    monkeypatch.setattr(service, "print_result", lambda p, pretty: captured.update(p))
    cfg = {"server": {"url": "http://127.0.0.1:1810"}}
    with pytest.raises(SystemExit):
        service._launchd_start(cfg, pretty=False)
    assert killed == []  # 非 miloco 进程没有被杀
    assert "port already in use" in captured.get("message", "")


def test_launchd_kill_non_miloco_port_holder_reports(monkeypatch):
    """kill 时端口被非 miloco 进程占用 → 不误杀 + 明确报错。"""
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(0))
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_find_pid_by_port", lambda url: 4242)  # 用户 http.server
    monkeypatch.setattr(service, "_is_miloco_backend_proc", lambda pid: False)
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: True)
    monkeypatch.setattr(service, "_has_port_lookup_tool", lambda: True)
    killed = []
    monkeypatch.setattr(service, "_terminate", lambda pid, *a, **k: killed.append(pid))
    captured = {}
    monkeypatch.setattr(service, "print_result", lambda p, pretty: captured.update(p))
    cfg = {"server": {"url": "http://127.0.0.1:1810"}}
    with pytest.raises(SystemExit):
        service._launchd_kill(cfg, pretty=False)
    assert killed == []  # 未误杀
    assert "非 miloco" in captured.get("error", "")


def test_launchd_start_already_running(monkeypatch):
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: 999)
    captured: dict = {}
    monkeypatch.setattr(
        service, "print_result", lambda payload, pretty: captured.update(payload)
    )
    cfg = {"server": {"url": "http://127.0.0.1:1810"}}
    with pytest.raises(SystemExit):
        service._launchd_start(cfg, pretty=False)
    assert "already running" in captured.get("message", "")


def test_launchd_start_missing_launcher_does_not_touch_running_backend(monkeypatch):
    """签名启动器缺失时必须先于任何破坏性拆卸退出:_launcher_ready_or_exit /
    _server_cmd_or_exit 任一失败都会 sys.exit(1),此时不能已经杀过旧
    supervisord/端口占用者 —— 否则用户从「老版本还在跑」变成「服务被停且起不来」。
    """
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: None)
    monkeypatch.setattr(
        service, "_launcher_ready_or_exit", lambda pretty: (_ for _ in ()).throw(SystemExit(1))
    )
    reaped: list = []
    monkeypatch.setattr(
        service, "_reap_legacy_supervisord", lambda: reaped.append(1) or []
    )
    cleaned: list = []
    monkeypatch.setattr(
        service, "_cleanup_stale_port_holder", lambda *a, **k: cleaned.append(1)
    )

    with pytest.raises(SystemExit):
        service._launchd_start({"server": {"url": "http://127.0.0.1:1810"}}, pretty=False)

    assert reaped == []  # 校验失败前不许碰残留 supervisord
    assert cleaned == []  # 也不许碰占端口的旧后端


# ─── 环境变量透传（launchd 不继承 shell 环境） ────────────────────────────────


def test_passthrough_env_picks_miloco_and_network_vars(monkeypatch):
    """MILOCO_* 与代理/CA 变量要捞进来——launchd 不继承 shell,不透传就静默失效。"""
    monkeypatch.setenv("MILOCO_MODEL__OMNI__API_KEY", "sk-xxx")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("no_proxy", "localhost")  # 小写变体 httpx 认
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ca.pem")
    monkeypatch.setenv("UNRELATED_VAR", "nope")

    env = service._passthrough_env()

    assert env["MILOCO_MODEL__OMNI__API_KEY"] == "sk-xxx"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert env["no_proxy"] == "localhost"
    assert env["SSL_CERT_FILE"] == "/etc/ca.pem"
    assert "UNRELATED_VAR" not in env  # 白名单之外不带


def test_passthrough_env_denies_supervised_flag(monkeypatch):
    """MILOCO_SUPERVISED 必须挡死:透传进去会让 backend 跳过自管日志 → 退回无轮转。"""
    monkeypatch.setenv("MILOCO_SUPERVISED", "1")
    assert "MILOCO_SUPERVISED" not in service._passthrough_env()


def test_passthrough_env_denies_miloco_home(monkeypatch):
    """MILOCO_HOME 由 miloco_home() 算好显式写入,不从环境二次取。"""
    monkeypatch.setenv("MILOCO_HOME", "/tmp/whatever")
    assert "MILOCO_HOME" not in service._passthrough_env()


def test_plist_passthrough_does_not_override_explicit(monkeypatch, tmp_path):
    """显式项优先:环境里的同名值不能盖掉算好的 PATH / HOME / MILOCO_HOME。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_EXTRA_FLAG", "kept")
    monkeypatch.setattr(service, "_resolve_timezone", lambda: None)

    service._generate_launchagent_plist(["/opt/py/bin/python", "-m", "miloco.main"])
    env = plistlib.loads(service._launchagent_plist().read_bytes())[
        "EnvironmentVariables"
    ]

    assert env["MILOCO_EXTRA_FLAG"] == "kept"  # 透传生效
    assert env["MILOCO_HOME"] == str(tmp_path)  # 显式项仍是算出来的那个
    assert "/opt/homebrew/bin" in env["PATH"]  # PATH 没被环境里的覆盖


def test_is_miloco_backend_proc_uses_ww(monkeypatch):
    """必须传 -ww：macOS 的 BSD ps 在非 tty 输出下把 command 截断到 80 列，
    而默认安装路径（uv tool 的 venv python）+ ` -m miloco.main` 就在 80 列附近 ——
    用户名稍长就把判据词截掉，端口清理静默失效。"""
    seen: dict = {}

    def _ps(args, **k):
        seen["args"] = list(args)
        return subprocess.CompletedProcess(args, 0, stdout="python -m miloco.main\n")

    monkeypatch.setattr(service.subprocess, "run", _ps)
    assert service._is_miloco_backend_proc(1111) is True
    assert "-ww" in seen["args"]


def test_find_supervisord_pids_uses_ww(monkeypatch, tmp_path):
    """同理：要匹配完整的 `supervisord -c <绝对路径 conf>`，截断会让迁移 reap 失效。"""
    monkeypatch.setattr(service, "miloco_home", lambda: tmp_path)
    seen: dict = {}

    def _ps(args, **k):
        seen["args"] = list(args)
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(service.subprocess, "run", _ps)
    service._find_supervisord_pids()
    assert "-ww" in seen["args"]


def test_launchd_start_kills_miloco_port_holder_and_continues(monkeypatch):
    """门禁的**正向**分支：确认是 miloco 残留就杀掉并继续启动。

    只有负向用例（非 miloco 不杀）时，把条件写反成 `not _is_miloco_backend_proc(...)`
    现有测试照样全绿 —— 那正好是「误杀用户进程」的方向。
    """
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: None)
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_find_pid_by_port", lambda url: 7777)
    monkeypatch.setattr(service, "_is_miloco_backend_proc", lambda pid: True)
    killed: list = []
    # 杀掉之后端口就释放了
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: not killed)
    monkeypatch.setattr(service, "_terminate", lambda pid, *a, **k: killed.append(pid))
    monkeypatch.setattr(service, "_launcher_ready_or_exit", lambda pretty: None)
    monkeypatch.setattr(service, "_server_cmd_or_exit", lambda pretty: ["python"])
    monkeypatch.setattr(service, "_generate_launchagent_plist", lambda cmd: None)
    monkeypatch.setattr(service, "_launchd_reload", lambda: (True, ""))
    monkeypatch.setattr(service, "_wait_for_health", lambda *a, **k: None)
    monkeypatch.setattr(service, "_launchd_crashloop_check", lambda: None)
    monkeypatch.setattr(service, "_check_lnp_blocked", lambda pretty: None)
    captured: dict = {}
    monkeypatch.setattr(service, "print_result", lambda p, pretty: captured.update(p))

    service._launchd_start({"server": {"url": "http://127.0.0.1:1810"}}, pretty=False)

    assert killed == [7777]
    assert captured.get("code") == 0 and captured.get("message") == "started"


def test_launchd_restart_cleans_stale_port_holder(monkeypatch):
    """restart 也要走同一段端口清理：旧 python 被 SIGKILL 成孤儿占端口时，
    只有 start 能自愈而 restart 直接 crashloop 报错，恢复路径不该不对称。"""
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: None)
    monkeypatch.setattr(service, "_find_pid_by_port", lambda url: 7777)
    monkeypatch.setattr(service, "_is_miloco_backend_proc", lambda pid: True)
    killed: list = []
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: not killed)
    monkeypatch.setattr(service, "_terminate", lambda pid, *a, **k: killed.append(pid))
    monkeypatch.setattr(service, "_launcher_ready_or_exit", lambda pretty: None)
    monkeypatch.setattr(service, "_server_cmd_or_exit", lambda pretty: ["python"])
    monkeypatch.setattr(service, "_generate_launchagent_plist", lambda cmd: None)
    monkeypatch.setattr(service, "_launchd_reload", lambda: (True, ""))
    monkeypatch.setattr(service, "_wait_for_health", lambda *a, **k: None)
    monkeypatch.setattr(service, "_launchd_crashloop_check", lambda: None)
    monkeypatch.setattr(service, "_check_lnp_blocked", lambda pretty: None)
    monkeypatch.setattr(service, "print_result", lambda p, pretty: None)

    service._launchd_restart({"server": {"url": "http://127.0.0.1:1810"}}, pretty=False)
    assert killed == [7777]


def test_launchd_restart_does_not_kill_live_managed_backend(monkeypatch):
    """有活着的受管实例时不许在这里掐它：端口正是它在占，
    应由 _launchd_reload 的 bootout 按 ExitTimeOut 优雅收走。"""
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: 4321)
    killed: list = []
    monkeypatch.setattr(service, "_terminate", lambda pid, *a, **k: killed.append(pid))
    monkeypatch.setattr(
        service, "_find_pid_by_port", lambda url: pytest.fail("不该查端口")
    )
    monkeypatch.setattr(service, "_launcher_ready_or_exit", lambda pretty: None)
    monkeypatch.setattr(service, "_server_cmd_or_exit", lambda pretty: ["python"])
    monkeypatch.setattr(service, "_generate_launchagent_plist", lambda cmd: None)
    monkeypatch.setattr(service, "_launchd_reload", lambda: (True, ""))
    monkeypatch.setattr(service, "_wait_for_health", lambda *a, **k: None)
    monkeypatch.setattr(service, "_launchd_crashloop_check", lambda: None)
    monkeypatch.setattr(service, "_check_lnp_blocked", lambda pretty: None)
    monkeypatch.setattr(service, "print_result", lambda p, pretty: None)

    service._launchd_restart({"server": {"url": "http://127.0.0.1:1810"}}, pretty=False)
    assert killed == []


def test_launchd_restart_missing_launcher_does_not_touch_running_backend(monkeypatch):
    """restart 是升级脚本/hermes 插件最常调的命令（见 install-hermes.sh step 7），
    同 start：签名启动器缺失时不能先拆掉活着的旧实例再退出——那会把「重启」变成
    「停机且起不来」。"""
    monkeypatch.setattr(
        service, "_launcher_ready_or_exit", lambda pretty: (_ for _ in ()).throw(SystemExit(1))
    )
    reaped: list = []
    monkeypatch.setattr(
        service, "_reap_legacy_supervisord", lambda: reaped.append(1) or []
    )
    cleaned: list = []
    monkeypatch.setattr(
        service, "_cleanup_stale_port_holder", lambda *a, **k: cleaned.append(1)
    )

    with pytest.raises(SystemExit):
        service._launchd_restart({"server": {"url": "http://127.0.0.1:1810"}}, pretty=False)

    assert reaped == []
    assert cleaned == []


def test_plist_is_not_world_readable(monkeypatch, tmp_path):
    """plist 必须 0600：EnvironmentVariables 里是环境快照，含 MILOCO_* 的云端
    API key 与可能带凭证的 HTTPS_PROXY。~/Library/LaunchAgents 与 ~ 默认 755，
    落成 0644 就等于同机其他账号 `plutil -p` 直接读出密钥。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_MODEL__OMNI__API_KEY", "sk-secret")
    monkeypatch.setattr(service, "_resolve_timezone", lambda: None)

    service._generate_launchagent_plist(["/opt/py/bin/python", "-m", "miloco.main"])
    plist_path = service._launchagent_plist()
    assert "sk-secret" in plist_path.read_bytes().decode()  # 确实写进去了
    assert oct(plist_path.stat().st_mode & 0o777) == "0o600"

    # 内容没变的幂等分支也要收权限（升级上来的旧 plist 是 0644）
    os.chmod(plist_path, 0o644)
    service._generate_launchagent_plist(["/opt/py/bin/python", "-m", "miloco.main"])
    assert oct(plist_path.stat().st_mode & 0o777) == "0o600"


def test_check_lnp_blocked_window_is_relative_to_log_tail(monkeypatch, tmp_path):
    """LNP 提示的时间窗必须与宿主时钟解耦。

    plist 显式注入 TZ/MILOCO_TIMEZONE，后端日志时间戳按注入时区打，而 CLI 侧
    _extract_log_ts 用 time.mktime 按 CLI 进程本地时区解析。用 `time.time() - 15`
    做窗口，两者不一致时相差整小时数：Errno 65 行会全部落在窗外，提示恰好在最需要
    它的场景里永远不出现。改成相对日志尾部时间戳后，偏移多少都不影响判定。
    """
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    log_file = log_dir / "miloco-backend.log"
    # 模拟"注入时区比宿主快 8 小时"：整段日志的时间戳都偏移了
    shifted = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 8 * 3600))
    log_file.write_text(
        f"{shifted} - miot.central_hub - ERROR - connect failed: [Errno 65] "
        "No route to host\n"
        f"{shifted} - miot.central_hub - INFO - retrying\n"
    )
    monkeypatch.setattr(service, "_log_dir", lambda: log_dir)
    printed: list = []
    monkeypatch.setattr(service, "print", lambda *a, **k: printed.append(a), raising=False)

    service._check_lnp_blocked(pretty=True)
    out = "\n".join(str(a) for args in printed for a in args)
    assert "LNP" in out, "偏移时区的日志也必须能命中提示"


def test_check_lnp_blocked_ignores_stale_log(monkeypatch, tmp_path):
    """相对窗口不能把陈旧日志当成"刚刚发生"：文件 mtime（真 epoch，抗时区）兜底。"""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    log_file = log_dir / "miloco-backend.log"
    log_file.write_text("2020-01-01 00:00:00 - x - ERROR - [Errno 65] No route\n")
    old = time.time() - 7200
    os.utime(log_file, (old, old))
    monkeypatch.setattr(service, "_log_dir", lambda: log_dir)
    printed: list = []
    monkeypatch.setattr(service, "print", lambda *a, **k: printed.append(a), raising=False)

    service._check_lnp_blocked(pretty=True)
    assert printed == []


def test_logs_follow_uses_capital_F(monkeypatch, tmp_path):
    """`service logs -f` 必须用 tail -F：主日志现在是 rename 式轮转，
    小写 -f 跟随已打开的 fd，轮转后用户盯着的"实时日志"会静默冻结。"""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    latest = log_dir / "miloco-backend.log"
    latest.write_text("x\n")
    monkeypatch.setattr(service, "_log_dir", lambda: log_dir)
    monkeypatch.setattr(service, "_find_latest_log", lambda: latest)
    captured: dict = {}
    monkeypatch.setattr(
        service.os, "execvp", lambda file, args: captured.update(file=file, args=args)
    )

    service.service_logs.callback(lines=50, follow=True)
    assert "-F" in captured["args"] and "-f" not in captured["args"]


def test_trim_launchd_stdio_caps_runaway_file(monkeypatch, tmp_path):
    """launchd 兜底 stdio 日志必须有上限。

    没有任何写方轮转它：后端在接管 fd **之前**崩溃（最现实的是手改坏 config.json
    → bootstrap 里 get_settings() 抛 ValidationError）时，KeepAlive 按 launchd 的
    ~10s 节流不停重拉，每次追加一份 traceback ≈ 17MB/天，而崩溃循环检测只在
    start/restart 的健康探测窗口内布防 —— 正是本 PR 引为动机的失控形态换了个文件。
    """
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    stdio = log_dir / "launchd-stdio.log"
    stdio.write_text("".join(f"line-{i}\n" for i in range(140000)))  # > 1MB
    assert stdio.stat().st_size > service._LAUNCHD_STDIO_TRIM_THRESHOLD
    monkeypatch.setattr(service, "_log_dir", lambda: log_dir)

    service._trim_launchd_stdio()

    kept = stdio.read_text().splitlines()
    assert len(kept) == service._LAUNCHD_STDIO_KEEP_LINES
    assert kept[-1] == "line-139999"  # 保留的是**最后** N 行（最新的报错）
    assert oct(stdio.stat().st_mode & 0o777) == "0o600"


def test_trim_launchd_stdio_leaves_small_file_but_tightens_mode(monkeypatch, tmp_path):
    """1MB 以内不折腾内容，但权限照收：这个文件由 launchd 按 umask 建出来通常是
    0644，是本 PR 全部日志产物里唯一没收紧的缺口。"""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    stdio = log_dir / "launchd-stdio.log"
    stdio.write_text("boot ok\n")
    os.chmod(stdio, 0o644)
    monkeypatch.setattr(service, "_log_dir", lambda: log_dir)

    service._trim_launchd_stdio()

    assert stdio.read_text() == "boot ok\n"  # 内容不动
    assert oct(stdio.stat().st_mode & 0o777) == "0o600"


def test_trim_launchd_stdio_missing_file_is_noop(monkeypatch, tmp_path):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    monkeypatch.setattr(service, "_log_dir", lambda: log_dir)
    service._trim_launchd_stdio()  # 不抛异常即可
    assert not (log_dir / "launchd-stdio.log").exists()


def test_launchd_reload_trims_stdio_between_bootout_and_bootstrap(monkeypatch):
    """裁剪必须发生在 bootout 之后、bootstrap 之前 —— 那是唯一没人持有该 fd 的窗口。"""
    order: list[str] = []

    def _fake_launchctl(*args):
        order.append(args[0])
        return _cp(0)

    monkeypatch.setattr(service, "_launchctl", _fake_launchctl)
    monkeypatch.setattr(service, "_launchd_is_loaded", lambda: False)
    monkeypatch.setattr(
        service, "_trim_launchd_stdio", lambda: order.append("trim")
    )

    ok, err = service._launchd_reload()

    assert ok and err == ""
    assert order == ["bootout", "trim", "bootstrap"]


def test_reload_gives_up_when_job_never_unloads(monkeypatch):
    """job 一直卸不掉时必须直接失败：不 bootstrap、不裁日志、更不能报成功。

    `launchctl print` 对"卸载中"的 job 同样返回 0，所以 `_launchd_is_loaded()` 分不清
    「旧 job 还没卸完」和「新 job 已加载」。超时后继续往下走，重试循环里的竞态兜底
    `if _launchd_is_loaded(): return True` 就会把前者误判成后者 —— 新 plist 从未被
    bootstrap，旧实例随后退出，restart 实际变成 stop，而用户看到的是"启动超时"。
    """
    monkeypatch.setattr(service, "_launchd_is_loaded", lambda: True)
    monkeypatch.setattr(service, "_LAUNCHD_BOOTOUT_WAIT_S", 0.1)
    calls: list[tuple] = []

    def _fake_launchctl(*args):
        calls.append(args)
        return _cp(0)

    monkeypatch.setattr(service, "_launchctl", _fake_launchctl)
    trimmed: list[int] = []
    monkeypatch.setattr(service, "_trim_launchd_stdio", lambda: trimmed.append(1))

    ok, err = service._launchd_reload()

    assert ok is False and "未从 launchd 卸载" in err
    assert trimmed == []  # 别人还持有 fd 时不许截断
    assert not any(c[0] == "bootstrap" for c in calls)  # 不许盲发 bootstrap
    assert [c[0] for c in calls] == ["bootout"]


def test_bootout_wait_covers_launchd_exit_timeout():
    """预算必须覆盖 launchd 的 ExitTimeOut（plist 未覆盖 → 默认 20s），
    并与同文件 stop 的 30s grace 一个量级 —— 后端收管线/连接超个位数秒是常态。"""
    assert service._LAUNCHD_BOOTOUT_WAIT_S >= 30.0


def _cfg():
    return {"server": {"url": "http://127.0.0.1:1810"}}


def test_stop_waits_for_unload_before_checking_port(monkeypatch, capsys):
    """bootout 后必须等 job 真正卸载,才能查端口残留 —— 否则 backend 优雅退出期间
    早释放了监听 socket,_find_pid_by_port 提前返回 None,会把"job 还没卸完"误判成
    "stopped"(调用方紧接着 start 会撞见旧 job 仍在)。"""
    loaded_calls = {"n": 0}

    def _fake_is_loaded():
        loaded_calls["n"] += 1
        # 前两次仍 loaded(模拟卸载中),第三次才真正消失
        return loaded_calls["n"] < 3

    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: None)
    monkeypatch.setattr(service, "_find_pid_by_port", lambda url: None)
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_launchd_is_loaded", _fake_is_loaded)
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(0))
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: False)
    monkeypatch.setattr(service, "_trim_launchd_stdio", lambda: None)
    monkeypatch.setattr(service.time, "sleep", lambda s: None)  # 别真等 0.3s

    service._launchd_stop(_cfg(), pretty=False)

    # 必须真的轮询到 job 消失(第 3 次调用返回 False)才继续,不能只查一次就放弃等待
    assert loaded_calls["n"] >= 3


def test_stop_gives_up_waiting_but_still_falls_back_to_port_cleanup(monkeypatch):
    """job 一直卸不掉也不能卡死或跳过残留清理:超时后继续走端口兜底(与 reload 的
    "直接失败"不同 —— stop 没有后续 bootstrap 步骤需要"卸载完成"这个前提)。"""
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: None)
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_launchd_is_loaded", lambda: True)  # 永不消失
    monkeypatch.setattr(service, "_LAUNCHD_BOOTOUT_WAIT_S", 0.05)
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(0))
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: False)
    monkeypatch.setattr(service, "_trim_launchd_stdio", lambda: None)

    port_checked: list[bool] = []

    def _fake_find_pid_by_port(url):
        port_checked.append(True)
        return None

    monkeypatch.setattr(service, "_find_pid_by_port", _fake_find_pid_by_port)

    service._launchd_stop(_cfg(), pretty=False)  # 不应抛异常 / 不应无限等待

    assert port_checked  # 超时后仍然走到了端口兜底清理


def test_stop_skips_trim_when_job_never_unloads(monkeypatch):
    """job 超时仍未卸载时,launchd 可能仍持着 launchd-stdio.log 的 fd,不能裁剪
    (_trim_launchd_stdio 自己的前置条件是"没人持有该 fd"),否则会留一段 NUL 空洞。"""
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: None)
    monkeypatch.setattr(service, "_find_pid_by_port", lambda url: None)
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_launchd_is_loaded", lambda: True)  # 永不消失
    monkeypatch.setattr(service, "_LAUNCHD_BOOTOUT_WAIT_S", 0.05)
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(0))
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: False)
    trimmed: list[int] = []
    monkeypatch.setattr(service, "_trim_launchd_stdio", lambda: trimmed.append(1))

    service._launchd_stop(_cfg(), pretty=False)

    assert trimmed == []


def test_stop_trims_when_job_unloads_in_time(monkeypatch):
    """job 正常卸载完的路径必须仍然裁剪 —— 前一条测试只钉住"超时不裁"这一半。"""
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: None)
    monkeypatch.setattr(service, "_find_pid_by_port", lambda url: None)
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_launchd_is_loaded", lambda: False)  # 已卸载
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(0))
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: False)
    trimmed: list[int] = []
    monkeypatch.setattr(service, "_trim_launchd_stdio", lambda: trimmed.append(1))

    service._launchd_stop(_cfg(), pretty=False)

    assert trimmed == [1]


def test_cleanup_stale_port_holder_uses_30s_grace_by_default(monkeypatch):
    """start/restart 共用的端口清理必须跟 stop 同一个 30s grace,不能停在
    _terminate 的默认 6s —— 后端自身关停序列里单是 perception stop_engine 一步
    就有 10s 预算,6s 必然中途 SIGKILL,留半截清理并丢掉最后一段 trace。"""
    monkeypatch.setattr(service, "_find_pid_by_port", lambda url: 4321)
    monkeypatch.setattr(service, "_is_miloco_backend_proc", lambda pid: True)
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: False)

    calls: list[tuple] = []
    monkeypatch.setattr(
        service, "_terminate", lambda pid, grace=6.0: calls.append((pid, grace))
    )

    service._cleanup_stale_port_holder(_cfg(), pretty=False)

    assert calls == [(4321, service._MILOCO_STOP_GRACE_S)]
    assert service._MILOCO_STOP_GRACE_S >= 30.0


def test_launchd_stop_reuses_same_grace_constant_as_cleanup(monkeypatch):
    """_launchd_stop 和 _cleanup_stale_port_holder 必须共用同一个 grace 常量,
    不能各写一份字面量(否则将来改一处会漏改另一处,重新制造这次修的不对称问题)。"""
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: None)
    monkeypatch.setattr(service, "_reap_legacy_supervisord", lambda: [])
    monkeypatch.setattr(service, "_launchd_is_loaded", lambda: False)
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(0))
    monkeypatch.setattr(service, "_is_port_in_use", lambda url: False)
    monkeypatch.setattr(service, "_trim_launchd_stdio", lambda: None)
    monkeypatch.setattr(service, "_find_pid_by_port", lambda url: 4321)
    monkeypatch.setattr(service, "_is_miloco_backend_proc", lambda pid: True)

    calls: list[tuple] = []
    monkeypatch.setattr(
        service, "_terminate", lambda pid, grace=6.0: calls.append((pid, grace))
    )

    service._launchd_stop(_cfg(), pretty=False)

    assert calls == [(4321, service._MILOCO_STOP_GRACE_S)]
