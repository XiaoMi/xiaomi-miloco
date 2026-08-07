"""Regression tests for MiOT device refresh error propagation."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.middleware.exceptions import MiotServiceException
from miloco.miot.client import MiotProxy
from miloco.miot.service import MiotService


def _kv_stub(values: dict[str, str] | None = None):
    store = dict(values or {})

    return SimpleNamespace(
        db_connector=SimpleNamespace(
            execute_update=lambda *a, **kw: 0,
            execute_query=lambda *a, **kw: [],
        ),
        get=lambda key, default=None: store.get(key, default),
        set=lambda key, value: store.__setitem__(key, value) or True,
        delete=lambda key: store.pop(key, None) is not None,
    )


def _bare_proxy(
    *,
    devices: dict | None = None,
    refresh_error: Exception | None = None,
    kv_values: dict[str, str] | None = None,
) -> MiotProxy:
    """Build only the MiotProxy state touched by refresh_devices/get_devices."""
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._kv_repo = _kv_stub(kv_values)
    proxy._refresh_devices_lock = asyncio.Lock()
    proxy._device_info_dict = {}
    proxy._sync_meta_subscriptions = AsyncMock()
    proxy._sync_scene_subscriptions = AsyncMock()
    proxy.get_cameras = AsyncMock(return_value={})

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
async def test_assert_did_in_allowed_home_wraps_lazy_refresh_error():
    underlying = TimeoutError("cloud read timed out")
    svc = MiotService(
        miot_proxy=_bare_proxy(
            refresh_error=underlying,
            kv_values={ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])},
        )
    )

    with pytest.raises(MiotServiceException) as exc_info:
        await svc._assert_did_in_allowed_home("did-1")

    assert exc_info.value.__cause__ is underlying
    assert "cloud read timed out" in str(exc_info.value)


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
async def test_get_home_info_refresh_returns_partial_data_when_devices_fail():
    proxy = SimpleNamespace(
        _kv_repo=_kv_stub({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])}),
        _device_info_dict={},
        refresh_devices=AsyncMock(side_effect=TimeoutError("cloud read timed out")),
        refresh_scenes=AsyncMock(
            return_value={"scene-1": SimpleNamespace(scene_id="scene-1", home_id="H1")}
        ),
        refresh_cameras=AsyncMock(
            return_value={
                "camera-1": SimpleNamespace(home_id="H1", home_name="Main Home")
            }
        ),
        get_home_info_data=AsyncMock(
            return_value={
                "home_name": "Main Home",
                "home_id_to_name": {"H1": "Main Home"},
                "devices": [],
                "areas": [],
                "scenes": [{"scene_id": "scene-1", "scene_name": "Scene 1"}],
                "persons": [],
            }
        ),
        get_all_scenes=AsyncMock(
            return_value={"scene-1": SimpleNamespace(scene_id="scene-1", home_id="H1")}
        ),
        get_devices=AsyncMock(side_effect=AssertionError("should use cached devices")),
    )
    svc = MiotService(miot_proxy=proxy)

    result = await svc.get_home_info(refresh=True)

    assert result["home_name"] == "Main Home"
    assert result["devices"] == []
    assert result["scenes"] == [{"scene_id": "scene-1", "scene_name": "Scene 1"}]
    assert "home_id_to_name" not in result
    proxy.refresh_devices.assert_awaited_once()
    proxy.refresh_scenes.assert_awaited_once()
    proxy.refresh_cameras.assert_awaited_once()
    proxy.get_devices.assert_not_awaited()


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
