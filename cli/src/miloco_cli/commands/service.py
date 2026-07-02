"""service 命令组：start / stop / restart / status / logs"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import click

from miloco_cli.config import miloco_home
from miloco_cli.output import print_result

_PROGRAM_NAME = "miloco-backend"
_SERVER_MODULE = "miloco.main"


# 路径相关常量延迟到调用时解析：``miloco_home()``
def _log_dir() -> Path:
    return miloco_home() / "log"


def _supervisor_conf() -> Path:
    return miloco_home() / "supervisord.conf"


def _supervisor_pid_file() -> Path:
    return miloco_home() / "supervisord.pid"


def _supervisor_sock() -> Path:
    return miloco_home() / "supervisor.sock"


def _supervisor_log() -> Path:
    return _log_dir() / "supervisord.log"


# ─── 进程 / 端口辅助 ─────────────────────────────────────────────────────────


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _is_port_in_use(base_url: str) -> bool:
    parsed = urlparse(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port
    if port is None:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _find_pid_by_port(base_url: str) -> int | None:
    """通过端口反查监听进程的 PID。优先 lsof（macOS/Linux 通用），lsof 缺失时回退 ss（Linux）。"""
    parsed = urlparse(base_url)
    port = parsed.port
    if port is None:
        return None
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        # 部分精简 Linux 无 lsof，回退 ss（iproute2，多数发行版自带）
        return _find_pid_by_port_ss(port)
    except Exception:
        return None
    pids = [int(x) for x in result.stdout.split() if x.isdigit()]
    return pids[0] if pids else None


def _has_port_lookup_tool() -> bool:
    """系统是否具备按端口反查进程的工具（lsof 或 ss）。"""
    return bool(shutil.which("lsof") or shutil.which("ss"))


def _find_pid_by_port_ss(port: int) -> int | None:
    """lsof 不可用时的回退：用 ss 反查监听端口的 PID（Linux）。"""
    import re

    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    m = re.search(r"pid=(\d+)", result.stdout)
    return int(m.group(1)) if m else None


def _find_supervisord_pids() -> list[int]:
    """枚举所有加载本项目 supervisord.conf 的守护进程 PID（socket 失联也能找到）。"""
    conf = str(_supervisor_conf())
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        pid_str, _, cmd = line.strip().partition(" ")
        if "supervisord" in cmd and conf in cmd:
            try:
                pids.append(int(pid_str))
            except ValueError:
                pass
    return pids


def _terminate(pid: int, grace: float = 6.0) -> None:
    """SIGTERM 优雅退出，超时未退则 SIGKILL。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        time.sleep(0.2)
        if not _is_running(pid):
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _process_uptime_seconds(pid: int) -> int | None:
    """进程已运行秒数（跨平台：ps -o etimes=）。"""
    try:
        result = subprocess.run(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    s = result.stdout.strip()
    return int(s) if s.isdigit() else None


# ─── server 启动命令解析 ─────────────────────────────────────────────────────


def _server_cmd_or_exit(pretty: bool) -> list[str]:
    """根据 ``server.python_bin`` 构造启动命令；非法则退出。"""
    from miloco_cli.config import get_value

    try:
        python_bin = get_value("server.python_bin")
    except KeyError:
        python_bin = ""

    if not python_bin:
        print_result(
            {
                "error": "server.python_bin 未配置",
                "hint": "配置方法: miloco-cli config set server.python_bin /path/to/python",
            },
            pretty,
        )
        sys.exit(1)

    p = Path(str(python_bin))
    if not p.exists() or not os.access(p, os.X_OK):
        print_result(
            {
                "error": f"server.python_bin 指向的解释器不可执行: {python_bin}",
                "hint": "通过 miloco-cli config set server.python_bin <path> 更新",
            },
            pretty,
        )
        sys.exit(1)

    return [str(p), "-m", _SERVER_MODULE]


# ─── supervisor 辅助 ─────────────────────────────────────────────────────────


def _parse_uptime_seconds(uptime_str: str) -> int | None:
    """'0:01:23' or '2 days, 0:01:23' → seconds"""
    try:
        days = 0
        if " days, " in uptime_str:
            day_part, uptime_str = uptime_str.split(" days, ")
            days = int(day_part)
        elif " day, " in uptime_str:
            day_part, uptime_str = uptime_str.split(" day, ")
            days = int(day_part)
        h, m, s = uptime_str.split(":")
        return days * 86400 + int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return None


def _find_latest_log() -> Path | None:
    """查找最新的 backend 日志文件。优先 supervisor 管理的固定名，fallback 到历史 timestamp 格式。"""
    log_dir = _log_dir()
    if not log_dir.exists():
        return None
    supervised = log_dir / "miloco-backend.log"
    if supervised.exists():
        return supervised
    candidates = sorted(log_dir.glob("miloco-backend_*.log"))
    return candidates[-1] if candidates else None


def _find_latest_log_str() -> str | None:
    log = _find_latest_log()
    return str(log) if log else None


def _resolve_timezone() -> str | None:
    """部署时区 IANA 名,注入 supervisord ``environment=`` 让 backend 子进程继承。

    单一真源,与 backend ``deploy_timezone()`` / 插件 ``deployTimezone()`` 的解析顺序对齐:
    ``MILOCO_TIMEZONE`` env > ``config.json`` 的 ``timezone``。两者都拿不到 → 返回 None,
    不强塞 Asia/Shanghai:让子进程继承宿主 TZ、backend 自身再走系统反查兜底,避免把
    未配置部署错标成中国时区。

    注入 ``TZ`` 是防御纵深:除 ``MILOCO_TIMEZONE`` 让 pydantic ``settings.timezone`` 生效外,
    ``TZ`` 还统一 backend 里一切"裸" naive datetime(``datetime.now()`` / ``fromtimestamp``,
    含尚未逐个改造到 ``deploy_timezone()`` 的调用点)的 OS 级时区。
    """
    if tz := os.environ.get("MILOCO_TIMEZONE"):
        return tz
    try:
        from miloco_cli.config import load_config

        tz = load_config().get("timezone")
    except Exception:
        return None
    return tz if isinstance(tz, str) and tz else None


def _generate_supervisor_conf(server_cmd: str) -> None:
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    sup_conf_path = _supervisor_conf()
    tz = _resolve_timezone()
    # 解析到时区才追加 TZ + MILOCO_TIMEZONE；否则不塞,交给子进程继承宿主 + backend 兜底。
    tz_env = f',TZ="{tz}",MILOCO_TIMEZONE="{tz}"' if tz else ""
    conf = f"""\
[supervisord]
logfile={_supervisor_log()}
logfile_maxbytes=10MB
logfile_backups=2
pidfile={_supervisor_pid_file()}
nodaemon=false
silent=true

[unix_http_server]
file={_supervisor_sock()}

[supervisorctl]
serverurl=unix://{_supervisor_sock()}

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[program:{_PROGRAM_NAME}]
command={server_cmd}
autorestart=true
startretries=3
startsecs=5
stopwaitsecs=30
redirect_stderr=true
stdout_logfile={log_dir}/miloco-backend.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=20
environment=MILOCO_SUPERVISED="1",MILOCO_HOME="{miloco_home()}"{tz_env}
"""
    if sup_conf_path.exists() and sup_conf_path.read_text() == conf:
        return
    sup_conf_path.write_text(conf)


def _supervisord_is_running() -> bool:
    if not _supervisor_sock().exists() or not _supervisor_conf().exists():
        return False
    result = _supervisorctl("pid")
    return result.returncode == 0 and result.stdout.strip().isdigit()


def _supervisorctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["supervisorctl", "-c", str(_supervisor_conf()), *args],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="", stderr="timeout"
        )


def _get_backend_pid_from_supervisor() -> int | None:
    """从 supervisorctl status 输出中解析 backend 的 PID。"""
    result = _supervisorctl("status", _PROGRAM_NAME)
    # 格式: "miloco-backend   RUNNING   pid 12345, uptime 0:01:23"
    line = result.stdout.strip()
    if "RUNNING" not in line:
        return None
    import re

    m = re.search(r"pid\s+(\d+)", line)
    return int(m.group(1)) if m else None


def _resolve_backend_pid(cfg: dict, timeout: float = 8.0) -> int | None:
    """取 backend PID：轮询 supervisor 至 RUNNING（避开 startsecs 窗口），兜底按端口反查。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = _get_backend_pid_from_supervisor()
        if pid:
            return pid
        time.sleep(0.3)
    return _find_pid_by_port(cfg["server"]["url"])


def _wait_for_health(cfg: dict, pretty: bool) -> None:
    """轮询 /health，超时 30 秒。检测 FATAL 状态提前退出。"""
    import httpx

    health_url = cfg["server"]["url"].rstrip("/") + "/health"
    deadline = time.time() + 30
    while time.time() < deadline:
        status = _supervisorctl("status", _PROGRAM_NAME).stdout
        if "FATAL" in status:
            print_result({"error": "process failed to start, check logs"}, pretty)
            sys.exit(1)
        try:
            if httpx.get(health_url, timeout=2, verify=False).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    print_result(
        {"error": "service did not become ready within 30s, check logs"}, pretty
    )
    sys.exit(1)


# ─── 命令定义 ────────────────────────────────────────────────────────────────


@click.group("service")
def service_group():
    """服务管理：启动 / 停止 / 重启 / 状态 / 日志。"""


@service_group.command("start")
@click.option("--foreground", is_flag=True, help="前台运行（不 daemonize）")
@click.option("--pretty", is_flag=True)
def service_start(foreground, pretty):
    """启动 Miloco Backend 服务。"""
    # 检查 supervisor 托管的进程是否已在运行
    if _supervisord_is_running():
        backend_pid = _get_backend_pid_from_supervisor()
        if backend_pid:
            print_result(
                {"code": 1, "message": f"already running (pid={backend_pid})"}, pretty
            )
            sys.exit(1)

    from miloco_cli.config import load_config

    cfg = load_config()
    if _is_port_in_use(cfg["server"]["url"]):
        print_result(
            {"code": 1, "message": f"port already in use: {cfg['server']['url']}"},
            pretty,
        )
        sys.exit(1)

    cmd = _server_cmd_or_exit(pretty)

    if foreground:
        os.execvp(cmd[0], cmd)
        # 不会到达这里
    else:
        _generate_supervisor_conf(shlex.join(cmd))

        if _supervisord_is_running():
            _supervisorctl("reread")
            _supervisorctl("update")
            result = _supervisorctl("start", _PROGRAM_NAME)
            if result.returncode != 0:
                print_result(
                    {"error": f"supervisorctl start failed: {result.stdout.strip()}"},
                    pretty,
                )
                sys.exit(1)
        else:
            # 防重复孵化：socket 失联但仍有残留守护进程时，先 reap 再起，保证全局单例
            for orphan in _find_supervisord_pids():
                _terminate(orphan)
            _supervisor_sock().unlink(missing_ok=True)
            _supervisor_pid_file().unlink(missing_ok=True)
            try:
                subprocess.run(
                    ["supervisord", "-c", str(_supervisor_conf())],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print_result({"error": f"supervisord failed to start: {e}"}, pretty)
                sys.exit(1)

        _wait_for_health(cfg, pretty)
        backend_pid = _resolve_backend_pid(cfg)
        print_result({"code": 0, "message": "started", "pid": backend_pid}, pretty)


@service_group.command("stop")
@click.option("--pretty", is_flag=True)
@click.pass_context
def service_stop(ctx, pretty):
    """停止 Miloco Backend 服务。"""
    _do_stop(pretty=pretty, quiet=ctx.obj.get("quiet", False) if ctx.obj else False)


def _do_stop(pretty: bool, quiet: bool = False) -> None:
    from miloco_cli.config import load_config

    cfg = load_config()

    # reap 前先快照：是否有可停对象 + backend pid（reap 会连带停 backend，事后无从得知）
    control_up = _supervisord_is_running()
    backend_pid = _get_backend_pid_from_supervisor() if control_up else None
    if backend_pid is None:
        backend_pid = _find_pid_by_port(cfg["server"]["url"])
    acted = control_up or bool(_find_supervisord_pids()) or backend_pid is not None

    # 1. 控制通道在线时，优雅 shutdown supervisord（连带停掉子进程）
    if control_up:
        # 先读 pidfile 再 shutdown，避免 shutdown 后 pidfile 被清理
        try:
            sup_pid = int(_supervisor_pid_file().read_text().strip())
        except (ValueError, OSError):
            # pidfile 异常时回退：控制通道在线，supervisorctl pid 必能拿到
            result = _supervisorctl("pid")
            out = result.stdout.strip()
            sup_pid = int(out) if out.isdigit() else None
        _supervisorctl("shutdown")
        if sup_pid:
            _terminate(sup_pid, grace=40.0)

    # 2. 兜底：reap 所有残留 supervisord 守护进程（socket 失联也能杀，断掉 autorestart）
    for pid in _find_supervisord_pids():
        _terminate(pid)

    # 3. 兜底：autorestart 已断，按端口收尾仍在监听的 backend
    port_pid = _find_pid_by_port(cfg["server"]["url"])
    if port_pid:
        _terminate(port_pid)

    # 4. 清理运行时文件
    for f in (_supervisor_sock(), _supervisor_pid_file()):
        f.unlink(missing_ok=True)

    # 清理后端口仍被占用，却无 lsof/ss 可定位残留进程 → 明确报错退出，不静默假装已停
    if _is_port_in_use(cfg["server"]["url"]) and not _has_port_lookup_tool():
        print_result(
            {
                "error": f"端口仍被占用，且系统无 lsof / ss 可定位残留进程: {cfg['server']['url']}",
                "hint": "请安装 lsof 或 ss（iproute2）后重试，或手动 kill 占用该端口的进程",
            },
            pretty,
        )
        sys.exit(1)

    if not quiet:
        msg = "stopped" if acted else "not running"
        print_result({"code": 0, "message": msg, "pid": backend_pid}, pretty)


@service_group.command("restart")
@click.option("--pretty", is_flag=True)
@click.pass_context
def service_restart(ctx, pretty):
    """重启 Miloco Backend 服务。"""
    if _supervisord_is_running():
        cmd = _server_cmd_or_exit(pretty)
        _generate_supervisor_conf(shlex.join(cmd))
        _supervisorctl("reread")
        _supervisorctl("update")
        result = _supervisorctl("restart", _PROGRAM_NAME)
        if result.returncode != 0:
            print_result(
                {"error": f"supervisorctl restart failed: {result.stdout.strip()}"},
                pretty,
            )
            sys.exit(1)

        from miloco_cli.config import load_config

        cfg = load_config()
        _wait_for_health(cfg, pretty)
        backend_pid = _resolve_backend_pid(cfg)
        print_result({"code": 0, "message": "restarted", "pid": backend_pid}, pretty)
    else:
        _do_stop(pretty=pretty, quiet=True)
        ctx.invoke(service_start, foreground=False, pretty=pretty)


@service_group.command("status")
@click.option("--pretty", is_flag=True)
def service_status(pretty):
    """查询服务进程状态（PID、端口、uptime）。"""
    from miloco_cli.config import load_config

    cfg = load_config()

    # 优先从 supervisor 查询
    if _supervisord_is_running():
        result = _supervisorctl("status", _PROGRAM_NAME)
        line = result.stdout.strip()
        parts = line.split()
        state = parts[1] if len(parts) > 1 else "UNKNOWN"
        # 格式: "miloco-backend   RUNNING   pid 12345, uptime 0:01:23"
        if state == "RUNNING":
            import re

            pid = None
            uptime_seconds = None
            m_pid = re.search(r"pid\s+(\d+)", line)
            if m_pid:
                pid = int(m_pid.group(1))
            m_uptime = re.search(r"uptime\s+(.+)", line)
            if m_uptime:
                uptime_seconds = _parse_uptime_seconds(m_uptime.group(1))

            print_result(
                {
                    "running": True,
                    "managed": True,
                    "pid": pid,
                    "uptime_seconds": uptime_seconds,
                    "log_file": _find_latest_log_str(),
                    "server": {"url": cfg["server"]["url"]},
                },
                pretty,
            )
            return
        else:
            print_result(
                {
                    "running": False,
                    "managed": True,
                    "supervisor_state": state,
                    "log_file": _find_latest_log_str(),
                },
                pretty,
            )
            return

    # 兜底：通过端口找非托管进程
    managed = False
    pid = _find_pid_by_port(cfg["server"]["url"])
    if not pid:
        print_result({"running": False}, pretty)
        return

    uptime_seconds = _process_uptime_seconds(pid)

    print_result(
        {
            "running": True,
            "managed": managed,
            "pid": pid,
            "uptime_seconds": uptime_seconds,
            "log_file": _find_latest_log_str(),
            "server": {"url": cfg["server"]["url"]},
        },
        pretty,
    )


@service_group.command("kill")
@click.option("--pretty", is_flag=True)
def service_kill(pretty):
    """强制杀掉所有 supervisord 守护进程与残留 backend，清运行时文件（脏状态逃生舱）。"""
    from miloco_cli.config import load_config

    cfg = load_config()
    killed_supervisord: list[int] = []
    killed_backend: list[int] = []

    # 先杀全部 supervisord（断 autorestart），再按端口收尾残留 backend
    for pid in _find_supervisord_pids():
        _terminate(pid)
        killed_supervisord.append(pid)
    port_pid = _find_pid_by_port(cfg["server"]["url"])
    if port_pid:
        _terminate(port_pid)
        killed_backend.append(port_pid)

    removed: list[str] = []
    for f in (_supervisor_sock(), _supervisor_pid_file()):
        if f.exists():
            f.unlink(missing_ok=True)
            removed.append(str(f))

    # 清理后端口仍被占用，却无 lsof/ss 可定位残留进程 → 明确报错退出
    if _is_port_in_use(cfg["server"]["url"]) and not _has_port_lookup_tool():
        print_result(
            {
                "error": f"端口仍被占用，且系统无 lsof / ss 可定位残留进程: {cfg['server']['url']}",
                "hint": "请安装 lsof 或 ss（iproute2）后重试，或手动 kill 占用该端口的进程",
            },
            pretty,
        )
        sys.exit(1)

    print_result(
        {
            "code": 0,
            "message": "cleaned",
            "killed_supervisord": killed_supervisord,
            "killed_backend": killed_backend,
            "removed": removed,
        },
        pretty,
    )


@service_group.command("logs")
@click.option("--follow", "-f", is_flag=True, help="持续跟踪日志（类似 tail -f）")
@click.option("--lines", "-n", default=50, show_default=True, help="显示最后 N 行")
def service_logs(follow, lines):
    """查看服务日志。"""
    log_dir = _log_dir()
    if not log_dir.exists():
        click.echo(f"log dir not found: {log_dir}", err=True)
        sys.exit(1)

    latest = _find_latest_log()
    if not latest:
        click.echo(f"no backend log in {log_dir}", err=True)
        sys.exit(1)

    cmd = ["tail", f"-n{lines}"]
    if follow:
        cmd.append("-f")
    cmd.append(str(latest))

    os.execvp("tail", cmd)
