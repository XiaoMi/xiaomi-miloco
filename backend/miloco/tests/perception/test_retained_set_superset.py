"""``sync_devices(disconnect_require_lan=False)`` 的保留集不变量回归。

主线 3 为了不让探测偶发失败把正在拉流的相机断掉，把周期同步的断开判据从「发现集」
换成放宽 LAN 门重算出的「保留集」。整个设计成立的前提只有一条：

    **保留集必须是发现集的超集** —— 放宽一道门，候选只应变多。

投喂上限的截断发生在过滤之后、按合成 did 字典序取前 N 个，而
``sorted(超集)[:N]`` 并不包含 ``sorted(子集)[:N]``。所以重算时若沿用
``cap=True``，超过上限的家庭里「唯一真正 LAN 可达那台」会落在保留集之外，于是
每轮同步连上、下一轮又被断开，画面每 20s 中断一次且永不自愈。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.miot.filter import MAX_ENABLED_CAMERAS
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miot.types import MIoTCameraInfo, MIoTCameraStatus, MIoTDeviceInfo


def _cam(did: str, *, lan_online: bool) -> MIoTCameraInfo:
    device = MIoTDeviceInfo(
        did=did, name=did, uid="u", urn="urn:miot", model="mi.cam.1",
        manufacturer="xiaomi", connect_type=1, pid=1, token="tok",
        online=True, voice_ctrl=0, order_time=0,
    )
    cam = MIoTCameraInfo(
        **device.model_dump(),
        channel_count=1,
        camera_status=MIoTCameraStatus.DISCONNECTED,
    )
    cam.home_id = "H1"
    cam.lan_online = lan_online
    cam.local_ip = "192.168.1.9" if lan_online else None
    return cam


def _make_adapter(cams: dict[str, MIoTCameraInfo]) -> CameraDeviceAdapter:
    proxy = AsyncMock()
    store = {ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])}
    proxy._kv_repo = SimpleNamespace(
        get=lambda key, default=None: store.get(key, default),
        set=lambda key, value: store.__setitem__(key, value) or True,
    )
    proxy._camera_awake_cache = {}
    proxy.is_authenticated = True
    proxy.get_cameras = AsyncMock(return_value=cams)
    proxy.get_cached_camera = lambda did: cams.get(did)
    # 订阅一律成功，让 connect_device 真的把 did 留在 _devices 里。
    proxy.start_camera_decode_video_stream = AsyncMock(return_value=1)
    proxy.start_camera_decode_audio_stream = AsyncMock(return_value=2)
    proxy.stop_camera_decode_video_stream = AsyncMock(return_value=True)
    proxy.stop_camera_decode_audio_stream = AsyncMock(return_value=True)
    return CameraDeviceAdapter(miot_proxy=proxy)


@pytest.mark.asyncio
async def test_retained_set_is_superset_of_discovered():
    """超过投喂上限的家庭里，唯一 LAN 可达的相机不得在下一轮被断开。

    构造 5 台云端在线的单摄相机（did 字典序 100..500），只有字典序最末的 500
    局域网可达：
      - 发现集（严格 LAN 门）= {500}
      - 保留集若带 cap=True = sorted({100..500})[:4] = {100,200,300,400} —— 不含 500
    于是第一轮连上 500，第二轮 `已连集 - 保留集` = {500} → 当场断开，稳态循环。
    """
    cams = {
        "100": _cam("100", lan_online=False),
        "200": _cam("200", lan_online=False),
        "300": _cam("300", lan_online=False),
        "400": _cam("400", lan_online=False),
        "500": _cam("500", lan_online=True),
    }
    # 前提校验：候选数确实超过上限，否则 cap 不触发、这条用例失去意义。
    assert len(cams) > MAX_ENABLED_CAMERAS

    adapter = _make_adapter(cams)

    await adapter.sync_devices(cams)
    assert "500" in adapter.get_connected_devices(), "第一轮应连上唯一 LAN 可达的相机"

    await adapter.sync_devices(cams)
    assert "500" in adapter.get_connected_devices(), (
        "第二轮不得被断开——保留集必须是发现集的超集"
    )


@pytest.mark.asyncio
async def test_retained_set_keeps_camera_whose_lan_flag_flapped():
    """已连相机的 lan_online 掉成 False（直连掐死同网段保活）时不得被断开。

    这是放宽 LAN 门这条分支的本来目的，与上面的超集不变量一并钉住。
    """
    cams = {"cam1": _cam("cam1", lan_online=True)}
    adapter = _make_adapter(cams)

    await adapter.sync_devices(cams)
    assert "cam1" in adapter.get_connected_devices()

    # 拉流建立后同网段 OTU 保活被掐死 → lan_online 翻 False，但云端仍在线。
    cams["cam1"].lan_online = False
    await adapter.sync_devices(cams)
    assert "cam1" in adapter.get_connected_devices(), (
        "lan_online 偶发假 False 不该断掉正在拉流的相机"
    )
