# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""P4b：pet:observe 选 crop + omni 编排（mock detector + 真 SortTracker + mock omni）。

不依赖真 onnx：detector 用 mock（返回 Detection 列表）；omni 用 monkeypatch 桩。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from miloco.perception.engine.identity.tracker.detector import Detection
from miloco.pet import observe as obs


def _frame(h=200, w=200):
    # 噪声帧：compute_sharpness > 0，使评分有意义
    return np.random.randint(0, 255, (h, w, 3), dtype=np.uint8)


def _det(x, y, w, h, conf=0.9, cls=Detection.CLASS_CAT) -> Detection:
    return Detection(x=x, y=y, w=w, h=h, confidence=conf, class_id=cls)


# ── 单图选最大 crop ──────────────────────────────────────────────────────────


def test_largest_pet_crop_picks_biggest():
    frame = _frame()
    detector = SimpleNamespace(
        detect_pets=lambda f: [_det(5, 5, 20, 20), _det(40, 40, 80, 80)]
    )
    out = obs._largest_pet_crop(frame, detector)
    assert out is not None
    assert out["class_id"] == Detection.CLASS_CAT
    # 取了大框（约 80×80 + 扩边），明显大于小框 20×20
    assert out["crop"].shape[0] >= 80 and out["crop"].shape[1] >= 80


def test_largest_pet_crop_none_when_no_pets():
    detector = SimpleNamespace(detect_pets=lambda f: [])
    assert obs._largest_pet_crop(_frame(), detector) is None


def test_largest_pet_crop_ignores_non_pet_class():
    # 即便 detect_pets 误带 HUMAN，也按宠物类过滤
    detector = SimpleNamespace(
        detect_pets=lambda f: [_det(10, 10, 50, 50, cls=Detection.CLASS_HUMAN)]
    )
    assert obs._largest_pet_crop(_frame(), detector) is None


# ── 视频：SORT 分 track + 每 track 选优 ──────────────────────────────────────


def test_video_single_pet_one_track():
    frames = [(i, _frame(300, 300)) for i in range(8)]
    detector = SimpleNamespace(detect_pets=lambda f: [_det(50, 50, 60, 60)])
    crops = obs._video_pet_crops(frames, detector, fps=1)
    assert len(crops) >= 1
    assert crops[0]["class_id"] == Detection.CLASS_CAT
    assert crops[0]["crop"].size > 0


def test_video_two_separated_pets_two_tracks():
    # 两只相距很远的宠物，稳定出现多帧 → SORT 应分成两个 track
    frames = [(i, _frame(400, 400)) for i in range(8)]
    detector = SimpleNamespace(
        detect_pets=lambda f: [
            _det(20, 20, 60, 60, cls=Detection.CLASS_CAT),
            _det(300, 300, 60, 60, cls=Detection.CLASS_DOG),
        ]
    )
    crops = obs._video_pet_crops(frames, detector, fps=1)
    assert len(crops) == 2
    assert {c["class_id"] for c in crops} == {Detection.CLASS_CAT, Detection.CLASS_DOG}


# ── omni 编排 ────────────────────────────────────────────────────────────────


def _stub_omni(monkeypatch, payload_holder=None, *, with_head=False):
    desc = {
        "species": "猫",
        "breed": "",
        "size_build": "中等体型",
        "coat_color_pattern": "黑色",
        "coat_length_texture": "短毛",
        "distinctive_markings": ["尾尖一撮白"],
        "accessories": "",
        "summary": "中等体型的黑色短毛猫，尾巴尖有一撮白毛",
    }
    if with_head:
        desc["head_bbox"] = [0.3, 0.1, 0.4, 0.4]
    content = json.dumps(desc, ensure_ascii=False)

    async def _fake_call_omni(payload, config, type="realtime"):
        if payload_holder is not None:
            payload_holder["payload"] = payload
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(obs, "call_omni", _fake_call_omni)


@pytest.mark.asyncio
async def test_omni_describe_parses_description(monkeypatch):
    holder = {}
    _stub_omni(monkeypatch, holder)
    desc, head = await obs._omni_describe(_frame(), grounding=False)
    assert desc["species"] == "猫"
    assert desc["summary"].startswith("中等体型")
    assert head is None
    # crop 以 image/jpeg 进 payload 的 crops
    assert holder["payload"]["crops"][0]["media_type"] == "image/jpeg"
    assert "head_bbox" not in obs.OBSERVE_SYSTEM_PROMPT  # 关闭时 prompt 不含 grounding 段


@pytest.mark.asyncio
async def test_omni_describe_grounding_returns_head_bbox(monkeypatch):
    _stub_omni(monkeypatch, with_head=True)
    desc, head = await obs._omni_describe(_frame(), grounding=True)
    assert head == [0.3, 0.1, 0.4, 0.4]
    assert "head_bbox" not in desc  # head_bbox 已从 description 弹出


# ── observe_pet 端到端（mock detector + mock omni）──────────────────────────


@pytest.mark.asyncio
async def test_observe_pet_image_detected(monkeypatch):
    _stub_omni(monkeypatch)
    monkeypatch.setattr(
        obs,
        "default_detector",
        lambda: SimpleNamespace(detect_pets=lambda f: [_det(20, 20, 80, 80)]),
    )
    ok, buf = cv2.imencode(".jpg", _frame(160, 160))
    res = await obs.observe_pet(buf.tobytes(), is_video=False, grounding=False)
    assert res["detected"] is True
    assert res["description"]["species"] == "猫"
    assert res["primary_crop_b64"]
    assert len(res["candidates"]) == 1


@pytest.mark.asyncio
async def test_observe_pet_no_pet_detected(monkeypatch):
    _stub_omni(monkeypatch)
    monkeypatch.setattr(
        obs, "default_detector", lambda: SimpleNamespace(detect_pets=lambda f: [])
    )
    ok, buf = cv2.imencode(".jpg", _frame(160, 160))
    res = await obs.observe_pet(buf.tobytes(), is_video=False, grounding=False)
    assert res["detected"] is False
    assert res["description"] is None
    assert res["candidates"] == []
