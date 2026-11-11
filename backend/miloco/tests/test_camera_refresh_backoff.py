# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""get_cameras 的空缓存重取节流。

背景：感知同步循环在没有活跃设备时 1 秒一轮、每轮调 get_cameras 两次。
如果「加载成功但账号下没有相机」和「还没加载过」用同一个判据，零相机账号
就会变成每秒两次云端拉取且永不停止。这里只验证节流语义：成功但为空 → 低频
重查；失败 → 短间隔重试；缓存非空 / 不可操作 → 不刷新。
"""

import asyncio
import time
import types

from miloco.miot import client as client_mod
from miloco.miot.client import MiotProxy


def _proxy(cameras=None, refresh_result=None, publishes=True, loaded=False):
    """``publishes`` 模拟 refresh_cameras 是否真的发布了缓存（代次 +1 且标记已加载）。"""
    calls = []

    async def refresh_cameras():
        calls.append(time.monotonic())
        if publishes:
            proxy._camera_load_gen += 1
            proxy._cameras_loaded = True
        return refresh_result

    proxy = types.SimpleNamespace(
        _camera_info_dict={} if cameras is None else cameras,
        _camera_retry_at=0.0,
        _camera_load_gen=0,
        _camera_get_lock=None,
        _cameras_loaded=loaded,
        is_operational=True,
        refresh_cameras=refresh_cameras,
    )
    return proxy, calls


async def _get(proxy):
    return await MiotProxy.get_cameras(proxy)


class TestGetCamerasThrottle:
    def test_empty_result_is_not_refetched_every_call(self):
        proxy, calls = _proxy(cameras={}, refresh_result={})

        asyncio.run(_get(proxy))
        asyncio.run(_get(proxy))
        asyncio.run(_get(proxy))

        assert len(calls) == 1
        assert proxy._cameras_loaded is True

    def test_loaded_but_empty_never_refetches(self):
        # 加载成功但账号下确实没有相机：不再有任何周期重拉，靠事件/手动刷新发现
        proxy, calls = _proxy(cameras={}, refresh_result={})
        asyncio.run(_get(proxy))
        assert len(calls) == 1

        proxy._camera_retry_at = 0.0  # 就算窗口早已过期也不该再拉
        asyncio.run(_get(proxy))
        asyncio.run(_get(proxy))
        assert len(calls) == 1

    def test_failure_uses_short_retry_window(self):
        proxy, calls = _proxy(cameras={}, refresh_result=None, publishes=False)

        asyncio.run(_get(proxy))
        assert len(calls) == 1
        assert (
            time.monotonic()
            < proxy._camera_retry_at
            < time.monotonic() + client_mod._CAMERA_FAILURE_RETRY_SEC + 1
        )
        asyncio.run(_get(proxy))
        assert len(calls) == 1

    def test_non_empty_cache_never_refreshes(self):
        proxy, calls = _proxy(cameras={"did": object()}, refresh_result={})
        asyncio.run(_get(proxy))
        asyncio.run(_get(proxy))
        assert calls == []

    def test_published_empty_but_returned_none_still_marks_loaded(self):
        # refresh_cameras 发布缓存后若在副作用里抛错会返回 None；以代次判成功，
        # 不能当成失败去每 10s 重试。
        proxy, calls = _proxy(cameras={}, refresh_result=None, publishes=True)
        asyncio.run(_get(proxy))
        asyncio.run(_get(proxy))
        assert len(calls) == 1

    def test_not_operational_never_refreshes(self):
        proxy, calls = _proxy(cameras={}, refresh_result={})
        proxy.is_operational = False
        asyncio.run(_get(proxy))
        assert calls == []


class TestResetCloudBackoff:
    """用户主动刷新要能同时打破相机节流与网络层退避。"""

    def _proxy(self, clear):
        return types.SimpleNamespace(
            _camera_retry_at=time.monotonic() + 300,
            _miot_client=types.SimpleNamespace(clear_backoff=clear),
        )

    def test_clears_camera_throttle(self):
        cleared = []
        proxy = self._proxy(lambda: cleared.append(1) or 1)
        MiotProxy.reset_cloud_backoff(proxy)
        assert proxy._camera_retry_at == 0.0
        assert cleared == [1]

    def test_client_error_does_not_break_refresh(self):
        def boom():
            raise RuntimeError("boom")

        proxy = self._proxy(boom)
        MiotProxy.reset_cloud_backoff(proxy)  # 不抛
        assert proxy._camera_retry_at == 0.0


class TestSingleFlight:
    def test_concurrent_get_cameras_refreshes_once(self):
        calls = {"n": 0}
        gate = asyncio.Event()

        proxy = types.SimpleNamespace(
            _camera_info_dict={},
            _camera_retry_at=0.0,
            _camera_load_gen=0,
            _camera_get_lock=None,
            _cameras_loaded=False,
            is_operational=True,
        )

        async def refresh_cameras():
            calls["n"] += 1
            if calls["n"] == 1:
                await gate.wait()  # 卡住第一个，让第二个排队
            proxy._camera_load_gen += 1
            proxy._cameras_loaded = True
            return {}

        proxy.refresh_cameras = refresh_cameras

        async def scenario():
            first = asyncio.create_task(MiotProxy.get_cameras(proxy))
            await asyncio.sleep(0)
            second = asyncio.create_task(MiotProxy.get_cameras(proxy))
            await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(first, second)

        asyncio.run(scenario())
        assert calls["n"] == 1, "并发调用只应拉一次"


class TestUnconfirmedEmpty:
    """非空→空的一次刷新不认作「确实没有相机」：留复核窗口，不动 manager。"""

    def test_first_empty_keeps_window_and_skips_teardown(self):
        async def get_cameras_async():
            return {}

        proxy = types.SimpleNamespace(
            _refresh_cameras_lock=asyncio.Lock(),
            _camera_info_dict={"cam1": object()},  # 此前有相机
            _camera_load_gen=0,
            _cameras_loaded=True,
            _camera_retry_at=0.0,
            _miot_client=types.SimpleNamespace(get_cameras_async=get_cameras_async),
            _spawn_subscription_sync=lambda: None,
        )
        asyncio.run(MiotProxy.refresh_cameras(proxy))

        assert proxy._camera_info_dict == {}
        assert proxy._cameras_loaded is False, "未确认的返空不能标成已加载"
        assert proxy._camera_retry_at > time.monotonic() + 5, "复核要排短窗"
