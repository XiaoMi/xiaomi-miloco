# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""miot-spec.org 的请求保护。

spec 解析失败时旧实现会立即重试 3 次，且不记失败——对端故障时每次操作都会
再打 3 次。这里钉住：重试带退避、失败进负缓存、强刷绕过负缓存；以及第三方
host 的滑动窗口配额（窗口内允许尖峰、长期限速）。
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from miot.spec import MIoTSpecParser

from miot import common

pytestmark = pytest.mark.unit


def _parser():
    parser = MIoTSpecParser.__new__(MIoTSpecParser)
    parser._init_done = True
    parser._failed_until = {}
    parser._lang = "zh-Hans"
    parser._storage = None
    parser._MIoTSpecParser__cache_get = AsyncMock(return_value=None)
    return parser


class TestParseRetry:
    def test_retries_with_backoff_then_negative_cache(self, monkeypatch):
        parser = _parser()
        parse = AsyncMock(side_effect=RuntimeError("boom"))
        parser._MIoTSpecParser__parse = parse
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr("miot.spec.asyncio.sleep", fake_sleep)

        assert asyncio.run(parser.parse_async("urn:test")) is None
        assert parse.await_count == 3
        assert len(delays) == 2  # 3 次尝试之间只等 2 次

        # 负缓存：TTL 内不再联网
        assert asyncio.run(parser.parse_async("urn:test")) is None
        assert parse.await_count == 3

        # 强制刷新绕过负缓存
        assert asyncio.run(parser.parse_async("urn:test", skip_cache=True)) is None
        assert parse.await_count == 6


class TestRateLimit:
    def test_unlimited_host_is_noop(self, monkeypatch):
        calls = []

        async def fake_sleep(delay):
            calls.append(delay)

        monkeypatch.setattr("miot.common.asyncio.sleep", fake_sleep)
        asyncio.run(common._acquire_rate_slot("https://api.example.com/x"))
        assert calls == []

    def test_limited_host_allows_burst_then_waits(self, monkeypatch):
        host = "test.local"
        # 10 分钟窗口、每 60 秒一个桶，最多 3 次：窗口内可一次跑完（尖峰），
        # 第 4 次等最老的分钟桶滑出窗口
        monkeypatch.setitem(common._RATE_LIMIT_HOSTS, host, (600.0, 3))
        common._rate_windows.clear()
        common._rate_locks.clear()

        clock = [0.0]
        monkeypatch.setattr("miot.common.time.monotonic", lambda: clock[0])

        async def fake_sleep(delay):
            clock[0] += delay

        monkeypatch.setattr("miot.common.asyncio.sleep", fake_sleep)

        async def scenario():
            for _ in range(3):
                await common._acquire_rate_slot(f"https://{host}/x")
            assert clock[0] == 0.0  # 配额内，全部立即放行
            await common._acquire_rate_slot(f"https://{host}/x")
            assert clock[0] >= 599.99  # 等第 0 分钟的桶滑出窗口

        asyncio.run(scenario())
