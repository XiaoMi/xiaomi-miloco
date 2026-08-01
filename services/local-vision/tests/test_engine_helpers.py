"""引擎里两个不依赖 GPU 的纯函数。

它们都在**启动路径**上,而启动路径的失败最难排查 —— ``cv-preinfer`` 找不到时,
报错发生在第一次推理,而不是启动时,现场完全联想不到是 PATH 问题。
"""

from __future__ import annotations

import os
import sys

from local_vision.engine import ensure_codec_tool_on_path, resolve_checkpoint


def test_explicit_env_override_wins(monkeypatch):
    monkeypatch.setenv("CV_PREINFER_BIN", "/opt/cv-preinfer")
    assert ensure_codec_tool_on_path() == "/opt/cv-preinfer"


def test_finds_the_tool_next_to_the_interpreter(tmp_path, monkeypatch):
    """systemd / supervisor / nohup 都是直接调 venv 里的 python 而不激活 venv,
    此时 venv/bin 不在 PATH 上 —— 这正是 codec 通路最常见的失效方式。"""
    monkeypatch.delenv("CV_PREINFER_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "cv-preinfer").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))

    found = ensure_codec_tool_on_path()
    assert found == str(bindir / "cv-preinfer")
    # 找到之后要真的让后续子进程能用上,而不只是返回一个字符串。
    assert str(bindir) in os.environ["PATH"]
    assert os.environ["CV_PREINFER_BIN"] == found


def test_tool_already_on_path_is_used_as_is(tmp_path, monkeypatch):
    """最常见的情形(激活了 venv 再起服务)反而最容易漏测。"""
    monkeypatch.delenv("CV_PREINFER_BIN", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / "cv-preinfer"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert ensure_codec_tool_on_path() == str(tool)


def test_missing_tool_returns_none_rather_than_raising(tmp_path, monkeypatch):
    """返回 None 让调用方降级到 frames;抛异常会让整个服务起不来。"""
    monkeypatch.delenv("CV_PREINFER_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "empty" / "python"))
    assert ensure_codec_tool_on_path() is None


def test_local_directory_is_used_as_is(tmp_path):
    assert resolve_checkpoint(str(tmp_path)) == str(tmp_path)


def test_non_directory_is_left_for_transformers_to_resolve():
    """不是本地目录就当 HF repo id 原样传下去,不要自作主张拼路径。"""
    assert resolve_checkpoint("microsoft/Mage-VL") == "microsoft/Mage-VL"


# ── 两条视觉通路的输入构造 ────────────────────────────────────────────────


class _RecordingProcessor:
    """只记录收到的 kwargs。不加载权重、不碰 GPU。"""

    def __init__(self):
        self.kw = None

    def apply_chat_template(self, messages, **_):
        return "PROMPT"

    def __call__(self, **kw):
        self.kw = kw
        return {"input_ids": None}


def _engine_with_stub_processor(backend: str):
    from local_vision.engine import MageVLEngine

    # num_frames 取个不常见的值:与断言里的字面量撞车的话,把 target_canvas 写死
    # 也照样绿。
    e = MageVLEngine(checkpoint="x", video_backend=backend, num_frames=7, max_pixels=1234)
    e._processor = _RecordingProcessor()
    return e


def test_codec_branch_bounds_the_visual_budget():
    e = _engine_with_stub_processor("codec")
    e._build_inputs("/tmp/seg.mp4", "描述一下", "codec")
    assert e._processor.kw["max_pixels"] == 1234
    # patch=16 与 Mage-ViT 的预测块对齐,不可随意改;target_canvas 走 num_frames。
    assert e._processor.kw["codec_config"] == {
        "engine": "hevc", "target_canvas": 7, "patch": 16,
    }


def test_frames_branch_bounds_the_visual_budget_too(monkeypatch):
    """段太短时会降级到 frames —— 而那正是 miloco 默认 4s 窗口的**常态**落点。

    这条分支不限像素的话,同一台相机切到降级通路就会突然按原始分辨率铺 patch:
    token 数与显存跟着跳变,而两条通路的视觉预算本该同源。
    """
    import local_vision.engine as eng

    monkeypatch.setattr(eng, "sample_frames", lambda video, n: ["F"] * n)
    e = _engine_with_stub_processor("frames")
    e._build_inputs("/tmp/seg.mp4", "描述一下", "frames")
    assert e._processor.kw["max_pixels"] == 1234
    assert e._processor.kw["videos"] == [["F"] * 7]
