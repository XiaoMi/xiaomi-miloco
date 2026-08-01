# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""本地视觉感知通路的单测(不需要 GPU / 不需要边车进程)。

覆盖三类不变量:
1. **纯视觉不变量** —— speeches / env_sounds / suggestions 恒空。这不是"还没做",
   是刻意不产:让看不见音频的模型填这些字段只会得到脑补结果。
2. **规则语义** —— per-device 定向下发、命中回填 rule_id、fail-closed。
3. **失败降级** —— 编码失败 / 边车不可达时不产假事件,标 skipped 交由上层跳过。
"""

from __future__ import annotations

import numpy as np
import pytest

from miloco.perception.local_vision.engine import (
    LocalVisionEngine,
    _physical_did,
    _rules_for_device,
)
from miloco.perception.types import (
    BatchedSnapshot,
    DeviceSnapshot,
    PerceptionDevice,
    VideoFrame,
    VideoStream,
)


def _snapshot(did: str, room: str = "客厅", frames: int = 4) -> DeviceSnapshot:
    imgs = [
        VideoFrame(data=np.zeros((64, 64, 3), dtype=np.uint8), timestamp=float(i * 1000))
        for i in range(frames)
    ]
    return DeviceSnapshot(
        device=PerceptionDevice(did=did, name=f"cam-{did}", device_type="camera", room_name=room),
        video=VideoStream(frames=imgs, width=64, height=64),
        audio=None,
        start_timestamp=0.0,
        end_timestamp=float(frames * 1000),
    )


class _FakeClient:
    """替身边车:记录收到的请求,按脚本返回。"""

    def __init__(self, responses: list[dict] | None = None, fail: bool = False):
        self.responses = responses or []
        self.fail = fail
        self.calls: list[dict] = []

    async def perceive(self, video, rules, scene_ask=None, max_new_tokens=256, want_gate=True):
        from miloco.perception.local_vision.client import LocalVisionError

        self.calls.append({"rules": rules, "scene_ask": scene_ask, "bytes": len(video)})
        if self.fail:
            raise LocalVisionError("sidecar down")
        if not self.responses:
            return {"caption": "空", "rule_hits": [], "gate_p": None, "backend": "frames"}
        return self.responses.pop(0)


def _engine(client, **kw) -> LocalVisionEngine:
    return LocalVisionEngine(client, fps=2, **kw)


# ── 规则定向 ──────────────────────────────────────────────────────────────


def test_physical_did_strips_channel():
    assert _physical_did("cam1:ch0") == "cam1"
    assert _physical_did("cam1") == "cam1"


def test_rules_for_device_broadcast_and_targeted():
    rules = [
        {"id": "r1", "name": "广播", "condition": {"query": "q", "perceive_device_ids": []}},
        {"id": "r2", "name": "仅A", "condition": {"query": "q", "perceive_device_ids": ["camA"]}},
        {"id": "r3", "name": "绑物理机", "condition": {"query": "q", "perceive_device_ids": ["camB"]}},
    ]
    assert [r["id"] for r in _rules_for_device(rules, "camA")] == ["r1", "r2"]
    # 规则绑整台相机的物理 did 时,该机任一通道都要命中
    assert [r["id"] for r in _rules_for_device(rules, "camB:ch1")] == ["r1", "r3"]


# ── 纯视觉不变量 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_only_never_emits_audio_or_suggestions():
    """边车即使回了音频类字段也不该出现在结果里 —— 本通路没有音频输入。"""
    client = _FakeClient([{
        "caption": "客厅有人在看书",
        "rule_hits": [],
        "gate_p": 0.9,
        "backend": "codec",
        # 故意塞入不该被采信的字段
        "speeches": [{"speaker": "人", "content": "开灯"}],
        "env_sounds": ["玻璃碎裂声"],
        "suggestions": [{"event": "危险"}],
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert res is not None
    assert res.speeches == []
    assert res.env_sounds == []
    assert res.suggestions == []
    assert res.caption[0].description == "客厅有人在看书"


@pytest.mark.asyncio
async def test_static_rules_disabled_is_declared():
    """本通路不执行设备动作,必须显式声明,供上层告知用户。"""
    assert _engine(_FakeClient()).static_rules_disabled is True


# ── 命中回填 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_matched_rule_carries_rule_id_and_device():
    rules = [
        {"id": "r-sofa", "name": "沙发有人", "condition": {"query": "有人在沙发上", "perceive_device_ids": []}},
        {"id": "r-pet", "name": "有宠物", "condition": {"query": "有宠物", "perceive_device_ids": []}},
    ]
    client = _FakeClient([{
        "caption": "有人躺在沙发上",
        "rule_hits": [
            {"name": "沙发有人", "hit": True, "reason": "沙发上躺着一个人"},
            {"name": "有宠物", "hit": False, "reason": "没有宠物"},
        ],
        "gate_p": None,
        "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1", room="客厅")]), rules
    )
    assert res is not None
    assert len(res.matched_rules) == 1
    m = res.matched_rules[0]
    assert m.rule_id == "r-sofa"  # 回填的是 miloco 的 rule_id,不是模型给的名字
    assert m.room_name == "客厅"
    assert m.source_device_ids == ["cam1"]


@pytest.mark.asyncio
async def test_hit_name_mismatch_falls_back_to_name_lookup():
    """模型把顺序打乱时按名字找回,而不是错配到别的规则上。"""
    rules = [
        {"id": "r1", "name": "A", "condition": {"query": "qa", "perceive_device_ids": []}},
        {"id": "r2", "name": "B", "condition": {"query": "qb", "perceive_device_ids": []}},
    ]
    client = _FakeClient([{
        "caption": "x",
        "rule_hits": [{"name": "B", "hit": True, "reason": "b 成立"}],
        "gate_p": None, "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), rules
    )
    assert [m.rule_id for m in res.matched_rules] == ["r2"]


# ── 门控 ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_default_observes_but_never_skips():
    """默认阈值 0:门控概率只记录不决策(参考实现的门控是体育域训练的)。"""
    client = _FakeClient([{
        "caption": "静止的客厅", "rule_hits": [], "gate_p": 0.01, "backend": "codec",
    }])
    res = await _engine(client).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert len(res.caption) == 1                      # 低概率也没被丢
    assert res.timing["_gate_p_cam1"] == pytest.approx(0.01)  # 但留了痕


@pytest.mark.asyncio
async def test_gate_skips_when_threshold_configured():
    client = _FakeClient([{
        "caption": "静止的客厅", "rule_hits": [], "gate_p": 0.1, "backend": "codec",
    }])
    res = await _engine(client, gate_threshold=0.5).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert res.caption == []
    assert res.skipped is True


# ── 失败降级 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sidecar_failure_marks_skipped_not_fake_event():
    """边车挂了要标 skipped,绝不能产一条内容为空的"感知事件"。"""
    res = await _engine(_FakeClient(fail=True)).realtime_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), []
    )
    assert res is not None
    assert res.skipped is True
    assert res.error_code == "local_vision_unavailable"
    assert res.caption == []


@pytest.mark.asyncio
async def test_empty_batch_returns_none():
    assert await _engine(_FakeClient()).realtime_perceive(BatchedSnapshot(snapshots=[]), []) is None


@pytest.mark.asyncio
async def test_on_demand_uses_query_as_scene_ask():
    client = _FakeClient([{"caption": "有两个人在看电视", "rule_hits": [], "gate_p": None, "backend": "codec"}])
    out = await _engine(client).on_demand_perceive(
        BatchedSnapshot(snapshots=[_snapshot("cam1")]), "现在有人吗?"
    )
    assert out is not None
    assert out.answer == "有两个人在看电视"
    assert client.calls[0]["scene_ask"] == "现在有人吗?"
    assert client.calls[0]["rules"] == []
