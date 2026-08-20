"""Unified initialization for both CLI and server environments."""

import logging
import logging.config
import os
import sys

from miloco.config import get_settings
from miloco.utils.agent_config import ensure_backend_token
from miloco.utils.log_rotation import start_stdio_rotation
from miloco.utils.uvicorn import get_uvicorn_log_config

logger = logging.getLogger(__name__)


BOOT_FROM = None


def _redirect_stdio_to_file(log_path: str) -> None:
    """Replace process fd 1 & 2 with an append fd to log_path.

    After this call every write to stdout/stderr — including output from
    native C libraries that bypass Python's logging (e.g., ONNX Runtime) —
    flows into log_path. Python loggers configured with a StreamHandler on
    sys.stderr share the same fd, so there is a single writer to the file.
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # 0o600:日志含设备 did / 家庭房间名等,不该 world-readable。与
    # log_rotation.rotate_now 创建轮转文件时同模式,避免轮转后权限漂移。
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        # O_CREAT 的 mode 只在**新建**时生效,文件已存在则被内核完全忽略。
        # supervisord 时代的日志是同名同路径(两侧都是 $MILOCO_HOME/log/
        # miloco-backend.log)、由 supervisord 裸 open 落的 0644;迁到 launchd 后
        # 本进程只是接着往同一个文件追加写 —— 不显式收一次权限,凡是从旧版升上来的
        # 机器(即 _reap_legacy_supervisord 专门去回收的那批)日志会一直 world-readable,
        # 同机其他账号 cat 一下就拿到设备 did、家庭/房间名和偶发的 token 片段。
        # 也不会「下次轮转就自愈」:轮转是 os.replace 改名,不改权限。
        # 用 fchmod 而非 chmod(path):作用在已打开的 fd 上,没有 TOCTOU 窗口。
        os.fchmod(fd, 0o600)
    except (OSError, AttributeError) as e:
        # OSError:无 POSIX 权限的文件系统。AttributeError:os.fchmod 是 Unix-only
        # (非 Unix 上根本没这个属性,不是 OSError)。收不紧权限不该让后端起不来。
        # 这条 print 必须在下面的 dup2 **之前**,否则警告会掉进那个刚好收不紧的
        # 文件里、用户看不到。
        print(f"warn: chmod 0600 on {log_path} failed: {e}", file=sys.stderr)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)
    # Rebind sys.stdout/stderr so Python StreamHandlers see the new fd.
    sys.stdout = os.fdopen(1, "w", buffering=1)
    sys.stderr = os.fdopen(2, "w", buffering=1)


def bootstrap(target: str = "server", debug: bool = False) -> None:
    """Bootstrap: logging + shared token.

    Args:
        target: "cli" or "server"
        debug: when True, force console logging even in daemon mode.
    """
    global BOOT_FROM
    if BOOT_FROM:
        return
    BOOT_FROM = target

    # Ensure backend token exists & is published to $MILOCO_HOME/config.json.
    ensure_backend_token()

    settings = get_settings()

    # Daemon mode (stdout not a tty) → redirect stdio to a per-boot file so
    # native C stderr (ORT warnings, etc.) is captured alongside Python logger
    # output. Foreground/dev (tty) leaves stdio on the terminal — no file.
    if (
        target == "server"
        and not sys.stdout.isatty()
        and "debugpy" not in sys.modules
        and not os.environ.get("MILOCO_SUPERVISED")
    ):
        # 固定名 + 按大小轮转，与 supervisord 时代的文件布局一致
        # （miloco-backend.log / .1 … .20）——CLI 的 _find_latest_log 优先命中
        # 这个固定名，logs / status 无需改动。
        #
        # 早前这里用的是 per-boot 时间戳名（miloco-backend_20260814_142514.log）：
        # 那时只有"非 supervised 的 daemon"才走到，属边缘路径，靠每次启动换新文件
        # 避免单文件涨大。launchd 迁移后这条路成了 macOS 的主路径，长跑不重启就会
        # 无限增长（实测涨到 874MB），必须真正的轮转而不是换名。
        log_path = os.path.join(
            str(settings.directories.log_dir),
            f"{settings.app.service_name}.log",
        )
        _redirect_stdio_to_file(log_path)
        start_stdio_rotation(log_path)

    log_config = get_uvicorn_log_config(
        enable_console_logging=True if debug else None,
    )
    logging.config.dictConfig(log_config)
