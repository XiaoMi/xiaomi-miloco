# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
Constants.
"""

from typing import List

NICK_NAME_DEFAULT: str = "Xiaomi"
PROJECT_CODE: str = "mico"

# Xiaomi Home HTTP Configuration
MIHOME_HTTP_API_TIMEOUT: int = 30
MIHOME_HTTP_USER_AGENT: str = f"{PROJECT_CODE}/docker"
MIHOME_HTTP_X_CLIENT_BIZID: str = f"{PROJECT_CODE}api"
MIHOME_HTTP_X_ENCRYPT_TYPE: str = "1"
MIHOME_HTTP_API_PUBKEY: str = "\
-----BEGIN PUBLIC KEY-----\
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzH220YGgZOlXJ4eSleFb\
Beylq4qHsVNzhPTUTy/caDb4a3GzqH6SX4GiYRilZZZrjjU2ckkr8GM66muaIuJw\
r8ZB9SSY3Hqwo32tPowpyxobTN1brmqGK146X6JcFWK/QiUYVXZlcHZuMgXLlWyn\
zTMVl2fq7wPbzZwOYFxnSRh8YEnXz6edHAqJqLEqZMP00bNFBGP+yc9xmc7ySSyw\
OgW/muVzfD09P2iWhl3x8N+fBBWpuI5HjvyQuiX8CZg3xpEeCV8weaprxMxR0epM\
3l7T6rJuPXR1D7yhHaEQj2+dyrZTeJO8D8SnOgzV5j4bp1dTunlzBXGYVjqDsRhZ\
qQIDAQAB\
-----END PUBLIC KEY-----"

# Xiaomi OAuth 2.0 Configuration
OAUTH2_CLIENT_ID: str = "2882303761520431603"
OAUTH2_AUTH_URL: str = "https://account.xiaomi.com/oauth2/authorize"
OAUTH2_API_HOST_DEFAULT: str = f"{PROJECT_CODE}.api.mijia.tech"
# Registered in Xiaomi OAuth 2.0 Service
# DO NOT CHANGE UNLESS YOU HAVE AN ADMINISTRATOR PERMISSION
OAUTH2_REDIRECT_URI_LIST: List[str] = [
    "https://127.0.0.1",  # localhost
    f"https://{PROJECT_CODE}.api.mijia.tech/login_redirect",  # Xiaomi official
]

# seconds, 30 days
SPEC_STD_LIB_EFFECTIVE_TIME = 3600 * 24 * 30
# seconds, 30 days
MANUFACTURER_EFFECTIVE_TIME = 3600 * 24 * 30

# MIoT MQTT cloud broker (mips_cloud).
# Broker hostname = f"{cloud_server}-{MIHOME_MQTT_BROKER_HOST_SUFFIX}"
# e.g. "cn-ha.mqtt.io.mi.com".
MIHOME_MQTT_BROKER_HOST_SUFFIX: str = "ha.mqtt.io.mi.com"
MIHOME_MQTT_PORT: int = 8883
MIHOME_MQTT_KEEPALIVE: int = 60
MIHOME_MQTT_SUBSCRIBE_TIMEOUT: float = 10.0
MIHOME_MQTT_RECONNECT_MIN_SEC: float = 1.0
MIHOME_MQTT_RECONNECT_MAX_SEC: float = 120.0

# Camera reconnect interval, seconds
CAMERA_RECONNECT_TIME_MIN: int = 3
CAMERA_RECONNECT_TIME_MAX: int = 1200

# ---------------------------------------------------------------------------
# Local central hub gateway (中枢网关) — mips_local
# ---------------------------------------------------------------------------
# Central-gateway local control is only offered by Xiaomi in mainland China.
# Outside `cn`, we always fall back to the cloud path.
SUPPORT_CENTRAL_GATEWAY_CTRL: List[str] = ["cn"]

# mDNS service type broadcast by the main central hub gateway (role=1).
MIPS_MDNS_TYPE: str = "_miot-central._tcp.local."

# Local gateway MQTT broker (mTLS, MQTT v5). Host/port come from mDNS.
MIPS_LOCAL_PORT_DEFAULT: int = 8883
# Local reconnect backoff is slower than cloud (LAN broker, less churn).
MIPS_LOCAL_RECONNECT_MIN_SEC: float = 6.0
MIPS_LOCAL_RECONNECT_MAX_SEC: float = 60.0
# RPC (rpcReq/get) reply timeout over the local broker. Healthy LAN round-trips
# are ~30ms; a reply that has not arrived in a few seconds means the device is
# unreachable, so fail fast and let the caller fall back to cloud rather than
# stalling the batch. Paired with the per-did local cooldown in MiotProxy.
MIPS_LOCAL_RPC_TIMEOUT: float = 5.0

# User certificate is refreshed this many seconds before it actually expires.
# 3 days, matches the Xiaomi Home integration.
MIHOME_CERT_EXPIRE_MARGIN: int = 3600 * 24 * 3

# Mijia root CA chain used to verify the central hub gateway server cert.
MIHOME_CA_CERT_STR: str = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBazCCAQ+gAwIBAgIEA/UKYDAMBggqhkjOPQQDAgUAMCIxEzARBgNVBAoTCk1p\n"
    "amlhIFJvb3QxCzAJBgNVBAYTAkNOMCAXDTE2MTEyMzAxMzk0NVoYDzIwNjYxMTEx\n"
    "MDEzOTQ1WjAiMRMwEQYDVQQKEwpNaWppYSBSb290MQswCQYDVQQGEwJDTjBZMBMG\n"
    "ByqGSM49AgEGCCqGSM49AwEHA0IABL71iwLa4//4VBqgRI+6xE23xpovqPCxtv96\n"
    "2VHbZij61/Ag6jmi7oZ/3Xg/3C+whglcwoUEE6KALGJ9vccV9PmjLzAtMAwGA1Ud\n"
    "EwQFMAMBAf8wHQYDVR0OBBYEFJa3onw5sblmM6n40QmyAGDI5sURMAwGCCqGSM49\n"
    "BAMCBQADSAAwRQIgchciK9h6tZmfrP8Ka6KziQ4Lv3hKfrHtAZXMHPda4IYCIQCG\n"
    "az93ggFcbrG9u2wixjx1HKW4DUA5NXZG0wWQTpJTbQ==\n"
    "-----END CERTIFICATE-----\n"
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBjzCCATWgAwIBAgIBATAKBggqhkjOPQQDAjAiMRMwEQYDVQQKEwpNaWppYSBS\n"
    "b290MQswCQYDVQQGEwJDTjAgFw0yMjA2MDkxNDE0MThaGA8yMDcyMDUyNzE0MTQx\n"
    "OFowLDELMAkGA1UEBhMCQ04xHTAbBgNVBAoMFE1JT1QgQ0VOVFJBTCBHQVRFV0FZ\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEdYrzbnp/0x/cZLZnuEDXTFf8mhj4\n"
    "CVpZPwgj9e9Ve5r3K7zvu8Jjj7JF1JjQYvEC6yhp1SzBgglnK4L8xQzdiqNQME4w\n"
    "HQYDVR0OBBYEFCf9+YBU7pXDs6K6CAQPRhlGJ+cuMB8GA1UdIwQYMBaAFJa3onw5\n"
    "sblmM6n40QmyAGDI5sURMAwGA1UdEwQFMAMBAf8wCgYIKoZIzj0EAwIDSAAwRQIh\n"
    "AKUv+c8v98vypkGMTzMwckGjjVqTef8xodsy6PhcSCq+AiA/n9mDs62hAo5zXyJy\n"
    "Bs1s7mqXPf1XgieoxIvs1MqyiA==\n"
    "-----END CERTIFICATE-----\n"
)
MIHOME_CA_CERT_SHA256: str = (
    "8b7bf306be3632e08b0ead308249e5f2b2520dc921ad143872d5fcc7c68d6759"
)

CLOUD_SERVER_DEFAULT: str = "cn"
CLOUD_SERVERS: dict = {
    "cn": "中国大陆",
    "de": "Europe",
    "i2": "India",
    "ru": "Russia",
    "sg": "Singapore",
    "us": "United States",
}

SYSTEM_LANGUAGE_DEFAULT: str = "zh-Hans"
SYSTEM_LANGUAGES = {
    "de": "Deutsch",
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "it": "Italiano",
    "ja": "日本語",
    "ru": "Русский",
    "zh-Hans": "简体中文",
    "zh-Hant": "繁體中文",
}
