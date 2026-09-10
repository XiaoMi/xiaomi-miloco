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
    MIoTSpecTypeLevel,
    _urn_type_name,
)
from miot.storage import MIoTStorage

_LOGGER = logging.getLogger(__name__)


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


@pytest.mark.asyncio
async def test_parse_lite_carries_notify(monkeypatch):
    """access 里的 notify 要跟着进 lite 模型。

    只 read 不 notify 的属性拿不到推送，消费方靠这一位区分「会自己更新」和「开机
    写过一次就冻住」，构造处漏传时这一位恒假、两类属性长得一模一样。
    """
    from miot.spec import (
        MIoTSpecDevice,
        MIoTSpecProperty,
        MIoTSpecService,
    )

    def _prop(iid: int, name: str, access: list[str]) -> MIoTSpecProperty:
        return MIoTSpecProperty(
            iid=iid,
            name=name,
            type=f"urn:miot-spec-v2:property:{name}:0000000{iid}:vendor:1",
            description=name,
            description_trans=name,
            format="uint8",
            access=access,
        )

    device = MIoTSpecDevice(
        urn="urn:miot-spec-v2:device:lock:0000A00E:vendor:1",
        name="lock",
        description="lock",
        description_trans="门锁",
        services=[
            MIoTSpecService(
                iid=5,
                name="door",
                type="urn:miot-spec-v2:service:door:00007830:vendor:1",
                description="door",
                description_trans="门",
                properties=[
                    _prop(1, "door-status", ["notify"]),
                    _prop(2, "battery", ["read"]),
                ],
            )
        ],
    )

    parser = MIoTSpecParser.__new__(MIoTSpecParser)

    async def _fake_parse(urn: str, skip_cache: bool = False):
        return device

    monkeypatch.setattr(parser, "parse_async", _fake_parse, raising=False)

    # 与 miloco 侧同一组参数：三级都放宽到 UNKNOWN，否则模板未命中的属性被整批滤掉
    lite = await parser.parse_lite_async(
        urn=device.urn,
        spec_service_level=MIoTSpecTypeLevel.UNKNOWN,
        spec_property_level=MIoTSpecTypeLevel.UNKNOWN,
        spec_action_level=MIoTSpecTypeLevel.UNKNOWN,
    )

    assert lite["prop.0.5.1"].notify is True
    assert lite["prop.0.5.2"].notify is False
