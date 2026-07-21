"""Tests for online / connected status separation.

Verifies that:
* ``CameraInfo.online`` reflects cloud-reported device reachability.
* ``CameraInfo.connected`` derives from ``camera_status == CONNECTED``.
* ``PerceptionDevice`` carries both ``online`` and ``connected`` fields.
* ``CameraDeviceAdapter.discover_devices`` correctly populates both fields
  and filters only by ``online`` (cloud status).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.miot.schema import CameraInfo
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.types import PerceptionDevice
from miot.types import MIoTCameraInfo, MIoTCameraStatus, MIoTDeviceInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_miot_device(did: str = "cam1", online: bool = True) -> MIoTDeviceInfo:
    return MIoTDeviceInfo(
        did=did,
        name=did,
        uid="u",
        urn="urn:miot",
        model="mi.cam.1",
        manufacturer="xiaomi",
        connect_type=1,
        pid=1,
        token="tok",
        online=online,
        voice_ctrl=0,
        order_time=0,
    )


def _make_camera_info(
    did: str = "cam1",
    online: bool = True,
    camera_status: MIoTCameraStatus = MIoTCameraStatus.DISCONNECTED,
    lan_online: bool | None = None,
    local_ip: str | None = None,
) -> MIoTCameraInfo:
    device = _make_miot_device(did=did, online=online)
    cam = MIoTCameraInfo(
        **device.model_dump(),
        channel_count=1,
        camera_status=camera_status,
    )
    cam.lan_online = lan_online
    cam.local_ip = local_ip
    return cam


# ---------------------------------------------------------------------------
# CameraInfo.connected property
# ---------------------------------------------------------------------------


class TestCameraInfoConnected:
    def test_connected_when_status_connected(self):
        cam = _make_camera_info(camera_status=MIoTCameraStatus.CONNECTED)
        ci = CameraInfo.model_validate(cam.model_dump())
        assert ci.connected is True

    def test_not_connected_when_status_disconnected(self):
        cam = _make_camera_info(camera_status=MIoTCameraStatus.DISCONNECTED)
        ci = CameraInfo.model_validate(cam.model_dump())
        assert ci.connected is False

    def test_not_connected_when_status_connecting(self):
        cam = _make_camera_info(camera_status=MIoTCameraStatus.CONNECTING)
        ci = CameraInfo.model_validate(cam.model_dump())
        assert ci.connected is False

    def test_not_connected_when_status_error(self):
        cam = _make_camera_info(camera_status=MIoTCameraStatus.ERROR)
        ci = CameraInfo.model_validate(cam.model_dump())
        assert ci.connected is False

    def test_not_connected_when_status_none(self):
        ci = CameraInfo(did="x", name="x", online=True, camera_status=None)
        assert ci.connected is False

    def test_online_preserved_from_cloud(self):
        """online should reflect the cloud value, not camera_status."""
        cam = _make_camera_info(
            online=True, camera_status=MIoTCameraStatus.DISCONNECTED
        )
        ci = CameraInfo.model_validate(cam.model_dump())
        assert ci.online is True
        assert ci.connected is False

    def test_offline_device_not_connected(self):
        cam = _make_camera_info(
            online=False, camera_status=MIoTCameraStatus.DISCONNECTED
        )
        ci = CameraInfo.model_validate(cam.model_dump())
        assert ci.online is False
        assert ci.connected is False


# ---------------------------------------------------------------------------
# PerceptionDevice
# ---------------------------------------------------------------------------


class TestPerceptionDeviceFields:
    def test_defaults(self):
        pd = PerceptionDevice(did="d", name="n", device_type="camera")
        assert pd.online is True

    def test_explicit_online_false(self):
        pd = PerceptionDevice(did="d", name="n", device_type="camera", online=False)
        assert pd.online is False


# ---------------------------------------------------------------------------
# CameraDeviceAdapter.discover_devices
# ---------------------------------------------------------------------------


class TestDiscoverDevicesOnlineConnected:
    @pytest.fixture
    def adapter(self):
        import json

        from miloco.database.kv_repo import ScopeConfigKeys

        proxy = AsyncMock()
        # 默认启用 H1 家庭，让 discover_devices 不被空启用集过滤掉
        store: dict[str, str] = {
            ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        }
        # _kv_repo must be sync (filter.py 用 kv.get(...) 同步读)；
        # 默认 AsyncMock 会让所有属性访问返回 coroutine，导致
        # json.loads 收到 coroutine 而炸。
        proxy._kv_repo = SimpleNamespace(
            get=lambda key, default=None: store.get(key, default),
            set=lambda key, value: store.__setitem__(key, value) or True,
        )
        # awake 缓存真身是普通 dict；显式给空 dict，别让 AsyncMock 自动生成协程属性
        # （select_active 会读它做镜头门）。空 dict = 全部镜头态未知 → 不 gate。
        proxy._camera_awake_cache = {}
        return CameraDeviceAdapter(miot_proxy=proxy)

    @pytest.mark.asyncio
    async def test_online_lan_reachable_camera_discovered(self, adapter):
        """Cloud-online + LAN-reachable camera should be discoverable."""
        cam = _make_camera_info(
            did="cam1",
            online=True,
            camera_status=MIoTCameraStatus.DISCONNECTED,
            lan_online=True,
            local_ip="192.168.1.10",
        ).model_copy(update={"home_id": "H1"})
        adapter._miot_proxy.get_cameras.return_value = {"cam1": cam}

        result = await adapter.discover_devices(online_only=True)

        assert "cam1" in result
        assert result["cam1"].online is True

    @pytest.mark.asyncio
    async def test_online_but_not_on_lan_filtered_out(self, adapter):
        """Cloud-online but NOT LAN-reachable should be filtered."""
        cam = _make_camera_info(did="cam1", online=True, lan_online=False)
        adapter._miot_proxy.get_cameras.return_value = {"cam1": cam}

        result = await adapter.discover_devices(online_only=True)

        assert "cam1" not in result

    @pytest.mark.asyncio
    async def test_online_lan_none_filtered_out(self, adapter):
        """Cloud-online but lan_online=None should be filtered."""
        cam = _make_camera_info(did="cam1", online=True, lan_online=None)
        adapter._miot_proxy.get_cameras.return_value = {"cam1": cam}

        result = await adapter.discover_devices(online_only=True)

        assert "cam1" not in result

    @pytest.mark.asyncio
    async def test_offline_camera_filtered_out(self, adapter):
        """Cloud-offline camera should be filtered when online_only=True."""
        cam = _make_camera_info(did="cam1", online=False, lan_online=True)
        adapter._miot_proxy.get_cameras.return_value = {"cam1": cam}

        result = await adapter.discover_devices(online_only=True)

        assert "cam1" not in result

    @pytest.mark.asyncio
    async def test_all_cameras_included_when_not_online_only(self, adapter):
        """All cameras should appear when online_only=False."""
        cam = _make_camera_info(did="cam1", online=False, lan_online=False).model_copy(update={"home_id": "H1"})
        adapter._miot_proxy.get_cameras.return_value = {"cam1": cam}

        result = await adapter.discover_devices(online_only=False)

        assert "cam1" in result
        assert result["cam1"].online is False

    @pytest.mark.asyncio
    async def test_require_lan_false_keeps_stale_lan_camera(self, adapter):
        """A2 应连数判据(online_only=True, require_lan=False)放过 lan_online 陈旧
        成 false 的卡死态相机(云端 online=True)——它正是要靠 refresh 救活的。"""
        cam = _make_camera_info(did="cam1", online=True, lan_online=False).model_copy(
            update={"home_id": "H1"}
        )
        adapter._miot_proxy.get_cameras.return_value = {"cam1": cam}

        result = await adapter.discover_devices(online_only=True, require_lan=False)

        assert "cam1" in result

    @pytest.mark.asyncio
    async def test_require_lan_false_still_excludes_offline_camera(self, adapter):
        """A2 应连数判据仍排除云端离线相机(online=False)——它救不活，算进应连数
        会让判据永真、refresh_cameras 每轮空转(MR review 指出的过度触发)。"""
        cam = _make_camera_info(did="cam1", online=False, lan_online=False).model_copy(
            update={"home_id": "H1"}
        )
        adapter._miot_proxy.get_cameras.return_value = {"cam1": cam}

        result = await adapter.discover_devices(online_only=True, require_lan=False)

        assert "cam1" not in result

    @pytest.mark.asyncio
    async def test_filter_cameras_from_all(self, adapter):
        """_filter_cameras_from_all requires online AND lan_online."""
        cam = _make_camera_info(
            did="cam1",
            online=True,
            camera_status=MIoTCameraStatus.CONNECTED,
            lan_online=True,
            local_ip="10.0.0.1",
        ).model_copy(update={"home_id": "H1"})
        result = adapter._filter_cameras_from_all(
            {"cam1": cam},
            online_only=True,
        )
        assert "cam1" in result
        assert result["cam1"].online is True

    @pytest.mark.asyncio
    async def test_filter_cameras_from_all_no_lan(self, adapter):
        """_filter_cameras_from_all filters when lan_online is False."""
        cam = _make_camera_info(
            did="cam1", online=True, lan_online=False,
        )
        result = adapter._filter_cameras_from_all(
            {"cam1": cam},
            online_only=True,
        )
        assert "cam1" not in result

    @pytest.mark.asyncio
    async def test_filter_cameras_drops_disallowed_home(self, adapter):
        """Camera 不在启用集的家庭应当被 drop——启用集生效后
        adapter 必须断开未启用家庭的相机连接，不只是 API 层隐藏。"""
        import json

        from miloco.database.kv_repo import ScopeConfigKeys

        store: dict[str, str] = {
            ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        }
        adapter._miot_proxy._kv_repo = SimpleNamespace(
            get=lambda key, default=None: store.get(key, default),
            set=lambda key, value: store.__setitem__(key, value) or True,
        )

        cam_in = _make_camera_info(
            did="cam_in", online=True, lan_online=True,
        ).model_copy(update={"home_id": "H1"})
        cam_out = _make_camera_info(
            did="cam_out", online=True, lan_online=True,
        ).model_copy(update={"home_id": "H2"})

        result = adapter._filter_cameras_from_all(
            {"cam_in": cam_in, "cam_out": cam_out},
            online_only=True,
        )
        assert "cam_in" in result
        assert "cam_out" not in result


# ---------------------------------------------------------------------------
# LAN status / local_ip passthrough
# ---------------------------------------------------------------------------


class TestCameraInfoLanStatus:
    def test_lan_online_preserved(self):
        cam = _make_camera_info(lan_online=True, local_ip="192.168.1.100")
        ci = CameraInfo.model_validate(cam.model_dump())
        assert ci.lan_online is True
        assert ci.local_ip == "192.168.1.100"

    def test_lan_online_none_by_default(self):
        cam = _make_camera_info()
        ci = CameraInfo.model_validate(cam.model_dump())
        assert ci.lan_online is None
        assert ci.local_ip is None

    def test_lan_online_false(self):
        cam = _make_camera_info(lan_online=False, local_ip=None)
        ci = CameraInfo.model_validate(cam.model_dump())
        assert ci.lan_online is False
        assert ci.local_ip is None


