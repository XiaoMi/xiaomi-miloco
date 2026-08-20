# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for `miot.cert.MIoTCert`.

Uses a real MIoTStorage(tmpdir). Synthetic user certs are built with
`cryptography` (self-signed Ed25519 — the remaining-time check only parses the
subject + validity window, not the chain). Covers CA SHA-256 pinning, CSR
subject encoding, remaining-time edge cases (missing / expired / did mismatch),
and key/cert removal keeping the CA.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from miot.cert import MIoTCert
from miot.storage import MIoTStorage

_UID = "123456789"


def _cert(uid, did_hash, *, days_before=1, days_after=30, country="CN", org="Mijia Device"):
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, country),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
            x509.NameAttribute(NameOID.COMMON_NAME, f"mips.{uid}.{did_hash}.2"),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=days_before))
        .not_valid_after(now + timedelta(days=days_after))
        .sign(key, algorithm=None)
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _make(loop=None):
    return MIoTCert(
        storage=MIoTStorage(tempfile.mkdtemp(prefix="cert_"), loop=loop),
        uid=_UID,
        cloud_server="cn",
        loop=loop,
    )


@pytest.mark.asyncio
async def test_verify_ca_writes_and_pins_sha256():
    c = _make()
    # First call writes the bundled CA and its hash must match the pin.
    assert await c.verify_ca_cert_async() is True


@pytest.mark.asyncio
async def test_verify_ca_self_heals_tampered():
    c = _make()
    # Pre-seed a wrong CA blob → hash mismatch → rewritten from the bundled
    # constant on first mismatch, re-verified → True.
    await c._storage.save_file_async(
        domain=MIoTCert.CERT_DOMAIN, name_with_suffix=MIoTCert.CA_NAME, data=b"not-a-ca"
    )
    assert await c.verify_ca_cert_async() is True


@pytest.mark.asyncio
async def test_gen_csr_subject_encoding():
    c = _make()
    key = c.gen_user_key()
    did = "130840348819888211"
    csr_pem = c.gen_user_csr(key, did=did)
    csr = x509.load_pem_x509_csr(csr_pem.encode("utf-8"))
    cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    country = csr.subject.get_attributes_for_oid(NameOID.COUNTRY_NAME)[0].value
    org = csr.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value
    assert cn == f"mips.{_UID}.{_sha1(did)}.2"
    assert country == "CN"
    assert org == "Mijia Device"


@pytest.mark.asyncio
async def test_remaining_time_valid_cert():
    c = _make()
    did = "abc"
    data = _cert(_UID, _sha1(did), days_after=30)
    remaining = await c.user_cert_remaining_time_async(cert_data=data, did=did)
    assert remaining > 0
    assert remaining <= 30 * 24 * 3600


@pytest.mark.asyncio
async def test_remaining_time_missing_cert_is_zero():
    c = _make()
    assert await c.user_cert_remaining_time_async() == 0  # nothing stored


@pytest.mark.asyncio
async def test_remaining_time_expired_is_zero():
    c = _make()
    did = "abc"
    data = _cert(_UID, _sha1(did), days_before=40, days_after=-10)  # already expired
    assert await c.user_cert_remaining_time_async(cert_data=data, did=did) == 0


@pytest.mark.asyncio
async def test_remaining_time_did_mismatch_is_zero():
    c = _make()
    data = _cert(_UID, _sha1("real-did"), days_after=30)
    # Asking about a *different* did → CN mismatch → treated as unusable (0),
    # which forces a re-sign for the new identity.
    assert await c.user_cert_remaining_time_async(cert_data=data, did="other-did") == 0


@pytest.mark.asyncio
async def test_remove_user_key_cert_keeps_ca():
    c = _make()
    await c.verify_ca_cert_async()  # writes CA
    await c.update_user_key_async("KEYDATA")
    await c.update_user_cert_async("CERTDATA")
    assert await c.load_user_key_async() == "KEYDATA"
    assert await c.load_user_cert_async() == "CERTDATA"

    assert await c.remove_user_key_async() is True
    assert await c.remove_user_cert_async() is True
    assert await c.load_user_key_async() is None
    assert await c.load_user_cert_async() is None
    # CA still present / valid.
    assert await c.verify_ca_cert_async() is True


@pytest.mark.asyncio
async def test_user_key_is_not_world_readable(tmp_path):
    """私钥必须 0600:它是本客户端连用户家网关的 mTLS 凭证,同机任何用户读到
    就能冒充这台 miloco 实例下发设备控制指令。MIoTStorage 用裸 open 写文件,
    默认 umask 022 下会落成 0644,所以必须显式收紧。"""
    storage = MIoTStorage(str(tmp_path), loop=None)
    cert = MIoTCert(storage, uid="u1", cloud_server="cn", loop=None)

    assert await cert.update_user_key_async("-----BEGIN PRIVATE KEY-----\nx\n") is True

    mode = os.stat(cert.key_file).st_mode & 0o777
    assert mode == 0o600, f"私钥权限应为 0o600, 实际 {oct(mode)}"


@pytest.mark.asyncio
async def test_user_key_never_world_readable_even_mid_write(tmp_path, monkeypatch):
    """0600 必须在**写入内容之前**就位,不能靠事后 chmod。

    上一版是「裸 open 落 0644 → 回事件循环 → chmod 0600」,首次生成时这中间隔着一次
    executor 往返 + 一次事件循环调度,私钥在这段窗口里是 world-readable —— 多用户主机
    上盯着目录的本地攻击者(inotify/fsevents)刚好够读走它,正是 docstring 声称要防的
    事,而只断言最终模式的测试测不出来。这里在 storage 写内容的那一刻抓一次快照。
    """
    storage = MIoTStorage(str(tmp_path), loop=None)
    cert = MIoTCert(storage, uid="u1", cloud_server="cn", loop=None)

    seen: list[int] = []
    orig_save = storage.save_file_async

    async def spy(*args, **kwargs):
        # 进 save 时文件应已被预创建成 0600
        if os.path.exists(cert.key_file):
            seen.append(os.stat(cert.key_file).st_mode & 0o777)
        return await orig_save(*args, **kwargs)

    monkeypatch.setattr(storage, "save_file_async", spy)

    assert await cert.update_user_key_async("-----BEGIN PRIVATE KEY-----\nx\n") is True
    assert seen == [0o600], f"写入前的权限快照应为 0o600, 实际 {[oct(m) for m in seen]}"
    assert os.stat(cert.key_file).st_mode & 0o777 == 0o600
