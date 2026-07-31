"""``NalClipRecorder.feed_bgr`` 边界帧编码契约单测。

reviewer 指出:旧逻辑在 ``elapsed >= duration`` 的帧上先 finalize 再 return、
不编码这一帧——当帧时间戳恰好踩在 duration 边界(30fps 下 ts 分辨率 ~33ms,
对齐并非不可能)时丢掉最后一帧,"录了 15s"实测只有 ~14.967s。改成"先编码
当前帧,再判断是否到点 finalize"。

本测试用计数桩替掉真实 PyAV 编码(``_init_encoder`` / ``_encode_frame_sync``)
和 finalize,只验状态机:哪些帧进了编码、何时切 DONE、finalize 是否即时且只
触发一次——不引入 libx264 / mp4 mux 这条"现 codebase 不 mock 的重链路"。
"""

from __future__ import annotations

import asyncio

import numpy as np
from miloco.miot.ws import NalClipRecorder


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


def _make_rec(duration_ms: int):
    """构造一个把 PyAV 调用换成计数桩的 recorder。

    必须在 running loop 内调用——``NalClipRecorder.__init__`` 会取
    ``asyncio.get_running_loop()`` 建 future。返回 (rec, encoded, finalized),
    后两者是被桩记录的副作用列表。
    """
    rec = NalClipRecorder(duration_ms=duration_ms)
    encoded: list[int] = []
    finalized: list[bool] = []
    rec._init_encoder = lambda w, h: None  # type: ignore[method-assign]
    rec._encode_frame_sync = lambda bgr: encoded.append(int(bgr.shape[0]))  # type: ignore[method-assign]

    async def _fake_finalize() -> None:
        finalized.append(True)

    rec._finalize_async = _fake_finalize  # type: ignore[method-assign]
    return rec, encoded, finalized


async def test_boundary_frame_is_encoded_before_finalize():
    # duration=100;喂 ts=0,33,66,99,100。elapsed=100 的那帧正好踩边界,
    # 必须被编码(旧逻辑会丢它)。
    rec, encoded, finalized = _make_rec(100)
    for ts in (0, 33, 66, 99, 100):
        await rec.feed_bgr(_frame(), ts)
    await asyncio.sleep(0)  # 让 ensure_future 调度的 finalize 跑一次
    assert len(encoded) == 5          # 含边界帧
    assert rec._frame_count == 5
    assert rec._state == "DONE"
    assert finalized == [True]        # 即时 finalize


async def test_frames_past_boundary_not_encoded():
    # 到点后再来的帧(state==DONE)被首行短路:不编码、不重复 finalize。
    rec, encoded, finalized = _make_rec(100)
    for ts in (0, 100, 133, 166):
        await rec.feed_bgr(_frame(), ts)
    await asyncio.sleep(0)
    # ts=0 编码;ts=100 编码并触发 DONE;之后两帧被 DONE 短路
    assert len(encoded) == 2
    assert rec._frame_count == 2
    assert rec._state == "DONE"
    assert finalized == [True]        # 只 finalize 一次


async def test_under_duration_keeps_recording():
    # 没到 duration 的帧持续编码,不 finalize。
    rec, encoded, finalized = _make_rec(1000)
    for ts in (0, 33, 66, 99):
        await rec.feed_bgr(_frame(), ts)
    assert len(encoded) == 4
    assert rec._state == "RECORDING"
    assert finalized == []


async def test_first_frame_inits_encoder_and_anchors_start_ts():
    # 首帧:WAITING_FIRST → 调 _init_encoder(w,h)、以首帧 ts 为 start_ts 基准、
    # 切 RECORDING。验证 elapsed 从首帧起算(首帧 ts 非 0 也不应立刻判到点)。
    rec, encoded, _ = _make_rec(1000)
    inited: list[tuple[int, int]] = []
    rec._init_encoder = lambda w, h: inited.append((w, h))  # type: ignore[method-assign]
    await rec.feed_bgr(_frame(), 500)
    assert inited == [(4, 4)]
    assert rec._start_ts == 500
    assert rec._state == "RECORDING"
    assert len(encoded) == 1


async def test_init_encoder_pins_thread_count():
    # 不桩 _init_encoder——要验的就是真实实现里那行 thread_count 赋值还在。
    # 其余用例把 _init_encoder 整个桩掉了,删掉那行也照样绿;此用例守住线程数。
    from miot.tuning import ENCODE_THREADS

    rec = NalClipRecorder(duration_ms=1000)
    try:
        rec._init_encoder(64, 64)
        assert rec._stream is not None
        # Stream 上的 thread_count 转发到 codec context(av/stream.py:152-154),
        # 从 codec_context 读回才是底层真实值。
        assert rec._stream.codec_context.thread_count == ENCODE_THREADS
    finally:
        # _init_encoder 真开了一个 mp4 输出容器,连它一起收(先容器后线程池)。
        if rec._container is not None:
            rec._container.close()
        rec._executor.shutdown(wait=False)
