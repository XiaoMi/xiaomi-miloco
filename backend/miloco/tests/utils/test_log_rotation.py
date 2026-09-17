"""stdio 日志轮转测试（launchd 迁移后 backend 自管日志）。"""

import os

from miloco.utils.log_rotation import (
    DEFAULT_BACKUPS,
    DEFAULT_MAX_BYTES,
    _roll_backups,
    _should_rotate,
    _tighten_existing,
    rotate_now,
    start_stdio_rotation,
)


def test_defaults_match_supervisord():
    """保留量必须与 supervisord 时代一致,否则迁移前后磁盘占用上限不同。"""
    assert DEFAULT_MAX_BYTES == 10 * 1024 * 1024  # stdout_logfile_maxbytes=10MB
    assert DEFAULT_BACKUPS == 20  # stdout_logfile_backups=20


def test_should_rotate_threshold(tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"x" * 100)
    assert _should_rotate(str(log), 200) is False
    assert _should_rotate(str(log), 100) is True  # >= 阈值即轮转


def test_should_rotate_missing_file_is_false(tmp_path):
    assert _should_rotate(str(tmp_path / "nope.log"), 1) is False


def test_roll_backups_shifts_and_drops_oldest(tmp_path):
    log = tmp_path / "a.log"
    for idx in range(1, 4):
        (tmp_path / f"a.log.{idx}").write_text(f"gen{idx}")

    _roll_backups(str(log), backups=3)

    # .1→.2、.2→.3;原 .3 超出保留份数被覆盖丢弃
    assert (tmp_path / "a.log.2").read_text() == "gen1"
    assert (tmp_path / "a.log.3").read_text() == "gen2"
    assert not (tmp_path / "a.log.4").exists()


def test_rotate_now_redirects_fd_to_new_file(tmp_path):
    """轮转后写入必须落到新文件,旧内容保留在 .1 —— 这是本模块的核心保证。"""
    log = tmp_path / "b.log"
    log.write_text("old-content\n")

    saved_out, saved_err = os.dup(1), os.dup(2)
    fd = os.open(str(log), os.O_WRONLY | os.O_APPEND)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)

        assert rotate_now(str(log), backups=3) is True
        os.write(1, b"after-rotate\n")
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)

    assert (tmp_path / "b.log.1").read_text() == "old-content\n"
    assert log.read_text() == "after-rotate\n"


def test_rotate_now_missing_file_returns_false(tmp_path):
    """目标不存在时安全失败,不抛异常(轮转线程不能因此退出)。"""
    assert rotate_now(str(tmp_path / "gone.log"), backups=3) is False


def test_rotated_file_not_world_readable(tmp_path):
    """轮转出的新文件必须 0o600(CodeQL #619):日志含 did / 家庭房间名等。"""
    log = tmp_path / "c.log"
    log.write_text("x\n")

    saved_out, saved_err = os.dup(1), os.dup(2)
    fd = os.open(str(log), os.O_WRONLY | os.O_APPEND)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        assert rotate_now(str(log), backups=2) is True
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)

    mode = log.stat().st_mode & 0o777
    assert mode == 0o600, f"轮转新文件权限应为 0o600, 实际 {oct(mode)}"


def test_rotate_now_rolls_back_rename_when_open_fails(tmp_path, monkeypatch):
    """改名成功但开新文件失败 → 必须回滚改名,不能永久失去轮转。

    不回滚的话:fd 1/2 继续写向已改名成 .log.1 的 inode,而固定名 path 不存在 →
    之后每轮 _should_rotate 的 getsize 抛 OSError 恒 False → **永远不再轮转**,
    日志在 .log.1 里无限增长(本模块声明绝不允许的静默退化),`service logs`
    按固定名也找不到文件。最现实的触发是 fd 耗尽(EMFILE)。
    """
    log = tmp_path / "d.log"
    log.write_text("payload\n")

    real_open = os.open

    def boom(path, flags, mode=0o777, **kw):
        if str(path) == str(log) and flags & os.O_CREAT:
            raise OSError(24, "EMFILE")
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", boom)
    assert rotate_now(str(log), backups=3) is False
    monkeypatch.undo()

    assert log.exists(), "改名必须已回滚,固定名文件不能消失"
    assert log.read_text() == "payload\n"
    assert not (tmp_path / "d.log.1").exists()
    # 回滚后下一轮还能正常轮转(退化不是永久的)
    assert _should_rotate(str(log), max_bytes=1) is True


def test_start_rotation_tightens_inherited_files(tmp_path):
    """启动时收紧历史文件权限：升级路径上主日志与备份可能是 supervisord 时代
    按 umask 落的 0644，而轮转 os.replace 改名不改权限 —— 不主动收一次，那份
    0644 的 inode 最久要经过 20 轮轮转才被挤出保留窗口。"""
    log = tmp_path / "e.log"
    log.write_text("now\n")
    os.chmod(log, 0o644)
    b1 = tmp_path / "e.log.1"
    b1.write_text("old\n")
    os.chmod(b1, 0o644)
    b2 = tmp_path / "e.log.2"
    b2.write_text("older\n")
    os.chmod(b2, 0o644)

    # interval 给大值 + max_bytes 给大值：只验证启动时的一次性收权，不让线程真轮转
    start_stdio_rotation(
        str(log), max_bytes=1 << 30, backups=3, interval=3600.0
    )

    for p in (log, b1, b2):
        assert oct(p.stat().st_mode & 0o777) == "0o600", p


def test_tighten_existing_tolerates_missing_files(tmp_path):
    """备份文件不存在（全新安装）时不抛异常 —— 轮转线程的启动绝不能被这个卡住。"""
    log = tmp_path / "f.log"
    log.write_text("x\n")
    _tighten_existing(str(log), backups=20)  # .1 … .20 都不存在
    assert oct(log.stat().st_mode & 0o777) == "0o600"
