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
