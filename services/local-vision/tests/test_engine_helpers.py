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

    # num_frames 取个不常见的值:与断言里的字面量撞车的话,把它写死也照样绿。
    # 注意 codec 分支**不**用 num_frames 定画布数(那是按实际帧数推导的),
    # 它只用于 frames 分支的抽帧。
    e = MageVLEngine(checkpoint="x", video_backend=backend, num_frames=7, max_pixels=1234)
    e._processor = _RecordingProcessor()
    return e


def test_codec_branch_bounds_the_visual_budget():
    e = _engine_with_stub_processor("codec")
    e._build_inputs("/tmp/seg.mp4", "描述一下", "codec")
    assert e._processor.kw["max_pixels"] == 1234
    # patch=16 与 Mage-ViT 的预测块对齐,不可随意改;target_canvas 走 num_frames。
    cfg = e._processor.kw["codec_config"]
    assert cfg["engine"] == "hevc"
    assert cfg["patch"] == 16
    # 画布数必须随帧数变。只断言 engine/patch 的话,把推导换成常量照样绿。
    e._build_inputs("/tmp/seg.mp4", "描述一下", "codec", frame_count=256)
    assert e._processor.kw["codec_config"]["target_canvas"] > cfg["target_canvas"]


def test_frames_branch_bounds_the_visual_budget_too(monkeypatch):
    """段太短时会降级到 frames。虽是边缘情形,预算也必须与 codec 分支同源 ——
    否则同一台相机一旦落到降级通路,就会突然按原始分辨率铺 patch,token 数与
    显存跟着跳变。"""
    import local_vision.engine as eng

    monkeypatch.setattr(eng, "sample_frames", lambda video, n: ["F"] * n)
    e = _engine_with_stub_processor("frames")
    e._build_inputs("/tmp/seg.mp4", "描述一下", "frames")
    assert e._processor.kw["max_pixels"] == 1234
    assert e._processor.kw["videos"] == [["F"] * 7]


# ── codec 的画布数必须按实际帧数算 ────────────────────────────────────────


def test_target_canvas_is_derived_from_the_real_frame_count():
    """target_canvas 是**画布数**,不是帧数:所需源帧 = (tc/ipg)*gs。

    把 num_frames(32)直接填进去等于要求 256 帧源视频,而 miloco 一个 4s 窗最多
    给 32 帧 —— 于是每一次推理都打一条 "yields ~4 canvases instead of the
    requested 32",请求本身不自洽。实测修正后该警告归零、输出质量不变。
    """
    from local_vision.engine import MageVLEngine

    e = MageVLEngine(checkpoint="x")
    gs, ipg = e._CODEC_GROUP_SIZE, e._CODEC_IMAGES_PER_GROUP
    # 先钉住"会变":常量实现能满足下面所有的整除/上界断言,唯独过不了这一条。
    assert len({e._codec_target_canvas(n) for n in (32, 128, 256)}) == 3

    for frames in (1, 8, 31, 32, 64, 256):
        tc = e._codec_target_canvas(frames)
        assert tc % ipg == 0, "target_canvas 必须能被 images_per_group 整除"
        assert tc >= ipg, "至少要有一组"
        required = (tc // ipg) * gs
        assert required <= max(frames, gs), (
            f"{frames} 帧却要求 {required} 帧:又在超额索取"
        )


def test_codec_config_carries_the_derived_canvas(monkeypatch):
    """真正送进处理器的那份 codec_config 要带上算出来的值,而不是 num_frames。"""
    e = _engine_with_stub_processor("codec")
    e._build_inputs("/tmp/seg.mp4", "描述一下", "codec", frame_count=32)
    cfg = e._processor.kw["codec_config"]
    assert cfg["target_canvas"] == e._codec_target_canvas(32)
    assert cfg["group_size"] == e._CODEC_GROUP_SIZE
    assert cfg["images_per_group"] == e._CODEC_IMAGES_PER_GROUP
    assert cfg["patch"] == 16


def test_checkpoint_is_resolved_in_load_not_in_the_constructor(tmp_path):
    """路径写错时必须落进 load_error 经 /health 说出来,而不是让进程起不来。

    在构造期解析的话,一个写错的目录会让 lifespan 抛异常 —— 服务根本起不来,于是
    连 /health 都没有,用户看到的只有"连不上",而真正的原因(目录不存在)只在终端
    里一闪而过。
    """
    import pytest as _pytest

    from local_vision.engine import MageVLEngine

    bad = str(tmp_path / "no-such-dir")
    e = MageVLEngine(checkpoint=bad)      # 构造期不该抛
    assert e.checkpoint == bad

    with _pytest.raises(FileNotFoundError):
        e.load()                          # 抛在 load 里,由加载线程接住并写 load_error
