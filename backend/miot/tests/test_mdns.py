# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unit test for miot.mdns.py.
"""

import asyncio
import logging

import pytest
from miot.mdns import MdnsService

_LOGGER = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_mdns_lifecycle() -> None:
    """init → 短暂运行(发一轮查询)→ deinit 的生命周期冒烟。

    不断言发现结果(CI 无网关),只验证起停不抛异常、get_services 返回 dict。
    （原实现是 ``while True: sleep(1)`` 死循环，会永久挂起整套 backend 测试。）
    """
    mdns = MdnsService()
    await mdns.init_async()
    try:
        await asyncio.sleep(0.5)  # 让启动突发查询发出去
        services = mdns.get_services()
        assert isinstance(services, dict)
    finally:
        await mdns.deinit_async()
