# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unit tests for init/deinit idempotency fixes.

These tests cover the failure paths introduced by commit 0c6f190:
- MIoTCamera.deinit_async: C call failure must still clear _init_done.
- MIoTCamera.__del__: partial __init__ failure must not AttributeError.
- MIoTClient.deinit_async: one sub-client failure must not strand state.
- MIoTClient.init_async: partial init failure must trigger full cleanup.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_LOGGER = logging.getLogger(__name__)

# 无真实网络/凭据/设备依赖 —— CI 的 `miot 单元测试` 步骤按 `-m unit` 选择。
pytestmark = pytest.mark.unit


# ─── MIoTCamera ──────────────────────────────────────────────────────────────


@pytest.fixture
def fake_camera_lib():
    """A CDLL stand-in whose C functions are no-ops but can be told to raise."""
    lib = MagicMock()
    lib.miot_camera_version.return_value = b"test-1.0"
    return lib


async def test_camera_deinit_c_failure_clears_init_done(fake_camera_lib):
    """miot_camera_deinit raising must not leave _init_done=True — otherwise
    __del__ would later call miot_camera_deinit a second time (double-free)."""
    from miot.camera import MIoTCamera

    with patch("miot.camera._load_dynamic_lib", return_value=fake_camera_lib):
        cam = MIoTCamera(cloud_server="cn", access_token="t", loop=asyncio.get_running_loop())
        await cam.init_async()
        assert cam._init_done is True

        fake_camera_lib.miot_camera_deinit.side_effect = RuntimeError("C crash")
        # Should not raise; the warning is logged internally.
        await cam.deinit_async()
        assert cam._init_done is False

        # Second deinit is a no-op.
        fake_camera_lib.miot_camera_deinit.reset_mock()
        await cam.deinit_async()
        fake_camera_lib.miot_camera_deinit.assert_not_called()

        # Reinit on the same instance should succeed.
        fake_camera_lib.miot_camera_deinit.side_effect = None
        await cam.init_async()
        assert cam._init_done is True
        await cam.deinit_async()


async def test_camera_init_c_failure_rolls_back_log_handler(fake_camera_lib):
    """miot_camera_init raising must clear the log handler from the C library
    so it does not retain a pointer into a ctypes callback that may be freed
    once this instance is GC'd."""
    from miot.camera import MIoTCamera

    with patch("miot.camera._load_dynamic_lib", return_value=fake_camera_lib):
        cam = MIoTCamera(cloud_server="cn", access_token="t", loop=asyncio.get_running_loop())

        fake_camera_lib.miot_camera_init.side_effect = RuntimeError("C init fail")

        with pytest.raises(RuntimeError, match="C init fail"):
            await cam.init_async()

        assert cam._init_done is False
        # Log handler set with non-None first, then rolled back to None.
        calls = fake_camera_lib.miot_camera_set_log_handler.call_args_list
        assert len(calls) == 2
        assert calls[0].args[0] is cam._log_handler
        assert calls[1].args[0] is None

        # Recovery: clear the failure and reinit on the same instance.
        fake_camera_lib.miot_camera_init.side_effect = None
        await cam.init_async()
        assert cam._init_done is True
        await cam.deinit_async()


async def test_camera_deinit_destroy_failure_still_clears_map(fake_camera_lib):
    """A failure inside destroy_camera_async must not stop _camera_map.clear()
    nor leave _init_done=True."""
    from miot.camera import MIoTCamera

    with patch("miot.camera._load_dynamic_lib", return_value=fake_camera_lib):
        cam = MIoTCamera(cloud_server="cn", access_token="t", loop=asyncio.get_running_loop())
        await cam.init_async()

        broken_instance = MagicMock()
        broken_instance.destroy_async = AsyncMock(side_effect=RuntimeError("destroy fail"))
        cam._camera_map["did-broken"] = broken_instance

        await cam.deinit_async()
        assert cam._init_done is False
        assert cam._camera_map == {}


async def test_camera_del_survives_partial_init():
    """If _load_dynamic_lib raises, __del__ must not AttributeError when GC
    reclaims the half-built MIoTCamera."""
    from miot.camera import MIoTCamera

    with patch("miot.camera._load_dynamic_lib", side_effect=RuntimeError("lib missing")):
        with pytest.raises(RuntimeError, match="lib missing"):
            MIoTCamera(cloud_server="cn", access_token="t", loop=asyncio.get_running_loop())
        # Implicit: object is dropped; __del__ must be silent.
        # If it raised, pytest would show it in stderr and mark the test unclean.


# ─── MIoTClient ──────────────────────────────────────────────────────────────


def _make_client_with_fake_subclients():
    """Build a MIoTClient in the "init succeeded" shape with AsyncMock sub-clients."""
    from miot.client import MIoTClient

    client = MIoTClient(uuid="u", redirect_uri="http://x", loop=asyncio.get_running_loop())
    client._i18n = AsyncMock()
    client._storage = MagicMock()
    client._spec_parser = AsyncMock()
    client._oauth_client = AsyncMock()
    client._http_client = AsyncMock()
    client._network_client = AsyncMock()
    client._lan_client = AsyncMock()
    client._camera_client = AsyncMock()
    client._init_done = True
    return client


async def test_client_deinit_one_subclient_failure_does_not_stop_chain():
    """When camera_client.deinit_async raises, lan/network/spec/i18n must
    still be called and state must be fully cleared."""
    client = _make_client_with_fake_subclients()

    client._camera_client.deinit_async.side_effect = RuntimeError("camera boom")

    # Keep refs so we can assert after deinit nulls them out.
    lan = client._lan_client
    network = client._network_client
    spec = client._spec_parser
    i18n = client._i18n
    oauth = client._oauth_client
    http = client._http_client

    await client.deinit_async()

    oauth.deinit_async.assert_awaited_once()
    http.deinit_async.assert_awaited_once()
    client._camera_client  # noqa: B018  (just here for readability; real check below)
    lan.unregister_status_changed_async.assert_awaited_once_with("miot_client")
    lan.deinit_async.assert_awaited_once()
    network.deinit_async.assert_awaited_once()
    spec.deinit_async.assert_awaited_once()
    i18n.deinit_async.assert_awaited_once()

    assert client._init_done is False
    assert client._oauth_client is None
    assert client._http_client is None
    assert client._camera_client is None
    assert client._lan_client is None
    assert client._network_client is None
    assert client._spec_parser is None
    assert client._i18n is None


async def test_client_deinit_multiple_failures_all_cleared():
    """Every sub-client raising must not prevent state reset."""
    client = _make_client_with_fake_subclients()

    for sub in (
        client._oauth_client,
        client._http_client,
        client._camera_client,
        client._lan_client,
        client._network_client,
        client._spec_parser,
        client._i18n,
    ):
        sub.deinit_async.side_effect = RuntimeError("boom")
    client._lan_client.unregister_status_changed_async.side_effect = RuntimeError("boom")

    await client.deinit_async()  # must not raise

    assert client._init_done is False
    assert client._oauth_client is None
    assert client._camera_client is None
    assert client._lan_client is None
    assert client._network_client is None


async def test_client_deinit_is_idempotent():
    """Calling deinit_async twice on a freshly-constructed client must not
    AttributeError (sub-client slots are pre-declared as None)."""
    from miot.client import MIoTClient

    client = MIoTClient(uuid="u", redirect_uri="http://x", loop=asyncio.get_running_loop())
    # _init_done is False; deinit should early-return.
    await client.deinit_async()
    await client.deinit_async()  # still fine


async def test_client_init_camera_failure_triggers_full_cleanup():
    """If MIoTCamera.init_async raises at the tail of init, the already-built
    network/lan/i18n/spec/oauth/http must all be deinit'd."""
    from miot.client import MIoTClient

    with (
        patch("miot.client.MIoTI18n") as I18nCls,
        patch("miot.client.MIoTStorage"),
        patch("miot.client.MIoTSpecParser") as SpecCls,
        patch("miot.client.MIoTOAuth2Client") as OauthCls,
        patch("miot.client.MIoTHttpClient") as HttpCls,
        patch("miot.client.MIoTNetwork") as NetCls,
        patch("miot.client.MIoTLan") as LanCls,
        patch("miot.client.MIoTCamera") as CameraCls,
    ):
        # Wire up just enough for init_async to walk through.
        I18nCls.return_value.init_async = AsyncMock()
        I18nCls.return_value.deinit_async = AsyncMock()
        SpecCls.return_value.init_async = AsyncMock()
        SpecCls.return_value.deinit_async = AsyncMock()
        NetCls.return_value.init_async = AsyncMock()
        NetCls.return_value.get_info_async = AsyncMock(return_value={})
        NetCls.return_value.deinit_async = AsyncMock()
        LanCls.return_value.init_async = AsyncMock()
        LanCls.return_value.register_status_changed_async = AsyncMock()
        LanCls.return_value.unregister_status_changed_async = AsyncMock()
        LanCls.return_value.deinit_async = AsyncMock()
        OauthCls.return_value.deinit_async = AsyncMock()
        HttpCls.return_value.deinit_async = AsyncMock()
        CameraCls.return_value.init_async = AsyncMock(side_effect=RuntimeError("camera init fail"))
        CameraCls.return_value.deinit_async = AsyncMock()

        client = MIoTClient(
            uuid="u",
            redirect_uri="http://x",
            cache_path="/tmp/miot_cache",  # enable storage + spec path
            loop=asyncio.get_running_loop(),
        )

        with pytest.raises(RuntimeError, match="camera init fail"):
            await client.init_async()

        # Everything built up to the failure point must be deinit'd.
        I18nCls.return_value.deinit_async.assert_awaited_once()
        SpecCls.return_value.deinit_async.assert_awaited_once()
        NetCls.return_value.deinit_async.assert_awaited_once()
        LanCls.return_value.deinit_async.assert_awaited_once()
        OauthCls.return_value.deinit_async.assert_awaited_once()
        HttpCls.return_value.deinit_async.assert_awaited_once()
        # camera_client.deinit_async: called because we assigned before init_async threw.
        CameraCls.return_value.deinit_async.assert_awaited_once()

        assert client._init_done is False
        assert client._camera_client is None
        assert client._lan_client is None
        assert client._network_client is None
        assert client._i18n is None


async def test_client_init_early_i18n_failure_does_not_attribute_error():
    """If i18n.init_async raises before any other sub-client is created,
    deinit_async must walk the None slots without AttributeError."""
    from miot.client import MIoTClient

    with (
        patch("miot.client.MIoTI18n") as I18nCls,
        patch("miot.client.MIoTStorage"),
        patch("miot.client.MIoTSpecParser") as SpecCls,
    ):
        # i18n.init_async raises; spec_parser was already built before it.
        I18nCls.return_value.init_async = AsyncMock(side_effect=RuntimeError("i18n fail"))
        I18nCls.return_value.deinit_async = AsyncMock()
        SpecCls.return_value.init_async = AsyncMock()
        SpecCls.return_value.deinit_async = AsyncMock()

        client = MIoTClient(
            uuid="u",
            redirect_uri="http://x",
            cache_path="/tmp/miot_cache",
            loop=asyncio.get_running_loop(),
        )
        with pytest.raises(RuntimeError, match="i18n fail"):
            await client.init_async()

        # network/lan/camera were never created — their slots stay None and
        # deinit_async must not have AttributeError'd walking over them.
        assert client._network_client is None
        assert client._lan_client is None
        assert client._camera_client is None
        assert client._init_done is False

        # spec_parser was built: its deinit_async must have been called.
        SpecCls.return_value.deinit_async.assert_awaited_once()


async def test_client_init_deinit_init_cycle():
    """Basic happy-path idempotency: a successful init→deinit→init→deinit
    cycle must leave the instance ready for reuse."""
    from miot.client import MIoTClient

    def build_subclient_mock(with_registers=False):
        m = AsyncMock()
        m.init_async = AsyncMock()
        m.deinit_async = AsyncMock()
        if with_registers:
            m.register_status_changed_async = AsyncMock()
            m.unregister_status_changed_async = AsyncMock()
        return m

    with (
        patch("miot.client.MIoTI18n") as I18nCls,
        patch("miot.client.MIoTStorage"),
        patch("miot.client.MIoTSpecParser") as SpecCls,
        patch("miot.client.MIoTOAuth2Client") as OauthCls,
        patch("miot.client.MIoTHttpClient") as HttpCls,
        patch("miot.client.MIoTNetwork") as NetCls,
        patch("miot.client.MIoTLan") as LanCls,
        patch("miot.client.MIoTCamera") as CameraCls,
    ):
        for cls in (I18nCls, SpecCls, OauthCls, HttpCls):
            cls.return_value = build_subclient_mock()
        NetCls.return_value = build_subclient_mock()
        NetCls.return_value.get_info_async = AsyncMock(return_value={})
        LanCls.return_value = build_subclient_mock(with_registers=True)
        CameraCls.return_value = build_subclient_mock()

        client = MIoTClient(
            uuid="u",
            redirect_uri="http://x",
            cache_path="/tmp/miot_cache",
            loop=asyncio.get_running_loop(),
        )
        await client.init_async()
        assert client._init_done is True
        await client.deinit_async()
        assert client._init_done is False

        await client.init_async()
        assert client._init_done is True
        await client.deinit_async()
        assert client._init_done is False
