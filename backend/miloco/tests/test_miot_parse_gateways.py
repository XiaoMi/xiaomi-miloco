# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for MiotProxy._parse_gateways — config 'ip' / 'ip:port' parsing.

Pure static method; exercises the boundary cases (default port, explicit port,
whitespace, empty, bad port, and IPv6 which is currently unsupported but must
degrade gracefully rather than crash).
"""

from __future__ import annotations

from miloco.miot.client import MiotProxy

parse = MiotProxy._parse_gateways


def test_ip_only_defaults_port_8883():
    assert parse(["192.168.1.5"]) == [("192.168.1.5", 8883)]


def test_ip_with_explicit_port():
    assert parse(["192.168.1.5:1883"]) == [("192.168.1.5", 1883)]


def test_whitespace_trimmed():
    assert parse(["  192.168.1.5 : 8883 "]) == [("192.168.1.5", 8883)]


def test_empty_and_blank_skipped():
    assert parse(["", "   ", None if False else " "]) == []


def test_bad_port_skipped():
    assert parse(["host:notaport"]) == []


def test_missing_host_skipped():
    assert parse([":8883"]) == []


def test_mixed_keeps_valid_only():
    assert parse(
        ["10.0.0.1", "10.0.0.2:1234", "bad:x", "", ":80"]
    ) == [("10.0.0.1", 8883), ("10.0.0.2", 1234)]


def test_none_input():
    assert parse(None) == []


def test_ipv6_unsupported_but_graceful():
    # IPv6 is not supported by the 'host:port' partition scheme; it must be
    # skipped gracefully (no exception), not crash. Documents the limitation.
    assert parse(["::1"]) == []            # bare IPv6 → host parses empty → skip
    assert parse(["[::1]:8883"]) == []     # bracketed → bad port token → skip
    # A valid IPv4 alongside an IPv6 entry still yields the IPv4 one.
    assert parse(["::1", "192.168.1.9"]) == [("192.168.1.9", 8883)]


def test_port_out_of_range_is_skipped():
    """端口越界与非整数端口同样处理（skip + warning），与 CLI 侧口径一致。

    CLI 的 _validate_gateway 只拦 `config set` 写入的值；手工编辑 config.json
    绕过 CLI 时，只有这里能挡住 `:0` / `:99999`，否则会在建连接时抛一个跟配置
    错误毫无关系的底层报错。
    """
    assert parse(["10.0.0.1:0"]) == []
    assert parse(["10.0.0.1:65536"]) == []
    assert parse(["10.0.0.1:99999"]) == []
    assert parse(["10.0.0.1:-1"]) == []
    # 边界值有效
    assert parse(["10.0.0.1:1"]) == [("10.0.0.1", 1)]
    assert parse(["10.0.0.1:65535"]) == [("10.0.0.1", 65535)]
    # 坏项被跳过不影响同批好项
    assert parse(["10.0.0.1:0", "10.0.0.2:8883"]) == [("10.0.0.2", 8883)]


def test_default_port_comes_from_sdk_constant():
    """默认端口必须取 SDK 常量，不能是字面量。

    mDNS 自动发现的网关默认端口取自 ``MIPS_LOCAL_PORT_DEFAULT``（经
    ``MipsLocalClient.__init__``）。这里若写字面量，将来中枢换端口时自动发现的网关
    跟着走新端口、而配置里写成裸 IP 的静态网关仍连 8883，表现是「静态网关连不上、
    自动发现的能连上」，日志里只有一个跟端口无关的 ConnectionRefusedError。
    """
    from miot.const import MIPS_LOCAL_PORT_DEFAULT

    assert MiotProxy._parse_gateways(["10.0.0.9"]) == [
        ("10.0.0.9", MIPS_LOCAL_PORT_DEFAULT)
    ]
