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
        proxy.get_cached_camera = MagicMock(return_value=None)
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        adapter._devices["cam1"] = _CameraDeviceState(did="cam1")  # 已连 1 台
        monkeypatch.setattr(
            adapter, "discover_devices", AsyncMock(return_value={"cam1": _source()})
        )

        asyncio.run(adapter.sync_devices())

        proxy.refresh_cameras.assert_not_awaited()

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


class TestSyncDevicesStaleStatusRefresh:
    """sync_devices 云端 online 状态自愈：后端重启时摄像头恰好断连的恢复。

    场景：后端重启 → _camera_info_dict 缓存 online=false → sync 循环永远
    认为摄像头不可连。当 expected 为空且无设备时，应主动刷新云端状态。
    """

    def _adapter_with_mocked_connect(self, monkeypatch, proxy):
        adapter = CameraDeviceAdapter(miot_proxy=proxy)
        monkeypatch.setattr(adapter, "connect_device", AsyncMock())
        monkeypatch.setattr(adapter, "disconnect_device", AsyncMock())
        return adapter

    def test_refresh_when_expected_empty_and_no_devices(self, monkeypatch):
        """expected=0 且无设备 → 应调 refresh_camera_online_status。"""
        proxy = MagicMock()
        proxy.is_authenticated = True
        proxy.refresh_cameras = AsyncMock()
        proxy.refresh_camera_online_status = AsyncMock()
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        monkeypatch.setattr(
            adapter, "discover_devices", AsyncMock(return_value={})
        )

        asyncio.run(adapter.sync_devices())

        proxy.refresh_camera_online_status.assert_awaited_once()
        proxy.refresh_cameras.assert_not_awaited()

    def test_no_stale_refresh_when_expected_nonempty(self, monkeypatch):
        """expected 非空 → 不应调 refresh_camera_online_status。"""
        proxy = MagicMock()
        proxy.is_authenticated = True
        proxy.refresh_cameras = AsyncMock()
        proxy.refresh_camera_online_status = AsyncMock()
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        monkeypatch.setattr(
            adapter, "discover_devices",
            AsyncMock(return_value={"cam1": _source()}),
        )

        asyncio.run(adapter.sync_devices())

        proxy.refresh_camera_online_status.assert_not_awaited()

    def test_no_stale_refresh_when_has_connected_devices(self, monkeypatch):
        """有已连设备 → 不应调 refresh_camera_online_status。"""
        proxy = MagicMock()
        proxy.is_authenticated = True
        proxy.refresh_cameras = AsyncMock()
        proxy.refresh_camera_online_status = AsyncMock()
        proxy.get_cached_camera = MagicMock(return_value=None)
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        adapter._devices["cam1"] = _CameraDeviceState(did="cam1")
        monkeypatch.setattr(
            adapter, "discover_devices", AsyncMock(return_value={})
        )

        asyncio.run(adapter.sync_devices())

        proxy.refresh_camera_online_status.assert_not_awaited()

    def test_throttle_stale_status_refresh(self, monkeypatch):
        """节流：refresh_camera_online_status 最多每 30s 一次。"""
        proxy = MagicMock()
        proxy.is_authenticated = True
        proxy.refresh_cameras = AsyncMock()
        proxy.refresh_camera_online_status = AsyncMock()
        adapter = self._adapter_with_mocked_connect(monkeypatch, proxy)
        monkeypatch.setattr(
            adapter, "discover_devices", AsyncMock(return_value={})
        )
        clock = [100_000]
        monkeypatch.setattr(
            "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: clock[0]
        )

        asyncio.run(adapter.sync_devices())  # 首次：触发
        assert proxy.refresh_camera_online_status.await_count == 1
        clock[0] = 115_000  # +15s < 30s
        asyncio.run(adapter.sync_devices())  # 节流：跳过
        assert proxy.refresh_camera_online_status.await_count == 1
        clock[0] = 135_000  # 距上次 +35s >= 30s
        asyncio.run(adapter.sync_devices())  # 再次触发
        assert proxy.refresh_camera_online_status.await_count == 2
