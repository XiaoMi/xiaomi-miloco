"""Multi-channel (dual-lens) camera perception regression tests.

A single-lens camera keeps its bare did; each lens of a multi-channel camera
gets a synthetic did ``{did}:ch{n}`` so downstream keying (device_results,
tracking, identity) never collides across lenses. These tests pin:

- ``split_channel_did`` / ``_physical_did`` parsing round-trips
- ``discover`` expands a dual camera into two synthetic dids, single stays bare
- the feed cap counts by *stream* (a dual camera eats 2 slots)
- connect/disconnect route the physical did + channel to the SDK
- each channel produces a distinct ``DeviceData.meta.did`` (the collision fix)
- rules bind at either granularity (physical did = whole camera, or one lens)
- the voice allow-list (stored by *physical* did) still gates a multi-channel
  camera's audio: both gate sites normalize the synthetic did before matching
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.miot import filter as miot_filter
from miloco.perception.collect.camera_adapter import (
    CameraDeviceAdapter,
    split_channel_did,
)
from miloco.perception.engine.api import _physical_did


class _FakeKV:
    def __init__(self, initial: dict[str, str] | None = None):
        self._store = dict(initial or {})

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._store.get(key, default)

    def set(self, key: str, value: str) -> bool:
        self._store[key] = value
        return True


def _cam(did: str, *, home_id: str = "H1", channel_count: int | None = None):
    return SimpleNamespace(
        did=did,
        home_id=home_id,
        name=f"cam-{did}",
        online=True,
        lan_online=True,
        channel_count=channel_count,
    )


# ── did parsing ──────────────────────────────────────────────────────────────


def test_split_channel_did_roundtrip():
    assert split_channel_did("cam1") == ("cam1", 0)
    assert split_channel_did("cam1:ch0") == ("cam1", 0)
    assert split_channel_did("cam1:ch1") == ("cam1", 1)
    # 物理 did 里含冒号也不误伤（只认末尾的 :ch{n}）。
    assert split_channel_did("a:b:ch2") == ("a:b", 2)


def test_physical_did_matches_adapter_parsing():
    assert _physical_did("cam1") == "cam1"
    assert _physical_did("cam1:ch1") == "cam1"
    # 与 adapter 的拆分口径一致。
    for did in ("cam1", "cam1:ch0", "cam1:ch1"):
        assert _physical_did(did) == split_channel_did(did)[0]


# ── feed cap counts by stream, not device ─────────────────────────────────────


def test_select_active_caps_by_stream_count(monkeypatch):
    monkeypatch.setattr("miloco.miot.filter.MAX_ENABLED_CAMERAS", 3)
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cameras = {
        "c1": _cam("c1", channel_count=2),  # 双摄 = 2 路
        "c2": _cam("c2"),  # 单摄 = 1 路 → 累计 3，正好到顶
        "c3": _cam("c3"),  # 第 4 路 → 超限，被截断
    }
    # 按流路数：c1(2)+c2(1)=3 ≤ 3，c3 会让总数到 4 → 排除。
    assert miot_filter.select_active_camera_dids(kv, cameras) == ["c1", "c2"]
    # cap=False（列全集语义）不受上限影响。
    assert set(miot_filter.select_active_camera_dids(kv, cameras, cap=False)) == {
        "c1",
        "c2",
        "c3",
    }


def test_select_active_single_cameras_unchanged(monkeypatch):
    """全单摄时口径与旧行为一致（回归护栏）。"""
    monkeypatch.setattr("miloco.miot.filter.MAX_ENABLED_CAMERAS", 2)
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cameras = {"c3": _cam("c3"), "c1": _cam("c1"), "c2": _cam("c2")}
    assert miot_filter.select_active_camera_dids(kv, cameras) == ["c1", "c2"]


# ── discover expands channels ─────────────────────────────────────────────────


def _mioT_cam(did: str, *, home_id="H1", channel_count=None):
    """MIoTCameraInfo skipping validation (only the fields discover reads)."""
    from miot.types import MIoTCameraInfo

    return MIoTCameraInfo.model_construct(
        did=did,
        name=f"cam-{did}",
        online=True,
        lan_online=True,
        room_name="客厅",
        home_id=home_id,
        channel_count=channel_count,
    )


def test_discover_expands_dual_camera_keeps_single_bare():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    proxy = SimpleNamespace(_kv_repo=kv, _camera_awake_cache=None)
    adapter = CameraDeviceAdapter(miot_proxy=proxy)  # type: ignore[arg-type]

    all_devices = {
        "dual": _mioT_cam("dual", channel_count=2),
        "single": _mioT_cam("single", channel_count=1),
    }
    result = adapter._filter_cameras_from_all(all_devices)

    assert set(result) == {"dual:ch0", "dual:ch1", "single"}
    # 合成身份落在 PerceptionDevice.did 上，供下游按通道分桶。
    assert result["dual:ch0"].did == "dual:ch0"
    assert result["dual:ch1"].did == "dual:ch1"
    assert result["single"].did == "single"
    # 相机名不带通道标签（通道标签是前端关注点）。
    assert result["dual:ch0"].name == "cam-dual"


# ── connect / collect / disconnect route physical did + channel ────────────────


class _RecordingProxy:
    """Records the (physical_did, channel) passed to the SDK stream calls."""

    is_authenticated = True

    def __init__(self):
        self.video_subs: list[tuple[str, int]] = []
        self.audio_subs: list[tuple[str, int]] = []
        self.stops: list[tuple[str, int, int]] = []

    def get_cached_camera(self, did: str):
        return SimpleNamespace(
            model_dump=lambda: {
                "did": did,
                "name": f"cam-{did}",
                "online": True,
                "lan_online": True,
                "room_name": "客厅",
            }
        )

    async def start_camera_decode_video_stream(self, did, channel, cb):
        self.video_subs.append((did, channel))
        return 10 + channel

    async def start_camera_decode_audio_stream(self, did, channel, cb):
        self.audio_subs.append((did, channel))
        return 20 + channel

    async def stop_camera_decode_video_stream(self, did, channel, reg_id):
        self.stops.append((did, channel, reg_id))

    async def stop_camera_decode_audio_stream(self, did, channel, reg_id):
        self.stops.append((did, channel, reg_id))


def test_connect_dual_channels_routes_physical_did_and_channel():
    proxy = _RecordingProxy()
    adapter = CameraDeviceAdapter(miot_proxy=proxy)  # type: ignore[arg-type]

    asyncio.run(adapter.connect_device("dual:ch0", source=object()))  # type: ignore[arg-type]
    asyncio.run(adapter.connect_device("dual:ch1", source=object()))  # type: ignore[arg-type]

    # SDK 建流用物理 did + 各自通道号。
    assert proxy.video_subs == [("dual", 0), ("dual", 1)]
    assert proxy.audio_subs == [("dual", 0), ("dual", 1)]
    # 适配器按合成 did 分别持有两条通道状态。
    assert set(adapter._devices) == {"dual:ch0", "dual:ch1"}
    # get_connected_devices 回吐合成 did（供 base sync 的集合差与 collector 分桶）。
    assert set(adapter.get_connected_devices()) == {"dual:ch0", "dual:ch1"}


def test_each_channel_produces_distinct_meta_did():
    """两路帧不再交错进同一 DeviceData —— 每条通道产出独立 meta.did。"""
    proxy = _RecordingProxy()
    adapter = CameraDeviceAdapter(miot_proxy=proxy)  # type: ignore[arg-type]
    asyncio.run(adapter.connect_device("dual:ch0", source=object()))  # type: ignore[arg-type]
    asyncio.run(adapter.connect_device("dual:ch1", source=object()))  # type: ignore[arg-type]

    for syn_did in ("dual:ch0", "dual:ch1"):
        cb = adapter._make_decoded_video_callback(syn_did)
        asyncio.run(
            cb(syn_did, np.zeros((2, 2, 3), dtype=np.uint8), 1_000, 0, 0, 0)
        )

    d0 = adapter.collect("dual:ch0", drain=False)
    d1 = adapter.collect("dual:ch1", drain=False)
    assert d0 is not None and d1 is not None
    assert d0.meta.did == "dual:ch0"
    assert d1.meta.did == "dual:ch1"


def test_disconnect_channel_routes_physical_did_and_channel():
    proxy = _RecordingProxy()
    adapter = CameraDeviceAdapter(miot_proxy=proxy)  # type: ignore[arg-type]
    asyncio.run(adapter.connect_device("dual:ch1", source=object()))  # type: ignore[arg-type]

    asyncio.run(adapter.disconnect_device("dual:ch1"))

    assert "dual:ch1" not in adapter._devices
    # 停流同样用物理 did + 通道号（reg_id 为建流时返回的 11 / 21）。
    assert ("dual", 1, 11) in proxy.stops
    assert ("dual", 1, 21) in proxy.stops


# ── rule target validation accepts both granularities ─────────────────────────


def test_valid_perceive_device_ids_include_physical_dids(monkeypatch):
    from miloco.rule.service import RuleService

    devices = [SimpleNamespace(did="dual:ch0"), SimpleNamespace(did="dual:ch1"),
               SimpleNamespace(did="single")]
    mgr = MagicMock()
    mgr.perception_service.get_devices = AsyncMock(return_value=devices)
    monkeypatch.setattr("miloco.manager.get_manager", lambda: mgr)

    svc = RuleService.__new__(RuleService)  # bypass __init__ — method uses no self
    valid = asyncio.run(svc._get_valid_perceive_device_ids())

    # 合成通道 did + 物理 did 都是合法绑定目标。
    assert "dual:ch0" in valid and "dual:ch1" in valid
    assert "dual" in valid  # 绑整台相机
    assert "single" in valid
    # 不重复收录已存在的裸 did。
    assert valid.count("single") == 1


# ── voice gate keys by physical did (allow-list stores physical dids) ─────────


def _snapshot(did: str, *, audio):
    from miloco.perception.types import DeviceSnapshot, PerceptionDevice

    return DeviceSnapshot(
        device=PerceptionDevice(did=did, name=f"cam-{did}", device_type="camera"),
        start_timestamp=0.0,
        end_timestamp=1000.0,
        audio=audio,
    )


def test_strip_voice_audio_keys_by_physical_did(monkeypatch):
    """引擎入口剥音频：白名单存物理 did，双摄两条通道（合成 did）都应放行。"""
    from miloco.perception.engine import api as engine_api
    from miloco.perception.types import BatchedSnapshot

    # 白名单只含物理 did（前端 / toggle_camera_voice 都按整台走）。
    monkeypatch.setattr(engine_api, "_voice_allowed_dids", lambda: {"dual"})

    eng = engine_api.PerceptionEngine.__new__(engine_api.PerceptionEngine)
    eng._mic_off_logged = set()
    eng._audio_tail = {}
    eng._pending_speech = {}
    eng._pending_speech_rounds = {}

    ch0, ch1 = _snapshot("dual:ch0", audio=object()), _snapshot("dual:ch1", audio=object())
    other = _snapshot("other", audio=object())  # 不在白名单 → 应被剥
    batch = BatchedSnapshot(snapshots=[ch0, ch1, other])

    eng._strip_unauthorized_voice_audio(batch)

    # 双摄两路（物理 did "dual" 在白名单）音频保留；未授权相机被剥。
    assert ch0.audio is not None and ch1.audio is not None
    assert other.audio is None
    # 日志去重按物理 did（双摄只记一次 "dual"，不是两条通道各记）。
    assert eng._mic_off_logged == {"other"}


def test_filter_voice_enabled_keys_by_physical_did(monkeypatch):
    """dispatch/落库兜底闸门：source_device_ids 是合成 did，比对前归一到物理 did。"""
    from miloco.perception.client import _filter_voice_enabled
    from miloco.perception.types import Speech

    monkeypatch.setattr(
        "miloco.miot.filter.voice_allowed_camera_dids", lambda _kv: {"dual"}
    )
    monkeypatch.setattr("miloco.manager.get_manager", lambda: MagicMock())

    def _sp(did: str) -> Speech:
        return Speech(
            needs_response=True, speaker="用户", content="开灯", source_device_ids=[did]
        )

    kept = _filter_voice_enabled([_sp("dual:ch0"), _sp("dual:ch1"), _sp("other:ch0")])

    # 双摄两路放行（物理 "dual" 已授权），未授权相机的语音丢弃。
    assert {s.source_device_ids[0] for s in kept} == {"dual:ch0", "dual:ch1"}
