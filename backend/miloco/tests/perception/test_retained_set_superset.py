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


def _make_adapter_with_managers(
    cams: dict[str, MIoTCameraInfo], managers: set[str]
) -> tuple[CameraDeviceAdapter, set[str]]:
    """更忠实的模型：订阅只在该相机的 native manager 已建成时才成功。

    真实链路里 ``start_camera_decode_*_stream`` 唯一的前置条件就是
    ``camera_img_manager`` 在字典里（``client.py`` 里那两处 "not found in managers"
    分支返回 -1），而 manager 由 ``refresh_cameras`` 按**活跃集**建销。前一个
    ``_make_adapter`` 让订阅恒成功，因此看不见「manager 缺失 → 订阅失败 → did 被
    剔除 → 依赖补建 refresh 才能恢复」这条链，也就抓不到补建触发条件的缺陷。
    """
    adapter = _make_adapter(cams)
    proxy = adapter._miot_proxy

    async def _start_video(physical_did, channel, cb):
        return 1 if physical_did in managers else -1

    async def _start_audio(physical_did, channel, cb):
        return 2 if physical_did in managers else -1

    async def _refresh_cameras():
        # 与真实 refresh_cameras 同口径：按活跃集(cap=True)建销 manager。
        from miloco.miot.filter import select_active_camera_dids

        active = set(
            select_active_camera_dids(proxy._kv_repo, cams, awake_map={})
        )
        managers.clear()
        managers.update(active)
        return cams

    proxy.start_camera_decode_video_stream = _start_video
    proxy.start_camera_decode_audio_stream = _start_audio
    proxy.refresh_cameras = _refresh_cameras
    return adapter, managers


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
async def test_connected_set_never_exceeds_feed_cap():
    """低字典序相机后上线时，已连路数不得越过投喂上限。

    与「超集不变量」正交的另一个方向：保留集为了满足超集前提去掉了上限截断，
    于是断开判据不再收敛已连集规模——而连接侧走的是带截断的发现集、
    ``connect_device`` 自己不认上限。低字典序相机上线会被发现集纳入并新连，先前
    占位的高字典序相机却仍在保留集里不被断开 ⇒ 已连路数单调越过上限，且被挤出
    活跃集的那路会在 refresh_cameras(销) 与静默检测(建) 之间每 5 分钟震荡一次。
    """
    cams = {
        "200": _cam("200", lan_online=True),
        "300": _cam("300", lan_online=True),
        "400": _cam("400", lan_online=True),
        "500": _cam("500", lan_online=True),
    }
    adapter = _make_adapter(cams)
    await adapter.sync_devices(cams)
    assert len(adapter.get_connected_devices()) == MAX_ENABLED_CAMERAS

    # 第 5 台（字典序最小）后上线 → 它进发现集，但总数不得超过上限。
    cams["100"] = _cam("100", lan_online=True)
    await adapter.sync_devices(cams)
    connected = set(adapter.get_connected_devices())
    assert len(connected) == MAX_ENABLED_CAMERAS, f"越过上限: {sorted(connected)}"
    assert "500" not in connected, "被上限挤出的那路应当被断开"


@pytest.mark.asyncio
async def test_membership_drift_triggers_ondemand_rebuild():
    """「数量相等、成员不同」也必须触发按需补建，否则顶位相机永远连不上。

    第三个方向（前两条钉的是超集与数量上限）：5 台相机、低字典序的 `100` 后上线顶位时
      - 应连集(cap=True) = {100,200,300,400}
      - 已连集           = {200,300,400,500}
    数量恰好都是 4 ⇒ 数量判据 `len(expected) > len(_devices)` 恒 False ⇒ refresh_cameras
    永不触发 ⇒ `100` 因 manager 缺失每轮订阅失败被剔除（日志反复刷 "will retry on next
    sync"，而那个 retry 依赖的补建永不成立），`500` 继续占着原生会话白拉流。
    保留集超集化拿走了旧的自愈路径（旧的带截断保留集会把 500 断掉使数量下降），
    而 `_converge_feed_cap` 只处理「数量 > 上限」，看不见成员漂移。
    """
    cams = {
        "200": _cam("200", lan_online=True),
        "300": _cam("300", lan_online=True),
        "400": _cam("400", lan_online=True),
        "500": _cam("500", lan_online=True),
    }
    managers = {"200", "300", "400", "500"}
    adapter, managers = _make_adapter_with_managers(cams, managers)

    await adapter.sync_devices()
    assert set(adapter.get_connected_devices()) == {"200", "300", "400", "500"}

    # `100` 后上线并顶位：活跃集变成 {100,200,300,400}，但数量仍是 4。
    cams["100"] = _cam("100", lan_online=True)
    # 补建有 10s 最小间隔节流(_ONDEMAND_REFRESH_MIN_INTERVAL_MS)，真实 sync 循环
    # 本就 10s 一轮；测试里两轮紧邻，需把时钟推过节流窗，否则挡住的是节流而非判据。
    adapter._last_ondemand_refresh_ms -= 60_000
    await adapter.sync_devices()

    connected = set(adapter.get_connected_devices())
    assert "100" in connected, (
        f"顶位相机必须被补建 manager 后连上，实际已连集={sorted(connected)}"
    )
    assert connected == {"100", "200", "300", "400"}, (
        f"应收敛到活跃集，实际={sorted(connected)}"
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


@pytest.mark.asyncio
async def test_feed_cap_evicts_zombie_before_healthy_channel():
    """上限收敛的淘汰顺序：先淘汰「本轮不通过严格门」的僵尸，再按字典序。

    只按字典序排会把优先级反转。保留集刻意放宽了 LAN 门（正是要救「云端在线但
    lan_online 偶发假 False」那类），代价是已连集里可能留着字典序靠前的僵尸通道
    （云端在线、LAN 已探不到、原生也没连上）。它会把字典序靠后、刚刚真连上的健康
    通道挤掉：日志每轮刷一条 over feed cap，那一路投喂反复中断，而占着名额的僵尸
    一帧不出。

    构造：050 是僵尸（lan_online=False），100~400 健康，上限 4 ⇒ 已连 5 路。
    """
    cams = {
        "050": _cam("050", lan_online=False),
        "100": _cam("100", lan_online=True),
        "200": _cam("200", lan_online=True),
        "300": _cam("300", lan_online=True),
        "400": _cam("400", lan_online=True),
    }
    adapter = _make_adapter(cams)
    # 让 050 已经在已连集里（上一轮 LAN 可达时连上的，之后探测掉了但保留集保住它）。
    cams["050"].lan_online = True
    await adapter.sync_devices(cams)
    cams["050"].lan_online = False

    await adapter.sync_devices(cams)

    connected = set(adapter.get_connected_devices())
    assert len(connected) == MAX_ENABLED_CAMERAS
    assert "050" not in connected, f"僵尸应优先被淘汰，实际={sorted(connected)}"
    assert "400" in connected, "健康通道不该被字典序靠前的僵尸挤掉"


@pytest.mark.asyncio
async def test_feed_cap_falls_back_to_did_order_when_discover_fails():
    """严格集取不到时退回纯字典序（与改动前行为一致），不能因此放弃收敛。"""
    cams = {f"{i}00": _cam(f"{i}00", lan_online=True) for i in range(1, 6)}
    adapter = _make_adapter(cams)
    await adapter.sync_devices(cams)
    # 手工塞满 5 路，再让 discover 抛错。
    from miloco.perception.collect.camera_adapter import _CameraDeviceState

    adapter._devices["500"] = _CameraDeviceState(did="500")
    assert len(adapter._devices) == 5

    async def _boom(*a, **kw):
        raise RuntimeError("cache not ready")

    adapter.discover_devices = _boom  # type: ignore[assignment]
    await adapter._converge_feed_cap()

    connected = set(adapter.get_connected_devices())
    assert len(connected) == MAX_ENABLED_CAMERAS
    assert "500" not in connected, "字典序最末的应被淘汰"


@pytest.mark.asyncio
async def test_ondemand_refresh_not_triggered_for_unconnectable_camera():
    """LAN 不可达、原生也没连上的相机不得让补建判据永真。

    refresh_cameras 建 manager 用的是同一道严格门 ⇒ 它压根不会为这台建 manager ⇒
    触发 refresh 是必然空转。而 missing 恒非空 + 节流窗(10s) ≈ sync 周期(10s) ⇒
    每轮都打一次云端 get_cameras_async。
    """
    cams = {
        "cam1": _cam("cam1", lan_online=True),
        "dead": _cam("dead", lan_online=False),  # 云端在线、LAN 探不到、没连上
    }
    adapter = _make_adapter(cams)
    proxy = adapter._miot_proxy
    proxy.refresh_cameras = AsyncMock(return_value=cams)

    await adapter.sync_devices()
    proxy.refresh_cameras.reset_mock()
    adapter._last_ondemand_refresh_ms -= 60_000

    await adapter.sync_devices()

    proxy.refresh_cameras.assert_not_awaited()
