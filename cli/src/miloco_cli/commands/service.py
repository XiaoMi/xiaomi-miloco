"""service 命令组：start / stop / restart / status / logs"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import click

from miloco_cli.config import atomic_write_text, miloco_home
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


# ─── 内存分配器（jemalloc 预加载） ───────────────────────────────────────────
#
# backend 长跑 RSS 单调涨到 2GB+ 的根因是 glibc malloc 的 arena 碎片，换 jemalloc 能把空闲页
# 真正还给系统。Python 层换不掉分配器，唯一的进程级方式是 LD_PRELOAD，且必须在进程启动前设好，
# 所以落点在这里生成的 supervisord.conf。
#
# 第一优先级是 backend 能起来，jemalloc 只是优化：下面每一步失败都退回"什么都不注入"。

_JEMALLOC_FILE_NAMES = ("libjemalloc.so.2", "libjemalloc.so")

# background_thread 把归还内存的 madvise 挪出解码线程热路径，两个 decay 让空闲页 5 秒后还给
# 系统。5000ms 来自真机 glibc / mimalloc / jemalloc 三方对比：再短会让解码期的分配-释放抖动
# 频繁 madvise，再长则空闲内存降不下去。
_JEMALLOC_MALLOC_CONF = "background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000"

# 出现在 environment= 行里就会让 supervisord 起不来的字符，理由见 _is_conf_safe。
_CONF_HOSTILE_CHARS = frozenset('"%\n\r')

_ARCH_LIB_DIR_NAMES = {
    "x86_64": "x86_64-linux-gnu",
    "amd64": "x86_64-linux-gnu",
    "aarch64": "aarch64-linux-gnu",
    "arm64": "aarch64-linux-gnu",
}

_SYSTEM_LIB_DIRS = (Path("/usr/lib64"), Path("/usr/local/lib"), Path("/usr/lib"))

# 实测(x86_64 开发机)：探针子进程 18ms、问自带那份的路径 28ms，3s 是百倍余量。
# 系统候选共用一份预算、自带那份单独一份：共用一条总预算的话，前面一两个卡住的系统库就能把
# 预算吃光，让"系统没装 jemalloc 时唯一可用的那份"永远探不到。
_PROBE_TIMEOUT_S = 3
_BUNDLED_LOOKUP_TIMEOUT_S = 3
_SYSTEM_PROBE_BUDGET_S = 5
_MIN_PROBE_SLICE_S = 0.5

# 在 backend 解释器里跑，读 mallctl 实证 jemalloc 是否真接管了 malloc、旋钮是否真生效。
# 契约：成功打 5 行 ver=/page=/bg=/dirty=/muzzy=，否则单行 not-taken-over 或 probe-crashed:。
# 业务逻辑整个包在 probe() 里并兜住异常，是为了不让自己的 traceback 混进 stderr 判据。
_PROBE_SCRIPT = '''\
def probe():
    import ctypes

    try:
        mallctl = ctypes.CDLL(None).mallctl     # jemalloc 没接管 → 取不到这个符号
    except (AttributeError, OSError):
        return "not-taken-over"
    mallctl.restype = ctypes.c_int

    def read(name, ctype):                      # 返回 None = 这个旋钮不存在
        val = ctype()
        size = ctypes.c_size_t(ctypes.sizeof(val))
        if mallctl(name.encode(), ctypes.byref(val), ctypes.byref(size), None, 0):
            return None
        return val.value

    version = read("version", ctypes.c_char_p)
    return "\\n".join([
        "ver=" + (version.decode("ascii", "replace").split("-")[0] if version else "?"),
        "page=" + str(read("arenas.page", ctypes.c_size_t)),
        "bg=" + str(read("background_thread", ctypes.c_bool)),
        "dirty=" + str(read("arenas.dirty_decay_ms", ctypes.c_ssize_t)),
        "muzzy=" + str(read("arenas.muzzy_decay_ms", ctypes.c_ssize_t)),
    ])


try:
    out = probe()
except Exception as e:
    out = "probe-crashed: %r" % (e,)
print(out)
'''


@dataclass(frozen=True)
class _ProbeResult:
    """一次预加载探针的结果。``fatal`` 非空表示这份 .so 用不了，其余字段是读回的实况。"""

    fatal: str | None = None
    version: str = "unknown"
    page: int | None = None
    background_thread: bool | None = None
    dirty_decay_ms: int | None = None
    muzzy_decay_ms: int | None = None


def _system_lib_dirs() -> list[Path]:
    """系统库目录，多架构 triplet 目录排最前（Debian/Ubuntu 把库放那儿）。

    triplet 用硬编码映射而不是 ``sysconfig.get_config_var("MULTIARCH")``：后者是 CLI 解释器的
    编译期值，CLI 装在自己的 venv 里、解释器可能不是发行版的，取不到发行版的实际布局。
    映射里没有的架构（armv7 / riscv64 等）跳过这一项，其余目录照走。
    """
    dirs = list(_SYSTEM_LIB_DIRS)
    if arch_dir_name := _ARCH_LIB_DIR_NAMES.get(platform.machine().lower()):
        dirs.insert(0, Path("/usr/lib") / arch_dir_name)
    return dirs


# 在 backend 解释器里跑，算出自带那份 .so 的路径。未知架构（armv7 / riscv64 等）打空串，
# 让调用方按"拿不到"处理 —— 与 _system_lib_dirs 对未知架构"跳过这一项"同一个口径。二选一会
# 让 armv7 拿到 x86_64 那份的路径，而那个文件在源码树里真实存在，于是白起一次探针、再被
# ld.so 以 wrong ELF class 打回来，还多一行看着像故障的告警。
_BUNDLED_PATH_SCRIPT = (
    "import pathlib, platform, miot;"
    "arch = {'x86_64': 'x86_64', 'amd64': 'x86_64',"
    " 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower(), '');"
    "print(pathlib.Path(miot.__file__).parent / 'libs' / 'linux' / arch"
    "      / 'libjemalloc.so.2' if arch else '')"
)


def _bundled_jemalloc(backend_python: str) -> Path | None:
    """问 backend 解释器要它自带的 libjemalloc.so.2（随平台归档分发的那个）。

    CLI 装在自己的 venv 里、``import miot`` 拿不到，只能让 backend 的解释器算路径。
    这一步只读 stdout 和退出码，不像预加载探针那样在意 stderr 有没有噪声。
    拿不到（miot 没装 / 归档里没带 / 解释器起不来）返回 None。
    """
    try:
        result = subprocess.run(
            [backend_python, "-c", _BUNDLED_PATH_SCRIPT],
            capture_output=True,
            text=True,
            timeout=_BUNDLED_LOOKUP_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    so_path = Path(result.stdout.strip())
    return so_path if so_path.is_file() else None


def _jemalloc_candidates(
    backend_python: str | None = None,
) -> Iterator[tuple[Path, bool]]:
    """惰性产出候选 ``(.so 路径, 是否自带那份)``，用哪个由调用方探针后决定。

    **系统那份优先**：真机三方对比里"SBC 最优 = jemalloc"这个结论用的就是 apt 那份，让自带那份
    抢在前面等于把结论建立在一个没被测过的库上；次要理由是发行版维护 + 安全更新 + 不增加平台
    包体积。**不是**"编译参数更贴合内核"——查过四家发行版的打包配方，aarch64 一律按 64K 页编
    （``--with-lg-page=16``），自带那份按同样参数编，两者粒度一致。

    按 ``resolve()`` 去重：Arch 上 ``/usr/lib64`` 是指向 ``lib`` 的符号链接、``.so`` 是指向
    ``.so.2`` 的符号链接，四条路径会解析到同一个文件，不去重就要白跑探针、白花预算。

    自带那份排最后且惰性：只有前面全都没通过，才起子进程问它的路径。
    """
    seen: set[Path] = set()
    for lib_dir in _system_lib_dirs():
        for file_name in _JEMALLOC_FILE_NAMES:
            so_path = lib_dir / file_name
            if not so_path.is_file():
                continue
            real = so_path.resolve()
            if real in seen:
                continue
            seen.add(real)
            yield so_path, False
    if backend_python and (bundled := _bundled_jemalloc(backend_python)):
        if bundled.resolve() not in seen:
            yield bundled, True


def _preload_value(so_path: Path) -> str:
    """``LD_PRELOAD`` 的值：我们的放最前（要它接管 malloc），环境里原有的追加在后。

    纯拼值不告警——探针对每个候选都会调一次，在这里 echo 会把同一句重复打 N 遍。原有那段
    被丢掉的提醒由 :func:`_warn_on_dropped_preload` 在真要注入时打一次。

    原有那段含危险字符时只是**不追加**，不影响我们这一份：它写进 conf 会让 supervisord
    拒绝加载，但那是别人的库，没道理连累 jemalloc。``so_path`` 自身的字符检查在
    :func:`_probe_jemalloc` 入口，那里能顺着候选链换下一个。
    """
    existing = os.environ.get("LD_PRELOAD", "").strip()
    if existing and _is_conf_safe(existing):
        return f"{so_path}:{existing}"
    return str(so_path)


def _warn_on_dropped_preload() -> None:
    """原有 ``LD_PRELOAD`` 因为含危险字符没被追加时，提醒一次。"""
    existing = os.environ.get("LD_PRELOAD", "").strip()
    if existing and not _is_conf_safe(existing):
        click.echo(
            'warning: 环境里已有的 LD_PRELOAD 含有 " % 或换行，追加进 supervisord.conf 会让 '
            "supervisord 拒绝加载配置，本次只预加载 jemalloc、不追加它",
            err=True,
        )


def _stderr_tail(stderr: str, limit: int = 200) -> str:
    """探针 stderr 里最有诊断价值的那一行，拼进失败原因用。不参与任何判定。"""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    return f"；stderr: {lines[-1][:limit]}" if lines else ""


def _as_int(raw: str | None) -> int | None:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_bool(raw: str | None) -> bool | None:
    return {"True": True, "False": False}.get(raw or "")


def _probe_jemalloc(
    so_path: Path,
    malloc_conf: str,
    backend_python: str | None,
    timeout: float,
) -> _ProbeResult:
    """真拿这套 LD_PRELOAD + MALLOC_CONF 起一个进程，读 mallctl 实证接管情况。

    **判据只有一条：jemalloc 有没有接管 malloc**，由 stdout 回答。stderr 一概不看——
    接管成功后它打什么都不改变"能用"这个事实（不认识的旋钮、qemu 下 MADV_DONTNEED 退化成
    memset、环境里别人的 LD_PRELOAD 条目加载失败，全属此类）；而接管失败的每一种（页大小
    不符、坏 .so、根本不是 jemalloc）都会让 stdout 拿不到 ``ver=``。拿 stderr 做判据要维护
    一张永远补不完的良性输出白名单，漏一条的后果是库在正常工作却被判死。

    只报事实，判定和告警在 :func:`_pick_jemalloc` / :func:`_resolve_malloc_env`。

    用 **backend 的解释器**而不是 CLI 自己的：两个解释器的 libc 版本、链接方式都可能不同，
    "CLI 能被接管"推不出"backend 能被接管"，而后者才是要保护的目标。

    ``-E -S`` 屏蔽掉环境和 site 目录：sitecustomize / .pth 里的东西可能自己就崩掉或改写
    分配器，那样测的就不是这份 .so 了。

    **它不保证 backend 里一定没问题**——探针只加载 libc，不加载 libav / onnxruntime；
    只在特定分配模式下才触发的问题只能靠真机跑出来。
    """
    # 写不进 supervisord.conf 的路径等于用不了，和加载失败同一个出口：候选链换下一个，
    # 绝对路径那条也会照常打出真实原因。放在探针入口，所有来源（候选链 / MILOCO_MALLOC /
    # 自带那份）就都被这一处覆盖，不用每个来源各查一遍。
    if not _is_conf_safe(str(so_path)):
        return _ProbeResult(
            fatal='路径含有 " % 或换行，写进 supervisord.conf 会让 supervisord 拒绝加载配置'
        )
    env = {
        **os.environ,
        "LD_PRELOAD": _preload_value(so_path),
        "MALLOC_CONF": malloc_conf,
    }
    try:
        result = subprocess.run(
            [backend_python or sys.executable, "-E", "-S", "-c", _PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _ProbeResult(fatal=f"探针超过 {timeout:.1f}s 未返回")
    except OSError as e:
        return _ProbeResult(fatal=f"探针无法执行: {e}")

    # 被信号打死时 returncode 为负（-signal），正常退出非 0 则是进程自己的退出码。
    if result.returncode < 0:
        return _ProbeResult(
            fatal=f"探针被信号 {-result.returncode} 打死{_stderr_tail(result.stderr)}"
        )
    if result.returncode != 0:
        return _ProbeResult(
            fatal=f"探针以退出码 {result.returncode} 结束{_stderr_tail(result.stderr)}"
        )

    stdout = result.stdout.strip()
    if stdout == "not-taken-over":
        # ld.so 对加载不了的预加载库是"打一行 ERROR 然后忽略、程序照常跑"，退出码仍是 0，
        # 所以这条静默降级只有靠 mallctl 符号取不到才抓得住。
        return _ProbeResult(
            fatal="jemalloc 没有接管 malloc（预加载被忽略，或这不是 jemalloc）"
            + _stderr_tail(result.stderr)
        )
    if stdout.startswith("probe-crashed:"):
        return _ProbeResult(fatal=f"探针自身异常：{stdout}")

    # 页大小不符这类故障在 jemalloc 初始化期就炸，探针脚本一行都没执行到，stdout 是空的；
    # 而退出码不保证非 0（release 构建 opt_abort 默认 false）。没有这条判据，意料外的输出
    # 会默认落到"通过"。
    values = dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)
    if "ver" not in values:
        # stderr 不参与判定，但附进失败原因里：页大小不符时 jemalloc 的原话
        # （"Unsupported system page size"）比"输出不符合契约"好排查得多。
        return _ProbeResult(fatal=f"探针输出不符合契约: {stdout!r}{_stderr_tail(result.stderr)}")

    return _ProbeResult(
        version=values["ver"] if values["ver"] != "?" else "unknown",
        page=_as_int(values.get("page")),
        background_thread=_as_bool(values.get("bg")),
        dirty_decay_ms=_as_int(values.get("dirty")),
        muzzy_decay_ms=_as_int(values.get("muzzy")),
    )


def _pick_jemalloc(
    backend_python: str | None, malloc_conf: str
) -> tuple[Path, _ProbeResult] | None:
    """按候选链逐个探针，返回第一个可用的 ``(.so, 探针结果)``；全不可用返回 None。"""
    system_deadline = time.monotonic() + _SYSTEM_PROBE_BUDGET_S
    budget_reported = False
    for so_path, is_bundled in _jemalloc_candidates(backend_python):
        timeout = float(_PROBE_TIMEOUT_S)
        if not is_bundled:
            # deadline 只管得住"要不要再起一个"，管不住"已经起的这个能跑多久"，所以单次超时
            # 也要收进剩余预算，否则最后一个候选能把总预算打穿。
            remaining = system_deadline - time.monotonic()
            if remaining < _MIN_PROBE_SLICE_S:
                if not budget_reported:
                    budget_reported = True
                    click.echo(
                        "warning: 探测 libjemalloc 超过时间预算，跳过剩余的系统候选",
                        err=True,
                    )
                continue
            timeout = min(timeout, remaining)
        probe = _probe_jemalloc(so_path, malloc_conf, backend_python, timeout)
        if probe.fatal is None:
            return so_path, probe
        # 不说"试下一个候选"：最后一个失败时后面已经没有了。链路全灭的结论由调用方给。
        click.echo(f"warning: {so_path} 不可用（{probe.fatal}）", err=True)
    return None


def _malloc_env_pairs(
    so_path: Path, probe: _ProbeResult, malloc_conf: str
) -> list[tuple[str, str]]:
    """组装要注入的环境变量，并把读回的实况打出来。

    旋钮全部原样注入，不因为"这份库不认识某个旋钮"做任何加工：jemalloc 逐个旋钮独立解析，
    被拒的那个不影响其它旋钮生效，它自己会在 backend 日志里打 ``Invalid conf pair``。

    读回值直接照打，不替它分类。``dirty/muzzy_decay_ms=None/None`` 摆在那儿就是"这份库
    没有这两个旋钮"，再写几段文案去解释它属于哪一态，是给一个锦上添花的优化项加不必要的
    判断分支。
    """
    click.echo(
        f"分配器: jemalloc {probe.version} ({so_path}) page={probe.page}  "
        f"background_thread={probe.background_thread} "
        f"dirty/muzzy_decay_ms={probe.dirty_decay_ms}/{probe.muzzy_decay_ms}",
        err=True,
    )
    _warn_on_dropped_preload()
    return [("LD_PRELOAD", _preload_value(so_path)), ("MALLOC_CONF", malloc_conf)]


def _safe_mode_enabled() -> bool:
    """读 ``safe_mode``（``MILOCO_SAFE_MODE`` 环境变量能临时覆盖）。读不出来当没开。"""
    from miloco_cli.config import load_config

    try:
        return bool(load_config().get("safe_mode", False))
    except Exception:
        return False


def _is_conf_safe(value: str) -> bool:
    """值里不含会让 supervisord 拒绝加载配置的字符。

    三种字符，后果都是 supervisord 起不来（比 jemalloc 本身危险得多），逐个实测过：

    - ``"`` —— 提前闭合引号，报 ``Unexpected end of key/value pairs``
    - 换行 —— 报 ``No closing quotation``
    - ``%`` —— supervisord 加载时对这一行做一次百分号展开（``options.py::expand`` 走
      ``s % expansions``），落单的 ``%`` 报 ``badly formatted``

    **逗号不在此列**：值用双引号包住后 supervisord 能正确解析引号内的逗号，实测值不会被
    拆坏。这一条不是可有可无的保守——``MALLOC_CONF`` 的合法值本身就是逗号分隔的，把逗号
    拒掉会让默认旋钮串一个都注入不进去。
    """
    return not set(value) & _CONF_HOSTILE_CHARS


def _resolve_malloc_env(backend_python: str | None = None) -> list[tuple[str, str]]:
    """决定给 backend 注入哪套分配器环境变量（``LD_PRELOAD`` / ``MALLOC_CONF``）。

    判定顺序：非 Linux → 安全模式 → ``MILOCO_MALLOC`` → 候选链。

    ``MILOCO_MALLOC`` 的取值语义：

    - 未设置 / ``jemalloc`` —— 走候选链（系统那份优先，自带那份兜底）。区别只在找不到时：
      未设置是静默（没装 libjemalloc2 是常态），显式写了才告警
    - ``glibc`` —— 什么都不注入，按 glibc 默认起
    - ``.so`` 的绝对路径 —— 只试这一个，不进候选链
    - 其它取值 —— 告警并按不注入处理

    任何一步不成都返回空列表，也就是什么都不注入、按 glibc 默认起 —— backend 能起来的
    优先级高于换分配器。

    macOS 上 ``LD_PRELOAD`` 本就无效（该是 ``DYLD_INSERT_LIBRARIES`` 且受 SIP 限制），
    所以非 Linux 在最前面直接返回。
    """
    if sys.platform != "linux":
        return []

    choice = os.environ.get("MILOCO_MALLOC", "").strip()

    # 安全模式赢过 MILOCO_MALLOC 的一切取值：它的语义是"我遇到问题了"，不该被更细的设置推翻。
    if _safe_mode_enabled():
        click.echo(
            "分配器: 安全模式已开启，不预加载 jemalloc；"
            "关闭用 miloco-cli config set safe_mode false",
            err=True,
        )
        if choice and choice != "glibc":
            click.echo(
                f"warning: 安全模式已开启，MILOCO_MALLOC={choice} 被忽略", err=True
            )
        return []

    if choice == "glibc":
        return []

    # 空串按"没设"处理，和上面 MILOCO_MALLOC 同一个口径：os.environ.get 的默认值只在 key
    # 不存在时才给，而 `export MALLOC_CONF=` / Dockerfile 的 `ENV MALLOC_CONF=` 会让它返回
    # 空串——那样三个旋钮一个都注入不进去，日志却完全看不出异常。
    malloc_conf = os.environ.get("MALLOC_CONF", "").strip() or _JEMALLOC_MALLOC_CONF
    # 沿用用户的 MALLOC_CONF 意味着它会原样进 conf，和绝对路径一样要过这道闸。
    if not _is_conf_safe(malloc_conf):
        click.echo(
            "warning: MALLOC_CONF 含有会写坏 supervisord.conf 的字符，改用默认旋钮",
            err=True,
        )
        malloc_conf = _JEMALLOC_MALLOC_CONF

    if not choice or choice == "jemalloc":
        picked = _pick_jemalloc(backend_python, malloc_conf)
        if picked is None:
            # 没装 libjemalloc2 是常态，默认路径静默回退即可；用户显式点名却没给上才必须说。
            if choice:
                click.echo(
                    "warning: 找不到可用的 libjemalloc，本次不改分配器；"
                    "Debian/Ubuntu 装法：apt install libjemalloc2",
                    err=True,
                )
            return []
    elif choice.startswith("/"):
        # 绝对路径只有一份可试，不进候选链——用户点名了这一份，探不通就该说"这一份不行"，
        # 而不是接着去试别的。路径里的危险字符由探针入口统一挡，会照常打出真实原因。
        so_path = Path(choice)
        if not so_path.is_file():
            click.echo(
                f"warning: MILOCO_MALLOC 指定的 {so_path} 不存在，本次不改分配器", err=True
            )
            return []
        probe = _probe_jemalloc(so_path, malloc_conf, backend_python, _PROBE_TIMEOUT_S)
        if probe.fatal:
            click.echo(
                f"warning: MILOCO_MALLOC 指定的 {so_path} 不可用（{probe.fatal}），"
                "本次不改分配器",
                err=True,
            )
            return []
        picked = (so_path, probe)
    else:
        click.echo(
            f"warning: 无法识别 MILOCO_MALLOC={choice}"
            "（可用值：jemalloc / glibc / libjemalloc.so 绝对路径），本次不改分配器",
            err=True,
        )
        return []

    return _malloc_env_pairs(*picked, malloc_conf)


def _malloc_env_or_empty(backend_python: str | None) -> list[tuple[str, str]]:
    """:func:`_resolve_malloc_env` 的兜底外壳：它自己有 bug 也绝不能让服务起不来。"""
    try:
        return _resolve_malloc_env(backend_python)
    except Exception as e:
        click.echo(f"warning: 分配器设置出错（{e!r}），本次不改分配器", err=True)
        return []


def _injected_jemalloc_from_conf() -> str | None:
    """从已生成的 supervisord.conf 读回本次注入的 ``LD_PRELOAD`` 首段；没注入返回 None。

    让提示自己去 conf 取事实，而不是把路径一层层传下来：conf 就是 backend 实际在用的那份配置，
    和用户排查时 ``grep LD_PRELOAD supervisord.conf`` 看的是同一个来源；安全模式或候选链全灭时
    conf 里本来就没这一行，"仅当真注入了才提示"也就不需要额外判断。
    """
    import re

    try:
        conf = _supervisor_conf().read_text()
    except OSError:
        return None
    m = re.search(r'LD_PRELOAD="([^"]*)"', conf)
    # 追加语义下值是 <我们的>:<原有的>，我们的在最前。
    return m.group(1).split(":")[0] if m else None


def _echo_jemalloc_hint_if_injected() -> None:
    """启动失败时提示可以先排除 jemalloc。

    措辞上**不下结论**——启动失败的原因很多（端口占用、依赖未就绪、配置错），
    "摘掉后成功"也推不出"是 jemalloc 的错"，所以只报告、只给开关，判断留给看日志的人。
    """
    if so_path := _injected_jemalloc_from_conf():
        click.echo(
            f"提示: 本次启用了 jemalloc ({so_path})。如果反复起不来，可以先排除它：\n"
            "      miloco-cli config set safe_mode true",
            err=True,
        )


# ─── supervisord 配置生成 ────────────────────────────────────────────────────


def _supervisor_environment(server_cmd: str) -> str:
    """拼 supervisord 的 ``environment=`` 行。

    值一律带双引号：这一行按逗号分隔，``MALLOC_CONF`` 里的逗号不加引号会被当成分隔符拆开。

    这一行里**任何一个**值含 ``"`` / ``%`` / 换行，supervisord 都会拒绝加载整份配置、守护
    进程起不来。分配器那几个值在各自来源处已经挡过（那里挡才能换下一个候选、或换回默认
    旋钮，走到这里再挡就只剩"丢掉"一种处置），这里兜的是剩下的——``MILOCO_HOME`` 能被
    同名环境变量指到 ``/data/50%off`` 这类路径上，它不比分配器那几个值安全。
    少一个变量 backend 有自己的兜底，整个守护进程起不来没有。
    """
    pairs = [
        ("MILOCO_SUPERVISED", "1"),
        ("MILOCO_HOME", str(miloco_home())),
    ]
    # 解析到时区才追加 TZ + MILOCO_TIMEZONE；否则不塞,交给子进程继承宿主 + backend 兜底。
    if tz := _resolve_timezone():
        pairs += [("TZ", tz), ("MILOCO_TIMEZONE", tz)]
    # 命令的第一段就是 backend 的解释器，拿它问自带的 libjemalloc 在哪、拿它跑探针。
    cmd_parts = shlex.split(server_cmd)
    pairs += _malloc_env_or_empty(cmd_parts[0] if cmd_parts else None)

    safe_pairs = []
    for name, value in pairs:
        if not _is_conf_safe(value):
            click.echo(
                f'warning: {name} 的值含有 " % 或换行，写进 supervisord.conf 会让 '
                "supervisord 拒绝加载配置，本次不注入它",
                err=True,
            )
            continue
        safe_pairs.append((name, value))
    return ",".join(f'{name}="{value}"' for name, value in safe_pairs)


def _generate_supervisor_conf(server_cmd: str) -> None:
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    sup_conf_path = _supervisor_conf()
    environment = _supervisor_environment(server_cmd)
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
environment={environment}
"""
    if sup_conf_path.exists() and sup_conf_path.read_text() == conf:
        return
    # 原子写：先截断再写的话，进程在中间被杀或磁盘满就会留下空/半截的 conf，supervisord 直接
    # 起不来——比 jemalloc 本身危险得多。
    atomic_write_text(sup_conf_path, conf)


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
            _echo_jemalloc_hint_if_injected()
            print_result({"error": "process failed to start, check logs"}, pretty)
            sys.exit(1)
        try:
            if httpx.get(health_url, timeout=2, verify=False).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    _echo_jemalloc_hint_if_injected()
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
        # 前台模式不经过 supervisord，分配器变量要自己塞进环境再 exec（ld.so 在 exec 时读它）。
        os.environ.update(dict(_malloc_env_or_empty(cmd[0])))
        os.execvp(cmd[0], cmd)
        # 不会到达这里
    else:
        _generate_supervisor_conf(shlex.join(cmd))

        if _supervisord_is_running():
            _supervisorctl("reread")
            _supervisorctl("update")
            result = _supervisorctl("start", _PROGRAM_NAME)
            if result.returncode != 0:
                # supervisord 活着但程序没起来。启动失败一共四条出口，逃生开关每条都要挂：
                # FATAL 和 health 超时那两条要求先走到 _wait_for_health，这条在那之前就 exit。
                _echo_jemalloc_hint_if_injected()
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
                # conf 的 environment= 行被写坏时，supervisord 起不来是唯一的症状——FATAL 和
                # health 超时那两处提示都要求它已经起来了，逃生开关必须也挂在这条出口上。
                _echo_jemalloc_hint_if_injected()
                # supervisord 的原话在 e.stderr 里（capture_output=True），CalledProcessError
                # 自己只说"returned non-zero exit status 2"，不带出来等于没有线索。
                detail = (getattr(e, "stderr", "") or "").strip()
                print_result(
                    {"error": f"supervisord failed to start: {e}", "detail": detail},
                    pretty,
                )
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
            # 同 start。升级后第一次 restart 正是配置里刚带上 LD_PRELOAD 的那一次，
            # 这条出口反而最可能撞上注入问题。
            _echo_jemalloc_hint_if_injected()
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
