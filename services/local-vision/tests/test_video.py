"""段落盘与后端选择。

``pick_backend`` 是一条**降级**逻辑:选错方向的代价不对称 —— 该退不退会让每一窗
推理直接失败(codec 分组凑不齐就报错),而多退一步只是拿不到 token 削减。所以
它的契约是"拿不准就退",这里把每种拿不准的情形都钉死。
"""

from __future__ import annotations

import io
import tempfile

import pytest

from local_vision.video import (
    MIN_CODEC_FRAMES,
    pick_backend,
    probe_frame_count,
    write_temp_video,
)


def test_short_segments_fall_back_to_frames():
    """段被截短(相机刚上线、窗口被打断)时凑不齐一组,必须优雅回退。

    注意这是**边缘情形**:默认路径是 codec(实测连续 246 个窗口全部走 codec)。
    """
    assert pick_backend("codec", MIN_CODEC_FRAMES - 1) == "frames"
    assert pick_backend("codec", 0) == "frames"


def test_long_enough_segments_stay_on_codec():
    assert pick_backend("codec", MIN_CODEC_FRAMES) == "codec"
    assert pick_backend("codec", 300) == "codec"


def test_probe_failure_falls_back_rather_than_guessing():
    """探不出帧数多半是没有 ffprobe;codec 通路同样依赖外部工具,硬走会每窗 500。"""
    assert pick_backend("codec", -1) == "frames"


def test_frames_preference_is_never_upgraded():
    """显式选了 frames 就不该被"优化"回 codec —— 那是部署者关掉 codec 的手段。"""
    assert pick_backend("frames", 300) == "frames"
    assert pick_backend("frames", -1) == "frames"


def test_probe_on_a_non_video_file_returns_minus_one(tmp_path):
    """不抛异常 —— 调用方靠这个 -1 走保守分支,抛出去会毁掉整窗感知。"""
    p = tmp_path / "not-a-video.mp4"
    p.write_bytes(b"definitely not a video")
    assert probe_frame_count(p) == -1


def test_probe_on_a_missing_file_returns_minus_one(tmp_path):
    assert probe_frame_count(tmp_path / "nope.mp4") == -1


def test_write_temp_video_roundtrips_and_uses_an_mp4_suffix():
    """codec 通路要把路径交给 ffprobe / cv-preinfer,后缀必须像个视频文件。"""
    path = write_temp_video(b"\x00\x01payload")
    try:
        assert path.suffix == ".mp4"
        assert path.read_bytes() == b"\x00\x01payload"
    finally:
        path.unlink(missing_ok=True)


def test_write_temp_video_cleans_up_when_the_write_fails(monkeypatch, tmp_path):
    """写失败时不能把空壳留在盘上——那条路径还没交给调用方,没人能清理它。

    最现实的触发是磁盘满,而磁盘满会让**每一个窗口**都走这条路:一小时能攒下几百
    个残留(sweep 的兜底年龄是 1 小时),还都堆在那块已经满了的盘上。
    """
    import os

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    real_fdopen = os.fdopen

    class _Boom(io.RawIOBase):
        def __init__(self, fd):
            self._f = real_fdopen(fd, "wb")

        def write(self, _data):  # noqa: D102
            raise OSError(28, "No space left on device")

        def close(self):
            self._f.close()

    monkeypatch.setattr(os, "fdopen", lambda fd, mode: _Boom(fd))
    with pytest.raises(OSError):
        write_temp_video(b"payload")

    assert list(tmp_path.glob("lv-seg-*")) == [], "写失败后把空壳留在磁盘上了"


# ── 上一次进程留下的残段 ──────────────────────────────────────────────────


def test_sweep_removes_only_old_segments(tmp_path, monkeypatch):
    """单次请求的清理写在 finally 里,进程被 SIGKILL 时不会执行 —— 每被中断一次
    就留下一段几百 KB 的**家里画面**。开机扫一次,但只扫足够老的:一机多卡跑两个
    边车是合理部署,不能误删另一个实例正在用的段。"""
    import os
    import time

    from local_vision import video as v

    monkeypatch.setattr(v.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / "lv-seg-old.mp4"
    fresh = tmp_path / "lv-seg-fresh.mp4"
    other = tmp_path / "someone-elses.mp4"
    for p in (old, fresh, other):
        p.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))

    assert v.sweep_stale_segments() == 1
    assert not old.exists()
    assert fresh.exists(), "刚写的段可能正在飞,不能删"
    assert other.exists(), "只清自己的前缀"


def test_sweep_survives_unremovable_files(tmp_path, monkeypatch):
    """清扫是尽力而为:一个删不掉的文件不该让服务起不来。"""
    import time

    from local_vision import video as v

    monkeypatch.setattr(v.tempfile, "gettempdir", lambda: str(tmp_path))
    p = tmp_path / "lv-seg-locked.mp4"
    p.write_bytes(b"x")
    import os
    os.utime(p, (time.time() - 7200, time.time() - 7200))
    monkeypatch.setattr(v.Path, "unlink", lambda self, **kw: (_ for _ in ()).throw(OSError("nope")))
    assert v.sweep_stale_segments() == 0
