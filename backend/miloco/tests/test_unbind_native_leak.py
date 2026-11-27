# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""解绑收尾：放弃卡住的原生相机实例后必须重启进程回收。

相机 start 未返回时不能 free 原生实例（use-after-free），只能泄漏；泄漏的
实例留在 miss 全局表里，本进程内无法回收，所以解绑成功后要让进程退出一次，
由 launchd/supervisord 以非 0 退出码重拉。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.miot import service as svc
from miloco.miot.service import MiotService


def _service_with_proxy(proxy) -> MiotService:
    svc_obj = MiotService.__new__(MiotService)
    svc_obj._miot_proxy = proxy
    svc_obj._stop_perception_for_teardown = AsyncMock()
    svc_obj._clear_account_scope_state = MagicMock()
    svc_obj._sync_camera_adapter = AsyncMock()

    async def _reset_state_scope(rebuild, realign=False):
        await rebuild()

    svc_obj._reset_state_scope = _reset_state_scope
    return svc_obj


def _proxy(leaked: bool):
    return SimpleNamespace(
        camera_native_leaked=leaked,
        reset_central_identity_async=AsyncMock(),
        deinit=AsyncMock(),
        init=AsyncMock(),
        refresh_cameras=AsyncMock(),
    )


class TestUnbindNativeLeak:
    def test_restarts_when_native_instance_leaked(self, monkeypatch):
        scheduled: list[str] = []
        monkeypatch.setattr(svc, "_schedule_backend_restart", scheduled.append)

        svc_obj = _service_with_proxy(_proxy(leaked=True))
        asyncio.run(svc_obj.unbind_miot())

        assert scheduled, "原生实例泄漏时必须重启进程回收"

    def test_restarts_when_leak_and_init_raises(self, monkeypatch):
        """泄漏已检测到，后续 init 抛异常也必须重启；否则重试时标记已被覆盖。"""
        from miloco.middleware.exceptions import MiotServiceException

        scheduled: list[str] = []
        monkeypatch.setattr(svc, "_schedule_backend_restart", scheduled.append)
        proxy = _proxy(leaked=True)
        proxy.init = AsyncMock(side_effect=RuntimeError("init boom"))

        with pytest.raises(MiotServiceException):
            asyncio.run(_service_with_proxy(proxy).unbind_miot())

        assert scheduled, "init 失败也必须重启回收泄漏实例"

    def test_no_restart_when_clean(self, monkeypatch):
        scheduled: list[str] = []
        monkeypatch.setattr(svc, "_schedule_backend_restart", scheduled.append)

        svc_obj = _service_with_proxy(_proxy(leaked=False))
        asyncio.run(svc_obj.unbind_miot())

        assert scheduled == []


def test_schedule_backend_restart_exits_nonzero(monkeypatch):
    """非 0 退出码才会触发 launchd KeepAlive{SuccessfulExit:false}/supervisord。"""
    codes: list[int] = []
    monkeypatch.setattr(svc.os, "_exit", codes.append)

    async def run():
        svc._schedule_backend_restart("test", delay=0)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert codes == [1]
