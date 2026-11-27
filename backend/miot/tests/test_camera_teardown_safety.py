# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""相机原生拆除的安全性。

原生 miot_camera_start 是不可取消的阻塞调用；在它还在飞的时候做
miot_camera_free / miot_camera_deinit 会 use-after-free，或与 libmiss 的
rwlock 互锁。这里钉住三条：
- start 在飞时 destroy 不 free，并标记实例泄漏
- deinit 永不调用原生 miot_camera_deinit（库按进程级单例保留）
- __del__ 也不调原生拆除
"""

import asyncio
import types
from unittest.mock import MagicMock

import pytest
from miot.camera import MIoTCamera, MIoTCameraInstance

pytestmark = pytest.mark.unit


class TestDestroySafety:
    def test_destroy_skips_free_when_start_inflight(self):
        async def scenario():
            inst = MIoTCameraInstance.__new__(MIoTCameraInstance)
            inst._did = "cam1"
            inst._manager = types.SimpleNamespace(
                native_start_leaked=False, _leaked_instances=[]
            )
            inst._lib_miot_camera = MagicMock()
            inst._c_instance = MagicMock()
            inst._callbacks = {"r0": {"1": object()}}
            inst._callback_refs = {"r0": object()}
            inst._decoders = []
            inst._reconnect_timer = None
            inst._enable_reconnect = False
            inst._start_tasks = set()
            inst._start_inflight = 1  # 计数形态：重连定时器可叠加多条 start
            inst._main_loop = asyncio.get_running_loop()

            await inst.destroy_async()
            return inst

        inst = asyncio.run(scenario())
        inst._lib_miot_camera.miot_camera_free.assert_not_called()
        assert inst._manager.native_start_leaked is True
        # 原生仍持有 trampoline：实例必须被强引用住，业务回调摘掉。
        import miot.camera as camera_module

        assert inst in camera_module._native_lib.leaked_instances
        assert inst in inst._manager._leaked_instances
        assert inst._callbacks == {}
        assert inst._callback_refs

    def test_settle_waits_all_inflight_start_tasks(self):
        """重叠 start：settle 必须等全部 task，而不只是最新那一条。"""

        async def scenario():
            inst = MIoTCameraInstance.__new__(MIoTCameraInstance)
            inst._did = "cam1"
            inst._start_inflight = 0
            loop = asyncio.get_running_loop()

            async def slow():
                await asyncio.sleep(0.05)

            tasks = {loop.create_task(slow()), loop.create_task(slow())}
            inst._start_tasks = set(tasks)

            import miot.camera as camera_module

            old_timeout = camera_module._START_SETTLE_TIMEOUT_S
            camera_module._START_SETTLE_TIMEOUT_S = 1.0
            try:
                await inst._MIoTCameraInstance__settle_start_async()
            finally:
                camera_module._START_SETTLE_TIMEOUT_S = old_timeout
            return tasks

        tasks = asyncio.run(scenario())
        assert all(task.done() for task in tasks)


class TestDeinitSafety:
    def test_deinit_never_calls_native(self):
        cam = MIoTCamera.__new__(MIoTCamera)
        cam._init_done = True
        cam.native_start_leaked = False
        cam._camera_map = {}
        cam._lib_miot_camera = MagicMock()

        asyncio.run(cam.deinit_async())

        cam._lib_miot_camera.miot_camera_deinit.assert_not_called()
        # 保持 initialized：后续客户端复用这把库，只更新 token
        assert cam._init_done is True

    def test_del_never_calls_native(self):
        cam = MIoTCamera.__new__(MIoTCamera)
        cam._init_done = True
        cam.native_start_leaked = False
        lib = MagicMock()
        cam._lib_miot_camera = lib

        cam.__del__()

        lib.miot_camera_deinit.assert_not_called()
