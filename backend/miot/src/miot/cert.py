# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
Central hub gateway certificate management.

Connecting to a central hub gateway requires mTLS mutual authentication: the
client must hold an Ed25519 certificate signed by Xiaomi. On first connect we
generate a keypair, build a CSR, submit it to the cloud for signing, and
persist the ca/key/cert triplet. The certificate is renewed automatically
before it expires.

Ported from the Xiaomi Home integration `miot_storage.py::MIoTCert`, adapted to
Miloco's ``MIoTStorage`` file API and error types. Central hub gateway control
is only supported in mainland China (see ``SUPPORT_CENTRAL_GATEWAY_CTRL``).
"""

import asyncio
import binascii
import hashlib
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from .const import MIHOME_CA_CERT_SHA256, MIHOME_CA_CERT_STR
from .error import MIoTCertError, MIoTError, MIoTStorageError
from .storage import MIoTStorage

_LOGGER = logging.getLogger(__name__)


class MIoTCert:
    """MIoT central hub gateway certificate file management."""

    CERT_DOMAIN: str = "cert"
    CA_NAME: str = "mihome_ca.cert"

    _storage: MIoTStorage
    _uid: str
    _cloud_server: str
    _key_name: str
    _cert_name: str

    def __init__(
        self,
        storage: MIoTStorage,
        uid: str,
        cloud_server: str,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if not isinstance(storage, MIoTStorage) or not isinstance(uid, str):
            raise MIoTError("invalid params")
        self._loop = loop or asyncio.get_running_loop()
        self._storage = storage
        self._uid = uid
        self._cloud_server = cloud_server
        self._key_name = f"{uid}_{cloud_server}.key"
        self._cert_name = f"{uid}_{cloud_server}.cert"

    @property
    def ca_file(self) -> str:
        """CA certificate file path."""
        return self._storage.gen_storage_path(
            domain=self.CERT_DOMAIN, name_with_suffix=self.CA_NAME
        )

    @property
    def key_file(self) -> str:
        """User private key file path."""
        return self._storage.gen_storage_path(
            domain=self.CERT_DOMAIN, name_with_suffix=self._key_name
        )

    @property
    def cert_file(self) -> str:
        """User certificate file path."""
        return self._storage.gen_storage_path(
            domain=self.CERT_DOMAIN, name_with_suffix=self._cert_name
        )

    async def verify_ca_cert_async(self) -> bool:
        """Verify the integrity of the CA certificate file, writing it if absent."""
        ca_data = await self._storage.load_file_async(
            domain=self.CERT_DOMAIN, name_with_suffix=self.CA_NAME
        )
        if ca_data is None:
            if not await self._storage.save_file_async(
                domain=self.CERT_DOMAIN,
                name_with_suffix=self.CA_NAME,
                data=MIHOME_CA_CERT_STR.encode("utf-8"),
            ):
                raise MIoTStorageError("ca cert save failed")
            ca_data = await self._storage.load_file_async(
                domain=self.CERT_DOMAIN, name_with_suffix=self.CA_NAME
            )
            if ca_data is None:
                raise MIoTStorageError("ca cert load failed")
            _LOGGER.debug("ca cert save success")
        ca_cert_hash = hashlib.sha256(ca_data).digest()
        hash_str = binascii.hexlify(ca_cert_hash).decode("utf-8")
        return hash_str == MIHOME_CA_CERT_SHA256

    async def user_cert_remaining_time_async(
        self, cert_data: Optional[bytes] = None, did: Optional[str] = None
    ) -> int:
        """Return remaining validity of the user certificate in seconds.

        Returns 0 if the certificate is missing, malformed, or expired.
        """
        if cert_data is None:
            cert_data = await self._storage.load_file_async(
                domain=self.CERT_DOMAIN, name_with_suffix=self._cert_name
            )
        if cert_data is None:
            return 0
        try:
            user_cert: x509.Certificate = x509.load_pem_x509_certificate(
                cert_data, default_backend()
            )
            cert_info = {}
            for attribute in user_cert.subject:
                if attribute.oid == NameOID.COMMON_NAME:
                    cert_info["CN"] = attribute.value
                elif attribute.oid == NameOID.COUNTRY_NAME:
                    cert_info["C"] = attribute.value
                elif attribute.oid == NameOID.ORGANIZATION_NAME:
                    cert_info["O"] = attribute.value

            if len(cert_info) != 3:
                raise MIoTCertError("invalid cert info")
            if did and cert_info["CN"] != f"mips.{self._uid}.{self.__did_hash(did)}.2":
                raise MIoTCertError("invalid COMMON_NAME")
            if cert_info.get("C") != "CN":
                raise MIoTCertError("invalid COUNTRY_NAME")
            if cert_info.get("O") != "Mijia Device":
                raise MIoTCertError("invalid ORGANIZATION_NAME")
            now_utc: datetime = datetime.now(timezone.utc)
            if (
                now_utc < user_cert.not_valid_before_utc
                or now_utc > user_cert.not_valid_after_utc
            ):
                raise MIoTCertError("cert is not valid")
            return int((user_cert.not_valid_after_utc - now_utc).total_seconds())
        except (MIoTCertError, ValueError) as error:
            _LOGGER.error(
                "load_pem_x509_certificate failed, %s, %s",
                error,
                traceback.format_exc(),
            )
            return 0

    def gen_user_key(self) -> str:
        """Generate a fresh Ed25519 user private key (PEM/PKCS8)."""
        private_key = ed25519.Ed25519PrivateKey.generate()
        return private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

    def gen_user_csr(self, user_key: str, did: str) -> str:
        """Build a CSR for the user certificate.

        The subject CN encodes the account uid and a SHA1 hash of the virtual
        did (``mips.{uid}.{sha1(did)}.2``). Ed25519 CSRs must be signed with
        ``algorithm=None``.
        """
        private_key = serialization.load_pem_private_key(
            data=user_key.encode("utf-8"), password=None
        )
        did_hash = self.__did_hash(did)
        builder = x509.CertificateSigningRequestBuilder().subject_name(
            x509.Name(
                [
                    # Central hub gateway service is only supported in China.
                    x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Mijia Device"),
                    x509.NameAttribute(
                        NameOID.COMMON_NAME, f"mips.{self._uid}.{did_hash}.2"
                    ),
                ]
            )
        )
        csr = builder.sign(
            private_key, algorithm=None, backend=default_backend()  # type: ignore
        )
        return csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    async def load_user_key_async(self) -> Optional[str]:
        """Load the persisted user private key."""
        data = await self._storage.load_file_async(
            domain=self.CERT_DOMAIN, name_with_suffix=self._key_name
        )
        return data.decode("utf-8") if data else None

    async def update_user_key_async(self, key: str) -> bool:
        """Persist the user private key."""
        return await self._storage.save_file_async(
            domain=self.CERT_DOMAIN,
            name_with_suffix=self._key_name,
            data=key.encode("utf-8"),
        )

    async def load_user_cert_async(self) -> Optional[str]:
        """Load the persisted user certificate."""
        data = await self._storage.load_file_async(
            domain=self.CERT_DOMAIN, name_with_suffix=self._cert_name
        )
        return data.decode("utf-8") if data else None

    async def update_user_cert_async(self, cert: str) -> bool:
        """Persist the user certificate."""
        return await self._storage.save_file_async(
            domain=self.CERT_DOMAIN,
            name_with_suffix=self._cert_name,
            data=cert.encode("utf-8"),
        )

    async def remove_ca_cert_async(self) -> bool:
        """Remove the CA certificate."""
        return await self._storage.remove_file_async(
            domain=self.CERT_DOMAIN, name_with_suffix=self.CA_NAME
        )

    async def remove_user_key_async(self) -> bool:
        """Remove the user private key."""
        return await self._storage.remove_file_async(
            domain=self.CERT_DOMAIN, name_with_suffix=self._key_name
        )

    async def remove_user_cert_async(self) -> bool:
        """Remove the user certificate."""
        return await self._storage.remove_file_async(
            domain=self.CERT_DOMAIN, name_with_suffix=self._cert_name
        )

    def __did_hash(self, did: str) -> str:
        sha1_hash = hashes.Hash(hashes.SHA1(), backend=default_backend())
        sha1_hash.update(did.encode("utf-8"))
        return binascii.hexlify(sha1_hash.finalize()).decode("utf-8")
