"""``MIoTVideoStreamManager.resubscribe_camera``：native 会话重建后补注册解码订阅。

静默自愈会 destroy 整个 native 实例（``mi_camera_free``），旧实例上的解码回调随之
全部消失、新实例的回调表是空的。三个解码订阅消费方里只有感知适配器有兜底（同一轮
sync 的 connect_device 会补注册），watch 直播与 ``record_clip`` 没有：

- watch 直播：``_camera_reg_id`` 还持有旧实例上的死 id，要等 15s 失流看门狗踢前端
  重连才恢复（住户视角 15~45s 冻结，而这恰好发生在住户正盯着这台相机的时刻）；
- ``record_clip``：录像器只会等到 ``recorder.wait`` 超时、API 返回 504，这条路径
  没有客户端看门狗兜底。

本测试 mock 掉 SDK（``manager.miot_service``），只验补注册的控制流与边界。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import miloco.miot.ws as ws_mod
from miloco.miot.ws import MIoTVideoStreamManager


def _patch_sdk(monkeypatch, start: AsyncMock) -> None:
    monkeypatch.setattr(
        ws_mod,
        "manager",
        SimpleNamespace(miot_service=SimpleNamespace(start_video_stream=start)),
    )


def _mgr_with_ws(camera_tag: str, reg_id: int = 7) -> MIoTVideoStreamManager:
    mgr = MIoTVideoStreamManager()
    mgr._camera_reg_id[camera_tag] = reg_id
    mgr._camera_connect_map[camera_tag] = {"u": {"c0": AsyncMock()}}
    mgr._camera_encoder[camera_tag] = AsyncMock()
    return mgr


async def test_resubscribes_ws_channel_with_new_reg_id(monkeypatch):
    """有 WS 订阅方的通道必须在新实例上重新注册，并换上新 reg_id。"""
    start = AsyncMock(return_value=42)
    _patch_sdk(monkeypatch, start)
    mgr = _mgr_with_ws("cam1.0", reg_id=7)

    await mgr.resubscribe_camera("cam1")

    start.assert_awaited_once()
    assert start.await_args.kwargs["camera_id"] == "cam1"
    assert start.await_args.kwargs["channel"] == 0
    assert mgr._camera_reg_id["cam1.0"] == 42


async def test_resubscribes_recorder_only_channel(monkeypatch):
    """只有录像器附着（住户没开 watch tab）的通道同样要补注册，否则录像等到 504。"""
    start = AsyncMock(return_value=5)
    _patch_sdk(monkeypatch, start)
    mgr = MIoTVideoStreamManager()
    mgr._camera_reg_id["cam1.0"] = 1
    mgr._camera_recorders["cam1.0"] = [AsyncMock()]

    await mgr.resubscribe_camera("cam1")

    start.assert_awaited_once()
    assert mgr._camera_reg_id["cam1.0"] == 5


async def test_resubscribes_all_channels_of_one_physical_camera(monkeypatch):
    """双摄：同一物理相机的所有活跃通道都要补，一条 native 会话带两路订阅。"""
    start = AsyncMock(side_effect=[11, 12])
    _patch_sdk(monkeypatch, start)
    mgr = _mgr_with_ws("dual.0")
    mgr._camera_reg_id["dual.1"] = 8
    mgr._camera_connect_map["dual.1"] = {"u": {"c1": AsyncMock()}}

    await mgr.resubscribe_camera("dual")

    assert start.await_count == 2
    assert sorted(mgr._camera_reg_id.values()) == [11, 12]


async def test_other_cameras_are_untouched(monkeypatch):
    """只重订本次重建的那台相机；别台的 native 实例没被 destroy，重订等于白开一路流。"""
    start = AsyncMock(return_value=99)
    _patch_sdk(monkeypatch, start)
    mgr = _mgr_with_ws("cam1.0")
    mgr._camera_reg_id["cam2.0"] = 3
    mgr._camera_connect_map["cam2.0"] = {"u": {"c0": AsyncMock()}}

    await mgr.resubscribe_camera("cam1")

    assert start.await_count == 1
    assert mgr._camera_reg_id["cam2.0"] == 3


async def test_channel_without_subscribers_is_not_resubscribed(monkeypatch):
    """订阅方已全部离开的通道不补注册——相机并发流名额有限，不该白占一路。"""
    start = AsyncMock(return_value=1)
    _patch_sdk(monkeypatch, start)
    mgr = MIoTVideoStreamManager()
    mgr._camera_reg_id["cam1.0"] = 7  # reg_id 还在，但 WS/recorder 都没了

    await mgr.resubscribe_camera("cam1")

    start.assert_not_awaited()


async def test_sdk_rejection_keeps_going_and_does_not_poison_reg_id(monkeypatch):
    """SDK 返回负值/抛异常时不能把失败值写进 reg_id，也不能中断其余通道的补注册。"""
    start = AsyncMock(side_effect=[-1, RuntimeError("boom"), 21])
    _patch_sdk(monkeypatch, start)
    mgr = _mgr_with_ws("dual.0", reg_id=1)
    for ch, rid in (("1", 2), ("2", 3)):
        mgr._camera_reg_id[f"dual.{ch}"] = rid
        mgr._camera_connect_map[f"dual.{ch}"] = {"u": {"c": AsyncMock()}}

    await mgr.resubscribe_camera("dual")

    assert start.await_count == 3
    # 前两路保留旧值（已是死 id，但绝不能被 -1 覆盖：-1 会让 _teardown_if_idle
    # 跳过 stop_video_stream），第三路换上新 id。
    assert mgr._camera_reg_id["dual.0"] == 1
    assert mgr._camera_reg_id["dual.1"] == 2
    assert mgr._camera_reg_id["dual.2"] == 21


async def test_encoder_and_keyframe_state_survive_rebuild(monkeypatch):
    """编码器与 keyframe 标记刻意不动：libx264 是本层自己的，编码流跨重建是连续的。

    清掉 ``_camera_seen_keyframe`` 只会让已在播的前端白等一个 GOP（~1.2s）。
    """
    start = AsyncMock(return_value=42)
    _patch_sdk(monkeypatch, start)
    mgr = _mgr_with_ws("cam1.0")
    enc = mgr._camera_encoder["cam1.0"]
    mgr._camera_seen_keyframe.add("cam1.0")

    await mgr.resubscribe_camera("cam1")

    assert mgr._camera_encoder["cam1.0"] is enc
    assert "cam1.0" in mgr._camera_seen_keyframe
