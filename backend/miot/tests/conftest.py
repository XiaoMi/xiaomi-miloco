# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Pytest fixtures.
"""

import logging
import random
from os import environ, makedirs, path
from uuid import uuid4

import pytest
from miot.const import PROJECT_CODE

## ------MIoT------ ##
TEST_ROOT_PATH: str = path.dirname(path.abspath(__file__))
TEST_CACHE_PATH: str = path.join(TEST_ROOT_PATH, ".test_cache")
TEST_OAUTH2_REDIRECT_URI: str = f"https://{PROJECT_CODE}.api.mijia.tech/login_redirect"
TEST_LANG: str = "zh-Hans"
TEST_UID: str = "123456789"
TEST_CLOUD_SERVER: str = "cn"  # cn, de, i2, ru, sg, us
DOMAIN_CLOUD_CACHE: str = "cloud_cache"

## ------Home Assistant------ ##
DOMAIN_HA_CACHE: str = "ha_cache"
TEST_HA_URL: str = "http://10.189.144.201:8123"
TEST_HA_TOKEN: str = environ.get("TEST_HA_TOKEN") or "<Your Home Assistant Token>"
TEST_HA_OAUTH2_REDIRECT_URI: str = (
    f"https://{PROJECT_CODE}.api.mijia.tech/login_redirect"
)

## ------Baidu------ ##
DOMAIN_BAIDU_CACHE: str = "baidu_cache"
TEST_BAIDU_OAUTH2_BASE_URL: str = "https://openapi.baidu.com/oauth/2.0"
TEST_BAIDU_OAUTH2_CLIENT_ID: str = (
    environ.get("TEST_BAIDU_OAUTH2_CLIENT_ID") or "<Your Client Id>"
)
TEST_BAIDU_OAUTH2_CLIENT_SECRET: str = (
    environ.get("TEST_BAIDU_OAUTH2_CLIENT_SECRET") or "<Your Client Secret>"
)
TEST_BAIDU_OAUTH2_REDIRECT_URI: str = (
    f"https://{PROJECT_CODE}.api.mijia.tech/login_redirect"
)


_LOGGER = logging.getLogger(__name__)


def pytest_configure(config):
    """注册 ``unit`` marker。

    注册点放这里而不是 ``backend/pyproject.toml``:该 marker 只被本目录使用,
    这里才是它的作用域;而 ``miot/tests/`` 整个目录被根配置的 ``norecursedirs``
    排除,只有显式 ``pytest miot/tests/`` 才会加载本 conftest —— 正是 CI 的
    ``miot 单元测试`` 步骤（``-m unit``）的调用方式。

    为什么需要 ``-m unit`` 而不是直接跑整个目录:本目录多数用例是历史遗留的
    联调脚本,依赖真实凭据与内网地址（见上面的 ``TEST_HA_TOKEN`` /
    ``TEST_HA_URL`` / Baidu client secret），在 CI 里跑不起来。新增的纯 mock
    用例靠 ``unit`` marker 挑出来单独进门禁。
    """
    config.addinivalue_line(
        "markers",
        "unit: 纯 mock、无真实网络/凭据依赖的单元测试；CI 的 "
        "`miot 单元测试` 步骤按此 marker 选择",
    )


@pytest.fixture(scope="session", autouse=True)
def set_logger():
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    _LOGGER.info("set logger, %s", logger)


@pytest.fixture(scope="session")
def test_root_path() -> str:
    return TEST_ROOT_PATH


@pytest.fixture(scope="session")
def test_cache_path() -> str:
    makedirs(TEST_CACHE_PATH, exist_ok=True)
    return TEST_CACHE_PATH


@pytest.fixture(scope="session")
def test_oauth2_redirect_uri() -> str:
    return TEST_OAUTH2_REDIRECT_URI


@pytest.fixture(scope="session")
def test_lang() -> str:
    return TEST_LANG


@pytest.fixture(scope="session")
def test_uid() -> str:
    return TEST_UID


@pytest.fixture(scope="session")
def test_random_did() -> str:
    # Gen random did
    return str(random.getrandbits(64))


@pytest.fixture(scope="session")
def test_uuid() -> str:
    # Gen uuid
    return uuid4().hex


@pytest.fixture(scope="session")
def test_cloud_server() -> str:
    return TEST_CLOUD_SERVER


@pytest.fixture(scope="session")
def test_domain_cloud_cache() -> str:
    return DOMAIN_CLOUD_CACHE


@pytest.fixture(scope="session")
def test_name_oauth2_info() -> str:
    return f"{TEST_CLOUD_SERVER}_oauth2_info"


@pytest.fixture(scope="session")
def test_name_user_info() -> str:
    return f"{TEST_CLOUD_SERVER}_user_info"


@pytest.fixture(scope="session")
def test_name_uuid() -> str:
    return f"{TEST_CLOUD_SERVER}_uuid"


@pytest.fixture(scope="session")
def test_name_rd_did() -> str:
    return f"{TEST_CLOUD_SERVER}_rd_did"


@pytest.fixture(scope="session")
def test_name_homes() -> str:
    return f"{TEST_CLOUD_SERVER}_homes"


@pytest.fixture(scope="session")
def test_name_devices() -> str:
    return f"{TEST_CLOUD_SERVER}_devices"


@pytest.fixture(scope="session")
def test_name_cameras() -> str:
    return f"{TEST_CLOUD_SERVER}_cameras"


@pytest.fixture(scope="session")
def test_name_manual_scene_list() -> str:
    return f"{TEST_CLOUD_SERVER}_manual_scene_list"


## ------Home Assistant------ ##


@pytest.fixture(scope="session")
def test_domain_ha_cache() -> str:
    return DOMAIN_HA_CACHE


@pytest.fixture(scope="session")
def test_ha_url() -> str:
    return TEST_HA_URL


@pytest.fixture(scope="session")
def test_ha_token() -> str:
    return TEST_HA_TOKEN


@pytest.fixture(scope="session")
def test_ha_oauth2_client_id() -> str:
    return TEST_HA_OAUTH2_REDIRECT_URI


@pytest.fixture(scope="session")
def test_ha_oauth2_redirect_uri() -> str:
    return TEST_HA_OAUTH2_REDIRECT_URI


@pytest.fixture(scope="session")
def test_name_ha_oauth_info() -> str:
    return "ha_oauth_info"


@pytest.fixture(scope="session")
def test_name_ha_states() -> str:
    return "ha_states"


@pytest.fixture(scope="session")
def test_name_ha_automations() -> str:
    return "ha_automations"


## ------Baidu------ ##
@pytest.fixture(scope="session")
def test_domain_baidu_cache() -> str:
    return DOMAIN_BAIDU_CACHE


@pytest.fixture(scope="session")
def test_baidu_oauth2_base_url() -> str:
    return TEST_BAIDU_OAUTH2_BASE_URL


@pytest.fixture(scope="session")
def test_baidu_oauth2_client_id() -> str:
    return TEST_BAIDU_OAUTH2_CLIENT_ID


@pytest.fixture(scope="session")
def test_baidu_oauth2_client_secret() -> str:
    return TEST_BAIDU_OAUTH2_CLIENT_SECRET


@pytest.fixture(scope="session")
def test_baidu_oauth2_redirect_uri() -> str:
    return TEST_BAIDU_OAUTH2_REDIRECT_URI


@pytest.fixture(scope="session")
def test_name_baidu_oauth_info() -> str:
    return "baidu_oauth_info"
