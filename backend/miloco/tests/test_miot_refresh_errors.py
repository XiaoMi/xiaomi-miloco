"""Regression tests for MiOT device refresh error propagation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.middleware.exceptions import MiotServiceException
from miloco.miot.client import MiotProxy
from miloco.miot.service import MiotService


def _kv_stub():
    return SimpleNamespace(
        db_connector=SimpleNamespace(
            execute_update=lambda *a, **kw: 0,
            execute_query=lambda *a, **kw: [],
        ),
        get=lambda key, default=None: default,
        set=lambda key, value: True,
        delete=lambda key: True,
    )


def _bare_proxy(
    *,
    devices: dict | None = None,
    refresh_error: Exception | None = None,
) -> MiotProxy:
    """Build only the MiotProxy state touched by refresh_devices/get_devices."""
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._kv_repo = _kv_stub()
    proxy._refresh_devices_lock = asyncio.Lock()
    proxy._device_info_dict = {}
    proxy._sync_meta_subscriptions = AsyncMock()
    proxy._sync_scene_subscriptions = AsyncMock()

    if refresh_error is not None:
        get_devices_async = AsyncMock(side_effect=refresh_error)
    else:
        get_devices_async = AsyncMock(return_value=devices or {})

    proxy._miot_client = SimpleNamespace(get_devices_async=get_devices_async)
    return proxy


@pytest.mark.asyncio
async def test_refresh_miot_devices_chains_underlying_refresh_error():
    underlying = TimeoutError("cloud read timed out")
    svc = MiotService(miot_proxy=_bare_proxy(refresh_error=underlying))

    with pytest.raises(MiotServiceException) as exc_info:
        await svc.refresh_miot_devices()

    exc = exc_info.value
    assert exc.__cause__ is underlying
    assert "cloud read timed out" in str(exc)
    assert "Failed to refresh MiOT devices: Failed to refresh MiOT devices" not in str(
        exc
    )


@pytest.mark.asyncio
async def test_get_miot_device_list_chains_underlying_refresh_error():
    underlying = TimeoutError("cloud read timed out")
    svc = MiotService(miot_proxy=_bare_proxy(refresh_error=underlying))

    with pytest.raises(MiotServiceException) as exc_info:
        await svc.get_miot_device_list()

    exc = exc_info.value
    assert exc.__cause__ is underlying
    assert "cloud read timed out" in str(exc)


@pytest.mark.asyncio
async def test_refresh_miot_devices_returns_true_when_devices_refreshed():
    svc = MiotService(
        miot_proxy=_bare_proxy(devices={"did-1": SimpleNamespace(did="did-1")})
    )

    assert await svc.refresh_miot_devices() is True


@pytest.mark.asyncio
async def test_refresh_miot_devices_empty_refresh_reports_no_devices():
    svc = MiotService(miot_proxy=_bare_proxy(devices={}))

    with pytest.raises(MiotServiceException) as exc_info:
        await svc.refresh_miot_devices()

    assert str(exc_info.value) == "No MiOT devices found after refresh"
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_get_device_spec_wraps_lazy_refresh_error():
    # A lazy get_devices() refresh that fails must surface as a
    # MiotServiceException carrying the cause, not a raw exception that
    # reaches the global error handler as a 500.
    underlying = TimeoutError("cloud read timed out")
    svc = MiotService(miot_proxy=_bare_proxy(refresh_error=underlying))

    with pytest.raises(MiotServiceException) as exc_info:
        await svc.get_device_spec("did-1")

    assert exc_info.value.__cause__ is underlying
    assert "cloud read timed out" in str(exc_info.value)


@pytest.mark.asyncio
async def test_refresh_miot_info_records_refresh_devices_error():
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._oauth_info = object()
    proxy.refresh_cameras = AsyncMock(return_value={})
    proxy.refresh_scenes = AsyncMock(return_value={})
    proxy.refresh_user_info = AsyncMock(return_value={})
    proxy.refresh_devices = AsyncMock(side_effect=TimeoutError("cloud read timed out"))

    result = await proxy.refresh_miot_info()

    assert result["devices"] is False
    assert "devices: cloud read timed out" in result["errors"]
