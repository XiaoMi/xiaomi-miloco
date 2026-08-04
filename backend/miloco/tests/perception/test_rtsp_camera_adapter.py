from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter


class _FakeKV:
    def get(self, key: str, default: str | None = None) -> str | None:
        return default


class _FakeRtspService:
    def __init__(self):
        self.camera = SimpleNamespace(
            did="rtsp:test",
            name="Entrance",
            room_name="RTSP",
        )
        self.callback = None
        self.removed = None

    def list_records(self):
        return [self.camera]

    def get(self, did: str):
        return self.camera if did == self.camera.did else None

    def is_online(self, did: str) -> bool:
        return did == self.camera.did

    def add_frame_callback(self, did: str, key: str, callback):
        assert did == self.camera.did
        self.callback = callback

    def remove_frame_callback(self, did: str, key: str):
        self.removed = (did, key)


def test_rtsp_camera_works_without_miot_authentication(monkeypatch):
    rtsp = _FakeRtspService()
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter.get_rtsp_service", lambda: rtsp
    )
    proxy = SimpleNamespace(is_authenticated=False, _kv_repo=_FakeKV())
    adapter = CameraDeviceAdapter(proxy)  # type: ignore[arg-type]

    discovered = asyncio.run(adapter.discover_devices())
    assert set(discovered) == {"rtsp:test"}

    asyncio.run(adapter.connect_device("rtsp:test", discovered["rtsp:test"]))
    assert rtsp.callback is not None
    rtsp.callback(
        "rtsp:test",
        np.zeros((4, 4, 3), dtype=np.uint8),
        1_000,
        1_700_000_000_000,
        1_700_000_000_000,
    )

    data = adapter.collect("rtsp:test", drain=False)
    assert data is not None
    assert data.meta.did == "rtsp:test"
    assert len(data.video) == 1
    assert data.audio == []

    asyncio.run(adapter.disconnect_device("rtsp:test"))
    assert rtsp.removed == ("rtsp:test", f"perception:{id(adapter)}")
