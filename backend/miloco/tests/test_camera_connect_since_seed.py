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
CONNECTED 分支会把时间戳 pop 掉（见 test_connected_status_clears_seeded_timestamp）。

另一半是「谁才算这一轮尝试结束」：只有 CONNECTED（或管理器被销毁）才 pop，
DISCONNECTED/ERROR 一律保留最早的起点——原生带退避自动重连，那两个状态的语义是
「这一轮握手失败、马上还要再试」，在它们上面 pop 会让计时器每轮归零、判据永久为假。
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


# ── 失败重试不得把 60s 计时器归零 ──────────────────────────────────────────


async def test_disconnected_retry_does_not_reset_timer():
    """CONNECTING → DISCONNECTED → CONNECTING 三连后，起点仍是**第一次**的时刻。

    原生实例是 ``enable_reconnect=True`` 起的：握手失败会收敛成 DISCONNECTED，再按
    3s→6s→12s…→1200s 退避自动重试（``camera.py`` ``__on_status_changed`` /
    ``__get_try_start_timeout``）。底层的 DISCONNECTED 语义是「这一轮握手失败、马上还
    要再试」，不是「不再尝试了」。若上层在 DISCONNECTED 上 pop 起点，每轮重试都把
    计时器归零，已计时长恒不超过单次握手超时（实测 ~35s）⇒ ``stream_nat_blocked``
    永久为假 ⇒ 主线 6 整条跨 NAT 诊断链路全是死代码。
    """
    proxy = _make_proxy({"cam1": _cam("cam1")})
    await proxy.refresh_cameras()
    first = proxy._camera_connect_since["cam1"]

    await proxy._on_camera_status_changed("cam1", MIoTCameraStatus.CONNECTING)
    await proxy._on_camera_status_changed("cam1", MIoTCameraStatus.DISCONNECTED)
    await proxy._on_camera_status_changed("cam1", MIoTCameraStatus.CONNECTING)

    assert proxy._camera_connect_since.get("cam1") == first, "失败重试不得重置起点"


async def test_nat_blocked_survives_a_full_retry_cycle():
    """一台反复「握手 35s 超时 → 退避 3s → 再试」的跨网段相机，最终必须被判出。

    这是上一条的行为面：判据要能撑过整轮重试，而不是每轮从零开始。
    """
    proxy = _make_proxy({"cam1": _cam("cam1")})
    await proxy.refresh_cameras()
    # 把起点推到 61s 前，模拟已经卡了一整轮多。
    proxy._camera_connect_since["cam1"] = (
        time.monotonic() - proxy._STREAM_NAT_TIMEOUT - 1
    )

    # 中间穿插一轮失败重试。
    await proxy._on_camera_status_changed("cam1", MIoTCameraStatus.DISCONNECTED)
    await proxy._on_camera_status_changed("cam1", MIoTCameraStatus.RE_CONNECTING)

    assert proxy.stream_nat_blocked("cam1") is True


async def test_native_failure_does_not_extend_keepalive_grace():
    """原生报非 CONNECTED 时，反向同步给 lan 层要带 keep_alive_on_stop=False。

    否则「连不上」这件事会被用去续期「可达性」100s 宽限窗，而原生每 3s~96s 重试一次、
    每次都短于宽限窗 ⇒ 窗口被连续续期 ⇒ 相机被拔电后接口仍连续几分钟报
    lan_reachable=true，启停接口的可达硬门照样放行。
    """
    proxy = _make_proxy({"cam1": _cam("cam1")})

    await proxy._on_camera_status_changed("cam1", MIoTCameraStatus.DISCONNECTED)

    kwargs = proxy._miot_client.set_camera_connected.call_args.kwargs
    assert kwargs.get("keep_alive_on_stop") is False


async def test_deliberate_teardown_keeps_default_grace():
    """我们自己主动关流（重建路径）保持默认续期，防瞬时闪 offline。"""
    proxy = _make_proxy({"cam1": _cam("cam1")})
    manager = MagicMock()
    manager.destroy = AsyncMock()
    proxy._camera_info_dict = {"cam1": _cam("cam1")}
    proxy._camera_img_managers = {"cam1": manager}

    await proxy.reconnect_camera("cam1")

    call = proxy._miot_client.set_camera_connected.call_args
    assert call.args == ("cam1", False)
    assert "keep_alive_on_stop" not in call.kwargs


async def test_cloud_offline_camera_is_not_diagnosed_as_nat_blocked():
    """云端已报离线的相机不得被判成跨 NAT 阻断——问题不在路由器上。

    相机断电/断网时云端心跳先超时，而 LAN 侧保活窗还没到期（最长 100s）。这段窗口里
    若不加这道门，列表接口会给出「请检查 NAT 类型 / 开 UPnP」这种错误方向的提示，把
    住户指去折腾路由器配置，还会稀释「NAT 阻断」这个本来信噪比很高的信号。
    门下沉在 stream_nat_blocked 里，列表接口与播放页看门狗共用，两个界面口径一致。
    """
    cams = {"cam1": _cam("cam1")}
    proxy = _make_proxy(cams)
    await proxy.refresh_cameras()
    proxy._camera_connect_since["cam1"] = (
        time.monotonic() - proxy._STREAM_NAT_TIMEOUT - 1
    )
    assert proxy.stream_nat_blocked("cam1") is True  # 前提：其余判据都成立

    proxy._camera_info_dict["cam1"].online = False

    assert proxy.stream_nat_blocked("cam1") is False
