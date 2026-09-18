"""直播 WS 发送通道背压单测。

核心语义(_SubscriberSender):卡顿=队列丢最旧、恢复续流不断连;
失联=send 挂满 20s 判死逐出,此后投递被拒。mock 掉 ws/manager。
"""

from __future__ import annotations

import asyncio
import json
import time
import types
from collections import OrderedDict
from unittest.mock import AsyncMock

import miloco.miot.ws as ws_mod
import numpy as np
import pytest
from fastapi.websockets import WebSocketState
from miloco.miot.ws import (
    _MSG_BYTES,
    _MSG_TEXT,
    MIoTAudioStreamManager,
    MIoTVideoStreamManager,
    _SubscriberSender,
)
from miot.types import MIoTCameraCodec


def _callback(mgr: MIoTVideoStreamManager):
    # __video_stream_callback 双下划线私有 → name-mangled
    return getattr(mgr, "_MIoTVideoStreamManager__video_stream_callback")


def _audio_callback(mgr: MIoTAudioStreamManager):
    return getattr(mgr, "_MIoTAudioStreamManager__audio_stream_callback")


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


class _ScriptedWS:
    """可控 send 行为的假 ws。

    mode:
      healthy      — 所有 send 立即完成并记录。
      hang_once    — 第一笔 send(文本或二进制都算)挂起直到 release 被置位,
                     之后所有 send 即时完成。模拟一次网络波动。
      hang_forever — 所有 send 永久挂起。模拟静默失联(写背压门不放行)。
    """

    client_state = WebSocketState.CONNECTED

    def __init__(self, mode: str = "healthy"):
        self.mode = mode
        self.sent: list[bytes] = []
        self.texts: list[str] = []
        self.closed = False
        self.close_code: int | None = None
        self.first_send_started = asyncio.Event()
        self._release = asyncio.Event()

    async def send_bytes(self, payload: bytes) -> None:
        await self._maybe_hang()
        self.sent.append(payload)

    async def send_text(self, text: str) -> None:
        await self._maybe_hang()
        self.texts.append(text)

    async def _maybe_hang(self) -> None:
        if self.mode == "healthy":
            return
        if self.mode == "hang_once" and self._release.is_set():
            return
        self.first_send_started.set()
        await self._release.wait()
        if self.mode == "hang_once":
            self._release.set()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = True
        self.close_code = code


class _OrderRecordingWS(_ScriptedWS):
    """记录 text/bytes 的实际发送先后(判定「init 先于数据送达」用)。"""

    def __init__(self, mode: str = "healthy"):
        super().__init__(mode)
        self.order: list[str] = []

    async def send_text(self, text: str) -> None:
        await super().send_text(text)
        self.order.append("text")

    async def send_bytes(self, payload: bytes) -> None:
        await super().send_bytes(payload)
        self.order.append("bytes")


def _attach_video(mgr, ws, tag="cam.0", user="u", cid="c0") -> _SubscriberSender:
    sender = _SubscriberSender(
        ws,
        camera_id=tag.rsplit(".", 1)[0],
        channel=int(tag.rsplit(".", 1)[1]),
        camera_tag=tag,
        user_name=user,
        token_hash="tok",
        cid=cid,
        on_dead=mgr._evict_stale_connection,
        maxsize=8,
    )
    mgr._camera_connect_map.setdefault(tag, {}).setdefault(
        f"{user}.tok", OrderedDict()
    )
    mgr._camera_connect_map[tag][f"{user}.tok"][cid] = sender
    return sender


def _attach_audio(mgr, ws, tag="cam.0", user="u", cid="c0") -> _SubscriberSender:
    sender = _SubscriberSender(
        ws,
        camera_id=tag.rsplit(".", 1)[0],
        channel=int(tag.rsplit(".", 1)[1]),
        camera_tag=tag,
        user_name=user,
        token_hash="tok",
        cid=cid,
        on_dead=mgr._evict_stale_audio_connection,
        maxsize=25,
    )
    mgr._camera_connect_map.setdefault(tag, {}).setdefault(
        f"{user}.tok", OrderedDict()
    )
    mgr._camera_connect_map[tag][f"{user}.tok"][cid] = sender
    return sender


async def _wait_sent(ws: _ScriptedWS, n: int, timeout: float = 2.0) -> None:
    """等到 ws 收满 n 条消息(文本+二进制合计),避免断言时序竞争。"""

    async def _until():
        while len(ws.sent) + len(ws.texts) < n:
            await asyncio.sleep(0.002)

    await asyncio.wait_for(_until(), timeout)


async def _stop(*senders: _SubscriberSender) -> None:
    """测试收尾:停掉所有发送协程,防 pending task 泄漏告警。"""
    for s in senders:
        s.cancel()
    await asyncio.gather(*(s._task for s in senders), return_exceptions=True)


def _init_json(codec: str) -> str:
    return json.dumps(
        {"type": "init", "codec": codec, "sampleRate": 48000, "numberOfChannels": 1}
    )


@pytest.fixture
def fake_manager(monkeypatch):
    """替换 ws 模块级 manager,收集 stop_video_stream / stop_audio_stream 调用。"""
    svc = types.SimpleNamespace(
        stop_video_stream=AsyncMock(),
        stop_audio_stream=AsyncMock(),
        get_audio_codec=lambda *a, **k: None,
    )
    monkeypatch.setattr(ws_mod, "manager", types.SimpleNamespace(miot_service=svc))
    return svc


# ---------- 视频:投递 / 卡顿丢旧 / 恢复续流 ----------


async def test_fanout_offers_to_all_subscribers(fake_manager):
    mgr = MIoTVideoStreamManager()
    a = _attach_video(mgr, _ScriptedWS(), cid="c0")
    b = _attach_video(mgr, _ScriptedWS(), user="v", cid="c1")

    await mgr._broadcast("cam.0", payload=b"nal")

    await _wait_sent(a.ws, 1)
    await _wait_sent(b.ws, 1)
    assert a.ws.sent == [b"nal"] and b.ws.sent == [b"nal"]
    await _stop(a, b)


async def test_stall_drops_stale_frames_and_resumes_with_fresh(fake_manager):
    """核心场景:网络波动 → 过时帧即产即弃;恢复后续发最新帧,连接不断。"""
    mgr = MIoTVideoStreamManager()
    ws = _ScriptedWS(mode="hang_once")
    sender = _attach_video(mgr, ws)

    await mgr._broadcast("cam.0", payload=b"f1")
    await asyncio.wait_for(ws.first_send_started.wait(), 1)  # f1 已挂在网上
    for i in range(2, 12):  # 波动期间继续来帧:队列 maxlen=8,最旧的被覆盖
        await mgr._broadcast("cam.0", payload=f"f{i}".encode())

    ws._release.set()  # 网络恢复:f1 + 队列里最新 8 帧(f4..f11)会发出
    await _wait_sent(ws, 9)
    assert ws.sent[0] == b"f1"
    assert ws.sent[1] == b"f4"  # f2/f3 已作为过时数据被丢弃
    assert ws.sent[-1] == b"f11"
    # 连接未被逐出,仍在连接表里
    assert mgr._camera_connect_map["cam.0"]["u.tok"]["c0"] is sender
    await _stop(sender)


async def test_liveness_timeout_evicts_and_stops_stream(fake_manager):
    """静默失联:send 挂满判死阈值 → 逐出 + 停流 + 主动 close。"""
    mgr = MIoTVideoStreamManager()
    mgr._camera_reg_id["cam.0"] = 7
    ws = _ScriptedWS(mode="hang_forever")
    sender = _attach_video(mgr, ws)
    sender._LIVENESS_S = 0.05

    await mgr._broadcast("cam.0", payload=b"nal")
    await asyncio.wait_for(sender._task, 2)

    assert "cam.0" not in mgr._camera_connect_map
    fake_manager.stop_video_stream.assert_awaited_once_with("cam", 0, 7)
    assert ws.closed and ws.close_code == 1001
    # 判死后生产端再 offer 直接被拒(不复活、不报错)——「不再往队列里塞数据」
    sender.offer(_MSG_BYTES, b"x")
    assert not sender._queue


async def test_send_error_evicts(fake_manager):
    """对端断开等发送错误 → 判死逐出(不等 route 清理)。"""
    mgr = MIoTVideoStreamManager()
    mgr._camera_reg_id["cam.0"] = 7

    class _ErrWS(_ScriptedWS):
        async def send_bytes(self, payload):
            raise ConnectionResetError("closed")

    ws = _ErrWS()
    sender = _attach_video(mgr, ws)

    await mgr._broadcast("cam.0", payload=b"nal")
    await asyncio.wait_for(sender._task, 2)

    assert "cam.0" not in mgr._camera_connect_map
    fake_manager.stop_video_stream.assert_awaited_once_with("cam", 0, 7)
    await _stop(sender)


async def test_healthy_subscriber_unaffected_by_stalled_peer(fake_manager):
    """多订阅者隔离:一个卡顿不能冻结另一个(旧 gather 方案的头部阻塞)。"""
    mgr = MIoTVideoStreamManager()
    mgr._camera_codec["cam.0"] = MIoTCameraCodec.VIDEO_H264  # 跳过 init 广播
    enc = AsyncMock()
    enc.encode.return_value = [(b"nal", True)]
    mgr._camera_encoder["cam.0"] = enc
    stalled = _attach_video(mgr, _ScriptedWS(mode="hang_once"), cid="c0")
    healthy = _attach_video(mgr, _ScriptedWS(), user="v", cid="c1")

    for i in range(3):
        await _callback(mgr)("cam", _frame(), i, 0, 0, 0)
    await _wait_sent(healthy.ws, 3)

    assert len(healthy.ws.sent) == 3  # 健康端一帧不落
    assert stalled.ws.sent == []  # 卡顿端挂着,但没有拖累别人
    assert "c0" in mgr._camera_connect_map["cam.0"]["u.tok"]  # 也没被误逐出
    await _stop(stalled, healthy)


async def test_frame_storm_bounded_and_stops_stream(fake_manager):
    """事故核心回归:死连接 + 连续帧派发 → 生产端全程不阻塞,判死后停流。"""
    mgr = MIoTVideoStreamManager()
    mgr._camera_reg_id["cam.0"] = 7
    ws = _ScriptedWS(mode="hang_forever")
    sender = _attach_video(mgr, ws)
    sender._LIVENESS_S = 0.05
    enc = AsyncMock()
    enc.encode.return_value = [(b"nal", True)]
    mgr._camera_encoder["cam.0"] = enc

    started = time.monotonic()
    for i in range(20):
        await _callback(mgr)("cam", _frame(), i, 0, 0, 0)
    await asyncio.wait_for(sender._task, 2)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0  # 旧实现会在这里挂死/堆积
    assert "cam.0" not in mgr._camera_connect_map
    fake_manager.stop_video_stream.assert_awaited_once_with("cam", 0, 7)


async def test_cached_init_survives_burst_before_sender_scheduled(fake_manager):
    """late-joiner cached init 不能被数据突发挤出队列。

    new_connection 把 cached init 与数据帧放进同一条队列(maxsize=8)。发送
    协程获得调度前,若积压超过容量,init 一旦被挤出客户端就永远拿不到解码
    参数(watch.html 无 codecHint 会丢弃全部二进制帧)且服务端无补发路径。
    """
    mgr = MIoTVideoStreamManager()
    mgr._camera_codec["cam.0"] = MIoTCameraCodec.VIDEO_H264
    ws = _OrderRecordingWS()
    sender = _attach_video(mgr, ws)

    # 复刻 new_connection 的 cached-init 投递;随后的突发灌帧走真实 fanout。
    # _broadcast 内部无挂起点,连续 await 不会让出事件循环 → 整批数据都落在
    # 发送协程首次调度之前(即 init 仍在队列里时队列已被灌满)。
    sender.offer(_MSG_TEXT, mgr._build_init_msg(MIoTCameraCodec.VIDEO_H264))
    for i in range(1, 10):  # 9 帧 > maxsize=8
        await mgr._broadcast("cam.0", payload=f"n{i}".encode())

    await _wait_sent(ws, 8)
    assert ws.texts == [mgr._build_init_msg(MIoTCameraCodec.VIDEO_H264)]
    # 被挤掉的是最旧的数据帧,不是 init;且 init 先于全部数据送达
    assert ws.sent == [f"n{i}".encode() for i in range(3, 10)]
    assert ws.order == ["text"] + ["bytes"] * 7
    await _stop(sender)


async def test_close_connection_cancels_sender(fake_manager):
    """route 侧正常断开:发送协程必须被回收(不泄漏)。"""
    mgr = MIoTVideoStreamManager()
    sender = _attach_video(mgr, _ScriptedWS(), user="u")

    await mgr.close_connection("u", "tok", "cam", 0, "c0")
    await asyncio.gather(sender._task, return_exceptions=True)

    assert sender.closed
    assert sender._task.done()
    assert not mgr._camera_connect_map.get("cam.0")


async def test_evict_idempotent_after_route_cleanup(fake_manager):
    mgr = MIoTVideoStreamManager()
    mgr._camera_reg_id["cam.0"] = 7
    ws = _ScriptedWS(mode="hang_forever")
    sender = _attach_video(mgr, ws, user="u", cid="c0")
    sender._LIVENESS_S = 0.05

    await mgr._broadcast("cam.0", payload=b"nal")
    await asyncio.wait_for(sender._task, 2)
    await mgr.close_connection("u", "tok", "cam", 0, "c0")

    fake_manager.stop_video_stream.assert_awaited_once()  # 无重复 teardown


# ---------- 音频 ----------


async def test_audio_stall_drops_oldest_keeps_window(fake_manager):
    """音频卡顿:丢最旧保住约 0.5s 连续窗,恢复后续播,不断连。"""
    mgr = MIoTAudioStreamManager()
    ws = _ScriptedWS(mode="hang_once")
    sender = _attach_audio(mgr, ws)

    await _audio_callback(mgr)("cam", b"p1", 1, 0, 0)
    await asyncio.wait_for(ws.first_send_started.wait(), 1)
    for i in range(2, 32):  # 30 帧,超过 maxsize=25
        await _audio_callback(mgr)("cam", f"p{i}".encode(), i, 0, 0)

    ws._release.set()
    await _wait_sent(ws, 26)  # p1 + 窗口 25 帧

    assert ws.sent[0] == b"p1"
    assert ws.sent[1] == b"p7"  # 最旧 5 帧被丢,窗口保住
    assert ws.sent[-1] == b"p31"
    assert len(ws.sent) == 26
    assert mgr._camera_connect_map["cam.0"]["u.tok"]["c0"] is sender  # 未被逐出
    await _stop(sender)


async def test_audio_liveness_timeout_evicts_and_stops_stream(fake_manager):
    mgr = MIoTAudioStreamManager()
    ws = _ScriptedWS(mode="hang_forever")
    sender = _attach_audio(mgr, ws)
    sender._LIVENESS_S = 0.05
    mgr._camera_init_done.add("cam.0")

    await _audio_callback(mgr)("cam", b"pcm", 1, 0, 0)
    await asyncio.wait_for(sender._task, 2)

    assert "cam.0" not in mgr._camera_connect_map
    assert "cam.0" not in mgr._camera_init_done
    fake_manager.stop_audio_stream.assert_awaited_once_with("cam", 0)
    assert ws.closed and ws.close_code == 1001


async def test_audio_init_survives_burst_before_sender_scheduled(fake_manager):
    """音频首帧路径:init 不能被数据突发挤出队列。

    首帧回调把 init 与后续帧放进同一条队列(maxsize=25),发送协程获得调度
    前若积压超过容量,init 被挤出后 _camera_init_done 已置位、永不补发,
    客户端拿不到 codec/采样率/声道,只剩一堆解不了的二进制帧。
    """
    mgr = MIoTAudioStreamManager()
    ws = _OrderRecordingWS()
    sender = _attach_audio(mgr, ws)
    fake_manager.get_audio_codec = lambda *a, **k: "opus"

    # 首帧回调投 init+p0;回调内无挂起点,连续 await 不会让出事件循环 →
    # 后续突发全部落在发送协程首次调度之前(init 仍在队列里时队列已灌满)。
    await _audio_callback(mgr)("cam", b"p0", 0, 0, 0)
    for i in range(1, 27):  # 26 帧 > maxsize=25
        await _audio_callback(mgr)("cam", f"p{i}".encode(), i, 0, 0)

    await _wait_sent(ws, 25)
    assert ws.texts == [_init_json("opus")]
    # 被挤掉的是最旧的数据帧(p0/p1/p2),不是 init;且 init 先于全部数据送达
    assert ws.sent == [f"p{i}".encode() for i in range(3, 27)]
    assert ws.order == ["text"] + ["bytes"] * 24
    await _stop(sender)


async def test_audio_healthy_passthrough_order(fake_manager):
    """init 信令与数据帧同队列保序;健康路径逐帧送达。"""
    mgr = MIoTAudioStreamManager()
    ws = _ScriptedWS()
    sender = _attach_audio(mgr, ws)
    fake_manager.get_audio_codec = lambda *a, **k: "opus"

    await _audio_callback(mgr)("cam", b"pcm1", 1, 0, 0)
    await _audio_callback(mgr)("cam", b"pcm2", 2, 0, 0)
    await _wait_sent(ws, 3)

    assert ws.texts == [_init_json("opus")]
    assert ws.sent == [b"pcm1", b"pcm2"]
    await _stop(sender)
