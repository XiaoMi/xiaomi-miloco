"""``MIoTVideoStreamManager.__video_stream_callback`` 纯 recorder 场景控制流单测。

reviewer #2:只点录制、没开 watch tab 时(``_camera_connect_map`` 空、只有
recorder 附着),旧逻辑仍每帧跑 H.264 encode + broadcast——libx264 给零 WS
客户端白烧 CPU,还和 recorder 自己的编码抢核。改动:

- recorder fan-out 后,若无 WS 客户端就 early-return,跳过白跑的 encode;
- codec init 的 announce 上移到 encode 之前、不再依赖 packets,确保 WS 客户端
  中途接入仍能经 ``new_connection`` 的 cached-codec replay 拿到 init handshake。

本测试 mock 掉 encoder / recorder / ws,只验回调控制流——不引入真实 libx264 /
SDK / WebSocket。私有回调经 name-mangling 取。
"""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock

import numpy as np
from miloco.miot.ws import MIoTVideoStreamManager, _SubscriberSender
from miot.types import MIoTCameraCodec


def _attach_sender(mgr, cid="c0", user_tag="u.tok"):
    """轻量订阅者:真 sender 包 AsyncMock ws,避免裸 mock 的未 await 协程告警。"""
    sender = _SubscriberSender(
        AsyncMock(),
        camera_id="cam",
        channel=0,
        camera_tag="cam.0",
        user_name=user_tag,
        token_hash="",
        cid=cid,
        on_dead=mgr._evict_stale_connection,
        maxsize=8,
    )
    mgr._camera_connect_map.setdefault("cam.0", {}).setdefault(user_tag, {})[cid] = (
        sender
    )
    return sender


async def _stop_sender(sender):
    sender.cancel()
    await asyncio.gather(sender._task, return_exceptions=True)


def _callback(mgr: MIoTVideoStreamManager):
    # __video_stream_callback 是双下划线私有方法 → name-mangled
    return getattr(mgr, "_MIoTVideoStreamManager__video_stream_callback")


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


def _mgr_with_recorder(camera_tag: str = "cam.0"):
    """manager + 一个假 recorder + 一个假 encoder;connect_map 留空(无 WS)。"""
    mgr = MIoTVideoStreamManager()
    rec = AsyncMock()  # rec.feed_bgr 是可 await 的
    enc = AsyncMock()  # enc.encode 是可 await 的
    mgr._camera_recorders[camera_tag] = [rec]
    mgr._camera_encoder[camera_tag] = enc
    return mgr, rec, enc


async def test_recorder_only_skips_encode_but_feeds_recorder():
    # did="cam" channel=0 → camera_tag="cam.0"
    mgr, rec, enc = _mgr_with_recorder("cam.0")
    await _callback(mgr)("cam", _frame(), 1234, 0, 0, 0)
    rec.feed_bgr.assert_awaited_once()  # recorder 收到 BGR
    enc.encode.assert_not_awaited()  # 白跑的 live encode 被跳过


async def test_recorder_only_still_sets_codec_for_late_joiner():
    # codec init 上移:即便此刻没 WS 客户端,_camera_codec 也被填,
    # 这样后来连入的 WS 客户端能经 new_connection 的 cached-codec replay 拿到 init。
    mgr, _, _ = _mgr_with_recorder("cam.0")
    await _callback(mgr)("cam", _frame(), 1, 0, 0, 0)
    assert mgr._camera_codec.get("cam.0") == MIoTCameraCodec.VIDEO_H264


async def test_with_ws_client_runs_encode():
    # 有 WS 客户端时不跳过:encode 照跑,recorder 也照喂。
    mgr, rec, enc = _mgr_with_recorder("cam.0")
    enc.encode.return_value = []  # 无 packets → keyframe 广播循环空转
    sender = _attach_sender(mgr)
    await _callback(mgr)("cam", _frame(), 1, 0, 0, 0)
    enc.encode.assert_awaited_once()
    rec.feed_bgr.assert_awaited_once()
    await _stop_sender(sender)


async def test_no_subscribers_returns_early():
    # 既无 WS 又无 recorder → _has_subscribers False,直接 return:不喂不编码。
    mgr = MIoTVideoStreamManager()
    enc = AsyncMock()
    mgr._camera_encoder["cam.0"] = enc
    await _callback(mgr)("cam", _frame(), 1, 0, 0, 0)
    enc.encode.assert_not_awaited()
    assert "cam.0" not in mgr._camera_codec


async def test_sentinel_ts_sanitized_in_wire_header():
    """哨兵 PTS 0xFFFFFFFFFFFFFFFF 不能原样进 wire 帧头(否则前端 WebCodecs
    EncodedVideoChunk timestamp=ts*1000 溢出 [EnforceRange] long long 弹红)。
    应替换成服务端 decoded_unix_ms。"""
    mgr, _, enc = _mgr_with_recorder("cam.0")
    # is_keyframe=True 必须:回调有 _camera_seen_keyframe 门控,首个非关键帧会被
    # continue 丢弃 → 不广播 → sent 空。用 keyframe 绕过门控,保证这一帧真被广播。
    enc.encode.return_value = [(b"\x00\x00\x00\x01nal", True)]  # 一个 keyframe 包
    sender = _attach_sender(mgr)  # 有 WS 才走 encode/broadcast
    sent: list[bytes] = []

    async def _capture(camera_tag, *, text=None, payload=None):
        if payload is not None:
            sent.append(payload)

    mgr._broadcast = _capture  # type: ignore[assignment]
    decoded_unix_ms = 1_700_000_000_000
    await _callback(mgr)("cam", _frame(), 0xFFFFFFFFFFFFFFFF, 0, 0, decoded_unix_ms)
    assert len(sent) == 1
    # 帧头 ">B7xQ":offset 8-16 是 uint64 ts
    wire_ts = struct.unpack(">Q", sent[0][8:16])[0]
    assert wire_ts == decoded_unix_ms  # 哨兵被换成 wall-clock,不是原样透传
    await _stop_sender(sender)


async def test_normal_ts_passes_through_wire_header():
    """正常相机 ts(远低于安全上界)原样进 wire 帧头,不被误兜底。"""
    mgr, _, enc = _mgr_with_recorder("cam.0")
    enc.encode.return_value = [(b"\x00\x00\x00\x01nal", True)]
    sender = _attach_sender(mgr)
    sent: list[bytes] = []

    async def _capture(camera_tag, *, text=None, payload=None):
        if payload is not None:
            sent.append(payload)

    mgr._broadcast = _capture  # type: ignore[assignment]
    normal_ts = 192_914_858  # 典型 uptime ms
    await _callback(mgr)("cam", _frame(), normal_ts, 0, 0, 1_700_000_000_000)
    assert struct.unpack(">Q", sent[0][8:16])[0] == normal_ts
    await _stop_sender(sender)
