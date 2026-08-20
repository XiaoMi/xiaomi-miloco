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
    """枚举所有加载本项目 supervisord.conf 的守护进程 PID（socket 失联也能找到）。

    ``-ww``：要匹配的是完整的 ``supervisord -c <conf 绝对路径>``，macOS 上非 tty
    输出默认截断到 80 列，会把 conf 路径切掉 → 迁移 reap 静默失效（同
    ``_is_miloco_backend_proc``）。
    """
    conf = str(_supervisor_conf())
    try:
        result = subprocess.run(
            ["ps", "-ww", "-axo", "pid=,command="],
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
    """查找 backend 日志文件(固定名,由 backend 自己写并按 10MB × 20 轮转)。

    历史上 daemon 模式用过 per-boot 时间戳名 ``miloco-backend_<ts>.log``；该形态
    自 launchd 迁移起（两平台均已改为 backend 自管日志）不再产生，仅为从旧版
    升级上来、目录里还留着旧文件的机器保留 fallback。
    """
    log_dir = _log_dir()
    if not log_dir.exists():
        return None
    supervised = log_dir / "miloco-backend.log"
    if supervised.exists():
        return supervised
    # 升级残留:旧版按 boot 时间戳命名的日志,新版不再产生
    candidates = sorted(log_dir.glob("miloco-backend_*.log"))
    return candidates[-1] if candidates else None


def _find_latest_log_str() -> str | None:
    log = _find_latest_log()
    return str(log) if log else None


def _resolve_timezone() -> str | None:
    """部署时区 IANA 名,注入进程管理器的环境让 backend 子进程继承。

    Linux 走 supervisord 的 ``environment=``；macOS 走 LaunchAgent plist 的
    ``EnvironmentVariables``（见 ``_generate_launchagent_plist``,同时写 ``TZ``
    与 ``MILOCO_TIMEZONE``）。

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


# launchd 不继承登录 shell 环境(实测只给 OSLogRateLimit / XPC_* / SSH_AUTH_SOCK /
# LOGNAME / USER / HOME / SHELL / TMPDIR 和最小 PATH 共 10 个)。supervisord 时代
# backend 是 shell 启动链的子孙进程、继承整套环境,以下两类因此会**静默失效**:
#   1. MILOCO_* —— settings 是 env_prefix="MILOCO_" + env_nested_delimiter="__",
#      `export MILOCO_MODEL__OMNI__API_KEY=…` 这类覆盖会悄悄回落到 config.json;
#   2. 代理 / 自定义 CA —— backend 代码里搜不到,但 httpx / requests 底层会读,
#      丢了以后表现为云端调用失败,很难联想到是环境变量没了。
# 故在生成 plist 时按白名单快照当前环境(语义对齐 supervisord "继承启动时环境")。
_ENV_PASSTHROUGH_EXACT = frozenset(
    {
        # 大小写两套都捞:httpx / requests 认小写,curl 等认大写。
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    }
)
_ENV_PASSTHROUGH_PREFIX = "MILOCO_"
# MILOCO_HOME 由 miloco_home() 算好后显式写入,无需也不该从环境二次取;
# MILOCO_SUPERVISED 更要挡死——shell 里若残留它(如调试),透传进去会让 backend
# 以为"外部管理者会轮转我的 stdout",跳过自管日志分支,又退回无轮转的老问题。
_ENV_PASSTHROUGH_DENY = frozenset({"MILOCO_HOME", "MILOCO_SUPERVISED"})


def _passthrough_env() -> dict[str, str]:
    """按白名单快照当前进程环境,供写入 plist 的 EnvironmentVariables。"""
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _ENV_PASSTHROUGH_DENY:
            continue
        if key in _ENV_PASSTHROUGH_EXACT or key.startswith(_ENV_PASSTHROUGH_PREFIX):
            out[key] = value
    return out


def _generate_launchagent_plist(cmd: list[str]) -> None:
    """写 ``~/Library/LaunchAgents/com.xiaomi.miloco.backend.plist``(内容变才写,idempotent)。

    ProgramArguments = 签名启动器 + cmd(=[python_bin, -m, miloco.main]);
    EnvironmentVariables = 白名单透传的宿主环境(见 ``_passthrough_env``)叠加显式项,
    显式项优先。显式项补的是 launchd 给不全的 ``PATH``(含 homebrew,后端要 shell 调裸
    ffmpeg)与 miloco 自身约定的 ``MILOCO_HOME`` / 时区。
    """
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path = _launchagent_plist()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    # launchd 只做 fd 重定向、不做轮转，所以这里**不能**声称 MILOCO_SUPERVISED
    # （那个标志的契约是"外部管理者会收集并轮转我的 stdout"，supervisord 成立、
    # launchd 不成立）。不设它 → backend 的 bootstrap 走 daemon 分支，自己接管
    # miloco-backend.log 并按 10MB × 20 轮转（对齐 supervisord 时代的上限）。
    #
    # StandardOutPath 因此只兜底 backend 完成 dup2 **之前**的输出：解释器启动失败、
    # 启动器自身的报错、import 期异常。
    #
    # "极少"只在后端能跑到 dup2 那一步时成立:bootstrap() 里 get_settings() 在
    # _redirect_stdio_to_file 之前,所以 config.json 被手改坏(多个逗号、枚举值拼错)
    # 会让后端在 import 期抛 pydantic ValidationError 并把 traceback 写进这个文件,
    # 然后 KeepAlive 按 launchd 的 ~10s 节流不停重拉 ≈ 17MB/天 —— 而
    # _launchd_crashloop_check 只在 service start/restart 的健康探测窗口内布防,
    # "启动成功之后才开始崩(比如改坏配置又重启了 mac)"没人再进那个窗口。
    # 正是本 PR 引为动机的那个失控形态,只是换了一个文件。故 _launchd_reload /
    # _launchd_stop 在 bootout 之后裁剪它(见 _trim_launchd_stdio)。
    launchd_stdio = str(log_dir / "launchd-stdio.log")

    # 白名单快照宿主环境打底,显式项覆盖其上(显式优先)。
    env = _passthrough_env()
    env.update(
        {
            "MILOCO_HOME": str(miloco_home()),
            # launchd 实测是给 HOME 的,这里显式固定是为了不依赖其默认行为
            # (也避免 plist 被搬到别的账号下时 HOME 跟着漂)。
            "HOME": str(Path.home()),
            # launchd 只给最小 PATH;补 homebrew(arm64 /opt/homebrew、intel /usr/local)
            # 供后端 shell 调裸 ffmpeg,并补 ~/.local/bin(uv 工具所在),避免丢失继承。
            "PATH": (
                f"/opt/homebrew/bin:/usr/local/bin:{Path.home()}/.local/bin"
                ":/usr/bin:/bin:/usr/sbin:/sbin"
            ),
        }
    )
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
        "StandardOutPath": launchd_stdio,
        "StandardErrorPath": launchd_stdio,
        "WorkingDirectory": str(miloco_home()),
    }
    data = plistlib.dumps(plist)
    if plist_path.exists() and plist_path.read_bytes() == data:
        # 内容没变也要收权限:升级上来的 plist 是旧版本按 umask 落的 0644。
        os.chmod(plist_path, 0o600)
        return
    # 原子写入:先写 .tmp 再 os.replace,避免中途 crash/断电留下损坏的 plist
    # (launchd 会尝试加载损坏文件)。
    tmp = plist_path.with_name(plist_path.name + ".tmp")
    tmp.write_bytes(data)
    # 0600:EnvironmentVariables 里是 _passthrough_env() 的快照,含 MILOCO_* 里的
    # 云端 API key(如 MILOCO_MODEL__OMNI__API_KEY)与可能带凭证的 HTTPS_PROXY。
    # write_bytes 按 umask 022 落成 0644,而 ~/Library/LaunchAgents 与 ~ 默认 755 →
    # 同机其他账号 plutil -p 就能读出来。日志(bootstrap / log_rotation,CodeQL #619)
    # 都已收紧到 0600,这份更敏感,不能例外。os.replace 不改已有权限,故在 tmp 上改。
    os.chmod(tmp, 0o600)
    os.replace(tmp, plist_path)


# 等 bootout 把 job 从 launchd 卸掉的预算。必须覆盖 launchd 的 ExitTimeOut
# (plist 未覆盖 → 默认 20s:SIGTERM → 等 20s → SIGKILL):后端要收感知管线、中枢
# 连接、在途请求,退出超过个位数秒是常态 —— 同文件 _launchd_stop 把 grace 提到 30s
# 正是这个理由,这里不能还停在 8s。循环一探到 job 消失就立刻继续,正常路径不变慢。
_LAUNCHD_BOOTOUT_WAIT_S = 35.0

_LAUNCHD_STDIO_KEEP_LINES = 200
_LAUNCHD_STDIO_TRIM_THRESHOLD = 1 << 20  # 1MB 以内不折腾


def _launchd_stdio_path() -> Path:
    """launchd 自己的 StandardOutPath/StandardErrorPath 落点（见 plist 生成处）。"""
    return _log_dir() / "launchd-stdio.log"


def _trim_launchd_stdio() -> None:
    """把 launchd 兜底 stdio 日志裁到最后 N 行，并收紧到 0600。

    为什么需要：这个文件没有任何写方轮转它。后端接管 fd **之前**就崩溃时
    （最现实的是手改坏 config.json → bootstrap 里 get_settings() 抛
    ValidationError），KeepAlive 会按 launchd 的 ~10s 节流不停重拉，每次追加一份
    traceback ≈ 17MB/天，而崩溃循环检测只在 start/restart 的健康探测窗口内布防。

    **只能在 bootout 完成、bootstrap 之前调用**：那是唯一没有进程持有该 fd 的
    窗口。job 还在跑时截断，能否安全取决于 launchd 是否以 O_APPEND 打开（未见
    文档保证），非 append 的写方会按旧偏移继续写、在文件里留一段空洞。

    裁剪不是硬上限（用户始终不跑 start/restart 就不会触发），但增长速率与
    「修配置必然要重启服务」这个事实配起来足够：真出问题的人一定会走到这里。
    """
    path = _launchd_stdio_path()
    if not path.exists():
        return
    try:
        # 权限先收:本 PR 其余日志产物(轮转后日志、重定向后的 stdio、plist)都是
        # 0600,这个文件由 launchd 按 umask 创建通常是 0644,是唯一缺口。
        # 放在体积判断之前,免得小文件永远收不到权限。
        os.chmod(path, 0o600)
        if path.stat().st_size <= _LAUNCHD_STDIO_TRIM_THRESHOLD:
            return
        tail = _tail_lines(path, _LAUNCHD_STDIO_KEEP_LINES)
        # write_text 是 O_TRUNC 原地截断,不换 inode,所以上面 chmod 的结果保持有效。
        path.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except OSError:
        pass  # 兜底日志的裁剪失败不该阻断 start / stop / restart


def _launchd_reload() -> tuple[bool, str]:
    """bootout(容错)+ 等 launchd 拆卸完成 + bootstrap 重新加载（picks up 重写后的 plist）。

    直接 bootout 后立刻 bootstrap 会撞 launchd 拆卸竞态,报
    ``Bootstrap failed: 5: Input/output error``。故 bootout 后轮询到 job 真正从
    launchd 消失再 bootstrap,并对该竞态做有限重试。

    等待超时视为**明确失败**(返回 False),不再往下 bootstrap:那之后的每一步都建立在
    "job 已消失"这个前提上,详见下面 `if _launchd_is_loaded()` 处的注释。
    """
    target = _launchd_target()
    _launchctl("bootout", target)  # 未加载时报错,忽略
    # 等 job 从 launchd 完全消失,避免 bootstrap 撞拆卸竞态。
    deadline = time.time() + _LAUNCHD_BOOTOUT_WAIT_S
    while time.time() < deadline and _launchd_is_loaded():
        time.sleep(0.3)
    if _launchd_is_loaded():
        # 拆卸没结束就往下走会连错三步:
        #   ① 在 launchd 仍持有 fd 时截断 launchd-stdio.log(_trim_launchd_stdio
        #      的前置条件就是"没人持有该 fd");
        #   ② bootstrap 必撞 EIO;
        #   ③ 下面那条竞态兜底 `if _launchd_is_loaded(): return True` 会把**旧 job
        #      还没卸完**误判成"新 job 已加载"—— launchctl print 对"卸载中"的 job
        #      同样返回 0,两种状态在这个函数里不可区分。
        # 后果是 restart 变成 stop:新 plist 从未被 bootstrap,旧实例随后退出,
        # _wait_for_health 白等 30s 报"启动超时",而后端日志里什么异常都没有
        # (它是被正常 SIGTERM 收走的),排查方向被彻底带偏。宁可明确失败。
        return False, (
            f"旧 job 在 {_LAUNCHD_BOOTOUT_WAIT_S:.0f}s 内未从 launchd 卸载,"
            "已放弃 bootstrap;请稍后重试 miloco-cli service restart"
        )
    # job 已从 launchd 消失,此刻没人持有 launchd-stdio.log 的 fd → 可以安全裁剪
    # (bootstrap 之后再裁就可能撞上持有旧偏移的写方)。
    _trim_launchd_stdio()
    err = ""
    for _ in range(5):
        result = _launchctl("bootstrap", _launchd_domain(), str(_launchagent_plist()))
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout).strip()
        # 上面已确认旧 job 卸载完毕,故此刻 loaded 只可能是"本次 bootstrap 报错但
        # 实际生效"(拆卸/加载竞态)→ 视作成功。
        if _launchd_is_loaded():
            return True, ""
        time.sleep(0.6)
    return False, err


def _check_lnp_blocked(pretty: bool) -> None:
    """后端已起，但若近期日志里有 Errno 65 则说明 macOS LNP 仍拦着中枢连接——给用户
    一条可操作的提示，告诉他们去哪打开授权开关。

    "近期" = 日志文件 mtime 在 60s 内（真 epoch，抗时区）且该行落在日志尾部时间戳
    的 15s 窗口内（相对比较，抗 plist 注入的 TZ 与宿主时区不一致）。不阻塞。"""
    log_dir = _log_dir()
    log_file = log_dir / "miloco-backend.log"
    if not log_file.exists():
        return
    try:
        lines = _tail_lines(log_file, 400)
        # 第一道:「日志本身是否新鲜」用 mtime 判(真 epoch,与任何时区无关)。下面的
        # 相对窗口只看行与行的间隔,单独用它会把一小时前停机留下的尾部 Errno 65 也
        # 算成"刚刚发生"。
        if time.time() - log_file.stat().st_mtime > 60:
            return
        # 第二道:窗口基准取**日志自己最后一行**的时间戳,而不是 time.time()。plist
        # 显式注入 TZ/MILOCO_TIMEZONE,后端时间戳按注入时区打;CLI 侧 _extract_log_ts
        # 用 time.mktime 按 CLI 进程本地时区解析。两者不一致时相差整小时数 —— 要么
        # Errno 65 行全部落在窗外(提示在最需要它的场景里永远不出现),要么旧行被误判
        # 成近期。相对比较与宿主时钟、注入 TZ 都解耦。
        stamps = [ts for ts in (_extract_log_ts(ln) for ln in lines) if ts > 0]
        if not stamps:
            return
        cutoff = max(stamps) - 15
        hits = [
            ln for ln in lines if "Errno 65" in ln and _extract_log_ts(ln) >= cutoff
        ]
    except Exception:
        return
    if not hits:
        return
    # padding 按**终端显示宽度**算(East Asian Wide/Fullwidth 占 2 列,箭头 → 是
    # Neutral 占 1 列),不是按字符数。改动这三行文案时要重算,否则右边框会歪:
    #   61 - display_width(内容) = 该行补的空格数
    box = (
        "\n"
        + "┌" + "─" * 61 + "┐\n"
        "│  macOS LNP 正在拦截中枢连接 (Errno 65)" + " " * 22 + "│\n"
        "│" + " " * 61 + "│\n"
        "│  → 系统设置 → 隐私与安全性 → 本地网络" + " " * 23 + "│\n"
        "│     找到 miloco 并打开开关" + " " * 34 + "│\n"
        "└" + "─" * 61 + "┘"
    )
    if pretty:
        print(box)
    else:
        # 调用点在 print_result(...) 之后,非 pretty 模式的 stdout 是给
        # openclaw / hermes 解析的 JSON;这个 ASCII 框打到 stdout 会变成
        # json.loads 解析不了的噪声。走 stderr:既不污染机器可读输出,
        # 又不丢掉这条对人有用的提示。
        print(box, file=sys.stderr)


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

    ``-ww`` 不可省:macOS 的 BSD ps 在 stdout 不是 tty 时(``capture_output=True``
    就是管道)把 command 截断到 80 列,只有 -ww 解除。安装器写进 plist 的
    python_bin 是 uv tool 的 venv 解释器(``/Users/<name>/.local/share/uv/tools/
    miloco/bin/python``),加上 ``-m miloco.main`` 约 77-85 列 —— 用户名稍长就把
    ``-m miloco.main`` 截掉,谓词恒 False,按端口清理残留的能力在默认安装路径上直接
    失效(方向仍安全:只会漏杀,不会误杀)。
    """
    import re

    try:
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
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
                # 面向真实用户给可操作路径:重跑安装器是普通用户唯一能做的动作。
                # 原提示只指了 scripts/sync-to-remote.sh —— 那是开发者部署脚本,
                # 装过发布包的用户手上没有它,照着做只会更困惑。
                "hint": (
                    "macOS 后端必须由签名启动器拉起（否则拿不到局域网权限）。"
                    "请重跑安装器补齐 miloco.app："
                    # 必须是 install.sh:release 产物里没有独立可下载的 install.py
                    # (build.sh 把它 base64 内嵌进 install.sh),且这是仓库其它
                    # 各处(install-guide-openclaw.md / install-hermes.sh /
                    # release.yml)统一的写法。
                    "curl -LsSf https://github.com/XiaoMi/xiaomi-miloco/releases"
                    "/latest/download/install.sh | bash"
                    "（开发部署用 scripts/sync-to-remote.sh）。"
                    "若安装时看到过「签名启动器包缺失」告警，则是发布包漏打了 launcher"
                    "，需换一个完整的发布包。"
                ),
            },
            pretty,
        )
        sys.exit(1)


# 与 _launchd_stop 同口径:对齐 supervisord 时代 stopwaitsecs=30。后端自身的关停
# 序列里 perception stop_engine 单步预算就是 10s(见 main.py lifespan),之后还排着
# dispatcher / poller / metrics_client(drain queue)/ event_log,默认 6s 必然在
# 中途 SIGKILL,留半截清理并丢掉最后一段 trace。
_MILOCO_STOP_GRACE_S = 30.0


def _cleanup_stale_port_holder(
    cfg: dict, pretty: bool, grace: float = _MILOCO_STOP_GRACE_S
) -> None:
    """启动前端口清理：只杀可证明属于 miloco 的残留进程，否则报 port-in-use 退出。

    抽成函数是为了让 ``restart`` 和 ``start`` 走同一段逻辑：旧 python 对 SIGTERM
    无响应、超过 launchd ExitTimeOut 被 SIGKILL 成孤儿占着端口时，只有 start 能自
    愈而 restart 直接 crashloop 报错 —— 语义更强的命令反而更脆，恢复路径不对称。
    """
    port_pid = _find_pid_by_port(cfg["server"]["url"])
    if port_pid and _is_miloco_backend_proc(port_pid):
        _terminate(port_pid, grace=grace)
    if _is_port_in_use(cfg["server"]["url"]):
        print_result(
            {"code": 1, "message": f"port already in use: {cfg['server']['url']}"},
            pretty,
        )
        sys.exit(1)


def _launchd_start(cfg: dict, pretty: bool) -> None:
    pid = _launchd_backend_pid()
    if pid:
        print_result({"code": 1, "message": f"already running (pid={pid})"}, pretty)
        sys.exit(1)
    # 前置校验必须跑在破坏性拆卸之前:这两步任一失败都会 sys.exit(1),此时旧
    # supervisord/后端还活着,用户停在「老版本仍在跑」而不是「服务被停且起不来」。
    _launcher_ready_or_exit(pretty)
    cmd = _server_cmd_or_exit(pretty)
    _reap_legacy_supervisord()  # 老 supervisord 版升级迁移:先清残留再起 launchd
    # 兜底：supervisord 被 SIGKILL 后其子 backend 可能仍占端口。只清理可证明
    # 属于 miloco 的残留进程(命令行含 -m miloco.main)，绝不碰任意用户进程。
    _cleanup_stale_port_holder(cfg, pretty)
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
    # 等 job 真正从 launchd 消失,复用 _launchd_reload 的等待预算:bootout 只是
    # 发出拆卸请求,backend 收尾(感知管线/中枢连接/在途请求)可能耗时到 30s 量级,
    # 期间它早已释放监听 socket ——若这里不等、立刻查端口,_find_pid_by_port 会
    # 提前返回 None,让下面的残留清理整段跳过,stop 就在 job 还没卸完时误报
    # "stopped"(调用方紧接着 start 会撞见旧 job 仍在,见 _launchd_start 的
    # already-running 分支)。超时仍未卸载也不阻断:继续走残留清理兜底。
    deadline = time.time() + _LAUNCHD_BOOTOUT_WAIT_S
    while time.time() < deadline and _launchd_is_loaded():
        time.sleep(0.3)
    unloaded = not _launchd_is_loaded()
    # 兜底:bootout 后仍在监听端口的残留进程——只清理可证明属于 miloco 的
    # (命令行含 -m miloco.main),绝不误杀用户其它服务(与 _launchd_start 一致)。
    port_pid = _find_pid_by_port(cfg["server"]["url"])
    if port_pid and _is_miloco_backend_proc(port_pid):
        # 复用 _MILOCO_STOP_GRACE_S(与 _cleanup_stale_port_holder 同一个常量),
        # 避免两处各写一份 30.0 字面量、将来改一处漏改另一处。理由同该常量注释:
        # bootout 的 SIGTERM 经 launcher 转发到 python 后,这里紧接着再发一次
        # SIGTERM,backend 要收感知管线、中枢连接、在途请求,6s 默认值必然中途 SIGKILL。
        _terminate(port_pid, grace=_MILOCO_STOP_GRACE_S)
    if _is_port_in_use(cfg["server"]["url"]) and not _has_port_lookup_tool():
        print_result(
            {
                "error": f"端口仍被占用，且系统无 lsof / ss 可定位残留进程: {cfg['server']['url']}",
                "hint": "请安装 lsof 后重试，或手动 kill 占用该端口的进程",
            },
            pretty,
        )
        sys.exit(1)
    # 只有 job 真从 launchd 消失(= 没人持有那个 fd)才裁剪,与 _trim_launchd_stdio
    # 自己写明的前置条件一致:job 还在跑时裁,launchd 是否以 O_APPEND 打开未见文档
    # 保证,非 append 的写方会按旧偏移继续写、在文件里留一段 NUL 空洞 —— 超时分支
    # 下 launchd 可能仍持着 fd,不该无条件裁。顺手裁一次是因为崩溃循环中的用户很
    # 可能先 `service stop` 止血再去改配置,不该让日志在那期间一直躺着占几百 MB。
    if unloaded:
        _trim_launchd_stdio()
    else:
        print(
            f"警告:job 未在 {_LAUNCHD_BOOTOUT_WAIT_S:.0f}s 内卸载,"
            "跳过 launchd-stdio.log 裁剪(避免截断到仍持有 fd 的写方)",
            file=sys.stderr,
        )
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
    # 同 start:前置校验先行,拆掉活实例之前先确认新实例起得来。
    _launcher_ready_or_exit(pretty)
    cmd = _server_cmd_or_exit(pretty)
    _reap_legacy_supervisord()  # 升级迁移:先清残留 supervisord,免其 backend 占端口
    # 与 start 同一段清理,恢复路径必须对称。但只在 launchd 手上**没有**活着的
    # backend 时才做:有活实例时端口正是它在占,该由下面 _launchd_reload 的 bootout
    # 按 ExitTimeOut 优雅收走,而不是在这里先 SIGTERM/6s 后 SIGKILL 掐掉。要清的是
    # 「job 还挂着但进程已被 SIGKILL 成孤儿、端口没释放」那种残留。
    if _launchd_backend_pid() is None:
        _cleanup_stale_port_holder(cfg, pretty)
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
        # -F 而非 -f:主日志现在是 rename 式轮转(每 10MB 一次,log_rotation.py)。
        # -f 跟随的是已打开的 fd,轮转后 tail 继续读被改名的旧 inode,新文件的输出
        # 再也看不到 —— 用户盯着的「实时日志」静默冻结。-F 按**名字**跟随并在文件
        # 被替换时重开(BSD/GNU tail 都支持)。
        cmd.append("-F")
    cmd.append(str(latest))

    os.execvp("tail", cmd)
