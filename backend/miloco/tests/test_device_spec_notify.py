# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""`notify` 从 SDK 的 spec 解析一路透到 miloco 的 spec entry。

iot 条件项要求属性 access 含 notify（只 read 不 notify 的属性拿不到推送，规则只会
在 seed 那一刻算一次），所以校验方必须拿得到这一位。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.miot.client import MiotProxy
from miot.spec import MIoTSpecDeviceLite


def _proxy_with_spec(lite: dict[str, MIoTSpecDeviceLite]) -> MiotProxy:
    kv_repo = SimpleNamespace(get=lambda *a, **k: None, set=lambda *a, **k: True)
    proxy = MiotProxy(uuid="u", redirect_uri="http://x", kv_repo=kv_repo)
    client = MagicMock()
    client.spec_parser.parse_lite_async = AsyncMock(return_value=lite)
    proxy._miot_client = client
    return proxy


def _lite(iid: str, **overrides) -> MIoTSpecDeviceLite:
    fields = {
        "iid": iid,
        "description": "门状态",
        "format": "uint8",
        "writeable": False,
        "readable": False,
        "notify": False,
    }
    fields.update(overrides)
    return MIoTSpecDeviceLite(**fields)


@pytest.mark.asyncio
async def test_notify_reaches_spec_entry():
    """只有 notify 的属性，entry 里 notify 为真。

    lite 模型漏这个字段时这条会以 pydantic 报错的方式红。
    """
    proxy = _proxy_with_spec({"prop.0.5.1": _lite("prop.0.5.1", notify=True)})

    spec = await proxy._fetch_device_spec("urn:test:door")

    assert spec["prop.5.1"]["notify"] is True


@pytest.mark.asyncio
async def test_read_only_property_reports_notify_false():
    """能读但不推的属性 notify 为假 —— 与上一条一起，把这一位钉成真实的 access 位
    而不是恒真的占位。"""
    proxy = _proxy_with_spec({"prop.0.4.1": _lite("prop.0.4.1", readable=True)})

    spec = await proxy._fetch_device_spec("urn:test:battery")

    assert spec["prop.4.1"]["notify"] is False
