# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Regression guard for sub_devices normalization in DeviceInfo/CameraInfo.

Background: devices that carry sub-devices (e.g. 小米智能中控屏 Max,
``xiaomi.controller.oh10p``, matched into the camera allowlist) expose
``MIoTDeviceInfo.sub_devices`` as a ``dict[str, MIoTDeviceInfo]``. After
``model_dump()`` that becomes a dict-of-dict, which failed to validate
against ``DeviceInfo.sub_devices: dict[str, str]`` and raised
ValidationError. The camera discovery loop has no per-device guard, so a
single such device crashed the whole discover pass and silently took every
camera offline for perception.

The ``sub_devices`` before-validator normalizes the MIoT shape to
``{siid: alias}`` so validation no longer crashes. These tests pin the
three branches (dict-of-dict / already-converted / empty) and the
parent-name suffix stripping.
"""

from __future__ import annotations

from miloco.miot.schema import CameraInfo, DeviceInfo, normalize_sub_devices


def test_dict_of_dict_is_normalized_and_does_not_crash():
    """The exact shape from MIoTCameraInfo.model_dump() — the bug trigger."""
    dumped = {
        "did": "1108865025",
        "name": "中控屏Max",
        "model": "xiaomi.controller.oh10p",
        "camera_status": "3",
        "channel_count": 1,
        "sub_devices": {
            "s10": {"did": "1108865025.s10", "name": "开关1-中控屏Max", "online": None},
            "s11": {"did": "1108865025.s11", "name": "开关2-中控屏Max"},
            "s12": {"did": "1108865025.s12", "name": "开关3-中控屏Max"},
        },
    }
    ci = CameraInfo.model_validate(dumped)
    # siid 's' prefix dropped, parent-name suffix stripped.
    assert ci.sub_devices == {"10": "开关1", "11": "开关2", "12": "开关3"}


def test_already_converted_passes_through_without_restripping():
    """service.py pre-converts via build_sub_device_names → {siid: str}."""
    di = DeviceInfo.model_validate(
        {"did": "x", "name": "多路开关", "sub_devices": {"3": "三楼书房"}}
    )
    assert di.sub_devices == {"3": "三楼书房"}


def test_empty_and_missing_normalize_to_none():
    assert (
        CameraInfo.model_validate(
            {"did": "1", "name": "客厅", "sub_devices": {}}
        ).sub_devices
        is None
    )
    assert CameraInfo.model_validate({"did": "y", "name": "z"}).sub_devices is None


def test_non_numeric_siid_keys_are_dropped():
    di = DeviceInfo.model_validate(
        {
            "did": "x",
            "name": "网关",
            "sub_devices": {
                "s2": {"did": "x.s2", "name": "子设备A-网关"},
                "sx": {"did": "x.sx", "name": "脏数据"},
            },
        }
    )
    assert di.sub_devices == {"2": "子设备A"}


def test_normalize_accepts_object_values():
    """normalize_sub_devices also takes raw MIoTDeviceInfo-like objects."""

    class _Sub:
        def __init__(self, name):
            self.name = name

    out = normalize_sub_devices({"s5": _Sub("书房-客厅多路开关")}, "客厅多路开关")
    assert out == {"5": "书房"}
