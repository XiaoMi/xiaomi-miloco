"""``_camera_connect_since`` 必须在 native 建连发起处播种，而不是只靠状态回调。

跨 NAT 主动提示（``stream_nat_blocked`` → 接口 ``stream_error=cross_subnet_nat``）
依赖「进入连接中」的起始时间戳。这个时间戳原来只由相机状态回调 ``setdefault``，而
回调是在 manager 创建**之后**才注册的——``_create_camera_img_manager`` 里的
``start_async`` 早已发起 native 建连，此刻实例回调表还是空的：

- ``camera.py __on_status_changed`` 遍历空回调表 → 首次 CONNECTING 当场丢弃；
- ``register_status_changed_async`` 只往回调表加条目，**不回灌当前状态**。

对「卡在首次 CONNECTING 既不成功也不失败」的相机——正是跨网段被严格 NAT 黑掉媒体流
的典型形态——此后可能再无第二次状态迁移，``setdefault`` 永不执行，
``stream_nat_blocked`` 取不到起点恒判 False，这个特性专门要避免的「住户干等连接中」
就永远等不到提示。

反向误报不成立：CONNECTING 是发起建连时立刻发出的（与注册只隔几个 await），而
CONNECTED 要等十几秒的 MTP 握手，落进这个窗口的只可能是 CONNECTING；真连上后
CONNECTED 分支会把时间戳 pop 掉——本文件最后一条用例钉住这一点。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from miloco.database.kv_repo import ScopeConfigKeys
from miloco.miot.client import MiotProxy
from miot.types import MIoTCameraStatus


def _cam(did: str, *, cross_subnet: bool = True) -> SimpleNamespace:
    """够 select_active_camera_dids / stream_nat_blocked 用的最小相机对象。"""
    return SimpleNamespace(
        did=did,
        name=did,
        model="mi.cam.1",
        home_id="H1",
        online=True,
        connected=False,
        camera_status=MIoTCameraStatus.DISCONNECTED,
        channel_count=1,
        lan_online=True,
        local_ip="192.168.9.9",
        cross_subnet=cross_subnet,
    )


def _make_proxy(cams: dict[str, SimpleNamespace]) -> MiotProxy:
    store = {ScopeConfigKeys.HOME_WHITE_LIST_KEY: '["H1"]'}
    kv_repo = MagicMock()
    kv_repo.get.side_effect = lambda key, default=None: store.get(key, default)
    proxy = MiotProxy(uuid="u", redirect_uri="r", kv_repo=kv_repo)
    miot_client = MagicMock()
    miot_client.get_cameras_async = AsyncMock(return_value=cams)
    miot_client.register_lan_device_changed_async = AsyncMock()
    miot_client.register_camera_status_changed_async = AsyncMock()
    miot_client.unregister_lan_device_changed_async = AsyncMock()
    miot_client.unregister_camera_status_changed_async = AsyncMock()
    proxy._miot_client = miot_client
    # 预填 awake 缓存，跳过 refresh_cameras 里的补读（走真实网络）。
    # awake_map 是 per-lens 的 {did: {channel: bool|None}}。
    proxy._camera_awake_cache = {did: {0: True} for did in cams}
    proxy._create_camera_img_manager = AsyncMock(return_value=MagicMock())
    return proxy


async def test_refresh_cameras_seeds_connect_since_on_create():
    """新建 manager 即播种起始时间戳，不等状态回调。"""
    proxy = _make_proxy({"cam1": _cam("cam1")})

    await proxy.refresh_cameras()

    assert "cam1" in proxy._camera_connect_since


async def test_nat_blocked_fires_without_any_status_callback():
    """一次状态回调都没来的相机，卡满阈值后照样能判出跨 NAT 受阻。

    这是修复前的坏行为：接口 ``stream_error`` 永远是 None，住户只能等 72s 看门狗。
    """
    proxy = _make_proxy({"cam1": _cam("cam1")})
    await proxy.refresh_cameras()

    # 未连上、未收到任何状态事件，只是把时钟推过阈值。
    proxy._camera_connect_since["cam1"] = (
        time.monotonic() - proxy._STREAM_NAT_TIMEOUT - 1
    )
    assert proxy.stream_nat_blocked("cam1") is True


async def test_seed_does_not_overwrite_existing_timestamp():
    """已有起点时不能被刷新——否则每轮 refresh 都把「卡了多久」清零，永远判不到阈值。"""
    proxy = _make_proxy({"cam1": _cam("cam1")})
    await proxy.refresh_cameras()
    first = proxy._camera_connect_since["cam1"]

    # 第二轮 refresh：manager 已在册，走 update 分支，不该动时间戳。
    await proxy.refresh_cameras()
    assert proxy._camera_connect_since["cam1"] == first


async def test_connected_status_clears_seeded_timestamp():
    """正常连上的相机不受播种影响：CONNECTED 分支 pop 掉起点，诊断随即恒 False。"""
    proxy = _make_proxy({"cam1": _cam("cam1")})
    await proxy.refresh_cameras()
    assert "cam1" in proxy._camera_connect_since

    await proxy._on_camera_status_changed("cam1", MIoTCameraStatus.CONNECTED)

    assert "cam1" not in proxy._camera_connect_since
    assert proxy.stream_nat_blocked("cam1") is False


async def test_reconnect_camera_reseeds_connect_since():
    """重建路径同因：重建的 start_async 也在注册前发起建连，首次 CONNECTING 同样丢失。

    重建时先 pop 旧时间戳（避免拿重建前的远古时刻立刻误报），故必须重新播种，
    否则重建后的相机若再次卡在首次 CONNECTING 就又没有起点了。
    """
    proxy = _make_proxy({"cam1": _cam("cam1")})
    manager = MagicMock()
    manager.destroy = AsyncMock()
    proxy._camera_info_dict = {"cam1": _cam("cam1")}
    proxy._camera_img_managers = {"cam1": manager}
    proxy._camera_connect_since = {"cam1": 1.0}

    await proxy.reconnect_camera("cam1")

    since = proxy._camera_connect_since.get("cam1")
    assert since is not None
    assert since != 1.0, "旧时间戳必须被换成本次重建的时刻"
