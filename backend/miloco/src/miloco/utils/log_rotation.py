"""stdio 日志按大小轮转（daemon 模式下 backend 自管日志时使用）。

**为什么由 backend 自己做**：supervisord 时代日志轮转由 supervisord 负责
（``stdout_logfile_maxbytes`` / ``stdout_logfile_backups``）；迁到 launchd 后，
``StandardOutPath`` 只做 fd 重定向，launchd **不提供任何轮转能力**
（``man launchd.plist`` 无 rotate/maxbytes/backups 语义）。macOS 侧的等价物
newsyslog 需要 root 写 ``/etc/newsyslog.d/``（我们是用户级 LaunchAgent），且它靠
``mv`` + 发信号让进程重开 fd、没有 logrotate 的 copytruncate 模式——backend 不接
SIGHUP 的话轮转后会继续写已改名的旧 inode。两条路都要改 backend，索性由 backend
自己持 fd、自己重开，不依赖 root、不依赖信号协议，跨平台一致。

**为什么是 fd 级而不是 logging.RotatingFileHandler**：daemon 模式下 fd 1/2 被
``dup2`` 重定向，原生 C 库（ONNX Runtime、摄像头原生库等）绕过 Python logging 直接
写 fd 2。RotatingFileHandler 只看得见 Python logging 的写入，管不住这部分——而恰恰
是原生库的刷屏最容易把日志顶大。
"""

import os
import threading

# 与 supervisord 时代的 stdout_logfile_maxbytes / stdout_logfile_backups 对齐，
# 保证迁移前后磁盘占用上限一致（10MB × 20 ≈ 210MB 封顶），属于补回丢失的能力
# 而非改变行为。
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 20
DEFAULT_CHECK_INTERVAL = 30.0


def _roll_backups(path: str, backups: int) -> None:
    """``.19→.20 … .1→.2``，最旧的一份被覆盖丢弃。"""
    for idx in range(backups - 1, 0, -1):
        src = f"{path}.{idx}"
        if os.path.exists(src):
            try:
                os.replace(src, f"{path}.{idx + 1}")
            except OSError:
                # 单份滚动失败不该中断整轮轮转（更不该让业务线程受影响）。
                pass


def rotate_now(path: str, backups: int = DEFAULT_BACKUPS) -> bool:
    """滚动备份并把 fd 1/2 切到新文件，返回是否真的轮转了。

    顺序是「先改名、再开新文件、最后 dup2」：改名后 fd 仍指向原 inode（此刻叫
    ``.log.1``），这几微秒内的写入会落在 ``.log.1`` 末尾——顺序不乱、不丢，只是
    分界处略有毛刺。反过来先开新文件则会因目标名被占用而拿不到干净的 inode。

    已知局限：只切换**本进程**的 fd。启动时继承了 fd 1/2 的子进程（如 shell 调起的
    ffmpeg）仍写向轮转前的 inode，其输出会留在 ``.log.1`` 里直到子进程退出。
    """
    try:
        _roll_backups(path, backups)
        os.replace(path, f"{path}.1")
        # 0o600 与 bootstrap._redirect_stdio_to_file 保持一致:日志里有设备 did、
        # 家庭/房间名乃至偶发的 token 片段,不该 world-readable(CodeQL #619)。
        # 两处必须同模式,否则轮转出来的新文件权限会与原文件漂移。
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        except OSError:
            # 改名成功、开新文件失败(最现实的是 fd 耗尽 EMFILE)必须**回滚改名**:
            # 否则 fd 1/2 继续写向已改名成 .log.1 的 inode,而固定名 path 不存在 →
            # 之后每轮 _should_rotate 的 getsize 抛 OSError 恒 False → 永远不再轮转,
            # 日志在 .log.1 里无限增长(正是本模块声明绝不允许的静默退化),
            # 且 `service logs` 按固定名也找不到文件。
            os.replace(f"{path}.1", path)
            return False
        try:
            os.dup2(fd, 1)
            os.dup2(fd, 2)
        finally:
            os.close(fd)
        return True
    except OSError:
        # 轮转失败就继续用原 fd 写——日志涨大远好过把进程搞挂。
        return False


def _should_rotate(path: str, max_bytes: int) -> bool:
    try:
        return os.path.getsize(path) >= max_bytes
    except OSError:
        return False


def _tighten_existing(path: str, backups: int) -> None:
    """把主日志与历史备份的权限收到 0600（best-effort）。

    针对**升级路径**：supervisord 时代同名同路径的日志（含它自己轮出的 ``.log.1``…）
    是裸 open 落的 0644。轮转用 ``os.replace`` 改名、不改权限，所以那份 0644 的 inode
    会以备份的形态在整个保留窗口（20 份）里一直留着 —— 不主动收一次，最久要 20 轮
    轮转才被挤出去。全新安装不受影响（文件由本进程创建，O_CREAT 的 mode 生效）。
    """
    for name in (path, *(f"{path}.{i}" for i in range(1, backups + 1))):
        try:
            os.chmod(name, 0o600)
        except OSError:
            pass  # 不存在 / 无 POSIX 权限：跳过，绝不能影响轮转线程启动


def start_stdio_rotation(
    path: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backups: int = DEFAULT_BACKUPS,
    interval: float = DEFAULT_CHECK_INTERVAL,
) -> threading.Thread:
    """起一个 daemon 线程周期检查大小并轮转，返回该线程（便于测试）。

    用轮询而非「每次写入后检查」：写入方包括绕过 Python 的原生库，没有统一钩子；
    而 30s 的检查间隔相对 10MB 的阈值足够密（实测日志增速约 320MB/天，即约 40 分钟
    才写满一份），超出阈值的部分最多是一个检查周期的量。

    启动时先收一次历史文件的权限（见 ``_tighten_existing``：升级路径上主日志与备份
    可能是 supervisord 时代按 umask 落的 0644）。
    """
    _tighten_existing(path, backups)

    def _loop() -> None:
        while True:
            try:
                if _should_rotate(path, max_bytes):
                    rotate_now(path, backups)
            except Exception:
                # 轮转线程绝不能因任何异常退出，否则悄悄退化成"无轮转"。
                pass
            threading.Event().wait(interval)

    thread = threading.Thread(target=_loop, name="stdio-log-rotation", daemon=True)
    thread.start()
    return thread
