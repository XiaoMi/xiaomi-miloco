"""静默检测 + 自愈重连的回归测试（主线 3）。

覆盖三处「一个 mock 计数断言就能挡住」的缺陷：

- 双摄相机的静默重连按**物理 did** 归并：两个通道同时静默也只重建一次 native 会话，
  不能按通道 did 各触发一次（否则刚 create 出来的会话几毫秒内又被 destroy）。
- 单通道静默也要把**兄弟通道一起断开**：destroy 会作废兄弟通道在旧实例上的 reg_id，
  而 connect_device 对已在 _devices 里的 did 直接 early-return，不先断开就补不回订阅。
- ``reconnect_camera`` 重建后必须重新注册相机状态回调并复位连接态，否则 lan 层会
  把该 did 永远留在 _connected_dids、整个扫描停摆。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from miloco.miot.client import MiotProxy
from miloco.perception.collect.camera_adapter import (
    CameraDeviceAdapter,
    _CameraDeviceState,
)


def _cam(did: str) -> SimpleNamespace:
    return SimpleNamespace(did=did, online=True)


def _make_stalled_adapter(monkeypatch, stalled_dids: set[str], now: int = 1_000_000):
    """构造一台双摄相机（dual:ch0 / dual:ch1），指定哪些通道已静默。"""
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: now
    )
    proxy = MagicMock()
    proxy.get_cached_camera.return_value = _cam("dual")
    proxy.reconnect_camera = AsyncMock()
    adapter = CameraDeviceAdapter(miot_proxy=proxy)
    for ch in ("ch0", "ch1"):
        did = f"dual:{ch}"
        state = _CameraDeviceState(did=did)
        # 静默通道：40s 无帧（超 30s 阈值）；正常通道：1s 前刚出过帧。
        state.last_video_frame_ms = now - 40_000 if did in stalled_dids else now - 1_000
        adapter._devices[did] = state
    return adapter, proxy


async def test_stalled_dual_camera_rebuilds_native_session_once(monkeypatch):
    """双摄两个通道同时静默 → native 会话只重建一次（按物理 did 归并）。"""
    adapter, proxy = _make_stalled_adapter(
        monkeypatch, stalled_dids={"dual:ch0", "dual:ch1"}
    )
    await adapter._check_stalled_cameras()
    proxy.reconnect_camera.assert_awaited_once_with("dual")


async def test_stalled_single_channel_disconnects_all_siblings(monkeypatch):
    """只有 ch0 静默也要把 ch1 一起断开，否则 connect_device 会 early-return。"""
    adapter, _ = _make_stalled_adapter(monkeypatch, stalled_dids={"dual:ch0"})
    await adapter._check_stalled_cameras()
    assert "dual:ch0" not in adapter._devices
    assert "dual:ch1" not in adapter._devices


async def test_reconnect_camera_repairs_status_callback_and_connected_flag():
    """重建后必须重新注册状态回调并复位连接态，否则 lan 扫描永久暂停。"""
    kv_repo = MagicMock()
    kv_repo.get.return_value = None
    proxy = MiotProxy(uuid="u", redirect_uri="r", kv_repo=kv_repo)
    miot_client = MagicMock()
    miot_client.unregister_lan_device_changed_async = AsyncMock()
    miot_client.unregister_camera_status_changed_async = AsyncMock()
    miot_client.register_lan_device_changed_async = AsyncMock()
    miot_client.register_camera_status_changed_async = AsyncMock()
    manager = MagicMock()
    manager.destroy = AsyncMock()
    proxy._miot_client = miot_client
    proxy._camera_info_dict = {"cam1": _cam("cam1")}
    proxy._camera_img_managers = {"cam1": manager}
    proxy._camera_connect_since = {"cam1": 123.0}
    proxy._create_camera_img_manager = AsyncMock(return_value=manager)

    await proxy.reconnect_camera("cam1")

    miot_client.set_camera_connected.assert_called_with("cam1", False)
    miot_client.register_camera_status_changed_async.assert_awaited_with(
        did="cam1", callback=proxy._on_camera_status_changed
    )
    assert "cam1" not in proxy._camera_connect_since
