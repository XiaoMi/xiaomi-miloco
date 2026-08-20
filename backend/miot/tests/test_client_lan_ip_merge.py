# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""局域网状态合并对 ``local_ip`` 只补充、不清空。

`cloud.py` 从设备列表解析出的 `localip` 全仓只有一个生产消费方：API 出参
`/api/miot/cameras` → `miloco-cli doctor` 的摄像机直连诊断。合并局域网结果时若在
`else` 分支把 `local_ip` 写成 None，那个云端值就永远到不了消费方——键名解析修对了
也等于没修。两条真实触发路径：

  1. 小米摄像机 3 Pro 只应答单播探测，从不出现在局域网设备表里
     （见 knowledge/05-external-deps/sdk-miot.md）；
  2. LAN 客户端未初始化时 `get_devices_async()` 直接返回 `{}`，那一轮**所有**设备
     都走 else。

两种情况下 doctor 都会走 all_missing_ip 分支报 `cameras.all_missing`。
"""

from __future__ import annotations

import pytest
from miot.client import MIoTClient
from miot.types import MIoTDeviceInfo


class _FakeHttp:
    def __init__(self, devices):
        self._devices = devices

    async def get_devices_async(self, home_infos=None, fetch_share_home=False):
        return dict(self._devices)


class _FakeLan:
    def __init__(self, devices=None):
        self._devices = devices or {}

    async def get_devices_async(self):
        return dict(self._devices)


class _LanInfo:
    def __init__(self, ip, online=True):
        self.ip = ip
        self.online = online


def _dev(did, local_ip=None):
    return MIoTDeviceInfo(
        did=did,
        name=f"dev-{did}",
        uid="u1",
        urn="urn:miot-spec-v2:device:camera:0000A01C:xiaomi-x:1",
        model="xiaomi.camera.x",
        manufacturer="xiaomi",
        connect_type=0,
        pid=0,
        token="t",
        online=True,
        voice_ctrl=0,
        order_time=0,
        local_ip=local_ip,
    )


def _client(http, lan):
    c = object.__new__(MIoTClient)  # bypass heavy __init__
    c._http_client = http
    c._lan_client = lan
    c._device_buffer = {}
    return c


@pytest.mark.asyncio
async def test_cloud_local_ip_survives_empty_lan_table():
    """LAN 表为空（客户端未初始化）→ 云端 localip 必须保留。"""
    client = _client(_FakeHttp({"d1": _dev("d1", "192.168.1.152")}), _FakeLan())
    devices = await client.get_devices_async()
    assert devices["d1"].local_ip == "192.168.1.152"
    assert devices["d1"].lan_online is False  # "现在探不到"仍如实表达


@pytest.mark.asyncio
async def test_lan_discovery_overrides_cloud_local_ip():
    """探到就用探到的 IP（更准，反映当前网络实况）。"""
    client = _client(
        _FakeHttp({"d1": _dev("d1", "192.168.1.152")}),
        _FakeLan({"d1": _LanInfo("192.168.1.200", online=True)}),
    )
    devices = await client.get_devices_async()
    assert devices["d1"].local_ip == "192.168.1.200"
    assert devices["d1"].lan_online is True


@pytest.mark.asyncio
async def test_missing_from_lan_table_keeps_cloud_ip_for_other_devices():
    """混合批次：探到的用探到的，没探到的保留云端值，互不影响。"""
    client = _client(
        _FakeHttp(
            {
                "cam3pro": _dev("cam3pro", "192.168.1.152"),  # 只应答单播,不在表里
                "plug": _dev("plug", "192.168.1.9"),
            }
        ),
        _FakeLan({"plug": _LanInfo("192.168.1.10", online=True)}),
    )
    devices = await client.get_devices_async()
    assert devices["cam3pro"].local_ip == "192.168.1.152"
    assert devices["cam3pro"].lan_online is False
    assert devices["plug"].local_ip == "192.168.1.10"
    assert devices["plug"].lan_online is True


@pytest.mark.asyncio
async def test_no_cloud_ip_stays_none():
    """云端也没给 localip → 保持 None，不凭空造值。"""
    client = _client(_FakeHttp({"d1": _dev("d1")}), _FakeLan())
    devices = await client.get_devices_async()
    assert devices["d1"].local_ip is None
