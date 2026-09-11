# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unit test miot spec.
"""

import logging

import pytest
import yaml
from miot.spec import (
    MIoTSpecDeviceLite,
    MIoTSpecLiteActionParam,
    MIoTSpecParser,
    MIoTSpecTypeClass,
    _urn_type_name,
)
from miot.storage import MIoTStorage

_LOGGER = logging.getLogger(__name__)


@pytest.mark.unit
def test_urn_type_name():
    """type_name lives at URN segment[3]."""
    assert (
        _urn_type_name("urn:miot-spec-v2:property:on:00000006:vendor:1") == "on"
    )
    assert (
        _urn_type_name(
            "urn:miot-spec-v2:property:color-temperature:0000000F:vendor:1"
        )
        == "color-temperature"
    )
    assert _urn_type_name("urn:miot-spec-v2:action:turn-on:00000003:v:1") == "turn-on"
    assert (
        _urn_type_name("urn:miot-spec-v2:service:light:00007802:vendor:1")
        == "light"
    )
    assert _urn_type_name("") is None
    assert _urn_type_name("urn:miot-spec-v2:property") is None


@pytest.mark.unit
def test_lite_action_param_roundtrip():
    """MIoTSpecDeviceLite carries structured action input parameters."""
    lite = MIoTSpecDeviceLite(
        iid="action.0.5.1",
        description="speaker play-text",
        format="[]",
        writeable=True,
        readable=False,
        type_name="play-text",
        service_type_name="speaker",
        service_description="speaker",
        in_params=[MIoTSpecLiteActionParam(name="text", format="string")],
    )
    assert lite.type_name == "play-text"
    assert lite.service_type_name == "speaker"
    assert lite.in_params is not None
    assert lite.in_params[0].name == "text"
    assert lite.in_params[0].format == "string"


@pytest.mark.asyncio
async def test_spec(
    test_cache_path: str,
):
    """Test miot spec."""
    # 不打 unit：要向云端 spec 接口取 urn 定义。同理下面的 test_spec_type 还会
    # 往当前工作目录写 types_default.yaml。故本文件按用例打标而非模块级。
    miot_storage = MIoTStorage(root_path=test_cache_path)

    spec_parser = MIoTSpecParser(storage=miot_storage, lang="zh-Hans")
    await spec_parser.init_async()

    spec1 = await spec_parser.parse_async(
        urn="urn:miot-spec-v2:device:nas:0000A0E6:xiaomi-rp05:1"
    )
    assert spec1 is not None

    # _LOGGER.info('spec1: %s', spec1)
    _LOGGER.info("spec1: %s", spec1.model_dump_json(by_alias=True, exclude_none=True))


@pytest.mark.asyncio
async def test_spec_type(test_cache_path: str):
    """Test miot spec type."""
    miot_storage = MIoTStorage(root_path=test_cache_path)

    spec_type = MIoTSpecTypeClass(storage=miot_storage)
    await spec_type.init_async()

    with open("./types_default.yaml", "w", encoding="utf-8") as f:
        yaml.dump(spec_type.data.model_dump(by_alias=True), f, allow_unicode=True)
    # _LOGGER.info('device_types: %s', spec_type.data.model_dump_json(by_alias=True))
