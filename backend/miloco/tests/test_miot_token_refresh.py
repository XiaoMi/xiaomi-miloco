"""token 刷新失败降级 + 空 discover 不误断开测试。

回归 2026-09-01 10:40 故障：本机/远端共享同一米家账号，远端 8/31 刷新成功后
refresh_token 被米家轮换，本机用旧 refresh_token 刷新 → 96009 invalid refresh
token → 旧代码把 _oauth_info 置 None → is_authenticated=False → 摄像头 discover
返回 {} → sync_devices 把已连接摄像头全部当"下线"断开 → 感知停摆。

修复后：
- 刷新失败保留 oauth_info（不停感知、按指数退避重试）；
- sync_devices 空 discover + 已连接设备时不误 disconnect。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from miot.error import MIoTOAuth2Error

from miloco.config import reset_settings
from miloco.miot.client import MiotProxy
from miloco.perception.collect.camera_adapter import (
    CameraDeviceAdapter,
    _CameraDeviceState,
)
from miloco.perception.types import PerceptionDevice


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """$MILOCO_HOME → tmp so get_settings() doesn't touch user data."""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    reset_settings()
    yield
    reset_settings()


def _kv_stub() -> SimpleNamespace:
    store: dict[str, str | None] = {}
    return SimpleNamespace(
        get=lambda key, default=None: store.get(key, default),
        set=lambda key, value: store.__setitem__(key, value) or True,
        delete=lambda key: store.pop(key, None),
    )


def _make_proxy(monkeypatch) -> tuple[MiotProxy, MagicMock]:
    """MiotProxy with stubbed collaborators; returns (proxy, client_stub)."""
    p = MiotProxy(uuid="u", redirect_uri="http://x", kv_repo=_kv_stub())
    client_stub = MagicMock()
    client_stub.refresh_access_token_async = AsyncMock()
    client_stub.init_async = AsyncMock()
    client_stub.deinit_async = AsyncMock()
    p._miot_client = client_stub  # 直接注入,绕过 _create_miot_client
    monkeypatch.setattr(p, "refresh_miot_info", AsyncMock(return_value={}))
    return p, client_stub


def _set_oauth(proxy: MiotProxy, expires_ts: int) -> None:
    proxy._oauth_info = SimpleNamespace(
        access_token="at",
        refresh_token="rt",
        expires_ts=expires_ts,
        model_dump_json=lambda: json.dumps({}),
    )


@pytest.mark.asyncio
async def test_refresh_failure_keeps_oauth_info(monkeypatch):
    """刷新失败(96009)后 oauth_info 必须保留——清空会导致 is_authenticated=False、
    discover 返回空、sync_devices 误断开全部摄像头(感知停摆的根因)。"""
    proxy, client_stub = _make_proxy(monkeypatch)
    _set_oauth(proxy, expires_ts=int(time.time()) + 100)
    client_stub.refresh_access_token_async.side_effect = MIoTOAuth2Error(
        'invalid http response, {"code":-6,"message":"{\\"error\\":96009,'
        '\\"error_description\\":\\"invalid refresh token\\"}"}'
    )

    result = await proxy.refresh_xiaomi_home_token_info()

    assert result is None
    assert proxy._oauth_info is not None, "刷新失败不得清空 oauth_info"
    assert proxy._oauth_info.refresh_token == "rt"
    assert "invalid refresh token" in proxy._last_refresh_error


@pytest.mark.asyncio
async def test_check_refresh_sets_backoff_and_identifies_96009(monkeypatch):
    """失败后按指数退避设置下次重试时间;96009 错误给出明确日志路径。"""
    proxy, client_stub = _make_proxy(monkeypatch)
    _set_oauth(proxy, expires_ts=int(time.time()) - 3600)  # 已过期 1 小时
    client_stub.refresh_access_token_async.side_effect = MIoTOAuth2Error(
        'invalid http response, {"code":-6,"message":"invalid refresh token"}'
    )

    await proxy._check_and_refresh_token()

    assert proxy._token_refresh_failures == 1
    assert proxy._token_refresh_next_retry_ts >= int(time.time()) + 299
    assert proxy._oauth_info is not None  # 失败不清空

    # 退避窗口内第二次检查不重复请求
    calls_before = client_stub.refresh_access_token_async.call_count
    await proxy._check_and_refresh_token()
    assert client_stub.refresh_access_token_async.call_count == calls_before


@pytest.mark.asyncio
async def test_check_refresh_healthy_token_resets_backoff(monkeypatch):
    """token 健康(>30min 到期)时不刷新、并重置失败退避状态。"""
    proxy, client_stub = _make_proxy(monkeypatch)
    _set_oauth(proxy, expires_ts=int(time.time()) + 7200)
    proxy._token_refresh_failures = 3
    proxy._token_refresh_next_retry_ts = int(time.time()) + 999

    await proxy._check_and_refresh_token()

    client_stub.refresh_access_token_async.assert_not_called()
    assert proxy._token_refresh_failures == 0
    assert proxy._token_refresh_next_retry_ts == 0


@pytest.mark.asyncio
async def test_sync_devices_empty_discover_keeps_connected(monkeypatch):
    """空 discover + 已有连接设备 → 跳过 disconnect(认证失效时不误停感知)。"""
    proxy = MagicMock()
    # 未认证 → discover 返回 {}
    proxy.is_authenticated = False
    adapter = CameraDeviceAdapter(miot_proxy=proxy)
    adapter._devices["cam1"] = _CameraDeviceState(did="cam1")

    monkeypatch.setattr(adapter, "connect_device", AsyncMock())
    monkeypatch.setattr(adapter, "disconnect_device", AsyncMock())

    await adapter.sync_devices()

    adapter.disconnect_device.assert_not_called()
    assert "cam1" in adapter._devices


def test_sync_devices_real_offline_still_disconnects(monkeypatch):
    """设备真下线(discover 非空但不含它)仍正常断开——不破坏 hot-plug 语义。"""
    from miloco.database.kv_repo import ScopeConfigKeys

    proxy = MagicMock()
    proxy.is_authenticated = True
    proxy.get_cameras = AsyncMock(return_value={})
    proxy.get_cached_camera = MagicMock(return_value=None)
    proxy._kv_repo.get = MagicMock(
        side_effect=lambda key, default=None: (
            '["home-1"]' if key == ScopeConfigKeys.HOME_WHITE_LIST_KEY else default
        )
    )
    adapter = CameraDeviceAdapter(miot_proxy=proxy)
    adapter._devices["cam1"] = _CameraDeviceState(did="cam1")

    async def _run():
        await adapter.sync_devices()
    asyncio.run(_run())

    assert "cam1" not in adapter._devices


def test_sync_devices_unknown_auth_state_keeps_connected(monkeypatch):
    """真实停机场景边界:discover 空且无已连接设备 → 无操作不报错。"""
    proxy = MagicMock()
    proxy.is_authenticated = False
    adapter = CameraDeviceAdapter(miot_proxy=proxy)
    monkeypatch.setattr(adapter, "disconnect_device", AsyncMock())
    asyncio.run(adapter.sync_devices())
    adapter.disconnect_device.assert_not_called()
