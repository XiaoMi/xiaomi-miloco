# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""固定输入源 clip_source 测试：无摄像头时以本地视频作为 omni 输入画面。

覆盖：clip 路径热读、解码缓存与窗口旋转、DeviceData 组装、
collect_batch 用 clip 替换摄像头、以及 clip 输入跑通 rule_only 全链路（mock omni）。
"""

import json

import cv2
import numpy as np
import pytest
from miloco.perception.collect import clip_source
from miloco.perception.collect.adapter_base import BaseDeviceAdapter
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.types import PerceptionDevice


@pytest.fixture(autouse=True)
def _reset_clip_cache():
    clip_source.reset_cache()
    yield
    clip_source.reset_cache()


def _write_clip(tmp_path, n_frames: int = 6, size=(64, 64)) -> str:
    """生成一段带运动（帧间亮度递增）的小 mp4，2fps。"""
    path = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 2.0, size)
    for i in range(n_frames):
        frame = np.full((size[1], size[0], 3), (i * 30 % 255, 60, 90), dtype=np.uint8)
        writer.write(frame)
    writer.release()
    assert path.exists() and path.stat().st_size > 0
    return str(path)


def _patch_clip_source(monkeypatch, path: str):
    """把 clip_source_path 钉死为给定路径（免去改 settings）。"""
    monkeypatch.setattr(clip_source, "clip_source_path", lambda: path)


# ─── 路径热读 ─────────────────────────────────────────────────────────────────


def test_clip_source_path_reads_settings(monkeypatch):
    from unittest.mock import patch

    with patch("miloco.config.get_settings") as mock_gs:
        engine = {"input": {"clip_source": "/tmp/a.mp4"}}
        mock_gs.return_value.perception.engine.get.side_effect = (
            lambda key, default=None: engine.get(key, default)
        )
        assert clip_source.clip_source_path() == "/tmp/a.mp4"
    # 缺省 → 关闭
    with patch("miloco.config.get_settings") as mock_gs:
        engine = {}
        mock_gs.return_value.perception.engine.get.side_effect = (
            lambda key, default=None: engine.get(key, default)
        )
        assert clip_source.clip_source_path() == ""


# ─── 解码缓存 + 窗口旋转 ─────────────────────────────────────────────────────


def test_take_window_frames_returns_whole_clip_and_rotates(tmp_path):
    path = _write_clip(tmp_path, n_frames=6)
    w1 = clip_source.take_window_frames(path)
    assert len(w1) == 6, "窗口应包含整段 clip"
    w2 = clip_source.take_window_frames(path)
    assert len(w2) == 6
    # 相邻窗口整体旋转 1 帧：w2 首帧 == w1 次帧；末帧环绕回 w1 首帧
    assert w2[0] is w1[1]
    assert w2[-1] is w1[0]


def test_decode_failure_returns_none(tmp_path):
    # 底层取帧对坏路径抛异常（由 build_clip_device_data 兜底）
    with pytest.raises(RuntimeError):
        clip_source.take_window_frames(str(tmp_path / "nope.mp4"))
    # build_clip_device_data 对坏路径返回 None（不抛，上层空窗告警）
    assert clip_source.build_clip_device_data(str(tmp_path / "nope.mp4")) is None


# ─── DeviceData 组装 ─────────────────────────────────────────────────────────


def test_build_clip_device_data(tmp_path):
    path = _write_clip(tmp_path, n_frames=6)
    dd = clip_source.build_clip_device_data(path)
    assert dd is not None
    assert dd.meta.did == clip_source.DID
    assert dd.meta.room_name == clip_source.ROOM
    assert len(dd.video) == 6
    assert dd.has_data and not dd.audio
    assert dd.window_start_unix_ms < dd.window_end_unix_ms
    assert dd.video[0].unix_ms >= dd.window_start_unix_ms


# ─── collect_batch 替换摄像头 ────────────────────────────────────────────────


class _FakeCameraAdapter(BaseDeviceAdapter):
    """假摄像头 adapter：恒有一台在线设备并有数据。"""

    device_type = "camera"

    def __init__(self):
        self._dev = PerceptionDevice(did="cam-1", name="假摄像头", device_type="camera", room_name="客厅")

    async def discover_devices(self, all_devices=None, online_only=True, cap=True):
        return {"cam-1": self._dev}

    async def connect_device(self, did: str) -> None:
        pass

    async def disconnect_device(self, did: str) -> None:
        pass

    def collect(self, did: str, *, drain: bool = True):
        from miloco.perception.schema import DecodedVideoFrame, DeviceData

        now = 1_700_000_000_000
        return DeviceData(
            meta=self._dev,
            video=[DecodedVideoFrame(frame=np.zeros((8, 8, 3), np.uint8), stream_ts=0)],
            window_start_ms=now - 3000,
            window_end_ms=now,
        )

    def get_connected_devices(self):
        return {"cam-1": self._dev}


def test_collect_batch_uses_clip_when_configured(tmp_path, monkeypatch):
    """clip_source 配置后：即使摄像头在线，batch 也只含 clip 设备（替换画面）。"""
    clip_path = _write_clip(tmp_path)
    _patch_clip_source(monkeypatch, clip_path)
    collector = MultimodalCollector([_FakeCameraAdapter()])

    batch = collector.collect_batch()
    assert list(batch.devices.keys()) == [clip_source.DID]
    assert not batch.empty
    assert len(batch.devices[clip_source.DID].video) == 6


def test_collect_batch_without_clip_keeps_cameras(tmp_path, monkeypatch):
    """未配置 clip_source：行为不变，正常走摄像头。"""
    _patch_clip_source(monkeypatch, "")
    collector = MultimodalCollector([_FakeCameraAdapter()])
    batch = collector.collect_batch()
    assert list(batch.devices.keys()) == ["cam-1"]


def test_collect_batch_clip_unavailable_gives_empty_batch(tmp_path, monkeypatch):
    """clip 路径配错 / 解码失败：batch 为空（不崩，管线照常空轮询）。"""
    _patch_clip_source(monkeypatch, str(tmp_path / "nope.mp4"))
    collector = MultimodalCollector([_FakeCameraAdapter()])
    batch = collector.collect_batch()
    assert batch.empty


# ─── active sources：无摄像头时也能驱动感知循环 ───────────────────────────────


def test_active_sources_includes_clip_device_without_cameras(tmp_path, monkeypatch):
    """runner._tick 的闸 ``if not get_all_active_sources(): return``：
    clip_source 配置后，即使没有任何摄像头，active sources 也非空 → 感知循环继续向下跑。"""
    clip_path = _write_clip(tmp_path)
    _patch_clip_source(monkeypatch, clip_path)
    collector = MultimodalCollector([])  # 无任何摄像头
    sources = collector.get_all_active_sources()
    assert clip_source.DID in sources
    assert sources[clip_source.DID].room_name == clip_source.ROOM


def test_active_sources_clip_adds_to_cameras(tmp_path, monkeypatch):
    """clip_source 配置 + 摄像头在线：active sources 两者都在（batch 内容仍由 clip 替换）。"""
    clip_path = _write_clip(tmp_path)
    _patch_clip_source(monkeypatch, clip_path)
    collector = MultimodalCollector([_FakeCameraAdapter()])
    sources = collector.get_all_active_sources()
    assert "cam-1" in sources and clip_source.DID in sources


def test_active_sources_without_clip_unchanged(tmp_path, monkeypatch):
    """未配置 clip_source：active sources 行为不变。"""
    _patch_clip_source(monkeypatch, "")
    collector = MultimodalCollector([_FakeCameraAdapter()])
    assert list(collector.get_all_active_sources().keys()) == ["cam-1"]


# ─── 规则下发：clip 虚拟源接收全部规则 ───────────────────────────────────────


def _rule(rule_id: str, name: str, query: str, dids: list[str] | None):
    cond: dict = {"query": query}
    if dids is not None:
        cond["perceive_device_ids"] = dids
    return {"id": rule_id, "name": name, "condition": cond}


def test_dispatch_rules_to_clip_source_ignores_device_binding():
    """规则绑定了摄像头 did 也应全量下发到 clip_source（本地视频替换了所有摄像头画面）。"""
    from miloco.perception.engine.api import _dispatch_rules_for_device

    rules = [
        _rule("r1", "有人读书", "有人坐在床上看书", ["1144206310"]),
        _rule("r2", "打扫地面", "地面有垃圾", ["1144206310"]),
    ]
    dispatched = _dispatch_rules_for_device(rules, clip_source.DID)
    assert [r["id"] for r in dispatched] == ["r1", "r2"]


def test_dispatch_rules_normal_device_binding():
    """普通摄像头设备：绑定其他 did 的规则不下发，广播/绑定命中的下发。"""
    from miloco.perception.engine.api import _dispatch_rules_for_device

    rules = [
        _rule("r1", "绑定本机", "q1", ["cam-1"]),
        _rule("r2", "绑定他机", "q2", ["cam-2"]),
        _rule("r3", "物理did命中", "q3", ["cam-1"]),
        _rule("r4", "广播", "q4", None),
    ]
    dispatched = _dispatch_rules_for_device(rules, "cam-1")
    assert [r["id"] for r in dispatched] == ["r1", "r3", "r4"]


# ─── clip 输入跑通 rule_only 全链路（mock omni） ─────────────────────────────


@pytest.mark.asyncio
async def test_clip_source_rule_only_pipeline(tmp_path, monkeypatch):
    """无摄像头：clip → collect → to_batched_snapshot → run_batch_pipeline(rule_only)
    → 解析出 matched_rules。整个管线在无摄像头环境下可跑。"""
    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.engine.pipeline import run_batch_pipeline
    from miloco.perception.engine.types import OmniContext, RuleCondition

    clip_path = _write_clip(tmp_path)
    _patch_clip_source(monkeypatch, clip_path)
    collector = MultimodalCollector([])  # 无任何摄像头
    batch = collector.collect_batch()
    assert not batch.empty

    snapshots = batch.to_batched_snapshot()
    assert snapshots is not None
    assert snapshots.snapshots[0].device.did == clip_source.DID
    assert len(snapshots.snapshots[0].video.frames) == 6

    config = PerceptionConfig(rule_only=True)
    config.omni.api_key = "test-key"
    ctx = OmniContext(
        rule_only=True,
        rule_conditions=[
            RuleCondition(rule_id="r1", rule_name="手扫地", query="人手出现在画面中"),
        ],
    )

    captured: dict = {}

    async def _fake_call_omni(payload, config, type="realtime"):
        captured["payload"] = payload
        return {
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
            "usage": {"prompt_tokens": 66, "completion_tokens": 9},
        }

    monkeypatch.setattr(
        "miloco.perception.engine.omni.omni.call_omni", _fake_call_omni
    )
    result = await run_batch_pipeline(
        snapshots,
        {clip_source.DID: ctx},
        config,
    )

    assert not result.rooms[clip_source.ROOM].omni_outputs[clip_source.DID].skipped
    out = result.rooms[clip_source.ROOM].omni_outputs[clip_source.DID]
    assert [m.rule_id for m in out.matched_rules] == ["r1"]
    assert "video_base64" in captured["payload"]
    assert "audio_base64" not in captured["payload"]
