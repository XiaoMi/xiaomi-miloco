"""realtime_perceive 的 rule 过滤分发逻辑（api.py:728-753）——确认 rule 按
`condition.perceive_device_ids` 精确下发到对应 device，不再 room-级扩散。"""

from unittest.mock import patch

import numpy as np
import pytest
from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.config import PerceptionConfig
from miloco.perception.engine.types import BatchPipelineResult
from miloco.perception.types import (
    AudioFrame,
    AudioStream,
    BatchedSnapshot,
    DeviceSnapshot,
    PerceptionDevice,
    VideoFrame,
    VideoStream,
)


def _make_snapshot(did: str, room_name: str) -> DeviceSnapshot:
    device = PerceptionDevice(did=did, name=did, device_type="camera", room_name=room_name)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    video = VideoStream(
        frames=[VideoFrame(data=frame, timestamp=1000.0)], width=100, height=100
    )
    audio = AudioStream(
        frames=[AudioFrame(data=np.zeros(16000, dtype=np.int16), timestamp=1000.0)],
        sample_rate=16000,
    )
    return DeviceSnapshot(
        device=device,
        start_timestamp=1000.0,
        end_timestamp=4000.0,
        video=video,
        audio=audio,
    )


def _rule(rule_id: str, perceive_device_ids: list[str]) -> dict:
    return {
        "id": rule_id,
        "name": rule_id,
        "condition": {
            "perceive_device_ids": perceive_device_ids,
            "query": f"q-{rule_id}",
        },
    }


@pytest.mark.asyncio
async def test_rules_filtered_per_device():
    """同 room 多 cam + 跨 room cam，rule 按 did 精确下发，不扩散到同 room 其他 cam。"""
    cam_a = _make_snapshot("cam_a", "客厅")
    cam_b = _make_snapshot("cam_b", "客厅")
    cam_c = _make_snapshot("cam_c", "卧室")
    batch = BatchedSnapshot(snapshots=[cam_a, cam_b, cam_c])

    rules = [
        _rule("r_only_a", ["cam_a"]),
        _rule("r_only_b", ["cam_b"]),
        _rule("r_broadcast", []),
        _rule("r_only_c", ["cam_c"]),
    ]

    engine = PerceptionEngine(PerceptionConfig())
    captured: dict = {}

    async def fake_run_batch_pipeline(batch_, contexts, *args, **kwargs):
        captured["contexts"] = contexts
        return BatchPipelineResult()

    with patch(
        "miloco.perception.engine.pipeline.run_batch_pipeline",
        side_effect=fake_run_batch_pipeline,
    ):
        await engine.realtime_perceive(batch, rules=rules)

    contexts = captured["contexts"]
    assert set(contexts.keys()) == {"cam_a", "cam_b", "cam_c"}

    def rule_ids(did: str) -> set[str]:
        return {rc.rule_id for rc in contexts[did].rule_conditions}

    assert rule_ids("cam_a") == {"r_only_a", "r_broadcast"}
    assert rule_ids("cam_b") == {"r_only_b", "r_broadcast"}
    assert rule_ids("cam_c") == {"r_only_c", "r_broadcast"}


@pytest.mark.asyncio
async def test_empty_rules_list_yields_empty_rule_conditions():
    cam = _make_snapshot("cam_x", "书房")
    batch = BatchedSnapshot(snapshots=[cam])

    engine = PerceptionEngine(PerceptionConfig())
    captured: dict = {}

    async def fake_run_batch_pipeline(batch_, contexts, *args, **kwargs):
        captured["contexts"] = contexts
        return BatchPipelineResult()

    with patch(
        "miloco.perception.engine.pipeline.run_batch_pipeline",
        side_effect=fake_run_batch_pipeline,
    ):
        await engine.realtime_perceive(batch, rules=[])

    assert captured["contexts"]["cam_x"].rule_conditions == []


@pytest.mark.asyncio
async def test_room_name_still_attached_to_context():
    """room_name 仍要由 device.room_name 注入到 OmniContext（prompt 场景参考依赖）。"""
    cam = _make_snapshot("cam_x", "厨房")
    batch = BatchedSnapshot(snapshots=[cam])

    engine = PerceptionEngine(PerceptionConfig())
    captured: dict = {}

    async def fake_run_batch_pipeline(batch_, contexts, *args, **kwargs):
        captured["contexts"] = contexts
        return BatchPipelineResult()

    with patch(
        "miloco.perception.engine.pipeline.run_batch_pipeline",
        side_effect=fake_run_batch_pipeline,
    ):
        await engine.realtime_perceive(batch, rules=[])

    assert captured["contexts"]["cam_x"].room_name == "厨房"


@pytest.mark.asyncio
async def test_context_current_time_wired_to_fmt_clock(monkeypatch):
    """OmniContext.current_time 必须走 _fmt_clock（部署时区），非裸 fromtimestamp。

    双时区断言使回退接线（改回 host 时钟）在任何 CI host 上都必红：
    snapshot.start_timestamp=1000ms = 1970-01-01T00:00:01Z。
    """
    from miloco.config import reset_settings

    cam = _make_snapshot("cam_x", "书房")

    engine = PerceptionEngine(PerceptionConfig())
    captured: dict = {}

    async def fake_run_batch_pipeline(batch_, contexts, *args, **kwargs):
        captured["contexts"] = contexts
        return BatchPipelineResult()

    try:
        with patch(
            "miloco.perception.engine.pipeline.run_batch_pipeline",
            side_effect=fake_run_batch_pipeline,
        ):
            monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
            reset_settings()
            await engine.realtime_perceive(BatchedSnapshot(snapshots=[cam]), rules=[])
            assert captured["contexts"]["cam_x"].current_time == "08:00:01"

            monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
            reset_settings()
            await engine.realtime_perceive(BatchedSnapshot(snapshots=[cam]), rules=[])
            assert captured["contexts"]["cam_x"].current_time == "16:00:01"
    finally:
        reset_settings()


# ---------- device_rule_map 构造正确性(层 1)----------
# device_rule_map: did → 本 batch 实际下发的 rule_id 列表。
# client.py EXITED 阶段据此精确推退状态机桶,绑 cam_A 的 rule 不会被只有 cam_B 的 batch
# 错误推退。每个 case 都校验 result.device_rule_map 而非 contexts。


async def _run_perceive(batch, rules):
    """跑一次 perceive,只关心 device_rule_map 构造,不模拟 omni 调用。"""
    engine = PerceptionEngine(PerceptionConfig())

    async def fake_run_batch_pipeline(batch_, contexts, *args, **kwargs):
        return BatchPipelineResult()

    with patch(
        "miloco.perception.engine.pipeline.run_batch_pipeline",
        side_effect=fake_run_batch_pipeline,
    ):
        return await engine.realtime_perceive(batch, rules=rules)


@pytest.mark.asyncio
async def test_device_rule_map_populated_per_device():
    """rule 绑 cam_a,batch=[a,b]:map[a] 含 rule,map[b] 不含。"""
    batch = BatchedSnapshot(snapshots=[
        _make_snapshot("cam_a", "客厅"),
        _make_snapshot("cam_b", "客厅"),
    ])
    result = await _run_perceive(batch, [_rule("r_only_a", ["cam_a"])])
    assert result.device_rule_map == {"cam_a": ["r_only_a"], "cam_b": []}


@pytest.mark.asyncio
async def test_device_rule_map_excludes_unbound_when_target_missing():
    """rule 绑 cam_a,batch=[b]:map[b] 不含 rule,且 map 不出现 cam_a key。"""
    batch = BatchedSnapshot(snapshots=[_make_snapshot("cam_b", "客厅")])
    result = await _run_perceive(batch, [_rule("r_only_a", ["cam_a"])])
    assert result.device_rule_map == {"cam_b": []}


@pytest.mark.asyncio
async def test_device_rule_map_empty_perceive_ids_broadcasts():
    """rule.perceive_device_ids 空 → 广播给 batch 所有 device。"""
    batch = BatchedSnapshot(snapshots=[
        _make_snapshot("cam_a", "客厅"),
        _make_snapshot("cam_b", "卧室"),
    ])
    result = await _run_perceive(batch, [_rule("r_broadcast", [])])
    assert result.device_rule_map == {"cam_a": ["r_broadcast"], "cam_b": ["r_broadcast"]}


@pytest.mark.asyncio
async def test_device_rule_map_rule_bound_to_multiple_dids():
    """rule 绑 [a, b],batch=[a, b, c]:map[a]/map[b] 含,map[c] 不含。"""
    batch = BatchedSnapshot(snapshots=[
        _make_snapshot("cam_a", "客厅"),
        _make_snapshot("cam_b", "客厅"),
        _make_snapshot("cam_c", "厨房"),
    ])
    result = await _run_perceive(batch, [_rule("r_a_b", ["cam_a", "cam_b"])])
    assert result.device_rule_map == {
        "cam_a": ["r_a_b"],
        "cam_b": ["r_a_b"],
        "cam_c": [],
    }


@pytest.mark.asyncio
async def test_device_rule_map_cross_room_isolation():
    """同一 rule 绑跨房间多 did,batch 含旁观房间的 cam → 旁观 cam 不下发。"""
    batch = BatchedSnapshot(snapshots=[
        _make_snapshot("cam_a", "客厅"),
        _make_snapshot("cam_b", "卧室"),
        _make_snapshot("cam_c", "厨房"),  # 旁观房间
    ])
    rules = [_rule("r_living_bedroom", ["cam_a", "cam_b"])]
    result = await _run_perceive(batch, rules)
    assert result.device_rule_map["cam_a"] == ["r_living_bedroom"]
    assert result.device_rule_map["cam_b"] == ["r_living_bedroom"]
    assert result.device_rule_map["cam_c"] == []


@pytest.mark.asyncio
async def test_device_rule_map_multiple_rules_per_device():
    """多 rule × 多 device:per-device 聚合后顺序与 rules 一致。"""
    batch = BatchedSnapshot(snapshots=[
        _make_snapshot("cam_a", "客厅"),
        _make_snapshot("cam_b", "客厅"),
    ])
    rules = [
        _rule("rX", ["cam_a"]),
        _rule("rY", ["cam_a", "cam_b"]),
        _rule("rZ", []),  # broadcast
    ]
    result = await _run_perceive(batch, rules)
    assert result.device_rule_map["cam_a"] == ["rX", "rY", "rZ"]
    assert result.device_rule_map["cam_b"] == ["rY", "rZ"]


@pytest.mark.asyncio
async def test_device_rule_map_empty_rules_yields_empty_lists():
    """rules=[]:map 每个 did 都是空 list(不是 missing key)。"""
    batch = BatchedSnapshot(snapshots=[
        _make_snapshot("cam_a", "客厅"),
        _make_snapshot("cam_b", "卧室"),
    ])
    result = await _run_perceive(batch, [])
    assert result.device_rule_map == {"cam_a": [], "cam_b": []}
