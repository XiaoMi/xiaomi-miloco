# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for the MIPS TLV envelope (`miot.mips_local._MipsMessage`).

Pure encode/decode — no MQTT / network. Guards the wire format the central
hub gateway speaks: each field is ``len(4B LE) + type(1B) + data``; for string
fields ``len`` includes a trailing NUL (``len(str) + 1``).
"""

from __future__ import annotations

import struct

from miot.mips_local import _MipsMessage, _MipsMsgType


def test_pack_unpack_round_trip_all_fields():
    packed = _MipsMessage.pack(
        mid=12345,
        payload='{"hello":"world"}',
        msg_from="local",
        ret_topic="1899/reply",
    )
    msg = _MipsMessage.unpack(packed)
    assert msg.mid == 12345
    assert msg.msg_from == "local"
    assert msg.ret_topic == "1899/reply"
    assert msg.payload == '{"hello":"world"}'


def test_pack_unpack_payload_only():
    # msg_from / ret_topic are optional; only mid + payload are always present.
    packed = _MipsMessage.pack(mid=7, payload="{}")
    msg = _MipsMessage.unpack(packed)
    assert msg.mid == 7
    assert msg.payload == "{}"
    assert msg.msg_from is None
    assert msg.ret_topic is None


def test_pack_string_field_length_includes_nul():
    # A string field's declared length is len(str)+1 (trailing NUL). The mid
    # field is a fixed 4-byte body, so the first field is always mid.
    payload = "abc"
    packed = _MipsMessage.pack(mid=1, payload=payload)
    # field 0: mid  -> len=4, type=0
    length0, type0 = struct.unpack("<IB", packed[:5])
    assert (length0, type0) == (4, _MipsMsgType.ID.value)
    # field 1: payload -> len=len("abc")+1=4, type=2
    off = 5 + length0
    length1, type1 = struct.unpack("<IB", packed[off : off + 5])
    assert type1 == _MipsMsgType.PAYLOAD.value
    assert length1 == len(payload) + 1


def test_unpack_strips_trailing_nul():
    packed = _MipsMessage.pack(mid=2, payload="x", ret_topic="t")
    msg = _MipsMessage.unpack(packed)
    # No stray NUL bytes leak into decoded strings.
    assert msg.payload == "x"
    assert msg.ret_topic == "t"
    assert "\x00" not in (msg.payload or "")
    assert "\x00" not in (msg.ret_topic or "")


def test_unpack_unicode_payload():
    payload = '{"名字":"客厅灯"}'
    packed = _MipsMessage.pack(mid=99, payload=payload)
    msg = _MipsMessage.unpack(packed)
    assert msg.payload == payload
