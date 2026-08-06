"""service 命令组：start / stop / restart / status / logs"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
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

    委托共享 ``deploy_tz.explicit_timezone_name()``（CLI 侧时区解析唯一真源,与
    time_compute / backend ``deploy_timezone()`` / 插件 ``deployTimezone()`` 同序）:
    ``MILOCO_TIMEZONE`` env > ``config.json`` 的 ``timezone``。两者都拿不到（或名字
    非法——比旧实现多一道 IANA 校验,非法名 warning 后不注入）→ 返回 None,
    不强塞 Asia/Shanghai:让子进程继承宿主 TZ、backend 自身再走系统反查兜底,避免把
    未配置部署错标成中国时区。

    注入 ``TZ`` 是防御纵深:除 ``MILOCO_TIMEZONE`` 让 pydantic ``settings.timezone`` 生效外,
    ``TZ`` 还统一 backend 里一切"裸" naive datetime(``datetime.now()`` / ``fromtimestamp``,
    含尚未逐个改造到 ``deploy_timezone()`` 的调用点)的 OS 级时区。
    """
    from miloco_cli.deploy_tz import explicit_timezone_name

    return explicit_timezone_name()


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


def _wait_for_health(
    cfg: dict, pretty: bool, fatal_check: Callable[[], bool] | None = None
) -> None:
    """轮询 /health，超时 30 秒。``fatal_check`` 返回 True 时判定启动失败提前退出
    （supervisord 传 FATAL 检测；launchd 传 crashloop 检测，见
    ``_launchd_crashloop_check``）。"""
    import httpx

    health_url = cfg["server"]["url"].rstrip("/") + "/health"
    deadline = time.time() + 30
    while time.time() < deadline:
        if fatal_check and fatal_check():
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


def _supervisor_fatal() -> bool:
    return "FATAL" in _supervisorctl("status", _PROGRAM_NAME).stdout


# ─── launchd (macOS) ─────────────────────────────────────────────────────────
#
# macOS 上以用户态(euid 501)跑的后端直连 LAN 网关会被 Local Network Privacy(LNP)
# 拦。绕过它的正道:让 python 作为"有独立签名身份的 app"的子进程运行,LNP 把子进程的
# 本地网络访问归属到该 app、授权一次即通。故 darwin 不用 supervisord(双 fork 会丢 gui
# 会话归属),改用 launchd:
#   launchd(gui/uid) → miloco.app 签名启动器 → python -m miloco.main(子进程)
# 启动器是随包 vendored 的签名 stub,只负责 posix_spawn 其参数指定的子进程。

_LAUNCHD_LABEL = "com.xiaomi.miloco.backend"


def _use_launchd() -> bool:
    return sys.platform == "darwin"


def _launchagent_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def _launcher_bin() -> Path:
    """vendored 签名启动器可执行文件(部署时落到 miloco_home()/miloco.app)。"""
    return miloco_home() / "miloco.app" / "Contents" / "MacOS" / "miloco"


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_target() -> str:
    return f"{_launchd_domain()}/{_LAUNCHD_LABEL}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="", stderr="timeout"
        )


def _launchd_is_loaded() -> bool:
    return _launchctl("print", _launchd_target()).returncode == 0


def _launchd_backend_pid() -> int | None:
    """从 ``launchctl print`` 输出解析 backend PID(未运行/未加载则 None)。"""
    result = _launchctl("print", _launchd_target())
    if result.returncode != 0:
        return None
    import re

    m = re.search(r"\bpid = (\d+)", result.stdout)
    return int(m.group(1)) if m else None


def _generate_launchagent_plist(cmd: list[str]) -> None:
    """写 ``~/Library/LaunchAgents/com.xiaomi.miloco.backend.plist``(内容变才写,idempotent)。

    ProgramArguments = 签名启动器 + cmd(=[python_bin, -m, miloco.main]);
    EnvironmentVariables 复刻 supervisord 的 ``environment=`` 并补 launchd 默认不给的
    ``HOME`` / ``PATH``(含 homebrew,后端要 shell 调裸 ffmpeg)。
    """
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path = _launchagent_plist()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = str(log_dir / "miloco-backend.log")

    env = {
        "MILOCO_SUPERVISED": "1",
        "MILOCO_HOME": str(miloco_home()),
        "HOME": str(Path.home()),
        # launchd 只给最小 PATH;补 homebrew(arm64 /opt/homebrew、intel /usr/local)
        # 供后端 shell 调裸 ffmpeg,并补 ~/.local/bin(uv 工具所在),避免丢失继承。
        "PATH": (
            f"/opt/homebrew/bin:/usr/local/bin:{Path.home()}/.local/bin"
            ":/usr/bin:/bin:/usr/sbin:/sbin"
        ),
    }
    tz = _resolve_timezone()
    if tz:
        env["TZ"] = tz
        env["MILOCO_TIMEZONE"] = tz

    plist = {
        "Label": _LAUNCHD_LABEL,
        "ProgramArguments": [str(_launcher_bin()), *cmd],
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        # 对齐 supervisord autorestart=true:非 0 退出码 或 信号崩溃 都重拉,
        # clean exit(0)不拉。信号崩溃依赖启动器如实透传 WIFSIGNALED(见
        # miloco_launcher.c),否则这条路匹配不上。
        # launchd 内建 ~10s ThrottleInterval 防紧循环;"放弃"语义由 CLI 侧
        # _launchd_crashloop_check 在健康探测窗口内补。
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": log_file,
        "StandardErrorPath": log_file,
        "WorkingDirectory": str(miloco_home()),
    }
    data = plistlib.dumps(plist)
    if plist_path.exists() and plist_path.read_bytes() == data:
        return
    # 原子写入:先写 .tmp 再 os.replace,避免中途 crash/断电留下损坏的 plist
    # (launchd 会尝试加载损坏文件)。
    tmp = plist_path.with_name(plist_path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, plist_path)


def _launchd_reload() -> tuple[bool, str]:
    """bootout(容错)+ 等 launchd 拆卸完成 + bootstrap 重新加载（picks up 重写后的 plist）。

    直接 bootout 后立刻 bootstrap 会撞 launchd 拆卸竞态,报
    ``Bootstrap failed: 5: Input/output error``。故 bootout 后轮询到 job 真正从
    launchd 消失再 bootstrap,并对该竞态做有限重试。
    """
    target = _launchd_target()
    _launchctl("bootout", target)  # 未加载时报错,忽略
    # 等 job 从 launchd 完全消失(最多 ~8s),避免 bootstrap 撞拆卸竞态
    deadline = time.time() + 8
    while time.time() < deadline and _launchd_is_loaded():
        time.sleep(0.3)
    err = ""
    for _ in range(5):
        result = _launchctl("bootstrap", _launchd_domain(), str(_launchagent_plist()))
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout).strip()
        # 竞态下 bootstrap 可能报错但实际已加载 → 视作成功
        if _launchd_is_loaded():
            return True, ""
        time.sleep(0.6)
    return False, err


def _check_lnp_blocked(pretty: bool) -> None:
    """后端已起，但若近期日志里有 Errno 65 则说明 macOS LNP 仍拦着中枢连接——给用户
    一条可操作的提示，告诉他们去哪打开授权开关。只读最近 15s 的日志行，不阻塞。"""
    log_dir = _log_dir()
    log_file = log_dir / "miloco-backend.log"
    if not log_file.exists():
        return
    try:
        cutoff = time.time() - 15
        hits = [
            ln
            for ln in _tail_lines(log_file, 400)
            if "Errno 65" in ln and _extract_log_ts(ln) >= cutoff
        ]
    except Exception:
        return
    if not hits:
        return
    print(
        "\n"
        + "┌" + "─" * 61 + "┐\n"
        "│  macOS LNP 正在拦截中枢连接 (Errno 65)" + " " * 18 + "│\n"
        "│" + " " * 61 + "│\n"
        "│  → 系统设置 → 隐私与安全性 → 本地网络" + " " * 21 + "│\n"
        "│     找到 miloco 并打开开关" + " " * 36 + "│\n"
        "└" + "─" * 61 + "┘",
    )


def _tail_lines(path: Path, n: int, block: int = 65536) -> list[str]:
    """读文件末尾 ~n 行,只 seek 尾部若干块,不读全量(避免大日志 OOM)。"""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            buf = b""
            pos = size
            while pos > 0 and buf.count(b"\n") <= n:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
        return buf.decode("utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def _extract_log_ts(line: str) -> float:
    """从 ``2026-07-30 12:00:00`` 格式日志行提取 unix epoch，失败返回 0。
    日志时间戳是**本地时区**，故用 ``time.mktime`` 而非 ``calendar.timegm``。"""
    try:
        ts_str = line.split(" - ")[0].strip()
        dt = time.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return time.mktime(dt)
    except Exception:
        return 0.0


def _launchd_crashloop_check() -> Callable[[], bool]:
    """给 ``_wait_for_health`` 用的 fatal_check：检测后端反复重生(crashloop)。

    launchd `KeepAlive={SuccessfulExit:false}` 没有 supervisord `startretries→FATAL` 的
    "放弃"态,持续崩溃会每 ~10s 无限重拉。这里在健康探测窗口内跟踪 backend pid:
    正常启动 pid 恒定;若窗口内见到 ≥3 个不同 pid(即崩溃重生 ≥2 次),判定 crashloop,
    **bootout 停掉**(不再无限重生)并让 `_wait_for_health` 报失败退出——对齐 FATAL 语义。
    单次重启(2 个 pid)容忍,不误判慢启动。
    """
    seen: set[int] = set()

    def check() -> bool:
        pid = _launchd_backend_pid()
        if pid:
            seen.add(pid)
        if len(seen) >= 3:
            _launchctl("bootout", _launchd_target())  # 放弃:停止无限重生
            return True
        return False

    return check


def _is_miloco_backend_proc(pid: int) -> bool:
    """判断进程是不是 miloco backend(命令行含 ``-m miloco.main``)。

    用于启动前按端口清理：只允许杀可证明属于 miloco 的残留进程，绝不误杀
    用户其它服务占用了同一端口的情况。token 级匹配(``-m`` + ``miloco.main``
    词边界),避免命中 vim/grep miloco.main.py 等无关进程。
    """
    import re

    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return bool(re.search(r"-m\s+miloco\.main\b", out.stdout))


def _reap_legacy_supervisord() -> list[int]:
    """darwin 升级迁移:老版本后端由 supervisord 托管,切到 launchd 前必须把残留的
    supervisord 守护进程收拾掉——否则它 autorestart 的 backend 占着端口,新 launchd
    实例起不来 / 撞端口。reap 全部 supervisord(其 shutdown 连带停子 backend)并清掉
    老运行时文件(conf/pid/sock)。无残留时是 no-op,可反复安全调用。"""
    reaped: list[int] = []
    for pid in _find_supervisord_pids():
        _terminate(pid)
        reaped.append(pid)
    if reaped:
        for f in (_supervisor_sock(), _supervisor_pid_file(), _supervisor_conf()):
            f.unlink(missing_ok=True)
    return reaped


def _launcher_ready_or_exit(pretty: bool) -> None:
    launcher = _launcher_bin()
    if not launcher.exists() or not os.access(launcher, os.X_OK):
        print_result(
            {
                "error": f"签名启动器缺失或不可执行: {launcher}",
                "hint": "部署应把 miloco.app 落到 miloco_home()/miloco.app（scripts/sync-to-remote.sh 负责）",
            },
            pretty,
        )
        sys.exit(1)


def _launchd_start(cfg: dict, pretty: bool) -> None:
    pid = _launchd_backend_pid()
    if pid:
        print_result({"code": 1, "message": f"already running (pid={pid})"}, pretty)
        sys.exit(1)
    _reap_legacy_supervisord()  # 老 supervisord 版升级迁移:先清残留再起 launchd
    # 兜底：supervisord 被 SIGKILL 后其子 backend 可能仍占端口。只清理可证明
    # 属于 miloco 的残留进程(命令行含 -m miloco.main)，绝不碰任意用户进程。
    port_pid = _find_pid_by_port(cfg["server"]["url"])
    if port_pid and _is_miloco_backend_proc(port_pid):
        _terminate(port_pid)
    if _is_port_in_use(cfg["server"]["url"]):
        print_result(
            {"code": 1, "message": f"port already in use: {cfg['server']['url']}"},
            pretty,
        )
        sys.exit(1)
    _launcher_ready_or_exit(pretty)
    cmd = _server_cmd_or_exit(pretty)
    _generate_launchagent_plist(cmd)
    ok, err = _launchd_reload()
    if not ok:
        print_result({"error": f"launchctl bootstrap failed: {err}"}, pretty)
        sys.exit(1)
    _wait_for_health(cfg, pretty, fatal_check=_launchd_crashloop_check())
    print_result(
        {"code": 0, "message": "started", "pid": _launchd_backend_pid()}, pretty
    )
    _check_lnp_blocked(pretty)


def _launchd_stop(cfg: dict, pretty: bool, quiet: bool = False) -> None:
    backend_pid = _launchd_backend_pid() or _find_pid_by_port(cfg["server"]["url"])
    legacy = _reap_legacy_supervisord()  # 混合态:连带停掉残留的老 supervisord
    acted = _launchd_is_loaded() or backend_pid is not None or bool(legacy)
    _launchctl("bootout", _launchd_target())  # 未加载时报错,忽略
    # 兜底:bootout 后仍在监听端口的残留进程——只清理可证明属于 miloco 的
    # (命令行含 -m miloco.main),绝不误杀用户其它服务(与 _launchd_start 一致)。
    port_pid = _find_pid_by_port(cfg["server"]["url"])
    if port_pid and _is_miloco_backend_proc(port_pid):
        _terminate(port_pid)
    if _is_port_in_use(cfg["server"]["url"]) and not _has_port_lookup_tool():
        print_result(
            {
                "error": f"端口仍被占用，且系统无 lsof / ss 可定位残留进程: {cfg['server']['url']}",
                "hint": "请安装 lsof 后重试，或手动 kill 占用该端口的进程",
            },
            pretty,
        )
        sys.exit(1)
    if not quiet:
        print_result(
            {
                "code": 0,
                "message": "stopped" if acted else "not running",
                "pid": backend_pid,
            },
            pretty,
        )


def _launchd_restart(cfg: dict, pretty: bool) -> None:
    _reap_legacy_supervisord()  # 升级迁移:先清残留 supervisord,免其 backend 占端口
    _launcher_ready_or_exit(pretty)
    cmd = _server_cmd_or_exit(pretty)
    _generate_launchagent_plist(cmd)
    ok, err = _launchd_reload()
    if not ok:
        print_result({"error": f"launchctl bootstrap failed: {err}"}, pretty)
        sys.exit(1)
    _wait_for_health(cfg, pretty, fatal_check=_launchd_crashloop_check())
    print_result(
        {"code": 0, "message": "restarted", "pid": _launchd_backend_pid()}, pretty
    )
    _check_lnp_blocked(pretty)


def _launchd_status(cfg: dict, pretty: bool) -> None:
    if _launchd_is_loaded():
        pid = _launchd_backend_pid()
        if pid:
            print_result(
                {
                    "running": True,
                    "managed": True,
                    "pid": pid,
                    "uptime_seconds": _process_uptime_seconds(pid),
                    "log_file": _find_latest_log_str(),
                    "server": {"url": cfg["server"]["url"]},
                },
                pretty,
            )
            return
        print_result(
            {"running": False, "managed": True, "log_file": _find_latest_log_str()},
            pretty,
        )
        return
    pid = _find_pid_by_port(cfg["server"]["url"])
    if not pid:
        print_result({"running": False}, pretty)
        return
    print_result(
        {
            "running": True,
            "managed": False,
            "pid": pid,
            "uptime_seconds": _process_uptime_seconds(pid),
            "log_file": _find_latest_log_str(),
            "server": {"url": cfg["server"]["url"]},
        },
        pretty,
    )


def _launchd_kill(cfg: dict, pretty: bool) -> None:
    _launchctl("bootout", _launchd_target())
    killed_supervisord = _reap_legacy_supervisord()  # 逃生舱:残留老 supervisord 也清
    killed_backend: list[int] = []
    port_pid = _find_pid_by_port(cfg["server"]["url"])
    if port_pid and _is_miloco_backend_proc(port_pid):  # 只杀 miloco,不误杀用户进程
        _terminate(port_pid)
        killed_backend.append(port_pid)
    if _is_port_in_use(cfg["server"]["url"]):
        leftover = _find_pid_by_port(cfg["server"]["url"])
        if leftover and not _is_miloco_backend_proc(leftover):
            # 端口被非 miloco 进程占用:不误杀,明确提示(与 _launchd_start 口径一致)
            print_result(
                {
                    "error": f"端口 {cfg['server']['url']} 仍被非 miloco 进程占用 (pid={leftover})",
                    "hint": "该进程不是 miloco backend，未动它；请自行处理后重试",
                },
                pretty,
            )
            sys.exit(1)
        if not _has_port_lookup_tool():
            print_result(
                {
                    "error": f"端口仍被占用，且系统无 lsof / ss 可定位残留进程: {cfg['server']['url']}",
                    "hint": "请安装 lsof 后重试，或手动 kill 占用该端口的进程",
                },
                pretty,
            )
            sys.exit(1)
    print_result(
        {
            "code": 0,
            "message": "cleaned",
            "killed_backend": killed_backend,
            "killed_supervisord": killed_supervisord,
        },
        pretty,
    )


# ─── 命令定义 ────────────────────────────────────────────────────────────────


@click.group("service")
def service_group():
    """服务管理：启动 / 停止 / 重启 / 状态 / 日志。"""


@service_group.command("start")
@click.option("--foreground", is_flag=True, help="前台运行（不 daemonize）")
@click.option("--pretty", is_flag=True)
def service_start(foreground, pretty):
    """启动 Miloco Backend 服务。"""
    # macOS: 走 launchd（签名启动器绕过 LNP）。--foreground 仍直接 execvp 便于调试，
    # 但此时 python 以 euid 501 直跑、受 LNP 限，本地中枢连接不可用（仅供本地排障）。
    if _use_launchd() and not foreground:
        from miloco_cli.config import load_config

        _launchd_start(load_config(), pretty)
        return

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

        _wait_for_health(cfg, pretty, fatal_check=_supervisor_fatal)
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

    if _use_launchd():
        _launchd_stop(cfg, pretty, quiet)
        return

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
    if _use_launchd():
        from miloco_cli.config import load_config

        _launchd_restart(load_config(), pretty)
        return

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
        _wait_for_health(cfg, pretty, fatal_check=_supervisor_fatal)
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

    if _use_launchd():
        _launchd_status(cfg, pretty)
        return

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

    if _use_launchd():
        _launchd_kill(cfg, pretty)
        return

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
