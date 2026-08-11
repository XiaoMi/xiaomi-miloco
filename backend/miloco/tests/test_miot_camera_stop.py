# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""MIoTCameraInstance.stop_async() 清理不变量回归测试。"""

import asyncio
import logging
from unittest.mock import Mock

from miot.camera import MIoTCameraInstance


def _make_camera(decoders):
    """绕过 __init__（要连 native 库），只装配 stop_async 用到的属性。"""
    camera = object.__new__(MIoTCameraInstance)
    camera._did = "test-did"
    camera._enable_reconnect = True
    camera._reconnect_timer = None
    camera._main_loop = asyncio.get_running_loop()
    camera._lib_miot_camera = Mock()
    camera._lib_miot_camera.miot_camera_stop.return_value = 0
    camera._c_instance = Mock()
    camera._decoders = list(decoders)
    return camera


async def test_stop_async_clears_decoders_when_one_stop_raises():
    """任一路 decoder.stop() 抛异常都不能中断清理。

    解码线程是 append 进 _decoders 之后才 start 的，start 抛
    RuntimeError: can't start new thread 时列表里会留下一个从未启动的解码器，
    对它 join() 直接抛 RuntimeError: cannot join thread before it is started。
    若异常上抛，destroy_async 后面的 miot_camera_free 不执行，native 相机实例
    永久泄漏——而它在上一层已经被 pop 出 _camera_map，再没人能拿到。
    """
    raising_decoder = Mock()
    raising_decoder.stop.side_effect = RuntimeError(
        "cannot join thread before it is started"
    )
    ok_decoder = Mock()
    ok_decoder.stop.return_value = True
    camera = _make_camera([raising_decoder, ok_decoder])

    await camera.stop_async()

    assert camera._decoders == [], "抛异常路不得跳过 _decoders.clear()"
    assert ok_decoder.stop.called, "并发停止,一路抛错不影响其他路被停"


async def test_stop_async_logs_stuck_channel_index(caplog):
    """没停干净的通道号必须打进日志,只报数量定位不到是哪一路。"""
    stuck_decoder = Mock()
    stuck_decoder.stop.return_value = False
    ok_decoder = Mock()
    ok_decoder.stop.return_value = True
    camera = _make_camera([ok_decoder, stuck_decoder])

    with caplog.at_level(logging.ERROR, logger="miot.camera"):
        await camera.stop_async()

    error_logs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(error_logs) == 1, f"应只有一条聚合 error, 实际 {error_logs}"
    assert "1/2" in error_logs[0]
    assert "(1, False)" in error_logs[0], "必须报出卡住的是 channel 1"
