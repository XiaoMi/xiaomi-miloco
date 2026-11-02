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


def _proxy(cameras=None, refresh_result=None, publishes=True):
    """``publishes`` 模拟 refresh_cameras 是否真的发布了缓存（代次 +1）。"""
    calls = []

    async def refresh_cameras():
        calls.append(time.monotonic())
        if publishes:
            proxy._camera_load_gen += 1
        return refresh_result

    proxy = types.SimpleNamespace(
        _camera_info_dict={} if cameras is None else cameras,
        _camera_retry_at=0.0,
        _camera_load_gen=0,
        _camera_get_lock=None,
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
        assert proxy._camera_retry_at > time.monotonic() + 50

    def test_empty_result_refetches_after_window(self):
        proxy, calls = _proxy(cameras={}, refresh_result={})
        asyncio.run(_get(proxy))
        assert len(calls) == 1

        # 把窗口拨到过期，模拟 60s 之后
        proxy._camera_retry_at = time.monotonic()
        asyncio.run(_get(proxy))
        assert len(calls) == 2

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

    def test_published_empty_but_returned_none_still_uses_long_window(self):
        # refresh_cameras 发布缓存后若在副作用里抛错会返回 None；以代次判成功。
        proxy, calls = _proxy(cameras={}, refresh_result=None, publishes=True)
        asyncio.run(_get(proxy))
        assert len(calls) == 1
        assert proxy._camera_retry_at > time.monotonic() + 200

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
