# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""``scripts/clip_omni.py`` 的离线测试（不联网、不调真实模型）。

覆盖：--rule 解析、clip → snapshot → pipeline(rule_only) 全链路（mock omni）、
结果渲染。真实验证模型输出请直接跑脚本：
  python scripts/clip_omni.py <clip.mp4> --rule "手扫地：人手出现在画面中"
"""

import importlib.util
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

_SCRIPT = Path(__file__).resolve().parents[4].parent / "scripts" / "clip_omni.py"


@pytest.fixture(scope="module")
def clip_omni():
    spec = importlib.util.spec_from_file_location("clip_omni", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _write_clip(tmp_path, n_frames: int = 4, size=(64, 64)) -> Path:
    """生成一段纯色渐变的小 mp4（2fps）。"""
    path = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 2.0, size
    )
    for i in range(n_frames):
        frame = np.full((size[1], size[0], 3), (i * 40 % 255, 60, 90), dtype=np.uint8)
        writer.write(frame)
    writer.release()
    assert path.exists() and path.stat().st_size > 0
    return path


# ─── --rule 解析 ─────────────────────────────────────────────────────────────


def test_parse_rules_separators(clip_omni):
    rules = clip_omni.parse_rules(
        ["手扫地：人手出现在画面中", "比耶开灯: 摆出比耶手势", "无分隔符"]
    )
    assert [r.rule_name for r in rules] == ["手扫地", "比耶开灯", "无分隔符"]
    assert rules[0].query == "人手出现在画面中"
    assert rules[1].query == "摆出比耶手势"
    assert rules[0].rule_id == "local-0"
    assert rules[2].query == "无分隔符"


def test_parse_rules_skips_empty(clip_omni):
    assert clip_omni.parse_rules(["", "   "]) == []


# ─── clip → snapshot / packet ────────────────────────────────────────────────


def test_load_snapshot_decodes_frames(clip_omni, tmp_path):
    clip = _write_clip(tmp_path)
    snap = clip_omni.load_snapshot(str(clip), room="卧室", fps=1.0)
    assert snap.device.room_name == "卧室"
    assert snap.video is not None and len(snap.video.frames) >= 1
    frames = [f.data for f in snap.video.frames]
    assert all(f.shape[:2] == (64, 64) for f in frames)


def test_load_snapshot_missing_file(clip_omni, tmp_path):
    with pytest.raises(SystemExit, match="不存在"):
        clip_omni.load_snapshot(str(tmp_path / "nope.mp4"))


def test_packet_from_snapshot_strips_audio(clip_omni, tmp_path):
    clip = _write_clip(tmp_path)
    snap = clip_omni.load_snapshot(str(clip), room="卧室", fps=1.0)
    ep = clip_omni.packet_from_snapshot(snap, room="卧室", fps=1.0)
    assert len(ep.all_frames) >= 1
    # 无音频：不喂音频、不触发 audio route
    assert ep.trigger is not None and ep.trigger.audio_active is False


# ─── rule_only 全链路（mock omni） ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_rule_only_pipeline_with_local_clip(clip_omni, tmp_path, monkeypatch):
    """本地 clip → run_pipeline(rule_only) → 解析出 matched_rules（mock omni 不联网）。"""
    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.engine.pipeline import run_pipeline
    from miloco.perception.engine.types import OmniContext

    clip = _write_clip(tmp_path)
    snap = clip_omni.load_snapshot(str(clip), room="卧室", fps=1.0)
    rules = clip_omni.parse_rules(["手扫地：人手出现在画面中", "比耶开灯：摆出比耶手势"])

    config = PerceptionConfig(rule_only=True)
    config.omni.api_key = "test-key"
    ctx = OmniContext(rule_only=True, rule_conditions=rules)

    captured: dict = {}

    async def _fake_call_omni(payload, config, type="realtime"):
        captured["payload"] = payload
        return {
            "id": "mock",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "matched_rules": [
                                    {
                                        "rule_name": "手扫地",
                                        "reason": "画面中可见一只手",
                                        "hit": True,
                                    }
                                ]
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 88, "completion_tokens": 12},
        }

    monkeypatch.setattr(
        "miloco.perception.engine.omni.omni.call_omni",
        _fake_call_omni,
    )
    result = await run_pipeline(snap, ctx, config)

    assert not result.skipped
    out = result.omni_output
    assert out is not None
    # 模型按 rule_name 回抄 → 解析层映射回 rule_id
    assert [m.rule_id for m in out.matched_rules] == ["local-0"]
    assert out.matched_rules[0].reason == "画面中可见一只手"
    # 只出 matched_rules，其余字段空
    assert out.caption == [] and out.suggestions == [] and out.speeches == []
    # 规则段进入 user content
    assert "手扫地" in captured["payload"]["user_content"]
    # 输入是 mp4 视频、无音频
    assert "video_base64" in captured["payload"]
    assert "audio_base64" not in captured["payload"]


@pytest.mark.asyncio
async def test_image_mode_payload(clip_omni, tmp_path, monkeypatch):
    """--image：rule_only 输入改为窗口末帧 JPEG（image_frames 而非 video_base64）。"""
    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.engine.pipeline import run_pipeline
    from miloco.perception.engine.types import OmniContext

    clip = _write_clip(tmp_path)
    snap = clip_omni.load_snapshot(str(clip), room="卧室", fps=1.0)
    rules = clip_omni.parse_rules(["手扫地：人手出现在画面中"])

    config = PerceptionConfig(rule_only=True)
    config.omni.api_key = "test-key"
    ctx = OmniContext(rule_only=True, rule_conditions=rules)

    restore = clip_omni.apply_temp_settings(system_prompt=None, image_mode=True)
    captured: dict = {}

    async def _fake_call_omni(payload, config, type="realtime"):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"matched_rules":[]}'}}]}

    try:
        monkeypatch.setattr(
            "miloco.perception.engine.omni.omni.call_omni", _fake_call_omni
        )
        result = await run_pipeline(snap, ctx, config)
    finally:
        restore()

    assert not result.skipped
    assert "image_frames" in captured["payload"]
    assert "video_base64" not in captured["payload"]
    # 图片模式下仍会为回看额外编码一份 mp4（不进 payload），此处不校验字节


def test_image_mode_setting_restored(clip_omni, monkeypatch):
    """apply_temp_settings 的恢复：运行后 rule_only_input / system prompt 回到原值。"""
    from miloco.config import get_settings

    engine = get_settings().perception.engine
    engine["rule_only_system_prompt"] = ""
    engine.setdefault("input", {})["rule_only_input"] = "video"
    restore = clip_omni.apply_temp_settings(
        system_prompt="临时覆盖", image_mode=True
    )
    assert engine["rule_only_system_prompt"] == "临时覆盖"
    assert engine["input"]["rule_only_input"] == "image"
    restore()
    assert engine["rule_only_system_prompt"] == ""
    assert engine["input"]["rule_only_input"] == "video"


# ─── 结果渲染 ────────────────────────────────────────────────────────────────


def test_strip_json_fences(clip_omni):
    assert clip_omni.strip_json_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert clip_omni.strip_json_fences('```\n{"a":1}\n```') == '{"a":1}'
    assert clip_omni.strip_json_fences('{"a":1}') == '{"a":1}'


def test_render_rule_only_hit_and_miss(clip_omni):
    from miloco.perception.types import MatchedRule, RealtimePerceptionResult

    rules = clip_omni.parse_rules(["手扫地：人手出现在画面中", "比耶开灯：摆出比耶手势"])
    out = RealtimePerceptionResult(
        matched_rules=[
            MatchedRule(rule_id="local-0", rule_name="手扫地", reason="画面中可见一只手")
        ]
    )
    lines = clip_omni.render_rule_only(rules, out)
    assert any("✅ 命中" in line and "手扫地" in line for line in lines)
    assert any("reason: 画面中可见一只手" in line for line in lines)
    assert any("⬜ 未命中" in line and "比耶开灯" in line for line in lines)


def test_render_full_parses_json(clip_omni):
    raw = json.dumps(
        {
            "caption": [{"description": "小明坐在床上看书"}],
            "speeches": [],
            "env_sounds": ["翻书声"],
            "matched_rules": [{"rule_name": "读书", "reason": "人在看书", "hit": True}],
            "suggestions": [{"event": "灯未开", "action": "提醒开灯", "urgency": "low"}],
        }
    )
    lines = clip_omni.render_full(raw)
    text = "\n".join(lines)
    assert "小明坐在床上看书" in text
    assert "翻书声" in text
    assert "✅" in text and "读书" in text
    assert "提醒开灯" in text
