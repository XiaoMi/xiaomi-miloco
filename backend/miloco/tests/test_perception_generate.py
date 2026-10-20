# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""规则生成式动作的感知侧：最近帧 → JPEG crop → 一次 omni → 文本。"""

import base64
import json

import cv2
import numpy as np
import pytest
from miloco.perception import generate as generate_mod
from miloco.perception.generate import GenerateError, generate_text


class _Collector:
    def __init__(self, frames: dict[str, np.ndarray | None]):
        self.frames = frames
        self.seen: list[str] = []

    def peek_latest_frame(self, did: str):
        self.seen.append(did)
        return self.frames.get(did)


def _frame(h: int = 480, w: int = 640) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _raw(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _decode(crop: dict) -> np.ndarray:
    buf = np.frombuffer(base64.b64decode(crop["data"]), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


@pytest.fixture
def omni(monkeypatch):
    """记录 call_omni / resolve_live_omni_config 的入参，返回可改的响应。"""
    calls: list[dict] = []
    state = {"raw": _raw('{"result":"你今天的配色很精神"}')}

    async def _call(payload, config, type="realtime"):
        calls.append({"payload": payload, "config": config, "type": type})
        return state["raw"]

    def _resolve(base):
        calls.append({"base": base})
        return base

    monkeypatch.setattr(generate_mod, "call_omni", _call)
    monkeypatch.setattr(generate_mod, "resolve_live_omni_config", _resolve)
    return {"calls": calls, "state": state}


@pytest.mark.asyncio
async def test_generate_sends_crops_with_fixed_contract(omni):
    collector = _Collector({"cam-1": _frame()})

    text = await generate_text(
        collector, prompt="夸夸画面里的人", sources=["cam-1"], max_chars=12
    )

    assert text == "你今天的配色很精神"
    call = omni["calls"][-1]
    assert call["type"] == "on_demand"
    assert call["payload"]["user_content"] == "夸夸画面里的人"
    assert "不超过 12 个字" in call["payload"]["system_prompt"]
    assert '{"result"' in call["payload"]["system_prompt"]
    assert len(call["payload"]["crops"]) == 1
    assert call["payload"]["crops"][0]["media_type"] == "image/jpeg"
    assert _decode(call["payload"]["crops"][0]).shape[:2] == (480, 640)
    assert call["config"].temperature == 0.5
    assert call["config"].max_completion_tokens == 512


@pytest.mark.asyncio
async def test_generate_downscales_and_never_upscales(omni):
    collector = _Collector(
        {"wide": _frame(1000, 2000), "small": _frame(240, 320)}
    )

    await generate_text(collector, prompt="看看", sources=["wide", "small"])

    crops = omni["calls"][-1]["payload"]["crops"]
    assert _decode(crops[0]).shape[:2] == (640, 1280)
    assert _decode(crops[1]).shape[:2] == (240, 320)


@pytest.mark.asyncio
async def test_generate_skips_sources_without_frames_and_caps_at_three(omni):
    collector = _Collector(
        {"a": None, "b": _frame(), "c": None, "d": _frame()}
    )

    await generate_text(collector, prompt="看看", sources=["a", "b", "c", "d"])

    assert collector.seen == ["a", "b", "c"]
    assert len(omni["calls"][-1]["payload"]["crops"]) == 1


@pytest.mark.asyncio
async def test_generate_collapses_whitespace_and_truncates(omni):
    omni["state"]["raw"] = _raw(json.dumps({"result": "  你 好\n世界  "}))

    text = await generate_text(
        _Collector({"cam-1": _frame()}), prompt="看看", sources=["cam-1"],
        max_chars=3,
    )

    assert text == "你 好"


@pytest.mark.asyncio
async def test_generate_rejects_no_source(omni):
    with pytest.raises(GenerateError) as exc:
        await generate_text(_Collector({}), prompt="看看", sources=[])

    assert exc.value.code == "no_source"
    assert not omni["calls"]


@pytest.mark.asyncio
async def test_generate_rejects_no_frame(omni):
    with pytest.raises(GenerateError) as exc:
        await generate_text(
            _Collector({"cam-1": None}), prompt="看看", sources=["cam-1"]
        )

    assert exc.value.code == "no_frame"
    assert not omni["calls"]


@pytest.mark.asyncio
async def test_generate_maps_omni_failure(monkeypatch):
    async def _boom(payload, config, type="realtime"):
        raise TimeoutError("timed out")

    monkeypatch.setattr(generate_mod, "call_omni", _boom)
    monkeypatch.setattr(
        generate_mod, "resolve_live_omni_config", lambda base: base
    )

    with pytest.raises(GenerateError) as exc:
        await generate_text(
            _Collector({"cam-1": _frame()}), prompt="看看", sources=["cam-1"]
        )

    assert exc.value.code == "omni_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "这不是 JSON",
        "",
        '{"result": 1}',
        '["result"]',
    ],
)
async def test_generate_rejects_invalid_json(omni, content):
    omni["state"]["raw"] = _raw(content)

    with pytest.raises(GenerateError) as exc:
        await generate_text(
            _Collector({"cam-1": _frame()}), prompt="看看", sources=["cam-1"]
        )

    assert exc.value.code == "invalid_json"


@pytest.mark.asyncio
async def test_generate_treats_blank_result_as_empty(omni):
    omni["state"]["raw"] = _raw('{"result":"   "}')

    with pytest.raises(GenerateError) as exc:
        await generate_text(
            _Collector({"cam-1": _frame()}), prompt="看看", sources=["cam-1"]
        )

    assert exc.value.code == "empty_result"
