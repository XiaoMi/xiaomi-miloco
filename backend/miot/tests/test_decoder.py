# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""MIoTMediaDecoder.stop() 顺序回归测试。"""

import asyncio
from unittest.mock import Mock

from miot.decoder import MIoTMediaDecoder


def test_stop_joins_before_clearing_video_decoder():
    """stop() 必须先 join() 再把 _video_decoder 置 None。

    若顺序颠倒(先置 None 再 join),join 期间仍在跑的 run() 会走进
    _on_video_callback 的 ``if not self._video_decoder`` 分支重建一个 codec,
    这个新 codec 逃过释放 → frame-threading worker 泄漏、线程/RSS 随重连单调涨。
    锁定"join 被调用时 decoder 还在"这个顺序不变量,防回归。
    """
    loop = asyncio.new_event_loop()
    try:

        async def _noop_video(*_args):
            return None

        dec = MIoTMediaDecoder(
            frame_interval=1000,
            video_callback=_noop_video,
            main_loop=loop,
        )
        dec._queue = Mock()  # 隔离 ring buffer,只测 stop 的顺序
        dec._video_decoder = object()
        dec._audio_decoder = object()

        seen = {}

        def _record_join(timeout=None):
            # join 被调用的瞬间,decoder 必须还没被清空
            seen["video_decoder_at_join"] = dec._video_decoder

        dec.join = _record_join
        dec.stop()

        assert seen["video_decoder_at_join"] is not None, "join 前 decoder 就被清空 → 顺序颠倒"
        assert dec._video_decoder is None
        assert dec._audio_decoder is None
    finally:
        loop.close()
