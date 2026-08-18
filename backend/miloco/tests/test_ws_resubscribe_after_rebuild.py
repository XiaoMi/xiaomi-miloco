"""``MIoTVideoStreamManager.resubscribe_camera``：native 会话重建后补注册解码订阅。

静默自愈会 destroy 整个 native 实例（``mi_camera_free``），旧实例上的解码回调随之
全部消失、新实例的回调表是空的。**四个**向原生实例注册流的消费方里只有感知适配器
有兜底（同一轮 sync 的 connect_device 会补注册），另三个没有：

- watch 直播：``_camera_reg_id`` 还持有旧实例上的死 id，要等 15s 失流看门狗踢前端
  重连才恢复（住户视角 15~45s 冻结，而这恰好发生在住户正盯着这台相机的时刻）；
- ``record_clip``：录像器只会等到 ``recorder.wait`` 超时、API 返回 504，这条路径
  没有客户端看门狗兜底；
- ``/ws/audio_stream`` 的原始音频：``MIoTAudioStreamManager`` 另有一张连接表，且它
  「要不要注册」的判据是**连接表空不空** ⇒ 重建期间客户端还挂着、表非空 ⇒ 连新接进来
  的订阅方也不会重新触发注册 ⇒ 该通道永久静音，且没有任何看门狗会察觉。

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


async def test_failed_resubscribe_clears_reg_id_to_minus_one(monkeypatch):
    """补注册失败必须把 reg_id 清成 -1，且不中断其余通道的补注册。

    留着旧号码是有害的：``_next_reg_id`` 每个新实例都从 1 重新发号，直播与感知注册进
    同一个 ``decode_video_frame.{channel}`` 字典、unregister 只按号码 pop 不校验归属。
    订阅方离开时 ``_teardown_if_idle`` 只判 ``reg_id >= 0``，就会拿死号去新实例上注销，
    pop 掉感知在新实例上拿到的同号回调 → 住户关掉直播页后这台相机的感知彻底零帧，
    还要等静默检测的 5min 重建冷却过去才自愈。

    这条路不需要 SDK 抽风：``reconnect_camera`` 在「旧实例已拆、新实例没建起来」时是
    静默返回不抛的，补注册照常执行，而底层已经没有该相机的 manager → 桥接层返回 -1。
    """
    start = AsyncMock(side_effect=[-1, RuntimeError("boom"), 21])
    _patch_sdk(monkeypatch, start)
    mgr = _mgr_with_ws("dual.0", reg_id=1)
    for ch, rid in (("1", 2), ("2", 3)):
        mgr._camera_reg_id[f"dual.{ch}"] = rid
        mgr._camera_connect_map[f"dual.{ch}"] = {"u": {"c": AsyncMock()}}

    await mgr.resubscribe_camera("dual")

    assert start.await_count == 3
    # 失败的两路清成 -1（_teardown_if_idle 天然跳过注销），第三路换上新 id。
    # 键本身要保留：resubscribe_camera 靠遍历这些键决定下次重建补哪些通道。
    assert mgr._camera_reg_id["dual.0"] == -1
    assert mgr._camera_reg_id["dual.1"] == -1
    assert mgr._camera_reg_id["dual.2"] == 21


async def test_teardown_after_failed_resubscribe_does_not_unregister(monkeypatch):
    """上一条的真实危害面：补注册失败后订阅方离开，绝不能去新实例上注销。

    直接钉 ``_teardown_if_idle`` 的行为——它是把死号送进 SDK 的那一步，也是「关掉
    直播页导致感知零帧」这条坏行为的落点。
    """
    stop = AsyncMock()
    monkeypatch.setattr(
        ws_mod,
        "manager",
        SimpleNamespace(
            miot_service=SimpleNamespace(
                start_video_stream=AsyncMock(return_value=-1),
                stop_video_stream=stop,
            )
        ),
    )
    mgr = _mgr_with_ws("cam1.0", reg_id=1)

    await mgr.resubscribe_camera("cam1")
    # 住户关掉 watch 页 → 订阅方清空 → 走 teardown。
    mgr._camera_connect_map.pop("cam1.0")
    await mgr._teardown_if_idle("cam1", 0, "cam1.0")

    stop.assert_not_awaited()


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


# ── 音频侧：第四个消费方，重建后同样要补注册 ──────────────────────────────


def _audio_mgr(camera_tag: str) -> ws_mod.MIoTAudioStreamManager:
    mgr = ws_mod.MIoTAudioStreamManager()
    mgr._camera_connect_map[camera_tag] = {"u": {"c0": AsyncMock()}}
    mgr._camera_init_done.add(camera_tag)
    return mgr


def _patch_audio_sdk(monkeypatch, start: AsyncMock) -> None:
    monkeypatch.setattr(
        ws_mod,
        "manager",
        SimpleNamespace(miot_service=SimpleNamespace(start_audio_stream=start)),
    )


async def test_audio_channel_is_resubscribed_after_rebuild(monkeypatch):
    """音频侧「要不要注册」的判据是连接表空不空，重建期间客户端还挂着 ⇒ 表非空 ⇒
    连新接进来的订阅方也不会重新触发注册，这条通道会永久静音直到最后一个连接断开。

    视频有 15s 失流看门狗、感知有同轮 sync，音频两个都没有（首帧看门狗只挂视频路由）。
    """
    start = AsyncMock()
    _patch_audio_sdk(monkeypatch, start)
    mgr = _audio_mgr("cam1.0")

    await mgr.resubscribe_camera("cam1")

    start.assert_awaited_once()
    assert start.await_args.kwargs["camera_id"] == "cam1"
    assert start.await_args.kwargs["channel"] == 0


async def test_audio_init_flag_cleared_so_codec_is_redetected(monkeypatch):
    """必须清 _camera_init_done：codec 缓存在 handler 上，重建后是全新的、值为 None。

    不清的话中途接入的客户端会拿到 codec: null 的 init（new_connection 里
    `if camera_tag in self._camera_init_done` 就直接发了）。
    """
    _patch_audio_sdk(monkeypatch, AsyncMock())
    mgr = _audio_mgr("cam1.0")

    await mgr.resubscribe_camera("cam1")

    assert "cam1.0" not in mgr._camera_init_done


async def test_audio_channel_without_connections_is_skipped(monkeypatch):
    """连接表为空的通道不补注册——没人听就别白开一路流、白占并发名额。"""
    start = AsyncMock()
    _patch_audio_sdk(monkeypatch, start)
    mgr = ws_mod.MIoTAudioStreamManager()
    mgr._camera_connect_map["cam1.0"] = {}

    await mgr.resubscribe_camera("cam1")

    start.assert_not_awaited()


async def test_audio_other_cameras_untouched(monkeypatch):
    """只重订本次重建那台。"""
    start = AsyncMock()
    _patch_audio_sdk(monkeypatch, start)
    mgr = _audio_mgr("cam1.0")
    mgr._camera_connect_map["cam2.0"] = {"u": {"c0": AsyncMock()}}
    mgr._camera_init_done.add("cam2.0")

    await mgr.resubscribe_camera("cam1")

    assert start.await_count == 1
    assert "cam2.0" in mgr._camera_init_done


async def test_audio_failure_keeps_going_and_keeps_init_flag(monkeypatch):
    """某一路注册失败不中断其余通道；失败那路不清 init 标记（它还是旧状态）。"""
    start = AsyncMock(side_effect=[RuntimeError("boom"), None])
    _patch_audio_sdk(monkeypatch, start)
    mgr = _audio_mgr("dual.0")
    mgr._camera_connect_map["dual.1"] = {"u": {"c0": AsyncMock()}}
    mgr._camera_init_done.add("dual.1")

    await mgr.resubscribe_camera("dual")

    assert start.await_count == 2
    assert "dual.0" in mgr._camera_init_done
    assert "dual.1" not in mgr._camera_init_done
