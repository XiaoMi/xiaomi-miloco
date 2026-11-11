# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""网络层失败退避的单测。

退避放在 cloud.py 的发包口，是为了让「缓存为空就再拉」这类上层逻辑
绕不过去——所以这里只验证退避器本身的语义，不涉及具体接口。
"""

import asyncio
import json
import time
import types

import pytest
from miot.backoff import (
    CAP_SEC,
    FORCE_MAX_PER_WINDOW,
    SERVER_WAIT_CAP_SEC,
    EndpointBackoff,
)
from miot.client import MIoTClient
from miot.cloud import MIoTHttpClient
from miot.error import MIoTErrorCode, MIoTHttpError

# CI 按 ``-m unit`` 选纯 mock 用例；不标就白写（见 conftest.pytest_configure）。
pytestmark = pytest.mark.unit


class TestEndpointBackoff:
    def test_success_resets(self):
        backoff = EndpointBackoff()
        backoff.on_failure()
        backoff.on_failure()
        backoff.on_success()
        assert backoff.failures == 0
        assert backoff.remaining() == 0

    def test_check_raises_inside_window(self):
        backoff = EndpointBackoff()
        backoff.on_failure(retry_after=30)
        with pytest.raises(MIoTHttpError) as exc:
            backoff.check()
        assert exc.value.code is MIoTErrorCode.CODE_HTTP_BACKOFF
        assert 25 < backoff.remaining() <= 30

    def test_check_passes_after_window(self):
        backoff = EndpointBackoff(base_sec=0.01)
        backoff.on_failure()
        time.sleep(0.02)
        backoff.check()  # 不抛
        assert backoff.remaining() == 0

    def test_exponential_growth_and_cap(self):
        backoff = EndpointBackoff(base_sec=1.0)
        waits = [backoff.on_failure() for _ in range(10)]
        assert waits[:4] == [1.0, 2.0, 4.0, 8.0]
        assert max(waits) == CAP_SEC

    def test_server_wait_second_wins_over_backoff(self):
        backoff = EndpointBackoff()
        # 首次失败本来只退避 1s，但云端要 30s
        assert backoff.on_failure(retry_after=30) == 30
        assert backoff.remaining() > 25

    def test_server_wait_second_is_capped(self):
        backoff = EndpointBackoff()
        assert backoff.on_failure(retry_after=10_000) == SERVER_WAIT_CAP_SEC

    def test_failures_are_per_instance(self):
        a, b = EndpointBackoff(), EndpointBackoff()
        a.on_failure(retry_after=30)
        assert a.remaining() > 0
        assert b.remaining() == 0


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self, encoding: str | None = None) -> str:
        return self._body


class _FakeSession:
    """只数调用次数：退避窗口内不该再走到这里。"""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def post(self, **kwargs) -> _FakeResponse:
        self.calls += 1
        return self.responses.pop(0)


class TestHttpClientBackoffWiring:
    """验证明文 429 与业务码 -127 都能让同一接口停下来，且对调用方透明。"""

    def _client(self, session: _FakeSession) -> MIoTHttpClient:
        # 必须在事件循环内构造：__init__ 里会取 running loop。
        client = MIoTHttpClient(cloud_server="cn", access_token="token")
        client._ensure_session = lambda: session  # type: ignore[method-assign]
        client.aes_encrypt_with_b64 = lambda data: "x"  # type: ignore[method-assign]
        return client

    def test_429_stops_until_window_passes(self):
        session = _FakeSession(
            [_FakeResponse(429, json.dumps({"code": -127, "wait_second": 30}))]
        )

        async def scenario():
            client = self._client(session)
            post = client._MIoTHttpClient__mihome_api_post_async
            with pytest.raises(MIoTHttpError) as first:
                await post("/app/v2/home/device_list_page", {})
            assert first.value.code is MIoTErrorCode.CODE_HTTP_RATE_LIMITED

            # 窗口内第二次调用：直接拒绝，一个包都不发
            with pytest.raises(MIoTHttpError) as second:
                await post("/app/v2/home/device_list_page", {})
            assert second.value.code is MIoTErrorCode.CODE_HTTP_BACKOFF
            assert session.calls == 1

        asyncio.run(scenario())

    def test_success_resets_backoff(self):
        session = _FakeSession(
            [
                _FakeResponse(500, "boom"),
                _FakeResponse(200, "encrypted"),
            ]
        )
        async def scenario():
            client = self._client(session)
            client.aes_decrypt_with_b64 = lambda s: {"code": 0}  # type: ignore[method-assign]
            post = client._MIoTHttpClient__mihome_api_post_async
            with pytest.raises(MIoTHttpError):
                await post("/app/v2/homeroom/gethome", {})
            backoff = client._backoff_for("/app/v2/homeroom/gethome")
            assert backoff.failures == 1

            # 拨到窗口外再发一次：成功即清零
            backoff._retry_at = time.monotonic()
            assert await post("/app/v2/homeroom/gethome", {}) == {"code": 0}
            assert backoff.failures == 0

        asyncio.run(scenario())

    def test_business_rate_limit_code_also_backs_off(self):
        session = _FakeSession([_FakeResponse(200, "encrypted")])

        async def scenario():
            client = self._client(session)
            client.aes_decrypt_with_b64 = lambda s: {  # type: ignore[method-assign]
                "code": -127,
                "wait_second": 20,
            }
            post = client._MIoTHttpClient__mihome_api_post_async
            with pytest.raises(MIoTHttpError):
                await post("/app/v2/homeroom/gethome", {})
            backoff = client._backoff_for("/app/v2/homeroom/gethome")
            assert backoff.remaining() > 15

        asyncio.run(scenario())


class TestForceChannel:
    """用户主动刷新可以打破退避，但强刷本身有额度。"""

    def test_force_clears_window(self):
        backoff = EndpointBackoff()
        backoff.on_failure(retry_after=300)
        assert backoff.remaining() > 0

        assert backoff.force() is True
        assert backoff.remaining() == 0
        assert backoff.failures == 0

    def test_force_quota_is_enforced(self):
        backoff = EndpointBackoff()
        for _ in range(FORCE_MAX_PER_WINDOW):
            backoff.on_failure(retry_after=300)
            assert backoff.force() is True

        # 额度用尽后再失败：强刷不再生效，窗口保留
        backoff.on_failure(retry_after=300)
        assert backoff.force() is False
        assert backoff.remaining() > 0

    def test_force_without_window_does_not_consume_quota(self):
        backoff = EndpointBackoff()
        # 正常状态下点刷新不该吃掉额度，否则真出限流时反而刷不动
        for _ in range(FORCE_MAX_PER_WINDOW * 2):
            assert backoff.force() is True
        backoff.on_failure(retry_after=300)
        assert backoff.force() is True
        assert backoff.remaining() == 0

    def test_concurrent_success_does_not_clear_failure_window(self):
        backoff = EndpointBackoff()
        token = backoff.begin()          # 请求 A 出发
        backoff.on_failure(retry_after=30)  # 请求 B 并发失败，建立窗口
        backoff.on_success(token)        # A 成功返回
        assert backoff.remaining() > 0   # 窗口不能被抹掉

    def test_clear_backoff_only_touches_read_paths(self):
        async def scenario():
            client = MIoTHttpClient(cloud_server="cn", access_token="token")
            read = client._backoff_for("/app/v2/home/device_list_page")
            control = client._backoff_for("/app/v2/miotspec/prop/set")
            read.on_failure(retry_after=300)
            control.on_failure(retry_after=300)

            assert client.clear_backoff() == 1  # 只清只读口
            assert read.remaining() == 0
            assert control.remaining() > 0

        asyncio.run(scenario())

    def test_clear_backoff_counts_only_real_windows(self):
        async def scenario():
            client = MIoTHttpClient(cloud_server="cn", access_token="token")
            gethome = client._backoff_for("/app/v2/homeroom/gethome")
            client._backoff_for("/app/v2/home/device_list_page")  # 没窗口
            gethome.on_failure(retry_after=300)
            assert client.clear_backoff() == 1

        asyncio.run(scenario())


class TestMiotClientClearBackoff:
    """MiotProxy 持有的是 MIoTClient，不是 HTTP 客户端——这层转发必须存在。

    部署验证时踩过：MiotProxy 直接调 ``self._miot_client.clear_backoff()``，
    而该对象上没有这个方法，强刷通道静默失效（只打一条 warning）。
    """

    def test_forwards_to_http_client(self):
        called = []

        class _Http:
            def clear_backoff(self, key=None):
                called.append(key)
                return 2

        stub = types.SimpleNamespace(_http_client=_Http())
        assert MIoTClient.clear_backoff(stub) == 2
        assert called == [None]

    def test_tolerates_uninitialized_http_client(self):
        stub = types.SimpleNamespace(_http_client=None)
        assert MIoTClient.clear_backoff(stub) == 0


class TestNonRateLimitErrors:
    def test_4xx_other_than_429_does_not_back_off(self):
        session = _FakeSession([_FakeResponse(400, "bad request")])

        async def scenario():
            client = MIoTHttpClient(cloud_server="cn", access_token="token")
            client._ensure_session = lambda: session  # type: ignore[method-assign]
            client.aes_encrypt_with_b64 = lambda data: "x"  # type: ignore[method-assign]
            post = client._MIoTHttpClient__mihome_api_post_async
            with pytest.raises(MIoTHttpError):
                await post("/app/v2/home/device_list_page", {})
            # 参数错误不该冻结同一接口上的正常请求
            assert client._backoff_for("/app/v2/home/device_list_page").failures == 0

        asyncio.run(scenario())

    def test_business_rate_limit_carries_rate_limited_code(self):
        session = _FakeSession([_FakeResponse(200, "encrypted")])

        async def scenario():
            client = MIoTHttpClient(cloud_server="cn", access_token="token")
            client._ensure_session = lambda: session  # type: ignore[method-assign]
            client.aes_encrypt_with_b64 = lambda data: "x"  # type: ignore[method-assign]
            client.aes_decrypt_with_b64 = lambda s: {  # type: ignore[method-assign]
                "code": -127,
                "wait_second": 5,
            }
            with pytest.raises(MIoTHttpError) as exc:
                await client._MIoTHttpClient__mihome_api_post_async(
                    "/app/v2/homeroom/gethome", {}
                )
            assert exc.value.code is MIoTErrorCode.CODE_HTTP_RATE_LIMITED

        asyncio.run(scenario())

    def test_undecryptable_200_counts_as_failure(self):
        session = _FakeSession([_FakeResponse(200, "<html>portal</html>")])

        async def scenario():
            client = MIoTHttpClient(cloud_server="cn", access_token="token")
            client._ensure_session = lambda: session  # type: ignore[method-assign]
            client.aes_encrypt_with_b64 = lambda data: "x"  # type: ignore[method-assign]
            with pytest.raises(Exception):
                await client._MIoTHttpClient__mihome_api_post_async(
                    "/app/v2/homeroom/gethome", {}
                )
            assert client._backoff_for("/app/v2/homeroom/gethome").failures == 1

        asyncio.run(scenario())


class TestStableAnswersDoNotBackOff:
    """404 是稳定答案（机型没有 spec / 没有图标），不该冻结同接口的其它查询。"""

    def test_urn_404_does_not_back_off(self):
        class _Sess:
            async def get(self, **kw):
                return _FakeResponse(404, "{}")

        async def scenario():
            client = MIoTHttpClient(cloud_server="cn", access_token="t")
            client._ensure_session = lambda: _Sess()  # type: ignore[method-assign]
            urn = await client._MIoTHttpClient__get_urn_by_model_async("x.camera.y")
            assert urn is None
            assert client._backoff_for("urn-by-model").failures == 0

        asyncio.run(scenario())

    def test_icon_404_does_not_back_off_and_is_remembered(self):
        class _Sess:
            def __init__(self):
                self.calls = 0

            async def post(self, **kw):
                self.calls += 1
                return _FakeResponse(404, "")

        session = _Sess()

        async def scenario():
            client = MIoTHttpClient(cloud_server="cn", access_token="t")
            client._ensure_session = lambda: session  # type: ignore[method-assign]
            client.aes_encrypt_with_b64 = lambda d: "x"  # type: ignore[method-assign]
            with pytest.raises(MIoTHttpError):
                await client._MIoTHttpClient__get_device_icon_async("x.camera.y")
            assert client._backoff_for("icon").failures == 0
            assert "x.camera.y" in client._icon_missing
            # 负缓存命中后不再重打
            assert await client._MIoTHttpClient__get_device_icon_batch_async(
                {"x.camera.y"}
            ) == {}
            assert session.calls == 1

        asyncio.run(scenario())

    def test_icon_batch_keeps_cached_icons_while_backed_off(self):
        async def scenario():
            client = MIoTHttpClient(cloud_server="cn", access_token="t")
            client._icon_map["x.camera.y"] = "https://icon"
            client._backoff_for("icon").on_failure(retry_after=300)
            icons = await client._MIoTHttpClient__get_device_icon_batch_async(
                {"x.camera.y", "z.camera.w"}
            )
            # 缓存命中与网络无关，退避期也应照常返回
            assert icons == {"x.camera.y": "https://icon"}

        asyncio.run(scenario())

    def test_clear_backoff_rejects_non_refreshable_key(self):
        async def scenario():
            client = MIoTHttpClient(cloud_server="cn", access_token="t")
            ctl = client._backoff_for("/app/v2/miotspec/prop/set")
            ctl.on_failure(retry_after=300)
            assert client.clear_backoff("/app/v2/miotspec/prop/set") == 0
            assert ctl.remaining() > 0

        asyncio.run(scenario())
