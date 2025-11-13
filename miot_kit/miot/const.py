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
    "https://127.0.0.1",                                        # localhost
    f"https://{PROJECT_CODE}.api.mijia.tech/login_redirect",    # Xiaomi official
]

# seconds, 30 days
SPEC_STD_LIB_EFFECTIVE_TIME = 3600*24*30
# seconds, 30 days
MANUFACTURER_EFFECTIVE_TIME = 3600*24*30

# Camera reconnect interval, seconds
CAMERA_RECONNECT_TIME_MIN: int = 3
CAMERA_RECONNECT_TIME_MAX: int = 1200

CLOUD_SERVER_DEFAULT: str = "cn"
CLOUD_SERVERS: dict = {
    "cn": "中国大陆",
    "de": "Europe",
    "i2": "India",
    "ru": "Russia",
    "sg": "Singapore",
    "us": "United States"
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
    "zh-Hant": "繁體中文"
}
