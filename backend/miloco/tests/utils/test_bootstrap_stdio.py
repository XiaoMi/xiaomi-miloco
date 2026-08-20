"""`_redirect_stdio_to_file` 的权限不变量（此前零覆盖）。

日志里有设备 did、家庭 / 房间名，偶发还有 token 片段，所以必须 0600 —— 这条不变量
在本仓其它三处（plist / 轮转后日志 / mTLS 私钥）都有用例钉住，主日志这条不能是例外。
"""

import os
import sys

from miloco.utils.bootstrap import _redirect_stdio_to_file


def _run_isolated(fn):
    """在保存/恢复 fd 1、2 与 sys.stdout/stderr 的前提下跑 fn。

    `_redirect_stdio_to_file` 会 dup2 到 fd 1/2 并重绑 sys.stdout/stderr，不还原会
    把后续测试的输出都写进临时文件（pytest 的捕获也跟着错乱）。
    """
    saved_out, saved_err = os.dup(1), os.dup(2)
    saved_pyout, saved_pyerr = sys.stdout, sys.stderr
    try:
        return fn()
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        sys.stdout, sys.stderr = saved_pyout, saved_pyerr


def test_new_log_file_is_not_world_readable(tmp_path):
    """全新安装：文件由本进程创建，O_CREAT 的 mode 生效。"""
    log = tmp_path / "log" / "miloco-backend.log"

    _run_isolated(lambda: _redirect_stdio_to_file(str(log)))

    assert oct(log.stat().st_mode & 0o777) == "0o600"


def test_inherited_0644_log_is_tightened(tmp_path):
    """升级路径：supervisord 时代同名同路径的日志已存在且是 0644。

    `os.open` 的 mode 参数**只在新建时生效**，文件已存在就被内核完全忽略 —— 所以
    只靠 O_CREAT 的 0o600，从旧版升上来的机器（即 _reap_legacy_supervisord 专门去
    回收的那批）日志会一直 world-readable，同机其他账号 cat 一下就拿到设备 did、
    家庭/房间名和偶发的 token 片段。也不会「下次轮转就自愈」：轮转是 os.replace
    改名，不改权限。
    """
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    log = log_dir / "miloco-backend.log"
    log.write_text("supervisord 时代写下的内容\n")
    os.chmod(log, 0o644)

    _run_isolated(lambda: _redirect_stdio_to_file(str(log)))

    assert oct(log.stat().st_mode & 0o777) == "0o600"
    # 追加语义不变：既有内容不能被截断
    assert log.read_text().startswith("supervisord 时代写下的内容")
