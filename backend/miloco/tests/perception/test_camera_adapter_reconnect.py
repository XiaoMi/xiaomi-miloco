"""相机 manager 自愈测试：登录/LAN 竞态下的连接重试 + 按需补建。

覆盖 bug「登录瞬间相机 LAN 未就绪 → manager 没建成 → 永久不拉流」的两条修复：
- connect_device：两路流都没订上（manager 缺失）时不保留 device，避免假象 + 阻塞重试。
- sync_devices：scope 应连数 > 已连数时先 refresh_cameras 补建 manager。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from miloco.perception.collect.camera_adapter import (
    CameraDeviceAdapter,
    _CameraDeviceState,
)
from miloco.perception.types import PerceptionDevice


def _source(did: str = "cam1") -> PerceptionDevice:
    return PerceptionDevice(
        did=did, name=did, device_type="camera", room_id="r", room_name="r", online=True
    )


class TestConnectDeviceManagerMissing:
    """manager 缺失时 start_*_stream 返回 -1，device 不应留在 _devices。"""

    def test_both_streams_fail_device_dropped(self):
        proxy = MagicMock()
        proxy.start_camera_decode_video_stream = AsyncMock(return_value=-1)
        proxy.start_camera_decode_audio_stream = AsyncMock(return_value=-1)
        adapter = CameraDeviceAdapter(miot_proxy=proxy)

        asyncio.run(adapter.connect_device("cam1", source=_source()))

        assert "cam1" not in adapter._devices

    def test_video_ok_device_kept(self):
        # 至少一路订上 = manager 存在，保留 device。
        proxy = MagicMock()
        proxy.start_camera_decode_video_stream = AsyncMock(return_value=0)
        proxy.start_camera_decode_audio_stream = AsyncMock(return_value=-1)
        adapter = CameraDeviceAdapter(miot_proxy=proxy)

        asyncio.run(adapter.connect_device("cam1", source=_source()))

        assert "cam1" in adapter._devices


class TestSyncDevicesOnDemandRefresh:
    """sync_devices 按需补建：应连 > 已连才触发 refresh_cameras。"""

    def _adapter_with_mocked_connect(self, monkeypatch, proxy):
        adapter = CameraDeviceAdapter(miot_proxy=proxy)
        # 隔离基类的实际连接/断开，只验证 refresh 触发与否。
        monkeypatch.setattr(adapter, "connect_device", AsyncMock())
        monkeypatch.setattr(adapter, "disconnect_device", AsyncMock())
        return adapter

    def test_refresh_when_expected_gt_connected(self, monkeypatch):
        proxy = MagicMock()
        proxy.is_authenticated = True
        proxy.refresh_cameras = AsyncMock()
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        # _devices 空（卡死态：reg_id<0 已被剔除），discover 出 1 台 → 应连 1 > 已连 0
        monkeypatch.setattr(
            adapter, "discover_devices", AsyncMock(return_value={"cam1": _source()})
        )

        asyncio.run(adapter.sync_devices())

        proxy.refresh_cameras.assert_awaited_once()

    def test_no_refresh_when_all_connected(self, monkeypatch):
        proxy = MagicMock()
        proxy.is_authenticated = True
        proxy.refresh_cameras = AsyncMock()
        proxy.is_camera_stream_connected = MagicMock(return_value=True)
        proxy.get_cached_camera = MagicMock(return_value=None)
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        adapter._devices["cam1"] = _CameraDeviceState(did="cam1")  # 已连 1 台
        monkeypatch.setattr(
            adapter, "discover_devices", AsyncMock(return_value={"cam1": _source()})
        )

        asyncio.run(adapter.sync_devices())

        proxy.refresh_cameras.assert_not_awaited()

    def test_refresh_when_manager_exists_but_ppcs_is_disconnected(self, monkeypatch):
        """订阅已登记不代表 native/PPCS 已连；断流也应推进 refresh/降权计数。"""
        proxy = MagicMock()
        proxy.is_authenticated = True
        proxy.refresh_cameras = AsyncMock()
        proxy.is_camera_stream_connected = MagicMock(return_value=False)
        proxy.get_cached_camera = MagicMock(return_value=None)
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        adapter._devices["cam1"] = _CameraDeviceState(did="cam1")
        monkeypatch.setattr(
            adapter, "discover_devices", AsyncMock(return_value={"cam1": _source()})
        )

        asyncio.run(adapter.sync_devices())

        proxy.refresh_cameras.assert_awaited_once()

    def test_no_refresh_when_unauthenticated(self, monkeypatch):
        proxy = MagicMock()
        proxy.is_authenticated = False
        proxy.refresh_cameras = AsyncMock()
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        monkeypatch.setattr(adapter, "discover_devices", AsyncMock(return_value={}))

        asyncio.run(adapter.sync_devices())

        proxy.refresh_cameras.assert_not_awaited()

    def test_throttle_skips_rapid_refresh(self, monkeypatch):
        # 无设备态 sync 1s 一轮，节流让 refresh_cameras 最多每 10s 一次。
        proxy = MagicMock()
        proxy.is_authenticated = True
        proxy.refresh_cameras = AsyncMock()
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        monkeypatch.setattr(
            adapter, "discover_devices", AsyncMock(return_value={"cam1": _source()})
        )
        clock = [100_000]
        monkeypatch.setattr(
            "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: clock[0]
        )

        asyncio.run(adapter.sync_devices())  # 首次：触发
        clock[0] = 105_000  # +5s < 10s
        asyncio.run(adapter.sync_devices())  # 节流：跳过
        assert proxy.refresh_cameras.await_count == 1
        clock[0] = 116_000  # 距上次 +11s >= 10s
        asyncio.run(adapter.sync_devices())  # 再次触发
        assert proxy.refresh_cameras.await_count == 2
