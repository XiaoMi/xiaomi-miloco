# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for the pure-Python legacy-unicast mDNS discovery (miot.mdns).

No network: the query builder, DNS response parser (name compression + SRV /
TXT / A extraction), MipsServiceData.from_raw, and the end-to-end ingest are
exercised against two REAL captured ``_miot-central`` response packets.
"""

from __future__ import annotations

import base64
import struct

import pytest
from miot.mdns import (
    MIPS_MDNS_TYPE,
    MdnsService,
    MdnsServiceError,
    MdnsServiceState,
    MipsServiceData,
)

# Real mDNS responses captured on-device (two main gateways).
PKT_4399 = base64.b64decode(
    "EjSEAAABAAEAAAAEDV9taW90LWNlbnRyYWwEX3RjcAVsb2NhbAAADAABwAwADAABAAAACgAb"
    "GHhpYW9taS1nYXRld2F5LWh1YjEtNDM5OcAMwDYAIQABAAAACgAQAAAAACKzB0FuZHJvaWTA"
    "H8A2ABAAAQAAAAoAKShwcm9maWxlPUZBQUFBQUJDRWtnL0Jvd3JmK3VEOGpzZ0FBQVJBQUk9"
    "wGMAAQABAAAACgAEwKgBmMBjABwAAQAAAAoAEP6AAAAAAAAAUogR//75678="
)
PKT_0855 = base64.b64decode(
    "EjSEAAABAAEAAAAEDV9taW90LWNlbnRyYWwEX3RjcAVsb2NhbAAADAABwAwADAABAAAACgAb"
    "GHhpYW9taS1nYXRld2F5LWh1YjEtMDg1NcAMwDYAIQABAAAACgANAAAAACKzBG9oMXDAH8A2"
    "ABAAAQAAAAoAKShwcm9maWxlPUJBQUFBQUEzNk1IWDdvMk5UVGppRzg0UEFBQVNBQUk9wGMA"
    "AQABAAAACgAEwKgBgMBjABwAAQAAAAoAEP6AAAAAAAAAkvtd//4+TwM="
)


# --------------------------------------------------------------- query build

def test_build_query_is_ptr_for_service_type():
    q = MdnsService._build_query()
    qd = struct.unpack(">H", q[4:6])[0]
    assert qd == 1
    name, off = MdnsService._read_name(q, 12)
    assert name == "_miot-central._tcp.local"
    qtype, qclass = struct.unpack(">HH", q[off:off + 4])
    assert qtype == 12 and qclass == 1  # PTR / IN


# --------------------------------------------------------------- parse response

def test_parse_response_4399():
    svcs = MdnsService._parse_response(PKT_4399)
    assert len(svcs) == 1
    s = svcs[0]
    assert s["instance"].startswith("xiaomi-gateway-hub1-4399")
    assert s["host"] == "Android.local"
    assert s["port"] == 8883
    assert s["ip"] == "192.168.1.152"
    assert s["profile"] == "FAAAAABCEkg/Bowrf+uD8jsgAAARAAI="


def test_parse_response_0855():
    s = MdnsService._parse_response(PKT_0855)[0]
    assert s["host"] == "oh1p.local"
    assert s["port"] == 8883
    assert s["ip"] == "192.168.1.128"
    assert s["profile"] == "BAAAAAA36MHX7o2NTTjiG84PAAASAAI="


def test_parse_response_garbage_is_empty():
    assert MdnsService._parse_response(b"\x00\x01\x02") == []


# --------------------------------------------------------------- from_raw

def test_from_raw_parses_profile():
    s = MdnsService._parse_response(PKT_4399)[0]
    sd = MipsServiceData.from_raw(
        name=s["instance"], type_=MIPS_MDNS_TYPE, server=s["host"],
        addresses=[s["ip"]], port=s["port"], profile=s["profile"],
    )
    assert sd.group_id == "3bf283eb7f2b8c06"
    assert sd.role == 1 and sd.suite_mqtt is True and sd.valid_service()
    assert sd.did.isdigit()


def test_from_raw_rejects_bad_input():
    with pytest.raises(MdnsServiceError):
        MipsServiceData.from_raw(name="x", type_=MIPS_MDNS_TYPE, server="h",
                                 addresses=["1.2.3.4"], port=8883, profile="")
    with pytest.raises(MdnsServiceError):
        MipsServiceData.from_raw(name="x", type_=MIPS_MDNS_TYPE, server="h",
                                 addresses=[], port=8883,
                                 profile="FAAAAABCEkg/Bowrf+uD8jsgAAARAAI=")
    with pytest.raises(MdnsServiceError):
        MipsServiceData.from_raw(name="x", type_=MIPS_MDNS_TYPE, server="h",
                                 addresses=["1.2.3.4"], port=0,
                                 profile="FAAAAABCEkg/Bowrf+uD8jsgAAARAAI=")


# --------------------------------------------------------------- end-to-end ingest

@pytest.mark.asyncio
async def test_handle_packet_ingests_and_fires_added():
    m = MdnsService()
    seen = []

    async def on_change(group_id, state, data):
        seen.append((group_id, state, data))

    m.sub_service_change("t", "*", on_change)
    # feed a real packet through the private handler
    m._MdnsService__handle_packet(PKT_4399)
    import asyncio
    await asyncio.sleep(0)  # let the dispatched task run
    svcs = m.get_services()
    assert "3bf283eb7f2b8c06" in svcs
    assert svcs["3bf283eb7f2b8c06"]["addresses"] == ["192.168.1.152"]
    assert len(seen) == 1
    gid, state, data = seen[0]
    assert gid == "3bf283eb7f2b8c06" and state == MdnsServiceState.ADDED


class _NI:
    def __init__(self, name, ip, netmask="255.255.255.0"):
        self.name = name
        self.ip = ip
        self.netmask = netmask


class _FakeNetwork:
    def __init__(self, infos):
        self.network_info = {ni.name: ni for ni in infos}


@pytest.mark.asyncio
async def test_interface_addrs_filters_loopback_and_linklocal():
    net = _FakeNetwork([
        _NI("lo0", "127.0.0.1"),
        _NI("en1", "192.168.1.239"),
        _NI("bridge100", "192.168.64.1"),
        _NI("en9", "169.254.10.2"),
    ])
    m = MdnsService(network=net)
    addrs = m._MdnsService__interface_addrs()
    names = {n for n, _, _ in addrs}
    assert names == {"en1", "bridge100"}
    assert all(not ip.startswith(("127.", "169.254.")) for _, ip, _ in addrs)


@pytest.mark.asyncio
async def test_interface_addrs_empty_without_network():
    assert MdnsService()._MdnsService__interface_addrs() == []


def test_subnet_broadcast():
    sb = MdnsService._MdnsService__subnet_broadcast
    assert sb("192.168.1.239", "255.255.255.0") == "192.168.1.255"
    assert sb("10.0.5.7", "255.255.0.0") == "10.0.255.255"
    assert sb("bad", "255.255.255.0") is None


@pytest.mark.asyncio
async def test_handle_packet_dedup_no_duplicate_added():
    m = MdnsService()
    seen = []

    async def on_change(group_id, state, data):
        seen.append(state)

    m.sub_service_change("t", "*", on_change)
    m._MdnsService__handle_packet(PKT_0855)
    m._MdnsService__handle_packet(PKT_0855)  # identical → no second ADDED
    import asyncio
    await asyncio.sleep(0)
    assert seen == [MdnsServiceState.ADDED]
